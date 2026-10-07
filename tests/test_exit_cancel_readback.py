"""Offline owned cancellation proofs and source-only private SDK patch checks."""
import ast
import copy
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy import patch_exit_cancel_readback as patcher
from test_live_gateway import Exchange, Review


ROOT = Path(__file__).resolve().parents[1]


def private_fixture(newline=b'\n'):
    source = (ROOT / 'deploy/live_gateway_template.py').read_bytes()
    assert source.count(patcher.NEW_METHOD.encode()) == 1
    source = source.replace(patcher.NEW_METHOD.encode(), patcher.OLD_METHOD.encode(), 1)
    prefix = b"# Private prefix must survive.\nPRIVATE_KEY = 'offline-only-secret'\n"
    suffix = b"\n# Private suffix must survive.\ndef private_status():\n    return 'private-value'\n"
    return (prefix + source + suffix).replace(b'\n', newline)


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


class ExitCancelSourcePatchTests(unittest.TestCase):
    def test_only_owned_method_changes_and_private_bytes_survive(self):
        raw = private_fixture()
        changed = patcher.transform(raw, hashlib.sha256(raw).hexdigest())
        self.assertEqual(changed, raw.replace(patcher.OLD_METHOD.encode(), patcher.NEW_METHOD.encode(), 1))
        before, after = ast.parse(raw), ast.parse(changed)
        for tree in (before, after):
            cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
            cls.body.remove(next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_cancel_algo'))
        self.assertEqual(ast.dump(before), ast.dump(after))

    def test_crlf_private_bytes_and_method_line_endings_are_preserved(self):
        raw = private_fixture(b'\r\n')
        changed = patcher.transform(raw, hashlib.sha256(raw).hexdigest())
        self.assertEqual(changed, raw.replace(patcher.OLD_METHOD.replace('\n', '\r\n').encode(), patcher.NEW_METHOD.replace('\n', '\r\n').encode(), 1))
        self.assertNotIn(b'\n', changed.replace(b'\r\n', b''))

    def test_wrong_full_hash_method_shape_and_reformat_are_refused(self):
        raw = private_fixture()
        with self.assertRaisesRegex(ValueError, 'GATEWAY_SOURCE_CHANGED'):
            patcher.transform(raw)
        for method in (patcher.OLD_METHOD.replace("quantity = self._decimal(algo['quantity'], positive=True)", "quantity = self._decimal(algo['quantity'])", 1),
                       patcher.OLD_METHOD.replace("        kind = 'sl'", "        kind  = 'sl'", 1)):
            changed = raw.replace(patcher.OLD_METHOD.encode(), method.encode(), 1)
            with self.assertRaisesRegex(ValueError, 'GATEWAY_METHOD'):
                patcher.transform(changed, hashlib.sha256(changed).hexdigest())

    def test_default_inspection_does_not_write_and_apply_preserves_owner_mode(self):
        raw = private_fixture()
        original = patcher.transform
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'order_gateway.py'
            path.write_bytes(raw)
            path.chmod(0o640)
            before = path.stat()
            with patch.object(patcher, 'transform', side_effect=lambda value: original(value, hashlib.sha256(raw).hexdigest())):
                report = patcher.apply(path)
                self.assertEqual(report['status'], 'PATCH_READY')
                self.assertEqual(path.read_bytes(), raw)
                if os.geteuid() == 0:
                    report = patcher.apply(path, True)
                    self.assertEqual(report['status'], 'PATCHED')
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), report['after_sha256'])
                    after = path.stat()
                    self.assertEqual((after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)),
                                     (before.st_uid, before.st_gid, 0o640))

    def test_atomic_apply_refuses_source_changed_during_transform(self):
        raw = private_fixture()
        original = patcher.transform
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'order_gateway.py'
            path.write_bytes(raw)
            def changed_during_patch(value):
                result = original(value, hashlib.sha256(raw).hexdigest())
                path.write_bytes(raw + b'# concurrent edit\n')
                return result
            with patch.object(patcher, 'transform', side_effect=changed_during_patch), patch.object(patcher.os, 'geteuid', return_value=0):
                with self.assertRaisesRegex(ValueError, 'GATEWAY_SOURCE_CHANGED'):
                    patcher.apply(path, True)
            self.assertEqual(path.read_bytes(), raw + b'# concurrent edit\n')
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == '__main__':
    unittest.main()
