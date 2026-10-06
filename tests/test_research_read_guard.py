"""OFF stops subsequent research GETs without stopping the account poll thread."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.parse import urlsplit, parse_qs
import unittest
from unittest.mock import Mock, patch

from worker.account_state import account_state
from worker.analysis import account_fee_rules, analysis_context
from worker.binance_private import BinanceReadOnly
from worker.core import Rules
from worker.market import Market
from worker.research_guard import (ResearchReadPaused, check_research_read,
                                   research_reads, gateway_transaction_reads)


NOW = 1700000000.0
SYMBOL = 'BTCUSDT'


def catalog():
    return {'symbols': [{'symbol': SYMBOL, 'status': 'TRADING',
        'contractType': 'PERPETUAL', 'quoteAsset': 'USDT', 'marginAsset': 'USDT',
        'orderTypes': ['LIMIT'], 'filters': [
            {'filterType': 'LOT_SIZE', 'stepSize': '0.001', 'minQty': '0.001', 'maxQty': '100'},
            {'filterType': 'PRICE_FILTER', 'tickSize': '0.1', 'minPrice': '0.1', 'maxPrice': '1000000'},
            {'filterType': 'MIN_NOTIONAL', 'notional': '5'},
        ]}]}


def private_reader(transport):
    # Fixtures never read a key file or call the production transport.
    reader = BinanceReadOnly.__new__(BinanceReadOnly)
    reader._key = 'fixture-api-key-not-real'
    reader._secret = 'fixture-secret-not-real'
    reader._transport = transport
    reader._clock = lambda: 100.0
    reader._server = None
    reader._synced = None
    return reader


class ResearchReadGuardTests(unittest.TestCase):
    def test_off_after_catalog_finishes_prevents_market_and_commission_reads(self):
        allowed = [True]
        public_calls = []
        private_calls = []

        def transport(method, url, **options):
            public_calls.append(urlsplit(url).path)
            allowed[0] = False
            return 200, {}, catalog()

        market = Market(transport=transport, clock=lambda: NOW)
        reader = private_reader(lambda url, headers: private_calls.append(url) or {'serverTime': int(NOW * 1000)})
        with research_reads(lambda: allowed[0]):
            # The already-started request is allowed to finish after OFF.
            self.assertIn(SYMBOL, market.catalog())
            with self.assertRaises(ResearchReadPaused):
                analysis_context(market, SYMBOL, account_client=reader)
            with self.assertRaises(ResearchReadPaused):
                market.rules(SYMBOL)
            with self.assertRaises(ResearchReadPaused):
                reader.commission_rate(SYMBOL)
        self.assertEqual(public_calls, ['/fapi/v1/exchangeInfo'])
        self.assertEqual(private_calls, [])

    def test_off_after_mark_prevents_server_and_chart_reads(self):
        allowed = [True]
        calls = []

        def transport(method, url, **options):
            calls.append(urlsplit(url).path)
            allowed[0] = False
            return 200, {}, {'symbol': SYMBOL, 'time': int(NOW * 1000), 'markPrice': '100'}

        market = Market(transport=transport, clock=lambda: NOW)
        with research_reads(lambda: allowed[0]):
            with self.assertRaises(ResearchReadPaused):
                market.data(SYMBOL)
        self.assertEqual(calls, ['/fapi/v1/premiumIndex'])

    def test_off_after_first_chart_prevents_second_timeframe(self):
        allowed = [True]
        calls = []

        def transport(method, url, **options):
            parsed = urlsplit(url)
            calls.append((parsed.path, parse_qs(parsed.query).get('interval', [None])[0]))
            if parsed.path.endswith('premiumIndex'):
                value = {'symbol': SYMBOL, 'time': int(NOW * 1000), 'markPrice': '100'}
            elif parsed.path.endswith('time'):
                value = {'serverTime': int(NOW * 1000)}
            else:
                interval = 3600000
                latest = int(NOW * 1000) // interval * interval
                value = [[latest - (19 - index) * interval, '100', '101', '99', '100', '1',
                          latest - (19 - index) * interval + interval - 1, '0', 1, '0', '0', '0']
                         for index in range(20)]
                allowed[0] = False
            return 200, {}, value

        market = Market(lookback=20, transport=transport, clock=lambda: NOW)
        with research_reads(lambda: allowed[0]):
            with self.assertRaises(ResearchReadPaused):
                market.data(SYMBOL)
        self.assertEqual(calls, [('/fapi/v1/premiumIndex', None),
                                ('/fapi/v1/time', None), ('/fapi/v1/klines', '1h')])

    def test_off_during_commission_time_sync_prevents_signed_fee_request(self):
        allowed = [True]
        calls = []

        def transport(url, headers):
            calls.append(urlsplit(url).path)
            allowed[0] = False
            return {'serverTime': int(NOW * 1000)}

        reader = private_reader(transport)
        with research_reads(lambda: allowed[0]):
            with self.assertRaises(ResearchReadPaused):
                reader.commission_rate(SYMBOL)
        self.assertEqual(calls, ['/fapi/v1/time'])

    def test_pause_passes_through_fee_rules_instead_of_becoming_fee_failure(self):
        reader = private_reader(Mock(side_effect=AssertionError('no transport allowed')))
        rules = Rules(Decimal('0.001'), Decimal('0.001'), Decimal('100'), Decimal('0.1'), Decimal('5'), NOW)
        with research_reads(lambda: False):
            with self.assertRaises(ResearchReadPaused):
                account_fee_rules(rules, SYMBOL, reader)

    def test_pause_passes_through_account_preflight_instead_of_account_failure(self):
        allowed = [True]
        calls = []

        def transport(url, headers):
            calls.append(urlsplit(url).path)
            allowed[0] = False
            return {'serverTime': int(NOW * 1000)}

        reader = private_reader(transport)
        with research_reads(lambda: allowed[0]):
            with self.assertRaises(ResearchReadPaused):
                account_state(reader, Mock(), '2023-11-15')
        self.assertEqual(calls, ['/fapi/v1/time'])

    def test_transport_pause_is_preserved_by_public_wrappers(self):
        market = Market(transport=Mock(side_effect=ResearchReadPaused('ROBOT_RESEARCH_PAUSED')))
        for read in (market.catalog, lambda: market.mark(SYMBOL),
                     lambda: market.data(SYMBOL), lambda: market.rules(SYMBOL)):
            with self.subTest(read=read):
                with self.assertRaises(ResearchReadPaused):
                    read()

    def test_transport_pause_is_preserved_by_reader_and_cli_wrapper(self):
        reader = private_reader(Mock(side_effect=ResearchReadPaused('ROBOT_RESEARCH_PAUSED')))
        with self.assertRaises(ResearchReadPaused):
            reader.sync_time()
        with patch('worker.binance_private.BinanceReadOnly', return_value=reader):
            from worker.binance_private import check
            with self.assertRaises(ResearchReadPaused):
                check()

    def test_scope_resets_after_exception_so_unscoped_status_reads_continue(self):
        public_calls = []
        private_calls = []
        market = Market(transport=lambda method, url, **options:
                        public_calls.append(url) or (200, {}, {'serverTime': int(NOW * 1000)}))
        reader = private_reader(lambda url, headers: private_calls.append(url) or {'serverTime': int(NOW * 1000)})
        with self.assertRaises(RuntimeError):
            with research_reads(lambda: False):
                with self.assertRaises(ResearchReadPaused):
                    market.get('time')
                raise RuntimeError('leave context')
        market.get('time')
        reader.sync_time()
        self.assertEqual(len(public_calls), 1)
        self.assertEqual(len(private_calls), 1)

    def test_research_scope_does_not_pause_background_account_thread(self):
        calls = []

        def account_poll():
            reader = private_reader(lambda url, headers:
                                    calls.append(urlsplit(url).path) or {'serverTime': int(NOW * 1000)})
            reader.sync_time()
            return reader.server_time_ms()

        with research_reads(lambda: False):
            with self.assertRaises(ResearchReadPaused):
                check_research_read()
            with ThreadPoolExecutor(max_workers=1) as executor:
                self.assertEqual(executor.submit(account_poll).result(timeout=3), int(NOW * 1000))
        self.assertEqual(calls, ['/fapi/v1/time'])

    def test_nested_scope_cannot_override_parent_pause_and_resets_normally(self):
        with research_reads(lambda: True):
            check_research_read()
            with research_reads(lambda: False):
                with research_reads(lambda: True):
                    with self.assertRaises(ResearchReadPaused):
                        check_research_read()
            check_research_read()
        check_research_read()

    def test_started_adapter_can_finish_reads_when_off_changes_inside_callback(self):
        allowed = [True]
        calls = []

        def transport(url, headers):
            path = urlsplit(url).path
            calls.append(path)
            if path.endswith('/time'):
                allowed[0] = False
                return {'serverTime': int(NOW * 1000)}
            return {'symbol': SYMBOL, 'makerCommissionRate': '0.0002', 'takerCommissionRate': '0.0005'}

        reader = private_reader(transport)
        with research_reads(lambda: allowed[0]):
            check_research_read()  # Caller authorizes this callback while ON.
            with gateway_transaction_reads():
                quote = reader.commission_rate(SYMBOL)
            self.assertEqual(quote['taker'], '0.0005')
            with self.assertRaises(ResearchReadPaused):
                reader.sync_time()  # A subsequent research operation is OFF.
        self.assertEqual(calls, ['/fapi/v1/time', '/fapi/v1/commissionRate'])

    def test_adapter_exception_restores_the_live_research_predicate(self):
        allowed = [True]
        with research_reads(lambda: allowed[0]):
            check_research_read()
            with self.assertRaises(RuntimeError):
                with gateway_transaction_reads():
                    allowed[0] = False
                    check_research_read()
                    raise RuntimeError('callback failed after starting')
            with self.assertRaises(ResearchReadPaused):
                check_research_read()
        check_research_read()

    def test_nested_adapter_scope_restores_outer_transaction_then_research(self):
        with research_reads(lambda: False):
            with gateway_transaction_reads():
                check_research_read()
                with gateway_transaction_reads():
                    check_research_read()
                check_research_read()
            with self.assertRaises(ResearchReadPaused):
                check_research_read()

    def test_adapter_read_scope_does_not_disable_another_threads_guard(self):
        def another_research_thread():
            with research_reads(lambda: False):
                try:
                    check_research_read()
                except ResearchReadPaused:
                    return 'PAUSED'
                return 'READ_ALLOWED'

        with research_reads(lambda: False):
            with gateway_transaction_reads():
                check_research_read()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    self.assertEqual(executor.submit(another_research_thread).result(timeout=3), 'PAUSED')
            with self.assertRaises(ResearchReadPaused):
                check_research_read()


if __name__ == '__main__':
    unittest.main()
