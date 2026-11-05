"""Factual projections of durable closed-position evidence, never trade policy.

Closed-position net PNL already includes trading fees/funding/insurance. Operating
costs and account equity are not supplied by this journal and remain unknown.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo


def net_pnl(row):
    if row.get('complete') is not True or row.get('pnl_usdt') is None:
        return None
    try:
        raw = row['pnl_usdt']
        if not isinstance(raw, str) or len(raw) > 64:
            raise ValueError
        value = Decimal(raw)
        if not value.is_finite():
            raise ValueError
        return value
    except (ValueError, InvalidOperation):
        raise ValueError('CLOSED_NET_PNL_INVALID') from None


def fixed(value):
    return format(value, 'f') if value is not None else None


def actual_reward_risk(binding):
    """Realized net R against the initial SL and actually filled entry/quantity.

    This is not the exit-distance RR or the strategy's requested reward target.
    A partial fill uses its filled quantity; zero/missing invalidation is unknown.
    """
    try:
        execution = binding['execution']
        with localcontext() as context:
            context.prec = 160
            entry, stop, quantity, pnl = (Decimal(str(value)) for value in
                (execution['entry'], execution['sl'], execution['quantity'], binding['outcome']['pnl_usdt']))
            if (not all(v.is_finite() for v in (entry, stop, quantity, pnl))
                    or entry <= 0 or stop <= 0 or quantity <= 0 or entry == stop):
                return None
            return fixed(pnl / (abs(entry - stop) * quantity))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


def decline_windows(rows):
    """Six actual closes: last three have fewer wins AND lower mean net PNL.

    This fixed comparable sample is an observation trigger only. Unknown closes
    break comparison, and every close has at most one stable aggregate job.
    """
    for index in range(5, len(rows)):
        window = rows[index - 5:index + 1]
        values = [net_pnl(row) for row in window]
        if any(value is None for value in values):
            continue
        with localcontext() as context:
            context.prec = 160
            before, recent = values[:3], values[3:]
            wins_before, wins_recent = sum(v > 0 for v in before), sum(v > 0 for v in recent)
            if wins_recent < wins_before and sum(recent) < sum(before):
                yield window, dict(comparison='LAST_3_VS_PRECEDING_3_OBSERVED_CLOSES',
                    baseline_wins=wins_before, recent_wins=wins_recent,
                    baseline_net_pnl_usdt=fixed(sum(before)), recent_net_pnl_usdt=fixed(sum(recent)),
                    experiment_status='NOT_RUN')


def monthly_metrics(trades, bindings):
    from .trade_evaluation_evidence import digest
    groups = {}
    for row in sorted(trades.values(), key=lambda value: (value['closed_at'], value['id'])):
        # The calendar's own close_date is WIB. Reconstruct from the exact
        # source instant so a stale display field cannot change month attribution.
        instant = datetime.fromisoformat(row['closed_at'].replace('Z', '+00:00'))
        month = instant.astimezone(ZoneInfo('Asia/Jakarta')).strftime('%Y-%m')
        groups.setdefault(month, []).append(row)
    result = []
    with localcontext() as context:
        context.prec = 160
        for month, rows in sorted(groups.items()):
            values = [net_pnl(row) for row in rows]
            known = [value for value in values if value is not None]
            complete = len(known) == len(rows)
            wins, losses = sum(v > 0 for v in known), sum(v < 0 for v in known)
            cumulative = peak = drawdown = Decimal(0)
            day_values = {}
            for row, value in zip(rows, values):
                if value is None:
                    continue
                cumulative += value
                peak = max(peak, cumulative)
                drawdown = max(drawdown, peak - cumulative)
                day = datetime.fromisoformat(row['closed_at'].replace('Z', '+00:00')).astimezone(
                    ZoneInfo('Asia/Jakarta')).date().isoformat()
                day_values[day] = day_values.get(day, Decimal(0)) + value
            realized_r = []
            for row in rows:
                binding = bindings.get(row['id'])
                if binding is not None and binding.get('closed_position_sha256') == digest(row):
                    value = actual_reward_risk(binding)
                    if value is not None:
                        realized_r.append(Decimal(value))
            result.append(dict(month=month, timezone='Asia/Jakarta', scope='OBSERVED_CLOSED_POSITIONS',
                closed_positions=len(rows), complete=complete, wins=wins, losses=losses,
                breakeven=len(known)-wins-losses, unknown_positions=len(rows)-len(known),
                win_rate=fixed(Decimal(wins)/len(rows)) if complete else None,
                net_pnl_usdt=fixed(sum(known, Decimal(0))) if complete else None,
                known_closed_net_pnl_usdt=fixed(sum(known, Decimal(0))),
                closed_net_pnl_drawdown_usdt=fixed(drawdown) if complete else None,
                equity_drawdown_percent=None,
                mean_actual_net_r=fixed(sum(realized_r)/len(realized_r)) if realized_r else None,
                actual_net_r_positions=len(realized_r),
                profitable_close_day_ratio=fixed(Decimal(sum(v > 0 for v in day_values.values()))/
                    len(day_values)) if complete and day_values else None,
                operational_cost_usdt=None, after_operational_cost_net_usdt=None,
                missing_reasons=['OPERATING_COST_EVIDENCE_NOT_LINKED', 'ACCOUNT_EQUITY_PATH_NOT_LINKED'] +
                    ([] if complete else ['CLOSED_NET_PNL_INCOMPLETE']) +
                    ([] if len(realized_r)==len(rows) else ['INITIAL_SL_AND_FILLED_ENTRY_NOT_LINKED']),
                calculation_method='CLOSED_NET_PNL_ALREADY_INCLUDES_TRADING_COSTS_ONCE'))
    return result
