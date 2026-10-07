"""Offline gateway failures keep their claim and reveal only fixed diagnostic codes."""
from pathlib import Path
import unittest

import test_order_pipeline as pipeline
from worker.core import Review
from worker.robot import Coordinator


ROOT = Path(__file__).resolve().parents[1]
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
        # Recovery may read existing exchange evidence, but an unavailable
        # proof must retain the original uncertainty and never resubmit.
        gateway.on_reconcile = sdk_failure('_configure', error)
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
            (1, 3, original_calls))
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
            (1, 4, original_calls))
        self.assertEqual(fixture.intent()['result'], previous)


if __name__ == '__main__':
    unittest.main()
