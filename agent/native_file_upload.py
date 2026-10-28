"""Deliver CSV data through the existing Neurobro attachment menu.

This adapter retains the native PNG upload receipts and native message parser.
Only the extra data-file input and its visible per-user-turn witnesses are added.
It never starts a browser, sends a message, or contacts an application API.
"""
from __future__ import annotations

import hashlib
import re
import math
import time
from urllib.parse import urlsplit

from .native_data_files import deliver_market_data_files


FILE_WITNESS = r'''(root, args) => {
 const visible = e => { const s=getComputedStyle(e),r=e.getBoundingClientRect();
  return e.isConnected && s.display!=='none' && s.visibility!=='hidden' && Number(s.opacity)!==0 && r.width>0 && r.height>0; };
 const body=args.bodySelector ? root.querySelector(args.bodySelector) : null;
 const nodes=[root,...root.querySelectorAll('*')];
 return args.names.map(name=>{
  const matches=nodes.filter(e=>!e.children.length && (!body || !body.contains(e)) &&
    (e.textContent||'').trim()===name && visible(e));
  if(matches.length!==1) return {name,ready:false,matches:matches.length};
  let tile=matches[0];
  for(let p=tile.parentElement;p && p!==root && (!body || p!==body);p=p.parentElement){
   const r=p.getBoundingClientRect(); if(r.width>520 || r.height>220) break;
   tile=p; if(p.querySelector('button,a[href]')) break;
  }
  const text=(tile.textContent||'').trim();
  const busy=tile.matches('[aria-busy="true"]') || !!tile.querySelector('[aria-busy="true"],[role="progressbar"],.animate-spin');
  const failed=/\b(?:failed|failure|error|gagal)\b/i.test(text) ||
    tile.matches('[aria-invalid="true"],[data-state="error"],[data-status="error"]');
  return {name,ready:!busy && !failed,matches:1};
 });
}'''


def enabled(native, payload):
    return (type(payload) is dict and payload.get('context') is not None
        and getattr(getattr(native, 'user_prompt', None), '__harun_data_files_v1__', False) is True)


class NativeFileTurnReader:
    """Keep genuine file witnesses available to the native final receipt reader."""
    def __init__(self, bridge, native):
        self.bridge, self.native = bridge, native
        self.original_turns = bridge._turns
        self.turn_files = {}

    def attachment_names(self, names):
        # Native DOM.ask still derives this argument from its two PNGs. Match
        # only an exact registered pair, never an arbitrary filename prefix.
        values = tuple(names)
        matches = [all_names for all_names, (png_names, _, _) in self.turn_files.items()
            if values in (all_names, png_names)]
        if len(matches) > 1:
            raise self.native.NotSubmitted('UNAVAILABLE')
        return list(matches[0]) if matches else list(names)

    def read(self):
        all_names = tuple(self.bridge.uploaded_names)
        data = self.turn_files.get(all_names)
        if data is None:
            return self.original_turns()
        png_names, csv_names, witness = data
        try:
            self.bridge.uploaded_names = list(png_names)
            rows = self.original_turns()
        finally:
            self.bridge.uploaded_names = list(all_names)
        turns = self.bridge._visible(self.native.MESSAGE)
        if len(turns) != len(rows):
            raise self.native.NotSubmitted('UNAVAILABLE')
        for turn, row in zip(turns, rows):
            if row.get('role') == 'user' and row.get('attachments') == list(png_names):
                row['attachments'] = (list(all_names) if witness(turn, csv_names, user=True)
                    else ['UNCONFIRMED'])
        return rows


class NativeDataFileDelivery:
    def __init__(self, bridge, native, gate, *, wall_clock=time.time):
        self.bridge, self.native, self.gate = bridge, native, gate
        self.wall_clock = wall_clock
        self.original_upload = bridge._upload
        self.original_answer = bridge._answer
        reader = getattr(bridge, '_harun_file_turn_reader', None)
        if reader is None:
            reader = NativeFileTurnReader(bridge, native)
            bridge._harun_file_turn_reader = reader
        if not isinstance(reader, NativeFileTurnReader) or reader.bridge is not bridge:
            raise native.NotSubmitted('UNAVAILABLE')
        self.reader = reader
        self.observations = []
        self.installed = False
        self.network_errors = []

    def attachment_names(self, names):
        return self.reader.attachment_names(names)

    def _answer(self, prompt, attachment_names=()):
        return self.original_answer(prompt, self.attachment_names(attachment_names))

    def receipt_callback(self, callback):
        def retain(receipt):
            self.gate()
            self._need(type(receipt) is dict and type(receipt.get('attachment_names')) is list)
            names = self.attachment_names(receipt['attachment_names'])
            self._need(len(names) == 4 and tuple(names) in self.reader.turn_files)
            prompt = receipt.get('rendered_prompt')
            self._need(type(prompt) is str and any(
                row.get('role') == 'user' and row.get('text') == prompt
                and row.get('attachments') == names for row in self.reader.read()))
            # CSV witnesses were checked on the submitted user turn above.
            # Keep the native answer, response ID and PNG evidence unchanged.
            callback({**receipt, 'attachment_names': names})
        return retain

    def _need(self, value):
        if not value:
            raise self.native.NotSubmitted('UNAVAILABLE')

    def _visible(self, locator):
        return [locator.nth(i) for i in range(locator.count()) if locator.nth(i).is_visible()]

    def _attachment_scope(self, composer):
        area = composer.locator(self.bridge._dom_contract.area)
        if getattr(self.bridge._dom_contract, 'name', None) == 'RAIL_INPUT_GROUP':
            scope = area.locator('xpath=..')
            self._need(scope.get_attribute('role') == 'presentation')
            return area, scope
        return area, area

    def _file_menu(self, composer):
        # These are the actual labels shown by the owner's current Neurobro UI.
        page = self.bridge.page
        candidates = self._visible(page.get_by_text('File', exact=True))
        if not candidates:
            area, _ = self._attachment_scope(composer)
            plus = self._visible(area.locator('button:has(svg.lucide-plus)'))
            self._need(len(plus) == 1)
            self.gate(); plus[0].click(timeout=self.bridge._timeout(5000))
            candidates = self._visible(page.get_by_text('File', exact=True))
        self._need(len(candidates) == 1)
        return candidates[0]

    def _witness(self, root, names, *, user=False):
        selector = getattr(self.native, 'USER_BODY', None) if user else None
        if user:
            self._need(type(selector) is str and selector)
        value = root.evaluate(FILE_WITNESS, {'names': list(names), 'bodySelector': selector})
        return (type(value) is list and len(value) == len(names)
            and all(type(row) is dict and row.get('name') == name
                and row.get('ready') is True and row.get('matches') == 1
                for row, name in zip(value, names)))

    def _upload(self, composer, payload, artifacts):
        files = deliver_market_data_files(payload['context'], now=self.wall_clock())
        self._need(len(files) == 2)
        self.gate()
        bound = self.original_upload(composer, payload, artifacts)
        png_names = list(self.bridge.uploaded_names)
        self._need(type(bound) is list and len(bound) == len(png_names) == 2)
        names = [record['name'] for record in files]
        self._need(len(set(png_names + names)) == 4)
        record = {'symbol': files[0]['symbol'], 'stage': 'DATA_FILE_MENU',
            'operation': payload.get('operation'), 'request_sha256': payload.get('request_sha256'),
            'files': [{key: value for key, value in row.items() if key != 'buffer'} for row in files],
            'png_names': png_names, 'data_names': names, 'ui_upload_responses': [],
            'preview_verified': False, 'messages_sent': 0}
        self.observations.append(record)
        page = self.bridge.page
        def observe(response):
            try:
                request = response.request
                raw = request.post_data_buffer
                if request.method not in ('POST', 'PUT') or type(raw) is not bytes:
                    return
                matching = [row['name'] for row in files if row['buffer'] in raw]
                if matching:
                    parsed = urlsplit(response.url)
                    record['ui_upload_responses'].append({'method': request.method,
                        'status': response.status, 'path': parsed.path,
                        'csv_names': matching, 'request_body_sha256': hashlib.sha256(raw).hexdigest()})
            except Exception:
                pass
        page.on('response', observe)
        try:
            item = self._file_menu(composer)
            self.gate()
            record['stage'] = 'DATA_FILE_CHOOSER'
            with page.expect_file_chooser(timeout=self.bridge._timeout(5000)) as choice:
                item.click(timeout=self.bridge._timeout(5000))
            self.gate()
            choice.value.set_files([{key: row[key] for key in ('name', 'mimeType', 'buffer')}
                for row in files], timeout=self.bridge._timeout(5000))
            record['stage'] = 'DATA_FILE_PREVIEW'
            area, scope = self._attachment_scope(composer)
            stable = 0
            until = self.bridge._until(30)
            while self.bridge.clock() < until:
                self.gate()
                buttons = self.bridge._visible(self.bridge._dom_contract.send)
                images = self._visible(scope.locator('img'))
                loaded = len(images) == 2 and all(image.evaluate(
                    '(e) => e.complete && e.naturalWidth > 0 && e.naturalHeight > 0') for image in images)
                ready = (loaded and self._witness(scope, names)
                    and len(buttons) == 1 and buttons[0].is_enabled())
                stable = stable + 1 if ready else 0
                if stable >= 3:
                    break
                page.wait_for_timeout(100)
            self._need(stable >= 3)
            self.bridge.uploaded_names = png_names + names
            self.reader.turn_files[tuple(self.bridge.uploaded_names)] = (
                tuple(png_names), tuple(names), self._witness)
            record.update(stage='COMPLETE', preview_verified=True)
            return bound
        finally:
            try:
                page.remove_listener('response', observe)
            except Exception:
                pass

    def _observe_error_response(self, response):
        try:
            parsed = urlsplit(response.url)
            if not (parsed.hostname == 'neurobro.ai' or (parsed.hostname or '').endswith('.neurobro.ai')):
                return
            if response.status < 400:
                return
            record = {'kind': 'HTTP_RESPONSE', 'status': response.status,
                'method': response.request.method, 'path': parsed.path}
            # Only error/message fields from a JSON error are retained. Cookies,
            # headers, signed URLs and HTML challenge scripts are never copied.
            try:
                value = response.json()
                if type(value) is dict:
                    def safe(item):
                        if type(item) in (int, bool) or item is None:
                            return item
                        if type(item) is float:
                            return item if math.isfinite(item) else None
                        if type(item) is str:
                            return re.sub(r'(?i)(?:bearer\s+|eyJ)[A-Za-z0-9_.-]{24,}',
                                '[REDACTED]', item[:2048])
                        if type(item) is dict:
                            return {key: safe(val) for key, val in item.items()
                                if key in ('error', 'message', 'detail', 'reason', 'code', 'type')}
                        return None
                    record['error'] = safe(value)
            except Exception:
                record['error_body'] = 'NON_JSON_OR_UNAVAILABLE'
            self.network_errors.append(record)
            del self.network_errors[:-12]
        except Exception:
            pass

    def _observe_request_failure(self, request):
        try:
            parsed = urlsplit(request.url)
            if parsed.hostname == 'neurobro.ai' or (parsed.hostname or '').endswith('.neurobro.ai'):
                self.network_errors.append({'kind': 'REQUEST_FAILED', 'path': parsed.path,
                    'method': request.method, 'reason': str(request.failure)[:512]})
                del self.network_errors[:-12]
        except Exception:
            pass

    def install(self):
        self._need(not self.installed)
        self.reader.turn_files.clear()
        self.bridge._upload, self.bridge._turns = self._upload, self.reader.read
        self.bridge._answer = self._answer
        self.bridge.data_file_deliveries = self.observations
        self.bridge.data_file_network_errors = self.network_errors
        self.bridge.page.on('response', self._observe_error_response)
        self.bridge.page.on('requestfailed', self._observe_request_failure)
        self.installed = True
        return self

    def close(self):
        if self.installed:
            self.bridge._upload = self.original_upload
            self.bridge._answer = self.original_answer
            for event, callback in (('response', self._observe_error_response),
                    ('requestfailed', self._observe_request_failure)):
                try:
                    self.bridge.page.remove_listener(event, callback)
                except Exception:
                    pass
            # The native post-ask receipt observer still reads the actual four
            # attachments. Keep this reader, without any gate or upload action.
            self.bridge._turns = self.reader.read
            self.installed = False
