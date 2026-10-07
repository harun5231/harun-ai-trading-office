"""Private, incremental USD-M fill history and reconstructed closed-position PNL.

Binance's public signed API exposes fills and income, not the mobile app's
position-history records. A flat-to-flat lifecycle is reconstructed from those
facts. Historical leverage/ROI are deliberately absent. This reader has no
order interface and never writes the trading ledger.
"""
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext, ROUND_DOWN
from pathlib import Path

from .binance_private import BinanceCheckError, CODES
from .http_client import unique

WIB = timezone(timedelta(hours=7))
DAY_MS = 86_400_000
WINDOW_MS = 7 * DAY_MS
START_DATE = '2026-10-01'
START_MS = int(datetime(2026, 10, 1, tzinfo=WIB).timestamp()) * 1000
# A carry-in whose opening precedes this bounded bootstrap stays incomplete.
BOOTSTRAP_MS = START_MS - WINDOW_MS
CACHE_NAME = 'pnl-history-v1.json'
PAGE_SIZE = 1000
MAX_REQUESTS = 4
MAX_SECONDS = 15
MAX_SYMBOLS = 100
MAX_ROWS = 100_000
MAX_CACHE_BYTES = 32_000_000
SYMBOL = re.compile(r'[A-Z0-9_]{2,26}USDT')
NUMBER = re.compile(r'-?\d+(?:\.\d+)?')


def _ms(value):
    if type(value) is not int or not 0 <= value < 10**16:
        raise ValueError('PNL_HISTORY_INVALID')
    return value


def _number(value):
    if not isinstance(value, str) or len(value) > 64 or not NUMBER.fullmatch(value):
        raise ValueError('PNL_HISTORY_INVALID')
    return Decimal(value)


def _iso(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _date(value):
    return datetime.fromtimestamp(value / 1000, WIB).date().isoformat()


def _fixed(value):
    result = format(value, 'f')
    if len(result) > 64:
        raise ValueError('PNL_VALUE_LIMIT')
    return result


def _average(value):
    result = _fixed(value.quantize(Decimal('0.0000000000000001')))
    return result.rstrip('0').rstrip('.') if '.' in result else result


def _integer(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError('PNL_HISTORY_INVALID')
    return value


def _fill(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('symbol'), str) or not SYMBOL.fullmatch(raw['symbol']):
        raise ValueError('PNL_HISTORY_INVALID')
    if raw.get('side') not in ('BUY', 'SELL') or raw.get('positionSide') not in ('BOTH', 'LONG', 'SHORT'):
        raise ValueError('PNL_HISTORY_INVALID')
    if not isinstance(raw.get('commissionAsset'), str) or not re.fullmatch(r'[A-Z0-9]{1,16}', raw['commissionAsset']):
        raise ValueError('PNL_HISTORY_INVALID')
    for field in ('qty', 'price', 'realizedPnl', 'commission'):
        value = _number(raw[field])
        if field in ('qty', 'price') and value <= 0:
            raise ValueError('PNL_HISTORY_INVALID')
    return {key: raw[key] for key in ('symbol', 'side', 'positionSide', 'qty', 'price', 'realizedPnl', 'commission', 'commissionAsset')} | {
        'id': _integer(raw['id']), 'orderId': _integer(raw['orderId']), 'time': _ms(raw['time'])}


def _income(raw):
    if not isinstance(raw, dict):
        raise ValueError('PNL_HISTORY_INVALID')
    symbol = raw.get('symbol', '')
    if symbol and (not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9_]{3,30}', symbol)):
        raise ValueError('PNL_HISTORY_INVALID')
    kind, asset = raw['incomeType'], raw['asset']
    if not isinstance(kind, str) or not re.fullmatch(r'[A-Z_]{1,40}', kind):
        raise ValueError('PNL_HISTORY_INVALID')
    if not isinstance(asset, str) or not re.fullmatch(r'[A-Z0-9]{1,16}', asset):
        raise ValueError('PNL_HISTORY_INVALID')
    _number(raw['income'])
    return dict(symbol=symbol, incomeType=kind, asset=asset, income=raw['income'],
                time=_ms(raw['time']), tranId=_integer(raw['tranId']))


def _key(row, income=False):
    return (row['incomeType'] + ':' + str(row['tranId'])) if income else row['symbol'] + ':' + str(row['id'])


def _merge(intervals):
    result = []
    for start, end in sorted(intervals):
        _ms(start); _ms(end)
        if start > end:
            raise ValueError('PNL_CACHE_INVALID')
        if result and start <= result[-1][1] + 1:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def _covers(intervals, start, end):
    return end < start or any(a <= start and b >= end for a, b in _merge(intervals))


def _gap(intervals, start, end, *, latest=True):
    """A bounded missing window, with explicit historical/tail fairness."""
    if not latest:
        cursor = start
        for a, b in _merge(intervals):
            if b < cursor:
                continue
            if a > cursor:
                return [cursor, min(end, a - 1, cursor + WINDOW_MS - 1)] if cursor <= end else None
            cursor = max(cursor, b + 1)
        return [cursor, min(end, cursor + WINDOW_MS - 1)] if cursor <= end else None
    for a, b in reversed(_merge(intervals)):
        if a > end:
            continue
        if b < end:
            return [max(start, b + 1, end - WINDOW_MS + 1), end]
        end = a - 1
    return [max(start, end - WINDOW_MS + 1), end] if end >= start else None


def _risk(rows):
    if not isinstance(rows, list):
        raise ValueError('PNL_POSITION_SNAPSHOT_INVALID')
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('PNL_POSITION_SNAPSHOT_INVALID')
        symbol, side = row.get('symbol'), row.get('positionSide')
        if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9_]{3,30}', symbol) or side not in ('BOTH', 'LONG', 'SHORT'):
            raise ValueError('PNL_POSITION_SNAPSHOT_INVALID')
        amount = _number(row['positionAmt'])
        if side == 'LONG' and amount < 0 or side == 'SHORT' and amount > 0:
            raise ValueError('PNL_POSITION_SNAPSHOT_INVALID')
        update = row.get('updateTime')
        if update is not None:
            _ms(update)
        key = symbol + ':' + side
        if key in result:
            raise ValueError('PNL_POSITION_SNAPSHOT_INVALID')
        # Preserve zero-row updateTime as a witness of a post-cutoff close.
        # Harmless zero-row appearance/disappearance is ignored when comparing
        # quantities, but available race evidence must never be discarded.
        result[key] = dict(quantity=row['positionAmt'], updateTime=update)
    return result


def _empty(identity):
    return dict(version=1, identity=identity, incomes={}, fills={}, income_coverage=[],
                trade_coverage={}, income_task=None, trade_tasks={}, blocked={}, anchors={}, turn=0, next_kind='INCOME',
                income_mode='TAIL', trade_mode={})


def _validate(state, identity):
    expected = set(_empty(identity))
    if not isinstance(state, dict) or set(state) != expected or state['version'] != 1:
        raise ValueError('PNL_CACHE_INVALID')
    if not isinstance(state['identity'], str) or not re.fullmatch(r'[0-9a-f]{64}', state['identity']):
        raise ValueError('PNL_CACHE_INVALID')
    if state['identity'] != identity:
        return _empty(identity)
    if type(state['turn']) is not int or state['turn'] < 0:
        raise ValueError('PNL_CACHE_INVALID')
    if state['next_kind'] not in ('INCOME', 'TRADES'):
        raise ValueError('PNL_CACHE_INVALID')
    if state['income_mode'] not in ('TAIL', 'BACKFILL'):
        raise ValueError('PNL_CACHE_INVALID')
    if any(not isinstance(state[k], dict) for k in ('incomes', 'fills', 'trade_coverage', 'trade_tasks', 'blocked', 'anchors', 'trade_mode')):
        raise ValueError('PNL_CACHE_INVALID')
    if len(state['incomes']) + len(state['fills']) > MAX_ROWS:
        raise ValueError('PNL_CACHE_LIMIT')
    for name, parser, income in (('incomes', _income, True), ('fills', _fill, False)):
        for key, value in state[name].items():
            if parser(value) != value or _key(value, income) != key:
                raise ValueError('PNL_CACHE_INVALID')
    state['income_coverage'] = _merge(state['income_coverage'])
    for name in ('trade_coverage', 'trade_tasks', 'blocked'):
        if len(state[name]) > MAX_SYMBOLS:
            raise ValueError('PNL_CACHE_LIMIT')
        for symbol, intervals in state[name].items():
            if not SYMBOL.fullmatch(symbol) or not isinstance(intervals, list):
                raise ValueError('PNL_CACHE_INVALID')
            checked = _merge(intervals)
            if name != 'trade_tasks':
                state[name][symbol] = checked
            elif any(end - start >= WINDOW_MS for start, end in intervals):
                raise ValueError('PNL_CACHE_INVALID')
    task = state['income_task']
    if task is not None:
        if not isinstance(task, dict) or set(task) != {'start', 'end', 'page', 'seen_keys'}:
            raise ValueError('PNL_CACHE_INVALID')
        _ms(task['start']); _ms(task['end'])
        if not task['start'] <= task['end'] < task['start'] + WINDOW_MS or type(task['page']) is not int or not 1 <= task['page'] <= 100:
            raise ValueError('PNL_CACHE_INVALID')
        if not isinstance(task['seen_keys'], list) or len(task['seen_keys']) > MAX_ROWS or len(set(task['seen_keys'])) != len(task['seen_keys']) or any(not isinstance(k, str) for k in task['seen_keys']):
            raise ValueError('PNL_CACHE_INVALID')
    for symbol, mode in state['trade_mode'].items():
        if not SYMBOL.fullmatch(symbol) or mode not in ('TAIL', 'BACKFILL'):
            raise ValueError('PNL_CACHE_INVALID')
    for symbol, anchor in state['anchors'].items():
        if not SYMBOL.fullmatch(symbol) or not isinstance(anchor, dict) or set(anchor) != {'end', 'quantities'}:
            raise ValueError('PNL_CACHE_INVALID')
        _ms(anchor['end'])
        if not isinstance(anchor['quantities'], dict) or set(anchor['quantities']) - {'BOTH', 'LONG', 'SHORT'}:
            raise ValueError('PNL_CACHE_INVALID')
        for side, quantity in anchor['quantities'].items():
            amount = _number(quantity)
            if side == 'LONG' and amount < 0 or side == 'SHORT' and amount > 0:
                raise ValueError('PNL_CACHE_INVALID')
    return state


class HistoryCache:
    """One private atomic file, credential-scoped, separate from ledger.sqlite3."""
    def __init__(self, directory, identity):
        self.directory, self.identity = Path(directory), identity
        self.fd = self.lock = None

    def __enter__(self):
        try:
            self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(self.fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('PNL_CACHE_NOT_PRIVATE')
            self.lock = os.open('pnl-history.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.fd)
            info = os.fstat(self.lock)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError('PNL_CACHE_NOT_PRIVATE')
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('PNL_REFRESH_BUSY') from None
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *unused):
        for fd in (self.lock, self.fd):
            if fd is not None:
                os.close(fd)
        self.fd = self.lock = None

    def load(self):
        try:
            fd = os.open(CACHE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return _empty(self.identity)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError('PNL_CACHE_NOT_PRIVATE')
            if info.st_size > MAX_CACHE_BYTES:
                raise ValueError('PNL_CACHE_LIMIT')
            raw = source.read(MAX_CACHE_BYTES + 1)
            if len(raw) > MAX_CACHE_BYTES:
                raise ValueError('PNL_CACHE_LIMIT')
            try:
                state = json.loads(raw, object_pairs_hook=unique)
            except Exception:
                raise ValueError('PNL_CACHE_INVALID') from None
        return _validate(state, self.identity)

    def save(self, state):
        _validate(state, self.identity)
        raw = json.dumps(state, sort_keys=True, separators=(',', ':')).encode()
        if len(raw) > MAX_CACHE_BYTES:
            raise ValueError('PNL_CACHE_LIMIT')
        fd, name = tempfile.mkstemp(prefix='.pnl-history-', dir=self.directory)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(raw); output.flush(); os.fsync(output.fileno())
            os.replace(name, self.directory / CACHE_NAME)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass


def _put(state, name, rows, parser, start, end, symbol=None):
    parsed = [parser(row) for row in rows]
    if any(not start <= row['time'] <= end or symbol is not None and row['symbol'] != symbol for row in parsed):
        raise ValueError('PNL_HISTORY_INVALID')
    incoming = {}
    for row in parsed:
        key = _key(row, name == 'incomes')
        if key in incoming or key in state[name] and state[name][key] != row:
            raise ValueError('PNL_HISTORY_CONFLICT')
        incoming[key] = row
    if len(state['incomes']) + len(state['fills']) + len(set(incoming) - set(state[name])) > MAX_ROWS:
        raise ValueError('PNL_CACHE_LIMIT')
    state[name].update(incoming)
    return list(incoming)


def _read(client, path, **options):
    try:
        return client.signed_get(path, **options)
    except BinanceCheckError as error:
        if str(error) != 'BINANCE_CLOCK_ERROR':
            raise
    client.sync_time()
    return client.signed_get(path, **options)


def _page(rows):
    if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
        raise ValueError('PNL_HISTORY_INVALID')
    return rows


def _update(client, state, before, end, *, max_requests, budget_seconds):
    deadline = time.monotonic() + budget_seconds
    reasons = []
    used = 0
    # Official REST history is retained for three months. Previously cached
    # facts survive; uncached older time remains unknown, never an empty day.
    lower = max(BOOTSTRAP_MS, end - 90 * DAY_MS + 1)
    if lower > BOOTSTRAP_MS and not _covers(state['income_coverage'], BOOTSTRAP_MS, lower - 1):
        reasons.append('HISTORY_RETENTION_LIMIT')
    if end < START_MS:
        return reasons
    if state['income_task'] is None:
        gap = _gap(state['income_coverage'], lower, end, latest=state['income_mode'] == 'TAIL')
        if gap is None:
            gap = [max(lower, end - 2 * DAY_MS), end]
        state['income_task'] = dict(start=gap[0], end=gap[1], page=1, seen_keys=[])
    task = state['income_task']
    if task['start'] < lower:
        state['income_task'] = None
        reasons.append('HISTORY_RETENTION_LIMIT')
    elif used < max_requests and time.monotonic() < deadline and (max_requests > 1 or state['next_kind'] == 'INCOME' or
            not (any(SYMBOL.fullmatch(row['symbol']) for row in state['incomes'].values()) or
                 any(_number(value['quantity']) != 0 for value in before.values()))):
        used += 1
        state['next_kind'] = 'TRADES'
        try:
            rows = _page(_read(client, '/fapi/v1/income', start_time=task['start'], end_time=task['end'], limit=PAGE_SIZE, page=task['page']))
            keys = _put(state, 'incomes', rows, _income, task['start'], task['end'])
            if set(keys) & set(task['seen_keys']):
                task.update(page=1, seen_keys=[])
                raise ValueError('PNL_HISTORY_PAGINATION_STALLED')
            task['seen_keys'].extend(keys)
            if len(rows) < PAGE_SIZE:
                state['income_coverage'] = _merge(state['income_coverage'] + [[task['start'], task['end']]])
                state['income_task'] = None
                state['income_mode'] = 'BACKFILL' if state['income_mode'] == 'TAIL' else 'TAIL'
            elif task['page'] >= 100:
                reasons.append('INCOME_PAGE_LIMIT')
            else:
                task.update(page=task['page'] + 1)
        except Exception as error:
            reasons.append(_reason(error))
    symbols = sorted({row['symbol'] for row in state['incomes'].values() if SYMBOL.fullmatch(row['symbol'])} |
                     {key.rsplit(':', 1)[0] for key, value in before.items() if _number(value['quantity']) != 0 and SYMBOL.fullmatch(key.rsplit(':', 1)[0])})
    if len(symbols) > MAX_SYMBOLS:
        reasons.append('HISTORY_SYMBOL_LIMIT')
    symbols = symbols[:MAX_SYMBOLS]
    if symbols:
        offset = state['turn'] % len(symbols)
        ordered = symbols[offset:] + symbols[:offset]
        for symbol in ordered:
            if used >= max_requests or time.monotonic() >= deadline:
                break
            coverage = state['trade_coverage'].get(symbol, [])
            tasks = state['trade_tasks'].setdefault(symbol, [])
            if not tasks:
                gap = _gap(coverage + state['blocked'].get(symbol, []), lower, end,
                           latest=state['trade_mode'].get(symbol, 'TAIL') == 'TAIL')
                if gap is None:
                    gap = [max(lower, end - 2 * DAY_MS), end]
                tasks.append(gap)
            start, finish = tasks[0]
            if start < lower:
                tasks.pop(0); reasons.append('HISTORY_RETENTION_LIMIT'); continue
            used += 1
            state['next_kind'] = 'INCOME'
            try:
                rows = _page(_read(client, '/fapi/v1/userTrades', symbol=symbol, start_time=start, end_time=finish, limit=PAGE_SIZE))
                _put(state, 'fills', rows, _fill, start, finish, symbol)
                tasks.pop(0)
                if len(rows) < PAGE_SIZE:
                    state['trade_coverage'][symbol] = _merge(coverage + [[start, finish]])
                elif start < finish:
                    # Disjoint halves cover every same-ms boundary. Never drop
                    # all remaining fills at the timestamp of the oldest row.
                    middle = (start + finish) // 2
                    tasks[0:0] = [[middle + 1, finish], [start, middle]]
                else:
                    state['blocked'][symbol] = _merge(state['blocked'].get(symbol, []) + [[start, finish]])
                    reasons.append('DENSE_TRADE_TIMESTAMP_UNPROVEN')
                if not tasks:
                    state['trade_mode'][symbol] = 'BACKFILL' if state['trade_mode'].get(symbol, 'TAIL') == 'TAIL' else 'TAIL'
            except Exception as error:
                reasons.append(_reason(error))
            state['turn'] += 1
    if any(state['blocked'].values()):
        reasons.append('DENSE_TRADE_TIMESTAMP_UNPROVEN')
    if time.monotonic() >= deadline:
        reasons.append('HISTORY_TIME_BUDGET')
        return reasons
    after = _risk(_read(client, '/fapi/v3/positionRisk'))
    for symbol in symbols:
        old = {key: value for key, value in before.items() if key.startswith(symbol + ':') and _number(value['quantity']) != 0}
        new = {key: value for key, value in after.items() if key.startswith(symbol + ':') and _number(value['quantity']) != 0}
        witnesses = [value for key, value in after.items() if key.startswith(symbol + ':')]
        if old != new or any(value['updateTime'] is not None and value['updateTime'] > end for value in witnesses):
            reasons.append('POSITION_SNAPSHOT_CHANGED')
            continue
        if any(a <= end <= b for a, b in state['trade_coverage'].get(symbol, [])):
            state['anchors'][symbol] = dict(end=end, quantities={key.rsplit(':', 1)[1]: value['quantity'] for key, value in old.items()})
    return reasons


def _new_group(symbol, position_side, amount, closed_at=None, closing_id=None):
    return dict(symbol=symbol, position_side=position_side, side='LONG' if amount > 0 else 'SHORT',
                opened=None, closed=closed_at, closing_id=closing_id, entry_qty=Decimal(0), entry_value=Decimal(0),
                exit_qty=Decimal(0), exit_value=Decimal(0), realized=Decimal(0), commission=Decimal(0),
                fee_assets=set(), opening_proven=False, funding=Decimal(0), insurance=Decimal(0), reasons=[])


def _component(group, row, quantity, *, opening):
    if quantity <= 0:
        return
    total, whole = _number(row['commission']), _number(row['qty'])
    if quantity == whole:
        cost = total
    else:
        # A repeating fractional allocation cannot be serialized as hundreds of
        # digits. Round the closing share toward zero at at least 16 decimal
        # places, then assign its exact remainder to the opening share. The two
        # lifecycles retain the whole original commission, including rebates.
        closing = whole - quantity if opening else quantity
        places = max(16, -total.as_tuple().exponent)
        closing_cost = (total * closing / whole).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN).normalize()
        cost = total - closing_cost if opening else closing_cost
    if row['commissionAsset'] == 'USDT':
        group['commission'] += cost
    elif cost != 0:
        group['fee_assets'].add(row['commissionAsset'])
    name = 'entry' if opening else 'exit'
    group[name + '_qty'] += quantity
    group[name + '_value'] += quantity * _number(row['price'])
    if not opening:
        group['realized'] += _number(row['realizedPnl'])


def _groups(state, issues=None):
    """Reverse replay from independently verified ending quantities.

    Direction follows BUY/SELL and quantity, never the sign of realized PNL.
    A reversal splits its commission pro rata; all realized PNL belongs to its
    closing portion. Full contiguous coverage is necessary to cross a boundary.
    """
    groups = []
    for symbol, anchor in sorted(state['anchors'].items()):
        end = anchor['end']
        intervals = [pair for pair in _merge(state['trade_coverage'].get(symbol, [])) if pair[0] <= end <= pair[1]]
        if not intervals:
            continue
        start = intervals[0][0]
        rows = sorted((row for row in state['fills'].values() if row['symbol'] == symbol and start <= row['time'] <= end),
                      key=lambda row: (row['time'], row['id']), reverse=True)
        quantities = {side: _number(value) for side, value in anchor['quantities'].items()}
        current = {side: _new_group(symbol, side, amount) for side, amount in quantities.items() if amount != 0}
        symbol_groups = []
        try:
            for row in rows:
                side = row['positionSide']; delta = _number(row['qty']) * (1 if row['side'] == 'BUY' else -1)
                q1 = quantities.get(side, Decimal(0)); q0 = q1 - delta
                if side == 'LONG' and q0 < 0 or side == 'SHORT' and q0 > 0:
                    raise ValueError('POSITION_FILL_QUANTITY_MISMATCH')
                closing = min(abs(q0), abs(delta)) if q0 * delta < 0 else Decimal(0)
                opening = abs(delta) - closing
                if closing == 0 and _number(row['realizedPnl']) != 0:
                    raise ValueError('POSITION_FILL_QUANTITY_MISMATCH')
                if opening:
                    group = current.setdefault(side, _new_group(symbol, side, q1))
                    _component(group, row, opening, opening=True)
                    if q0 == 0 or q0 * q1 < 0:
                        group.update(opened=row['time'], opening_proven=True)
                        symbol_groups.append(group); current.pop(side)
                if closing:
                    group = current.setdefault(side, _new_group(symbol, side, q0, row['time'], row['id']))
                    _component(group, row, closing, opening=False)
                quantities[side] = q0
            for group in current.values():
                group['reasons'].append('OPENING_BEFORE_HISTORY_WINDOW')
                symbol_groups.append(group)
            for group in symbol_groups:
                group.update(history_start=start, history_end=end)
            groups.extend(symbol_groups)
        except ValueError:
            # A bad anchor, history gap or inconsistent exchange response cannot
            # create a fictional flat boundary or a closed-position PNL.
            if issues is not None:
                issues.append('POSITION_FILL_QUANTITY_MISMATCH')
            continue
    return groups


def _positions(state, end, issues=None):
    groups = _groups(state, issues)
    for group in groups:
        finish = group['closed'] if group['closed'] is not None else group['history_end']
        if group['opened'] is None or not _covers(state['income_coverage'], group['opened'], finish):
            group['reasons'].append('POSITION_INCOME_HISTORY_INCOMPLETE')
        if group['fee_assets']:
            group['reasons'].append('NON_USDT_COMMISSION')
    for row in state['incomes'].values():
        if row['incomeType'] not in ('FUNDING_FEE', 'INSURANCE_CLEAR'):
            continue
        # Income carries symbol, but no historical position side. Attribute only
        # if exactly one lifecycle spans the event, away from a reversal boundary.
        matches = [group for group in groups if group['symbol'] == row['symbol'] and
                   (group['opened'] if group['opened'] is not None else group['history_start']) <= row['time'] <=
                   (group['closed'] if group['closed'] is not None else group['history_end'])]
        if not matches:
            for group in groups:
                if group['symbol'] == row['symbol'] and group['closed'] is not None:
                    group['reasons'].append('POSITION_INCOME_ATTRIBUTION_UNKNOWN')
            continue
        boundary = any(row['time'] in (group['opened'], group['closed']) for group in matches)
        if len(matches) != 1 or boundary:
            for group in matches:
                group['reasons'].append('POSITION_INCOME_ATTRIBUTION_UNKNOWN')
        elif row['asset'] != 'USDT':
            matches[0]['reasons'].append('NON_USDT_POSITION_INCOME')
        else:
            name = 'funding' if row['incomeType'] == 'FUNDING_FEE' else 'insurance'
            matches[0][name] += _number(row['income'])
    positions = []
    for group in groups:
        if group['closed'] is None or not START_MS <= group['closed'] <= end or group['exit_qty'] <= 0:
            continue
        complete = group['opening_proven'] and not group['reasons']
        fee_net = group['realized'] - group['commission']
        known = fee_net + group['funding'] + group['insurance']
        identity = '{}:{}:{}:{}'.format(group['symbol'], group['position_side'], group['closed'], group['closing_id'])
        positions.append(dict(id=hashlib.sha256(identity.encode()).hexdigest()[:24], symbol=group['symbol'], side=group['side'],
            position_side=group['position_side'], status='CLOSED', opened_at=_iso(group['opened']) if group['opened'] is not None else None,
            closed_at=_iso(group['closed']), close_date=_date(group['closed']), closed_quantity=_fixed(group['exit_qty']),
            entry_price=_average(group['entry_value'] / group['entry_qty']) if group['opening_proven'] and group['entry_qty'] else None,
            exit_price=_average(group['exit_value'] / group['exit_qty']), realized_pnl_usdt=_fixed(group['realized']),
            commission_usdt=_fixed(group['commission']) if not group['fee_assets'] else None,
            funding_usdt=_fixed(group['funding']) if complete else None, insurance_usdt=_fixed(group['insurance']) if complete else None,
            pnl_usdt=_fixed(known) if complete else None, known_pnl_usdt=_fixed(known), complete=complete,
            calculation_method='REALIZED_MINUS_FEES_PLUS_FUNDING_AND_INSURANCE' if complete else 'KNOWN_COMPONENTS_PARTIAL',
            incomplete_reasons=list(dict.fromkeys(group['reasons'])), excluded_commission_assets=sorted(group['fee_assets'])))
    return sorted(positions, key=lambda row: (row['closed_at'], row['id']), reverse=True)


def unavailable(reason='HISTORY_LOADING', end_ms=None):
    end_ms = _ms(end_ms) if end_ms is not None else int(time.time() * 1000)
    return dict(source='BINANCE_FUTURES', kind='CLOSED_POSITIONS_FROM_FILLS', status='UNAVAILABLE', checked_at=None,
                timezone='Asia/Jakarta', start_date=START_DATE, end_date=max(START_DATE, _date(end_ms)), complete=False,
                days=[], positions=[], funding_usdt=None, insurance_usdt=None, incomplete_reasons=[reason])


def calendar(state, end, reasons=()):
    with localcontext() as precision:
        precision.prec = 256
        issues = []
        positions = _positions(state, end, issues)
        days = []
        date = datetime(2026, 10, 1, tzinfo=WIB)
        last = datetime.fromtimestamp(end / 1000, WIB).date()
        for unused in range(36_600):
            if date.date() > last:
                break
            label = date.date().isoformat()
            rows = [row for row in positions if row['close_date'] == label]
            subtotal = sum((_number(row['known_pnl_usdt']) for row in rows), Decimal(0))
            days.append(dict(date=label, pnl_usdt=None,
                             known_pnl_usdt=_fixed(subtotal), closed_positions=len(rows), complete=False))
            date += timedelta(days=1)
        incomplete = list(reasons) + issues
        if not _covers(state['income_coverage'], START_MS, end):
            incomplete.append('HISTORY_LOADING')
        symbols = {row['symbol'] for row in state['incomes'].values() if SYMBOL.fullmatch(row['symbol'])}
        if any(not _covers(state['trade_coverage'].get(symbol, []), START_MS, end) for symbol in symbols):
            incomplete.append('HISTORY_LOADING')
        incomplete.extend(reason for row in positions for reason in row['incomplete_reasons'])
        incomplete.append('SYMBOL_DISCOVERY_NOT_EXHAUSTIVE')
        totals = {'FUNDING_FEE': Decimal(0), 'INSURANCE_CLEAR': Decimal(0)}
        income_complete = _covers(state['income_coverage'], START_MS, end)
        for row in state['incomes'].values():
            if START_MS <= row['time'] <= end and row['incomeType'] in totals:
                if row['asset'] == 'USDT':
                    totals[row['incomeType']] += _number(row['income'])
                else:
                    incomplete.append('NON_USDT_POSITION_INCOME'); income_complete = False
        return dict(source='BINANCE_FUTURES', kind='CLOSED_POSITIONS_FROM_FILLS', status='PARTIAL' if state['incomes'] or state['fills'] else 'UNAVAILABLE',
            checked_at=_iso(end), timezone='Asia/Jakarta', start_date=START_DATE, end_date=max(START_DATE, _date(end)), complete=False,
            days=days, positions=positions, funding_usdt=_fixed(totals['FUNDING_FEE']) if income_complete else None,
            insurance_usdt=_fixed(totals['INSURANCE_CLEAR']) if income_complete else None,
            incomplete_reasons=list(dict.fromkeys(incomplete)))


def _reason(error):
    value = str(error)
    if isinstance(error, BinanceCheckError) and value in CODES:
        return value
    if isinstance(error, ValueError) and re.fullmatch(r'(?:PNL|POSITION|INCOME|HISTORY|DENSE)_[A-Z_]{1,80}', value):
        return value
    return 'PNL_HISTORY_UNAVAILABLE'


def collect_calendar(client, directory, *, max_requests=MAX_REQUESTS, budget_seconds=MAX_SECONDS):
    """At most four history GETs per refresh; durable backfill resumes next poll.

    positionRisk is read before and after history. Changed snapshots cannot
    advance a symbol's reverse-replay anchor. Last verified cached records remain
    visible as partial. REST snapshots are not an atomic exchange transaction;
    the calendar therefore identifies its reconstruction and limited scope.
    """
    state = None; end = None
    try:
        if type(max_requests) is not int or not 1 <= max_requests <= 8 or type(budget_seconds) not in (int, float) or not 0 < budget_seconds <= 30:
            raise ValueError('PNL_HISTORY_INVALID')
        key = getattr(client, '_key', None)
        if not isinstance(key, str) or not key:
            raise ValueError('PNL_CACHE_IDENTITY_UNAVAILABLE')
        identity = hashlib.sha256(key.encode()).hexdigest()
        with HistoryCache(directory, identity) as cache:
            state = cache.load()
            deadline = time.monotonic() + budget_seconds
            client.sync_time()
            end = client.server_time_ms()
            before = _risk(_read(client, '/fapi/v3/positionRisk'))
            end = client.server_time_ms()
            if type(end) is not int:
                raise ValueError('PNL_HISTORY_INVALID')
            remaining = deadline - time.monotonic()
            reasons = (_update(client, state, before, end, max_requests=max_requests, budget_seconds=remaining)
                       if remaining > 0 else ['HISTORY_TIME_BUDGET'])
            cache.save(state)
            return calendar(state, end, reasons)
    except Exception as error:
        reason = _reason(error)
        if state is not None and end is not None:
            try:
                return calendar(state, end, [reason])
            except Exception:
                pass
        return unavailable(reason, end)
