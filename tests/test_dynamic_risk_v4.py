import copy
from dataclasses import asdict, replace, fields
from fractions import Fraction as F
import time
import unittest
from unittest.mock import patch

from worker.core import (D, Review, Signal, Rules, DynamicRules, FEE_SOURCE,
    RISK_MODEL, DYNAMIC_RISK_MODEL, DYNAMIC_REWARD_RISK_POLICY,
    risk_check, maximum_risk_quantity, preflight, validate_normalized_plan, verified_rr)
from worker.dynamic_risk import (COST_SOURCE, BRACKET_SOURCE, costs, estimate_existing,
    leverage_for, market_evidence, book_impact, MODEL)


class DynamicOwnerRiskTests(unittest.TestCase):
    def rules(self, **changes):
        at = time.time()
        price = D('100')
        evidence = dict(schema_version=1, symbol='ETHUSDT', source=COST_SOURCE,
            observed_at=at, bids=[['99.99','2'],['99.9','8'],['99.5','1000']],
            asks=[['100.01','2'],['100.1','8'],['100.5','1000']],
            price_moves=[dict(start=at-(30-i)*900,end=at-(29-i)*900,
                reference='100',high=str(price+D(i+1)/D('10000')),
                low=str(price-D(i+1)/D('10000')))for i in range(29)],
            execution_samples=[])
        brackets=dict(schema_version=1,symbol='ETHUSDT',source=BRACKET_SOURCE,
            observed_at=at,brackets=[dict(notional_floor='0',notional_cap='10000',max_leverage=125),
                dict(notional_floor='10000',notional_cap='10000000',max_leverage=20)])
        base=DynamicRules(D('.01'),D('.01'),D('1000'),D('.01'),D('1'),at,
            taker_fee_rate=D('.0005'),fee_observed_at=at,fee_symbol='ETHUSDT',fee_source=FEE_SOURCE,
            dynamic_cost_evidence=evidence,leverage_brackets=brackets)
        return replace(base,**changes)

    def plan(self,side='LONG',target='5',tp=None,rules=None):
        rules=rules or self.rules()
        signal=Signal('ETHUSDT',side,D('100'),D(tp or('100.5'if side=='LONG'else'99.5')),
            D('99'if side=='LONG'else'101'))
        return risk_check(signal,rules,D(target),risk_model=DYNAMIC_RISK_MODEL,
            reward_risk_policy=DYNAMIC_REWARD_RISK_POLICY)

    def test_nominal_office_r_is_per_trade_and_tp_uses_twice_budget_after_quantity(self):
        for side in ('LONG','SHORT'):
            for target in ('5','3'):
                with self.subTest(side=side,target=target):
                    rules=self.rules();plan=self.plan(side,target,rules=rules)
                    self.assertLessEqual(D(plan['risk']),D(target))
                    self.assertEqual(plan['target_net_reward_usdt'],str(D(target)*2))
                    self.assertEqual(plan['neurobro_tp'],'100.5'if side=='LONG'else'99.5')
                    self.assertEqual(plan['entry'],'100')
                    self.assertEqual(plan['sl'],'99'if side=='LONG'else'101')
                    q=D(plan['quantity']);self.assertEqual(q,maximum_risk_quantity(
                        D('100'),D(plan['sl']),rules,D(target),risk_model=MODEL,side=side))
                    higher=costs(D('100'),D(plan['tp']),D(plan['sl']),q+rules.step,rules,side)
                    self.assertGreater(D(higher['risk']),D(target))
                    # Nearest legal TP minimizes nominal USDT deviation, not 2x
                    # rounded-down actual SL risk, and both neighbors are tested.
                    difference=abs(D(plan['net_reward'])-D(target)*2)
                    for other in (D(plan['tp'])-rules.tick,D(plan['tp'])+rules.tick):
                        if other<=100 and side=='LONG'or other>=100 and side=='SHORT':continue
                        neighbour=costs(D('100'),other,D(plan['sl']),q,rules,side)
                        self.assertLessEqual(difference,abs(D(neighbour['net_reward'])-D(target)*2))
                    self.assertEqual(D(plan['net_reward_target_difference_usdt']),D(plan['net_reward'])-D(target)*2)
                    validate_normalized_plan(plan,rules);preflight(plan,rules,D(target));verified_rr(plan)

    def test_quantity_is_independent_of_leverage_and_notional_selects_actual_bracket(self):
        rules=self.rules();original=self.plan(rules=rules)
        for maximum in (75,50,20,150):
            changed=copy.deepcopy(rules.leverage_brackets)
            changed['brackets'][0]['max_leverage']=maximum
            p=self.plan(rules=replace(rules,leverage_brackets=changed))
            self.assertEqual(p['quantity'],original['quantity'])
            self.assertEqual(p['leverage'],min(75,maximum))
            self.assertEqual(p['margin_mode'],'CROSS')
        self.assertEqual(leverage_for(F(10000),rules,'ETHUSDT')[0],20)
        changed=copy.deepcopy(rules.leverage_brackets);changed['account_max_leverage']=10
        self.assertEqual(self.plan(rules=replace(rules,leverage_brackets=changed))['leverage'],10)

    def test_calculation_does_not_depend_on_callers_decimal_precision(self):
        from decimal import localcontext
        rules=self.rules();expected=self.plan(rules=rules)
        with localcontext() as context:
            context.prec=3
            actual=self.plan(rules=rules)
            validate_normalized_plan(actual,rules)
        self.assertEqual(actual,expected)

    def test_close_side_uses_correct_book_and_adverse_price_direction(self):
        rules=self.rules();long=self.plan('LONG',rules=rules);short=self.plan('SHORT',rules=rules)
        changed=copy.deepcopy(rules.dynamic_cost_evidence)
        changed['asks']=[['100.5','1000']]
        # Worsening BUY depth affects short exits; it also moves midpoint,
        # so compare the independently observed side-sensitive book impact.
        e=market_evidence(replace(rules,dynamic_cost_evidence=changed),'ETHUSDT','SHORT')
        self.assertGreater(book_impact(F(3),e)[0],
            book_impact(F(3),market_evidence(rules,'ETHUSDT','SHORT'))[0])
        changed=copy.deepcopy(rules.dynamic_cost_evidence)
        for row in changed['price_moves']:row['low']='90'
        altered=replace(rules,dynamic_cost_evidence=changed)
        self.assertLess(D(self.plan('LONG',rules=altered)['quantity']),D(long['quantity']))
        self.assertEqual(self.plan('SHORT',rules=altered)['quantity'],short['quantity'])

    def test_cost_components_sum_exactly_once_and_entry_does_not_assume_maker(self):
        for side in ('LONG','SHORT'):
            p=self.plan(side)
            total=sum((D(p[k])for k in ('gross_risk','entry_fee_usdt','sl_exit_fee_usdt',
                'sl_book_impact_usdt','sl_liquidity_uncertainty_usdt')),D(0))
            self.assertEqual(total,D(p['risk']))
            self.assertEqual(D(p['entry_fee_usdt']),D(p['quantity'])*D('100')*D('.0005'))
            net=D(p['gross_reward'])-sum((D(p[k])for k in('entry_fee_usdt','tp_exit_fee_usdt',
                'tp_book_impact_usdt','tp_liquidity_uncertainty_usdt')),D(0))
            self.assertEqual(net,D(p['net_reward']))
            self.assertEqual(p['cost_evidence']['closing_side'],'SELL'if side=='LONG'else'BUY')

    def test_dynamic_book_size_volatility_and_mature_execution_history_change_estimate(self):
        rules=self.rules();e=market_evidence(rules,'ETHUSDT','LONG')
        self.assertGreater(book_impact(F(3),e)[0],book_impact(F(1),e)[0])
        original=self.plan(rules=rules)
        changed=copy.deepcopy(rules.dynamic_cost_evidence)
        for row in changed['price_moves']:row['low']='95'
        volatile=self.plan(rules=replace(rules,dynamic_cost_evidence=changed))
        self.assertLess(D(volatile['quantity']),D(original['quantity']))
        changed=copy.deepcopy(rules.dynamic_cost_evidence)
        sample=dict(order_id='order-0',execution_id='fill-0',observed_at=changed['observed_at'],side='SELL',expected_impact_rate='.001',actual_impact_rate='.1')
        changed['execution_samples']=[sample]
        one=self.plan(rules=replace(rules,dynamic_cost_evidence=changed))
        self.assertEqual(one['quantity'],original['quantity'])
        changed['execution_samples']=[dict(sample,order_id='order-'+str(i),execution_id='fill-'+str(i),observed_at=sample['observed_at']-i)for i in range(20)]
        mature=self.plan(rules=replace(rules,dynamic_cost_evidence=changed))
        self.assertLess(D(mature['quantity']),D(original['quantity']))
        self.assertEqual(mature['cost_evidence']['execution_excess_q95_rate'],'0.099')

    def test_unavailable_or_stale_data_is_technical_error_not_hold_or_static_fallback(self):
        rules=self.rules()
        for change,code in ((dict(dynamic_cost_evidence=None),'SLIPPAGE_EVIDENCE_UNAVAILABLE'),
                (dict(leverage_brackets=None),'LEVERAGE_EVIDENCE_UNAVAILABLE')):
            with self.assertRaisesRegex(Review,code):self.plan(rules=replace(rules,**change))
        changed=copy.deepcopy(rules.dynamic_cost_evidence);changed['observed_at']-=301
        with self.assertRaisesRegex(Review,'STALE_SLIPPAGE_EVIDENCE'):
            self.plan(rules=replace(rules,dynamic_cost_evidence=changed))
        changed=copy.deepcopy(rules.dynamic_cost_evidence);changed['bids']=[['99.99','0.01']]
        with self.assertRaisesRegex(Review,'SLIPPAGE_DEPTH_INSUFFICIENT'):
            self.plan(rules=replace(rules,dynamic_cost_evidence=changed))

    def test_tampering_rejected_but_estimate_existing_does_not_change_old_order(self):
        rules=self.rules();p=self.plan(rules=rules)
        for key,value in (('quantity','9'),('entry','101'),('sl','98'),('tp','110'),('leverage',50),
                ('net_reward_target_difference_usdt','0'),('target_net_reward_usdt','9')):
            bad=copy.deepcopy(p);bad[key]=value
            with self.subTest(key=key),self.assertRaises(Review):preflight(bad,rules)
        old=copy.deepcopy(p);fresh=copy.deepcopy(rules.dynamic_cost_evidence)
        for row in fresh['price_moves']:row['low']='90'
        estimate=estimate_existing(p,replace(rules,dynamic_cost_evidence=fresh))
        self.assertEqual(p,old)
        self.assertFalse(estimate['order_parameters_changed'])
        self.assertGreater(D(estimate['estimated_total_risk']),D(p['risk']))

    def test_frozen_rule_round_trip_uses_exact_legacy_or_v4_schema(self):
        import json
        from worker.core import deserialize_sizing_rules
        rules=self.rules();p=self.plan(rules=rules)
        p['sizing_rules']=json.loads(json.dumps(asdict(rules),default=str))
        round_trip=deserialize_sizing_rules(p)
        self.assertIsInstance(round_trip,DynamicRules)
        validate_normalized_plan(p,round_trip)
        legacy=Rules(**{f.name:getattr(rules,f.name)for f in fields(Rules)})
        saved={'risk_model':RISK_MODEL,'sizing_rules':json.loads(json.dumps(asdict(legacy),default=str))}
        self.assertIs(type(deserialize_sizing_rules(saved)),Rules)
        saved['sizing_rules']['dynamic_cost_evidence']={}
        with self.assertRaisesRegex(Review,'INVALID_RULES_SCHEMA'):deserialize_sizing_rules(saved)
        p['sizing_rules'].pop('dynamic_cost_evidence')
        with self.assertRaisesRegex(Review,'INVALID_RULES_SCHEMA'):deserialize_sizing_rules(p)

    def test_partial_fills_from_one_order_do_not_count_as_independent_execution_history(self):
        rules=self.rules();original=self.plan(rules=rules)
        changed=copy.deepcopy(rules.dynamic_cost_evidence)
        changed['execution_samples']=[dict(order_id='one-order',execution_id='fill-'+str(i),
            observed_at=changed['observed_at']-i,side='SELL',expected_impact_rate='0.001',
            actual_impact_rate='0.1')for i in range(20)]
        plan=self.plan(rules=replace(rules,dynamic_cost_evidence=changed))
        self.assertEqual(plan['quantity'],original['quantity'])
        self.assertEqual(plan['cost_evidence']['execution_sample_count'],1)
        self.assertEqual(plan['cost_evidence']['execution_excess_q95_rate'],'0')

    def test_v3_and_original_rules_field_contract_remain_unchanged(self):
        self.assertNotIn('dynamic_cost_evidence',{f.name for f in fields(Rules)})
        dynamic=self.rules()
        legacy=Rules(**{f.name:getattr(dynamic,f.name)for f in fields(Rules)})
        signal=Signal('ETHUSDT','LONG',D('100'),D('104'),D('99'))
        p=risk_check(signal,legacy,risk_model=RISK_MODEL)
        original=copy.deepcopy(p);preflight(p,legacy)
        self.assertEqual(p,original);self.assertEqual(p['leverage'],75)
        self.assertEqual(p['exit_slippage_rate'],'0.005')
        self.assertNotIn('policy_version',p)


if __name__=='__main__':unittest.main()
