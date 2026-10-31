"""Read the existing native message projection in one actual DOM snapshot.

The current native renderer and every row field remain authoritative. Before
the first optimized read of each operation, compare two snapshots against the
original reader. No send, assistant output, attachment, or completion is made
by this adapter.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import re
import time
import uuid


FOOTER = ":scope > div.flex.items-center[class~='-mx-1.5'] > div.flex.items-center[class~='gap-0.5']"


class NativeTurnBatch:
    def __init__(self, bridge, native, archive_root=None):
        self.bridge, self.native = bridge, native
        self.original_turns, self.original_visible = bridge._turns, bridge._visible
        self.verified = False
        self.native_reads = self.batch_reads = 0
        self.native_seconds = self.batch_seconds = 0.0
        self.archive_root = Path(archive_root) if archive_root is not None else None
        self.payload = None
        self.row_count = 0
        path = getattr(native, '__file__', None)
        self.native_source_sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else None
        renderer = getattr(native, 'CURRENT_ASSISTANT_TEXT', None)
        if not isinstance(renderer, str) or not renderer.strip():
            raise ValueError('NATIVE_TURN_BATCH_RENDERER_UNVERIFIED')
        values = {name: getattr(native, name, None) for name in
                  ('MESSAGE', 'USER_BODY', 'ASSISTANT_BODY', 'COMPLETE_ACTIONS')}
        if (any(not isinstance(values[key], str) or not values[key] for key in
                ('MESSAGE', 'USER_BODY', 'ASSISTANT_BODY'))
                or not isinstance(values['COMPLETE_ACTIONS'], (tuple, list))
                or not values['COMPLETE_ACTIONS']
                or any(not isinstance(item, str) or not item for item in values['COMPLETE_ACTIONS'])):
            raise ValueError('NATIVE_TURN_BATCH_CONTRACT_UNVERIFIED')
        # The renderer is the same trusted module constant passed to evaluate
        # by Native._assistant_text; do not reconstruct Markdown or price text.
        self.script = '''(roots) => {
 const renderAnswer = (''' + renderer + ''');
 const userSelector = ''' + json.dumps(values['USER_BODY']) + ''';
 const answerSelector = ''' + json.dumps(values['ASSISTANT_BODY']) + ''';
 const footerSelector = ''' + json.dumps(FOOTER) + ''';
 const actions = ''' + json.dumps(values['COMPLETE_ACTIONS']) + ''';
 const visible = element => {
  if (!element.isConnected) return false;
  const style = getComputedStyle(element);
  if (style.display === 'contents') {
   return Array.from(element.childNodes).some(child => {
    if (child.nodeType === Node.ELEMENT_NODE) return visible(child);
    if (child.nodeType !== Node.TEXT_NODE) return false;
    const range = child.ownerDocument.createRange(); range.selectNode(child);
    const rect = range.getBoundingClientRect(); return rect.width > 0 && rect.height > 0;
   });
  }
  if (style.visibility !== 'visible') return false;
  if (typeof element.checkVisibility === 'function' && !element.checkVisibility()) return false;
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
 };
 return roots.filter(visible).map(root => {
  const users = root.querySelectorAll(userSelector);
  const answers = root.querySelectorAll(answerSelector);
  const row = {identifier:root.getAttribute('data-message-id'),
   users:users.length, answers:answers.length};
  if (users.length === 1 && answers.length === 0) {
   row.text = users[0].textContent || '';
   row.images = Array.from(root.querySelectorAll('img')).map(image => ({
    visible:visible(image), decoded:image.complete === true && image.naturalWidth > 0 && image.naturalHeight > 0,
    alt:image.getAttribute('alt') || ''}));
  } else if (users.length === 0 && answers.length === 1) {
   const parent = answers[0].parentElement;
   const footer = parent ? parent.querySelectorAll(footerSelector) : [];
   row.complete = footer.length === 1 && actions.every(selector => footer[0].querySelectorAll(selector).length === 1);
   row.text = renderAnswer(answers[0]);
  }
  return row;
 });
}'''

    def begin(self, payload):
        """Each research operation establishes parity before using batching."""
        self.verified = False
        self.payload = payload
        self.native_reads = self.batch_reads = self.row_count = 0
        self.native_seconds = self.batch_seconds = 0.0

    def evidence(self, phase):
        try:
            if self.archive_root is None:
                return
            value = {'schema_version': 1, 'operation': self.payload.get('operation'),
                'request_sha256': self.payload.get('request_sha256'), 'phase': phase,
                'parity_verified': self.verified, 'row_count': self.row_count,
                'native_reads': self.native_reads, 'batch_reads': self.batch_reads,
                'native_seconds': self.native_seconds, 'batch_seconds': self.batch_seconds,
                'native_source_sha256': self.native_source_sha256, 'at': time.time()}
            _write(self.archive_root, 'native-turn-batch-latest.json', value, replace=True)
            digest = value['request_sha256']
            if isinstance(digest, str) and re.fullmatch('[a-f0-9]{64}', digest):
                _write(self.archive_root, 'native-turn-batch-' + digest + '.' + phase.lower() + '.json', value)
        except BaseException:
            pass  # Diagnostics cannot change native request semantics.

    def _native(self):
        current = self.bridge._visible
        started = time.monotonic()
        try:
            self.bridge._visible = self.original_visible
            self.native_reads += 1
            return self.original_turns()
        finally:
            self.bridge._visible = current
            self.native_seconds += time.monotonic() - started

    def _read(self):
        self.batch_reads += 1
        started = time.monotonic()
        try:
            snapshot = self.bridge.page.locator(self.native.MESSAGE).evaluate_all(self.script)
        finally:
            self.batch_seconds += time.monotonic() - started
        if not isinstance(snapshot, list):
            raise self.native.NotSubmitted('UNAVAILABLE')
        names = list(self.bridge.uploaded_names)
        rows, identifiers = [], set()
        for item in snapshot:
            if not isinstance(item, dict):
                raise self.native.NotSubmitted('UNAVAILABLE')
            identifier = item.get('identifier')
            if (not isinstance(identifier, str)
                    or re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', identifier) is None
                    or identifier in identifiers):
                raise self.native.NotSubmitted('UNAVAILABLE')
            identifiers.add(identifier)
            user, answer = item.get('users'), item.get('answers')
            if type(user) is not int or type(answer) is not int or min(user, answer) < 0:
                raise self.native.NotSubmitted('UNAVAILABLE')
            if user == 1 and answer == 0:
                images, text = item.get('images'), item.get('text')
                if not isinstance(images, list) or not isinstance(text, str):
                    raise self.native.NotSubmitted('UNAVAILABLE')
                accepted = len(images) == len(names)
                for index, image in enumerate(images):
                    if (not isinstance(image, dict) or type(image.get('visible')) is not bool
                            or type(image.get('decoded')) is not bool or not isinstance(image.get('alt'), str)):
                        raise self.native.NotSubmitted('UNAVAILABLE')
                    accepted = (accepted and image['visible'] and image['decoded']
                                and index < len(names) and image['alt'] == names[index])
                rows.append(dict(role='user', text=text,
                                 attachments=list(names) if accepted else ['UNCONFIRMED']))
            elif user == 0 and answer == 1:
                text, complete = item.get('text'), item.get('complete')
                if not isinstance(text, str) or type(complete) is not bool:
                    raise self.native.NotSubmitted('UNAVAILABLE')
                rows.append(dict(role='assistant', text=text, complete=complete, id=identifier))
            else:
                rows.append(dict(role='unknown', text='', complete=False))
        return rows

    def read(self):
        # Legacy UI continues through its exact original renderer and reader.
        if self.bridge._dom_contract != self.native.RAIL_INPUT_GROUP:
            return self._native()
        try:
            rows = self._read()
        except Exception:
            self.verified = False
            return self._native()
        if not self.verified:
            original = self._native()
            try:
                repeated = self._read()
            except Exception:
                return original
            # A growing message may differ between snapshots. Until an actual
            # completed room is stable, retain the original reader's result;
            # an empty home view never proves the nonempty message contract.
            if (rows != original or repeated != original or not repeated
                    or repeated[-1].get('role') != 'assistant'
                    or repeated[-1].get('complete') is not True):
                return original
            self.verified = True
            self.row_count = len(repeated)
            self.evidence('PARITY')
            return repeated
        self.row_count = len(rows)
        return rows


def install_native_turn_batch(bridge, native, archive_root=None):
    reader = NativeTurnBatch(bridge, native, archive_root)
    bridge._turns = reader.read
    return reader


def _write(root, name, value, *, replace=False):
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = root / ('.native-return-' + uuid.uuid4().hex + '.tmp')
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, root / name)
        else:
            try:
                os.link(temporary, root / name)
            except FileExistsError:
                pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def retain_return_failure(root, payload, state, clock, deadline, error):
    """Best-effort first failure evidence without messages, headers or secrets."""
    if root is None:
        return
    digest = payload.get('request_sha256')
    if not isinstance(digest, str) or re.fullmatch('[a-f0-9]{64}', digest) is None:
        return
    chain, seen, current = [], set(), error
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current)); frames = []; frame = current.__traceback__
        while frame is not None and len(frames) < 24:
            frames.append({'file': frame.tb_frame.f_code.co_filename,
                           'function': frame.tb_frame.f_code.co_name, 'line': frame.tb_lineno})
            frame = frame.tb_next
        text = str(current)
        chain.append({'class': type(current).__name__,
                      'reason': text if re.fullmatch('[A-Z][A-Z0-9_]{0,119}', text) else None,
                      'frames': frames})
        current = current.__context__
    value = {'schema_version': 1, 'operation': payload.get('operation'),
        'request_sha256': digest, 'phase': state.get('phase'),
        'accepted_reply_sha256': state.get('accepted_reply_sha256'),
        'followup_intent_sha256': [hashlib.sha256(json.dumps(item, sort_keys=True,
            separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')).hexdigest()
            for item in state.get('followup_intents', [])],
        'clock': clock, 'deadline': deadline, 'at': time.time(), 'chain': chain}
    if not all(type(item) in (int, float) and math.isfinite(item) for item in (clock, deadline)):
        return
    _write(root, 'native-return-failure-' + digest + '.json', value)
