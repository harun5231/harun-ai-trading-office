"""Offline provider contracts, bounded retries, and durable uncertain outcomes."""
import copy
import json
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from worker.analysis import SIZING_CONTRACT, analysis_context
from worker.core import D, Ledger, Review
from worker.http_client import NoRedirect, request, unique
from worker.neuroapi import NeuroAPI, SCREEN_SCHEMA, SCREEN_ONE_SCHEMA, SETUP_SCHEMA, selections, setup
from worker.prompts import ANALYSIS, SCREENING
from support import FakeMarket, GOOD, NUMERIC_FIELDS, hold


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ledger.sqlite3'
        self.ledger = Ledger(self.path)
        self.addCleanup(self.ledger.db.close)
        self.catalog = {'BTCUSDT': {}, 'ETHUSDT': {}}
        self.calls = []
        self.sleep = Mock()
        self.key = 'synthetic-private-provider-key'

    def transport(self, method, url, headers=None, body=None, timeout=None):
        self.calls.append((method, url, copy.deepcopy(headers), copy.deepcopy(body), timeout))
        if url.endswith('/health'):
            return 200, {}, dict(status='healthy', authenticated=True, key_prefix='PRIVATE_PREFIX')
        if body['output_schema'] in (SCREEN_SCHEMA, SCREEN_ONE_SCHEMA):
            count = body['output_schema']['properties']['symbols']['minItems']
            output = dict(symbols=list(self.catalog)[:count])
        else:
            output = copy.deepcopy(GOOD)
        return 200, {}, dict(mode='smart', answer=None, output=output)

    def client(self, transport=None, ledger=None, key=None):
        return NeuroAPI(ledger or self.ledger, key=self.key if key is None else key,
                        transport=self.transport if transport is None else transport, sleep=self.sleep)

    def screen(self, client=None, operation='screen', schema=None, prompt=SCREENING, context=None):
        schema = SCREEN_SCHEMA if schema is None else schema
        count = schema['properties']['symbols']['minItems']
        return (client or self.client()).ask(operation, prompt, schema,
            lambda value: selections(value, self.catalog, count), context, catalog=self.catalog)

    def record(self, operation='screen'):
        return dict(self.ledger.db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone())

    def test_exact_prompt_smart_schema_and_auth_are_kept_separate(self):
        self.screen()
        method, url, headers, body, timeout = self.calls[0]
        self.assertEqual((method, url, timeout), ('POST', 'https://api.neurobro.ai/api/v1/agent/ask', 90))
        self.assertEqual(body['prompt'].encode(), b'pilihkan 2 coin yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance')
        self.assertEqual((body['mode'], body['stream'], body['output_schema']), ('smart', False, SCREEN_SCHEMA))
        self.assertEqual(headers['X-API-Key'], self.key)
        self.assertNotIn('Idempotency-Key', headers)
        self.assertNotIn('system_prompt', body)
        self.assertNotIn(self.key, json.dumps(body))

    def test_one_slot_schema_requires_exactly_one_active_symbol(self):
        prompt = 'pilihkan 1 coin yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance'
        self.assertEqual(self.screen(schema=SCREEN_ONE_SCHEMA, prompt=prompt), dict(symbols=['BTCUSDT']))
        for symbols in ([], ['BTCUSDT', 'ETHUSDT'], ['BTC'], ['SOLUSDT']):
            with self.subTest(symbols=symbols), self.assertRaises(Review):
                selections(dict(symbols=symbols), self.catalog, 1)

    def test_analysis_context_has_rules_before_request_and_prompt_stays_literal(self):
        context, rules = analysis_context(FakeMarket(), 'BTCUSDT', D('10'))
        self.client().ask('analysis', ANALYSIS, SETUP_SCHEMA, lambda value: setup(value, 'BTCUSDT'), context)
        body = self.calls[0][3]
        self.assertEqual(body['prompt'], ANALYSIS)
        self.assertEqual(body['message_history'][0]['role'], 'user')
        self.assertEqual(json.loads(body['message_history'][0]['content']), context)
        self.assertEqual(context['risk_constraints']['target_loss_at_sl_usdt'], '10')
        self.assertEqual(context['risk_constraints']['position_sizing_contract'], SIZING_CONTRACT.replace('5 USDT', '10 USDT'))
        self.assertEqual(context['contract_rules']['stepSize'], str(rules.step))

    def test_health_uses_get_and_discards_provider_prefix(self):
        result = self.client().health()
        self.assertEqual(result, 'NEUROAPI_CONNECTED')
        self.assertEqual(self.calls[0][0], 'GET')
        self.assertIsNone(self.calls[0][3])
        self.assertNotIn('PRIVATE_PREFIX', result)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM api_requests').fetchone()[0], 0)

    def test_missing_key_stops_before_network_or_request_claim(self):
        client = self.client(key='')
        with self.assertRaisesRegex(Review, 'NEUROAPI_NOT_CONFIGURED'):
            self.screen(client)
        with self.assertRaisesRegex(Review, 'NEUROAPI_NOT_CONFIGURED'):
            client.health()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM api_requests').fetchone()[0], 0)

    def test_429_and_503_retry_at_most_three_times_with_same_body(self):
        for code in (429, 503):
            with self.subTest(code=code):
                calls = []
                def transient(*args, **kwargs):
                    calls.append(copy.deepcopy(args))
                    if len(calls) < 3:
                        return code, {'retry-after': '7'}, None
                    return self.transport(*args, **kwargs)
                self.screen(self.client(transient), operation=str(code))
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[0][3], calls[1][3])
                self.assertEqual(calls[0][3], calls[2][3])
                self.assertTrue(all('Idempotency-Key' not in call[2] for call in calls))
                self.assertEqual(self.record(str(code))['attempts'], 3)
                self.assertEqual(self.sleep.call_args_list[-2:], [unittest.mock.call(7), unittest.mock.call(7)])

    def test_retry_exhaustion_and_untrusted_retry_after_stop_durably(self):
        for code in (429, 503):
            transport = Mock(return_value=(code, {}, None))
            with self.assertRaisesRegex(Review, 'HTTP_' + str(code)):
                self.screen(self.client(transport), operation='exhaust-' + str(code))
            self.assertEqual(transport.call_count, 3)
            row = self.record('exhaust-' + str(code))
            self.assertEqual((row['state'], row['attempts'], row['output']), ('NEEDS_REVIEW', 3, None))
        for hint in ('31', '999999999', '-1', '1.5', 'invalid', 'Wed, 21 Oct 2026 07:28:00 GMT'):
            with self.subTest(hint=hint):
                transport = Mock(return_value=(429, {'retry-after': hint}, None))
                before = self.sleep.call_count
                with self.assertRaises(Review):
                    self.screen(self.client(transport), operation='hint-' + hint)
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(self.sleep.call_count, before)

    def test_other_http_failures_do_not_retry(self):
        for code in (400, 401, 403, 409, 500, 502, 504):
            with self.subTest(code=code):
                transport = Mock(return_value=(code, {}, None))
                with self.assertRaisesRegex(Review, '^HTTP_' + str(code) + '$'):
                    self.screen(self.client(transport), operation=str(code))
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(self.record(str(code))['state'], 'NEEDS_REVIEW')

    def test_uncertain_network_result_is_sanitized_and_never_replayed_after_restart(self):
        transport = Mock(side_effect=OSError(self.key + ' PRIVATE_SIGNATURE RAW_RESPONSE'))
        try:
            self.screen(self.client(transport))
        except Review as error:
            self.assertEqual(str(error), 'NEUROAPI_REQUEST_NEEDS_REVIEW')
            trace = traceback.format_exc()
        else:
            self.fail('Uncertain result must block')
        row = self.record()
        self.assertEqual((row['state'], row['attempts'], row['output'], row['failure_code']),
                         ('NEEDS_REVIEW', 1, None, 'NETWORK_UNCERTAIN'))
        for secret in (self.key, 'PRIVATE_SIGNATURE', 'RAW_RESPONSE'):
            self.assertNotIn(secret, trace)
            self.assertNotIn(secret, json.dumps(row))
        reopened = Ledger(self.path)
        self.addCleanup(reopened.db.close)
        retry = Mock(side_effect=AssertionError('Paid replay'))
        with self.assertRaisesRegex(Review, 'NEUROAPI_REQUEST_NEEDS_REVIEW'):
            self.screen(self.client(retry, ledger=reopened))
        retry.assert_not_called()
        self.assertEqual(transport.call_count, 1)

    def test_crash_after_durable_claim_leaves_pending_and_blocks_replay(self):
        def crash(*args, **kwargs):
            row = self.record()
            self.assertEqual((row['state'], row['attempts']), ('PENDING', 1))
            raise SystemExit('Synthetic process interruption')
        with self.assertRaises(SystemExit):
            self.screen(self.client(crash))
        self.assertEqual(self.record()['state'], 'PENDING')
        retry = Mock(side_effect=AssertionError('Paid replay'))
        with self.assertRaisesRegex(Review, 'NEUROAPI_REQUEST_NEEDS_REVIEW'):
            self.screen(self.client(retry))
        retry.assert_not_called()

    def test_completed_journal_reuses_output_and_rejects_changed_request_body(self):
        expected = self.screen()
        reopened = Ledger(self.path)
        self.addCleanup(reopened.db.close)
        network = Mock(side_effect=AssertionError('Completed request replay'))
        client = self.client(network, ledger=reopened)
        self.assertEqual(self.screen(client), expected)
        with self.assertRaisesRegex(Review, 'NEUROAPI_REQUEST_NEEDS_REVIEW'):
            self.screen(client, prompt=SCREENING + ' changed')
        network.assert_not_called()
        self.assertEqual(self.record()['attempts'], 1)

    def test_invalid_envelopes_are_not_retried_or_cached(self):
        envelopes = [dict(mode='fast', answer=None, output={}),
                     dict(mode='smart', answer='PRIVATE_PROSE', output=dict(symbols=list(self.catalog))),
                     dict(mode='smart', answer=None, output=None),
                     dict(mode='smart', answer=None, output=dict(symbols=['BTCUSDT', 'BTCUSDT'])),
                     dict(mode='smart', answer=None, output=dict(symbols=['BTCUSDT', 'SOLUSDT']))]
        for index, envelope in enumerate(envelopes):
            transport = Mock(return_value=(200, {}, envelope))
            with self.subTest(envelope=envelope), self.assertRaises(Review):
                self.screen(self.client(transport), operation=str(index))
            self.assertEqual(transport.call_count, 1)
            self.assertEqual((self.record(str(index))['state'], self.record(str(index))['output']), ('NEEDS_REVIEW', None))
            self.assertNotIn('PRIVATE_PROSE', json.dumps(self.record(str(index))))

    def test_setup_schema_preserves_hold_and_rejects_ambiguous_numbers(self):
        self.assertEqual(setup(hold('BTCUSDT'), 'BTCUSDT').side, 'HOLD')
        for field in NUMERIC_FIELDS:
            with self.subTest(field=field), self.assertRaises(Review):
                setup({**hold('BTCUSDT'), field: 1}, 'BTCUSDT')
        for value in ({**GOOD, 'position_size': '2.5'}, {**GOOD, 'limit_entry': 100.0},
                      {**GOOD, 'risk_reward': True}, {**GOOD, 'symbol': 'ETHUSDT'},
                      {**GOOD, 'take_profit': D('103.99'), 'risk_reward': 99}, {**GOOD, 'extra': 'PRIVATE'}):
            with self.subTest(value=value), self.assertRaises(Review):
                setup(value, 'BTCUSDT')
        self.assertEqual(setup({**GOOD, 'risk_reward': 99}, 'BTCUSDT').tp, D('104'))


class PublicTransportTests(unittest.TestCase):
    def test_live_public_data_has_both_frames_rules_and_freshness_check(self):
        market = FakeMarket()
        value = market.data('BTCUSDT')
        market.fresh(value, 'BTCUSDT')
        self.assertEqual(set(value['timeframes']), {'1h', '15m'})
        self.assertTrue(all(method == 'GET' for method, _ in market.calls))
        self.assertEqual(market.rules('BTCUSDT').step, D('.001'))
        value['fetched_at'] -= 181
        with self.assertRaisesRegex(Review, 'STALE_MARKET_CONTEXT'):
            market.fresh(value, 'BTCUSDT')

    def test_mutation_endpoints_are_denied_before_transport(self):
        market = FakeMarket()
        with self.assertRaisesRegex(Review, 'MARKET_ENDPOINT_DENIED'):
            market.get('order')
        self.assertEqual(market.calls, [])
        with patch('worker.http_client.build_opener') as opener:
            with self.assertRaisesRegex(Review, 'BINANCE_ENDPOINT_DENIED'):
                request('POST', 'https://fapi.binance.com/fapi/v1/order', body=dict(symbol='BTCUSDT'))
            opener.assert_not_called()

    def test_redirect_duplicate_json_and_raw_transport_error_fail_closed(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://attacker.invalid'))
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_FIELD'):
            unique([('symbol', 'BTCUSDT'), ('symbol', 'ETHUSDT')])
        with patch('worker.http_client.build_opener', side_effect=OSError('PRIVATE_API_KEY')):
            with self.assertRaisesRegex(Review, '^HTTP_RESULT_UNCERTAIN$'):
                request('GET', 'https://api.neurobro.ai/api/v1/health')


if __name__ == '__main__':
    unittest.main()
