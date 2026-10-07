"""Record this ETH's verified SL closure using GET-only Binance reconciliation.

This case-bound maintenance command preserves the original entry intent and
first-fill receipt.  Inspection is the default; --apply invokes the production
observation recorder only after the exact closure has been proved again.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

EXPECTED_UID = 10001
SOURCE_PINS = {
    'worker/order_gateway.py': 'df6705bbdda9f2a34374fde186244d400e2fc84ecb8f27ce05ad15f35a9bbfc4',
    'worker/robot.py': 'cc0e1c6b68581fa23402dd61c9de023941a1f13489137282c83ae32ccf0dfa8d',
}
CASE_DAY = '2026-10-07'
OPERATION = CASE_DAY + ':robot-v9:0:analysis-v9:ETHUSDT'
CLIENT = 'hao-ce5286d37dcdca54a2f1174a2c48'
ENTRY_ID = '8389766291635748850'
SL_ID = '4000001952621457'
TP_ID = '4000001952686853'
EXIT_ID = '8389766291712624741'
FIRST_FILL = '2026-10-07T08:18:23.715000+00:00'
CLOSED_AT = '2026-10-07T10:07:39.052000+00:00'
FAILURE = 'BINANCE_ORDER_EXIT_RACE'
REPORT_TABLES = frozenset(('robot_status', 'office_cache', 'office_activity', 'sqlite_sequence'))


class Refuse(Exception):
    pass


def require(value, reason):
    if not value:
        raise Refuse(reason)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def load_json(value):
    require(isinstance(value, str) and len(value) <= 4_000_000, 'JSON_INVALID')
    try:
        return json.loads(value, object_pairs_hook=unique_object)
    except Refuse:
        raise
    except Exception:
        raise Refuse('JSON_INVALID') from None


def directory_check(path, reason, *, private=False):
    for parent in (path, *path.parents):
        require(stat.S_ISDIR(parent.lstat().st_mode), reason)
    if private:
        info = path.lstat()
        require(info.st_uid == os.geteuid() == EXPECTED_UID and
                not stat.S_IMODE(info.st_mode) & 0o077, reason)


def private_file(path, reason):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
            info.st_uid == os.geteuid() == EXPECTED_UID and
            not stat.S_IMODE(info.st_mode) & 0o077, reason)
    return info


def file_identity(info):
    return info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)


def current_files(trading, path, ledger_info, lock_path, lock_info, lock):
    directory_check(trading, 'TRADING_PATH_INVALID')
    require(file_identity(private_file(path, 'JOURNAL_NOT_PRIVATE')) ==
            file_identity(ledger_info), 'JOURNAL_CHANGED')
    require(file_identity(private_file(lock_path, 'LOCK_NOT_PRIVATE')) ==
            file_identity(lock_info), 'LOCK_CHANGED')
    held = os.fstat(lock)
    require(stat.S_ISREG(held.st_mode) and held.st_nlink == 1 and
            file_identity(held) == file_identity(lock_info), 'LOCK_CHANGED')


def check_sources(root, source_hashes=None):
    root = Path(root).absolute()
    directory_check(root, 'SOURCE_PATH_INVALID')
    expected = dict(SOURCE_PINS)
    if source_hashes is not None:
        expected = load_json(source_hashes) if isinstance(source_hashes, str) else source_hashes
        require(isinstance(expected, dict) and 0 < len(expected) <= 512 and
                all(expected.get(name) == sha for name, sha in SOURCE_PINS.items()), 'SOURCE_MAP_INVALID')
        inventory = list((root/'worker').rglob('*.py')) + [
            root/'deploy/container_boot.py', root/'deploy/runtime_permissions.py']
        require(set(expected) == {str(path.relative_to(root)) for path in inventory}, 'SOURCE_INVENTORY_CHANGED')
    for name, sha in expected.items():
        require(isinstance(name, str) and re.fullmatch(
            r'(?:worker|deploy)/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.py', name) and
            isinstance(sha, str) and re.fullmatch(r'[a-f0-9]{64}', sha), 'SOURCE_MAP_INVALID')
        path = root / name
        directory_check(path.parent, 'SOURCE_PATH_INVALID')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                    info.st_size <= 2_000_000, 'SOURCE_PATH_INVALID')
            with os.fdopen(os.dup(descriptor), 'rb') as stream:
                require(hashlib.sha256(stream.read(2_000_001)).hexdigest() == sha, 'SOURCE_CHANGED')
        finally:
            os.close(descriptor)


def production(root):
    # Used as an inline stdin command on the VPS as well as a standalone file.
    sys.path.insert(0, str(Path(root).absolute()))
    from worker.robot import Coordinator
    from worker.robot_store import RobotStore
    from worker.order_gateway import OrderGateway
    from worker.binance_private import BinanceReadOnly
    from worker.account_state import account_state
    from worker.core import day
    return Coordinator, RobotStore, OrderGateway, BinanceReadOnly, account_state, day


def numeric_equal(value, expected):
    return isinstance(value, str) and re.fullmatch(r'\d+(?:\.\d+)?', value) and Decimal(value) == Decimal(expected)


def closure_proof(result, *, fresh=False):
    require(isinstance(result, dict), 'CLOSURE_PROOF_CHANGED')
    expected = dict(source='BINANCE_FUTURES', state='CLOSED', symbol='ETHUSDT',
                    client_order_id=CLIENT, order_id=ENTRY_ID, first_fill_at=FIRST_FILL,
                    exit_order_id=EXIT_ID, closed_at=CLOSED_AT)
    require(all(result.get(key) == value for key, value in expected.items()) and
            numeric_equal(result.get('filled_quantity'), '0.181') and
            result.get('sl_confirmed') is False and result.get('tp_confirmed') is False,
            'CLOSURE_PROOF_CHANGED')
    if fresh:
        observed = datetime.fromisoformat(result['observed_at'])
        require(observed.tzinfo is not None and
                0 <= (datetime.now(timezone.utc)-observed).total_seconds() <= 120,
                'CLOSURE_PROOF_STALE')


def case_rows(db):
    require(not db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone(),
            'JOURNAL_TRIGGER_PRESENT')
    rows = db.execute("SELECT * FROM order_intents WHERE symbol='ETHUSDT'").fetchall()
    require(len(rows) == 1, 'ETH_CASE_CHANGED')
    row = dict(rows[0])
    require(row['id'] == row['candidate_id'] == OPERATION and row['symbol'] == 'ETHUSDT',
            'ETH_CASE_CHANGED')
    candidate = db.execute('SELECT * FROM robot_candidates WHERE id=?', (OPERATION,)).fetchone()
    require(candidate is not None, 'CANDIDATE_CHANGED')
    candidate = dict(candidate)
    require(candidate['symbol'] == 'ETHUSDT' and candidate['cycle'] == CASE_DAY+':robot-v9:0',
            'CANDIDATE_CHANGED')
    before = (row['state'], row['failure_code'], candidate['status'], candidate['failure_code'])
    unsettled = ('NEEDS_REVIEW', FAILURE, 'NEEDS_REVIEW', FAILURE)
    settled = ('CLOSED', None, 'CLOSED', None)
    require(before in (unsettled, settled), 'ETH_STATE_CHANGED')
    payload, saved = load_json(row['payload']), load_json(row['result'])
    require(payload.get('intent_id') == OPERATION and payload.get('client_order_id') == CLIENT and
            payload.get('symbol') == 'ETHUSDT' and payload.get('side') == 'LONG' and
            payload.get('position_side') == 'BOTH', 'INTENT_CHANGED')
    entry, protection = payload.get('entry'), payload.get('protection')
    require(isinstance(entry, dict) and isinstance(protection, dict) and entry.get('side') == 'BUY' and
            entry.get('order_type') == 'LIMIT' and entry.get('time_in_force') == 'GTC' and
            numeric_equal(entry.get('price'), '2610') and numeric_equal(entry.get('quantity'), '0.181') and
            protection.get('exit_side') == 'SELL' and protection.get('working_type') == 'MARK_PRICE' and
            numeric_equal(protection.get('stop_loss'), '2585') and
            numeric_equal(protection.get('take_profit'), '2730'), 'INTENT_CHANGED')
    if before == unsettled:
        expected = dict(source='BINANCE_FUTURES', state='POSITION_PROTECTED', symbol='ETHUSDT',
                        client_order_id=CLIENT, order_id=ENTRY_ID, first_fill_at=FIRST_FILL,
                        sl_order_id=SL_ID, tp_order_id=TP_ID)
        require(isinstance(saved, dict) and all(saved.get(key) == value for key, value in expected.items())
                and saved.get('sl_confirmed') is True and saved.get('tp_confirmed') is True and
                numeric_equal(saved.get('filled_quantity'), '0.181'), 'SAVED_PROOF_CHANGED')
    else:
        closure_proof(saved)
    receipt = db.execute('SELECT * FROM robot_entry_receipts WHERE id=?', (CLIENT,)).fetchone()
    require(receipt is not None and receipt['symbol'] == 'ETHUSDT' and
            receipt['entry_day'] == CASE_DAY and receipt['confirmed_at'] == FIRST_FILL,
            'RECEIPT_CHANGED')
    others = db.execute("SELECT id FROM order_intents WHERE state IN ('SUBMITTING','NEEDS_REVIEW')").fetchall()
    require([item[0] for item in others] == ([] if before == settled else [OPERATION]),
            'OTHER_UNRESOLVED_ORDER')
    return row, candidate, dict(receipt), before == settled


def protected_digest(db):
    """Everything except the one owned observation/candidate and report caches."""
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    snapshot = []
    for name in tables:
        if name in REPORT_TABLES:
            continue
        quoted = '"'+name.replace('"', '""')+'"'
        sql, arguments = 'SELECT * FROM '+quoted, ()
        if name in ('order_intents', 'robot_candidates'):
            sql += ' WHERE id<>?'
            arguments = (OPERATION,)
        rows = []
        for row in db.execute(sql, arguments):
            values = []
            for value in row:
                if isinstance(value, bytes):
                    values.append(['bytes', value.hex()])
                elif isinstance(value, float):
                    values.append(['float', value.hex()])
                else:
                    values.append([type(value).__name__, value])
            rows.append(json.dumps(values, separators=(',', ':')))
        snapshot.append([name, sorted(rows)])
    return hashlib.sha256(json.dumps(snapshot, separators=(',', ':')).encode()).hexdigest()


def backup_database(db, trading):
    maintenance = trading/'maintenance'
    if not maintenance.exists():
        maintenance.mkdir(mode=0o700)
    directory_check(maintenance, 'MAINTENANCE_NOT_PRIVATE', private=True)
    folder = Path(tempfile.mkdtemp(prefix='eth-closed-', dir=maintenance))
    folder.chmod(0o700)
    backup = folder/'ledger.before.sqlite3'
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    with sqlite3.connect(backup) as saved:
        saved.execute('PRAGMA trusted_schema=OFF')
        db.backup(saved)
        require(saved.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'BACKUP_INVALID')
    private_file(backup, 'BACKUP_NOT_PRIVATE')
    return backup


def record(data_dir='/data', *, apply=False, source_hashes=None, source_root='/app'):
    require(os.geteuid() == EXPECTED_UID, 'WORKER_UID_REQUIRED')
    check_sources(source_root, source_hashes)
    Coordinator, RobotStore, Gateway, Reader, account_state, day = production(source_root)
    trading = Path(data_dir).absolute()/'trading'
    directory_check(trading, 'TRADING_PATH_INVALID')
    path, lock_path = trading/'ledger.sqlite3', trading/'cycle.lock'
    ledger_info = private_file(path, 'JOURNAL_NOT_PRIVATE')
    lock_info = private_file(lock_path, 'LOCK_NOT_PRIVATE')
    lock = db = None
    try:
        lock = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(lock)
        require((info.st_dev, info.st_ino) == (lock_info.st_dev, lock_info.st_ino), 'LOCK_CHANGED')
        deadline = time.monotonic()+15
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, 'WORKER_BUSY')
                time.sleep(.2)
        current_files(trading, path, ledger_info, lock_path, lock_info, lock)
        db = sqlite3.connect(path.as_uri()+'?mode='+('rw' if apply else 'ro'), uri=True,
                             isolation_level=None, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA trusted_schema=OFF')
        if not apply:
            db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'JOURNAL_INVALID')
        row, candidate, receipt, already = case_rows(db)
        preserved = protected_digest(db)
        c = Coordinator.__new__(Coordinator)
        c.db, c.store = db, RobotStore(db, initialize=False)
        intent = c.verified_intent(row)
        require(intent == load_json(row['payload']), 'INTENT_CHANGED')
        db.execute('COMMIT')
        backup = backup_database(db, trading) if apply and not already else None
        gateway = Gateway()
        original = gateway._wire
        def get_only(method, path, query='', signed=False):
            require(method == 'GET', 'BINANCE_WRITE_BLOCKED')
            return original(method, path, query, signed)
        gateway._wire = get_only
        account = account_state(Reader(), c.store, day())
        require('ETHUSDT' not in account['running_symbols'], 'ETH_POSITION_PRESENT')
        observed = gateway.reconcile(intent)
        closure_proof(observed, fresh=True)
        # Snapshot remains protected by cycle.lock; account/report polling may
        # update only its excluded caches. All user settings/research remain CAS.
        current_files(trading, path, ledger_info, lock_path, lock_info, lock)
        current = case_rows(db)
        require(current == (row, candidate, receipt, already) and protected_digest(db) == preserved,
                'JOURNAL_CHANGED')
        result = dict(status='ETH_CLOSED_ALREADY_VERIFIED' if already else 'INSPECTION_ONLY',
                      can_apply=not already, entry_receipt=True, case_day=CASE_DAY,
                      entries_case_day=c.store.entries(CASE_DAY), exit_order_id=EXIT_ID,
                      protected_rows_sha256=preserved)
        if already or not apply:
            return result
        current_files(trading, path, ledger_info, lock_path, lock_info, lock)
        recorder_error = None
        try:
            c.record_gateway_observation(OPERATION, OPERATION, observed, account)
        except Exception as error:
            # Reporting can fail after the recorder's committed transaction.
            # Never suppress a failed/unknown record: inspect exact stored rows.
            recorder_error = error
        current_files(trading, path, ledger_info, lock_path, lock_info, lock)
        after, after_candidate, after_receipt, settled = case_rows(db)
        immutable = set(row)-{'state', 'result', 'failure_code', 'updated'}
        candidate_immutable = set(candidate)-{'status', 'failure_code'}
        require(settled and all(after[key] == row[key] for key in immutable) and
                all(after_candidate[key] == candidate[key] for key in candidate_immutable) and
                after_receipt == receipt and load_json(after['result']) == observed and
                protected_digest(db) == preserved, 'CLOSURE_RECORD_NOT_VERIFIED')
        if recorder_error is not None:
            result['reporting_error_after_verified_commit'] = type(recorder_error).__name__
        result.update(status='ETH_CLOSED_JOURNAL_VERIFIED', can_apply=False, backup=str(backup))
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
        result = record(args.data_dir, apply=args.apply, source_hashes=args.source_hashes_json,
                        source_root=args.source_root)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps(dict(status='ETH_CLOSED_RECORD_NOT_COMPLETED',
                              reason=str(error) if isinstance(error, Refuse) else type(error).__name__),
                         sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
