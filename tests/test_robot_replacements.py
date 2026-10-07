"""Bounded HOLD/fee-RR replacements use fake providers and never real orders."""
import json
import unittest
from unittest.mock import patch

import test_order_pipeline as pipeline
import test_slot_resume_pipeline as slot_pipeline
from worker.core import D, Review, day, now
from worker.account_state import SCREENING, SCREENING_ONE


class RobotReplacementTests(unittest.TestCase):
    def fixture(self, seeded=False):
        fixture = (slot_pipeline.SlotResumePipelineTests() if seeded
                   else pipeline.OrderPipelineTests())
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.account.maker_fee = '0.000200'
        fixture.account.taker_fee = '0.000500'
        return fixture

    def rejected_one(self):
        fixture = self.fixture()
        fixture.account.position('HYPEUSDT', '4.16')
        fixture.screens = [['BTCUSDT'], ['SOLUSDT']]
        fixture.on()
        self.assertEqual(fixture.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(fixture.robot.tick()['failure_code'], 'NET_RISK_REWARD_BELOW_2')
        return fixture

    def candidate(self, fixture, symbol):
        return fixture.ledger.db.execute(
            'SELECT * FROM robot_candidates WHERE symbol=? ORDER BY rowid DESC LIMIT 1',
            (symbol,)).fetchone()

    def test_far_tp_is_replaced_before_entry_and_next_coin_uses_net_target(self):
        fixture = self.fixture()
        fixture.connected()
        fixture.account.position('HYPEUSDT', '4.16')
        fixture.screens = [['BTCUSDT'], ['SOLUSDT']]
        fixture.on()
        fixture.robot.tick()
        with patch.dict(pipeline.GOOD, take_profit=110, risk_reward=5):
            self.assertEqual(fixture.robot.tick()['failure_code'], 'NET_RISK_REWARD_NOT_TARGET_2')
        rejected = dict(self.candidate(fixture, 'BTCUSDT'))
        self.assertIsNone(rejected['plan'])
        self.assertEqual(fixture.gateway.submissions, [])
        fixture.restart()
        self.assertEqual(fixture.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        with patch.dict(pipeline.GOOD, take_profit=D('104.31'), risk_reward=D('2.155')):
            self.assertEqual(fixture.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(fixture.robot.tick()['bot_status'], 'ENTRY_PENDING')
        self.assertEqual(dict(self.candidate(fixture, 'BTCUSDT')), rejected)
        self.assertEqual(len(fixture.gateway.submissions), 1)
        intent = fixture.gateway.submissions[0]
        self.assertEqual(intent['symbol'], 'SOLUSDT')
        self.assertEqual(intent['protection']['take_profit'], '104.31')
        self.assertEqual(intent['reward_risk_policy'], 'NET_1_TO_2_NEAREST_TICK')
        self.assertEqual(intent['tp_tick_size'], '0.01')
        self.assertEqual(intent['protection']['working_type'], 'CONTRACT_PRICE')

    def test_far_tp_and_hold_share_three_round_cap_after_restart(self):
        fixture = self.fixture()
        fixture.account.position('HYPEUSDT', '4.16')
        fixture.screens = [['BTCUSDT'], ['SOLUSDT'], ['BNBUSDT'], ['ADAUSDT']]
        fixture.decisions = dict(SOLUSDT='HOLD', ADAUSDT='HOLD')
        fixture.on()
        with patch.dict(pipeline.GOOD, take_profit=110, risk_reward=5):
            fixture.ticks(4)
            fixture.restart()
            fixture.ticks(8)
        row, data = fixture.current_cycle()
        self.assertEqual((row['state'], data['replacements']), ('COMPLETE', 3))
        self.assertEqual(fixture.screen_counts(), [1, 1, 1, 1])
        reasons = {r['symbol']: r['failure_code'] for r in fixture.store.results(row['id'])}
        self.assertEqual(reasons['BTCUSDT'], 'NET_RISK_REWARD_NOT_TARGET_2')
        self.assertEqual(reasons['BNBUSDT'], 'NET_RISK_REWARD_NOT_TARGET_2')
        self.assertEqual(fixture.ledger.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0], 0)
        count = len(fixture.calls)
        self.assertEqual(fixture.ticks(3)['bot_status'], 'INSUFFICIENT_ACTIONABLE_SETUPS')
        self.assertEqual(len(fixture.calls), count)

    def test_two_fee_rejections_or_mixed_hold_request_exactly_two_replacements(self):
        for eth_decision in ('LONG', 'HOLD'):
            with self.subTest(eth_decision=eth_decision):
                fixture = self.fixture()
                fixture.screens = [['BTCUSDT', 'ETHUSDT'], ['SOLUSDT', 'BNBUSDT']]
                fixture.decisions['ETHUSDT'] = eth_decision
                fixture.on()
                fixture.ticks(3)
                self.assertEqual(fixture.calls[0]['prompt'], SCREENING)
                self.assertNotIn('message_history', fixture.calls[0])
                btc = dict(self.candidate(fixture, 'BTCUSDT'))
                self.assertEqual((btc['status'], btc['failure_code'], btc['plan']),
                                 ('REJECTED', 'NET_RISK_REWARD_BELOW_2', None))
                self.assertEqual(self.candidate(fixture, 'ETHUSDT')['status'],
                                 'REJECTED' if eth_decision == 'LONG' else 'HOLD')
                self.assertEqual(fixture.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
                self.assertEqual(fixture.screen_counts(), [2, 2])
                replacement = fixture.calls[-1]
                self.assertEqual(replacement['prompt'], SCREENING)
                context = json.loads(replacement['message_history'][0]['content'])
                self.assertEqual(context['requested_count'], 2)
                self.assertEqual(context['excluded_symbols'], ['BTCUSDT', 'ETHUSDT', 'HYPEUSDT'])
                self.assertIn('different Binance USD-M USDT perpetual coins', context['replacement_instruction'])
                _, data = fixture.current_cycle()
                self.assertEqual((data['target'], data['replacements'], data['queue']),
                                 (2, 1, ['SOLUSDT', 'BNBUSDT']))
                with patch.dict(pipeline.GOOD, take_profit=D("104.31"), risk_reward=D("2.155")):
                    self.assertEqual(fixture.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
                    self.assertEqual(fixture.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
                self.assertEqual(dict(self.candidate(fixture, 'BTCUSDT')), btc)
                self.assertEqual(fixture.receipt_count(), 0)
                self.assertEqual(fixture.ledger.db.execute(
                    "SELECT COUNT(*) FROM order_intents WHERE candidate_id=?", (btc['id'],)).fetchone()[0], 0)

    def test_ready_candidate_and_fee_rejection_request_only_one_replacement(self):
        fixture = self.fixture()
        fixture.screens = [['BTCUSDT', 'ETHUSDT'], ['SOLUSDT']]
        fixture.on()
        fixture.robot.tick()
        self.assertEqual(fixture.robot.tick()['failure_code'], 'NET_RISK_REWARD_BELOW_2')
        with patch.dict(pipeline.GOOD, take_profit=D("104.31"), risk_reward=D("2.155")):
            self.assertEqual(fixture.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        # Default unimplemented dispatch keeps the ready slot reserved but permits
        # analysis of the one replaceable, never-submitted research result.
        self.assertEqual(fixture.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(self.candidate(fixture, 'ETHUSDT')['status'], 'EXECUTION_BLOCKED')
        self.assertEqual(fixture.screen_counts(), [2, 1])
        _, data = fixture.current_cycle()
        self.assertEqual((data['queue'], data['replacements']), (['SOLUSDT'], 1))
        self.assertEqual(fixture.store.snapshot()['last_decision']['symbol'], 'ETHUSDT')
        self.assertEqual(fixture.receipt_count(), 0)

    def test_hold_and_fee_rejections_share_three_round_cap_across_restart(self):
        fixture = self.fixture()
        fixture.account.position('HYPEUSDT', '4.16')
        fixture.screens = [['BTCUSDT'], ['SOLUSDT'], ['BNBUSDT'], ['ADAUSDT']]
        fixture.decisions = dict(SOLUSDT='HOLD', ADAUSDT='HOLD')
        fixture.on()
        fixture.ticks(4)
        fixture.restart()
        fixture.ticks(8)
        row, data = fixture.current_cycle()
        self.assertEqual((row['state'], data['target'], data['screen'], data['replacements']),
                         ('COMPLETE', 1, 3, 3))
        self.assertEqual(fixture.screen_counts(), [1, 1, 1, 1])
        self.assertEqual(len(fixture.calls), 8)
        self.assertEqual({r['symbol']: r['status'] for r in fixture.store.results(row['id'])},
                         dict(BTCUSDT='REJECTED', SOLUSDT='HOLD', BNBUSDT='REJECTED', ADAUSDT='HOLD'))
        count = len(fixture.calls)
        fixture.restart()
        self.assertEqual(fixture.ticks(4)['bot_status'], 'INSUFFICIENT_ACTIONABLE_SETUPS')
        self.assertEqual(len(fixture.calls), count)
        self.assertEqual(fixture.ledger.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0], 0)
        self.assertEqual(fixture.account.positions[0]['symbol'], 'HYPEUSDT')

    def test_existing_active_or_complete_seeded_fee_rejection_resumes_same_cycle(self):
        for state in ('ACTIVE', 'COMPLETE'):
            with self.subTest(state=state):
                fixture = self.fixture(seeded=True)
                fixture.seed_one()
                fixture.screens = [['BTCUSDT'], ['SOLUSDT']]
                fixture.robot.tick()
                self.assertEqual(fixture.robot.tick()['failure_code'], 'NET_RISK_REWARD_BELOW_2')
                rejected = dict(self.candidate(fixture, 'BTCUSDT'))
                fixture.ledger.db.execute('UPDATE robot_cycles SET state=? WHERE id=?', (state, fixture.new))
                count = len(fixture.calls)
                submitted = len(fixture.gateway.submissions)
                fixture.restart()
                self.assertEqual(fixture.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
                data = json.loads(fixture.cycle()['data'])
                initial = fixture.calls[fixture.initial_calls]
                self.assertEqual(initial['prompt'], SCREENING_ONE)
                self.assertNotIn('message_history', initial)
                replacement = fixture.calls[-1]
                self.assertEqual(replacement['prompt'], SCREENING_ONE)
                context = json.loads(replacement['message_history'][0]['content'])
                self.assertEqual(context['requested_count'], 1)
                # ETH belongs to the old cycle, so its active order must be
                # excluded independently of the new cycle's seen BTC symbol.
                self.assertEqual(context['excluded_symbols'], ['BTCUSDT', 'ETHUSDT', 'HYPEUSDT'])
                self.assertEqual((data['target'], data['screen'], data['replacements'], data['queue']),
                                 (1, 1, 1, ['SOLUSDT']))
                self.assertEqual(len(fixture.calls), count + 1)
                self.assertEqual(len(fixture.gateway.submissions), submitted)
                self.assertEqual(dict(self.candidate(fixture, 'BTCUSDT')), rejected)
                self.assertEqual(fixture.ledger.db.execute(
                    'SELECT COUNT(*) FROM robot_jobs WHERE cycle=? AND kind=?',
                    (fixture.new, 'ANALYSIS')).fetchone()[0], 1)
                self.assertEqual(fixture.ledger.db.execute('SELECT COUNT(*) FROM robot_cycles').fetchone()[0], 2)
                fixture.preserve_old()

    def test_seeded_fee_replacement_keeps_epoch_and_counter_after_eth_first_fill(self):
        fixture = self.fixture(seeded=True)
        fixture.seed_one()
        fixture.screens = [['BTCUSDT'], ['SOLUSDT']]
        fixture.robot.tick()
        self.assertEqual(fixture.robot.tick()['failure_code'], 'NET_RISK_REWARD_BELOW_2')
        fixture.fill('ETHUSDT')
        self.assertEqual(fixture.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(fixture.store.entries(day()), 1)
        self.assertEqual(json.loads(fixture.cycle()['data'])['replacements'], 1)
        with patch.dict(pipeline.GOOD, take_profit=D("104.31"), risk_reward=D("2.155")):
            self.assertEqual(fixture.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
            self.assertEqual(fixture.robot.tick()['bot_status'], 'ENTRY_PENDING')
        count = len(fixture.calls)
        self.assertEqual(fixture.ticks(3)['wait_reason'], 'ROBOT_CAPACITY_FULL')
        self.assertEqual(len(fixture.calls), count)
        self.assertEqual(fixture.screens_after(), [1, 1])
        self.assertEqual(fixture.ledger.db.execute('SELECT COUNT(*) FROM robot_cycles').fetchone()[0], 2)
        self.assertEqual(json.loads(fixture.cycle()['data'])['replacements'], 1)
        fixture.preserve_old()

    def test_other_failures_plan_or_existing_intent_do_not_trigger_replacement(self):
        for mutation in ('other_failure', 'non_null_plan', 'rejected_intent', 'unknown_intent'):
            with self.subTest(mutation=mutation):
                fixture = self.rejected_one()
                row = self.candidate(fixture, 'BTCUSDT')
                if mutation == 'other_failure':
                    fixture.ledger.db.execute("UPDATE robot_candidates SET failure_code='INVALID_PRICE_FILTER'")
                elif mutation == 'non_null_plan':
                    fixture.ledger.db.execute("UPDATE robot_candidates SET plan='{}'")
                else:
                    state = 'REJECTED' if mutation == 'rejected_intent' else 'NEEDS_REVIEW'
                    fixture.ledger.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
                        (row['id'], row['id'], row['symbol'], state, '{}', None,
                         'NET_RISK_REWARD_BELOW_2', now(), now()))
                self.assertFalse(fixture.robot.replaceable_result(self.candidate(fixture, 'BTCUSDT')))
                count = len(fixture.calls)
                fixture.restart()
                fixture.ticks(3)
                self.assertEqual(len(fixture.calls), count)
                self.assertEqual(fixture.screen_counts(), [1])
                _, data = fixture.current_cycle()
                self.assertEqual(data['replacements'], 0)

    def test_off_or_provider_ambiguity_stops_fee_replacement_before_new_request(self):
        for blocker in ('OFF', 'PROVIDER_UNKNOWN'):
            with self.subTest(blocker=blocker):
                fixture = self.rejected_one()
                if blocker == 'OFF':
                    fixture.store.configure(dict(robot_on=False))
                else:
                    fixture.ledger.db.execute("UPDATE api_requests SET state='NEEDS_REVIEW' WHERE operation LIKE '%:analysis-v9:%'")
                count, reads = len(fixture.calls), len(fixture.account.calls)
                result = fixture.ticks(3)
                self.assertEqual(len(fixture.calls), count)
                self.assertEqual(fixture.screen_counts(), [1])
                if blocker == 'OFF':
                    self.assertEqual(result['bot_status'], 'OFF')
                    self.assertEqual(len(fixture.account.calls), reads)
                else:
                    self.assertEqual(result['failure_code'], 'ROBOT_REQUEST_NEEDS_REVIEW')
                self.assertEqual(self.candidate(fixture, 'BTCUSDT')['status'], 'REJECTED')

    def test_new_technical_rejection_does_not_replace_historical_fee_rejection(self):
        fixture = self.rejected_one()
        self.assertEqual(fixture.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        with patch('worker.robot.risk_check', side_effect=Review('INVALID_PRICE_FILTER')):
            self.assertEqual(fixture.robot.tick()['failure_code'], 'INVALID_PRICE_FILTER')
        count = len(fixture.calls)
        fixture.ticks(4)
        self.assertEqual(len(fixture.calls), count)
        self.assertEqual(fixture.screen_counts(), [1, 1])
        row, data = fixture.current_cycle()
        self.assertEqual((row['state'], data['replacements']), ('COMPLETE', 1))
        self.assertEqual(self.candidate(fixture, 'BTCUSDT')['failure_code'], 'NET_RISK_REWARD_BELOW_2')
        self.assertEqual(self.candidate(fixture, 'SOLUSDT')['failure_code'], 'INVALID_PRICE_FILTER')

    def test_provider_repeating_excluded_coin_creates_no_order_or_extra_round(self):
        fixture = self.rejected_one()
        original = dict(self.candidate(fixture, 'BTCUSDT'))
        fixture.screens = [['BTCUSDT']]
        self.assertEqual(fixture.robot.tick()['wait_reason'], 'SCREENING_NO_ELIGIBLE_SYMBOLS')
        context = json.loads(fixture.calls[-1]['message_history'][0]['content'])
        self.assertEqual((context['requested_count'], context['excluded_symbols']),
                         (1, ['BTCUSDT', 'HYPEUSDT']))
        count = len(fixture.calls)
        fixture.restart()
        fixture.ticks(4)
        self.assertEqual(len(fixture.calls), count)
        self.assertEqual(fixture.screen_counts(), [1, 1])
        self.assertEqual(dict(self.candidate(fixture, 'BTCUSDT')), original)
        self.assertEqual(fixture.ledger.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0], 0)
        row, data = fixture.current_cycle()
        self.assertEqual((row['state'], data['queue'], data['replacements']), ('COMPLETE', [], 1))
        self.assertEqual(fixture.account.positions[0]['symbol'], 'HYPEUSDT')


if __name__ == '__main__':
    unittest.main(verbosity=2)
