"""Offline owned cancellation proofs and bounded readback guards."""
import copy
import unittest
from unittest.mock import patch

from test_live_gateway import Exchange, Review


class ExitCancelReadbackTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch('time.sleep').start()
        self.addCleanup(patch.stopall)

    def prepared(self, snapshots):
        gateway = Exchange(fill='4')
        gateway.submit(gateway.command)
        algo = next(a for a in gateway.algos.values() if a['orderType'] == 'TAKE_PROFIT_MARKET')
        original = gateway._request
        calls = []
        deleted = []
        pending = list(snapshots)
        def request(method, path, params):
            calls.append((method, path, copy.deepcopy(params)))
            if method == 'DELETE':
                deleted.append(True)
                return original(method, path, params)
            if deleted and method == 'GET' and path == '/fapi/v1/algoOrder':
                update = pending.pop(0)
                if isinstance(update, Exception):
                    raise update
                row = copy.deepcopy(algo)
                row.update(update)
                return row
            return original(method, path, params)
        gateway._request = request
        return gateway, copy.deepcopy(algo), calls

    def assert_single_cancel(self, calls, algo, readbacks):
        writes = [call for call in calls if call[0] != 'GET']
        self.assertEqual(writes, [('DELETE', '/fapi/v1/algoOrder', {'algoId': str(algo['algoId'])})])
        reads = [call for call in calls if call[0] == 'GET']
        self.assertEqual(len(reads), 1 + readbacks)
        self.assertTrue(all(call[2] == {'clientAlgoId': algo['clientAlgoId']} for call in reads))

    def test_lagged_new_then_canceled_uses_one_delete_and_read_only_polls(self):
        for count in (1, 2):
            with self.subTest(lagged=count):
                gateway, algo, calls = self.prepared([{'algoStatus': 'NEW'}] * count + [{'algoStatus': 'CANCELED'}])
                gateway._cancel_algo(gateway.command, algo)
                self.assert_single_cancel(calls, algo, count + 1)
                self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [0.5, 1.0][:count])
                self.sleep.reset_mock()

    def test_immediate_canceled_needs_no_sleep_or_additional_read(self):
        gateway, algo, calls = self.prepared([{'algoStatus': 'CANCELED'}])
        gateway._cancel_algo(gateway.command, algo)
        self.assert_single_cancel(calls, algo, 1)
        self.sleep.assert_not_called()

    def test_stable_new_is_bounded_and_never_repeats_cancel_or_posts(self):
        gateway, algo, calls = self.prepared([{'algoStatus': 'NEW'}] * 3)
        with self.assertRaisesRegex(Review, '^BINANCE_ORDER_EXIT_RACE$'):
            gateway._cancel_algo(gateway.command, algo)
        self.assert_single_cancel(calls, algo, 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [0.5, 1.0])

    def test_trigger_or_actual_order_never_delays_or_retries(self):
        for update in ({'algoStatus': 'TRIGGERED'}, {'algoStatus': 'FINISHED'},
                       {'algoStatus': 'NEW', 'actualOrderId': '800'},
                       {'algoStatus': 'CANCELED', 'actualOrderId': '800'},
                       {'algoStatus': 'EXPIRED'}):
            with self.subTest(update=update):
                gateway, algo, calls = self.prepared([update])
                with self.assertRaisesRegex(Review, '^BINANCE_ORDER_EXIT_RACE$'):
                    gateway._cancel_algo(gateway.command, algo)
                self.assert_single_cancel(calls, algo, 1)
                self.sleep.assert_not_called()

    def test_late_trigger_after_new_stops_at_second_readback(self):
        gateway, algo, calls = self.prepared([{'algoStatus': 'NEW'}, {'algoStatus': 'TRIGGERED'}])
        with self.assertRaisesRegex(Review, 'EXIT_RACE'):
            gateway._cancel_algo(gateway.command, algo)
        self.assert_single_cancel(calls, algo, 2)
        self.sleep.assert_called_once_with(0.5)

    def test_each_snapshot_requires_unchanged_ownership_and_quantity(self):
        for update in ({'quantity': '3'}, {'clientAlgoId': 'manual-client'},
                       {'workingType': 'CONTRACT_PRICE'}, {'reduceOnly': False}, {'side': 'BUY'}):
            with self.subTest(update=update):
                gateway, algo, calls = self.prepared([{'algoStatus': 'NEW'}, dict(algoStatus='CANCELED', **update)])
                with self.assertRaisesRegex(Review, 'PROTECTION_PROOF_INVALID'):
                    gateway._cancel_algo(gateway.command, algo)
                self.assert_single_cancel(calls, algo, 2)
                self.sleep.assert_called_once_with(0.5)
                self.sleep.reset_mock()

    def test_readback_error_preserves_unknown_without_additional_attempt(self):
        gateway, algo, calls = self.prepared([Review('BINANCE_ORDER_OUTCOME_UNKNOWN')])
        with self.assertRaisesRegex(Review, 'OUTCOME_UNKNOWN'):
            gateway._cancel_algo(gateway.command, algo)
        self.assert_single_cancel(calls, algo, 1)
        self.sleep.assert_not_called()

    def test_failed_delete_has_no_readback_or_repeated_write(self):
        gateway, algo, calls = self.prepared([])
        original = gateway._request
        def request(method, path, params):
            if method == 'DELETE':
                calls.append((method, path, copy.deepcopy(params)))
                raise Review('BINANCE_ORDER_OUTCOME_UNKNOWN')
            return original(method, path, params)
        gateway._request = request
        with self.assertRaisesRegex(Review, 'OUTCOME_UNKNOWN'):
            gateway._cancel_algo(gateway.command, algo)
        self.assert_single_cancel(calls, algo, 0)
        self.sleep.assert_not_called()

    def test_budget_must_cover_delay_before_sleep_or_extra_read(self):
        gateway, algo, calls = self.prepared([{'algoStatus': 'NEW'}])
        clock = [0.0]
        original = gateway._request
        def request(method, path, params):
            result = original(method, path, params)
            if method == 'DELETE':
                clock[0] = 89.8
            return result
        gateway._request = request
        with patch('time.monotonic', side_effect=lambda: clock[0]):
            with gateway._operation():
                with self.assertRaisesRegex(Review, 'CALLBACK_DEADLINE'):
                    gateway._cancel_algo(gateway.command, algo)
        self.assert_single_cancel(calls, algo, 1)
        self.sleep.assert_not_called()
        self.assertIsNone(gateway._deadline)

    def test_existing_terminal_and_trigger_guards_stay_unchanged(self):
        for state in ('CANCELED', 'EXPIRED', 'REJECTED', 'TRIGGERED', 'FINISHED'):
            with self.subTest(state=state):
                gateway, algo, calls = self.prepared([])
                gateway.algos[algo['clientAlgoId']]['algoStatus'] = state
                if state in ('TRIGGERED', 'FINISHED'):
                    with self.assertRaisesRegex(Review, 'EXIT_RACE'):
                        gateway._cancel_algo(gateway.command, algo)
                else:
                    gateway._cancel_algo(gateway.command, algo)
                self.assertEqual([call[0] for call in calls], ['GET'])
                self.sleep.assert_not_called()

    def test_existing_new_actual_order_or_bad_proof_refuses_before_any_cancel(self):
        for update, reason in (({'actualOrderId': '800'}, 'EXIT_RACE'),
                               ({'quantity': '3'}, 'PROTECTION_PROOF_INVALID'),
                               ({'reduceOnly': False}, 'PROTECTION_PROOF_INVALID')):
            with self.subTest(update=update):
                gateway, algo, calls = self.prepared([])
                gateway.algos[algo['clientAlgoId']].update(update)
                with self.assertRaisesRegex(Review, reason):
                    gateway._cancel_algo(gateway.command, algo)
                self.assertEqual([call[0] for call in calls], ['GET'])
                self.sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
