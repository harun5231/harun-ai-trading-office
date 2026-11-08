"""Offline exact-native wire contracts with fake TLS/socket/HTTP only.

Set NATIVE_CLOCK_CANDIDATE_FILE to a saved original or isolated candidate to
run the SAME assertions before/after correction. No real credentials or orders.
"""
import hashlib,hmac,http.client,json,os,socket,ssl,threading,time,types,unittest,urllib.parse,urllib.request
from pathlib import Path
from unittest.mock import patch
from worker import research_guard

SOURCE=Path(os.environ.get('NATIVE_CLOCK_CANDIDATE_FILE', str(Path(__file__).resolve().parents[1]/'vps_native/worker/order_gateway.py')))
EPOCH=1700000000000

class Clock:
 def __init__(self):self.times={};self.lock=threading.Lock()
 @property
 def value(self):
  with self.lock:return self.times.get(threading.current_thread().name,0.0)
 @value.setter
 def value(self,value):
  with self.lock:self.times[threading.current_thread().name]=value
 def __call__(self):return self.value

class Response:
 def __init__(self,raw,status=200):
  self.code=self.status=status;self.reason='fixture';self.headers={};self.raw=raw;self.offset=0;self.closed=False
 def read(self,size=-1):
  if size<0:size=len(self.raw)
  chunk=self.raw[self.offset:self.offset+size];self.offset+=len(chunk);return chunk
 read1=read
 def __enter__(self):return self
 def __exit__(self,*args):self.close();return False
 def close(self):self.closed=True
 def info(self):return self.headers

class Network:
 def __init__(self,clock,plans):
  self.clock=clock;self.plans=list(plans);self.thread_plans={};self.sent=[];self.connections=[];self.contexts=[];self.lock=threading.Lock()
 def connection_class(self):
  network=self
  class Connection:
   debuglevel=0;_http_vsn=11
   def __init__(self,host,timeout=None,context=None,check_hostname=None,**kwargs):
    self.host=host;self.port=443;self.sock=None;self._tunnel_host=None;self.timeout=timeout
    with network.lock:
     plans=network.thread_plans.get(threading.current_thread().name,network.plans)
     self.plan=plans.pop(0);network.connections.append(self);network.contexts.append(context)
   def set_debuglevel(self,value):self.debuglevel=value
   def connect(self):
    network.clock.value+=self.plan.get('setup',0)
    callback=self.plan.get('after_connect')
    if callback:callback()
    if 'dns_error' in self.plan:raise socket.gaierror(self.plan['dns_error'],'SECRET_DNS_MESSAGE https://fapi.binance.com/?signature=SECRET_SIGNATURE')
    if self.plan.get('connect_error'):raise OSError(101,'fixture network unavailable')
    self.sock=type('Socket',(),{'close':lambda self:None})()
   def request(self,method,selector,body=None,headers=None,*,encode_chunked=False):
    if self.sock is None:self.connect()
    self.method,self.selector,self.body=method,selector,body
    headers=dict(headers or {})
    row={'method':method,'selector':selector,'body':body,'headers':headers,
         'at':network.clock.value,'thread':threading.current_thread().name}
    self.sent_row=row
    with network.lock:network.sent.append(row)
   def getresponse(self):
    network.clock.value+=self.plan.get('wire',.05)
    if 'response_dns_error' in self.plan:raise socket.gaierror(self.plan['response_dns_error'],'SECRET_DNS_MESSAGE')
    if self.plan.get('response_error'):raise OSError(104,'fixture reply interrupted')
    path=urllib.parse.urlsplit(self.selector).path
    if path=='/fapi/v1/time':
     data=self.plan.get('data',{'serverTime':EPOCH+int(network.clock.value*1000)})
     return Response(json.dumps(data).encode(),self.plan.get('status',200))
    raw_query=self.body.decode()if self.method=='POST'else urllib.parse.urlsplit(self.selector).query
    pairs=urllib.parse.parse_qsl(raw_query);values=dict(pairs)
    unsigned=urllib.parse.urlencode(pairs[:-1])
    valid_signature=(pairs[-1][0]=='signature'and hmac.compare_digest(values['signature'],hmac.new(b'fixture-secret',unsigned.encode(),hashlib.sha256).hexdigest()))
    at_wire=EPOCH+int(self.sent_row['at']*1000)
    stamp=int(values['timestamp']);window=int(values['recvWindow'])
    if not valid_signature:return Response(b'{"code":-1022}',400)
    code=self.plan.get('code')
    if code is None and not at_wire-window<=stamp<at_wire+1000:code=-1021
    if code is not None:return Response(json.dumps({'code':code,'msg':'offline fixture'}).encode(),400)
    return Response(json.dumps(self.plan.get('data',[])).encode(),self.plan.get('status',200))
   def close(self):self.sock=None
  return Connection

class NativeClockContractTests(unittest.TestCase):
 def setUp(self):self.clock=Clock();self.guard={'on':True}
 def install(self,plans):
  self.net=Network(self.clock,plans)
  p=patch('http.client.HTTPSConnection',self.net.connection_class());p.start();self.addCleanup(p.stop)
  p=patch('time.monotonic',self.clock);p.start();self.addCleanup(p.stop)
  scope={'__name__':'worker._isolated_native_clock_contract','__package__':'worker'}
  # Legacy module imports requests but these native urllib methods never use it.
  with patch.dict('sys.modules',{'requests':types.ModuleType('requests')}):
   exec(compile(SOURCE.read_bytes(),str(SOURCE),'exec'),scope)
  self.Gateway=scope['OrderGateway'];self.Review=scope['Review']
  if '_wire_clock' in scope:
   self.addCleanup(lambda:self.assertIsNone(scope['_wire_clock'].get()))
  gateway=object.__new__(self.Gateway);gateway.base_url='https://fapi.binance.com'
  gateway.api_key='fixture-key';gateway.api_secret='fixture-secret';gateway._connected=True
  return gateway
 def plans(self,private_setup=8.1):return [{'setup':8.06},{'setup':private_setup}]
 def params(self,row):
  text=row['body'].decode()if row['method']=='POST'else urllib.parse.urlsplit(row['selector']).query
  return dict(urllib.parse.parse_qsl(text))
 def private(self):return [row for row in self.net.sent if urllib.parse.urlsplit(row['selector']).path!='/fapi/v1/time']
 def test_get_signature_is_created_after_slow_tls(self):
  gateway=self.install(self.plans());params={'symbol':'BTCUSDT','origClientOrderId':'hao-existing-fixture'}
  self.assertEqual(gateway._request('GET','/fapi/v1/order',params),[])
  row=self.private()[0];values=self.params(row)
  self.assertLessEqual(abs(int(values['timestamp'])-(EPOCH+int(row['at']*1000))),100)
  self.assertEqual(values['recvWindow'],'5000');self.assertEqual({k:values[k]for k in params},params)
  self.assertTrue(all(c.verify_mode==ssl.CERT_REQUIRED and c.check_hostname for c in self.net.contexts))
 def test_post_body_signature_is_fresh_without_changing_order_fields(self):
  gateway=self.install(self.plans());params={'symbol':'BTCUSDT','newClientOrderId':'hao-stable-fixture','side':'BUY','quantity':'0.010','price':'100'}
  with research_guard.gateway_mutations(lambda:self.guard['on']):gateway._request('POST','/fapi/v1/order',params)
  rows=self.private();self.assertEqual(len(rows),1);row=rows[0];values=self.params(row)
  self.assertEqual({k:values[k]for k in params},params);self.assertEqual(values['recvWindow'],'5000')
  lengths=[v for k,v in row['headers'].items()if k.lower()=='content-length'];self.assertEqual(lengths,[str(len(row['body']))])
  self.assertLessEqual(abs(int(values['timestamp'])-(EPOCH+int(row['at']*1000))),100)
 def test_delete_selector_is_fresh_without_changing_original_order_identity(self):
  gateway=self.install(self.plans());params={'symbol':'BTCUSDT','origClientOrderId':'hao-existing-fixture'}
  with research_guard.gateway_mutations(lambda:self.guard['on']):gateway._request('DELETE','/fapi/v1/order',params)
  row=self.private()[0];self.assertIsNone(row['body']);values=self.params(row)
  self.assertEqual({k:values[k]for k in params},params)
  self.assertLessEqual(abs(int(values['timestamp'])-(EPOCH+int(row['at']*1000))),100)
 def test_get_confirmed_minus1021_resyncs_once(self):
  gateway=self.install([{}, {'code':-1021}, {}, {}])
  self.assertEqual(gateway._request('GET','/fapi/v1/openOrders',{}),[])
  self.assertEqual(len(self.private()),2);self.assertEqual(len(self.net.sent),4)
  values=[self.params(row)for row in self.private()];self.assertNotEqual(values[0]['timestamp'],values[1]['timestamp']);self.assertNotEqual(values[0]['signature'],values[1]['signature'])
 def test_persistent_minus1021_stops_after_one_get_retry(self):
  gateway=self.install([{}, {'code':-1021}, {}, {'code':-1021}])
  with self.assertRaises(self.Gateway._ExchangeError)as caught:gateway._request('GET','/fapi/v1/openOrders',{})
  self.assertEqual(caught.exception.code,-1021);self.assertEqual(len(self.private()),2);self.assertEqual(len(self.net.sent),4)
 def test_nonclock_exchange_failure_never_retries(self):
  for code in(-2015,-1003,-1022):
   with self.subTest(code=code):
    self.doCleanups();self.setUp();gateway=self.install([{}, {'code':code}])
    with self.assertRaises(self.Gateway._ExchangeError)as caught:gateway._request('GET','/fapi/v1/openOrders',{})
    self.assertEqual(caught.exception.code,code);self.assertEqual(len(self.private()),1)
 def test_mutation_minus1021_is_not_repeated(self):
  for method in('POST','DELETE'):
   with self.subTest(method=method):
    self.doCleanups();self.setUp();gateway=self.install([{}, {'code':-1021}])
    with research_guard.gateway_mutations(lambda:True):
     with self.assertRaises(self.Gateway._ExchangeError):gateway._request(method,'/fapi/v1/order',{'symbol':'BTCUSDT','origClientOrderId':'hao-same-fixture'})
    self.assertEqual(len(self.private()),1);self.assertEqual(len(self.net.sent),2)
 def test_uncertain_post_is_never_repeated(self):
  gateway=self.install([{}, {'response_error':True}])
  with research_guard.gateway_mutations(lambda:True):
   with self.assertRaisesRegex(self.Review,'BINANCE_ORDER_OUTCOME_UNKNOWN'):gateway._request('POST','/fapi/v1/order',{'newClientOrderId':'hao-stable-fixture'})
  self.assertEqual(len(self.private()),1);self.assertEqual(len(self.net.sent),2)
 def test_off_during_tls_denies_mutation_before_signed_bytes(self):
  gateway=self.install([{}, {'setup':6.0,'after_connect':lambda:self.guard.update(on=False)}])
  with research_guard.gateway_mutations(lambda:self.guard['on']):
   with self.assertRaisesRegex(self.Review,'BINANCE_ORDER_ROBOT_OFF'):gateway._request('POST','/fapi/v1/order',{'newClientOrderId':'hao-fixed'})
  self.assertEqual(len(self.private()),0)
 def test_owned_protection_stays_authorized_when_research_is_off(self):
  gateway=self.install(self.plans());params={'symbol':'BTCUSDT','type':'STOP_MARKET','quantity':'0.01','clientAlgoId':'hao-sl-stable'}
  def authorize(method,path,value):
   if type(value)is str:
    value=dict(urllib.parse.parse_qsl(value));[value.pop(k,None)for k in('timestamp','recvWindow','signature')]
   return method=='POST'and path=='/fapi/v1/algoOrder'and value==params
  with research_guard.gateway_mutations(lambda:False),research_guard.gateway_protection_mutations(authorize):
   gateway._request('POST','/fapi/v1/algoOrder',params)
  self.assertEqual({k:self.params(self.private()[0])[k]for k in params},params)
 def test_deadline_exhausted_in_tls_sends_no_private_bytes(self):
  gateway=self.install([{}, {'setup':11.0}]);gateway._deadline=40;gateway._request_count=0;gateway._cleanup=False
  with self.assertRaises(self.Review):gateway._request('GET','/fapi/v1/openOrders',{})
  self.assertEqual(len(self.private()),0)
 def test_expired_anchor_during_tls_sends_no_private_bytes(self):
  gateway=self.install([{}, {'setup':30.01}])
  with self.assertRaises(self.Review):gateway._request('GET','/fapi/v1/openOrders',{})
  self.assertEqual(len(self.private()),0)
 def test_time_response_latency_guard_excludes_tls_but_includes_http_body(self):
  gateway=self.install([{'setup':20.0,'wire':2.01}])
  with self.assertRaises(self.Review):gateway._get_server_time()
  self.assertEqual(len(self.private()),0)
 def test_owned_protection_revoked_during_tls_sends_no_mutation(self):
  authority={'allowed':True}
  gateway=self.install([{}, {'setup':6.0,'after_connect':lambda:authority.update(allowed=False)}])
  with research_guard.gateway_mutations(lambda:False),research_guard.gateway_protection_mutations(lambda m,p,v:authority['allowed']):
   with self.assertRaisesRegex(self.Review,'BINANCE_ORDER_ROBOT_OFF'):
    gateway._request('POST','/fapi/v1/algoOrder',{'clientAlgoId':'hao-sl-fixed','quantity':'0.1'})
  self.assertEqual(len(self.private()),0)
 def test_post_content_length_is_refreshed_if_timestamp_digit_count_changes(self):
  gateway=self.install([{}, {'setup':6.0}])
  with patch.dict(globals(),{'EPOCH':9999999999900}),research_guard.gateway_mutations(lambda:True):
   gateway._request('POST','/fapi/v1/order',{'newClientOrderId':'hao-stable'})
  row=self.private()[0];self.assertEqual(len(self.params(row)['timestamp']),14)
  lengths=[v for k,v in row['headers'].items()if k.lower()=='content-length']
  self.assertEqual(lengths,[str(len(row['body']))])
 def test_malformed_time_fails_without_private_request_or_fallback(self):
  for value in(True,0,-1,'1700000000000',None):
   with self.subTest(value_type=type(value).__name__):
    self.doCleanups();self.setUp();gateway=self.install([{'data':{'serverTime':value}}])
    with self.assertRaisesRegex(self.Review,'BINANCE_ORDER_CLOCK_INVALID'):
     gateway._request('GET','/fapi/v1/openOrders',{})
    self.assertEqual(len(self.private()),0)
 def test_get_transport_unknown_is_not_generically_retried(self):
  gateway=self.install([{}, {'response_error':True}])
  with self.assertRaisesRegex(self.Review,'BINANCE_ORDER_OUTCOME_UNKNOWN'):
   gateway._request('GET','/fapi/v1/openOrders',{})
  self.assertEqual(len(self.private()),1);self.assertEqual(len(self.net.sent),2)
 def test_host_wall_clock_jump_is_not_used_for_api_timestamp(self):
  gateway=self.install(self.plans())
  with patch('time.time',side_effect=AssertionError('WALL_CLOCK_FORBIDDEN')):
   self.assertEqual(gateway._request('GET','/fapi/v1/openOrders',{}),[])
  row=self.private()[0]
  self.assertLessEqual(abs(int(self.params(row)['timestamp'])-(EPOCH+int(row['at']*1000))),100)
 def test_get_dns_before_private_write_recovers_once_same_identity(self):
  gateway=self.install([{}, {'setup':4.0,'dns_error':socket.EAI_NONAME}, {}, {}])
  params={'symbol':'LTCUSDT','origClientOrderId':'hao-original-id'}
  with self.assertLogs('worker._isolated_native_clock_contract','WARNING')as logs:
   self.assertEqual(gateway._request('GET','/fapi/v1/order',params),[])
  self.assertEqual(len(self.net.connections),4);self.assertEqual(len(self.private()),1)
  values=self.params(self.private()[0]);self.assertEqual({k:values[k]for k in params},params)
  self.assertEqual(values['recvWindow'],'5000')
  text='\n'.join(logs.output);self.assertIn('phase=CONNECT delivery=NOT_SENT',text)
  self.assertIn('exception_class=gaierror',text);self.assertIn('errno_name=EAI_NONAME',text)
  self.assertNotIn('SECRET_DNS_MESSAGE',text);self.assertNotIn('SECRET_SIGNATURE',text)
 def test_get_time_dns_before_write_recovers_once(self):
  gateway=self.install([{'setup':4.0,'dns_error':socket.EAI_AGAIN}, {}, {}])
  with self.assertLogs('worker._isolated_native_clock_contract','WARNING'):
   self.assertEqual(gateway._request('GET','/fapi/v1/openOrders',{}),[])
  self.assertEqual(len(self.net.connections),3);self.assertEqual(len(self.private()),1)
 def test_persistent_get_dns_stops_after_total_two_attempts(self):
  gateway=self.install([{}, {'dns_error':socket.EAI_NONAME}, {}, {'dns_error':socket.EAI_AGAIN}])
  with self.assertLogs('worker._isolated_native_clock_contract','WARNING'):
   with self.assertRaisesRegex(self.Review,'BINANCE_ORDER_OUTCOME_UNKNOWN')as caught:
    gateway._request('GET','/fapi/v1/openOrders',{})
  self.assertEqual(len(self.net.connections),4);self.assertEqual(len(self.private()),0)
  detail=caught.exception.transport_diagnostics
  self.assertEqual((detail['phase'],detail['delivery'],detail['exception_class']),('CONNECT','NOT_SENT','gaierror'))
  self.assertEqual(detail['errno'],socket.EAI_AGAIN)
 def test_dns_and_minus1021_share_one_retry_slot(self):
  for plans,exchange_error,connections in (([{}, {'code':-1021}, {}, {'dns_error':socket.EAI_NONAME}],False,4),
                                ([{}, {'dns_error':socket.EAI_NONAME}, {}, {'code':-1021}],True,4),
                                ([{'dns_error':socket.EAI_NONAME}, {}, {'code':-1021}],True,3)):
   with self.subTest(exchange_error=exchange_error):
    self.doCleanups();self.setUp();gateway=self.install(plans)
    with self.assertLogs('worker._isolated_native_clock_contract','WARNING'):
     with self.assertRaises(self.Review)as caught:gateway._request('GET','/fapi/v1/openOrders',{})
    self.assertEqual(len(self.net.connections),connections);self.assertEqual(len(self.private()),1)
    if exchange_error:
     self.assertIsInstance(caught.exception,self.Gateway._ExchangeError)
     self.assertEqual(caught.exception.code,-1021)
     self.assertEqual(caught.exception.transport_diagnostics['delivery'],'RESPONSE_RECEIVED')
    else:self.assertEqual(caught.exception.transport_diagnostics['delivery'],'NOT_SENT')
 def test_mutation_dns_never_retries_time_or_signed_request(self):
  for method in('POST','DELETE'):
   for time_failure in(False,True):
    with self.subTest(method=method,time_failure=time_failure):
     self.doCleanups();self.setUp()
     plans=[{'dns_error':socket.EAI_NONAME}]if time_failure else[{}, {'dns_error':socket.EAI_NONAME}]
     gateway=self.install(plans)
     with research_guard.gateway_mutations(lambda:True),self.assertLogs('worker._isolated_native_clock_contract','WARNING'):
      with self.assertRaisesRegex(self.Review,'BINANCE_ORDER_OUTCOME_UNKNOWN')as caught:
       gateway._request(method,'/fapi/v1/order',{'symbol':'LTCUSDT','origClientOrderId':'hao-original'})
     self.assertEqual(len(self.net.connections),1 if time_failure else 2)
     self.assertEqual(len(self.private()),0)
     self.assertEqual(caught.exception.transport_diagnostics['delivery'],'NOT_SENT')
 def test_dns_after_write_is_uncertain_and_never_retried(self):
  gateway=self.install([{}, {'response_dns_error':socket.EAI_NONAME}])
  with self.assertLogs('worker._isolated_native_clock_contract','WARNING'):
   with self.assertRaisesRegex(self.Review,'BINANCE_ORDER_OUTCOME_UNKNOWN')as caught:
    gateway._request('GET','/fapi/v1/openOrders',{})
  self.assertEqual(len(self.net.connections),2);self.assertEqual(len(self.private()),1)
  detail=caught.exception.transport_diagnostics
  self.assertEqual((detail['phase'],detail['delivery']),('READ','DELIVERY_UNCONFIRMED'))
 def test_dns_retry_cannot_extend_native_operation_deadline(self):
  gateway=self.install([{}, {'setup':4.0,'dns_error':socket.EAI_NONAME}])
  gateway._deadline=34.0;gateway._request_count=0;gateway._cleanup=False
  with self.assertLogs('worker._isolated_native_clock_contract','WARNING'):
   with self.assertRaisesRegex(self.Review,'BINANCE_ORDER_CALLBACK_DEADLINE'):
    gateway._request('GET','/fapi/v1/openOrders',{})
  self.assertEqual(len(self.net.connections),2);self.assertEqual(len(self.private()),0)
 def test_response_headers_and_clock_abort_have_distinct_delivery_metadata(self):
  gateway=self.install([{}, {'code':-2015}])
  with self.assertLogs('worker._isolated_native_clock_contract','WARNING'):
   with self.assertRaises(self.Gateway._ExchangeError)as caught:gateway._request('GET','/fapi/v1/openOrders',{})
  detail=caught.exception.transport_diagnostics
  self.assertEqual((detail['delivery'],detail['http_status'],detail['binance_code']),('RESPONSE_RECEIVED',400,-2015))
  self.doCleanups();self.setUp();gateway=self.install([{}, {'setup':30.01}])
  with self.assertLogs('worker._isolated_native_clock_contract','WARNING'):
   with self.assertRaisesRegex(self.Review,'BINANCE_ORDER_CLOCK_INVALID')as caught:gateway._request('GET','/fapi/v1/openOrders',{})
  self.assertEqual(caught.exception.transport_diagnostics['delivery'],'NOT_SENT')
  self.assertEqual(len(self.private()),0)
 def test_isolated_concurrent_requests_keep_their_own_time_anchor(self):
  gateway=self.install([]);barrier=threading.Barrier(2);errors=[]
  self.net.thread_plans={'clock-a':[{}, {'setup':6.0,'after_connect':lambda:barrier.wait(3)}],
                         'clock-b':[{}, {'setup':9.0,'after_connect':lambda:barrier.wait(3)}]}
  def run(start):
   self.clock.value=start;other=object.__new__(self.Gateway);other.__dict__.update(gateway.__dict__)
   try:other._request('GET','/fapi/v1/openOrders',{})
   except Exception as error:errors.append(type(error).__name__)
  threads=[threading.Thread(target=run,args=(start,),name=name)for name,start in(('clock-a',100),('clock-b',200))]
  for t in threads:t.start()
  for t in threads:t.join(5);self.assertFalse(t.is_alive())
  self.assertEqual(errors,[])
  for row in self.private():self.assertLessEqual(abs(int(self.params(row)['timestamp'])-(EPOCH+int(row['at']*1000))),100)

if __name__=='__main__':
 print('SOURCE_SHA256='+hashlib.sha256(SOURCE.read_bytes()).hexdigest())
 unittest.main()
