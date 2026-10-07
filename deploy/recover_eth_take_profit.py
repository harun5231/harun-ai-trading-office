"""Case-bound VPS maintenance: verify an existing ETH fill and repair only its TP."""
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl

GATEWAY_SHA = '7714469daa3c529ee228d8e049cea3b3a52484a46a8429b2b31e9e233573c422'
ROBOT_SHA = '4bb72c65d8eb3bada7c3739058e270cf648d129de64d2042ecee90a4627bbbef'
PINS = dict(symbol='ETHUSDT', client='hao-ce5286d37dcdca54a2f1174a2c48',
            entry_id='8389766291635748850', quantity='0.181', price='2610',
            sl='2585', tp='2730', sl_id='4000001952621457')

def require(value, reason):
    if not value:
        raise ValueError(reason)

def repair(g, intent, pins, enabled):
    """Use the reviewed lifecycle, with one exact TP POST and no entry/cancellation."""
    require(intent['symbol'] == pins['symbol'] and intent['client_order_id'] == pins['client'], 'INTENT_CHANGED')
    for actual, expected in ((intent['entry']['quantity'], pins['quantity']),
                             (intent['entry']['price'], pins['price']),
                             (intent['protection']['stop_loss'], pins['sl']),
                             (intent['protection']['take_profit'], pins['tp'])):
        require(Decimal(actual) == Decimal(expected), 'SETUP_CHANGED')
    require(intent['entry']['side'] == 'BUY' and intent['protection']['exit_side'] == 'SELL', 'SIDE_CHANGED')
    quantity = Decimal(pins['quantity'])
    client = g._algo_id(intent, 'tp', quantity)
    allowed = dict(algoType='CONDITIONAL', symbol=pins['symbol'], side='SELL', positionSide='BOTH',
                   type='TAKE_PROFIT_MARKET', quantity=g._quantity_text(quantity), reduceOnly='true',
                   triggerPrice=intent['protection']['take_profit'], workingType='MARK_PRICE',
                   clientAlgoId=client, newOrderRespType='ACK')
    original_request, original_wire = g._request, g._wire
    claim, sent = [], []

    def request(method, path, params):
        if method != 'GET':
            require(method == 'POST' and path == '/fapi/v1/algoOrder' and params == allowed, 'OTHER_WRITE_BLOCKED')
            require(not claim and enabled(), 'TP_POST_BLOCKED')
            claim.append(True)  # Claim before transport: no retry after uncertain acceptance.
        return original_request(method, path, params)

    def wire(method, path, query='', signed=False):
        if method != 'GET':
            pairs = parse_qsl(query, keep_blank_values=True)
            values = dict(pairs)
            require(method == 'POST' and path == '/fapi/v1/algoOrder' and signed is True and
                    claim and not sent and len(pairs) == len(values), 'OTHER_WRITE_BLOCKED')
            require(re.fullmatch(r'[a-f0-9]{64}', values.pop('signature', '')) and
                    values.pop('timestamp', '').isdigit() and values.pop('recvWindow', '') == '5000' and
                    values == allowed and enabled(), 'TP_PARAMETERS_CHANGED')
            sent.append(True)
        try:
            return original_wire(method, path, query, signed)
        except Exception as error:
            if method == 'POST':
                code = getattr(error, 'code', None)
                print('TP_POST_ERROR', json.dumps(dict(binance_code=code if type(code) is int else None,
                                                       error_type=type(error).__name__)))
            raise

    g._request, g._wire = request, wire
    try:
        with g._operation():
            g._validate_intent(intent)
            entry = g._entry(intent)
            require(entry['status'] == 'FILLED' and g._id(entry['orderId']) == pins['entry_id'] and
                    Decimal(entry['executedQty']) == quantity, 'FILL_CHANGED')
            sl = g._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': g._algo_id(intent, 'sl', quantity)})
            g._algo_proof(intent, 'sl', quantity, sl, active=True)
            require(g._id(sl['algoId']) == pins['sl_id'], 'SL_CHANGED')
            result = g.reconcile(intent)  # Ownership, actual fills, SL/TP, fresh proofs.
            require(result['state'] == 'POSITION_PROTECTED' and result['order_id'] == pins['entry_id'] and
                    result['sl_order_id'] == pins['sl_id'] and Decimal(result['filled_quantity']) == quantity and
                    result['sl_confirmed'] is True and result['tp_confirmed'] is True, 'PROTECTION_NOT_VERIFIED')
            return result
    finally:
        g._request, g._wire = original_request, original_wire

def record(db, coordinator, row, result, account):
    require(dict(db.execute('SELECT * FROM order_intents WHERE id=?', (row['id'],)).fetchone()) == dict(row), 'JOURNAL_CHANGED')
    candidate = db.execute('SELECT status,failure_code FROM robot_candidates WHERE id=?', (row['candidate_id'],)).fetchone()
    require(candidate and candidate['status'] == row['state'] and candidate['failure_code'] == row['failure_code'], 'CANDIDATE_CHANGED')
    report_failed = False
    try:
        coordinator.record_gateway_observation(row['id'], row['candidate_id'], result, account)
    except Exception:
        # A final Office report can fail after the transaction committed.
        # Inspect the actual journal; never repeat a TP POST to fix reporting.
        report_failed = True
    current = db.execute('SELECT state,result,failure_code FROM order_intents WHERE id=?', (row['id'],)).fetchone()
    receipt = db.execute('SELECT confirmed_at FROM robot_entry_receipts WHERE id=?', (result['client_order_id'],)).fetchone()
    candidate = db.execute('SELECT status,failure_code FROM robot_candidates WHERE id=?', (row['candidate_id'],)).fetchone()
    require(current['state'] == 'POSITION_PROTECTED' and current['failure_code'] is None and receipt and
            json.loads(current['result']) == result and candidate['status'] == 'POSITION_PROTECTED' and
            candidate['failure_code'] is None and receipt['confirmed_at'] == result['first_fill_at'], 'JOURNAL_NOT_VERIFIED')
    return report_failed

def main():
    stage = 'SOURCE_CHECK'
    db = lock = None
    try:
        for name, expected in (('order_gateway.py', GATEWAY_SHA), ('robot.py', ROBOT_SHA)):
            path = Path('/app/worker') / name
            require(not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == expected, 'SOURCE_CHANGED')
        from worker.robot import Coordinator
        from worker.robot_store import RobotStore
        from worker.order_gateway import OrderGateway
        from worker.binance_private import BinanceReadOnly
        from worker.account_state import account_state
        from worker.core import day
        trading = Path('/data/trading')
        info = (trading / 'ledger.sqlite3').lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                info.st_uid == os.geteuid() == 10001 and not info.st_mode & 0o077, 'JOURNAL_NOT_PRIVATE')
        stage = 'LOCK'
        lock = os.open(trading / 'cycle.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid(), 'LOCK_INVALID')
        deadline = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, 'WORKER_BUSY')
                time.sleep(.2)
        stage = 'JOURNAL_CHECK'
        db = sqlite3.connect('file:/data/trading/ledger.sqlite3?mode=rw', uri=True, isolation_level=None, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA trusted_schema=OFF')
        require(not db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone(), 'JOURNAL_TRIGGER_PRESENT')
        rows = db.execute("SELECT * FROM order_intents WHERE symbol='ETHUSDT'").fetchall()
        require(len(rows) == 1 and rows[0]['state'] in ('NEEDS_REVIEW', 'POSITION_PROTECTED'), 'ORDER_STATE_CHANGED')
        row = rows[0]
        require(row['failure_code'] == 'BINANCE_ORDER_PROTECTION_OUTCOME_UNKNOWN' if row['state'] == 'NEEDS_REVIEW'
                else row['failure_code'] is None, 'ORDER_FAILURE_CHANGED')
        require(not db.execute("SELECT 1 FROM order_intents WHERE id<>? AND state IN ('SUBMITTING','NEEDS_REVIEW')", (row['id'],)).fetchone(), 'OTHER_ORDER_UNRESOLVED')
        candidate = db.execute('SELECT status,failure_code FROM robot_candidates WHERE id=?', (row['candidate_id'],)).fetchone()
        require(candidate and candidate['status'] == row['state'] and candidate['failure_code'] == row['failure_code'], 'CANDIDATE_CHANGED')
        coordinator = Coordinator.__new__(Coordinator)
        coordinator.db, coordinator.store = db, RobotStore(db, initialize=False)
        intent = coordinator.verified_intent(row)
        require(intent['client_order_id'] == PINS['client'], 'INTENT_CHANGED')
        enabled = lambda: db.execute('SELECT enabled FROM robot_settings WHERE id=1').fetchone()[0] == 1
        require(enabled(), 'ROBOT_ON_REQUIRED_FOR_PROTECTION_REPAIR')
        stage = 'BACKUP'
        folder = Path(tempfile.mkdtemp(prefix='eth-tp-', dir=trading / 'maintenance'))
        folder.chmod(0o700)
        backup = folder / 'ledger.before.sqlite3'
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(backup) as saved:
            db.backup(saved)
            require(saved.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'BACKUP_INVALID')
        print('BACKUP', str(backup))
        stage = 'ACCOUNT_CHECK'
        account = account_state(BinanceReadOnly(), coordinator.store, day())
        stage = 'PROTECTION_REPAIR'
        result = repair(OrderGateway(), intent, PINS, enabled)
        stage = 'JOURNAL_RECORD'
        report_failed = record(db, coordinator, row, result, account)
        print(json.dumps(dict(status='ETH_TP_RECOVERY_VERIFIED', symbol=PINS['symbol'], quantity=PINS['quantity'],
                             sl=PINS['sl'], tp=PINS['tp'], sl_order_id=result['sl_order_id'],
                             tp_order_id=result['tp_order_id'], entry_receipt=True,
                             report_refresh_pending=report_failed), sort_keys=True))
        return 0
    except Exception as error:
        code = str(error)
        print(json.dumps(dict(status='RECOVERY_NOT_COMPLETED', stage=stage,
                              reason=code if re.fullmatch(r'[A-Z_]{1,100}', code) else type(error).__name__)))
        return 1
    finally:
        if db is not None:
            db.close()
        if lock is not None:
            os.close(lock)

if __name__ == '__main__':
    raise SystemExit(main())
