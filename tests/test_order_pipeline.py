"""Automatic pipeline fixtures: no credentials, sockets, or real exchange orders."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from support import Account, GOOD, RobotMarket, hold
from worker.account_state import account_state, SCREENING_ONE, SCREENING
from worker.core import D, Ledger, Review, day, now
from worker.neuroapi import NeuroAPI, SCREEN_SCHEMA, SCREEN_ONE_SCHEMA, SETUP_SCHEMA
from worker.order_gateway import OrderGateway, GatewayUnavailable, NOT_CONNECTED, build_intent
from worker.robot import Coordinator
from worker.robot_store import RobotStore


class FixtureGateway:
    """An explicitly injected test adapter, never a runtime fallback."""
    def __init__(self):
        self.submissions = []
        self.reconciliations = []
        self.on_submit = lambda intent: self.observation(intent)
        self.on_reconcile = lambda intent: self.observation(intent)

    def observation(self, intent, state='ENTRY_PENDING', filled='0', **changes):
        value = dict(source='BINANCE_FUTURES', state=state, symbol=intent['symbol'],
                     client_order_id=intent['client_order_id'], order_id='10',
                     filled_quantity=filled, observed_at=now())
        if D(filled): value.update(first_fill_at=now())
        if state == 'POSITION_PROTECTED':
            value.update(sl_confirmed=True, tp_confirmed=True, sl_order_id='11', tp_order_id='12')
        value.update(changes)
        return value

    def submit(self, intent):
        self.submissions.append(copy.deepcopy(intent))
        return self.on_submit(intent)

    def reconcile(self, intent):
        self.reconciliations.append(copy.deepcopy(intent))
        return self.on_reconcile(intent)


class OrderPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'ledger.sqlite3'
        self.calls = []
        self.screens = [['BTCUSDT', 'ETHUSDT']]
        self.decisions = {}
        self.account = Account()
        self.market = RobotMarket()
        self.gateway = None
        self.ledger = Ledger(self.path)
        self.addCleanup(lambda: self.ledger.db.close())
        self.make()

    def transport(self, method, url, headers, body, timeout):
        self.assertEqual(method, 'POST')
        self.assertEqual(url, 'https://api.neurobro.ai/api/v1/agent/ask')
        self.calls.append(copy.deepcopy(body))
        if body['output_schema'] in (SCREEN_SCHEMA, SCREEN_ONE_SCHEMA):
            value = dict(symbols=self.screens.pop(0))
        else:
            symbol = json.loads(body['message_history'][0]['content'])['symbol']
            choice = self.decisions.get(symbol, 'LONG')
            if choice == 'NETWORK': raise TimeoutError('private transport details')
            value = hold(symbol) if choice == 'HOLD' else {**GOOD, 'symbol': symbol}
        return 200, {}, dict(mode='smart', answer=None, output=value)

    def make(self):
        self.client = NeuroAPI(self.ledger, key='fixture-only', transport=self.transport)
        self.robot = Coordinator(self.ledger, self.client, self.market, lambda: self.account, gateway=self.gateway)
        self.store = self.robot.store

    def restart(self):
        self.ledger.db.close()
        self.ledger = Ledger(self.path)
        self.make()

    def on(self):
        self.store.configure(dict(robot_on=True))

    def ticks(self, count=4):
        for _ in range(count): result = self.robot.tick()
        return result

    def one_slot_ready(self):
        self.account.position('HYPEUSDT', '4.16')
        self.screens = [['BTCUSDT']]
        self.on()
        self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')

    def connected(self):
        self.gateway = FixtureGateway()
        self.robot.gateway = self.gateway
        return self.gateway

    def intent(self):
        return self.ledger.db.execute('SELECT * FROM order_intents ORDER BY rowid LIMIT 1').fetchone()

    def receipt_count(self):
        return self.ledger.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0]

    def test_off_claims_no_paid_call_or_account_read(self):
        result = self.ticks(5)
        self.assertEqual(result['bot_status'], 'OFF')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.account.calls, [])
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_jobs').fetchone()[0], 0)

    def test_hype_manual_one_slot_reaches_blocked_without_exchange_mutation(self):
        self.one_slot_ready()
        positions = copy.deepcopy(self.account.positions)
        result = self.ticks(2)
        self.assertEqual(result['bot_status'], 'EXECUTION_BLOCKED')
        self.assertEqual(result['failure_code'], NOT_CONNECTED)
        self.assertEqual(result['running_positions'], 1)
        self.assertEqual(result['available_slots'], 1)
        self.assertEqual(result['manual_exposure'], ['HYPEUSDT'])
        self.assertEqual(self.calls[0]['prompt'], SCREENING_ONE)
        self.assertEqual(self.calls[0]['output_schema'], SCREEN_ONE_SCHEMA)
        self.assertEqual(self.calls[1]['output_schema'], SETUP_SCHEMA)
        self.assertEqual(len(self.calls), 2)
        row = self.intent()
        self.assertIn(':robot-v9:0:analysis-v9:BTCUSDT', row['id'])
        self.assertEqual(row['state'], 'EXECUTION_BLOCKED')
        payload = json.loads(row['payload'])
        self.assertEqual(payload['symbol'], 'BTCUSDT')
        self.assertEqual(payload['risk_target_usdt'], '5')
        self.assertLessEqual(D(payload['risk_usdt']), D('5'))
        self.assertEqual(self.receipt_count(), 0)
        self.assertEqual(self.account.positions, positions)
        self.assertTrue(all(method == 'GET' for method, _ in self.account.calls))
        self.assertFalse({'setups', 'simulation', 'ticket', 'live_execution', 'mode'} & set(result))

    def test_blocked_restart_keeps_same_intent_and_no_new_paid_call(self):
        self.one_slot_ready()
        self.robot.tick()
        before = dict(self.intent())
        self.restart()
        self.ticks(8)
        after = dict(self.intent())
        self.assertEqual((after['id'], after['payload'], after['created']),
                         (before['id'], before['payload'], before['created']))
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0], 1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.receipt_count(), 0)

    def test_default_gateway_has_no_network_submission_or_reconciliation(self):
        self.one_slot_ready()
        candidate = self.ledger.db.execute('SELECT * FROM robot_candidates').fetchone()
        intent = build_intent(self.store.verified_plan(candidate), candidate['id'])
        with patch('worker.http_client.request', side_effect=AssertionError('Unexpected network')) as request:
            gateway = OrderGateway()
            self.assertFalse(gateway.status()['connected'])
            for method in (gateway.submit, gateway.reconcile):
                with self.assertRaises(GatewayUnavailable): method(intent)
            request.assert_not_called()
        self.assertEqual(self.receipt_count(), 0)

    def test_submit_claim_is_persisted_before_adapter_callback(self):
        self.one_slot_ready()
        gateway = self.connected()
        def submit(intent):
            connection = Ledger(self.path)
            try:
                row = connection.db.execute('SELECT state,payload FROM order_intents WHERE id=?', (intent['intent_id'],)).fetchone()
                self.assertEqual(row['state'], 'SUBMITTING')
                self.assertEqual(json.loads(row['payload']), intent)
                self.assertFalse(connection.db.in_transaction)
            finally: connection.db.close()
            return gateway.observation(intent)
        gateway.on_submit = submit
        self.assertEqual(self.robot.tick()['bot_status'], 'ENTRY_PENDING')
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(self.receipt_count(), 0)

    def test_manual_position_during_fee_preflight_blocks_third_position(self):
        self.one_slot_ready()
        gateway = self.connected()
        original = self.account.commission_rate
        def commission(symbol):
            value = original(symbol)
            self.account.position('ETHUSDT', '1')
            return value
        self.account.commission_rate = commission
        result = self.robot.tick()
        self.assertEqual(result['wait_reason'], 'ROBOT_CAPACITY_FULL')
        self.assertEqual(result['running_symbols'], ['ETHUSDT', 'HYPEUSDT'])
        self.assertEqual(result['available_slots'], 0)
        self.assertEqual(self.intent()['state'], 'READY_FOR_EXECUTION')
        self.assertEqual(gateway.submissions, [])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.receipt_count(), 0)
        self.assertFalse(self.ledger.db.execute("SELECT 1 FROM office_activity WHERE state='EXECUTING'").fetchone())

    def test_symbol_opened_manually_during_fee_preflight_is_not_submitted(self):
        self.one_slot_ready()
        gateway = self.connected()
        original = self.account.commission_rate
        def commission(symbol):
            value = original(symbol)
            self.account.position('BTCUSDT', '1')
            return value
        self.account.commission_rate = commission
        result = self.robot.tick()
        self.assertEqual(result['failure_code'], 'ROBOT_SYMBOL_EXPOSED')
        self.assertEqual(self.intent()['state'], 'REJECTED')
        self.assertEqual(gateway.submissions, [])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.receipt_count(), 0)

    def test_off_during_fee_preflight_leaves_unsubmitted_intent_for_later(self):
        self.one_slot_ready()
        gateway = self.connected()
        original = self.account.commission_rate
        def commission(symbol):
            value = original(symbol)
            self.store.configure(dict(robot_on=False))
            return value
        self.account.commission_rate = commission
        self.assertEqual(self.robot.tick()['wait_reason'], 'ROBOT_OFF')
        self.assertEqual(self.intent()['state'], 'READY_FOR_EXECUTION')
        self.assertEqual(gateway.submissions, [])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.receipt_count(), 0)

    def test_yesterday_blocked_intent_retires_without_adapter_and_allows_fresh_screen(self):
        self.one_slot_ready()
        self.robot.tick()
        self.assertEqual(self.intent()['state'], 'EXECUTION_BLOCKED')
        self.screens.append(['ETHUSDT'])
        tomorrow = (datetime.fromisoformat(day()) + timedelta(days=1)).date().isoformat()
        fee_count = sum('/commissionRate' in path for _, path in self.account.calls)
        with patch('worker.robot.day', return_value=tomorrow):
            self.assertEqual(self.robot.tick()['failure_code'], 'STALE_ORDER_INTENT')
            self.assertEqual(self.intent()['state'], 'REJECTED')
            self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(sum('/commissionRate' in path for _, path in self.account.calls), fee_count)
        self.assertEqual(self.screen_counts(), [1, 1])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.receipt_count(), 0)

    def test_implemented_adapter_dispatch_ignores_false_connection_switch(self):
        self.one_slot_ready()
        gateway = self.connected()
        gateway.connected = False
        self.assertEqual(self.robot.tick()['bot_status'], 'ENTRY_PENDING')
        self.assertEqual(self.robot.tick()['bot_status'], 'WAITING')
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(len(gateway.reconciliations), 1)
        self.assertEqual(self.receipt_count(), 0)

    def test_implemented_subclass_dispatch_does_not_use_display_status(self):
        class ImplementedGateway(FixtureGateway, OrderGateway):
            pass
        self.one_slot_ready()
        gateway = ImplementedGateway()
        self.robot.gateway = gateway
        self.assertFalse(gateway.status()['connected'])
        self.assertEqual(self.robot.tick()['bot_status'], 'ENTRY_PENDING')
        self.assertEqual(len(gateway.submissions), 1)

    def test_true_flag_cannot_replace_missing_transport(self):
        self.one_slot_ready()
        self.robot.gateway.connected = True
        self.assertEqual(self.robot.tick()['failure_code'], NOT_CONNECTED)
        self.assertEqual(self.intent()['state'], 'EXECUTION_BLOCKED')
        self.assertEqual(self.receipt_count(), 0)
        self.assertFalse(self.ledger.db.execute("SELECT 1 FROM office_activity WHERE state='EXECUTING'").fetchone())

    def test_missing_reconciliation_is_detected_before_submission_claim(self):
        self.one_slot_ready()
        gateway = self.connected()
        gateway.reconcile = None
        self.assertEqual(self.robot.tick()['failure_code'], NOT_CONNECTED)
        self.assertEqual(self.intent()['state'], 'EXECUTION_BLOCKED')
        self.assertEqual(gateway.submissions, [])
        self.assertEqual(self.receipt_count(), 0)

    def test_implementing_public_gateway_methods_needs_no_other_switch(self):
        self.one_slot_ready()
        fixture = FixtureGateway()
        with patch.object(OrderGateway, 'submit', fixture.submit), patch.object(OrderGateway, 'reconcile', fixture.reconcile):
            self.assertEqual(self.robot.tick()['bot_status'], 'ENTRY_PENDING')
            self.assertEqual(self.robot.tick()['bot_status'], 'WAITING')
        self.assertEqual(len(fixture.submissions), 1)
        self.assertEqual(len(fixture.reconciliations), 1)

    def test_existing_pending_order_can_reconcile_without_submit_implementation(self):
        self.one_slot_ready()
        fixture = self.connected()
        self.assertEqual(self.robot.tick()['bot_status'], 'ENTRY_PENDING')
        class ReaderGateway(OrderGateway):
            def reconcile(self, intent): return fixture.reconcile(intent)
        self.robot.gateway = ReaderGateway()
        self.assertEqual(self.robot.tick()['bot_status'], 'WAITING')
        self.assertEqual(self.intent()['state'], 'ENTRY_PENDING')
        self.assertEqual(len(fixture.submissions), 1)
        self.assertEqual(len(fixture.reconciliations), 1)

    def test_missing_reader_preserves_existing_pending_order_and_slot(self):
        self.one_slot_ready()
        fixture = self.connected()
        self.assertEqual(self.robot.tick()['bot_status'], 'ENTRY_PENDING')
        before = dict(self.intent())
        self.robot.gateway = OrderGateway()
        self.assertEqual(self.robot.tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(dict(self.intent()), before)
        current_account = account_state(self.account, self.store, day())
        self.assertEqual(self.robot.execution_slots(current_account, day()), 0)
        self.assertEqual(len(fixture.submissions), 1)

    def test_gateway_unavailable_after_claim_is_unknown_and_never_replayed(self):
        self.one_slot_ready()
        gateway = self.connected()
        def unavailable(intent): raise GatewayUnavailable(NOT_CONNECTED)
        gateway.on_submit = unavailable
        self.assertEqual(self.robot.tick()['failure_code'], 'ORDER_OUTCOME_UNKNOWN')
        self.assertEqual(self.intent()['state'], 'NEEDS_REVIEW')
        self.restart()
        self.ticks(4)
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(gateway.reconciliations, [])
        self.assertEqual(self.receipt_count(), 0)

    def test_submit_timeout_never_replays_after_restart(self):
        self.one_slot_ready()
        gateway = self.connected()
        def uncertain(intent): raise TimeoutError('private details must not be persisted')
        gateway.on_submit = uncertain
        self.assertEqual(self.robot.tick()['failure_code'], 'ORDER_OUTCOME_UNKNOWN')
        self.restart()
        result = self.ticks(8)
        self.assertEqual(result['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(gateway.reconciliations, [])
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn('private', self.intent()['failure_code'])
        self.assertEqual(self.receipt_count(), 0)

    def test_interrupted_submitting_claim_is_never_replayed(self):
        self.one_slot_ready()
        self.robot.tick()
        self.ledger.db.execute("UPDATE order_intents SET state='SUBMITTING',failure_code=NULL")
        gateway = self.connected()
        self.restart()
        self.assertEqual(self.ticks(3)['failure_code'], 'ORDER_OUTCOME_UNKNOWN')
        self.assertEqual(gateway.submissions, [])
        self.assertEqual(gateway.reconciliations, [])
        self.assertEqual(len(self.calls), 2)

    def test_mismatched_ack_is_needs_review_without_receipt(self):
        self.one_slot_ready()
        gateway = self.connected()
        gateway.on_submit = lambda intent: gateway.observation(intent, symbol='ETHUSDT')
        result = self.robot.tick()
        self.assertEqual(result['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.receipt_count(), 0)
        self.restart()
        self.ticks(3)
        self.assertEqual(len(gateway.submissions), 1)

    def test_unprotected_fill_is_needs_review_without_fabricated_receipt(self):
        self.one_slot_ready()
        gateway = self.connected()
        gateway.on_submit = lambda intent: gateway.observation(intent, 'POSITION_PROTECTED', '2.5', sl_confirmed=False)
        self.assertEqual(self.robot.tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.receipt_count(), 0)

    def test_pending_zero_fill_ack_does_not_count_an_entry(self):
        self.one_slot_ready()
        gateway = self.connected()
        self.assertEqual(self.robot.tick()['bot_status'], 'ENTRY_PENDING')
        self.assertEqual(self.intent()['state'], 'ENTRY_PENDING')
        self.assertEqual(self.receipt_count(), 0)
        self.assertEqual(self.store.entries(day()), 0)
        self.ticks(3)
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(len(gateway.reconciliations), 3)
        self.assertEqual(len(self.calls), 2)

    def test_real_fixture_protected_fill_creates_one_stable_receipt(self):
        self.one_slot_ready()
        gateway = self.connected()
        self.robot.tick()
        first_fill = now()
        def reconcile(intent):
            self.account.positions = [self.account.positions[0], dict(symbol='BTCUSDT', positionSide='BOTH', positionAmt='2.5')]
            return gateway.observation(intent, 'POSITION_PROTECTED', '2.5', first_fill_at=first_fill)
        gateway.on_reconcile = reconcile
        result = self.robot.tick()
        self.assertEqual(self.intent()['state'], 'POSITION_PROTECTED')
        receipt = dict(self.ledger.db.execute('SELECT * FROM robot_entry_receipts').fetchone())
        self.assertEqual(receipt['id'], json.loads(self.intent()['payload'])['client_order_id'])
        expected_day = datetime.fromisoformat(first_fill).astimezone(ZoneInfo('Asia/Bangkok')).date().isoformat()
        self.assertEqual(receipt['entry_day'], expected_day)
        self.assertEqual(receipt['confirmed_at'], first_fill)
        self.restart()
        self.ticks(4)
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual(dict(self.ledger.db.execute('SELECT * FROM robot_entry_receipts').fetchone()), receipt)
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(len(self.calls), 2)

    def test_two_actual_positions_block_all_new_paid_research(self):
        self.account.position('HYPEUSDT', '4.16')
        self.account.position('ETHUSDT', '1')
        self.on()
        self.assertEqual(self.ticks(5)['wait_reason'], 'ROBOT_CAPACITY_FULL')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.receipt_count(), 0)

    def test_manual_symbol_opened_after_screening_does_not_consume_remaining_analysis_slot(self):
        self.on()
        self.robot.tick()
        self.account.position('BTCUSDT', '1')
        before = copy.deepcopy(self.account.positions)
        self.assertEqual(self.robot.tick()['failure_code'], 'ROBOT_SYMBOL_EXPOSED')
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.ticks(4)
        self.assertEqual(self.screen_counts(), [2])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual({row['symbol']: row['status'] for row in self.ledger.db.execute(
            'SELECT symbol,status FROM robot_candidates')},
            {'BTCUSDT': 'REJECTED', 'ETHUSDT': 'EXECUTION_BLOCKED'})
        self.assertEqual(self.account.positions, before)

    def test_ready_symbol_now_manual_does_not_double_reserve_other_slot(self):
        self.on()
        self.ticks(2)
        self.account.position('BTCUSDT', '1')
        before = copy.deepcopy(self.account.positions)
        self.assertEqual(self.robot.tick()['failure_code'], 'ROBOT_SYMBOL_EXPOSED')
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[-1]['output_schema'], SETUP_SCHEMA)
        self.assertEqual(json.loads(self.calls[-1]['message_history'][0]['content'])['symbol'], 'ETHUSDT')
        gateway = self.connected()
        self.assertEqual(self.robot.tick()['bot_status'], 'ENTRY_PENDING')
        self.assertEqual([value['symbol'] for value in gateway.submissions], ['ETHUSDT'])
        self.assertEqual(self.account.positions, before)
        self.assertEqual(self.receipt_count(), 0)

    def test_historical_daily_receipts_do_not_limit_concurrent_research(self):
        for index in range(5):
            self.ledger.db.execute('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)',
                                   ('fixture-' + str(index), 'BTCUSDT', day(), now()))
        self.on()
        self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(self.calls[0]['prompt'], SCREENING)
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(self.ticks(3)['bot_status'], 'EXECUTION_BLOCKED')
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.receipt_count(), 5)
        self.assertTrue(all(':robot-v9:5:analysis-v9:' in row['id'] for row in
                            self.ledger.db.execute('SELECT id FROM robot_candidates')))

    def screen_counts(self):
        return [1 if call['output_schema'] == SCREEN_ONE_SCHEMA else 2
                for call in self.calls if call['output_schema'] in (SCREEN_SCHEMA, SCREEN_ONE_SCHEMA)]

    def current_cycle(self):
        row = self.ledger.db.execute('SELECT * FROM robot_cycles ORDER BY rowid DESC LIMIT 1').fetchone()
        return row, json.loads(row['data'])

    def test_ready_candidate_does_not_stop_single_hold_replacement(self):
        self.screens = [['BTCUSDT', 'ETHUSDT'], ['SOLUSDT']]
        self.decisions['ETHUSDT'] = 'HOLD'
        self.on()
        self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(self.robot.tick()['bot_status'], 'HOLD')
        self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(self.ticks(5)['bot_status'], 'EXECUTION_BLOCKED')
        self.assertEqual(self.screen_counts(), [2, 1])
        self.assertEqual(len(self.calls), 5)
        self.assertEqual({row['symbol']: row['status'] for row in self.ledger.db.execute(
            'SELECT symbol,status FROM robot_candidates')},
            {'BTCUSDT': 'EXECUTION_BLOCKED', 'ETHUSDT': 'HOLD', 'SOLUSDT': 'READY_FOR_EXECUTION'})
        _, data = self.current_cycle()
        self.assertEqual((data['target'], data['replacements']), (2, 1))
        self.assertEqual(self.receipt_count(), 0)
        self.restart()
        self.ticks(6)
        self.assertEqual(len(self.calls), 5)

    def test_two_simultaneous_holds_replace_exactly_two_after_restart(self):
        self.screens = [['BTCUSDT', 'ETHUSDT'], ['SOLUSDT', 'BNBUSDT']]
        self.decisions = dict(BTCUSDT='HOLD', ETHUSDT='HOLD')
        self.on()
        self.robot.tick()
        self.assertEqual(self.robot.tick()['bot_status'], 'HOLD')
        self.restart()
        self.assertEqual(self.robot.tick()['bot_status'], 'HOLD')
        self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(self.ticks(2)['bot_status'], 'READY_FOR_EXECUTION')
        self.ticks(4)
        self.assertEqual(self.screen_counts(), [2, 2])
        self.assertEqual(len(self.calls), 6)
        _, data = self.current_cycle()
        self.assertEqual((data['target'], data['replacements']), (2, 1))
        self.assertEqual(data['round_symbols'], ['SOLUSDT', 'BNBUSDT'])
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM robot_candidates WHERE status IN ('READY_FOR_EXECUTION','EXECUTION_BLOCKED')").fetchone()[0], 2)

    def test_hold_replacement_exhausts_three_rounds_and_stays_stable(self):
        self.market.symbols = (*self.market.symbols, 'DOGEUSDT', 'TRXUSDT')
        self.screens = [['BTCUSDT', 'ETHUSDT'], ['SOLUSDT', 'BNBUSDT'],
                        ['XRPUSDT', 'ADAUSDT'], ['DOGEUSDT', 'TRXUSDT']]
        self.decisions = {symbol: 'HOLD' for pair in self.screens for symbol in pair}
        self.on()
        result = self.ticks(13)
        self.assertEqual(result['bot_status'], 'INSUFFICIENT_ACTIONABLE_SETUPS')
        self.assertEqual(self.screen_counts(), [2, 2, 2, 2])
        self.assertEqual(len(self.calls), 12)
        row, data = self.current_cycle()
        self.assertEqual(row['state'], 'COMPLETE')
        self.assertEqual(data['replacements'], 3)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_candidates').fetchone()[0], 8)
        self.restart()
        self.assertEqual(self.ticks(8)['bot_status'], 'INSUFFICIENT_ACTIONABLE_SETUPS')
        self.assertEqual(len(self.calls), 12)
        self.assertEqual(self.receipt_count(), 0)

    def test_technical_rejection_does_not_replace_historical_hold(self):
        self.screens = [['BTCUSDT', 'ETHUSDT'], ['SOLUSDT']]
        self.decisions['ETHUSDT'] = 'HOLD'
        self.on()
        self.ticks(4)
        with patch('worker.robot.risk_check', side_effect=Review('RISK_LIMIT_EXCEEDED')):
            self.assertEqual(self.robot.tick()['bot_status'], 'REJECTED')
        self.ticks(5)
        self.assertEqual(self.screen_counts(), [2, 1])
        self.assertEqual(len(self.calls), 5)
        row, data = self.current_cycle()
        self.assertEqual(row['state'], 'COMPLETE')
        self.assertEqual(data['replacements'], 1)
        self.assertEqual(self.ledger.db.execute("SELECT state FROM api_requests WHERE operation LIKE '%:SOLUSDT'").fetchone()[0], 'COMPLETE')
        self.assertEqual(self.ledger.db.execute("SELECT status FROM robot_candidates WHERE symbol='SOLUSDT'").fetchone()[0], 'REJECTED')
        self.restart()
        self.ticks(5)
        self.assertEqual(len(self.calls), 5)

    def test_excluded_screening_symbol_is_not_a_hold_replacement_trigger(self):
        self.account.position('HYPEUSDT', '4.16')
        self.screens = [['HYPEUSDT']]
        self.on()
        self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_NO_ELIGIBLE_SYMBOLS')
        self.assertEqual(self.ticks(4)['bot_status'], 'INSUFFICIENT_ACTIONABLE_SETUPS')
        self.assertEqual(self.screen_counts(), [1])
        self.assertEqual(len(self.calls), 1)
        row, data = self.current_cycle()
        self.assertEqual((row['state'], data['replacements']), ('COMPLETE', 0))
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_candidates').fetchone()[0], 0)
        self.assertEqual(self.account.positions[0]['symbol'], 'HYPEUSDT')

    def test_repeated_old_hold_symbol_does_not_create_another_replacement(self):
        self.account.position('HYPEUSDT', '4.16')
        self.screens = [['BTCUSDT'], ['BTCUSDT']]
        self.decisions['BTCUSDT'] = 'HOLD'
        self.on()
        self.robot.tick()
        self.assertEqual(self.robot.tick()['bot_status'], 'HOLD')
        self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_NO_ELIGIBLE_SYMBOLS')
        self.assertEqual(self.ticks(5)['bot_status'], 'INSUFFICIENT_ACTIONABLE_SETUPS')
        self.assertEqual(self.screen_counts(), [1, 1])
        self.assertEqual(len(self.calls), 3)
        _, data = self.current_cycle()
        self.assertEqual(data['round_symbols'], [])
        self.assertEqual(data['replacements'], 1)

    def test_legacy_active_cycle_is_not_resumed_by_current_namespace(self):
        previous = day() + ':robot-v8:0'
        previous_data = dict(target=2, queue=['ETHUSDT'], seen=['BTCUSDT'], screen=0,
                             replacements=0, replacement_due=True)
        self.ledger.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',
                               (previous, day(), 0, 'ACTIVE', json.dumps(previous_data)))
        self.on()
        self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.screen_counts(), [2])
        old = self.ledger.db.execute('SELECT * FROM robot_cycles WHERE id=?', (previous,)).fetchone()
        self.assertEqual(json.loads(old['data']), previous_data)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_cycles').fetchone()[0], 2)

    def test_off_after_ready_prevents_new_gateway_submission(self):
        self.one_slot_ready()
        gateway = self.connected()
        self.store.configure(dict(robot_on=False))
        self.assertEqual(self.ticks(3)['bot_status'], 'OFF')
        self.assertEqual(gateway.submissions, [])
        self.assertEqual(gateway.reconciliations, [])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.receipt_count(), 0)

    def test_off_keeps_pending_order_journal_without_automatic_replay(self):
        self.one_slot_ready()
        gateway = self.connected()
        self.robot.tick()
        self.store.configure(dict(robot_on=False))
        self.restart()
        self.assertEqual(self.ticks(3)['bot_status'], 'OFF')
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(gateway.reconciliations, [])
        self.assertEqual(self.intent()['state'], 'ENTRY_PENDING')
        self.assertEqual(len(self.calls), 2)

    def test_yesterday_protected_position_allows_today_remaining_slot(self):
        gateway, captured = self.protected_fixture()
        self.account.positions = [row for row in self.account.positions if row['symbol'] != 'HYPEUSDT']
        gateway.on_reconcile = lambda intent: gateway.observation(intent, 'POSITION_PROTECTED', '2.5', **captured)
        self.screens.append(['ETHUSDT'])
        tomorrow = (datetime.fromisoformat(day()) + timedelta(days=1)).date().isoformat()
        with patch('worker.robot.day', return_value=tomorrow):
            self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
            self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.store.entries(tomorrow), 0)
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_cycles').fetchone()[0], 2)

    def test_two_pending_orders_reserve_capacity_and_reconcile_once_each(self):
        self.on()
        gateway = self.connected()
        self.ticks(5)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(gateway.submissions), 2)
        self.assertEqual(self.receipt_count(), 0)
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM order_intents WHERE state='ENTRY_PENDING'").fetchone()[0], 2)
        before = len(gateway.reconciliations)
        result = self.robot.tick()
        self.assertEqual(len(gateway.reconciliations) - before, 2)
        self.assertEqual(result['wait_reason'], 'ROBOT_CAPACITY_FULL')
        self.assertEqual(self.store.entries(day()), 0)
        self.restart()
        self.ticks(5)
        self.assertEqual(len(gateway.submissions), 2)
        self.assertEqual(len(self.calls), 3)

    def test_protected_intent_not_yet_in_account_cannot_be_screened_again(self):
        gateway, captured = self.protected_fixture()
        # An exchange GET may temporarily lag a confirmed adapter observation.
        self.account.positions = []
        gateway.on_reconcile = lambda intent: gateway.observation(intent, 'POSITION_PROTECTED',
            intent['entry']['quantity'], **captured)
        self.screens.append(['BTCUSDT'])
        self.assertEqual(self.robot.tick()['wait_reason'], 'ROBOT_CYCLE_COMPLETE')
        self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_NO_ELIGIBLE_SYMBOLS')
        self.assertEqual(self.ticks(4)['bot_status'], 'INSUFFICIENT_ACTIONABLE_SETUPS')
        self.assertEqual(self.screen_counts(), [1, 1])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_candidates').fetchone()[0], 1)

    def test_protected_position_does_not_starve_original_eth_queue(self):
        self.on()
        gateway = self.connected()
        first_fill = {}
        def submit(intent):
            if intent['symbol'] == 'ETHUSDT': return gateway.observation(intent)
            first_fill['at'] = now()
            self.account.position('BTCUSDT', '2.5')
            return gateway.observation(intent, 'POSITION_PROTECTED', '2.5', first_fill_at=first_fill['at'])
        gateway.on_submit = submit
        gateway.on_reconcile = lambda intent: (gateway.observation(intent) if intent['symbol'] == 'ETHUSDT' else
            gateway.observation(intent, 'POSITION_PROTECTED', '2.5', first_fill_at=first_fill['at']))
        self.ticks(5)
        self.assertEqual([intent['symbol'] for intent in gateway.submissions], ['BTCUSDT', 'ETHUSDT'])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_cycles').fetchone()[0], 1)
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual({row['symbol']: row['state'] for row in self.ledger.db.execute('SELECT symbol,state FROM order_intents')},
                         {'BTCUSDT': 'POSITION_PROTECTED', 'ETHUSDT': 'ENTRY_PENDING'})

    def test_reconcile_failure_stops_before_other_order_or_research(self):
        self.on()
        gateway = self.connected()
        self.ticks(5)
        before = len(gateway.reconciliations)
        def uncertain(intent): raise TimeoutError('uncertain reconcile')
        gateway.on_reconcile = uncertain
        self.assertEqual(self.robot.tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(len(gateway.reconciliations) - before, 1)
        self.assertEqual(len(gateway.submissions), 2)
        self.assertEqual(len(self.calls), 3)
        self.restart()
        self.ticks(3)
        self.assertEqual(len(gateway.reconciliations) - before, 1)

    def test_closed_history_allows_next_bounded_research_cycle(self):
        self.one_slot_ready()
        gateway = self.connected()
        first_fill = {}
        def submit(intent):
            first_fill['at'] = now()
            self.account.position('BTCUSDT', '2.5')
            return gateway.observation(intent, 'POSITION_PROTECTED', '2.5', first_fill_at=first_fill['at'])
        gateway.on_submit = submit
        self.robot.tick()
        def closed(intent):
            self.account.positions = [row for row in self.account.positions if row['symbol'] != 'BTCUSDT']
            closed_at = now()
            return gateway.observation(intent, 'CLOSED', '2.5', first_fill_at=first_fill['at'],
                                       closed_at=closed_at, exit_order_id='11')
        gateway.on_reconcile = closed
        self.screens.append(['ETHUSDT'])
        self.ticks(4)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual({row['symbol']: row['status'] for row in self.ledger.db.execute('SELECT symbol,status FROM robot_candidates')},
                         {'BTCUSDT': 'CLOSED', 'ETHUSDT': 'READY_FOR_EXECUTION'})
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_cycles').fetchone()[0], 2)
        self.assertEqual(self.receipt_count(), 1)

    def test_rejected_positive_fill_is_needs_review(self):
        self.one_slot_ready()
        gateway = self.connected()
        gateway.on_submit = lambda intent: gateway.observation(intent, 'REJECTED', '0.5')
        self.assertEqual(self.robot.tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.receipt_count(), 0)

    def test_first_fill_before_order_intent_cannot_fabricate_receipt(self):
        self.one_slot_ready()
        gateway = self.connected()
        gateway.on_submit = lambda intent: gateway.observation(intent, 'POSITION_PROTECTED', '2.5',
            first_fill_at='2001-01-01T00:00:00+00:00')
        self.assertEqual(self.robot.tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.receipt_count(), 0)

    def test_closed_observation_requires_real_exit_evidence(self):
        self.one_slot_ready()
        gateway = self.connected()
        gateway.on_submit = lambda intent: gateway.observation(intent, 'CLOSED', '2.5')
        self.assertEqual(self.robot.tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.receipt_count(), 0)

    def test_tampered_intent_is_never_sent_to_adapter(self):
        self.one_slot_ready()
        self.robot.tick()
        row = self.intent()
        payload = json.loads(row['payload'])
        payload['entry']['quantity'] = '250'
        self.ledger.db.execute('UPDATE order_intents SET payload=?', (json.dumps(payload),))
        gateway = self.connected()
        self.restart()
        self.assertEqual(self.robot.tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(gateway.submissions, [])
        self.assertEqual(gateway.reconciliations, [])
        self.assertEqual(self.receipt_count(), 0)

    def test_tampered_pending_intent_is_not_reconciled(self):
        self.one_slot_ready()
        gateway = self.connected()
        self.robot.tick()
        row = self.intent()
        payload = json.loads(row['payload'])
        payload['symbol'] = 'ETHUSDT'
        self.ledger.db.execute('UPDATE order_intents SET payload=?', (json.dumps(payload),))
        self.restart()
        self.assertEqual(self.robot.tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(gateway.reconciliations, [])
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(self.receipt_count(), 0)

    def protected_fixture(self):
        self.one_slot_ready()
        gateway = self.connected()
        captured = {}
        def submit(intent):
            captured['first_fill_at'] = now()
            self.account.position('BTCUSDT', '2.5')
            return gateway.observation(intent, 'POSITION_PROTECTED', '2.5', **captured)
        gateway.on_submit = submit
        self.assertEqual(self.robot.tick()['bot_status'], 'POSITION_PROTECTED')
        return gateway, captured

    def test_reconcile_cannot_reduce_cumulative_filled_quantity(self):
        gateway, captured = self.protected_fixture()
        gateway.on_reconcile = lambda intent: gateway.observation(intent, 'POSITION_PROTECTED', '1', **captured)
        self.assertEqual(self.robot.tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.receipt_count(), 1)

    def test_reconcile_cannot_change_entry_order_identity(self):
        gateway, captured = self.protected_fixture()
        gateway.on_reconcile = lambda intent: gateway.observation(intent, 'POSITION_PROTECTED', '2.5', order_id='99', **captured)
        self.assertEqual(self.robot.tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.receipt_count(), 1)

    def test_reconcile_cannot_change_first_fill_time_or_receipt(self):
        gateway, captured = self.protected_fixture()
        receipt = dict(self.ledger.db.execute('SELECT * FROM robot_entry_receipts').fetchone())
        gateway.on_reconcile = lambda intent: gateway.observation(intent, 'POSITION_PROTECTED', '2.5', first_fill_at=now())
        self.assertEqual(self.robot.tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(dict(self.ledger.db.execute('SELECT * FROM robot_entry_receipts').fetchone()), receipt)

    def test_filled_position_cannot_regress_to_pending(self):
        gateway, captured = self.protected_fixture()
        gateway.on_reconcile = lambda intent: gateway.observation(intent)
        self.assertEqual(self.robot.tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.receipt_count(), 1)

    def test_fill_receipt_uses_bangkok_fill_day_across_server_midnight(self):
        observed = datetime(2026, 10, 7, 17, 1, tzinfo=timezone.utc)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None): return observed.astimezone(tz) if tz else observed.replace(tzinfo=None)
        with patch('worker.robot.day', return_value='2026-10-07'):
            self.one_slot_ready()
            gateway = self.connected()
            gateway.on_submit = lambda intent: gateway.observation(intent, 'POSITION_PROTECTED', '2.5',
                observed_at=observed.isoformat(), first_fill_at='2026-10-07T16:59:00+00:00')
            with patch('worker.robot.now', return_value='2026-10-07T16:58:00+00:00'), patch('worker.robot.datetime', Clock):
                result = self.robot.tick()
        self.assertEqual(result['bot_status'], 'POSITION_PROTECTED')
        self.assertEqual(self.store.entries('2026-10-07'), 1)
        self.assertEqual(self.store.entries('2026-10-08'), 0)
        self.assertEqual(self.receipt_count(), 1)

    def test_previous_approved_and_paper_setups_are_retired_never_dispatched(self):
        legacy_path = Path(self.temporary.name) / 'previous.sqlite3'
        previous = Ledger(legacy_path)
        self.addCleanup(previous.db.close)
        previous.db.executescript('''
          CREATE TABLE robot_settings(id INTEGER PRIMARY KEY,enabled INTEGER,risk TEXT);
          INSERT INTO robot_settings VALUES(1,1,'20');
          CREATE TABLE robot_setups(id TEXT PRIMARY KEY,symbol TEXT,status TEXT,plan TEXT);
          INSERT INTO robot_setups VALUES('old-ready','BTCUSDT','SETUP_READY','{"mode":"DRY_RUN"}');
          INSERT INTO robot_setups VALUES('old-approved','ETHUSDT','APPROVED','{"mode":"DRY_RUN"}');
          CREATE TABLE robot_decisions(setup_id TEXT PRIMARY KEY,decision TEXT);
          INSERT INTO robot_decisions VALUES('old-approved','APPROVED');
          CREATE TABLE robot_simulations(setup_id TEXT,result TEXT);
          INSERT INTO robot_simulations VALUES('old-approved','old paper result');
        ''')
        client = NeuroAPI(previous, key='fixture-only', transport=self.transport)
        gateway = FixtureGateway()
        coordinator = Coordinator(previous, client, self.market, lambda: self.account, gateway=gateway)
        self.assertFalse(coordinator.store.settings()['robot_on'])
        self.assertEqual(coordinator.tick()['bot_status'], 'OFF')
        self.assertEqual(gateway.submissions, [])
        self.assertEqual(self.calls, [])
        tables = {row[0] for row in previous.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse({'robot_setups', 'robot_decisions', 'robot_simulations'} & tables)
        self.assertEqual(previous.db.execute('SELECT COUNT(*) FROM robot_candidates').fetchone()[0], 0)
        self.assertEqual(previous.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0], 0)
        self.assertEqual(previous.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 0)
        archive = Ledger(legacy_path.parent / 'ledger-pre-order.sqlite3')
        try:
            self.assertEqual(archive.db.execute('SELECT COUNT(*) FROM robot_setups').fetchone()[0], 2)
        finally: archive.db.close()

    def test_account_loop_newer_success_survives_final_analysis_report(self):
        self.one_slot_ready_prepare_screen()
        original = self.client.transport
        connection = Ledger(self.path)
        self.addCleanup(connection.db.close)
        publisher = RobotStore(connection.db, initialize=False)
        observed = {}
        def transport(*args, **kwargs):
            value = original(*args, **kwargs)
            self.account.position('BTCUSDT', '2.5')
            account = account_state(self.account, publisher, day())
            observed.update(account)
            publisher.report_account(account)
            return value
        self.client.transport = transport
        result = self.robot.tick()
        self.assertEqual(result['running_symbols'], ['BTCUSDT', 'HYPEUSDT'])
        self.assertEqual(result['available_slots'], 0)
        self.assertEqual(result['account_checked_at'], observed['account_checked_at'])
        self.assertEqual(result['bot_status'], 'READY_FOR_EXECUTION')

    def one_slot_ready_prepare_screen(self):
        self.account.position('HYPEUSDT', '4.16')
        self.screens = [['BTCUSDT']]
        self.on()
        self.robot.tick()

    def test_account_loop_newer_failure_survives_final_analysis_report(self):
        self.one_slot_ready_prepare_screen()
        original = self.client.transport
        connection = Ledger(self.path)
        self.addCleanup(connection.db.close)
        publisher = RobotStore(connection.db, initialize=False)
        def transport(*args, **kwargs):
            value = original(*args, **kwargs)
            publisher.report_account(reason='BINANCE_ACCOUNT_UNAVAILABLE')
            return value
        self.client.transport = transport
        result = self.robot.tick()
        self.assertEqual(result['account_failure_code'], 'BINANCE_ACCOUNT_UNAVAILABLE')
        self.assertIsNone(result['available_slots'])
        self.assertIsNone(result['running_symbols'])
        self.assertIsNone(result['usdt_wallet_balance'])


if __name__ == '__main__': unittest.main()
