"""Offline integration contracts; fake market/account and isolated SQLite only."""
import copy
from dataclasses import asdict, fields
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from worker.analysis import analysis_context
from worker.core import (D, Rules, DynamicRules, Signal, Review, FEE_SOURCE,
    DYNAMIC_RISK_MODEL, DYNAMIC_REWARD_RISK_POLICY, risk_check, preflight,
    validate_normalized_plan)
from worker.dynamic_market import (record_execution, execution_samples, dynamic_rules,
    bracket_evidence, closed_moves)
from worker.dynamic_risk import market_evidence
from worker.new_setup_policy import refresh_unsubmitted, serialize_rules, request_policy
from worker.order_gateway import build_intent
from worker.robot_provenance import stamp, verify


class FakeAccount:
    def __init__(self, at):
        self.at=at;self.calls=[];self.taker='0.0005'
    def commission_rate(self,symbol):
        self.calls.append(('commission',symbol))
        return dict(source=FEE_SOURCE,symbol=symbol,maker='0.0002',taker=self.taker,
            checked_at=datetime.fromtimestamp(self.at,timezone.utc).isoformat(),
            checked_at_ms=self.at*1000)
    def signed_get(self,path,symbol):
        self.calls.append(('signed_get',path,symbol))
        assert path=='/fapi/v1/leverageBracket'
        return [dict(symbol=symbol,brackets=[dict(notionalFloor='0',notionalCap='10000',
            initialLeverage=50),dict(notionalFloor='10000',notionalCap='10000000',initialLeverage=20)])]


class FakeMarket:
    def __init__(self,at):
        self.at=at;self.depth_calls=0;self.fresh_calls=0
        self.book=dict(source='BINANCE_FUTURES_PUBLIC_DEPTH',symbol='ETHUSDT',observed_at=at,
            last_update_id=10,bids=[['99.99','1000']],asks=[['100.01','1000']])
        end=at//900*900
        candles=[]
        for i in range(200):
            start=(end-(200-i)*900)*1000
            candles.append(dict(open_time=start,close_time=start+900000-1,
                open='100',high='100.001',low='99.999',close='100',volume='1'))
        candles.append(dict(open_time=end*1000,close_time=(end+900)*1000-1,
            open='100',high='101',low='99',close='100',volume='1'))
        self.context=dict(source='Binance Futures',symbol='ETHUSDT',fetched_at=at,
            server_time=at*1000,timeframes={tf:dict(symbol='ETHUSDT',timeframe=tf,
            candles=copy.deepcopy(candles))for tf in('1h','15m')})
    def data(self,symbol):
        assert symbol=='ETHUSDT';return copy.deepcopy(self.context)
    def rules(self,symbol):
        assert symbol=='ETHUSDT'
        return Rules(D('0.01'),D('0.01'),D('1000'),D('0.01'),D('1'),self.at)
    def depth(self,symbol):
        assert symbol=='ETHUSDT';self.depth_calls+=1;return copy.deepcopy(self.book)
    def fresh(self,context,symbol):
        self.fresh_calls+=1
        assert context['symbol']==symbol and context['fetched_at']==self.at


class DynamicMarketPolicyTests(unittest.TestCase):
    def setUp(self):
        self.at=int(time.time());self.market=FakeMarket(self.at);self.account=FakeAccount(self.at)
        self.db=sqlite3.connect(':memory:',isolation_level=None);self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)

    def prepare(self):
        return analysis_context(self.market,'ETHUSDT','5',self.account,
            policy=DYNAMIC_RISK_MODEL,db=self.db)

    def test_analysis_binds_fresh_account_fees_depth_brackets_and_closed_utc_values(self):
        original=copy.deepcopy(self.market.context)
        context,rules=analysis_context(self.market,'ETHUSDT','3',self.account,
            policy=DYNAMIC_RISK_MODEL,db=self.db)
        self.assertIsInstance(rules,DynamicRules)
        self.assertEqual(rules.taker_fee_rate,D('0.0005'))
        self.assertEqual(rules.fee_observed_at,self.at)
        self.assertEqual(rules.dynamic_cost_evidence['bids'],self.market.book['bids'])
        self.assertEqual(rules.dynamic_cost_evidence['observed_at'],self.at)
        self.assertEqual(rules.leverage_brackets['brackets'][0]['max_leverage'],50)
        self.assertEqual(self.account.calls,[('commission','ETHUSDT'),
            ('signed_get','/fapi/v1/leverageBracket','ETHUSDT')])
        constraint=context['risk_constraints']
        self.assertEqual(constraint['risk_budget_scope'],'PER_TRANSACTION')
        self.assertEqual(constraint['target_net_profit_at_tp_usdt'],'6')
        self.assertEqual(constraint['leverage_ceiling'],75)
        self.assertFalse(constraint['tp_requires_neurobro_reapproval'])
        self.assertNotIn('exit_slippage_rate',constraint)
        moves=rules.dynamic_cost_evidence['price_moves']
        self.assertEqual(len(moves),200)
        self.assertEqual(moves[0]['start'],original['timeframes']['15m']['candles'][0]['open_time']/1000)
        self.assertEqual(moves[0]['end'],(original['timeframes']['15m']['candles'][0]['close_time']+1)/1000)
        self.assertEqual(moves[-1]['reference'],'100')
        self.assertEqual(self.market.context,original)
        self.assertGreater(self.market.fresh_calls,0)
        plan=risk_check(Signal('ETHUSDT','LONG',D('100'),D('100.5'),D('99')),rules,
            D('3'),risk_model=DYNAMIC_RISK_MODEL)
        self.assertEqual(plan['leverage'],50)
        self.assertEqual(plan['target_net_reward_usdt'],'6')

    def test_stale_fee_or_book_and_malformed_brackets_fail_as_technical_input(self):
        self.account.at-=301
        with self.assertRaisesRegex(Review,'STALE_FEE_EVIDENCE'):self.prepare()
        self.assertEqual(self.market.depth_calls,0)
        self.account.at=self.at;self.market.book['observed_at']-=301
        with self.assertRaisesRegex(Review,'STALE_SLIPPAGE_EVIDENCE'):self.prepare()
        broken=SimpleNamespace(signed_get=lambda *args:[dict(symbol='ETHUSDT',brackets=[{}])])
        with self.assertRaisesRegex(Review,'INVALID_LEVERAGE_BRACKETS'):
            bracket_evidence(broken,'ETHUSDT')

    def record(self,order='one-order',execution='fill-0',**changes):
        args=dict(symbol='ETHUSDT',side='SELL',order_id=order,execution_id=execution,
            filled_at=self.at-30,detected_at=self.at-29,reference_price='100',
            fill_price='99',quantity='0.1',expected_impact_rate='0.0001',
            fee_amount='0.01',fee_asset='USDT')
        args.update(changes);record_execution(self.db,**args)

    def test_execution_observations_are_idempotent_and_require_independent_orders(self):
        self.record();before=tuple(self.db.execute('SELECT * FROM execution_cost_observations').fetchone())
        self.record(detected_at=self.at-20)
        self.assertEqual(tuple(self.db.execute('SELECT * FROM execution_cost_observations').fetchone()),before)
        with self.assertRaisesRegex(Review,'EXECUTION_COST_OBSERVATION_CONFLICT'):
            self.record(fill_price='98')
        for i in range(1,20):self.record(execution='fill-'+str(i))
        _,rules=self.prepare()
        evidence=market_evidence(rules,'ETHUSDT','LONG')
        self.assertEqual(evidence['execution_sample_count'],1)
        self.assertEqual(evidence['execution_reserve'],0)
        for i in range(1,20):self.record(order='order-'+str(i),execution='other-'+str(i))
        _,rules=self.prepare();evidence=market_evidence(rules,'ETHUSDT','LONG')
        self.assertEqual(evidence['execution_sample_count'],20)
        self.assertGreater(evidence['execution_reserve'],0)
        self.assertEqual(len(execution_samples(self.db,'ETHUSDT')),39)

    def test_extreme_confirmed_fill_is_recorded_not_hidden_by_reserve_model_limits(self):
        self.record(side='BUY',reference_price='1',fill_price='3')
        row=self.db.execute('SELECT fill_price,reference_price,actual_impact_rate FROM execution_cost_observations').fetchone()
        self.assertEqual(tuple(row),('3','1','2'))

    def seed_candidate(self,state=None,result=None):
        self.db.executescript('''
        CREATE TABLE api_requests(operation TEXT PRIMARY KEY,output TEXT,state TEXT,body_hash TEXT);
        CREATE TABLE robot_candidates(id TEXT PRIMARY KEY,cycle TEXT,symbol TEXT,status TEXT,plan TEXT,failure_code TEXT);
        CREATE TABLE order_intents(id TEXT PRIMARY KEY,candidate_id TEXT,symbol TEXT,state TEXT,payload TEXT,result TEXT,failure_code TEXT,created TEXT,updated TEXT);
        CREATE TABLE research_browser_requests(operation TEXT PRIMARY KEY,request_body TEXT);
        ''')
        self.op='2026-11-01:robot-v9:0:analysis-v9:ETHUSDT'
        output=dict(symbol='ETHUSDT',side='LONG',limit_entry=100,take_profit=100.5,
            stop_loss=99,risk_reward=0.5)
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?)',
            (self.op,json.dumps(output),'COMPLETE',hashlib.sha256(b'fixture').hexdigest()))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?)',(self.op,
            json.dumps(dict(context=dict(risk_constraints=dict(risk_model=DYNAMIC_RISK_MODEL))))))
        _,rules=self.prepare()
        plan=risk_check(Signal('ETHUSDT','LONG',D('100'),D('100.5'),D('99')),rules,
            D('5'),risk_model=DYNAMIC_RISK_MODEL)
        plan['sizing_rules']=serialize_rules(rules)
        plan['protection_working_type']='CONTRACT_PRICE'
        stamp(self.db,plan,self.op);verify(self.db,plan,self.op)
        self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
            (self.op,'2026-11-01:robot-v9:0','ETHUSDT','READY_FOR_EXECUTION',json.dumps(plan),None))
        if state is not None:
            self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
                (self.op,self.op,'ETHUSDT',state,json.dumps(build_intent(plan,self.op)),result,None,'created','updated'))
        coordinator=SimpleNamespace(db=self.db,market=self.market,account_factory=lambda:self.account)
        row=self.db.execute('SELECT * FROM robot_candidates WHERE id=?',(self.op,)).fetchone()
        return coordinator,row,plan

    def frozen(self):
        return {table:[tuple(row)for row in self.db.execute('SELECT * FROM '+table)]
            for table in('api_requests','research_browser_requests','robot_candidates','order_intents')}

    def test_definitely_unsent_v4_recomputes_fresh_costs_without_duplicate_intents(self):
        coordinator,row,old=self.seed_candidate('READY_FOR_EXECUTION')
        old_intent=json.loads(self.db.execute('SELECT payload FROM order_intents').fetchone()[0])
        self.market.book['bids']=[['99.5','1000']]
        fresh,rules=refresh_unsubmitted(coordinator,row,old,build_intent=build_intent)
        self.assertLess(D(fresh['quantity']),D(old['quantity']))
        self.assertEqual((fresh['entry'],fresh['sl'],fresh['neurobro_tp']),
            (old['entry'],old['sl'],old['neurobro_tp']))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0],1)
        current=json.loads(self.db.execute('SELECT payload FROM order_intents').fetchone()[0])
        self.assertEqual(current['client_order_id'],old_intent['client_order_id'])
        self.assertNotEqual(current['entry']['quantity'],old_intent['entry']['quantity'])
        self.assertEqual(request_policy(self.db,self.op),DYNAMIC_RISK_MODEL)
        row=self.db.execute('SELECT * FROM robot_candidates WHERE id=?',(self.op,)).fetchone()
        again,_=refresh_unsubmitted(coordinator,row,fresh,build_intent=build_intent)
        for key in('entry','sl','tp','quantity','risk','target_net_reward_usdt','leverage'):
            self.assertEqual(again[key],fresh[key])
        self.assertGreaterEqual(again['leverage_evidence']['snapshot']['observed_at'],
            fresh['leverage_evidence']['snapshot']['observed_at'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM api_requests').fetchone()[0],1)

    def test_accepted_uncertain_result_or_mismatched_intent_is_frozen(self):
        for state,result in (('ENTRY_PENDING','{}'),('SUBMITTING',None),('NEEDS_REVIEW',None),
                ('POSITION_PROTECTED','{}'),('READY_FOR_EXECUTION','{}')):
            with self.subTest(state=state):
                self.db.close();self.db=sqlite3.connect(':memory:',isolation_level=None);self.db.row_factory=sqlite3.Row
                coordinator,row,plan=self.seed_candidate(state,result);before=self.frozen()
                with self.assertRaisesRegex(Review,'ACCEPTED_OR_UNCERTAIN_INTENT_IMMUTABLE'):
                    refresh_unsubmitted(coordinator,row,plan,build_intent=build_intent)
                self.assertEqual(self.frozen(),before)
        self.db.execute("UPDATE order_intents SET state='READY_FOR_EXECUTION',result=NULL,candidate_id='foreign'")
        before=self.frozen()
        with self.assertRaisesRegex(Review,'ORDER_EVIDENCE_UNVERIFIED'):
            refresh_unsubmitted(coordinator,row,plan,build_intent=build_intent)
        self.assertEqual(self.frozen(),before)

    def test_saved_request_policy_does_not_silently_reinterpret_unknown_version(self):
        self.seed_candidate()
        for marker in(None,'FEE_SLIPPAGE_RISK_V3'):
            body=dict(context=dict(risk_constraints={}if marker is None else dict(risk_model=marker)))
            self.db.execute('UPDATE research_browser_requests SET request_body=?',(json.dumps(body),))
            self.assertIsNone(request_policy(self.db,self.op))
        self.db.execute('UPDATE research_browser_requests SET request_body=?',
            (json.dumps(dict(context=dict(risk_constraints=dict(risk_model='UNRECOGNIZED_V5')))),))
        with self.assertRaisesRegex(Review,'INVALID_COST_MODEL'):request_policy(self.db,self.op)

    def test_legacy_plan_and_stale_unsent_plan_do_not_migrate(self):
        coordinator,row,plan=self.seed_candidate()
        legacy=dict(plan,risk_model='FEE_SLIPPAGE_RISK_V3')
        with patch('worker.new_setup_policy.analysis_context',side_effect=AssertionError('NO_REFRESH')):
            self.assertEqual(refresh_unsubmitted(coordinator,row,legacy,build_intent=build_intent),(legacy,None))
        before=self.frozen()
        with self.assertRaisesRegex(Review,'STALE_ORDER_INTENT'):
            refresh_unsubmitted(coordinator,row,plan,build_intent=build_intent,clock=lambda:self.at+301)
        self.assertEqual(self.frozen(),before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0],0)

    def test_race_to_uncertain_during_refresh_is_detected_before_plan_write(self):
        coordinator,row,plan=self.seed_candidate('READY_FOR_EXECUTION');before_plan=row['plan']
        normal=self.market.depth
        def race(symbol):
            self.db.execute("UPDATE order_intents SET state='SUBMITTING'")
            return normal(symbol)
        self.market.depth=race
        with self.assertRaisesRegex(Review,'ACCEPTED_OR_UNCERTAIN_INTENT_IMMUTABLE'):
            refresh_unsubmitted(coordinator,row,plan,build_intent=build_intent)
        self.assertEqual(self.db.execute('SELECT plan FROM robot_candidates').fetchone()[0],before_plan)
        self.assertEqual(self.db.execute('SELECT state FROM order_intents').fetchone()[0],'SUBMITTING')

    def test_accepted_v4_validation_is_mathematical_after_five_hours_and_next_day(self):
        _,_,plan=self.seed_candidate('ENTRY_PENDING','{}')
        from worker.core import deserialize_sizing_rules
        rules=deserialize_sizing_rules(plan)
        for elapsed in(5*3600,36*3600):
            with patch('time.time',return_value=self.at+elapsed):
                self.assertTrue(validate_normalized_plan(plan,rules,check_fresh=False))
                with self.assertRaises(Review):preflight(plan,rules,D('5'))


if __name__=='__main__':unittest.main()
