#!/usr/bin/env python3
"""Inspect, or explicitly retire proven pre-v8 research claims without replay.

Use container Python -I -B -S as UID10001. No SDK/provider imports, credentials,
network calls, settings updates, order changes, or generic journal clearing.
"""
import argparse
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
import uuid

ARCHIVE = 'legacy_research_archive'
ARCHIVE_COLUMNS = ('operation', 'original_row', 'row_sha256', 'scope', 'retired_at')
LEGACY = re.compile(r'([0-9]{4}-[0-9]{2}-[0-9]{2}):('
    r'robot-v7:[0-2]:(?:screening:[0-3]:[12]|analysis-v7:[A-Z0-9]{2,18}USDT)'
    r'|manual-screening:v2|analysis-v[3-6]:[A-Z0-9]{2,18}USDT'
    r'|screening|replacement-screening:[1-3]|analysis:[A-Z0-9]{2,18}USDT)')
PUBLIC_FAILURES = frozenset(('NETWORK_UNCERTAIN', 'LOCAL_PROCESSING_FAILED',
    'INVALID_RESPONSE_ENVELOPE', 'INVALID_SETUP_SCHEMA', 'INVALID_OUTPUT_SCHEMA',
    'INVALID_SCREENING_SCHEMA', 'INVALID_SCREENING_SYMBOL', 'INVALID_SETUP_SYMBOL_SIDE', 'MARKET_DATA_UNAVAILABLE',
    'BINANCE_NOT_CONFIGURED', 'NEUROAPI_NOT_CONFIGURED', 'RATE_LIMITED',
    'RESEARCH_PAUSED', 'HTTP_401', 'HTTP_403', 'HTTP_429', 'HTTP_500', 'HTTP_503'))
MAX_ROWS = 10000
MAX_ROW_BYTES = 2 * 1024 * 1024
MAX_UNRESOLVED_DETAILS = 50
MAX_PRE_ORDER_ARCHIVE_BYTES = 64 * 1024 * 1024
PRE_ORDER_ARCHIVE = 'ledger-pre-order.sqlite3'
PRE_ORDER_SCOPE = re.compile(r'pre_order_archive:([0-9a-f]{64})')
# Published pre-order core.py (1b6b6b2); these anchors cannot be inferred from
# a filename, timestamp, or an opaque operation ID.
PRE_ORDER_CORE_SQL = '''
CREATE TABLE trades(id TEXT PRIMARY KEY, day TEXT NOT NULL, source TEXT NOT NULL,
  state TEXT NOT NULL, plan TEXT NOT NULL, exit_price TEXT, pnl TEXT, created TEXT NOT NULL, closed_day TEXT);
CREATE TABLE events(seq INTEGER PRIMARY KEY,at TEXT,state TEXT,agent TEXT,message TEXT);
'''
PRE_ORDER_TABLES = frozenset(('trades', 'events', 'cycles', 'analysis_checks',
    'api_requests', 'robot_settings', 'robot_cycles', 'robot_jobs', 'robot_setups',
    'robot_decisions', 'robot_simulations', 'robot_status', 'robot_entry_receipts',
    'shadow_plans', 'shadow_events', 'live_records', 'live_actions', 'live_events',
    'live_settings', 'live_requests', 'live_arms', 'live_scheduler', 'sqlite_stat1', 'sqlite_stat4'))
PRE_ORDER_EMPTY_TABLES = ('trades', 'robot_entry_receipts', 'shadow_plans', 'shadow_events',
    'live_records', 'live_actions', 'live_events', 'live_requests', 'live_arms', 'live_scheduler')
PUBLIC_OPERATION_TOKENS = {
    **{'robot-v' + str(version): 'ROBOT_V' + str(version) for version in range(1, 10)},
    **{'analysis-v' + str(version): 'ANALYSIS_V' + str(version) for version in range(1, 10)},
    'screening': 'SCREENING', 'analysis': 'ANALYSIS',
    'replacement-screening': 'REPLACEMENT_SCREENING',
    'manual-screening': 'MANUAL_SCREENING', 'v2': 'VERSION_V2',
}
CURRENT_OPERATIONS = {
    'CURRENT_V8': re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}:robot-v8:[0-2]:'
        r'(?:screening:[0-3]:[12]|analysis-v8:[A-Z0-9]{2,18}USDT)'),
    'CURRENT_V9': re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}:robot-v9:(?:0|[1-9][0-9]{0,11}):'
        r'(?:screening:[0-3]:[12]|analysis-v9:[A-Z0-9]{2,18}USDT)'),
}


class Refuse(Exception):
    pass


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def checksum(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(columns, row):
    cells = []
    for value in row:
        if value is None: cell = ['null', None]
        elif type(value) is int: cell = ['integer', value]
        elif type(value) is float: cell = ['real', struct.pack('>d', value).hex()]
        elif type(value) is str: cell = ['text', value]
        elif type(value) is bytes: cell = ['blob', value.hex()]
        else: raise Refuse('UNSUPPORTED_SQL_VALUE')
        cells.append(cell)
    payload = json.dumps([list(columns), cells], ensure_ascii=True, separators=(',', ':')).encode('ascii')
    if len(payload) > MAX_ROW_BYTES: raise Refuse('REQUEST_ROW_TOO_LARGE')
    return payload


def classify(operation):
    if not isinstance(operation, str) or len(operation) > 120: return None
    match = LEGACY.fullmatch(operation)
    if not match: return None
    try: date.fromisoformat(match[1])
    except ValueError: return None
    tail = match[2]
    if tail.startswith('robot-v7:'):
        return ('robot_v7_screening' if ':screening:' in tail else 'robot_v7_analysis', match[1], operation.rsplit(':screening:', 1)[0] if ':screening:' in tail else operation.rsplit(':analysis-v7:', 1)[0])
    if tail.startswith('analysis-v'): return ('analysis_' + tail.split(':')[0].replace('-', '_'), match[1], match[1])
    if tail.startswith('analysis:'): return ('legacy_analysis', match[1], match[1])
    if tail.startswith('replacement-screening:'): return ('legacy_replacement_screening', match[1], match[1])
    if tail == 'manual-screening:v2': return ('legacy_manual_screening_v2', match[1], match[1])
    return ('legacy_screening', match[1], match[1])


def public_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        return None
    try: return date.fromisoformat(value).isoformat()
    except ValueError: return None


def operation_metadata(operation):
    """Diagnostic labels only; these never authorize retirement or replay."""
    digest = checksum(operation.encode('utf-8') if isinstance(operation, str)
        else encoded(('operation',), (operation,)))
    if not isinstance(operation, str) or len(operation) > 120:
        return dict(operation_sha256=digest, scope_category='UNRECOGNIZED',
            operation_date=None, shape=['OPAQUE'])
    parts = operation.split(':')
    day = public_date(parts[0])
    category = 'LEGACY_RECOGNIZED' if classify(operation) else 'UNRECOGNIZED'
    if day:
        for label, pattern in CURRENT_OPERATIONS.items():
            if pattern.fullmatch(operation): category = label
    shape = []
    if len(parts) > 12: shape = ['OPAQUE']
    else:
        for offset, part in enumerate(parts):
            if offset == 0 and day: token = 'DATE'
            elif part in PUBLIC_OPERATION_TOKENS: token = PUBLIC_OPERATION_TOKENS[part]
            elif re.fullmatch(r'[0-9]{1,12}', part): token = 'INTEGER'
            elif re.fullmatch(r'[A-Z0-9]{2,18}USDT', part): token = 'USDT_SYMBOL'
            else: token = 'REDACTED'
            shape.append(token)
    return dict(operation_sha256=digest, scope_category=category,
        operation_date=day, shape=shape)


def public_created(value):
    if value is None: return 'UNAVAILABLE'
    # Comparisons also exclude nonfinite REALs; no arbitrary value is printed.
    if type(value) not in (int, float) or not 0 <= value <= 4102444800:
        return 'INVALID'
    try: return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='microseconds')
    except (ValueError, OverflowError, OSError): return 'INVALID'


def public_failure(value):
    return value if value in PUBLIC_FAILURES else 'UNAVAILABLE' if value is None else 'REDACTED'


def unresolved_details(columns, rows):
    index = {name: offset for offset, name in enumerate(columns)}
    unresolved = [row for row in rows if row[index['state']] in ('PENDING', 'NEEDS_REVIEW')]
    keyed = [(operation_metadata(row[index['operation']]), row) for row in unresolved]
    keyed.sort(key=lambda item: item[0]['operation_sha256'])
    details = []
    for metadata, row in keyed[:MAX_UNRESOLVED_DETAILS]:
        created = row[index['created']] if 'created' in index else None
        attempts = row[index['attempts']] if 'attempts' in index else None
        failure = row[index['failure_code']] if 'failure_code' in index else None
        details.append(dict(metadata, state=row[index['state']],
            created_at=public_created(created),
            attempts=attempts if type(attempts) is int and 0 <= attempts <= 1000
                else 'UNAVAILABLE' if attempts is None else 'INVALID',
            failure_code=public_failure(failure)))
    return dict(unresolved_request_count=len(unresolved), unresolved_requests=details,
        unresolved_truncated_count=max(0, len(unresolved) - len(details)))


def identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid,
            stat.S_IMODE(info.st_mode), info.st_nlink)


def directory(path):
    path = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        info = os.fstat(fd)
        if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
            raise Refuse('UNSAFE_STATE_DIRECTORY')
        return path, fd
    except BaseException:
        os.close(fd)
        raise


def inside_checkout(root):
    return any((ancestor / '.git').exists() for ancestor in (root, *root.parents))


def checked_file(parent, name, missing=False):
    try: fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        if missing: return None
        raise Refuse('STATE_FILE_MISSING') from None
    except OSError: raise Refuse('UNSAFE_STATE_FILE') from None
    try:
        value = os.fstat(fd)
        if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
                or value.st_uid not in (0, os.geteuid()) or value.st_mode & 0o7022):
            raise Refuse('UNSAFE_STATE_FILE')
        return fd
    except BaseException:
        os.close(fd)
        raise


def validate_files(parent, files, writing=False):
    for name, fd in files.items():
        try: current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if fd is None: continue
            raise Refuse('STATE_FILE_CHANGED') from None
        if fd is None and writing and name in ('ledger.sqlite3-wal', 'ledger.sqlite3-shm', 'ledger.sqlite3-journal'):
            checked = checked_file(parent, name)
            os.close(checked)
            continue
        if fd is None or identity(current) != identity(os.fstat(fd)):
            raise Refuse('STATE_FILE_CHANGED')


def validate_directories(root, root_fd, parent):
    for path, fd in ((root, root_fd), (root / 'trading', parent)):
        try: current = path.lstat()
        except OSError: raise Refuse('STATE_DIRECTORY_CHANGED') from None
        if not stat.S_ISDIR(current.st_mode) or identity(current) != identity(os.fstat(fd)):
            raise Refuse('STATE_DIRECTORY_CHANGED')


def connect(path, readonly=True, immutable=False):
    uri = path.as_uri() + ('?mode=ro' if readonly else '?mode=rw')
    if immutable: uri += '&immutable=1'
    db = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
    if readonly: db.execute('PRAGMA query_only=ON')
    return db


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def rowset(db):
    if 'api_requests' not in tables(db): raise Refuse('REQUEST_SCHEMA_UNAVAILABLE')
    info = db.execute('PRAGMA table_info(api_requests)').fetchall()
    columns = tuple(row[1] for row in info)
    if not {'operation', 'state'}.issubset(columns) or not any(row[1] == 'operation' and row[5] == 1 for row in info):
        raise Refuse('REQUEST_SCHEMA_UNSUPPORTED')
    rows = db.execute('SELECT * FROM api_requests ORDER BY operation LIMIT ?', (MAX_ROWS + 1,)).fetchall()
    if len(rows) > MAX_ROWS: raise Refuse('REQUEST_JOURNAL_TOO_LARGE')
    return columns, rows


def unsafe_schema(db, names):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN (?,?) LIMIT 1", ('api_requests', ARCHIVE)).fetchone():
        return True
    for name in names:
        foreign = db.execute('PRAGMA foreign_key_list(' + quoted(name) + ')').fetchall()
        if name in ('api_requests', ARCHIVE) and foreign or any(row[2] in ('api_requests', ARCHIVE) for row in foreign):
            return True
    return False


def table_schema(db, name, normalize_core=False):
    """Exact ordered schema, including indexes, without exposing SQL in output."""
    info = db.execute('PRAGMA table_xinfo(' + quoted(name) + ')').fetchall()
    definitions = db.execute('SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? ORDER BY type,name', (name,)).fetchall()
    if not info or any(row[6] != 0 for row in info): raise Refuse('PRE_ORDER_SCHEMA_UNSUPPORTED')
    definitions = [(kind, item, ''.join(sql.split()).upper() if normalize_core and sql else sql) for kind, item, sql in definitions]
    indexes = []
    for row in db.execute('PRAGMA index_list(' + quoted(name) + ')').fetchall():
        indexes.append((row, db.execute('PRAGMA index_xinfo(' + quoted(row[1]) + ')').fetchall()))
    foreign = db.execute('PRAGMA foreign_key_list(' + quoted(name) + ')').fetchall()
    return json.dumps([info, definitions, indexes, foreign], ensure_ascii=True, separators=(',', ':')).encode('ascii')


def screening_rejection(columns, row):
    values = dict(zip(columns, row))
    operation = values.get('operation')
    created = values.get('created')
    body_hash = values.get('body_hash')
    return (isinstance(operation, str) and 1 <= len(operation) <= 120 and ':' not in operation
        and 'robot-v' not in operation.lower() and 'analysis-v' not in operation.lower()
        and values.get('state') == 'NEEDS_REVIEW' and values.get('failure_code') == 'INVALID_SCREENING_SYMBOL'
        and type(values.get('attempts')) is int and values['attempts'] == 1
        and 'output' in values and values['output'] is None
        and isinstance(body_hash, str) and bool(re.fullmatch(r'[0-9a-fA-F]{64}', body_hash))
        and type(created) in (int, float) and 0 < created <= 4102444800)


def contains_operation(value, operation):
    pending = [value]
    count = 0
    while pending:
        item = pending.pop()
        count += 1
        if count > MAX_ROWS: raise Refuse('PRE_ORDER_REFERENCE_TOO_LARGE')
        if isinstance(item, str) and item == operation: return True
        if isinstance(item, dict): pending.extend(item.keys()); pending.extend(item.values())
        elif isinstance(item, list): pending.extend(item)
    return False


def no_operation_references(db, operation):
    names = tables(db)
    references = {
        'robot_jobs': ('operation', 'cycle'), 'robot_setups': ('id', 'cycle'),
        'robot_candidates': ('id', 'cycle'), 'robot_cycles': ('id',),
        'analysis_checks': ('operation',), 'robot_decisions': ('setup_id',),
        'robot_simulations': ('setup_id',), 'cycles': ('day',),
    }
    for name, expected in references.items():
        if name not in names: continue
        columns = {row[1] for row in db.execute('PRAGMA table_info(' + quoted(name) + ')')}
        if not set(expected).issubset(columns): raise Refuse('PRE_ORDER_REFERENCE_SCHEMA_UNSUPPORTED')
        condition = ' OR '.join(quoted(column) + '=?' for column in expected)
        if db.execute('SELECT 1 FROM ' + quoted(name) + ' WHERE ' + condition + ' LIMIT 1', (operation,) * len(expected)).fetchone():
            raise Refuse('PRE_ORDER_REQUEST_REFERENCED')
    for name, field, required in (('robot_setups', 'plan', True), ('robot_candidates', 'plan', True),
            ('robot_cycles', 'data', True), ('analysis_checks', 'result', True),
            ('robot_simulations', 'result', True), ('cycles', 'data', False)):
        if name not in names: continue
        columns = {row[1] for row in db.execute('PRAGMA table_info(' + quoted(name) + ')')}
        if field not in columns:
            if required: raise Refuse('PRE_ORDER_REFERENCE_SCHEMA_UNSUPPORTED')
            continue
        values = db.execute('SELECT ' + quoted(field) + ' FROM ' + quoted(name) + ' WHERE ' + quoted(field) + ' IS NOT NULL LIMIT ?', (MAX_ROWS + 1,)).fetchall()
        if len(values) > MAX_ROWS: raise Refuse('PRE_ORDER_REFERENCE_TOO_LARGE')
        for (value,) in values:
            if not isinstance(value, str) or len(value.encode('utf-8')) > MAX_ROW_BYTES:
                raise Refuse('PRE_ORDER_REFERENCE_SCHEMA_UNSUPPORTED')
            try: parsed = json.loads(value)
            except (ValueError, RecursionError): raise Refuse('PRE_ORDER_REFERENCE_SCHEMA_UNSUPPORTED') from None
            if contains_operation(parsed, operation): raise Refuse('PRE_ORDER_REQUEST_REFERENCED')


def validated_shadow_rows(db):
    """Verify the two historical unexecuted models found in the VPS archive."""
    if 'shadow_plans' not in tables(db): return []
    if not db.execute('SELECT 1 FROM shadow_plans LIMIT 1').fetchone(): return []
    schema = 'CREATE TABLE shadow_plans(setup_id TEXT PRIMARY KEY,day TEXT NOT NULL,symbol TEXT NOT NULL,plan TEXT NOT NULL,state TEXT NOT NULL,model TEXT NOT NULL,UNIQUE(day,symbol))'
    keys = set('status setup_id business_day symbol side position_mode positionSide margin_target leverage_target execution_quantity entry TP SL calculated_risk actual_RR protective_side client_ids entry_order take_profit_order stop_loss_order would_submit live_execution mode failure_policy future_reconciliation future_states'.split())
    optional = {'failure_code', 'required_account_mutations'}
    default = dict(state='PLAN_READY', tp_confirmed=False, sl_confirmed=False, model_only=True, fill_confirmed=False)

    def reject(*unused):
        raise Refuse('PRE_ORDER_ARCHIVE_ORDER_EVIDENCE_PRESENT')

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: reject()
            value[key] = item
        return value

    def decode(raw):
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_ROW_BYTES: reject()
        try: return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject, parse_float=reject)
        except (ValueError, TypeError, RecursionError): reject()

    def normalized(value):
        if type(value) not in (str, int): reject()
        text = str(value)
        if len(text) > 64 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', text): reject()
        with localcontext() as context:
            context.prec, context.rounding = 160, ROUND_HALF_EVEN
            number = Decimal(text)
            if number <= 0: reject()
            return format(number.normalize(), 'f')

    expected = sqlite3.connect(':memory:')
    try:
        expected.execute(schema)
        if table_schema(db, 'shadow_plans', normalize_core=True) != table_schema(expected, 'shadow_plans', normalize_core=True): reject()
    finally: expected.close()
    stored = db.execute('SELECT setup_id,day,symbol,plan,state,model FROM shadow_plans LIMIT 3').fetchall()
    if not stored: return []
    if len(stored) != 2: reject()
    result = []
    for row in stored:
        if any(not isinstance(value, str) or len(value.encode('utf-8')) > MAX_ROW_BYTES for value in row): reject()
        key, day, symbol, raw, state, model_raw = row
        plan, model = decode(raw), decode(model_raw)
        if not isinstance(plan, dict) or not keys.issubset(plan) or set(plan) - keys - optional: reject()
        if not isinstance(model, dict) or set(model) != set(default) or any(type(model[field]) is not type(value) or model[field] != value for field, value in default.items()): reject()
        if state not in ('SHADOW_PLAN_READY', 'SHADOW_PREFLIGHT_OK', 'NEEDS_REVIEW') or plan['status'] != state: reject()
        if any(plan[field] != value for field, value in (('setup_id', key), ('business_day', day), ('symbol', symbol), ('mode', 'DRY_RUN'))): reject()
        if plan['would_submit'] is not False or plan['live_execution'] is not False: reject()
        if not re.fullmatch(r'[0-9a-f]{64}', key) or public_date(day) != day or not re.fullmatch(r'[A-Z0-9]{2,18}USDT', symbol): reject()
        side = plan['side']
        if side not in ('LONG', 'SHORT') or plan['position_mode'] != 'ONE_WAY' or plan['positionSide'] != 'BOTH' or plan['margin_target'] != 'CROSS' or type(plan['leverage_target']) is not int or plan['leverage_target'] != 75: reject()
        identity = [day, symbol, side] + [normalized(plan[field]) for field in ('entry', 'TP', 'SL', 'execution_quantity')]
        if checksum(json.dumps(identity, separators=(',', ':')).encode()) != key: reject()
        normalized(plan['calculated_risk'])
        rr = plan['actual_RR']
        if type(rr) not in (str, int) or len(str(rr)) > 256 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:E[+-]?[0-9]{1,3})?', str(rr)): reject()
        rr_number = Decimal(str(rr))
        if not rr_number.is_finite() or rr_number <= 0 or abs(rr_number.as_tuple().exponent) > 256: reject()
        ids = {leg: 'ho-' + key[:28] + '-' + suffix for leg, suffix in (('ENTRY', 'e'), ('TP', 't'), ('SL', 's'))}
        enter, exit_side = ('BUY', 'SELL') if side == 'LONG' else ('SELL', 'BUY')
        if plan['client_ids'] != ids or plan['protective_side'] != exit_side: reject()
        entry = dict(api_family='USD-M_FUTURES', intended_path='/fapi/v1/order', payload=dict(symbol=symbol, side=enter, positionSide='BOTH', type='LIMIT', timeInForce='GTC', quantity=plan['execution_quantity'], price=plan['entry'], newClientOrderId=ids['ENTRY']))
        if plan['entry_order'] != entry: reject()
        future = dict(entry=dict(intended_get_path='/fapi/v1/order', lookup=dict(symbol=symbol, origClientOrderId=ids['ENTRY'])), implemented=False)
        for field, leg, kind, price in (('take_profit', 'TP', 'TAKE_PROFIT_MARKET', plan['TP']), ('stop_loss', 'SL', 'STOP_MARKET', plan['SL'])):
            expected_order = dict(api_family='USD-M_ALGO', intended_path='/fapi/v1/algoOrder', activation='AFTER_CONFIRMED_ENTRY_FILL', payload=dict(algoType='CONDITIONAL', symbol=symbol, side=exit_side, positionSide='BOTH', type=kind, triggerPrice=price, workingType='MARK_PRICE', closePosition='true', clientAlgoId=ids[leg]))
            if plan[field + '_order'] != expected_order: reject()
            future[field] = dict(intended_get_path='/fapi/v1/algoOrder', lookup=dict(clientAlgoId=ids[leg]))
        if plan['future_reconciliation'] != future or plan['future_reconciliation']['implemented'] is not False: reject()
        policy = dict(uncertain_entry='RECONCILE_SAME_CLIENT_ID_NO_BLIND_RETRY', partial_fill='PROTECTION_INCOMPLETE_RECONCILE_AND_PROTECT_FILLED_EXPOSURE', protection='REQUIRE_BOTH_ACKNOWLEDGED_LEGS', incomplete='BLOCK_NEXT_SETUP', after_exit='RECONCILE_FLAT_AND_CLEAR_SIBLING_BEFORE_NEW_SETUP')
        if plan['failure_policy'] != policy or plan['future_states'] != ['PLAN_READY', 'ENTRY_SUBMITTED', 'ENTRY_CONFIRMED', 'PROTECTION_SUBMITTED', 'POSITION_PROTECTED']: reject()
        if 'failure_code' in plan and (not isinstance(plan['failure_code'], str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', plan['failure_code'])): reject()
        if 'required_account_mutations' in plan and (not isinstance(plan['required_account_mutations'], list) or len(plan['required_account_mutations']) > 2 or any(value not in ('SET_MARGIN_TYPE_CROSS', 'SET_LEVERAGE_75') for value in plan['required_account_mutations'])): reject()
        contains_operation(plan, None)
        result.append((row, plan, model))
    return result


class PreOrderArchive:
    def __init__(self, parent):
        self.parent, self.fd, self.db = parent, None, None
        try:
            self.fd = checked_file(parent, PRE_ORDER_ARCHIVE)
            info = os.fstat(self.fd)
            if stat.S_IMODE(info.st_mode) & 0o077: raise Refuse('PRE_ORDER_ARCHIVE_NOT_PRIVATE')
            if not 100 <= info.st_size <= MAX_PRE_ORDER_ARCHIVE_BYTES: raise Refuse('PRE_ORDER_ARCHIVE_SIZE_UNSUPPORTED')
            self.signature = (identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            self.digest = self.file_hash()
            self.scope = 'pre_order_archive:' + self.digest
            header = os.pread(self.fd, 100, 0)
            page_size = int.from_bytes(header[16:18], 'big')
            if page_size == 1: page_size = 65536
            if (header[:16] != b'SQLite format 3\x00' or page_size not in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)
                    or int.from_bytes(header[28:32], 'big') * page_size != info.st_size
                    or header[24:28] != header[92:96]):
                raise Refuse('PRE_ORDER_ARCHIVE_NOT_SELF_CONTAINED')
            self.validate_stable()
            self.db = sqlite3.connect('file:/proc/self/fd/' + str(self.fd) + '?mode=ro&immutable=1', uri=True)
            self.db.execute('PRAGMA query_only=ON')
            self.db.execute('PRAGMA trusted_schema=OFF')
            if self.db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]: raise Refuse('PRE_ORDER_ARCHIVE_INVALID')
            names = tables(self.db)
            if not {'trades', 'events', 'api_requests'}.issubset(names) or not names.issubset(PRE_ORDER_TABLES):
                raise Refuse('PRE_ORDER_ARCHIVE_SCHEMA_UNSUPPORTED')
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') LIMIT 1").fetchone():
                raise Refuse('PRE_ORDER_ARCHIVE_SCHEMA_UNSUPPORTED')
            for name in names:
                if self.db.execute('PRAGMA foreign_key_list(' + quoted(name) + ')').fetchone():
                    raise Refuse('PRE_ORDER_ARCHIVE_SCHEMA_UNSUPPORTED')
            expected = sqlite3.connect(':memory:')
            try:
                expected.executescript(PRE_ORDER_CORE_SQL)
                for name in ('trades', 'events'):
                    if table_schema(self.db, name, normalize_core=True) != table_schema(expected, name, normalize_core=True):
                        raise Refuse('PRE_ORDER_ARCHIVE_SCHEMA_UNSUPPORTED')
            finally: expected.close()
            self.shadow_rows = validated_shadow_rows(self.db)
            for name in PRE_ORDER_EMPTY_TABLES:
                if name == 'shadow_plans': continue # The historical rows were checked above.
                if name in names and self.db.execute('SELECT 1 FROM ' + quoted(name) + ' LIMIT 1').fetchone():
                    raise Refuse('PRE_ORDER_ARCHIVE_ORDER_EVIDENCE_PRESENT')
            self.columns, self.rows = rowset(self.db)
            index = self.columns.index('operation')
            if any(isinstance(row[index], str) and re.search(r'(?:robot|analysis)-v(?:[89]|[1-9][0-9]+)', row[index]) for row in self.rows):
                raise Refuse('PRE_ORDER_ARCHIVE_SCHEMA_UNSUPPORTED')
            self.api_schema = table_schema(self.db, 'api_requests')
            self.validate_stable()
        except BaseException as error:
            self.close()
            if isinstance(error, (sqlite3.Error, OSError)):
                raise Refuse('PRE_ORDER_ARCHIVE_INVALID') from None
            raise

    def file_hash(self):
        digest = hashlib.sha256()
        offset = 0
        size = self.signature[1]
        while offset < size:
            data = os.pread(self.fd, min(65536, size - offset), offset)
            if not data: raise Refuse('PRE_ORDER_ARCHIVE_CHANGED')
            digest.update(data)
            offset += len(data)
        return digest.hexdigest()

    def validate_stable(self):
        for suffix in ('-wal', '-shm', '-journal'):
            try: os.stat(PRE_ORDER_ARCHIVE + suffix, dir_fd=self.parent, follow_symlinks=False)
            except FileNotFoundError: continue
            raise Refuse('PRE_ORDER_ARCHIVE_SIDECAR_PRESENT')
        try: info = os.stat(PRE_ORDER_ARCHIVE, dir_fd=self.parent, follow_symlinks=False)
        except OSError: raise Refuse('PRE_ORDER_ARCHIVE_CHANGED') from None
        signature = (identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        held = os.fstat(self.fd)
        if signature != self.signature or signature != (identity(held), held.st_size, held.st_mtime_ns, held.st_ctime_ns) or self.file_hash() != self.digest:
            raise Refuse('PRE_ORDER_ARCHIVE_CHANGED')

    def match(self, db, columns, row):
        if not screening_rejection(columns, row): raise Refuse('PRE_ORDER_CANDIDATE_UNSUPPORTED')
        if columns != self.columns or table_schema(db, 'api_requests') != self.api_schema:
            raise Refuse('PRE_ORDER_API_SCHEMA_MISMATCH')
        operation = row[columns.index('operation')]
        archived = self.db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone()
        if not archived: raise Refuse('PRE_ORDER_REQUEST_NOT_IN_ARCHIVE')
        if encoded(columns, row) != encoded(self.columns, archived):
            raise Refuse('PRE_ORDER_REQUEST_ROW_MISMATCH')
        no_operation_references(self.db, operation)
        no_operation_references(db, operation)
        for stored, plan, model in self.shadow_rows + validated_shadow_rows(db):
            if any(value == operation for value in stored) or contains_operation(plan, operation) or contains_operation(model, operation):
                raise Refuse('PRE_ORDER_REQUEST_REFERENCED')
        return checksum(encoded(columns, row))

    def close(self):
        if self.db is not None: self.db.close(); self.db = None
        if self.fd is not None: os.close(self.fd); self.fd = None


def observe(db, proof=None, prove_pre_order=False):
    names = tables(db)
    columns, rows = rowset(db)
    index = {name: offset for offset, name in enumerate(columns)}
    selected = []
    states, scopes, days, failures = Counter(), Counter(), Counter(), Counter()
    reasons = set()
    row = db.execute('SELECT enabled FROM robot_settings WHERE id=1').fetchone() if 'robot_settings' in names else None
    off = row == (0,)
    if not off: reasons.add('ROBOT_OFF_REQUIRED')
    for table in ('order_intents', 'robot_entry_receipts'):
        if table not in names: reasons.add('ORDER_EVIDENCE_SCHEMA_UNAVAILABLE')
        elif db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone(): reasons.add('ORDER_EVIDENCE_PRESENT')
    if 'robot_jobs' not in names or 'robot_cycles' not in names or 'robot_candidates' not in names:
        reasons.add('ROBOT_JOURNAL_SCHEMA_UNAVAILABLE')
    else:
        if db.execute("SELECT 1 FROM robot_jobs WHERE state IN ('PENDING','NEEDS_REVIEW') LIMIT 1").fetchone(): reasons.add('ROBOT_JOB_UNRESOLVED')
        if db.execute("SELECT 1 FROM robot_cycles WHERE state IN ('PENDING','NEEDS_REVIEW') LIMIT 1").fetchone(): reasons.add('ROBOT_CYCLE_UNRESOLVED')
    archive_rows = {}
    if ARCHIVE in names:
        if tuple(row[1] for row in db.execute('PRAGMA table_info(' + ARCHIVE + ')')) != ARCHIVE_COLUMNS:
            reasons.add('RETIREMENT_ARCHIVE_SCHEMA_UNSUPPORTED')
        else:
            archive_rows = {row[0]: row for row in db.execute('SELECT operation,original_row,row_sha256,scope,retired_at FROM ' + ARCHIVE)}
    if unsafe_schema(db, names): reasons.add('UNSAFE_RETIREMENT_SCHEMA')
    opaque = None
    proof_hashes = []
    if prove_pre_order:
        unknown = [row for row in rows if row[index['state']] == 'NEEDS_REVIEW' and not classify(row[index['operation']])]
        markers = [row for row in rows if row[index['state']] == 'RETIRED_LEGACY'
            and row[index['operation']] in archive_rows and isinstance(archive_rows[row[index['operation']]][3], str)
            and PRE_ORDER_SCOPE.fullmatch(archive_rows[row[index['operation']]][3])]
        if len(unknown) != 1 and not (not unknown and markers): raise Refuse('PRE_ORDER_CANDIDATE_REQUIRED')
        if unknown:
            if proof is None: raise Refuse('PRE_ORDER_ARCHIVE_REQUIRED')
            opaque = unknown[0][index['operation']]
            proof_hashes.append(proof.match(db, columns, unknown[0]))
    for row in rows:
        operation, state = row[index['operation']], row[index['state']]
        recognized = classify(operation)
        states[state if state in ('PENDING', 'NEEDS_REVIEW', 'COMPLETE', 'RETIRED_LEGACY') else 'OTHER'] += 1
        if state == 'PENDING': reasons.add('PENDING_REQUEST_PRESENT')
        if state not in ('PENDING', 'NEEDS_REVIEW', 'COMPLETE', 'RETIRED_LEGACY'): reasons.add('UNSUPPORTED_REQUEST_STATE')
        if state == 'NEEDS_REVIEW':
            if recognized:
                selected.append((operation, recognized[0], tuple(row)))
                scopes[recognized[0]] += 1
                days[recognized[1]] += 1
                failure = row[index['failure_code']] if 'failure_code' in index else None
                failures[failure if failure in PUBLIC_FAILURES else 'UNAVAILABLE' if failure is None else 'REDACTED'] += 1
            elif proof is not None and operation == opaque:
                selected.append((operation, proof.scope, tuple(row)))
                scopes['pre_order_archive'] += 1
            else: reasons.add('CURRENT_OR_UNKNOWN_REQUEST_UNRESOLVED')
        if state == 'RETIRED_LEGACY':
            original = list(row)
            original[index['state']] = 'NEEDS_REVIEW'
            payload = encoded(columns, original)
            archived = archive_rows.get(operation)
            archive_scope = PRE_ORDER_SCOPE.fullmatch(archived[3]) if archived and isinstance(archived[3], str) else None
            if archive_scope:
                if proof is None or archive_scope[1] != proof.digest:
                    reasons.add('RETIREMENT_MARKER_INVALID')
                else:
                    proof_hashes.append(proof.match(db, columns, original))
            elif not recognized or not archived or archived[3] != recognized[0]:
                reasons.add('RETIREMENT_MARKER_INVALID')
            if not archived or archived[1] != payload or archived[2] != checksum(payload): reasons.add('RETIREMENT_MARKER_INVALID')
    for operation, scope, source in selected:
        if operation in archive_rows: reasons.add('RETIREMENT_MARKER_CONFLICT')
        recognized = classify(operation)
        if not recognized: continue # Proven opaque row already passed both reference checks.
        cycle = recognized[2]
        for table, condition, arguments in (
            ('robot_jobs', 'operation=? OR cycle=?', (operation, cycle)),
            ('robot_candidates', 'id=? OR cycle=?', (operation, cycle)),
            ('robot_cycles', 'id=?', (cycle,)),
            ('analysis_checks', 'operation=?', (operation,)),
            ('cycles', 'day=?', (cycle,)),
        ):
            if table in names and db.execute('SELECT 1 FROM ' + table + ' WHERE ' + condition + ' LIMIT 1', arguments).fetchone():
                reasons.add('LEGACY_REQUEST_REFERENCED')
    fingerprint = checksum(b'\n'.join(encoded(columns, row) for row in rows))
    report = dict(status='INSPECTION_ONLY', eligible_count=len(selected),
        request_states=dict(states), eligible_scopes=dict(scopes), eligible_dates=dict(days),
        failure_codes=dict(failures), journal_sha256=fingerprint, robot_off=off,
        can_apply=not reasons and bool(selected), refusal_reasons=sorted(reasons))
    report.update(unresolved_details(columns, rows))
    if proof is not None:
        report['pre_order_proof'] = dict(status='VERIFIED', matched_count=len(proof_hashes),
            archive_sha256=proof.digest, api_schema_sha256=checksum(proof.api_schema), row_sha256=sorted(proof_hashes))
    return dict(columns=columns, selected=selected, report=report, fingerprint=fingerprint)


def create_backup(source, directory_fd, path, observation, proof=None, prove_pre_order=False):
    try: os.mkdir('legacy-research-backups', 0o700, dir_fd=directory_fd)
    except FileExistsError: pass
    destination = os.open('legacy-research-backups', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
    fd = None
    output = None
    try:
        info = os.fstat(destination)
        if info.st_uid not in (0, os.geteuid()) or stat.S_IMODE(info.st_mode) != 0o700:
            raise Refuse('UNSAFE_BACKUP_DIRECTORY')
        name = 'before-retirement-' + uuid.uuid4().hex + '.sqlite3'
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=destination)
        os.fchmod(fd, 0o600)
        output = sqlite3.connect('file:/proc/self/fd/' + str(fd) + '?mode=rw', uri=True)
        source.backup(output)
        if output.execute('PRAGMA integrity_check').fetchall() != [('ok',)]: raise Refuse('BACKUP_VERIFICATION_FAILED')
        copied = observe(output, proof, prove_pre_order)
        if copied['fingerprint'] != observation['fingerprint'] or copied['report'] != observation['report']:
            raise Refuse('BACKUP_VERIFICATION_FAILED')
        output.close()
        output = None
        os.fsync(fd)
        os.fsync(destination)
        os.fsync(directory_fd)
        return path / 'legacy-research-backups' / name
    finally:
        if output is not None: output.close()
        if fd is not None: os.close(fd)
        os.close(destination)


def apply_rows(db, observation):
    db.execute('''CREATE TABLE IF NOT EXISTS legacy_research_archive(
        operation TEXT PRIMARY KEY,original_row BLOB NOT NULL,row_sha256 TEXT NOT NULL,
        scope TEXT NOT NULL,retired_at TEXT NOT NULL)''')
    state_index = observation['columns'].index('state')
    retired_at = datetime.now(timezone.utc).isoformat()
    for operation, scope, row in observation['selected']:
        payload = encoded(observation['columns'], row)
        db.execute('INSERT INTO legacy_research_archive VALUES(?,?,?,?,?)',
            (operation, payload, checksum(payload), scope, retired_at))
        changed = db.execute("UPDATE api_requests SET state='RETIRED_LEGACY' WHERE operation=? AND state='NEEDS_REVIEW'", (operation,))
        if changed.rowcount != 1: raise Refuse('REQUEST_JOURNAL_CHANGED')
        after = db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone()
        expected = list(row)
        expected[state_index] = 'RETIRED_LEGACY'
        if encoded(observation['columns'], after) != encoded(observation['columns'], expected):
            raise Refuse('REQUEST_JOURNAL_CHANGED')


def run(data_dir, apply=False, prove_pre_order=False):
    root, root_fd = directory(data_dir)
    parent = None
    source = None
    writer = None
    proof = None
    files = {}
    try:
        parent = os.open('trading', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        info = os.fstat(parent)
        if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022: raise Refuse('UNSAFE_STATE_DIRECTORY')
        path = root / 'trading'
        for name in ('ledger.sqlite3', 'ledger.sqlite3-wal', 'ledger.sqlite3-shm', 'ledger.sqlite3-journal', 'cycle.lock'):
            files[name] = checked_file(parent, name, missing=name != 'ledger.sqlite3')
        validate_directories(root, root_fd, parent)
        if files['ledger.sqlite3-journal'] is not None: raise Refuse('LEDGER_JOURNAL_PRESENT')
        header = os.pread(files['ledger.sqlite3'], 100, 0)
        if header[:16] != b'SQLite format 3\x00' or len(header) != 100: raise Refuse('INVALID_SQLITE_LEDGER')
        if (header[18] == 2 or files['ledger.sqlite3-wal'] is not None) and (files['ledger.sqlite3-wal'] is None or files['ledger.sqlite3-shm'] is None):
            raise Refuse('WAL_STATE_UNAVAILABLE')
        source = connect(path / 'ledger.sqlite3')
        source.execute('BEGIN')
        rowset(source) # Preserve baseline schema errors before optional marker lookup.
        archive_columns = tuple(row[1] for row in source.execute('PRAGMA table_info(' + ARCHIVE + ')')) if ARCHIVE in tables(source) else ()
        has_marker = archive_columns == ARCHIVE_COLUMNS and source.execute(
            'SELECT 1 FROM ' + ARCHIVE + " a JOIN api_requests r ON r.operation=a.operation WHERE r.state='RETIRED_LEGACY' AND a.scope LIKE ? LIMIT 1",
            ('pre_order_archive:%',)).fetchone()
        if prove_pre_order or has_marker: proof = PreOrderArchive(parent)
        observation = observe(source, proof, prove_pre_order)
        validate_directories(root, root_fd, parent)
        validate_files(parent, files)
        if proof is not None: proof.validate_stable()
        if not apply: return observation['report']
        if observation['report']['refusal_reasons']: raise Refuse(observation['report']['refusal_reasons'][0])
        if not observation['selected']: return dict(status='UNCHANGED', retired_count=0)
        if inside_checkout(root):
            raise Refuse('BACKUP_INSIDE_CHECKOUT')
        lock = files['cycle.lock']
        if lock is None: raise Refuse('CYCLE_LOCK_MISSING')
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise Refuse('CYCLE_LOCK_BUSY') from None
        validate_files(parent, files)
        # Re-read under the cycle lock before creating the private backup.
        source.execute('ROLLBACK')
        source.execute('BEGIN')
        locked = observe(source, proof, prove_pre_order)
        if locked != observation: raise Refuse('REQUEST_JOURNAL_CHANGED')
        validate_directories(root, root_fd, parent)
        if proof is not None: proof.validate_stable()
        saved = create_backup(source, parent, path, observation, proof, prove_pre_order) if proof is not None else create_backup(source, parent, path, observation)
        source.close()
        source = None
        validate_directories(root, root_fd, parent)
        validate_files(parent, files)
        if proof is not None: proof.validate_stable()
        writer = connect(path / 'ledger.sqlite3', readonly=False)
        writer.execute('BEGIN IMMEDIATE')
        try:
            validate_directories(root, root_fd, parent)
            validate_files(parent, files, writing=True)
            current = observe(writer, proof, prove_pre_order)
            if current != observation: raise Refuse('REQUEST_JOURNAL_CHANGED')
            apply_rows(writer, current)
            validate_directories(root, root_fd, parent)
            validate_files(parent, files, writing=True)
            if proof is not None: proof.validate_stable()
            writer.execute('COMMIT')
        except BaseException:
            writer.execute('ROLLBACK')
            raise
        return dict(status='RETIRED_LEGACY', retired_count=len(observation['selected']),
            backup=str(saved), retired_scopes=observation['report']['eligible_scopes'])
    finally:
        if writer is not None: writer.close()
        if source is not None: source.close()
        if proof is not None: proof.close()
        for fd in files.values():
            if fd is not None: os.close(fd)
        if parent is not None: os.close(parent)
        os.close(root_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('/data'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--prove-pre-order-screening-rejection', action='store_true',
        help='Require matching pre-order snapshot proof for one opaque screening rejection; never replay it.')
    args = parser.parse_args(argv)
    try: result = run(args.data_dir, args.apply, args.prove_pre_order_screening_rejection)
    except Refuse as error:
        print(json.dumps({'error': str(error)}, sort_keys=True))
        return 1
    except Exception:
        print('{"error":"LEGACY_RETIREMENT_FAILED"}')
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
