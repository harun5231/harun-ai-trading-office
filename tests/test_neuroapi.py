import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from worker.core import Ledger,Review,Rules,Signal,risk_check,D,day
from worker.market import Market,INTERVALS
from worker.neuroapi import NeuroAPI,SCREEN_SCHEMA,SETUP_SCHEMA,selections,setup
from worker.prompts import SCREENING,ANALYSIS
from worker.workflow import Workflow

GOOD={'symbol':'BTCUSDT','side':'LONG','position_size':2,'limit_entry':100,'take_profit':104,'stop_loss':98,'risk_reward':2}
RULES=lambda:Rules(D('.001'),D('.001'),D('1000'),D('.01'),D('5'),time.time())
def row(symbol):return {'symbol':symbol,'status':'TRADING','contractType':'PERPETUAL','quoteAsset':'USDT','marginAsset':'USDT','orderTypes':['LIMIT'],'filters':[
 {'filterType':'LOT_SIZE','stepSize':'0.001','minQty':'0.001','maxQty':'1000'},
 {'filterType':'PRICE_FILTER','tickSize':'0.01','minPrice':'0.01','maxPrice':'1000000'},
 {'filterType':'MIN_NOTIONAL','notional':'5'}]}
class FakeMarket(Market):
    def __init__(self):super().__init__(20,transport=self.respond);self.calls=[]
    def respond(self,method,url,headers=None,body=None,timeout=None):
        self.calls.append((method,url));now=int(time.time()*1000)
        if 'exchangeInfo' in url:return 200,{}, {'symbols':[row('BTCUSDT'),row('ETHUSDT')]}
        if 'premiumIndex' in url:return 200,{}, {'symbol':url.split('symbol=')[1],'markPrice':'101','time':now}
        if url.endswith('/time'):return 200,{}, {'serverTime':now}
        tf='15m' if 'interval=15m' in url else '1h';interval=INTERVALS[tf];start=now//interval*interval
        return 200,{},[[start-(19-i)*interval,'100','102','99','101','10',start-(19-i)*interval+interval-1,'0',0,'0','0','0'] for i in range(20)]

class APITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)
        self.ledger=Ledger(self.path/'ledger.sqlite3');self.addCleanup(self.ledger.db.close)
        self.calls=[];self.key='private-test-key-never-publish'
    def transport(self,method,url,headers=None,body=None,timeout=None):
        self.calls.append((method,url,headers,body))
        if url.endswith('/health'):return 200,{}, {'status':'healthy','authenticated':True,'key_prefix':'sensitive'}
        output={'symbols':['BTCUSDT','ETHUSDT']} if body['prompt']==SCREENING else {**GOOD,'symbol':json.loads(body['message_history'][0]['content'])['symbol']}
        return 200,{}, {'mode':'smart','answer':None,'output':output}
    def client(self,transport=None):return NeuroAPI(self.ledger,key=self.key,transport=transport or self.transport,sleep=lambda _:None)
    def test_screen_prompt_schema_auth_mode_literal(self):
        n=self.client();n.ask('screen',SCREENING,SCREEN_SCHEMA,lambda v:selections(v,{'BTCUSDT':{},'ETHUSDT':{}}))
        m,u,h,b=self.calls[0];self.assertEqual(m,'POST');self.assertEqual(u,'https://api.neurobro.ai/api/v1/agent/ask')
        self.assertEqual(h['X-API-Key'],self.key);self.assertNotIn('Idempotency-Key',h)
        self.assertEqual(b['prompt'],'pilihkan 2 coin yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance');self.assertEqual(b['mode'],'smart');self.assertFalse(b['stream']);self.assertNotIn('system_prompt',b)
    def test_literal_analysis_separate_json_context(self):
        n=self.client();data=FakeMarket().data('BTCUSDT');n.ask('analysis',ANALYSIS,SETUP_SCHEMA,lambda v:setup(v,'BTCUSDT'),data)
        b=self.calls[0][3];self.assertEqual(b['prompt'],ANALYSIS);self.assertEqual(json.loads(b['message_history'][0]['content']),data);self.assertNotIn('75x',b['prompt'])
    def test_key_missing_fails_without_network(self):
        n=NeuroAPI(self.ledger,key='',transport=self.transport)
        with self.assertRaisesRegex(Review,'NEUROAPI_NOT_CONFIGURED'):n.health()
        with self.assertRaises(Review):n.ask('x',SCREENING,SCREEN_SCHEMA,lambda x:None)
        self.assertEqual(self.calls,[])
    def test_health_does_not_send_prompt_or_expose_prefix(self):
        self.assertEqual(self.client().health(),'NEUROAPI_CONNECTED');self.assertEqual(self.calls[0][0],'GET');self.assertIsNone(self.calls[0][3])
    def test_unknown_timeout_no_retry_or_secret_leak(self):
        def fail(*a,**k):raise RuntimeError(self.key+' private raw response')
        n=self.client(fail)
        with self.assertRaises(Review) as c:n.ask('x',SCREENING,SCREEN_SCHEMA,lambda x:None)
        self.assertNotIn(self.key,str(c.exception));self.assertNotIn(self.key,str(list(self.ledger.db.execute('SELECT * FROM api_requests'))))
        self.assertEqual(self.ledger.db.execute('SELECT attempts FROM api_requests').fetchone()[0],1)
        with self.assertRaises(Review):self.client().ask('x',SCREENING,SCREEN_SCHEMA,lambda x:None)
        self.assertEqual(self.calls,[])
    def test_bounded_documented_retry_same_body_without_idempotency_header(self):
        calls=[]
        def transient(*a,**k):calls.append(a);return (503,{'retry-after':'0'},None) if len(calls)<3 else self.transport(*a,**k)
        self.client(transient).ask('x',SCREENING,SCREEN_SCHEMA,lambda x:selections(x,{'BTCUSDT':{},'ETHUSDT':{}}))
        self.assertEqual(len(calls),3);self.assertTrue(all('Idempotency-Key' not in a[2] for a in calls))
        self.assertEqual(calls[0][3],calls[-1][3])
    def test_large_retry_after_stops_without_early_retry(self):
        n=self.client(lambda *a,**k:(429,{'retry-after':'999'},None))
        with self.assertRaises(Review):n.ask('x',SCREENING,SCREEN_SCHEMA,lambda x:None)
        self.assertEqual(self.ledger.db.execute('SELECT attempts FROM api_requests').fetchone()[0],1)
    def test_complete_restart_reuses_validated_output(self):
        validate=lambda x:selections(x,{'BTCUSDT':{},'ETHUSDT':{}})
        expected=self.client().ask('x',SCREENING,SCREEN_SCHEMA,validate)
        second=Ledger(self.path/'ledger.sqlite3')
        try:
            n=NeuroAPI(second,key=self.key,transport=lambda *a,**k:self.fail('Network replay'))
            self.assertEqual(n.ask('x',SCREENING,SCREEN_SCHEMA,validate),expected)
            with self.assertRaises(Review):n.ask('x','different',SCREEN_SCHEMA,validate)
        finally:second.db.close()
    def test_exact_two_active_symbols_no_fuzzy_mapping(self):
        for value in ({'symbols':['BTCUSDT']},{'symbols':['BTCUSDT','BTCUSDT']},{'symbols':['BTC','ETH']},{'symbols':['BTCUSDT','SOLUSDT']},{'symbols':['BTCUSDT','ETHUSDT'],'extra':1}):
            with self.subTest(value=value),self.assertRaises(Review):selections(value,{'BTCUSDT':{},'ETHUSDT':{}})
    def test_strict_structured_setup(self):
        for value in ('ENTRY: 100',{**GOOD,'extra':1},{k:v for k,v in GOOD.items() if k!='position_size'},{**GOOD,'symbol':'ETHUSDT'},{**GOOD,'side':'BUY'},{**GOOD,'limit_entry':'99-100'},{**GOOD,'position_size':'2'}):
            with self.subTest(value=value),self.assertRaises(Review):setup(value,'BTCUSDT')
    def test_rr_declaration_must_be_positive_numeric(self):
        for rr in (0,'3','1:2'):
            with self.assertRaises(Review):setup({**GOOD,'risk_reward':rr},'BTCUSDT')
    def test_overrisk_is_rejected_not_resized(self):
        signal=setup({**GOOD,'position_size':10},'BTCUSDT')
        with self.assertRaises(Review):risk_check(signal,RULES())
        self.assertEqual(signal.quantity,D('10'))
    def test_long_and_short_exact_plan(self):
        for output in (GOOD,{**GOOD,'side':'SHORT','take_profit':96,'stop_loss':102}):
            p=risk_check(setup(output,'BTCUSDT'),RULES());self.assertEqual(p['quantity'],'2');self.assertEqual(p['entry'],'100');self.assertEqual(p['leverage'],75);self.assertEqual(p['margin_mode'],'CROSS');self.assertEqual(p['mode'],'DRY_RUN')
    def test_workflow_end_to_end_two_plans_and_no_duplicate(self):
        m=FakeMarket();flow=Workflow(self.ledger,self.client(),m,self.path/'snapshot.json');s=flow.run()
        self.assertEqual(s['trades_today'],2);self.assertTrue(s['locked']);self.assertFalse(s['live_enabled']);self.assertEqual(s['status'],'DRY_RUN_READY')
        self.assertEqual(len(self.calls),3);self.assertTrue(all(x[0]=='GET' for x in m.calls))
        for t in s['trades']:self.assertTrue(t['plan']['protective_plan']['simulated'])
        flow.run();self.assertEqual(len(self.calls),3);self.assertEqual(self.ledger.count(),2)
    def test_analysis_duplicates_and_cycle_survive_restart(self):
        f=Workflow(self.ledger,self.client(),FakeMarket(),self.path/'snapshot.json');f.run()
        second=Ledger(self.path/'ledger.sqlite3')
        try:
            f=Workflow(second,NeuroAPI(second,key=self.key,transport=lambda *a,**k:self.fail('Duplicate')),FakeMarket(),self.path/'snapshot.json');f.run();self.assertEqual(second.count(),2)
        finally:second.db.close()
    def test_market_failure_stops_before_paid_screening(self):
        m=FakeMarket();m.catalog=lambda:(_ for _ in ()).throw(Review('FAIL'))
        s=Workflow(self.ledger,self.client(),m,self.path/'snapshot.json').run();self.assertEqual(s['status'],'ERROR');self.assertEqual(self.calls,[]);self.assertEqual(s['trades_today'],0)
    def test_analysis_failure_rejects_no_order(self):
        def fail(method,url,headers=None,body=None,timeout=None):
            if body['prompt']==ANALYSIS:return 401,{},None
            return self.transport(method,url,headers,body,timeout)
        s=Workflow(self.ledger,self.client(fail),FakeMarket(),self.path/'snapshot.json').run();self.assertEqual(s['trades_today'],0);self.assertEqual(s['status'],'REJECTED')
    def test_uncertain_cycle_cannot_start_again(self):
        flow=Workflow(self.ledger,self.client(),FakeMarket(),self.path/'snapshot.json')
        self.ledger.db.execute('INSERT INTO cycles VALUES(?,?,?)',(day(),'PENDING',time.time()))
        self.assertEqual(flow.run()['status'],'ERROR');self.assertEqual(self.calls,[])
    def test_quote_monitor_is_paper_only(self):
        m=FakeMarket();f=Workflow(self.ledger,self.client(),m,self.path/'snapshot.json');f.run()
        m.mark=lambda symbol:{'price':'100','time':int(time.time()*1000)};s=f.monitor();self.assertEqual(s['active_positions'],2)
        m.mark=lambda symbol:{'price':'104','time':int(time.time()*1000)};s=f.monitor();self.assertEqual(s['active_positions'],0);self.assertEqual(D(s['pnl_today']),D('16'))

class MarketTests(unittest.TestCase):
    def test_current_dual_frame_ohlcv(self):
        m=FakeMarket();d=m.data('BTCUSDT');m.fresh(d,'BTCUSDT');self.assertEqual(set(d['timeframes']),{'1h','15m'})
        for tf,frame in d['timeframes'].items():self.assertEqual(len(frame['candles']),20);self.assertGreater(frame['candles'][-1]['close_time'],d['server_time'])
    def test_stale_after_analysis_rejected(self):
        m=FakeMarket();d=m.data('BTCUSDT');d['fetched_at']-=181
        with self.assertRaises(Review):m.fresh(d,'BTCUSDT')
    def test_stale_provider_candles_rejected(self):
        m=FakeMarket();original=m.transport
        def stale(*a,**k):
            code,h,d=original(*a,**k)
            if 'klines?' in a[1]:
                for r in d:r[0]-=3600000;r[6]-=3600000
            return code,h,d
        m.transport=stale
        with self.assertRaises(Review):m.data('BTCUSDT')
    def test_wrong_or_stale_mark_rejected(self):
        for v in ({'symbol':'ETHUSDT','markPrice':'1','time':int(time.time()*1000)}, {'symbol':'BTCUSDT','markPrice':'1','time':0}):
            with self.assertRaises(Review):Market(transport=lambda *a,**k:(200,{},v)).mark('BTCUSDT')
    def test_catalog_inactive_contract_not_selected(self):
        inactive=row('BTCUSDT');inactive['status']='BREAK'
        m=Market(transport=lambda *a,**k:(200,{}, {'symbols':[inactive,row('ETHUSDT')]}))
        self.assertEqual(set(m.catalog()),{'ETHUSDT'})
    def test_min_notional_tick_step_loaded(self):
        r=FakeMarket().rules('BTCUSDT');self.assertEqual(r.min_notional,D('5'));self.assertEqual(r.tick,D('.01'));self.assertEqual(r.step,D('.001'))
    def test_no_order_endpoint_or_method(self):
        m=FakeMarket()
        with self.assertRaises(Review):m.get('order')
        self.assertEqual(m.calls,[])
    def test_config_is_bounded(self):
        for n in (0,19,501):
            with self.assertRaises(Review):Market(n)

class TransportSafetyTests(unittest.TestCase):
    def test_redirect_is_never_followed(self):
        from worker.http_client import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'redirect',{},'https://attacker.invalid'))
    def test_duplicate_json_fields_rejected(self):
        from worker.http_client import unique
        with self.assertRaises(ValueError):unique([('symbol','BTCUSDT'),('symbol','ETHUSDT')])
    def test_raw_transport_exception_is_sanitized(self):
        from worker.http_client import request
        with patch('worker.http_client.build_opener',side_effect=RuntimeError('PRIVATE_API_KEY')):
            with self.assertRaises(Review) as error:request('GET','https://api.neurobro.ai/api/v1/health')
        self.assertEqual(str(error.exception),'HTTP_RESULT_UNCERTAIN')
    def test_decimal_precision_never_silently_rounds_quantity(self):
        signal=Signal('BTCUSDT','LONG',D('100'),D('104'),D('98'),D('2.5000000000000000000000000000001'))
        with self.assertRaises(Review):risk_check(signal,RULES())
    def test_percent_price_filter_rejects_without_reprice(self):
        from dataclasses import replace
        r=replace(RULES(),multiplier_up=D('1.1'),multiplier_down=D('.9'),mark_price=D('200'))
        with self.assertRaises(Review):risk_check(setup(GOOD,'BTCUSDT'),r)
