"""Case-bound terminal recording of one received SOL NeuroAPI HTTP 422.

No provider or Binance requests are made.  The original request remains an
audit row with its original hash, attempt count and failure.  Its billing
outcome is unknown.  A separately reviewed coordinator may spend the final
replacement round on another coin; this helper never replays SOL or resets a
cycle, receipt, order, or replacement counter.
"""
import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

EXPECTED_UID = 10001
CASE_DAY = '2026-10-07'
CYCLE = CASE_DAY + ':robot-v9:1'
OPERATION = CYCLE + ':analysis-v9:SOLUSDT'
REQUEST_TERMINAL = 'REJECTED_REQUEST_VALIDATION'
JOB_TERMINAL = 'REQUEST_REJECTED'
ETH_CLIENT = 'hao-ce5286d37dcdca54a2f1174a2c48'
BTC_CLIENT = 'hao-6dd9743f6a90afa1c72f0931932a'
FINANCIAL_TABLES = frozenset((
    'order_intents', 'robot_entry_receipts', 'trades', 'shadow_plans',
    'shadow_events', 'live_records', 'live_actions', 'live_events',
    'live_requests', 'live_arms', 'live_scheduler',
))
REQUIRED_COLUMNS = {
    'api_requests': {'operation', 'idempotency', 'body_hash', 'state', 'created',
                     'attempts', 'output', 'failure_code'},
    'robot_jobs': {'operation', 'cycle', 'kind', 'symbol', 'state', 'risk_target'},
    'robot_cycles': {'id', 'day', 'entry_epoch', 'state', 'data'},
    'robot_candidates': {'id', 'cycle', 'symbol', 'status', 'plan', 'failure_code'},
    'order_intents': {'id', 'candidate_id', 'symbol', 'state', 'payload', 'result',
                      'failure_code', 'created', 'updated'},
    'robot_entry_receipts': {'id', 'symbol', 'entry_day', 'confirmed_at'},
}


class Refuse(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise Refuse(reason)


def unique_object(pairs):
    value = {}
    for name, item in pairs:
        require(name not in value, 'DUPLICATE_JSON_KEY')
        value[name] = item
    return value


def load_json(value):
    require(isinstance(value, str) and len(value) <= 4_000_000, 'JSON_INVALID')
    try:
        return json.loads(value, object_pairs_hook=unique_object)
    except Refuse:
        raise
    except Exception:
        raise Refuse('JSON_INVALID') from None


def digest_text(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _typed(value):
    if value is None:
        return ['null']
    if isinstance(value, bytes):
        return ['bytes', value.hex()]
    if isinstance(value, str):
        return ['text', value]
    if isinstance(value, int):
        return ['int', str(value)]
    if isinstance(value, float):
        return ['float', value.hex()]
    raise Refuse('SQL_VALUE_INVALID')


def financial_digest(db):
    """Hash every known financial table, including preserved older runtimes."""
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    snapshot = []
    for name in sorted(FINANCIAL_TABLES & tables):
        rows = [json.dumps([_typed(value) for value in row], separators=(',', ':'))
                for row in db.execute('SELECT * FROM "' + name + '"')]
        snapshot.append([name, sorted(rows)])
    return digest_text(json.dumps(snapshot, separators=(',', ':')))


def private_regular(path, reason):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
            info.st_uid == os.geteuid() == EXPECTED_UID and
            not stat.S_IMODE(info.st_mode) & 0o077, reason)
    return info


def directory_without_symlinks(path, reason, *, private=False):
    for part in (path, *path.parents):
        info = part.lstat()
        require(stat.S_ISDIR(info.st_mode), reason)
    if private:
        info = path.lstat()
        require(info.st_uid == os.geteuid() == EXPECTED_UID and
                not stat.S_IMODE(info.st_mode) & 0o077, reason)


def source_check(value, root):
    if value is None:
        return
    expected = load_json(value) if isinstance(value, str) else value
    require(isinstance(expected, dict) and 0 < len(expected) <= 512,
            'SOURCE_MAP_INVALID')
    root = Path(root).absolute()
    directory_without_symlinks(root, 'SOURCE_ROOT_INVALID')
    for name, sha in expected.items():
        require(isinstance(name, str) and
                re.fullmatch(r'(?:worker|deploy)/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.py', name)
                and isinstance(sha, str) and re.fullmatch(r'[a-f0-9]{64}', sha),
                'SOURCE_MAP_INVALID')
        path = root / name
        directory_without_symlinks(path.parent, 'SOURCE_PATH_INVALID')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                    info.st_size <= 2_000_000, 'SOURCE_PATH_INVALID')
            with os.fdopen(os.dup(descriptor), 'rb') as stream:
                actual = hashlib.sha256(stream.read(2_000_001)).hexdigest()
            require(actual == sha, 'SOURCE_CHANGED')
        finally:
            os.close(descriptor)


def schema_check(db):
    require(not db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone(),
            'JOURNAL_TRIGGER_PRESENT')
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    require(set(REQUIRED_COLUMNS) <= tables, 'JOURNAL_SCHEMA_INVALID')
    for name, required in REQUIRED_COLUMNS.items():
        columns = {row[1] for row in db.execute('PRAGMA table_info("' + name + '")')}
        require(required <= columns, 'JOURNAL_SCHEMA_INVALID')


def financial_check(db):
    require(not db.execute("SELECT 1 FROM order_intents WHERE state IN "
                           "('SUBMITTING','NEEDS_REVIEW') LIMIT 1").fetchone(),
            'UNRESOLVED_ORDER')
    eth = db.execute("SELECT * FROM order_intents WHERE symbol='ETHUSDT'").fetchall()
    btc = db.execute("SELECT * FROM order_intents WHERE symbol='BTCUSDT'").fetchall()
    require(len(eth) == len(btc) == 1, 'FINANCIAL_CASE_CHANGED')
    eth, btc = eth[0], btc[0]
    payload, result = load_json(eth['payload']), load_json(eth['result'])
    require(eth['state'] in ('POSITION_PROTECTED', 'CLOSED') and eth['failure_code'] is None and
            payload.get('client_order_id') == ETH_CLIENT and
            payload.get('symbol') == 'ETHUSDT' and payload.get('side') == 'LONG',
            'ETH_PROTECTION_CHANGED')
    entry, protection = payload.get('entry'), payload.get('protection')
    require(isinstance(entry, dict) and isinstance(protection, dict) and
            entry.get('side') == 'BUY' and Decimal(entry.get('price', '0')) == Decimal('2610') and
            Decimal(entry.get('quantity', '0')) == Decimal('0.181') and
            Decimal(protection.get('stop_loss', '0')) == Decimal('2585') and
            Decimal(protection.get('take_profit', '0')) == Decimal('2730') and
            protection.get('exit_side') == 'SELL' and
            protection.get('working_type') == 'MARK_PRICE', 'ETH_PROTECTION_CHANGED')
    expected = dict(source='BINANCE_FUTURES', state=eth['state'], symbol='ETHUSDT',
                    client_order_id=ETH_CLIENT, order_id='8389766291635748850')
    if eth['state'] == 'POSITION_PROTECTED':
        expected.update(sl_order_id='4000001952621457', tp_order_id='4000001952686853',
                        sl_confirmed=True, tp_confirmed=True)
    else:
        # This is the exact SL closure subsequently proved by Binance GETs,
        # and recorded through the production observation recorder. Do not
        # turn a closed position back into protected merely to settle research.
        expected.update(exit_order_id='8389766291712624741',
                        first_fill_at='2026-10-07T08:18:23.715000+00:00',
                        closed_at='2026-10-07T10:07:39.052000+00:00',
                        sl_confirmed=False, tp_confirmed=False)
        candidate = db.execute('SELECT status,failure_code FROM robot_candidates WHERE id=?',
                               (eth['candidate_id'],)).fetchone()
        require(candidate is not None and candidate['status'] == 'CLOSED' and
                candidate['failure_code'] is None, 'ETH_CLOSURE_CHANGED')
    require(isinstance(result, dict) and all(result.get(key) == value for key, value in expected.items())
            and result.get('sl_confirmed') is expected['sl_confirmed']
            and result.get('tp_confirmed') is expected['tp_confirmed']
            and Decimal(result.get('filled_quantity', '0')) == Decimal('0.181'),
            'ETH_PROTECTION_CHANGED')
    receipt = db.execute('SELECT * FROM robot_entry_receipts WHERE id=?', (ETH_CLIENT,)).fetchone()
    require(receipt is not None and receipt['symbol'] == 'ETHUSDT' and
            receipt['entry_day'] == CASE_DAY and
            receipt['confirmed_at'] == result.get('first_fill_at'), 'ETH_RECEIPT_CHANGED')
    require(btc['state'] == 'REJECTED' and btc['failure_code'] == 'NO_ACCEPTED_ORDER_OBSERVED' and
            load_json(btc['payload']).get('client_order_id') == BTC_CLIENT,
            'BTC_CASE_CHANGED')


def case_rows(db):
    schema_check(db)
    rows = {}
    for table, key in (('api_requests', 'operation'), ('robot_jobs', 'operation'),
                       ('robot_candidates', 'id'), ('robot_cycles', 'id')):
        value = CYCLE if table == 'robot_cycles' else OPERATION
        row = db.execute('SELECT * FROM ' + table + ' WHERE ' + key + '=?', (value,)).fetchone()
        require(row is not None, 'CASE_ROW_MISSING')
        rows[table] = dict(row)
    request, job, candidate, cycle = (rows[name] for name in
        ('api_requests', 'robot_jobs', 'robot_candidates', 'robot_cycles'))
    before = ('NEEDS_REVIEW', 'NEEDS_REVIEW', 'NEEDS_REVIEW')
    after = (REQUEST_TERMINAL, JOB_TERMINAL, 'ACTIVE')
    states = request['state'], job['state'], cycle['state']
    require(states in (before, after), 'CASE_STATE_CHANGED')
    created = request['created']
    require(type(created) in (int, float) and math.isfinite(created) and created > 0 and
            datetime.fromtimestamp(created, timezone(timedelta(hours=7))).date().isoformat() == CASE_DAY,
            'REQUEST_DATE_CHANGED')
    require(request['failure_code'] == 'HTTP_422' and type(request['attempts']) is int and
            request['attempts'] == 1 and request['output'] is None and request['idempotency'] is None and
            isinstance(request['body_hash'], str) and re.fullmatch(r'[a-f0-9]{64}', request['body_hash']),
            'REQUEST_EVIDENCE_CHANGED')
    require(job['cycle'] == CYCLE and job['kind'] == 'ANALYSIS' and job['symbol'] == 'SOLUSDT'
            and job['risk_target'] == '5', 'JOB_CHANGED')
    require(candidate['cycle'] == CYCLE and candidate['symbol'] == 'SOLUSDT' and
            candidate['status'] == 'REJECTED' and candidate['failure_code'] == 'HTTP_422' and
            candidate['plan'] is None, 'CANDIDATE_CHANGED')
    require(not db.execute('SELECT 1 FROM order_intents WHERE candidate_id=?', (OPERATION,)).fetchone(),
            'SOL_ORDER_PRESENT')
    require(cycle['day'] == CASE_DAY and type(cycle['entry_epoch']) is int and cycle['entry_epoch'] == 1,
            'CYCLE_CHANGED')
    data = load_json(cycle['data'])
    require(isinstance(data, dict) and all(type(data.get(key)) is int and data[key] == value
            for key, value in (('target', 1), ('screen', 2), ('replacements', 2))) and
            data.get('queue') == [] and data.get('round_symbols') == ['SOLUSDT'] and
            data.get('seen') == ['BTCUSDT', 'BNBUSDT', 'SOLUSDT'], 'CYCLE_COUNTER_CHANGED')
    for table in ('api_requests', 'robot_jobs'):
        unresolved = list(db.execute('SELECT operation FROM ' + table +
                                     " WHERE state IN ('PENDING','NEEDS_REVIEW')"))
        expected = [OPERATION] if states == before else []
        require(sorted(row[0] for row in unresolved) == expected, 'OTHER_REQUEST_UNRESOLVED')
    financial_check(db)
    return rows, states == after


def backup_database(db, trading):
    maintenance = trading / 'maintenance'
    if not maintenance.exists():
        maintenance.mkdir(mode=0o700)
    directory_without_symlinks(maintenance, 'MAINTENANCE_NOT_PRIVATE', private=True)
    folder = Path(tempfile.mkdtemp(prefix='sol-422-', dir=maintenance))
    folder.chmod(0o700)
    path = folder / 'ledger.before.sqlite3'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    with sqlite3.connect(path) as saved:
        saved.execute('PRAGMA trusted_schema=OFF')
        db.backup(saved)
        require(saved.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'BACKUP_INVALID')
    private_regular(path, 'BACKUP_NOT_PRIVATE')
    return path


def settle(data_dir='/data', *, apply=False, source_hashes=None, source_root='/app'):
    require(os.geteuid() == EXPECTED_UID, 'WORKER_UID_REQUIRED')
    source_check(source_hashes, source_root)
    trading = Path(data_dir).absolute() / 'trading'
    directory_without_symlinks(trading, 'TRADING_PATH_INVALID')
    ledger, lock_path = trading / 'ledger.sqlite3', trading / 'cycle.lock'
    ledger_info = private_regular(ledger, 'JOURNAL_NOT_PRIVATE')
    lock_info = private_regular(lock_path, 'LOCK_NOT_PRIVATE')
    lock = db = None
    try:
        lock = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        current = os.fstat(lock)
        require((current.st_dev, current.st_ino) == (lock_info.st_dev, lock_info.st_ino), 'LOCK_CHANGED')
        deadline = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, 'WORKER_BUSY')
                time.sleep(.2)
        current = private_regular(ledger, 'JOURNAL_NOT_PRIVATE')
        require((current.st_dev, current.st_ino) == (ledger_info.st_dev, ledger_info.st_ino), 'JOURNAL_CHANGED')
        db = sqlite3.connect(ledger.as_uri() + '?mode=' + ('rw' if apply else 'ro'),
                             uri=True, isolation_level=None, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA trusted_schema=OFF')
        if not apply:
            db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'JOURNAL_INVALID')
        rows, already = case_rows(db)
        finance = financial_digest(db)
        db.execute('COMMIT')
        result = dict(status='ALREADY_SETTLED' if already else 'INSPECTION_ONLY',
                      can_apply=not already, billing_outcome='UNKNOWN',
                      request_body_sha256=rows['api_requests']['body_hash'],
                      cycle_data_sha256=digest_text(rows['robot_cycles']['data']),
                      financial_sha256=finance, replacements=2, maximum_replacements=3)
        if already or not apply:
            return result
        backup = backup_database(db, trading)
        db.execute('BEGIN IMMEDIATE')
        try:
            current, repeated = case_rows(db)
            require(not repeated and current == rows and financial_digest(db) == finance, 'JOURNAL_CHANGED')
            require(db.execute("UPDATE api_requests SET state=? WHERE operation=? AND state='NEEDS_REVIEW'",
                               (REQUEST_TERMINAL, OPERATION)).rowcount == 1, 'REQUEST_CAS_FAILED')
            require(db.execute("UPDATE robot_jobs SET state=? WHERE operation=? AND state='NEEDS_REVIEW'",
                               (JOB_TERMINAL, OPERATION)).rowcount == 1, 'JOB_CAS_FAILED')
            require(db.execute("UPDATE robot_cycles SET state='ACTIVE' WHERE id=? AND state='NEEDS_REVIEW' AND data=?",
                               (CYCLE, rows['robot_cycles']['data'])).rowcount == 1, 'CYCLE_CAS_FAILED')
            after, verified = case_rows(db)
            expected = {table: dict(row) for table, row in rows.items()}
            expected['api_requests']['state'] = REQUEST_TERMINAL
            expected['robot_jobs']['state'] = JOB_TERMINAL
            expected['robot_cycles']['state'] = 'ACTIVE'
            require(verified and after == expected and financial_digest(db) == finance, 'SETTLEMENT_NOT_VERIFIED')
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        result.update(status='SOL_HTTP_422_SETTLED', can_apply=False, backup=str(backup),
                      request_state=REQUEST_TERMINAL, job_state=JOB_TERMINAL, cycle_state='ACTIVE')
        return result
    finally:
        if db is not None:
            db.close()
        if lock is not None:
            os.close(lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', default='/data')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--source-hashes-json')
    parser.add_argument('--source-root', default='/app')
    args = parser.parse_args(argv)
    try:
        result = settle(args.data_dir, apply=args.apply,
                        source_hashes=args.source_hashes_json, source_root=args.source_root)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps(dict(status='SOL_HTTP_422_NOT_SETTLED',
                              reason=str(error) if isinstance(error, Refuse) else type(error).__name__),
                         sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
