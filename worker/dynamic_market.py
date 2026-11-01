"""Read-only inputs and execution observations for the owner's V4 cost policy.

Observations never amend an accepted intent. This module has no order endpoint,
gateway, credential access, or trading switch.
"""
import json
import math
import re
import time
from dataclasses import fields
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from .core import D, Review, Rules, DynamicRules, number, _exact_text
from .dynamic_risk import _rate_up


def need(ok, code):
    if not ok:
        raise Review(code)


def closed_moves(context):
    """Keep exchange OHLC and timestamps; omit the still-running candle."""
    try:
        server = context['server_time']
        frame = context['timeframes']['15m']
        need(type(server) is int and frame['symbol'] == context['symbol']
            and frame['timeframe'] == '15m', 'INVALID_PRICE_MOVE_EVIDENCE')
        result = []
        for row in frame['candles']:
            start, close = row['open_time'], row['close_time']
            need(type(start) is int and type(close) is int and close > start,
                'INVALID_PRICE_MOVE_EVIDENCE')
            if close >= server:
                continue
            o, h, l = (number(row[k]) for k in ('open', 'high', 'low'))
            need(l <= o <= h, 'INVALID_PRICE_MOVE_EVIDENCE')
            result.append(dict(start=start / 1000, end=(close + 1) / 1000,
                reference=str(row['open']), high=str(row['high']), low=str(row['low'])))
        need(len(result) >= 20, 'SLIPPAGE_UNCERTAINTY_EVIDENCE_UNAVAILABLE')
        return result[-200:]
    except Review:
        raise
    except (KeyError, TypeError, ValueError):
        raise Review('INVALID_PRICE_MOVE_EVIDENCE') from None


def bracket_evidence(client, symbol, *, clock=time.time):
    """Use the signed account-specific bracket response, never exchange defaults."""
    need(client is not None, 'LEVERAGE_EVIDENCE_UNAVAILABLE')
    raw = client.signed_get('/fapi/v1/leverageBracket', symbol)
    if type(raw) is list:
        rows = [r for r in raw if type(r) is dict and r.get('symbol') == symbol]
        need(len(rows) == 1, 'INVALID_LEVERAGE_BRACKETS')
        raw = rows[0]
    need(type(raw) is dict and raw.get('symbol') == symbol
        and type(raw.get('brackets')) is list and raw['brackets'],
        'INVALID_LEVERAGE_BRACKETS')
    brackets = []
    try:
        for r in raw['brackets']:
            need(type(r) is dict and not isinstance(r.get('notionalFloor'),bool),
                'INVALID_LEVERAGE_BRACKETS')
            floor = D(str(r['notionalFloor']))
            cap = number(r['notionalCap'])
            leverage = r['initialLeverage']
            need(floor.is_finite() and floor >= 0 and cap > floor
                and type(leverage) is int and leverage > 0, 'INVALID_LEVERAGE_BRACKETS')
            brackets.append(dict(notional_floor=format(floor, 'f'),
                notional_cap=format(cap, 'f'), max_leverage=leverage))
    except (KeyError,TypeError,ValueError,InvalidOperation):
        raise Review('INVALID_LEVERAGE_BRACKETS') from None
    return dict(schema_version=1, source='BINANCE_FUTURES_ACCOUNT_LEVERAGE_BRACKET',
        symbol=symbol, observed_at=clock(), brackets=brackets)


def execution_samples(db, symbol):
    if db is None:
        return []
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='execution_cost_observations'").fetchone():
        return []
    return [dict(zip(('observed_at', 'side', 'expected_impact_rate',
        'actual_impact_rate', 'order_id', 'execution_id'), r)) for r in db.execute(
        'SELECT observed_at,side,expected_impact_rate,actual_impact_rate,order_id,execution_id '
        'FROM execution_cost_observations WHERE symbol=? AND expected_impact_rate IS NOT NULL '
        'ORDER BY observed_at DESC LIMIT 1000',
        (symbol,))]


def dynamic_rules(market, symbol, rules, context, client, *, db=None, clock=time.time):
    """Refresh book and account brackets before calculating a new setup."""
    need(context.get('symbol') == symbol, 'MARKET_CONTEXT_MISMATCH')
    book = market.depth(symbol)
    need(type(book) is dict and book.get('symbol') == symbol
        and book.get('source') == 'BINANCE_FUTURES_PUBLIC_DEPTH', 'INVALID_ORDER_BOOK')
    observed = book['observed_at']
    need(type(observed) in (int, float) and math.isfinite(observed)
        and 0 <= clock() - observed <= 300, 'STALE_SLIPPAGE_EVIDENCE')
    evidence = dict(schema_version=1, source='BINANCE_FUTURES_PUBLIC_MARKET_AND_OWN_FILLS',
        symbol=symbol, observed_at=observed, bids=book['bids'], asks=book['asks'],
        price_moves=closed_moves(context), execution_samples=execution_samples(db, symbol))
    values = {f.name: getattr(rules, f.name) for f in fields(Rules)}
    return DynamicRules(**values, dynamic_cost_evidence=evidence,
        leverage_brackets=bracket_evidence(client, symbol, clock=clock))


def record_execution(db, *, symbol, side, order_id, execution_id, filled_at,
        detected_at, reference_price, fill_price, quantity, expected_impact_rate,
        fee_amount=None, fee_asset=None, source='BINANCE_USER_DATA_ORDER_TRADE_UPDATE'):
    """Persist an owned, confirmed fill after priority protection has run.

    The caller supplies previously verified ownership. A repeat execution ID
    is idempotent. Raw fee asset/value stays separate from measured slippage.
    """
    need(type(symbol) is str and re.fullmatch(r'[A-Z0-9]{2,18}USDT', symbol)
        and side in ('BUY', 'SELL') and source in (
            'BINANCE_USER_DATA_ORDER_TRADE_UPDATE', 'BINANCE_FUTURES_USER_TRADES'),
        'INVALID_EXECUTION_COST_EVIDENCE')
    need(all(type(v) is str and v for v in (order_id, execution_id)),
        'INVALID_EXECUTION_COST_EVIDENCE')
    need(all(type(v) in (int, float) and math.isfinite(v) and v > 0
        for v in (filled_at, detected_at)) and detected_at >= filled_at,
        'INVALID_EXECUTION_COST_EVIDENCE')
    ref, price, qty = (number(v) for v in (reference_price, fill_price, quantity))
    expected = D(str(expected_impact_rate)) if expected_impact_rate is not None else None
    need(expected is None or expected.is_finite() and 0 <= expected < 1,
        'INVALID_EXECUTION_COST_EVIDENCE')
    adverse_exact = max(Fraction(0), (Fraction(price)-Fraction(ref))/Fraction(ref)
        if side=='BUY' else (Fraction(ref)-Fraction(price))/Fraction(ref))
    # Keep the actual raw prices; quantize only the derived rate conservatively
    # to the same documented precision as the sizing model.
    adverse = _exact_text(_rate_up(adverse_exact))
    if fee_amount is not None:
        fee = D(str(fee_amount))
        need(fee.is_finite() and type(fee_asset) is str and fee_asset,
            'INVALID_EXECUTION_COST_EVIDENCE')
        fee_amount = format(fee, 'f')
    db.execute('CREATE TABLE IF NOT EXISTS execution_cost_observations('
        'symbol TEXT NOT NULL,side TEXT NOT NULL,order_id TEXT NOT NULL,execution_id TEXT NOT NULL,'
        'filled_at REAL NOT NULL,observed_at REAL NOT NULL,reference_price TEXT NOT NULL,'
        'fill_price TEXT NOT NULL,quantity TEXT NOT NULL,expected_impact_rate TEXT,'
        'actual_impact_rate TEXT NOT NULL,fee_amount TEXT,fee_asset TEXT,source TEXT NOT NULL,'
        'PRIMARY KEY(symbol,order_id,execution_id))')
    values=(symbol,side,order_id,execution_id,filled_at,detected_at,format(ref,'f'),
        format(price,'f'),format(qty,'f'),format(expected,'f') if expected is not None else None,
        adverse,fee_amount,fee_asset,source)
    prior=db.execute('SELECT * FROM execution_cost_observations WHERE symbol=? AND order_id=? AND execution_id=?',
        (symbol,order_id,execution_id)).fetchone()
    if prior is not None:
        # Later delivery may have a later detection time, but cannot change a
        # confirmed fill's economics or its originally recorded reference.
        need(all(prior[i]==values[i] for i in range(len(values)) if i!=5),
            'EXECUTION_COST_OBSERVATION_CONFLICT')
        return
    db.execute('INSERT INTO execution_cost_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',values)
