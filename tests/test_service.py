"""Offline service/control checks: no listening sockets or provider/order requests."""
import contextlib
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import Mock,patch
from worker.api_service import Controller,handler
from worker.core import Ledger
from worker.neuroapi import NeuroAPI
from worker.robot_store import RobotStore
from worker.binance_private import BinanceCheckError
from worker import robot_status,health

def observation(history=True,balance='116.928'):
    value={'source':'BINANCE_FUTURES','generated_at':'2026-10-06T12:00:00+00:00',
        'account':{'status':'CONNECTED','checked_at':'2026-10-06T12:00:00+00:00',
            'usdt_wallet_balance':balance,'usdt_available_balance':'100','positions':[],
            'active_positions':0,'position_mode':'ONE_WAY','can_trade':True,'multi_assets_margin':False}}
    if history:value.update(reports={'status':'AVAILABLE','checked_at':'2026-10-06T12:00:00+00:00',
            'pnl_today_usdt':'-1','realized_pnl_today_usdt':'-1','commission_today_usdt':'0',
            'funding_today_usdt':'0','trades_today':None,'complete':True},
        position_history={'status':'PARTIAL','checked_at':'2026-10-06T12:00:00+00:00',
            'items':[],'kind':'BINANCE_FILLS','complete':False})
    return value

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.controllers=[];self.provider_calls=[]
        def forbidden(*args,**kwargs):self.provider_calls.append(True);raise AssertionError('Provider request in a service test')
        p=patch('worker.api_service.NeuroAPI',lambda ledger:NeuroAPI(ledger,key='synthetic-local-key',transport=forbidden))
        p.start();self.addCleanup(p.stop);self.addCleanup(self.cleanup)
    def cleanup(self):
        for controller in self.controllers:controller.drain(timeout=3)
    def dormant(self):
        with patch('worker.api_service.threading.Thread'):
            value=Controller(self.tmp.name)
        self.controllers.append(value);return value
    def call(self,controller,path,method='GET',value=None,raw=None,token='control',origin='https://office.example',extra=None):
        cls=handler(controller,'R'*32,'C'*32,'https://office.example');request=cls.__new__(cls)
        request.path=path;headers={}
        if token:headers['Authorization']='Bearer '+('R'*32 if token=='read' else 'C'*32)
        if origin:headers['Origin']=origin
        if method=='POST':
            body=raw if raw is not None else json.dumps(value).encode()
            headers.update({'Content-Length':str(len(body)),'Content-Type':'application/json'})
            request.rfile=io.BytesIO(body);request.connection=Mock()
        if extra:headers.update(extra)
        request.headers=headers;replies=[];request.reply=lambda code,data:replies.append((code,data))
        getattr(request,'do_'+method)();return replies[0]
    def wait_for(self,predicate):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.01)
        self.fail('Local service condition did not complete')

    def test_robot_control_is_strict_and_authenticated(self):
        controller=self.dormant()
        for token,origin in (('read','https://office.example'),('control',None),('control','https://evil.example')):
            self.assertEqual(self.call(controller,'/robot/settings','POST',{'robot_on':True},token=token,origin=origin)[0],403)
        for raw in (b'{"robot_on":"true"}',b'{"robot_on":1}',b'{"robot_on":true,"robot_on":false}',
                    b'{"robot_on":true,"risk_target_usdt":10}',b'{}',b'{"mode":"LIVE"}',b'x'*1025):
            self.assertEqual(self.call(controller,'/robot/settings','POST',raw=raw)[0],400)
        self.assertFalse(controller.robot()['robot_on'])
        code,value=self.call(controller,'/robot/settings','POST',{'robot_on':True})
        self.assertEqual(code,200);self.assertTrue(value['robot_on']);self.assertTrue(controller.robot_wakeup.is_set())
        controller.robot_wakeup.clear();controller.robot({'robot_on':True});self.assertFalse(controller.robot_wakeup.is_set())
        self.assertFalse(controller.robot({'robot_on':False})['robot_on']);self.assertEqual(self.provider_calls,[])

    def test_risk_only_save_never_wakes_actor_and_mixed_changes_are_atomic(self):
        controller=self.dormant()
        code,value=self.call(controller,'/robot/settings','POST',{'risk_target_usdt':'7.25'})
        self.assertEqual(code,200);self.assertEqual(value['risk_target_usdt'],'7.25');self.assertFalse(value['robot_on'])
        self.assertFalse(controller.robot_wakeup.is_set())
        for risk in ('0','-1','100.000001','NaN','Infinity','',True,5,None):
            code,_=self.call(controller,'/robot/settings','POST',{'robot_on':True,'risk_target_usdt':risk})
            self.assertEqual(code,400,risk)
            value=controller.robot();self.assertFalse(value['robot_on']);self.assertEqual(value['risk_target_usdt'],'7.25')
            self.assertFalse(controller.robot_wakeup.is_set())
        code,value=self.call(controller,'/robot/settings','POST',{'robot_on':True,'risk_target_usdt':'10'})
        self.assertEqual(code,200);self.assertTrue(value['robot_on']);self.assertEqual(value['risk_target_usdt'],'10')
        self.assertTrue(controller.robot_wakeup.is_set());controller.robot_wakeup.clear()
        code,value=self.call(controller,'/robot/settings','POST',{'risk_target_usdt':'100'})
        self.assertEqual(code,200);self.assertTrue(value['robot_on']);self.assertEqual(value['risk_target_usdt'],'100')
        self.assertFalse(controller.robot_wakeup.is_set())
        code,_=self.call(controller,'/robot/settings','POST',{'robot_on':False,'risk_target_usdt':'101'})
        self.assertEqual(code,400);self.assertTrue(controller.robot()['robot_on'])
        self.assertEqual(controller.robot()['risk_target_usdt'],'100');self.assertEqual(self.provider_calls,[])

    def test_legacy_routes_are_absent_and_health_identifies_pipeline(self):
        controller=self.dormant()
        for path in ('/robot/simulation','/robot/approval','/neuroapi/run','/neuroapi/check',
                     '/neuroapi/status','/snapshot','/binance/status','/binance/execute','/binance/arm'):
            for method in ('GET','POST'):
                self.assertEqual(self.call(controller,path,method,value={})[0],404,(path,method))
        status,data=self.call(controller,'/health',token='read')
        self.assertEqual(status,200);self.assertEqual(data['mode'],'ORDER_PIPELINE')
        self.assertFalse(data['gateway_connected']);self.assertEqual(self.provider_calls,[])

    def test_health_uses_the_single_gateway_adapter_connection_status(self):
        controller=self.dormant()
        with patch('worker.api_service.OrderGateway') as gateway:
            gateway.return_value.status.return_value={'connected':True,'status':'CONNECTED'}
            status,data=self.call(controller,'/health',token='read')
            self.assertEqual(status,200);self.assertTrue(data['gateway_connected'])
            gateway.return_value.status.side_effect=RuntimeError('PRIVATE_GATEWAY_CONFIG')
            status,data=self.call(controller,'/health',token='read')
            self.assertEqual(status,503);self.assertEqual(data['error'],'GATEWAY_STATUS_UNAVAILABLE')
            self.assertNotIn('PRIVATE_GATEWAY_CONFIG',json.dumps(data))
        self.assertEqual(self.provider_calls,[])

    def test_status_gets_are_read_only_and_observe_new_cache_without_network(self):
        controller=self.dormant();ledger=controller.open_ledger()
        try:RobotStore(ledger.db).report_office(observation())
        finally:ledger.db.close()
        original=sqlite3.connect;writes=[];sql=[];connections=[]
        write_actions={sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_CREATE_INDEX,sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_DROP_TABLE,sqlite3.SQLITE_ALTER_TABLE}
        def readonly(path,*args,**kwargs):
            self.assertTrue(path.endswith('?mode=ro'));self.assertEqual(kwargs['timeout'],5)
            db=original(path,*args,**kwargs);connections.append(path)
            def authorizer(action,*values):
                if action in write_actions:writes.append(action);return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorizer);db.set_trace_callback(sql.append);return db
        with (patch('worker.api_service.sqlite3.connect',readonly),
              patch.object(controller,'open_ledger',side_effect=AssertionError('Status opened writer')),
              patch('worker.api_service.collect',side_effect=AssertionError('Status called Binance'))):
            robot=controller.robot();office=controller.office()
        self.assertFalse(robot['robot_on']);self.assertEqual(office['schema_version'],2)
        self.assertEqual(office['source'],'BINANCE_FUTURES');self.assertEqual(office['account']['usdt_wallet_balance'],'116.928')
        self.assertEqual(len(connections),2);self.assertEqual(writes,[])
        self.assertTrue(all(statement.lstrip().upper().startswith(('SELECT','PRAGMA QUERY_ONLY')) for statement in sql))
        ledger=controller.open_ledger()
        try:RobotStore(ledger.db).report_office(observation(history=False,balance='120'))
        finally:ledger.db.close()
        current=controller.office();self.assertEqual(current['account']['usdt_wallet_balance'],'120')
        self.assertEqual(current['reports']['pnl_today_usdt'],'-1');self.assertEqual(self.provider_calls,[])

    def test_account_refresh_runs_while_robot_off_and_history_is_throttled(self):
        controller=self.dormant();flags=[]
        class Clock:
            now=0
        class StopAfterTwo:
            stopped=False;polls=0
            def is_set(self):return self.stopped
            def set(self):self.stopped=True
            def wait(self,interval):
                self.polls+=1;Clock.now+=interval;self.stopped=self.polls==2;return self.stopped
        controller.stopping=StopAfterTwo()
        def collected(client,include_history):flags.append(include_history);return observation(history=include_history)
        with (patch('worker.api_service.time.monotonic',side_effect=lambda:Clock.now),
              patch('worker.api_service.BinanceReadOnly',return_value=object()),patch('worker.api_service.collect',collected)):
            controller.account_loop()
        self.assertFalse(controller.robot()['robot_on']);self.assertEqual(flags,[True,False])
        office=controller.office();self.assertEqual(office['account']['status'],'CONNECTED')
        self.assertEqual(office['reports']['pnl_today_usdt'],'-1');self.assertEqual(self.provider_calls,[])

    def test_unexpected_account_cache_failure_masks_values_and_retries(self):
        controller=self.dormant();actual=RobotStore.report_office;attempts=[]
        class StopAfterTwo:
            stopped=False;polls=0
            def is_set(self):return self.stopped
            def set(self):self.stopped=True
            def wait(self,interval):
                self.polls+=1
                if self.polls==1:
                    office=controller.office()
                    self.assert_null(office)
                self.stopped=self.polls==2;return self.stopped
            def assert_null(self,office):
                if office['account']['usdt_wallet_balance'] is not None:raise AssertionError('Stale account remained visible')
        controller.stopping=StopAfterTwo()
        def flaky(store,value):
            attempts.append(True)
            if len(attempts)==1:raise OSError('PRIVATE_CACHE_DETAIL')
            return actual(store,value)
        output=io.StringIO()
        with (contextlib.redirect_stdout(output),patch.object(RobotStore,'report_office',flaky),
              patch('worker.api_service.BinanceReadOnly',return_value=object()),
              patch('worker.api_service.collect',return_value=observation())):
            controller.account_loop()
        self.assertEqual(len(attempts),2);self.assertIsNone(controller.account_failure)
        self.assertEqual(controller.office()['account']['usdt_wallet_balance'],'116.928')
        self.assertNotIn('PRIVATE_CACHE_DETAIL',output.getvalue());self.assertEqual(self.provider_calls,[])

    def test_success_then_account_poll_failure_invalidates_all_real_cache_sections(self):
        controller=self.dormant();ledger=controller.open_ledger()
        value=observation();value['position_history']['items']=[{'id':'synthetic-fill','symbol':'HYPEUSDT'}]
        try:RobotStore(ledger.db).report_office(value)
        finally:ledger.db.close()
        self.assertEqual(controller.office()['reports']['status'],'AVAILABLE')
        self.assertEqual(len(controller.office()['position_history']['items']),1)
        class StopAfterOne:
            stopped=False
            def is_set(self):return self.stopped
            def set(self):self.stopped=True
            def wait(self,interval):self.stopped=True;return True
        controller.stopping=StopAfterOne()
        with patch('worker.api_service.BinanceReadOnly',return_value=object()),patch('worker.api_service.collect',side_effect=BinanceCheckError('BINANCE_AUTH_FAILED')):
            controller.account_loop()
        current=controller.office();self.assertFalse(current['robot']['robot_on'])
        for section in ('account','reports','position_history'):
            self.assertEqual(current[section]['status'],'UNAVAILABLE',section)
        self.assertIsNone(current['account']['usdt_wallet_balance'])
        self.assertIsNone(current['reports']['pnl_today_usdt']);self.assertEqual(current['position_history']['items'],[])
        self.assertEqual(self.provider_calls,[])

    def test_memory_failure_overlay_does_not_leave_employees_working_from_old_observations(self):
        controller=self.dormant();controller.robot({'robot_on':True});ledger=controller.open_ledger()
        try:RobotStore(ledger.db).report('SCREENING')
        finally:ledger.db.close()
        statuses=lambda:{row['id']:row['status'] for row in controller.office()['employees']}
        self.assertEqual(statuses()['market'],'WORKING')
        with contextlib.redirect_stdout(io.StringIO()):controller.failure('ROBOT_COORDINATOR_UNAVAILABLE')
        self.assertEqual(statuses()['market'],'IDLE');self.assertEqual(statuses()['boss'],'IDLE')
        controller.clear_failure();controller.robot({'robot_on':False});ledger=controller.open_ledger()
        value=observation();value['account'].update(checked_at=datetime.now(timezone.utc).isoformat(),active_positions=1,
            positions=[{'symbol':'HYPEUSDT','side':'LONG','position_side':'BOTH','quantity':'1','signed_quantity':'1','entry_price':'20','unrealized_pnl':'0'}])
        try:RobotStore(ledger.db).report_office(value)
        finally:ledger.db.close()
        self.assertEqual(statuses()['position'],'IDLE')
        self.assertTrue(all(state=='IDLE' for state in statuses().values()))
        with contextlib.redirect_stdout(io.StringIO()):controller.failure('WORKER_ACCOUNT_READER_UNAVAILABLE',account=True)
        current=controller.office();self.assertEqual(current['robot']['bot_status'],'OFF')
        self.assertEqual(statuses()['position'],'IDLE');self.assertEqual(statuses()['boss'],'IDLE')
        self.assertIsNone(current['account']['can_trade']);self.assertIsNone(current['reports']['checked_at'])
        self.assertEqual(current['position_history']['items'],[]);self.assertEqual(self.provider_calls,[])

    def test_schema_ready_before_threads_and_on_wakes_single_actor(self):
        ticks=[];first=threading.Event();on=threading.Event()
        class LocalCoordinator:
            def __init__(self,ledger,*args,**kwargs):self.store=RobotStore(ledger.db)
            def tick(self):
                value=self.store.settings()['robot_on'];ticks.append(value)
                (on if value else first).set()
        with (patch('worker.api_service.Coordinator',LocalCoordinator),
              patch('worker.api_service.BinanceReadOnly',return_value=object()),
              patch('worker.api_service.collect',return_value=observation())):
            controller=Controller(self.tmp.name);self.controllers.append(controller)
            self.assertTrue(first.wait(3));controller.robot({'robot_on':True});self.assertTrue(on.wait(2))
            self.assertEqual(ticks,[False,True]);self.assertTrue(controller.healthy())
            controller.drain(timeout=3)
        self.assertEqual(self.provider_calls,[])

    def test_robot_error_is_payload_free_and_off_remains_off(self):
        controller=self.dormant();output=io.StringIO()
        with contextlib.redirect_stdout(output):
            controller.failure('ROBOT_COORDINATOR_UNAVAILABLE');controller.failure('ROBOT_COORDINATOR_UNAVAILABLE')
        off=controller.robot();self.assertFalse(off['robot_on']);self.assertEqual(off['bot_status'],'OFF')
        on=controller.robot({'robot_on':True});self.assertEqual(on['bot_status'],'NEEDS_REVIEW')
        self.assertEqual(on['failure_code'],'ROBOT_COORDINATOR_UNAVAILABLE')
        self.assertEqual(len(output.getvalue().splitlines()),1)
        controller.clear_failure();self.assertIsNone(controller.worker_failure)

    def test_coordinator_exception_recovers_on_wakeup_and_wall_clock_does_not_control_liveness(self):
        attempts=[]
        class RecoveringCoordinator:
            def __init__(self,*args,**kwargs):pass
            def tick(self):
                attempts.append(True)
                if len(attempts)==1:raise RuntimeError('PRIVATE_PROVIDER_RESPONSE')
        output=io.StringIO()
        with (contextlib.redirect_stdout(output),patch('worker.api_service.Coordinator',RecoveringCoordinator),
              patch('worker.api_service.BinanceReadOnly',return_value=object()),
              patch('worker.api_service.collect',return_value=observation()),
              patch('worker.api_service.time.time',return_value=-200)):
            controller=Controller(self.tmp.name);self.controllers.append(controller)
            self.wait_for(lambda:controller.worker_failure=='ROBOT_COORDINATOR_UNAVAILABLE')
            self.assertTrue(controller.healthy());self.assertEqual(controller.robot()['bot_status'],'OFF')
            controller.robot({'robot_on':True})
            self.wait_for(lambda:len(attempts)==2 and controller.worker_failure is None)
            self.assertEqual(controller.checked,-200);self.assertTrue(controller.healthy())
            controller.drain(timeout=3)
        self.assertNotIn('PRIVATE_PROVIDER_RESPONSE',output.getvalue());self.assertEqual(self.provider_calls,[])

    def test_health_probe_only_gets_authenticated_local_liveness(self):
        requests=[]
        def opener(request,timeout):
            requests.append((request.get_method(),request.full_url,request.get_header('Authorization'),timeout))
            return io.BytesIO(json.dumps({'status':'ONLINE','mode':'ORDER_PIPELINE','gateway_connected':False}).encode())
        with patch.object(health.Path,'read_text',return_value='R'*32),patch.object(health,'urlopen',opener):
            self.assertTrue(health.probe_api())
        self.assertEqual(requests,[('GET','http://127.0.0.1:8787/health','Bearer '+'R'*32,5)])
        with patch.object(health.Path,'read_text',return_value='R'*32),patch.object(health,'urlopen',return_value=io.BytesIO(json.dumps({'status':'ONLINE','mode':'ORDER_PIPELINE','gateway_connected':True}).encode())):
            self.assertTrue(health.probe_api())
        for invalid in ({'status':'ONLINE'}, {'status':'OFFLINE','mode':'ORDER_PIPELINE','gateway_connected':False},
                        {'status':'ONLINE','mode':'ORDER_PIPELINE','gateway_connected':'true'}):
            with patch.object(health.Path,'read_text',return_value='R'*32),patch.object(health,'urlopen',return_value=io.BytesIO(json.dumps(invalid).encode())):
                self.assertFalse(health.probe_api())
        self.assertEqual(self.provider_calls,[])

    def test_diagnostics_only_reads_health_and_office_and_does_not_make_files(self):
        controller=self.dormant();root=Path(self.tmp.name);token=root/'read-token';token.write_text('R'*32)
        ledger=controller.open_ledger()
        self.addCleanup(ledger.db.close)
        # The live service retains actor/account writer connections. Keep one
        # here too: SQLite WAL sidecars already exist before diagnostics starts.
        RobotStore(ledger.db).report_office(observation())
        requested=[];before={path.relative_to(root) for path in root.rglob('*')}
        def opener(request,timeout):
            requested.append((request.get_method(),request.full_url))
            value={'status':'ONLINE','mode':'ORDER_PIPELINE','gateway_connected':False} if request.full_url.endswith('/health') else controller.office()
            return io.BytesIO(json.dumps(value).encode())
        value=robot_status.collect(root,token,opener)
        self.assertEqual([url.rsplit('/',1)[-1] for _,url in requested],['health','status'])
        self.assertTrue(requested[1][1].endswith('/office/status'))
        self.assertTrue(all(method=='GET' for method,_ in requested));self.assertEqual(value['source'],'BINANCE_FUTURES')
        self.assertEqual(value['account']['usdt_wallet_balance'],'116.928')
        self.assertEqual(before,{path.relative_to(root) for path in root.rglob('*')})
        self.assertNotIn('R'*32,json.dumps(value));self.assertEqual(self.provider_calls,[])
