import json
import unittest
from unittest.mock import Mock,patch
from worker.binance_private import BinanceReadOnly,BinanceCheckError,read_only_get,BASE,SYMBOL_PATHS

class ShadowTransportTests(unittest.TestCase):
    def test_read_endpoints_symbol_query_signing_and_no_mutation(self):
        calls=[]
        def transport(url,headers):
            calls.append((url,headers));return {'serverTime':1700000000000} if url.endswith('/time') else []
        with patch('worker.binance_private.read_secret',return_value='synthetic-shadow-fixture'):
            client=BinanceReadOnly(transport=transport,clock=lambda:10);client.sync_time()
            for path in SYMBOL_PATHS:client.signed_get(path,'BTCUSDT')
            for path in ('/fapi/v1/order','/fapi/v1/algoOrder','/fapi/v1/leverage','/fapi/v1/marginType'):
                with self.assertRaisesRegex(BinanceCheckError,'BINANCE_ENDPOINT_DENIED'):client.signed_get(path,'BTCUSDT')
        for url,_ in calls[1:]:self.assertIn('&symbol=BTCUSDT&signature=',url)
    def test_transport_symbol_allowlist_fixed_get(self):
        response=Mock();response.code=200;response.read.return_value=b'[]'
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        with patch('worker.binance_private.build_opener') as opener:
            opener.return_value.open.return_value=response
            for path in SYMBOL_PATHS:
                query='?recvWindow=5000&timestamp=1700000000000&symbol=BTCUSDT&signature='+'a'*64
                headers={'X-MBX-APIKEY':'synthetic','Accept':'application/json','User-Agent':'harun-office/1.0'}
                self.assertEqual(read_only_get(BASE+path+query,headers),[])
                req=opener.return_value.open.call_args.args[0];self.assertEqual(req.get_method(),'GET');self.assertIsNone(req.data)
                for extra in ('&leverage=75','&side=BUY','&symbol=ETHUSDT'):
                    with self.assertRaises(BinanceCheckError):read_only_get(BASE+path+query+extra,headers)
    def test_shadow_has_no_callable_submission_or_live_switch(self):
        import inspect
        from pathlib import Path
        self.assertEqual(set(k for k in vars(BinanceReadOnly) if not k.startswith('_')),{'sync_time','signed_get','check'})
        for module in ('binance_shadow.py','execution_model.py'):
            source=(Path('worker')/module).read_text()
            for forbidden in ('requests.post','requests.put','requests.delete','OFFICE_LIVE','BINANCE_LIVE','LIVE_ENABLED'):self.assertNotIn(forbidden,source)
        self.assertNotIn('method',inspect.signature(read_only_get).parameters)
