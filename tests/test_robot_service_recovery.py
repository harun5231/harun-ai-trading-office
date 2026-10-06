"""Offline actor recovery checks; no listening sockets, exchange orders or real keys."""
import contextlib
import io
import json
import queue
import sqlite3
import tempfile
import threading
import time
import traceback
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from worker.api_service import Controller
from worker.core import Ledger,day
from worker.neuroapi import NeuroAPI,SCREEN_ONE_SCHEMA
from worker.robot import Coordinator,RobotStore
from worker.state import directory
from test_decisions import ExpandedMarket
from test_neuroapi import GOOD
from test_robot import Account


class ServiceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.controllers=[];self.calls=[];self.account=Account()
        self.account.position('HYPEUSDT','4.16')
        def transport(method,url,headers,body,timeout):
            self.calls.append(body)
            value={'symbols':['BTCUSDT']} if body['output_schema']==SCREEN_ONE_SCHEMA else GOOD
            return 200,{},dict(mode='smart',answer=None,output=value)
        self.transport=transport
        for target,value in (
            ('worker.api_service.NeuroAPI',lambda ledger:NeuroAPI(ledger,key='synthetic-local-key',transport=transport)),
            ('worker.api_service.Market',lambda *args:ExpandedMarket()),
            ('worker.api_service.BinanceReadOnly',lambda:self.account),
            ('worker.api_service.ROBOT_POLL_SECONDS',.02),
        ):
            patcher=patch(target,value);patcher.start();self.addCleanup(patcher.stop)
        self.addCleanup(self.stop_controllers)
    def stop_controllers(self):
        for controller in self.controllers:controller.drain(timeout=4)
    def start(self,path=None):
        controller=Controller(path or self.tmp.name);self.controllers.append(controller);return controller
    def wait_for(self,predicate,timeout=5):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.01)
        self.fail('Controller did not reach the expected local state')
    def ready_controller(self):
        with patch('worker.api_service.threading.Thread'):
            controller=self.start()
        ledger=controller.open_ledger()
        try:
            RobotStore(ledger.db).configure({'robot_on':True})
            robot=Coordinator(ledger,NeuroAPI(ledger,key='synthetic-local-key',transport=self.transport),ExpandedMarket(),lambda:self.account)
            robot.tick();robot.tick()
        finally:ledger.db.close()
        self.assertEqual(controller.robot()['setups'][0]['status'],'SETUP_READY')
        return controller

    def test_wal_robot_tables_and_snapshot_exist_before_either_actor_starts(self):
        starts=[]
        def before_start():
            root=Path(self.tmp.name)/'trading'
            with sqlite3.connect(root/'ledger.sqlite3') as db:
                self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'wal')
                names={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({'trades','robot_settings','robot_jobs','robot_simulations'}<=names)
                self.assertEqual(db.execute('SELECT enabled,risk FROM robot_settings').fetchone(),(0,'5'))
            self.assertFalse(json.loads((root/'snapshot.json').read_text())['live_enabled'])
            starts.append(True)
        with patch('worker.api_service.threading.Thread') as threads:
            threads.return_value.start.side_effect=before_start
            self.start()
        self.assertEqual(len(starts),2);self.assertEqual(self.calls,[])

    def test_native_cold_start_threads_have_serialized_connection_initialization(self):
        gate=threading.Lock();opening=0;maximum=0;first_thread=None
        def guarded_ledger(path):
            nonlocal opening,maximum,first_thread
            with gate:
                opening+=1;maximum=max(maximum,opening)
                if first_thread is None:first_thread=threading.current_thread()
            try:
                # Widen the original initial-WAL race without any network or sockets.
                time.sleep(.02)
                return Ledger(path)
            finally:
                with gate:opening-=1
        with patch('worker.api_service.Ledger',guarded_ledger):
            for attempt in range(3):
                controller=self.start(str(Path(self.tmp.name)/str(attempt)))
                self.wait_for(lambda:controller.checked>0)
                self.assertTrue(controller.healthy())
                self.assertIsNone(controller.snapshot()['worker_failure_code'])
                controller.drain(timeout=4)
        self.assertIs(first_thread,threading.current_thread())
        self.assertEqual(maximum,1);self.assertEqual(self.calls,[])

    def test_boot_failure_is_sanitized_and_starts_no_actor(self):
        with (patch('worker.api_service.Controller.open_ledger',side_effect=OSError('PRIVATE_BOOT_PAYLOAD')),
                patch('worker.api_service.threading.Thread') as threads):
            try:self.start()
            except RuntimeError as error:
                self.assertEqual(str(error),'WORKER_STATE_INITIALIZATION_FAILED')
                self.assertNotIn('PRIVATE_BOOT_PAYLOAD',''.join(traceback.format_exception(error)))
            else:self.fail('Expected sanitized initialization failure')
            threads.assert_not_called()

    def test_submit_cooldown_ignores_forward_and_backward_wall_clock_jumps(self):
        controller=Controller.__new__(Controller)
        controller.lock=threading.Lock();controller.stopping=threading.Event()
        controller.jobs=queue.Queue(maxsize=1);controller.busy=False
        controller.checked=1000;controller.checked_monotonic=50
        with patch('worker.api_service.time.monotonic',return_value=51),patch('worker.api_service.time.time',return_value=10**12):
            self.assertFalse(controller.submit('check'))
        with patch('worker.api_service.time.monotonic',return_value=54),patch('worker.api_service.time.time',return_value=1):
            self.assertTrue(controller.submit('check'))
        self.assertEqual(controller.checked,1000)

    def test_account_failure_hides_cached_capacity_and_ticket_review_then_recovers(self):
        with patch('worker.api_service.threading.Thread'):
            controller=self.start()
        ledger=controller.open_ledger()
        try:
            store=RobotStore(ledger.db);store.configure({'robot_on':True})
            robot=Coordinator(ledger,NeuroAPI(ledger,key='synthetic-local-key',transport=self.transport),ExpandedMarket(),lambda:self.account)
            robot.tick();robot.tick()
        finally:ledger.db.close()
        before=controller.robot();self.assertEqual(before['available_slots'],1)
        self.assertEqual(before['setups'][0]['status'],'SETUP_READY')
        self.assertIsNotNone(before['setups'][0]['account_review']['checked_at'])
        with contextlib.redirect_stdout(io.StringIO()):controller.failure('WORKER_ACCOUNT_READER_UNAVAILABLE',account=True)
        failed=controller.robot()
        self.assertEqual(failed['account_failure_code'],'WORKER_ACCOUNT_READER_UNAVAILABLE')
        for field in ('running_positions','available_slots','running_symbols','usdt_wallet_balance','usdt_available_balance','account_checked_at'):
            self.assertIsNone(failed[field],field)
        self.assertEqual(failed['manual_exposure'],[])
        review=failed['setups'][0]['account_review']
        for field in ('checked_at','available_slots','symbol_exposed'):self.assertIsNone(review[field],field)
        self.assertEqual(failed['setups'][0]['ticket'],before['setups'][0]['ticket'])
        controller.clear_failure(account=True)
        recovered=controller.robot()
        self.assertEqual(recovered['available_slots'],1)
        self.assertEqual(recovered['setups'][0]['account_review'],before['setups'][0]['account_review'])
        self.assertEqual(len(self.calls),2)

    def test_every_robot_response_masks_failed_account_review_and_keeps_simulation(self):
        controller=self.ready_controller();setup_id=controller.robot()['setups'][0]['id']
        with contextlib.redirect_stdout(io.StringIO()):controller.failure('WORKER_ACCOUNT_READER_UNAVAILABLE',account=True)
        responses=[
            controller.robot(),
            controller.robot({'risk_target_usdt':'8'}),
            controller.robot({'setup_id':setup_id,'decision':'APPROVED'},approval=True),
            controller.robot({'setup_id':setup_id,'scenario':'FULL_SL'},simulation=True),
            controller.robot({'robot_on':False}),
        ]
        for response in responses:
            self.assertEqual(response['account_failure_code'],'WORKER_ACCOUNT_READER_UNAVAILABLE')
            for field in ('running_positions','available_slots','running_symbols','usdt_wallet_balance','usdt_available_balance','account_checked_at'):
                self.assertIsNone(response[field],field)
            review=response['setups'][0]['account_review']
            for field in ('checked_at','available_slots','symbol_exposed'):self.assertIsNone(review[field],field)
            self.assertEqual(response['setups'][0]['ticket']['risk_target_usdt'],'5')
        self.assertEqual(responses[3]['simulation']['scenario'],'FULL_SL')
        self.assertEqual(responses[3]['simulation']['status'],'CLOSED')
        self.assertFalse(responses[3]['simulation']['real_order_submitted'])
        self.assertFalse(responses[4]['robot_on'])
        self.assertEqual(responses[4]['wait_reason'],'ROBOT_OFF')
        self.assertEqual(len(self.calls),2)

    def test_get_robot_is_sql_read_only_and_sees_latest_committed_observations(self):
        controller=self.ready_controller();original_connect=sqlite3.connect
        statements=[];denied=[];changes=[];connections=[]
        forbidden={
            getattr(sqlite3,name) for name in (
                'SQLITE_INSERT','SQLITE_UPDATE','SQLITE_DELETE','SQLITE_CREATE_INDEX','SQLITE_CREATE_TABLE',
                'SQLITE_CREATE_TEMP_INDEX','SQLITE_CREATE_TEMP_TABLE','SQLITE_CREATE_TEMP_TRIGGER','SQLITE_CREATE_TEMP_VIEW',
                'SQLITE_CREATE_TRIGGER','SQLITE_CREATE_VIEW','SQLITE_DROP_INDEX','SQLITE_DROP_TABLE','SQLITE_DROP_TEMP_INDEX',
                'SQLITE_DROP_TEMP_TABLE','SQLITE_DROP_TEMP_TRIGGER','SQLITE_DROP_TEMP_VIEW','SQLITE_DROP_TRIGGER','SQLITE_DROP_VIEW',
                'SQLITE_ALTER_TABLE','SQLITE_REINDEX','SQLITE_ANALYZE','SQLITE_ATTACH','SQLITE_DETACH',
            )
        }
        class ReadConnection(sqlite3.Connection):
            def close(self):changes.append(self.total_changes);super().close()
        def authorize(action,arg1,arg2,database,source):
            if action in forbidden or action==sqlite3.SQLITE_PRAGMA and arg1=='journal_mode' and arg2 is not None:
                denied.append((action,arg1));return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        def connect(path,*args,**kwargs):
            self.assertEqual(path,(controller.directory/'ledger.sqlite3').as_uri()+'?mode=ro')
            self.assertTrue(kwargs['uri']);self.assertEqual(kwargs['timeout'],5)
            db=original_connect(path,*args,**kwargs,factory=ReadConnection)
            db.set_authorizer(authorize);db.set_trace_callback(statements.append);connections.append(path);return db
        with (patch('worker.api_service.sqlite3.connect',connect),
              patch.object(controller,'open_ledger',side_effect=AssertionError('GET opened a state writer'))):
            first=controller.robot()
        self.assertEqual(first['available_slots'],1)
        ledger=controller.open_ledger()
        try:
            store=RobotStore(ledger.db)
            store.configure({'risk_target_usdt':'6'})
            store.report_account(dict(running_positions=2,running_symbols=['BTCUSDT','HYPEUSDT'],available_slots=0,
                manual_exposure=['BTCUSDT','HYPEUSDT'],usdt_wallet_balance='120.50',usdt_available_balance='100'))
        finally:ledger.db.close()
        with (patch('worker.api_service.sqlite3.connect',connect),
              patch.object(controller,'open_ledger',side_effect=AssertionError('GET opened a state writer'))):
            latest=controller.robot()
        self.assertEqual(latest['risk_target_usdt'],'6')
        self.assertEqual(latest['available_slots'],0);self.assertEqual(latest['usdt_wallet_balance'],'120.50')
        self.assertEqual(latest['setups'][0]['account_review']['available_slots'],0)
        self.assertTrue(latest['setups'][0]['account_review']['symbol_exposed'])
        self.assertIsNotNone(latest['setups'][0]['account_review']['checked_at'])
        self.assertEqual(latest['setups'][0]['ticket'],first['setups'][0]['ticket'])
        self.assertEqual(denied,[]);self.assertEqual(changes,[0,0]);self.assertEqual(len(connections),2)
        self.assertTrue(statements)
        self.assertTrue(all(sql.lstrip().upper().startswith(('SELECT','PRAGMA QUERY_ONLY')) for sql in statements))

    def test_unexpected_account_storage_failure_retries_without_payload_logging(self):
        with patch('worker.api_service.threading.Thread'):
            controller=self.start()
        ledger=controller.open_ledger()
        try:RobotStore(ledger.db).configure({'robot_on':True})
        finally:ledger.db.close()
        original=RobotStore.report_account;attempts=[];observations=[]
        def report(store,*args,**kwargs):
            attempts.append(True)
            if len(attempts)<=2:raise OSError('PRIVATE_ACCOUNT_STORAGE_DETAIL')
            return original(store,*args,**kwargs)
        class StopAfterTwoPolls:
            stopped=False
            def is_set(self):return self.stopped
            def set(self):self.stopped=True
            def wait(self,interval):
                observations.append((interval,controller.robot()))
                self.stopped=len(observations)==2;return self.stopped
        controller.stopping=StopAfterTwoPolls();output=io.StringIO()
        with contextlib.redirect_stdout(output),patch.object(RobotStore,'report_account',report):controller.account_loop()
        self.assertEqual(len(attempts),3)
        self.assertEqual([row[0] for row in observations],[15,15])
        self.assertEqual(observations[0][1]['account_failure_code'],'WORKER_ACCOUNT_READER_UNAVAILABLE')
        self.assertIsNone(observations[0][1]['available_slots'])
        self.assertIsNone(controller.snapshot()['account_reader_failure_code'])
        self.assertEqual(observations[1][1]['usdt_wallet_balance'],'117.25')
        self.assertNotIn('PRIVATE_ACCOUNT_STORAGE_DETAIL',output.getvalue())
        self.assertEqual(self.calls,[])

    def test_robot_off_does_not_clear_failed_account_review_without_a_new_observation(self):
        with patch('worker.api_service.threading.Thread'):
            controller=self.start()
        with contextlib.redirect_stdout(io.StringIO()):controller.failure('WORKER_ACCOUNT_READER_UNAVAILABLE',account=True)
        class StopAfterOnePoll:
            stopped=False
            def is_set(self):return self.stopped
            def set(self):self.stopped=True
            def wait(self,interval):self.stopped=True;return True
        controller.stopping=StopAfterOnePoll();controller.account_loop()
        self.assertEqual(controller.snapshot()['account_reader_failure_code'],'WORKER_ACCOUNT_READER_UNAVAILABLE')
        status=controller.robot()
        self.assertIsNone(status['available_slots']);self.assertIsNone(status['account_checked_at'])
        self.assertEqual(self.account.calls,[]);self.assertEqual(self.calls,[])

    def test_unexpected_tick_error_is_visible_sanitized_and_recovers(self):
        recovered=threading.Event();allow_recovery=threading.Event()
        class InterruptedCoordinator(Coordinator):
            first=True
            def tick(self):
                if self.first:
                    self.first=False;raise OSError('PRIVATE_REQUEST_OR_CREDENTIAL')
                if not allow_recovery.wait(4):raise TimeoutError('PRIVATE_WAIT_DETAIL')
                result=super().tick();recovered.set();return result
        output=io.StringIO()
        with contextlib.redirect_stdout(output),patch('worker.api_service.Coordinator',InterruptedCoordinator):
            controller=self.start()
            with controller.lock:controller.status='NEUROAPI_CONNECTED'
            self.wait_for(lambda:controller.snapshot()['worker_failure_code']=='ROBOT_COORDINATOR_UNAVAILABLE')
            status=controller.robot()
            self.assertEqual(status['bot_status'],'REJECTED')
            self.assertEqual(status['failure_code'],'ROBOT_COORDINATOR_UNAVAILABLE')
            self.assertEqual(controller.snapshot()['status'],'NEUROAPI_CONNECTED')
            self.assertTrue(controller.healthy())
            allow_recovery.set();self.assertTrue(recovered.wait(4))
            self.wait_for(lambda:controller.snapshot()['worker_failure_code'] is None)
            self.assertEqual(controller.robot()['wait_reason'],'ROBOT_OFF')
            controller.drain(timeout=4)
        self.assertNotIn('PRIVATE_REQUEST_OR_CREDENTIAL',output.getvalue())
        self.assertNotIn('PRIVATE_WAIT_DETAIL',output.getvalue())
        events=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events,[{'event':'WORKER_SERVICE_FAILURE','code':'ROBOT_COORDINATOR_UNAVAILABLE'}])
        self.assertEqual(self.calls,[])

    def test_unknown_paid_claim_is_not_replayed_on_retry_or_restart(self):
        root=directory(self.tmp.name);ledger=Ledger(root/'ledger.sqlite3')
        try:RobotStore(ledger.db).configure({'robot_on':True})
        finally:ledger.db.close()
        class ClaimedThenInterrupted(Coordinator):
            interrupted=False
            def tick(self):
                if not self.interrupted:
                    type(self).interrupted=True
                    cycle=day()+':robot-v7:0';operation=cycle+':screening:0:1'
                    self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',(operation,cycle,'SCREENING',None,'PENDING',None))
                    self.db.execute('INSERT INTO api_requests(operation,idempotency,body_hash,state,created,attempts,output) VALUES(?,?,?,?,?,?,?)',
                        (operation,None,'unknown-body-hash','PENDING',time.time(),1,None))
                    raise OSError('PRIVATE_UNKNOWN_PROVIDER_OUTCOME')
                return super().tick()
        output=io.StringIO()
        with contextlib.redirect_stdout(output),patch('worker.api_service.Coordinator',ClaimedThenInterrupted):
            controller=self.start()
            self.wait_for(lambda:controller.snapshot()['worker_failure_code']=='ROBOT_COORDINATOR_UNAVAILABLE')
            self.wait_for(lambda:controller.robot()['failure_code']=='ROBOT_REQUEST_NEEDS_REVIEW')
            controller.drain(timeout=4)
            restarted=self.start()
            self.wait_for(lambda:restarted.checked>0)
            self.assertEqual(restarted.robot()['failure_code'],'ROBOT_REQUEST_NEEDS_REVIEW')
            restarted.drain(timeout=4)
        ledger=Ledger(root/'ledger.sqlite3')
        try:
            self.assertEqual(ledger.db.execute('SELECT state,attempts FROM api_requests').fetchone()[:],('PENDING',1))
            self.assertEqual(ledger.db.execute('SELECT state FROM robot_jobs').fetchone()[0],'PENDING')
            self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0],0)
        finally:ledger.db.close()
        self.assertEqual(self.calls,[])
        self.assertNotIn('PRIVATE_UNKNOWN_PROVIDER_OUTCOME',output.getvalue())
