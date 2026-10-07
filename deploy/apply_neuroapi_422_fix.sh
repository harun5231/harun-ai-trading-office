#!/usr/bin/env bash
# Source-only request packing fix, followed by case-bound SOL HTTP422 settlement.
(
set -Eeuo pipefail
umask 077
cd /root/harun-ai-trading-office
HARUN_STAGE=PREFLIGHT
HARUN_RESTORE_READY=0
HARUN_WORKER_STOPPED=0
HARUN_CONFIG="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' harun-office-worker-1)"
[ "$HARUN_CONFIG" = /root/harun-ai-trading-office/compose.yaml ] || { printf '%s\n' COMPOSE_CONFIGURATION_CHANGED; exit 1; }
HARUN_BACKUP="$(mktemp -d /root/harun-neuroapi-422.XXXXXX)"

cat > "$HARUN_BACKUP/check.py" <<'HARUN_SOURCE_CHECK'
import hashlib,json,os,stat,sys
from pathlib import Path
expected=json.loads(sys.argv[1]);root=Path(sys.argv[2])
paths=list((root/'worker').rglob('*.py'))+[root/'deploy/container_boot.py',root/'deploy/runtime_permissions.py']
assert set(expected)=={str(p.relative_to(root)) for p in paths}, 'SOURCE_INVENTORY_CHANGED'
for name,value in expected.items():
    p=root/name;s=p.lstat()
    assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and hashlib.sha256(p.read_bytes()).hexdigest()==value, 'SOURCE_MISMATCH:'+name
if len(sys.argv)>3:
    assert not os.path.lexists(root/'worker/neuroapi_request.py'), 'NEW_MODULE_ALREADY_PRESENT'
print('SOURCE_MATCH')
HARUN_SOURCE_CHECK

cat > "$HARUN_BACKUP/settle.py" <<'HARUN_SETTLE_422'
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
HARUN_SETTLE_422

cat > "$HARUN_BACKUP/sdk_patch.py" <<'HARUN_SDK_READBACK_PATCH'
"""Offline source-only patch for bounded readback after an owned algo cancel.

The caller must back up the SDK and manage worker deployment. This utility
never imports the SDK, reads secrets or sends exchange requests.
"""
import ast
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

SOURCE_SHA = 'df6705bbdda9f2a34374fde186244d400e2fc84ecb8f27ce05ad15f35a9bbfc4'
OLD_METHOD = "    def _cancel_algo(self, intent, algo):\n        kind = 'sl' if algo['clientAlgoId'].startswith('hao-sl-') else 'tp'\n        quantity = self._decimal(algo['quantity'], positive=True)\n        current = self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': algo['clientAlgoId']})\n        self._algo_proof(intent, kind, quantity, current)\n        if current['algoStatus'] != 'NEW':\n            if current['algoStatus'] not in ('CANCELED', 'EXPIRED', 'REJECTED') or current.get('actualOrderId') not in ('', '0', 0, None):\n                raise Review('BINANCE_ORDER_EXIT_RACE')\n            return\n        self._request('DELETE', '/fapi/v1/algoOrder', {'algoId': self._id(current['algoId'])})\n        after = self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': algo['clientAlgoId']})\n        self._algo_proof(intent, kind, quantity, after)\n        if after['algoStatus'] != 'CANCELED' or after.get('actualOrderId') not in ('', '0', 0, None):\n            raise Review('BINANCE_ORDER_EXIT_RACE')\n"
NEW_METHOD = "    def _cancel_algo(self, intent, algo):\n        kind = 'sl' if algo['clientAlgoId'].startswith('hao-sl-') else 'tp'\n        quantity = self._decimal(algo['quantity'], positive=True)\n        current = self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': algo['clientAlgoId']})\n        self._algo_proof(intent, kind, quantity, current)\n        if current['algoStatus'] != 'NEW':\n            if current['algoStatus'] not in ('CANCELED', 'EXPIRED', 'REJECTED') or current.get('actualOrderId') not in ('', '0', 0, None):\n                raise Review('BINANCE_ORDER_EXIT_RACE')\n            return\n        if current.get('actualOrderId') not in ('', '0', 0, None):\n            raise Review('BINANCE_ORDER_EXIT_RACE')\n        self._request('DELETE', '/fapi/v1/algoOrder', {'algoId': self._id(current['algoId'])})\n        import time\n        for attempt in range(3):\n            after = self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': algo['clientAlgoId']})\n            self._algo_proof(intent, kind, quantity, after)\n            if after['algoStatus'] == 'CANCELED' and after.get('actualOrderId') in ('', '0', 0, None):\n                return\n            if after['algoStatus'] != 'NEW' or after.get('actualOrderId') not in ('', '0', 0, None) or attempt == 2:\n                raise Review('BINANCE_ORDER_EXIT_RACE')\n            delay = (0.5, 1.0)[attempt]\n            self._time_left(delay)\n            time.sleep(delay)\n"


def method_node(tree):
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'OrderGateway']
    if len(classes) != 1:
        raise ValueError('GATEWAY_CLASS_CHANGED')
    methods = [node for node in classes[0].body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == '_cancel_algo']
    if len(methods) != 1 or type(methods[0]) is not ast.FunctionDef or methods[0].decorator_list:
        raise ValueError('GATEWAY_METHOD_CHANGED')
    return methods[0]


def shape(text):
    return ast.dump(method_node(ast.parse('class OrderGateway:\n' + text)), include_attributes=False)


def transform(raw, expected_sha=SOURCE_SHA):
    if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError('GATEWAY_SOURCE_CHANGED')
    tree = ast.parse(raw)
    method = method_node(tree)
    if ast.dump(method, include_attributes=False) != shape(OLD_METHOD):
        raise ValueError('GATEWAY_METHOD_CHANGED')
    lines = raw.splitlines(keepends=True)
    first = sum(map(len, lines[:method.lineno-1]))
    last = sum(map(len, lines[:method.end_lineno]))
    original = raw[first:last]
    newline = b'\r\n' if b'\r\n' in original else b'\n'
    if original.replace(b'\r\n', b'\n') != OLD_METHOD.encode():
        raise ValueError('GATEWAY_METHOD_BYTES_CHANGED')
    replacement = NEW_METHOD.encode().replace(b'\n', newline)
    changed = raw[:first] + replacement + raw[last:]
    compile(changed, 'order_gateway.py', 'exec')
    patched = ast.parse(changed)
    if ast.dump(method_node(patched), include_attributes=False) != shape(NEW_METHOD):
        raise ValueError('GATEWAY_PATCH_INVALID')
    # The AST assertion also protects other methods and private module contents.
    before_cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'OrderGateway')
    after_cls = next(node for node in patched.body if isinstance(node, ast.ClassDef) and node.name == 'OrderGateway')
    before_cls.body.remove(method)
    after_cls.body.remove(method_node(patched))
    if ast.dump(tree, include_attributes=False) != ast.dump(patched, include_attributes=False):
        raise ValueError('GATEWAY_OTHER_SOURCE_CHANGED')
    return changed


def apply(path, write=False):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError('GATEWAY_SOURCE_UNAVAILABLE')
    raw = path.read_bytes()
    changed = transform(raw)
    result = dict(status='PATCH_READY', before_sha256=hashlib.sha256(raw).hexdigest(),
                  after_sha256=hashlib.sha256(changed).hexdigest())
    if not write:
        return result
    if os.geteuid() != 0:
        raise ValueError('ROOT_REQUIRED')
    fd, name = tempfile.mkstemp(prefix='.gateway-exit-readback-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
            os.fchown(output.fileno(), info.st_uid, info.st_gid)
            output.write(changed)
            output.flush()
            os.fsync(output.fileno())
        current = path.lstat()
        if (current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_gid) != (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid) or path.read_bytes() != raw:
            raise ValueError('GATEWAY_SOURCE_CHANGED')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    result['status'] = 'PATCHED'
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('path')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        report = apply(args.path, args.apply)
    except Exception as error:
        reason = str(error) if isinstance(error, ValueError) and str(error).isupper() else 'GATEWAY_PATCH_FAILED'
        report = dict(error=reason)
    print(json.dumps(report, sort_keys=True))
    raise SystemExit(1 if 'error' in report else 0)
HARUN_SDK_READBACK_PATCH

harun_check_sdk_patcher() {
  python3 -I -B -S - "$HARUN_BACKUP/sdk_patch.py" <<'HARUN_SDK_PATCHER_HASH'
import hashlib,sys
from pathlib import Path
p=Path(sys.argv[1]);assert p.is_file() and not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest()=='6d61b15721d3a510e3b3e64e464d768bc1e5cde8800686aa804d5af0c524f855', 'SDK_PATCHER_CHANGED'
HARUN_SDK_PATCHER_HASH
}

HARUN_OLD_MAP="$(python3 -I -B -S - <<'HARUN_OLD_SOURCE_MAP'
import hashlib,json,os,stat
from pathlib import Path
paths=list(Path('worker').rglob('*.py'))+[Path('deploy/container_boot.py'),Path('deploy/runtime_permissions.py')]
assert all(stat.S_ISREG(p.lstat().st_mode) and p.lstat().st_nlink==1 for p in paths), 'SOURCE_NOT_REGULAR'
assert not os.path.lexists('worker/neuroapi_request.py'), 'NEW_MODULE_ALREADY_PRESENT'
values={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
old={'worker/neuroapi.py': 'cf3445af2972b8227e9fc129c60fcf03de49253d28149948ae0a21f03ca7704c', 'worker/robot.py': 'cc0e1c6b68581fa23402dd61c9de023941a1f13489137282c83ae32ccf0dfa8d', 'worker/diagnostics.py': '2b28a42b0f03c76c97dc5111a33b2be167d346f8aba1103a7c0be10f08382f41'}
old['worker/order_gateway.py']='df6705bbdda9f2a34374fde186244d400e2fc84ecb8f27ce05ad15f35a9bbfc4'
assert all(values.get(name)==value for name,value in old.items()), 'SOURCE_VERSION_CHANGED'
print(json.dumps(values,sort_keys=True))
HARUN_OLD_SOURCE_MAP
)"
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - "$HARUN_OLD_MAP" /app absent < "$HARUN_BACKUP/check.py"
docker compose -f compose.yaml config --quiet

harun_check_helper() {
  python3 -I -B -S - "$HARUN_BACKUP/settle.py" <<'HARUN_HELPER_HASH'
import hashlib,sys
from pathlib import Path
p=Path(sys.argv[1]);assert p.is_file() and not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest()=='447e07d7901e489bdb74cb7f8a302b082e7eeddb9c398b19f37bcf7c0bcacab3', 'SETTLEMENT_HELPER_CHANGED'
HARUN_HELPER_HASH
}
harun_inspect_case() {
  harun_check_helper
  if ! docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - --data-dir /data --source-hashes-json "$HARUN_OLD_MAP" --source-root /app < "$HARUN_BACKUP/settle.py" > "$HARUN_BACKUP/inspection.json"; then
    cat "$HARUN_BACKUP/inspection.json" || true
    return 1
  fi
  python3 -I -B -S - "$HARUN_BACKUP/inspection.json" <<'HARUN_INSPECTION_PROOF'
import json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text());assert p['status']=='INSPECTION_ONLY' and p['can_apply'] is True and p['replacements']==2 and p['maximum_replacements']==3, 'SOL_CASE_CHANGED'
print('SOL_422_CASE_VERIFIED')
HARUN_INSPECTION_PROOF
}
harun_inspect_case
for HARUN_NAME in neuroapi robot diagnostics order_gateway; do
  cp -p "worker/$HARUN_NAME.py" "$HARUN_BACKUP/$HARUN_NAME.before.py"
done
printf '%s\n' absent > "$HARUN_BACKUP/neuroapi_request.before"
printf '%s\n' "$HARUN_OLD_MAP" > "$HARUN_BACKUP/source.before.json"
HARUN_OLD_IMAGE="$(docker inspect --format '{{.Image}}' harun-office-worker-1)"
HARUN_IMAGE_BACKUP="harun-office-worker:before-neuroapi-422-$(date -u +%Y%m%dT%H%M%SZ)"
docker image tag "$HARUN_OLD_IMAGE" "$HARUN_IMAGE_BACKUP"
printf 'Backup: %s\nImage backup: %s\n' "$HARUN_BACKUP" "$HARUN_IMAGE_BACKUP"

harun_restore_source_image() {
  python3 -I -B -S - "$HARUN_BACKUP" <<'HARUN_RESTORE_SOURCE' || return 1
import hashlib,json,os,shutil,stat,sys
from pathlib import Path
backup=Path(sys.argv[1]);old={'worker/neuroapi.py': 'cf3445af2972b8227e9fc129c60fcf03de49253d28149948ae0a21f03ca7704c', 'worker/robot.py': 'cc0e1c6b68581fa23402dd61c9de023941a1f13489137282c83ae32ccf0dfa8d', 'worker/diagnostics.py': '2b28a42b0f03c76c97dc5111a33b2be167d346f8aba1103a7c0be10f08382f41'};new={'worker/neuroapi.py': '23f78304f76b85af4c98bf9e8eff96a4e04572b0e3aa61a8b1874f008b00c3c5', 'worker/robot.py': '1f0ca77f82d4252bdd74fb4af83f32cff1f3da0cdc72d2faab86b5788a5c4423', 'worker/diagnostics.py': '7d2754c5ce0f3badb8b826db6a14705e1d204af75ac22e64f11c0a677f85ba19', 'worker/neuroapi_request.py': 'a98f0dc33d5ddf69fed1b64e04df30ce8ed03b0624e113ab1627b56f8d791bbb'}
old['worker/order_gateway.py']='df6705bbdda9f2a34374fde186244d400e2fc84ecb8f27ce05ad15f35a9bbfc4'
new['worker/order_gateway.py']=old['worker/order_gateway.py']
if (backup/'sdk.inspection.json').is_file():
    proof=json.loads((backup/'sdk.inspection.json').read_text())
    if proof.get('status')=='PATCH_READY':new['worker/order_gateway.py']=proof['after_sha256']
for name,value in old.items():
    p=Path(name);s=p.lstat();saved=backup/(p.stem+'.before.py')
    assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and hashlib.sha256(p.read_bytes()).hexdigest() in (value,new[name]), 'SOURCE_CHANGED_DURING_RECOVERY'
    assert hashlib.sha256(saved.read_bytes()).hexdigest()==value, 'SOURCE_BACKUP_CHANGED'
p=Path('worker/neuroapi_request.py')
if os.path.lexists(p):
    s=p.lstat();assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and hashlib.sha256(p.read_bytes()).hexdigest()==new[str(p)], 'NEW_SOURCE_CHANGED_DURING_RECOVERY'
for name in old:
    p=Path(name);shutil.copy2(backup/(p.stem+'.before.py'),p)
if os.path.lexists('worker/neuroapi_request.py'):Path('worker/neuroapi_request.py').unlink()
print('SOURCE_RESTORED')
HARUN_RESTORE_SOURCE
  docker image tag "$HARUN_OLD_IMAGE" harun-office-worker:latest
}
harun_abort() {
  local HARUN_STATUS="$1"
  trap - ERR INT TERM
  if [ "$HARUN_RESTORE_READY" = 1 ]; then
    if harun_restore_source_image; then
      if [ "$HARUN_WORKER_STOPPED" = 1 ]; then
        docker compose -f compose.yaml up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 worker || printf '%s\n' OLD_IMAGE_RESTART_FAILED
      fi
    else
      printf '%s\n' RESTORE_FAILED
    fi
  fi
  printf 'RECOVERY_NOT_COMPLETED stage=%s backup=%s\n' "$HARUN_STAGE" "$HARUN_BACKUP"
  exit "$HARUN_STATUS"
}
HARUN_RESTORE_READY=1
trap 'harun_abort "$?"' ERR
trap 'harun_abort 130' INT
trap 'harun_abort 143' TERM

cat > "$HARUN_BACKUP/request.patch" <<'HARUN_RUNTIME_PATCH'
diff --git a/worker/neuroapi.py b/worker/neuroapi.py
--- a/worker/neuroapi.py
+++ b/worker/neuroapi.py
@@ -10,6 +10,7 @@
 from .core import Review,number,Signal,D
 from .diagnostics import safe_code,validation_code
 from .http_client import request
+from .neuroapi_request import build_request_body

 BASE='https://api.neurobro.ai/api/v1'
 SYMBOL_PATTERN=r'^[A-Z0-9]{2,18}USDT$'
@@ -130,8 +131,7 @@
         if not permitted():raise ResearchPaused('RESEARCH_PAUSED')
         self.require_key()
         if screening_count(schema) is not None and (not isinstance(catalog,dict) or not catalog):raise Review('SCREENING_CATALOG_REQUIRED')
-        body={'prompt':prompt,'mode':'smart','stream':False,'output_schema':schema}
-        if context is not None:body['message_history']=[{'role':'user','content':json.dumps(context,separators=(',',':'))}]
+        body=build_request_body(prompt,schema,context)
         digest=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
         self.db.execute('BEGIN IMMEDIATE')
         try:
@@ -185,5 +185,8 @@
             if failure=='VALIDATION_REJECTED':failure=validation_code(exc)
             elif failure=='NETWORK_UNCERTAIN' and isinstance(exc,Review) and str(exc)=='INVALID_RESPONSE_ENVELOPE':failure='INVALID_RESPONSE_ENVELOPE'
             failure=safe_code(failure)
-            self.db.execute("UPDATE api_requests SET state='NEEDS_REVIEW',failure_code=? WHERE operation=?",(failure,operation))
+            # A received 422 is a terminal rejection of this request body.
+            # Preserve it for audit; never replay it or invent model output.
+            state='REJECTED_REQUEST_VALIDATION' if failure=='HTTP_422' else 'NEEDS_REVIEW'
+            self.db.execute('UPDATE api_requests SET state=?,failure_code=? WHERE operation=?',(state,failure,operation))
             raise Review('NEUROAPI_REQUEST_NEEDS_REVIEW' if failure=='NETWORK_UNCERTAIN' else failure) from None
diff --git a/worker/robot.py b/worker/robot.py
--- a/worker/robot.py
+++ b/worker/robot.py
@@ -45,10 +45,17 @@
         self.db.execute('UPDATE robot_cycles SET data=?,state=? WHERE id=?',(json.dumps(data),state,cycle))
     def replaceable_result(self,row):
         if row['status']=='HOLD':return True
-        # A local fee/RR rejection may select another coin, never replay an order.
-        return (row['status']=='REJECTED' and row['failure_code'] in ('NET_RISK_REWARD_BELOW_2','NET_RISK_REWARD_NOT_TARGET_2')
-            and row['plan'] is None and not self.db.execute(
-                'SELECT 1 FROM order_intents WHERE candidate_id=? LIMIT 1',(row['id'],)).fetchone())
+        if row['status']!='REJECTED' or row['plan'] is not None or self.db.execute(
+                'SELECT 1 FROM order_intents WHERE candidate_id=? LIMIT 1',(row['id'],)).fetchone():return False
+        # Only local RR failures or a recorded request-body rejection may
+        # select another coin. An unknown outcome never qualifies.
+        if row['failure_code'] in ('NET_RISK_REWARD_BELOW_2','NET_RISK_REWARD_NOT_TARGET_2'):return True
+        if row['failure_code']!='HTTP_422':return False
+        return bool(self.db.execute('''SELECT 1 FROM api_requests a JOIN robot_jobs j ON j.operation=a.operation
+            WHERE a.operation=? AND a.state='REJECTED_REQUEST_VALIDATION' AND a.failure_code='HTTP_422'
+            AND a.output IS NULL AND a.idempotency IS NULL AND a.attempts=1
+            AND j.cycle=? AND j.symbol=? AND j.kind='ANALYSIS' AND j.state='REQUEST_REJECTED' ''',
+            (row['id'],row['cycle'],row['symbol'])).fetchone())
     def replacement_count(self,data,results):
         return sum(r['symbol'] in data.get('round_symbols',[]) and self.replaceable_result(r) for r in results)
     def research_budget(self,data,results):
@@ -437,8 +444,10 @@
             return self.store.report('WAITING',account,wait_reason=self.pause_reason())
         except Exception as error:
             reason=validation_code(error)
-            row=self.db.execute('SELECT state FROM api_requests WHERE operation=?',(operation,)).fetchone()
-            unknown=not row or row[0]!='COMPLETE'
+            row=self.db.execute('SELECT state,failure_code FROM api_requests WHERE operation=?',(operation,)).fetchone()
+            request_rejected=bool(row and row['state']=='REJECTED_REQUEST_VALIDATION' and row['failure_code']=='HTTP_422')
+            local_rejected=not row and reason=='NEUROAPI_REQUEST_LIMIT_EXCEEDED'
+            unknown=not (row and row['state']=='COMPLETE' or request_rejected or local_rejected)
             if row and row[0]=='COMPLETE':self.neuro.record_validation(operation,error)
             plan=None
         data['queue'].pop(0);data['seen'].append(symbol)
@@ -446,7 +455,8 @@
         try:
             self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',(operation,cycle,symbol,state,json.dumps(plan) if plan else None,reason))
             self.save_cycle(cycle,data,'NEEDS_REVIEW' if unknown else 'ACTIVE')
-            self.db.execute('UPDATE robot_jobs SET state=? WHERE operation=?',('NEEDS_REVIEW' if unknown else 'COMPLETE',operation))
+            job_state='NEEDS_REVIEW' if unknown else 'REQUEST_REJECTED' if state=='REJECTED' and reason in ('HTTP_422','NEUROAPI_REQUEST_LIMIT_EXCEEDED') else 'COMPLETE'
+            self.db.execute('UPDATE robot_jobs SET state=? WHERE operation=?',(job_state,operation))
             self.db.execute('COMMIT')
         except BaseException:self.db.execute('ROLLBACK');raise
         return self.store.report(state,account,reason)
diff --git a/worker/diagnostics.py b/worker/diagnostics.py
--- a/worker/diagnostics.py
+++ b/worker/diagnostics.py
@@ -12,7 +12,7 @@
  'MARKET_DATA_REJECTED','LOCAL_PROCESSING_FAILED','STALE_CONTRACT_CONTEXT','INVALID_CONTRACT_CONTEXT',
  'INVALID_ORDER_CONTRACT','INVALID_RISK_REWARD','INVALID_RISK_TARGET','INVALID_EXECUTION_QUANTITY',
  'INVALID_SIDE','INVALID_RULES','RISK_ABOVE_TARGET','FEE_EVIDENCE_UNAVAILABLE','INVALID_FEE_EVIDENCE',
- 'STALE_FEE_EVIDENCE','NET_RISK_REWARD_BELOW_2','NET_RISK_REWARD_NOT_TARGET_2','ORDER_COSTS_CHANGED','INVALID_COST_MODEL','RESEARCH_PAUSED'))
+ 'STALE_FEE_EVIDENCE','NET_RISK_REWARD_BELOW_2','NET_RISK_REWARD_NOT_TARGET_2','ORDER_COSTS_CHANGED','INVALID_COST_MODEL','RESEARCH_PAUSED','NEUROAPI_REQUEST_LIMIT_EXCEEDED'))
 # Old operation IDs remain readable audit metadata, never executable requests.
 OPERATION_PATTERN=(r'\d{4}-\d{2}-\d{2}:(?:robot-v8:[0-2]:(?:screening:[0-3]:[12]|analysis-v8:[A-Z0-9]{2,18}USDT)'
     r'|robot-v9:(?:0|[1-9]\d{0,11}):(?:screening:[0-3]:[12]|analysis-v9:[A-Z0-9]{2,18}USDT))')
@@ -42,7 +42,7 @@
         result=[]
         for operation,state,attempts,failure in rows:
             operation=operation if isinstance(operation,str) and re.fullmatch(OPERATION_PATTERN,operation) else 'OPERATION_REDACTED'
-            state=state if state in ('PENDING','COMPLETE','NEEDS_REVIEW') else 'STATE_REDACTED'
+            state=state if state in ('PENDING','COMPLETE','NEEDS_REVIEW','REJECTED_REQUEST_VALIDATION') else 'STATE_REDACTED'
             reason=safe_code(failure) if failure else ('FAILURE_CODE_UNAVAILABLE' if state=='NEEDS_REVIEW' else None)
             result.append(dict(operation=operation,state=state,attempts=attempts if type(attempts) is int and 0<=attempts<=1000000 else None,failure_code=reason))
         return result
diff --git a/worker/neuroapi_request.py b/worker/neuroapi_request.py
new file mode 100644
--- /dev/null
+++ b/worker/neuroapi_request.py
@@ -0,0 +1,123 @@
+"""Lossless request packing within NeuroAPI's published character limits."""
+import json
+
+from .core import Review
+
+MAX_CHARACTERS = 32_000
+MAX_MESSAGES = 50
+LIMIT_ERROR = 'NEUROAPI_REQUEST_LIMIT_EXCEEDED'
+ANALYSIS_FORMAT = 'ANALYSIS_CONTEXT_PARTS_V1'
+FRAGMENT_FORMAT = 'JSON_CONTEXT_FRAGMENTS_V1'
+ANALYSIS_INSTRUCTION = (
+    'All numbered user messages form ONE complete analysis context. Merge the metadata '
+    'with each timeframe; concatenate its candle chunks in candle_start order. '
+    'Every original candle, price, timestamp, risk constraint and filter is included. '
+    'Use both complete timeframes together; no part replaces or truncates another.'
+)
+FRAGMENT_INSTRUCTION = (
+    'All numbered user messages form ONE complete JSON context. Concatenate '
+    'context_json_fragment strings in part order, then parse that complete JSON. '
+    'Fragments are consecutive text, not independent or truncated contexts.'
+)
+
+
+def _encode(value):
+    # New partitions use actual Unicode characters: the provider limits characters,
+    # not UTF-8 bytes. The historical single-message representation is kept below.
+    return json.dumps(value, separators=(',', ':'), ensure_ascii=False)
+
+
+def _packet(kind, data, *, part=MAX_MESSAGES, parts=MAX_MESSAGES, **details):
+    header = dict(format=ANALYSIS_FORMAT, part=part, parts=parts, kind=kind, **details)
+    if kind == 'metadata':
+        header['instruction'] = ANALYSIS_INSTRUCTION
+    return dict(context_partition=header, data=data)
+
+
+def _analysis_parts(context):
+    """Return valid-JSON candle partitions, or defer unusual shapes to raw packing."""
+    if not isinstance(context, dict) or not isinstance(context.get('timeframes'), dict):
+        return None
+    frames = context['timeframes']
+    if set(frames) != {'1h', '15m'} or any(
+            not isinstance(frame, dict) or not isinstance(frame.get('candles'), list)
+            or not frame['candles'] for frame in frames.values()):
+        return None
+    packets = [_packet('metadata', {key: value for key, value in context.items() if key != 'timeframes'})]
+    if len(_encode(packets[0])) > MAX_CHARACTERS:
+        return None
+    for timeframe, frame in frames.items():
+        candles = frame['candles']
+        start = 0
+        while start < len(candles):
+            def candidate(end):
+                data = {**frame, 'candles': candles[start:end]}
+                return _packet('timeframe', data, timeframe=timeframe, candle_start=start,
+                               candle_end=end, candle_total=len(candles))
+            low, high = start + 1, len(candles)
+            end = start
+            while low <= high:
+                middle = (low + high) // 2
+                if len(_encode(candidate(middle))) <= MAX_CHARACTERS:
+                    end = middle
+                    low = middle + 1
+                else:
+                    high = middle - 1
+            if end == start or len(packets) >= MAX_MESSAGES:
+                return None
+            packets.append(candidate(end))
+            start = end
+    for index, packet in enumerate(packets, 1):
+        packet['context_partition'].update(part=index, parts=len(packets))
+    return [_encode(packet) for packet in packets]
+
+
+def _raw_parts(serialized):
+    """Fallback preserves the exact historical JSON text by concatenation."""
+    fragments = []
+    start = 0
+    def candidate(end, *, part=MAX_MESSAGES, parts=MAX_MESSAGES):
+        return dict(context_partition=dict(format=FRAGMENT_FORMAT, part=part, parts=parts,
+                                           instruction=FRAGMENT_INSTRUCTION),
+                    context_json_fragment=serialized[start:end])
+    while start < len(serialized):
+        if len(fragments) >= MAX_MESSAGES:
+            raise Review(LIMIT_ERROR)
+        low, high = start + 1, min(len(serialized), start + MAX_CHARACTERS)
+        end = start
+        while low <= high:
+            middle = (low + high) // 2
+            if len(_encode(candidate(middle))) <= MAX_CHARACTERS:
+                end = middle
+                low = middle + 1
+            else:
+                high = middle - 1
+        if end == start:
+            raise Review(LIMIT_ERROR)
+        fragments.append(serialized[start:end])
+        start = end
+    return [_encode(dict(context_partition=dict(format=FRAGMENT_FORMAT, part=index,
+                                                parts=len(fragments), instruction=FRAGMENT_INSTRUCTION),
+                         context_json_fragment=fragment))
+            for index, fragment in enumerate(fragments, 1)]
+
+
+def build_request_body(prompt, schema, context=None):
+    """Keep small requests identical; partition oversized contexts without data loss."""
+    if not isinstance(prompt, str) or not 1 <= len(prompt) <= MAX_CHARACTERS:
+        raise Review(LIMIT_ERROR)
+    body = {'prompt': prompt, 'mode': 'smart', 'stream': False, 'output_schema': schema}
+    if context is None:
+        return body
+    serialized = json.dumps(context, separators=(',', ':'))
+    if len(serialized) <= MAX_CHARACTERS:
+        contents = [serialized]
+    else:
+        contents = _analysis_parts(context)
+        if contents is None:
+            contents = _raw_parts(serialized)
+    if not 1 <= len(contents) <= MAX_MESSAGES or any(
+            not 1 <= len(content) <= MAX_CHARACTERS for content in contents):
+        raise Review(LIMIT_ERROR)
+    body['message_history'] = [{'role': 'user', 'content': content} for content in contents]
+    return body
HARUN_RUNTIME_PATCH
HARUN_STAGE=SDK_READBACK_INSPECTION
harun_check_sdk_patcher
if ! python3 -I -B -S "$HARUN_BACKUP/sdk_patch.py" worker/order_gateway.py > "$HARUN_BACKUP/sdk.inspection.json"; then
  cat "$HARUN_BACKUP/sdk.inspection.json" || true
  false
fi
python3 -I -B -S - "$HARUN_BACKUP/sdk.inspection.json" <<'HARUN_SDK_INSPECTION_PROOF'
import json,re,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text())
assert p['status']=='PATCH_READY' and p['before_sha256']=='df6705bbdda9f2a34374fde186244d400e2fc84ecb8f27ce05ad15f35a9bbfc4' and re.fullmatch(r'[a-f0-9]{64}',p['after_sha256']), 'SDK_INSPECTION_NOT_VERIFIED'
HARUN_SDK_INSPECTION_PROOF
HARUN_STAGE=SDK_READBACK_PATCH
harun_check_sdk_patcher
if ! python3 -I -B -S "$HARUN_BACKUP/sdk_patch.py" worker/order_gateway.py --apply > "$HARUN_BACKUP/sdk.applied.json"; then
  cat "$HARUN_BACKUP/sdk.applied.json" || true
  false
fi
python3 -I -B -S - "$HARUN_BACKUP/sdk.inspection.json" "$HARUN_BACKUP/sdk.applied.json" <<'HARUN_SDK_PATCH_PROOF'
import json,sys
from pathlib import Path
before=json.loads(Path(sys.argv[1]).read_text());after=json.loads(Path(sys.argv[2]).read_text())
assert after['status']=='PATCHED' and all(after[key]==before[key] for key in ('before_sha256','after_sha256')), 'SDK_PATCH_NOT_VERIFIED'
print(json.dumps(after,sort_keys=True))
HARUN_SDK_PATCH_PROOF
HARUN_STAGE=SOURCE_PATCH
git apply --check "$HARUN_BACKUP/request.patch"
git apply "$HARUN_BACKUP/request.patch"
HARUN_NEW_MAP="$(python3 -I -B -S - "$HARUN_OLD_MAP" "$HARUN_BACKUP/sdk.inspection.json" <<'HARUN_NEW_SOURCE_MAP'
import hashlib,json,sys
from pathlib import Path
values=json.loads(sys.argv[1]);values.update({'worker/neuroapi.py': '23f78304f76b85af4c98bf9e8eff96a4e04572b0e3aa61a8b1874f008b00c3c5', 'worker/robot.py': '1f0ca77f82d4252bdd74fb4af83f32cff1f3da0cdc72d2faab86b5788a5c4423', 'worker/diagnostics.py': '7d2754c5ce0f3badb8b826db6a14705e1d204af75ac22e64f11c0a677f85ba19', 'worker/neuroapi_request.py': 'a98f0dc33d5ddf69fed1b64e04df30ce8ed03b0624e113ab1627b56f8d791bbb'})
values['worker/order_gateway.py']=json.loads(Path(sys.argv[2]).read_text())['after_sha256']
for name,value in values.items():
    p=Path(name);assert p.is_file() and not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest()==value, 'SOURCE_MISMATCH:'+name
    if name in {'worker/neuroapi.py': '23f78304f76b85af4c98bf9e8eff96a4e04572b0e3aa61a8b1874f008b00c3c5', 'worker/robot.py': '1f0ca77f82d4252bdd74fb4af83f32cff1f3da0cdc72d2faab86b5788a5c4423', 'worker/diagnostics.py': '7d2754c5ce0f3badb8b826db6a14705e1d204af75ac22e64f11c0a677f85ba19', 'worker/neuroapi_request.py': 'a98f0dc33d5ddf69fed1b64e04df30ce8ed03b0624e113ab1627b56f8d791bbb'}:compile(p.read_bytes(),name,'exec')
print(json.dumps(values,sort_keys=True))
HARUN_NEW_SOURCE_MAP
)"
printf '%s\n' "$HARUN_NEW_MAP" > "$HARUN_BACKUP/source.after.json"
python3 -I -B -S "$HARUN_BACKUP/check.py" "$HARUN_NEW_MAP" .
HARUN_STAGE=BUILD
docker compose -f compose.yaml build worker
HARUN_NEW_IMAGE="$(docker image inspect --format '{{.Id}}' harun-office-worker:latest)"
HARUN_STAGE=OFFLINE_IMAGE_CHECK
docker run --rm -i --network none --read-only --user 10001:10001 --entrypoint python "$HARUN_NEW_IMAGE" -I -B -S - "$HARUN_NEW_MAP" /app < "$HARUN_BACKUP/check.py"
python3 -I -B -S "$HARUN_BACKUP/check.py" "$HARUN_NEW_MAP" .
HARUN_STAGE=SECOND_PREFLIGHT
harun_inspect_case
HARUN_STAGE=GRACEFUL_RESTART
HARUN_WORKER_STOPPED=1
docker compose -f compose.yaml stop -t 660 worker
docker compose -f compose.yaml up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 worker
# Once the new worker is healthy, retain this functional runtime on any later
# journal/diagnostic failure. No financial journal or ON/OFF choice is restored.
HARUN_RESTORE_READY=0
HARUN_STAGE=SOL_REQUEST_SETTLEMENT
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - "$HARUN_NEW_MAP" /app < "$HARUN_BACKUP/check.py"
harun_check_helper
if ! docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - --data-dir /data --apply --source-hashes-json "$HARUN_NEW_MAP" --source-root /app < "$HARUN_BACKUP/settle.py" > "$HARUN_BACKUP/settlement.json"; then
  cat "$HARUN_BACKUP/settlement.json" || true
  false
fi
python3 -I -B -S - "$HARUN_BACKUP/settlement.json" "$HARUN_BACKUP/inspection.json" <<'HARUN_SETTLEMENT_PROOF'
import json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text());before=json.loads(Path(sys.argv[2]).read_text())
assert p['status']=='SOL_HTTP_422_SETTLED' and p['request_state']=='REJECTED_REQUEST_VALIDATION' and p['job_state']=='REQUEST_REJECTED' and p['cycle_state']=='ACTIVE', 'SETTLEMENT_NOT_VERIFIED'
assert p['replacements']==2 and p['maximum_replacements']==3 and p['billing_outcome']=='UNKNOWN', 'SETTLEMENT_COUNTER_CHANGED'
assert all(p[key]==before[key] for key in ('request_body_sha256','cycle_data_sha256')), 'SETTLEMENT_EVIDENCE_CHANGED'
print(json.dumps(p,sort_keys=True))
HARUN_SETTLEMENT_PROOF

# The ON worker may already spend the final replacement after helper unlock.
# Read current progress without reopening the settled request or replaying SOL.
HARUN_STAGE=READ_ONLY_STATUS
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - "$HARUN_NEW_MAP" /app < "$HARUN_BACKUP/check.py"
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - <<'HARUN_CURRENT_STATE'
import json,sqlite3
operation='2026-10-07:robot-v9:1:analysis-v9:SOLUSDT';cycle='2026-10-07:robot-v9:1'
db=sqlite3.connect('file:/data/trading/ledger.sqlite3?mode=ro',uri=True);db.row_factory=sqlite3.Row
db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
a=db.execute('SELECT state,attempts,failure_code,output,idempotency FROM api_requests WHERE operation=?',(operation,)).fetchone()
j=db.execute('SELECT state FROM robot_jobs WHERE operation=?',(operation,)).fetchone()
assert a and a['state']=='REJECTED_REQUEST_VALIDATION' and a['attempts']==1 and a['failure_code']=='HTTP_422' and a['output'] is None and a['idempotency'] is None and j and j['state']=='REQUEST_REJECTED', 'SETTLED_REQUEST_CHANGED'
assert not db.execute('SELECT 1 FROM order_intents WHERE candidate_id=?',(operation,)).fetchone(), 'SOL_ORDER_PRESENT'
c=db.execute('SELECT state,data FROM robot_cycles WHERE id=?',(cycle,)).fetchone();data=json.loads(c['data'])
row=db.execute('SELECT enabled FROM robot_settings WHERE id=1').fetchone();assert row and row['enabled'] in (0,1), 'ROBOT_SETTING_INVALID'
candidates=[dict(r) for r in db.execute('SELECT symbol,status,failure_code FROM robot_candidates WHERE cycle=? ORDER BY rowid',(cycle,))]
eth=db.execute("SELECT state,result FROM order_intents WHERE symbol='ETHUSDT'").fetchone();proof=json.loads(eth['result']) if eth and eth['result'] else {}
unresolved=[dict(r) for r in db.execute("SELECT symbol,state,failure_code FROM order_intents WHERE state IN ('SUBMITTING','NEEDS_REVIEW')")]
print(json.dumps(dict(status='RECOVERY_STATE_READ',robot_on=bool(row['enabled']),settled_sol_request=a['state'],settled_sol_job=j['state'],cycle_state=c['state'],replacements=data.get('replacements'),target=data.get('target'),candidates=candidates,eth_state=eth['state'] if eth else None,eth_sl_confirmed=proof.get('sl_confirmed'),eth_tp_confirmed=proof.get('tp_confirmed'),current_unresolved_orders=unresolved),sort_keys=True))
db.close()
HARUN_CURRENT_STATE
printf '%s\n' NEUROAPI_422_RECOVERY_VERIFIED
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker.robot_status
)
