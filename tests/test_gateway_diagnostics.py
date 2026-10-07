"""Offline gateway failures keep their claim and reveal only fixed diagnostic codes."""
import ast
import importlib.util
from pathlib import Path
import stat
import tempfile
import unittest

import test_order_pipeline as pipeline
from worker.core import Review
from worker.robot import Coordinator


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('patch_gateway_diagnostics',
    ROOT / 'deploy/patch_gateway_diagnostics.py')
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)
CURRENT = (ROOT / 'worker/robot.py').read_text()
BASELINE = CURRENT.replace(tool.HELPER, '').replace(tool.NEW_R, tool.OLD_R).replace(tool.NEW_S, tool.OLD_S)
SDK_FILE = str(ROOT / 'worker/order_gateway.py')
PRIVATE = 'API_KEY_SYNTHETIC_PRIVATE_MESSAGE_MUST_NOT_APPEAR'


def sdk_failure(name, error, filename=SDK_FILE):
    """Compile only a throwing fixture with the real module filename; no SDK I/O."""
    namespace = {}
    exec(compile('def ' + name + '(error):\n    raise error\n', filename, 'exec'), namespace)
    return lambda intent: namespace[name](error.with_traceback(None))


class GatewayDiagnosticsTests(unittest.TestCase):
    def classify(self, name, error, filename=SDK_FILE):
        try: sdk_failure(name, error, filename)(None)
        except Exception as failure:
            result = Coordinator.gateway_failure_code(None, failure)
        self.assertNotIn(PRIVATE, result)
        return result

    def fixture(self):
        fixture = pipeline.OrderPipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_runtime_helper_numeric_stage_known_review_and_redaction(self):
        error = Review('BINANCE_ORDER_REQUEST_FAILED')
        error.code = -2015
        self.assertEqual(self.classify('_configure', error), 'ORDER_UNKNOWN_CONFIGURE_C2015')
        self.assertEqual(self.classify('_preflight', Review('BINANCE_ORDER_FEE_CHANGED')),
            'BINANCE_ORDER_FEE_CHANGED')
        self.assertEqual(self.classify('_configure', Review('BINANCE_ORDER_OUTCOME_UNKNOWN')),
            'BINANCE_ORDER_OUTCOME_UNKNOWN_CONFIGURE')
        self.assertEqual(self.classify('_entry', Review('BINANCE_ORDER_PROOF_INVALID')),
            'BINANCE_ORDER_PROOF_INVALID_ENTRY')
        self.assertEqual(self.classify('_ensure_algo', ValueError(PRIVATE)), 'ORDER_UNKNOWN_PROTECTION')
        self.assertEqual(self.classify('_validate_intent', ValueError(PRIVATE)), 'ORDER_UNKNOWN_VALIDATE')
        self.assertEqual(self.classify('_preflight', error, '/untrusted/worker/order_gateway.py'),
            'ORDER_OUTCOME_UNKNOWN')
        self.assertEqual(self.classify('unknown_sdk_function', error), 'ORDER_OUTCOME_UNKNOWN')
        for invalid in (1234567, True, PRIVATE):
            error.code = invalid
            self.assertEqual(self.classify('_configure', error), 'BINANCE_ORDER_REQUEST_FAILED_CONFIGURE')

    def test_submit_diagnostic_keeps_unknown_claim_immutable_and_never_replays_after_restart(self):
        fixture = self.fixture()
        fixture.one_slot_ready()
        gateway = fixture.connected()
        error = Review('BINANCE_ORDER_REQUEST_FAILED')
        error.code = -2015
        gateway.on_submit = sdk_failure('_configure', error)
        result = fixture.robot.tick()
        self.assertEqual((result['bot_status'], result['failure_code']),
            ('NEEDS_REVIEW', 'ORDER_UNKNOWN_CONFIGURE_C2015'))
        intent = fixture.intent()
        self.assertEqual((intent['state'], intent['failure_code'], intent['result']),
            ('NEEDS_REVIEW', 'ORDER_UNKNOWN_CONFIGURE_C2015', None))
        self.assertEqual(tuple(fixture.ledger.db.execute('SELECT status,failure_code FROM robot_candidates').fetchone()),
            ('NEEDS_REVIEW', 'ORDER_UNKNOWN_CONFIGURE_C2015'))
        original_payload, original_calls = intent['payload'], len(fixture.calls)
        self.assertEqual(fixture.receipt_count(), 0)
        fixture.restart()
        repeated = fixture.ticks(3)
        self.assertEqual(repeated['failure_code'], 'ORDER_UNKNOWN_CONFIGURE_C2015')
        self.assertEqual((len(gateway.submissions), len(gateway.reconciliations), len(fixture.calls)),
            (1, 0, original_calls))
        self.assertEqual(fixture.intent()['payload'], original_payload)
        self.assertEqual(fixture.account.positions[0]['symbol'], 'HYPEUSDT')
        self.assertEqual(fixture.account.positions[0]['positionAmt'], '4.16')

    def test_reconcile_diagnostic_preserves_previous_receipt_evidence_and_blocks_replay(self):
        fixture = self.fixture()
        fixture.one_slot_ready()
        gateway = fixture.connected()
        self.assertEqual(fixture.robot.tick()['bot_status'], 'ENTRY_PENDING')
        previous = fixture.intent()['result']
        error = Review('BINANCE_ORDER_REQUEST_FAILED')
        error.code = -1021
        gateway.on_reconcile = sdk_failure('_ensure_algo', error)
        result = fixture.robot.tick()
        self.assertEqual((result['bot_status'], result['failure_code']),
            ('NEEDS_REVIEW', 'ORDER_UNKNOWN_PROTECTION_C1021'))
        self.assertEqual(fixture.intent()['result'], previous)
        self.assertEqual(fixture.intent()['state'], 'NEEDS_REVIEW')
        self.assertEqual(fixture.receipt_count(), 0)
        original_calls = len(fixture.calls)
        fixture.restart()
        fixture.ticks(3)
        self.assertEqual((len(gateway.submissions), len(gateway.reconciliations), len(fixture.calls)),
            (1, 1, original_calls))
        self.assertEqual(fixture.intent()['result'], previous)

    def test_exact_patch_preserves_other_functions_refuses_anchor_changes_and_is_idempotent(self):
        patched = tool.patch_source(BASELINE)
        self.assertEqual(patched, CURRENT)
        self.assertEqual(tool.patch_source(patched), patched)
        before = next(node for node in ast.parse(BASELINE).body
            if isinstance(node, ast.ClassDef) and node.name == 'Coordinator')
        after = next(node for node in ast.parse(patched).body
            if isinstance(node, ast.ClassDef) and node.name == 'Coordinator')
        for method in before.body:
            if isinstance(method, ast.FunctionDef) and method.name != 'advance_execution':
                self.assertEqual(ast.dump(method), ast.dump(next(node for node in after.body
                    if isinstance(node, ast.FunctionDef) and node.name == method.name)))
        self.assertEqual(patched.count('reason=self.gateway_failure_code(error)'), 2)
        with self.assertRaises(ValueError):
            tool.patch_source(BASELINE.replace(tool.OLD_S, tool.OLD_S.replace('except Exception:', 'except RuntimeError:')))
        with self.assertRaises(ValueError):
            tool.patch_source(patched.replace("return 'ORDER_UNKNOWN_'+phase", "return 'PRIVATE_SECRET'"))

    def test_atomic_apply_private_backup_preserves_sdk_owner_modes_and_line_endings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'project'
            worker = root / 'worker'
            worker.mkdir(parents=True)
            robot = worker / 'robot.py'
            robot.write_bytes(BASELINE.replace('\n', '\r\n').encode())
            robot.chmod(0o640)
            sdk = worker / 'order_gateway.py'
            sdk.write_bytes(b'PRIVATE_CUSTOM_SDK_UNCHANGED')
            raw, owner = robot.read_bytes(), (robot.stat().st_uid, robot.stat().st_gid)
            before = list(Path(temporary).rglob('*'))
            self.assertEqual(tool.apply(root)['status'], 'PATCH_READY')
            self.assertEqual(list(Path(temporary).rglob('*')), before)
            self.assertEqual(robot.read_bytes(), raw)
            result = tool.apply(root, True)
            self.assertEqual(result['status'], 'PATCHED')
            self.assertEqual(Path(result['backup']).read_bytes(), raw)
            self.assertEqual(stat.S_IMODE(Path(result['backup']).parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(Path(result['backup']).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(robot.stat().st_mode), 0o640)
            self.assertEqual((robot.stat().st_uid, robot.stat().st_gid), owner)
            self.assertEqual(sdk.read_bytes(), b'PRIVATE_CUSTOM_SDK_UNCHANGED')
            self.assertIn(b'\r\n', robot.read_bytes())
            self.assertEqual(tool.apply(root, True)['status'], 'UNCHANGED')
            self.assertEqual(len(list(Path(result['backup']).parent.iterdir())), 1)


if __name__ == '__main__':
    unittest.main()
