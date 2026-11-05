"""Real public-depth transport admission with an isolated HTTP opener."""
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.request import ProxyHandler

from worker.core import Review
from worker.http_client import NoRedirect, request
from worker.market import Market


class Response(io.BytesIO):
    def __init__(self, body, code=200):
        super().__init__(body)
        self.code = code
        self.headers = {}


class PublicDepthTransportTests(unittest.TestCase):
    def test_actual_market_depth_reaches_fixed_origin_get_and_preserves_book(self):
        book = dict(lastUpdateId=12345, bids=[['0.630', '10'], ['0.629', '20']],
            asks=[['0.631', '12'], ['0.632', '18']])
        opener = Mock(open=Mock(return_value=Response(json.dumps(book).encode())))
        with patch('worker.http_client.build_opener', return_value=opener) as build:
            market = Market(transport=request, clock=lambda: 1793869200.0)
            result = market.depth('RUNEUSDT')
        sent = opener.open.call_args.args[0]
        self.assertEqual(sent.full_url,
            'https://fapi.binance.com/fapi/v1/depth?symbol=RUNEUSDT&limit=1000')
        self.assertEqual(sent.get_method(), 'GET')
        self.assertIsNone(sent.data)
        self.assertEqual(opener.open.call_args.kwargs, {'timeout': 15})
        self.assertEqual(result, dict(source='BINANCE_FUTURES_PUBLIC_DEPTH', symbol='RUNEUSDT',
            observed_at=1793869200.0, last_update_id=12345,
            bids=book['bids'], asks=book['asks']))
        self.assertIsInstance(build.call_args.args[0], ProxyHandler)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], NoRedirect)

    def test_depth_admission_still_denies_writes_orders_and_changed_binance_origin(self):
        depth = 'https://fapi.binance.com/fapi/v1/depth?symbol=RUNEUSDT&limit=1000'
        cases = [('POST', depth, None), ('DELETE', depth, None), ('PUT', depth, None),
            ('GET', depth, {}), ('GET', 'https://fapi.binance.com/fapi/v1/order', None),
            ('GET', depth.replace('https:', 'http:'), None),
            ('GET', depth.replace('fapi.binance.com/', 'fapi.binance.com:443/'), None),
            ('GET', depth.replace('fapi.binance.com/', 'user@fapi.binance.com/'), None),
            ('GET', depth + '#unexpected', None)]
        with patch('worker.http_client.build_opener') as build:
            for method, url, body in cases:
                with self.subTest(method=method, url=url, body=body), self.assertRaisesRegex(
                        Review, 'BINANCE_ENDPOINT_DENIED'):
                    request(method, url, body=body)
            build.assert_not_called()

    def test_market_cannot_turn_depth_path_into_another_origin_or_endpoint(self):
        transport = Mock()
        with self.assertRaisesRegex(Review, 'MARKET_ENDPOINT_DENIED'):
            Market(transport=transport).get('https://other.invalid/fapi/v1/depth')
        transport.assert_not_called()

    def test_depth_response_keeps_size_and_redirect_guards(self):
        url = 'https://fapi.binance.com/fapi/v1/depth?symbol=RUNEUSDT&limit=1000'
        response = Response(b'x' * 4_000_001)
        with patch('worker.http_client.build_opener', return_value=SimpleNamespace(
                open=lambda *args, **kwargs: response)):
            with self.assertRaisesRegex(Review, 'HTTP_RESULT_UNCERTAIN'):
                request('GET', url)
        redirect = Response(b'not an order book', code=302)
        redirect.headers = {'Location': 'https://other.invalid/depth'}
        opener = Mock(open=Mock(return_value=redirect))
        with patch('worker.http_client.build_opener', return_value=opener):
            self.assertEqual(request('GET', url), (302, {'retry-after': ''}, None))
        opener.open.assert_called_once()
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, 'redirect', {},
            'https://other.invalid/depth'))


if __name__ == '__main__':
    unittest.main()
