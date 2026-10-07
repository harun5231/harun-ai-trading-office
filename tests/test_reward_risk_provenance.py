"""Exact net 1:2 policy provenance, with immutable pre-policy ETH commands."""
import ast
import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from support import Account, FakeMarket, RULES
from worker.analysis import analysis_context
from worker.core import (D, Ledger, Review, Signal, REWARD_RISK_POLICY,
    NORMALIZED_REWARD_RISK_POLICY, RISK_MODEL, day, risk_check, target_reward_risk_tp)
from worker.neuroapi import NeuroAPI, SETUP_SCHEMA, setup
from worker.order_gateway import build_intent
from worker.prompts import ANALYSIS
from worker.robot_provenance import stamp, verify


class RewardRiskProvenanceTests(unittest.TestCase):
    def setUp(self):
        network = patch('socket.socket', side_effect=AssertionError('Live exchange forbidden'))
        network.start()
        self.addCleanup(network.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.ledger = Ledger(Path(folder.name) / 'ledger.sqlite3')
        self.addCleanup(self.ledger.db.close)

    def plan(self, *, side='LONG', legacy=False):
        symbol = 'ETHUSDT'
        rules = replace(RULES(), tick=D('.1'), taker_fee_rate=D('.0005'), fee_symbol=symbol)
        entry, sl = D('2610'), D('2585') if side == 'LONG' else D('2635')
        tp = D('2730') if legacy else target_reward_risk_tp(entry, sl, side, rules, symbol=symbol)
        source = dict(symbol=symbol, side=side, limit_entry=entry, stop_loss=sl,
                      take_profit=tp, risk_reward=abs(tp-entry)/abs(entry-sl))
        operation = day() + ':robot-v9:0:analysis-v9:' + symbol
        client = NeuroAPI(self.ledger, key='offline-reward-risk-proof',
            transport=lambda *args, **kwargs: (200, {}, dict(mode='smart', answer=None, output=copy.deepcopy(source))))
        client.ask(operation, ANALYSIS, SETUP_SCHEMA, lambda value: setup(value, symbol))
        plan = risk_check(Signal(symbol, side, entry, tp, sl), rules,
                          reward_risk_policy=None if legacy else REWARD_RISK_POLICY)
        plan['sizing_rules'] = {key: str(value) if isinstance(value, D) else value
                                for key, value in asdict(rules).items()}
        if not legacy:
            plan['protection_working_type'] = 'CONTRACT_PRICE'
        stamp(self.ledger.db, plan, operation)
        return plan, rules, operation

    def test_long_and_short_first_legal_tp_is_stamped_verified_and_exported(self):
        for side in ('LONG', 'SHORT'):
            with self.subTest(side=side):
                self.ledger.db.execute('DROP TABLE IF EXISTS api_requests')
                plan, rules, operation = self.plan(side=side)
                before = copy.deepcopy(plan)
                journal = [dict(row) for row in self.ledger.db.execute('SELECT * FROM api_requests')]
                self.assertTrue(verify(self.ledger.db, plan, operation))
                command = build_intent(plan, operation)
                self.assertEqual(command['reward_risk_policy'], REWARD_RISK_POLICY)
                self.assertEqual(command['tp_tick_size'], '0.1')
                self.assertEqual(command['protection']['take_profit'], plan['tp'])
                self.assertEqual(command['protection']['working_type'], 'CONTRACT_PRICE')
                self.assertEqual(plan, before)
                self.assertEqual([dict(row) for row in self.ledger.db.execute('SELECT * FROM api_requests')], journal)
                self.assertGreaterEqual(D(plan['net_rr']), D('2'))
                self.assertLess(D(plan['net_rr']), D('2.004'))

    def test_legacy_eth_high_rr_remains_verified_and_byte_identical(self):
        plan, rules, operation = self.plan(legacy=True)
        proof = plan['provenance']['payload_sha256']
        expected = dict(intent_id=operation,
            client_order_id='hao-' + hashlib.sha256(operation.encode()).hexdigest()[:28],
            symbol='ETHUSDT', position_side='BOTH', side='LONG',
            entry=dict(order_type='LIMIT', side='BUY', price='2610', quantity='0.181', time_in_force='GTC'),
            protection=dict(exit_side='SELL', stop_loss='2585', take_profit='2730', working_type='MARK_PRICE'),
            margin_mode='CROSS', leverage=75, risk_target_usdt='5', risk_usdt='4.9951475',
            gross_risk_usdt='4.525', entry_fee_usdt='0.236205', sl_exit_fee_usdt='0.2339425',
            tp_exit_fee_usdt='0.247065', net_reward_usdt='21.23673',
            net_reward_risk='4.251472053628046018661110607844913488540628680134070115046652776519612283721351571700335175287616631941299030709303378929250837938219041579853247576773258447323',
            fee_evidence=dict(source='BINANCE_FUTURES_COMMISSION_RATE', symbol='ETHUSDT',
                              observed_at=plan['fee_observed_at'], taker_rate='0.0005'),
            excluded_costs=['SLIPPAGE', 'FUNDING', 'GAPS'], evidence_sha256=proof)
        before = json.dumps(plan).encode()
        self.assertTrue(verify(self.ledger.db, plan, operation))
        self.assertEqual(json.dumps(build_intent(plan, operation)).encode(), json.dumps(expected).encode())
        self.assertEqual(json.dumps(plan).encode(), before)
        self.assertNotIn('reward_risk_policy', plan)
        self.assertNotIn('reward_risk_policy', expected)
        self.assertNotIn('tp_tick_size', expected)

    def test_removing_or_changing_policy_breaks_immutable_proof(self):
        plan, _, operation = self.plan()
        removed = copy.deepcopy(plan)
        removed.pop('reward_risk_policy')
        for changed in (removed, dict(plan, reward_risk_policy='GROSS_1_TO_2'),
                        dict(plan, reward_risk_policy=None)):
            with self.subTest(policy=changed.get('reward_risk_policy')):
                with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
                    verify(self.ledger.db, changed, operation)

    def test_flag_cannot_promote_old_eth_or_reprice_provider_level(self):
        plan, _, operation = self.plan(legacy=True)
        upgraded = copy.deepcopy(plan)
        upgraded['reward_risk_policy'] = REWARD_RISK_POLICY
        before = copy.deepcopy(upgraded)
        with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
            stamp(self.ledger.db, upgraded, operation)
        self.assertEqual(upgraded, before)
        with self.assertRaisesRegex(Review, '^INVALID_ORDER_CONTRACT$'):
            build_intent(upgraded, operation)

    def test_restatement_with_changed_tick_fee_or_extra_tp_is_rejected(self):
        plan, _, operation = self.plan()
        changes = []
        changed = copy.deepcopy(plan)
        changed['tp'] = str(D(changed['tp']) + D('.1'))
        changes.append(changed)
        for field, value in (('tick', '1'), ('taker_fee_rate', '0.001')):
            changed = copy.deepcopy(plan)
            changed['sizing_rules'][field] = value
            changes.append(changed)
        for changed in changes:
            with self.subTest(changed=changed['sizing_rules']):
                with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
                    stamp(self.ledger.db, changed, operation)
                with self.assertRaisesRegex(Review, '^INVALID_ORDER_CONTRACT$'):
                    build_intent(changed, operation)

    def test_new_builder_validates_rules_and_policy_before_export(self):
        plan, _, operation = self.plan()
        invalid = [dict(plan, reward_risk_policy=value) for value in (True, None, '', 'GROSS_1_TO_2')]
        for sizing in (None, {}, dict(plan['sizing_rules'], tick='0'),
                       dict(plan['sizing_rules'], tick_size='0.1'),
                       dict(plan['sizing_rules'], tick='invalid'),
                       dict(plan['sizing_rules'], taker_fee_rate='invalid')):
            invalid.append(dict(plan, sizing_rules=sizing))
        for changed in invalid:
            with self.subTest(changed=changed.get('reward_risk_policy')):
                with self.assertRaisesRegex(Review, '^INVALID_ORDER_CONTRACT$'):
                    build_intent(changed, operation)

    def test_private_vps_builder_resolves_helpers_without_new_module_imports(self):
        import worker.order_gateway as module
        source = Path(module.__file__).read_text()
        tree = ast.parse(source)
        builder = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'build_intent')
        isolated = ast.Module(body=[builder], type_ignores=[])
        namespace = dict(__name__='worker.private_vps_builder', __package__='worker',
                         hashlib=hashlib, D=D, Review=Review)
        from worker.core import number, validated_risk_target
        namespace.update(number=number, validated_risk_target=validated_risk_target)
        exec(compile(isolated, 'private_vps_builder.py', 'exec'), namespace)
        plan, _, operation = self.plan()
        self.assertEqual(namespace['build_intent'](plan, operation), build_intent(plan, operation))
        self.assertNotIn('validate_reward_risk_policy', namespace)
        self.assertNotIn('validated_protection_working_type', namespace)

    def test_analysis_context_supplies_exact_net_formula_without_editing_prompt_or_schema(self):
        prompt, schema = ANALYSIS, copy.deepcopy(SETUP_SCHEMA)
        account = Account()
        account.taker_fee = '0.0005'
        context, _ = analysis_context(FakeMarket(), 'BTCUSDT', '5', account)
        constraints = context['risk_constraints']
        self.assertEqual(constraints['reward_risk_policy'], NORMALIZED_REWARD_RISK_POLICY)
        self.assertEqual(constraints['risk_model'], RISK_MODEL)
        self.assertEqual(constraints['exit_slippage_rate'], '0.005')
        self.assertEqual(constraints['target_actual_reward_risk'], 2)
        self.assertEqual(constraints['take_profit_tick_rounding'], {'LONG': 'CEILING', 'SHORT': 'FLOOR'})
        self.assertIn('(E*(1+f)+2*L)/((1-a)*(1-f))', constraints['take_profit_contract'])
        self.assertIn('(E*(1-f)-2*L)/((1+a)*(1+f))', constraints['take_profit_contract'])
        self.assertIn('Preserve model Entry, TP, and SL', constraints['position_sizing_contract'])
        self.assertIn('normalizes the final order TP to net 1:2', constraints['position_sizing_contract'])
        self.assertEqual(ANALYSIS, prompt)
        self.assertEqual(SETUP_SCHEMA, schema)


if __name__ == '__main__':
    unittest.main()
