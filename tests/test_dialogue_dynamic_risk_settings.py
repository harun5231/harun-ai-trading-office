"""Owner risk at new-plan/claim boundaries; real native methods, fake IO only.

The existing provider receipt/material validator has separate source-bound
regressions. These tests isolate the budget integration and never contact a
model, browser, or exchange. Formula/gateway tests remain unchanged.
"""
import ast
import copy
from fractions import Fraction
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

import test_order_pipeline as pipeline
from worker import robot as mirror
from worker.account_state import account_state
from worker.core import D, DYNAMIC_RISK_MODEL, day
from worker.new_setup_policy import retire_stale_unsubmitted, stale_unsubmitted
from worker.order_gateway import build_intent
from worker.robot_provenance import verify


ROOT = Path(__file__).resolve().parents[1]


def native_coordinator():
    path = ROOT/'vps_native/worker/robot.py'
    node = next(item for item in ast.parse(path.read_text()).body
                if isinstance(item, ast.ClassDef) and item.name == 'Coordinator')
    namespace = dict(vars(mirror), __name__='worker._native_budget_fixture',
        __package__='worker', stale_unsubmitted=stale_unsubmitted,
        retire_stale_unsubmitted=retire_stale_unsubmitted)
    # The provider fixture supplies a completed structured response. This one
    # guard is outside budget sizing and is independently tested with the real
    # native source/attachment receipts; it cannot perform any test IO here.
    namespace['completed_decision_fresh'] = lambda *args, **kwargs: True
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['Coordinator']


class DialogueDynamicRiskSettingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.NativeCoordinator = native_coordinator()

    def setUp(self):
        self.fixture = pipeline.OrderPipelineTests('test_off_claims_no_paid_call_or_account_read')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        self.network.start();self.addCleanup(self.network.stop)

    def prepare(self, target='2.75', *, dynamic=True):
        f = self.fixture
        original = f.account.signed_get
        def get(path, symbol=None, **options):
            if path == '/fapi/v1/leverageBracket':
                return [dict(symbol=symbol, brackets=[dict(notionalFloor='0',
                    notionalCap='10000000', initialLeverage=75)])]
            value = original(path, symbol=symbol, **options)
            if path == '/fapi/v3/account':value['availableBalance'] = '1000'
            return value
        f.account.signed_get = get
        f.account.taker_fee = '0.0005'
        f.market.lookback = 201
        f.market.depth = lambda symbol:dict(source='BINANCE_FUTURES_PUBLIC_DEPTH',
            symbol=symbol, observed_at=mirror.time.time(), last_update_id=1,
            bids=[['99.99','1000']], asks=[['100.01','1000']])
        f.account.position('HYPEUSDT', '4.16')
        f.screens = [['ETHUSDT']]
        f.store.configure(dict(robot_on=True, risk_target_usdt=target))
        self.assertEqual(f.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        data_row = f.ledger.db.execute('SELECT * FROM robot_cycles ORDER BY rowid DESC LIMIT 1').fetchone()
        self.cycle = data_row['id'];self.data = json.loads(data_row['data'])
        self.robot = self.NativeCoordinator(f.ledger, f.client, f.market, lambda:f.account,
            new_setup_policy=DYNAMIC_RISK_MODEL if dynamic else None)
        self.store = self.robot.store
        def dialogue(method, url, headers, body, timeout):
            self.assertEqual((method, url), ('POST', 'https://api.neurobro.ai/api/v1/agent/ask'))
            f.calls.append(copy.deepcopy(body))
            packet = json.loads(body['message_history'][0]['content'])
            context = packet['data'] if 'context_partition' in packet else packet
            response = pipeline.confirmed_output({**pipeline.GOOD, 'symbol':context['symbol']}, context)
            return 200, {}, dict(mode='smart', answer=None, output=response)
        f.client.transport = dialogue
        return f

    def analyze(self):
        return self.robot.analyze(self.cycle, self.data, 'ETHUSDT',
            account_state(self.fixture.account, self.store, day()), day())

    def candidate(self):
        return self.fixture.ledger.db.execute('SELECT * FROM robot_candidates WHERE symbol=?', ('ETHUSDT',)).fetchone()

    def plan(self):return json.loads(self.candidate()['plan'])

    def assert_current_plan(self, target):
        value = self.plan();q = Fraction(D(value['quantity']))
        e, sl = Fraction(D(value['entry'])), Fraction(D(value['sl']))
        fee = Fraction(D(value['entry_fee_rate']))
        loss = q*(abs(e-sl)+fee*(e+sl))
        self.assertEqual(D(value['risk_target_usdt']), D(target))
        self.assertEqual(Fraction(D(value['risk'])), loss)
        self.assertLessEqual(loss, Fraction(D(target)))
        step = Fraction(D(value['sizing_rules']['step']))
        self.assertGreater((q+step)*(abs(e-sl)+fee*(e+sl)), Fraction(D(target)))
        self.assertEqual(Fraction(D(value['target_price_pnl_usdt'])), 2*loss)
        self.assertEqual((D(value['entry']), D(value['sl'])), (D('100'), D('98')))
        self.assertEqual(value['margin_mode'], 'CROSS')
        self.assertLessEqual(value['leverage'],75)
        self.assertEqual(value['policy_version'], 'FEE_ONLY_SL_RISK_PRICE_PNL_2R_V1')
        self.assertEqual(D(value['exit_slippage_rate']),D('0'))
        self.assertTrue(verify(self.fixture.ledger.db, value, self.candidate()['id']))
        return value

    def account(self):return account_state(self.fixture.account, self.store, day())

    def submit(self):return self.robot.advance_execution(self.account(), day())

    def test_native_new_plan_uses_latest_saved_budget_after_dialogue(self):
        f = self.prepare('2.75');original = f.client.transport
        def respond(*args, **kwargs):
            response = original(*args, **kwargs)
            self.store.configure(dict(risk_target_usdt='7.125'))
            return response
        f.client.transport = respond
        self.assertEqual(self.analyze()['bot_status'],'READY_FOR_EXECUTION')
        self.assert_current_plan('7.125')
        self.assertEqual(f.ledger.db.execute("SELECT risk_target FROM robot_jobs WHERE kind='ANALYSIS'").fetchone()[0],'2.75')
        request_context=json.loads(f.calls[1]['message_history'][0]['content'])['data']
        self.assertEqual(request_context['risk_constraints']['maximum_loss_at_sl_usdt'],'2.75')
        self.assertEqual(len(f.calls),2)

    def test_budget_changed_during_post_dialogue_fee_reads_sizes_new_plan_without_replaying_dialogue(self):
        f=self.prepare('2.75');original=f.account.signed_get;changed=[]
        def get(path,*args,**kwargs):
            result=original(path,*args,**kwargs)
            if path=='/fapi/v1/leverageBracket' and len(f.calls)==2 and not changed:
                changed.append(True);self.store.configure(dict(risk_target_usdt='7.125'))
            return result
        f.account.signed_get=get
        self.assertEqual(self.analyze()['bot_status'],'READY_FOR_EXECUTION')
        self.assert_current_plan('7.125')
        self.assertEqual(f.ledger.db.execute("SELECT risk_target FROM robot_jobs WHERE kind='ANALYSIS'").fetchone()[0],'2.75')
        self.assertEqual(len(f.calls),2)

    def test_ready_setup_resizes_at_new_claim_without_another_provider_request(self):
        f = self.prepare('2.75');self.analyze();old = self.plan()
        self.store.configure(dict(risk_target_usdt='17.3'));f.gateway=f.connected();self.robot.gateway=f.gateway
        self.assertEqual(self.submit()['bot_status'],'ENTRY_PENDING')
        fresh=self.assert_current_plan('17.3')
        self.assertGreater(D(fresh['quantity']),D(old['quantity']))
        self.assertEqual(len(f.calls),2);self.assertEqual(len(f.gateway.submissions),1)
        submitted=f.gateway.submissions[0]
        self.assertEqual(D(submitted['risk_target_usdt']),D('17.3'))
        self.assertEqual((submitted['entry']['price'],submitted['protection']['stop_loss']),
            (build_intent(old,self.candidate()['id'])['entry']['price'],build_intent(old,self.candidate()['id'])['protection']['stop_loss']))
        accepted=f.ledger.db.execute('SELECT payload FROM order_intents').fetchone()[0]
        plan_bytes=self.candidate()['plan'];self.store.configure(dict(risk_target_usdt='1.75'))
        f.restart()
        self.robot=self.NativeCoordinator(f.ledger,f.client,f.market,lambda:f.account,
            gateway=f.gateway,new_setup_policy=DYNAMIC_RISK_MODEL)
        self.store=self.robot.store
        self.assertEqual(self.store.settings()['risk_target_usdt'],'1.75')
        self.assertIsNone(self.submit())
        self.assertEqual(f.ledger.db.execute('SELECT payload FROM order_intents').fetchone()[0],accepted)
        self.assertEqual(self.candidate()['plan'],plan_bytes)
        self.assertEqual(len(f.gateway.submissions),1)

    def test_budget_changed_during_final_account_read_waits_then_reuses_same_operation(self):
        f=self.prepare('2.75');self.analyze();gateway=f.connected();self.robot.gateway=gateway
        original=f.account.signed_get;changed=[]
        def get(path,*args,**kwargs):
            result=original(path,*args,**kwargs)
            if path=='/fapi/v3/positionRisk'and not changed:
                changed.append(True);self.store.configure(dict(risk_target_usdt='7.125'))
            return result
        f.account.signed_get=get
        # Prepare the account before installing the read-boundary mutation.
        with patch.object(self.robot,'execution_slots',return_value=1):
            result=self.robot.advance_execution(dict(running_symbols=['HYPEUSDT'],running_positions=1,
                open_entry_symbols=[],available_slots=1),day())
        self.assertEqual(result['bot_status'],'WAITING')
        self.assertEqual(result['wait_reason'],'RISK_SETTING_CHANGED')
        self.assertEqual(gateway.submissions,[])
        identity=self.candidate()['id']
        self.assertEqual(self.submit()['bot_status'],'ENTRY_PENDING')
        self.assertEqual(self.candidate()['id'],identity);self.assert_current_plan('7.125')
        self.assertEqual(len(f.calls),2);self.assertEqual(len(gateway.submissions),1)

    def test_budget_changed_during_rule_reads_uses_latest_target_with_same_fresh_rules(self):
        f=self.prepare('2.75');self.assertEqual(self.analyze()['bot_status'],'READY_FOR_EXECUTION')
        original=f.account.signed_get;changed=[]
        def get(path,*args,**kwargs):
            result=original(path,*args,**kwargs)
            if path=='/fapi/v1/leverageBracket' and not changed:
                changed.append(True);self.store.configure(dict(risk_target_usdt='7.125'))
            return result
        f.account.signed_get=get;gateway=f.connected();self.robot.gateway=gateway
        self.assertEqual(self.submit()['bot_status'],'ENTRY_PENDING')
        self.assert_current_plan('7.125')
        self.assertEqual(f.ledger.db.execute("SELECT risk_target FROM robot_jobs WHERE kind='ANALYSIS'").fetchone()[0],'2.75')
        self.assertEqual(len(f.calls),2);self.assertEqual(len(gateway.submissions),1)

    def test_legacy_unsubmitted_budget_change_rejects_instead_of_waiting_or_submitting(self):
        f=self.prepare('2.75',dynamic=False);self.analyze();before=self.candidate()['plan']
        self.store.configure(dict(risk_target_usdt='7.125'));gateway=f.connected();self.robot.gateway=gateway
        result=self.submit()
        self.assertEqual((result['bot_status'],result['failure_code']),('REJECTED','ORDER_COSTS_CHANGED'))
        self.assertEqual(gateway.submissions,[]);self.assertEqual(self.candidate()['plan'],before)
        self.assertEqual(self.candidate()['status'],'REJECTED')
        self.assertEqual(len(f.calls),2)

