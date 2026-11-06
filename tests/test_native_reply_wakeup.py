"""Existing native actor wakeups; controlled time, no provider or order transport."""
import ast
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
import unittest

from worker.core import Review
from worker.neuroapi import SETUP_SCHEMA_V4
from worker.owner_source_protocol import completed_decision_fresh,WEBSITE_SOURCE


ROOT=Path(__file__).resolve().parents[1]
OP='2026-11-06:robot-v9:0:analysis-v9:ETHUSDT'
REQUEST='a56a7ecebafb9e86d0e99157704224b276599487a4d4a1cc34de6f7907b021c1'
CAPTURED=1793980359.907221
RECEIVED=1793980505.7471535
OLD_CONSUMED=1793980555.2537105


class Clock:
    def __init__(self):self.now=RECEIVED
    def time(self):return self.now
    def monotonic(self):return self.now


class Wake:
    def __init__(self,clock):self.flag=False;self.clock=clock;self.emitted=0
    def is_set(self):return self.flag
    def clear(self):self.flag=False
    def set(self):self.flag=True;self.emitted+=1
    def wait(self,seconds):
        if self.flag:return True
        self.clock.now+=seconds
        return False


def status(state,wait=None,**extra):
    return dict(robot_on=True,bot_status=state,wait_reason=wait,failure_code=None,
        last_decision=dict(symbol='ETHUSDT',status=state),**extra)


class NativeReplyWakeTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock();self.db=sqlite3.connect(':memory:',isolation_level=None)
        self.db.row_factory=sqlite3.Row;self.addCleanup(self.db.close)
        self.db.executescript('''CREATE TABLE api_requests(operation TEXT PRIMARY KEY,body_hash TEXT,
            state TEXT,output TEXT,failure_code TEXT);
            CREATE TABLE robot_jobs(operation TEXT PRIMARY KEY,kind TEXT,state TEXT);
            CREATE TABLE analysis_response_recoveries(operation TEXT PRIMARY KEY,request_sha256 TEXT,
                proof_sha256 TEXT,phase TEXT,updated REAL);
            CREATE TABLE robot_candidates(id TEXT PRIMARY KEY,symbol TEXT,status TEXT,plan TEXT);
            CREATE TABLE order_intents(id TEXT PRIMARY KEY,candidate_id TEXT,state TEXT);
            CREATE TABLE research_browser_requests(operation TEXT PRIMARY KEY,request_body TEXT,request_sha256 TEXT);
            CREATE TABLE research_browser_responses(operation TEXT PRIMARY KEY,received_at REAL,
                chart_evidence TEXT,chart_max_age INTEGER);''')
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?)',
            (OP,REQUEST,'COMPLETE',json.dumps(dict(symbol='ETHUSDT',side='SHORT',
                limit_entry=2248,take_profit=2188,stop_loss=2265,risk_reward=3.5294117647058822)),None))
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?)',(OP,'ANALYSIS','PENDING'))
        self.db.execute('INSERT INTO analysis_response_recoveries VALUES(?,?,?,?,?)',
            (OP,REQUEST,'b'*64,'RESUME_CACHED',RECEIVED))
        self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?)',
            (OP,'ETHUSDT','REJECTED',None))
        body=dict(output_schema=SETUP_SCHEMA_V4,context=dict(symbol='ETHUSDT'))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?)',(OP,json.dumps(body),REQUEST))
        charts=[dict(source=WEBSITE_SOURCE,symbol='ETHUSDT',timeframe=tf,
            captured_at=datetime.fromtimestamp(at,timezone.utc).isoformat())
            for tf,at in (('15m',CAPTURED),('1h',1793980366.882499))]
        self.db.execute('INSERT INTO research_browser_responses VALUES(?,?,?,?)',
            (OP,RECEIVED,json.dumps(charts),180))
        self.enabled=True;self.closed=[]
        self.coordinator=SimpleNamespace(db=self.db,
            store=SimpleNamespace(settings=lambda:dict(robot_on=self.enabled)))
        path=ROOT/'vps_native/worker/api_service.py'
        node=next(n for n in ast.parse(path.read_text()).body
            if isinstance(n,ast.ClassDef)and n.name=='Controller')
        scope=dict(__name__='worker.test_actual_reply_controller',__package__='worker',
            SHUTDOWN_DRAIN_SECONDS=600,ROBOT_POLL_SECONDS=45,time=self.clock,
            threading=threading,os=os,json=json,
            SERVICE_FAILURE_CODES={'ROBOT_COORDINATOR_UNAVAILABLE'},
            create_provider=lambda ledger:object(),Market=lambda *args:object(),
            Coordinator=lambda *args,**kwargs:self.coordinator,
            BinanceReadOnly=object(),DYNAMIC_RISK_MODEL='DYNAMIC_NET_2R_V4')
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),scope)
        self.controller=scope['Controller'].__new__(scope['Controller'])
        self.controller.stopping=threading.Event()
        self.controller.robot_wakeup=Wake(self.clock)
        self.controller.state_lock=threading.Lock();self.controller.lock=threading.Lock()
        self.controller.worker_failure=None;self.controller.account_failure=None
        self.controller.open_ledger=lambda:SimpleNamespace(db=SimpleNamespace(close=lambda:self.closed.append(True)))

    def fresh(self,now):
        return completed_decision_fresh(self.db,OP,object(),'ETHUSDT',now=now,
            source_reader=lambda *_:dict(source_type='NEUROBRO_BROWSER',provider_evidence_sha256='b'*64),
            browser_source='NEUROBRO_BROWSER')

    def ready(self):
        self.db.execute("UPDATE analysis_response_recoveries SET phase='CONSUMED'")
        self.db.execute("UPDATE robot_jobs SET state='COMPLETE'")
        self.db.execute("UPDATE robot_candidates SET status='READY_FOR_EXECUTION',plan='{}'")

    def test_actual_eth_clock_guard_stays_180_but_successful_steps_do_not_wait_45(self):
        self.assertTrue(self.fresh(RECEIVED))
        self.assertAlmostEqual(RECEIVED-CAPTURED,145.8399325,places=5)
        with self.assertRaisesRegex(Review,'STALE_MARKET_CONTEXT'):self.fresh(OLD_CONSUMED)
        ticks=[];normal_gateway_calls=[]
        def tick():
            ticks.append(self.clock.now)
            if len(ticks)==1:return status('WAITING','ANALYSIS_RECEIVED_RESUME_PENDING')
            if len(ticks)==2:
                self.assertTrue(self.fresh(self.clock.now));self.ready()
                return status('READY_FOR_EXECUTION')
            self.assertTrue(self.fresh(self.clock.now))
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0],0)
            normal_gateway_calls.append(OP)
            self.db.execute('INSERT INTO order_intents VALUES(?,?,?)',('existing-client',OP,'ENTRY_PENDING'))
            self.controller.stopping.set()
            return status('ENTRY_PENDING')
        self.coordinator.tick=tick;self.controller.loop()
        self.assertEqual(ticks,[RECEIVED]*3)
        self.assertEqual(self.controller.robot_wakeup.emitted,2)
        self.assertEqual(normal_gateway_calls,[OP]);self.assertEqual(self.closed,[True])

    def test_repeated_positive_status_is_one_wakeup_per_durable_transition(self):
        result=status('WAITING','ANALYSIS_RECEIVED_RESUME_PENDING')
        self.assertTrue(self.controller.wake_completed_research(self.coordinator,result))
        self.controller.robot_wakeup.clear()
        self.assertFalse(self.controller.wake_completed_research(self.coordinator,result))
        self.assertFalse(self.controller.robot_wakeup.is_set())
        self.ready();result=status('READY_FOR_EXECUTION')
        self.assertTrue(self.controller.wake_completed_research(self.coordinator,result))
        self.controller.robot_wakeup.clear()
        self.assertFalse(self.controller.wake_completed_research(self.coordinator,result))
        self.assertEqual(self.controller.robot_wakeup.emitted,2)

    def test_no_wakeup_for_uncommitted_unknown_failed_or_already_submitted_work(self):
        scenarios=(
            ("UPDATE api_requests SET state='NEEDS_REVIEW',output=NULL",status('WAITING','ANALYSIS_RECEIVED_RESUME_PENDING')),
            ("UPDATE api_requests SET failure_code='NEUROBRO_BROWSER_OUTCOME_UNKNOWN'",status('WAITING','ANALYSIS_RECEIVED_RESUME_PENDING')),
            ("UPDATE robot_jobs SET state='COMPLETE'",status('WAITING','ANALYSIS_RECEIVED_RESUME_PENDING')),
            ("UPDATE analysis_response_recoveries SET phase='NOEXEC'",status('WAITING','ANALYSIS_RECEIVED_RESUME_PENDING')),
            ("UPDATE analysis_response_recoveries SET request_sha256='different'",status('WAITING','ANALYSIS_RECEIVED_RESUME_PENDING')),
            ("INSERT INTO order_intents VALUES('accepted',"+repr(OP)+",'ENTRY_PENDING')",status('WAITING','ANALYSIS_RECEIVED_RESUME_PENDING')))
        for sql,result in scenarios:
            with self.subTest(sql=sql):
                self.db.execute('SAVEPOINT scenario');self.db.execute(sql)
                self.assertFalse(self.controller.wake_completed_research(self.coordinator,result))
                self.assertFalse(self.controller.robot_wakeup.is_set())
                self.db.execute('ROLLBACK TO scenario');self.db.execute('RELEASE scenario')
        self.ready();self.db.execute('INSERT INTO order_intents VALUES(?,?,?)',('accepted',OP,'ENTRY_PENDING'))
        self.assertFalse(self.controller.wake_completed_research(self.coordinator,status('READY_FOR_EXECUTION')))

    def test_off_or_stop_never_generates_an_immediate_continuation(self):
        result=status('WAITING','ANALYSIS_RECEIVED_RESUME_PENDING')
        for mode in ('owner-off','snapshot-off','stopping'):
            with self.subTest(mode=mode):
                self.enabled=mode!='owner-off';self.controller.stopping.clear()
                report={**result,'robot_on':mode!='snapshot-off'}
                if mode=='stopping':self.controller.stopping.set()
                self.assertFalse(self.controller.wake_completed_research(self.coordinator,report))
                self.assertFalse(self.controller.robot_wakeup.is_set())

    def test_normal_and_failed_ticks_keep_the_existing_45_second_fallback(self):
        for result in (status('WAITING','NEUROBRO_BUSY'),status('NEEDS_REVIEW'),
                status('EXECUTION_BLOCKED'),{**status('REJECTED'),'failure_code':'ORDER_OUTCOME_UNKNOWN'},
                {**status('READY_FOR_EXECUTION'),'failure_code':'STALE_MARKET_CONTEXT'}):
            with self.subTest(result=result):
                self.clock.now=RECEIVED;self.controller.stopping.clear();self.controller.robot_wakeup.clear()
                ticks=[]
                def tick():
                    ticks.append(self.clock.now)
                    if len(ticks)==2:self.controller.stopping.set()
                    return result
                self.coordinator.tick=tick;self.controller.loop()
                self.assertEqual(ticks,[RECEIVED,RECEIVED+45])
                self.assertFalse(self.controller.robot_wakeup.is_set())


if __name__=='__main__':unittest.main()
