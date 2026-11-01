"""Actual coordinator/gateway boundaries with isolated SQLite and fake exchange."""
import ast
import copy
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import tempfile
import sys
import time
import unittest
from unittest.mock import patch

from worker import robot
from worker.core import Ledger,day,now
from worker.research_guard import gateway_mutations,gateway_mutations_allowed
from tests import test_native_gateway_owner_revision as native


ROOT=Path(__file__).resolve().parents[1]


def native_coordinator():
    # Use the actual successor class unchanged. Ordinary imports supply only
    # shared pure contracts; no API service, account or browser is started.
    path=ROOT/'vps_native/worker/robot.py'
    cls=next(node for node in ast.parse(path.read_text()).body
        if isinstance(node,ast.ClassDef)and node.name=='Coordinator')
    namespace=dict(vars(robot))
    exec(compile(ast.Module(body=[cls],type_ignores=[]),str(path),'exec'),namespace)
    return namespace['Coordinator']


class CoordinatorProtectionRevisionTests(unittest.TestCase):
    def fixture(self,coordinator_class,*,failure='tp'):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        ledger=Ledger(Path(temporary.name)/'ledger.sqlite3');self.addCleanup(ledger.db.close)
        plan=native.legacy_plan();cycle=day()+':robot-v9:0'
        operation=cycle+':analysis-v9:'+plan['symbol']
        intent=native.gateway.build_intent(plan,operation)
        first_fill_ms=int(time.time()*1000)-1000
        class FrozenExchange(native.FakeExchange):
            def _trades(self,symbol,oid,qty,side,since):
                return[{'id':'1','orderId':int(oid),'time':first_fill_ms}]
        exchange=FrozenExchange(intent,fail=failure)
        coordinator=coordinator_class(ledger,None,None,None,gateway=exchange)
        coordinator.store.configure({'robot_on':True})
        # Fixture source evidence is fixed here; verified_intent still rebuilds
        # and compares the exact full plan/intent before any gateway operation.
        coordinator.store.verified_plan=lambda candidate:copy.deepcopy(plan)
        ledger.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
            (operation,cycle,plan['symbol'],'NEEDS_REVIEW',json.dumps(plan),'ORDER_OUTCOME_UNKNOWN'))
        created=(datetime.now(timezone.utc)-timedelta(seconds=20)).isoformat()
        ledger.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
            (operation,operation,plan['symbol'],'NEEDS_REVIEW',json.dumps(intent),None,
                'ORDER_OUTCOME_UNKNOWN',created,created))
        coordinator.store.report('WAITING')
        value=json.loads(ledger.db.execute('SELECT data FROM robot_status').fetchone()[0])
        cached={'usdt_wallet_balance':'123.45','account_checked_at':now(),
            'running_positions':1,'running_symbols':['OTHERUSDT']}
        value.update(cached)
        ledger.db.execute('UPDATE robot_status SET data=?',(json.dumps(value),))
        return coordinator,exchange,intent,cached

    def test_uncertain_production_reconcile_restores_exits_under_false_scope_and_keeps_cache(self):
        for coordinator_class,failure in [(cls,leg)for cls in (robot.Coordinator,native_coordinator())for leg in ('sl','tp')]:
            with self.subTest(coordinator=coordinator_class.__module__,failure=failure):
                coordinator,exchange,intent,cached=self.fixture(coordinator_class,failure=failure)
                original=copy.deepcopy(intent)
                with gateway_mutations(lambda:False):
                    result=coordinator.advance_execution(None,day())
                    self.assertFalse(gateway_mutations_allowed())
                self.assertEqual(result['bot_status'],'PROTECTION_INCOMPLETE')
                row=coordinator.db.execute('SELECT * FROM order_intents WHERE id=?',(intent['intent_id'],)).fetchone()
                observed=json.loads(row['result'])
                self.assertEqual(row['state'],'PROTECTION_INCOMPLETE')
                self.assertEqual(observed['sl_confirmed'],failure!='sl')
                self.assertEqual(observed['tp_confirmed'],failure!='tp')
                self.assertEqual(observed[failure+'_failure_code'],'BINANCE_PROTECTION_C1111')
                self.assertEqual(observed[failure+'_exchange_code'],-1111)
                self.assertEqual(json.loads(row['payload']),original)
                actual=json.loads(coordinator.db.execute('SELECT data FROM robot_status').fetchone()[0])
                for key,value in cached.items():self.assertEqual(actual[key],value)
                self.assertEqual(actual['protection_observations'][intent['client_order_id']]['state'],
                    'PROTECTION_INCOMPLETE')
                exchange.fail=None;exchange.calls=[]
                with gateway_mutations(lambda:False):
                    coordinator.advance_execution(None,day())
                row=coordinator.db.execute('SELECT * FROM order_intents WHERE id=?',(intent['intent_id'],)).fetchone()
                final=json.loads(row['result'])
                self.assertEqual(row['state'],'POSITION_PROTECTED')
                self.assertTrue(final['sl_confirmed']and final['tp_confirmed'])
                self.assertEqual([params['type']for method,path,params in exchange.calls if method=='POST'],
                    ['STOP_MARKET'if failure=='sl'else'TAKE_PROFIT_MARKET'])
                self.assertFalse(any(method=='POST'and path=='/fapi/v1/order'
                    for method,path,params in exchange.calls))

    def test_known_unsent_gateway_expiry_rejects_without_unknown_submission_state(self):
        from worker.research_guard import EntryNotSubmitted
        from tests import support
        with patch.dict(sys.modules,{'support':support}):
            from tests import test_order_pipeline as pipeline
        for coordinator_class in (robot.Coordinator,native_coordinator()):
            fixture=pipeline.OrderPipelineTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
            fixture.one_slot_ready();fixture.robot.__class__=coordinator_class
            exchange=fixture.connected()
            def rejected(intent):raise EntryNotSubmitted('ORDER_UNSENT_STALE_EVIDENCE')
            exchange.on_submit=rejected
            from worker.account_state import account_state
            result=fixture.robot.advance_execution(account_state(fixture.account,fixture.store,day()),day())
            self.assertEqual(result['bot_status'],'REJECTED')
            self.assertEqual(result['failure_code'],'ORDER_UNSENT_STALE_EVIDENCE')
            self.assertEqual(fixture.intent()['state'],'REJECTED')
            self.assertEqual(len(exchange.submissions),1)
            self.assertTrue(fixture.store.settings()['robot_on'])

    def test_fallback_legacy_reconcile_retains_false_mutation_scope(self):
        for coordinator_class in (robot.Coordinator,native_coordinator()):
            coordinator=coordinator_class.__new__(coordinator_class)
            calls=[]
            class Legacy:
                def reconcile(self,intent):
                    calls.append((intent,gateway_mutations_allowed()))
                    return{'state':'ENTRY_PENDING'}
            coordinator.gateway=Legacy()
            with gateway_mutations(lambda:False):
                self.assertEqual(coordinator.reconcile_owned({'frozen':True}),{'state':'ENTRY_PENDING'})
            self.assertEqual(calls,[({'frozen':True},False)])

    @unittest.skipUnless(os.getenv('OFFICE_OWNER_FROZEN_FIXTURE'),
        'private captured order fixture is supplied only for offline release compatibility')
    def test_captured_existing_orders_rebuild_identically_and_keep_validation(self):
        fixture=json.loads(Path(os.environ['OFFICE_OWNER_FROZEN_FIXTURE']).read_text())
        plans={row['id']:json.loads(row['plan'])for row in fixture['candidates']}
        self.assertEqual(len(fixture['intents']),2)
        for row in fixture['intents']:
            frozen=json.loads(row['payload'])
            rebuilt=native.gateway.build_intent(plans[row['candidate_id']],row['id'])
            with self.subTest(symbol=row['symbol']):
                self.assertEqual(rebuilt,frozen)
                native.gateway.OrderGateway.__new__(native.gateway.OrderGateway)._validate_intent(frozen)

    def test_placeholder_remains_and_internal_builder_matches_actual_successor(self):
        current=ast.parse((ROOT/'worker/order_gateway.py').read_text())
        actual=ast.parse((ROOT/'vps_native/worker/order_gateway.py').read_text())
        get=lambda tree:next(node for node in tree.body if isinstance(node,ast.FunctionDef)and node.name=='build_intent')
        self.assertEqual(ast.dump(get(current),include_attributes=False),ast.dump(get(actual),include_attributes=False))
        cls=next(node for node in current.body if isinstance(node,ast.ClassDef)and node.name=='OrderGateway')
        self.assertFalse(any(isinstance(node,ast.FunctionDef)and node.name=='_request'for node in cls.body))


if __name__=='__main__':unittest.main()
