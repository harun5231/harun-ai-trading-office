"""Offline TP-only maintenance checks; no credentials, sockets, or live orders."""
import copy
import json
import os
import unittest
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode

from deploy.recover_eth_take_profit import record, repair
from test_live_gateway import Exchange, OrderGateway
import test_order_pipeline as pipeline
from worker.account_state import account_state
from worker.core import Review, day


class SignedExchange(Exchange):
    """Run the SDK's signing path against the existing in-memory exchange."""

    def _request(self, method, path, params):
        return OrderGateway._request(self, method, path, params)

    def _wire(self, method, path, query='', signed=False):
        self.assertion(signed is True, 'unsigned fixture request')
        pairs = parse_qsl(query, keep_blank_values=True)
        values = dict(pairs)
        signature = values.pop('signature')
        self.assertion(signature == self._sign(urlencode(sorted(values.items()))), 'bad signature')
        self.assertion(values.pop('recvWindow') == '5000', 'bad recvWindow')
        self.assertion(values.pop('timestamp') == str(self.ms), 'bad server time')
        for key in ('startTime', 'endTime', 'fromId', 'limit'):
            if key in values:
                values[key] = int(values[key])
        return Exchange._request(self, method, path, values)

    @staticmethod
    def assertion(value, reason):
        if not value:
            raise AssertionError(reason)


class RecoverTakeProfitTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.network = patch('urllib.request.build_opener', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def fixture(self, *, protected=False):
        gateway = SignedExchange(fill='4')
        command = gateway.command
        cfg = command['entry']
        gateway._request('POST', '/fapi/v1/order', dict(
            symbol=command['symbol'], side=cfg['side'], positionSide='BOTH', type='LIMIT',
            timeInForce='GTC', price=cfg['price'], quantity=cfg['quantity'],
            newClientOrderId=command['client_order_id']))
        sl = gateway._ensure_algo(command, 'sl', Decimal('4'))
        if protected:
            gateway._ensure_algo(command, 'tp', Decimal('4'))
        pins = dict(symbol=command['symbol'], client=command['client_order_id'], entry_id='10',
                    quantity=cfg['quantity'], price=cfg['price'], sl=command['protection']['stop_loss'],
                    tp=command['protection']['take_profit'], sl_id=str(sl['algoId']))
        gateway.calls.clear()
        return gateway, command, pins

    def writes(self, gateway):
        return [call for call in gateway.calls if call[0] != 'GET']

    def assert_restored(self, gateway, request, wire):
        self.assertEqual(gateway._request, request)
        self.assertEqual(gateway._wire, wire)

    def test_missing_tp_only_one_exact_signed_tp_post_and_verified_result(self):
        gateway, command, pins = self.fixture()
        before_sl = copy.deepcopy(gateway.algos)
        before_entry = copy.deepcopy(gateway.entry)
        request, wire = gateway._request, gateway._wire
        result = repair(gateway, command, pins, lambda: True)
        writes = self.writes(gateway)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][:2], ('POST', '/fapi/v1/algoOrder'))
        self.assertEqual(writes[0][2], dict(
            algoType='CONDITIONAL', symbol='BTCUSDT', side='SELL', positionSide='BOTH',
            type='TAKE_PROFIT_MARKET', quantity='4', reduceOnly='true', triggerPrice='103',
            workingType='MARK_PRICE', clientAlgoId=gateway._algo_id(command, 'tp', Decimal('4')),
            newOrderRespType='ACK'))
        self.assertEqual(result['state'], 'POSITION_PROTECTED')
        self.assertEqual(result['filled_quantity'], '4')
        self.assertTrue(result['sl_confirmed'])
        self.assertTrue(result['tp_confirmed'])
        self.assertEqual(result['sl_order_id'], pins['sl_id'])
        self.assertEqual(gateway.entry, before_entry)
        for client, sl in before_sl.items():
            self.assertEqual(gateway.algos[client], sl)
        self.assert_restored(gateway, request, wire)

    def test_both_legs_exist_no_post_and_idempotent_proof(self):
        gateway, command, pins = self.fixture(protected=True)
        before = copy.deepcopy(gateway.algos)
        result = repair(gateway, command, pins, lambda: True)
        self.assertEqual(result['state'], 'POSITION_PROTECTED')
        self.assertEqual(self.writes(gateway), [])
        self.assertEqual(gateway.algos, before)

    def test_foreign_position_or_trade_refuses_before_tp(self):
        for conflict in ('position', 'trade', 'regular_order', 'algo_order'):
            with self.subTest(conflict=conflict):
                gateway, command, pins = self.fixture()
                if conflict == 'position':
                    gateway.manual.append(dict(symbol='BTCUSDT', positionSide='BOTH', positionAmt='1', liquidationPrice='0'))
                elif conflict == 'trade':
                    gateway.trades.append(dict(id=900, symbol='BTCUSDT', orderId=999, qty='1',
                                               side='BUY', positionSide='BOTH', time=gateway.ms-1))
                elif conflict == 'regular_order':
                    gateway.manual_orders.append(dict(symbol='BTCUSDT', orderId=999, clientOrderId='manual'))
                else:
                    gateway.manual_algos.append(dict(symbol='BTCUSDT', clientAlgoId='manual'))
                before = copy.deepcopy(gateway.algos)
                with self.assertRaises(Review):
                    repair(gateway, command, pins, lambda: True)
                self.assertEqual(self.writes(gateway), [])
                self.assertEqual(gateway.algos, before)

    def test_bad_or_different_sl_refuses_before_tp(self):
        for field, value in (('reduceOnly', False), ('triggerPrice', '98'), ('algoStatus', 'CANCELED'),
                             ('quantity', '3'), ('algoId', 999)):
            with self.subTest(field=field):
                gateway, command, pins = self.fixture()
                sl = next(iter(gateway.algos.values()))
                sl[field] = value
                with self.assertRaises((Review, ValueError)):
                    repair(gateway, command, pins, lambda: True)
                self.assertEqual(self.writes(gateway), [])

    def test_enabled_false_blocks_missing_tp_and_restores_fences(self):
        gateway, command, pins = self.fixture()
        request, wire = gateway._request, gateway._wire
        with self.assertRaisesRegex(Review, 'PROTECTION_OUTCOME_UNKNOWN'):
            repair(gateway, command, pins, lambda: False)
        self.assertEqual(self.writes(gateway), [])
        self.assert_restored(gateway, request, wire)

    def test_tp_timeout_claimed_once_no_entry_no_delete_and_no_false_result(self):
        gateway, command, pins = self.fixture()
        original = gateway._wire
        attempts = []
        def timeout(method, path, query='', signed=False):
            if method == 'POST':
                attempts.append((method, path))
                # Model acceptance followed by lost response, not proven rejection.
                original(method, path, query, signed)
                raise Review('BINANCE_ORDER_OUTCOME_UNKNOWN')
            return original(method, path, query, signed)
        gateway._wire = timeout
        request, wire = gateway._request, gateway._wire
        with self.assertRaisesRegex(Review, 'PROTECTION_OUTCOME_UNKNOWN'):
            repair(gateway, command, pins, lambda: True)
        self.assertEqual(attempts, [('POST', '/fapi/v1/algoOrder')])
        self.assertEqual(len(self.writes(gateway)), 1)
        self.assertEqual(len(gateway.algos), 2)
        self.assert_restored(gateway, request, wire)
        # A later explicit inspection observes the accepted TP and sends nothing.
        gateway.calls.clear()
        result = repair(gateway, command, pins, lambda: True)
        self.assertEqual(result['state'], 'POSITION_PROTECTED')
        self.assertEqual(self.writes(gateway), [])
        self.assertEqual(len(attempts), 1)

    def test_request_fence_refuses_entry_cancel_sl_and_changed_tp(self):
        for attack in ('entry', 'cancel', 'sl', 'quantity', 'trigger', 'client'):
            with self.subTest(attack=attack):
                gateway, command, pins = self.fixture()
                params = dict(algoType='CONDITIONAL', symbol='BTCUSDT', side='SELL', positionSide='BOTH',
                              type='TAKE_PROFIT_MARKET', quantity='4', reduceOnly='true', triggerPrice='103',
                              workingType='MARK_PRICE', clientAlgoId=gateway._algo_id(command, 'tp', Decimal('4')),
                              newOrderRespType='ACK')
                method, path = 'POST', '/fapi/v1/algoOrder'
                if attack == 'entry': path = '/fapi/v1/order'
                elif attack == 'cancel': method = 'DELETE'
                elif attack == 'sl': params['type'] = 'STOP_MARKET'
                elif attack == 'quantity': params['quantity'] = '5'
                elif attack == 'trigger': params['triggerPrice'] = '104'
                else: params['clientAlgoId'] = 'foreign'
                gateway.reconcile = lambda unused: gateway._request(method, path, params)
                with self.assertRaisesRegex(ValueError, 'OTHER_WRITE_BLOCKED'):
                    repair(gateway, command, pins, lambda: True)
                self.assertEqual(self.writes(gateway), [])

    def test_wire_fence_refuses_writes_without_request_authorization(self):
        for method, path in (('POST', '/fapi/v1/algoOrder'), ('POST', '/fapi/v1/order'),
                             ('DELETE', '/fapi/v1/order'), ('DELETE', '/fapi/v1/algoOrder')):
            with self.subTest(method=method, path=path):
                gateway, command, pins = self.fixture()
                gateway.reconcile = lambda unused: gateway._wire(method, path, 'signature='+'a'*64, True)
                with self.assertRaisesRegex(ValueError, 'OTHER_WRITE_BLOCKED'):
                    repair(gateway, command, pins, lambda: True)
                self.assertEqual(self.writes(gateway), [])

    def test_wire_fence_checks_authorized_post_body_and_on_state(self):
        for change in ('quantity', 'duplicate', 'signature', 'timestamp', 'window', 'unsigned', 'off'):
            with self.subTest(change=change):
                gateway, command, pins = self.fixture()
                original = gateway._request
                state = [True]
                def altered(method, path, params):
                    if method != 'POST':
                        return original(method, path, params)
                    values = dict(params, timestamp=str(gateway.ms), recvWindow='5000')
                    if change == 'quantity': values['quantity'] = '5'
                    if change == 'timestamp': values['timestamp'] = 'bad'
                    if change == 'window': values['recvWindow'] = '6000'
                    query = urlencode(sorted(values.items()))
                    query += '&signature=' + ('bad' if change == 'signature' else gateway._sign(query))
                    if change == 'duplicate': query += '&quantity=4'
                    if change == 'off': state[0] = False
                    return gateway._wire(method, path, query, change != 'unsigned')
                gateway._request = altered
                with self.assertRaisesRegex(Review, 'PROTECTION_OUTCOME_UNKNOWN'):
                    repair(gateway, command, pins, lambda: state[0])
                self.assertEqual(self.writes(gateway), [])

    def test_second_tp_claim_is_blocked_after_first_uncertain_attempt(self):
        gateway, command, pins = self.fixture()
        params = dict(algoType='CONDITIONAL', symbol='BTCUSDT', side='SELL', positionSide='BOTH',
                      type='TAKE_PROFIT_MARKET', quantity='4', reduceOnly='true', triggerPrice='103',
                      workingType='MARK_PRICE', clientAlgoId=gateway._algo_id(command, 'tp', Decimal('4')),
                      newOrderRespType='ACK')
        def replay(unused):
            gateway._request('POST', '/fapi/v1/algoOrder', params)
            return gateway._request('POST', '/fapi/v1/algoOrder', params)
        gateway.reconcile = replay
        with self.assertRaisesRegex(ValueError, 'TP_POST_BLOCKED'):
            repair(gateway, command, pins, lambda: True)
        self.assertEqual(len(self.writes(gateway)), 1)

    def recording_fixture(self):
        # Compose the established fixture; do not inherit its test methods.
        fixture = pipeline.OrderPipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        gateway = fixture.connected()
        fixture.one_slot_ready()
        fixture.robot.tick()
        row = fixture.intent()
        self.assertEqual(row['state'], 'ENTRY_PENDING')
        intent = fixture.robot.verified_intent(row)
        account = account_state(fixture.account, fixture.store, day())
        fixture.robot.mark_unknown(row['id'], row['candidate_id'], account,
                                   'BINANCE_ORDER_PROTECTION_OUTCOME_UNKNOWN')
        result = gateway.observation(intent, state='POSITION_PROTECTED', filled=intent['entry']['quantity'])
        return fixture, fixture.intent(), result, account

    def test_record_validated_protection_is_atomic_and_receipt_idempotent(self):
        fixture, row, result, account = self.recording_fixture()
        original_payload = row['payload']
        self.assertFalse(record(fixture.ledger.db, fixture.robot, row, result, account))
        current = fixture.intent()
        self.assertEqual(current['state'], 'POSITION_PROTECTED')
        self.assertEqual(current['payload'], original_payload)
        self.assertEqual(json.loads(current['result']), result)
        self.assertEqual(fixture.receipt_count(), 1)
        self.assertFalse(record(fixture.ledger.db, fixture.robot, current, result, account))
        self.assertEqual(fixture.receipt_count(), 1)
        receipt = fixture.ledger.db.execute('SELECT * FROM robot_entry_receipts').fetchone()
        self.assertEqual(receipt['confirmed_at'], result['first_fill_at'])

    def test_record_post_commit_report_failure_keeps_real_receipt_and_proof(self):
        fixture, row, result, account = self.recording_fixture()
        with patch.object(fixture.store, 'report', side_effect=RuntimeError('report unavailable')):
            self.assertTrue(record(fixture.ledger.db, fixture.robot, row, result, account))
        self.assertEqual(fixture.intent()['state'], 'POSITION_PROTECTED')
        self.assertEqual(json.loads(fixture.intent()['result']), result)
        self.assertEqual(fixture.receipt_count(), 1)

    def test_record_pre_commit_validation_failure_never_claims_protection_or_fill(self):
        fixture, row, result, account = self.recording_fixture()
        original_payload = row['payload']
        result['filled_quantity'] = '0'  # Real recorder rejects PROTECTED without a fill.
        with self.assertRaisesRegex(ValueError, 'JOURNAL_NOT_VERIFIED'):
            record(fixture.ledger.db, fixture.robot, row, result, account)
        self.assertEqual(fixture.intent()['state'], 'NEEDS_REVIEW')
        self.assertEqual(fixture.intent()['payload'], original_payload)
        self.assertEqual(fixture.receipt_count(), 0)


if __name__ == '__main__':
    unittest.main()
