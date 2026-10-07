"""Offline SDK checks for immutable, tick-rounded net 1:2 future intents."""
import copy
import json
import os
import time
import unittest
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from unittest.mock import patch

import test_live_gateway as sdk
from worker.core import D, FEE_SOURCE, REWARD_RISK_POLICY, Review, Rules, target_reward_risk_tp


class LiveGatewayTargetRewardRiskTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.network = patch('urllib.request.build_opener', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def command(self, side='LONG', *, entry='100', stop=None, tick='0.01', fee='0.0005',
                quantity='2', working_type='CONTRACT_PRICE', tp=None, policy=True):
        stop = stop if stop is not None else '98' if side == 'LONG' else '102'
        rules = Rules(D('0.001'), D('0.001'), D('1000'), D(tick), D('5'), time.time(),
                      taker_fee_rate=D(fee), fee_observed_at=time.time(), fee_symbol='BTCUSDT', fee_source=FEE_SOURCE)
        if tp is None:
            tp = format(target_reward_risk_tp(entry, stop, side, rules, symbol='BTCUSDT'), 'f')
        command = sdk.intent(side)
        command['entry'].update(price=entry, quantity=quantity)
        command['protection'].update(stop_loss=stop, take_profit=tp, working_type=working_type)
        command['fee_evidence']['taker_rate'] = fee
        command['fee_evidence']['observed_at'] = rules.fee_observed_at
        if policy:
            command.update(reward_risk_policy=REWARD_RISK_POLICY, tp_tick_size=tick)
        self.costs(command)
        return command

    def costs(self, command):
        # Rebuild independently supplied evidence, including deliberately rejected TP levels.
        with localcontext() as context:
            context.prec = 200
            e, q = D(command['entry']['price']), D(command['entry']['quantity'])
            sl, tp = D(command['protection']['stop_loss']), D(command['protection']['take_profit'])
            fee = D(command['fee_evidence']['taker_rate'])
            gross = q*abs(e-sl)
            entry_fee, sl_fee, tp_fee = q*e*fee, q*sl*fee, q*tp*fee
            risk, reward = gross+entry_fee+sl_fee, q*abs(tp-e)-entry_fee-tp_fee
            command.update(risk_usdt=format(risk, 'f'), gross_risk_usdt=format(gross, 'f'),
                           entry_fee_usdt=format(entry_fee, 'f'), sl_exit_fee_usdt=format(sl_fee, 'f'),
                           tp_exit_fee_usdt=format(tp_fee, 'f'), net_reward_usdt=format(reward, 'f'),
                           net_reward_risk=format(Context(prec=160).divide(reward, risk), 'f'))

    def gateway(self, command):
        gateway = sdk.Exchange()
        gateway.command = command
        return gateway

    def test_fee_inclusive_targets_match_core_and_are_first_qualifying_ticks(self):
        for side, stop, expected in (('LONG', '98', '104.31'), ('SHORT', '102', '95.7')):
            with self.subTest(side=side):
                command = self.command(side, stop=stop)
                self.assertEqual(D(command['protection']['take_profit']), D(expected))
                gateway = self.gateway(command)
                before = json.dumps(command, sort_keys=True)
                gateway._validate_intent(command)
                self.assertEqual(json.dumps(command, sort_keys=True), before)
                self.assertEqual(gateway.calls, [])
                self.assertGreaterEqual(Fraction(D(command['net_reward_usdt'])), 2*Fraction(D(command['risk_usdt'])))
                nearer = copy.deepcopy(command)
                with localcontext() as context:
                    context.prec = 200
                    tp = D(nearer['protection']['take_profit'])
                    tp += -D('0.01') if side == 'LONG' else D('0.01')
                    nearer['protection']['take_profit'] = format(tp, 'f')
                self.costs(nearer)
                with self.assertRaisesRegex(Review, '^NET_RISK_REWARD_BELOW_2$'):
                    gateway.submit(nearer)
                self.assertEqual(gateway.calls, [])

    def test_farther_tp_and_off_tick_tp_are_refused_without_repricing_or_post(self):
        for side, far, off_tick in (('LONG', '110', '104.311'), ('SHORT', '90', '95.699')):
            for tp in (far, off_tick):
                with self.subTest(side=side, tp=tp):
                    command = self.command(side, tp=tp)
                    before = copy.deepcopy(command)
                    gateway = self.gateway(command)
                    with self.assertRaisesRegex(Review, '^NET_RISK_REWARD_NOT_TARGET_2$'):
                        gateway.submit(command)
                    self.assertEqual(command, before)
                    self.assertEqual(gateway.calls, [])

    def test_zero_fee_and_coarse_ticks_accept_closest_legal_price(self):
        for side, fee, tick, expected in (('LONG', '0', '0.01', '104'), ('SHORT', '0', '0.01', '96'),
                                          ('LONG', '0.0005', '1', '105'), ('SHORT', '0.0005', '1', '95'),
                                          ('LONG', '0.0005', '0.25', '104.5'), ('SHORT', '0.0005', '0.25', '95.5')):
            with self.subTest(side=side, fee=fee, tick=tick):
                command = self.command(side, fee=fee, tick=tick)
                self.assertEqual(D(command['protection']['take_profit']), D(expected))
                self.gateway(command)._validate_intent(command)

    def test_high_precision_long_short_validation_ignores_global_decimal_precision(self):
        for side, stop in (('LONG', '1.123400000000000001'), ('SHORT', '1.123500000000000001')):
            with self.subTest(side=side):
                command = self.command(side, entry='1.123456789012345678', stop=stop,
                                       tick='0.000000000000000001', fee='0.000000000000000137', quantity='1')
                gateway = self.gateway(command)
                before = copy.deepcopy(command)
                with localcontext() as context:
                    context.prec = 6
                    gateway._validate_intent(command)
                self.assertEqual(command, before)
                self.assertEqual(gateway.calls, [])

    def test_invalid_or_absent_tick_and_unknown_policy_refused_before_http(self):
        for tick in (None, '', '0', '-0.01', '1E-2', True, 0.01, [], {}):
            with self.subTest(tick=tick):
                command = self.command()
                command['tp_tick_size'] = tick
                gateway = self.gateway(command)
                with self.assertRaisesRegex(Review, '^BINANCE_ORDER_INTENT_INVALID$'):
                    gateway.submit(command)
                self.assertEqual(gateway.calls, [])
        for policy in (None, '', True, 2, [], {}, 'NET_1_TO_3'):
            with self.subTest(policy=policy):
                command = self.command()
                command['reward_risk_policy'] = policy
                gateway = self.gateway(command)
                with self.assertRaisesRegex(Review, '^BINANCE_ORDER_INTENT_INVALID$'):
                    gateway.submit(command)
                self.assertEqual(gateway.calls, [])
        command = self.command()
        del command['tp_tick_size']
        with self.assertRaisesRegex(Review, '^BINANCE_ORDER_INTENT_INVALID$'):
            self.gateway(command).submit(command)
        command = self.command()
        del command['reward_risk_policy']
        gateway = self.gateway(command)
        with self.assertRaisesRegex(Review, '^BINANCE_ORDER_INTENT_INVALID$'):
            gateway.submit(command)
        self.assertEqual(gateway.calls, [])

    def test_canonical_tp_never_overrides_five_usdt_fee_inclusive_risk_cap(self):
        for side in ('LONG', 'SHORT'):
            with self.subTest(side=side):
                command = self.command(side, quantity='3')
                self.assertGreater(D(command['risk_usdt']), D(command['risk_target_usdt']))
                gateway = self.gateway(command)
                before = copy.deepcopy(command)
                with self.assertRaisesRegex(Review, '^BINANCE_ORDER_INTENT_INVALID$'):
                    gateway.submit(command)
                self.assertEqual(command, before)
                self.assertEqual(gateway.calls, [])

    def test_legacy_wide_reward_and_live_eth_setup_remain_byte_identical(self):
        for command in (sdk.intent(), self.command('LONG', entry='2610', stop='2585', quantity='0.181',
                                                  tp='2730', working_type='MARK_PRICE', policy=False)):
            with self.subTest(symbol=command['symbol'], entry=command['entry']['price']):
                if command['entry']['price'] == '2610':
                    command['symbol'] = 'ETHUSDT'
                    command['fee_evidence']['symbol'] = 'ETHUSDT'
                before = json.dumps(command, sort_keys=True)
                gateway = self.gateway(command)
                gateway._validate_intent(command)
                self.assertNotIn('reward_risk_policy', command)
                self.assertNotIn('tp_tick_size', command)
                self.assertEqual(json.dumps(command, sort_keys=True), before)
                self.assertEqual(gateway.calls, [])

    def test_new_policy_preserves_both_trigger_bases_and_actual_partial_protection(self):
        for side in ('LONG', 'SHORT'):
            for basis in ('MARK_PRICE', 'CONTRACT_PRICE'):
                for filled in ('2', '4'):
                    with self.subTest(side=side, basis=basis, filled=filled):
                        gateway = sdk.Exchange(fill=filled, side=side)
                        gateway.command = self.command(side, stop='99' if side == 'LONG' else '101',
                                                       quantity='4', working_type=basis)
                        expected_tp = gateway.command['protection']['take_profit']
                        result = gateway.submit(gateway.command)
                        self.assertEqual(result['state'], 'POSITION_PROTECTED')
                        self.assertEqual(result['filled_quantity'], filled)
                        posts = [params for method, path, params in gateway.calls
                                 if method == 'POST' and path == '/fapi/v1/algoOrder']
                        self.assertEqual([params['type'] for params in posts], ['STOP_MARKET', 'TAKE_PROFIT_MARKET'])
                        self.assertTrue(all(params['quantity'] == filled and params['workingType'] == basis
                                            and params['reduceOnly'] == 'true' for params in posts))
                        self.assertEqual(posts[1]['triggerPrice'], expected_tp)
                        self.assertEqual(gateway.command['protection']['take_profit'], expected_tp)

    def test_new_policy_does_not_accept_opposite_trigger_basis_proof(self):
        gateway = sdk.Exchange(fill='2')
        gateway.command = self.command(stop='99', quantity='4', working_type='CONTRACT_PRICE')
        gateway.bad_proof = {'workingType': 'MARK_PRICE'}
        with self.assertRaisesRegex(Review, 'PROTECTION_OUTCOME_UNKNOWN'):
            gateway.submit(gateway.command)
        self.assertEqual(len(gateway.post_entries()), 1)
        self.assertEqual(gateway.entry['status'], 'CANCELED')


if __name__ == '__main__':
    unittest.main()
