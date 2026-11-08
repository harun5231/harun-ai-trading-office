"""Phase diagnostics preserve native decisions, unknown outcomes and journal state."""
import copy
import logging
import sqlite3
import unittest
from unittest.mock import patch

from agent import native_pending_review as pending
from tests import test_native_pending_review as fixtures

SENSITIVE = 'https://fapi.binance.com/fapi/v1/order?signature=PRIVATE_SECRET&apiKey=PRIVATE_KEY'
LOGGER = 'agent.native_pending_review'


class PendingDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.PendingReviewTests()
        self.case.setUp()

    def tearDown(self):
        self.case.tearDown()

    def assert_safe(self, logs, phase, kind=None):
        text = '\n'.join(logs.output)
        self.assertIn('phase=' + phase + ' ', text)
        if kind is not None:
            self.assertIn('exception_class=' + kind + ' ', text)
        self.assertNotIn(SENSITIVE, text)
        self.assertNotIn('PRIVATE_SECRET', text)
        self.assertNotIn('PRIVATE_KEY', text)
        self.assertNotIn('Traceback', text)
        for record in logs.records:
            self.assertIsNone(record.exc_info)
            self.assertIsNone(record.stack_info)
        return text

    def test_journal_open_error_keeps_state_guard_and_safe_sqlite_code(self):
        error = sqlite3.OperationalError(SENSITIVE)
        error.sqlite_errorcode = 5
        with self.assertLogs(LOGGER, 'WARNING') as logs, \
                patch.object(pending, '_journal', side_effect=error):
            value = self.case.run_tick()
        text = self.assert_safe(logs, 'JOURNAL_OPEN', 'OperationalError')
        self.assertIn('sqlite_code=5 ', text)
        self.assertEqual(value['failure_code'], 'PENDING_REVIEW_STATE_UNVERIFIED')
        self.assertFalse((self.case.root / pending.JOURNAL_NAME).exists())
        self.assertEqual((self.case.coordinator.ask_count, self.case.coordinator.advance_count), (0, 0))

    def test_gateway_reconcile_exception_keeps_original_intent_and_guard(self):
        original = tuple(self.case.db.execute('SELECT * FROM order_intents').fetchone())
        error = ValueError(SENSITIVE)
        error.code = -1021
        with self.assertLogs(LOGGER, 'WARNING') as logs, \
                patch.object(self.case.coordinator.gateway, 'reconcile', side_effect=error):
            value = self.case.run_tick()
        text = self.assert_safe(logs, 'GATEWAY_RECONCILE', 'ValueError')
        self.assertIn('binance_code=-1021 ', text)
        self.assertEqual(value['failure_code'], 'PENDING_REVIEW_STATE_UNVERIFIED')
        self.assertEqual(tuple(self.case.db.execute('SELECT * FROM order_intents').fetchone()), original)
        self.assertEqual(self.case.coordinator.ask_count, 0)

    def test_cancel_unknown_logs_inner_phase_without_replay_or_completed_claim(self):
        self.case.coordinator.decision = 'BATALKAN'
        self.case.coordinator.gateway.cancel_error = True
        with self.assertLogs(LOGGER, 'WARNING') as logs:
            first = self.case.run_tick()
        text = self.assert_safe(logs, 'CANCEL_REMAINDER', 'ValueError')
        claim = self.case.claims()[0]
        self.assertIn('operation=' + claim['operation'], text)
        self.assertEqual(first['failure_code'], 'PENDING_REVIEW_OUTCOME_UNKNOWN')
        self.assertEqual(claim['state'], 'NEEDS_REVIEW')
        api_before = tuple(self.case.db.execute('SELECT * FROM api_requests').fetchone())
        self.assertEqual(self.case.run_tick()['failure_code'], 'PENDING_REVIEW_OUTCOME_UNKNOWN')
        self.assertEqual(tuple(self.case.db.execute('SELECT * FROM api_requests').fetchone()), api_before)
        self.assertEqual((self.case.coordinator.ask_count, self.case.coordinator.gateway.cancel_count), (1, 1))

    def test_cancel_recovery_exception_preserves_original_ambiguous_claim(self):
        self.case.coordinator.decision = 'BATALKAN'
        self.case.coordinator.gateway.cancel_error = True
        with self.assertLogs(LOGGER, 'WARNING'):
            self.case.run_tick()
        original = copy.deepcopy(self.case.claims())
        with self.assertLogs(LOGGER, 'WARNING') as logs, \
                patch.object(pending, '_recover_cancel_checkpoints', side_effect=ValueError(SENSITIVE)):
            value = self.case.run_tick()
        self.assert_safe(logs, 'RECOVER_CANCEL', 'ValueError')
        self.assertEqual(value['failure_code'], 'PENDING_REVIEW_STATE_UNVERIFIED')
        self.assertEqual(self.case.claims(), original)
        self.assertEqual((self.case.coordinator.ask_count, self.case.coordinator.gateway.cancel_count), (1, 1))

    def test_completed_receipt_recovery_error_preserves_receipt_then_drains_once(self):
        def interrupted(coordinator):
            raise KeyboardInterrupt()
        self.case.coordinator.after_reply = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.case.run_tick()
        api_before = tuple(self.case.db.execute('SELECT * FROM api_requests').fetchone())
        original = copy.deepcopy(self.case.claims())
        with self.assertLogs(LOGGER, 'WARNING') as logs, \
                patch.object(pending, '_recover_completed_decisions', side_effect=ValueError(SENSITIVE)):
            value = self.case.run_tick()
        self.assert_safe(logs, 'RECOVER_COMPLETED', 'ValueError')
        self.assertEqual(value['failure_code'], 'PENDING_REVIEW_STATE_UNVERIFIED')
        self.assertEqual(tuple(self.case.db.execute('SELECT * FROM api_requests').fetchone()), api_before)
        self.assertEqual(self.case.claims(), original)
        self.case.coordinator.after_reply = None
        self.case.run_tick()
        self.assertEqual(self.case.claims()[0]['state'], 'COMPLETE')
        self.assertEqual(self.case.coordinator.ask_count, 1)

    def test_terminal_recovery_exception_does_not_retire_unknown_review(self):
        with self.assertLogs(LOGGER, 'WARNING'):
            result = self.case.uncertain_review_then_terminal_order()
        original = copy.deepcopy(self.case.claims())
        with self.assertLogs(LOGGER, 'WARNING') as logs, \
                patch.object(self.case.coordinator.gateway, 'reconcile', return_value=result), \
                patch.object(pending, '_recover_terminal_order_reviews', side_effect=ValueError(SENSITIVE)):
            value = self.case.run_tick()
        self.assert_safe(logs, 'RECOVER_TERMINAL', 'ValueError')
        self.assertEqual(value['failure_code'], 'PENDING_REVIEW_STATE_UNVERIFIED')
        self.assertEqual(self.case.claims(), original)
        self.assertEqual(self.case.coordinator.advance_count, 0)
        self.assertEqual(self.case.coordinator.ask_count, 1)

    def test_bad_source_receipt_remains_unknown_without_faking_hold_or_retry(self):
        with self.assertLogs(LOGGER, 'WARNING') as logs, \
                patch.object(self.case.native['research_provider'], 'source_evidence',
                             return_value={'source_type': 'UNVERIFIED', 'provider_evidence_sha256': 'a' * 64}):
            value = self.case.run_tick()
        self.assert_safe(logs, 'SOURCE_EVIDENCE', 'PendingReviewUnavailable')
        self.assertEqual(value['failure_code'], 'PENDING_REVIEW_OUTCOME_UNKNOWN')
        self.assertEqual(self.case.claims()[0]['state'], 'NEEDS_REVIEW')
        self.assertIsNone(self.case.claims()[0]['source_sha256'])
        self.assertEqual(self.case.db.execute('SELECT state FROM api_requests').fetchone()[0], 'COMPLETE')
        self.assertEqual((self.case.coordinator.ask_count, self.case.coordinator.gateway.cancel_count), (1, 0))

    def test_logging_failure_cannot_change_the_original_native_response(self):
        with patch.object(self.case.coordinator.gateway, 'reconcile', side_effect=ValueError(SENSITIVE)), \
                patch.object(logging.getLogger(LOGGER), 'warning', side_effect=RuntimeError(SENSITIVE)):
            value = self.case.run_tick()
        self.assertEqual(value['failure_code'], 'PENDING_REVIEW_STATE_UNVERIFIED')
        self.assertEqual(self.case.coordinator.ask_count, 0)

    def test_metadata_accessor_failure_does_not_change_guard_response(self):
        class BrokenMetadata(ValueError):
            @property
            def code(self):
                raise RuntimeError(SENSITIVE)
        with patch.object(self.case.coordinator.gateway, 'reconcile', side_effect=BrokenMetadata(SENSITIVE)):
            value = self.case.run_tick()
        self.assertEqual(value['failure_code'], 'PENDING_REVIEW_STATE_UNVERIFIED')
        self.assertEqual(self.case.coordinator.ask_count, 0)

    def test_diagnostics_never_formats_or_reads_exception_text(self):
        class UnprintableError(ValueError):
            def __str__(self):
                raise AssertionError('EXCEPTION_TEXT_MUST_NOT_BE_READ')
        with self.assertLogs(LOGGER, 'WARNING') as logs, \
                patch.object(self.case.coordinator.gateway, 'reconcile', side_effect=UnprintableError(SENSITIVE)):
            value = self.case.run_tick()
        self.assert_safe(logs, 'GATEWAY_RECONCILE', 'UnprintableError')
        self.assertEqual(value['failure_code'], 'PENDING_REVIEW_STATE_UNVERIFIED')

    def test_diagnostic_metadata_only_accepts_numeric_codes_and_valid_operation(self):
        error = ValueError(SENSITIVE)
        error.code = '-1021'
        error.sqlite_errorcode = True
        error.errno = 101
        with self.assertLogs(LOGGER, 'WARNING') as logs:
            pending._log_review_failure('PENDING_ENTRY', error, operation=SENSITIVE)
        text = self.assert_safe(logs, 'PENDING_ENTRY', 'ValueError')
        self.assertIn('binance_code=None ', text)
        self.assertIn('sqlite_code=None ', text)
        self.assertIn('errno=101 ', text)
        self.assertIn('operation=None', text)


if __name__ == '__main__':
    unittest.main()
