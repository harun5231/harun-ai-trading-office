"""Native coordinator retires only proved stale pre-submit commands; no IO."""
import copy
from datetime import datetime,timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import time
import unittest
from unittest.mock import patch

from worker import new_setup_policy as policy
from worker.core import D,DYNAMIC_RISK_MODEL,day
from worker.robot_provenance import stamp
from worker.robot_store import RobotStore
from tests import test_dynamic_risk_v4 as dynamic_fixture
from tests.test_native_gateway_owner_revision import gateway

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('worker.native_stale_coordinator',ROOT/'vps_native/worker/robot.py')
native=importlib.util.module_from_spec(SPEC)
PROVIDER_SPEC=importlib.util.spec_from_file_location('worker.research_provider',ROOT/'vps_native/worker/research_provider.py')
provider=importlib.util.module_from_spec(PROVIDER_SPEC)
with patch.dict(sys.modules,{'worker.order_gateway':gateway,'worker.research_provider':provider}),\
        patch('worker.owner_source_protocol.load_owner_release_cutoff',return_value='2026-11-04T00:00:00Z'):
    PROVIDER_SPEC.loader.exec_module(provider);SPEC.loader.exec_module(native)


class NativeStaleUnsubmittedTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:',isolation_level=None);self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''
        CREATE TABLE api_requests(operation TEXT PRIMARY KEY,output TEXT,state TEXT,body_hash TEXT);
        CREATE TABLE robot_candidates(id TEXT PRIMARY KEY,cycle TEXT,symbol TEXT,status TEXT,plan TEXT,failure_code TEXT);
        CREATE TABLE order_intents(id TEXT PRIMARY KEY,candidate_id TEXT UNIQUE,symbol TEXT,state TEXT,payload TEXT,result TEXT,failure_code TEXT,created TEXT,updated TEXT);
        CREATE TABLE robot_entry_receipts(id TEXT PRIMARY KEY,symbol TEXT,entry_day TEXT,confirmed_at TEXT);
        CREATE TABLE robot_settings(id INTEGER PRIMARY KEY,enabled INTEGER,risk TEXT);
        INSERT INTO robot_settings VALUES(1,1,'5');
        ''')
        self.operation=day()+':robot-v9:0:analysis-v9:ETHUSDT';self.cycle=self.operation.rsplit(':analysis-v9:',1)[0]
        output=dict(symbol='ETHUSDT',side='LONG',limit_entry=100,take_profit=100.5,stop_loss=99,risk_reward=.5)
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?)',(self.operation,json.dumps(output),'COMPLETE',hashlib.sha256(b'original reply').hexdigest()))
        # Produce a valid historical plan at its own clock, then observe it
        # later. Production clocks are never touched by these isolated tests.
        at=time.time()-601
        with patch('time.time',return_value=at):
            fixture=dynamic_fixture.DynamicOwnerRiskTests();rules=fixture.rules();plan=fixture.plan(rules=rules)
        plan['sizing_rules']=policy.serialize_rules(rules);plan['protection_working_type']='CONTRACT_PRICE'
        stamp(self.db,plan,self.operation)
        self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
            (self.operation,self.cycle,'ETHUSDT','READY_FOR_EXECUTION',json.dumps(plan),None))
        intent=gateway.build_intent(plan,self.operation);self.payload=json.dumps(intent);self.client=intent['client_order_id']
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
            (self.operation,self.operation,'ETHUSDT','READY_FOR_EXECUTION',self.payload,None,None,'created','updated'))
        self.coordinator=object.__new__(native.Coordinator);self.coordinator.db=self.db
        self.coordinator.store=RobotStore(self.db,initialize=False);self.coordinator.stopping=None
        self.coordinator.store.report=lambda state,account=None,reason=None,wait_reason=None:dict(
            bot_status=state,failure_code=reason,wait_reason=wait_reason)
        class Forbidden:
            def __getattr__(self,name):raise AssertionError('No provider, market or gateway call: '+name)
        self.coordinator.gateway=Forbidden();self.coordinator.market=Forbidden();self.coordinator.neuro=Forbidden()
        self.account=dict(running_positions=0,running_symbols=[],open_entry_symbols=[],available_slots=2)

    def candidate(self):return self.db.execute('SELECT * FROM robot_candidates WHERE id=?',(self.operation,)).fetchone()
    def intent(self):return self.db.execute('SELECT * FROM order_intents WHERE id=?',(self.operation,)).fetchone()
    def frozen(self):return {name:[tuple(r)for r in self.db.execute('SELECT * FROM '+name)]for name in(
        'api_requests','robot_candidates','order_intents','robot_entry_receipts','robot_settings')}
    def legacy_orphan(self):
        self.db.execute("UPDATE robot_candidates SET status='REJECTED',failure_code='ORDER_EVIDENCE_UNVERIFIED'")

    def test_native_future_stale_reason_retires_both_records_and_preserves_original_levels(self):
        original_plan=self.candidate()['plan'];original_source=self.frozen()['api_requests']
        result=self.coordinator.advance_execution(self.account,day())
        self.assertEqual(result['failure_code'],'STALE_ORDER_INTENT')
        self.assertEqual((self.candidate()['status'],self.intent()['state']),('REJECTED','REJECTED'))
        self.assertEqual(self.intent()['failure_code'],'STALE_ORDER_INTENT')
        self.assertEqual(self.candidate()['plan'],original_plan);self.assertEqual(self.intent()['payload'],self.payload)
        self.assertEqual(self.frozen()['api_requests'],original_source);self.assertEqual(self.frozen()['robot_entry_receipts'],[])
        self.assertTrue(self.coordinator.replaceable_result(self.candidate()))
        data=dict(target=1,round_symbols=['ETHUSDT'],screen=1,queue=[],seen=['ETHUSDT'])
        self.assertEqual(self.coordinator.research_budget(data,[self.candidate()]),1)
        self.assertEqual(self.coordinator.replacement_count(data,[self.candidate()]),1)
        self.assertIsNone(self.coordinator.advance_execution(self.account,day()))

    def test_native_scheduler_selectively_recovers_the_existing_mislabeled_orphan_once(self):
        self.legacy_orphan();plan=self.candidate()['plan'];result=self.coordinator.advance_execution(self.account,day())
        self.assertEqual(result['failure_code'],'STALE_ORDER_INTENT')
        self.assertEqual(self.candidate()['plan'],plan);self.assertEqual(self.intent()['payload'],self.payload)
        self.assertEqual(self.intent()['state'],'REJECTED');after=self.frozen()
        self.assertTrue(self.coordinator.replaceable_result(self.candidate()))
        self.assertIsNone(self.coordinator.advance_execution(self.account,day()));self.assertEqual(self.frozen(),after)

    def test_stale_before_any_intent_was_created_is_retired_without_creating_one(self):
        self.db.execute('DELETE FROM order_intents')
        result=self.coordinator.advance_execution(self.account,day())
        self.assertEqual(result['failure_code'],'STALE_ORDER_INTENT');self.assertIsNone(self.intent())
        self.assertTrue(self.coordinator.replaceable_result(self.candidate()))

    def test_fresh_candidate_is_not_classified_as_stale(self):
        row=self.candidate();plan=json.loads(row['plan']);clock=lambda:plan['rules_checked_at']+1
        self.assertFalse(policy.stale_unsubmitted(self.coordinator,row,build_intent=gateway.build_intent,clock=clock))

    def test_unknown_accepted_receipt_payload_or_source_drift_cannot_retire_or_replace(self):
        mutations=(
            "UPDATE order_intents SET state='SUBMITTING'",
            "UPDATE order_intents SET state='NEEDS_REVIEW',failure_code='ORDER_OUTCOME_UNKNOWN'",
            "UPDATE order_intents SET state='ENTRY_PENDING',result='{\"order_id\":\"123\"}'",
            "UPDATE order_intents SET result='{}'",
            "UPDATE order_intents SET payload='{}'",
            "UPDATE robot_candidates SET plan='{}'",
            "INSERT INTO robot_entry_receipts VALUES('"+self.client+"','ETHUSDT','"+day()+"','confirmed')",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.db.execute('SAVEPOINT fixture');self.legacy_orphan();self.db.execute(mutation)
                before=self.frozen();row=self.candidate()
                self.assertFalse(policy.stale_unsubmitted(self.coordinator,row,build_intent=gateway.build_intent,orphan=True))
                self.assertFalse(self.coordinator.replaceable_result(row));self.assertEqual(self.frozen(),before)
                self.db.execute('ROLLBACK TO fixture');self.db.execute('RELEASE fixture')

    def test_state_race_before_atomic_retirement_keeps_accepted_or_uncertain_rows(self):
        for state,result in(('SUBMITTING',None),('ENTRY_PENDING','{"order_id":"123"}')):
            with self.subTest(state=state):
                self.db.execute('UPDATE order_intents SET state=?,result=?',(state,result));before=self.frozen()
                self.assertIsNone(policy.retire_stale_unsubmitted(self.coordinator,self.candidate(),self.account,build_intent=gateway.build_intent))
                self.assertEqual(self.frozen(),before)

    def test_rejection_failure_rolls_back_the_pair_without_losing_plan_or_payload(self):
        before=self.frozen();original=self.coordinator.reject_intent
        def fail(*args):original(*args);raise RuntimeError('commit prevented')
        with patch.object(self.coordinator,'reject_intent',side_effect=fail),self.assertRaisesRegex(RuntimeError,'commit prevented'):
            policy.retire_stale_unsubmitted(self.coordinator,self.candidate(),self.account,build_intent=gateway.build_intent)
        self.assertEqual(self.frozen(),before);self.assertFalse(self.db.in_transaction)


if __name__=='__main__':unittest.main()
