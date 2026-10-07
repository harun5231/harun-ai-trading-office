import copy
import time
import unittest
from dataclasses import replace
from decimal import localcontext
from fractions import Fraction

from worker.core import (
    D, FEE_SOURCE, RISK_MODEL, NORMALIZED_REWARD_RISK_POLICY, EXIT_SLIPPAGE_RATE,
    Review, Rules, Signal, maximum_risk_quantity, normalized_reward_risk_tp,
    preflight, risk_check, risk_costs, validate_normalized_plan,
)


class ReservedRiskTests(unittest.TestCase):
    def rules(self, **changes):
        observed=time.time()
        base=Rules(D('.001'),D('.001'),D('1000'),D('.01'),D('1'),observed,
            taker_fee_rate=D('.0005'),fee_observed_at=observed,
            fee_symbol='ETHUSDT',fee_source=FEE_SOURCE)
        return replace(base,**changes)

    def plan(self, side='LONG', *, tp=None, rules=None, target='5'):
        rules=rules or self.rules()
        signal=Signal('ETHUSDT',side,D('2610'),D(tp or ('2730' if side=='LONG' else '2490')),
                      D('2585' if side=='LONG' else '2635'))
        return risk_check(signal,rules,D(target),risk_model=RISK_MODEL,
                          reward_risk_policy=NORMALIZED_REWARD_RISK_POLICY)

    def assert_exact_budget_and_target(self, plan, rules):
        q=Fraction(D(plan['quantity']));e=Fraction(D(plan['entry']))
        s=Fraction(D(plan['sl']));tp=Fraction(D(plan['tp']))
        reserve=Fraction(EXIT_SLIPPAGE_RATE);fee=Fraction(rules.taker_fee_rate)
        sign=-1 if plan['side']=='LONG' else 1
        adverse_sl=s*(1+sign*reserve);adverse_tp=tp*(1+sign*reserve)
        loss=abs(e-adverse_sl)+fee*(e+adverse_sl)
        reward=abs(adverse_tp-e)-fee*(e+adverse_tp)
        self.assertEqual(Fraction(D(plan['risk'])),q*loss)
        self.assertEqual(Fraction(D(plan['net_reward'])),q*reward)
        self.assertLessEqual(q*loss,Fraction(D(plan['risk_target_usdt'])))
        if q+Fraction(rules.step)<=Fraction(rules.maximum):
            self.assertGreater((q+Fraction(rules.step))*loss,Fraction(D(plan['risk_target_usdt'])))
        self.assertGreaterEqual(reward,2*loss)
        closer=tp-Fraction(rules.tick) if plan['side']=='LONG' else tp+Fraction(rules.tick)
        closer_fill=closer*(1+sign*reserve)
        closer_reward=abs(closer_fill-e)-fee*(e+closer_fill)
        self.assertLess(closer_reward,2*loss)

    def test_far_model_tp_is_normalized_both_sides_without_altering_original(self):
        for side,original,target,quantity in (
            ('LONG','2730','2707.23','0.123'),('SHORT','2490','2513.25','0.122')):
            with self.subTest(side=side):
                rules=self.rules();plan=self.plan(side,rules=rules)
                self.assertEqual(plan['tp'],target)
                self.assertEqual(plan['quantity'],quantity)
                self.assertEqual(plan['tp_normalization']['model_tp'],original)
                self.assertEqual(plan['tp_normalization']['model_gross_rr'],'4.8')
                self.assertEqual(plan['risk_model'],RISK_MODEL)
                self.assert_exact_budget_and_target(plan,rules)
                validate_normalized_plan(plan,rules)
                preflight(plan,rules)

    def test_model_gross_rr_exactly_two_is_accepted_then_normalized(self):
        for side,tp in (('LONG','2660'),('SHORT','2560')):
            with self.subTest(side=side):
                rules=self.rules();plan=self.plan(side,tp=tp,rules=rules)
                self.assertEqual(plan['tp_normalization']['model_gross_rr'],'2')
                self.assertNotEqual(plan['tp'],tp)
                self.assert_exact_budget_and_target(plan,rules)

    def test_model_gross_rr_below_two_stays_rejected(self):
        for side,tp in (('LONG','2659.99'),('SHORT','2560.01')):
            with self.subTest(side=side),self.assertRaisesRegex(Review,'RISK_REWARD_BELOW_2'):
                self.plan(side,tp=tp)

    def test_model_tp_off_tick_is_audit_only_and_execution_prices_remain_legal(self):
        rules=self.rules();plan=self.plan(tp='2660.005',rules=rules)
        self.assertEqual(plan['tp_normalization']['model_tp'],'2660.005')
        for key in ('entry','sl','tp'):
            self.assertEqual((Fraction(D(plan[key]))/Fraction(rules.tick)).denominator,1)

    def test_adverse_reserve_and_exit_fee_use_side_aware_fill_price(self):
        for side in ('LONG','SHORT'):
            with self.subTest(side=side):
                rules=self.rules();plan=self.plan(side,rules=rules)
                q=Fraction(D(plan['quantity']))
                for leg in ('sl','tp'):
                    trigger=Fraction(D(plan[leg]));fill=Fraction(D(plan[leg+'_execution_price']))
                    expected=trigger*(1-Fraction(EXIT_SLIPPAGE_RATE) if side=='LONG' else 1+Fraction(EXIT_SLIPPAGE_RATE))
                    self.assertEqual(fill,expected)
                    self.assertEqual(Fraction(D(plan[leg+'_slippage_usdt'])),q*trigger*Fraction(EXIT_SLIPPAGE_RATE))
                    self.assertEqual(Fraction(D(plan[leg+'_exit_fee_usdt'])),q*fill*Fraction(rules.taker_fee_rate))
                self.assertEqual(plan['entry_slippage_rate'],'0')
                self.assertNotIn('SLIPPAGE',plan['excluded_costs'])
                self.assertIn('GAPS_BEYOND_RESERVE',plan['excluded_costs'])

    def test_exact_results_do_not_depend_on_callers_decimal_precision(self):
        rules=self.rules()
        expected=self.plan(rules=rules)
        for precision in (6,10,28):
            with self.subTest(precision=precision),localcontext() as context:
                context.prec=precision
                actual=self.plan(rules=rules)
                self.assertEqual(actual,expected)
                self.assert_exact_budget_and_target(actual,rules)

    def test_configured_risk_target_resizes_maximum_lot_and_is_bound_at_preflight(self):
        rules=self.rules();five=self.plan(rules=rules);seven=self.plan(rules=rules,target='7')
        self.assertGreater(D(seven['quantity']),D(five['quantity']))
        self.assertEqual(seven['tp'],five['tp'])
        self.assert_exact_budget_and_target(seven,rules)
        preflight(seven,rules,D('7'))
        with self.assertRaisesRegex(Review,'ORDER_COSTS_CHANGED'):
            preflight(seven,rules,D('5'))

    def test_current_fee_quote_change_rejects_execution_instead_of_resizing(self):
        rules=self.rules();plan=self.plan(rules=rules)
        newer=replace(rules,fee_observed_at=time.time(),observed_at=time.time())
        preflight(plan,newer)
        with self.assertRaises(Review):
            preflight(plan,replace(newer,taker_fee_rate=D('.0006')))
        with self.assertRaisesRegex(Review,'STALE_FEE_EVIDENCE'):
            self.plan(rules=replace(rules,fee_observed_at=time.time()-301))

    def test_reserved_budget_rejects_market_minimum_when_no_legal_lot_fits(self):
        rules=self.rules(minimum=D('.181'))
        with self.assertRaisesRegex(Review,'NO_LEGAL_MAX_RISK_QUANTITY'):
            self.plan(rules=rules)

    def test_exchange_maximum_can_cap_quantity_below_budget(self):
        rules=self.rules(maximum=D('.05'));plan=self.plan(rules=rules)
        self.assertEqual(D(plan['quantity']),D('.05'))
        self.assertLess(D(plan['risk']),D('5'))

    def test_no_legal_normalized_tp_is_rejected_without_changing_entry_or_sl(self):
        with self.assertRaisesRegex(Review,'NO_LEGAL_NET_RR_TP'):
            self.plan(rules=self.rules(max_price=D('2700')))
        signal=Signal('ETHUSDT','SHORT',D('1'),D('.01'),D('1.49'))
        with self.assertRaisesRegex(Review,'INVALID_ENTRY_TP_SL'):
            risk_check(signal,self.rules(tick=D('.01')),risk_model=RISK_MODEL)

    def test_cost_normalization_or_reserve_tampering_is_rejected(self):
        rules=self.rules();original=self.plan(rules=rules)
        for key,value in (('tp','2730'),('quantity','.181'),('exit_slippage_rate','.004'),
                          ('sl_slippage_usdt','1'),('risk_model','V2'),('entry_slippage_rate','.001')):
            plan=copy.deepcopy(original);plan[key]=value
            with self.subTest(field=key),self.assertRaises(Review):
                validate_normalized_plan(plan,rules)
        plan=copy.deepcopy(original);plan['cost_evidence']['taker_fee_rate']='.0001'
        with self.assertRaises(Review):validate_normalized_plan(plan,rules)

    def test_legacy_model_keeps_high_tp_and_original_fee_only_quantity(self):
        rules=self.rules();signal=Signal('ETHUSDT','LONG',D('2610'),D('2730'),D('2585'))
        plan=risk_check(signal,rules)
        self.assertEqual(plan['tp'],'2730')
        self.assertEqual(D(plan['quantity']),D('.181'))
        self.assertNotIn('risk_model',plan)
        self.assertEqual(maximum_risk_quantity(signal.entry,signal.sl,rules),D('.181'))
        self.assertIn('SLIPPAGE',plan['excluded_costs'])
        preflight(plan,rules)


if __name__=='__main__':unittest.main()
