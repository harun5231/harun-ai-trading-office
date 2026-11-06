"""Recover one already answered native screening without another WEB Send.

The existing actor owns the page and claim. Recovery recognizes only the
observed initial-screening + exact coin-summary clarification shape; it never
fills, uploads, sends, creates a room, calls Mini, or changes the daily-room
journal. It may reopen that already BOUND room through the existing history UI.
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
from . import native_screening_identity as identity

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


def _verified_rows(payload, room, rows, rendered, *, observed_anchors=None):
    """Bind the exact native IDs, footer completion, text and stored anchors."""
    count = validate(payload)
    need(type(room) is dict and set(room) == {'day', 'room_id', 'url', 'updated_at',
         'anchors', 'first_prompt_sha256', 'marker', 'history_titles', 'identifier_reconciliation'})
    need(room['day'] == payload['operation'][:10]
         and type(room['room_id']) is str and re.fullmatch('[a-f0-9]{32}', room['room_id'])
         and type(room['url']) is str and room['url'].startswith('https://app.neurobro.ai/')
         and type(room['updated_at']) in (int, float) and math.isfinite(room['updated_at'])
         and room['updated_at'] > 0 and room['marker'] == payload['marker']
         and type(room['history_titles']) is list
         and all(type(title) is str for title in room['history_titles'])
         and type(rendered) is str and rendered.count(payload['marker']) == 1
         and room['first_prompt_sha256'] == text_sha(normal(rendered)))
    need(type(rows) is list and len(rows) == 4
         and all(type(row) is dict and row.get('attachments', []) == [] for row in rows)
         and identities(rows) == (room['anchors'] if observed_anchors is None else observed_anchors))
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
              'native_source_sha256', 'native_received_at', 'stable_reads', 'native_poll_reads', 'native_result',
              'history_restore_attempted', 'history_restored', 'identity_reconciliation'}
    need(type(value) is dict and set(value) == fields
         and value['schema_version'] == 1 and value['kind'] == PROTOCOL
         and value['payload_sha256'] == digest(payload)
         and value['native_source_sha256'] == NATIVE_DOM_SHA256
         and value['stable_reads'] == 4 and type(value['native_poll_reads']) is int
         and value['native_poll_reads'] >= 4
         and type(value['history_restore_attempted']) is bool
         and type(value['history_restored']) is bool
         and value['history_restore_attempted'] == value['history_restored'])
    reconciliation = value['identity_reconciliation']
    if reconciliation is None:
        need(value['room']['identifier_reconciliation'] is None)
        observed = None
    else:
        need(type(reconciliation) is dict and set(reconciliation) == {'alias', 'receipt'})
        alias = identity.validate_identity_receipt(reconciliation['receipt'], payload,
            value['room'], value['rows'])
        need(alias == reconciliation['alias'] == value['room']['identifier_reconciliation'])
        observed = alias['observed_anchors']
    output = _verified_rows(payload, value['room'], value['rows'], value['rendered_prompt'],
        observed_anchors=observed)
    if reconciliation is not None:
        need(output['symbols'] == json.loads(reconciliation['receipt']['identity_evidence'][
            'retained_agent']['first_mini_done']['result'])['decision']['symbols'])
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


def _room_record(root, day, *, allow_sent=False):
    """Existing journal only: RO SQLite cannot initialize or repair the room."""
    directory = _directory(root); fd = None; db = None
    try:
        fd = os.open(JOURNAL_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
             and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1)
        db = sqlite3.connect('file:/proc/self/fd/' + str(directory) + '/' + JOURNAL_NAME + '?mode=ro', uri=True,
                             timeout=1, isolation_level=None)
        db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM daily_rooms WHERE day=?', (day,)).fetchone()
        need(row is not None and row['state'] in (('BOUND', 'SENT') if allow_sent else ('BOUND',)))
        named = os.stat(JOURNAL_NAME, dir_fd=directory, follow_symlinks=False)
        need((before.st_dev, before.st_ino) == (named.st_dev, named.st_ino))
        binding = json.loads(row['binding'], object_pairs_hook=pairs)
        result = dict(day=row['day'], room_id=row['room_id'], url=row['url'],
                    updated_at=row['updated_at'], anchors=json.loads(row['anchors']),
                    first_prompt_sha256=binding['first_prompt_sha256'], marker=binding['marker'],
                    history_titles=binding.get('history_titles', []),
                    identifier_reconciliation=binding.get('identifier_reconciliation'))
        if allow_sent:
            # Private preimage for the observer's separately verified CAS.
            # Reading SENT is not permission to send or claim it completed.
            result['_journal_snapshot'] = dict(row)
        return result
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


def observation_prefix(payload):
    """Fixed operation-bound private capture prefix, never supplied as a path."""
    validate(payload)
    return 'screening-recovery-observation-' + digest({key: payload[key] for key in
        ('operation', 'request_sha256')})[:16] + '-'


def _retain_observation(root, payload, record, rows, rendered, stage, *,
                        attempted=False, restored=False, error=None):
    # This is failure evidence, never a completion or authority to trade.
    reason = str(error) if error is not None else None
    value = dict(schema_version=1, kind='NATIVE_SCREENING_RECOVERY_OBSERVATION_V1',
        operation=payload['operation'], request_sha256=payload['request_sha256'],
        payload_sha256=digest(payload), observed_at=time.time(), stage=stage,
        room=record, rows=rows, rendered_prompt=rendered,
        native_source_sha256=NATIVE_DOM_SHA256,
        history_restore_attempted=attempted, history_restored=restored,
        completion_verified=False, error=None if error is None else dict(
            exception_class=type(error).__name__, reason=reason if re.fullmatch(
                '[A-Z][A-Z0-9_]{0,119}', reason or '') else None))
    raw = canonical(value)
    need(0 < len(raw) <= MAX_RECEIPT, 'SCREENING_RECOVERY_OBSERVATION_TOO_LARGE')
    name = observation_prefix(payload) + digest(value) + '.json'
    directory = _directory(root)
    temporary = '.screening-observation-' + os.urandom(16).hex() + '.tmp'
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
            os.fsync(directory)
        except FileExistsError:
            need(_private_file(directory, name, MAX_RECEIPT) == raw)
        return name
    finally:
        try: os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError: pass
        os.close(directory)


def _unpack(value):
    if type(value) is dict and set(value) == {'payload', 'identity_evidence'}:
        validate(value['payload'])
        need(type(value['identity_evidence']) is dict)
        return value['payload'], value['identity_evidence']
    validate(value)
    return value, None


def _identity_file(root, name):
    need(type(name) is str and re.fullmatch(
        r'screening-identifier-reconciliation-[a-f0-9]{64}\.json', name))
    directory = _directory(root)
    try:
        return json.loads(_private_file(directory, name, MAX_RECEIPT), object_pairs_hook=pairs)
    finally:
        os.close(directory)


def _identity_candidate(root, payload, record, rows, evidence):
    alias = record['identifier_reconciliation']
    if alias is not None:
        receipt = _identity_file(root, alias['receipt_name'])
        verified = identity.validate_identity_receipt(receipt, payload, record, rows)
        need(verified == alias and (evidence is None or evidence == receipt['identity_evidence']))
        return receipt['identity_evidence']
    need(evidence is not None)
    identity.validate_evidence(payload, record, rows, evidence)
    return evidence


def _candidate_output(root, payload, record, rows, rendered, evidence):
    if identities(rows) == record['anchors'] and record['identifier_reconciliation'] is None:
        return _verified_rows(payload, record, rows, rendered), None
    checked = _identity_candidate(root, payload, record, rows, evidence)
    core = identity.validate_evidence(payload, record, rows, checked)
    output = _verified_rows(payload, record, rows, rendered,
                            observed_anchors=core['observed_anchors'])
    need(output['symbols'] == json.loads(checked['retained_agent']['first_mini_done'][
        'result'], object_pairs_hook=pairs)['decision']['symbols'])
    return output, checked


def _persist_identity(root, payload, record, rows, evidence):
    candidate = identity.build_receipt(payload, record, rows, evidence, time.time())
    name = identity.receipt_name(candidate['alias'])
    directory = _directory(root)
    temporary = '.screening-identifier-' + os.urandom(16).hex() + '.tmp'
    try:
        try:
            receipt = json.loads(_private_file(directory, name, MAX_RECEIPT), object_pairs_hook=pairs)
        except FileNotFoundError:
            raw = canonical(candidate)
            need(0 < len(raw) <= MAX_RECEIPT)
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            try:
                os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory,
                        follow_symlinks=False)
                os.fsync(directory)
            except FileExistsError:
                pass
            os.unlink(temporary, dir_fd=directory)
            receipt = json.loads(_private_file(directory, name, MAX_RECEIPT), object_pairs_hook=pairs)
        alias = identity.validate_identity_receipt(receipt, payload, record, rows)
        need(receipt['identity_evidence'] == evidence and alias['receipt_name'] == name)
        return dict(alias=alias, receipt=receipt)
    finally:
        try: os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError: pass
        os.close(directory)


def _bind_identity(root, record, reconciliation):
    # The immutable original anchors and message timestamp are not rewritten.
    # Only an explicit, receipt-backed identifier annotation is appended.
    alias = reconciliation['alias']
    if record['identifier_reconciliation'] is not None:
        need(record['identifier_reconciliation'] == alias)
        return record
    directory = _directory(root); fd = None; db = None
    try:
        fd = os.open(JOURNAL_NAME, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
             and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600)
        db = sqlite3.connect('file:/proc/self/fd/' + str(directory) + '/' + JOURNAL_NAME + '?mode=rw',
            uri=True, timeout=3, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA trusted_schema=OFF'); db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM daily_rooms WHERE day=?', (record['day'],)).fetchone()
        need(row is not None and row['state'] == 'BOUND' and row['room_id'] == record['room_id']
             and row['url'] == record['url'] and row['updated_at'] == record['updated_at']
             and json.loads(row['anchors']) == record['anchors'])
        binding = json.loads(row['binding'], object_pairs_hook=pairs)
        need(binding.get('first_prompt_sha256') == record['first_prompt_sha256']
             and binding.get('marker') == record['marker']
             and binding.get('history_titles', []) == record['history_titles']
             and binding.get('identifier_reconciliation') is None)
        named = os.stat(JOURNAL_NAME, dir_fd=directory, follow_symlinks=False)
        need((info.st_dev, info.st_ino) == (named.st_dev, named.st_ino))
        updated = dict(binding, identifier_reconciliation=alias)
        changed = db.execute('UPDATE daily_rooms SET binding=? WHERE day=? AND room_id=? '
            'AND state=? AND anchors=? AND binding=? AND updated_at=?',
            (json.dumps(updated, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                        allow_nan=False), record['day'], record['room_id'], 'BOUND',
             row['anchors'], row['binding'], record['updated_at'])).rowcount
        need(changed == 1)
        db.execute('COMMIT')
    except BaseException:
        if db is not None and db.in_transaction: db.execute('ROLLBACK')
        raise
    finally:
        if db is not None: db.close()
        if fd is not None: os.close(fd)
        os.close(directory)
    current = _room_record(root, record['day'])
    need(current == dict(record, identifier_reconciliation=alias))
    return current


def _validated_identifier_binding(binding, archive_root):
    alias = binding['identifier_reconciliation']
    need(type(alias) is dict)
    receipt = _identity_file(archive_root, alias['receipt_name'])
    verified = identity.validate_identity_receipt(receipt, receipt['payload'],
        dict(receipt['original_room'], identifier_reconciliation=alias), receipt['observed_rows'])
    need(verified == alias and binding.get('first_prompt_sha256') == receipt['original_room'][
        'first_prompt_sha256'] and binding.get('marker') == receipt['payload']['marker'])
    return alias, receipt


def matches_identifier_binding(rows, anchors, binding, archive_root):
    """DailyRoom's narrow reader for an explicitly admitted identity migration."""
    try:
        alias, receipt = _validated_identifier_binding(binding, archive_root)
        need(anchors == alias['original_anchors'])
        return identity.matches_reconciled_rows(receipt, alias, anchors, binding, rows)
    except (ValueError, TypeError, KeyError, OSError, AttributeError):
        return False


def identifier_checkpoint_verified(anchors, binding, archive_root):
    """Ordinary checkpoint may later advance anchors; its original alias survives."""
    try:
        alias, _ = _validated_identifier_binding(binding, archive_root)
        return len(anchors) >= 4 and anchors[:4] == alias['observed_anchors']
    except (ValueError, TypeError, KeyError, OSError, AttributeError):
        return False


def recover(actor, delegated_bridge, value):
    """Existing actor-thread entry. Returns the native four-field result."""
    from .native_received_continuation import is_continuation, continue_received
    if type(value)is dict and value.get('action')=='SCREENING_RECEIVED_REVIEW':
        from .native_received_screening import review_received
        return review_received(actor,delegated_bridge,value)
    if is_continuation(value):
        return continue_received(actor, delegated_bridge, value)
    from .native_received_analysis import is_observation, observe
    if is_observation(value):
        return observe(actor, delegated_bridge, value)
    payload, evidence = _unpack(value); _actor_gate(actor)
    need(actor.claim.locked(), 'SCREENING_RECOVERY_ACTOR_CLAIM_REQUIRED')
    need(actor.thread.ident == threading.get_ident(),
         'SCREENING_RECOVERY_ACTOR_THREAD_REQUIRED')
    room, raw, native = delegated_bridge.daily_room, delegated_bridge.bridge, delegated_bridge.native
    need(room is not None and room.bridge is raw and room.native is native)
    need(_native_source_digest(native) == NATIVE_DOM_SHA256)
    with room.lock:
        retained = read_receipt(room.store.root, payload)
        if retained is not None:
            return {key: copy.deepcopy(retained['native_result'][key]) for key in _RESULT}
        day = datetime.fromtimestamp(time.time(), ZoneInfo('Asia/Jakarta')).date().isoformat()
        need(day == payload['operation'][:10])
        record = _room_record(room.store.root, day)
        need(raw.status() == 'CONNECTED')
        need(delegated_bridge.prompt_cache is not None)
        cached = delegated_bridge.prompt_cache.read(payload)
        need(cached is not None)
        old = (room.offset, room.prefix, raw._turns, raw.operation_deadline,
               raw._operation_contract, room.day, room.room_id, room._binding, room._payload)
        sentinel = object(); old_matches = room.__dict__.get('matches', sentinel)
        until = raw.clock() + min(35.0, raw.timeout)
        need(math.isfinite(until) and until > raw.clock())
        attempted = restored = False; rows = None; stage = 'BOUND_ROOM_OBSERVATION'
        try:
            raw.operation_deadline, raw._operation_contract = until, raw._dom_contract
            room.day, room.room_id, room._payload = day, record['room_id'], payload
            room._binding = {key: record[key] for key in
                ('first_prompt_sha256', 'marker', 'history_titles')}
            if record['identifier_reconciliation'] is not None:
                room._binding['identifier_reconciliation'] = record['identifier_reconciliation']
            rows = room.full_turns()
            _retain_observation(room.store.root, payload, record, rows,
                cached['rendered_prompt'], stage)
            try:
                if identities(rows) == record['anchors'] and record['identifier_reconciliation'] is None:
                    checked_evidence = None
                else:
                    checked_evidence = _identity_candidate(room.store.root, payload, record, rows, evidence)
            except (ValueError, TypeError, KeyError):
                stage = 'BOUND_ROOM_RESTORE'; attempted = True
                def exact_candidate(candidate, anchors, binding, state, **kwargs):
                    try:
                        need(state == 'BOUND' and anchors == record['anchors'])
                        _candidate_output(room.store.root, payload, record, candidate,
                            cached['rendered_prompt'], evidence)
                        return True
                    except (ValueError, TypeError, KeyError):
                        return False
                room.matches = exact_candidate
                rows = room.restore(record['anchors'], room._binding, 'BOUND')
                output, checked_evidence = _candidate_output(room.store.root, payload, record,
                    rows, cached['rendered_prompt'], evidence)
                restored = True
                _retain_observation(room.store.root, payload, record, rows,
                    cached['rendered_prompt'], 'BOUND_ROOM_RESTORED',
                    attempted=attempted, restored=restored)
            stage = 'SCREENING_PARSE'
            need(raw.page.url == record['url'])
            _retain_observation(room.store.root, payload, record, rows,
                cached['rendered_prompt'], 'OBSERVED_BEFORE_SCREENING_PARSE',
                attempted=attempted, restored=restored)
            output, checked_evidence = _candidate_output(room.store.root, payload, record,
                rows, cached['rendered_prompt'], evidence)
            room.offset, room.prefix = 2, identities(rows[:2]); raw._turns = room.turns
            previous, stable, reads = None, 0, 0; stage = 'NATIVE_STABLE_READ'
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
                    reconciliation = None
                    if checked_evidence is not None:
                        stage = 'ASSISTANT_IDENTIFIER_RECONCILIATION'
                        reconciliation = _persist_identity(room.store.root, payload, record, rows, checked_evidence)
                        record = _bind_identity(room.store.root, record, reconciliation)
                    result = {**{key: payload[key] for key in ('operation', 'marker', 'request_sha256')},
                        'output': output, 'assistant_text': answer,
                        'response_id': 'dom-' + text_sha(raw.page.url + '\n' + payload['marker'] + '\n' + answer),
                        'chart_evidence': []}
                    receipt = dict(schema_version=1, kind=PROTOCOL, payload_sha256=digest(payload),
                        room=record, rows=rows, rendered_prompt=cached['rendered_prompt'],
                        initial_reply_sha256=text_sha(rows[1]['text']), final_reply_sha256=text_sha(answer),
                        native_source_sha256=NATIVE_DOM_SHA256, native_received_at=time.time(),
                        stable_reads=4, native_poll_reads=reads, native_result=result,
                        history_restore_attempted=attempted, history_restored=restored,
                        identity_reconciliation=reconciliation)
                    receipt = _save_receipt(room.store.root, payload, receipt)
                    return {key: copy.deepcopy(receipt['native_result'][key]) for key in _RESULT}
                raw.page.wait_for_timeout(250)
            need(False, 'SCREENING_RECOVERY_REPLY_NOT_STABLE')
        except BaseException as error:
            try: observed = room.full_turns()
            except BaseException: observed = rows
            try:
                _retain_observation(room.store.root, payload, record, observed,
                    cached['rendered_prompt'], stage + '_FAILED', attempted=attempted,
                    restored=restored, error=error)
            except BaseException:
                pass
            raise
        finally:
            (room.offset, room.prefix, raw._turns, raw.operation_deadline,
             raw._operation_contract, room.day, room.room_id, room._binding, room._payload) = old
            if old_matches is sentinel: room.__dict__.pop('matches', None)
            else: room.matches = old_matches


def enqueue(actor, value):
    """Authenticated service calls this; only the existing actor touches DOM."""
    from .native_received_analysis import is_observation, validate as validate_observation
    from .native_received_continuation import is_continuation, validate as validate_continuation
    continuation = is_continuation(value)
    screening_review = type(value)is dict and value.get('action')=='SCREENING_RECEIVED_REVIEW'
    observation = is_observation(value)
    generic_observation = (type(value) is dict and value.get('action') in (
        'ANALYSIS_RECEIVED_OBSERVE','SCREENING_RECEIVED_OBSERVE')) or continuation or screening_review
    from .native_received_screening import validate as validate_screening
    payload = (validate_screening(value)if screening_review else validate_continuation(value)[0] if continuation else
        validate_observation(value) if observation else _unpack(value)[0])
    native = actor._bridge.native
    def unavailable(reason):
        if generic_observation:
            raise ScreeningRecoveryUnavailable('SCREENING_RECOVERY_' + reason)
        raise native.NotSubmitted(reason)
    if actor.stopping.is_set(): unavailable('UNAVAILABLE')
    if not actor.claim.acquire(blocking=False): unavailable('BUSY')
    queued = False
    try:
        if actor.sessions.active(): unavailable('BUSY')
        with actor.lock:
            if actor.mode != 'AUTOMATION': unavailable('BUSY')
            status, ready = actor.current_status, actor.ready
        if not observation and status in ('CHALLENGE_REQUIRED', 'LOGIN_REQUIRED'):
            if continuation or screening_review: unavailable(status)
            raise native.NotSubmitted(status)
        if not ready or (not observation and status != 'CONNECTED'): unavailable('UNAVAILABLE')
        task = dict(request=copy.deepcopy(value), kind=KIND, done=threading.Event())
        actor.queue.put_nowait(task); queued = True; actor._wake.set()
        # Allow the actor to retain/return its actual terminal disposition after
        # the hard Send deadline. This response-only grace grants no browser
        # action time: dispatch and the native deadline still expire unchanged.
        wait_seconds = (max(0.0, min(900.0, value['grant']['expires_at']-time.time())) + 2.0
            if continuation else 122 if screening_review else 45)
        if not task['done'].wait(wait_seconds): raise native.OutcomeUnknown()
        if 'error' in task:
            if generic_observation and str(task['error']) in {
                    'BUSY','UNAVAILABLE','LOGIN_REQUIRED','CHALLENGE_REQUIRED','LOGIN_IN_PROGRESS'}:
                unavailable(str(task['error']))
            raise task['error']
        result = task['result']
        if screening_review:
            need(type(result)is dict and result.get('kind')in('NATIVE_RECEIVED_SCREENING_REVIEW_V1',
                'NATIVE_RECEIVED_SCREENING_REVIEW_INCOMPLETE_V1') and result.get('operation')==payload['operation']
                and result.get('request_sha256')==payload['request_sha256'])
            return result
        if continuation:
            need(type(result) is dict and result.get('kind') in (
                'NATIVE_ANALYSIS_CONTINUATION_RESULT_V1','NATIVE_ANALYSIS_CONTINUATION_NOT_SENT_V1')
                and result.get('operation') == payload['operation']
                and result.get('request_sha256') == payload['request_sha256']
                and result.get('grant_id') == value['grant']['grant_id'])
            return result
        if observation:
            need(type(result) is dict and result.get('kind') in ('NATIVE_RECEIVED_ANALYSIS_OBSERVATION_V1','NATIVE_RECEIVED_ANALYSIS_OBSERVATION_V2','NATIVE_RECEIVED_SCREENING_OBSERVATION_V1')
                 and result.get('operation') == payload['operation']
                 and result.get('request_sha256') == payload['request_sha256']
                 and result.get('operation_completed') is False)
            return result
        need(type(result) is dict and set(result) == _RESULT)
        return {**{key: payload[key] for key in ('operation', 'marker', 'request_sha256')}, **result}
    finally:
        if not queued: actor.claim.release()
