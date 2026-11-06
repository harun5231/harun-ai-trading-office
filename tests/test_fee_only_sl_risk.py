"""Fee-only SL policy contracts; all gateway checks are offline and local."""
import copy
from dataclasses import asdict, replace
from decimal import localcontext
from fractions import Fraction as F
import json
from pathlib import Path
import sys
import time
from types import ModuleType
import unittest
from unittest.mock import patch

from worker.core import (D, DynamicRules, FEE_SOURCE, Review, Signal,
    DYNAMIC_RISK_MODEL, DYNAMIC_REWARD_RISK_POLICY, deserialize_sizing_rules,
    maximum_risk_quantity, preflight, risk_check, validate_normalized_plan, verified_rr)
from worker.dynamic_risk import (BRACKET_SOURCE, COST_SOURCE, LEGACY_POLICY,
    MODEL, POLICY, POLICY_VERSION, estimate_existing)
from worker.order_gateway import build_intent
from worker.provenance import digest


ROOT = Path(__file__).resolve().parents[1]
LEGACY_FIXTURES = ROOT / 'tests/fixtures/fee_only_legacy_plans.json'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def frozen_rules(rules):
    return {key:format(value, 'f') if isinstance(value, D) else value
        for key, value in asdict(rules).items()}


class FeeOnlySLRiskTests(unittest.TestCase):
    def rules(self, *, available='1000', schema=2, brackets=None, **changes):
        at = time.time()
        evidence = dict(schema_version=1, symbol='ETHUSDT', source=COST_SOURCE,
            observed_at=at, bids=[['99.99', '2'], ['99.5', '1000']],
            asks=[['100.01', '2'], ['100.5', '1000']], execution_samples=[],
            price_moves=[dict(start=at-(21-i)*900, end=at-(20-i)*900,
                reference='100', high='100.01', low='99.99') for i in range(20)])
        account = dict(schema_version=schema, symbol='ETHUSDT', source=BRACKET_SOURCE,
            observed_at=at, brackets=brackets or [dict(notional_floor='0',
                notional_cap='10000000', max_leverage=125)])
        if schema == 2:
            account['margin'] = dict(source='BINANCE_FUTURES_ACCOUNT_AVAILABLE_BALANCE',
                asset='USDT', observed_at=at, available_balance_usdt=available)
        return replace(DynamicRules(D('.01'), D('.01'), D('1000'), D('.01'), D('1'), at,
            taker_fee_rate=D('.0005'), fee_observed_at=at, fee_symbol='ETHUSDT',
            fee_source=FEE_SOURCE, dynamic_cost_evidence=evidence,
            leverage_brackets=account), **changes)

    def signal(self, side='LONG', *, entry='100', sl=None, tp=None):
        return Signal('ETHUSDT', side, D(entry),
            D(tp or ('100.5' if side == 'LONG' else '99.5')),
            D(sl or ('99' if side == 'LONG' else '101')))

    def plan(self, side='LONG', *, rules=None, target='5', signal=None):
        return risk_check(signal or self.signal(side), rules or self.rules(), D(target),
            risk_model=DYNAMIC_RISK_MODEL, reward_risk_policy=DYNAMIC_REWARD_RISK_POLICY)

    def assert_price_target(self, value, rules):
        q, e, sl, tp = (F(D(value[key])) for key in ('quantity', 'entry', 'sl', 'tp'))
        fee = F(rules.taker_fee_rate)
        actual_loss = q*abs(e-sl)+q*e*fee+q*sl*fee
        target = 2*actual_loss
        price_pnl = q*abs(tp-e)
        self.assertEqual(F(D(value['risk'])), actual_loss)
        self.assertEqual(F(D(value['total_risk'])), actual_loss)
        self.assertEqual(F(D(value['target_price_pnl_usdt'])), target)
        self.assertEqual(F(D(value['gross_reward'])), price_pnl)
        self.assertGreaterEqual(price_pnl, target)
        self.assertEqual(F(D(value['price_pnl_target_difference_usdt'])), price_pnl-target)
        previous = tp-F(rules.tick) if value['side'] == 'LONG' else tp+F(rules.tick)
        self.assertLess(q*abs(previous-e), target)
        self.assertEqual((tp/F(rules.tick)).denominator, 1)
        self.assertEqual(F(D(value['entry_fee_usdt'])), q*e*fee)
        self.assertEqual(F(D(value['sl_exit_fee_usdt'])), q*sl*fee)
        self.assertEqual(F(D(value['tp_exit_fee_usdt'])), q*tp*fee)
        self.assertEqual(F(D(value['net_reward'])), price_pnl-q*e*fee-q*tp*fee)
        self.assertIsNone(value['target_net_reward_usdt'])
        self.assertIsNone(value['net_reward_target_difference_usdt'])
        self.assertFalse(value['tp_normalization']['tp_target_includes_tp_fee'])

    def test_long_short_exact_fees_maximum_lot_and_first_tick_reaching_target(self):
        for side in ('LONG', 'SHORT'):
            for budget in ('5', '3'):
                for fee in ('0', '.0005'):
                    with self.subTest(side=side, budget=budget, fee=fee):
                        rules = self.rules(taker_fee_rate=D(fee))
                        signal = self.signal(side)
                        value = self.plan(side, rules=rules, target=budget)
                        unit = abs(F(signal.entry)-F(signal.sl))+F(D(fee))*(F(signal.entry)+F(signal.sl))
                        expected = (F(D(budget))//(unit*F(rules.step)))*F(rules.step)
                        self.assertEqual(F(D(value['quantity'])), expected)
                        self.assertLessEqual(F(D(value['risk'])), F(D(budget)))
                        self.assertGreater((expected+F(rules.step))*unit, F(D(budget)))
                        self.assertEqual(D(value['quantity']), maximum_risk_quantity(
                            signal.entry, signal.sl, rules, D(budget), risk_model=MODEL, side=side))
                        self.assert_price_target(value, rules)
                        self.assertEqual(value['policy_version'], POLICY_VERSION)
                        self.assertEqual(value['reward_risk_policy'], POLICY)
                        self.assertEqual((value['entry'], value['sl'], value['neurobro_tp']),
                            tuple(format(v, 'f') for v in (signal.entry, signal.sl, signal.tp)))
                        original = canonical(value)
                        self.assertTrue(validate_normalized_plan(value, rules))
                        preflight(value, rules, D(budget))
                        verified_rr(value)
                        self.assertEqual(canonical(value), original)

    def test_margin_limited_trade_targets_actual_risk_and_not_twice_office_budget(self):
        for side in ('LONG', 'SHORT'):
            with self.subTest(side=side):
                rules = self.rules(available='2')
                value = self.plan(side, rules=rules)
                q, entry, fee = F(D(value['quantity'])), F(D(value['entry'])), F(rules.taker_fee_rate)
                required = lambda amount: amount*entry/F(value['leverage'])+amount*entry*fee
                self.assertLessEqual(required(q), 2)
                self.assertGreater(required(q+F(rules.step)), 2)
                self.assertLess(F(D(value['risk'])), 5)
                self.assertLess(F(D(value['target_price_pnl_usdt'])), 10)
                self.assertLess(F(D(value['gross_reward'])), 10)
                self.assert_price_target(value, rules)

    def test_notional_caps_margin_tier_gaps_and_account_leverage_choose_largest_legal_lot(self):
        cases = [
            ([dict(notional_floor='0', notional_cap='200', max_leverage=75)], '1000', '1.99'),
            ([dict(notional_floor='0', notional_cap='200', max_leverage=75),
              dict(notional_floor='200', notional_cap='100000', max_leverage=20)], '5', '1.99'),
            ([dict(notional_floor='0', notional_cap='100', max_leverage=1),
              dict(notional_floor='100', notional_cap='100000', max_leverage=75)], '2', '1.44')]
        for side in ('LONG', 'SHORT'):
            for brackets, available, expected in cases:
                with self.subTest(side=side, brackets=brackets, available=available):
                    rules = self.rules(available=available, brackets=brackets)
                    value = self.plan(side, rules=rules)
                    self.assertEqual(value['quantity'], expected)
                    self.assert_price_target(value, rules)
        rules = self.rules(available='2')
        account = copy.deepcopy(rules.leverage_brackets)
        account['account_max_leverage'] = 10
        value = self.plan(rules=replace(rules, leverage_brackets=account))
        self.assertEqual((value['leverage'], value['quantity']), (10, '0.19'))

    def test_schema_one_still_honors_exclusive_supported_notional_cap(self):
        rules = self.rules(schema=1, brackets=[dict(notional_floor='0',
            notional_cap='200', max_leverage=50)])
        value = self.plan(rules=rules)
        self.assertEqual((value['quantity'], value['leverage']), ('1.99', 50))
        self.assertLess(D(value['quantity'])*D(value['entry']), D('200'))
        self.assert_price_target(value, rules)

    def test_account_leverage_changes_margin_capacity_but_never_exceeds_risk_ceiling(self):
        rules = self.rules(available='2')
        for ceiling in (10, 20, 50, 75, 125):
            with self.subTest(ceiling=ceiling):
                account = copy.deepcopy(rules.leverage_brackets)
                account['account_max_leverage'] = ceiling
                value = self.plan(rules=replace(rules, leverage_brackets=account))
                self.assertEqual(value['leverage'], min(ceiling, 75))
                self.assertLessEqual(D(value['risk']), D('5'))

    def test_zero_fee_and_coarse_ticks_round_outward_and_reject_unreachable_bounds(self):
        for side, expected in (('LONG', '103'), ('SHORT', '97')):
            rules = self.rules(tick=D('1'))
            value = self.plan(side, rules=rules)
            self.assertEqual(value['tp'], expected)
            self.assert_price_target(value, rules)
            zero = self.plan(side, rules=replace(rules, taker_fee_rate=D('0')))
            self.assertEqual(zero['tp'], '102' if side == 'LONG' else '98')
        for side, changes in (('LONG', dict(max_price=D('102.19'))),
                ('SHORT', dict(min_price=D('97.81')))):
            with self.subTest(side=side), self.assertRaisesRegex(Review, 'NO_LEGAL_PRICE_PNL_2R_TP'):
                self.plan(side, rules=self.rules(**changes))

    def test_costs_and_tp_do_not_depend_on_book_depth_volatility_or_execution_reserves(self):
        rules = self.rules()
        changed = copy.deepcopy(rules.dynamic_cost_evidence)
        changed['bids'] = [['90', '1000']]
        changed['asks'] = [['110', '1000']]
        for move in changed['price_moves']:
            move.update(low='90', high='110')
        changed['execution_samples'] = [dict(order_id='order-'+str(i), execution_id='fill-'+str(i),
            observed_at=changed['observed_at'], side='SELL', expected_impact_rate='.001',
            actual_impact_rate='.2') for i in range(20)]
        for side in ('LONG', 'SHORT'):
            original = self.plan(side, rules=rules)
            altered = self.plan(side, rules=replace(rules, dynamic_cost_evidence=changed))
            absent = self.plan(side, rules=replace(rules, dynamic_cost_evidence=None))
            shallow = copy.deepcopy(changed)
            shallow['bids'] = [['90', '.0001']]
            shallow['asks'] = [['110', '.0001']]
            insufficient = self.plan(side, rules=replace(rules, dynamic_cost_evidence=shallow))
            malformed = copy.deepcopy(changed)
            del malformed['price_moves'][0]['low']
            malformed_rules = replace(rules, dynamic_cost_evidence=malformed)
            invalid_telemetry = self.plan(side, rules=malformed_rules)
            self.assertTrue(validate_normalized_plan(invalid_telemetry, malformed_rules))
            for value in (altered, absent, insufficient, invalid_telemetry):
                for key in ('entry', 'sl', 'quantity', 'risk', 'tp', 'gross_reward',
                        'target_price_pnl_usdt', 'net_reward'):
                    self.assertEqual(value[key], original[key])
            self.assertNotEqual(altered['cost_evidence']['expected_book_impact_rate'],
                original['cost_evidence']['expected_book_impact_rate'])
            self.assertIsNone(absent['cost_evidence']['expected_book_impact_rate'])
            self.assertIsNone(insufficient['cost_evidence']['expected_book_impact_rate'])
            self.assertIsNone(invalid_telemetry['cost_evidence']['expected_book_impact_rate'])
            for key in ('sl_book_impact_usdt', 'tp_book_impact_usdt',
                    'sl_liquidity_uncertainty_usdt', 'tp_liquidity_uncertainty_usdt',
                    'sl_slippage_usdt', 'tp_slippage_usdt', 'exit_slippage_rate'):
                self.assertEqual(altered[key], '0')
            self.assertFalse(altered['cost_evidence']['slippage_in_risk_and_tp'])

    def test_stale_optional_telemetry_cannot_change_frozen_validation_result(self):
        rules = self.rules()
        evidence = copy.deepcopy(rules.dynamic_cost_evidence)
        evidence['observed_at'] -= 301
        rules = replace(rules, dynamic_cost_evidence=evidence)
        value = self.plan(rules=rules)
        before = canonical(value)
        self.assertTrue(validate_normalized_plan(value, rules, check_fresh=False))
        preflight(value, rules)
        self.assertEqual(canonical(value), before)

    def test_original_neurobro_levels_remain_evidence_and_do_not_constrain_execution_tp(self):
        for side, original_tps in (('LONG', ('100.01', '120')),
                ('SHORT', ('99.99', '80'))):
            rules = self.rules()
            values = [self.plan(side, rules=rules, signal=self.signal(side, tp=tp))
                for tp in original_tps]
            self.assertEqual(values[0]['quantity'], values[1]['quantity'])
            self.assertEqual(values[0]['tp'], values[1]['tp'])
            self.assertEqual([v['neurobro_tp'] for v in values], list(original_tps))
            self.assertEqual([v['tp_normalization']['model_tp'] for v in values], list(original_tps))
            self.assertLess(D(values[0]['net_rr']), D('2'))
            for value in values:
                self.assert_price_target(value, rules)

    def test_high_precision_arithmetic_and_validation_ignore_callers_decimal_precision(self):
        for side in ('LONG', 'SHORT'):
            rules = self.rules(tick=D('.000000000000000001'), step=D('.001'),
                minimum=D('.001'), taker_fee_rate=D('.000000000000137'))
            signal = self.signal(side, entry='1.123456789012345678',
                sl='1.123400000000000001' if side == 'LONG' else '1.123500000000000001',
                tp='1.124' if side == 'LONG' else '1.123')
            expected = self.plan(side, signal=signal, rules=rules)
            with localcontext() as context:
                context.prec = 3
                actual = self.plan(side, signal=signal, rules=rules)
                self.assertTrue(validate_normalized_plan(actual, rules))
                preflight(actual, rules)
                verified_rr(actual)
                self.assert_price_target(actual, rules)
            self.assertEqual(actual, expected)

    def test_no_legal_lot_and_fee_margin_or_filter_evidence_remain_fail_closed(self):
        for rules in (self.rules(available='0'), self.rules(minimum=D('10')),
                self.rules(available='2', min_notional=D('200'))):
            with self.assertRaisesRegex(Review, 'NO_LEGAL_MAX_RISK_QUANTITY'):
                self.plan(rules=rules)
        rules = self.rules()
        for altered, code in ((replace(rules, fee_observed_at=rules.fee_observed_at-301), 'STALE_FEE_EVIDENCE'),
                (replace(rules, fee_symbol='BTCUSDT'), 'INVALID_FEE_EVIDENCE'),
                (replace(rules, observed_at=rules.observed_at-301), 'filter pasar kedaluwarsa')):
            with self.subTest(code=code), self.assertRaisesRegex(Review, code):
                self.plan(rules=altered)
        account = copy.deepcopy(rules.leverage_brackets)
        account['margin']['observed_at'] -= 301
        with self.assertRaisesRegex(Review, 'STALE_MARGIN_EVIDENCE'):
            self.plan(rules=replace(rules, leverage_brackets=account))

    def test_saved_policy_target_and_fee_tampering_is_rejected_without_rewriting_plan(self):
        rules = self.rules()
        value = self.plan(rules=rules)
        mutations = [('policy_version', MODEL), ('reward_risk_policy', LEGACY_POLICY),
            ('target_price_pnl_usdt', '10'), ('entry', '101'), ('sl', '98'),
            ('quantity', '1'), ('tp', '110'), ('sl_exit_fee_usdt', '0'),
            ('target_net_reward_usdt', '10'), ('exit_slippage_rate', '.005')]
        for key, replacement in mutations:
            altered = copy.deepcopy(value)
            altered[key] = replacement
            before = canonical(altered)
            with self.subTest(key=key), self.assertRaises(Review):
                validate_normalized_plan(altered, rules)
            self.assertEqual(canonical(altered), before)
        altered = copy.deepcopy(value)
        del altered['target_net_reward_usdt']
        with self.assertRaises(Review):
            validate_normalized_plan(altered, rules)

    def test_plan_freezes_nested_rule_evidence_without_modifying_input(self):
        rules = self.rules()
        original_rules = canonical(frozen_rules(rules))
        value = self.plan(rules=rules)
        self.assertEqual(canonical(frozen_rules(rules)), original_rules)
        original_plan = canonical(value)
        rules.dynamic_cost_evidence['bids'][0][0] = '50'
        rules.leverage_brackets['margin']['available_balance_usdt'] = '0'
        self.assertEqual(canonical(value), original_plan)

    def test_frozen_v4_both_schemas_and_v3_keep_exact_plans_and_gateway_commands(self):
        fixture_bytes = LEGACY_FIXTURES.read_bytes()
        fixtures = json.loads(fixture_bytes)
        self.assertEqual(fixtures['source'], 'd25fcc41f3c927d7e9098d78cdc2feb8e155625d')
        self.assertEqual({c['name'] for c in fixtures['cases']}, {'v4_schema1', 'v4_schema2', 'v3'})
        # Old validation must never invoke any new arithmetic helper.
        with (patch('worker.dynamic_risk._cost_values', side_effect=AssertionError('new costs')),
                patch('worker.dynamic_risk.maximum_quantity', side_effect=AssertionError('new sizing')),
                patch('worker.dynamic_risk.costs', side_effect=AssertionError('new reporting')),
                patch('worker.dynamic_risk.plan', side_effect=AssertionError('new policy'))):
            for case in fixtures['cases']:
                with self.subTest(case=case['name']):
                    value = copy.deepcopy(case['plan'])
                    before = canonical(value)
                    rules = deserialize_sizing_rules(value)
                    self.assertTrue(validate_normalized_plan(value, rules, check_fresh=False))
                    command = build_intent(value, case['intent']['intent_id'])
                    self.assertEqual(canonical(command), canonical(case['intent']))
                    self.assertEqual(canonical(value), before)
        self.assertEqual(LEGACY_FIXTURES.read_bytes(), fixture_bytes)

    def test_legacy_monitoring_keeps_reserves_and_new_monitoring_keeps_fee_only_basis(self):
        fixtures = json.loads(LEGACY_FIXTURES.read_text())
        legacy = next(c['plan'] for c in fixtures['cases'] if c['name'] == 'v4_schema2')
        rules = self.rules(available='2')
        before = canonical(legacy)
        old_estimate = estimate_existing(legacy, rules)
        self.assertGreater(D(old_estimate['components']['sl_book_impact_usdt']), 0)
        self.assertGreater(D(old_estimate['components']['sl_liquidity_uncertainty_usdt']), 0)
        self.assertFalse(old_estimate['order_parameters_changed'])
        self.assertEqual(canonical(legacy), before)
        new = self.plan(rules=rules)
        before = canonical(new)
        estimate = estimate_existing(new, rules)
        self.assertEqual(estimate['estimated_total_risk'], new['risk'])
        self.assertEqual(estimate['components']['sl_book_impact_usdt'], '0')
        self.assertFalse(estimate['order_parameters_changed'])
        self.assertEqual(canonical(new), before)


class NativeGatewayFeeOnlyABIContractTests(unittest.TestCase):
    """Use the actual native validator, with no client construction or requests."""
    rules = FeeOnlySLRiskTests.rules
    signal = FeeOnlySLRiskTests.signal
    plan = FeeOnlySLRiskTests.plan

    @classmethod
    def setUpClass(cls):
        path = ROOT / 'vps_native/worker/order_gateway.py'
        namespace = dict(__name__='worker._fee_only_native_gateway_fixture', __package__='worker')
        # Transport is outside this pure validator contract. An empty module
        # avoids installing an HTTP client and fails if any transport is used.
        with patch.dict(sys.modules, {'requests': ModuleType('requests')}):
            exec(compile(path.read_text(), str(path), 'exec'), namespace)
        cls.native_gateway_class = namespace['OrderGateway']

    def native_validate(self, command):
        gateway = object.__new__(self.native_gateway_class)
        with patch.object(gateway, '_request', side_effect=AssertionError('request forbidden')) as request:
            self.native_gateway_class._validate_intent(gateway, command)
        request.assert_not_called()

    def command(self, side):
        rules = self.rules(available='2')
        account = copy.deepcopy(rules.leverage_brackets)
        account['account_max_leverage'] = 20
        rules = replace(rules, leverage_brackets=account)
        value = self.plan(side, rules=rules)
        value['sizing_rules'] = frozen_rules(rules)
        value['protection_working_type'] = 'CONTRACT_PRICE'
        value['provenance'] = {'payload_sha256': digest(value)}
        return value, build_intent(value, 'offline-fee-only-'+side)

    def test_native_gateway_accepts_new_price_target_with_null_net_target_and_true_net_diagnostic(self):
        for side in ('LONG', 'SHORT'):
            with self.subTest(side=side):
                value, command = self.command(side)
                before = canonical(command)
                self.assertLess(D(command['net_reward_risk']), D('2'))
                self.assertIsNone(command['target_net_reward_usdt'])
                self.assertEqual(command['leverage'], 20)
                self.assertEqual(command['normalized_plan']['target_price_pnl_usdt'],
                    value['target_price_pnl_usdt'])
                self.native_validate(command)
                self.assertEqual(canonical(command), before)

    def test_native_gateway_refuses_outer_and_inner_cost_target_or_protection_tampering(self):
        _, original = self.command('LONG')
        for path, replacement in ((('risk_usdt',), '1'),
                (('target_net_reward_usdt',), '10'), (('net_reward_risk',), '2'),
                (('normalized_plan', 'target_price_pnl_usdt'), '10'),
                (('normalized_plan', 'tp_normalization', 'model_sl'), '98'),
                (('protection', 'take_profit'), '103')):
            value = copy.deepcopy(original)
            item = value
            for key in path[:-1]:
                item = item[key]
            item[path[-1]] = replacement
            before = canonical(value)
            with self.subTest(path=path), self.assertRaisesRegex(Review, 'INTENT_INVALID'):
                self.native_validate(value)
            self.assertEqual(canonical(value), before)


if __name__ == '__main__':
    unittest.main()
