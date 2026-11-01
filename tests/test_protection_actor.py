import ast
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from worker.protection_actor import ProtectionActor
from worker.fill_priority import FillPriority,owned_intent_lock
from tests import test_native_gateway_owner_revision as native_fixture


class ProtectionActorTests(unittest.TestCase):
    def test_real_gateway_lifecycle_runs_from_fill_event_while_robot_off_and_retains_other_pending(self):
        intent=native_fixture.gateway.build_intent(native_fixture.legacy_plan(),'owned-existing-operation')
        other=copy.deepcopy(intent);other['intent_id']='other-operation';other['client_order_id']='other-pending'
        with tempfile.TemporaryDirectory()as directory:
            path=Path(directory)/'ledger.sqlite3'
            db=sqlite3.connect(path,isolation_level=None)
            db.executescript('CREATE TABLE order_intents(id TEXT,candidate_id TEXT,state TEXT,payload TEXT,result TEXT);'
                'CREATE TABLE robot_settings(enabled INTEGER,risk TEXT);INSERT INTO robot_settings VALUES(0,"5");')
            db.execute('INSERT INTO order_intents VALUES(?,?,?,?,NULL)',(intent['intent_id'],'owned','NEEDS_REVIEW',json.dumps(intent)))
            db.execute('INSERT INTO order_intents VALUES(?,?,?,?,NULL)',(other['intent_id'],'other','ENTRY_PENDING',json.dumps(other)))
            before=db.execute('SELECT payload,state FROM order_intents WHERE id=?',(other['intent_id'],)).fetchone()
            db.close();stop=threading.Event();failures=[];args_seen=[]
            class Controller:
                stopping=stop;account_wakeup=threading.Event()
                def open_ledger(self):
                    value=sqlite3.connect(path,isolation_level=None);value.row_factory=sqlite3.Row
                    return types.SimpleNamespace(db=value)
                def failure(self,code):failures.append(code);stop.set()
            class Coordinator:
                def __init__(self,ledger,neuro,market,account,**kwargs):
                    self.db=ledger.db;args_seen.append((neuro,market,account))
                def verified_intent(self,row):return json.loads(row['payload'])
                def record_gateway_observation(self,operation,candidate,result,account):
                    self_outer.assertIsNone(account)
                    self.db.execute('UPDATE order_intents SET state=?,result=? WHERE id=?',
                        (result['state'],json.dumps(result),operation))
                    stop.set()
                    return {'bot_status':result['state']}
            self_outer=self
            class Feed:
                def __init__(self,key,on_event,on_recovery,**kwargs):
                    self.event=on_event;self.recovery=on_recovery
                def run(self,stopping):
                    self.recovery()
                    self.event({'e':'ORDER_TRADE_UPDATE','o':dict(x='TRADE',X='PARTIALLY_FILLED',
                        o='LIMIT',R=False,c=intent['client_order_id'],s='ETHUSDT',S='BUY',ps='BOTH',
                        z='0.4',l='0.4',L='100',i=101,t=55,T=int(time.time()*1000)-1000,n='0.02',N='USDT')})
                    stopping.wait(5)
            fake=native_fixture.FakeExchange(intent);fake.api_key='not-a-real-key'
            actor=ProtectionActor(Controller(),Coordinator,lambda:fake,feed_factory=Feed)
            thread=threading.Thread(target=actor.run);thread.start();thread.join(5)
            try:
                self.assertFalse(thread.is_alive());self.assertEqual(failures,[])
                self.assertEqual(args_seen,[(None,None,None)])
                db=sqlite3.connect(path)
                self.assertEqual(db.execute('SELECT enabled,risk FROM robot_settings').fetchone(),(0,'5'))
                self.assertEqual(db.execute('SELECT payload,state FROM order_intents WHERE id=?',(other['intent_id'],)).fetchone(),before)
                state,payload,result=db.execute('SELECT state,payload,result FROM order_intents WHERE id=?',(intent['intent_id'],)).fetchone()
                self.assertEqual(state,'POSITION_PROTECTED');self.assertEqual(json.loads(payload),intent)
                result=json.loads(result);self.assertTrue(result['sl_confirmed']and result['tp_confirmed'])
                self.assertTrue(result['fill_detected_at'])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM execution_cost_observations').fetchone()[0],1)
                self.assertIsNone(db.execute('SELECT expected_impact_rate FROM execution_cost_observations').fetchone()[0])
                from worker.dynamic_market import execution_samples
                self.assertEqual(execution_samples(db,'ETHUSDT'),[])
                self.assertEqual([p['type']for m,u,p in fake.calls if m=='POST'],['STOP_MARKET','TAKE_PROFIT_MARKET'])
                db.close()
            finally:
                stop.set();thread.join(5)

    def api_controller(self,namespace):
        path=Path(__file__).resolve().parents[1]/'vps_native/worker/api_service.py'
        node=next(n for n in ast.parse(path.read_text()).body if isinstance(n,ast.ClassDef)and n.name=='Controller')
        scope=dict(__name__='worker.test_actual_controller',__package__='worker',
            SHUTDOWN_DRAIN_SECONDS=600,ROBOT_POLL_SECONDS=45,ACCOUNT_POLL_SECONDS=15,
            HISTORY_POLL_SECONDS=60,ACCOUNT_FAILURE_CODES=set(),ACCOUNT_STALL_SECONDS=120,
            PROTECTION_STALL_SECONDS=180,ACTOR_STALL_SECONDS=600,
            time=time,threading=threading,os=__import__('os'),**namespace)
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),scope)
        controller=scope['Controller'].__new__(scope['Controller'])
        controller.stopping=threading.Event();controller.robot_wakeup=threading.Event()
        controller.account_wakeup=threading.Event();controller.state_lock=threading.Lock()
        controller.lock=threading.Lock();controller.worker_failure=None;controller.account_failure=None
        return controller

    def test_actual_api_analysis_admits_v4_policy_without_changing_gateway(self):
        calls=[];closed=[];sentinel=object();policy='DYNAMIC_NET_2R_V4'
        ledger=types.SimpleNamespace(db=types.SimpleNamespace(close=lambda:closed.append(True)))
        def coordinator(*args,**kwargs):
            calls.append((args,kwargs))
            return types.SimpleNamespace(tick=lambda:(controller.stopping.set(),controller.robot_wakeup.set()))
        controller=self.api_controller(dict(create_provider=lambda ledger:sentinel,
            Market=lambda *args:('market',args),Coordinator=coordinator,
            BinanceReadOnly='existing-reader',DYNAMIC_RISK_MODEL=policy))
        controller.open_ledger=lambda:ledger
        # The production factory must supply 200 CLOSED bars plus the running
        # bar even when an inherited pre-200-candle environment says100.
        with patch.dict('os.environ',{'OFFICE_CANDLE_LOOKBACK':'100'}):
            controller.loop()
        self.assertEqual(calls[0][0][2][1][0],201)
        self.assertEqual(calls[0][0][1],sentinel)
        self.assertEqual(calls[0][1]['new_setup_policy'],policy)
        self.assertEqual(closed,[True])

    def test_recovery_uses_existing_account_actor_before_expensive_account_history(self):
        calls=[];ledger=types.SimpleNamespace(db=types.SimpleNamespace(close=lambda:None))
        class Store:
            def __init__(self,db):pass
            def reconcile_carry(self,*args):pass
            def report_office(self,*args):pass
        def collect(client,**kwargs):
            calls.append('account-history');controller.stopping.set();controller.account_wakeup.set()
            return {'account':{'positions':[]},'generated_at':'2026-11-01T00:00:00+00:00'}
        controller=self.api_controller(dict(RobotStore=Store,OrderGateway=lambda:'existing-gateway',
            Coordinator=lambda *a,**k:types.SimpleNamespace(db=ledger.db),
            BinanceReadOnly=lambda:'existing-reader',collect=collect,
            collect_calendar=lambda *a,**k:{'checked_at':'2026-11-01T00:00:00+00:00'}))
        controller.open_ledger=lambda:ledger;controller.directory=Path('/unused')
        controller.protection=types.SimpleNamespace(recover_pending=lambda *args:calls.append('owned-get-recovery'))
        controller.account_loop()
        self.assertEqual(calls,['owned-get-recovery','account-history'])

    def test_per_intent_mutex_supports_nested_callbacks_without_a_cycle_lock(self):
        with tempfile.TemporaryDirectory()as directory:
            db=sqlite3.connect(Path(directory)/'ledger.sqlite3',isolation_level=None)
            with owned_intent_lock(db,'entry-a'):
                with owned_intent_lock(db,'entry-a'):
                    with owned_intent_lock(db,'entry-b'):pass
            self.assertEqual(len(list((Path(directory)/'intent-locks').glob('*.lock'))),2)
            db.close()


if __name__=='__main__':unittest.main()
