"""Five-hour reviews inside the existing native Coordinator and LIVE gateway.

This module never runs a second coordinator or implements an exchange client.
The native plan verifier, browser receipt verifier, order reconciliation,
partial-fill handling and signed cancellation method remain authoritative.
Importing this module performs no I/O. Its separate private journal preserves
ambiguous attempts rather than replaying a browser send or cancellation.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

from worker.fill_priority import owned_intent_lock
from worker.existing_risk_monitor import estimate_owned_order
from worker.owner_source_protocol import (MATERIAL_CONTRACT, NEW_OPERATION_SECONDS,
    owner_material_request, completed_decision_fresh)

from .native_pending_protocol import (OPERATION_PATTERN, REVIEW_SCHEMA,
    _context_valid, _utc_instant, _number, closed_review_materials)


REVIEW_SECONDS = 5 * 60 * 60
RETRY_SECONDS = 60
JOURNAL_NAME = 'agent-pending-reviews.sqlite3'
_UNRESOLVED = ('REVIEWING', 'CANCEL_PREPARED', 'NEEDS_REVIEW')
_SCHEMA = '''CREATE TABLE pending_reviews (
 operation TEXT PRIMARY KEY,intent_id TEXT NOT NULL,intent_ref TEXT NOT NULL,
 sequence INTEGER NOT NULL,entry_created_at TEXT NOT NULL,created REAL NOT NULL,
 next_due REAL NOT NULL,state TEXT NOT NULL,decision TEXT,request_body TEXT NOT NULL,
 request_sha256 TEXT NOT NULL,source_sha256 TEXT,result TEXT,
 UNIQUE(intent_id,sequence))'''
_JOURNAL_SCHEMA = (
    ('index', 'sqlite_autoindex_pending_reviews_1', 'pending_reviews', None),
    ('index', 'sqlite_autoindex_pending_reviews_2', 'pending_reviews', None),
    ('table', 'pending_reviews', 'pending_reviews', _SCHEMA),
)


class PendingReviewUnavailable(ValueError):
    pass


_FAILURE_PHASES = frozenset((
    'CHECK_UNRESOLVED_ORDER', 'ADVANCE_EXECUTION', 'JOURNAL_OPEN',
    'CHECK_UNRESOLVED_REVIEW', 'RECOVER_COMPLETED', 'MONITOR_EXISTING',
    'RECOVER_CANCEL', 'RECOVER_TERMINAL', 'SELECT_DUE',
    'CHECK_UNRESOLVED_RESEARCH', 'VERIFY_INTENT', 'OWNED_INTENT_LOCK',
    'GATEWAY_RECONCILE', 'GATEWAY_OBSERVATION', 'READ_CURRENT_INTENT',
    'PENDING_ENTRY', 'CHECK_ACCEPTED_TIME', 'MARKET_CONTEXT', 'MARKET_FRESHNESS',
    'RISK_ESTIMATE', 'MARKET_MATERIALS', 'ORDER_CONTEXT', 'CLAIM_PREPARE',
    'REQUEST_PREPARE', 'CLAIM_WRITE', 'BROWSER_DISCUSSION', 'DECISION_VALIDATE',
    'SOURCE_EVIDENCE', 'DECISION_RECEIPT', 'CHECK_CURRENT_AUTHORITY',
    'SAVE_PAUSED', 'DECISION_FRESHNESS', 'SAVE_TAHAN', 'SAVE_CANCEL_PREPARED',
    'VERIFY_CANCEL_INTENT', 'BEFORE_CANCEL_RECONCILE', 'CANCEL_ENTRY_READ',
    'SAVE_CANCEL_NOT_APPLIED', 'CANCEL_REMAINDER', 'AFTER_CANCEL_RECONCILE',
    'SAVE_CANCEL_RESULT', 'BROWSER_FAILURE_STATE',
))


def _log_review_failure(phase, error, operation=None):
    """Log fixed stages/codes/locations, never error text, locals or traceback data.

    A failing logging handler or metadata accessor cannot change a native
    journal transition, return value, recovery, or exception boundary.
    """
    try:
        phase = phase if phase in _FAILURE_PHASES else 'UNCLASSIFIED'
        kind = type(error).__name__
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', kind) is None:
            kind = 'Exception'
        operation = (operation if type(operation) is str
            and re.fullmatch(OPERATION_PATTERN, operation) is not None else None)
        codes = {}
        for name in ('code', 'sqlite_errorcode', 'errno'):
            value = getattr(error, name, None)
            codes[name] = value if type(value) is int else None
        filename = function = line = None
        trace = error.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            basename = Path(code.co_filename).name
            filename = (basename if re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', basename)
                        else 'UNKNOWN')
            function = (code.co_name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,99}', code.co_name)
                        else 'UNKNOWN')
            line = trace.tb_lineno
            trace = trace.tb_next
        logging.getLogger(__name__).warning(
            'NATIVE_PENDING_REVIEW_FAILED phase=%s exception_class=%s binance_code=%s '
            'sqlite_code=%s errno=%s source_file=%s source_function=%s source_line=%s operation=%s',
            phase, kind, codes['code'], codes['sqlite_errorcode'], codes['errno'],
            filename, function, line, operation)
    except Exception:
        pass


def _native():
    """Resolve the actual deployed modules only when the native tick calls us."""
    return {name: importlib.import_module('worker.' + name) for name in
            ('robot', 'analysis', 'research_provider', 'neuroapi', 'research_guard')}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _journal_path(db):
    rows = list(db.execute('PRAGMA database_list'))
    filename = next((row[2] for row in rows if row[1] == 'main'), None)
    if type(filename) is not str or not filename or not Path(filename).is_absolute():
        raise PendingReviewUnavailable('PENDING_REVIEW_LEDGER_PATH_UNVERIFIED')
    path = Path(filename).parent / JOURNAL_NAME
    # No symlink traversal or group/world-writable parent is accepted. Native
    # worker-owned directories need not be changed to another permission mode.
    for parent in reversed((path.parent, *path.parent.parents)):
        metadata = os.lstat(parent)
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in (0, os.geteuid())
                or metadata.st_mode & 0o022):
            raise PendingReviewUnavailable('PENDING_REVIEW_DIRECTORY_UNSAFE')
    return path


def _no_native_pending_attempt(db):
    """Never infer a discarded journal is unused from its empty file alone."""
    if db.execute("SELECT 1 FROM api_requests WHERE operation GLOB 'pending-review-v1:*' LIMIT 1").fetchone():
        raise PendingReviewUnavailable('PENDING_REVIEW_INITIALIZATION_HAS_NATIVE_ATTEMPT')


def _journal_initialized(connection, db):
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    schema = tuple(tuple(row) for row in connection.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name'))
    if version == 1 and schema == _JOURNAL_SCHEMA:
        return True
    if version == 0 and not schema:
        # The worker persists its claim before native NeuroBrowser.ask inserts
        # PENDING and before any external dispatch. Absence of ALL native review
        # operations, including completed/historical rows, proves this specific
        # initial file could never have authorized a prior provider attempt.
        _no_native_pending_attempt(db)
        return False
    raise PendingReviewUnavailable('PENDING_REVIEW_JOURNAL_SCHEMA_UNVERIFIED')


@contextmanager
def _journal(db, *, create=False):
    path = _journal_path(db)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        _no_native_pending_attempt(db)
        if not create:
            yield None
            return
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR |
                         os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except FileExistsError:
            fd = os.open(path, flags)
    connection = None
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise PendingReviewUnavailable('PENDING_REVIEW_JOURNAL_UNSAFE')
        mode = 'rw' if create else 'ro'
        connection = sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=' + mode,
            uri=True, timeout=2, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.execute('PRAGMA busy_timeout=2000')
        initialized_now = False
        if create:
            # Schema plus version commit together. A kill before COMMIT leaves
            # only the recoverable, proven-unused version0/no-schema state.
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('BEGIN IMMEDIATE')
            try:
                if not _journal_initialized(connection, db):
                    connection.execute(_SCHEMA)
                    connection.execute('PRAGMA user_version=1')
                    initialized_now = True
                connection.execute('COMMIT')
            except BaseException:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        else:
            connection.execute('PRAGMA query_only=ON')
            if not _journal_initialized(connection, db):
                current = os.lstat(path)
                if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise PendingReviewUnavailable('PENDING_REVIEW_JOURNAL_CHANGED')
                yield None
                return
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise PendingReviewUnavailable('PENDING_REVIEW_JOURNAL_CHANGED')
        if initialized_now:
            os.fsync(fd)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        yield connection
    finally:
        if connection is not None:
            connection.close()
        os.close(fd)


def _intent_row(db, intent_id):
    return db.execute('SELECT * FROM order_intents WHERE id=?', (intent_id,)).fetchone()


def _verified_from_store(db, row, native):
    """Same native candidate/plan proof used by Coordinator.verified_intent."""
    candidate = db.execute('SELECT * FROM robot_candidates WHERE id=?',
                           (row['candidate_id'],)).fetchone()
    if (not candidate or row['id'] != candidate['id']
            or row['symbol'] != candidate['symbol']):
        raise PendingReviewUnavailable('PENDING_REVIEW_INTENT_UNVERIFIED')
    store = native['robot'].RobotStore(db, initialize=False)
    expected = native['robot'].build_intent(store.verified_plan(candidate), candidate['id'])
    if json.loads(row['payload']) != expected:
        raise PendingReviewUnavailable('PENDING_REVIEW_INTENT_UNVERIFIED')
    return expected, store


def _matching_intent(db, claim):
    row = _intent_row(db, claim['intent_id'])
    if (not row or row['state'] not in ('ENTRY_PENDING', 'POSITION_PROTECTED')
            or row['created'] != claim['entry_created_at']
            or type(row['payload']) is not str or _sha(row['payload']) != claim['intent_ref']):
        raise PendingReviewUnavailable('PENDING_REVIEW_INTENT_CHANGED')
    return row


def verify_pending_dispatch(db, operation, request_marker, request_sha256, today, now,
                            *, max_age=180):
    """Read-only admission of an owned, due, current native browser request.

    A caller's ON flag or model response cannot authorize this path. Admission
    needs the native settings/plan, exact durable worker claim, original native
    request hash/marker and the native api_requests PENDING/attempts=1 boundary.
    """
    try:
        if (type(operation) is not str or re.fullmatch(OPERATION_PATTERN, operation) is None
                or not _finite(now) or type(max_age) is not int or not 30 <= max_age <= 300
                or datetime.fromtimestamp(now, ZoneInfo('Asia/Jakarta')).date().isoformat() != today):
            return False
        native = _native()
        with _journal(db) as journal:
            if journal is None:
                return False
            claim = journal.execute('SELECT * FROM pending_reviews WHERE operation=?',
                                    (operation,)).fetchone()
            if (not claim or claim['state'] != 'REVIEWING' or not _finite(claim['created'])
                    or not 0 <= now - claim['created']
                    or claim['request_sha256'] != request_sha256):
                return False
            row = _matching_intent(db, claim)
            intent, store = _verified_from_store(db, row, native)
            if not store.settings()['robot_on']:
                return False
            body = json.loads(claim['request_body'])
            owner_material = owner_material_request(body)
            if now - claim['created'] > (NEW_OPERATION_SECONDS if owner_material else max_age):
                return False
            validation_at = claim['created'] if owner_material else now
            order_context = body.get('context', {}).get('pending_order_context')
            accepted = (_utc_instant(order_context['accepted_at']) if type(order_context) is dict
                else _utc_instant(claim['entry_created_at']))
            if now - accepted < REVIEW_SECONDS:
                return False
            if type(order_context) is dict and any(order_context.get(key) != expected for key, expected in (
                    ('symbol', intent['symbol']), ('side', intent['side']),
                    ('entry_client_order_id', intent['client_order_id']),
                    ('entry_price', intent['entry']['price']), ('quantity', intent['entry']['quantity']),
                    ('stop_loss', intent['protection']['stop_loss']),
                    ('take_profit', intent['protection']['take_profit']))):
                return False
            if (body.get('output_schema') != REVIEW_SCHEMA or body.get('operation') != operation
                    or body.get('context', {}).get('symbol') != intent['symbol']
                    or body['context'].get('pending_review') != {
                        'intent_ref': claim['intent_ref'], 'symbol': intent['symbol'],
                        'entry_created_at': claim['entry_created_at']}
                    or not _context_valid(body['context'], operation=operation, now=validation_at,
                                          max_age=max_age, created=claim['created'])
                    or not closed_review_materials(body['context'])):
                return False
            rebuilt, serialized, digest = native['research_provider'].request_body(
                operation, body['prompt'], REVIEW_SCHEMA, body['context'])
            if (body != rebuilt or serialized != claim['request_body']
                    or digest != request_sha256 or body.get('marker') != request_marker):
                return False
            api = db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone()
            return bool(api and api['state'] == 'PENDING' and api['attempts'] == 1
                and api['body_hash'] == digest and api['output'] is None
                and api['idempotency'] is None and _finite(api['created'])
                and claim['created'] <= api['created'] <= now
                and now - api['created'] <= (NEW_OPERATION_SECONDS if owner_material else max_age))
    except Exception:
        return False


def _report(coordinator, account, state='WAITING', reason=None):
    if state == 'WAITING':
        return coordinator.store.report(state, account, wait_reason=reason or coordinator.pause_reason())
    return coordinator.store.report(state, account, reason=reason)


def _gate(coordinator, today):
    try:
        return coordinator.gateway_allowed() is True
    except Exception:
        return False


@contextmanager
def _maintenance_reads(coordinator, native):
    """An existing order's research clock is independent of the entry day.

    This scope is used only for its verified pending discussion/public data,
    never coordinator.advance_execution. Keep owner ON/stopping checks while
    avoiding an old entry-day predicate aborting at midnight. Financial writes
    retain their separate owned-intent lock and current mutation predicate.
    """
    guard = native['research_guard']
    with guard.gateway_transaction_reads(), guard.research_reads(coordinator.gateway_allowed):
        yield


def _set_state(journal, operation, state, *, decision=None, next_due=None,
               source_sha256=None, result=None):
    journal.execute('UPDATE pending_reviews SET state=?,decision=COALESCE(?,decision),'
        'next_due=COALESCE(?,next_due),source_sha256=COALESCE(?,source_sha256),'
        'result=COALESCE(?,result) WHERE operation=?',
        (state, decision, next_due, source_sha256,
         None if result is None else _json(result), operation))


def _accepted_at(row, entry=None, *, now=None):
    """Use the exchange's acceptance clock, never the local submission claim."""
    now = time.time() if now is None else now
    result = json.loads(row['result']) if row.get('result') else {}
    supplied = result.get('accepted_at') if type(result) is dict else None
    if type(supplied) is str:
        accepted = _utc_instant(supplied)
    else:
        wire = entry.get('time') if type(entry) is dict else None
        if type(wire) is not int or wire <= 0:
            raise PendingReviewUnavailable('PENDING_REVIEW_ACCEPTED_AT_UNAVAILABLE')
        accepted = wire / 1000
    if not _finite(now) or accepted > now + 30:
        raise PendingReviewUnavailable('PENDING_REVIEW_ACCEPTED_AT_UNVERIFIED')
    return accepted


def _order_context(intent, row, entry, accepted_at, *, now):
    """Copy frozen order facts; this snapshot cannot change order parameters."""
    result = json.loads(row['result']) if row.get('result') else {}
    risk = intent.get('risk_target_usdt')
    if risk is None and type(result) is dict:
        risk = result.get('risk_target_usdt')
    return {'symbol': intent['symbol'], 'side': intent['side'],
        'entry_price': intent['entry']['price'], 'quantity': intent['entry']['quantity'],
        'entry_client_order_id': intent['client_order_id'], 'entry_order_id': str(entry.get('orderId', '')),
        'accepted_at': datetime.fromtimestamp(accepted_at, timezone.utc).isoformat(),
        'age_seconds': max(0, now - accepted_at), 'status': entry['status'],
        'filled_quantity': entry.get('executedQty', '0'),
        'stop_loss': intent['protection']['stop_loss'],
        'take_profit': intent['protection']['take_profit'], 'risk_usdt': risk,
        'estimated_loss': intent.get('risk_usdt'), 'estimated_profit': intent.get('net_reward_usdt'),
        'observed_at': now}


def _prompt(intent, intent_ref, order=None):
    order = order or {'symbol': intent['symbol'], 'side': intent['side'],
        'entry_price': intent['entry']['price'], 'quantity': intent['entry']['quantity'],
        'stop_loss': intent['protection']['stop_loss'],
        'take_profit': intent['protection']['take_profit']}
    # Operation hashes and schema belong to the integration, not the discussion.
    lines = ['Dengan chart terbaru 1H dan 15M, evaluasi kembali order yang sama untuk coin ini.',
        'Coin '+order['symbol']+', arah '+order['side']+', harga LIMIT '+order['entry_price']+
        ', quantity '+order['quantity']+', SL '+order['stop_loss']+', TP '+order['take_profit']+'.']
    if order.get('accepted_at'):
        lines.append('Diterima Binance pada '+order['accepted_at']+'. Status '+order['status']+
            ', quantity terisi '+str(order['filled_quantity'])+'.')
    # Budgets and detailed cost estimates remain in pending_order_context for
    # internal Risk Manager/receipts; they never become Neurobro WEB material.
    lines.append('Apakah order dengan ENTRY, SL dan TP tersebut masih valid? '
        'Berikan keputusan TAHAN atau BATALKAN beserta alasan singkat.')
    return '\n\n'.join(lines)


def _decision_receipt(db, operation, *, accepted_at=None, now=None):
    """Persist the actual response time/reason; recovery must not reset its clock."""
    now = time.time() if now is None else now
    row = db.execute('SELECT assistant_text,assistant_sha256,received_at '
        'FROM research_browser_responses WHERE operation=?', (operation,)).fetchone()
    if (not row or type(row['assistant_text']) is not str
            or _sha(row['assistant_text']) != row['assistant_sha256']
            or not _finite(row['received_at']) or row['received_at'] > now + 30):
        raise PendingReviewUnavailable('PENDING_REVIEW_DECISION_RECEIPT_UNVERIFIED')
    return {'accepted_at': accepted_at, 'decision_at': row['received_at'],
        'reason': row['assistant_text'], 'assistant_sha256': row['assistant_sha256']}


def _completed_policy_output(coordinator, operation, kind, cycle, symbol, schema):
    """Read a completed native job/API pair; labels alone carry no policy."""
    from worker.neuroapi import canonical_output, screening_count
    try:
        db = coordinator.db
        job = db.execute('SELECT * FROM robot_jobs WHERE operation=?', (operation,)).fetchone()
        api = db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone()
        if (not job or job['operation'] != operation or job['cycle'] != cycle
                or job['kind'] != kind or job['symbol'] != symbol or job['state'] != 'COMPLETE'
                or not api or api['state'] != 'COMPLETE' or api['attempts'] != 1
                or type(api['attempts']) is not int or api['idempotency'] is not None
                or api['failure_code'] is not None or not _finite(api['created'])
                or type(api['body_hash']) is not str or re.fullmatch('[a-f0-9]{64}', api['body_hash']) is None
                or type(api['output']) is not str):
            return None
        value = json.loads(api['output'])
        catalog = ({item: {} for item in value.get('symbols', []) if type(item) is str}
            if screening_count(schema) is not None and type(value) is dict else None)
        if canonical_output(value, schema, catalog) != api['output']:
            return None
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_browser_requests'").fetchone()
        source = db.execute('SELECT * FROM research_browser_requests WHERE operation=?', (operation,)).fetchone() if table else None
        if source is not None:
            native = _native()
            body = json.loads(source['request_body'])
            if (body.get('operation') != operation or body.get('output_schema') != schema
                    or kind == 'ANALYSIS' and body.get('context', {}).get('symbol') != symbol):
                return None
            rebuilt, serialized, request_hash = native['research_provider'].request_body(
                operation, body['prompt'], schema, body['context'])
            if (rebuilt != body or serialized != source['request_body']
                    or request_hash != source['request_sha256'] or request_hash != api['body_hash']):
                return None
            evidence = native['research_provider'].source_evidence(db, operation)
            if (evidence.get('source_type') != native['research_provider'].BROWSER_SOURCE
                    or type(evidence.get('provider_evidence_sha256')) is not str
                    or re.fullmatch('[a-f0-9]{64}', evidence['provider_evidence_sha256']) is None):
                return None
        return value
    except Exception:
        return None


def _entry_remainder_receipt(intent, entry, order_id, *, filled_at_least='0'):
    """Project only the existing owned terminal GET returned by cancellation."""
    if (type(entry) is not dict or entry.get('status') not in ('CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'FILLED')
            or entry.get('clientOrderId') != intent['client_order_id']
            or str(entry.get('orderId')) != str(order_id)
            or re.fullmatch(r'[1-9]\d*', str(order_id)) is None
            or not _number(filled_at_least, zero=True) <= _number(entry.get('executedQty'), zero=True)
                <= _number(intent['entry']['quantity'])):
        raise PendingReviewUnavailable('PENDING_REVIEW_CANCELLATION_UNVERIFIED')
    return {'status': entry['status'], 'client_order_id': entry['clientOrderId'],
        'order_id': str(entry['orderId']), 'filled_quantity': entry['executedQty']}


def _confirmed_cancel_receipt(coordinator, candidate, journal, native, *, zero_fill=True):
    """Recognize existing confirmed cancellation proof without freeing a fill."""
    try:
        db = coordinator.db
        stored = db.execute('SELECT * FROM robot_candidates WHERE id=?', (candidate['id'],)).fetchone()
        row = _intent_row(db, candidate['id'])
        states = ('REJECTED',) if zero_fill else ('REJECTED', 'POSITION_PROTECTED', 'CLOSED')
        if (not stored or dict(stored) != dict(candidate) or stored['status'] not in states
                or not row or row['candidate_id'] != stored['id'] or row['state'] != stored['status']):
            return None
        intent, _ = _verified_from_store(db, row, native)
        fill_receipt = db.execute('SELECT * FROM robot_entry_receipts WHERE id=?', (intent['client_order_id'],)).fetchone()
        result = json.loads(row['result'])
        filled = _number(result.get('filled_quantity'), zero=True)
        partial = filled > 0
        if (result.get('source') != 'BINANCE_FUTURES' or result.get('state') != row['state']
                or result.get('symbol') != intent['symbol']
                or result.get('client_order_id') != intent['client_order_id']
                or type(result.get('order_id')) is not str
                or re.fullmatch(r'[1-9]\d*', result['order_id']) is None
                or partial and (zero_fill or row['state'] not in ('POSITION_PROTECTED', 'CLOSED')
                    or filled >= _number(intent['entry']['quantity']) or not fill_receipt
                    or fill_receipt['symbol'] != intent['symbol'])
                or not partial and (row['state'] != 'REJECTED' or fill_receipt is not None)):
            return None
        claim = journal.execute('SELECT * FROM pending_reviews WHERE intent_id=? '
            'ORDER BY sequence DESC LIMIT 1', (row['id'],)).fetchone()
        intent_ref = _sha(row['payload'])
        if (not claim or claim['state'] != 'COMPLETE' or claim['decision'] != 'BATALKAN'
                or claim['intent_ref'] != intent_ref or claim['entry_created_at'] != row['created']
                or type(claim['sequence']) is not int or claim['sequence'] < 1
                or claim['operation'] != 'pending-review-v1:' + intent_ref[:32] + ':' + str(claim['sequence'])
                or not _finite(claim['created'])):
            return None
        body = json.loads(claim['request_body'])
        context = body.get('context', {})
        if (body.get('operation') != claim['operation'] or body.get('output_schema') != REVIEW_SCHEMA
                or context.get('symbol') != intent['symbol']
                or context.get('pending_review') != {'intent_ref': intent_ref,
                    'symbol': intent['symbol'], 'entry_created_at': row['created']}):
            return None
        rebuilt, serialized, request_hash = native['research_provider'].request_body(
            claim['operation'], body['prompt'], REVIEW_SCHEMA, context)
        source = db.execute('SELECT * FROM research_browser_requests WHERE operation=?', (claim['operation'],)).fetchone()
        api = db.execute('SELECT * FROM api_requests WHERE operation=?', (claim['operation'],)).fetchone()
        if (rebuilt != body or serialized != claim['request_body'] or _sha(serialized) != request_hash
                or request_hash != claim['request_sha256'] or not source
                or source['request_body'] != serialized or source['request_sha256'] != request_hash
                or not api or api['body_hash'] != request_hash or api['state'] != 'COMPLETE'
                or api['attempts'] != 1 or type(api['attempts']) is not int or api['idempotency'] is not None
                or api['failure_code'] is not None or not _finite(api['created'])
                or api['created'] < claim['created']):
            return None
        value = json.loads(api['output'])
        if (native['neuroapi'].canonical_output(value, REVIEW_SCHEMA) != api['output']
                or value != {'symbol': intent['symbol'], 'order_ref': intent_ref, 'decision': 'BATALKAN'}):
            return None
        evidence = native['research_provider'].source_evidence(db, claim['operation'])
        if (evidence.get('source_type') != native['research_provider'].BROWSER_SOURCE
                or type(claim['source_sha256']) is not str
                or re.fullmatch('[a-f0-9]{64}', claim['source_sha256']) is None
                or evidence.get('provider_evidence_sha256') != claim['source_sha256']):
            return None
        receipt = _decision_receipt(db, claim['operation'])
        if receipt['decision_at'] < api['created']:
            return None
        retained = json.loads(claim['result'])
        observation = retained.get('gateway_observation', {})
        if (retained.get('decision_not_applied_after_restart') is True
                or retained.get('decision_not_applied_after_fill') is True
                or observation.get('source') != 'BINANCE_FUTURES'
                or observation.get('state') != ('POSITION_PROTECTED' if partial else 'REJECTED')
                or observation.get('symbol') != intent['symbol']
                or observation.get('client_order_id') != intent['client_order_id']
                or observation.get('order_id') != result['order_id']
                or _number(observation.get('filled_quantity'), zero=True) != filled
                or any(key in retained and retained[key] != receipt[key]
                    for key in ('decision_at', 'reason', 'assistant_sha256'))):
            return None
        if partial:
            remainder = retained.get('entry_remainder')
            if (type(remainder) is not dict
                    or set(remainder) != {'status', 'client_order_id', 'order_id', 'filled_quantity'}
                    or remainder['status'] not in ('CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH')
                    or remainder['client_order_id'] != intent['client_order_id']
                    or remainder['order_id'] != result['order_id']
                    or _number(remainder['filled_quantity'], zero=True) != filled
                    or observation.get('sl_confirmed') is not True or observation.get('tp_confirmed') is not True
                    or _utc_instant(fill_receipt['confirmed_at']) > receipt['decision_at']
                    or datetime.fromtimestamp(_utc_instant(fill_receipt['confirmed_at']),
                        ZoneInfo('Asia/Jakarta')).date().isoformat() != fill_receipt['entry_day']):
                return None
        return receipt
    except Exception:
        return None


def confirmed_cancel_result(coordinator, candidate):
    """Return only a proven completed BATALKAN with no filled entry or receipt."""
    if not coordinator.db.execute("SELECT 1 FROM api_requests WHERE operation GLOB 'pending-review-v1:*' LIMIT 1").fetchone():
        return False
    with _journal(coordinator.db) as journal:
        if journal is None:
            raise PendingReviewUnavailable('PENDING_REVIEW_JOURNAL_UNVERIFIED')
        return _confirmed_cancel_receipt(coordinator, candidate, journal, _native()) is not None


def screening_day_exclusions(coordinator, today):
    """Exclude only proved market HOLDs or confirmed cancellations for this day."""
    from worker.neuroapi import SETUP_SCHEMA, SETUP_SCHEMA_V4
    if (type(today) is not str or re.fullmatch(r'\d{4}-\d{2}-\d{2}', today) is None
            or datetime.fromisoformat(today).date().isoformat() != today):
        raise PendingReviewUnavailable('PENDING_REVIEW_DAY_UNVERIFIED')
    db = coordinator.db
    excluded = set()
    holds = (db.execute("SELECT p.* FROM robot_candidates p JOIN robot_cycles c ON c.id=p.cycle "
        "WHERE p.status='HOLD' AND c.day=?", (today,))
        if db.execute("SELECT 1 FROM robot_candidates WHERE status='HOLD' LIMIT 1").fetchone() else ())
    for candidate in holds:
        if candidate['cycle'][:10] != today or candidate['id'] != candidate['cycle'] + ':analysis-v9:' + candidate['symbol']:
            continue
        for schema in (SETUP_SCHEMA, SETUP_SCHEMA_V4):
            value = _completed_policy_output(coordinator, candidate['id'], 'ANALYSIS',
                candidate['cycle'], candidate['symbol'], schema)
            if value is not None and value['side'] == 'HOLD' and value['symbol'] == candidate['symbol']:
                excluded.add(candidate['symbol'])
                break
    if db.execute("SELECT 1 FROM api_requests WHERE operation GLOB 'pending-review-v1:*' LIMIT 1").fetchone():
        with _journal(db) as journal:
            if journal is None:
                raise PendingReviewUnavailable('PENDING_REVIEW_JOURNAL_UNVERIFIED')
            native = _native()
            for candidate in db.execute("SELECT * FROM robot_candidates WHERE status IN ('REJECTED','POSITION_PROTECTED','CLOSED')"):
                receipt = _confirmed_cancel_receipt(coordinator, candidate, journal, native, zero_fill=False)
                if (receipt is not None and datetime.fromtimestamp(receipt['decision_at'],
                        ZoneInfo('Asia/Jakarta')).date().isoformat() == today):
                    excluded.add(candidate['symbol'])
    return excluded


def screening_day_replacement_due(coordinator, cycle, data):
    """Count only completed last-screen choices blocked by proved daily policy."""
    from worker.account_state import screening_contract
    if (type(cycle) is not str or re.fullmatch(r'\d{4}-\d{2}-\d{2}:robot-v9:(0|[1-9]\d{0,11})', cycle) is None
            or type(data) is not dict or type(data.get('screen')) is not int or data['screen'] < 0):
        return 0
    outputs = []
    for count in (1, 2):
        operation = cycle + ':screening:' + str(data['screen']) + ':' + str(count)
        _, schema = screening_contract(count)
        value = _completed_policy_output(coordinator, operation, 'SCREENING', cycle, None, schema)
        if value is not None:
            outputs.append(value['symbols'])
    if len(outputs) != 1:
        return 0
    excluded = screening_day_exclusions(coordinator, cycle[:10])
    return len(set(outputs[0]).intersection(excluded))


def screening_day_rejected_result(coordinator, candidate):
    """Reopen only a proved day-policy rejection before any analysis claim."""
    from worker.account_state import screening_contract
    try:
        if (candidate['status'] != 'REJECTED' or candidate['failure_code'] != 'ROBOT_DAY_EXCLUDED'
                or candidate['plan'] is not None):
            return False
        db = coordinator.db
        stored = db.execute('SELECT * FROM robot_candidates WHERE id=?', (candidate['id'],)).fetchone()
        cycle = db.execute('SELECT * FROM robot_cycles WHERE id=?', (candidate['cycle'],)).fetchone()
        if (stored is None or dict(stored) != dict(candidate) or cycle is None
                or cycle['day'] != candidate['cycle'][:10]
                or candidate['id'] != candidate['cycle'] + ':analysis-v9:' + candidate['symbol']
                or any(db.execute('SELECT 1 FROM '+table+' WHERE '+key+'=?', (candidate['id'],)).fetchone()
                    for table, key in (('api_requests', 'operation'), ('robot_jobs', 'operation'),
                        ('order_intents', 'candidate_id')))):
            return False
        data = json.loads(cycle['data'])
        if type(data.get('screen')) is not int or data['screen'] < 0:
            return False
        choices = []
        for count in (1, 2):
            operation = candidate['cycle'] + ':screening:' + str(data['screen']) + ':' + str(count)
            _, schema = screening_contract(count)
            value = _completed_policy_output(coordinator, operation, 'SCREENING', candidate['cycle'], None, schema)
            if value is not None:
                choices.append(value['symbols'])
        return (len(choices) == 1 and candidate['symbol'] in choices[0]
            and candidate['symbol'] in screening_day_exclusions(coordinator, cycle['day']))
    except Exception:
        return False


def _recover_completed_decisions(coordinator, journal, native, now):
    """Drain completed replies without replaying their browser operations.

    A retained TAHAN keeps its original five-hour clock. A cancellation that
    was never prepared is not blindly applied after an interruption: one new
    current-order review may discuss that recorded decision with fresh charts.
    CANCEL_PREPARED ambiguity remains under the original gateway reconciliation.
    """
    for claim in journal.execute("SELECT * FROM pending_reviews WHERE state='REVIEWING' "
            "OR (state='PAUSED' AND decision='TAHAN')").fetchall():
        api = coordinator.db.execute('SELECT * FROM api_requests WHERE operation=?',
                                    (claim['operation'],)).fetchone()
        if not api or api['state'] != 'COMPLETE':
            continue
        value = json.loads(api['output'])
        if (api['body_hash'] != claim['request_sha256']
                or json.loads(native['neuroapi'].canonical_output(value, REVIEW_SCHEMA)) != value
                or value.get('decision') not in ('TAHAN', 'BATALKAN')
                or value.get('order_ref') != claim['intent_ref']):
            continue
        current = _intent_row(coordinator.db, claim['intent_id'])
        if current is not None and current['state'] == 'PROTECTION_INCOMPLETE':
            continue  # The independent native fill lane has priority.
        terminal = bool(current is not None and current['state'] in ('CLOSED', 'REJECTED')
            and current['created'] == claim['entry_created_at']
            and _sha(current['payload']) == claim['intent_ref'])
        row = current if terminal else _matching_intent(coordinator.db, claim)
        intent, _ = _verified_from_store(coordinator.db, row, native)
        if value['symbol'] != intent['symbol']:
            raise PendingReviewUnavailable('PENDING_REVIEW_DECISION_UNVERIFIED')
        evidence = native['research_provider'].source_evidence(coordinator.db, claim['operation'])
        source = evidence.get('provider_evidence_sha256')
        if (evidence.get('source_type') != native['research_provider'].BROWSER_SOURCE
                or type(source) is not str or re.fullmatch(r'[a-f0-9]{64}', source) is None):
            raise PendingReviewUnavailable('PENDING_REVIEW_SOURCE_UNVERIFIED')
        body = json.loads(claim['request_body'])
        receipt = _decision_receipt(coordinator.db, claim['operation'],
            accepted_at=body.get('context', {}).get('pending_order_context', {}).get('accepted_at'), now=now)
        receipt['decision_not_applied_after_restart'] = value['decision'] == 'BATALKAN' and not terminal
        receipt['order_already_terminal'] = terminal
        _set_state(journal, claim['operation'], 'COMPLETE', decision=value['decision'],
            next_due=(receipt['decision_at'] + REVIEW_SECONDS if value['decision'] == 'TAHAN' else now),
            source_sha256=source, result=receipt)


def _pending_entry(coordinator, intent, native, today):
    if not _gate(coordinator, today):
        return None
    guard = native['research_guard']
    with (owned_intent_lock(coordinator.db, intent['client_order_id']),
          guard.gateway_transaction_reads(), guard.gateway_mutations(coordinator.gateway_allowed)):
        with coordinator.gateway._operation():
            coordinator.gateway._validate_intent(intent)
            coordinator.gateway._account_config()
            entry = coordinator.gateway._entry(intent)
    if not _gate(coordinator, today):
        return None
    return entry if entry['status'] in ('NEW', 'PARTIALLY_FILLED') else None


def _monitor_existing(coordinator, account, today, native):
    """Retain native active-position protection while new research is blocked.

    Calling advance_execution here could submit a queued candidate. This finite
    loop invokes only the native verified-intent/reconcile/receipt methods and
    uses the very same current-ON mutation context as its active-order branch.
    """
    rows = list(coordinator.db.execute("SELECT * FROM order_intents WHERE state IN "
        "('ENTRY_PENDING','POSITION_PROTECTED','PROTECTION_INCOMPLETE') ORDER BY rowid LIMIT 3"))
    if len(rows) > 2:
        return _report(coordinator, account, 'NEEDS_REVIEW', 'PENDING_REVIEW_CAPACITY_UNVERIFIED')
    guard = native['research_guard']
    for row in rows:
        if not _gate(coordinator, today):
            return _report(coordinator, account)
        try:
            intent = coordinator.verified_intent(row)
            with (owned_intent_lock(coordinator.db, intent['client_order_id']),
                  guard.gateway_transaction_reads(), guard.gateway_mutations(coordinator.gateway_allowed)):
                value = coordinator.gateway.reconcile(intent)
                report = coordinator.record_gateway_observation(row['id'], row['candidate_id'], value, account)
        except Exception as error:
            _log_review_failure('MONITOR_EXISTING', error)
            return coordinator.mark_unknown(row['id'], row['candidate_id'], account)
        if report.get('bot_status') in ('NEEDS_REVIEW', 'PROTECTION_INCOMPLETE'):
            return report
    return None


def _recover_cancel_checkpoints(coordinator, journal, today, native):
    """Resolve one owned cancellation only from the original gateway's reads.

    This never sends DELETE. An entry still NEW/PARTIALLY_FILLED remains
    uncertain; a confirmed terminal remainder may retire its exact review.
    Active fills/protections are reconciled by _monitor_existing first.
    """
    guard = native['research_guard']
    claims = journal.execute("SELECT * FROM pending_reviews WHERE decision='BATALKAN' "
        "AND state IN ('CANCEL_PREPARED','NEEDS_REVIEW')").fetchall()
    for claim in claims:
        row = _intent_row(coordinator.db, claim['intent_id'])
        if (not row or row['created'] != claim['entry_created_at']
                or _sha(row['payload']) != claim['intent_ref'] or not _gate(coordinator, today)):
            continue
        intent = coordinator.verified_intent(row)
        result = json.loads(row['result']) if row['result'] else None
        if (type(result) is not dict or result.get('source') != 'BINANCE_FUTURES'
                or result.get('client_order_id') != intent['client_order_id']):
            continue
        resolved = row['state'] in ('REJECTED', 'CLOSED') and result.get('state') == row['state']
        remainder = None
        if row['state'] == 'POSITION_PROTECTED':
            if result.get('sl_confirmed') is not True or result.get('tp_confirmed') is not True:
                continue
            with (owned_intent_lock(coordinator.db, intent['client_order_id']),
                  guard.gateway_transaction_reads(), guard.gateway_mutations(coordinator.gateway_allowed)):
                with coordinator.gateway._operation():
                    coordinator.gateway._validate_intent(intent)
                    coordinator.gateway._account_config()
                    entry = coordinator.gateway._entry(intent)
            resolved = entry['status'] in ('CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'FILLED')
            if resolved:
                remainder = _entry_remainder_receipt(intent, entry, result.get('order_id'),
                    filled_at_least=result.get('filled_quantity'))
        if resolved and _gate(coordinator, today):
            retained = {'resolution': 'NATIVE_GATEWAY_RECONCILED', 'gateway_observation': result,
                'resolved_at': time.time()}
            if remainder is not None:
                retained['entry_remainder'] = remainder
            _set_state(journal, claim['operation'], 'COMPLETE', result=retained)


_TERMINAL_REVIEW = 'ORDER_TERMINAL_NOT_APPLIED'
_TERMINAL_API_FIELDS = ('operation', 'idempotency', 'body_hash', 'state',
                        'created', 'attempts', 'output', 'failure_code')


def _terminal_review_api(db, claim, intent, native):
    """Bind the exact uncertain request; never waive an unrelated operation."""
    body = json.loads(claim['request_body'])
    context = body.get('context', {})
    if (claim['operation'] != 'pending-review-v1:' + claim['intent_ref'][:32] + ':' + str(claim['sequence'])
            or body.get('operation') != claim['operation'] or body.get('output_schema') != REVIEW_SCHEMA
            or context.get('symbol') != intent['symbol']
            or context.get('pending_review') != {'intent_ref': claim['intent_ref'],
                'symbol': intent['symbol'], 'entry_created_at': claim['entry_created_at']}):
        raise PendingReviewUnavailable('PENDING_REVIEW_TERMINAL_REQUEST_UNVERIFIED')
    rebuilt, serialized, request_hash = native['research_provider'].request_body(
        claim['operation'], body['prompt'], REVIEW_SCHEMA, context)
    if (rebuilt != body or serialized != claim['request_body']
            or request_hash != claim['request_sha256'] or _sha(serialized) != request_hash):
        raise PendingReviewUnavailable('PENDING_REVIEW_TERMINAL_REQUEST_UNVERIFIED')
    api = db.execute('SELECT * FROM api_requests WHERE operation=?', (claim['operation'],)).fetchone()
    source = db.execute('SELECT request_body,request_sha256 FROM research_browser_requests '
                        'WHERE operation=?', (claim['operation'],)).fetchone()
    if api is None:
        if source is not None:
            raise PendingReviewUnavailable('PENDING_REVIEW_TERMINAL_REQUEST_UNVERIFIED')
        return None
    value = {key: api[key] for key in _TERMINAL_API_FIELDS}
    if (value['body_hash'] != request_hash or value['state'] not in ('PENDING', 'NEEDS_REVIEW', _TERMINAL_REVIEW)
            or value['attempts'] != 1 or value['output'] is not None or value['idempotency'] is not None
            or not _finite(value['created']) or value['created'] < claim['created']
            or source is None or source['request_body'] != serialized
            or source['request_sha256'] != request_hash):
        raise PendingReviewUnavailable('PENDING_REVIEW_TERMINAL_REQUEST_UNVERIFIED')
    return value


def _retire_terminal_review_api(db, before, operation):
    """One atomic CAS, after its forensic checkpoint is durable in the sidecar.

    Retain the unknown response, error, attempts and request identity. A crash
    after this statement still leaves the review unresolved until recovery.
    The ordinary browser refuses to replay this distinct, non-COMPLETE state.
    """
    if before is None:
        return not any(db.execute('SELECT 1 FROM '+table+' WHERE operation=?',
                                  (operation,)).fetchone() for table in
                       ('api_requests', 'research_browser_requests', 'research_browser_responses'))
    current = db.execute('SELECT * FROM api_requests WHERE operation=?', (before['operation'],)).fetchone()
    if current is None:
        return False
    value = {key: current[key] for key in _TERMINAL_API_FIELDS}
    if value == dict(before, state=_TERMINAL_REVIEW):
        return True  # Interrupted after the exact CAS; do not write it again.
    if value != before or before['state'] not in ('PENDING', 'NEEDS_REVIEW'):
        return False
    query = ('UPDATE api_requests SET state=? WHERE ' +
             ' AND '.join(key + ' IS ?' for key in _TERMINAL_API_FIELDS))
    return db.execute(query, (_TERMINAL_REVIEW, *(before[key] for key in _TERMINAL_API_FIELDS))).rowcount == 1


def _recover_terminal_order_reviews(coordinator, journal, account, today, native):
    """Retire an inapplicable discussion only after the owned order is terminal.

    An uncertain browser response is still uncertain. Its exact claim and any
    recorded decision remain in the journal; terminal retirement does not assert an
    analyst reply or authorize a cancellation/replay. The original gateway and
    native observation verifier, rather than an empty account or an old label,
    must confirm that this same order has no remaining entry or position.
    """
    guard = native['research_guard']
    claims = journal.execute("SELECT * FROM pending_reviews WHERE state IN "
        "('REVIEWING','CANCEL_PREPARED','NEEDS_REVIEW')").fetchall()
    for claim in claims:
        row = _intent_row(coordinator.db, claim['intent_id'])
        if (not row or row['state'] not in ('CLOSED', 'REJECTED')
                or row['created'] != claim['entry_created_at']
                or type(row['payload']) is not str or _sha(row['payload']) != claim['intent_ref']
                or not _gate(coordinator, today)):
            continue
        intent = coordinator.verified_intent(row)
        with (owned_intent_lock(coordinator.db, intent['client_order_id']),
              guard.gateway_transaction_reads(), guard.gateway_mutations(coordinator.gateway_allowed)):
            observation = coordinator.gateway.reconcile(intent)
            coordinator.record_gateway_observation(row['id'], row['candidate_id'], observation, account)
        current = _intent_row(coordinator.db, claim['intent_id'])
        if (not current or current['state'] not in ('CLOSED', 'REJECTED')
                or current['created'] != claim['entry_created_at']
                or current['payload'] != row['payload'] or not current['result']
                or not _gate(coordinator, today)):
            continue
        result = json.loads(current['result'])
        if (type(result) is not dict or result.get('state') != current['state']
                or result.get('source') != 'BINANCE_FUTURES'
                or result.get('client_order_id') != intent['client_order_id']):
            continue
        api = _terminal_review_api(coordinator.db, claim, intent, native)
        previous = json.loads(claim['result']) if claim['result'] else None
        if type(previous) is dict and previous.get('resolution') == 'NATIVE_GATEWAY_ORDER_TERMINAL_PREPARED':
            prepared = previous
            before = prepared['api_before']
            if (prepared.get('request_sha256') != claim['request_sha256']
                    or prepared.get('api_before_sha256') != _sha(_json(before))
                    or api not in (before, None if before is None else dict(before, state=_TERMINAL_REVIEW))):
                raise PendingReviewUnavailable('PENDING_REVIEW_TERMINAL_CHECKPOINT_UNVERIFIED')
        else:
            if api is not None and api['state'] == _TERMINAL_REVIEW:
                raise PendingReviewUnavailable('PENDING_REVIEW_TERMINAL_CHECKPOINT_UNVERIFIED')
            prepared = {'resolution': 'NATIVE_GATEWAY_ORDER_TERMINAL_PREPARED',
                'request_sha256': claim['request_sha256'], 'previous_state': claim['state'],
                'api_before': api, 'api_before_sha256': _sha(_json(api)),
                'prior_result': previous, 'gateway_observation': result,
                'prepared_at': time.time()}
            _set_state(journal, claim['operation'], claim['state'], result=prepared)
        if not _gate(coordinator, today) or not _retire_terminal_review_api(
                coordinator.db, prepared['api_before'], claim['operation']):
            continue
        _set_state(journal, claim['operation'], _TERMINAL_REVIEW, result={
            'resolution': 'NATIVE_GATEWAY_ORDER_TERMINAL',
            'review_outcome': 'NOT_INFERRED', 'previous_state': prepared['previous_state'],
            'prior_result': prepared['prior_result'], 'request_sha256': claim['request_sha256'],
            'api_before': prepared['api_before'], 'api_before_sha256': prepared['api_before_sha256'],
            'prepared_gateway_observation': prepared['gateway_observation'],
            'prepared_at': prepared['prepared_at'],
            'gateway_observation': result, 'resolved_at': time.time()})


def review_then_advance(coordinator, account, today):
    """Called once under the native Coordinator cycle lock, before new entries."""
    if not _gate(coordinator, today):
        return _report(coordinator, account)
    native, now = _native(), time.time()
    phase, operation = 'CHECK_UNRESOLVED_ORDER', None
    try:
        # Preserve the original highest-priority unknown-order reconciliation.
        # That native branch always returns before screening or submission.
        if coordinator.db.execute("SELECT 1 FROM order_intents WHERE state IN "
                "('SUBMITTING','NEEDS_REVIEW') LIMIT 1").fetchone():
            phase = 'ADVANCE_EXECUTION'
            return coordinator.advance_execution(account, today)
        # Existing journal ambiguity blocks new entries; no blind chat/DELETE replay.
        phase = 'JOURNAL_OPEN'
        with _journal(coordinator.db) as existing:
            phase = 'CHECK_UNRESOLVED_REVIEW'
            if existing is not None and existing.execute(
                    "SELECT 1 FROM pending_reviews WHERE state='REVIEWING' "
                    "OR (state='PAUSED' AND decision='TAHAN') LIMIT 1").fetchone():
                phase = 'RECOVER_COMPLETED'
                with _journal(coordinator.db, create=True) as recovering:
                    _recover_completed_decisions(coordinator, recovering, native, now)
            phase = 'CHECK_UNRESOLVED_REVIEW'
            if existing is not None and existing.execute(
                    "SELECT 1 FROM pending_reviews WHERE state IN ('REVIEWING','CANCEL_PREPARED','NEEDS_REVIEW') LIMIT 1").fetchone():
                phase = 'MONITOR_EXISTING'
                monitored = _monitor_existing(coordinator, account, today, native)
                phase = 'RECOVER_CANCEL'
                with _journal(coordinator.db, create=True) as recovering:
                    _recover_cancel_checkpoints(coordinator, recovering, today, native)
                    phase = 'RECOVER_TERMINAL'
                    _recover_terminal_order_reviews(coordinator, recovering, account, today, native)
                if not existing.execute("SELECT 1 FROM pending_reviews WHERE state IN "
                        "('REVIEWING','CANCEL_PREPARED','NEEDS_REVIEW') LIMIT 1").fetchone():
                    phase = 'ADVANCE_EXECUTION'
                    return coordinator.advance_execution(account, today)
                return monitored or _report(coordinator, account, 'NEEDS_REVIEW', 'PENDING_REVIEW_OUTCOME_UNKNOWN')
            phase = 'SELECT_DUE'
            due = []
            for row in coordinator.db.execute(
                    "SELECT * FROM order_intents WHERE state IN ('ENTRY_PENDING','POSITION_PROTECTED') ORDER BY created,id LIMIT 3"):
                created = _utc_instant(row['created'])
                latest = existing.execute('SELECT * FROM pending_reviews WHERE intent_id=? ORDER BY sequence DESC LIMIT 1',
                    (row['id'],)).fetchone() if existing is not None else None
                # created is only a conservative candidate-filter clock. The
                # authenticated exchange acceptance time is checked below.
                try:
                    accepted = _accepted_at(dict(row), now=now)
                except PendingReviewUnavailable as error:
                    if str(error) != 'PENDING_REVIEW_ACCEPTED_AT_UNAVAILABLE':
                        raise
                    accepted = created
                next_due = latest['next_due'] if latest is not None else accepted + REVIEW_SECONDS
                if not _finite(next_due):
                    raise PendingReviewUnavailable('PENDING_REVIEW_CHECKPOINT_UNVERIFIED')
                if created <= now - REVIEW_SECONDS and next_due <= now:
                    due.append(dict(row))
        if not due:
            phase = 'ADVANCE_EXECUTION'
            return coordinator.advance_execution(account, today)
        # A prior native browser send with unknown outcome must not be
        # overlapped by a new pending-review conversation. Monitoring continues.
        phase = 'CHECK_UNRESOLVED_RESEARCH'
        if coordinator.unresolved_research():
            phase = 'MONITOR_EXISTING'
            monitored = _monitor_existing(coordinator, account, today, native)
            return monitored or _report(coordinator, account, 'NEEDS_REVIEW', 'ROBOT_REQUEST_NEEDS_REVIEW')
        if len(due) > 2:
            raise PendingReviewUnavailable('PENDING_REVIEW_CAPACITY_UNVERIFIED')
        guard = native['research_guard']
        for row in due:
            phase = 'VERIFY_INTENT'
            intent = coordinator.verified_intent(row)
            if not _gate(coordinator, today):
                return _report(coordinator, account)
            phase = 'OWNED_INTENT_LOCK'
            with (owned_intent_lock(coordinator.db, intent['client_order_id']),
                  guard.gateway_transaction_reads(), guard.gateway_mutations(coordinator.gateway_allowed)):
                phase = 'GATEWAY_RECONCILE'
                observation = coordinator.gateway.reconcile(intent)
                phase = 'GATEWAY_OBSERVATION'
                report = coordinator.record_gateway_observation(row['id'], row['candidate_id'], observation, account)
            phase = 'READ_CURRENT_INTENT'
            fresh = _intent_row(coordinator.db, row['id'])
            if not fresh or fresh['state'] == 'NEEDS_REVIEW':
                return report
            if (fresh['state'] in ('ENTRY_PENDING', 'POSITION_PROTECTED')
                    and observation.get('state') in ('ENTRY_PENDING', 'POSITION_PROTECTED')):
                phase = 'PENDING_ENTRY'
                reviewed_entry = _pending_entry(coordinator, intent, native, today)
                if reviewed_entry is not None:
                    phase = 'CHECK_ACCEPTED_TIME'
                    accepted_at = _accepted_at(dict(fresh), reviewed_entry, now=now)
                    if accepted_at + REVIEW_SECONDS <= now:
                        break
            if not _gate(coordinator, today):
                return _report(coordinator, account)
        else:
            # Fully filled protected positions have no entry remainder. Do not
            # repeatedly choose them or prevent another legitimate native task.
            phase = 'ADVANCE_EXECUTION'
            return coordinator.advance_execution(account, today)
        phase = 'VERIFY_INTENT'
        coordinator.verified_intent(fresh)
        if row['payload'] != fresh['payload'] or row['created'] != fresh['created']:
            raise PendingReviewUnavailable('PENDING_REVIEW_INTENT_CHANGED')
        phase = 'MARKET_CONTEXT'
        with _maintenance_reads(coordinator, native):
            context, _ = native['analysis'].analysis_context(coordinator.market, intent['symbol'],
                coordinator.store.settings()['risk_target_usdt'], coordinator.account_factory())
            phase = 'MARKET_FRESHNESS'
            coordinator.market.fresh(context, intent['symbol'])
            phase = 'RISK_ESTIMATE'
            estimate = estimate_owned_order(coordinator, fresh, coordinator.market, coordinator.account_factory())
        phase = 'MARKET_MATERIALS'
        if not closed_review_materials(context):
            raise PendingReviewUnavailable('PENDING_REVIEW_CLOSED_CANDLES_INSUFFICIENT')
        intent_ref = _sha(row['payload'])
        phase = 'ORDER_CONTEXT'
        order = _order_context(intent, dict(fresh), reviewed_entry, accepted_at, now=now)
        if estimate:
            order['estimate_status'] = estimate['status']
            if estimate['status'] == 'AVAILABLE':
                order.update(risk_usdt=estimate['original_risk_target_usdt'],
                    estimated_loss=estimate['estimated_total_risk'], cost_components={**estimate['components'],
                        'estimated_budget_difference_usdt': estimate['estimated_budget_difference_usdt'],
                        'evidence_sha256': estimate['cost_evidence_sha256']})
            else:
                order['cost_components'] = {'failure_code': estimate['failure_code']}
        context = dict(context, pending_review={'intent_ref': intent_ref,
            'symbol': intent['symbol'], 'entry_created_at': row['created']},
            pending_order_context=order, material_contract=MATERIAL_CONTRACT)
        if not _gate(coordinator, today):
            return _report(coordinator, account)
        phase = 'CLAIM_PREPARE'
        with _journal(coordinator.db, create=True) as journal:
            sequence = journal.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM pending_reviews WHERE intent_id=?',
                                       (row['id'],)).fetchone()[0]
            if type(sequence) is not int or sequence < 1:
                raise PendingReviewUnavailable('PENDING_REVIEW_SEQUENCE_EXHAUSTED')
            operation = 'pending-review-v1:' + intent_ref[:32] + ':' + str(sequence)
            prompt = _prompt(intent, intent_ref, order)
            prior = journal.execute('SELECT decision,result FROM pending_reviews WHERE intent_id=? '
                'ORDER BY sequence DESC LIMIT 1', (row['id'],)).fetchone()
            if prior and prior['result']:
                prior_result = json.loads(prior['result'])
                if prior_result.get('decision_not_applied_after_restart') is True:
                    prompt += ('\n\nEvaluasi sebelumnya menyatakan '+prior['decision']+
                        ' tetapi belum diterapkan karena proses terputus. Tolong nilai kembali '
                        'order yang sama memakai kondisi dan lampiran terbaru ini; bukan membuat order baru.')
            phase = 'REQUEST_PREPARE'
            body, serialized, digest = native['research_provider'].request_body(operation, prompt, REVIEW_SCHEMA, context)
            created = time.time()
            if not _context_valid(context, operation=operation, now=created, created=created):
                raise PendingReviewUnavailable('PENDING_REVIEW_CONTEXT_UNVERIFIED')
            phase = 'CLAIM_WRITE'
            journal.execute('INSERT INTO pending_reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (operation, row['id'], intent_ref, sequence, row['created'], created,
                 created + REVIEW_SECONDS, 'REVIEWING', None, serialized, digest, None, None))

            def permitted():
                if not _gate(coordinator, today):
                    return False
                current = _intent_row(coordinator.db, row['id'])
                return bool(current and current['state'] in ('ENTRY_PENDING', 'POSITION_PROTECTED')
                    and current['payload'] == row['payload'] and current['created'] == row['created'])

            def validate(value):
                canonical = native['neuroapi'].canonical_output(value, REVIEW_SCHEMA)
                if (json.loads(canonical) != value or value['symbol'] != intent['symbol']
                        or value['order_ref'] != intent_ref):
                    raise PendingReviewUnavailable('PENDING_REVIEW_DECISION_UNVERIFIED')
                return value

            try:
                phase = 'BROWSER_DISCUSSION'
                with _maintenance_reads(coordinator, native):
                    value = coordinator.neuro.ask(operation, prompt, REVIEW_SCHEMA, validate,
                        context=context, continue_if=permitted)
                phase = 'DECISION_VALIDATE'
                validate(value)
                phase = 'SOURCE_EVIDENCE'
                evidence = native['research_provider'].source_evidence(coordinator.db, operation)
                source = evidence.get('provider_evidence_sha256')
                if (evidence.get('source_type') != native['research_provider'].BROWSER_SOURCE
                        or type(source) is not str or re.fullmatch(r'[a-f0-9]{64}', source) is None):
                    raise PendingReviewUnavailable('PENDING_REVIEW_SOURCE_UNVERIFIED')
                phase = 'DECISION_RECEIPT'
                receipt = _decision_receipt(coordinator.db, operation,
                    accepted_at=order['accepted_at'])
                # An OFF or rollover after a completed reply never cancels anything.
                phase = 'CHECK_CURRENT_AUTHORITY'
                if not permitted():
                    phase = 'SAVE_PAUSED'
                    _set_state(journal, operation, 'PAUSED', decision=value['decision'],
                        next_due=time.time() + RETRY_SECONDS, source_sha256=source)
                    phase = 'READ_CURRENT_INTENT'
                    current = _intent_row(coordinator.db, row['id'])
                    if (_gate(coordinator, today) and current is not None
                            and current['state'] == 'PROTECTION_INCOMPLETE'):
                        return _report(coordinator, account, 'PROTECTION_INCOMPLETE',
                            'PENDING_REVIEW_WAITING_FOR_PROTECTION')
                    return _report(coordinator, account)
                phase = 'DECISION_FRESHNESS'
                if owner_material_request({'output_schema': REVIEW_SCHEMA, 'context': context}):
                    with _maintenance_reads(coordinator, native):
                        context = completed_decision_fresh(coordinator.db, operation, coordinator.market,
                            intent['symbol'], max_age=getattr(coordinator.market, 'max_age', 180), refresh=True,
                            source_reader=native['research_provider'].source_evidence,
                            browser_source=native['research_provider'].BROWSER_SOURCE)
                else:
                    phase = 'MARKET_FRESHNESS'
                    coordinator.market.fresh(context, intent['symbol'])
                if value['decision'] == 'TAHAN':
                    phase = 'SAVE_TAHAN'
                    _set_state(journal, operation, 'COMPLETE', decision='TAHAN',
                        next_due=receipt['decision_at'] + REVIEW_SECONDS,
                        source_sha256=source, result=receipt)
                    # The chat can outlive an entry fill even for TAHAN. Use
                    # the existing gateway immediately, independently of the
                    # next analysis/scheduler interval, preserving this already
                    # completed discussion if reconciliation itself fails.
                    try:
                        phase = 'OWNED_INTENT_LOCK'
                        with (owned_intent_lock(coordinator.db, intent['client_order_id']),
                              guard.gateway_transaction_reads(), guard.gateway_mutations(coordinator.gateway_allowed)):
                            phase = 'GATEWAY_RECONCILE'
                            latest = coordinator.gateway.reconcile(intent)
                            phase = 'GATEWAY_OBSERVATION'
                            return coordinator.record_gateway_observation(row['id'], row['candidate_id'], latest, account)
                    except Exception as error:
                        _log_review_failure(phase, error, operation)
                        return coordinator.mark_unknown(row['id'], row['candidate_id'], account)
                phase = 'SAVE_CANCEL_PREPARED'
                _set_state(journal, operation, 'CANCEL_PREPARED', decision='BATALKAN', source_sha256=source)
                # Re-prove ownership and re-read the entry immediately before the
                # existing gateway cancels only its native client-ID remainder.
                phase = 'VERIFY_CANCEL_INTENT'
                current = _matching_intent(coordinator.db, dict(row,
                    intent_id=row['id'], intent_ref=intent_ref, entry_created_at=row['created']))
                current_intent = coordinator.verified_intent(current)
                if current_intent != intent or not permitted():
                    phase = 'SAVE_PAUSED'
                    _set_state(journal, operation, 'PAUSED', next_due=time.time() + RETRY_SECONDS)
                    return _report(coordinator, account)
                phase = 'OWNED_INTENT_LOCK'
                with (owned_intent_lock(coordinator.db, intent['client_order_id']),
                      guard.gateway_transaction_reads(), guard.gateway_mutations(coordinator.gateway_allowed)):
                    # A chart discussion can outlive the first account/order
                    # observation. Re-run the ORIGINAL gateway's inventory,
                    # fill ownership and position protection immediately before
                    # cancellation; an entry may have filled during the chat.
                    phase = 'BEFORE_CANCEL_RECONCILE'
                    before_cancel = coordinator.gateway.reconcile(intent)
                    phase = 'GATEWAY_OBSERVATION'
                    report = coordinator.record_gateway_observation(row['id'], row['candidate_id'],
                                                                    before_cancel, account)
                    phase = 'READ_CURRENT_INTENT'
                    current = _intent_row(coordinator.db, row['id'])
                    if (not current or current['state'] not in ('ENTRY_PENDING', 'POSITION_PROTECTED')
                            or coordinator.verified_intent(current) != intent):
                        raise PendingReviewUnavailable('PENDING_REVIEW_CANCELLATION_UNVERIFIED')
                    phase = 'CANCEL_ENTRY_READ'
                    with coordinator.gateway._operation():
                        coordinator.gateway._validate_intent(intent)
                        coordinator.gateway._account_config()
                        entry = coordinator.gateway._entry(intent)
                        # A decision about an earlier pending snapshot cannot
                        # cancel after an intervening fill. Reconciliation above
                        # has already protected that new exposure; review any
                        # remaining LIMIT afresh instead of using stale advice.
                        if (entry['status'] not in ('NEW', 'PARTIALLY_FILLED')
                                or _number(entry['executedQty'], zero=True)
                                    != _number(reviewed_entry['executedQty'], zero=True)):
                            receipt.update(decision_not_applied_after_fill=True,
                                gateway_observation=before_cancel)
                            phase = 'SAVE_CANCEL_NOT_APPLIED'
                            _set_state(journal, operation, 'COMPLETE', decision='BATALKAN',
                                next_due=time.time() + RETRY_SECONDS, result=receipt)
                            return report
                        # The private native method is itself status/partial-fill
                        # aware and re-GETs after the signed DELETE. Its wire gate
                        # rechecks current ROBOT ON, including a concurrent OFF.
                        phase = 'CANCEL_REMAINDER'
                        with guard.gateway_mutations(lambda: permitted()):
                            after = coordinator.gateway._cancel_entry_remainder(intent, entry)
                            receipt['entry_remainder'] = _entry_remainder_receipt(intent, after,
                                entry['orderId'], filled_at_least=entry['executedQty'])
                    phase = 'AFTER_CANCEL_RECONCILE'
                    final = coordinator.gateway.reconcile(intent)
                    phase = 'GATEWAY_OBSERVATION'
                    report = coordinator.record_gateway_observation(row['id'], row['candidate_id'], final, account)
                phase = 'READ_CURRENT_INTENT'
                current = _intent_row(coordinator.db, row['id'])
                if not current or current['state'] == 'NEEDS_REVIEW':
                    raise PendingReviewUnavailable('PENDING_REVIEW_CANCELLATION_UNVERIFIED')
                phase = 'SAVE_CANCEL_RESULT'
                receipt['gateway_observation'] = final
                _set_state(journal, operation, 'COMPLETE', next_due=time.time() + REVIEW_SECONDS, result=receipt)
                return report
            except Exception as error:
                _log_review_failure(phase, error, operation)
                phase = 'BROWSER_FAILURE_STATE'
                api = coordinator.db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone()
                claim = journal.execute('SELECT state FROM pending_reviews WHERE operation=?', (operation,)).fetchone()
                if (isinstance(error, native['neuroapi'].ResearchPaused) and api is None
                        and claim['state'] == 'REVIEWING'):
                    _set_state(journal, operation, 'UNSENT', next_due=time.time() + RETRY_SECONDS)
                    return _report(coordinator, account, reason='PENDING_REVIEW_WAITING')
                _set_state(journal, operation, 'NEEDS_REVIEW')
                return _report(coordinator, account, 'NEEDS_REVIEW', 'PENDING_REVIEW_OUTCOME_UNKNOWN')
    except Exception as error:
        _log_review_failure(phase, error, operation)
        return _report(coordinator, account, 'NEEDS_REVIEW', 'PENDING_REVIEW_STATE_UNVERIFIED')
