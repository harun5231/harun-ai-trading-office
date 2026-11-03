"""Owner-entered monthly expenses. Never contacts providers or the order pipeline."""
import calendar
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .secure_sqlite_snapshot import snapshot_database


PAYMENT_ROUTES = {
    'platform.openai.com': ('/settings/organization/billing/',),
    'hpanel.hostinger.com': ('/',),
    'console.hetzner.cloud': ('/',),
    'accounts.hetzner.com': ('/',),
    'my.vultr.com': ('/',),
    'cloud.digitalocean.com': ('/',),
}


def payment_url_allowed(value):
    """Conservative portal allowlist, not a claim that an invoice was verified."""
    if not isinstance(value, str) or len(value) > 500 or re.search(r'[\s\\\\%?#]', value):
        return False
    url = urlsplit(value)
    return (url.scheme == 'https' and url.netloc in PAYMENT_ROUTES and
            any((url.path or '/').startswith(prefix) for prefix in PAYMENT_ROUTES[url.netloc]) and
            not any(segment in ('.', '..') for segment in url.path.split('/')))


def month_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])', value):
        raise ValueError('INVALID_BILLING_MONTH')
    return value


def money(value, positive=False):
    if value is None:
        return
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,15}(?:\.\d{1,8})?', value) or positive and Decimal(value) <= 0:
        raise ValueError('INVALID_BILLING_AMOUNT')


def text(value, limit=500):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(char) < 32 for char in value):
        raise ValueError('INVALID_BILLING_TEXT')


def validate(value, today=None):
    today = today or datetime.now(timezone(timedelta(hours=7))).date()
    if not isinstance(value, dict) or set(value) - {'cashflow'} != {'month', 'coverage_confirmed', 'fx_idr_per_usdt', 'fx_source', 'bills'}:
        raise ValueError('INVALID_BILLING_DOCUMENT')
    if 'cashflow' in value:
        fees = value['cashflow']
        if not isinstance(fees, dict) or set(fees) != {'payment_fee_actual_idr', 'payment_fee_estimated_idr', 'withdrawal_fee_actual_usdt', 'withdrawal_fee_estimated_usdt', 'withdrawal_fee_percent'}:
            raise ValueError('INVALID_CASHFLOW_FEES')
        for amount in fees.values():
            money(amount)
        if fees['withdrawal_fee_percent'] is not None and Decimal(fees['withdrawal_fee_percent']) >= 100:
            raise ValueError('INVALID_WITHDRAWAL_PERCENT')
    month_key(value['month'])
    if type(value['coverage_confirmed']) is not bool or not isinstance(value['bills'], list) or len(value['bills']) > 30:
        raise ValueError('INVALID_BILLING_DOCUMENT')
    money(value['fx_idr_per_usdt'], True)
    if value['fx_idr_per_usdt'] is not None:
        text(value['fx_source'])
    elif value['fx_source'] is not None:
        raise ValueError('FX_SOURCE_WITHOUT_RATE')
    names = set()
    for row in value['bills']:
        if not isinstance(row, dict) or set(row) - {'usage'} != {'name', 'kind', 'actual_idr', 'estimated_idr', 'due_date', 'payment_url', 'paid', 'evidence'}:
            raise ValueError('INVALID_BILL')
        if 'usage' in row:
            usage = row['usage']
            if row['kind'] != 'prepaid_usage' or not isinstance(usage, dict) or set(usage) != {'through_date', 'available_credit_idr', 'credit_evidence'}:
                raise ValueError('INVALID_USAGE_EVIDENCE')
            through = usage['through_date']
            if not isinstance(through, str) or date.fromisoformat(through).isoformat() != through or not through.startswith(value['month']+'-') or date.fromisoformat(through) >= today:
                raise ValueError('INVALID_USAGE_DATE')
            money(usage['available_credit_idr'])
            if usage['available_credit_idr'] is not None:
                text(usage['credit_evidence'])
            elif usage['credit_evidence'] is not None:
                raise ValueError('CREDIT_EVIDENCE_WITHOUT_AMOUNT')
        text(row['name'], 80); text(row['evidence'])
        if row['name'].casefold() in names or row['kind'] not in ('subscription', 'prepaid_usage'):
            raise ValueError('INVALID_BILL')
        names.add(row['name'].casefold())
        money(row['actual_idr']); money(row['estimated_idr'])
        if row['paid'] is not None and type(row['paid']) is not bool:
            raise ValueError('INVALID_PAYMENT_STATUS')
        if row['kind'] == 'prepaid_usage' and (row['paid'] is not None or row['due_date'] is not None):
            raise ValueError('PREPAID_USAGE_IS_NOT_AN_INVOICE')
        if row['due_date'] is not None:
            if not isinstance(row['due_date'], str) or date.fromisoformat(row['due_date']).isoformat() != row['due_date'] or not row['due_date'].startswith(value['month']+'-'):
                raise ValueError('INVALID_DUE_DATE')
        if row['payment_url'] is not None:
            text(row['payment_url'], 500)
            if not payment_url_allowed(row['payment_url']):
                raise ValueError('INVALID_PAYMENT_URL')


class Store:
    """Append-only revisions in a dedicated DB, optimistic version check per month."""
    def __init__(self, directory):
        self.path = Path(directory)/'monthly-billing.sqlite3'

    def connect(self, *, write=False):
        """Context-bound memory snapshot; never give SQLite a filesystem path."""
        return snapshot_database(self.path.parent, self.path.name, write=write)

    def history(self, month):
        month_key(month)
        with self.connect() as db:
            if db is None:
                return []
            return [dict(version=version, recorded_at=at, document=json.loads(raw)) for version, at, raw in db.execute('SELECT version, recorded_at, document FROM revisions WHERE month=? ORDER BY version', (month,))]

    def read(self, month):
        rows = self.history(month)
        return rows[-1] if rows else dict(version=0, recorded_at=None, document=dict(month=month, coverage_confirmed=False, fx_idr_per_usdt=None, fx_source=None, bills=[]))

    def save(self, value, version):
        validate(value)
        if type(version) is not int or version < 0:
            raise ValueError('INVALID_BILLING_VERSION')
        with self.connect(write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS revisions (month TEXT NOT NULL, version INTEGER NOT NULL, recorded_at TEXT NOT NULL, document TEXT NOT NULL, PRIMARY KEY(month, version))')
            db.execute('BEGIN IMMEDIATE')
            latest = db.execute('SELECT MAX(version) FROM revisions WHERE month=?', (value['month'],)).fetchone()[0] or 0
            if latest != version:
                raise ValueError('BILLING_VERSION_CONFLICT')
            at = datetime.now(timezone.utc).isoformat()
            db.execute('INSERT INTO revisions VALUES (?,?,?,?)', (value['month'], version+1, at, json.dumps(value, allow_nan=False)))
            db.commit()
            return dict(version=version+1, recorded_at=at, document=value)


def extend_handler(base, controller, origin):
    """Add isolated billing routes, delegating every pre-existing route unchanged."""
    from urllib.parse import parse_qs
    from .http_client import unique

    class BillingHandler(base):
        def billing_route(self):
            return urlsplit(self.path).path in ('/billing/month', '/billing/history')

        def billing_auth(self, control=False):
            return len(self.headers.get_all('Authorization', [])) == 1 and self.authorized(control)

        def do_OPTIONS(self):
            if not self.billing_route():
                return super().do_OPTIONS()
            if self.headers.get_all('Origin', []) != [origin]:
                return self.reply(403, {'error': 'FORBIDDEN'})
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.end_headers()

        def do_GET(self):
            if not self.billing_route():
                return super().do_GET()
            if not self.billing_auth():
                return self.reply(403, {'error': 'FORBIDDEN'})
            try:
                query = parse_qs(urlsplit(self.path).query, strict_parsing=True)
                if set(query) != {'month'} or len(query['month']) != 1:
                    raise ValueError('INVALID_BILLING_MONTH')
                month = month_key(query['month'][0])
                store = Store(controller.directory)
                if urlsplit(self.path).path == '/billing/history':
                    return self.reply(200, {'revisions': store.history(month)})
                record = store.read(month)
                try:
                    from .monthly_pnl import read_month
                    summary = summarize(record['document'], read_month(controller.directory, month))
                except Exception:
                    summary = summarize(record['document'], {})
                return self.reply(200, dict(record, summary=summary))
            except ValueError:
                return self.reply(400, {'error': 'INVALID_BILLING_REQUEST'})
            except Exception:
                return self.reply(503, {'error': 'BILLING_UNAVAILABLE'})

        def do_POST(self):
            if not self.billing_route():
                return super().do_POST()
            if not self.billing_auth(True) or self.headers.get_all('Origin', []) != [origin]:
                return self.reply(403, {'error': 'FORBIDDEN'})
            try:
                if self.path != '/billing/month':
                    raise ValueError('INVALID_BILLING_REQUEST')
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 32768 or self.headers.get('Transfer-Encoding') or self.headers.get_all('Content-Type', []) != ['application/json']:
                    raise ValueError('INVALID_BILLING_REQUEST')
                self.connection.settimeout(5)
                value = json.loads(self.rfile.read(int(lengths[0])), object_pairs_hook=unique,
                                   parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(value, dict) or set(value) != {'document', 'version'}:
                    raise ValueError('INVALID_BILLING_REQUEST')
                result = Store(controller.directory).save(value['document'], value['version'])
                return self.reply(200, result)
            except ValueError as error:
                conflict = str(error) == 'BILLING_VERSION_CONFLICT'
                return self.reply(409 if conflict else 400, {'error': 'BILLING_VERSION_CONFLICT' if conflict else 'INVALID_BILLING_REQUEST'})
            except Exception:
                return self.reply(503, {'error': 'BILLING_UNAVAILABLE'})

    return BillingHandler


def summarize(value, pnl):
    """Accrual costs are not topups. Verified income PNL already includes trading fees."""
    with localcontext() as context:
        context.prec = 60
        return _summarize(value, pnl)


def _summarize(value, pnl):
    from decimal import ROUND_CEILING
    bills = value['bills']
    month = value['month']
    year, number = map(int, month.split('-'))
    count = calendar.monthrange(year, number)[1]
    expected = {f'{month}-{day:02}' for day in range(1, count+1)}
    fees = value.get('cashflow', {})
    def amount(raw):
        return Decimal(raw) if raw is not None else None
    def total(items):
        return sum(items, Decimal(0)) if all(item is not None for item in items) else None
    actuals, estimates, projected_usage, topups = [], [], [], []
    for row in bills:
        actual, estimate = amount(row['actual_idr']), amount(row['estimated_idr'])
        if row['kind'] == 'prepaid_usage':
            usage = row.get('usage')
            if usage and actual is not None:
                elapsed = date.fromisoformat(usage['through_date']).day
                estimate = actual * count / elapsed
                credit = amount(usage['available_credit_idr'])
                topups.append(max(estimate-actual-credit, Decimal(0)) if credit is not None else None)
                if elapsed != count:
                    actual = None
            else:
                topups.append(None)
                actual = None
            projected_usage.append(estimate)
        else:
            estimate = actual if actual is not None else estimate
        actuals.append(actual); estimates.append(estimate)
    covered = bool(bills) and value['coverage_confirmed']
    actual = total(actuals+[amount(fees.get('payment_fee_actual_idr'))]) if covered else None
    estimated = total(estimates+[amount(fees.get('payment_fee_actual_idr')), amount(fees.get('payment_fee_estimated_idr'))]) if covered else None
    days = [row for row in pnl.get('days', []) if row.get('date', '').startswith(month+'-')]
    first_ms = int(datetime(year, number, 1, tzinfo=timezone(timedelta(hours=7))).timestamp()) * 1000
    last_ms = first_ms + count * 86_400_000 - 1
    coverage = pnl.get('coverage', {})
    complete = (pnl.get('complete') is True and pnl.get('source') == 'BINANCE_FUTURES' and
                pnl.get('kind') == 'MONTHLY_NET_TRADING_INCOME' and
                isinstance(coverage, dict) and coverage.get('income_history_complete') is True and
                type(coverage.get('start_ms')) is int and type(coverage.get('end_ms')) is int and
                coverage == dict(start_ms=first_ms, end_ms=last_ms, income_history_complete=True) and
                pnl.get('timezone') == 'Asia/Jakarta' and pnl.get('start_date') == month+'-01' and
                pnl.get('end_date') == f'{month}-{count:02}' and
                {row['date'] for row in days} == expected and len(days) == len(expected) and
                all(row.get('complete') is True and isinstance(row.get('pnl_usdt'), str) and
                    re.fullmatch(r'-?\d{1,24}(?:\.\d{1,8})?', row['pnl_usdt']) for row in days))
    trading = sum((Decimal(row['pnl_usdt']) for row in days), Decimal(0)) if complete else None
    unpaid = [row for row in bills if row['kind'] == 'subscription' and row['paid'] is not True]
    amounts = [amount(row['actual_idr'] if row['actual_idr'] is not None else row['estimated_idr']) for row in unpaid]
    fx = amount(value['fx_idr_per_usdt'])
    topup = total(topups) if covered else None
    needed = total(amounts+topups+[amount(fees.get('payment_fee_estimated_idr'))]) if covered else None
    fixed = amount(fees.get('withdrawal_fee_estimated_usdt'))
    percent = amount(fees.get('withdrawal_fee_percent'))
    withdrawal = withdrawal_fee = None
    if fx and needed is not None and fixed is not None and percent is not None and all(row['paid'] is not None for row in unpaid):
        # Percentage is deducted from the gross withdrawal; round funding UP, never short-pay.
        base = needed/fx
        withdrawal = (base+fixed)/(1-percent/100) if needed else Decimal(0)
        withdrawal_fee = withdrawal-base
    actual_withdrawal_fee = amount(fees.get('withdrawal_fee_actual_usdt'))
    net = trading-actual/fx-actual_withdrawal_fee if trading is not None and actual is not None and fx and actual_withdrawal_fee is not None else None
    projected = trading-estimated/fx-actual_withdrawal_fee-withdrawal_fee if trading is not None and estimated is not None and fx and actual_withdrawal_fee is not None and withdrawal_fee is not None else None
    return dict(actual_cost_idr=str(actual) if actual is not None else None,
                estimated_cost_idr=str(estimated) if estimated is not None else None,
                prepaid_projected_usage_idr=str(total(projected_usage)) if covered and total(projected_usage) is not None else None,
                prepaid_topup_estimate_idr=str(topup) if topup is not None else None,
                withdrawal_estimate_usdt=str(withdrawal.quantize(Decimal('.00000001'), rounding=ROUND_CEILING)) if withdrawal is not None else None,
                estimated_withdrawal_fee_usdt=format(withdrawal_fee, '.8f') if withdrawal_fee is not None else None,
                trading_net_usdt=str(trading) if trading is not None else None,
                net_profit_usdt=format(net, '.8f') if net is not None else None,
                estimated_net_profit_usdt=format(projected, '.8f') if projected is not None else None,
                openai_balance=None)
