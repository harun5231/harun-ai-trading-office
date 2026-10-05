import io
import json
import unittest
import traceback
from unittest.mock import Mock,patch
from urllib.error import HTTPError
from urllib.parse import parse_qs
from worker.binance_execution_transport import wire,LiveError,NotFound
from worker.binance_private import signature

class WireTests(unittest.TestCase):
    def response(self,code,data):
        r=Mock();r.code=code;r.read.return_value=json.dumps(data).encode();r.__enter__=Mock(return_value=r);r.__exit__=Mock(return_value=False);return r
    def test_signed_get_canonical_query_and_headers(self):
        cid='ho-'+'a'*28+'-e'
        with patch('worker.binance_execution_transport.build_opener') as o:
            o.return_value.open.return_value=self.response(200,{'status':'NEW'})
            wire('GET','/fapi/v1/order',{'symbol':'BTCUSDT','origClientOrderId':cid},'synthetic-key','synthetic-secret',1700000000000)
            r=o.return_value.open.call_args.args[0]
            query='recvWindow=5000&timestamp=1700000000000&origClientOrderId='+cid+'&symbol=BTCUSDT'
            self.assertEqual(r.full_url,'https://fapi.binance.com/fapi/v1/order?'+query+'&signature='+signature('synthetic-secret',query))
            self.assertEqual(r.method,'GET');self.assertIsNone(r.data)
            self.assertEqual(r.get_header('X-mbx-apikey'),'synthetic-key')
    def test_delete_algo_is_disabled_even_for_scoped_bot_id(self):
        cid='ho-'+'a'*28+'-s'
        with patch('worker.binance_execution_transport.build_opener') as o:
            with self.assertRaisesRegex(LiveError,'LIVE_SUBMISSION_DISABLED'):
                wire('DELETE','/fapi/v1/algoOrder',{'clientAlgoId':cid},'k','s',1)
            o.assert_not_called()
    def test_query_notfound_only_explicit_2013(self):
        with patch('worker.binance_execution_transport.build_opener') as o:
            o.return_value.open.return_value=self.response(400,{'code':-2013,'msg':'sensitive'})
            with self.assertRaises(NotFound):wire('GET','/fapi/v1/order',{'symbol':'BTCUSDT','origClientOrderId':'ho-'+'a'*28+'-e'},'k','s',1)
    def test_timeout_no_retry_no_secret_in_traceback(self):
        with patch('worker.binance_execution_transport.build_opener') as o:
            o.return_value.open.side_effect=OSError('PRIVATE_SECRET signature=PRIVATE_SIGNATURE')
            try:wire('GET','/fapi/v1/order',{'symbol':'BTCUSDT','origClientOrderId':'ho-'+'a'*28+'-e'},'PRIVATE_KEY','PRIVATE_SECRET',1)
            except LiveError:
                trace=traceback.format_exc()
                for value in ('PRIVATE_SECRET signature','PRIVATE_SIGNATURE'):self.assertNotIn(value,trace)
            else:self.fail('Expected uncertainty')
            self.assertEqual(o.return_value.open.call_count,1)
    def test_margin_already_set_is_not_success_without_get(self):
        with patch('worker.binance_execution_transport.build_opener') as o:
            o.return_value.open.return_value=self.response(400,{'code':-4046,'msg':'No need'})
            with self.assertRaisesRegex(LiveError,'LIVE_SUBMISSION_DISABLED'):wire('POST','/fapi/v1/marginType',{'symbol':'BTCUSDT','marginType':'CROSSED'},'k','s',1)
    def test_hype_and_malformed_contract_never_reach_network(self):
        with patch('worker.binance_execution_transport.build_opener',side_effect=AssertionError('network')):
            with self.assertRaises(LiveError):wire('POST','/fapi/v1/leverage',{'symbol':'HYPEUSDT','leverage':75},'k','s',1)
            with self.assertRaises(LiveError):wire('POST','/fapi/v1/leverage',{'symbol':'BTCUSDT','leverage':76},'k','s',1)

    def test_unknown_400_is_uncertain_not_definite_rejection(self):
        for code in (-1000,-1001,-1006,-1007,-99999):
            with patch('worker.binance_execution_transport.build_opener') as o:
                o.return_value.open.return_value=self.response(400,{'code':code,'msg':'do not log'})
                with self.assertRaisesRegex(LiveError,'LIVE_REQUEST_UNCERTAIN'):
                    wire('GET','/fapi/v1/order',{'symbol':'BTCUSDT','origClientOrderId':'ho-'+'a'*28+'-e'},'k','s',1)
                self.assertEqual(o.return_value.open.call_count,1)
    def test_known_precision_rejection_is_sanitized(self):
        with patch('worker.binance_execution_transport.build_opener') as o:
            o.return_value.open.return_value=self.response(400,{'code':-1111,'msg':'PRIVATE'})
            with self.assertRaisesRegex(LiveError,'^LIVE_PROVIDER_REJECTED$'):
                wire('GET','/fapi/v1/order',{'symbol':'BTCUSDT','origClientOrderId':'ho-'+'a'*28+'-e'},'k','s',1)

    def test_every_mutation_method_disabled_before_network_or_authorization(self):
        from worker.binance_execution_transport import BinanceExecution
        with patch('worker.binance_private.read_secret',return_value='synthetic-only'),patch('worker.binance_execution_transport.build_opener') as opener:
            authorized=Mock(return_value=True);network=Mock()
            client=BinanceExecution(authorize=authorized,live_transport=network)
            for method in ('POST','PUT','DELETE','PATCH'):
                for path in ('order','algoOrder','leverage','marginType'):
                    with self.assertRaisesRegex(LiveError,'LIVE_SUBMISSION_DISABLED'):
                        client.scoped(method,'/fapi/v1/'+path,{'symbol':'BTCUSDT'})
                    with self.assertRaisesRegex(LiveError,'LIVE_SUBMISSION_DISABLED'):
                        wire(method,'/fapi/v1/'+path,{'symbol':'BTCUSDT'},'k','s',1)
            authorized.assert_not_called();network.assert_not_called();opener.assert_not_called()
    def test_no_non_get_request_constructor_in_binance_transport(self):
        import ast
        from pathlib import Path
        tree=ast.parse(Path('worker/binance_execution_transport.py').read_text())
        requests=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='Request']
        self.assertEqual(len(requests),1)
        self.assertEqual(next(k.value.value for k in requests[0].keywords if k.arg=='method'),'GET')
