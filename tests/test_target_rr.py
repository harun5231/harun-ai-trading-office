"""Future net 1:2 targets are exact tick checks, never automatic TP edits."""
import copy
from dataclasses import replace
from decimal import Decimal, localcontext
from fractions import Fraction
import time
import unittest

from worker.core import (D, FEE_SOURCE, REWARD_RISK_POLICY, Review, Rules, Signal,
                         number, preflight, risk_check, target_reward_risk_tp,
                         validate_reward_risk_policy)


class TargetRewardRiskTests(unittest.TestCase):
    def rules(self, tick='.01', fee='.0005', symbol='BTCUSDT'):
        return Rules(D('.001'), D('.001'), D('1000'), D(tick), D('5'), time.time(),
                     taker_fee_rate=D(fee), fee_observed_at=time.time(),
                     fee_symbol=symbol, fee_source=FEE_SOURCE)

    def signal(self, side='LONG', entry='100', sl='98', tp='104.31'):
        return Signal('BTCUSDT', side, D(entry), D(tp), D(sl))

    def net(self, entry, sl, tp, side, rate):
        e, s, p, f = map(Fraction, (entry, sl, tp, rate))
        risk = abs(e - s) + f * (e + s)
        reward = (p - e if side == 'LONG' else e - p) - f * (e + p)
        return reward, risk

    def test_long_and_short_first_tick_meets_net_two_and_previous_tick_does_not(self):
        for side, sl, expected in (('LONG', '98', '104.31'), ('SHORT', '102', '95.70')):
            with self.subTest(side=side):
                rules = self.rules()
                target = target_reward_risk_tp('100', sl, side, rules, symbol='BTCUSDT')
                self.assertEqual(target, D(expected))
                reward, risk = self.net(D('100'), D(sl), target, side, rules.taker_fee_rate)
                self.assertGreaterEqual(reward, 2 * risk)
                nearer = target - rules.tick if side == 'LONG' else target + rules.tick
                reward, risk = self.net(D('100'), D(sl), nearer, side, rules.taker_fee_rate)
                self.assertLess(reward, 2 * risk)

    def test_zero_fee_and_coarse_ticks_still_choose_first_valid_tick(self):
        for side, sl, tick, fee, expected in (
                ('LONG', '98', '.01', '0', '104'),
                ('SHORT', '102', '.01', '0', '96'),
                ('LONG', '98', '1', '.0005', '105'),
                ('SHORT', '102', '1', '.0005', '95'),
                ('LONG', '98', '.25', '.0005', '104.5'),
                ('SHORT', '102', '.25', '.0005', '95.5')):
            with self.subTest(side=side, tick=tick, fee=fee):
                rules = self.rules(tick, fee)
                target = target_reward_risk_tp('100', sl, side, rules)
                self.assertEqual(target, D(expected))
                self.assertEqual((Fraction(target) / Fraction(rules.tick)).denominator, 1)
                reward, risk = self.net(D('100'), D(sl), target, side, rules.taker_fee_rate)
                self.assertGreaterEqual(reward, 2 * risk)

    def test_fraction_target_is_independent_of_decimal_precision(self):
        rules = self.rules('.000000000000000001', '.000000000000000137')
        entry = D('1.123456789012345678')
        stop = D('1.123400000000000001')
        with localcontext() as context:
            context.prec = 160
            expected = target_reward_risk_tp(entry, stop, 'LONG', rules)
        with localcontext() as context:
            context.prec = 6
            actual = target_reward_risk_tp(entry, stop, 'LONG', rules)
        self.assertEqual(actual, expected)
        reward, risk = self.net(entry, stop, actual, 'LONG', rules.taker_fee_rate)
        self.assertGreaterEqual(reward, 2 * risk)
        nearer = Fraction(actual) - Fraction(rules.tick)
        reward, risk = self.net(entry, stop, nearer, 'LONG', rules.taker_fee_rate)
        self.assertLess(reward, 2 * risk)

    def test_policy_adds_only_flag_and_keeps_quantity_and_original_setup(self):
        for side, sl, tp in (('LONG', '98', '104.31'), ('SHORT', '102', '95.70')):
            with self.subTest(side=side):
                signal, rules = self.signal(side=side, sl=sl, tp=tp), self.rules()
                before = copy.deepcopy(signal)
                legacy = risk_check(signal, rules)
                target = risk_check(signal, rules, reward_risk_policy=REWARD_RISK_POLICY)
                self.assertEqual(target.pop('reward_risk_policy'), REWARD_RISK_POLICY)
                self.assertEqual(target, legacy)
                self.assertEqual(signal, before)
                self.assertLessEqual(D(target['risk']), D('5'))
                costs = D(target['gross_risk']) + D(target['entry_fee_usdt']) + D(target['sl_exit_fee_usdt'])
                self.assertEqual(costs, D(target['risk']))
                next_quantity = Fraction(D(target['quantity'])) + Fraction(rules.step)
                next_risk = next_quantity * (abs(Fraction(signal.entry) - Fraction(signal.sl)) +
                    Fraction(rules.taker_fee_rate) * (Fraction(signal.entry) + Fraction(signal.sl)))
                self.assertGreater(next_risk, Fraction(5))

    def test_far_tp_is_rejected_and_never_automatically_repriced(self):
        for side, sl, tp in (('LONG', '98', '110'), ('SHORT', '102', '90')):
            with self.subTest(side=side):
                signal, rules = self.signal(side=side, sl=sl, tp=tp), self.rules()
                before = copy.deepcopy(signal)
                with self.assertRaisesRegex(Review, '^NET_RISK_REWARD_NOT_TARGET_2$'):
                    risk_check(signal, rules, reward_risk_policy=REWARD_RISK_POLICY)
                self.assertEqual(signal, before)
                plan = risk_check(signal, rules)
                plan['reward_risk_policy'] = REWARD_RISK_POLICY
                before = copy.deepcopy(plan)
                with self.assertRaisesRegex(Review, '^NET_RISK_REWARD_NOT_TARGET_2$'):
                    preflight(plan, rules)
                self.assertEqual(plan, before)

    def test_below_net_two_retains_existing_reason_and_price_failures_keep_priority(self):
        rules = self.rules()
        for side, sl, tp in (('LONG', '98', '104.30'), ('SHORT', '102', '95.71')):
            with self.subTest(side=side):
                signal = self.signal(side=side, sl=sl, tp=tp)
                with self.assertRaisesRegex(Review, '^NET_RISK_REWARD_BELOW_2$'):
                    risk_check(signal, rules, reward_risk_policy=REWARD_RISK_POLICY)
        with self.assertRaisesRegex(Review, 'harga tidak sesuai tick/rentang'):
            risk_check(self.signal(tp='104.311'), rules, reward_risk_policy=REWARD_RISK_POLICY)

    def test_legacy_wide_rr_and_current_eth_remain_valid_without_policy_flag(self):
        examples = ((self.signal(sl='99', tp='104.8'), self.rules(fee='0')),
                    (Signal('ETHUSDT', 'LONG', D('2610'), D('2730'), D('2585')),
                     self.rules(symbol='ETHUSDT')))
        for signal, rules in examples:
            with self.subTest(symbol=signal.symbol):
                plan = risk_check(signal, rules)
                before = copy.deepcopy(plan)
                self.assertNotIn('reward_risk_policy', plan)
                self.assertTrue(validate_reward_risk_policy(plan, rules))
                preflight(plan, rules)
                self.assertEqual(plan, before)
                self.assertGreater(D(plan['net_rr']), D('4'))

    def test_future_policy_preflight_passes_but_unknown_flags_are_rejected(self):
        rules = self.rules()
        plan = risk_check(self.signal(), rules, reward_risk_policy=REWARD_RISK_POLICY)
        before = copy.deepcopy(plan)
        self.assertTrue(validate_reward_risk_policy(plan, rules))
        preflight(plan, rules)
        self.assertEqual(plan, before)
        for policy in (True, 2, [], {}, '', 'NET_1_TO_3', None):
            with self.subTest(policy=policy):
                changed = copy.deepcopy(plan)
                changed['reward_risk_policy'] = policy
                with self.assertRaisesRegex(Review, '^INVALID_RISK_REWARD$'):
                    validate_reward_risk_policy(changed, rules)
                with self.assertRaisesRegex(Review, '^INVALID_RISK_REWARD$'):
                    preflight(changed, rules)
        with self.assertRaisesRegex(Review, '^INVALID_RISK_REWARD$'):
            risk_check(self.signal(), rules, reward_risk_policy='NET_1_TO_3')

    def test_math_retains_historical_fee_evidence_but_risk_requires_fresh_get(self):
        rules = self.rules()
        stale = replace(rules, fee_observed_at=time.time() - 600)
        expected = target_reward_risk_tp('100', '98', 'LONG', rules)
        self.assertEqual(target_reward_risk_tp('100', '98', 'LONG', stale), expected)
        with self.assertRaisesRegex(Review, '^STALE_FEE_EVIDENCE$'):
            risk_check(self.signal(), stale, reward_risk_policy=REWARD_RISK_POLICY)

    def test_nonpositive_short_target_wrong_side_and_unproven_fee_are_rejected(self):
        with self.assertRaisesRegex(Review, '^INVALID_ENTRY_TP_SL$'):
            target_reward_risk_tp('1', '2', 'SHORT', self.rules())
        with self.assertRaisesRegex(Review, '^INVALID_ENTRY_TP_SL$'):
            target_reward_risk_tp('100', '102', 'LONG', self.rules())
        with self.assertRaisesRegex(Review, '^INVALID_SIDE$'):
            target_reward_risk_tp('100', '98', 'HOLD', self.rules())
        with self.assertRaisesRegex(Review, '^INVALID_FEE_EVIDENCE$'):
            target_reward_risk_tp('100', '98', 'LONG', replace(self.rules(), taker_fee_rate=D('1')))
        with self.assertRaisesRegex(Review, '^FEE_EVIDENCE_UNAVAILABLE$'):
            target_reward_risk_tp('100', '98', 'LONG', replace(self.rules(), taker_fee_rate=None))


if __name__ == '__main__':
    unittest.main(verbosity=2)
