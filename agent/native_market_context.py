"""Copy current native Futures facts into the dialogue-only model context.

The native browser actor must first validate its original request and chart
bindings. This module does not attest screenshots, fetch data, create setups,
or alter the worker's context. Native ``worker.market`` data is already public
Binance USD-M klines; private account/risk settings never enter this output.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from decimal import Decimal, InvalidOperation

from .models import NotSent


NATIVE_CHART_KIND = 'NATIVE_PUBLIC_KLINES_CONTEXT'
MAX_AGE = 180
INTERVALS = {'1h': 3_600_000, '15m': 900_000}
CANDLE_FIELDS = ('open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time')
NUMBER = re.compile(r'\d{1,32}(?:\.\d{1,24})?\Z', re.ASCII)
SYMBOL = re.compile(r'[A-Z0-9]{2,18}USDT\Z', re.ASCII)


class NativeContextUnavailable(NotSent):
    pass


def _need(condition):
    if not condition:
        raise NativeContextUnavailable('NATIVE_MARKET_CONTEXT_UNVERIFIED')


def _number(value, *, zero=False):
    _need(type(value) is str and NUMBER.fullmatch(value) is not None)
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise NativeContextUnavailable('NATIVE_MARKET_CONTEXT_UNVERIFIED') from None
    _need(parsed.is_finite() and (parsed >= 0 if zero else parsed > 0))
    return parsed


def _instant(value):
    return (type(value) in (int, float) and 0 < value <= 10**13
            and math.isfinite(value))


def convert_native_market_context(context, *, now=None):
    """Return exact native mark/last-three klines, without invented prices.

    This accepts the verified native Market.data/analysis_context structure.
    Extra native contract/risk fields are deliberately neither serialized nor
    forwarded. The provenance labels the public data format only: actual chart
    attachments continue to require the original browser's independent receipt.
    """
    now = time.time() if now is None else now
    _need(_instant(now) and type(context) is dict
          and context.get('source') == 'Binance Futures'
          and type(context.get('symbol')) is str and SYMBOL.fullmatch(context['symbol']) is not None)
    symbol = context['symbol']
    observed, server = context.get('fetched_at'), context.get('server_time')
    _need(_instant(observed) and 0 <= now-observed <= MAX_AGE
          and type(server) is int and 0 < server <= 10**16
          and abs(server/1000-observed) <= 30 and -30 <= now-server/1000 <= MAX_AGE)
    mark = context.get('mark_price')
    _need(type(mark) is dict and set(mark) == {'price', 'time'}
          and type(mark['time']) is int and 0 < mark['time'] <= 10**16
          and 0 <= now-mark['time']/1000 <= MAX_AGE and mark['time'] <= server)
    _number(mark['price'])
    frames = context.get('timeframes')
    _need(type(frames) is dict and set(frames) == set(INTERVALS))
    all_candles, recent = {}, {}
    for timeframe, interval in INTERVALS.items():
        frame = frames[timeframe]
        _need(type(frame) is dict and set(frame) == {'symbol', 'timeframe', 'candles'}
              and frame['symbol'] == symbol and frame['timeframe'] == timeframe
              and type(frame['candles']) is list and 20 <= len(frame['candles']) <= 500)
        copied, previous = [], None
        for row in frame['candles']:
            _need(type(row) is dict and set(row) == set(CANDLE_FIELDS))
            opened, closed = row['open_time'], row['close_time']
            _need(type(opened) is int and type(closed) is int and opened >= 0
                  and opened % interval == 0 and closed == opened+interval-1
                  and opened <= server and (previous is None or opened-previous == interval))
            o, h, low, c = (_number(row[key]) for key in ('open', 'high', 'low', 'close'))
            _number(row['volume'], zero=True)
            _need(low <= min(o, c) <= max(o, c) <= h)
            copied.append({key: row[key] for key in CANDLE_FIELDS})
            previous = opened
        _need(copied[-1]['open_time'] <= server <= copied[-1]['close_time']
              and now*1000 <= copied[-1]['close_time']+MAX_AGE*1000)
        all_candles[timeframe] = copied
        recent[timeframe] = [{key: row[key] for key in CANDLE_FIELDS} for row in copied[-3:]]
    digest = hashlib.sha256(json.dumps(all_candles, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode('ascii')).hexdigest()
    result = {'symbol': symbol, 'observed_at': observed, 'mark_price': mark['price'],
        'provenance': {'provider': 'BINANCE_FUTURES_PUBLIC', 'market': 'USDT_PERPETUAL',
            'origin': 'https://fapi.binance.com', 'candles_sha256': digest,
            'chart_kind': NATIVE_CHART_KIND}, 'recent_candles': recent}
    _need(len(json.dumps(result, allow_nan=False).encode()) <= 8192)
    return result
