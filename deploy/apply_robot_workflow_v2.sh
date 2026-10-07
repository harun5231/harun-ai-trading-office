#!/usr/bin/env bash
# OFF-only source deployment. Existing financial/research rows and orders stay intact.
(
set -Eeuo pipefail
umask 077
cd /root/harun-ai-trading-office
HARUN_STAGE=PREFLIGHT
HARUN_RESTORE_READY=0
HARUN_WORKER_STOPPED=0
HARUN_CONFIG="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' harun-office-worker-1)"
[ "$HARUN_CONFIG" = /root/harun-ai-trading-office/compose.yaml ] || { printf '%s\n' COMPOSE_CONFIGURATION_CHANGED; exit 1; }
HARUN_BACKUP="$(mktemp -d /root/harun-workflow-v2.XXXXXX)"
HARUN_BUILD_MAP="$(python3 -I -B -S - <<'HARUN_V2_BUILD_MAP'
import hashlib,json,stat
from pathlib import Path
values={}
for name in ('Dockerfile','.dockerignore','compose.yaml','worker/requirements.txt'):
    p=Path(name);info=p.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_nlink==1, 'BUILD_CONFIGURATION_INVALID'
    values[name]=hashlib.sha256(p.read_bytes()).hexdigest()
print(json.dumps(values,sort_keys=True))
HARUN_V2_BUILD_MAP
)"
harun_check_build_configuration() {
  python3 -I -B -S - "$HARUN_BUILD_MAP" <<'HARUN_V2_BUILD_CHECK'
import hashlib,json,stat,sys
from pathlib import Path
for name,sha in json.loads(sys.argv[1]).items():
    p=Path(name);info=p.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_nlink==1 and hashlib.sha256(p.read_bytes()).hexdigest()==sha, 'BUILD_CONFIGURATION_CHANGED'
HARUN_V2_BUILD_CHECK
}
cat > "$HARUN_BACKUP/audit.py" <<'HARUN_OFF_AUDIT'
"""OFF-only, read-only journal and exchange verification for one source update."""
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
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

UID = 10001
CACHE_TABLES = frozenset(('robot_status', 'office_cache', 'office_activity'))

class Refuse(Exception):
    pass

def require(value, reason):
    if not value:
        raise Refuse(reason)

def private_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                info.st_uid == os.geteuid() == UID and not info.st_mode & 0o077,
                'JOURNAL_FILE_INVALID')
        return fd, (info.st_dev, info.st_ino)
    except BaseException:
        os.close(fd)
        raise

def directories(path):
    for part in (path, *path.parents):
        require(stat.S_ISDIR(part.lstat().st_mode), 'JOURNAL_DIRECTORY_INVALID')

def pinned(path, identity):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
            (info.st_dev, info.st_ino) == identity, 'JOURNAL_PATH_CHANGED')

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def typed(value):
    if value is None:
        return ['null']
    if type(value) is int:
        return ['int', str(value)]
    if type(value) is float:
        return ['float', value.hex()]
    if isinstance(value, bytes):
        return ['bytes', value.hex()]
    if isinstance(value, str):
        return ['str', value]
    raise Refuse('JOURNAL_VALUE_INVALID')

def snapshot(db):
    """Exclude only display caches; retain every existing financial/research row."""
    require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'JOURNAL_INVALID')
    require(not db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone(),
            'JOURNAL_TRIGGER_PRESENT')
    require(db.execute('SELECT enabled FROM robot_settings WHERE id=1').fetchall() == [(0,)],
            'ROBOT_OFF_REQUIRED')
    require(db.execute('SELECT version FROM office_schema').fetchall() == [(2,)],
            'JOURNAL_MIGRATION_REQUIRED')
    require(not db.execute("SELECT 1 FROM order_intents WHERE state IN ('SUBMITTING','NEEDS_REVIEW') LIMIT 1").fetchone(),
            'UNRESOLVED_ORDER')
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    require({'robot_settings', 'order_intents', 'api_requests', 'robot_cycles',
             'robot_jobs', 'robot_candidates', 'robot_entry_receipts'} <= set(tables),
            'JOURNAL_SCHEMA_INVALID')
    records = []
    counts = {}
    for name in tables:
        require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,99}', name), 'JOURNAL_SCHEMA_INVALID')
        if name in CACHE_TABLES:
            continue
        rows = sorted(json.dumps([typed(v) for v in row], separators=(',', ':'))
                      for row in db.execute('SELECT * FROM "' + name + '"'))
        schema = sorted(tuple(row) for row in db.execute(
            'SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? ORDER BY type,name', (name,)))
        records.append([name, schema, rows])
        counts[name] = len(rows)
    for table in ('api_requests', 'robot_jobs'):
        require(not db.execute('SELECT 1 FROM "' + table + '" WHERE state IN (?,?) LIMIT 1', ('PENDING','NEEDS_REVIEW')).fetchone(),
                'UNRESOLVED_PROVIDER_REQUEST')
    settings = db.execute('SELECT enabled,risk FROM robot_settings WHERE id=1').fetchone()
    require(isinstance(settings[1], str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', settings[1]) and
            Decimal('0') < Decimal(settings[1]) <= Decimal('100'), 'RISK_SETTING_INVALID')
    states = dict(db.execute('SELECT state,count(*) FROM order_intents GROUP BY state'))
    require(all(re.fullmatch(r'[A-Z_]{1,60}', k) for k in states), 'JOURNAL_STATE_INVALID')
    return dict(journal_sha256=digest(records), table_counts=counts,
                robot_on=False, risk_target_usdt=settings[1], order_states=states)

def verify_sources(expected, root):
    root = Path(root).absolute()
    directories(root)
    paths = list((root / 'worker').rglob('*.py')) + [root / 'deploy/container_boot.py', root / 'deploy/runtime_permissions.py']
    require(set(expected) == {str(p.relative_to(root)) for p in paths}, 'SOURCE_INVENTORY_CHANGED')
    for name, sha in expected.items():
        require(re.fullmatch(r'(?:worker|deploy)/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.py', name)
                and re.fullmatch(r'[a-f0-9]{64}', sha), 'SOURCE_MAP_INVALID')
        p = root / name
        directories(p.parent)
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size < 2_000_000,
                    'SOURCE_PATH_INVALID')
            with os.fdopen(os.dup(fd), 'rb') as stream:
                require(hashlib.sha256(stream.read(2_000_001)).hexdigest() == sha, 'SOURCE_CHANGED')
        finally:
            os.close(fd)

def clean_scalar(value, *, number=False):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', value),
            'EXCHANGE_OBSERVATION_INVALID')
    if number:
        require(Decimal(value).is_finite(), 'EXCHANGE_OBSERVATION_INVALID')
    return value

def exchange_snapshot(reader=None):
    # BinanceReadOnly has no configurable HTTP method/body and its transport
    # validates the fixed Futures origin and GET allowlist independently.
    if reader is None:
        from worker.binance_private import BinanceReadOnly
        reader = BinanceReadOnly()
    reader.sync_time()
    positions = reader.signed_get('/fapi/v3/positionRisk')
    orders = reader.signed_get('/fapi/v1/openOrders')
    algos = reader.signed_get('/fapi/v1/openAlgoOrders')
    require(all(isinstance(v, list) and len(v) <= 10_000 for v in (positions, orders, algos)),
            'EXCHANGE_OBSERVATION_INVALID')
    active = []
    for row in positions:
        require(isinstance(row, dict), 'EXCHANGE_OBSERVATION_INVALID')
        amount = clean_scalar(row.get('positionAmt'), number=True)
        if Decimal(amount) == 0:
            continue
        active.append(dict(symbol=clean_scalar(row.get('symbol')),
                           position_side=clean_scalar(row.get('positionSide')),
                           quantity=amount))
    def brief(rows, algo):
        result = []
        for row in rows:
            require(isinstance(row, dict), 'EXCHANGE_OBSERVATION_INVALID')
            identity = row.get('algoId' if algo else 'orderId')
            require(type(identity) is int and identity > 0 or isinstance(identity, str) and re.fullmatch(r'[1-9][0-9]{0,24}', identity),
                    'EXCHANGE_OBSERVATION_INVALID')
            result.append(dict(symbol=clean_scalar(row.get('symbol')),
                               id=str(identity), status=clean_scalar(row.get('algoStatus' if algo else 'status')),
                               side=clean_scalar(row.get('side')),
                               order_type=clean_scalar(row.get('orderType' if algo else 'type'))))
        return sorted(result, key=lambda v: (v['symbol'], v['id']))
    result = dict(active_positions=sorted(active, key=lambda v:(v['symbol'],v['position_side'])),
                  open_orders=brief(orders, False), open_algos=brief(algos, True))
    result['get_only'] = True
    return result

def fresh_off(db, cutoff):
    require(db.execute('SELECT enabled FROM robot_settings WHERE id=1').fetchall() == [(0,)],
            'ROBOT_OFF_REQUIRED')
    row = db.execute('SELECT data FROM robot_status WHERE id=1').fetchone()
    if not row:
        return False
    data = json.loads(row[0])
    value = data.get('checked_at')
    if not isinstance(value, str):
        return False
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return (stamp.tzinfo is not None and stamp.timestamp() >= cutoff and
            data.get('bot_status') == 'OFF' and data.get('wait_reason') == 'ROBOT_OFF' and
            data.get('failure_code') is None)

def inspect(data_dir='/data', *, expected=None, root='/app', before=None, backup=False,
            exchange=False, fresh_after=None):
    require(os.geteuid() == UID, 'WORKER_UID_REQUIRED')
    directory = Path(data_dir).absolute() / 'trading'
    directories(directory)
    ledger = directory / 'ledger.sqlite3'
    lockpath = directory / 'cycle.lock'
    ledger_fd, ledger_id = private_file(ledger)
    lock_fd = None
    db = None
    try:
        lock_fd, lock_id = private_file(lockpath)
        deadline = time.monotonic() + 45
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, 'CYCLE_LOCK_BUSY')
                time.sleep(0.1)
        pinned(ledger, ledger_id)
        pinned(lockpath, lock_id)
        if expected is not None:
            verify_sources(expected, root)
        db = sqlite3.connect('file:/proc/self/fd/' + str(ledger_fd) + '?mode=ro', uri=True, timeout=10)
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')
        proof = snapshot(db)
        if before is not None:
            require(proof['journal_sha256'] == before['journal_sha256'], 'JOURNAL_CHANGED_DURING_UPDATE')
        if fresh_after is not None:
            require(fresh_off(db, fresh_after), 'FRESH_OFF_STATUS_REQUIRED')
        db.execute('COMMIT')
        if backup:
            maintenance = directory / 'maintenance'
            if not maintenance.exists():
                maintenance.mkdir(mode=0o700)
            directories(maintenance)
            info = maintenance.lstat()
            require(info.st_uid == UID and not info.st_mode & 0o077, 'BACKUP_DIRECTORY_INVALID')
            saved = Path(tempfile.mkdtemp(prefix='workflow-v2-', dir=maintenance))
            target = saved / 'ledger.before.sqlite3'
            fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            out = sqlite3.connect(str(target))
            try:
                # Source has no write transaction. cycle.lock fences coordinator;
                # display-cache writers may continue and are excluded above.
                db.backup(out)
                require(snapshot(out)['journal_sha256'] == proof['journal_sha256'], 'BACKUP_NOT_VERIFIED')
            finally:
                out.close()
            proof['backup'] = str(target)
        if exchange:
            sys.path.insert(0, str(Path(root).absolute()))
            proof['exchange'] = exchange_snapshot()
        db.execute('BEGIN')
        require(snapshot(db)['journal_sha256'] == proof['journal_sha256'], 'JOURNAL_CHANGED_DURING_AUDIT')
        db.execute('COMMIT')
        pinned(ledger, ledger_id)
        pinned(lockpath, lock_id)
        proof['status'] = 'OFF_STATE_PRESERVED' if before else 'OFF_PREFLIGHT_VERIFIED'
        return proof
    finally:
        if db is not None:
            db.close()
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(ledger_fd)

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default='/data')
    parser.add_argument('--source-hashes-json', required=True)
    parser.add_argument('--source-root', default='/app')
    parser.add_argument('--before-json')
    parser.add_argument('--backup', action='store_true')
    parser.add_argument('--exchange', action='store_true')
    parser.add_argument('--fresh-after', type=float)
    args = parser.parse_args(argv)
    try:
        result = inspect(args.data_dir, expected=json.loads(args.source_hashes_json),
                         root=args.source_root,
                         before=json.loads(args.before_json) if args.before_json else None,
                         backup=args.backup, exchange=args.exchange, fresh_after=args.fresh_after)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        read_codes = {'BINANCE_NOT_CONFIGURED', 'BINANCE_AUTH_FAILED', 'BINANCE_IP_RESTRICTED',
                      'BINANCE_PERMISSION_DENIED', 'BINANCE_CLOCK_ERROR',
                      'BINANCE_ACCOUNT_UNAVAILABLE', 'BINANCE_ENDPOINT_DENIED'}
        reason = str(error) if isinstance(error, Refuse) or str(error) in read_codes else type(error).__name__
        print(json.dumps(dict(status='WORKFLOW_UPDATE_REFUSED', reason=reason), sort_keys=True))
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
HARUN_OFF_AUDIT
cat > "$HARUN_BACKUP/sdk_patch.py" <<'HARUN_SDK_V3_PATCH'
"""Source-only upgrade of the pinned private SDK to reserved-exit risk and OFF fences.

The caller owns backup/restart. No SDK import, credentials, exchange requests,
ledger changes or configurable input SHA are exposed by this utility.
"""
import ast
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

SOURCE_SHA = '4cd0be4f03659ef6eb75193f261465fb33519b0602d51a4a9c7f3865d0153049'
OLD_BUILD = 'def build_intent(plan, operation):\n    """Immutable internal order command, never a user ticket or exchange receipt."""\n    from .core import validated_protection_working_type\n    if not isinstance(plan, dict) or plan.get(\'mode\') != \'ORDER_INTENT\' or plan.get(\'symbol\') == \'HYPEUSDT\':\n        raise Review(\'INVALID_ORDER_CONTRACT\')\n    try:\n        target = validated_risk_target(plan[\'risk_target_usdt\'])\n        risk = number(plan[\'risk\'])\n        working_type = validated_protection_working_type(plan.get(\'protection_working_type\', \'MARK_PRICE\'))\n    except (KeyError, TypeError, Review): raise Review(\'INVALID_ORDER_CONTRACT\') from None\n    if plan.get(\'side\') not in (\'LONG\', \'SHORT\') or not D(\'0\') < risk <= target:\n        raise Review(\'INVALID_ORDER_CONTRACT\')\n    entry_side = \'BUY\' if plan[\'side\'] == \'LONG\' else \'SELL\'\n    client_id = \'hao-\' + hashlib.sha256(operation.encode()).hexdigest()[:28]\n    intent = dict(intent_id=operation, client_order_id=client_id,\n        symbol=plan[\'symbol\'], position_side=\'BOTH\', side=plan[\'side\'],\n        entry=dict(order_type=\'LIMIT\', side=entry_side, price=plan[\'entry\'],\n                   quantity=plan[\'execution_quantity\'], time_in_force=\'GTC\'),\n        protection=dict(exit_side=\'SELL\' if entry_side == \'BUY\' else \'BUY\',\n                        stop_loss=plan[\'sl\'], take_profit=plan[\'tp\'], working_type=working_type),\n        margin_mode=plan[\'margin_mode\'], leverage=plan[\'leverage\'],\n        risk_target_usdt=plan[\'risk_target_usdt\'], risk_usdt=plan[\'risk\'],\n        gross_risk_usdt=plan[\'gross_risk\'],entry_fee_usdt=plan[\'entry_fee_usdt\'],\n        sl_exit_fee_usdt=plan[\'sl_exit_fee_usdt\'],tp_exit_fee_usdt=plan[\'tp_exit_fee_usdt\'],\n        net_reward_usdt=plan[\'net_reward\'],net_reward_risk=plan[\'net_rr\'],\n        fee_evidence=dict(source=plan[\'fee_source\'],symbol=plan[\'fee_symbol\'],\n            observed_at=plan[\'fee_observed_at\'],taker_rate=plan[\'entry_fee_rate\']),\n        excluded_costs=plan[\'excluded_costs\'],\n        evidence_sha256=plan[\'provenance\'][\'payload_sha256\'])\n    if \'reward_risk_policy\' in plan:\n        from .core import Rules, validate_reward_risk_policy\n        from dataclasses import fields\n        try:\n            sizing = plan[\'sizing_rules\']\n            if not isinstance(sizing, dict) or set(sizing) != {field.name for field in fields(Rules)}:\n                raise ValueError\n            rules = Rules(**{key: (D(value) if isinstance(value, str) and\n                key not in (\'fee_source\', \'fee_symbol\') else value) for key, value in sizing.items()})\n            validate_reward_risk_policy(plan, rules)\n            intent[\'reward_risk_policy\'] = plan[\'reward_risk_policy\']\n            intent[\'tp_tick_size\'] = format(number(rules.tick), \'f\')\n        except Exception:\n            raise Review(\'INVALID_ORDER_CONTRACT\') from None\n    return intent\n'
NEW_BUILD = 'def build_intent(plan, operation):\n    """Immutable internal order command, never a user ticket or exchange receipt."""\n    from .core import validated_protection_working_type\n    if not isinstance(plan, dict) or plan.get(\'mode\') != \'ORDER_INTENT\' or plan.get(\'symbol\') == \'HYPEUSDT\':\n        raise Review(\'INVALID_ORDER_CONTRACT\')\n    try:\n        target = validated_risk_target(plan[\'risk_target_usdt\'])\n        risk = number(plan[\'risk\'])\n        working_type = validated_protection_working_type(plan.get(\'protection_working_type\', \'MARK_PRICE\'))\n    except (KeyError, TypeError, Review): raise Review(\'INVALID_ORDER_CONTRACT\') from None\n    if plan.get(\'side\') not in (\'LONG\', \'SHORT\') or not D(\'0\') < risk <= target:\n        raise Review(\'INVALID_ORDER_CONTRACT\')\n    entry_side = \'BUY\' if plan[\'side\'] == \'LONG\' else \'SELL\'\n    client_id = \'hao-\' + hashlib.sha256(operation.encode()).hexdigest()[:28]\n    intent = dict(intent_id=operation, client_order_id=client_id,\n        symbol=plan[\'symbol\'], position_side=\'BOTH\', side=plan[\'side\'],\n        entry=dict(order_type=\'LIMIT\', side=entry_side, price=plan[\'entry\'],\n                   quantity=plan[\'execution_quantity\'], time_in_force=\'GTC\'),\n        protection=dict(exit_side=\'SELL\' if entry_side == \'BUY\' else \'BUY\',\n                        stop_loss=plan[\'sl\'], take_profit=plan[\'tp\'], working_type=working_type),\n        margin_mode=plan[\'margin_mode\'], leverage=plan[\'leverage\'],\n        risk_target_usdt=plan[\'risk_target_usdt\'], risk_usdt=plan[\'risk\'],\n        gross_risk_usdt=plan[\'gross_risk\'],entry_fee_usdt=plan[\'entry_fee_usdt\'],\n        sl_exit_fee_usdt=plan[\'sl_exit_fee_usdt\'],tp_exit_fee_usdt=plan[\'tp_exit_fee_usdt\'],\n        net_reward_usdt=plan[\'net_reward\'],net_reward_risk=plan[\'net_rr\'],\n        fee_evidence=dict(source=plan[\'fee_source\'],symbol=plan[\'fee_symbol\'],\n            observed_at=plan[\'fee_observed_at\'],taker_rate=plan[\'entry_fee_rate\']),\n        excluded_costs=plan[\'excluded_costs\'],\n        evidence_sha256=plan[\'provenance\'][\'payload_sha256\'])\n    if \'reward_risk_policy\' in plan or \'risk_model\' in plan:\n        from .core import Rules, validate_reward_risk_policy\n        from dataclasses import fields\n        try:\n            sizing = plan[\'sizing_rules\']\n            if not isinstance(sizing, dict) or set(sizing) != {field.name for field in fields(Rules)}:\n                raise ValueError\n            rules = Rules(**{key: (D(value) if isinstance(value, str) and\n                key not in (\'fee_source\', \'fee_symbol\') else value) for key, value in sizing.items()})\n            if \'risk_model\' in plan:\n                from .core import validate_normalized_plan\n                validate_normalized_plan(plan, rules, check_fresh=False)\n            else:\n                validate_reward_risk_policy(plan, rules)\n            intent[\'reward_risk_policy\'] = plan[\'reward_risk_policy\']\n            intent[\'tp_tick_size\'] = format(number(rules.tick), \'f\')\n            if \'risk_model\' in plan:\n                from copy import deepcopy\n                intent[\'excluded_costs\'] = deepcopy(plan[\'excluded_costs\'])\n                for key in (\'risk_model\', \'exit_slippage_rate\', \'entry_slippage_rate\',\n                            \'sl_slippage_usdt\', \'tp_slippage_usdt\',\n                            \'sl_execution_price\', \'tp_execution_price\',\n                            \'cost_evidence\', \'tp_normalization\'):\n                    intent[key] = deepcopy(plan[key])\n        except Exception:\n            raise Review(\'INVALID_ORDER_CONTRACT\') from None\n    return intent\n'
OLD_METHODS = {'_wire': '    def _wire(self, method, path, query=\'\', signed=False):\n        """No redirects, proxy credentials, automatic retry, or raw-error output."""\n        import json\n        from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler\n        from urllib.error import HTTPError\n        class NoRedirect(HTTPRedirectHandler):\n            def redirect_request(self, req, fp, code, msg, headers, newurl):\n                return None\n        def unique(pairs):\n            result = {}\n            for key, value in pairs:\n                if key in result:\n                    raise ValueError()\n                result[key] = value\n            return result\n        if self.base_url != \'https://fapi.binance.com\':\n            raise Review(\'BINANCE_ORDER_ORIGIN_DENIED\')\n        timeout = min(15, self._time_left())\n        self._request_count = getattr(self, \'_request_count\', 0)+1\n        url = self.base_url + path\n        headers = {\'Accept\': \'application/json\', \'User-Agent\': \'harun-office/1.0\'}\n        if signed:\n            headers[\'X-MBX-APIKEY\'] = self.api_key\n        body = None\n        if method == \'POST\':\n            body = query.encode(\'ascii\')\n            headers[\'Content-Type\'] = \'application/x-www-form-urlencoded\'\n        elif query:\n            url += \'?\' + query\n        try:\n            request = Request(url, data=body, headers=headers, method=method)\n            opener = build_opener(ProxyHandler({}), NoRedirect())\n            try:\n                response = opener.open(request, timeout=timeout)\n            except HTTPError as error:\n                response = error\n            with response:\n                chunks = []\n                total = 0\n                read_chunk = getattr(response, \'read1\', response.read)\n                while True:\n                    self._time_left()\n                    chunk = read_chunk(min(65536, 2_000_001-total))\n                    if not chunk:\n                        break\n                    chunks.append(chunk); total += len(chunk)\n                    if total > 2_000_000:\n                        raise ValueError()\n                self._time_left()\n                raw = b\'\'.join(chunks)\n                data = json.loads(raw, parse_float=str, object_pairs_hook=unique)\n                code = data.get(\'code\') if isinstance(data, dict) else None\n                if response.code != 200 or (type(code) is int and code < 0):\n                    raise self._ExchangeError(code if type(code) is int else None)\n                if not isinstance(data, (dict, list)):\n                    raise ValueError()\n                return data\n        except self._ExchangeError:\n            raise\n        except Exception:\n            raise Review(\'BINANCE_ORDER_OUTCOME_UNKNOWN\') from None\n', '_request': "    def _request(self, method: str, path: str, params: dict):\n        from urllib.parse import urlencode\n        self._time_left()\n        allowed = {\n            'GET': {'/fapi/v1/accountConfig', '/fapi/v3/account', '/fapi/v3/positionRisk',\n                    '/fapi/v1/openOrders', '/fapi/v1/openAlgoOrders', '/fapi/v1/symbolConfig',\n                    '/fapi/v1/leverageBracket', '/fapi/v1/commissionRate', '/fapi/v1/order',\n                    '/fapi/v1/algoOrder', '/fapi/v1/allAlgoOrders', '/fapi/v1/userTrades'},\n            'POST': {'/fapi/v1/marginType', '/fapi/v1/leverage', '/fapi/v1/order', '/fapi/v1/algoOrder'},\n            'DELETE': {'/fapi/v1/order', '/fapi/v1/algoOrder'},\n        }\n        if not self._connected:\n            raise Review('BINANCE_ORDER_NOT_CONFIGURED')\n        if method not in allowed or path not in allowed[method] or not isinstance(params, dict):\n            raise Review('BINANCE_ORDER_ENDPOINT_DENIED')\n        if any(not isinstance(k, str) or not isinstance(v, (str, int)) or isinstance(v, bool)\n               for k, v in params.items()) or {'timestamp', 'signature', 'recvWindow'} & set(params):\n            raise Review('BINANCE_ORDER_PARAMETERS_INVALID')\n        values = dict(params, timestamp=self._get_server_time(), recvWindow=5000)\n        query = urlencode(sorted(values.items()))\n        return self._wire(method, path, query + '&signature=' + self._sign(query), True)\n", '_validate_intent': "    def _validate_intent(self, intent):\n        import re\n        from fractions import Fraction\n        try:\n            if not isinstance(intent, dict):\n                raise ValueError()\n            symbol, client = intent['symbol'], intent['client_order_id']\n            if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9]{2,18}USDT', symbol) or symbol == 'HYPEUSDT':\n                raise ValueError()\n            if not isinstance(client, str) or not re.fullmatch(r'hao-[a-f0-9]{28}', client):\n                raise ValueError()\n            operation = intent['intent_id']\n            if not isinstance(operation, str) or client != 'hao-' + hashlib.sha256(operation.encode()).hexdigest()[:28]:\n                raise ValueError()\n            if intent['position_side'] != 'BOTH' or intent['margin_mode'] != 'CROSS' or type(intent['leverage']) is not int or intent['leverage'] != 75:\n                raise ValueError()\n            if intent['side'] not in ('LONG', 'SHORT'):\n                raise ValueError()\n            entry, protection = intent['entry'], intent['protection']\n            side = 'BUY' if intent['side'] == 'LONG' else 'SELL'\n            if entry['side'] != side or entry['order_type'] != 'LIMIT' or entry['time_in_force'] != 'GTC':\n                raise ValueError()\n            if protection['exit_side'] != ('SELL' if side == 'BUY' else 'BUY') or protection['working_type'] not in ('MARK_PRICE', 'CONTRACT_PRICE'):\n                raise ValueError()\n            e, q, sl, tp = (Fraction(self._decimal(v, positive=True)) for v in\n                             (entry['price'], entry['quantity'], protection['stop_loss'], protection['take_profit']))\n            if not (sl < e < tp if side == 'BUY' else tp < e < sl):\n                raise ValueError()\n            fees = intent['fee_evidence']\n            rate = Fraction(self._decimal(fees['taker_rate']))\n            if fees['source'] != 'BINANCE_FUTURES_COMMISSION_RATE' or fees['symbol'] != symbol or not 0 <= rate < 1:\n                raise ValueError()\n            target = Fraction(validated_risk_target(intent['risk_target_usdt']))\n            gross = q * abs(e - sl)\n            entry_fee, sl_fee, tp_fee = q*e*rate, q*sl*rate, q*tp*rate\n            risk, reward = gross + entry_fee + sl_fee, q*abs(tp-e) - entry_fee - tp_fee\n            if ('reward_risk_policy' in intent) != ('tp_tick_size' in intent):\n                raise ValueError()\n            if 'reward_risk_policy' in intent:\n                if intent['reward_risk_policy'] != 'NET_1_TO_2_NEAREST_TICK':\n                    raise ValueError()\n                tick = Fraction(self._decimal(intent['tp_tick_size'], positive=True))\n                if reward < 2*risk:\n                    raise Review('NET_RISK_REWARD_BELOW_2')\n                loss = risk/q\n                tp_target = (e*(1+rate)+2*loss)/(1-rate) if side == 'BUY' else (e*(1-rate)-2*loss)/(1+rate)\n                steps = tp_target/tick\n                count = -(-steps.numerator//steps.denominator) if side == 'BUY' else steps.numerator//steps.denominator\n                if count*tick <= 0 or tp != count*tick:\n                    raise Review('NET_RISK_REWARD_NOT_TARGET_2')\n            expected = {'risk_usdt': risk, 'gross_risk_usdt': gross,\n                        'entry_fee_usdt': entry_fee, 'sl_exit_fee_usdt': sl_fee,\n                        'tp_exit_fee_usdt': tp_fee, 'net_reward_usdt': reward}\n            if not 0 < risk <= target or reward < 2*risk:\n                raise ValueError()\n            if any(Fraction(self._decimal(intent[key])) != val for key, val in expected.items()):\n                raise ValueError()\n            ratio = intent['net_reward_risk']\n            if not isinstance(ratio, str) or len(ratio) > 256 or not re.fullmatch(r'[0-9]+(?:\\.[0-9]+)?(?:E[+-]?[0-9]{1,3})?', ratio):\n                raise ValueError()\n            if not Decimal(ratio).is_finite() or Fraction(Decimal(ratio)) < 2 or intent['excluded_costs'] != ['SLIPPAGE', 'FUNDING', 'GAPS']:\n                raise ValueError()\n            if not re.fullmatch(r'[a-f0-9]{64}', intent['evidence_sha256']):\n                raise ValueError()\n        except Review as error:\n            if str(error) in ('NET_RISK_REWARD_BELOW_2', 'NET_RISK_REWARD_NOT_TARGET_2'):\n                raise\n            raise Review('BINANCE_ORDER_INTENT_INVALID') from None\n        except Exception:\n            raise Review('BINANCE_ORDER_INTENT_INVALID') from None\n"}
NEW_METHODS = {'_wire': '    def _wire(self, method, path, query=\'\', signed=False):\n        """No redirects, proxy credentials, automatic retry, or raw-error output."""\n        import json\n        from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler\n        from urllib.error import HTTPError\n        class NoRedirect(HTTPRedirectHandler):\n            def redirect_request(self, req, fp, code, msg, headers, newurl):\n                return None\n        def unique(pairs):\n            result = {}\n            for key, value in pairs:\n                if key in result:\n                    raise ValueError()\n                result[key] = value\n            return result\n        if self.base_url != \'https://fapi.binance.com\':\n            raise Review(\'BINANCE_ORDER_ORIGIN_DENIED\')\n        if method in (\'POST\', \'DELETE\'):\n            from .research_guard import gateway_mutations_allowed\n            if not gateway_mutations_allowed():\n                raise Review(\'BINANCE_ORDER_ROBOT_OFF\')\n        timeout = min(15, self._time_left())\n        self._request_count = getattr(self, \'_request_count\', 0)+1\n        url = self.base_url + path\n        headers = {\'Accept\': \'application/json\', \'User-Agent\': \'harun-office/1.0\'}\n        if signed:\n            headers[\'X-MBX-APIKEY\'] = self.api_key\n        body = None\n        if method == \'POST\':\n            body = query.encode(\'ascii\')\n            headers[\'Content-Type\'] = \'application/x-www-form-urlencoded\'\n        elif query:\n            url += \'?\' + query\n        try:\n            request = Request(url, data=body, headers=headers, method=method)\n            opener = build_opener(ProxyHandler({}), NoRedirect())\n            try:\n                if method in (\'POST\', \'DELETE\') and not gateway_mutations_allowed():\n                    raise Review(\'BINANCE_ORDER_ROBOT_OFF\')\n                response = opener.open(request, timeout=timeout)\n            except HTTPError as error:\n                response = error\n            with response:\n                chunks = []\n                total = 0\n                read_chunk = getattr(response, \'read1\', response.read)\n                while True:\n                    self._time_left()\n                    chunk = read_chunk(min(65536, 2_000_001-total))\n                    if not chunk:\n                        break\n                    chunks.append(chunk); total += len(chunk)\n                    if total > 2_000_000:\n                        raise ValueError()\n                self._time_left()\n                raw = b\'\'.join(chunks)\n                data = json.loads(raw, parse_float=str, object_pairs_hook=unique)\n                code = data.get(\'code\') if isinstance(data, dict) else None\n                if response.code != 200 or (type(code) is int and code < 0):\n                    raise self._ExchangeError(code if type(code) is int else None)\n                if not isinstance(data, (dict, list)):\n                    raise ValueError()\n                return data\n        except self._ExchangeError:\n            raise\n        except Review as error:\n            if str(error) == \'BINANCE_ORDER_ROBOT_OFF\':\n                raise\n            raise Review(\'BINANCE_ORDER_OUTCOME_UNKNOWN\') from None\n        except Exception:\n            raise Review(\'BINANCE_ORDER_OUTCOME_UNKNOWN\') from None\n', '_request': "    def _request(self, method: str, path: str, params: dict):\n        from urllib.parse import urlencode\n        self._time_left()\n        allowed = {\n            'GET': {'/fapi/v1/accountConfig', '/fapi/v3/account', '/fapi/v3/positionRisk',\n                    '/fapi/v1/openOrders', '/fapi/v1/openAlgoOrders', '/fapi/v1/symbolConfig',\n                    '/fapi/v1/leverageBracket', '/fapi/v1/commissionRate', '/fapi/v1/order',\n                    '/fapi/v1/algoOrder', '/fapi/v1/allAlgoOrders', '/fapi/v1/userTrades'},\n            'POST': {'/fapi/v1/marginType', '/fapi/v1/leverage', '/fapi/v1/order', '/fapi/v1/algoOrder'},\n            'DELETE': {'/fapi/v1/order', '/fapi/v1/algoOrder'},\n        }\n        if not self._connected:\n            raise Review('BINANCE_ORDER_NOT_CONFIGURED')\n        if method not in allowed or path not in allowed[method] or not isinstance(params, dict):\n            raise Review('BINANCE_ORDER_ENDPOINT_DENIED')\n        if any(not isinstance(k, str) or not isinstance(v, (str, int)) or isinstance(v, bool)\n               for k, v in params.items()) or {'timestamp', 'signature', 'recvWindow'} & set(params):\n            raise Review('BINANCE_ORDER_PARAMETERS_INVALID')\n        if method in ('POST', 'DELETE'):\n            from .research_guard import gateway_mutations_allowed\n            if not gateway_mutations_allowed():\n                raise Review('BINANCE_ORDER_ROBOT_OFF')\n        values = dict(params, timestamp=self._get_server_time(), recvWindow=5000)\n        query = urlencode(sorted(values.items()))\n        return self._wire(method, path, query + '&signature=' + self._sign(query), True)\n", '_validate_intent': "    def _validate_intent(self, intent):\n        import re\n        from fractions import Fraction\n        try:\n            if not isinstance(intent, dict):\n                raise ValueError()\n            symbol, client = intent['symbol'], intent['client_order_id']\n            if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9]{2,18}USDT', symbol) or symbol == 'HYPEUSDT':\n                raise ValueError()\n            if not isinstance(client, str) or not re.fullmatch(r'hao-[a-f0-9]{28}', client):\n                raise ValueError()\n            operation = intent['intent_id']\n            if not isinstance(operation, str) or client != 'hao-' + hashlib.sha256(operation.encode()).hexdigest()[:28]:\n                raise ValueError()\n            if intent['position_side'] != 'BOTH' or intent['margin_mode'] != 'CROSS' or type(intent['leverage']) is not int or intent['leverage'] != 75:\n                raise ValueError()\n            if intent['side'] not in ('LONG', 'SHORT'):\n                raise ValueError()\n            entry, protection = intent['entry'], intent['protection']\n            side = 'BUY' if intent['side'] == 'LONG' else 'SELL'\n            if entry['side'] != side or entry['order_type'] != 'LIMIT' or entry['time_in_force'] != 'GTC':\n                raise ValueError()\n            if protection['exit_side'] != ('SELL' if side == 'BUY' else 'BUY') or protection['working_type'] not in ('MARK_PRICE', 'CONTRACT_PRICE'):\n                raise ValueError()\n            e, q, sl, tp = (Fraction(self._decimal(v, positive=True)) for v in\n                             (entry['price'], entry['quantity'], protection['stop_loss'], protection['take_profit']))\n            if not (sl < e < tp if side == 'BUY' else tp < e < sl):\n                raise ValueError()\n            fees = intent['fee_evidence']\n            rate = Fraction(self._decimal(fees['taker_rate']))\n            if fees['source'] != 'BINANCE_FUTURES_COMMISSION_RATE' or fees['symbol'] != symbol or not 0 <= rate < 1:\n                raise ValueError()\n            target = Fraction(validated_risk_target(intent['risk_target_usdt']))\n            gross = q * abs(e - sl)\n            entry_fee, sl_fee, tp_fee = q*e*rate, q*sl*rate, q*tp*rate\n            risk, reward = gross + entry_fee + sl_fee, q*abs(tp-e) - entry_fee - tp_fee\n            v3 = 'risk_model' in intent\n            if v3:\n                costs = self._validate_v3_costs(intent, e, q, sl, tp, rate)\n                risk, reward = costs['risk_usdt'], costs['net_reward_usdt']\n                gross, entry_fee = costs['gross_risk_usdt'], costs['entry_fee_usdt']\n                sl_fee, tp_fee = costs['sl_exit_fee_usdt'], costs['tp_exit_fee_usdt']\n            elif any(key in intent for key in ('exit_slippage_rate', 'entry_slippage_rate',\n                     'sl_slippage_usdt', 'tp_slippage_usdt', 'sl_execution_price',\n                     'tp_execution_price', 'cost_evidence', 'tp_normalization')):\n                raise ValueError()\n            if ('reward_risk_policy' in intent) != ('tp_tick_size' in intent):\n                raise ValueError()\n            if 'reward_risk_policy' in intent and not v3:\n                if intent['reward_risk_policy'] != 'NET_1_TO_2_NEAREST_TICK':\n                    raise ValueError()\n                tick = Fraction(self._decimal(intent['tp_tick_size'], positive=True))\n                if reward < 2*risk:\n                    raise Review('NET_RISK_REWARD_BELOW_2')\n                loss = risk/q\n                tp_target = (e*(1+rate)+2*loss)/(1-rate) if side == 'BUY' else (e*(1-rate)-2*loss)/(1+rate)\n                steps = tp_target/tick\n                count = -(-steps.numerator//steps.denominator) if side == 'BUY' else steps.numerator//steps.denominator\n                if count*tick <= 0 or tp != count*tick:\n                    raise Review('NET_RISK_REWARD_NOT_TARGET_2')\n            expected = {'risk_usdt': risk, 'gross_risk_usdt': gross,\n                        'entry_fee_usdt': entry_fee, 'sl_exit_fee_usdt': sl_fee,\n                        'tp_exit_fee_usdt': tp_fee, 'net_reward_usdt': reward}\n            if not 0 < risk <= target or reward < 2*risk:\n                raise ValueError()\n            if any(Fraction(self._decimal(intent[key])) != val for key, val in expected.items()):\n                raise ValueError()\n            ratio = intent['net_reward_risk']\n            if not isinstance(ratio, str) or len(ratio) > 256 or not re.fullmatch(r'[0-9]+(?:\\.[0-9]+)?(?:E[+-]?[0-9]{1,3})?', ratio):\n                raise ValueError()\n            exclusions = ['FUNDING', 'GAPS_BEYOND_RESERVE', 'FEE_CHANGES_AFTER_OBSERVATION'] if v3 else ['SLIPPAGE', 'FUNDING', 'GAPS']\n            if not Decimal(ratio).is_finite() or Fraction(Decimal(ratio)) < 2 or intent['excluded_costs'] != exclusions:\n                raise ValueError()\n            if not re.fullmatch(r'[a-f0-9]{64}', intent['evidence_sha256']):\n                raise ValueError()\n        except Review as error:\n            if str(error) in ('NET_RISK_REWARD_BELOW_2', 'NET_RISK_REWARD_NOT_TARGET_2'):\n                raise\n            raise Review('BINANCE_ORDER_INTENT_INVALID') from None\n        except Exception:\n            raise Review('BINANCE_ORDER_INTENT_INVALID') from None\n", '_validate_v3_costs': "    def _validate_v3_costs(self, intent, e, q, sl, tp, rate):\n        from fractions import Fraction\n        from decimal import Context, ROUND_HALF_EVEN\n        import math\n        model = 'FEE_SLIPPAGE_RISK_V3'\n        policy = 'NET_1_TO_2_NORMALIZED_WITH_EXIT_RESERVE'\n        exclusions = ['FUNDING', 'GAPS_BEYOND_RESERVE', 'FEE_CHANGES_AFTER_OBSERVATION']\n        if (intent['risk_model'] != model or intent['reward_risk_policy'] != policy\n                or intent['exit_slippage_rate'] != '0.005' or intent['entry_slippage_rate'] != '0'\n                or intent['protection']['working_type'] != 'CONTRACT_PRICE'):\n            raise ValueError()\n        tick = Fraction(self._decimal(intent['tp_tick_size'], positive=True))\n        reserve = Fraction(5, 1000)\n        long = intent['side'] == 'LONG'\n        sl_execution = sl*(1-reserve if long else 1+reserve)\n        tp_execution = tp*(1-reserve if long else 1+reserve)\n        gross, slippage, tp_slippage = q*abs(e-sl), q*sl*reserve, q*tp*reserve\n        entry_fee, sl_fee, tp_fee = q*e*rate, q*sl_execution*rate, q*tp_execution*rate\n        risk = gross+slippage+entry_fee+sl_fee\n        reward = q*(tp_execution-e if long else e-tp_execution)-entry_fee-tp_fee\n        expected = dict(risk_usdt=risk, net_reward_usdt=reward, gross_risk_usdt=gross,\n                        entry_fee_usdt=entry_fee, sl_exit_fee_usdt=sl_fee,\n                        tp_exit_fee_usdt=tp_fee, sl_slippage_usdt=slippage,\n                        tp_slippage_usdt=tp_slippage, sl_execution_price=sl_execution,\n                        tp_execution_price=tp_execution)\n        if any(Fraction(self._decimal(intent[key])) != value for key, value in expected.items()):\n            raise ValueError()\n        normalization = intent['tp_normalization']\n        if not isinstance(normalization, dict) or set(normalization) != {\n                'version', 'source', 'model_entry', 'model_tp', 'model_sl', 'model_gross_rr',\n                'execution_tp', 'tick_size', 'risk_model', 'exit_slippage_rate', 'taker_fee_rate'}:\n            raise ValueError()\n        if (normalization['version'] != 'TP_NET_RR_NORMALIZATION_V1'\n                or normalization['source'] != 'WORKER_DERIVED' or normalization['risk_model'] != model\n                or normalization['exit_slippage_rate'] != '0.005'\n                or Fraction(self._decimal(normalization['taker_fee_rate'])) != rate\n                or Fraction(self._decimal(normalization['model_entry'], positive=True)) != e\n                or Fraction(self._decimal(normalization['model_sl'], positive=True)) != sl\n                or Fraction(self._decimal(normalization['execution_tp'], positive=True)) != tp\n                or Fraction(self._decimal(normalization['tick_size'], positive=True)) != tick):\n            raise ValueError()\n        original_tp = Fraction(self._decimal(normalization['model_tp'], positive=True))\n        if not (sl < e < original_tp if long else original_tp < e < sl):\n            raise ValueError()\n        model_rr = abs(original_tp-e)/abs(e-sl)\n        if model_rr < 2:\n            raise Review('NET_RISK_REWARD_BELOW_2')\n        def ratio_text(value):\n            return format(Context(prec=160, rounding=ROUND_HALF_EVEN).divide(\n                Decimal(value.numerator), Decimal(value.denominator)), 'f')\n        if normalization['model_gross_rr'] != ratio_text(model_rr) or intent['net_reward_risk'] != ratio_text(reward/risk):\n            raise ValueError()\n        evidence = intent['cost_evidence']\n        observed = intent['fee_evidence']['observed_at']\n        if type(observed) not in (int, float) or not math.isfinite(observed) or observed < 0:\n            raise ValueError()\n        expected_evidence = dict(version=model, fee_source='BINANCE_FUTURES_COMMISSION_RATE',\n            fee_symbol=intent['symbol'], fee_observed_at=observed,\n            taker_fee_rate=intent['fee_evidence']['taker_rate'], exit_slippage_rate='0.005',\n            entry_slippage_rate='0', reserve_source='CONFIGURED_ADVERSE_EXIT_RATE', excluded_costs=exclusions)\n        if not isinstance(evidence, dict) or evidence != expected_evidence:\n            raise ValueError()\n        if reward < 2*risk:\n            raise Review('NET_RISK_REWARD_BELOW_2')\n        loss = risk/q\n        raw_tp = ((e*(1+rate)+2*loss)/((1-reserve)*(1-rate)) if long else\n                  (e*(1-rate)-2*loss)/((1+reserve)*(1+rate)))\n        steps = raw_tp/tick\n        count = -(-steps.numerator//steps.denominator) if long else steps.numerator//steps.denominator\n        if count*tick <= 0 or tp != count*tick:\n            raise Review('NET_RISK_REWARD_NOT_TARGET_2')\n        return expected\n"}


def find_nodes(tree):
    builders = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'build_intent']
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'OrderGateway']
    if len(builders) != 1 or len(classes) != 1 or builders[0].decorator_list:
        raise ValueError('GATEWAY_BOUNDARIES_CHANGED')
    cls = classes[0]
    methods = {}
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in methods:
                raise ValueError('GATEWAY_METHOD_CHANGED')
            methods[node.name] = node
    if any(name not in methods or type(methods[name]) is not ast.FunctionDef or methods[name].decorator_list for name in OLD_METHODS):
        raise ValueError('GATEWAY_METHOD_CHANGED')
    return builders[0], cls, methods


def span(raw, node):
    lines = raw.splitlines(keepends=True)
    return sum(map(len, lines[:node.lineno-1])), sum(map(len, lines[:node.end_lineno]))


def normalized(source):
    return source.replace(b'\r\n', b'\n')


def transform(raw, expected_sha=SOURCE_SHA):
    if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError('GATEWAY_SOURCE_CHANGED')
    tree = ast.parse(raw)
    builder, cls, methods = find_nodes(tree)
    if '_validate_v3_costs' in methods:
        raise ValueError('GATEWAY_HELPER_ALREADY_PRESENT')
    replacements = []
    for node, old, new in [(builder, OLD_BUILD, NEW_BUILD)] + [
            (methods[name], old, NEW_METHODS[name] + ('\n' + NEW_METHODS['_validate_v3_costs'] if name == '_validate_intent' else ''))
            for name, old in OLD_METHODS.items()]:
        first, last = span(raw, node)
        original = raw[first:last]
        if normalized(original) != old.encode():
            raise ValueError('GATEWAY_AUDITED_SOURCE_CHANGED')
        newline = b'\r\n' if b'\r\n' in original else b'\n'
        replacements.append((first, last, new.encode().replace(b'\n', newline)))
    changed = raw
    for first, last, replacement in sorted(replacements, reverse=True):
        changed = changed[:first] + replacement + changed[last:]
    compile(changed, 'order_gateway.py', 'exec')
    after = ast.parse(changed)
    new_builder, new_cls, new_methods = find_nodes(after)
    first, last = span(changed, new_builder)
    if normalized(changed[first:last]) != NEW_BUILD.encode():
        raise ValueError('GATEWAY_TARGET_INVALID')
    for name, expected in NEW_METHODS.items():
        node = new_methods.get(name)
        if type(node) is not ast.FunctionDef:
            raise ValueError('GATEWAY_TARGET_INVALID')
        first, last = span(changed, node)
        if normalized(changed[first:last]) != expected.encode():
            raise ValueError('GATEWAY_TARGET_INVALID')
    # Every other AST node and all bytes outside the replacement spans survive.
    tree.body.remove(builder)
    after.body.remove(new_builder)
    for name in OLD_METHODS:
        cls.body.remove(methods[name])
    for name in NEW_METHODS:
        new_cls.body.remove(new_methods[name])
    if ast.dump(tree, include_attributes=False) != ast.dump(after, include_attributes=False):
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
    fd, name = tempfile.mkstemp(prefix='.gateway-risk-v3-', dir=path.parent)
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
        report = dict(error=str(error) if isinstance(error, ValueError) and str(error).isupper() else 'GATEWAY_PATCH_FAILED')
    print(json.dumps(report, sort_keys=True))
    raise SystemExit(1 if 'error' in report else 0)
HARUN_SDK_V3_PATCH
cat > "$HARUN_BACKUP/runtime.patch" <<'HARUN_RUNTIME_V2_PATCH'
diff --git a/worker/account_state.py b/worker/account_state.py
index e207690..7a393f2 100644
--- a/worker/account_state.py
+++ b/worker/account_state.py
@@ -11,14 +11,14 @@ from .prompts import SCREENING
 SCREENING_ONE='pilihkan 1 coin yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance'
 MANUAL_ONLY_SYMBOLS=frozenset(('HYPEUSDT',))
 MAX_REPLACEMENTS=3
-ACCOUNT_FIELDS=('running_positions','running_symbols','available_slots','manual_exposure',
+ACCOUNT_FIELDS=('running_positions','running_symbols','open_entry_symbols','available_slots','manual_exposure',
     'usdt_wallet_balance','usdt_available_balance','bot_entries_today')
 ACCOUNT_GENERATION=uuid4().hex
 ACCOUNT_REVISION=count(1)
 ACCOUNT_ORDER_FIELDS=('_account_generation','_account_revision')

 def empty_account():
-    return dict(running_positions=None,running_symbols=None,available_slots=None,manual_exposure=[],
+    return dict(running_positions=None,running_symbols=None,open_entry_symbols=None,available_slots=None,manual_exposure=[],
         usdt_wallet_balance=None,usdt_available_balance=None,account_checked_at=None,account_failure_code=None,
         _account_generation=None,_account_revision=None)

@@ -74,11 +74,35 @@ def slots(running,entries=0,*,pending=0,unrepresented=0):
     # calendar day's two-entry allowance even after their positions are closed.
     return max(0,min(2-running-unrepresented,2-entries-pending))

+def open_entry_symbols(rows,*,algo=False,allow_hedge=False):
+    """Validate global regular open orders; reducing exits reserve no new slot."""
+    if not isinstance(rows,list):raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
+    symbols=set()
+    for row in rows:
+        if not isinstance(row,dict) or type(row.get('reduceOnly')) is not bool:
+            raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
+        if row['reduceOnly']:continue
+        if type(row.get('closePosition')) is not bool:raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
+        if row['closePosition']:continue
+        symbol=row.get('symbol')
+        if (not isinstance(symbol,str) or not re.fullmatch(r'[A-Z0-9_]{2,30}',symbol)
+                or row.get('positionSide') not in (('BOTH','LONG','SHORT') if allow_hedge else ('BOTH',)) or row.get('side') not in ('BUY','SELL')
+                or row.get('algoStatus' if algo else 'status') not in (
+                    ('NEW','TRIGGERING','TRIGGERED') if algo else ('NEW','PARTIALLY_FILLED','PENDING_CANCEL'))):
+            raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
+        symbols.add(symbol)
+    return sorted(symbols)
+
 def account_state(client,store,today):
     config=client.check()
     if config.get('status')!='BINANCE_CONNECTED' or config.get('position_mode')!='ONE_WAY' or config.get('multi_assets_margin') is not False or config.get('can_trade') is not True:
         raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
-    client.sync_time();value=client.signed_get('/fapi/v3/account')
+    client.sync_time()
+    # Read potential entries first: a fill between these GETs appears in the
+    # final positions snapshot and the union still reserves that symbol once.
+    pending_symbols=sorted(set(open_entry_symbols(client.signed_get('/fapi/v1/openOrders')))|
+        set(open_entry_symbols(client.signed_get('/fapi/v1/openAlgoOrders'),algo=True)))
+    value=client.signed_get('/fapi/v3/account')
     rows=value.get('positions') if isinstance(value,dict) else None
     if not isinstance(rows,list):raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
     running=set()
@@ -96,7 +120,8 @@ def account_state(client,store,today):
     # Until the gateway supplies current ownership evidence, every existing
     # exposure is externally managed and must remain protected from robot use.
     return dict(running_positions=len(running),running_symbols=sorted(running),manual_exposure=sorted(running),
-        bot_entries_today=entries,available_slots=store.available_slots(len(running),sorted(running),today),
+        open_entry_symbols=pending_symbols,bot_entries_today=entries,
+        available_slots=store.available_slots(len(running),sorted(running),today,pending_symbols=pending_symbols),
         usdt_wallet_balance=config.get('usdt_wallet_balance'),
         usdt_available_balance=config.get('usdt_available_balance'),account_checked_at=now(),account_failure_code=None,
         **account_order())
diff --git a/worker/analysis.py b/worker/analysis.py
index f80e9a2..32c321a 100644
--- a/worker/analysis.py
+++ b/worker/analysis.py
@@ -3,11 +3,12 @@ import time
 import re
 from dataclasses import replace
 from datetime import datetime
-from .core import D,Review,number,RISK,validated_risk_target,validated_fee_rate,FEE_SOURCE,REWARD_RISK_POLICY
+from .core import (D,Review,number,RISK,validated_risk_target,validated_fee_rate,FEE_SOURCE,
+    RISK_MODEL,NORMALIZED_REWARD_RISK_POLICY,EXIT_SLIPPAGE_RATE,RESERVE_EXCLUDED_COSTS)

-SIZING_CONTRACT='Target planned net loss at stop loss is 5 USDT including entry and stop-loss exit fees. The worker computes the largest legal Binance base-asset quantity such that quantity × (abs(limit_entry - stop_loss) + limit_entry × taker_fee_rate + stop_loss × taker_fee_rate) <= 5 USDT. Use the supplied per-symbol account taker commission for entry, SL exit, and TP exit. Select TP targeting net reward/risk 1:2 after entry and exit fees, with only the unavoidable legal price-tick rounding: the first LONG TP tick at or above the exact net 1:2 target, or the first SHORT TP tick at or below it. Do not select an arbitrarily larger reward/risk. Slippage, funding, and price gaps are outside this calculation. The provider determines entry, TP, SL, or HOLD; the worker alone sizes quantity and rejects noncanonical TP without rewriting model prices.'
+SIZING_CONTRACT='Target planned net loss at stop loss is 5 USDT including entry and stop-loss exit taker fees and a 0.5% adverse SL execution reserve. The worker computes the largest legal Binance base-asset quantity within that target. Return LONG, SHORT, or HOLD using the supplied complete 1h and 15m Futures charts. A trading setup requires declared gross reward/risk at least 2 and original model price distances at least 1:2. Preserve model Entry, TP, and SL in the response. The worker keeps Entry and SL, normalizes the final order TP to net 1:2 including account fees and a 0.5% adverse TP execution reserve, then rounds to the first valid profitable tick. Funding, gaps, and execution beyond the stated reserve are outside the estimate.'

-TP_CONTRACT='Let E=limit_entry, S=stop_loss, f=the supplied account taker_fee_rate, and L=abs(E-S)+f*(E+S). LONG exact TP=(E*(1+f)+2*L)/(1-f), rounded up to the next valid tickSize. SHORT exact TP=(E*(1-f)-2*L)/(1+f), rounded down to the previous valid tickSize. Return that first legal TP tick only; net reward after entry and TP fees must reach twice the fee-inclusive SL loss. If no accurate profitable setup meets this policy, return HOLD. The worker does not rewrite TP.'
+TP_CONTRACT='Return the original model TP with valid gross risk:reward at least 1:2; the worker preserves it as audit evidence and computes the actual order TP. Let E=limit_entry, S=stop_loss, f=account taker_fee_rate, a=0.005. For LONG, adverse SL fill is S*(1-a), L=E-S*(1-a)+f*(E+S*(1-a)), and exact order TP=(E*(1+f)+2*L)/((1-a)*(1-f)), rounded up to the next valid tick. For SHORT, adverse SL fill is S*(1+a), L=S*(1+a)-E+f*(E+S*(1+a)), and exact order TP=(E*(1-f)-2*L)/((1+a)*(1+f)), rounded down to the previous valid tick. Return HOLD when no accurate valid gross setup exists.'

 def account_fee_rules(rules,symbol,client):
     """Attach a fresh account commission GET, without assuming a fallback rate."""
@@ -52,11 +53,13 @@ def analysis_context(market,symbol,risk_target=RISK,account_client=None):
             'max_entry_price':str(rules.mark_price*rules.multiplier_up)}
     return {**data,'contract_rules':contract,'risk_constraints':{'quantity_unit':'base_asset',
         'margin_mode_target':'CROSS','leverage_target':75,'maximum_loss_at_sl_usdt':str(risk_target),
-        'minimum_actual_reward_risk':2,'reward_risk_basis':'NET_AFTER_ENTRY_AND_EXIT_FEES',
-        'target_actual_reward_risk':2,'reward_risk_policy':REWARD_RISK_POLICY,
+        'minimum_actual_reward_risk':2,'reward_risk_basis':'NET_AFTER_ENTRY_EXIT_FEES_AND_ADVERSE_EXIT_RESERVE',
+        'minimum_declared_model_reward_risk':2,'minimum_model_price_reward_risk':2,
+        'target_actual_reward_risk':2,'reward_risk_policy':NORMALIZED_REWARD_RISK_POLICY,
+        'risk_model':RISK_MODEL,'exit_slippage_rate':format(EXIT_SLIPPAGE_RATE,'f'),
         'take_profit_contract':TP_CONTRACT,'take_profit_tick_rounding':{'LONG':'CEILING','SHORT':'FLOOR'},
         'entry_fee_rate':format(rules.taker_fee_rate,'f'),'sl_exit_fee_rate':format(rules.taker_fee_rate,'f'),
         'tp_exit_fee_rate':format(rules.taker_fee_rate,'f'),'fee_source':rules.fee_source,
         'fee_symbol':rules.fee_symbol,'fee_observed_at':rules.fee_observed_at,
-        'excluded_costs':['SLIPPAGE','FUNDING','GAPS'],
+        'excluded_costs':list(RESERVE_EXCLUDED_COSTS),
         'target_loss_at_sl_usdt':str(risk_target),'position_sizing_contract':SIZING_CONTRACT.replace('5 USDT',str(risk_target)+' USDT')}},rules
diff --git a/worker/binance_office.py b/worker/binance_office.py
index d2fb1e0..f94ed45 100644
--- a/worker/binance_office.py
+++ b/worker/binance_office.py
@@ -10,7 +10,7 @@ from datetime import datetime, timedelta, timezone
 from decimal import Decimal, localcontext

 from .binance_private import BinanceCheckError, CODES
-from .account_state import account_order
+from .account_state import account_order, open_entry_symbols

 DAY_MS=86_400_000
 PAGE_SIZE=1000
@@ -232,6 +232,10 @@ def collect(client,include_history=True):
     try:
         if type(include_history) is not bool:raise ValueError()
         client.sync_time()
+        # Potential entries precede the position reads so a concurrent fill is
+        # still represented by their symbol union in capacity calculations.
+        pending=set(open_entry_symbols(_read(client,'/fapi/v1/openOrders'),allow_hedge=True))
+        pending.update(open_entry_symbols(_read(client,'/fapi/v1/openAlgoOrders'),algo=True,allow_hedge=True))
         raw_account=_read(client,'/fapi/v3/account')
         config=_read(client,'/fapi/v1/accountConfig')
         risks=_read(client,'/fapi/v3/positionRisk')
@@ -242,6 +246,7 @@ def collect(client,include_history=True):
             client.sync_time();end_ms=client.server_time_ms()
         checked_at=_iso(end_ms)
         account=_account(raw_account,config,risks,checked_at)
+        account['open_entry_symbols']=sorted(pending)
         account.update(completion_order)
     except Exception as error:
         reason=_reason(error)
diff --git a/worker/core.py b/worker/core.py
index 5d6036a..9740c6a 100644
--- a/worker/core.py
+++ b/worker/core.py
@@ -17,6 +17,12 @@ RISK = D('5')
 FEE_SOURCE = 'BINANCE_FUTURES_COMMISSION_RATE'
 SIZING_METHOD = 'MAX_LOT_ENTRY_SL_TAKER_FEES_V2'
 REWARD_RISK_POLICY = 'NET_1_TO_2_NEAREST_TICK'
+RISK_MODEL = 'FEE_SLIPPAGE_RISK_V3'
+NORMALIZED_REWARD_RISK_POLICY = 'NET_1_TO_2_NORMALIZED_WITH_EXIT_RESERVE'
+NORMALIZED_SIZING_METHOD = 'MAX_LOT_ENTRY_SL_TAKER_FEES_SLIPPAGE_V3'
+EXIT_SLIPPAGE_RATE = D('0.005')
+NORMALIZATION_VERSION = 'TP_NET_RR_NORMALIZATION_V1'
+RESERVE_EXCLUDED_COSTS = ['FUNDING','GAPS_BEYOND_RESERVE','FEE_CHANGES_AFTER_OBSERVATION']
 class Review(Exception): pass

 def now(): return datetime.now(timezone.utc).isoformat()
@@ -117,8 +123,8 @@ def validated_fee_rate(rules,symbol=None,*,check_fresh=True):
     return rate


-def maximum_risk_quantity(entry,sl,rules,risk_target=RISK,*,check_fresh=True):
-    """Largest legal lot within planned SL loss plus entry and SL taker fees."""
+def maximum_risk_quantity(entry,sl,rules,risk_target=RISK,*,check_fresh=True,risk_model=None,side=None):
+    """Largest legal lot within the explicitly selected planned SL cost model."""
     target=Fraction(validated_risk_target(risk_target))
     entry,sl=(Fraction(number(value)) for value in (entry,sl))
     for value in (rules.step,rules.minimum,rules.maximum):number(value)
@@ -127,7 +133,10 @@ def maximum_risk_quantity(entry,sl,rules,risk_target=RISK,*,check_fresh=True):
     rate=Fraction(validated_fee_rate(rules,check_fresh=check_fresh))
     distance=abs(entry-sl)
     if distance<=0:raise Review('INVALID_ENTRY_TP_SL')
-    loss=distance+(entry+sl)*rate
+    if risk_model is None:loss=distance+(entry+sl)*rate
+    else:
+        _require_risk_model(risk_model)
+        loss=_reserve_unit_loss(entry,sl,side,rate)
     step=Fraction(rules.step)
     # Fee products and lot floors stay exact even under a caller's low precision.
     steps=min(target//(loss*step),Fraction(rules.maximum)//step)
@@ -137,12 +146,15 @@ def maximum_risk_quantity(entry,sl,rules,risk_target=RISK,*,check_fresh=True):
     return _exact_decimal(expected)


-def risk_costs(entry,tp,sl,quantity,rules,*,symbol=None,check_fresh=True):
-    """Exact planned price/fee model, with archival arithmetic available explicitly.
+def risk_costs(entry,tp,sl,quantity,rules,*,symbol=None,check_fresh=True,risk_model=None,side=None):
+    """Exact planned costs; only the versioned model includes exit reserves.

-    This model includes entry and SL exit fees, and deducts entry and TP exit
-    fees from reward. Slippage, funding and price gaps are outside the model.
+    The absent-marker archival model retains its original fee-only arithmetic.
+    Neither model treats a planned trigger as proof of an actual exit fill.
     """
+    if risk_model is not None:
+        _require_risk_model(risk_model)
+        return _reserve_risk_costs(entry,tp,sl,quantity,rules,side,symbol=symbol,check_fresh=check_fresh)
     entry,tp,sl,quantity=(Fraction(number(value)) for value in (entry,tp,sl,quantity))
     rate=Fraction(validated_fee_rate(rules,symbol,check_fresh=check_fresh))
     distance=abs(entry-sl);reward=abs(tp-entry)
@@ -162,6 +174,147 @@ def risk_costs(entry,tp,sl,quantity,rules,*,symbol=None,check_fresh=True):
         excluded_costs=['SLIPPAGE','FUNDING','GAPS'])


+def _require_risk_model(value):
+    if not isinstance(value,str) or value!=RISK_MODEL:raise Review('INVALID_COST_MODEL')
+    return value
+
+
+def validated_exit_slippage_rate(value=EXIT_SLIPPAGE_RATE):
+    value=number(value)
+    if value!=EXIT_SLIPPAGE_RATE:raise Review('INVALID_COST_MODEL')
+    return value
+
+
+def _adverse_exit_price(trigger,side):
+    if side not in ('LONG','SHORT'):raise Review('INVALID_SIDE')
+    factor=1-Fraction(EXIT_SLIPPAGE_RATE) if side=='LONG' else 1+Fraction(EXIT_SLIPPAGE_RATE)
+    return Fraction(trigger)*factor
+
+
+def _reserve_unit_loss(entry,sl,side,fee):
+    if side not in ('LONG','SHORT'):raise Review('INVALID_SIDE')
+    if not (sl<entry if side=='LONG' else sl>entry):raise Review('INVALID_ENTRY_TP_SL')
+    adverse=_adverse_exit_price(sl,side)
+    return abs(entry-sl)+sl*Fraction(EXIT_SLIPPAGE_RATE)+fee*(entry+adverse)
+
+
+def normalized_reward_risk_tp(entry,stop_loss,side,rules,*,symbol=None,check_fresh=False):
+    """First legal trigger whose reserved adverse TP fill yields planned net 1:2."""
+    e,sl,tick=(Fraction(number(value)) for value in (entry,stop_loss,rules.tick))
+    fee=Fraction(validated_fee_rate(rules,symbol,check_fresh=check_fresh))
+    reserve=Fraction(EXIT_SLIPPAGE_RATE)
+    loss=_reserve_unit_loss(e,sl,side,fee)
+    target=(e*(1+fee)+2*loss)/((1-reserve)*(1-fee)) if side=='LONG' else (e*(1-fee)-2*loss)/((1+reserve)*(1+fee))
+    steps=target/tick
+    count=-(-steps.numerator//steps.denominator) if side=='LONG' else steps.numerator//steps.denominator
+    price=count*tick
+    if price<=0 or not (price>e if side=='LONG' else price<e):raise Review('INVALID_ENTRY_TP_SL')
+    return _exact_decimal(price)
+
+
+def _reserve_risk_costs(entry,tp,sl,quantity,rules,side,*,symbol=None,check_fresh=True):
+    e,t,s,q=(Fraction(number(value)) for value in (entry,tp,sl,quantity))
+    fee=Fraction(validated_fee_rate(rules,symbol,check_fresh=check_fresh))
+    loss=_reserve_unit_loss(e,s,side,fee)
+    if not (s<e<t if side=='LONG' else t<e<s):raise Review('INVALID_ENTRY_TP_SL')
+    adverse_sl=_adverse_exit_price(s,side);adverse_tp=_adverse_exit_price(t,side)
+    reserve=Fraction(EXIT_SLIPPAGE_RATE)
+    distance=abs(e-s);reward=abs(t-e)
+    gross_risk=q*distance;gross_reward=q*reward
+    entry_fee=q*e*fee;sl_fee=q*adverse_sl*fee;tp_fee=q*adverse_tp*fee
+    sl_slippage=q*s*reserve;tp_slippage=q*t*reserve
+    total=q*loss;net_reward=gross_reward-tp_slippage-entry_fee-tp_fee
+    if net_reward<2*total:raise Review('NET_RISK_REWARD_BELOW_2')
+    costs=dict(risk=_exact_text(total),total_risk=_exact_text(total),
+        gross_risk=_exact_text(gross_risk),entry_fee_usdt=_exact_text(entry_fee),
+        sl_exit_fee_usdt=_exact_text(sl_fee),tp_exit_fee_usdt=_exact_text(tp_fee),
+        gross_reward=_exact_text(gross_reward),net_reward=_exact_text(net_reward),
+        rr=str(_ratio_decimal(reward/distance)),net_rr=str(_ratio_decimal(net_reward/total)),
+        entry_fee_rate=format(rules.taker_fee_rate,'f'),exit_fee_rate=format(rules.taker_fee_rate,'f'),
+        tp_fee_rate=format(rules.taker_fee_rate,'f'),fee_symbol=rules.fee_symbol,fee_source=rules.fee_source,
+        fee_observed_at=rules.fee_observed_at,sizing_method=NORMALIZED_SIZING_METHOD,
+        excluded_costs=list(RESERVE_EXCLUDED_COSTS),risk_model=RISK_MODEL,
+        exit_slippage_rate=format(EXIT_SLIPPAGE_RATE,'f'),entry_slippage_rate='0',
+        sl_slippage_usdt=_exact_text(sl_slippage),tp_slippage_usdt=_exact_text(tp_slippage),
+        sl_execution_price=_exact_text(adverse_sl),tp_execution_price=_exact_text(adverse_tp))
+    costs['cost_evidence']=dict(version=RISK_MODEL,fee_source=rules.fee_source,fee_symbol=rules.fee_symbol,
+        fee_observed_at=rules.fee_observed_at,taker_fee_rate=format(rules.taker_fee_rate,'f'),
+        exit_slippage_rate=format(EXIT_SLIPPAGE_RATE,'f'),entry_slippage_rate='0',
+        reserve_source='CONFIGURED_ADVERSE_EXIT_RATE',excluded_costs=list(RESERVE_EXCLUDED_COSTS))
+    return costs
+
+
+def _tp_normalization(signal,tp,rules):
+    distance=abs(Fraction(signal.entry)-Fraction(signal.sl))
+    if distance<=0:raise Review('INVALID_ENTRY_TP_SL')
+    model_rr=abs(Fraction(signal.tp)-Fraction(signal.entry))/distance
+    return dict(version=NORMALIZATION_VERSION,source='WORKER_DERIVED',
+        model_entry=format(signal.entry,'f'),model_tp=format(signal.tp,'f'),model_sl=format(signal.sl,'f'),
+        model_gross_rr=str(_ratio_decimal(model_rr)),execution_tp=format(tp,'f'),
+        tick_size=format(rules.tick,'f'),risk_model=RISK_MODEL,
+        exit_slippage_rate=format(EXIT_SLIPPAGE_RATE,'f'),taker_fee_rate=format(rules.taker_fee_rate,'f'))
+
+
+def _validate_normalized_signal(signal,rules,*,check_fresh=True):
+    if type(rules.observed_at) not in (int,float) or not math.isfinite(rules.observed_at) or rules.observed_at<0:
+        raise Review('INVALID_RULES')
+    if check_fresh and not 0<=time.time()-rules.observed_at<=300:raise Review('NEEDS_REVIEW: filter pasar kedaluwarsa')
+    for value in (rules.step,rules.minimum,rules.maximum,rules.tick):number(value)
+    if not isinstance(rules.min_notional,D) or not rules.min_notional.is_finite() or rules.min_notional<0:raise Review('INVALID_RULES')
+    if any(not isinstance(value,D) or not value.is_finite() or value<0 for value in (rules.min_price,rules.max_price)) or rules.min_price>rules.max_price:
+        raise Review('INVALID_RULES')
+    if signal.side not in ('LONG','SHORT') or not re.fullmatch(r'[A-Z0-9]{2,18}USDT',signal.symbol):raise Review('INVALID_SIDE')
+    for value in (signal.entry,signal.tp,signal.sl):number(value)
+    if not (signal.sl<signal.entry<signal.tp if signal.side=='LONG' else signal.tp<signal.entry<signal.sl):raise Review('INVALID_ENTRY_TP_SL')
+    distance=abs(Fraction(signal.entry)-Fraction(signal.sl))
+    if abs(Fraction(signal.tp)-Fraction(signal.entry))<2*distance:raise Review('RISK_REWARD_BELOW_2')
+    # The original model TP is retained for audit; only entry/SL execute directly.
+    for price in (signal.entry,signal.sl):
+        if not rules.min_price<=price<=rules.max_price or (Fraction(price)/Fraction(rules.tick)).denominator!=1:
+            raise Review('NEEDS_REVIEW: harga tidak sesuai tick/rentang')
+    validated_fee_rate(rules,signal.symbol,check_fresh=check_fresh)
+    if rules.multiplier_up is not None:
+        if rules.mark_price is None or rules.multiplier_down is None:raise Review('INVALID_RULES')
+        for value in (rules.multiplier_up,rules.multiplier_down,rules.mark_price):number(value)
+        if not Fraction(rules.mark_price)*Fraction(rules.multiplier_down)<=Fraction(signal.entry)<=Fraction(rules.mark_price)*Fraction(rules.multiplier_up):
+            raise Review('REJECT_PERCENT_PRICE')
+
+
+def _normalized_plan(signal,rules,risk_target,*,check_fresh=True):
+    _validate_normalized_signal(signal,rules,check_fresh=check_fresh)
+    target=validated_risk_target(risk_target)
+    tp=normalized_reward_risk_tp(signal.entry,signal.sl,signal.side,rules,symbol=signal.symbol,check_fresh=check_fresh)
+    if not rules.min_price<=tp<=rules.max_price:raise Review('NO_LEGAL_NET_RR_TP')
+    qty=maximum_risk_quantity(signal.entry,signal.sl,rules,target,check_fresh=check_fresh,risk_model=RISK_MODEL,side=signal.side)
+    costs=risk_costs(signal.entry,tp,signal.sl,qty,rules,symbol=signal.symbol,check_fresh=check_fresh,risk_model=RISK_MODEL,side=signal.side)
+    if not Fraction(0)<Fraction(D(costs['risk']))<=Fraction(target):raise Review('NEEDS_REVIEW: risiko melampaui batas')
+    return dict(symbol=signal.symbol,side=signal.side,entry=format(signal.entry,'f'),tp=format(tp,'f'),sl=format(signal.sl,'f'),
+        quantity=format(qty,'f'),execution_quantity=format(qty,'f'),margin_mode='CROSS',leverage=75,order_type='LIMIT',
+        mode='ORDER_INTENT',risk_target_usdt=format(target,'f'),rules_checked_at=rules.observed_at,
+        reward_risk_policy=NORMALIZED_REWARD_RISK_POLICY,tp_normalization=_tp_normalization(signal,tp,rules),**costs)
+
+
+def validate_normalized_plan(plan,rules,*,check_fresh=False):
+    """Recompute a versioned execution plan from preserved original model levels."""
+    try:
+        _require_risk_model(plan['risk_model'])
+        if plan['reward_risk_policy']!=NORMALIZED_REWARD_RISK_POLICY:raise Review('INVALID_RISK_REWARD')
+        validated_exit_slippage_rate(plan['exit_slippage_rate'])
+        if plan['entry_slippage_rate']!='0':raise Review('INVALID_COST_MODEL')
+        original=plan['tp_normalization']
+        signal=Signal(plan['symbol'],plan['side'],number(original['model_entry']),number(original['model_tp']),number(original['model_sl']))
+        expected=_normalized_plan(signal,rules,plan['risk_target_usdt'],check_fresh=check_fresh)
+        for key,value in expected.items():
+            if key=='rules_checked_at' and check_fresh:continue
+            if key in ('fee_observed_at','cost_evidence') and check_fresh:
+                # Fresh quotes must keep the actual rate; original evidence remains bound below.
+                if key=='fee_observed_at':continue
+                value={**value,'fee_observed_at':plan['fee_observed_at']}
+            if plan.get(key)!=value:raise Review('ORDER_COSTS_CHANGED')
+        return True
+    except (KeyError,TypeError,ValueError):raise Review('INVALID_COST_MODEL') from None
+
+
 def target_reward_risk_tp(entry,stop_loss,side,rules,*,symbol=None):
     """Exact first legal tick reaching net 1:2; never change an analyzed level."""
     e,sl,tick=(Fraction(number(value)) for value in (entry,stop_loss,rules.tick))
@@ -176,6 +329,7 @@ def target_reward_risk_tp(entry,stop_loss,side,rules,*,symbol=None):
     return _exact_decimal(count*tick)

 def validate_reward_risk_policy(plan,rules):
+    if 'risk_model' in plan:return validate_normalized_plan(plan,rules)
     if 'reward_risk_policy' not in plan:return True
     if not isinstance(plan['reward_risk_policy'],str) or plan['reward_risk_policy']!=REWARD_RISK_POLICY:
         raise Review('INVALID_RISK_REWARD')
@@ -188,7 +342,11 @@ def validate_reward_risk_policy(plan,rules):
     if tp!=Fraction(target):raise Review('NET_RISK_REWARD_NOT_TARGET_2')
     return True

-def risk_check(signal, rules, risk_target=RISK,*,reward_risk_policy=None):
+def risk_check(signal, rules, risk_target=RISK,*,reward_risk_policy=None,risk_model=None):
+    if risk_model is not None:
+        _require_risk_model(risk_model)
+        if reward_risk_policy not in (None,NORMALIZED_REWARD_RISK_POLICY):raise Review('INVALID_RISK_REWARD')
+        return _normalized_plan(signal,rules,risk_target)
     risk_target=validated_risk_target(risk_target)
     if not 0 <= time.time()-rules.observed_at <= 300: raise Review('NEEDS_REVIEW: filter pasar kedaluwarsa')
     for v in (rules.step,rules.minimum,rules.maximum,rules.tick): number(v)
@@ -224,6 +382,12 @@ def risk_check(signal, rules, risk_target=RISK,*,reward_risk_policy=None):
 def preflight(plan, rules, risk_target=RISK):
     if not isinstance(plan,dict) or plan.get('mode')!='ORDER_INTENT': raise Review('INVALID_ORDER_CONTRACT')
     validated_protection_working_type(plan.get('protection_working_type','MARK_PRICE'))
+    if 'risk_model' in plan:
+        if validated_risk_target(plan['risk_target_usdt'])!=validated_risk_target(risk_target):raise Review('ORDER_COSTS_CHANGED')
+        validate_normalized_plan(plan,rules,check_fresh=True)
+        observed=plan.get('fee_observed_at')
+        if type(observed) not in (int,float) or not math.isfinite(observed) or not 0<=time.time()-observed<=300:raise Review('STALE_FEE_EVIDENCE')
+        return
     if plan.get('margin_mode')!='CROSS' or plan.get('leverage')!=75 or plan.get('order_type')!='LIMIT':
         raise Review('NEEDS_REVIEW: konfigurasi order salah')
     sig=Signal(plan['symbol'],plan['side'],number(plan['entry']),number(plan['tp']),number(plan['sl']),number(plan['quantity']))
diff --git a/worker/diagnostics.py b/worker/diagnostics.py
index e18a8c9..a9584de 100644
--- a/worker/diagnostics.py
+++ b/worker/diagnostics.py
@@ -9,7 +9,7 @@ CODES=frozenset(('NETWORK_UNCERTAIN','INVALID_RESPONSE_ENVELOPE','INVALID_OUTPUT
  'INVALID_ENTRY_TP_SL','RISK_REWARD_BELOW_2','VALIDATION_REJECTED',
  'INVALID_PRICE_FILTER','RISK_ABOVE_5','POSITION_SIZE_NOT_MAX_RISK','NO_LEGAL_MAX_RISK_QUANTITY','INVALID_SCREENING_SCHEMA','INVALID_SCREENING_COUNT','INVALID_SCREENING_SYMBOL',
  'REJECT_QUANTITY_PRECISION_OR_RANGE','REJECT_MIN_NOTIONAL','REJECT_PERCENT_PRICE',
- 'MARKET_DATA_REJECTED','LOCAL_PROCESSING_FAILED','STALE_CONTRACT_CONTEXT','INVALID_CONTRACT_CONTEXT',
+ 'MARKET_DATA_REJECTED','LOCAL_PROCESSING_FAILED','STALE_CONTRACT_CONTEXT','STALE_MARKET_CONTEXT','INVALID_CONTRACT_CONTEXT',
  'INVALID_ORDER_CONTRACT','INVALID_RISK_REWARD','INVALID_RISK_TARGET','INVALID_EXECUTION_QUANTITY',
  'INVALID_SIDE','INVALID_RULES','RISK_ABOVE_TARGET','FEE_EVIDENCE_UNAVAILABLE','INVALID_FEE_EVIDENCE',
  'STALE_FEE_EVIDENCE','NET_RISK_REWARD_BELOW_2','NET_RISK_REWARD_NOT_TARGET_2','ORDER_COSTS_CHANGED','INVALID_COST_MODEL','RESEARCH_PAUSED','NEUROAPI_REQUEST_LIMIT_EXCEEDED'))
diff --git a/worker/neuroapi.py b/worker/neuroapi.py
index 177d576..d7daf37 100644
--- a/worker/neuroapi.py
+++ b/worker/neuroapi.py
@@ -28,8 +28,8 @@ def screening_count(schema):
 NUM={'type':'number','exclusiveMinimum':0}
 PRICE={**NUM,'type':['number','null'],'description':'Price must conform exactly to Binance tickSize and applicable price limits supplied in context; do not round after generation.'}
 SETUP_SCHEMA={'type':'object','description':'LONG/SHORT require positive entry, TP, SL and declared reward/risk. HOLD requires all numeric fields null and creates no order.','properties':{'symbol':{'type':'string'},'side':{'type':'string','enum':['LONG','SHORT','HOLD']},
- 'limit_entry':PRICE,'take_profit':PRICE,'stop_loss':PRICE,
- 'risk_reward':{**NUM,'type':['number','null'],'description':'Reward divided by risk; 2 means risk:reward 1:2'}},
+ 'limit_entry':PRICE,'take_profit':{**PRICE,'description':'Original model TP proposal; preserve this price. The worker normalizes the final order TP to net risk:reward 1:2 using fees and the supplied adverse exit reserve.'},'stop_loss':PRICE,
+ 'risk_reward':{**NUM,'type':['number','null'],'description':'Declared gross reward divided by risk; LONG/SHORT require at least 2. HOLD requires null.'}},
  'required':['symbol','side','limit_entry','take_profit','stop_loss','risk_reward'],'additionalProperties':False}

 def api_key():
@@ -73,7 +73,7 @@ class Hold:
     symbol: str
     side: str = 'HOLD'

-def setup(output,expected):
+def setup(output,expected,*,require_declared_rr=True,require_gross_rr=True):
     if not isinstance(output,dict) or set(output)!=set(SETUP_SCHEMA['required']):raise Review('INVALID_SETUP_SCHEMA')
     if output['symbol']!=expected or not isinstance(expected,str) or not re.fullmatch(r'[A-Z0-9]{2,18}USDT',expected) or output['side'] not in ('LONG','SHORT','HOLD'):raise Review('INVALID_SETUP_SYMBOL_SIDE')
     if output['side']=='HOLD':
@@ -81,8 +81,9 @@ def setup(output,expected):
         return Hold(expected)
     e,tp,sl,rr=[setup_number(output[k]) for k in NUMERIC_FIELDS]
     if not (sl<e<tp if output['side']=='LONG' else tp<e<sl):raise Review('INVALID_ENTRY_TP_SL')
+    if require_declared_rr and rr<2:raise Review('RISK_REWARD_BELOW_2')
     # Compare distances exactly; no rounding/equality assumption for declared RR.
-    if abs(Fraction(tp)-Fraction(e))<2*abs(Fraction(e)-Fraction(sl)):raise Review('RISK_REWARD_BELOW_2')
+    if require_gross_rr and abs(Fraction(tp)-Fraction(e))<2*abs(Fraction(e)-Fraction(sl)):raise Review('RISK_REWARD_BELOW_2')
     return Signal(expected,output['side'],e,tp,sl)

 def canonical_output(value,schema,catalog=None):
@@ -92,7 +93,7 @@ def canonical_output(value,schema,catalog=None):
         symbols=selections(value,catalog,screening_count(schema))
         return json.dumps({'symbols':list(symbols)})
     if schema==SETUP_SCHEMA:
-        signal=setup(value,value.get('symbol'))
+        signal=setup(value,value.get('symbol'),require_declared_rr=False,require_gross_rr=False)
         fields={'symbol':signal.symbol,'side':signal.side,**{k:setup_number(value[k]) if value[k] is not None else None for k in NUMERIC_FIELDS}}
         return '{'+','.join(json.dumps(k)+':'+(format(v,'f') if isinstance(v,D) else json.dumps(v)) for k,v in fields.items())+'}'
     raise Review('INVALID_OUTPUT_SCHEMA')
diff --git a/worker/prompts.py b/worker/prompts.py
index 928f96a..cddbd39 100644
--- a/worker/prompts.py
+++ b/worker/prompts.py
@@ -1,8 +1,10 @@
 # Exact user literals; do not normalize LF or whitespace before sending.
 # Do not reflow or normalize whitespace before sending.
 SCREENING = 'pilihkan 2 coin yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance'
+REPLACEMENT_SCREENING = 'pilihkan {count} coin pengganti yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance'
 ANALYSIS = """Aku berikan data chart realtime di binance future 2 time frame 1 jam dan 15 menit, silahkan analisa dengan akurat dan Profitable. aku mau entry di time frame 15 menit untuk scalping.
 Tentukan !
+LONG, SHORT, atau HOLD
 ENTRY
 TP
 SL : yang tidak mudah terkena wick atau di jilat para bandar.
diff --git a/worker/research_guard.py b/worker/research_guard.py
index 4865d6b..aa084c7 100644
--- a/worker/research_guard.py
+++ b/worker/research_guard.py
@@ -6,6 +6,7 @@ from .core import Review


 _continue_research = ContextVar('office_continue_research', default=None)
+_continue_gateway_mutation = ContextVar('office_continue_gateway_mutation', default=None)


 class ResearchReadPaused(Review):
@@ -45,3 +46,32 @@ def gateway_transaction_reads():
         yield
     finally:
         _continue_research.reset(token)
+
+
+def gateway_mutations_allowed():
+    """An explicitly authorized live actor must still be ON at the wire boundary."""
+    continue_if = _continue_gateway_mutation.get()
+    if continue_if is None:
+        return False
+    try:
+        return continue_if() is True
+    except Exception:
+        return False
+
+
+@contextmanager
+def gateway_mutations(continue_if):
+    """Fence financial writes separately from reads that may finish after OFF.
+
+    A nested scope cannot broaden an outer actor's permission. There is no
+    implicit authorization for a standalone gateway or another thread.
+    """
+    if not callable(continue_if):
+        raise TypeError('gateway mutation predicate must be callable')
+    previous = _continue_gateway_mutation.get()
+    predicate = continue_if if previous is None else lambda: previous() is True and continue_if() is True
+    token = _continue_gateway_mutation.set(predicate)
+    try:
+        yield
+    finally:
+        _continue_gateway_mutation.reset(token)
diff --git a/worker/robot.py b/worker/robot.py
index 6aab83e..8c670e7 100644
--- a/worker/robot.py
+++ b/worker/robot.py
@@ -2,21 +2,22 @@
 from dataclasses import asdict
 import fcntl
 import json
+import math
 import re
 import time
 from pathlib import Path
 from datetime import datetime,timezone
 from zoneinfo import ZoneInfo
-from .core import D,Review,day,now,risk_check,preflight,REWARD_RISK_POLICY
+from .core import D,Review,day,now,risk_check,preflight,RISK_MODEL,NORMALIZED_REWARD_RISK_POLICY,validated_risk_target
 from .account_state import (account_state,screening_contract,MANUAL_ONLY_SYMBOLS,MAX_REPLACEMENTS)
 from .robot_store import RobotStore
 from .order_gateway import OrderGateway,GatewayUnavailable,NOT_CONNECTED,build_intent,require_implementation
 from .neuroapi import SETUP_SCHEMA,ResearchPaused,selections,setup
-from .prompts import ANALYSIS
+from .prompts import ANALYSIS,REPLACEMENT_SCREENING
 from .analysis import analysis_context
 from .diagnostics import validation_code
 from .robot_provenance import VERSION,stamp,verify
-from .research_guard import research_reads,ResearchReadPaused,gateway_transaction_reads
+from .research_guard import research_reads,ResearchReadPaused,gateway_transaction_reads,gateway_mutations

 class Coordinator:
     def __init__(self,ledger,neuro,market,account_factory,stopping=None,gateway=None):
@@ -43,13 +44,114 @@ class Coordinator:
                 return self.store.report('REJECTED',reason=reason if reason in allowed else 'ROBOT_PREFLIGHT_NEEDS_REVIEW')
     def save_cycle(self,cycle,data,state='ACTIVE'):
         self.db.execute('UPDATE robot_cycles SET data=?,state=? WHERE id=?',(json.dumps(data),state,cycle))
+    def completed_research(self,operation,cycle,kind,symbol):
+        job=self.db.execute('SELECT * FROM robot_jobs WHERE operation=?',(operation,)).fetchone()
+        if not job:return None
+        row=self.db.execute('SELECT * FROM api_requests WHERE operation=?',(operation,)).fetchone()
+        if (job['cycle']!=cycle or job['kind']!=kind or job['symbol']!=symbol
+                or job['state'] not in ('PENDING','COMPLETE') or not row
+                or row['state']!='COMPLETE' or not isinstance(row['output'],str) or not row['output']
+                or not isinstance(row['body_hash'],str) or not re.fullmatch(r'[a-f0-9]{64}',row['body_hash'])
+                or type(row['attempts']) is not int or not 1<=row['attempts']<=3
+                or type(row['created']) not in (int,float) or not math.isfinite(row['created']) or row['created']<=0):
+            raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
+        if kind=='ANALYSIS':validated_risk_target(job['risk_target'])
+        elif job['risk_target'] is not None:raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
+        return row,job
+    def research_job(self,job):
+        cycle=self.db.execute("SELECT * FROM robot_cycles WHERE id=? AND state IN ('ACTIVE','COMPLETE')",(job['cycle'],)).fetchone()
+        if not cycle or not re.fullmatch(r'\d{4}-\d{2}-\d{2}:robot-v9:(?:0|[1-9]\d{0,11})',cycle['id']):raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
+        if cycle['day']!=cycle['id'].split(':',1)[0] or cycle['entry_epoch']!=int(cycle['id'].rsplit(':',1)[1]):raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
+        datetime.strptime(cycle['day'],'%Y-%m-%d')
+        data=json.loads(cycle['data'])
+        if job['kind']=='ANALYSIS':
+            if not data['queue'] or data['queue'][0]!=job['symbol'] or job['operation']!=cycle['id']+':'+VERSION+':'+job['symbol']:raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
+        elif job['kind']=='SCREENING':
+            if data['queue'] or job['symbol'] is not None or job['operation'] not in (
+                    cycle['id']+':screening:'+str(data['screen']+1)+':1',
+                    cycle['id']+':screening:'+str(data['screen']+1)+':2'):raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
+        else:raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
+        if not self.completed_research(job['operation'],job['cycle'],job['kind'],job['symbol']):raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
+        return cycle,data
+    def unresolved_research(self):
+        if self.db.execute("SELECT 1 FROM api_requests WHERE state IN ('PENDING','NEEDS_REVIEW') LIMIT 1").fetchone():return True
+        for job in self.db.execute("SELECT * FROM robot_jobs WHERE state IN ('PENDING','NEEDS_REVIEW')"):
+            if job['state']=='NEEDS_REVIEW':return True
+            try:self.research_job(job)
+            except Exception:return True
+        return False
+    def settle_previous_research(self,today,account):
+        # A complete paid response may outlive a crash at midnight. Its old
+        # scheduling/quota context never creates an order on the new day.
+        if self.unresolved_research():return None
+        for job in self.db.execute("SELECT * FROM robot_jobs WHERE state='PENDING' ORDER BY rowid"):
+            cycle,data=self.research_job(job)
+            if cycle['day']>=today:continue
+            try:
+                api,_=self.completed_research(job['operation'],job['cycle'],job['kind'],job['symbol'])
+                value=json.loads(api['output'],parse_float=D)
+                if job['kind']=='ANALYSIS':
+                    setup(value,job['symbol'],require_declared_rr=False,require_gross_rr=False)
+                    if self.db.execute('SELECT 1 FROM robot_candidates WHERE id=?',(job['operation'],)).fetchone():raise ValueError
+                    if self.db.execute('SELECT 1 FROM order_intents WHERE candidate_id=?',(job['operation'],)).fetchone():raise ValueError
+                else:
+                    symbols=value.get('symbols',[]) if isinstance(value,dict) else []
+                    selections(value,{symbol:{} for symbol in symbols if isinstance(symbol,str)},int(job['operation'].rsplit(':',1)[1]))
+            except Exception:return self.store.report('REJECTED',account,'ROBOT_REQUEST_NEEDS_REVIEW')
+            self.db.execute('BEGIN IMMEDIATE')
+            try:
+                if not self.allowed(today):
+                    self.db.execute('ROLLBACK');return self.store.report('WAITING',account,wait_reason=self.pause_reason())
+                if job['kind']=='ANALYSIS':
+                    self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',(job['operation'],cycle['id'],job['symbol'],'REJECTED',None,'STALE_MARKET_CONTEXT'))
+                    data['queue'].pop(0);data['seen'].append(job['symbol'])
+                else:data['screen']+=1
+                self.db.execute("UPDATE robot_jobs SET state='COMPLETE' WHERE operation=? AND state='PENDING'",(job['operation'],))
+                self.save_cycle(cycle['id'],data,'COMPLETE');self.db.execute('COMMIT')
+            except BaseException:self.db.execute('ROLLBACK');raise
+            return self.store.report('REJECTED',account,'STALE_MARKET_CONTEXT')
+        return None
+    def context_witness(self,context,symbol):
+        self.market.fresh(context,symbol)
+        frames={tf:dict(symbol=frame['symbol'],timeframe=frame['timeframe'],last_close_time=frame['candles'][-1]['close_time'])
+            for tf,frame in context['timeframes'].items()}
+        return dict(version='ANALYSIS_CONTEXT_FRESHNESS_V1',source=context['source'],symbol=symbol,
+            fetched_at=context['fetched_at'],server_time=context['server_time'],timeframes=frames,claimed_at=time.time())
+    def original_context_fresh(self,row,job):
+        try:
+            data=json.loads(self.db.execute('SELECT data FROM robot_cycles WHERE id=?',(job['cycle'],)).fetchone()[0])
+            witness=data['analysis_contexts'][job['operation']]
+            if not isinstance(witness,dict) or set(witness)!={'version','source','symbol','fetched_at','server_time','timeframes','claimed_at'}:raise ValueError
+            if witness['version']!='ANALYSIS_CONTEXT_FRESHNESS_V1' or witness['symbol']!=job['symbol'] or witness['source']!='Binance Futures':raise ValueError
+            fetched,claimed=witness['fetched_at'],witness['claimed_at']
+            if any(type(value) not in (int,float) or not math.isfinite(value) or value<=0 for value in (fetched,claimed)):raise ValueError
+            if not fetched<=claimed<=row['created']<=time.time() or row['created']-fetched>self.market.max_age:raise ValueError
+            if type(witness['server_time']) is not int or witness['server_time']<=0:raise ValueError
+            if not isinstance(witness['timeframes'],dict) or set(witness['timeframes'])!={'1h','15m'}:raise ValueError
+            frames={}
+            for tf,frame in witness['timeframes'].items():
+                if not isinstance(frame,dict) or set(frame)!={'symbol','timeframe','last_close_time'}:raise ValueError
+                close=frame['last_close_time']
+                if type(close) is not int or close<=0:raise ValueError
+                frames[tf]=dict(symbol=frame['symbol'],timeframe=frame['timeframe'],candles=[dict(close_time=close)])
+            self.market.fresh(dict(source=witness['source'],symbol=witness['symbol'],fetched_at=fetched,timeframes=frames),job['symbol'])
+        except Exception:raise Review('STALE_MARKET_CONTEXT') from None
+    def completed_value(self,cached,validate):
+        row,job=cached
+        created=row['created']
+        if type(created) not in (int,float) or not 0<=time.time()-created<=self.market.max_age:
+            raise Review('STALE_MARKET_CONTEXT')
+        if job['kind']=='ANALYSIS':self.original_context_fresh(row,job)
+        value=json.loads(row['output'],parse_float=D)
+        validate(value)
+        return value
     def replaceable_result(self,row):
         if row['status']=='HOLD':return True
         if row['status']!='REJECTED' or row['plan'] is not None or self.db.execute(
                 'SELECT 1 FROM order_intents WHERE candidate_id=? LIMIT 1',(row['id'],)).fetchone():return False
         # Only local RR failures or a recorded request-body rejection may
         # select another coin. An unknown outcome never qualifies.
-        if row['failure_code'] in ('NET_RISK_REWARD_BELOW_2','NET_RISK_REWARD_NOT_TARGET_2'):return True
+        if row['failure_code'] in ('RISK_REWARD_BELOW_2','NET_RISK_REWARD_BELOW_2','NET_RISK_REWARD_NOT_TARGET_2'):return True
         if row['failure_code']!='HTTP_422':return False
         return bool(self.db.execute('''SELECT 1 FROM api_requests a JOIN robot_jobs j ON j.operation=a.operation
             WHERE a.operation=? AND a.state='REJECTED_REQUEST_VALIDATION' AND a.failure_code='HTTP_422'
@@ -74,6 +176,9 @@ class Coordinator:
             data['queue'].pop(0);data['seen'].append(symbol)
             self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
                 (cycle+':'+VERSION+':'+symbol,cycle,symbol,'REJECTED',None,reason))
+            self.db.execute("""UPDATE robot_jobs SET state='COMPLETE' WHERE operation=? AND state='PENDING'
+                AND EXISTS (SELECT 1 FROM api_requests WHERE operation=robot_jobs.operation
+                    AND state='COMPLETE' AND output IS NOT NULL)""",(cycle+':'+VERSION+':'+symbol,))
             self.save_cycle(cycle,data)
             self.db.execute('COMMIT')
         except BaseException:self.db.execute('ROLLBACK');raise
@@ -87,16 +192,32 @@ class Coordinator:
     def execution_slots(self,account,today):
         # Recompute reservations/receipts after every gateway observation;
         # account GET snapshots may precede an actual pending entry fill.
-        return self.store.available_slots(account['running_positions'],account['running_symbols'],today)
+        return self.store.available_slots(account['running_positions'],account['running_symbols'],today,
+            pending_symbols=account.get('open_entry_symbols') or ())
     def exposed_symbols(self,account):
         # A confirmed adapter observation can precede the next account GET.
         # Reserve both its slot and symbol until that intent is closed/rejected.
         active=self.db.execute("SELECT symbol FROM order_intents WHERE state IN ('ENTRY_PENDING','POSITION_PROTECTED')")
-        return set(account['running_symbols'])|{row['symbol'] for row in active}
+        return set(account['running_symbols'])|set(account.get('open_entry_symbols') or ())|{row['symbol'] for row in active}
     def advance_execution(self,account,today):
         if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
         row=self.db.execute("SELECT * FROM order_intents WHERE state IN ('SUBMITTING','NEEDS_REVIEW') ORDER BY rowid LIMIT 1").fetchone()
-        if row:return self.store.report('NEEDS_REVIEW',account,row['failure_code'] or 'ORDER_OUTCOME_UNKNOWN')
+        if row:
+            # An uncertain submission is never sent again. While ON, existing
+            # owned exchange evidence may resolve it using GET-only reconciliation.
+            reason=row['failure_code'] or 'ORDER_OUTCOME_UNKNOWN'
+            try:
+                require_implementation(self.gateway,'reconcile')
+                intent=self.verified_intent(row)
+            except Exception:return self.store.report('NEEDS_REVIEW',account,reason)
+            if not self.gateway_allowed():return self.store.report('WAITING',account,wait_reason=self.pause_reason())
+            try:
+                with gateway_transaction_reads(),gateway_mutations(lambda:False):
+                    result=self.gateway.reconcile(intent)
+            except Exception:return self.store.report('NEEDS_REVIEW',account,reason)
+            if not isinstance(result,dict) or result.get('state') not in ('ENTRY_PENDING','POSITION_PROTECTED','CLOSED'):
+                return self.store.report('NEEDS_REVIEW',account,reason)
+            return self.record_gateway_observation(row['id'],row['candidate_id'],result,account,preserve_unknown=True)
         rows=list(self.db.execute("SELECT * FROM order_intents WHERE state IN ('ENTRY_PENDING','POSITION_PROTECTED') ORDER BY rowid LIMIT 2"))
         for row in rows:
             if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
@@ -107,7 +228,8 @@ class Coordinator:
             if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
             try:
                 # A started adapter transaction may need reads to finish protection.
-                with gateway_transaction_reads():result=self.gateway.reconcile(intent)
+                with gateway_transaction_reads(),gateway_mutations(self.gateway_allowed):
+                    result=self.gateway.reconcile(intent)
             except Exception as error:return self.mark_unknown(row['id'],row['candidate_id'],account,reason=self.gateway_failure_code(error))
             observed=self.record_gateway_observation(row['id'],row['candidate_id'],result,account)
             if observed['bot_status']=='NEEDS_REVIEW':return observed
@@ -168,8 +290,9 @@ class Coordinator:
                 (previous_state,NOT_CONNECTED if previous_state=='EXECUTION_BLOCKED' else None,now(),row['id']))
             return self.store.report('WAITING',account,wait_reason=self.pause_reason())
         try:
-            # OFF stops the next callback; an invoked entry/protection callback drains.
-            with gateway_transaction_reads():result=self.gateway.submit(intent)
+            # GET proofs can drain; every later SDK mutation checks current ON.
+            with gateway_transaction_reads(),gateway_mutations(self.gateway_allowed):
+                result=self.gateway.submit(intent)
         except GatewayUnavailable:
             # A transport that might have sent a request must report an unknown
             # outcome, never claim this pre-send NOT_CONNECTED exception.
@@ -205,7 +328,7 @@ class Coordinator:
             if text.startswith('BINANCE_ORDER_') and text[14:] in known:
                 return text+('_'+phase if text[14:] in {'OUTCOME_UNKNOWN','REQUEST_FAILED','PROOF_INVALID'} else '')
         return 'ORDER_UNKNOWN_'+phase
-    def record_gateway_observation(self,intent_id,candidate_id,result,account):
+    def record_gateway_observation(self,intent_id,candidate_id,result,account,*,preserve_unknown=False):
         row=self.db.execute('SELECT * FROM order_intents WHERE id=?',(intent_id,)).fetchone()
         try:
             intent=self.verified_intent(row)
@@ -254,7 +377,9 @@ class Coordinator:
             receipt=self.db.execute('SELECT * FROM robot_entry_receipts WHERE id=?',(intent['client_order_id'],)).fetchone()
             if receipt and (not filled or receipt['symbol']!=intent['symbol'] or receipt['entry_day']!=entry_day or
                 datetime.fromisoformat(receipt['confirmed_at'])!=entry_at):raise ValueError
-        except Exception:return self.mark_unknown(intent_id,candidate_id,account)
+        except Exception:
+            if preserve_unknown:return self.store.report('NEEDS_REVIEW',account,row['failure_code'] or 'ORDER_OUTCOME_UNKNOWN')
+            return self.mark_unknown(intent_id,candidate_id,account)
         self.db.execute('BEGIN IMMEDIATE')
         try:
             self.db.execute('UPDATE order_intents SET state=?,result=?,failure_code=NULL,updated=? WHERE id=?',
@@ -268,7 +393,8 @@ class Coordinator:
         account=dict(account,bot_entries_today=self.store.entries(day()))
         account['available_slots']=self.execution_slots(account,day())
         return self.store.report(state,account)
-    def allowed(self,today):return not (self.stopping and self.stopping.is_set()) and self.store.settings()['robot_on'] and day()==today
+    def gateway_allowed(self):return not (self.stopping and self.stopping.is_set()) and self.store.settings()['robot_on']
+    def allowed(self,today):return self.gateway_allowed() and day()==today
     def pause_reason(self):
         if self.stopping and self.stopping.is_set():return 'ROBOT_STOPPING'
         return 'ROBOT_OFF' if not self.store.settings()['robot_on'] else 'ROBOT_DAY_CHANGED'
@@ -284,10 +410,12 @@ class Coordinator:
         # provider outcomes still stop the pipeline immediately.
         blocked=advanced if advanced is not None and advanced['bot_status']=='EXECUTION_BLOCKED' else None
         if advanced is not None and blocked is None:return advanced
+        settled=self.settle_previous_research(today,account)
+        if settled is not None:return settled
         available=self.execution_slots(account,today)
         if not available:return blocked or self.store.report('WAITING',account,wait_reason='ROBOT_CAPACITY_FULL')
         # An interrupted paid request cannot be automatically replayed under a new ID.
-        if self.db.execute("SELECT 1 FROM robot_jobs WHERE state IN ('PENDING','NEEDS_REVIEW') LIMIT 1").fetchone() or self.db.execute("SELECT 1 FROM api_requests WHERE state IN ('PENDING','NEEDS_REVIEW') LIMIT 1").fetchone():
+        if self.unresolved_research():
             return self.store.report('REJECTED',account,'ROBOT_REQUEST_NEEDS_REVIEW')
         self.neuro.require_key() # Configuration failure claims no paid operation.
         prefix=today+':robot-v9:'
@@ -316,7 +444,7 @@ class Coordinator:
         results=self.store.results(cycle)
         ready=sum(r['status'] in ('READY_FOR_EXECUTION','EXECUTION_BLOCKED','ENTRY_PENDING','POSITION_PROTECTED','CLOSED') for r in results)
         unsent=self.db.execute("SELECT symbol FROM robot_candidates WHERE status IN ('READY_FOR_EXECUTION','EXECUTION_BLOCKED')")
-        ready_unexposed=sum(r['symbol'] not in account['running_symbols'] for r in unsent)
+        ready_unexposed=sum(r['symbol'] not in self.exposed_symbols(account) for r in unsent)
         technical=sum(r['status']=='REJECTED' and not self.replaceable_result(r) for r in results)
         # Other rejected research opportunities use budget, not a live account slot.
         # Ready intents reserve current free slots; the original target stays fixed.
@@ -347,11 +475,16 @@ class Coordinator:
             self.save_cycle(cycle,data,'COMPLETE')
             return self.store.report('INSUFFICIENT_ACTIONABLE_SETUPS',account,wait_reason='ROBOT_CYCLE_COMPLETE')
         return self.screen(cycle,data,remaining if initial else min(remaining,replaceable),account,today,initial)
-    def claim(self,operation,cycle,kind,symbol,today,target=None):
+    def claim(self,operation,cycle,kind,symbol,today,target=None,*,data=None,context=None):
         self.db.execute('BEGIN IMMEDIATE')
         try:
             if not self.allowed(today):self.db.execute('ROLLBACK');return False
+            witness=self.context_witness(context,symbol) if kind=='ANALYSIS' else None
             self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',(operation,cycle,kind,symbol,'PENDING',target))
+            if witness is not None:
+                if not isinstance(data,dict) or not data['queue'] or data['queue'][0]!=symbol:raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
+                data.setdefault('analysis_contexts',{})[operation]=witness
+                self.save_cycle(cycle,data)
             self.db.execute('COMMIT');return True
         except BaseException:self.db.execute('ROLLBACK');raise
     def unclaim_unsent_research(self,operation):
@@ -360,8 +493,18 @@ class Coordinator:
     def screen(self,cycle,data,count,account,today,initial):
         if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
         catalog=self.market.catalog();prompt,schema=screening_contract(count)
+        if not initial:prompt=REPLACEMENT_SCREENING.format(count=count)
         index=data['screen']+1;operation=cycle+':screening:'+str(index)+':'+str(count)
-        if not self.claim(operation,cycle,'SCREENING',None,today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
+        # A paid response received before a crash is drained without rebuilding
+        # its historical request body or sending another provider request.
+        matching=list(self.db.execute('SELECT operation FROM robot_jobs WHERE cycle=? AND kind=? AND operation IN (?,?)',
+            (cycle,'SCREENING',cycle+':screening:'+str(index)+':1',cycle+':screening:'+str(index)+':2')))
+        if len(matching)>1:return self.store.report('REJECTED',account,'ROBOT_REQUEST_NEEDS_REVIEW')
+        if matching:
+            operation=matching[0]['operation'];count=int(operation.rsplit(':',1)[1]);_,schema=screening_contract(count)
+        try:cached=self.completed_research(operation,cycle,'SCREENING',None)
+        except Exception:return self.store.report('REJECTED',account,'ROBOT_REQUEST_NEEDS_REVIEW')
+        if not cached and not self.claim(operation,cycle,'SCREENING',None,today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
         self.store.report('SCREENING',account)
         if not self.allowed(today):
             self.unclaim_unsent_research(operation)
@@ -370,8 +513,9 @@ class Coordinator:
             context=None if initial else dict(requested_count=count,
                 excluded_symbols=sorted(set(data['seen'])|self.exposed_symbols(account)|MANUAL_ONLY_SYMBOLS),
                 replacement_instruction='Choose different Binance USD-M USDT perpetual coins that are not in excluded_symbols.')
-            value=self.neuro.ask(operation,prompt,schema,lambda v:selections(v,catalog,count),context,catalog=catalog,
-                continue_if=lambda:self.allowed(today))
+            value=(self.completed_value(cached,lambda v:selections(v,catalog,count)) if cached else
+                self.neuro.ask(operation,prompt,schema,lambda v:selections(v,catalog,count),context,catalog=catalog,
+                    continue_if=lambda:self.allowed(today)))
             coins=selections(value,catalog,count)
             data['screen']=index
             if not initial:data['replacements']+=1
@@ -391,15 +535,24 @@ class Coordinator:
         except ResearchPaused:
             self.unclaim_unsent_research(operation)
             return self.store.report('WAITING',account,wait_reason=self.pause_reason())
-        except Exception:
+        except Exception as error:
             if self.db.in_transaction:self.db.execute('ROLLBACK')
+            if cached and str(error)=='STALE_MARKET_CONTEXT':
+                self.db.execute("UPDATE robot_jobs SET state='COMPLETE' WHERE operation=?",(operation,))
+                data['screen']=index
+                if not initial:data['replacements']+=1
+                data['queue']=[];data['round_symbols']=[]
+                self.save_cycle(cycle,data,'COMPLETE')
+                return self.store.report('REJECTED',account,'STALE_MARKET_CONTEXT')
             self.db.execute("UPDATE robot_jobs SET state='NEEDS_REVIEW' WHERE operation=?",(operation,))
             self.save_cycle(cycle,data,'NEEDS_REVIEW')
             return self.store.report('REJECTED',account,'SCREENING_NEEDS_REVIEW')
     def analyze(self,cycle,data,symbol,account,today):
         if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
         operation=cycle+':'+VERSION+':'+symbol
-        target=self.store.settings()['risk_target_usdt']
+        try:cached=self.completed_research(operation,cycle,'ANALYSIS',symbol)
+        except Exception:return self.store.report('REJECTED',account,'ROBOT_REQUEST_NEEDS_REVIEW')
+        target=cached[1]['risk_target'] if cached else self.store.settings()['risk_target_usdt']
         # Symbol catalog/rules/context are read before the paid call, never guessed.
         try:
             catalog=self.market.catalog()
@@ -412,15 +565,17 @@ class Coordinator:
         except ResearchReadPaused:raise
         except Exception:
             return self.store.report('REJECTED',account,'MARKET_DATA_UNAVAILABLE')
-        if not self.claim(operation,cycle,'ANALYSIS',symbol,today,target):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
+        if not cached and not self.claim(operation,cycle,'ANALYSIS',symbol,today,target,data=data,context=context):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
         self.store.report('ANALYZING',account)
         if not self.allowed(today):
             self.unclaim_unsent_research(operation)
             return self.store.report('WAITING',account,wait_reason=self.pause_reason())
         plan=None;state='REJECTED';reason=None;unknown=False
         try:
-            value=self.neuro.ask(operation,ANALYSIS,SETUP_SCHEMA,lambda v:setup(v,symbol),context,
-                continue_if=lambda:self.allowed(today))
+            validate=lambda v:setup(v,symbol,require_declared_rr=False,require_gross_rr=False)
+            value=(self.completed_value(cached,validate) if cached else
+                self.neuro.ask(operation,ANALYSIS,SETUP_SCHEMA,validate,context,
+                    continue_if=lambda:self.allowed(today)))
             signal=setup(value,symbol)
             if signal.side=='HOLD':state='HOLD'
             else:
@@ -432,7 +587,8 @@ class Coordinator:
                     try:_,refreshed=analysis_context(self.market,symbol,target,self.account_factory())
                     except ResearchReadPaused:pass
                     else:rules=refreshed
-                plan=risk_check(signal,rules,target,reward_risk_policy=REWARD_RISK_POLICY);plan['risk_target_usdt']=target
+                plan=risk_check(signal,rules,target,risk_model=RISK_MODEL,
+                    reward_risk_policy=NORMALIZED_REWARD_RISK_POLICY);plan['risk_target_usdt']=target
                 # New Binance TP/SL use the requested Last Price trigger.
                 # Historical plans remain immutable and retain their own trigger.
                 plan['protection_working_type']='CONTRACT_PRICE'
diff --git a/worker/robot_provenance.py b/worker/robot_provenance.py
index c5e631e..c774a1f 100644
--- a/worker/robot_provenance.py
+++ b/worker/robot_provenance.py
@@ -8,23 +8,46 @@ from .neuroapi import setup

 VERSION='analysis-v9'
 SIZING='deterministic-order-fee-risk-v2'
+SIZING_V3='deterministic-order-fee-slippage-risk-v3'
 OPERATION=r'\d{4}-\d{2}-\d{2}:robot-v9:(?:0|[1-9]\d{0,11}):analysis-v9:'

+def sizing_rules(plan):
+    values=plan['sizing_rules']
+    if not isinstance(values,dict) or set(values)!={field.name for field in rule_fields(Rules)}:raise ValueError
+    return Rules(**{key:(D(value) if isinstance(value,str) and key not in ('fee_source','fee_symbol') else value)
+        for key,value in values.items()})
+
+def source_signal(db,operation,symbol,*,normalized):
+    source=db.execute("SELECT output FROM api_requests WHERE operation=? AND state='COMPLETE'",(operation,)).fetchone()
+    if not source or not source[0]:raise ValueError
+    return setup(json.loads(source[0],parse_float=D),symbol,require_declared_rr=normalized)
+
+def normalized_source(plan,signal):
+    """Keep the original provider TP separate from the worker's execution TP."""
+    if signal.side not in ('LONG','SHORT') or signal.side!=plan['side']:raise ValueError
+    original=plan['tp_normalization']
+    for field,value in [('entry',signal.entry),('sl',signal.sl)]:
+        if number(plan[field])!=value or number(original['model_'+field])!=value:raise ValueError
+    if number(original['model_tp'])!=signal.tp:raise ValueError
+
 def stamp(db,plan,operation):
     try:
         symbol=plan['symbol']
         validated_protection_working_type(plan.get('protection_working_type','MARK_PRICE'))
-        if 'reward_risk_policy' in plan:
-            sizing=plan['sizing_rules']
-            if not isinstance(sizing,dict) or set(sizing)!={field.name for field in rule_fields(Rules)}:raise ValueError
-            rules=Rules(**{key:(D(value) if isinstance(value,str) and key not in ('fee_source','fee_symbol') else value)
-                for key,value in sizing.items()})
-            validate_reward_risk_policy(plan,rules)
+        normalized='risk_model' in plan
+        if normalized:
+            from .core import validate_normalized_plan
+            validate_normalized_plan(plan,sizing_rules(plan),check_fresh=False)
+        elif 'reward_risk_policy' in plan:
+            validate_reward_risk_policy(plan,sizing_rules(plan))
         if plan.get('mode')!='ORDER_INTENT' or not isinstance(symbol,str) or not re.fullmatch(r'[A-Z0-9]{2,18}USDT',symbol):raise ValueError
         if not isinstance(operation,str) or not re.fullmatch(OPERATION+re.escape(symbol),operation):raise ValueError
         previous=plan.get('provenance')
         if previous is not None and (not isinstance(previous,dict) or previous.get('analysis_version')!=VERSION):raise ValueError
-        proof={'analysis_version':VERSION,'execution_sizing_version':SIZING,
+        sizing_version=SIZING_V3 if normalized else SIZING
+        if previous is not None and (normalized or previous.get('execution_sizing_version')==SIZING_V3) and previous.get('execution_sizing_version')!=sizing_version:raise ValueError
+        if normalized:normalized_source(plan,source_signal(db,operation,symbol,normalized=True))
+        proof={'analysis_version':VERSION,'execution_sizing_version':sizing_version,
             'source_type':'NEUROAPI_STRUCTURED','contract_version':'neuroapi-decision-v2',
             'operation':operation,'risk_target_usdt':plan['risk_target_usdt'],
             'payload_sha256':digest({k:v for k,v in plan.items() if k!='provenance'}),
@@ -42,22 +65,26 @@ def verify(db,plan,operation):
         stamp(db,copy,operation)
         if proof!=copy['provenance'] or not proof['request_output_sha256'] or not proof['request_body_sha256']:raise ValueError
         if not re.fullmatch(OPERATION+re.escape(plan['symbol']),operation):raise ValueError
-        source=db.execute("SELECT output FROM api_requests WHERE operation=? AND state='COMPLETE'",(operation,)).fetchone()
-        signal=setup(json.loads(source[0],parse_float=D),plan['symbol'])
+        normalized='risk_model' in plan
+        signal=source_signal(db,operation,plan['symbol'],normalized=normalized)
         if signal.side not in ('LONG','SHORT') or signal.side!=plan['side']:raise ValueError
-        for field,value in [('entry',signal.entry),('tp',signal.tp),('sl',signal.sl)]:
-            if number(plan[field])!=value:raise ValueError
+        if normalized:normalized_source(plan,signal)
+        else:
+            for field,value in [('entry',signal.entry),('tp',signal.tp),('sl',signal.sl)]:
+                if number(plan[field])!=value:raise ValueError
         qty=number(plan['execution_quantity'])
         target=validated_risk_target(plan['risk_target_usdt'])
-        fields=plan['sizing_rules']
-        if not isinstance(fields,dict) or set(fields)!={field.name for field in rule_fields(Rules)}:raise ValueError
-        rules=Rules(**{k:(D(v) if isinstance(v,str) and k not in ('fee_source','fee_symbol') else v) for k,v in fields.items()})
+        rules=sizing_rules(plan)
         for v in (rules.step,rules.minimum,rules.maximum,rules.tick):number(v)
         if not rules.min_notional.is_finite() or rules.min_notional<0:raise ValueError
-        validate_reward_risk_policy(plan,rules)
-        if qty!=maximum_risk_quantity(signal.entry,signal.sl,rules,target,check_fresh=False):raise ValueError
-        costs=risk_costs(signal.entry,signal.tp,signal.sl,qty,rules,symbol=signal.symbol,check_fresh=False)
-        if any(plan.get(key)!=value for key,value in costs.items()):raise ValueError
+        if normalized:
+            from .core import validate_normalized_plan
+            validate_normalized_plan(plan,rules,check_fresh=False)
+        else:
+            validate_reward_risk_policy(plan,rules)
+            if qty!=maximum_risk_quantity(signal.entry,signal.sl,rules,target,check_fresh=False):raise ValueError
+            costs=risk_costs(signal.entry,signal.tp,signal.sl,qty,rules,symbol=signal.symbol,check_fresh=False)
+            if any(plan.get(key)!=value for key,value in costs.items()):raise ValueError
         if qty!=number(plan['quantity']) or not 0<number(plan['risk'])<=target:raise ValueError
         if (plan['mode'],plan['margin_mode'],plan['leverage'],plan['order_type'])!=('ORDER_INTENT','CROSS',75,'LIMIT'):raise ValueError
         verified_rr(plan)
diff --git a/worker/robot_store.py b/worker/robot_store.py
index 07b0634..50033b4 100644
--- a/worker/robot_store.py
+++ b/worker/robot_store.py
@@ -82,11 +82,15 @@ class RobotStore:
     def entries(self, today):
         return self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts WHERE entry_day=?', (today,)).fetchone()[0]

-    def available_slots(self, running, symbols, today):
+    def available_slots(self, running, symbols, today, *, pending_symbols=()):
         """One capacity rule for submission, screening, and read-only Office."""
         active=list(self.db.execute("SELECT symbol,state FROM order_intents WHERE state IN ('ENTRY_PENDING','POSITION_PROTECTED')"))
         pending=sum(row['state']=='ENTRY_PENDING' for row in active)
-        unrepresented=sum(row['symbol'] not in symbols for row in active)
+        # A symbol already running or present in Binance open entry orders is
+        # counted once, even when the same bot intent also reserves that symbol.
+        represented=set(symbols)
+        reserved=set(pending_symbols)|{row['symbol'] for row in active}
+        unrepresented=len(reserved-represented)
         return slots(running,self.entries(today),pending=pending,unrepresented=unrepresented)

     def results(self, cycle):
@@ -112,7 +116,8 @@ class RobotStore:
         value.update(self.settings(), bot_entries_today=self.entries(today), execution_gateway=OrderGateway().status())
         running=value.get('running_positions');symbols=value.get('running_symbols')
         if type(running) is int and running>=0 and isinstance(symbols,list) and not value.get('account_failure_code'):
-            value['available_slots']=self.available_slots(running,symbols,today)
+            value['available_slots']=self.available_slots(running,symbols,today,
+                pending_symbols=value.get('open_entry_symbols') or ())
         if not value['robot_on']: value.update(bot_status='OFF', wait_reason='ROBOT_OFF')
         row = self.db.execute('SELECT symbol,status,failure_code FROM robot_candidates ORDER BY rowid DESC LIMIT 1').fetchone()
         value['last_decision'] = dict(row) if row else None
@@ -170,8 +175,10 @@ class RobotStore:
         account = value['account']
         if account.get('status') != 'CONNECTED': return
         running = [row['symbol'] for row in account['positions']]
+        pending_symbols=account.get('open_entry_symbols') or ()
         summary = dict(running_positions=account['active_positions'], running_symbols=running,
-            manual_exposure=sorted(set(running)), available_slots=self.available_slots(account['active_positions'],running,day()),
+            open_entry_symbols=list(pending_symbols),manual_exposure=sorted(set(running)),
+            available_slots=self.available_slots(account['active_positions'],running,day(),pending_symbols=pending_symbols),
             usdt_wallet_balance=account['usdt_wallet_balance'], usdt_available_balance=account['usdt_available_balance'],
             account_checked_at=account['checked_at'], **{key: account[key] for key in ('_account_generation', '_account_revision') if key in account})
         self.report_account(summary)
HARUN_RUNTIME_V2_PATCH
HARUN_OLD_MAP='{"deploy/container_boot.py":"2d44b5fab2c21db1f63ba7755b196a226fbca841de0d40465b54d770dcc3a141","deploy/runtime_permissions.py":"26a8748c695cc033a5845c87955dfb68ffa2264f001e2823548737a7517661f7","worker/__init__.py":"6840e3fa91a57fe1f618995e8abcf93a1b180ddf3f76e200d17bfb079b184c1d","worker/__main__.py":"8765c72e75140f18abbc96bc404d529fa069665faa5fb4d2880e8e071e140dea","worker/account_state.py":"56361e40a360ddccc2128bb2036dc0133cc8a9415b0f70f6f854e20cf1b3eba5","worker/analysis.py":"1777b60c663fd971969b526a7c2c785eed3cd057d0edc70fb55453377428b97e","worker/api_service.py":"9970187caffa5a2166e6c068566613850796a5c7ba872d506ae910427893b6bc","worker/binance_office.py":"6bbd83ba7235a72d4dd2e3a910a06b584616f88786db8db3ad6912c357455457","worker/binance_private.py":"b9db1d0161b23f30312571ba6d810ea950dbb2bbab8237b7ab4107883e24689c","worker/core.py":"4b08349cc49470c47d2d29f0e6e2d72e086356f42d90b1afab92f98a4f0c6b5e","worker/diagnostics.py":"7d2754c5ce0f3badb8b826db6a14705e1d204af75ac22e64f11c0a677f85ba19","worker/health.py":"c06f003b0b664c504cbe3653518f1155922046263bf8085b5f7ac52d45fc5cd8","worker/http_client.py":"1b25657ce3d058c23d00f0294c8272b945e4662164899d9aab80fb2330c17eb9","worker/market.py":"645240f560ad4c12982b4aea348e38e0617eb086a7f7d093e0d9e54f43f6eb15","worker/migration.py":"36f44069fefa98f7eae54ad7662ff02280345d66a926acbac470f4f202c23ed6","worker/neuroapi.py":"23f78304f76b85af4c98bf9e8eff96a4e04572b0e3aa61a8b1874f008b00c3c5","worker/neuroapi_request.py":"a98f0dc33d5ddf69fed1b64e04df30ce8ed03b0624e113ab1627b56f8d791bbb","worker/order_gateway.py":"4cd0be4f03659ef6eb75193f261465fb33519b0602d51a4a9c7f3865d0153049","worker/prompts.py":"c3119f7e00d228bcd9980a6e9104948947ed819b90b173d615ae6872944b90b5","worker/provenance.py":"894d27ac87d670767bf289add2708438ac4d1ed12d3c4e2a9dc44c737ea38481","worker/research_guard.py":"0296065b17f2cef48e17483301293d9bc0908d470e4d4e1f291ca28b0cd9a1b0","worker/robot.py":"1f0ca77f82d4252bdd74fb4af83f32cff1f3da0cdc72d2faab86b5788a5c4423","worker/robot_provenance.py":"2975732f7f3888147ae50c81b2b53e85c8c0930c47310f88246378a53ceac082","worker/robot_status.py":"41d08815187a25964d95b1802380a5c5f4345523fc5abdd6e7229b74d235ba2f","worker/robot_store.py":"6bd33d25bcea68783ce25eb2a41eb9a4b7e5fccd3588a5aa02623f283c1e2dc1","worker/state.py":"a16c1233b999ce600ad1f610ff2b80038899fe5fbcb2a2af880930bc55860b04"}'
HARUN_NEW_RUNTIME='{"worker/account_state.py":"88ba89b138a6db54ad2a82b4d1bc41531784143b76216f15f58f93b19fcd3ae0","worker/analysis.py":"5494aafd422ff387d786ae6802d4845d1c506ead02a1e921b163c6f31edad03a","worker/binance_office.py":"18af4d4bf9edc05e386e400381af1b4a39c6fb36bb4d49d3a45b24f7f78116b2","worker/core.py":"2c694f4cb7916aa7205d79a26cb426ae36a9098196bfe26dd9faadc9b0fe2166","worker/diagnostics.py":"761d32450c952d600d9747d0493efb2055a50476ff3ae2bae9d6208537ddf7bc","worker/neuroapi.py":"773bd2a4194adcbfb95f49b1558ce3ee8405d1c55284b7296049404193beb8d0","worker/prompts.py":"15872f85e63ed8aeb23c6d0ee9c125a2d92232e17c68d913009edcae69b27f45","worker/research_guard.py":"1f0d275ab51a59322fc6c67c173a4a1e9094c4cdd6d1fb4e2236a9fbc73e0b1e","worker/robot.py":"84e0ce64ec288fc2985c8b7f7993e57d5953658136b4580dd067d9bb01babb94","worker/robot_provenance.py":"7c03ec7400b9c29bb47bf0ccf44d7c3661457116499a3cf4388b1b0be25fd725","worker/robot_store.py":"688b5114ec21e575907eae039eb99b9b87636e37ef116c5872a8086e4c91d994"}'
HARUN_CLEANUP_MAP='{"deploy/LEGACY_RESEARCH_RECOVERY.md":"257ed5b15147d88e1e7e93e75eecb7f11ac9997bc4431fea5b13d315bad77f5e","deploy/ONE_SLOT_RECOVERY.md":"1edb8234f9b18766b45345e0d8b45b769e6bf90965b8830f1bcfd202d694b7fc","deploy/RR_REPLACEMENT.md":"554ff6273b90b64fa185ccb5c3f451d740f916f1a535caacc72c45b89fa7930e","deploy/UNKNOWN_ORDER_RECOVERY.md":"c591b01a5cd21d5dde40ac26cad03cc8aee4ab927830007f752724c1825570e8","deploy/VPS_ORDER_FIX.md":"b07d740effa452df117b2e96e4f58f152c522196b1daa6e7d0f88a7fe53308ae","deploy/apply_neuroapi_422_fix.sh":"55270bf5f52dc34943551562b4db65774c32dc67418571ed8bf223817001d392","deploy/apply_rr_replacement.sh":"7b69ebf59f6f944b07cd2f6c00f25050a876d99b4b906838126817e2de3f45ec","deploy/apply_vps_order_fix.sh":"f1f711f135b99492ac3d700096e4971c062328d51a4d6325e9b49983f40f5b04","deploy/patch_exit_cancel_readback.py":"6d61b15721d3a510e3b3e64e464d768bc1e5cde8800686aa804d5af0c524f855","deploy/patch_gateway_diagnostics.py":"4400373549c71e2b8c9a83f097cd27abd41fabce59a276b726db4872a4030f8a","deploy/record_eth_closed.py":"79ad238607f3599fd252568591a27eb991f3007714e67473a384f253bad7d5a1","deploy/record_eth_protection.py":"22ab7dbe2d16985798400a2dd3f6704dfbfd14c3844554ece5f77365ece46058","deploy/recover_absent_entry.py":"fb856de55f7a27081add1c8254b919aa3e1de8f66a0b0b21a1acd45dcb8756cc","deploy/recover_eth_take_profit.py":"92a962681367f48525986d429ea357c6dd63e8b7059f1984745e49e6b5624105","deploy/retire_legacy_research.py":"5b41bbfc8cc9ab6ca2cfa3b435ff0c3555c42fb5ed838a7ed66cf4575718a14d","deploy/seed_one_slot.py":"722798284678f43e55f1f7d515e9ecad1be6eee938f97c670a765feea8065e97","deploy/settle_sol_request_422.py":"447e07d7901e489bdb74cb7f8a302b082e7eeddb9c398b19f37bcf7c0bcacab3"}'

harun_check_helpers() {
  python3 -I -B -S - "$HARUN_BACKUP" <<'HARUN_HELPER_HASH'
import hashlib,sys
from pathlib import Path
root=Path(sys.argv[1])
for name,expected in {'audit.py': '3c9c792b111ea57a01653e660d0f88366ca5dd2df15e3fcdff2b0f9688541c3f', 'sdk_patch.py': 'c8bb6b301def714ad5834a82db0e3dd693293240a7136bdf4f6e3d07d972f5f8'}.items():
    p=root/name
    assert p.is_file() and not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest()==expected, 'UPDATE_HELPER_CHANGED'
HARUN_HELPER_HASH
}
harun_read_audit() {
  local HARUN_MAP="$1" HARUN_OUTPUT="$2"
  shift 2
  harun_check_helpers || return 1
  if ! docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B - --source-hashes-json "$HARUN_MAP" --source-root /app "$@" < "$HARUN_BACKUP/audit.py" > "$HARUN_OUTPUT"; then
    cat "$HARUN_OUTPUT" || true
    return 1
  fi
}
harun_check_helpers
python3 -I -B -S - "$HARUN_OLD_MAP" "$HARUN_BACKUP/audit.py" <<'HARUN_INITIAL_SOURCE_CHECK'
import json,sys
from pathlib import Path
namespace={'__name__':'source_check'}
exec(compile(Path(sys.argv[2]).read_text(),'audit.py','exec'),namespace)
namespace['verify_sources'](json.loads(sys.argv[1]),'.')
print('HOST_SOURCE_MATCH')
HARUN_INITIAL_SOURCE_CHECK
harun_check_build_configuration
printf '%s\n' "$HARUN_BUILD_MAP" > "$HARUN_BACKUP/build.before.json"
docker compose -f compose.yaml config --quiet
harun_read_audit "$HARUN_OLD_MAP" "$HARUN_BACKUP/before.json" --backup --exchange
HARUN_BEFORE="$(cat "$HARUN_BACKUP/before.json")"
cat "$HARUN_BACKUP/before.json"
HARUN_STAGE=SDK_INSPECTION
if ! python3 -I -B -S "$HARUN_BACKUP/sdk_patch.py" worker/order_gateway.py > "$HARUN_BACKUP/sdk.inspection.json"; then
  cat "$HARUN_BACKUP/sdk.inspection.json" || true
  exit 1
fi
HARUN_NEW_MAP="$(python3 -I -B -S - "$HARUN_OLD_MAP" "$HARUN_NEW_RUNTIME" "$HARUN_BACKUP/sdk.inspection.json" <<'HARUN_NEW_V2_MAP'
import json,re,sys
from pathlib import Path
values=json.loads(sys.argv[1]);values.update(json.loads(sys.argv[2]))
proof=json.loads(Path(sys.argv[3]).read_text())
assert proof['status']=='PATCH_READY' and proof['before_sha256']=='4cd0be4f03659ef6eb75193f261465fb33519b0602d51a4a9c7f3865d0153049' and re.fullmatch(r'[a-f0-9]{64}',proof['after_sha256']), 'SDK_PATCH_NOT_VERIFIED'
values['worker/order_gateway.py']=proof['after_sha256']
print(json.dumps(values,sort_keys=True))
HARUN_NEW_V2_MAP
)"
printf '%s\n' "$HARUN_OLD_MAP" > "$HARUN_BACKUP/source.before.json"
printf '%s\n' "$HARUN_NEW_MAP" > "$HARUN_BACKUP/source.after.json"
python3 -I -B -S - "$HARUN_OLD_MAP" "$HARUN_NEW_MAP" "$HARUN_BACKUP" <<'HARUN_BACKUP_SOURCES'
import hashlib,json,shutil,sys
from pathlib import Path
old,new=json.loads(sys.argv[1]),json.loads(sys.argv[2]);backup=Path(sys.argv[3])/'source-before'
for name in sorted(set(old)|set(new)):
    if old.get(name)==new.get(name):continue
    target=backup/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    source=Path(name)
    if name in old:
        assert hashlib.sha256(source.read_bytes()).hexdigest()==old[name], 'SOURCE_CHANGED_BEFORE_BACKUP'
        shutil.copy2(source,target)
        assert hashlib.sha256(target.read_bytes()).hexdigest()==old[name], 'SOURCE_BACKUP_FAILED'
    else:
        assert not source.exists() and not source.is_symlink(), 'NEW_SOURCE_ALREADY_PRESENT'
print('SOURCE_BACKUPS_VERIFIED')
HARUN_BACKUP_SOURCES
HARUN_OLD_IMAGE="$(docker inspect --format '{{.Image}}' harun-office-worker-1)"
HARUN_IMAGE_BACKUP="harun-office-worker:before-workflow-v2-$(date -u +%Y%m%dT%H%M%SZ)"
docker image tag "$HARUN_OLD_IMAGE" "$HARUN_IMAGE_BACKUP"
printf 'Backup: %s\nImage backup: %s\n' "$HARUN_BACKUP" "$HARUN_IMAGE_BACKUP"
harun_restore_source_image() {
  python3 -I -B -S - "$HARUN_BACKUP" <<'HARUN_V2_RESTORE_SOURCE' || return 1
import hashlib,json,os,shutil,stat,sys
from pathlib import Path
backup=Path(sys.argv[1]);old=json.loads((backup/'source.before.json').read_text());new=json.loads((backup/'source.after.json').read_text())
changed=[n for n in sorted(set(old)|set(new)) if old.get(n)!=new.get(n)]
# Validate the entire restore set before touching any file. Keep concurrent edits.
for name in changed:
    path=Path(name)
    if os.path.lexists(path):
        info=path.lstat()
        assert stat.S_ISREG(info.st_mode) and info.st_nlink==1 and hashlib.sha256(path.read_bytes()).hexdigest() in {v for v in (old.get(name),new.get(name)) if v}, 'SOURCE_CHANGED_DURING_UPDATE'
    else:
        assert name not in old, 'ORIGINAL_SOURCE_MISSING'
    if name in old:
        saved=backup/'source-before'/name
        assert saved.is_file() and not saved.is_symlink() and hashlib.sha256(saved.read_bytes()).hexdigest()==old[name], 'SOURCE_BACKUP_CHANGED'
for name in changed:
    path=Path(name)
    if name in old:shutil.copy2(backup/'source-before'/name,path)
    elif os.path.lexists(path):path.unlink()
print('SOURCE_RESTORED')
HARUN_V2_RESTORE_SOURCE
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
  printf 'WORKFLOW_UPDATE_NOT_COMPLETED stage=%s backup=%s\n' "$HARUN_STAGE" "$HARUN_BACKUP"
  exit "$HARUN_STATUS"
}
HARUN_RESTORE_READY=1
trap 'harun_abort "$?"' ERR
trap 'harun_abort 130' INT
trap 'harun_abort 143' TERM
HARUN_STAGE=SDK_SOURCE_PATCH
harun_check_helpers
if ! python3 -I -B -S "$HARUN_BACKUP/sdk_patch.py" worker/order_gateway.py --apply > "$HARUN_BACKUP/sdk.applied.json"; then
  cat "$HARUN_BACKUP/sdk.applied.json" || true
  false
fi
python3 -I -B -S - "$HARUN_BACKUP/sdk.inspection.json" "$HARUN_BACKUP/sdk.applied.json" <<'HARUN_V2_SDK_PROOF'
import json,sys
from pathlib import Path
before=json.loads(Path(sys.argv[1]).read_text());after=json.loads(Path(sys.argv[2]).read_text())
assert after['status']=='PATCHED' and all(after[k]==before[k] for k in ('before_sha256','after_sha256')), 'SDK_PATCH_NOT_VERIFIED'
print(json.dumps(after,sort_keys=True))
HARUN_V2_SDK_PROOF
HARUN_STAGE=SOURCE_PATCH
git apply --check "$HARUN_BACKUP/runtime.patch"
git apply "$HARUN_BACKUP/runtime.patch"
python3 -I -B -S - "$HARUN_NEW_MAP" "$HARUN_BACKUP/audit.py" <<'HARUN_V2_COMPILE'
import json,sys
from pathlib import Path
namespace={'__name__':'source_check'};exec(compile(Path(sys.argv[2]).read_text(),'audit.py','exec'),namespace)
expected=json.loads(sys.argv[1]);namespace['verify_sources'](expected,'.')
for name in expected:compile(Path(name).read_bytes(),name,'exec')
print('SOURCE_COMPILE_VERIFIED')
HARUN_V2_COMPILE
HARUN_STAGE=BUILD
harun_check_build_configuration
docker compose -f compose.yaml build worker
HARUN_NEW_IMAGE="$(docker image inspect --format '{{.Id}}' harun-office-worker:latest)"
HARUN_STAGE=OFFLINE_IMAGE_CHECK
docker run --rm -i --network none --read-only --user 10001:10001 --entrypoint python "$HARUN_NEW_IMAGE" -I -B - "$HARUN_NEW_MAP" <<'HARUN_V2_OFFLINE_IMAGE'
import hashlib,json,stat,sys,time
from pathlib import Path
root=Path('/app');expected=json.loads(sys.argv[1]);paths=list((root/'worker').rglob('*.py'))+[root/'deploy/container_boot.py',root/'deploy/runtime_permissions.py']
assert set(expected)=={str(p.relative_to(root)) for p in paths}, 'IMAGE_SOURCE_INVENTORY_CHANGED'
for name,sha in expected.items():
    p=root/name;info=p.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_nlink==1 and hashlib.sha256(p.read_bytes()).hexdigest()==sha, 'IMAGE_SOURCE_MISMATCH'
    compile(p.read_bytes(),name,'exec')
sys.path.insert(0,'/app')
from worker.core import D,Rules,Signal,FEE_SOURCE,RISK_MODEL,NORMALIZED_REWARD_RISK_POLICY,risk_check,validate_normalized_plan
stamp=time.time();rules=Rules(D('.001'),D('.001'),D('1000'),D('.01'),D('1'),stamp,taker_fee_rate=D('.0005'),fee_observed_at=stamp,fee_symbol='ETHUSDT',fee_source=FEE_SOURCE)
for side,model_tp,sl,tp,quantity,risk in [('LONG','2730','2585','2707.23','.123','4.9834726125'),('SHORT','2490','2635','2513.25','.122','4.978098675')]:
    plan=risk_check(Signal('ETHUSDT',side,D('2610'),D(model_tp),D(sl)),rules,risk_model=RISK_MODEL,reward_risk_policy=NORMALIZED_REWARD_RISK_POLICY)
    assert D(plan['tp'])==D(tp) and D(plan['execution_quantity'])==D(quantity) and D(plan['risk'])==D(risk), 'IMAGE_RISK_CONTRACT_INVALID'
    assert D(plan['tp_normalization']['model_tp'])==D(model_tp) and D('2')<=D(plan['net_rr'])<D('2.001'), 'IMAGE_RR_CONTRACT_INVALID'
    validate_normalized_plan(plan,rules)
print('OFFLINE_IMAGE_VERIFIED')
HARUN_V2_OFFLINE_IMAGE
HARUN_STAGE=SECOND_PREFLIGHT
harun_check_build_configuration
harun_read_audit "$HARUN_OLD_MAP" "$HARUN_BACKUP/second.json" --before-json "$HARUN_BEFORE"
HARUN_STAGE=GRACEFUL_RESTART
HARUN_STARTED="$(python3 -I -B -S -c 'import time;print(time.time())')"
HARUN_WORKER_STOPPED=1
docker compose -f compose.yaml stop -t 660 worker
docker compose -f compose.yaml up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 worker
# Once healthy, keep the functional runtime on a diagnostic refusal. Financial
# journals and settings are never rolled back, and the installer never turns ON.
HARUN_RESTORE_READY=0
HARUN_STAGE=FRESH_OFF_STATUS
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - "$HARUN_STARTED" <<'HARUN_V2_FRESH_OFF'
import json,sqlite3,sys,time
from datetime import datetime
cutoff=float(sys.argv[1]);deadline=time.monotonic()+60
while True:
    db=sqlite3.connect('file:/data/trading/ledger.sqlite3?mode=ro',uri=True);db.execute('PRAGMA query_only=ON')
    assert db.execute('SELECT enabled FROM robot_settings WHERE id=1').fetchall()==[(0,)], 'ROBOT_OFF_REQUIRED'
    row=db.execute('SELECT data FROM robot_status WHERE id=1').fetchone();db.close()
    data=json.loads(row[0]) if row else {};value=data.get('checked_at')
    stamp=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else None
    if stamp and stamp.tzinfo and stamp.timestamp()>=cutoff and data.get('bot_status')=='OFF' and data.get('wait_reason')=='ROBOT_OFF' and data.get('failure_code') is None:
        print('FRESH_ROBOT_OFF_VERIFIED');break
    assert time.monotonic()<deadline, 'FRESH_OFF_STATUS_REQUIRED'
    time.sleep(1)
HARUN_V2_FRESH_OFF
HARUN_STAGE=FINAL_OFF_JOURNAL_CHECK
harun_check_build_configuration
harun_read_audit "$HARUN_NEW_MAP" "$HARUN_BACKUP/verified.json" --before-json "$HARUN_BEFORE" --fresh-after "$HARUN_STARTED" --exchange
cat "$HARUN_BACKUP/verified.json"
HARUN_STAGE=ARCHIVE_OBSOLETE_ROBOT_HELPERS
python3 -I -B -S - "$HARUN_CLEANUP_MAP" "$HARUN_BACKUP" <<'HARUN_V2_ARCHIVE_OBSOLETE'
import hashlib,json,os,stat,sys
from pathlib import Path
allowed=json.loads(sys.argv[1]);backup=Path(sys.argv[2]);archive=backup/'obsolete-robot-source';archive.mkdir(mode=0o700)
moved=[];skipped=[]
try:
    for name,sha in sorted(allowed.items()):
        path=Path(name)
        assert name.startswith('deploy/') and len(path.parts)==2, 'CLEANUP_ALLOWLIST_INVALID'
        if not os.path.lexists(path):skipped.append(dict(file=name,reason='ABSENT'));continue
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:
            skipped.append(dict(file=name,reason='SOURCE_CHANGED'));continue
        assert stat.S_ISDIR(path.parent.lstat().st_mode), 'CLEANUP_DIRECTORY_INVALID'
        target=archive/name;target.parent.mkdir(mode=0o700,exist_ok=True)
        os.rename(path,target);moved.append(dict(file=name,sha256=sha))
    manifest=dict(status='OBSOLETE_ROBOT_SOURCE_ARCHIVED',archive=str(archive),archived=moved,preserved=skipped)
    (archive/'manifest.json').write_text(json.dumps(manifest,sort_keys=True)+'\n')
    print(json.dumps(manifest,sort_keys=True))
except BaseException:
    for item in reversed(moved):
        path=Path(item['file']);target=archive/item['file']
        assert not os.path.lexists(path), 'CLEANUP_RESTORE_REFUSED'
        os.rename(target,path)
    raise
HARUN_V2_ARCHIVE_OBSOLETE
printf '%s\n' VPS_ROBOT_WORKFLOW_V2_VERIFIED
)
