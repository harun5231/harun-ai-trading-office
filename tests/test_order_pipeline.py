"""Automatic pipeline fixtures: no credentials, sockets, or real exchange orders."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from support import Account, GOOD, RobotMarket, hold
from worker.account_state import account_state, SCREENING_ONE, SCREENING
from worker.binance_private import BinanceReadOnly
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
        if D(filled): value.update(first_fill_at=value['observed_at'])
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

    def gateway_reader_off_on_first_get(self):
        # Use the actual read boundary without key files, sockets, or writes.
        calls = []
        def transport(url, headers):
            path = urlsplit(url).path
            calls.append(path)
            if path == '/fapi/v1/time':
                self.store.configure(dict(robot_on=False))
                return dict(serverTime=int(datetime.now(timezone.utc).timestamp() * 1000))
            self.assertEqual(path, '/fapi/v1/openOrders')
            return []
        reader = BinanceReadOnly.__new__(BinanceReadOnly)
        reader._key, reader._secret = 'fixture-api-key-not-real', 'fixture-secret-not-real'
        reader._transport, reader._clock = transport, lambda: 100.0
        reader._server, reader._synced = None, None
        return reader, calls

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

    def test_off_during_market_preflight_stops_reads_without_rejecting_intent(self):
        self.one_slot_ready()
        gateway = self.connected()
        original = self.market.transport
        market_count, account_count = len(self.market.calls), len(self.account.calls)
        def transport(*args, **kwargs):
            value = original(*args, **kwargs)
            self.store.configure(dict(robot_on=False))
            return value
        with patch.object(self.market, 'transport', side_effect=transport):
            result = self.robot.tick()
        self.assertEqual((result['bot_status'], result['wait_reason']), ('OFF', 'ROBOT_OFF'))
        self.assertEqual(len(self.market.calls) - market_count, 1)
        self.assertIn('/premiumIndex?', self.market.calls[-1][1])
        self.assertEqual(len(self.account.calls) - account_count, 1)  # The initial live snapshot only.
        self.assertEqual(self.intent()['state'], 'READY_FOR_EXECUTION')
        self.assertEqual(self.ledger.db.execute('SELECT status FROM robot_candidates').fetchone()[0], 'READY_FOR_EXECUTION')
        self.assertFalse(self.ledger.db.execute("SELECT 1 FROM office_activity WHERE state='EXECUTING'").fetchone())
        self.assertEqual(gateway.submissions, [])
        self.assertEqual(len(self.calls), 2)
        self.restart()
        self.on()
        self.assertEqual(self.robot.tick()['bot_status'], 'ENTRY_PENDING')
        self.assertEqual(len(gateway.submissions), 1)

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

    def test_two_confirmed_daily_entries_block_research_even_when_closed(self):
        for index in range(2):
            self.ledger.db.execute('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)',
                                   ('fixture-' + str(index), 'BTCUSDT', day(), now()))
        self.on()
        result = self.ticks(5)
        self.assertEqual(result['wait_reason'], 'ROBOT_CAPACITY_FULL')
        self.assertEqual(result['running_positions'], 0)
        self.assertEqual(result['available_slots'], 0)
        self.assertEqual(result['bot_entries_today'], 2)
        self.assertEqual(self.calls, [])
        self.restart()
        self.assertEqual(self.ticks(5)['wait_reason'], 'ROBOT_CAPACITY_FULL')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.receipt_count(), 2)

    def test_new_day_resets_entry_budget_and_respects_zero_one_two_carryovers(self):
        previous = day()
        today = (datetime.fromisoformat(previous) + timedelta(days=1)).date().isoformat()
        for carried in (0, 1, 2):
            with self.subTest(carried=carried):
                ledger = Ledger(Path(self.temporary.name) / ('carry-' + str(carried) + '.sqlite3'))
                self.addCleanup(ledger.db.close)
                account = Account()
                for symbol in ('BTCUSDT', 'ETHUSDT')[:carried]: account.position(symbol, '1')
                positions = copy.deepcopy(account.positions)
                calls = []
                def transport(method, url, headers, body, timeout):
                    self.assertEqual(method, 'POST')
                    calls.append(copy.deepcopy(body))
                    return 200, {}, dict(mode='smart', answer=None, output=dict(
                        symbols=['SOLUSDT', 'BNBUSDT'] if carried == 0 else ['SOLUSDT']))
                client = NeuroAPI(ledger, key='fixture-only', transport=transport)
                coordinator = Coordinator(ledger, client, self.market, lambda: account)
                for index in range(2):
                    ledger.db.execute('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)',
                                      ('yesterday-' + str(index), 'XRPUSDT', previous, now()))
                coordinator.store.configure(dict(robot_on=True))
                with patch('worker.robot.day', return_value=today), patch('worker.robot_store.day', return_value=today):
                    result = coordinator.tick()
                self.assertEqual(result['running_positions'], carried)
                self.assertEqual(result['available_slots'], 2 - carried)
                self.assertEqual(result['bot_entries_today'], 0)
                self.assertEqual(account.positions, positions)
                if carried == 2:
                    self.assertEqual(result['wait_reason'], 'ROBOT_CAPACITY_FULL')
                    self.assertEqual(calls, [])
                else:
                    self.assertEqual(result['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
                    self.assertEqual(calls[0]['output_schema'], SCREEN_SCHEMA if carried == 0 else SCREEN_ONE_SCHEMA)
                    self.assertEqual(calls[0]['prompt'], SCREENING if carried == 0 else SCREENING_ONE)
                self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 2)

    def test_one_daily_receipt_plus_pending_fill_reserves_last_trade(self):
        self.ledger.db.execute('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)',
                               ('closed-today', 'ETHUSDT', day(), now()))
        self.screens = [['BTCUSDT']]
        self.on()
        gateway = self.connected()
        self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(self.robot.tick()['bot_status'], 'ENTRY_PENDING')
        self.assertEqual(self.robot.tick()['wait_reason'], 'ROBOT_CAPACITY_FULL')
        self.assertEqual(self.screen_counts(), [1])
        self.assertEqual(self.store.entries(day()), 1)
        self.assertEqual(self.robot.execution_slots(account_state(self.account, self.store, day()), day()), 0)
        self.restart()
        self.assertEqual(self.ticks(3)['wait_reason'], 'ROBOT_CAPACITY_FULL')
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.receipt_count(), 1)

    def test_yesterday_pending_reserves_today_but_allows_one_remaining_trade(self):
        self.one_slot_ready()
        gateway = self.connected()
        self.robot.tick()
        self.account.positions = []
        self.screens.append(['ETHUSDT'])
        today = (datetime.fromisoformat(day()) + timedelta(days=1)).date().isoformat()
        with patch('worker.robot.day', return_value=today), patch('worker.robot_store.day', return_value=today):
            self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
            self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
            self.assertEqual(self.robot.tick()['bot_status'], 'ENTRY_PENDING')
            self.assertEqual(self.robot.tick()['wait_reason'], 'ROBOT_CAPACITY_FULL')
            self.assertEqual(self.store.entries(today), 0)
            self.restart()
            self.assertEqual(self.ticks(3)['wait_reason'], 'ROBOT_CAPACITY_FULL')
        self.assertEqual(self.screen_counts(), [1, 1])
        self.assertEqual([intent['symbol'] for intent in gateway.submissions], ['BTCUSDT', 'ETHUSDT'])
        self.assertEqual(self.account.positions, [])
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.receipt_count(), 0)

    def test_yesterday_pending_and_today_closed_receipt_use_whole_daily_budget(self):
        self.one_slot_ready()
        gateway = self.connected()
        self.robot.tick()
        self.account.positions = []
        today = (datetime.fromisoformat(day()) + timedelta(days=1)).date().isoformat()
        self.ledger.db.execute('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)',
                               ('closed-today', 'ETHUSDT', today, now()))
        with patch('worker.robot.day', return_value=today), patch('worker.robot_store.day', return_value=today):
            result = self.ticks(3)
            current = account_state(self.account, self.store, today)
            self.assertEqual(current['running_positions'], 0)
            self.assertEqual(self.robot.execution_slots(current, today), 0)
            self.assertEqual(result['wait_reason'], 'ROBOT_CAPACITY_FULL')
            self.assertEqual(result['bot_entries_today'], 1)
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(len(self.calls), 2)

    def test_pending_fill_exchanges_daily_reservation_for_one_unique_receipt(self):
        self.one_slot_ready()
        gateway = self.connected()
        self.robot.tick()
        self.account.positions = []
        first_fill = now()
        gateway.on_reconcile = lambda intent: gateway.observation(intent, 'POSITION_PROTECTED',
            intent['entry']['quantity'], first_fill_at=first_fill)
        self.robot.tick()
        current = account_state(self.account, self.store, day())
        self.assertEqual(self.store.entries(day()), 1)
        self.assertEqual(self.robot.execution_slots(current, day()), 1)
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual(self.intent()['state'], 'POSITION_PROTECTED')
        self.screens.append(['ETHUSDT'])
        self.restart()
        self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual(self.robot.execution_slots(account_state(self.account, self.store, day()), day()), 1)

    def test_yesterday_pending_first_fill_counts_today_once_across_restart(self):
        self.one_slot_ready()
        gateway = self.connected()
        self.robot.tick()
        previous = day()
        today = (datetime.fromisoformat(previous) + timedelta(days=1)).date().isoformat()
        observed = datetime.fromisoformat(today).replace(hour=12, tzinfo=timezone.utc)
        first_fill = (observed - timedelta(seconds=30)).isoformat()
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None): return observed.astimezone(tz) if tz else observed.replace(tzinfo=None)
        self.account.positions = []
        self.screens.append(['ETHUSDT'])
        gateway.on_reconcile = lambda intent: (gateway.observation(intent, observed_at=observed.isoformat())
            if intent['symbol'] == 'ETHUSDT' else gateway.observation(intent, 'POSITION_PROTECTED',
                intent['entry']['quantity'], first_fill_at=first_fill, observed_at=observed.isoformat()))
        gateway.on_submit = lambda intent: gateway.observation(intent, observed_at=observed.isoformat())
        with patch('worker.robot.day', return_value=today), patch('worker.robot_store.day', return_value=today), \
                patch('worker.robot.datetime', Clock), patch('worker.robot.now', return_value=observed.isoformat()):
            self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
            self.assertEqual(self.store.entries(today), 1)
            self.assertEqual(self.store.entries(previous), 0)
            self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
            self.restart()
            self.assertEqual(self.robot.tick()['bot_status'], 'ENTRY_PENDING')
            self.assertEqual(self.ticks(3)['wait_reason'], 'ROBOT_CAPACITY_FULL')
            self.assertEqual(self.store.entries(today), 1)
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual(self.account.positions, [])
        self.assertEqual([intent['symbol'] for intent in gateway.submissions], ['BTCUSDT', 'ETHUSDT'])
        self.assertEqual(self.screen_counts(), [1, 1])
        self.assertEqual(len(self.calls), 4)

    def screen_counts(self):
        return [1 if call['output_schema'] == SCREEN_ONE_SCHEMA else 2
                for call in self.calls if call['output_schema'] in (SCREEN_SCHEMA, SCREEN_ONE_SCHEMA)]

    def current_cycle(self):
        row = self.ledger.db.execute('SELECT * FROM robot_cycles ORDER BY rowid DESC LIMIT 1').fetchone()
        return row, json.loads(row['data'])

    def temporary_capacity_queue(self):
        self.on()
        self.robot.tick()
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.account.position('HYPEUSDT', '1')
        self.assertEqual(self.robot.tick()['bot_status'], 'EXECUTION_BLOCKED')
        row, data = self.current_cycle()
        self.assertEqual(data['queue'], ['ETHUSDT'])
        return row['id'], data

    def fill_first_and_release_manual_slot(self):
        gateway = self.connected()
        captured = {}
        def submit(intent):
            captured['first_fill_at'] = now()
            self.account.position(intent['symbol'], intent['entry']['quantity'])
            return gateway.observation(intent, 'POSITION_PROTECTED', intent['entry']['quantity'], **captured)
        gateway.on_submit = submit
        gateway.on_reconcile = lambda intent: gateway.observation(intent, 'POSITION_PROTECTED',
            intent['entry']['quantity'], **captured)
        self.account.positions = []
        self.assertEqual(self.robot.tick()['bot_status'], 'POSITION_PROTECTED')
        self.assertEqual(self.receipt_count(), 1)
        return gateway

    def legacy_paid_epoch_screen(self):
        cycle = day() + ':robot-v9:' + str(self.store.entries(day()))
        operation = cycle + ':screening:0:1'
        self.screens.append(['SOLUSDT'])
        self.client.ask(operation, SCREENING_ONE, SCREEN_ONE_SCHEMA, lambda value: None,
                        catalog=self.market.catalog())
        data = dict(target=1, queue=['SOLUSDT'], seen=[], screen=0, replacements=0,
                    round_symbols=['SOLUSDT'])
        self.ledger.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',
                               (cycle, day(), self.store.entries(day()), 'ACTIVE', json.dumps(data)))
        self.ledger.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
                               (operation, cycle, 'SCREENING', None, 'COMPLETE', None))
        return cycle, data

    def test_temporary_capacity_preserves_paid_queue_until_original_slot_opens(self):
        cycle, before = self.temporary_capacity_queue()
        self.assertEqual(self.ledger.db.execute('SELECT state FROM robot_cycles WHERE id=?', (cycle,)).fetchone()[0], 'ACTIVE')
        self.restart()
        gateway = self.fill_first_and_release_manual_slot()
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(self.screen_counts(), [2])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_cycles').fetchone()[0], 1)
        row = self.ledger.db.execute('SELECT * FROM robot_cycles WHERE id=?', (cycle,)).fetchone()
        after = json.loads(row['data'])
        self.assertEqual((after['target'], after['screen'], after['replacements']),
                         (before['target'], before['screen'], before['replacements']))
        self.assertEqual(after['queue'], [])
        self.assertEqual(self.ledger.db.execute("SELECT cycle FROM robot_candidates WHERE symbol='ETHUSDT'").fetchone()[0], cycle)
        self.assertEqual(len(gateway.submissions), 1)

    def test_legacy_complete_paid_queue_recovers_across_fill_epoch_and_restart(self):
        cycle, before = self.temporary_capacity_queue()
        self.ledger.db.execute("UPDATE robot_cycles SET state='COMPLETE' WHERE id=?", (cycle,))
        self.restart()
        gateway = self.fill_first_and_release_manual_slot()
        self.restart()
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(self.screen_counts(), [2])
        self.assertEqual(len(self.calls), 3)
        row = self.ledger.db.execute('SELECT * FROM robot_cycles WHERE id=?', (cycle,)).fetchone()
        after = json.loads(row['data'])
        self.assertEqual(row['state'], 'ACTIVE')
        self.assertEqual((after['target'], after['screen'], after['replacements']),
                         (before['target'], before['screen'], before['replacements']))
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_cycles').fetchone()[0], 1)
        self.assertEqual(len(gateway.submissions), 1)

    def test_legacy_queue_takes_priority_over_existing_epoch_paid_screen(self):
        older, _ = self.temporary_capacity_queue()
        self.ledger.db.execute("UPDATE robot_cycles SET state='COMPLETE' WHERE id=?", (older,))
        self.fill_first_and_release_manual_slot()
        newer, pending = self.legacy_paid_epoch_screen()
        self.restart()
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(self.ledger.db.execute("SELECT cycle FROM robot_candidates WHERE symbol='ETHUSDT'").fetchone()[0], older)
        newer_row = self.ledger.db.execute('SELECT * FROM robot_cycles WHERE id=?', (newer,)).fetchone()
        self.assertEqual(json.loads(newer_row['data']), pending)
        self.assertEqual(self.screen_counts(), [2, 1])
        self.assertEqual(len(self.calls), 4)

    def test_existing_epoch_ready_intent_reserves_slot_before_older_queue_recovery(self):
        older, _ = self.temporary_capacity_queue()
        self.ledger.db.execute("UPDATE robot_cycles SET state='COMPLETE' WHERE id=?", (older,))
        fixture = self.fill_first_and_release_manual_slot()
        newer, data = self.legacy_paid_epoch_screen()
        self.assertEqual(self.robot.analyze(newer, data, 'SOLUSDT',
            account_state(self.account, self.store, day()), day())['bot_status'], 'READY_FOR_EXECUTION')
        class ReaderGateway(OrderGateway):
            def reconcile(self, intent): return fixture.reconcile(intent)
        self.robot.gateway = ReaderGateway()
        before_calls = len(self.calls)
        self.assertEqual(self.ticks(4)['bot_status'], 'EXECUTION_BLOCKED')
        self.assertEqual(len(self.calls), before_calls)
        self.assertFalse(self.ledger.db.execute("SELECT 1 FROM robot_candidates WHERE symbol='ETHUSDT'").fetchone())
        old = self.ledger.db.execute('SELECT * FROM robot_cycles WHERE id=?', (older,)).fetchone()
        self.assertEqual(old['state'], 'ACTIVE')
        self.assertEqual(json.loads(old['data'])['queue'], ['ETHUSDT'])

    def test_definitively_delisted_head_is_rejected_and_valid_queue_continues(self):
        self.on()
        self.robot.tick()
        self.market.symbols = tuple(symbol for symbol in self.market.symbols if symbol != 'BTCUSDT')
        self.assertEqual(self.robot.tick()['failure_code'], 'INVALID_SCREENING_SYMBOL')
        _, data = self.current_cycle()
        self.assertEqual(data['queue'], ['ETHUSDT'])
        self.assertEqual(data['seen'], ['BTCUSDT'])
        self.restart()
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.ticks(4)
        self.assertEqual(self.screen_counts(), [2])
        self.assertEqual(len(self.calls), 2)
        rejected = self.ledger.db.execute("SELECT * FROM robot_candidates WHERE symbol='BTCUSDT'").fetchone()
        self.assertEqual((rejected['status'], rejected['failure_code'], rejected['plan']),
                         ('REJECTED', 'INVALID_SCREENING_SYMBOL', None))
        self.assertFalse(self.ledger.db.execute("SELECT 1 FROM robot_jobs WHERE kind='ANALYSIS' AND symbol='BTCUSDT'").fetchone())
        row, data = self.current_cycle()
        self.assertEqual((row['state'], data['replacements']), ('COMPLETE', 0))

    def test_transient_catalog_failure_keeps_head_retryable_without_paid_call(self):
        self.on()
        self.robot.tick()
        with patch.object(self.market, 'transport', side_effect=TimeoutError('temporary fixture outage')):
            self.assertEqual(self.robot.tick()['failure_code'], 'MARKET_DATA_UNAVAILABLE')
        _, data = self.current_cycle()
        self.assertEqual(data['queue'], ['BTCUSDT', 'ETHUSDT'])
        self.assertEqual(data['seen'], [])
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_candidates').fetchone()[0], 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(self.screen_counts(), [2])
        self.assertEqual(len(self.calls), 3)

    def test_transient_fee_failure_keeps_head_retryable_without_paid_call(self):
        self.one_slot_ready_prepare_screen()
        with patch.object(self.account, 'commission_rate', side_effect=Review('FEE_EVIDENCE_UNAVAILABLE')):
            self.assertEqual(self.robot.tick()['failure_code'], 'MARKET_DATA_UNAVAILABLE')
        _, data = self.current_cycle()
        self.assertEqual(data['queue'], ['BTCUSDT'])
        self.assertEqual(data['seen'], [])
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_candidates').fetchone()[0], 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(len(self.calls), 2)

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

    def test_completed_three_hold_rounds_are_not_revived_when_ready_trade_fills(self):
        self.screens = [['BTCUSDT', 'ETHUSDT'], ['SOLUSDT'], ['BNBUSDT'], ['XRPUSDT']]
        self.decisions = {symbol: 'HOLD' for symbol in ('ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT')}
        self.on()
        self.assertEqual(self.ticks(10)['bot_status'], 'INSUFFICIENT_ACTIONABLE_SETUPS')
        row, old_data = self.current_cycle()
        older = row['id']
        self.assertEqual((row['state'], old_data['queue'], old_data['replacements']), ('COMPLETE', [], 3))
        self.assertEqual(len(self.calls), 9)
        self.screens.append(['ADAUSDT'])
        self.fill_first_and_release_manual_slot()
        self.restart()
        self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        old_row = self.ledger.db.execute('SELECT * FROM robot_cycles WHERE id=?', (older,)).fetchone()
        self.assertEqual((old_row['state'], json.loads(old_row['data'])), ('COMPLETE', old_data))
        row, data = self.current_cycle()
        self.assertNotEqual(row['id'], older)
        self.assertEqual((row['entry_epoch'], data['target'], data['screen'], data['replacements']), (1, 1, 0, 0))
        self.assertEqual(data['queue'], ['ADAUSDT'])
        self.assertEqual(self.screen_counts(), [2, 1, 1, 1, 1])
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM robot_jobs WHERE cycle=? AND kind='SCREENING'", (older,)).fetchone()[0], 4)

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

    def test_off_during_account_read_starts_no_gateway_reconcile(self):
        self.one_slot_ready()
        gateway = self.connected()
        self.robot.tick()
        before = dict(self.intent())
        positions = copy.deepcopy(self.account.positions)
        original = self.account.signed_get
        def account_get(path):
            value = original(path)
            self.store.configure(dict(robot_on=False))
            return value
        self.account.signed_get = account_get
        self.assertEqual(self.robot.tick()['bot_status'], 'OFF')
        self.assertEqual(gateway.reconciliations, [])
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(dict(self.intent()), before)
        self.assertEqual(self.account.positions, positions)
        self.assertEqual(len(self.calls), 2)

    def test_off_during_first_reconcile_journals_it_and_stops_second(self):
        self.on()
        gateway = self.connected()
        self.ticks(5)
        before = len(gateway.reconciliations)
        other = dict(self.ledger.db.execute("SELECT * FROM order_intents WHERE symbol='ETHUSDT'").fetchone())
        def reconcile(intent):
            self.store.configure(dict(robot_on=False))
            return gateway.observation(intent, 'POSITION_PROTECTED', intent['entry']['quantity'])
        gateway.on_reconcile = reconcile
        gateway.cancel = lambda *args: self.fail('OFF must not cancel an order')
        gateway.close = lambda *args: self.fail('OFF must not close a position')
        self.assertEqual(self.robot.tick()['bot_status'], 'OFF')
        self.assertEqual(len(gateway.reconciliations) - before, 1)
        self.assertEqual(self.intent()['state'], 'POSITION_PROTECTED')
        self.assertEqual(dict(self.ledger.db.execute("SELECT * FROM order_intents WHERE symbol='ETHUSDT'").fetchone()), other)
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual(self.store.entries(day()), 1)
        self.restart()
        self.ticks(4)
        self.assertEqual(len(gateway.reconciliations) - before, 1)
        self.assertEqual(len(gateway.submissions), 2)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.receipt_count(), 1)

    def test_off_after_local_submit_claim_restores_proven_unsent_state(self):
        self.one_slot_ready()
        gateway = self.connected()
        original = self.store.report
        def report(state, *args, **kwargs):
            value = original(state, *args, **kwargs)
            if state == 'EXECUTING': self.store.configure(dict(robot_on=False))
            return value
        with patch.object(self.store, 'report', side_effect=report):
            self.assertEqual(self.robot.tick()['bot_status'], 'OFF')
        self.assertEqual(self.intent()['state'], 'READY_FOR_EXECUTION')
        self.assertEqual(gateway.submissions, [])
        self.assertEqual(self.receipt_count(), 0)
        self.restart()
        self.ticks(3)
        self.assertEqual(gateway.submissions, [])
        self.on()
        self.assertEqual(self.robot.tick()['bot_status'], 'ENTRY_PENDING')
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(len(self.calls), 2)

    def test_off_during_started_submit_still_journals_actual_fill_once(self):
        self.one_slot_ready()
        gateway = self.connected()
        def submit(intent):
            self.account.position('BTCUSDT', intent['entry']['quantity'])
            self.store.configure(dict(robot_on=False))
            return gateway.observation(intent, 'POSITION_PROTECTED', intent['entry']['quantity'])
        gateway.on_submit = submit
        self.assertEqual(self.robot.tick()['bot_status'], 'OFF')
        self.assertEqual(self.intent()['state'], 'POSITION_PROTECTED')
        self.assertEqual(self.receipt_count(), 1)
        positions = copy.deepcopy(self.account.positions)
        self.restart()
        self.ticks(4)
        self.assertEqual(self.account.positions, positions)
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(gateway.reconciliations, [])
        self.assertEqual(self.receipt_count(), 1)

    def test_off_after_screening_claim_starts_no_paid_request_and_can_resume(self):
        self.on()
        original = self.store.report
        def report(state, *args, **kwargs):
            value = original(state, *args, **kwargs)
            if state == 'SCREENING': self.store.configure(dict(robot_on=False))
            return value
        with patch.object(self.store, 'report', side_effect=report):
            self.assertEqual(self.robot.tick()['bot_status'], 'OFF')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_jobs').fetchone()[0], 0)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM api_requests').fetchone()[0], 0)
        self.restart()
        self.on()
        self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(len(self.calls), 1)

    def test_off_after_analysis_claim_preserves_unpaid_queue_for_resume(self):
        self.one_slot_ready_prepare_screen()
        original = self.store.report
        def report(state, *args, **kwargs):
            value = original(state, *args, **kwargs)
            if state == 'ANALYZING': self.store.configure(dict(robot_on=False))
            return value
        with patch.object(self.store, 'report', side_effect=report):
            self.assertEqual(self.robot.tick()['bot_status'], 'OFF')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_jobs').fetchone()[0], 1)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_candidates').fetchone()[0], 0)
        _, data = self.current_cycle()
        self.assertEqual(data['queue'], ['BTCUSDT'])
        self.restart()
        self.on()
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(len(self.calls), 2)

    def test_off_during_analysis_catalog_stops_followup_reads_and_preserves_queue(self):
        self.one_slot_ready_prepare_screen()
        original = self.market.transport
        market_count, account_count = len(self.market.calls), len(self.account.calls)
        def transport(*args, **kwargs):
            value = original(*args, **kwargs)
            self.store.configure(dict(robot_on=False))
            return value
        with patch.object(self.market, 'transport', side_effect=transport):
            result = self.robot.tick()
        self.assertEqual((result['bot_status'], result['wait_reason']), ('OFF', 'ROBOT_OFF'))
        self.assertEqual(len(self.market.calls) - market_count, 1)
        self.assertTrue(self.market.calls[-1][1].endswith('/exchangeInfo'))
        self.assertEqual(len(self.account.calls) - account_count, 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_candidates').fetchone()[0], 0)
        self.assertFalse(self.ledger.db.execute("SELECT 1 FROM robot_jobs WHERE kind='ANALYSIS'").fetchone())
        _, data = self.current_cycle()
        self.assertEqual(data['queue'], ['BTCUSDT'])
        self.restart()
        self.on()
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(len(self.calls), 2)

    def test_off_during_postpaid_refresh_keeps_received_setup_with_initial_rules(self):
        self.one_slot_ready_prepare_screen()
        provider, market_transport = self.client.transport, self.market.transport
        after_response = {}
        def transport(*args, **kwargs):
            value = provider(*args, **kwargs)
            after_response['market_calls'] = len(self.market.calls)
            after_response['account_calls'] = len(self.account.calls)
            return value
        def market(*args, **kwargs):
            value = market_transport(*args, **kwargs)
            if after_response: self.store.configure(dict(robot_on=False))
            return value
        with patch.object(self.client, 'transport', side_effect=transport), patch.object(self.market, 'transport', side_effect=market):
            result = self.robot.tick()
        self.assertEqual((result['bot_status'], result['wait_reason']), ('OFF', 'ROBOT_OFF'))
        self.assertEqual(len(self.market.calls) - after_response['market_calls'], 1)
        self.assertIn('/premiumIndex?', self.market.calls[-1][1])
        self.assertEqual(len(self.account.calls), after_response['account_calls'])
        candidate = self.ledger.db.execute('SELECT * FROM robot_candidates').fetchone()
        self.assertEqual(candidate['status'], 'READY_FOR_EXECUTION')
        plan = self.store.verified_plan(candidate)
        self.assertEqual(plan['sizing_rules']['fee_symbol'], 'BTCUSDT')
        self.assertEqual(plan['sizing_rules']['taker_fee_rate'], '0')
        self.assertEqual(self.ledger.db.execute("SELECT state FROM api_requests WHERE operation LIKE '%:BTCUSDT'").fetchone()[0], 'COMPLETE')
        self.assertEqual(self.ledger.db.execute("SELECT state FROM robot_jobs WHERE kind='ANALYSIS'").fetchone()[0], 'COMPLETE')
        _, data = self.current_cycle()
        self.assertEqual(data['queue'], [])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.receipt_count(), 0)
        self.assertIsNone(self.intent())

    def test_off_during_completed_analysis_keeps_response_without_fresh_fee_get(self):
        self.one_slot_ready_prepare_screen()
        original = self.client.transport
        after_response = {}
        def transport(*args, **kwargs):
            value = original(*args, **kwargs)
            after_response['account_calls'] = len(self.account.calls)
            after_response['market_calls'] = len(self.market.calls)
            self.store.configure(dict(robot_on=False))
            return value
        self.client.transport = transport
        self.assertEqual(self.robot.tick()['bot_status'], 'OFF')
        self.assertEqual(len(self.account.calls), after_response['account_calls'])
        self.assertEqual(len(self.market.calls), after_response['market_calls'])
        self.assertEqual(self.ledger.db.execute("SELECT state FROM api_requests WHERE operation LIKE '%:BTCUSDT'").fetchone()[0], 'COMPLETE')
        self.assertEqual(self.ledger.db.execute("SELECT state FROM robot_jobs WHERE kind='ANALYSIS'").fetchone()[0], 'COMPLETE')
        self.assertEqual(self.ledger.db.execute('SELECT status FROM robot_candidates').fetchone()[0], 'READY_FOR_EXECUTION')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.receipt_count(), 0)

    def test_provider_pause_before_first_send_preserves_screening_for_resume(self):
        self.on()
        original = self.client.ask
        def ask(*args, **kwargs):
            self.store.configure(dict(robot_on=False))
            return original(*args, **kwargs)
        with patch.object(self.client, 'ask', side_effect=ask):
            self.assertEqual(self.robot.tick()['bot_status'], 'OFF')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM api_requests').fetchone()[0], 0)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_jobs').fetchone()[0], 0)
        _, data = self.current_cycle()
        self.assertEqual(data['screen'], -1)
        self.restart()
        self.on()
        self.assertEqual(self.robot.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(len(self.calls), 1)

    def test_provider_pause_before_first_send_preserves_analysis_queue(self):
        self.one_slot_ready_prepare_screen()
        original = self.client.ask
        def ask(*args, **kwargs):
            self.store.configure(dict(robot_on=False))
            return original(*args, **kwargs)
        with patch.object(self.client, 'ask', side_effect=ask):
            self.assertEqual(self.robot.tick()['bot_status'], 'OFF')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM api_requests').fetchone()[0], 1)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_jobs').fetchone()[0], 1)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_candidates').fetchone()[0], 0)
        _, data = self.current_cycle()
        self.assertEqual(data['queue'], ['BTCUSDT'])
        self.restart()
        self.on()
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(len(self.calls), 2)

    def test_off_after_retryable_provider_response_starts_no_retry_or_replay(self):
        self.on()
        def transport(method, url, headers, body, timeout):
            self.calls.append(copy.deepcopy(body))
            self.store.configure(dict(robot_on=False))
            return 503, {}, {}
        self.client.transport = transport
        self.client.sleep = lambda delay: self.fail('OFF must stop before retry sleep/send')
        self.assertEqual(self.robot.tick()['bot_status'], 'OFF')
        self.assertEqual(len(self.calls), 1)
        request = self.ledger.db.execute('SELECT state,attempts,failure_code FROM api_requests').fetchone()
        self.assertEqual(tuple(request), ('NEEDS_REVIEW', 1, 'RESEARCH_PAUSED'))
        self.assertEqual(self.ledger.db.execute('SELECT state FROM robot_jobs').fetchone()[0], 'NEEDS_REVIEW')
        self.restart()
        self.on()
        self.assertEqual(self.ticks(4)['failure_code'], 'ROBOT_REQUEST_NEEDS_REVIEW')
        self.assertEqual(len(self.calls), 1)

    def test_off_during_completed_screening_preserves_paid_result_and_queue(self):
        self.on()
        original = self.client.transport
        def transport(*args, **kwargs):
            value = original(*args, **kwargs)
            self.store.configure(dict(robot_on=False))
            return value
        self.client.transport = transport
        self.assertEqual(self.robot.tick()['bot_status'], 'OFF')
        self.assertEqual(self.ledger.db.execute('SELECT state FROM api_requests').fetchone()[0], 'COMPLETE')
        self.assertEqual(self.ledger.db.execute('SELECT state FROM robot_jobs').fetchone()[0], 'COMPLETE')
        _, data = self.current_cycle()
        self.assertEqual(data['queue'], ['BTCUSDT', 'ETHUSDT'])
        self.restart()
        self.on()
        self.assertEqual(self.robot.tick()['bot_status'], 'READY_FOR_EXECUTION')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.screen_counts(), [2])

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

    def test_started_reconcile_drains_adapter_reads_after_off_then_stops_next_callback(self):
        self.on()
        gateway = self.connected()
        self.ticks(5)
        self.assertEqual(len(gateway.submissions), 2)
        previous = {row['symbol']: row['result'] for row in self.ledger.db.execute('SELECT symbol,result FROM order_intents')}
        reader, calls = self.gateway_reader_off_on_first_get()
        def reconcile(intent):
            reader.sync_time()  # OFF while a started adapter transaction is in progress.
            reader.signed_get('/fapi/v1/openOrders', intent['symbol'])
            return gateway.observation(intent)
        gateway.on_reconcile = reconcile
        before = len(gateway.reconciliations)
        result = self.robot.tick()
        self.assertEqual((result['bot_status'], result['wait_reason']), ('OFF', 'ROBOT_OFF'))
        self.assertEqual(calls, ['/fapi/v1/time', '/fapi/v1/openOrders'])
        self.assertEqual(len(gateway.reconciliations) - before, 1)
        states = {row['symbol']: row['state'] for row in self.ledger.db.execute('SELECT symbol,state FROM order_intents')}
        self.assertEqual(states, {'BTCUSDT': 'ENTRY_PENDING', 'ETHUSDT': 'ENTRY_PENDING'})
        self.assertEqual(self.ledger.db.execute("SELECT result FROM order_intents WHERE symbol='ETHUSDT'").fetchone()[0], previous['ETHUSDT'])
        self.assertEqual(self.receipt_count(), 0)
        self.assertEqual(len(self.calls), 3)
        gateway.on_reconcile = lambda intent: gateway.observation(intent)
        self.restart()
        self.on()
        self.assertEqual(self.robot.tick()['wait_reason'], 'ROBOT_CAPACITY_FULL')
        self.assertEqual(len(gateway.reconciliations) - before, 3)
        self.assertEqual(len(gateway.submissions), 2)
        self.assertEqual(len(self.calls), 3)

    def test_started_submit_drains_adapter_protection_reads_and_journals_fill_after_off(self):
        self.one_slot_ready()
        gateway = self.connected()
        reader, calls = self.gateway_reader_off_on_first_get()
        def submit(intent):
            self.assertEqual(self.intent()['state'], 'SUBMITTING')
            reader.sync_time()
            reader.signed_get('/fapi/v1/openOrders', intent['symbol'])
            self.account.position(intent['symbol'], intent['entry']['quantity'])
            return gateway.observation(intent, 'POSITION_PROTECTED', intent['entry']['quantity'])
        gateway.on_submit = submit
        result = self.robot.tick()
        self.assertEqual(result['bot_status'], 'OFF')
        self.assertEqual(calls, ['/fapi/v1/time', '/fapi/v1/openOrders'])
        self.assertEqual(self.intent()['state'], 'POSITION_PROTECTED')
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.ticks(3)['bot_status'], 'OFF')
        self.assertEqual(gateway.reconciliations, [])
        self.assertEqual(len(gateway.submissions), 1)
        self.assertEqual(self.receipt_count(), 1)

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
