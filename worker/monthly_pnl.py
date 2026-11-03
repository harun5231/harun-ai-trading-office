"""Selected-month realized trading income from the existing private history cache.

Not closed-position PNL, unrealized PNL, a balance, or withdrawable funds.
No client is constructed and no provider requests are made. Coverage comes only
from the collector's exhausted, unfiltered income-history pagination windows.
"""
import calendar
import hashlib
import time
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any

from .binance_private import read_secret
from .monthly_billing import month_key
from .pnl_calendar import HistoryCache, WIB, _covers, _date, _fixed, _number


# Income amounts are already signed; COMMISSION is normally negative.
# Use income ONLY: fills contain the same realized PNL/commission facts.
TRADING_TYPES = ('REALIZED_PNL', 'COMMISSION', 'FUNDING_FEE', 'INSURANCE_CLEAR',
                 'COMMISSION_REBATE', 'FEE_RETURN')
# API_REBATE is broker/referral partner income, not this account's fee refund.
# Transfers, promotional rewards and BFUSD yield are not realized trading PNL.
EXCLUDED_TYPES = frozenset(('TRANSFER', 'INTERNAL_TRANSFER', 'CROSS_COLLATERAL_TRANSFER',
                           'STRATEGY_UMFUTURES_TRANSFER', 'WELCOME_BONUS', 'CONTEST_REWARD',
                           'REFERRAL_KICKBACK', 'API_REBATE', 'BFUSD_REWARD'))


def read_month(directory, month, *, now_ms=None):
    """Fail closed on missing/private/credential-mismatched or unproven evidence."""
    month_key(month)
    year, number = map(int, month.split('-'))
    count = calendar.monthrange(year, number)[1]
    start = datetime(year, number, 1, tzinfo=WIB)
    finish = start + timedelta(days=count)
    first, last = int(start.timestamp()) * 1000, int(finish.timestamp()) * 1000 - 1
    result: dict[str, Any] = dict(source='BINANCE_FUTURES', kind='MONTHLY_NET_TRADING_INCOME',
                  timezone='Asia/Jakarta', start_date=month+'-01', end_date=f'{month}-{count:02}',
                  complete=False, days=[], components_usdt=None, excluded_income_types=[],
                  incomplete_reasons=[], coverage=dict(start_ms=first, end_ms=last,
                                                       income_history_complete=False))
    try:
        identity = hashlib.sha256(read_secret('BINANCE_API_KEY_FILE').encode()).hexdigest()
        with HistoryCache(directory, identity) as cache:
            state: Any = cache.load()
        covered = _covers(state['income_coverage'], first, last)
        result['coverage']['income_history_complete'] = covered
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if type(now) is not int or now <= last:
            result['incomplete_reasons'].append('MONTH_NOT_CLOSED')
        if not covered:
            result['incomplete_reasons'].append('MONTH_INCOME_HISTORY_INCOMPLETE')
        with localcontext() as precision:
            precision.prec = 256
            totals = {kind: Decimal(0) for kind in TRADING_TYPES}
            days = {f'{month}-{day:02}': Decimal(0) for day in range(1, count+1)}
            excluded = set()
            for row in state['incomes'].values():
                if not first <= row['time'] <= last:
                    continue
                kind = row['incomeType']
                if kind in EXCLUDED_TYPES:
                    excluded.add(kind)
                elif kind not in TRADING_TYPES:
                    result['incomplete_reasons'].append('UNKNOWN_INCOME_TYPE')
                elif row['asset'] != 'USDT':
                    result['incomplete_reasons'].append('NON_USDT_TRADING_INCOME')
                else:
                    amount = _number(row['income'])
                    totals[kind] += amount
                    days[_date(row['time'])] += amount
            result['excluded_income_types'] = sorted(excluded)
            if not result['incomplete_reasons']:
                result.update(complete=True, components_usdt={k: _fixed(v) for k, v in totals.items()},
                              days=[dict(date=k, complete=True, pnl_usdt=_fixed(v)) for k, v in days.items()])
    except Exception:
        result['incomplete_reasons'].append('MONTH_INCOME_CACHE_UNAVAILABLE')
    result['incomplete_reasons'] = list(dict.fromkeys(result['incomplete_reasons']))
    return result
