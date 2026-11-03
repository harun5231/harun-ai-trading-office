"""Local synthetic billing records only; no provider access or financial journal."""
import calendar
import copy
import importlib
import tempfile
import unittest
from typing import Any
from pathlib import Path


def document() -> dict[str, Any]:
    return dict(month='2026-10', coverage_confirmed=True, fx_idr_per_usdt='16000',
                fx_source='Synthetic owner quote 2026-10-07',
                cashflow=dict(payment_fee_actual_idr='0', payment_fee_estimated_idr='0',
                              withdrawal_fee_actual_usdt='0', withdrawal_fee_estimated_usdt='0',
                              withdrawal_fee_percent='0'), bills=[
                    dict(name='Synthetic VPS', kind='subscription', actual_idr='160000',
                         estimated_idr='170000', due_date='2026-10-15', payment_url=None,
                         paid=False, evidence='Synthetic invoice'),
                    dict(name='OpenAI', kind='prepaid_usage', actual_idr='32000',
                         estimated_idr='64000', due_date=None,
                         payment_url='https://platform.openai.com/settings/organization/billing/overview',
                         paid=None, evidence='Synthetic usage export; cumulative month-start usage in IDR',
                         usage=dict(through_date='2026-10-31', available_credit_idr='0',
                                    credit_evidence='Synthetic final-month official snapshot'))])


def pnl() -> dict[str, Any]:
    from tests.test_monthly_pnl import START, FINISH
    return dict(complete=True, source='BINANCE_FUTURES', timezone='Asia/Jakarta',
                kind='MONTHLY_NET_TRADING_INCOME',
                coverage=dict(start_ms=START, end_ms=FINISH, income_history_complete=True),
                start_date='2026-10-01', end_date='2026-10-31',
                days=[dict(date=f'2026-10-{day:02}', complete=True, pnl_usdt='10' if day == 1 else '0',
                           commission_usdt='-1') for day in range(1, 32)])


class MonthlyBilling(unittest.TestCase):
    def module(self):
        import worker
        self.assertTrue((Path(worker.__file__).parent/'monthly_billing.py').exists(),
                        'monthly billing backend missing')
        return importlib.import_module('worker.monthly_billing')

    def test_actual_estimate_withdrawal_and_no_second_exchange_fee(self):
        result = self.module().summarize(document(), pnl())
        self.assertEqual(result['actual_cost_idr'], '192000')
        self.assertEqual(result['estimated_cost_idr'], '192000')
        self.assertEqual(result['withdrawal_estimate_usdt'], '10.00000000')
        self.assertEqual(result['net_profit_usdt'], '-2.00000000')
        self.assertEqual(result['trading_net_usdt'], '10')
        self.assertIsNone(result['openai_balance'])
    def test_usage_evidence_drives_topup_projection_with_explicit_cashflow_fees(self):
        value = document()
        value['cashflow'] = dict(payment_fee_actual_idr='1000', payment_fee_estimated_idr='2000',
                                 withdrawal_fee_actual_usdt='0.1', withdrawal_fee_estimated_usdt='1',
                                 withdrawal_fee_percent='1')
        value['bills'][1]['usage'] = dict(through_date='2026-10-08', available_credit_idr='24000',
                                        credit_evidence='Owner-entered official credit snapshot 2026-10-08')
        billing = self.module(); billing.validate(value)
        result = billing.summarize(value, pnl())
        # 32k consumed / 8 elapsed days * 31 days = 124k; remaining 92k - credit 24k = 68k.
        self.assertEqual(result['prepaid_projected_usage_idr'], '124000')
        self.assertEqual(result['prepaid_topup_estimate_idr'], '68000')
        # (160k subscription + 68k topup + 2k payment fee) / 16k + fixed fee, grossed up 1%.
        self.assertEqual(result['withdrawal_estimate_usdt'], '15.53030304')
        self.assertIsNone(result['actual_cost_idr'])  # partial-month usage is not final actual
        self.assertIsNone(result['net_profit_usdt'])
        self.assertEqual(result['estimated_cost_idr'], '287000')
        self.assertEqual(result['estimated_net_profit_usdt'], '-9.19280303')
        self.assertIsNone(result['openai_balance'])  # owner snapshot is not automatic provider verification
        value['bills'][1]['usage']['available_credit_idr'] = None
        value['bills'][1]['usage']['credit_evidence'] = None
        self.assertIsNone(billing.summarize(value, pnl())['withdrawal_estimate_usdt'])
        self.assertIsNone(billing.summarize(value, pnl())['prepaid_topup_estimate_idr'])

    def test_usage_dates_must_be_closed_wib_days_and_legacy_usage_is_not_final(self):
        from datetime import date
        billing = self.module()
        value = document(); del value['bills'][1]['usage']
        self.assertIsNone(billing.summarize(value, pnl())['actual_cost_idr'])
        for day in ['2026-11-01', '2026-10-32', '2026-10-03', '2026-10-04', '20261002', '2026-10-2']:
            value['bills'][1]['usage'] = dict(through_date=day, available_credit_idr='0', credit_evidence='Owner snapshot')
            with self.subTest(day=day), self.assertRaises(ValueError): billing.validate(value, today=date(2026, 10, 3))
        value['bills'][1]['usage']['through_date'] = '2026-10-02'
        billing.validate(value, today=date(2026, 10, 3))

    def test_withdrawal_ceiling_does_not_turn_funding_dust_into_an_expense(self):
        value = document(); value['bills'] = value['bills'][:1]
        value['fx_idr_per_usdt'] = '3'; value['bills'][0]['actual_idr'] = '1'
        result = self.module().summarize(value, pnl())
        self.assertEqual(result['withdrawal_estimate_usdt'], '0.33333334')
        self.assertEqual(result['estimated_withdrawal_fee_usdt'], '0.00000000')
        self.assertEqual(result['estimated_net_profit_usdt'], '9.66666667')

    def test_nonfinite_calendar_values_do_not_become_profit(self):
        for amount in ['NaN', 'Infinity', '-Infinity', True, 1.5, 'not-money']:
            calendar_value = pnl(); calendar_value['days'][0]['pnl_usdt'] = amount
            with self.subTest(amount=amount):
                result = self.module().summarize(document(), calendar_value)
                self.assertIsNone(result['trading_net_usdt'])
                self.assertIsNone(result['net_profit_usdt'])

    def test_conflicting_calendar_period_or_timezone_is_not_a_monthly_pnl(self):
        for field, bad in [('timezone', 'UTC'), ('start_date', '2026-10-02'),
                           ('end_date', '2026-10-30'), ('source', 'UNVERIFIED')]:
            calendar_value = pnl(); calendar_value[field] = bad
            with self.subTest(field=field):
                self.assertIsNone(self.module().summarize(document(), calendar_value)['trading_net_usdt'])

    def test_month_income_requires_adapter_kind_and_exact_coverage_evidence(self):
        cases = []
        for kind in (None, 'CLOSED_POSITIONS_FROM_FILLS'):
            value = pnl(); value['kind'] = kind; cases.append(value)
        for coverage in ({}, dict(pnl()['coverage'], income_history_complete=False),
                         dict(pnl()['coverage'], income_history_complete=1),
                         dict(pnl()['coverage'], start_ms=float(pnl()['coverage']['start_ms'])),
                         dict(pnl()['coverage'], start_ms=pnl()['coverage']['start_ms']+1)):
            value = pnl(); value['coverage'] = coverage; cases.append(value)
        value = pnl(); value['days'].append(value['days'][0]); cases.append(value)
        for value in cases:
            with self.subTest(value=value):
                self.assertIsNone(self.module().summarize(document(), value)['trading_net_usdt'])

    def test_leap_month_projection_precision_and_unknown_fee_contracts(self):
        from decimal import localcontext
        from datetime import date
        billing = self.module()
        for month, last_day in [('2027-02', 28), ('2028-02', 29)]:
            value = document(); value['month'] = month; value['bills'] = value['bills'][1:]
            row = value['bills'][0]; row['actual_idr'] = '140'
            row['usage'] = dict(through_date=month+'-14', available_credit_idr='10', credit_evidence='Owner snapshot in IDR')
            billing.validate(value, today=date(2029, 1, 1))
            with localcontext() as context:
                context.prec = 3
                result = billing.summarize(value, {})
            self.assertEqual(result['prepaid_projected_usage_idr'], str(last_day*10))
            self.assertEqual(result['prepaid_topup_estimate_idr'], str(last_day*10-150))
            self.assertIsNone(result['actual_cost_idr'])
            for fee in value['cashflow']:
                modified = copy.deepcopy(value); modified['cashflow'][fee] = None
                summary = billing.summarize(modified, {})
                if fee != 'withdrawal_fee_actual_usdt':
                    self.assertIsNone(summary['estimated_cost_idr'] if fee == 'payment_fee_actual_idr' else
                                      summary['withdrawal_estimate_usdt'])
            value['cashflow']['withdrawal_fee_percent'] = '100'
            with self.assertRaises(ValueError): billing.validate(value, today=date(2029, 1, 1))

    def test_estimated_profit_includes_incurred_fees_plus_remaining_fees_once(self):
        value = document(); value['bills'] = value['bills'][:1]
        value['cashflow'].update(payment_fee_actual_idr='1000', payment_fee_estimated_idr='2000',
                                 withdrawal_fee_actual_usdt='0.1', withdrawal_fee_estimated_usdt='1')
        result = self.module().summarize(value, pnl())
        self.assertEqual(result['actual_cost_idr'], '161000')
        self.assertEqual(result['estimated_cost_idr'], '163000')
        self.assertEqual(result['withdrawal_estimate_usdt'], '11.12500000')
        self.assertEqual(result['net_profit_usdt'], '-0.16250000')
        self.assertEqual(result['estimated_net_profit_usdt'], '-1.28750000')

    def test_payment_links_require_approved_exact_hosts_without_redirect_or_parser_ambiguity(self):
        billing = self.module()
        for url in ['https://evil.test/pay', 'https://platform.openai.com.evil.test/',
                    'https://platform.openai.com:444/settings/organization/billing/overview',
                    'https://platform.openai.com/settings/organization/billing/overview?redirect=https://evil.test',
                    'https://platform.openai.com\\\\@evil.test/',
                    'https://platform.openai.com./settings/organization/billing/overview',
                    'https://platform.openai.com/redirect', 'https://127.0.0.1/',
                    'https://platform.openai.com/settings/organization/billing/overview#token']:
            value = document(); value['bills'][1]['payment_url'] = url
            with self.subTest(url=url), self.assertRaises(ValueError): billing.validate(value)
        billing.validate(document())

    def test_persist_versions_separately_and_reject_unsafe_unknown_inputs(self):
        billing = self.module()
        self.assertTrue(hasattr(billing, 'Store'), 'billing persistence missing')
        with tempfile.TemporaryDirectory() as root:
            store = billing.Store(Path(root))
            self.assertEqual(store.read('2026-10')['version'], 0)
            first = store.save(document(), 0)
            self.assertEqual(first['version'], 1)
            updated = document(); updated['bills'][0]['paid'] = True
            self.assertEqual(store.save(updated, 1)['version'], 2)
            self.assertEqual(store.read('2026-10')['document'], updated)
            self.assertEqual(len(store.history('2026-10')), 2)
            self.assertEqual(store.history('2026-10')[0]['document'], document())
            self.assertFalse((Path(root)/'ledger.sqlite3').exists())
            with self.assertRaises(ValueError): store.save(document(), 0)
            for field, bad in [('fx_idr_per_usdt', 'NaN'), ('fx_idr_per_usdt', '0'),
                               ('fx_source', ''), ('month', '../2026-10')]:
                item = document(); item[field] = bad
                with self.subTest(field=field, bad=bad), self.assertRaises(ValueError): store.save(item, 2)
            for field, bad in [('actual_idr', '-1'), ('actual_idr', 42), ('due_date', '2026-02-30'),
                               ('due_date', '2026-11-01'), ('payment_url', 'javascript:alert(1)'),
                               ('payment_url', 'https://user:pass@example.com/'), ('paid', 'yes'),
                               ('evidence', '')]:
                item = document(); item['bills'][0][field] = bad
                with self.subTest(field=field, bad=bad), self.assertRaises(ValueError): store.save(item, 2)
            self.assertEqual(store.read('2026-10')['version'], 2)

    def test_concurrent_same_version_saves_have_one_winner_and_one_revision(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        billing = self.module()
        with tempfile.TemporaryDirectory() as root:
            store = billing.Store(Path(root)); store.read('2026-10')
            barrier = threading.Barrier(8)
            def save(index):
                value = document(); value['bills'][0]['evidence'] = f'Concurrent synthetic revision {index}'
                barrier.wait(timeout=5)
                try:
                    return store.save(value, 0)
                except ValueError as error:
                    return str(error)
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(save, range(8)))
            winners = [result for result in results if isinstance(result, dict)]
            self.assertEqual(len(winners), 1)
            self.assertEqual(results.count('BILLING_VERSION_CONFLICT'), 7)
            self.assertEqual(store.read('2026-10'), winners[0])
            self.assertEqual(store.history('2026-10'), winners)
            self.assertFalse((Path(root)/'ledger.sqlite3').exists())

    def test_unknowns_partial_calendar_and_paid_status_never_become_zero_profit(self):
        billing = self.module()
        value = document(); value['fx_idr_per_usdt'] = None; value['fx_source'] = None
        result = billing.summarize(value, pnl())
        self.assertIsNone(result['net_profit_usdt']); self.assertIsNone(result['withdrawal_estimate_usdt'])
        value = document(); value['bills'][0]['paid'] = None
        self.assertIsNone(billing.summarize(value, pnl())['withdrawal_estimate_usdt'])
        value = document(); value['bills'][0]['actual_idr'] = None
        self.assertIsNone(billing.summarize(value, pnl())['actual_cost_idr'])
        value['coverage_confirmed'] = False
        self.assertIsNone(billing.summarize(value, pnl())['estimated_cost_idr'])
        partial = pnl(); partial['days'].pop()
        self.assertIsNone(billing.summarize(document(), partial)['net_profit_usdt'])
        partial = pnl(); partial['complete'] = False
        self.assertIsNone(billing.summarize(document(), partial)['trading_net_usdt'])
    def test_native_and_public_routes_auth_readback_history_and_old_routes(self):
        import ast
        import hmac
        import http.client
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from types import SimpleNamespace
        from urllib.parse import urlsplit
        billing = self.module()
        for source in ('worker/api_service.py', 'vps_native/worker/api_service.py'):
            root = Path(__file__).resolve().parents[1]
            tree = ast.parse((root/source).read_text())
            definition = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'handler')
            self.assertIn('monthly_billing', ast.unparse(definition), 'real API handler missing billing integration')
            namespace = dict(__name__='worker.billing_test_api', __package__='worker', hmac=hmac,
                             json=json, urlsplit=urlsplit, BaseHTTPRequestHandler=BaseHTTPRequestHandler)
            exec(compile(ast.Module(body=[definition], type_ignores=[]), source, 'exec'), namespace)
            from unittest.mock import patch
            from tests.test_monthly_pnl import produced_cache, KEY
            with tempfile.TemporaryDirectory() as directory, patch('worker.monthly_pnl.read_secret', return_value=KEY):
                produced_cache(directory)
                def no_calendar_shortcut(**kwargs):
                    raise AssertionError('billing must read verified income cache, not partial calendar')
                controller = SimpleNamespace(directory=Path(directory), read_state=no_calendar_shortcut, robot=lambda: {'preserved': True}, office=lambda: {'preserved': True})
                server = ThreadingHTTPServer(('127.0.0.1', 0), namespace['handler'](controller, 'R'*32, 'C'*32, 'https://office.test'))
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                response_headers = {}
                def request(method, path='/billing/month?month=2026-10', value=None, token: str | None='R', origin: str | None='https://office.test', raw=None, extra_headers=()):
                    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                    headers = {'Content-Type': 'application/json'}
                    if token is not None: headers['Authorization'] = 'Bearer '+token*32
                    if origin is not None: headers['Origin'] = origin
                    body = raw if raw is not None else json.dumps(value) if value is not None else None
                    if extra_headers:
                        conn.putrequest(method, path)
                        for key, item in list(headers.items()) + list(extra_headers):
                            conn.putheader(key, item)
                        if body is not None:
                            conn.putheader('Content-Length', str(len(body.encode())))
                        conn.endheaders(body.encode() if body is not None else None)
                    else:
                        conn.request(method, path, body, headers)
                    response = conn.getresponse(); data = response.read()
                    response_headers.clear(); response_headers.update(response.getheaders())
                    conn.close()
                    return response.status, json.loads(data) if data else None
                try:
                    self.assertEqual(request('GET', token='X')[0], 403)
                    status, data = request('GET'); self.assertEqual(status, 200); self.assertEqual(data['version'], 0)
                    self.assertIsNone(data['summary']['net_profit_usdt'])
                    payload = dict(document=document(), version=0)
                    self.assertEqual(request('POST', '/billing/month', payload)[0], 403)
                    self.assertEqual(request('POST', '/billing/month', payload, token='C', origin='https://evil.test')[0], 403)
                    self.assertEqual(request('POST', '/billing/month', payload, token='C')[0], 200)
                    status, data = request('GET'); self.assertEqual(data['document'], document()); self.assertEqual(data['version'], 1)
                    self.assertEqual(data['summary']['net_profit_usdt'], '-2.00000000')
                    self.assertEqual(request('POST', '/billing/month', payload, token='C')[0], 409)
                    self.assertEqual(request('GET', '/billing/history?month=2026-10')[1]['revisions'][0]['document'], document())
                    self.assertEqual(request('GET', '/billing/month?month=../x')[0], 400)
                    self.assertEqual(request('POST', '/billing/month', token='C', raw='{"version":0,"version":1}')[0], 400)
                    self.assertEqual(request('POST', '/billing/month', token='C', raw='x'*33000)[0], 400)
                    self.assertEqual(request('GET', '/robot/status')[1], {'preserved': True})
                    self.assertEqual(request('OPTIONS', token=None, extra_headers=[
                        ('Access-Control-Request-Method', 'POST'),
                        ('Access-Control-Request-Headers', 'authorization,content-type')])[0], 204)
                    self.assertEqual(response_headers['Access-Control-Allow-Origin'], 'https://office.test')
                    self.assertEqual(response_headers['Vary'], 'Origin')
                    self.assertEqual(response_headers['Access-Control-Allow-Headers'], 'Authorization, Content-Type')
                    self.assertEqual(response_headers['Access-Control-Allow-Methods'], 'GET, POST, OPTIONS')
                    self.assertEqual(request('OPTIONS', token=None, origin=None)[0], 403)
                    self.assertEqual(request('GET', token=None)[0], 403)
                    self.assertEqual(request('OPTIONS', origin='https://evil.test')[0], 403)
                    for duplicate in ('https://office.test', 'https://evil.test'):
                        self.assertEqual(request('OPTIONS', extra_headers=[('Origin', duplicate)])[0], 403)
                        self.assertEqual(request('POST', '/billing/month', payload, token='C',
                                                 extra_headers=[('Origin', duplicate)])[0], 403)
                    for auth in ('Bearer '+'R'*32, 'Bearer '+'X'*32):
                        self.assertEqual(request('GET', extra_headers=[('Authorization', auth)])[0], 403)
                        self.assertEqual(request('POST', '/billing/month', payload, token='C',
                                                 extra_headers=[('Authorization', auth)])[0], 403)
                finally:
                    server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
