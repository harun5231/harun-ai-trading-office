"""Owner policy V4: immutable, data-derived costs for new order intents only.

This module has no exchange client, order methods, credentials, or state writes.
Older plans stay on their original core V2/V3 arithmetic. Market observations are
inputs, not predictions or permission to alter an accepted order.
"""
import copy
import hashlib
import json
import math
import time
from fractions import Fraction as F
from dataclasses import replace

from .core import (D, Review, Signal, number, validated_risk_target,
    validated_fee_rate, _exact_decimal, _exact_text, _ratio_decimal)

MODEL = 'DYNAMIC_NET_2R_V4'
POLICY = 'NOMINAL_OFFICE_R_NET_2R_NEAREST_TICK_V4'
COST_SOURCE = 'BINANCE_FUTURES_PUBLIC_MARKET_AND_OWN_FILLS'
BRACKET_SOURCE = 'BINANCE_FUTURES_ACCOUNT_LEVERAGE_BRACKET'
MIN_SAMPLE_COUNT = 20
RATE_PLACES = 12


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _need(value, code):
    if not value:
        raise Review(code)


def _nonnegative(value, *, upper_one=True):
    _need(not isinstance(value, bool) and isinstance(value, (str, int, D)), 'INVALID_COST_EVIDENCE')
    try:
        result = D(value)
    except Exception:
        raise Review('INVALID_COST_EVIDENCE') from None
    _need(result.is_finite() and result >= 0 and (not upper_one or result < 1) and len(str(value)) <= 64,
        'INVALID_COST_EVIDENCE')
    return F(result)


def _zero_number(value):
    if value in ('0', 0, D('0')) and not isinstance(value, bool):
        return D('0')
    return number(value)


def _stamp(value, code):
    _need(type(value) in (int, float) and math.isfinite(value) and value >= 0, code)
    return value


def _fresh(value, check_fresh, code):
    _stamp(value, code)
    if check_fresh:
        _need(0 <= time.time() - value <= 300, code)


def _rate_up(value):
    """Round a measured rate upward to documented precision, never downward."""
    _need(value >= 0, 'INVALID_COST_EVIDENCE')
    scale = 10 ** RATE_PLACES
    value *= scale
    return F(-(-value.numerator // value.denominator), scale)


def _q95(values):
    values = sorted(values)
    _need(len(values) >= MIN_SAMPLE_COUNT, 'SLIPPAGE_UNCERTAINTY_EVIDENCE_UNAVAILABLE')
    return values[(95 * len(values) + 99) // 100 - 1]


def market_evidence(rules, symbol, side, *, check_fresh=True):
    data = copy.deepcopy(getattr(rules, 'dynamic_cost_evidence', None))
    _need(type(data) is dict and type(data.get('schema_version')) is int and data.get('schema_version') == 1
        and data.get('symbol') == symbol and data.get('source') == COST_SOURCE,
        'SLIPPAGE_EVIDENCE_UNAVAILABLE')
    _fresh(data.get('observed_at'), check_fresh, 'STALE_SLIPPAGE_EVIDENCE')
    _need(side in ('LONG', 'SHORT'), 'INVALID_SIDE')
    book = {}
    for name in ('bids', 'asks'):
        raw = data.get(name)
        _need(type(raw) is list and raw, 'SLIPPAGE_DEPTH_UNAVAILABLE')
        levels = []
        for row in raw:
            _need(type(row) in (list, tuple) and len(row) == 2, 'INVALID_ORDER_BOOK')
            levels.append((F(number(row[0])), F(number(row[1]))))
        prices = [row[0] for row in levels]
        _need(len(set(prices)) == len(prices)
            and prices == sorted(prices, reverse=name == 'bids'), 'INVALID_ORDER_BOOK')
        book[name] = levels
    _need(book['bids'][0][0] < book['asks'][0][0], 'INVALID_ORDER_BOOK')
    moves = data.get('price_moves')
    _need(type(moves) is list, 'SLIPPAGE_UNCERTAINTY_EVIDENCE_UNAVAILABLE')
    observed = data['observed_at']
    adverse = []
    intervals = set()
    horizons = set()
    for row in moves:
        _need(type(row) is dict, 'INVALID_PRICE_MOVE_EVIDENCE')
        start, end = (_stamp(row.get(key), 'INVALID_PRICE_MOVE_EVIDENCE') for key in ('start', 'end'))
        _need(start < end <= observed and (start, end) not in intervals,
            'INVALID_PRICE_MOVE_EVIDENCE')
        intervals.add((start, end)); horizons.add(end - start)
        reference, high, low = (F(number(row[key])) for key in ('reference', 'high', 'low'))
        _need(low <= reference <= high, 'INVALID_PRICE_MOVE_EVIDENCE')
        adverse.append((reference - low) / reference if side == 'LONG' else (high - reference) / reference)
    _need(len(intervals) >= MIN_SAMPLE_COUNT, 'SLIPPAGE_UNCERTAINTY_EVIDENCE_UNAVAILABLE')
    _need(observed - max(end for _, end in intervals) <= 3600,
        'STALE_PRICE_MOVE_EVIDENCE')
    move_reserve = _q95(adverse)
    closing = 'SELL' if side == 'LONG' else 'BUY'
    executions = data.get('execution_samples', [])
    _need(type(executions) is list, 'INVALID_EXECUTION_COST_EVIDENCE')
    per_order_excess = {}
    execution_ids = set()
    for row in executions:
        _need(type(row) is dict and row.get('side') in ('SELL', 'BUY'), 'INVALID_EXECUTION_COST_EVIDENCE')
        _need(_stamp(row.get('observed_at'), 'INVALID_EXECUTION_COST_EVIDENCE') <= observed,
            'INVALID_EXECUTION_COST_EVIDENCE')
        order_id, execution_id = row.get('order_id'), row.get('execution_id')
        _need(type(order_id) is str and order_id and type(execution_id) is str and execution_id
            and execution_id not in execution_ids, 'INVALID_EXECUTION_COST_EVIDENCE')
        execution_ids.add(execution_id)
        expected = _nonnegative(row.get('expected_impact_rate'))
        actual = _nonnegative(row.get('actual_impact_rate'), upper_one=False)
        if row['side'] == closing:
            per_order_excess[order_id] = max(per_order_excess.get(order_id,F(0)), actual-expected)
    excess = list(per_order_excess.values())
    fill_reserve = _q95(excess) if len(excess) >= MIN_SAMPLE_COUNT else F(0)
    # Execution shortfall samples already include expected book impact. Only
    # their excess enters uncertainty, and it is not added twice to price moves.
    uncertainty = _rate_up(max(move_reserve, fill_reserve))
    _need(uncertainty < 1, 'INVALID_COST_EVIDENCE')
    return dict(raw=copy.deepcopy(data), book=book, closing_side=closing,
        midpoint=(book['bids'][0][0] + book['asks'][0][0]) / 2,
        uncertainty=uncertainty, move_reserve=_rate_up(move_reserve),
        execution_reserve=_rate_up(fill_reserve), price_move_count=len(adverse),
        execution_sample_count=len(excess), horizons=sorted(horizons))


def book_impact(quantity, evidence):
    quantity = F(quantity)
    _need(quantity > 0, 'INVALID_EXECUTION_QUANTITY')
    levels = evidence['book']['bids' if evidence['closing_side'] == 'SELL' else 'asks']
    remaining = quantity; value = F(0)
    for price, available in levels:
        used = min(remaining, available)
        value += used * price; remaining -= used
        if remaining == 0:
            break
    _need(remaining == 0, 'SLIPPAGE_DEPTH_INSUFFICIENT')
    vwap = value / quantity
    impact = ((evidence['midpoint'] - vwap) / evidence['midpoint']
        if evidence['closing_side'] == 'SELL' else (vwap - evidence['midpoint']) / evidence['midpoint'])
    return _rate_up(max(F(0), impact)), vwap


def _cost_values(entry, tp, sl, quantity, fee, side, evidence):
    e, t, s, q = map(F, (entry, tp, sl, quantity))
    impact, vwap = book_impact(q, evidence)
    uncertainty = evidence['uncertainty']; reserve = impact + uncertainty
    _need(reserve < 1, 'INVALID_COST_EVIDENCE')
    factor = 1 - reserve if side == 'LONG' else 1 + reserve
    sl_fill, tp_fill = s * factor, t * factor
    gross_risk, gross_reward = q * abs(e - s), q * abs(t - e)
    entry_fee, sl_fee, tp_fee = q * e * fee, q * sl_fill * fee, q * tp_fill * fee
    sl_book, sl_unknown = q * s * impact, q * s * uncertainty
    tp_book, tp_unknown = q * t * impact, q * t * uncertainty
    total = gross_risk + entry_fee + sl_fee + sl_book + sl_unknown
    reward = gross_reward - entry_fee - tp_fee - tp_book - tp_unknown
    return dict(total=total, reward=reward, gross_risk=gross_risk,
        gross_reward=gross_reward, entry_fee=entry_fee, sl_fee=sl_fee, tp_fee=tp_fee,
        sl_book=sl_book, sl_unknown=sl_unknown, tp_book=tp_book, tp_unknown=tp_unknown,
        sl_fill=sl_fill, tp_fill=tp_fill, impact=impact, reserve=reserve, vwap=vwap)


def maximum_quantity(entry, sl, side, rules, risk_target, *, symbol=None, check_fresh=True):
    symbol = symbol or rules.fee_symbol
    evidence = market_evidence(rules, symbol, side, check_fresh=check_fresh)
    fee = F(validated_fee_rate(rules, symbol, check_fresh=check_fresh))
    e, s, target = F(number(entry)), F(number(sl)), F(validated_risk_target(risk_target))
    _need(side in ('LONG', 'SHORT') and (s < e if side == 'LONG' else s > e), 'INVALID_ENTRY_TP_SL')
    step, minimum, maximum = (F(number(v)) for v in (rules.step, rules.minimum, rules.maximum))
    levels = evidence['book']['bids' if side == 'LONG' else 'asks']
    depth = sum(amount for _, amount in levels)
    high = int(min(maximum, depth) // step)
    low = 0
    # Monotone total loss includes the size-dependent closing-book VWAP. The
    # search ends on a legal lot interval, not an arbitrary iteration limit.
    while low < high:
        middle = (low + high + 1) // 2
        costs = _cost_values(e, e, s, middle * step, fee, side, evidence)
        if costs['total'] <= target:
            low = middle
        else:
            high = middle - 1
    q = low * step
    _need(q >= minimum and q > 0 and q * e >= F(rules.min_notional), 'NO_LEGAL_MAX_RISK_QUANTITY')
    # Missing depth is missing evidence, never a justification to silently
    # downsize an otherwise larger within-budget order.
    if q + step <= maximum and q + step > depth:
        _need(_cost_values(e, e, s, q, fee, side, evidence)['total'] >= target,
            'SLIPPAGE_DEPTH_INSUFFICIENT')
    return _exact_decimal(q)


def leverage_for(notional, rules, symbol, *, check_fresh=True):
    data = copy.deepcopy(getattr(rules, 'leverage_brackets', None))
    _need(type(data) is dict and type(data.get('schema_version')) is int and data.get('schema_version') == 1
        and data.get('symbol') == symbol and data.get('source') == BRACKET_SOURCE,
        'LEVERAGE_EVIDENCE_UNAVAILABLE')
    _fresh(data.get('observed_at'), check_fresh, 'STALE_LEVERAGE_EVIDENCE')
    rows = data.get('brackets')
    _need(type(rows) is list and rows, 'INVALID_LEVERAGE_EVIDENCE')
    selected = None; previous_cap = F(0)
    for index, row in enumerate(rows):
        _need(type(row) is dict, 'INVALID_LEVERAGE_EVIDENCE')
        try:
            floor = F(_zero_number(row['notional_floor'])); cap = F(number(row['notional_cap']))
            maximum = row['max_leverage']
        except Exception:
            raise Review('INVALID_LEVERAGE_EVIDENCE') from None
        _need(floor >= 0 and floor < cap and floor == previous_cap
            and type(maximum) is int and 1 <= maximum and len(str(maximum)) <= 6, 'INVALID_LEVERAGE_EVIDENCE')
        previous_cap = cap
        if floor <= notional < cap:
            selected = dict(index=index, notional_floor=str(_exact_decimal(floor)),
                notional_cap=str(_exact_decimal(cap)), max_leverage=maximum)
    _need(selected is not None, 'LEVERAGE_NOTIONAL_UNSUPPORTED')
    account_max = data.get('account_max_leverage', max(row['max_leverage'] for row in rows))
    _need(type(account_max) is int and 1 <= account_max and len(str(account_max)) <= 6, 'INVALID_LEVERAGE_EVIDENCE')
    value = min(75, selected['max_leverage'], account_max)
    return value, dict(snapshot=copy.deepcopy(data), selected_bracket=selected,
        account_max_leverage=account_max, selected_leverage=value)


def costs(entry, tp, sl, quantity, rules, side, *, symbol=None, check_fresh=True):
    symbol = symbol or rules.fee_symbol
    evidence = market_evidence(rules, symbol, side, check_fresh=check_fresh)
    fee = F(validated_fee_rate(rules, symbol, check_fresh=check_fresh))
    e, t, s, q = (F(number(v)) for v in (entry, tp, sl, quantity))
    _need(side in ('LONG', 'SHORT') and (s < e < t if side == 'LONG' else t < e < s),
        'INVALID_ENTRY_TP_SL')
    values = _cost_values(e, t, s, q, fee, side, evidence)
    result = dict(risk_model=MODEL, risk=_exact_text(values['total']), total_risk=_exact_text(values['total']),
        gross_risk=_exact_text(values['gross_risk']), gross_reward=_exact_text(values['gross_reward']),
        net_reward=_exact_text(values['reward']), entry_fee_usdt=_exact_text(values['entry_fee']),
        sl_exit_fee_usdt=_exact_text(values['sl_fee']), tp_exit_fee_usdt=_exact_text(values['tp_fee']),
        sl_book_impact_usdt=_exact_text(values['sl_book']), tp_book_impact_usdt=_exact_text(values['tp_book']),
        sl_liquidity_uncertainty_usdt=_exact_text(values['sl_unknown']),
        tp_liquidity_uncertainty_usdt=_exact_text(values['tp_unknown']),
        sl_slippage_usdt=_exact_text(values['sl_book'] + values['sl_unknown']),
        tp_slippage_usdt=_exact_text(values['tp_book'] + values['tp_unknown']),
        sl_execution_price=_exact_text(values['sl_fill']), tp_execution_price=_exact_text(values['tp_fill']),
        exit_slippage_rate=_exact_text(values['reserve']), entry_slippage_rate='0',
        entry_fee_rate=format(rules.taker_fee_rate, 'f'), exit_fee_rate=format(rules.taker_fee_rate, 'f'),
        tp_fee_rate=format(rules.taker_fee_rate, 'f'), fee_symbol=rules.fee_symbol,
        fee_source=rules.fee_source, fee_observed_at=rules.fee_observed_at,
        rr=str(_ratio_decimal(abs(t-e)/abs(e-s))),
        net_rr=str(_ratio_decimal(values['reward']/values['total'])),
        sizing_method='MAX_LOT_DYNAMIC_CLOSE_COST_V4', excluded_costs=['FUNDING','GAPS_BEYOND_EMPIRICAL_RESERVE'])
    result['cost_evidence'] = dict(version=MODEL, market=copy.deepcopy(evidence['raw']),
        closing_side=evidence['closing_side'], expected_book_impact_rate=_exact_text(values['impact']),
        future_liquidity_uncertainty_rate=_exact_text(evidence['uncertainty']),
        price_move_q95_rate=_exact_text(evidence['move_reserve']),
        execution_excess_q95_rate=_exact_text(evidence['execution_reserve']),
        price_move_sample_count=evidence['price_move_count'], execution_sample_count=evidence['execution_sample_count'],
        uncertainty_observation_horizons_seconds=evidence['horizons'],
        uncertainty_method='MAX_EMPIRICAL_ADVERSE_MOVE_Q95_AND_EXECUTION_EXCESS_Q95',
        execution_history_min_samples=MIN_SAMPLE_COUNT, rate_rounding='CEILING_12_DECIMAL_PLACES',
        fee_source=rules.fee_source, fee_symbol=symbol, fee_observed_at=rules.fee_observed_at,
        taker_fee_rate=format(rules.taker_fee_rate, 'f'),
        entry_slippage_reason='LIMIT_PRICE_BOUNDS_ADVERSE_ENTRY_PRICE',
        warning='Observed reserves are estimates, not guarantees of future SL liquidity or realized PNL.')
    return result


def plan(signal, rules, risk_target, *, check_fresh=True):
    from .core import _validate_normalized_signal, DynamicRules
    _need(isinstance(rules,DynamicRules), 'INVALID_RULES_SCHEMA')
    rules = replace(rules, dynamic_cost_evidence=copy.deepcopy(rules.dynamic_cost_evidence),
        leverage_brackets=copy.deepcopy(rules.leverage_brackets))
    # Preserve historical V3 validators. V4 validates original ENTRY/SL and a
    # directional TP proposal, but does not impose its old pre-normalization RR.
    _validate_normalized_signal(signal, rules, check_fresh=check_fresh, require_original_rr=False)
    target = validated_risk_target(risk_target)
    quantity = maximum_quantity(signal.entry, signal.sl, signal.side, rules, target,
        symbol=signal.symbol, check_fresh=check_fresh)
    evidence = market_evidence(rules, signal.symbol, signal.side, check_fresh=check_fresh)
    fee = F(validated_fee_rate(rules, signal.symbol, check_fresh=check_fresh))
    e, q, tick = F(signal.entry), F(quantity), F(rules.tick)
    impact, _ = book_impact(q, evidence)
    reserve = impact + evidence['uncertainty']
    _need(reserve < 1, 'INVALID_COST_EVIDENCE')
    ideal = ((e*(1+fee)+2*F(target)/q)/((1-reserve)*(1-fee)) if signal.side=='LONG'
        else (e*(1-fee)-2*F(target)/q)/((1+reserve)*(1+fee)))
    _need(F(rules.min_price) <= ideal <= F(rules.max_price), 'NO_LEGAL_NET_RR_TP')
    count = ideal // tick
    candidates = []
    for n in (count, count+1):
        price = n*tick
        if price <= 0 or not (price > e if signal.side=='LONG' else price < e):
            continue
        if not F(rules.min_price) <= price <= F(rules.max_price):
            continue
        values = _cost_values(e, price, F(signal.sl), q, fee, signal.side, evidence)
        candidates.append((abs(values['reward']-2*F(target)), -values['reward'], price, values))
    _need(candidates, 'NO_LEGAL_NET_RR_TP')
    _, _, tp, values = min(candidates, key=lambda item:(item[0], item[1]))
    selected_leverage, leverage = leverage_for(q*e, rules, signal.symbol, check_fresh=check_fresh)
    result = dict(symbol=signal.symbol, side=signal.side, entry=format(signal.entry,'f'),
        sl=format(signal.sl,'f'), tp=_exact_text(tp), neurobro_tp=format(signal.tp,'f'),
        execution_tp=_exact_text(tp), quantity=format(quantity,'f'), execution_quantity=format(quantity,'f'),
        margin_mode='CROSS', leverage=selected_leverage, order_type='LIMIT', mode='ORDER_INTENT',
        risk_target_usdt=format(target,'f'), rules_checked_at=rules.observed_at,
        reward_risk_policy=POLICY, policy_version=MODEL,
        target_net_reward_usdt=_exact_text(2*F(target)),
        net_reward_target_difference_usdt=_exact_text(values['reward']-2*F(target)),
        leverage_evidence=leverage,
        tp_normalization=dict(version=MODEL, source='OWNER_AUTHORIZED_RISK_MANAGER',
            model_entry=format(signal.entry,'f'), model_sl=format(signal.sl,'f'),
            model_tp=format(signal.tp,'f'), execution_tp=_exact_text(tp),
            model_gross_rr=str(_ratio_decimal(abs(F(signal.tp)-e)/abs(e-F(signal.sl)))),
            nominal_risk_target_usdt=format(target,'f'), target_net_reward_usdt=_exact_text(2*F(target)),
            net_reward_target_difference_usdt=_exact_text(values['reward']-2*F(target)),
            tick_size=format(rules.tick,'f'), rounding='NEAREST_NET_USDT_TARGET_TICK', risk_model=MODEL),
        **costs(signal.entry,_exact_decimal(tp),signal.sl,quantity,rules,signal.side,
            symbol=signal.symbol,check_fresh=check_fresh))
    return result


def validate(plan_value, rules, *, check_fresh=False):
    try:
        _need(plan_value.get('risk_model') == MODEL and plan_value.get('policy_version') == MODEL
            and plan_value.get('reward_risk_policy') == POLICY, 'INVALID_COST_MODEL')
        original = plan_value['tp_normalization']
        signal = Signal(plan_value['symbol'], plan_value['side'], number(original['model_entry']),
            number(original['model_tp']), number(original['model_sl']))
        expected = plan(signal, rules, plan_value['risk_target_usdt'], check_fresh=check_fresh)
        for key, value in expected.items():
            _need(plan_value.get(key) == value, 'ORDER_COSTS_CHANGED')
        return True
    except Review:
        raise
    except Exception:
        raise Review('INVALID_COST_MODEL') from None


def estimate_existing(plan_value, fresh_rules):
    """Return a separate monitoring estimate; never return replacement levels."""
    side = plan_value['side']; symbol = plan_value['symbol']
    result = costs(number(plan_value['entry']), number(plan_value['tp']), number(plan_value['sl']),
        number(plan_value['quantity']), fresh_rules, side, symbol=symbol, check_fresh=True)
    target = validated_risk_target(plan_value['risk_target_usdt'])
    return dict(kind='MONITORING_ESTIMATE_ONLY', symbol=symbol,
        estimated_total_risk=result['risk'], original_risk_target_usdt=format(target,'f'),
        estimated_budget_difference_usdt=_exact_text(F(D(result['risk']))-F(target)),
        components={key:result[key] for key in ('gross_risk','entry_fee_usdt','sl_exit_fee_usdt',
            'sl_book_impact_usdt','sl_liquidity_uncertainty_usdt')},
        cost_evidence=result['cost_evidence'], order_parameters_changed=False)
