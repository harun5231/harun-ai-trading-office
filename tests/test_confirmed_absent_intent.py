"""Read-only negative exchange proof retires an old intent, never retries it."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl

from worker import new_setup_policy as policy
from worker.core import day
from worker.research_guard import gateway_mutations_allowed
from tests import test_native_stale_unsubmitted as native_fixture

native=native_fixture.native
gateway=native_fixture.gateway


class ReadOnlyWire(gateway.OrderGateway):
    """The real gateway request/signer/entry parser stop at this in-memory wire."""
    def __init__(self, intent):
        self.intent=copy.deepcopy(intent)
        self._connected=True
        self.api_key='synthetic-offline-key'
        self.api_secret='synthetic-offline-secret'
        self.calls=[]
        self.values={}
        self.hook=None
        self.transient=0
        self.order=None
        self.clock_skew=0

    def _wire(self,method,path,query='',signed=False):
        params=dict(parse_qsl(query))
        params={k:v for k,v in params.items() if k not in ('signature','timestamp','recvWindow')}
        self.calls.append((method,path,params,gateway_mutations_allowed()))
        if method!='GET':raise AssertionError('Financial mutation attempted')
        if self.hook:self.hook(method,path,params)
        if path=='/fapi/v1/time':return {'serverTime':int((time.time()+self.clock_skew)*1000)}
        if path=='/fapi/v1/accountConfig':return {'canTrade':True,'dualSidePosition':False,'multiAssetsMargin':False}
        if path=='/fapi/v1/order':
            if self.transient:
                self.transient-=1
                raise gateway.Review('BINANCE_ORDER_OUTCOME_UNKNOWN')
            if self.order is not None:return copy.deepcopy(self.order)
            raise self._ExchangeError(-2013)
        if path in self.values:
            value=self.values[path]
            if isinstance(value,Exception):raise value
            return copy.deepcopy(value)
        if path in ('/fapi/v3/positionRisk','/fapi/v1/openOrders','/fapi/v1/openAlgoOrders','/fapi/v1/userTrades'):return []
        raise AssertionError(path)


class ConfirmedAbsentIntentTests(unittest.TestCase):
    def setUp(self):
        fixture=native_fixture.NativeStaleUnsubmittedTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture=fixture
        self.db=fixture.db
        self.coordinator=fixture.coordinator
        self.operation=fixture.operation
        self.client=fixture.client
        self.payload=fixture.payload
        self.plan=fixture.candidate()['plan']
        self.account=fixture.account
        at=time.time()
        created=datetime.fromtimestamp(at-120,timezone.utc).isoformat()
        updated=datetime.fromtimestamp(at-40,timezone.utc).isoformat()
        self.db.execute("UPDATE order_intents SET state='NEEDS_REVIEW',failure_code='BINANCE_ORDER_OUTCOME_UNKNOWN_PREFLIGHT',created=?,updated=?",(created,updated))
        self.db.execute("UPDATE robot_candidates SET status='NEEDS_REVIEW',failure_code='BINANCE_ORDER_OUTCOME_UNKNOWN_PREFLIGHT'")
        self.exchange=ReadOnlyWire(json.loads(self.payload))
        self.coordinator.gateway=self.exchange

    def candidate(self):return self.fixture.candidate()
    def intent(self):return self.fixture.intent()
    def frozen(self):return self.fixture.frozen()

    def error(self):
        try:
            with self.exchange._operation():self.exchange._entry(json.loads(self.payload))
        except Exception as failure:return failure
        self.fail('Fixture did not produce an original native entry lookup error')

    def recover(self,error=None):
        error=error or self.error()
        self.exchange.calls=[]
        return policy.retire_confirmed_absent(self.coordinator,self.intent(),self.account,error)

    def assert_retained(self,before):
        self.assertEqual(self.frozen(),before)
        self.assertFalse(policy.confirmed_absent_result(self.coordinator,self.candidate(),build_intent=gateway.build_intent))
        self.assertTrue(all(method=='GET' for method,*_ in self.exchange.calls))
        self.assertFalse(self.db.in_transaction)

    def test_complete_fresh_negative_proof_preserves_payload_plan_history_and_quota(self):
        before=self.frozen()
        result=self.recover()
        self.assertEqual(result['failure_code'],policy.NOT_ACCEPTED)
        self.assertEqual((self.intent()['state'],self.candidate()['status']),('REJECTED','REJECTED'))
        self.assertEqual(self.intent()['payload'],self.payload)
        self.assertEqual(self.candidate()['plan'],self.plan)
        self.assertEqual(self.frozen()['api_requests'],before['api_requests'])
        self.assertEqual(self.frozen()['robot_entry_receipts'],[])
        self.assertEqual(self.coordinator.store.entries(day()),0)
        self.assertTrue(self.coordinator.replaceable_result(self.candidate()))
        value=json.loads(self.intent()['result'])
        proof=value['negative_proof']
        self.assertEqual(value['outcome'],'CONFIRMED_NOT_ACCEPTED')
        self.assertNotIn('order_id',value)
        self.assertEqual(proof['payload_sha256'],hashlib.sha256(self.payload.encode()).hexdigest())
        self.assertEqual([r['path']for r in proof['reads']],list(policy._NEGATIVE_PATHS))
        self.assertEqual([r.get('exchange_code')for r in proof['reads']if r['path']=='/fapi/v1/order'],[-2013,-2013])
        self.assertEqual([r['rows']for r in proof['reads'][2:5]],[0,0,0])
        self.assertTrue(all(method=='GET' and not allowed for method,path,params,allowed in self.exchange.calls))
        history=next(params for method,path,params,allowed in self.exchange.calls if path=='/fapi/v1/userTrades')
        self.assertEqual(history['symbol'],'ETHUSDT')
        self.assertEqual(history['limit'],'1000')
        self.assertLess(int(history['startTime']),datetime.fromisoformat(self.intent()['created']).timestamp()*1000)

    def test_new_proof_records_the_exact_reviewed_native_source(self):
        self.assertIsNotNone(self.recover())
        value=json.loads(self.intent()['result'])
        current=hashlib.sha256(Path(self.exchange._entry.__func__.__code__.co_filename).read_bytes()).hexdigest()
        self.assertEqual(value['negative_proof']['gateway_source_sha256'],current)
        self.assertEqual(current,'df129ffe67aee9f47d4bd241bc83139a3f23bf4468464c0cbfdf20fabc27b579')
        self.assertNotEqual(current,policy._GATEWAY_SOURCE)
        self.assertTrue(self.coordinator.replaceable_result(self.candidate()))

    def test_historical_approved_source_proof_remains_readable_without_recalculation(self):
        self.assertIsNotNone(self.recover())
        current=json.loads(self.intent()['result'])
        # A persisted legacy proof has the identical schema and original source
        # identity. Changing only this fixture's source/hash simulates that row;
        # production readers never rewrite the old proof or frozen parameters.
        legacy=copy.deepcopy(current)
        legacy['negative_proof']['gateway_source_sha256']=policy._GATEWAY_SOURCE
        legacy['negative_proof_sha256']=policy._proof_hash(legacy['negative_proof'])
        self.db.execute('UPDATE order_intents SET result=?',(json.dumps(legacy),))
        frozen=self.frozen()
        self.assertTrue(self.coordinator.replaceable_result(self.candidate()))
        self.assertEqual(self.frozen(),frozen)
        self.assertEqual(self.intent()['payload'],self.payload)
        self.assertEqual(self.candidate()['plan'],self.plan)

    def test_unapproved_live_source_cannot_collect_or_retire_an_intent(self):
        error=self.error();before=self.frozen()
        with patch.object(policy.Path,'read_bytes',return_value=b'unreviewed-native-source'):
            self.assertIsNone(self.recover(error))
        self.assertEqual(self.exchange.calls,[])
        self.assert_retained(before)

    def test_unapproved_persisted_source_cannot_release_replacement_even_with_matching_digest(self):
        self.assertIsNotNone(self.recover())
        value=json.loads(self.intent()['result'])
        value['negative_proof']['gateway_source_sha256']='0'*64
        value['negative_proof_sha256']=policy._proof_hash(value['negative_proof'])
        self.db.execute('UPDATE order_intents SET result=?',(json.dumps(value),))
        frozen=self.frozen()
        self.assertFalse(self.coordinator.replaceable_result(self.candidate()))
        self.assertEqual(self.frozen(),frozen)

    def test_native_tick_retries_read_then_releases_replacement_on_following_tick_without_submit(self):
        self.scheduler_progression()

    def test_restarted_native_coordinator_releases_old_submitting_claim_without_resubmit(self):
        self.restart_claim('READY_FOR_EXECUTION',None,180)
        restarted=object.__new__(native.Coordinator)
        restarted.__dict__.update(self.coordinator.__dict__)
        self.coordinator=restarted
        self.scheduler_progression()
        proof=json.loads(self.intent()['result'])['negative_proof']['source_attempt']
        self.assertEqual((proof['state'],proof['failure_code']),('SUBMITTING',None))
        self.assertEqual((proof['candidate_status'],proof['candidate_failure_code']),('READY_FOR_EXECUTION',None))

    def restart_claim(self,status,reason,age):
        at=time.time()
        self.db.execute("UPDATE order_intents SET state='SUBMITTING',failure_code=NULL,created=?,updated=?,result=NULL",
            tuple(datetime.fromtimestamp(v,timezone.utc).isoformat()for v in(at-age-60,at-age)))
        self.db.execute('UPDATE robot_candidates SET status=?,failure_code=?',(status,reason))

    def scheduler_progression(self):
        # Give tick its own temporary ledger directory; every exchange/provider
        # dependency below remains an in-memory fixture.
        directory=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(directory.cleanup)
        disk=sqlite3.connect(str(Path(directory.name)/'ledger.sqlite3'),isolation_level=None)
        self.addCleanup(disk.close)
        disk.row_factory=sqlite3.Row
        self.db.backup(disk)
        self.db=disk;self.fixture.db=disk;self.coordinator.db=disk
        self.coordinator.store.db=disk
        self.db.executescript('''
            CREATE TABLE robot_cycles(id TEXT PRIMARY KEY,day TEXT,entry_epoch INTEGER,state TEXT,data TEXT);
            CREATE TABLE robot_jobs(operation TEXT PRIMARY KEY,cycle TEXT,kind TEXT,symbol TEXT,state TEXT,risk_target TEXT);
            ALTER TABLE api_requests ADD COLUMN failure_code TEXT;
            ALTER TABLE api_requests ADD COLUMN attempts INTEGER DEFAULT 1;
            ALTER TABLE api_requests ADD COLUMN created REAL DEFAULT 1;
            ALTER TABLE api_requests ADD COLUMN idempotency TEXT;
        ''')
        cycle=self.fixture.cycle
        data=dict(target=1,queue=[],seen=['ETHUSDT'],screen=0,replacements=0,round_symbols=['ETHUSDT'])
        self.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',(cycle,day(),0,'ACTIVE',json.dumps(data)))
        self.coordinator.account_factory=lambda:None
        self.coordinator.store.report_account=lambda *args:None
        self.coordinator.execution_slots=lambda account,today:1
        self.coordinator.settle_previous_research=lambda today,account:None
        self.coordinator.recover_invalid_screening=lambda today,account:None
        self.coordinator.neuro=type('FixtureNeuro',(),{'require_key':lambda self:None})()
        screens=[]
        def screen(cycle,data,count,account,today,initial):
            screens.append((cycle,copy.deepcopy(data),count,initial))
            return dict(bot_status='SCREENING',failure_code=None,wait_reason=None)
        self.coordinator.screen=screen
        self.exchange.transient=1
        initial_state=self.intent()['state']
        with patch.dict(sys.modules,{'worker.research_provider':native_fixture.provider}),\
                patch.object(native,'account_state',return_value=self.account),\
                patch('worker.trade_evaluation_runtime.after_trading_tick'),\
                patch('worker.screening_response_recovery.resume_uncertain_screening',return_value=None),\
                patch('worker.analysis_response_recovery.resume_uncertain_analysis',return_value=None):
            first=self.coordinator.tick()
            self.assertEqual(first['bot_status'],'NEEDS_REVIEW')
            self.assertEqual(screens,[])
            self.assertEqual(self.intent()['state'],initial_state)
            second=self.coordinator.tick()
            self.assertEqual(second['failure_code'],policy.NOT_ACCEPTED)
            self.assertEqual(screens,[])
            third=self.coordinator.tick()
        self.assertEqual(third['bot_status'],'SCREENING')
        self.assertEqual(len(screens),1)
        self.assertEqual(screens[0][2:],(1,False))
        self.assertIn('ETHUSDT',screens[0][1]['seen'])
        self.assertEqual(self.intent()['payload'],self.payload)
        self.assertEqual(self.candidate()['plan'],self.plan)
        self.assertTrue(all(method=='GET' for method,*_ in self.exchange.calls))
        self.assertEqual(self.frozen()['robot_entry_receipts'],[])

    def test_transport_failure_any_inventory_or_history_cannot_prove_absence(self):
        cases=(
            ('/fapi/v3/positionRisk',[{'symbol':'ETHUSDT','positionSide':'BOTH','positionAmt':'0.1'}]),
            ('/fapi/v3/positionRisk',[{'symbol':'ETHUSDT','positionSide':'LONG','positionAmt':'0'}]),
            ('/fapi/v1/openOrders',[{'symbol':'ETHUSDT','orderId':123}]),
            ('/fapi/v1/openAlgoOrders',[{'symbol':'ETHUSDT','algoId':124}]),
            ('/fapi/v1/userTrades',[{'symbol':'ETHUSDT','orderId':125,'qty':'0.1'}]),
            ('/fapi/v1/userTrades',[{}]*1000),
            ('/fapi/v1/userTrades',{}),
            ('/fapi/v1/openOrders',TimeoutError('synthetic transport failure')),
        )
        for path,value in cases:
            with self.subTest(path=path,value_type=type(value).__name__):
                self.exchange.values={path:value};before=self.frozen()
                self.assertIsNone(self.recover());self.assert_retained(before)
        self.exchange.values={}

    def test_nonnegative_original_error_or_forged_2013_without_native_entry_frame_is_refused(self):
        for error in (gateway.Review('BINANCE_ORDER_OUTCOME_UNKNOWN'),self.exchange._ExchangeError(-2013),self.exchange._ExchangeError(-2015)):
            before=self.frozen()
            self.assertIsNone(self.recover(error));self.assert_retained(before)
            self.assertEqual(self.exchange.calls,[])

    def test_positive_entry_on_either_bookend_cannot_be_retired(self):
        error=self.error()
        for bookend in (1,2):
            count=[0]
            def race(method,path,params):
                if path=='/fapi/v1/order':
                    count[0]+=1
                    if count[0]==bookend:self.exchange.order={'status':'CANCELED','executedQty':'0','orderId':101}
            self.exchange.hook=race;self.exchange.order=None;before=self.frozen()
            self.assertIsNone(self.recover(error));self.assert_retained(before)
        self.exchange.hook=None;self.exchange.order=None

    def test_accepted_partial_canceled_or_filled_intents_are_never_retired(self):
        error=self.error()
        for state,result in (('ENTRY_PENDING','{}'),('POSITION_PROTECTED','{}'),('CLOSED','{}'),('REJECTED','{}'),('SUBMITTING',None),('NEEDS_REVIEW','{}')):
            with self.subTest(state=state,result=result):
                self.db.execute('UPDATE order_intents SET state=?,result=?',(state,result))
                before=self.frozen();self.assertIsNone(self.recover(error));self.assert_retained(before)
                self.db.execute("UPDATE order_intents SET state='NEEDS_REVIEW',result=NULL")

    def test_receipts_and_fill_events_block_negative_retirement(self):
        self.db.executescript('CREATE TABLE fill_priority(client_id TEXT,intent_id TEXT);CREATE TABLE entry_fill_samples(client_id TEXT);')
        for table,values in (
                ('robot_entry_receipts',(self.client,'ETHUSDT',day(),'confirmed')),
                ('robot_entry_receipts',(self.operation,'ETHUSDT',day(),'confirmed')),
                ('fill_priority',(self.client,self.operation)),
                ('fill_priority',('different-client',self.operation)),
                ('entry_fill_samples',(self.client,))):
            with self.subTest(table=table,values=values):
                self.db.execute('INSERT INTO '+table+' VALUES('+','.join('?'for _ in values)+')',values)
                before=self.frozen();self.assertIsNone(self.recover());self.assert_retained(before)
                self.db.execute('DELETE FROM '+table)

    def test_stream_receipt_or_owner_state_race_before_commit_refuses_retirement(self):
        error=self.error()
        self.db.executescript('CREATE TABLE fill_priority(client_id TEXT,intent_id TEXT);CREATE TABLE entry_fill_samples(client_id TEXT);')
        for mutation in ('receipt','priority','sample','settings','payload','candidate'):
            with self.subTest(mutation=mutation):
                original_payload=self.intent()['payload'];original_plan=self.candidate()['plan']
                def race(method,path,params):
                    if path!='/fapi/v1/userTrades':return
                    if mutation=='receipt':self.db.execute('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)',(self.client,'ETHUSDT',day(),'confirmed'))
                    elif mutation=='priority':self.db.execute('INSERT INTO fill_priority VALUES(?,?)',(self.client,self.operation))
                    elif mutation=='sample':self.db.execute('INSERT INTO entry_fill_samples VALUES(?)',(self.client,))
                    elif mutation=='settings':self.db.execute('UPDATE robot_settings SET enabled=0')
                    elif mutation=='payload':self.db.execute("UPDATE order_intents SET payload='{}'")
                    else:self.db.execute("UPDATE robot_candidates SET plan='{}'")
                self.exchange.hook=race
                self.assertIsNone(self.recover(error))
                self.assertEqual(self.intent()['state'],'NEEDS_REVIEW')
                self.assertIsNone(self.intent()['result'])
                self.db.execute('DELETE FROM robot_entry_receipts');self.db.execute('DELETE FROM fill_priority');self.db.execute('DELETE FROM entry_fill_samples')
                self.db.execute('UPDATE robot_settings SET enabled=1')
                self.db.execute('UPDATE order_intents SET payload=?',(original_payload,));self.db.execute('UPDATE robot_candidates SET plan=?',(original_plan,))
        self.exchange.hook=None

    def test_clock_settle_retention_or_server_skew_refuses_negative_proof(self):
        error=self.error();original=dict(self.intent());at=time.time()
        for created,updated,skew in ((at-120,at-1,0),(at-73*3600,at-40,0),(at-120,at-40,10),(at-120,at-40,-10)):
            with self.subTest(created=created,updated=updated,skew=skew):
                self.db.execute('UPDATE order_intents SET created=?,updated=?',tuple(datetime.fromtimestamp(v,timezone.utc).isoformat()for v in(created,updated)))
                self.exchange.clock_skew=skew;before=self.frozen()
                self.assertIsNone(self.recover(error));self.assert_retained(before)
        self.exchange.clock_skew=0
        self.db.execute('UPDATE order_intents SET created=?,updated=?',(original['created'],original['updated']))


    def test_submitting_claim_matrix_requires_old_callback_fence_and_original_candidate(self):
        original_intent=tuple(self.intent());original_candidate=tuple(self.candidate())
        for status,reason,age,accepted in (
                ('READY_FOR_EXECUTION',None,180,True),
                ('EXECUTION_BLOCKED','BINANCE_ORDER_GATEWAY_NOT_CONNECTED',180,True),
                ('READY_FOR_EXECUTION',None,30,False),
                ('EXECUTION_BLOCKED','BINANCE_ORDER_GATEWAY_NOT_CONNECTED',30,False),
                ('NEEDS_REVIEW','ORDER_OUTCOME_UNKNOWN',180,False),
                ('ENTRY_PENDING',None,180,False),
                ('READY_FOR_EXECUTION','unexpected',180,False)):
            with self.subTest(status=status,reason=reason,age=age):
                self.restart_claim(status,reason,age);before=self.frozen()
                recovered=self.recover()
                if accepted:
                    self.assertEqual(recovered['failure_code'],policy.NOT_ACCEPTED)
                    self.assertTrue(self.coordinator.replaceable_result(self.candidate()))
                    self.assertEqual(self.intent()['payload'],self.payload)
                else:
                    self.assertIsNone(recovered);self.assert_retained(before)
                self.db.execute('DELETE FROM order_intents');self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',original_intent)
                self.db.execute('DELETE FROM robot_candidates');self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',original_candidate)

    def test_monotonic_collection_bound_clock_backstep_and_slow_server_read_refuse(self):
        original=time.monotonic
        error=self.error()
        for path,delay in (('/fapi/v3/positionRisk',31),('/fapi/v1/time',3)):
            elapsed=[0]
            def hook(method,current,params):
                if current==path:elapsed[0]=delay
            self.exchange.hook=hook;before=self.frozen()
            with patch('time.monotonic',side_effect=lambda:original()+elapsed[0]):
                self.assertIsNone(self.recover(error))
            self.assert_retained(before)
        self.exchange.hook=None
        at=time.time();calls=[0]
        def backstep():
            calls[0]+=1
            return at if calls[0]<3 else at-1
        before=self.frozen()
        self.assertIsNone(policy.retire_confirmed_absent(self.coordinator,self.intent(),self.account,error,clock=backstep))
        self.assert_retained(before)

    def test_tampered_proof_or_bare_reason_cannot_release_replacement(self):
        self.assertIsNotNone(self.recover())
        original=self.intent()['result']
        for change in ('digest','payload','reads','reason-only'):
            with self.subTest(change=change):
                value=json.loads(original)
                if change=='digest':value['negative_proof_sha256']='0'*64
                elif change=='payload':value['negative_proof']['payload_sha256']='0'*64
                elif change=='reads':value['negative_proof']['reads'][3]['rows']=1
                else:value={}
                self.db.execute('UPDATE order_intents SET result=?',(json.dumps(value),))
                self.assertFalse(self.coordinator.replaceable_result(self.candidate()))
        self.db.execute('UPDATE order_intents SET result=?',(original,))
        self.assertTrue(self.coordinator.replaceable_result(self.candidate()))


if __name__=='__main__':unittest.main()
