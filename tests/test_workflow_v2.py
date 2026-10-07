"""Offline workflow: declared RR, normalized TP, live pending slots and cached results."""
import copy
import json
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta

import test_order_pipeline as pipeline
from support import GOOD, hold
from worker.account_state import account_state, open_entry_symbols
from worker.analysis import analysis_context
from worker.core import D, Review, day, now
from worker.neuroapi import SETUP_SCHEMA, SCREEN_SCHEMA, SCREEN_ONE_SCHEMA, setup
from worker.prompts import ANALYSIS, REPLACEMENT_SCREENING
from worker.research_guard import gateway_mutations_allowed


def entry(symbol, *, algo=False, **changes):
    row = dict(symbol=symbol, positionSide='BOTH', side='BUY', reduceOnly=False,
               closePosition=False)
    row['algoStatus' if algo else 'status'] = 'NEW'
    row.update(changes)
    return row


class WorkflowV2Tests(unittest.TestCase):
    def fixture(self):
        f = pipeline.OrderPipelineTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.account.taker_fee = '0.0005'
        f.responses = {}
        original = f.transport
        def transport(method, url, headers, body, timeout):
            if body['output_schema'] in (SCREEN_SCHEMA, SCREEN_ONE_SCHEMA):
                return original(method, url, headers, body, timeout)
            f.calls.append(copy.deepcopy(body))
            context = json.loads(body['message_history'][0]['content'])
            symbol = context.get('symbol') or context['data']['symbol']
            value = copy.deepcopy(f.responses.get(symbol, {**GOOD, 'symbol': symbol}))
            return 200, {}, dict(mode='smart', answer=None, output=value)
        f.transport = transport
        f.make()
        return f

    def candidate(self, f, symbol='BTCUSDT'):
        return f.ledger.db.execute('SELECT * FROM robot_candidates WHERE symbol=?', (symbol,)).fetchone()

    def test_model_tp_is_preserved_but_new_order_tp_is_net_normalized_with_reserve(self):
        for model_tp, declared in ((104, 2), (110, 5), (104, 99)):
            with self.subTest(model_tp=model_tp, declared=declared):
                f = self.fixture()
                f.account.position('HYPEUSDT', '4.16')
                f.screens = [['BTCUSDT']]
                f.responses['BTCUSDT'] = {**GOOD, 'take_profit': model_tp, 'risk_reward': declared}
                f.on();f.robot.tick()
                self.assertEqual(f.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
                candidate = self.candidate(f)
                plan = json.loads(candidate['plan'])
                output = json.loads(f.ledger.db.execute('SELECT output FROM api_requests WHERE operation=?',
                    (candidate['id'],)).fetchone()[0], parse_float=D)
                self.assertEqual((output['take_profit'], output['risk_reward']), (D(model_tp), D(declared)))
                self.assertEqual(D(plan['tp_normalization']['model_tp']), D(model_tp))
                self.assertEqual((plan['entry'], plan['sl']), ('100', '98'))
                self.assertNotEqual(D(plan['tp']), D(model_tp))
                self.assertEqual(plan['risk_model'], 'FEE_SLIPPAGE_RISK_V3')
                self.assertEqual(plan['reward_risk_policy'], 'NET_1_TO_2_NORMALIZED_WITH_EXIT_RESERVE')
                self.assertLessEqual(D(plan['risk']), D('5'))
                self.assertGreaterEqual(D(plan['net_rr']), D('2'))
                self.assertEqual(plan['protection_working_type'], 'CONTRACT_PRICE')

    def test_declared_or_price_rr_below_two_is_known_rejection_and_one_replacement(self):
        for changes in (dict(risk_reward=D('1.99')), dict(take_profit=D('103.99'), risk_reward=99)):
            with self.subTest(changes=changes):
                f = self.fixture();f.account.position('HYPEUSDT', '4.16')
                f.screens = [['BTCUSDT'], ['ETHUSDT']]
                f.responses['BTCUSDT'] = {**GOOD, **changes}
                f.on();f.robot.tick()
                self.assertEqual(f.robot.tick()['failure_code'], 'RISK_REWARD_BELOW_2')
                row = self.candidate(f)
                self.assertEqual((row['status'], row['plan']), ('REJECTED', None))
                api = f.ledger.db.execute('SELECT * FROM api_requests WHERE operation=?', (row['id'],)).fetchone()
                self.assertEqual((api['state'], api['attempts']), ('COMPLETE', 1))
                self.assertEqual(json.loads(api['output'], parse_float=D)['risk_reward'], D(changes['risk_reward']))
                self.assertEqual(f.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
                self.assertEqual(f.calls[-1]['prompt'], REPLACEMENT_SCREENING.format(count=1))
                self.assertEqual(json.loads(f.calls[-1]['message_history'][0]['content'])['excluded_symbols'],
                                 ['BTCUSDT', 'HYPEUSDT'])
                self.assertEqual(f.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
                self.assertEqual(f.screen_counts(), [1, 1])

    def test_two_holds_request_two_replacements_and_shared_cap_survives_restart(self):
        f = self.fixture();f.screens = [['BTCUSDT','ETHUSDT'], ['SOLUSDT','BNBUSDT'],
                                      ['XRPUSDT','ADAUSDT'], ['BTCUSDT','ETHUSDT']]
        f.responses = {symbol: hold(symbol) for symbol in ('BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','ADAUSDT')}
        f.on();f.ticks(3)
        self.assertEqual(f.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(f.calls[-1]['prompt'], REPLACEMENT_SCREENING.format(count=2))
        f.restart();f.ticks(10)
        cycle, data = f.current_cycle()
        self.assertEqual((cycle['state'], data['replacements']), ('COMPLETE', 3))
        self.assertEqual(f.screen_counts(), [2, 2, 2, 2])
        count = len(f.calls);f.restart();f.ticks(3)
        self.assertEqual(len(f.calls), count)
        self.assertEqual(f.ledger.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0], 0)

    def test_manual_regular_and_conditional_entries_dedup_and_reserve_concurrency_only(self):
        f = self.fixture()
        f.account.open_orders = [entry('ZECUSDT'), entry('ZECUSDT'), entry('BTCUSDT', reduceOnly=True)]
        f.account.open_algos = [entry('ZECUSDT', algo=True), entry('ETHUSDT', algo=True, reduceOnly=True)]
        account = account_state(f.account, f.store, day())
        self.assertEqual(account['open_entry_symbols'], ['ZECUSDT'])
        self.assertEqual((account['running_positions'], account['available_slots'], account['bot_entries_today']), (0, 1, 0))
        self.assertEqual([path for _, path in f.account.calls],
                         ['/fapi/v1/openOrders','/fapi/v1/openAlgoOrders','/fapi/v3/account'])
        f.account.position('ZECUSDT', '1')
        self.assertEqual(account_state(f.account, f.store, day())['available_slots'], 1)
        f.account.open_algos.append(entry('ETHUSDT', algo=True))
        self.assertEqual(account_state(f.account, f.store, day())['available_slots'], 0)
        f.on();self.assertEqual(f.ticks(3)['wait_reason'], 'ROBOT_CAPACITY_FULL')
        self.assertEqual(f.calls, [])

    def test_pending_entry_fill_between_order_and_position_reads_never_frees_a_slot(self):
        f = self.fixture();f.account.open_orders = [entry('ZECUSDT')]
        original = f.account.signed_get
        def fills(path):
            value = original(path)
            if path == '/fapi/v1/openOrders':
                f.account.open_orders = [];f.account.position('ZECUSDT', '1')
            return value
        f.account.signed_get = fills
        account = account_state(f.account, f.store, day())
        self.assertEqual((account['running_symbols'], account['open_entry_symbols'], account['available_slots']),
                         (['ZECUSDT'], ['ZECUSDT'], 1))

    def test_invalid_pending_read_blocks_account_instead_of_assuming_empty(self):
        for rows, algo in ((None, False), ([entry('ZECUSDT', reduceOnly='false')], False),
                           ([entry('ZECUSDT', positionSide='LONG')], False),
                           ([entry('ZECUSDT', algo=True, algoStatus='FINISHED')], True)):
            with self.subTest(rows=rows, algo=algo), self.assertRaisesRegex(Review, 'ROBOT_ACCOUNT_UNAVAILABLE'):
                open_entry_symbols(rows, algo=algo)

    def cached_analysis(self, *, age=0, state='PENDING', context_age=0):
        f = self.fixture();f.account.position('HYPEUSDT', '4.16');f.screens = [['BTCUSDT']]
        f.on();f.robot.tick();cycle,data = f.current_cycle()
        operation = cycle['id']+':analysis-v9:BTCUSDT'
        f.responses['BTCUSDT'] = {**GOOD, 'take_profit': 110, 'risk_reward': 5}
        context,_=analysis_context(f.market,'BTCUSDT','5',f.account)
        context['fetched_at']-=context_age
        self.assertTrue(f.robot.claim(operation,cycle['id'],'ANALYSIS','BTCUSDT',day(),'5',data=data,context=context))
        f.client.ask(operation, 'historical analysis literal', SETUP_SCHEMA,
                     lambda value: setup(value,'BTCUSDT'), dict(symbol='BTCUSDT'))
        f.ledger.db.execute('UPDATE api_requests SET created=? WHERE operation=?',(time.time()-age,operation))
        f.ledger.db.execute('UPDATE robot_jobs SET state=? WHERE operation=?',(state,operation))
        return f,operation

    def test_fresh_complete_paid_analysis_drains_without_new_body_and_keeps_captured_risk(self):
        for state in ('PENDING','COMPLETE'):
            with self.subTest(state=state):
                f,operation = self.cached_analysis(state=state)
                before = dict(f.ledger.db.execute('SELECT * FROM api_requests WHERE operation=?',(operation,)).fetchone())
                f.store.configure(dict(risk_target_usdt='10'));f.restart()
                with patch.object(f.client,'ask',side_effect=AssertionError('paid replay')):
                    self.assertEqual(f.robot.tick()['bot_status'],'READY_FOR_EXECUTION')
                self.assertEqual(dict(f.ledger.db.execute('SELECT * FROM api_requests WHERE operation=?',(operation,)).fetchone()), before)
                plan = json.loads(self.candidate(f)['plan'])
                self.assertEqual(plan['risk_target_usdt'],'5')
                self.assertEqual(len(f.calls),2)
                self.assertEqual(f.ledger.db.execute('SELECT state FROM robot_jobs WHERE operation=?',(operation,)).fetchone()[0],'COMPLETE')

    def test_stale_complete_analysis_is_rejected_without_paid_replay_or_reset(self):
        f,operation = self.cached_analysis(age=500)
        output = f.ledger.db.execute('SELECT output FROM api_requests WHERE operation=?',(operation,)).fetchone()[0]
        with patch.object(f.client,'ask',side_effect=AssertionError('paid replay')):
            self.assertEqual(f.robot.tick()['failure_code'],'STALE_MARKET_CONTEXT')
            f.ticks(3)
        self.assertEqual((self.candidate(f)['status'], self.candidate(f)['plan']),('REJECTED',None))
        self.assertEqual(f.ledger.db.execute('SELECT state,output,attempts FROM api_requests WHERE operation=?',
                         (operation,)).fetchone()[:],('COMPLETE',output,1))
        self.assertEqual(json.loads(f.current_cycle()[0]['data'])['replacements'],0)

    def test_unknown_or_wrong_linked_cached_analysis_stays_blocked(self):
        for statement in ("UPDATE api_requests SET state='PENDING' WHERE operation=?",
                          "UPDATE robot_jobs SET cycle='other' WHERE operation=?",
                          "UPDATE api_requests SET output=NULL WHERE operation=?"):
            with self.subTest(statement=statement):
                f,operation = self.cached_analysis()
                f.ledger.db.execute(statement,(operation,))
                self.assertEqual(f.robot.tick()['failure_code'],'ROBOT_REQUEST_NEEDS_REVIEW')
                self.assertEqual(len(f.calls),2)
                self.assertIsNone(self.candidate(f))

    def test_started_gateway_callback_receives_dynamic_off_mutation_fence(self):
        f = self.fixture();f.account.position('HYPEUSDT','4.16');f.screens=[['BTCUSDT']]
        gateway=f.connected();f.on();f.ticks(2)
        checks=[]
        def submit(intent):
            checks.append(gateway_mutations_allowed())
            f.store.configure(dict(robot_on=False))
            checks.append(gateway_mutations_allowed())
            return gateway.observation(intent)
        gateway.on_submit=submit
        self.assertEqual(f.robot.tick()['bot_status'],'OFF')
        self.assertEqual(f.ledger.db.execute('SELECT state FROM order_intents').fetchone()[0],'ENTRY_PENDING')
        self.assertEqual(checks,[True,False])
        self.assertFalse(gateway_mutations_allowed())
        calls=len(f.calls);f.ticks(3)
        self.assertEqual(len(f.calls),calls)
        self.assertEqual(gateway.reconciliations,[])

    def cached_screening(self,age=0):
        f=self.fixture();f.on()
        cycle=day()+':robot-v9:0';operation=cycle+':screening:0:2'
        data=dict(target=2,queue=[],seen=[],screen=-1,replacements=0,round_symbols=[])
        f.ledger.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',(cycle,day(),0,'ACTIVE',json.dumps(data)))
        f.client.ask(operation,'historical screening literal',SCREEN_SCHEMA,
                     lambda value:None,catalog=f.market.catalog())
        f.ledger.db.execute('UPDATE api_requests SET created=? WHERE operation=?',(time.time()-age,operation))
        f.ledger.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',(operation,cycle,'SCREENING',None,'PENDING',None))
        return f,operation

    def test_cached_two_coin_screen_drains_when_live_capacity_changes_to_one(self):
        f,operation=self.cached_screening();f.account.position('HYPEUSDT','4.16');f.restart()
        before=dict(f.ledger.db.execute('SELECT * FROM api_requests').fetchone())
        with patch.object(f.client,'ask',side_effect=AssertionError('cached paid replay')):
            self.assertEqual(f.robot.tick()['wait_reason'],'SCREENING_COMPLETE_ANALYSIS_PENDING')
        cycle,data=f.current_cycle()
        self.assertEqual((data['target'],data['queue'],data['screen']),(2,['BTCUSDT','ETHUSDT'],0))
        self.assertEqual(dict(f.ledger.db.execute('SELECT * FROM api_requests').fetchone()),before)
        self.assertEqual(f.ledger.db.execute('SELECT state FROM robot_jobs').fetchone()[0],'COMPLETE')
        self.assertEqual(f.robot.tick()['bot_status'],'READY_FOR_EXECUTION')
        self.assertEqual(f.robot.tick()['bot_status'],'EXECUTION_BLOCKED')
        self.assertEqual(json.loads(f.current_cycle()[0]['data'])['queue'],['ETHUSDT'])
        self.assertEqual(len(f.calls),2)

    def test_stale_cached_screen_finishes_without_replay_or_cap_reset(self):
        f,operation=self.cached_screening(age=500);f.restart()
        with patch.object(f.client,'ask',side_effect=AssertionError('cached paid replay')):
            self.assertEqual(f.robot.tick()['failure_code'],'STALE_MARKET_CONTEXT')
            f.ticks(3)
        cycle,data=f.current_cycle()
        self.assertEqual((cycle['state'],data['screen'],data['replacements'],data['queue']),('COMPLETE',0,0,[]))
        self.assertEqual(f.ledger.db.execute('SELECT state FROM robot_jobs').fetchone()[0],'COMPLETE')
        self.assertEqual(f.ledger.db.execute('SELECT COUNT(*) FROM robot_candidates').fetchone()[0],0)
        self.assertEqual(len(f.calls),1)

    def test_cached_exposed_head_finishes_bookkeeping_without_orphan_job(self):
        f,operation=self.cached_analysis();f.account.positions=[];f.account.open_orders=[entry('BTCUSDT')]
        with patch.object(f.client,'ask',side_effect=AssertionError('cached paid replay')):
            self.assertEqual(f.robot.tick()['failure_code'],'ROBOT_SYMBOL_EXPOSED')
            f.ticks(2)
        self.assertEqual(f.ledger.db.execute('SELECT state FROM robot_jobs WHERE operation=?',(operation,)).fetchone()[0],'COMPLETE')
        self.assertFalse(f.robot.unresolved_research())
        self.assertEqual(self.candidate(f)['plan'],None)
        self.assertEqual(len(f.calls),2)

    def unknown_order(self):
        f=self.fixture();f.account.position('HYPEUSDT','4.16');f.screens=[['BTCUSDT']]
        gateway=f.connected();f.on();f.ticks(2)
        def unknown(intent):raise TimeoutError('private order error')
        gateway.on_submit=unknown
        self.assertEqual(f.robot.tick()['bot_status'],'NEEDS_REVIEW')
        return f,gateway

    def test_unknown_order_resolves_only_owned_get_proofs_without_reentry(self):
        for state in ('ENTRY_PENDING','POSITION_PROTECTED','CLOSED'):
            with self.subTest(state=state):
                f,gateway=self.unknown_order();calls=len(f.calls);f.restart();fences=[]
                def proof(intent):
                    fences.append(gateway_mutations_allowed())
                    fill='0' if state=='ENTRY_PENDING' else intent['entry']['quantity']
                    first_fill=now()
                    changes={} if state!='CLOSED' else dict(first_fill_at=first_fill,closed_at=now(),exit_order_id='20')
                    return gateway.observation(intent,state,fill,**changes)
                gateway.on_reconcile=proof
                self.assertEqual(f.robot.tick()['bot_status'],state)
                self.assertEqual(f.intent()['state'],state)
                self.assertEqual(fences,[False])
                self.assertEqual(len(gateway.submissions),1)
                self.assertEqual(len(f.calls),calls)
                self.assertEqual(f.receipt_count(),0 if state=='ENTRY_PENDING' else 1)
                self.assertEqual(f.account.positions[0]['symbol'],'HYPEUSDT')

    def test_unknown_bad_or_mutating_proof_preserves_original_uncertainty(self):
        for invalid in ('mutation_required','foreign_symbol','missing_tp','rejected'):
            with self.subTest(invalid=invalid):
                f,gateway=self.unknown_order();before=dict(f.intent());candidate=dict(self.candidate(f))
                def proof(intent):
                    self.assertFalse(gateway_mutations_allowed())
                    if invalid=='mutation_required':raise Review('BINANCE_ORDER_ROBOT_OFF')
                    value=gateway.observation(intent,'POSITION_PROTECTED',intent['entry']['quantity'])
                    if invalid=='foreign_symbol':value['symbol']='ETHUSDT'
                    elif invalid=='missing_tp':value['tp_confirmed']=False
                    elif invalid=='rejected':value=gateway.observation(intent,'REJECTED')
                    return value
                gateway.on_reconcile=proof
                self.assertEqual(f.ticks(2)['bot_status'],'NEEDS_REVIEW')
                self.assertEqual(dict(f.intent()),before)
                self.assertEqual(dict(self.candidate(f)),candidate)
                self.assertEqual(f.receipt_count(),0)
                self.assertEqual(len(gateway.submissions),1)
                f.store.configure(dict(robot_on=False));count=len(gateway.reconciliations)
                self.assertEqual(f.robot.tick()['bot_status'],'OFF')
                self.assertEqual(len(gateway.reconciliations),count)

    def test_midnight_does_not_disable_started_gateway_protection(self):
        f=self.fixture();f.account.position('HYPEUSDT','4.16');f.screens=[['BTCUSDT']]
        gateway=f.connected();f.on();f.ticks(2);today=day();checks=[]
        def submit(intent):
            with patch('worker.robot.day',return_value='2099-01-01'):
                checks.extend([f.robot.allowed(today),gateway_mutations_allowed()])
            return gateway.observation(intent)
        gateway.on_submit=submit
        self.assertEqual(f.robot.tick()['bot_status'],'ENTRY_PENDING')
        self.assertEqual(checks,[False,True])
        self.assertFalse(gateway_mutations_allowed())

    def test_previous_day_complete_paid_result_settles_locally_without_new_order(self):
        for kind in ('ANALYSIS','SCREENING'):
            with self.subTest(kind=kind):
                f,operation=(self.cached_analysis() if kind=='ANALYSIS' else self.cached_screening())
                before=dict(f.ledger.db.execute('SELECT * FROM api_requests WHERE operation=?',(operation,)).fetchone())
                tomorrow=(datetime.fromisoformat(day())+timedelta(days=1)).date().isoformat()
                with patch('worker.robot.day',return_value=tomorrow),patch('worker.robot_store.day',return_value=tomorrow),patch.object(f.client,'ask',side_effect=AssertionError('paid replay')):
                    self.assertEqual(f.robot.tick()['failure_code'],'STALE_MARKET_CONTEXT')
                    self.assertFalse(f.robot.unresolved_research())
                self.assertEqual(dict(f.ledger.db.execute('SELECT * FROM api_requests WHERE operation=?',(operation,)).fetchone()),before)
                self.assertEqual(f.ledger.db.execute('SELECT state FROM robot_jobs WHERE operation=?',(operation,)).fetchone()[0],'COMPLETE')
                self.assertEqual(f.ledger.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0],0)
                cycle,data=f.current_cycle()
                self.assertEqual((cycle['state'],data['replacements']),('COMPLETE',0))
                if kind=='ANALYSIS':self.assertEqual((self.candidate(f)['status'],self.candidate(f)['failure_code']),('REJECTED','STALE_MARKET_CONTEXT'))

    def test_previous_day_unknown_or_malformed_result_stays_blocked(self):
        for mutation in ("UPDATE api_requests SET state='NEEDS_REVIEW' WHERE operation=?",
                         "UPDATE api_requests SET output='{}' WHERE operation=?"):
            with self.subTest(mutation=mutation):
                f,operation=self.cached_analysis();f.ledger.db.execute(mutation,(operation,))
                tomorrow=(datetime.fromisoformat(day())+timedelta(days=1)).date().isoformat()
                with patch('worker.robot.day',return_value=tomorrow),patch('worker.robot_store.day',return_value=tomorrow):
                    self.assertEqual(f.robot.tick()['failure_code'],'ROBOT_REQUEST_NEEDS_REVIEW')
                self.assertEqual(f.ledger.db.execute('SELECT state FROM robot_jobs WHERE operation=?',(operation,)).fetchone()[0],'PENDING')
                self.assertIsNone(self.candidate(f));self.assertEqual(len(f.calls),2)

    def test_collector_can_count_hedge_pending_without_relaxing_robot(self):
        for algo in (False,True):
            rows=[entry('ZECUSDT',algo=algo,positionSide='LONG'),entry('ZECUSDT',algo=algo,positionSide='SHORT')]
            self.assertEqual(open_entry_symbols(rows,algo=algo,allow_hedge=True),['ZECUSDT'])
            with self.assertRaises(Review):open_entry_symbols(rows,algo=algo)

    def test_off_blocks_cached_bookkeeping_and_research_before_any_reads(self):
        f,operation=self.cached_analysis();f.store.configure(dict(robot_on=False));reads=len(f.account.calls)
        before=dict(f.ledger.db.execute('SELECT * FROM robot_jobs WHERE operation=?',(operation,)).fetchone())
        self.assertEqual(f.robot.tick()['bot_status'],'OFF')
        self.assertEqual(len(f.account.calls),reads)
        self.assertEqual(dict(f.ledger.db.execute('SELECT * FROM robot_jobs WHERE operation=?',(operation,)).fetchone()),before)
        self.assertIsNone(self.candidate(f))

    def test_original_chart_age_not_request_age_controls_cached_analysis(self):
        f,operation=self.cached_analysis(context_age=170)
        before=dict(f.ledger.db.execute('SELECT * FROM api_requests WHERE operation=?',(operation,)).fetchone())
        resumed=time.time()+100;f.market.clock=lambda:resumed
        with patch('worker.robot.time.time',return_value=resumed),patch.object(f.client,'ask',side_effect=AssertionError('cached paid replay')):
            self.assertEqual(f.robot.tick()['failure_code'],'STALE_MARKET_CONTEXT')
        self.assertEqual(f.ledger.db.execute('SELECT state FROM robot_jobs WHERE operation=?',(operation,)).fetchone()[0],'COMPLETE')
        after=dict(f.ledger.db.execute('SELECT * FROM api_requests WHERE operation=?',(operation,)).fetchone())
        self.assertEqual(after['state'],'COMPLETE')
        for key in ('created','output','body_hash','attempts'):self.assertEqual(after[key],before[key])
        self.assertIsNone(self.candidate(f)['plan']);self.assertEqual(f.receipt_count(),0)

    def test_missing_or_unbound_original_chart_witness_rejects_without_replay(self):
        for mutation in ('missing','source','symbol','timeframes','claimed_after_request','expired_last_candle'):
            with self.subTest(mutation=mutation):
                f,operation=self.cached_analysis();cycle,data=f.current_cycle()
                witness=data['analysis_contexts'][operation]
                if mutation=='missing':del data['analysis_contexts']
                elif mutation=='source':witness['source']='Binance Spot'
                elif mutation=='symbol':witness['symbol']='ETHUSDT'
                elif mutation=='timeframes':del witness['timeframes']['15m']
                elif mutation=='claimed_after_request':witness['claimed_at']=time.time()+1
                else:witness['timeframes']['15m']['last_close_time']=int((time.time()-181)*1000)
                f.robot.save_cycle(cycle['id'],data);f.restart()
                with patch.object(f.client,'ask',side_effect=AssertionError('cached paid replay')):
                    self.assertEqual(f.robot.tick()['failure_code'],'STALE_MARKET_CONTEXT')
                self.assertEqual((self.candidate(f)['status'],self.candidate(f)['plan']),('REJECTED',None))
                self.assertEqual(f.ledger.db.execute('SELECT state FROM robot_jobs WHERE operation=?',(operation,)).fetchone()[0],'COMPLETE')
                self.assertEqual(f.ledger.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0],0)
                self.assertEqual(len(f.calls),2)

    def test_original_context_witness_is_durable_before_paid_transport(self):
        f=self.fixture();f.account.position('HYPEUSDT','4.16');f.screens=[['BTCUSDT']]
        f.on();f.robot.tick();transport=f.client.transport;witnesses=[]
        def response(*args,**kwargs):
            cycle,data=f.current_cycle();job=f.ledger.db.execute("SELECT * FROM robot_jobs WHERE kind='ANALYSIS'").fetchone()
            self.assertEqual(job['state'],'PENDING')
            witness=data['analysis_contexts'][job['operation']]
            body=kwargs.get('body',args[3] if len(args)>3 else None)
            context=json.loads(body['message_history'][0]['content'])
            self.assertEqual((witness['source'],witness['symbol'],witness['fetched_at']),
                             (context['source'],context['symbol'],context['fetched_at']))
            self.assertEqual(witness['timeframes']['15m']['last_close_time'],context['timeframes']['15m']['candles'][-1]['close_time'])
            witnesses.append(witness)
            return transport(*args,**kwargs)
        f.client.transport=response
        self.assertEqual(f.robot.tick()['bot_status'],'READY_FOR_EXECUTION')
        self.assertEqual(len(witnesses),1)
        self.assertLessEqual(witnesses[0]['claimed_at'],f.ledger.db.execute("SELECT created FROM api_requests WHERE operation LIKE '%:analysis-v9:%'").fetchone()[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
