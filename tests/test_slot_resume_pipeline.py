"""Offline integration for one bounded future numeric cycle; no SDK/network calls."""
import json
import unittest
from datetime import timedelta, datetime
from unittest.mock import patch

import test_order_pipeline as fixture
from worker.core import D, day, now
from worker.neuroapi import SCREEN_ONE_SCHEMA, SCREEN_SCHEMA

class SlotResumePipelineTests(fixture.OrderPipelineTests):
    def setUp(self):
        network=patch('socket.socket',side_effect=AssertionError('Real network forbidden'))
        network.start(); self.addCleanup(network.stop)
        super().setUp()
        self.connected(); self.fills={}; self.closed={}
        def submit(intent):
            if intent['symbol']=='BTCUSDT' and ':robot-v9:0:' in intent['intent_id']: raise RuntimeError('fixture uncertain submission')
            return self.gateway.observation(intent)
        self.gateway.on_submit=submit
        self.gateway.on_reconcile=self.observe
        self.on()
        self.assertEqual(self.robot.tick()['wait_reason'],'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(self.robot.tick()['bot_status'],'READY_FOR_EXECUTION')
        self.assertEqual(self.robot.tick()['bot_status'],'NEEDS_REVIEW')
        btc=self.ledger.db.execute("SELECT * FROM order_intents WHERE symbol='BTCUSDT'").fetchone()
        self.ledger.db.execute("UPDATE order_intents SET state='REJECTED',failure_code='NO_ACCEPTED_ORDER_OBSERVED' WHERE id=?",(btc['id'],))
        self.ledger.db.execute("UPDATE robot_candidates SET status='REJECTED',failure_code='NO_ACCEPTED_ORDER_OBSERVED' WHERE id=?",(btc['candidate_id'],))
        self.assertEqual(self.robot.tick()['bot_status'],'READY_FOR_EXECUTION')
        self.assertEqual(self.robot.tick()['bot_status'],'ENTRY_PENDING')
        self.assertEqual(self.robot.tick()['wait_reason'],'ROBOT_CYCLE_COMPLETE')
        self.old=day()+':robot-v9:0'; self.new=day()+':robot-v9:1'
        oldrow=self.ledger.db.execute('SELECT * FROM robot_cycles WHERE id=?',(self.old,)).fetchone()
        self.assertEqual((oldrow['state'],oldrow['entry_epoch']),('COMPLETE',0))
        data=json.loads(oldrow['data']); self.assertEqual((data['target'],data['queue']),(2,[]))
        self.assertEqual(self.store.entries(day()),0); self.assertEqual(self.store.snapshot()['available_slots'],1)
        self.old_before=dict(oldrow); self.btc_before=dict(self.ledger.db.execute("SELECT * FROM order_intents WHERE symbol='BTCUSDT'").fetchone())
        self.eth_before=dict(self.ledger.db.execute("SELECT * FROM order_intents WHERE symbol='ETHUSDT'").fetchone())
        self.provider_before=list(self.calls); self.submissions_before=len(self.gateway.submissions)
        self.initial_calls=len(self.calls)
        self.seed=data
    def observe(self,intent):
        fill=self.fills.get(intent['symbol'])
        if intent['symbol'] in self.closed:
            return self.gateway.observation(intent,'CLOSED',intent['entry']['quantity'],first_fill_at=fill,closed_at=self.closed[intent['symbol']],exit_order_id='20')
        if fill:
            return self.gateway.observation(intent,'POSITION_PROTECTED',intent['entry']['quantity'],first_fill_at=fill)
        return self.gateway.observation(intent)
    def seed_one(self):
        data=dict(target=1,queue=[],seen=[],screen=-1,replacements=0,round_symbols=[])
        data['maintenance']=dict(reason='NO_ACCEPTED_ORDER_OBSERVED',proof_sha256='fixture-only')
        self.ledger.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',(self.new,day(),1,'ACTIVE',json.dumps(data)))
    def cycle(self): return self.ledger.db.execute('SELECT * FROM robot_cycles WHERE id=?',(self.new,)).fetchone()
    def screens_after(self): return [body['output_schema']['properties']['symbols']['minItems'] for body in self.calls[self.initial_calls:] if body['output_schema'] in (SCREEN_SCHEMA,SCREEN_ONE_SCHEMA)]
    def fill(self,symbol):
        row=self.ledger.db.execute('SELECT payload FROM order_intents WHERE symbol=?',(symbol,)).fetchone()
        intent=json.loads(row['payload']); self.fills[symbol]=now()
        self.account.position(symbol,intent['entry']['quantity'])
    def close(self,symbol):
        self.closed[symbol]=now(); self.account.positions=[row for row in self.account.positions if row['symbol']!=symbol]
    def preserve_old(self):
        self.assertEqual(dict(self.ledger.db.execute('SELECT * FROM robot_cycles WHERE id=?',(self.old,)).fetchone()),self.old_before)
        self.assertEqual(dict(self.ledger.db.execute('SELECT * FROM order_intents WHERE id=?',(self.btc_before['id'],)).fetchone()),self.btc_before)
        eth=dict(self.ledger.db.execute("SELECT * FROM order_intents WHERE symbol='ETHUSDT'").fetchone())
        self.assertEqual((eth['id'],eth['payload'],eth['created']),(self.eth_before['id'],self.eth_before['payload'],self.eth_before['created']))
    def new_pending(self):
        self.seed_one(); self.screens=[['SOLUSDT']]
        self.assertEqual(self.robot.tick()['wait_reason'],'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(self.robot.tick()['bot_status'],'READY_FOR_EXECUTION')
        self.assertEqual(self.robot.tick()['bot_status'],'ENTRY_PENDING')
    def test_resumes_one_coin_cycle_without_replaying_btc_or_eth(self):
        self.new_pending(); self.preserve_old()
        self.assertEqual(self.screens_after(),[1])
        self.assertEqual([row['symbol'] for row in self.gateway.submissions[self.submissions_before:]],['SOLUSDT'])
        self.assertEqual(self.ledger.db.execute("SELECT cycle FROM robot_candidates WHERE symbol='SOLUSDT'").fetchone()[0],self.new)
        self.assertEqual(self.ledger.db.execute("SELECT state FROM order_intents WHERE symbol='ETHUSDT'").fetchone()[0],'ENTRY_PENDING')
        self.assertEqual(self.receipt_count(),0)
    def test_two_pending_reserve_both_slots_and_no_third_order_or_paid_call(self):
        self.new_pending(); count=len(self.calls); submissions=len(self.gateway.submissions)
        self.assertEqual(self.store.snapshot()['available_slots'],0)
        self.assertEqual(self.ticks(6)['wait_reason'],'ROBOT_CAPACITY_FULL')
        self.assertEqual((len(self.calls),len(self.gateway.submissions)),(count,submissions))
        self.assertEqual(self.store.entries(day()),0); self.preserve_old()
    def test_eth_first_fill_reuses_cycle_one_paid_queue_across_epoch(self):
        self.seed_one(); self.screens=[['SOLUSDT']]
        self.assertEqual(self.robot.tick()['wait_reason'],'SCREENING_COMPLETE_ANALYSIS_PENDING')
        before=json.loads(self.cycle()['data']); self.assertEqual(before['queue'],['SOLUSDT'])
        self.fill('ETHUSDT')
        self.assertEqual(self.robot.tick()['bot_status'],'READY_FOR_EXECUTION')
        self.assertEqual(self.store.entries(day()),1)
        self.assertEqual(self.robot.tick()['bot_status'],'ENTRY_PENDING')
        self.ticks(3)
        self.assertEqual(self.screens_after(),[1]); self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_cycles').fetchone()[0],2)
        after=json.loads(self.cycle()['data']); self.assertEqual((after['target'],after['screen'],after['replacements']),(1,0,0))
        self.assertEqual(after['queue'],[]); self.preserve_old()
    def test_new_coin_first_fill_cannot_create_third_entry_while_eth_pending(self):
        self.new_pending(); self.fill('SOLUSDT')
        count=len(self.calls); submissions=len(self.gateway.submissions)
        self.assertEqual(self.ticks(5)['wait_reason'],'ROBOT_CAPACITY_FULL')
        self.assertEqual(self.store.entries(day()),1); self.assertEqual(self.store.snapshot()['available_slots'],0)
        self.assertEqual((len(self.calls),len(self.gateway.submissions)),(count,submissions))
        self.preserve_old()
    def test_daily_two_entry_cap_remains_after_both_fills(self):
        self.new_pending(); self.fill('ETHUSDT'); self.fill('SOLUSDT')
        self.assertEqual(self.ticks(3)['wait_reason'],'ROBOT_CAPACITY_FULL')
        self.assertEqual(self.store.entries(day()),2); self.assertEqual(self.store.snapshot()['available_slots'],0)
        count=len(self.calls); submissions=len(self.gateway.submissions)
        self.assertEqual(self.ticks(4)['wait_reason'],'ROBOT_CAPACITY_FULL')
        self.assertEqual((len(self.calls),len(self.gateway.submissions)),(count,submissions))
        self.close('ETHUSDT'); self.close('SOLUSDT')
        self.assertEqual(self.ticks(3)['wait_reason'],'ROBOT_CAPACITY_FULL')
        self.assertEqual(self.store.snapshot()['running_positions'],0); self.assertEqual(self.store.entries(day()),2)
        self.assertEqual((len(self.calls),len(self.gateway.submissions)),(count,submissions))
        self.assertEqual(self.screens_after(),[1]); self.preserve_old()
    def test_two_carryover_positions_still_block_next_day(self):
        self.new_pending(); self.fill('ETHUSDT'); self.fill('SOLUSDT'); self.ticks(2)
        tomorrow=(datetime.fromisoformat(day())+timedelta(days=1)).date().isoformat()
        count=len(self.calls); submissions=len(self.gateway.submissions)
        with patch('worker.robot.day',return_value=tomorrow),patch('worker.robot_store.day',return_value=tomorrow):
            self.assertEqual(self.store.entries(tomorrow),0)
            self.assertEqual(self.ticks(3)['wait_reason'],'ROBOT_CAPACITY_FULL')
            self.assertEqual(self.store.snapshot()['available_slots'],0)
        self.assertEqual((len(self.calls),len(self.gateway.submissions)),(count,submissions)); self.preserve_old()
    def test_bounded_hold_replacements_not_reset_when_epoch_changes(self):
        self.seed_one(); self.screens=[['SOLUSDT'],['BNBUSDT'],['XRPUSDT'],['ADAUSDT']]
        self.decisions={coin:'HOLD' for pair in self.screens for coin in pair}
        self.ticks(14)
        self.assertEqual(self.screens_after(),[1,1,1,1])
        data=json.loads(self.cycle()['data']); self.assertEqual((data['target'],data['screen'],data['replacements']),(1,3,3))
        count=len(self.calls); submissions=len(self.gateway.submissions)
        self.fill('ETHUSDT')
        self.assertEqual(self.robot.tick()['bot_status'],'INSUFFICIENT_ACTIONABLE_SETUPS')
        self.ticks(4)
        self.assertEqual((len(self.calls),len(self.gateway.submissions)),(count,submissions))
        self.assertEqual(json.loads(self.cycle()['data'])['replacements'],3)
        self.assertEqual(self.cycle()['state'],'COMPLETE'); self.preserve_old()
    def test_eth_exposure_is_excluded_without_replay(self):
        self.seed_one(); self.screens=[['ETHUSDT']]
        self.assertEqual(self.robot.tick()['wait_reason'],'SCREENING_NO_ELIGIBLE_SYMBOLS')
        self.ticks(4)
        self.assertEqual(len(self.gateway.submissions),self.submissions_before)
        self.assertEqual(json.loads(self.cycle()['data'])['queue'],[]); self.preserve_old()
    def test_new_btc_choice_requires_fresh_analysis_and_new_client_id(self):
        self.seed_one(); self.screens=[['BTCUSDT']]
        self.assertEqual(self.robot.tick()['wait_reason'],'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(self.robot.tick()['bot_status'],'READY_FOR_EXECUTION')
        self.assertEqual(self.robot.tick()['bot_status'],'ENTRY_PENDING')
        submitted=self.gateway.submissions[self.submissions_before:]
        self.assertEqual([row['symbol'] for row in submitted],['BTCUSDT'])
        self.assertNotEqual(submitted[0]['client_order_id'],json.loads(self.btc_before['payload'])['client_order_id'])
        self.assertEqual(submitted[0]['intent_id'],self.new+':analysis-v9:BTCUSDT')
        self.assertEqual(self.screens_after(),[1]); self.assertEqual(len(self.calls)-self.initial_calls,2)
        self.assertEqual(self.ledger.db.execute("SELECT state FROM api_requests WHERE operation=?",(self.new+':analysis-v9:BTCUSDT',)).fetchone()[0],'COMPLETE')
        self.preserve_old()
    def test_fee_rejected_setup_selects_one_replacement_without_replaying_old_entry(self):
        self.seed_one(); self.screens=[['BTCUSDT'],['SOLUSDT']]
        self.assertEqual(self.robot.tick()['wait_reason'],'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.account.maker_fee='0.000200'; self.account.taker_fee='0.000500'
        result=self.robot.tick()
        self.assertEqual((result['bot_status'],result['failure_code']),('REJECTED','NET_RISK_REWARD_BELOW_2'))
        row=self.ledger.db.execute('SELECT status,failure_code FROM robot_candidates WHERE cycle=?',(self.new,)).fetchone()
        self.assertEqual(tuple(row),('REJECTED','NET_RISK_REWARD_BELOW_2'))
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM order_intents WHERE candidate_id IN (SELECT id FROM robot_candidates WHERE cycle=?)',(self.new,)).fetchone()[0],0)
        count=len(self.calls)
        self.assertEqual(self.robot.tick()['wait_reason'],'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(len(self.calls),count+1)
        self.assertEqual(len(self.gateway.submissions),self.submissions_before)
        self.assertEqual(json.loads(self.cycle()['data'])['replacements'],1)
        self.assertEqual(self.screens_after(),[1,1])
        self.assertEqual(self.store.snapshot()['available_slots'],1)
        self.assertEqual(self.store.snapshot()['last_decision']['failure_code'],'NET_RISK_REWARD_BELOW_2')
        with patch.dict(fixture.GOOD,take_profit=D("104.31"),risk_reward=D("2.155")):
            self.assertEqual(self.robot.tick()['bot_status'],'READY_FOR_EXECUTION')
            self.assertEqual(self.robot.tick()['bot_status'],'ENTRY_PENDING')
        self.assertEqual([order['symbol'] for order in self.gateway.submissions[self.submissions_before:]],['SOLUSDT'])
        count=len(self.calls);self.ticks(3);self.assertEqual(len(self.calls),count)
        row=self.ledger.db.execute("SELECT status,failure_code FROM robot_candidates WHERE cycle=? AND symbol='BTCUSDT'",(self.new,)).fetchone()
        self.assertEqual(tuple(row),('REJECTED','NET_RISK_REWARD_BELOW_2'))
        self.preserve_old()
    def test_one_carryover_position_limits_new_day_screening_to_one(self):
        self.new_pending(); self.fill('ETHUSDT'); self.fill('SOLUSDT'); self.ticks(2)
        self.close('SOLUSDT'); self.ticks(2)
        tomorrow=(datetime.fromisoformat(day())+timedelta(days=1)).date().isoformat()
        self.screens=[['BNBUSDT']]; count=len(self.calls)
        with patch('worker.robot.day',return_value=tomorrow),patch('worker.robot_store.day',return_value=tomorrow):
            result=self.robot.tick()
            self.assertEqual(result['wait_reason'],'SCREENING_COMPLETE_ANALYSIS_PENDING')
            self.assertEqual(result['running_positions'],1); self.assertEqual(result['available_slots'],1)
            self.assertEqual(self.store.entries(tomorrow),0)
        self.assertEqual(len(self.calls),count+1)
        self.assertEqual(self.calls[-1]['output_schema'],SCREEN_ONE_SCHEMA)
        self.preserve_old()

# Select only the added focused tests; parent fixture methods are not rerun.
def load_tests(loader, tests, pattern):
    return unittest.TestSuite(SlotResumePipelineTests(name) for name in sorted(SlotResumePipelineTests.__dict__) if name.startswith('test_'))

if __name__=='__main__': unittest.main(verbosity=2)
