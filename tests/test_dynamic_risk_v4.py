import copy
import json
from dataclasses import asdict, replace, fields
from fractions import Fraction as F
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from worker.core import (D, Review, Signal, Rules, DynamicRules, FEE_SOURCE,
    RISK_MODEL, DYNAMIC_RISK_MODEL, DYNAMIC_REWARD_RISK_POLICY,
    risk_check, maximum_risk_quantity, preflight, validate_normalized_plan, verified_rr)
from worker.dynamic_risk import (COST_SOURCE, BRACKET_SOURCE, costs, estimate_existing,
    leverage_for, market_evidence, book_impact, MODEL, plan as dynamic_plan)


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

    def margin_rules(self, available='1000', brackets=None, **changes):
        rules=self.rules(**changes)
        account=copy.deepcopy(rules.leverage_brackets)
        account.update(schema_version=2,margin=dict(
            source='BINANCE_FUTURES_ACCOUNT_AVAILABLE_BALANCE',asset='USDT',
            observed_at=account['observed_at'],available_balance_usdt=available))
        if brackets is not None:account['brackets']=brackets
        return replace(rules,leverage_brackets=account)

    def test_margin_limited_quantity_is_largest_legal_lot_and_tp_keeps_nominal_target(self):
        for side in ('LONG','SHORT'):
            with self.subTest(side=side):
                rules=self.margin_rules('2');p=self.plan(side,rules=rules)
                q=F(D(p['quantity']));entry=F(D(p['entry']));fee=F(rules.taker_fee_rate)
                required=lambda amount:amount*entry/F(p['leverage'])+amount*entry*fee
                self.assertLessEqual(required(q),F(2))
                self.assertGreater(required(q+F(rules.step)),F(2))
                self.assertLess(D(p['risk']),D('5'))
                self.assertEqual(p['target_net_reward_usdt'],'10')
                self.assertEqual((p['entry'],p['sl']),('100','99'if side=='LONG'else'101'))
                validate_normalized_plan(p,rules);preflight(p,rules)

    def test_account_notional_cap_and_tier_margin_choose_global_maximum(self):
        for tiers,available,expected in (([
                dict(notional_floor='0',notional_cap='200',max_leverage=75)],'1000','1.99'),
                ([dict(notional_floor='0',notional_cap='200',max_leverage=75),
                dict(notional_floor='200',notional_cap='100000',max_leverage=20)],'5','1.99'),
                ([dict(notional_floor='0',notional_cap='100',max_leverage=1),
                dict(notional_floor='100',notional_cap='100000',max_leverage=75)],'2','1.44')):
            with self.subTest(tiers=tiers,available=available):
                rules=self.margin_rules(available,brackets=tiers)
                p=self.plan(rules=rules)
                self.assertEqual(p['quantity'],expected)
                self.assertLessEqual(D(p['risk']),D('5'))
                validate_normalized_plan(p,rules)

    def test_new_margin_evidence_is_required_fresh_and_can_prevent_a_legal_order(self):
        rules=self.margin_rules('0')
        with self.assertRaisesRegex(Review,'NO_LEGAL_MAX_RISK_QUANTITY'):
            self.plan(rules=rules)
        rules=self.margin_rules('2',min_notional=D('200'))
        with self.assertRaisesRegex(Review,'NO_LEGAL_MAX_RISK_QUANTITY'):
            self.plan(rules=rules)
        rules=self.margin_rules('2');bad=copy.deepcopy(rules.leverage_brackets)
        bad['margin']['observed_at']-=301
        with self.assertRaisesRegex(Review,'STALE_MARGIN_EVIDENCE'):
            self.plan(rules=replace(rules,leverage_brackets=bad))
        bad=copy.deepcopy(rules.leverage_brackets);bad.pop('margin')
        with self.assertRaisesRegex(Review,'MARGIN_EVIDENCE_UNAVAILABLE'):
            self.plan(rules=replace(rules,leverage_brackets=bad))

    def test_account_leverage_cap_limits_quantity_using_fees_and_exact_margin(self):
        rules=self.margin_rules('2');account=copy.deepcopy(rules.leverage_brackets)
        account['account_max_leverage']=10
        p=self.plan(rules=replace(rules,leverage_brackets=account))
        self.assertEqual(p['leverage'],10)
        self.assertEqual(p['quantity'],'0.19')

    def test_missing_depth_only_blocks_if_a_larger_account_legal_lot_could_fit_risk(self):
        tiers=[dict(notional_floor='0',notional_cap='100',max_leverage=75),
            dict(notional_floor='100',notional_cap='200',max_leverage=1),
            dict(notional_floor='200',notional_cap='100000',max_leverage=75)]
        rules=self.margin_rules('3',brackets=tiers)
        evidence=copy.deepcopy(rules.dynamic_cost_evidence)
        evidence['bids']=[['99.99','1.5']]
        rules=replace(rules,dynamic_cost_evidence=evidence)
        p=risk_check(Signal('ETHUSDT','LONG',D('100'),D('100.5'),D('97')),rules,
            risk_model=MODEL)
        # Every legal lot above .99 is at least 2 units, whose price loss alone
        # exceeds R. The observed book therefore proves .99 is the legal maximum.
        self.assertEqual(p['quantity'],'0.99')
        with self.assertRaisesRegex(Review,'SLIPPAGE_DEPTH_INSUFFICIENT'):
            self.plan(rules=rules)

    def test_legacy_v4_plan_uses_its_frozen_schema_without_new_margin_arithmetic(self):
        rules=self.rules();old=self.plan(rules=rules);frozen=copy.deepcopy(old)
        constrained=self.plan(rules=self.margin_rules('2'))
        self.assertGreater(D(old['quantity']),D(constrained['quantity']))
        validate_normalized_plan(old,rules);preflight(old,rules)
        self.assertEqual(old,frozen)

    def test_new_margin_policy_normalizes_both_above_and_below_neurobro_tp(self):
        rules=self.margin_rules()
        for side,tps in (('LONG',('100.01','120')),('SHORT',('99.99','80'))):
            plans=[self.plan(side,tp=tp,rules=rules)for tp in tps]
            self.assertEqual(plans[0]['quantity'],plans[1]['quantity'])
            self.assertEqual(plans[0]['tp'],plans[1]['tp'])
            self.assertEqual([p['neurobro_tp']for p in plans],list(tps))

    def test_observed_inj_maximum_lot_and_cost_components_remain_unchanged(self):
        fixture=json.loads((Path(__file__).parent/'fixtures'/'inj_v4_risk_summary_20261105.json').read_text())
        summary=fixture['market_summary'];observed=fixture['observed_plan']
        book={side:[tuple(F(D(v))for v in row)for row in summary[side+'_first_two']]
            for side in('bids','asks')}
        evidence=dict(raw=summary,book=book,closing_side='SELL',
            midpoint=(book['bids'][0][0]+book['asks'][0][0])/2,
            uncertainty=F(D(summary['future_liquidity_uncertainty_rate'])),
            move_reserve=F(D(summary['price_move_q95_rate'])),execution_reserve=F(0),
            price_move_count=200,execution_sample_count=0,horizons=[900.0])
        brackets=dict(schema_version=1,symbol='INJUSDT',source=BRACKET_SOURCE,
            observed_at=fixture['bracket_observed_at'],brackets=[
                dict(notional_floor='0',notional_cap='10000',max_leverage=75)])
        rules=DynamicRules(D('.1'),D('.1'),D('500000'),D('.001'),D('5'),
            fixture['rules_observed_at'],min_price=D('.001'),max_price=D('2000'),
            taker_fee_rate=D(fixture['taker_fee_rate']),fee_observed_at=fixture['fee_observed_at'],
            fee_symbol='INJUSDT',fee_source=FEE_SOURCE,dynamic_cost_evidence=summary,
            leverage_brackets=brackets)
        model=fixture['signal']
        signal=Signal(model['symbol'],model['side'],D(model['entry']),D(model['neurobro_tp']),D(model['sl']))
        # Raw 200-candle history is unavailable in the sanitized report. Inject
        # its observed derived summary; do not fabricate candles to pass parsing.
        with patch('worker.dynamic_risk.market_evidence',return_value=evidence):
            original=dynamic_plan(signal,rules,D('5'),check_fresh=False)
            self.assertEqual((original['entry'],original['sl'],original['neurobro_tp']),
                ('8.26','8.18','8.48'))
            self.assertEqual((original['quantity'],original['tp']),('27.8','8.726'))
            for key,value in observed.items():
                self.assertLessEqual(abs(D(original[key])-D(value)),D('0.000000000000001'))
            self.assertEqual(original['risk'],fixture['recomputed_exact']['risk'])
            self.assertEqual(original['net_reward'],fixture['recomputed_exact']['net_reward'])
            next_lot=costs(signal.entry,D(original['tp']),signal.sl,D('27.9'),rules,
                signal.side,check_fresh=False)
            self.assertEqual(next_lot['risk'],fixture['recomputed_exact']['next_lot_risk'])
            self.assertLess(D(original['risk']),D('5'))
            self.assertGreater(D(next_lot['risk']),D('5'))
            frozen=copy.deepcopy(original)
            validate_normalized_plan(original,rules)
            margin=copy.deepcopy(brackets)
            margin.update(schema_version=2,margin=dict(
                source='BINANCE_FUTURES_ACCOUNT_AVAILABLE_BALANCE',asset='USDT',
                observed_at=fixture['bracket_observed_at'],available_balance_usdt='1000'))
            new=dynamic_plan(signal,replace(rules,leverage_brackets=margin),D('5'),check_fresh=False)
            for key in('entry','sl','tp','quantity','risk','net_reward','leverage'):
                self.assertEqual(new[key],original[key])
            self.assertEqual(original,frozen)

    def test_calculation_does_not_depend_on_callers_decimal_precision(self):
        from decimal import localcontext
        for rules in (self.rules(),self.margin_rules('2')):
            expected=self.plan(rules=rules)
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
