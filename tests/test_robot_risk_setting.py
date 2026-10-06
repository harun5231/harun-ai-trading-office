"""Configurable risk is captured per analysis; all providers and adapters are fixtures."""
import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_order_pipeline as pipeline_fixtures
from worker.core import D, Review, now
from worker.order_gateway import build_intent
from worker.robot_provenance import verify


class RobotRiskSettingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = pipeline_fixtures.OrderPipelineTests('test_off_claims_no_paid_call_or_account_read')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def candidate(self, symbol='BTCUSDT'):
        return self.fixture.ledger.db.execute('SELECT * FROM robot_candidates WHERE symbol=?', (symbol,)).fetchone()

    def plan(self, symbol='BTCUSDT'):
        return json.loads(self.candidate(symbol)['plan'])

    def prepare_one_slot(self, target='5'):
        f = self.fixture
        f.account.position('HYPEUSDT', '4.16')
        f.screens = [['BTCUSDT']]
        f.store.configure(dict(robot_on=True, risk_target_usdt=target))
        f.robot.tick()
        return f

    def test_default_five_and_risk_only_update_keeps_robot_off(self):
        f = self.fixture
        self.assertEqual(f.store.settings(), dict(robot_on=False, risk_target_usdt='5'))
        result = f.store.configure(dict(risk_target_usdt=' 007.50 '))
        self.assertEqual(result['risk_target_usdt'], '7.50')
        self.assertFalse(result['robot_on'])
        self.assertEqual(f.calls, [])
        self.assertEqual(f.account.calls, [])
        self.assertEqual(f.ledger.db.execute('SELECT risk FROM robot_settings').fetchone()[0], '7.50')
        self.assertEqual(f.ledger.db.execute('SELECT state FROM office_activity').fetchone()[0], 'RISK_UPDATED')

    def test_combined_settings_keep_on_off_and_risk_journal_evidence(self):
        f = self.fixture
        result = f.store.configure(dict(robot_on=True, risk_target_usdt='10'))
        self.assertTrue(result['robot_on'])
        self.assertEqual(result['risk_target_usdt'], '10')
        self.assertEqual([row[0] for row in f.ledger.db.execute('SELECT state FROM office_activity ORDER BY seq')],
                         ['ON', 'RISK_UPDATED'])
        result = f.store.configure(dict(robot_on=False))
        self.assertFalse(result['robot_on'])
        self.assertEqual(result['risk_target_usdt'], '10')
        self.assertEqual(f.ledger.db.execute('SELECT state FROM office_activity ORDER BY seq DESC LIMIT 1').fetchone()[0], 'OFF')

    def test_invalid_risk_cannot_partially_turn_robot_on_or_write_activity(self):
        f = self.fixture
        for target in ('0', '-1', 'NaN', 'Infinity', '100.001', '1e1', '', 10, True, None):
            with self.subTest(target=target), self.assertRaises(Review):
                f.store.configure(dict(robot_on=True, risk_target_usdt=target))
            self.assertEqual(f.store.settings(), dict(robot_on=False, risk_target_usdt='5'))
            self.assertEqual(f.ledger.db.execute('SELECT COUNT(*) FROM office_activity').fetchone()[0], 0)
        self.assertEqual(f.calls, [])

    def test_invalid_setting_shape_does_not_change_valid_risk(self):
        f = self.fixture
        f.store.configure(dict(risk_target_usdt='10'))
        for value in ({}, [], None, {'robot_on': 1}, {'robot_on': 'true'},
                      {'robot_on': True, 'risk_target_usdt': '20', 'unknown': True}):
            with self.subTest(value=value), self.assertRaises(Review): f.store.configure(value)
            self.assertEqual(f.store.settings(), dict(robot_on=False, risk_target_usdt='10'))

    def test_database_failure_rolls_back_both_settings_and_activity(self):
        f = self.fixture
        f.ledger.db.execute("CREATE TRIGGER reject_risk BEFORE UPDATE OF risk ON robot_settings BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            f.store.configure(dict(robot_on=True, risk_target_usdt='10'))
        self.assertEqual(f.store.settings(), dict(robot_on=False, risk_target_usdt='5'))
        self.assertEqual(f.ledger.db.execute('SELECT COUNT(*) FROM office_activity').fetchone()[0], 0)
        self.assertFalse(f.ledger.db.in_transaction)

    def test_restart_preserves_risk_and_enabled_setting_without_paid_calls(self):
        f = self.fixture
        f.store.configure(dict(robot_on=True, risk_target_usdt='12.25'))
        f.restart()
        self.assertEqual(f.store.settings(), dict(robot_on=True, risk_target_usdt='12.25'))
        self.assertEqual(f.store.snapshot()['risk_target_usdt'], '12.25')
        self.assertEqual(f.calls, [])

    def test_risk_changed_after_screening_is_captured_before_analysis_claim(self):
        f = self.prepare_one_slot()
        f.store.configure(dict(risk_target_usdt='10'))
        self.assertEqual(f.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        plan = self.plan()
        self.assertEqual(plan['risk_target_usdt'], '10')
        self.assertEqual(D(plan['risk']), D('10'))
        self.assertEqual(D(plan['execution_quantity']), D('5'))
        self.assertEqual((plan['entry'], plan['tp'], plan['sl']), ('100', '104', '98'))
        self.assertEqual(plan['neurobro_position_size'], '2.5')
        self.assertEqual(f.ledger.db.execute("SELECT risk_target FROM robot_jobs WHERE kind='ANALYSIS'").fetchone()[0], '10')
        context = json.loads(f.calls[1]['message_history'][0]['content'])
        self.assertEqual(context['risk_constraints']['target_loss_at_sl_usdt'], '10')
        self.assertTrue(verify(f.ledger.db, plan, self.candidate()['id']))

    def test_risk_change_during_paid_request_keeps_claim_and_intent_immutable(self):
        f = self.prepare_one_slot()
        original = f.client.transport
        def transport(*args, **kwargs):
            self.assertEqual(f.ledger.db.execute("SELECT risk_target FROM robot_jobs WHERE kind='ANALYSIS'").fetchone()[0], '5')
            response = original(*args, **kwargs)
            f.store.configure(dict(risk_target_usdt='10'))
            return response
        f.client.transport = transport
        self.assertEqual(f.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        before = self.candidate()['plan']
        self.assertEqual(self.plan()['risk_target_usdt'], '5')
        self.assertEqual(D(self.plan()['risk']), D('5'))
        f.robot.tick()
        intent = dict(f.intent())
        self.assertEqual(json.loads(intent['payload'])['risk_target_usdt'], '5')
        self.assertEqual(f.store.settings()['risk_target_usdt'], '10')
        f.restart()
        f.ticks(3)
        self.assertEqual(self.candidate()['plan'], before)
        self.assertEqual(f.intent()['payload'], intent['payload'])
        self.assertEqual(len(f.calls), 2)

    def test_risk_change_affects_next_analysis_without_repricing_first_candidate(self):
        f = self.fixture
        f.on()
        f.robot.tick()
        f.robot.tick()
        before = self.candidate()['plan']
        f.store.configure(dict(risk_target_usdt='10'))
        gateway = f.connected()
        captured = {}
        def submit(intent):
            captured['first_fill_at'] = now()
            f.account.position('BTCUSDT', '2.5')
            return gateway.observation(intent, 'POSITION_PROTECTED', '2.5', **captured)
        gateway.on_submit = submit
        gateway.on_reconcile = lambda intent: gateway.observation(intent, 'POSITION_PROTECTED', '2.5', **captured)
        self.assertEqual(f.robot.tick()['bot_status'], 'POSITION_PROTECTED')
        self.assertEqual(f.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(self.candidate()['plan'], before)
        self.assertEqual(self.plan()['risk_target_usdt'], '5')
        self.assertEqual(self.plan('ETHUSDT')['risk_target_usdt'], '10')
        self.assertEqual(D(self.plan('ETHUSDT')['risk']), D('10'))
        self.assertEqual(gateway.submissions[0]['risk_target_usdt'], '5')
        self.assertEqual(len(f.calls), 3)

    def test_target_ten_reaches_only_injected_adapter_with_verified_contract(self):
        f = self.prepare_one_slot('10')
        f.robot.tick()
        plan = self.plan()
        gateway = f.connected()
        with patch('worker.http_client.request', side_effect=AssertionError('Unexpected network')) as request:
            self.assertEqual(f.robot.tick()['bot_status'], 'ENTRY_PENDING')
            request.assert_not_called()
        intent = gateway.submissions[0]
        self.assertEqual(intent['risk_target_usdt'], '10')
        self.assertEqual(D(intent['risk_usdt']), D('10'))
        self.assertEqual(D(intent['entry']['quantity']), D('5'))
        self.assertEqual(intent['evidence_sha256'], plan['provenance']['payload_sha256'])
        self.assertEqual(f.receipt_count(), 0)

    def test_lower_current_setting_does_not_rewrite_captured_ready_intent(self):
        f = self.prepare_one_slot('10')
        f.robot.tick()
        f.robot.tick()
        before = dict(f.intent())
        f.store.configure(dict(risk_target_usdt='2'))
        gateway = f.connected()
        self.assertEqual(f.robot.tick()['bot_status'], 'ENTRY_PENDING')
        self.assertEqual(f.store.settings()['risk_target_usdt'], '2')
        self.assertEqual(gateway.submissions[0], json.loads(before['payload']))
        self.assertEqual(f.intent()['payload'], before['payload'])
        self.assertEqual(gateway.submissions[0]['risk_target_usdt'], '10')

    def test_gateway_rejects_risk_above_captured_target_and_unbounded_target(self):
        f = self.prepare_one_slot('10')
        f.robot.tick()
        plan = self.plan()
        for changed in (dict(risk_target_usdt='9'), dict(risk_target_usdt='100.001'),
                        dict(risk_target_usdt='0'), dict(risk_target_usdt='NaN'), dict(risk='11')):
            with self.subTest(changed=changed), self.assertRaises(Review):
                build_intent({**copy.deepcopy(plan), **changed}, self.candidate()['id'])
        self.assertEqual(self.plan(), plan)
        self.assertEqual(f.receipt_count(), 0)


if __name__ == '__main__': unittest.main()
