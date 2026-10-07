"""Versioned fee/slippage plans preserve model evidence and legacy commands."""
import ast
import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from support import RULES
from worker.core import (D, Ledger, Review, Signal, RISK_MODEL,
                         NORMALIZED_REWARD_RISK_POLICY, day, risk_check)
from worker.neuroapi import NeuroAPI, SETUP_SCHEMA, setup
from worker.order_gateway import build_intent
from worker.prompts import ANALYSIS
from worker.robot_provenance import SIZING, SIZING_V3, stamp, verify


class RiskV3ProvenanceTests(unittest.TestCase):
    def setUp(self):
        network = patch('socket.socket', side_effect=AssertionError('Live exchange forbidden'))
        network.start()
        self.addCleanup(network.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.ledger = Ledger(Path(folder.name) / 'ledger.sqlite3')
        self.addCleanup(self.ledger.db.close)

    def plan(self, *, side='LONG', legacy=False, raw_tp=None, declared_rr='4.8'):
        symbol = 'ETHUSDT'
        rules = replace(RULES(), tick=D('.01'), taker_fee_rate=D('.0005'), fee_symbol=symbol)
        entry = D('2610')
        sl = D('2585') if side == 'LONG' else D('2635')
        tp = D(raw_tp) if raw_tp else (D('2730') if side == 'LONG' else D('2490'))
        source = dict(symbol=symbol, side=side, limit_entry=entry, stop_loss=sl,
                      take_profit=tp, risk_reward=D(declared_rr))
        operation = day() + ':robot-v9:0:analysis-v9:' + symbol
        client = NeuroAPI(self.ledger, key='offline-risk-v3-proof',
            transport=lambda *args, **kwargs: (200, {}, dict(mode='smart', answer=None, output=copy.deepcopy(source))))
        client.ask(operation, ANALYSIS, SETUP_SCHEMA,
                   lambda value: setup(value, symbol, require_declared_rr=not legacy))
        signal = Signal(symbol, side, entry, tp, sl)
        if legacy:
            plan = risk_check(signal, rules)
        else:
            plan = risk_check(signal, rules, risk_model=RISK_MODEL,
                              reward_risk_policy=NORMALIZED_REWARD_RISK_POLICY)
            plan['protection_working_type'] = 'CONTRACT_PRICE'
        plan['sizing_rules'] = {key: str(value) if isinstance(value, D) else value
                                for key, value in asdict(rules).items()}
        stamp(self.ledger.db, plan, operation)
        return plan, rules, operation

    def test_new_long_and_short_proofs_export_reserved_costs_and_original_model_levels(self):
        for side, tp, qty, risk in (('LONG', '2707.23', '0.123', '4.9834726125'),
                                    ('SHORT', '2513.25', '0.122', '4.978098675')):
            with self.subTest(side=side):
                self.ledger.db.execute('DROP TABLE IF EXISTS api_requests')
                plan, _, operation = self.plan(side=side)
                original = copy.deepcopy(plan)
                self.assertTrue(verify(self.ledger.db, plan, operation))
                self.assertEqual(plan['provenance']['execution_sizing_version'], SIZING_V3)
                command = build_intent(plan, operation)
                self.assertEqual(command['entry']['quantity'], qty)
                self.assertEqual(command['protection']['take_profit'], tp)
                self.assertEqual(command['risk_usdt'], risk)
                self.assertEqual(command['risk_model'], RISK_MODEL)
                self.assertEqual(command['reward_risk_policy'], NORMALIZED_REWARD_RISK_POLICY)
                self.assertEqual(command['protection']['working_type'], 'CONTRACT_PRICE')
                self.assertEqual(command['cost_evidence'], plan['cost_evidence'])
                self.assertEqual(command['tp_normalization'], plan['tp_normalization'])
                self.assertEqual(command['exit_slippage_rate'], '0.005')
                self.assertEqual(command['entry_slippage_rate'], '0')
                self.assertEqual(command['tp_tick_size'], '0.01')
                self.assertGreaterEqual(D(command['net_reward_risk']), D('2'))
                self.assertLess(D(command['net_reward_risk']), D('2.001'))
                self.assertEqual(plan, original)
                self.assertNotEqual(plan['tp'], plan['tp_normalization']['model_tp'])

    def test_builder_nested_evidence_has_no_alias_to_saved_plan(self):
        plan, _, operation = self.plan()
        original = copy.deepcopy(plan)
        command = build_intent(plan, operation)
        command['cost_evidence']['excluded_costs'].append('FORGED')
        command['excluded_costs'].append('FORGED')
        command['tp_normalization']['model_tp'] = '9999'
        self.assertEqual(plan, original)

    def test_off_tick_model_tp_is_audited_while_execution_tp_is_legal(self):
        plan, _, operation = self.plan(raw_tp='2730.005')
        self.assertEqual(plan['tp_normalization']['model_tp'], '2730.005')
        self.assertEqual(plan['tp'], '2707.23')
        self.assertTrue(verify(self.ledger.db, plan, operation))
        self.assertEqual(build_intent(plan, operation)['protection']['take_profit'], '2707.23')

    def test_legacy_eth_remains_verified_with_byte_identical_old_intent(self):
        plan, _, operation = self.plan(legacy=True)
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
            excluded_costs=['SLIPPAGE', 'FUNDING', 'GAPS'], evidence_sha256=plan['provenance']['payload_sha256'])
        before = json.dumps(plan).encode()
        self.assertTrue(verify(self.ledger.db, plan, operation))
        self.assertEqual(plan['provenance']['execution_sizing_version'], SIZING)
        self.assertEqual(json.dumps(build_intent(plan, operation)).encode(), json.dumps(expected).encode())
        self.assertEqual(json.dumps(plan).encode(), before)
        self.assertNotIn('risk_model', expected)
        self.assertNotIn('cost_evidence', expected)
        self.assertNotIn('tp_normalization', expected)

    def test_legacy_cache_keeps_historical_declared_rr_semantics(self):
        plan, _, operation = self.plan(legacy=True, declared_rr='1')
        before = json.dumps(plan).encode()
        self.assertTrue(verify(self.ledger.db, plan, operation))
        self.assertEqual(build_intent(plan, operation)['protection']['take_profit'], '2730')
        self.assertEqual(json.dumps(plan).encode(), before)

    def test_existing_proof_detects_every_reserved_math_and_normalization_change(self):
        plan, _, operation = self.plan()
        mutations = [('risk_model', 'FEE_SLIPPAGE_RISK_V2'), ('exit_slippage_rate', '0.001'),
                     ('entry_slippage_rate', '0.005'), ('sl_slippage_usdt', '0'),
                     ('tp_slippage_usdt', '0'), ('sl_execution_price', '2585'),
                     ('tp_execution_price', '2730'), ('risk', '4'), ('net_reward', '10'),
                     ('execution_quantity', '0.181'), ('tp', '2730')]
        for key, value in mutations:
            with self.subTest(key=key):
                changed = copy.deepcopy(plan)
                changed[key] = value
                with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
                    verify(self.ledger.db, changed, operation)
                with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
                    stamp(self.ledger.db, changed, operation)
                with self.assertRaisesRegex(Review, '^INVALID_ORDER_CONTRACT$'):
                    build_intent(changed, operation)

    def test_restamping_inconsistent_tick_fee_rate_or_nested_evidence_is_refused(self):
        plan, _, operation = self.plan()
        changes = []
        for key, value in (('tick', '1'), ('taker_fee_rate', '0.001'), ('step', '0.01')):
            changed = copy.deepcopy(plan)
            changed['sizing_rules'][key] = value
            changes.append(changed)
        for section, key, value in (('cost_evidence', 'exit_slippage_rate', '0.001'),
                                     ('cost_evidence', 'taker_fee_rate', '0.001'),
                                     ('cost_evidence', 'reserve_source', 'INVENTED'),
                                     ('tp_normalization', 'execution_tp', '2730'),
                                     ('tp_normalization', 'risk_model', 'INVENTED'),
                                     ('tp_normalization', 'model_gross_rr', '2')):
            changed = copy.deepcopy(plan)
            changed[section][key] = value
            changes.append(changed)
        for changed in changes:
            with self.subTest(changed=changed['tp_normalization']):
                before = copy.deepcopy(changed)
                with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
                    stamp(self.ledger.db, changed, operation)
                self.assertEqual(changed, before)
                with self.assertRaisesRegex(Review, '^INVALID_ORDER_CONTRACT$'):
                    build_intent(changed, operation)

    def test_fully_recomputed_plan_cannot_restamp_different_original_model_levels(self):
        plan, rules, operation = self.plan()
        for signal in (Signal('ETHUSDT', 'LONG', D('2610'), D('2800'), D('2585')),
                       Signal('ETHUSDT', 'LONG', D('2610'), D('2730'), D('2580')),
                       Signal('ETHUSDT', 'LONG', D('2611'), D('2730'), D('2585'))):
            with self.subTest(signal=signal):
                changed = risk_check(signal, rules, risk_model=RISK_MODEL,
                                     reward_risk_policy=NORMALIZED_REWARD_RISK_POLICY)
                changed['sizing_rules'] = copy.deepcopy(plan['sizing_rules'])
                changed['protection_working_type'] = 'CONTRACT_PRICE'
                with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
                    stamp(self.ledger.db, changed, operation)
                self.assertNotIn('provenance', changed)

    def test_source_or_request_hash_change_invalidates_saved_execution_proof(self):
        plan, _, operation = self.plan()
        saved = dict(self.ledger.db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone())
        source = json.loads(saved['output'])
        source['take_profit'] = 2800
        self.ledger.db.execute('UPDATE api_requests SET output=? WHERE operation=?', (json.dumps(source), operation))
        with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
            verify(self.ledger.db, plan, operation)
        self.ledger.db.execute('UPDATE api_requests SET output=?,body_hash=? WHERE operation=?',
                               (saved['output'], '0'*64, operation))
        with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
            verify(self.ledger.db, plan, operation)

    def test_version_marker_cannot_be_added_removed_or_restamped_as_another_version(self):
        plan, _, operation = self.plan()
        for value in (None, True, '', 'FEE_SLIPPAGE_RISK_V2'):
            changed = dict(plan, risk_model=value)
            with self.subTest(value=value):
                with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
                    stamp(self.ledger.db, changed, operation)
                with self.assertRaisesRegex(Review, '^INVALID_ORDER_CONTRACT$'):
                    build_intent(changed, operation)
        removed = copy.deepcopy(plan)
        removed.pop('risk_model')
        with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
            verify(self.ledger.db, removed, operation)
        changed = copy.deepcopy(plan)
        changed['provenance']['execution_sizing_version'] = SIZING
        with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
            stamp(self.ledger.db, changed, operation)

    def test_private_vps_builder_needs_no_new_module_level_imports(self):
        import worker.order_gateway as module
        tree = ast.parse(Path(module.__file__).read_text())
        builder = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'build_intent')
        isolated = ast.Module(body=[builder], type_ignores=[])
        namespace = dict(__name__='worker.private_vps_builder', __package__='worker',
                         hashlib=hashlib, D=D, Review=Review)
        from worker.core import number, validated_risk_target
        namespace.update(number=number, validated_risk_target=validated_risk_target)
        exec(compile(isolated, 'private_vps_builder.py', 'exec'), namespace)
        plan, _, operation = self.plan()
        self.assertEqual(namespace['build_intent'](plan, operation), build_intent(plan, operation))
        self.assertNotIn('validate_normalized_plan', namespace)


if __name__ == '__main__':
    unittest.main()
