"""Read-only fill evidence for the daily quota of known carry positions.

The original first-fill receipts remain unchanged. A position present at 00:00
WIB consumes one quota unit for that whole day, including after it closes.
Only current or previously tracked symbols are inspected. Exchange observations
are quiescent GET witnesses, not an atomic Binance account snapshot.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from fractions import Fraction

from .core import Review

WIB = timezone(timedelta(hours=7))
SYMBOL = re.compile(r'[A-Z0-9_]{2,30}')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS robot_carry_symbols(symbol TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS robot_daily_carry(
 day TEXT NOT NULL,symbol TEXT NOT NULL,carried INTEGER NOT NULL CHECK(carried IN(0,1)),
 observed_ms INTEGER NOT NULL,evidence_sha256 TEXT NOT NULL,PRIMARY KEY(day,symbol));
'''


def need(value):
    if not value:
        raise Review('ROBOT_CARRY_QUOTA_UNVERIFIED')


def quantity(value):
    need(type(value) is str and len(value) <= 64 and
         re.fullmatch(r'-?\d+(?:\.\d+)?', value) is not None)
    return Fraction(value)


def positions(rows):
    need(type(rows) is list)
    result = {}
    for row in rows:
        need(type(row) is dict and type(row.get('symbol')) is str and
             SYMBOL.fullmatch(row['symbol']) is not None)
        amount = quantity(row.get('positionAmt'))
        if amount == 0:
            continue
        need(row.get('positionSide') == 'BOTH' and row['symbol'] not in result and
             type(row.get('updateTime')) is int and row['updateTime'] >= 0)
        result[row['symbol']] = (amount, row['updateTime'])
    return result


def day_start(today, anchor):
    need(type(today) is str and type(anchor) is int and 0 < anchor < 10**16)
    instant = datetime.fromtimestamp(anchor / 1000, WIB)
    need(instant.date().isoformat() == today)
    return int(instant.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()) * 1000


def fills(client, symbol, start, anchor):
    """Collect disjoint complete windows; never treat a full page as complete."""
    windows, result = [(start, anchor)], []
    while windows:
        lo, hi = windows.pop()
        client.sync_time()
        rows = client.signed_get('/fapi/v1/userTrades', symbol,
                                 start_time=lo, end_time=hi, limit=1000)
        need(type(rows) is list and len(rows) <= 1000)
        if len(rows) == 1000:
            # One millisecond containing a full page cannot prove coverage.
            need(lo < hi)
            mid = (lo + hi) // 2
            windows.extend(((mid + 1, hi), (lo, mid)))
            continue
        for row in rows:
            need(type(row) is dict and row.get('symbol') == symbol and
                 row.get('positionSide') == 'BOTH' and row.get('side') in ('BUY', 'SELL') and
                 type(row.get('time')) is int and lo <= row['time'] <= hi and
                 type(row.get('id')) is int and 0 <= row['id'] < 2**63 and
                 type(row.get('orderId')) is int and 0 < row['orderId'] < 2**63 and
                 quantity(row.get('qty')) > 0)
            result.append({key: row[key] for key in
                           ('symbol', 'positionSide', 'side', 'time', 'id', 'orderId', 'qty')})
    result.sort(key=lambda row: (row['time'], row['id']))
    identifiers = [row['id'] for row in result]
    need(len(identifiers) == len(set(identifiers)) and identifiers == sorted(identifiers))
    return result


def reconcile(store, client, today, current_symbols):
    """Persist new daily carry proofs; repeated polls do not repeat history GETs.

    Previously observed symbols and existing bot intents remain tracked across
    restarts, so a known carry closed before the next poll is still counted.
    This does not infer unknown, already closed manual symbols on first install.
    """
    need(type(current_symbols) in (list, tuple, set) and
         all(type(s) is str and SYMBOL.fullmatch(s) for s in current_symbols))
    known = set(current_symbols)
    known.update(row[0] for row in store.db.execute('SELECT symbol FROM robot_carry_symbols'))
    known.update(row[0] for row in store.db.execute('SELECT DISTINCT symbol FROM order_intents'))
    need(all(type(s) is str and SYMBOL.fullmatch(s) for s in known))
    # Discoveries carry no financial claim. Persist them before network work so
    # an incomplete history read cannot silently restore free quota on restart.
    store.db.executemany('INSERT OR IGNORE INTO robot_carry_symbols VALUES(?)', [(s,) for s in sorted(known)])
    proved = {row[0] for row in store.db.execute('SELECT symbol FROM robot_daily_carry WHERE day=?', (today,))}
    missing = sorted(known - proved)
    if not missing:
        return
    client.sync_time()
    before = positions(client.signed_get('/fapi/v3/positionRisk'))
    client.sync_time()
    anchor = client.server_time_ms()
    start = day_start(today, anchor)
    evidence = {symbol: fills(client, symbol, start, anchor) for symbol in missing}
    client.sync_time()
    after = positions(client.signed_get('/fapi/v3/positionRisk'))
    client.sync_time()
    need(day_start(today, client.server_time_ms()) == start)
    records = []
    for symbol in missing:
        prior, current = before.get(symbol, (Fraction(0), 0)), after.get(symbol, (Fraction(0), 0))
        need(prior == current and current[1] <= anchor)
        signed = sum((quantity(row['qty']) * (1 if row['side'] == 'BUY' else -1)
                      for row in evidence[symbol]), Fraction(0))
        midnight = current[0] - signed
        document = dict(day=today, symbol=symbol, day_start_ms=start, anchor_ms=anchor,
                        current_quantity=str(current[0]), update_time=current[1],
                        midnight_quantity=str(midnight), fills=evidence[symbol])
        digest = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        records.append((today, symbol, int(midnight != 0), anchor, digest))
    # Never overwrite a proved day when a later closure or restart is observed.
    store.db.execute('SAVEPOINT carry_quota')
    try:
        store.db.executemany('INSERT OR IGNORE INTO robot_daily_carry VALUES(?,?,?,?,?)', records)
        store.db.execute('RELEASE carry_quota')
    except BaseException:
        store.db.execute('ROLLBACK TO carry_quota')
        store.db.execute('RELEASE carry_quota')
        raise


def charged_symbols(db, today):
    return {row[0] for row in db.execute('SELECT symbol FROM robot_daily_carry WHERE day=? AND carried=1', (today,))}


def ready(db, today, symbols=()):
    known = set(symbols)
    known.update(row[0] for row in db.execute('SELECT symbol FROM robot_carry_symbols'))
    # A just-created, proven-unsent intent cannot be a carry. Every other
    # historical intent remains relevant even if discovery persistence failed.
    known.update(row[0] for row in db.execute("SELECT DISTINCT symbol FROM order_intents WHERE state NOT IN ('READY_FOR_EXECUTION','EXECUTION_BLOCKED')"))
    proved = {row[0] for row in db.execute('SELECT symbol FROM robot_daily_carry WHERE day=?', (today,))}
    return known <= proved


def pending_remainder_is_charged(db, row, today, carry):
    """Only the exact prior-day first fill can overlap a carried remainder."""
    if row['symbol'] not in carry:
        return False
    try:
        payload = json.loads(row['payload'])
        identity = payload['client_order_id']
    except (TypeError, ValueError, KeyError):
        return False
    if type(identity) is not str:
        return False
    receipt = db.execute('SELECT symbol,entry_day FROM robot_entry_receipts WHERE id=?', (identity,)).fetchone()
    return (receipt is not None and receipt['symbol'] == row['symbol'] and
            type(receipt['entry_day']) is str and receipt['entry_day'] < today)
