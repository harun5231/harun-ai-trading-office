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
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .native_pending_protocol import (OPERATION_PATTERN, REVIEW_SCHEMA,
    _context_valid, _utc_instant)


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
                    or not 0 <= now - claim['created'] <= max_age
                    or now - _utc_instant(claim['entry_created_at']) < REVIEW_SECONDS
                    or claim['request_sha256'] != request_sha256):
                return False
            row = _matching_intent(db, claim)
            intent, store = _verified_from_store(db, row, native)
            if not store.settings()['robot_on']:
                return False
            body = json.loads(claim['request_body'])
            if (body.get('output_schema') != REVIEW_SCHEMA or body.get('operation') != operation
                    or body.get('context', {}).get('symbol') != intent['symbol']
                    or body['context'].get('pending_review') != {
                        'intent_ref': claim['intent_ref'], 'symbol': intent['symbol'],
                        'entry_created_at': claim['entry_created_at']}
                    or not _context_valid(body['context'], operation=operation, now=now,
                                          max_age=max_age, created=claim['created'])):
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
                and now - api['created'] <= max_age)
    except Exception:
        return False


def _report(coordinator, account, state='WAITING', reason=None):
    if state == 'WAITING':
        return coordinator.store.report(state, account, wait_reason=reason or coordinator.pause_reason())
    return coordinator.store.report(state, account, reason=reason)


def _gate(coordinator, today):
    try:
        return coordinator.allowed(today) is True
    except Exception:
        return False


def _set_state(journal, operation, state, *, decision=None, next_due=None,
               source_sha256=None, result=None):
    journal.execute('UPDATE pending_reviews SET state=?,decision=COALESCE(?,decision),'
        'next_due=COALESCE(?,next_due),source_sha256=COALESCE(?,source_sha256),'
        'result=COALESCE(?,result) WHERE operation=?',
        (state, decision, next_due, source_sha256,
         None if result is None else _json(result), operation))


def _prompt(intent, intent_ref):
    existing = {key: intent[key] for key in ('symbol', 'side', 'entry', 'protection')}
    return ('Evaluate this existing owned Binance Futures USDT Perpetual LIMIT entry after '
        'five hours using the attached fresh 1H and 15M charts and current public market data. '
        'Decide TAHAN to retain the existing LIMIT or BATALKAN to cancel only its unfilled '
        'remainder. Never change a filled position, TP or SL; do not invent a new setup or '
        'prices. Return only JSON with symbol, order_ref and decision (TAHAN or BATALKAN). '
        'Use this exact order_ref: ' + intent_ref + '. Existing native intent: ' + _json(existing))


def _pending_entry(coordinator, intent, native, today):
    if not _gate(coordinator, today):
        return None
    guard = native['research_guard']
    with guard.gateway_transaction_reads(), guard.gateway_mutations(coordinator.gateway_allowed):
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
        "('ENTRY_PENDING','POSITION_PROTECTED') ORDER BY rowid LIMIT 3"))
    if len(rows) > 2:
        return _report(coordinator, account, 'NEEDS_REVIEW', 'PENDING_REVIEW_CAPACITY_UNVERIFIED')
    guard = native['research_guard']
    for row in rows:
        if not _gate(coordinator, today):
            return _report(coordinator, account)
        try:
            intent = coordinator.verified_intent(row)
            with guard.gateway_transaction_reads(), guard.gateway_mutations(coordinator.gateway_allowed):
                value = coordinator.gateway.reconcile(intent)
            report = coordinator.record_gateway_observation(row['id'], row['candidate_id'], value, account)
        except Exception:
            return coordinator.mark_unknown(row['id'], row['candidate_id'], account)
        if report.get('bot_status') == 'NEEDS_REVIEW':
            return report
    return None


def review_then_advance(coordinator, account, today):
    """Called once under the native Coordinator cycle lock, before new entries."""
    if not _gate(coordinator, today):
        return _report(coordinator, account)
    native, now = _native(), time.time()
    try:
        # Preserve the original highest-priority unknown-order reconciliation.
        # That native branch always returns before screening or submission.
        if coordinator.db.execute("SELECT 1 FROM order_intents WHERE state IN "
                "('SUBMITTING','NEEDS_REVIEW') LIMIT 1").fetchone():
            return coordinator.advance_execution(account, today)
        # Existing journal ambiguity blocks new entries; no blind chat/DELETE replay.
        with _journal(coordinator.db) as existing:
            if existing is not None and existing.execute(
                    "SELECT 1 FROM pending_reviews WHERE state IN ('REVIEWING','CANCEL_PREPARED','NEEDS_REVIEW') LIMIT 1").fetchone():
                monitored = _monitor_existing(coordinator, account, today, native)
                return monitored or _report(coordinator, account, 'NEEDS_REVIEW', 'PENDING_REVIEW_OUTCOME_UNKNOWN')
            due = []
            for row in coordinator.db.execute(
                    "SELECT * FROM order_intents WHERE state IN ('ENTRY_PENDING','POSITION_PROTECTED') ORDER BY created,id LIMIT 3"):
                created = _utc_instant(row['created'])
                latest = existing.execute('SELECT * FROM pending_reviews WHERE intent_id=? ORDER BY sequence DESC LIMIT 1',
                    (row['id'],)).fetchone() if existing is not None else None
                next_due = latest['next_due'] if latest is not None else created + REVIEW_SECONDS
                if not _finite(next_due):
                    raise PendingReviewUnavailable('PENDING_REVIEW_CHECKPOINT_UNVERIFIED')
                if created <= now - REVIEW_SECONDS and next_due <= now:
                    due.append(dict(row))
        if not due:
            return coordinator.advance_execution(account, today)
        # A prior native browser send with unknown outcome must not be
        # overlapped by a new pending-review conversation. Monitoring continues.
        if coordinator.unresolved_research():
            monitored = _monitor_existing(coordinator, account, today, native)
            return monitored or _report(coordinator, account, 'NEEDS_REVIEW', 'ROBOT_REQUEST_NEEDS_REVIEW')
        if len(due) > 2:
            raise PendingReviewUnavailable('PENDING_REVIEW_CAPACITY_UNVERIFIED')
        guard = native['research_guard']
        for row in due:
            intent = coordinator.verified_intent(row)
            if not _gate(coordinator, today):
                return _report(coordinator, account)
            with guard.gateway_transaction_reads(), guard.gateway_mutations(coordinator.gateway_allowed):
                observation = coordinator.gateway.reconcile(intent)
            report = coordinator.record_gateway_observation(row['id'], row['candidate_id'], observation, account)
            fresh = _intent_row(coordinator.db, row['id'])
            if not fresh or fresh['state'] == 'NEEDS_REVIEW':
                return report
            if (fresh['state'] in ('ENTRY_PENDING', 'POSITION_PROTECTED')
                    and observation.get('state') in ('ENTRY_PENDING', 'POSITION_PROTECTED')
                    and _pending_entry(coordinator, intent, native, today) is not None):
                break
            if not _gate(coordinator, today):
                return _report(coordinator, account)
        else:
            # Fully filled protected positions have no entry remainder. Do not
            # repeatedly choose them or prevent another legitimate native task.
            return coordinator.advance_execution(account, today)
        coordinator.verified_intent(fresh)
        if row['payload'] != fresh['payload'] or row['created'] != fresh['created']:
            raise PendingReviewUnavailable('PENDING_REVIEW_INTENT_CHANGED')
        context, _ = native['analysis'].analysis_context(coordinator.market, intent['symbol'],
            coordinator.store.settings()['risk_target_usdt'], coordinator.account_factory())
        coordinator.market.fresh(context, intent['symbol'])
        intent_ref = _sha(row['payload'])
        context = dict(context, pending_review={'intent_ref': intent_ref,
            'symbol': intent['symbol'], 'entry_created_at': row['created']})
        if not _gate(coordinator, today):
            return _report(coordinator, account)
        with _journal(coordinator.db, create=True) as journal:
            sequence = journal.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM pending_reviews WHERE intent_id=?',
                                       (row['id'],)).fetchone()[0]
            if type(sequence) is not int or not 1 <= sequence <= 999999999:
                raise PendingReviewUnavailable('PENDING_REVIEW_SEQUENCE_EXHAUSTED')
            operation = 'pending-review-v1:' + intent_ref[:32] + ':' + str(sequence)
            prompt = _prompt(intent, intent_ref)
            body, serialized, digest = native['research_provider'].request_body(operation, prompt, REVIEW_SCHEMA, context)
            created = time.time()
            if not _context_valid(context, operation=operation, now=created, created=created):
                raise PendingReviewUnavailable('PENDING_REVIEW_CONTEXT_UNVERIFIED')
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
                value = coordinator.neuro.ask(operation, prompt, REVIEW_SCHEMA, validate,
                    context=context, continue_if=permitted)
                validate(value)
                evidence = native['research_provider'].source_evidence(coordinator.db, operation)
                source = evidence.get('provider_evidence_sha256')
                if (evidence.get('source_type') != native['research_provider'].BROWSER_SOURCE
                        or type(source) is not str or re.fullmatch(r'[a-f0-9]{64}', source) is None):
                    raise PendingReviewUnavailable('PENDING_REVIEW_SOURCE_UNVERIFIED')
                # An OFF or rollover after a completed reply never cancels anything.
                if not permitted():
                    _set_state(journal, operation, 'PAUSED', decision=value['decision'],
                        next_due=time.time() + RETRY_SECONDS, source_sha256=source)
                    return _report(coordinator, account)
                coordinator.market.fresh(context, intent['symbol'])
                if value['decision'] == 'TAHAN':
                    _set_state(journal, operation, 'COMPLETE', decision='TAHAN',
                        next_due=time.time() + REVIEW_SECONDS, source_sha256=source)
                    return report
                _set_state(journal, operation, 'CANCEL_PREPARED', decision='BATALKAN', source_sha256=source)
                # Re-prove ownership and re-read the entry immediately before the
                # existing gateway cancels only its native client-ID remainder.
                current = _matching_intent(coordinator.db, dict(row,
                    intent_id=row['id'], intent_ref=intent_ref, entry_created_at=row['created']))
                current_intent = coordinator.verified_intent(current)
                if current_intent != intent or not permitted():
                    _set_state(journal, operation, 'PAUSED', next_due=time.time() + RETRY_SECONDS)
                    return _report(coordinator, account)
                with guard.gateway_transaction_reads(), guard.gateway_mutations(coordinator.gateway_allowed):
                    # A chart discussion can outlive the first account/order
                    # observation. Re-run the ORIGINAL gateway's inventory,
                    # fill ownership and position protection immediately before
                    # cancellation; an entry may have filled during the chat.
                    before_cancel = coordinator.gateway.reconcile(intent)
                    coordinator.record_gateway_observation(row['id'], row['candidate_id'],
                                                           before_cancel, account)
                    current = _intent_row(coordinator.db, row['id'])
                    if (not current or current['state'] not in ('ENTRY_PENDING', 'POSITION_PROTECTED')
                            or coordinator.verified_intent(current) != intent):
                        raise PendingReviewUnavailable('PENDING_REVIEW_CANCELLATION_UNVERIFIED')
                    with coordinator.gateway._operation():
                        coordinator.gateway._validate_intent(intent)
                        coordinator.gateway._account_config()
                        entry = coordinator.gateway._entry(intent)
                        # The private native method is itself status/partial-fill
                        # aware and re-GETs after the signed DELETE. Its wire gate
                        # rechecks current ROBOT ON, including a concurrent OFF.
                        with guard.gateway_mutations(lambda: permitted()):
                            coordinator.gateway._cancel_entry_remainder(intent, entry)
                    final = coordinator.gateway.reconcile(intent)
                report = coordinator.record_gateway_observation(row['id'], row['candidate_id'], final, account)
                current = _intent_row(coordinator.db, row['id'])
                if not current or current['state'] == 'NEEDS_REVIEW':
                    raise PendingReviewUnavailable('PENDING_REVIEW_CANCELLATION_UNVERIFIED')
                _set_state(journal, operation, 'COMPLETE', next_due=time.time() + REVIEW_SECONDS, result=final)
                return report
            except Exception as error:
                api = coordinator.db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone()
                claim = journal.execute('SELECT state FROM pending_reviews WHERE operation=?', (operation,)).fetchone()
                if (isinstance(error, native['neuroapi'].ResearchPaused) and api is None
                        and claim['state'] == 'REVIEWING'):
                    _set_state(journal, operation, 'UNSENT', next_due=time.time() + RETRY_SECONDS)
                    return _report(coordinator, account, reason='PENDING_REVIEW_WAITING')
                _set_state(journal, operation, 'NEEDS_REVIEW')
                return _report(coordinator, account, 'NEEDS_REVIEW', 'PENDING_REVIEW_OUTCOME_UNKNOWN')
    except Exception:
        return _report(coordinator, account, 'NEEDS_REVIEW', 'PENDING_REVIEW_STATE_UNVERIFIED')
