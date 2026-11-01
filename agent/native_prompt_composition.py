"""Durable render receipts for Mini-authored prompts on the existing actor.

This cache cannot send a message or start a model call. The owner-thread hook
stores a validated completed draft before the native transaction renders it;
the same bytes remain available to daily-room and post-receipt checks.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode('ascii')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def compact_protocol(rendered, payload):
    """Keep human discussion/CSV metadata while moving schema to integration."""
    separator = '\n\nOUTPUT_JSON_SCHEMA:\n'
    marker = payload.get('marker')
    if separator not in rendered and isinstance(marker, str) and rendered.count(marker) == 1:
        return rendered  # The installed chart formatter already emits compact prose.
    rendered = rendered.split(separator, 1)[0].rstrip()
    required = set(payload.get('output_schema', {}).get('required', []))
    if required == {'symbols'}:
        summary = 'Akhiri dengan ringkasan COIN: simbol USDT Perpetual pilihanmu.'
    elif required == {'symbol', 'order_ref', 'decision'}:
        summary = ('Akhiri dengan COIN dan KEPUTUSAN: TAHAN atau BATALKAN untuk order '
            'yang sedang dievaluasi. Identitas order ditangani integrasi.')
    else:
        summary = ('Setelah pembahasannya jelas, ringkas COIN, ARAH: LONG/SHORT/HOLD, '
            'ENTRY, TP, dan SL. Gunakan satu harga per level untuk setup aktif; '
            'HOLD tidak perlu level order. Quantity dihitung Risk Manager.')
    if not isinstance(marker, str) or not marker:
        raise ValueError('NATIVE_COMPOSE_MARKER_UNVERIFIED')
    rendered += '\n\n' + summary
    if marker not in rendered:
        rendered += '\nReferensi percakapan: ' + marker + ' (tidak perlu diulang).'
    if rendered.count(marker) != 1:
        raise ValueError('NATIVE_COMPOSE_MARKER_UNVERIFIED')
    return rendered


class PromptCompositionCache:
    def __init__(self, native, root):
        self.native, self.root = native, Path(root)
        previous = native.user_prompt
        # Install once on the native module; a later bridge shares its renderer.
        prior = getattr(previous, '__harun_prompt_composition_cache__', None)
        if prior is not None:
            if prior.root != self.root:
                raise ValueError('NATIVE_COMPOSE_ARCHIVE_CHANGED')
            self.original = prior.original
            return
        self.original = previous
        @functools.wraps(previous)
        def render(payload):
            stored = self.read(payload)
            return stored['rendered_prompt'] if stored is not None else previous(payload)
        render.__harun_prompt_composition_cache__ = self
        native.user_prompt = render

    def _name(self, payload):
        return 'mini-prompt-' + digest(payload) + '.json'

    def read(self, payload):
        from .native_chart_followup import _directory
        directory = _directory(self.root)
        try:
            try:
                descriptor = os.open(self._name(payload), os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=directory)
            except FileNotFoundError:
                return None
            with os.fdopen(descriptor, 'rb') as stream:
                before = os.fstat(stream.fileno())
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                        or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600
                        or not 0 < before.st_size <= 384 * 1024):
                    raise ValueError('NATIVE_COMPOSE_RECEIPT_UNVERIFIED')
                raw = stream.read(384 * 1024 + 1)
                after = os.fstat(stream.fileno())
                named = os.stat(self._name(payload), dir_fd=directory, follow_symlinks=False)
                identity = lambda row: (row.st_dev, row.st_ino, row.st_mode, row.st_uid,
                    row.st_nlink, row.st_size, row.st_mtime_ns, row.st_ctime_ns)
                if len(raw) != before.st_size or not (identity(before) == identity(after) == identity(named)):
                    raise ValueError('NATIVE_COMPOSE_RECEIPT_UNVERIFIED')
                value = json.loads(raw)
            if (type(value) is not dict or set(value) != {'schema_version', 'operation',
                    'payload_sha256', 'model_request_id', 'rendered_prompt', 'rendered_sha256'}
                    or value['schema_version'] != 1 or value['operation'] != payload['operation']
                    or value['payload_sha256'] != digest(payload)
                    or type(value['model_request_id']) is not str or len(value['model_request_id']) != 64
                    or type(value['rendered_prompt']) is not str or not value['rendered_prompt']
                    or hashlib.sha256(value['rendered_prompt'].encode()).hexdigest() != value['rendered_sha256']):
                raise ValueError('NATIVE_COMPOSE_RECEIPT_UNVERIFIED')
            return value
        finally:
            os.close(directory)

    def store(self, payload, question, model_request_id):
        from .native_chart_followup import _directory
        before = self.read(payload)
        if before is not None:
            return before['rendered_prompt']
        projected = dict(payload, prompt=question)
        rendered = compact_protocol(self.original(projected), payload)
        if (type(rendered) is not str or not rendered
                or len(rendered.encode()) > 384 * 1024):
            raise ValueError('NATIVE_COMPOSE_RENDER_UNVERIFIED')
        record = {'schema_version': 1, 'operation': payload['operation'],
            'payload_sha256': digest(payload), 'model_request_id': model_request_id,
            'rendered_prompt': rendered,
            'rendered_sha256': hashlib.sha256(rendered.encode()).hexdigest()}
        directory = _directory(self.root)
        temporary = '.mini-prompt-' + uuid.uuid4().hex + '.tmp'
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(canonical(record)); stream.flush(); os.fsync(stream.fileno())
            os.link(temporary, self._name(payload), src_dir_fd=directory,
                dst_dir_fd=directory, follow_symlinks=False)
            os.unlink(temporary, dir_fd=directory)
            os.fsync(directory)
        finally:
            try: os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError: pass
            os.close(directory)
        return rendered
