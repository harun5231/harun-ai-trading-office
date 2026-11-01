"""Observation-only existing orders; no provider, exchange, thread or service."""
import ast
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from worker import existing_risk_monitor as monitor
from worker.core import Review, DYNAMIC_RISK_MODEL
from tests import test_dynamic_risk_v4 as risk_fixture


class ExistingRiskMonitorTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:',isolation_level=None);self.db.row_factory=sqlite3.Row
        self.db.executescript('''CREATE TABLE robot_status(id INTEGER PRIMARY KEY,data TEXT);
          CREATE TABLE order_intents(id TEXT PRIMARY KEY,candidate_id TEXT,symbol TEXT,state TEXT,payload TEXT,result TEXT);
          CREATE TABLE robot_candidates(id TEXT PRIMARY KEY,plan TEXT);
          CREATE TABLE robot_settings(enabled INTEGER,risk TEXT);
          INSERT INTO robot_settings VALUES(0,'99');''')
        self.addCleanup(self.db.close)
        self.plan=dict(symbol='ETHUSDT',side='LONG',entry='100',tp='103',sl='99',quantity='3',
            risk_target_usdt='3.75',leverage=75,margin_mode='CROSS',risk_model='OLD_FROZEN_PLAN')
        self.intent=dict(intent_id='owned',client_order_id='hao-old',symbol='ETHUSDT',side='LONG',
            entry=dict(price='100',quantity='3'),protection=dict(stop_loss='99',take_profit='103'),leverage=75)
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?)',
            ('owned','owned','ETHUSDT','ENTRY_PENDING',json.dumps(self.intent),'old-receipt'))
        self.db.execute('INSERT INTO robot_candidates VALUES(?,?)',('owned',json.dumps(self.plan)))
        self.status=dict(robot_on=False,bot_status='PROTECTION_INCOMPLETE',
            protection_observations={'hao-old':dict(sl_confirmed=True,tp_confirmed=False)})
        self.db.execute('INSERT INTO robot_status VALUES(1,?)',(json.dumps(self.status),))
        self.coordinator=SimpleNamespace(db=self.db,verified_intent=lambda row:json.loads(row['payload']),
            store=SimpleNamespace(verified_plan=lambda row:json.loads(row['plan'])))
        self.market=SimpleNamespace(fresh=Mock())
        self.rules=risk_fixture.DynamicOwnerRiskTests().rules()
        self.context=dict(source='Binance Futures',symbol='ETHUSDT')
        self.analysis=patch.object(monitor,'analysis_context',return_value=(self.context,self.rules))
        self.builder=self.analysis.start();self.addCleanup(self.analysis.stop)

    def row(self):return self.db.execute('SELECT * FROM order_intents').fetchone()
    def observe(self):return monitor.estimate_owned_order(self.coordinator,self.row(),self.market,'readonly-client')
    def records(self):return json.loads(self.db.execute('SELECT data FROM robot_status').fetchone()[0])

    def test_original_params_and_risk_survive_fresh_estimate_and_only_status_projection_changes(self):
        before=tuple(tuple(row)for row in self.db.execute('SELECT * FROM order_intents'))
        plan=deepcopy(self.plan);value=self.observe()
        self.assertEqual(value['status'],'AVAILABLE')
        self.assertEqual(value['order_parameters_changed'],False)
        self.assertEqual(value['original_risk_target_usdt'],'3.75')
        self.assertEqual(Decimal(value['estimated_budget_difference_usdt']),Decimal(value['estimated_total_risk'])-Decimal('3.75'))
        self.assertEqual(set(value['components']),set(monitor.COMPONENTS))
        self.builder.assert_called_once_with(self.market,'ETHUSDT',Decimal('3.75'),'readonly-client',
            policy=DYNAMIC_RISK_MODEL,db=self.db)
        self.assertEqual(tuple(tuple(row)for row in self.db.execute('SELECT * FROM order_intents')),before)
        self.assertEqual(self.plan,plan)
        self.assertEqual(self.db.execute('SELECT enabled,risk FROM robot_settings').fetchone()[:],(0,'99'))
        after=self.records()
        for key,original in self.status.items():self.assertEqual(after[key],original)
        self.assertEqual(after['risk_estimates']['hao-old'],value)
        self.assertNotIn('market',value['cost_evidence'])

    def test_technical_failure_is_separate_from_protection_and_never_a_market_decision(self):
        self.builder.side_effect=Review('SLIPPAGE_DEPTH_INSUFFICIENT')
        before=self.row()['payload'];value=self.observe()
        self.assertEqual(value['status'],'UNAVAILABLE')
        self.assertEqual(value['failure_code'],'SLIPPAGE_DEPTH_INSUFFICIENT')
        self.assertNotIn('decision',value);self.assertNotIn('estimated_total_risk',value)
        self.assertEqual(self.row()['payload'],before)
        self.assertEqual(self.records()['protection_observations'],self.status['protection_observations'])
        self.assertEqual(self.records()['bot_status'],'PROTECTION_INCOMPLETE')

    def test_concurrent_terminal_order_or_changed_payload_is_not_overwritten(self):
        for change in ("UPDATE order_intents SET state='CLOSED'","UPDATE order_intents SET payload='changed-by-actor'"):
            self.db.execute('SAVEPOINT racing_actor')
            def changed(*args,**kwargs):
                self.db.execute(change)
                return self.context,self.rules
            self.builder.side_effect=changed
            # The projection transaction cannot nest this savepoint; in a real
            # actor the DB mutation has committed before its public GET returns.
            self.db.execute('RELEASE racing_actor')
            self.assertIsNone(self.observe())
            self.assertNotIn('risk_estimates',self.records())
            self.db.execute('UPDATE order_intents SET state=?,payload=?',('ENTRY_PENDING',json.dumps(self.intent)))

    def test_history_pass_reads_owned_orders_then_retires_only_confirmed_terminal_observation(self):
        self.assertEqual(len(monitor.refresh_existing_risks(self.coordinator,self.market,'readonly-client')),1)
        self.db.execute("UPDATE order_intents SET state='CLOSED'")
        self.assertEqual(monitor.refresh_existing_risks(self.coordinator,self.market,'readonly-client'),[])
        self.assertEqual(self.records()['risk_estimates'],{})
        self.assertEqual(self.records()['protection_observations'],self.status['protection_observations'])


class ExistingAccountHookTests(unittest.TestCase):
    def test_actual_existing_account_actor_runs_estimates_after_recovery_and_snapshot_on_history_or_recovery(self):
        path=Path(__file__).resolve().parents[1]/'vps_native/worker/api_service.py'
        cls=next(node for node in ast.parse(path.read_bytes()).body if isinstance(node,ast.ClassDef)and node.name=='Controller')
        node=next(node for node in cls.body if isinstance(node,ast.FunctionDef)and node.name=='account_loop')
        calls=[];stop=threading.Event();recovery=threading.Event();count=[0]
        class Store:
            def __init__(self,db):pass
            def reconcile_carry(self,*args):pass
            def report_office(self,observation):calls.append('snapshot')
            def report_office_failure(self,reason):calls.append('account-failure')
        def collect(client,**kwargs):
            calls.append('account-get');count[0]+=1
            if count[0]==2:stop.set()
            return dict(account=dict(positions=[]),generated_at='2026-11-01T00:00:00Z')
        def estimates(*args):
            calls.append('estimate')
            if count[0]==2:raise Review('FAKE_GET_FAILURE')
        ledger=SimpleNamespace(db=SimpleNamespace(close=lambda:calls.append('closed')))
        scope=dict(__package__='worker',time=SimpleNamespace(monotonic=lambda:100),
            RobotStore=Store,Coordinator=lambda *a,**k:'existing-coordinator',OrderGateway=lambda:'existing-gateway',
            BinanceReadOnly=lambda:'readonly-client',Market=lambda:'fresh-public-market',collect=collect,
            collect_calendar=lambda *a,**k:dict(checked_at='2026-11-01T00:00:00Z'),
            HISTORY_POLL_SECONDS=60,ACCOUNT_POLL_SECONDS=15,ACCOUNT_FAILURE_CODES=set(),refresh_existing_risks=estimates)
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),scope)
        controller=SimpleNamespace(stopping=stop,state_lock=threading.Lock(),directory='/unused',
            open_ledger=lambda:ledger,progress=lambda **kwargs:None,
            clear_failure=lambda **kwargs:None,failure=lambda code,**kwargs:calls.append(code),
            protection=SimpleNamespace(account_recovery=recovery,recover_pending=lambda *args:calls.append('priority-recovery')),
            account_wakeup=SimpleNamespace(wait=lambda seconds:recovery.set(),clear=lambda:None))
        scope['account_loop'](controller)
        self.assertEqual(calls,['priority-recovery','account-get','snapshot','estimate',
            'priority-recovery','account-get','snapshot','estimate','closed'])


if __name__=='__main__':unittest.main()
