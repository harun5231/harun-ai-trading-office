"""Synthetic credentials/HTTP only. No private Binance account is contacted."""
import contextlib
import hashlib
import hmac
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.error import HTTPError
from worker.binance_private import BinanceReadOnly,BinanceCheckError,BASE,PRIVATE_PATHS,COMMISSION_PATH,COMMISSION_SOURCE,read_only_get,read_secret,signature,check

KEY='synthetic-key-not-a-real-credential'
SECRET='synthetic-secret-not-a-real-credential'
class PrivateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.root.chmod(0o700)
        self.key=self.root/'key';self.secret=self.root/'secret'
        for p,v in ((self.key,KEY),(self.secret,SECRET)):p.write_text(v+'\n');p.chmod(0o600)
        self.env=patch.dict(os.environ,{'BINANCE_API_KEY_FILE':str(self.key),'BINANCE_API_SECRET_FILE':str(self.secret)})
        self.env.start();self.addCleanup(self.env.stop);self.calls=[]
    def transport(self,url,headers):
        self.calls.append((url,headers))
        if url==BASE+'/fapi/v1/time':return {'serverTime':1700000000000}
        if '/accountConfig?' in url:return dict(canTrade=True,dualSidePosition=False,multiAssetsMargin=False,raw_private='DONT_EXPOSE')
        return dict(assets=[dict(asset='USDT',walletBalance='100.1200',availableBalance='90.1',raw_private='DONT_EXPOSE')],positions=['DONT_EXPOSE'])
    def client(self,clock=lambda:10):return BinanceReadOnly(transport=self.transport,clock=clock)
    def test_hmac_exact_published_binance_fixture(self):
        # Public documentation test vector, not an account secret.
        key='2b5eb11e18796d12d88f13dc27dbbd02c2cc51ff7059765ed9821957d82bb4d9'
        query='symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=9000&timeInForce=GTC&recvWindow=5000&timestamp=1591702613943'
        self.assertEqual(signature(key,query),'3c661234138461fcc7a7d8746c6558c9842d4e10870d2ecbedf7777cad694af9')
    def test_timestamp_recvwindow_header_and_sanitized_success(self):
        client=self.client();result=client.check()
        self.assertEqual(result,dict(status='BINANCE_CONNECTED',futures=True,can_trade=True,position_mode='ONE_WAY',multi_assets_margin=False,live_execution=False,mode='BINANCE_READ_ONLY',usdt_wallet_balance='100.1200',usdt_available_balance='90.1'))
        self.assertEqual(self.calls[0],(BASE+'/fapi/v1/time',{}))
        query='recvWindow=5000&timestamp=1700000000000'
        for url,headers in self.calls[1:]:
            self.assertTrue(url.endswith(query+'&signature='+hmac.new(SECRET.encode(),query.encode(),hashlib.sha256).hexdigest()))
            self.assertEqual(headers['X-MBX-APIKEY'],KEY)
        for sensitive in (KEY,SECRET,'DONT_EXPOSE','signature'):self.assertNotIn(sensitive,json.dumps(result))
    def test_hedge_and_cantrade_false_reported_without_enabling_anything(self):
        client=self.client();old=client._transport
        client._transport=lambda u,h:dict(canTrade=False,dualSidePosition=True,multiAssetsMargin=True) if 'accountConfig' in u else old(u,h)
        result=client.check();self.assertEqual(result['position_mode'],'HEDGE');self.assertFalse(result['can_trade']);self.assertFalse(result['live_execution'])
    def test_clock_uses_server_monotonic_not_vps_wall_time(self):
        client=self.client(clock=Mock(side_effect=[10,10.1,10.6,10.7]))
        with patch('time.time',side_effect=AssertionError('Wall clock used')):client.check()
        self.assertIn('timestamp=1700000000500',self.calls[1][0])
    def test_unsynced_stale_or_slow_clock_fails_closed(self):
        client=self.client()
        with self.assertRaisesRegex(BinanceCheckError,'BINANCE_CLOCK_ERROR'):client.signed_get('/fapi/v3/account')
        for ticks in ([10,13],[10,10,41],[10,9]):
            client=self.client(clock=Mock(side_effect=ticks))
            with self.assertRaisesRegex(BinanceCheckError,'BINANCE_CLOCK_ERROR'):client.check()
    def test_missing_secret_no_fallback_no_network(self):
        self.key.unlink()
        with patch.dict(os.environ,{'BINANCE_API_KEY':KEY,'BINANCE_API_SECRET':SECRET}),patch('worker.binance_private.read_only_get',side_effect=AssertionError('network')):
            self.assertEqual(check()['status'],'BINANCE_NOT_CONFIGURED')
    def test_unsafe_files_and_values_rejected(self):
        self.key.chmod(0o644)
        with self.assertRaisesRegex(BinanceCheckError,'BINANCE_NOT_CONFIGURED'):read_secret('BINANCE_API_KEY_FILE')
        self.key.unlink();self.key.symlink_to(self.secret)
        with self.assertRaises(BinanceCheckError):read_secret('BINANCE_API_KEY_FILE')
        self.key.unlink()
        for bad in ('short','A'*513,'a b'*20,'é'*32,'A'*32+'\n\n','A'*32+'\x00'):
            self.key.write_text(bad);self.key.chmod(0o600)
            with self.assertRaises(BinanceCheckError):read_secret('BINANCE_API_KEY_FILE')
    def test_no_mutating_endpoints_even_after_auth(self):
        client=self.client();client.sync_time()
        for path in ('/fapi/v1/order','/fapi/v1/leverage','/fapi/v1/marginType','/sapi/v1/asset/transfer','/sapi/v1/capital/withdraw/apply','/fapi/v1/positionSide/dual','/fapi/v3/account?evil=1'):
            with self.assertRaisesRegex(BinanceCheckError,'BINANCE_ENDPOINT_DENIED'):client.signed_get(path)
            with patch('worker.binance_private.build_opener',side_effect=AssertionError('network')):
                with self.assertRaisesRegex(BinanceCheckError,'BINANCE_ENDPOINT_DENIED'):read_only_get(BASE+path,{})
        self.assertEqual(len(self.calls),1)
    def test_transport_origin_query_and_redirect_guards(self):
        for url in ('http://fapi.binance.com/fapi/v1/time','https://evil.example/fapi/v1/time',BASE+'/fapi/v1/time?signature=secret',BASE+'/fapi/v3/account',BASE+'/fapi/v1/time#fragment'):
            with self.assertRaisesRegex(BinanceCheckError,'BINANCE_ENDPOINT_DENIED'):read_only_get(url,{})
        from worker.http_client import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'msg',{},'https://evil.example'))
    def response(self,status,body):
        response=Mock();response.code=status;response.read.return_value=body
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        return response
    def test_http_and_provider_errors_sanitized_no_raw_trace(self):
        import traceback
        for status,code,expected in ((401,-2015,'BINANCE_AUTH_FAILED'),(403,None,'BINANCE_ACCOUNT_UNAVAILABLE'),(400,-1021,'BINANCE_CLOCK_ERROR'),(400,-1022,'BINANCE_AUTH_FAILED'),(400,-1011,'BINANCE_IP_RESTRICTED'),(400,-1002,'BINANCE_PERMISSION_DENIED')):
            raw=json.dumps(dict(code=code,msg=KEY+SECRET+' signed query')).encode()
            with patch('worker.binance_private.build_opener') as opener:
                opener.return_value.open.return_value=self.response(status,raw)
                try:read_only_get(BASE+'/fapi/v1/time',{})
                except BinanceCheckError as error:
                    self.assertEqual(str(error),expected)
                    trace=traceback.format_exc();self.assertNotIn(KEY,trace);self.assertNotIn(SECRET,trace)
                else:self.fail('Error accepted')
    def test_transport_fixed_get_and_200_response(self):
        with patch('worker.binance_private.build_opener') as opener:
            opener.return_value.open.return_value=self.response(200,b'{"serverTime":1700000000000}')
            self.assertEqual(read_only_get(BASE+'/fapi/v1/time',{}),{'serverTime':1700000000000})
            request=opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(),'GET');self.assertIsNone(request.data)
    def test_unknown_exceptions_never_expose_credentials(self):
        client=self.client();client._transport=Mock(side_effect=RuntimeError(KEY+SECRET))
        with self.assertRaisesRegex(BinanceCheckError,'^BINANCE_ACCOUNT_UNAVAILABLE$'):client.check()
    def test_malformed_response_and_balance_not_echoed(self):
        for body in ({'assets':'secret'}, {'assets':[{'asset':'USDT','walletBalance':KEY,'availableBalance':'1'}]}):
            client=self.client();old=client._transport
            client._transport=lambda u,h:body if '/v3/account?' in u else old(u,h)
            with self.assertRaisesRegex(BinanceCheckError,'BINANCE_ACCOUNT_UNAVAILABLE'):client.check()
    def test_account_check_does_not_create_state_or_contact_neuroapi(self):
        client=self.client();out=io.StringIO();before=set(self.root.iterdir())
        with patch('worker.binance_private.BinanceReadOnly',return_value=client),patch('worker.neuroapi.NeuroAPI',side_effect=AssertionError('NeuroAPI')),contextlib.redirect_stdout(out):
            result=check()
        self.assertEqual(result['status'],'BINANCE_CONNECTED');self.assertEqual(out.getvalue(),'')
        self.assertEqual(set(self.root.iterdir()),before)
        for value in (KEY,SECRET,'signature','DONT_EXPOSE'):self.assertNotIn(value,json.dumps(result))
    def test_generic_public_transport_also_rejects_binance_mutations(self):
        from worker.http_client import request
        from worker.core import Review
        for method in ('POST','PUT','DELETE','PATCH'):
            for path in ('order','leverage','marginType','exchangeInfo'):
                with self.assertRaisesRegex(Review,'BINANCE_ENDPOINT_DENIED'):request(method,BASE+'/fapi/v1/'+path)
        with self.assertRaises(Review):request('GET',BASE+'/fapi/v1/order')
    def test_actual_transport_private_headers_get_only(self):
        responses=[self.response(200,b'{"serverTime":1700000000000}'),self.response(200,b'{"assets":[]}'),self.response(200,b'{"canTrade":true,"dualSidePosition":false,"multiAssetsMargin":false}')]
        with patch('worker.binance_private.build_opener') as opener:
            opener.return_value.open.side_effect=responses
            client=BinanceReadOnly(clock=lambda:10)
            self.assertEqual(client.check()['status'],'BINANCE_CONNECTED')
            for call in opener.return_value.open.call_args_list:
                req=call.args[0];self.assertEqual(req.get_method(),'GET');self.assertIsNone(req.data)
                if req.full_url!=BASE+'/fapi/v1/time':
                    self.assertEqual(req.get_header('X-mbx-apikey'),KEY)
                    self.assertIn('recvWindow=5000&timestamp=1700000000000&signature=',req.full_url)
    def test_http_error_object_and_network_message_not_exposed(self):
        import traceback
        for error in (HTTPError(BASE+'/fapi/v3/account?signature=PRIVATE_SIGNATURE',401,SECRET,{},io.BytesIO(json.dumps({'code':-2015,'msg':KEY}).encode())),OSError(KEY+SECRET)):
            with patch('worker.binance_private.build_opener') as opener:
                opener.return_value.open.side_effect=error
                try:read_only_get(BASE+'/fapi/v1/time',{})
                except BinanceCheckError:
                    trace=traceback.format_exc()
                    for value in (KEY,SECRET,'PRIVATE_SIGNATURE'):self.assertNotIn(value,trace)
                else:self.fail('Expected sanitized failure')
    def test_account_check_failure_never_prints_secret_or_calls_gateway(self):
        out=io.StringIO();err=io.StringIO()
        with patch('worker.binance_private.BinanceReadOnly',side_effect=RuntimeError(KEY+SECRET)),patch('worker.order_gateway.OrderGateway.submit',side_effect=AssertionError('Trading')),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            result=check()
        self.assertEqual(result['status'],'BINANCE_ACCOUNT_UNAVAILABLE');self.assertEqual(out.getvalue(),'');self.assertEqual(err.getvalue(),'')
        for value in (KEY,SECRET):self.assertNotIn(value,json.dumps(result))
    def fee_client(self,body=None,clock=lambda:10):
        body=body if body is not None else dict(symbol='BTCUSDT',makerCommissionRate='0.00020000',takerCommissionRate='0.00050000',private=KEY+SECRET)
        client=self.client(clock=clock);original=client._transport
        def transport(url,headers):
            if url.startswith(BASE+COMMISSION_PATH+'?'):
                self.calls.append((url,headers));return body
            return original(url,headers)
        client._transport=transport
        return client
    def test_real_account_symbol_fee_evidence_exact_rates_safe_fields_and_hmac(self):
        client=self.fee_client();result=client.commission_rate('BTCUSDT')
        self.assertEqual(result,dict(symbol='BTCUSDT',maker='0.00020000',taker='0.00050000',source=COMMISSION_SOURCE,
                                     checked_at='2023-11-14T22:13:20.000Z',checked_at_ms=1700000000000))
        self.assertEqual(self.calls[0],(BASE+'/fapi/v1/time',{}));self.assertEqual(len(self.calls),2)
        url,headers=self.calls[1]
        query='recvWindow=5000&timestamp=1700000000000&symbol=BTCUSDT'
        self.assertEqual(url,BASE+COMMISSION_PATH+'?'+query+'&signature='+signature(SECRET,query))
        self.assertEqual(headers['X-MBX-APIKEY'],KEY)
        for value in (KEY,SECRET,'private','signature'):self.assertNotIn(value,json.dumps(result))
    def test_zero_actual_commission_is_allowed_without_a_default(self):
        client=self.fee_client(dict(symbol='BTCUSDT',makerCommissionRate='0',takerCommissionRate='0.00000000'))
        result=client.commission_rates('BTCUSDT')
        self.assertEqual(result['maker'],'0');self.assertEqual(result['taker'],'0.00000000')
    def test_fee_evidence_refreshes_trusted_clock_and_marks_response_completion(self):
        client=self.fee_client(clock=Mock(side_effect=[10,10,11,12]))
        with patch('time.time',side_effect=AssertionError('VPS wall time')):result=client.commission_rate('BTCUSDT')
        self.assertIn('timestamp=1700000001000',self.calls[1][0])
        self.assertEqual(result['checked_at_ms'],1700000002000)
        self.assertEqual(result['checked_at'],'2023-11-14T22:13:22.000Z')
    def test_fee_query_requires_strict_usdt_symbol_before_any_request(self):
        client=self.fee_client()
        for symbol in (None,'btcusdt','BTCUSDT&side=BUY','../../order','BTCUSDT_261225','BTCUSDC',True):
            with self.assertRaisesRegex(BinanceCheckError,'^BINANCE_ENDPOINT_DENIED$'):client.commission_rate(symbol)
        self.assertEqual(self.calls,[])
        client.sync_time();before=len(self.calls)
        for options in ({},{'symbol':'BTCUSDT','limit':1},{'symbol':'BTCUSDT','from_id':0},
                        {'symbol':'BTCUSDT','start_time':1699999999000,'end_time':1700000000000}):
            with self.assertRaisesRegex(BinanceCheckError,'BINANCE_ENDPOINT_DENIED'):client.signed_get(COMMISSION_PATH,**options)
        self.assertEqual(len(self.calls),before)
    def test_missing_mismatched_or_invalid_rates_are_not_assumed_zero(self):
        valid=dict(symbol='BTCUSDT',makerCommissionRate='0.0002',takerCommissionRate='0.0005')
        bodies=[[],{},dict(valid,symbol='ETHUSDT'),dict(symbol='BTCUSDT',makerCommissionRate='0.0002')]
        for field in ('makerCommissionRate','takerCommissionRate'):
            for value in ('NaN','Infinity','-0.0001','1','1.000','1e-3','0.'+'0'*64,'0. 01',0.0005,0,True,KEY):
                bodies.append(dict(valid,**{field:value}))
        for body in bodies:
            self.calls=[]
            with self.assertRaisesRegex(BinanceCheckError,'^BINANCE_ACCOUNT_UNAVAILABLE$'):self.fee_client(body).commission_rate('BTCUSDT')
    def test_stale_or_slow_fee_clock_does_not_return_evidence(self):
        for clock in (Mock(side_effect=[10,13]),Mock(side_effect=[10,10,11,41])):
            self.calls=[]
            with self.assertRaisesRegex(BinanceCheckError,'^BINANCE_CLOCK_ERROR$'):self.fee_client(clock=clock).commission_rate('BTCUSDT')
    def test_fee_transport_origin_canonical_query_and_extra_keys_guarded(self):
        prefix='recvWindow=5000&timestamp=1700000000000'
        queries=[prefix,prefix+'&symbol=BTCUSDT&symbol=ETHUSDT',prefix+'&symbol=BTCUSDT&side=BUY',
                 prefix+'&symbol=BTCUSDT&limit=1',prefix+'&symbol=%42TCUSDT',prefix+'&symbol=BTCUSDT&fromId=1']
        headers={'X-MBX-APIKEY':KEY,'Accept':'application/json','User-Agent':'test'}
        with patch('worker.binance_private.build_opener',side_effect=AssertionError('No network')):
            for query in queries:
                with self.assertRaisesRegex(BinanceCheckError,'^BINANCE_ENDPOINT_DENIED$'):
                    read_only_get(BASE+COMMISSION_PATH+'?'+query+'&signature='+'0'*64,headers)
    def test_fee_actual_transport_fixed_get_no_body_and_no_extra_state(self):
        responses=[self.response(200,b'{"serverTime":1700000000000}'),
                   self.response(200,b'{"symbol":"BTCUSDT","makerCommissionRate":"0.0002","takerCommissionRate":"0.0005"}')]
        before=set(self.root.iterdir())
        with patch('worker.binance_private.build_opener') as opener,patch('worker.order_gateway.OrderGateway.submit',side_effect=AssertionError('Order')):
            opener.return_value.open.side_effect=responses
            result=BinanceReadOnly(clock=lambda:10).commission_rate('BTCUSDT')
            self.assertEqual(result['taker'],'0.0005')
            self.assertEqual(len(opener.return_value.open.call_args_list),2)
            for call in opener.return_value.open.call_args_list:
                request=call.args[0];self.assertEqual(request.get_method(),'GET');self.assertIsNone(request.data)
        self.assertEqual(set(self.root.iterdir()),before)
    def test_fee_transport_error_chain_never_exposes_credentials_or_url(self):
        import traceback
        client=self.fee_client();original=client._transport
        def fail(url,headers):
            if COMMISSION_PATH in url:raise RuntimeError('PRIVATE_TRANSPORT_URL'+url+KEY+SECRET)
            return original(url,headers)
        client._transport=fail
        try:client.commission_rate('BTCUSDT')
        except BinanceCheckError as error:
            self.assertEqual(str(error),'BINANCE_ACCOUNT_UNAVAILABLE');trace=traceback.format_exc()
            for value in (KEY,SECRET,'PRIVATE_TRANSPORT_URL',BASE+COMMISSION_PATH):self.assertNotIn(value,trace)
        else:self.fail('Expected sanitized exception')
