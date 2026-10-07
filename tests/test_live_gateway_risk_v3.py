"""Offline reserved-exit risk validation and mutation-boundary contract checks."""
import copy
import json
import time
import unittest
from dataclasses import asdict
from decimal import Decimal, localcontext
from fractions import Fraction
from unittest.mock import patch

import test_live_gateway as sdk
from worker.core import (D, FEE_SOURCE, NORMALIZED_REWARD_RISK_POLICY, RISK_MODEL,
                         Review, Rules, Signal, risk_check)
from worker.order_gateway import build_intent
from worker.research_guard import gateway_mutations, gateway_mutations_allowed


def command(side='LONG', *, entry='100', stop=None, model_tp=None,
            tick='0.01', fee='0.0005', target='5'):
    stop = stop or ('98' if side == 'LONG' else '102')
    model_tp = model_tp or ('110' if side == 'LONG' else '90')
    rules = Rules(D('0.001'), D('0.001'), D('1000'), D(tick), D('5'), time.time(),
                  taker_fee_rate=D(fee), fee_observed_at=time.time(),
                  fee_symbol='BTCUSDT', fee_source=FEE_SOURCE)
    plan = risk_check(Signal('BTCUSDT', side, D(entry), D(model_tp), D(stop)), rules,
                      D(target), risk_model=RISK_MODEL,
                      reward_risk_policy=NORMALIZED_REWARD_RISK_POLICY)
    plan.update(protection_working_type='CONTRACT_PRICE', provenance={'payload_sha256': 'a'*64})
    plan['sizing_rules'] = {key: format(value, 'f') if isinstance(value, D) else value
                            for key, value in asdict(rules).items()}
    return build_intent(plan, 'offline-v3-'+side)


class V3Exchange(sdk.Exchange):
    def __init__(self, command_value, fill='0'):
        super().__init__(fill=fill, side=command_value['side'])
        self.command = command_value

    def fill(self, quantity):
        super().fill(quantity)
        if Decimal(quantity) == Decimal(self.command['entry']['quantity']):
            self.entry['status'] = 'FILLED'

    def _request(self, method, path, params):
        # Same production mutation fence around the in-memory transport.
        if method in ('POST', 'DELETE') and not gateway_mutations_allowed():
            raise Review('BINANCE_ORDER_ROBOT_OFF')
        return super()._request(method, path, params)


class LiveGatewayRiskV3Tests(unittest.TestCase):
    def setUp(self):
        self.network = patch('urllib.request.build_opener', side_effect=AssertionError('network forbidden'))
        self.network_mock = self.network.start()
        self.addCleanup(self.network.stop)

    def test_long_short_actual_core_intents_validate_reserve_and_canonical_tp(self):
        for side in ('LONG', 'SHORT'):
            with self.subTest(side=side):
                value = command(side)
                original = copy.deepcopy(value)
                gateway = V3Exchange(value)
                gateway._validate_intent(value)
                self.assertEqual(value, original)
                self.assertEqual(gateway.calls, [])
                self.assertEqual(value['risk_model'], RISK_MODEL)
                self.assertLessEqual(D(value['risk_usdt']), D('5'))
                self.assertGreaterEqual(Fraction(D(value['net_reward_usdt'])), 2*Fraction(D(value['risk_usdt'])))
                self.assertGreater(D(value['sl_slippage_usdt']), 0)
                self.assertNotEqual(value['tp_normalization']['model_tp'], value['protection']['take_profit'])

    def test_zero_fee_coarse_tick_and_high_precision_costs_match_without_rounding(self):
        for side in ('LONG', 'SHORT'):
            for fee, tick, entry, stop, tp in (
                    ('0', '1', '100', '98' if side == 'LONG' else '102', '110' if side == 'LONG' else '90'),
                    ('0.000000000000137', '0.000000000000000001', '1.123456789012345678',
                     '1.123400000000000001' if side == 'LONG' else '1.123500000000000001',
                     '1.124' if side == 'LONG' else '1.123')):
                with self.subTest(side=side, fee=fee, tick=tick):
                    value = command(side, fee=fee, tick=tick, entry=entry, stop=stop, model_tp=tp)
                    with localcontext() as context:
                        context.prec = 6
                        V3Exchange(value)._validate_intent(value)

    def test_partial_and_full_fills_emit_sl_first_then_tp_at_actual_quantity(self):
        for side in ('LONG', 'SHORT'):
            for partial in (True, False):
                with self.subTest(side=side, partial=partial):
                    value = command(side)
                    filled = '0.5' if partial else value['entry']['quantity']
                    gateway = V3Exchange(value, filled)
                    with gateway_mutations(lambda: True):
                        result = gateway.submit(value)
                    self.assertEqual(result['state'], 'POSITION_PROTECTED')
                    self.assertEqual(D(result['filled_quantity']), D(filled))
                    entry = gateway.post_entries()
                    self.assertEqual(len(entry), 1)
                    self.assertEqual(entry[0][2]['side'], 'BUY' if side == 'LONG' else 'SELL')
                    self.assertEqual(entry[0][2]['quantity'], value['entry']['quantity'])
                    self.assertEqual(entry[0][2]['reduceOnly'], 'false')
                    self.assertEqual(entry[0][2]['positionSide'], 'BOTH')
                    self.assertEqual(entry[0][2]['timeInForce'], 'GTC')
                    posts = [params for method, path, params in gateway.calls
                             if method == 'POST' and path == '/fapi/v1/algoOrder']
                    self.assertEqual([p['type'] for p in posts], ['STOP_MARKET', 'TAKE_PROFIT_MARKET'])
                    self.assertEqual([D(p['quantity']) for p in posts], [D(filled), D(filled)])
                    for item in posts:
                        self.assertEqual(item['workingType'], 'CONTRACT_PRICE')
                        self.assertEqual(item['reduceOnly'], 'true')
                        self.assertEqual(item['positionSide'], 'BOTH')
                        self.assertNotIn('closePosition', item)
                        self.assertEqual(item['side'], 'SELL' if side == 'LONG' else 'BUY')

    def test_additional_partial_fill_replaces_only_owned_quantity_protections(self):
        value = command()
        gateway = V3Exchange(value, '0.5')
        with gateway_mutations(lambda: True):
            gateway.submit(value)
            gateway.fill(value['entry']['quantity'])
            result = gateway.reconcile(value)
        self.assertEqual(result['state'], 'POSITION_PROTECTED')
        active = [a for a in gateway.algos.values() if a['algoStatus'] == 'NEW']
        self.assertEqual(len(active), 2)
        self.assertEqual({D(a['quantity']) for a in active}, {D(value['entry']['quantity'])})
        self.assertEqual(len(gateway.post_entries()), 1)

    def test_tampered_reserve_original_evidence_or_fees_fail_before_any_request(self):
        for path, replacement in ((('risk_model',), 'UNKNOWN'), (('exit_slippage_rate',), '0'),
                (('entry_slippage_rate',), '0.005'), (('sl_slippage_usdt',), '0'),
                (('tp_slippage_usdt',), '0'), (('sl_execution_price',), '98'),
                (('tp_execution_price',), '110'), (('tp_normalization', 'model_gross_rr'), '3'),
                (('tp_normalization', 'model_entry'), '101'),
                (('cost_evidence', 'fee_symbol'), 'ETHUSDT'),
                (('cost_evidence', 'reserve_source'), 'UNVERIFIED'),
                (('protection', 'working_type'), 'MARK_PRICE'), (('risk_target_usdt',), '4')):
            with self.subTest(path=path):
                value = command()
                item = value
                for key in path[:-1]:
                    item = item[key]
                item[path[-1]] = replacement
                gateway = V3Exchange(value)
                with self.assertRaisesRegex(Review, 'INTENT_INVALID'):
                    gateway.submit(value)
                self.assertEqual(gateway.calls, [])

    def test_marker_cannot_be_removed_or_new_fields_partially_missing(self):
        for field in ('risk_model', 'tp_normalization', 'cost_evidence', 'sl_slippage_usdt',
                      'reward_risk_policy', 'tp_tick_size'):
            with self.subTest(field=field):
                value = command()
                del value[field]
                gateway = V3Exchange(value)
                with self.assertRaisesRegex(Review, 'INTENT_INVALID'):
                    gateway.submit(value)
                self.assertEqual(gateway.calls, [])

    def test_provider_below_two_is_refused_without_adopting_new_normalized_tp(self):
        value = command()
        value['tp_normalization']['model_tp'] = '103.9'
        with self.assertRaisesRegex(Review, 'NET_RISK_REWARD_BELOW_2'):
            V3Exchange(value).submit(value)

    def test_tp_timeout_stays_unknown_and_no_entry_or_tp_is_replayed(self):
        value = command()
        gateway = V3Exchange(value, value['entry']['quantity'])
        original = gateway._request
        def request(method, path, params):
            result = original(method, path, params)
            if method == 'POST' and path == '/fapi/v1/algoOrder' and params['type'] == 'TAKE_PROFIT_MARKET':
                raise Review('BINANCE_ORDER_OUTCOME_UNKNOWN')
            return result
        gateway._request = request
        with gateway_mutations(lambda: True):
            with self.assertRaisesRegex(Review, 'PROTECTION_OUTCOME_UNKNOWN'):
                gateway.submit(value)
        self.assertEqual(len(gateway.post_entries()), 1)
        self.assertEqual(len([p for m, path, p in gateway.calls if m == 'POST' and path == '/fapi/v1/algoOrder']), 2)
        gateway._request = original
        with gateway_mutations(lambda: True):
            self.assertEqual(gateway.reconcile(value)['state'], 'POSITION_PROTECTED')
        self.assertEqual(len(gateway.post_entries()), 1)
        self.assertEqual(len([p for m, path, p in gateway.calls if m == 'POST' and path == '/fapi/v1/algoOrder']), 2)

    def test_off_stops_configuration_entry_protection_and_cancellation(self):
        value = command()
        gateway = V3Exchange(value)
        with self.assertRaisesRegex(Review, 'ROBOT_OFF'):
            gateway.submit(value)
        self.assertFalse([c for c in gateway.calls if c[0] != 'GET'])
        gateway = V3Exchange(value, value['entry']['quantity'])
        with gateway_mutations(lambda: True):
            gateway.submit(value)
        before = copy.deepcopy(gateway.algos)
        gateway.exit('sl')
        after_exit = copy.deepcopy(gateway.algos)
        gateway.calls.clear()
        with self.assertRaisesRegex(Review, 'ROBOT_OFF'):
            gateway.reconcile(value)
        self.assertFalse([c for c in gateway.calls if c[0] != 'GET'])
        self.assertEqual(gateway.algos, after_exit)
        self.assertEqual(len(before), len(after_exit))

    def test_off_after_entry_fill_creates_no_later_sl_tp_or_cancel(self):
        value = command()
        gateway = V3Exchange(value, '0.5')
        enabled = [True]
        def hook(method, path, params):
            if method == 'POST' and path == '/fapi/v1/order':
                enabled[0] = False
        gateway.hook = hook
        with gateway_mutations(lambda: enabled[0]):
            with self.assertRaises(Review):
                gateway.submit(value)
        self.assertEqual(len(gateway.post_entries()), 1)
        self.assertFalse(gateway.algos)
        self.assertFalse([c for c in gateway.calls if c[0] == 'DELETE'])
        self.assertEqual(gateway.entry['status'], 'PARTIALLY_FILLED')

    def test_production_request_and_wire_require_explicit_enabled_scope(self):
        gateway = sdk.OrderGateway(api_key='fixture-key-123456', api_secret='fixture-secret-123456')
        with self.assertRaisesRegex(Review, 'ROBOT_OFF'):
            gateway._request('POST', '/fapi/v1/order', {'symbol': 'BTCUSDT'})
        with self.assertRaisesRegex(Review, 'ROBOT_OFF'):
            gateway._wire('DELETE', '/fapi/v1/algoOrder', 'algoId=10', True)
        self.network_mock.assert_not_called()

    def test_wire_rechecks_off_after_server_time_before_actual_write(self):
        enabled = [True]
        gateway = sdk.OrderGateway(api_key='fixture-key-123456', api_secret='fixture-secret-123456')
        def server_time():
            enabled[0] = False
            return 1234567890
        gateway._get_server_time = server_time
        with gateway_mutations(lambda: enabled[0]):
            with self.assertRaisesRegex(Review, 'ROBOT_OFF'):
                gateway._request('POST', '/fapi/v1/order', {'symbol': 'BTCUSDT'})
        self.network_mock.assert_not_called()

    def test_wire_last_check_before_open_blocks_off_during_opener_creation(self):
        enabled = [True]
        gateway = sdk.OrderGateway(api_key='fixture-key-123456', api_secret='fixture-secret-123456')
        def create(*args):
            enabled[0] = False
            return opener
        class Opener:
            def open(self, *args, **kwargs):
                raise AssertionError('financial transport forbidden')
        opener = Opener()
        with patch('urllib.request.build_opener', side_effect=create), gateway_mutations(lambda: enabled[0]):
            with self.assertRaisesRegex(Review, 'ROBOT_OFF'):
                gateway._wire('POST', '/fapi/v1/order', 'symbol=BTCUSDT', True)

    def test_legacy_intents_are_not_repriced_or_reclassified(self):
        for value in (sdk.intent('LONG'), sdk.intent('SHORT')):
            original = json.dumps(value, sort_keys=True)
            sdk.Exchange()._validate_intent(value)
            self.assertEqual(json.dumps(value, sort_keys=True), original)
            self.assertNotIn('risk_model', value)


if __name__ == '__main__':
    unittest.main()
