"""Recover one already answered native screening without another WEB Send.

The existing actor owns the page and claim. Recovery recognizes only the
observed initial-screening + exact coin-summary clarification shape; it never
fills, uploads, clicks, navigates, calls Mini, or changes the daily-room journal.
The ordinary worker still validates the receipt, catalog and research state.
"""
from __future__ import annotations

from datetime import date, datetime
import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
from zoneinfo import ZoneInfo

from .native_daily_room import JOURNAL_NAME, identities
from .native_chart_followup import _directory, _private_file, pairs
from .neurobro_reply import parse_reply_for_payload

KIND = 'SCREENING_RECOVERY'
PROTOCOL = 'NATIVE_SCREENING_COMPLETION_RECOVERY_V1'
NATIVE_DOM_SHA256 = '30ea13cd51706b1db65620cdfb5f8db6dec1c9b3b8a20c9b7ca1d6c9db07f72e'
MAX_RECEIPT = 2 * 1024 * 1024
_FIELDS = {'operation', 'marker', 'provider', 'contract_version', 'prompt', 'output_schema', 'context', 'request_sha256'}
_RESULT = {'output', 'assistant_text', 'response_id', 'chart_evidence'}


class ScreeningRecoveryUnavailable(ValueError):
    pass


def need(value, reason='SCREENING_RECOVERY_UNVERIFIED'):
    if not value:
        raise ScreeningRecoveryUnavailable(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def text_sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def normal(value):
    return value.replace('\r\n', '\n').strip()


def validate(payload):
    """Only the original one/two-slot screening body, never an arbitrary task."""
    need(type(payload) is dict and set(payload) == _FIELDS)
    operation = payload['operation']
    need(type(operation) is str)
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}):robot-v9:(?:0|[1-9]\d{0,11}):screening:0:([12])', operation)
    need(match is not None)
    try:
        date.fromisoformat(match[1])
    except ValueError:
        need(False)
    count = int(match[2])
    need(payload['provider'] == 'NEUROBRO_WEB'
         and payload['contract_version'] == 'neurobro-browser-decision-v2'
         and payload['marker'] == 'HARUN_' + text_sha(operation)[:32]
         and payload['context'] is None
         and payload['prompt'] == ('pilihkan ' + str(count) + ' coin yang bagus dan rate tinggi '
             'mandapatkan profit saat ini di future market binance'))
    schema = payload['output_schema']
    need(type(schema) is dict and schema.get('type') == 'object'
         and schema.get('required') == ['symbols']
         and schema.get('additionalProperties') is False
         and type(schema.get('properties')) is dict and set(schema['properties']) == {'symbols'})
    limits = schema['properties']['symbols']
    need(type(limits) is dict and limits.get('type') == 'array'
         and limits.get('minItems') == count and type(limits.get('minItems')) is int
         and limits.get('maxItems') == count and type(limits.get('maxItems')) is int
         and limits.get('uniqueItems') is True
         and type(limits.get('items')) is dict and limits['items'].get('type') == 'string'
         and limits['items'].get('pattern') == r'^[A-Z0-9]{2,18}USDT$')
    body = {key: value for key, value in payload.items() if key != 'request_sha256'}
    need(len(canonical(body)) <= 32768 and payload['request_sha256'] == digest(body))
    return count


def clarification(count):
    return ('Mohon ringkas ' + str(count) + ' coin pilihanmu sesuai kapasitas '
            'yang diminta dalam bentuk COIN: simbol USDT Perpetual. '
            'Jangan sertakan coin pembanding sebagai pilihan.')


def _name(payload):
    return 'screening-recovery-' + digest({key: payload[key] for key in
        ('operation', 'request_sha256')}) + '.json'


def _verified_rows(payload, room, rows, rendered):
    """Bind the exact native IDs, footer completion, text and stored anchors."""
    count = validate(payload)
    need(type(room) is dict and set(room) == {'day', 'room_id', 'url', 'updated_at',
         'anchors', 'first_prompt_sha256', 'marker'})
    need(room['day'] == payload['operation'][:10]
         and type(room['room_id']) is str and re.fullmatch('[a-f0-9]{32}', room['room_id'])
         and type(room['url']) is str and room['url'].startswith('https://app.neurobro.ai/')
         and type(room['updated_at']) in (int, float) and math.isfinite(room['updated_at'])
         and room['updated_at'] > 0 and room['marker'] == payload['marker']
         and type(rendered) is str and rendered.count(payload['marker']) == 1
         and room['first_prompt_sha256'] == text_sha(normal(rendered)))
    need(type(rows) is list and len(rows) == 4
         and all(type(row) is dict and row.get('attachments', []) == [] for row in rows)
         and identities(rows) == room['anchors'])
    for index, row in enumerate(rows):
        need(row.get('role') == ('user' if index % 2 == 0 else 'assistant')
             and type(row.get('text')) is str and row['text'])
        if index % 2:
            need(row.get('complete') is True and type(row.get('id')) is str
                 and re.fullmatch('[A-Za-z0-9_.:-]{1,160}', row['id']) is not None)
    need(rows[1]['id'] != rows[3]['id']
         and normal(rows[0]['text']) == normal(rendered)
         and normal(rows[2]['text']) == clarification(count))
    first = parse_reply_for_payload(rows[1]['text'], payload)
    final = parse_reply_for_payload(rows[3]['text'], payload)
    need(first == final and type(final) is dict and set(final) == {'symbols'}
         and type(final['symbols']) is list and len(final['symbols']) == count
         and len(set(final['symbols'])) == count)
    return final


def validate_receipt(value, payload):
    validate(payload)
    fields = {'schema_version', 'kind', 'payload_sha256', 'room', 'rows',
              'rendered_prompt', 'initial_reply_sha256', 'final_reply_sha256',
              'native_source_sha256', 'native_received_at', 'stable_reads', 'native_poll_reads', 'native_result'}
    need(type(value) is dict and set(value) == fields
         and value['schema_version'] == 1 and value['kind'] == PROTOCOL
         and value['payload_sha256'] == digest(payload)
         and value['native_source_sha256'] == NATIVE_DOM_SHA256
         and value['stable_reads'] == 4 and type(value['native_poll_reads']) is int
         and value['native_poll_reads'] >= 4)
    output = _verified_rows(payload, value['room'], value['rows'], value['rendered_prompt'])
    answer = value['rows'][-1]['text']
    result = value['native_result']
    need(type(result) is dict and set(result) == _RESULT | {'operation', 'marker', 'request_sha256'}
         and all(result[key] == payload[key] for key in ('operation', 'marker', 'request_sha256'))
         and result['output'] == output and result['assistant_text'] == answer
         and result['chart_evidence'] == []
         and result['response_id'] == 'dom-' + text_sha(value['room']['url'] + '\n' + payload['marker'] + '\n' + answer)
         and value['initial_reply_sha256'] == text_sha(value['rows'][1]['text'])
         and value['final_reply_sha256'] == text_sha(answer)
         and type(value['native_received_at']) in (int, float)
         and math.isfinite(value['native_received_at'])
         and value['native_received_at'] >= value['room']['updated_at'])
    return value


def read_receipt(archive_root, payload):
    """Read only a fixed-name immutable completion; no page or model calls."""
    validate(payload)
    directory = _directory(archive_root)
    try:
        try:
            raw = _private_file(directory, _name(payload), MAX_RECEIPT)
        except FileNotFoundError:
            return None
        value = json.loads(raw, object_pairs_hook=pairs)
        return validate_receipt(value, payload)
    finally:
        os.close(directory)


def _save_receipt(root, payload, value):
    raw = canonical(validate_receipt(value, payload))
    need(0 < len(raw) <= MAX_RECEIPT)
    directory = _directory(root)
    temporary = '.screening-recovery-' + os.urandom(16).hex() + '.tmp'
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, _name(payload), src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
            os.fsync(directory)
        except FileExistsError:
            retained = json.loads(_private_file(directory, _name(payload), MAX_RECEIPT),
                                  object_pairs_hook=pairs)
            validate_receipt(retained, payload)
            need(retained['native_result'] == value['native_result']
                 and retained['room'] == value['room'] and retained['rows'] == value['rows'])
            return retained
        return value
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _room_record(root, day):
    """Existing journal only: RO SQLite cannot initialize or repair the room."""
    directory = _directory(root); fd = None; db = None
    try:
        fd = os.open(JOURNAL_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
             and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1)
        db = sqlite3.connect('file:/proc/self/fd/' + str(fd) + '?mode=ro', uri=True,
                             timeout=1, isolation_level=None)
        db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM daily_rooms WHERE day=?', (day,)).fetchone()
        need(row is not None and row['state'] == 'BOUND')
        named = os.stat(JOURNAL_NAME, dir_fd=directory, follow_symlinks=False)
        need((before.st_dev, before.st_ino) == (named.st_dev, named.st_ino))
        binding = json.loads(row['binding'], object_pairs_hook=pairs)
        return dict(day=row['day'], room_id=row['room_id'], url=row['url'],
                    updated_at=row['updated_at'], anchors=json.loads(row['anchors']),
                    first_prompt_sha256=binding['first_prompt_sha256'], marker=binding['marker'])
    finally:
        if db is not None: db.close()
        if fd is not None: os.close(fd)
        os.close(directory)


def _native_source_digest(native):
    from pathlib import Path
    need(native.__name__ == 'browser_runtime.dom'
         and native.__file__ == '/app/browser_runtime/dom.py')
    return hashlib.sha256(Path(native.__file__).read_bytes()).hexdigest()


def _actor_gate(actor):
    need(not actor.stopping.is_set() and actor._mode() == 'AUTOMATION'
         and not actor.sessions.active(), 'SCREENING_RECOVERY_BROWSER_NOT_AUTOMATIC')


def recover(actor, delegated_bridge, payload):
    """Existing actor-thread entry. Returns the native four-field result."""
    validate(payload); _actor_gate(actor)
    need(actor.claim.locked(), 'SCREENING_RECOVERY_ACTOR_CLAIM_REQUIRED')
    room, raw, native = delegated_bridge.daily_room, delegated_bridge.bridge, delegated_bridge.native
    need(room is not None and room.bridge is raw and room.native is native)
    need(_native_source_digest(native) == NATIVE_DOM_SHA256)
    with room.lock:
        retained = read_receipt(room.store.root, payload)
        if retained is not None:
            return {key: copy.deepcopy(retained['native_result'][key]) for key in _RESULT}
        now = time.time()
        day = datetime.fromtimestamp(now, ZoneInfo('Asia/Jakarta')).date().isoformat()
        need(day == payload['operation'][:10])
        record = _room_record(room.store.root, day)
        need(raw.status() == 'CONNECTED')
        need(delegated_bridge.prompt_cache is not None)
        cached = delegated_bridge.prompt_cache.read(payload)
        need(cached is not None and record['url'] == raw.page.url)
        rows = room.full_turns()
        output = _verified_rows(payload, record, rows, cached['rendered_prompt'])
        old = (room.offset, room.prefix, raw._turns, raw.operation_deadline, raw._operation_contract)
        until = raw.clock() + min(30.0, raw.timeout)
        need(math.isfinite(until) and until > raw.clock())
        try:
            room.offset, room.prefix = 2, identities(rows[:2])
            # Reuse the native reader through its existing DailyRoom scoping;
            # the optimized batch reader must not expose all four rows here.
            raw._turns = room.turns
            raw.operation_deadline, raw._operation_contract = until, raw._dom_contract
            previous, stable, reads = None, 0, 0
            while raw.clock() < until:
                _actor_gate(actor)
                need(raw.status() == 'CONNECTED' and raw.page.url == record['url'])
                need(_room_record(room.store.root, day) == record and room.full_turns() == rows)
                raw._timeout(1000)
                answer = raw._answer(rows[2]['text'], ())
                reads += 1
                stable = stable + 1 if answer is not None and answer == previous else 0
                previous = answer
                if answer is not None and stable >= 3:
                    need(answer == rows[-1]['text'] and room.full_turns() == rows)
                    _actor_gate(actor)
                    result = {**{key: payload[key] for key in ('operation', 'marker', 'request_sha256')},
                        'output': output, 'assistant_text': answer,
                        'response_id': 'dom-' + text_sha(raw.page.url + '\n' + payload['marker'] + '\n' + answer),
                        'chart_evidence': []}
                    receipt = dict(schema_version=1, kind=PROTOCOL, payload_sha256=digest(payload),
                        room=record, rows=rows, rendered_prompt=cached['rendered_prompt'],
                        initial_reply_sha256=text_sha(rows[1]['text']), final_reply_sha256=text_sha(answer),
                        native_source_sha256=NATIVE_DOM_SHA256, native_received_at=time.time(),
                        stable_reads=4, native_poll_reads=reads, native_result=result)
                    receipt = _save_receipt(room.store.root, payload, receipt)
                    return {key: copy.deepcopy(receipt['native_result'][key]) for key in _RESULT}
                raw.page.wait_for_timeout(250)
            need(False, 'SCREENING_RECOVERY_REPLY_NOT_STABLE')
        finally:
            room.offset, room.prefix, raw._turns, raw.operation_deadline, raw._operation_contract = old


def enqueue(actor, payload):
    """Authenticated service calls this; only the existing actor touches DOM."""
    validate(payload)
    native = actor._bridge.native
    if actor.stopping.is_set(): raise native.NotSubmitted('UNAVAILABLE')
    if not actor.claim.acquire(blocking=False): raise native.NotSubmitted('BUSY')
    queued = False
    try:
        if actor.sessions.active(): raise native.NotSubmitted('BUSY')
        with actor.lock:
            if actor.mode != 'AUTOMATION': raise native.NotSubmitted('BUSY')
            status, ready = actor.current_status, actor.ready
        if status in ('CHALLENGE_REQUIRED', 'LOGIN_REQUIRED'):
            raise native.NotSubmitted(status)
        if not ready or status != 'CONNECTED': raise native.NotSubmitted('UNAVAILABLE')
        task = dict(request=copy.deepcopy(payload), kind=KIND, done=threading.Event())
        actor.queue.put_nowait(task); queued = True; actor._wake.set()
        if not task['done'].wait(45): raise native.OutcomeUnknown()
        if 'error' in task: raise task['error']
        result = task['result']
        need(type(result) is dict and set(result) == _RESULT)
        return {**{key: payload[key] for key in ('operation', 'marker', 'request_sha256')}, **result}
    finally:
        if not queued: actor.claim.release()
