"""Terminal provider validation failures replace coins without replay or real orders."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

import test_order_pipeline as pipeline
from support import GOOD, hold
from worker.core import D, Review, now
from worker.neuroapi import SCREEN_SCHEMA, SCREEN_ONE_SCHEMA, SETUP_SCHEMA, setup
from worker.neuroapi_request import LIMIT_ERROR, MAX_CHARACTERS, MAX_MESSAGES
from worker.prompts import ANALYSIS


class ProviderValidationReplacementTests(unittest.TestCase):
    def setUp(self):
        network = patch('urllib.request.build_opener', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def fixture(self, choices=None):
        fixture = pipeline.OrderPipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.account.maker_fee = '0.0002'
        fixture.account.taker_fee = '0.0005'
        fixture.account.position('HYPEUSDT', '4.16')
        fixture.choices = choices or {}

        def transport(method, url, headers, body, timeout):
            self.assertEqual((method, url, timeout),
                             ('POST', 'https://api.neurobro.ai/api/v1/agent/ask', 90))
            fixture.calls.append(copy.deepcopy(body))
            if body['output_schema'] in (SCREEN_SCHEMA, SCREEN_ONE_SCHEMA):
                output = dict(symbols=fixture.screens.pop(0))
            else:
                context = json.loads(body['message_history'][0]['content'])
                symbol = context.get('symbol') or context['data']['symbol']
                choice = fixture.choices.get(symbol, 'BELOW')
                if choice == 'NETWORK':
                    raise TimeoutError('private transport text must never become a failure code')
                if choice.startswith('HTTP_'):
                    return int(choice[5:]), {}, dict(private_provider_error='not model output')
                if choice == 'HOLD':
                    output = hold(symbol)
                else:
                    output = {**GOOD, 'symbol': symbol}
                    if choice == 'FAR':
                        output.update(take_profit=110, risk_reward=5)
                    elif choice == 'TARGET':
                        output.update(take_profit=D('104.31'), risk_reward=D('2.155'))
            return 200, {}, dict(mode='smart', answer=None, output=output)

        fixture.transport = transport
        fixture.make()
        return fixture

    def candidate(self, fixture, symbol):
        return fixture.ledger.db.execute(
            'SELECT * FROM robot_candidates WHERE symbol=? ORDER BY rowid DESC LIMIT 1',
            (symbol,)).fetchone()

    def request(self, fixture, operation):
        row = fixture.ledger.db.execute('SELECT * FROM api_requests WHERE operation=?',
                                       (operation,)).fetchone()
        return dict(row) if row is not None else None

    def rejected_422(self):
        fixture = self.fixture({'BTCUSDT': 'HTTP_422'})
        fixture.screens = [['BTCUSDT'], ['SOLUSDT']]
        fixture.on()
        self.assertEqual(fixture.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(fixture.robot.tick()['failure_code'], 'HTTP_422')
        return fixture

    def test_one_manual_slot_rr_rr_422_then_final_canonical_coin_without_replay(self):
        fixture = self.fixture({'BNBUSDT': 'FAR', 'SOLUSDT': 'HTTP_422', 'ADAUSDT': 'TARGET'})
        fixture.connected()
        fixture.screens = [['BTCUSDT'], ['BNBUSDT'], ['SOLUSDT'], ['ADAUSDT']]
        manual = copy.deepcopy(fixture.account.positions)
        fixture.on()
        fixture.robot.tick()
        self.assertEqual(fixture.robot.tick()['failure_code'], 'NET_RISK_REWARD_BELOW_2')
        fixture.robot.tick()
        self.assertEqual(fixture.robot.tick()['failure_code'], 'NET_RISK_REWARD_NOT_TARGET_2')
        fixture.robot.tick()
        self.assertEqual(fixture.robot.tick()['failure_code'], 'HTTP_422')
        rejected = dict(self.candidate(fixture, 'SOLUSDT'))
        request = self.request(fixture, rejected['id'])
        self.assertEqual((rejected['status'], rejected['plan']), ('REJECTED', None))
        self.assertEqual((request['state'], request['failure_code'], request['attempts'],
                          request['idempotency'], request['output']),
                         ('REJECTED_REQUEST_VALIDATION', 'HTTP_422', 1, None, None))
        self.assertEqual(request['body_hash'], hashlib.sha256(
            json.dumps(fixture.calls[-1], sort_keys=True).encode()).hexdigest())
        job = fixture.ledger.db.execute('SELECT * FROM robot_jobs WHERE operation=?',
                                       (rejected['id'],)).fetchone()
        self.assertEqual((job['state'], job['kind'], job['cycle'], job['symbol']),
                         ('REQUEST_REJECTED', 'ANALYSIS', rejected['cycle'], 'SOLUSDT'))
        fixture.restart()
        self.assertEqual(fixture.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        context = json.loads(fixture.calls[-1]['message_history'][0]['content'])
        self.assertEqual((context['requested_count'], context['excluded_symbols']),
                         (1, ['BNBUSDT', 'BTCUSDT', 'HYPEUSDT', 'SOLUSDT']))
        self.assertEqual(fixture.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(fixture.robot.tick()['bot_status'], 'ENTRY_PENDING')
        intent = fixture.gateway.submissions[0]
        self.assertEqual((intent['symbol'], intent['protection']['take_profit'],
                          intent['protection']['working_type']),
                         ('ADAUSDT', '104.31', 'CONTRACT_PRICE'))
        self.assertEqual(fixture.screen_counts(), [1, 1, 1, 1])
        self.assertEqual(len(fixture.calls), 8)
        self.assertEqual(dict(self.candidate(fixture, 'SOLUSDT')), rejected)
        self.assertEqual(self.request(fixture, rejected['id']), request)
        self.assertEqual(fixture.ledger.db.execute(
            'SELECT COUNT(*) FROM order_intents WHERE candidate_id=?',
            (rejected['id'],)).fetchone()[0], 0)
        self.assertEqual(fixture.receipt_count(), 0)
        self.assertEqual(fixture.account.positions, manual)
        cycle, data = fixture.current_cycle()
        self.assertEqual((data['target'], data['screen'], data['replacements']), (1, 3, 3))
        count = len(fixture.calls)
        fixture.restart()
        self.assertEqual(fixture.ticks(4)['wait_reason'], 'ROBOT_CAPACITY_FULL')
        self.assertEqual(len(fixture.calls), count)
        self.assertEqual(len(fixture.gateway.submissions), 1)
        self.assertEqual(json.loads(fixture.current_cycle()[0]['data'])['replacements'], 3)

    def test_hold_rr_and_terminal_422_share_three_rounds_even_after_restart(self):
        fixture = self.fixture({'BTCUSDT': 'HOLD', 'BNBUSDT': 'HTTP_422',
                                'SOLUSDT': 'FAR', 'ADAUSDT': 'HTTP_422'})
        fixture.screens = [['BTCUSDT'], ['BNBUSDT'], ['SOLUSDT'], ['ADAUSDT']]
        fixture.on()
        fixture.ticks(4)
        fixture.restart()
        fixture.ticks(6)
        cycle, data = fixture.current_cycle()
        self.assertEqual((cycle['state'], data['screen'], data['replacements']), ('COMPLETE', 3, 3))
        self.assertEqual(fixture.screen_counts(), [1, 1, 1, 1])
        self.assertEqual(len(fixture.calls), 8)
        self.assertEqual(fixture.ledger.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0], 0)
        self.assertEqual(fixture.ledger.db.execute(
            "SELECT COUNT(*) FROM api_requests WHERE state='REJECTED_REQUEST_VALIDATION'").fetchone()[0], 2)
        before = [dict(row) for row in fixture.ledger.db.execute('SELECT * FROM api_requests ORDER BY operation')]
        fixture.restart()
        self.assertEqual(fixture.ticks(4)['bot_status'], 'INSUFFICIENT_ACTIONABLE_SETUPS')
        self.assertEqual(len(fixture.calls), 8)
        self.assertEqual([dict(row) for row in fixture.ledger.db.execute(
            'SELECT * FROM api_requests ORDER BY operation')], before)

    def test_off_after_terminal_422_blocks_reads_and_replacement_then_resumes_once(self):
        fixture = self.rejected_422()
        candidate = dict(self.candidate(fixture, 'BTCUSDT'))
        fixture.store.configure(dict(robot_on=False))
        calls, reads = len(fixture.calls), len(fixture.account.calls)
        fixture.restart()
        self.assertEqual(fixture.ticks(3)['bot_status'], 'OFF')
        self.assertEqual((len(fixture.calls), len(fixture.account.calls)), (calls, reads))
        self.assertEqual(dict(self.candidate(fixture, 'BTCUSDT')), candidate)
        fixture.on()
        self.assertEqual(fixture.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(fixture.screen_counts(), [1, 1])
        self.assertEqual(json.loads(fixture.current_cycle()[0]['data'])['replacements'], 1)

    def test_only_coherent_terminal_request_and_matching_analysis_job_are_replaceable(self):
        mutations = {
            'unknown_api': "UPDATE api_requests SET state='NEEDS_REVIEW' WHERE operation=?",
            'completed_api': "UPDATE api_requests SET state='COMPLETE' WHERE operation=?",
            'missing_api': 'DELETE FROM api_requests WHERE operation=?',
            'wrong_attempt': 'UPDATE api_requests SET attempts=2 WHERE operation=?',
            'zero_attempt': 'UPDATE api_requests SET attempts=0 WHERE operation=?',
            'non_null_output': "UPDATE api_requests SET output='{}' WHERE operation=?",
            'idempotency': "UPDATE api_requests SET idempotency='opaque' WHERE operation=?",
            'wrong_failure': "UPDATE api_requests SET failure_code='HTTP_400' WHERE operation=?",
            'wrong_job_cycle': "UPDATE robot_jobs SET cycle='other-cycle' WHERE operation=?",
            'wrong_job_symbol': "UPDATE robot_jobs SET symbol='ETHUSDT' WHERE operation=?",
            'wrong_job_kind': "UPDATE robot_jobs SET kind='SCREENING' WHERE operation=?",
            'wrong_job_state': "UPDATE robot_jobs SET state='COMPLETE' WHERE operation=?",
            'missing_job': 'DELETE FROM robot_jobs WHERE operation=?',
            'plan': "UPDATE robot_candidates SET plan='{}' WHERE id=?",
            'different_candidate_failure': "UPDATE robot_candidates SET failure_code='HTTP_400' WHERE id=?",
        }
        for name, statement in mutations.items():
            with self.subTest(name=name):
                fixture = self.rejected_422()
                candidate = self.candidate(fixture, 'BTCUSDT')
                self.assertTrue(fixture.robot.replaceable_result(candidate))
                fixture.ledger.db.execute(statement, (candidate['id'],))
                self.assertFalse(fixture.robot.replaceable_result(self.candidate(fixture, 'BTCUSDT')))
                calls = len(fixture.calls)
                fixture.restart()
                result = fixture.ticks(3)
                self.assertEqual(len(fixture.calls), calls)
                self.assertEqual(fixture.screen_counts(), [1])
                if name == 'unknown_api':
                    self.assertEqual(result['failure_code'], 'ROBOT_REQUEST_NEEDS_REVIEW')

    def test_candidate_with_any_existing_order_intent_never_replaces(self):
        for state in ('REJECTED', 'ENTRY_PENDING', 'NEEDS_REVIEW'):
            with self.subTest(state=state):
                fixture = self.rejected_422()
                candidate = self.candidate(fixture, 'BTCUSDT')
                fixture.ledger.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
                    (candidate['id'], candidate['id'], 'BTCUSDT', state, '{}', None,
                     'HTTP_422', now(), now()))
                self.assertFalse(fixture.robot.replaceable_result(candidate))
                calls = len(fixture.calls)
                fixture.restart()
                fixture.ticks(3)
                self.assertEqual(len(fixture.calls), calls)
                self.assertEqual(fixture.screen_counts(), [1])

    def test_network_and_non_422_http_stop_without_replacement_or_replay(self):
        for error in ('NETWORK', 'HTTP_400', 'HTTP_401', 'HTTP_500'):
            with self.subTest(error=error):
                fixture = self.fixture({'BTCUSDT': error})
                fixture.screens = [['BTCUSDT'], ['SOLUSDT']]
                fixture.on()
                fixture.ticks(2)
                candidate = self.candidate(fixture, 'BTCUSDT')
                request = self.request(fixture, candidate['id'])
                self.assertEqual(request['state'], 'NEEDS_REVIEW')
                self.assertEqual((request['attempts'], request['output']), (1, None))
                self.assertEqual(fixture.ledger.db.execute('SELECT state FROM robot_jobs WHERE operation=?',
                    (candidate['id'],)).fetchone()[0], 'NEEDS_REVIEW')
                self.assertFalse(fixture.robot.replaceable_result(candidate))
                calls = len(fixture.calls)
                fixture.restart()
                self.assertEqual(fixture.ticks(3)['failure_code'], 'ROBOT_REQUEST_NEEDS_REVIEW')
                self.assertEqual(len(fixture.calls), calls)

    def test_old_422_needs_review_row_remains_blocked_without_maintenance(self):
        fixture = self.rejected_422()
        candidate = self.candidate(fixture, 'BTCUSDT')
        fixture.ledger.db.execute("UPDATE api_requests SET state='NEEDS_REVIEW' WHERE operation=?",
                                 (candidate['id'],))
        fixture.ledger.db.execute("UPDATE robot_jobs SET state='NEEDS_REVIEW' WHERE operation=?",
                                 (candidate['id'],))
        fixture.ledger.db.execute("UPDATE robot_cycles SET state='NEEDS_REVIEW'")
        before = self.request(fixture, candidate['id'])
        fixture.restart()
        self.assertEqual(fixture.ticks(4)['failure_code'], 'ROBOT_REQUEST_NEEDS_REVIEW')
        self.assertEqual(len(fixture.calls), 2)
        self.assertEqual(self.request(fixture, candidate['id']), before)
        self.assertEqual(fixture.ledger.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0], 0)

    def test_provider_terminal_422_same_operation_is_never_sent_again_after_restart(self):
        fixture = self.fixture({'BTCUSDT': 'HTTP_422'})
        context = dict(symbol='BTCUSDT')
        operation = 'terminal-provider-only'
        with self.assertRaisesRegex(Review, '^HTTP_422$'):
            fixture.client.ask(operation, ANALYSIS, SETUP_SCHEMA,
                               lambda value: setup(value, 'BTCUSDT'), context)
        before = self.request(fixture, operation)
        self.assertEqual((before['state'], before['attempts'], before['output'], before['idempotency']),
                         ('REJECTED_REQUEST_VALIDATION', 1, None, None))
        fixture.restart()
        for _ in range(2):
            with self.assertRaisesRegex(Review, '^NEUROAPI_REQUEST_NEEDS_REVIEW$'):
                fixture.client.ask(operation, ANALYSIS, SETUP_SCHEMA,
                                   lambda value: setup(value, 'BTCUSDT'), context)
        self.assertEqual(len(fixture.calls), 1)
        self.assertEqual(self.request(fixture, operation), before)

    def test_local_invalid_prompt_or_oversize_context_claims_no_api_row_or_http(self):
        fixture = self.fixture()
        cases = [('', None), (None, None), (True, None), ('x'*(MAX_CHARACTERS+1), None),
                 (ANALYSIS, {'symbol': 'BTCUSDT', 'text': 'x'*(MAX_CHARACTERS*MAX_MESSAGES+1)})]
        for index, (prompt, context) in enumerate(cases):
            with self.subTest(index=index), self.assertRaisesRegex(Review, '^'+LIMIT_ERROR+'$'):
                fixture.client.ask('unsent-'+str(index), prompt, SETUP_SCHEMA,
                                   lambda value: setup(value, 'BTCUSDT'), context)
        self.assertEqual(fixture.calls, [])
        self.assertEqual(fixture.ledger.db.execute('SELECT COUNT(*) FROM api_requests').fetchone()[0], 0)

    def test_local_packing_failure_is_terminal_job_without_unknown_or_coin_replacement(self):
        fixture = self.fixture()
        fixture.screens = [['BTCUSDT'], ['SOLUSDT']]
        fixture.on()
        fixture.robot.tick()
        with patch('worker.robot.ANALYSIS', 'x'*(MAX_CHARACTERS+1)):
            self.assertEqual(fixture.robot.tick()['failure_code'], LIMIT_ERROR)
        candidate = self.candidate(fixture, 'BTCUSDT')
        self.assertEqual((candidate['status'], candidate['failure_code'], candidate['plan']),
                         ('REJECTED', LIMIT_ERROR, None))
        self.assertIsNone(self.request(fixture, candidate['id']))
        self.assertEqual(fixture.ledger.db.execute('SELECT state FROM robot_jobs WHERE operation=?',
                         (candidate['id'],)).fetchone()[0], 'REQUEST_REJECTED')
        self.assertFalse(fixture.robot.replaceable_result(candidate))
        fixture.restart()
        result = fixture.ticks(3)
        self.assertEqual(result['wait_reason'], 'ROBOT_CYCLE_COMPLETE')
        self.assertIsNone(result['failure_code'])
        self.assertEqual(len(fixture.calls), 1)
        self.assertEqual(fixture.ledger.db.execute('SELECT COUNT(*) FROM api_requests').fetchone()[0], 1)

    def test_real_analysis_with_hundred_candles_per_frame_sends_bounded_body_and_stamps_hash(self):
        fixture = self.fixture({'BTCUSDT': 'TARGET'})
        fixture.market.lookback = 100
        public_get = fixture.market.transport
        def padded_prices(*args, **kwargs):
            code, headers, data = public_get(*args, **kwargs)
            if '/klines?' in args[1]:
                for candle in data:
                    candle[1:6] = [format(D(value), '.8f') for value in candle[1:6]]
            return code, headers, data
        fixture.market.transport = padded_prices
        fixture.screens = [['BTCUSDT']]
        fixture.on()
        fixture.robot.tick()
        self.assertEqual(fixture.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        body = fixture.calls[-1]
        self.assertEqual(body['prompt'], ANALYSIS)
        self.assertEqual(body['output_schema'], SETUP_SCHEMA)
        contents = [message['content'] for message in body['message_history']]
        self.assertGreater(len(contents), 1)
        self.assertLessEqual(len(contents), MAX_MESSAGES)
        self.assertTrue(all(0 < len(content) <= MAX_CHARACTERS for content in contents))
        packets = [json.loads(content) for content in contents]
        metadata = packets[0]['data']
        self.assertEqual(metadata['symbol'], 'BTCUSDT')
        self.assertEqual(metadata['risk_constraints']['reward_risk_policy'], 'NET_1_TO_2_NEAREST_TICK')
        self.assertEqual(metadata['risk_constraints']['entry_fee_rate'], '0.0005')
        frames = {'1h': [], '15m': []}
        for packet in packets[1:]:
            header = packet['context_partition']
            self.assertEqual(header['candle_start'], len(frames[header['timeframe']]))
            frames[header['timeframe']].extend(packet['data']['candles'])
            self.assertEqual(header['candle_end'], len(frames[header['timeframe']]))
        self.assertEqual({frame: len(candles) for frame, candles in frames.items()}, {'1h': 100, '15m': 100})
        candidate = self.candidate(fixture, 'BTCUSDT')
        request = self.request(fixture, candidate['id'])
        self.assertEqual((request['state'], request['attempts']), ('COMPLETE', 1))
        self.assertEqual(request['body_hash'], hashlib.sha256(
            json.dumps(body, sort_keys=True).encode()).hexdigest())
        self.assertEqual(fixture.ledger.db.execute('SELECT state FROM robot_jobs WHERE operation=?',
                         (candidate['id'],)).fetchone()[0], 'COMPLETE')


if __name__ == '__main__':
    unittest.main(verbosity=2)
