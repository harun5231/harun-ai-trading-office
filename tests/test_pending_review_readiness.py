"""Read-only browser readiness before preparing an overdue owned review."""
from datetime import datetime
import sqlite3
import types
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from agent import native_pending_review as pending
from tests import test_native_pending_review as fixtures
from worker.core import Review


class PendingReviewReadinessTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.PendingReviewTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def assert_no_discussion(self):
        self.assertEqual(self.case.coordinator.ask_count, 0)
        self.assertEqual(self.case.coordinator.gateway.cancel_count, 0)
        self.assertEqual(self.case.db.execute('SELECT count(*) FROM api_requests').fetchone()[0], 0)
        self.assertEqual(self.case.db.execute('SELECT count(*) FROM research_browser_requests').fetchone()[0], 0)
        self.assertFalse((self.case.root / pending.JOURNAL_NAME).exists())

    def test_known_pauses_reconcile_order_but_prepare_no_material_or_claim(self):
        original = dict(self.case.db.execute('SELECT * FROM order_intents').fetchone())
        for reason in sorted(pending._BROWSER_PAUSES):
            with self.subTest(reason=reason):
                self.case.coordinator.neuro.require_key = Mock(side_effect=Review(reason))
                with patch.object(self.case.native['analysis'], 'analysis_context') as context, \
                        patch.object(pending, 'estimate_owned_order') as estimate, \
                        patch.object(self.case.coordinator, 'account_factory') as account:
                    result = self.case.run_tick()
                self.assertEqual(result['wait_reason'], 'PENDING_REVIEW_' + reason[len('NEUROBRO_'):])
                self.assertEqual(result['bot_status'], 'WAITING')
                context.assert_not_called()
                estimate.assert_not_called()
                account.assert_not_called()
                self.case.coordinator.neuro.require_key.assert_called_once_with()
                self.assert_no_discussion()
                row = dict(self.case.db.execute('SELECT * FROM order_intents').fetchone())
                self.assertEqual((row['payload'], row['created']), (original['payload'], original['created']))
        self.assertIn('native_reconcile_ownership_protection', self.case.coordinator.gateway.calls)
        self.assertIn('native_owned_entry_get', self.case.coordinator.gateway.calls)

    def test_repeated_challenge_then_connected_reviews_original_due_order_once(self):
        readiness = Mock(side_effect=Review('NEUROBRO_CHALLENGE_REQUIRED'))
        self.case.coordinator.neuro.require_key = readiness
        original = dict(self.case.db.execute('SELECT * FROM order_intents').fetchone())
        for _ in range(3):
            self.assertEqual(self.case.run_tick()['wait_reason'], 'PENDING_REVIEW_CHALLENGE_REQUIRED')
        self.assert_no_discussion()
        readiness.side_effect = None
        self.assertEqual(self.case.run_tick()['bot_status'], 'ENTRY_PENDING')
        claim = self.case.claims()[0]
        receipt_time = self.case.db.execute('SELECT received_at FROM research_browser_responses').fetchone()[0]
        self.assertEqual((claim['state'], claim['decision'], claim['entry_created_at']),
                         ('COMPLETE', 'TAHAN', original['created']))
        self.assertEqual(claim['next_due'], receipt_time + pending.REVIEW_SECONDS)
        self.assertEqual(self.case.coordinator.ask_count, 1)
        calls = readiness.call_count
        self.assertEqual(self.case.run_tick()['bot_status'], 'ORIGINAL_ADVANCE_UNCHANGED')
        self.assertEqual(readiness.call_count, calls)
        self.assertEqual(self.case.claims(), [claim])
        self.assertEqual(self.case.coordinator.ask_count, 1)

    def test_existing_unsent_checkpoint_survives_readiness_wait_and_restart(self):
        self.case.coordinator.raise_at_ask = fixtures.ResearchPaused('NEUROBRO_CHALLENGE_REQUIRED')
        self.assertEqual(self.case.run_tick()['wait_reason'], 'PENDING_REVIEW_WAITING')
        prior = self.case.claims()[0]
        self.assertEqual(prior['state'], 'UNSENT')
        self.assertEqual(self.case.db.execute('SELECT count(*) FROM api_requests').fetchone()[0], 0)
        clock = prior['next_due'] + 1
        with patch.object(pending.time, 'time', return_value=clock):
            self.case.today = datetime.fromtimestamp(clock, ZoneInfo('Asia/Jakarta')).date().isoformat()
            self.case.coordinator.neuro.require_key = Mock(side_effect=Review('NEUROBRO_CHALLENGE_REQUIRED'))
            with patch.object(self.case.native['analysis'], 'analysis_context') as context:
                self.assertEqual(self.case.run_tick()['wait_reason'], 'PENDING_REVIEW_CHALLENGE_REQUIRED')
            context.assert_not_called()
            self.assertEqual(self.case.claims(), [prior])
            # Reopen the real local journals, retaining the same owned fake order.
            gateway = self.case.coordinator.gateway
            self.case.db.close()
            self.case.db = sqlite3.connect(self.case.root / 'ledger.sqlite3', isolation_level=None)
            self.case.db.row_factory = sqlite3.Row
            self.case.coordinator = fixtures.Coordinator(self.case)
            self.case.coordinator.gateway = gateway
            for name, function in fixtures.native_methods().items():
                setattr(self.case.coordinator, name, types.MethodType(function, self.case.coordinator))
            readiness = Mock()
            self.case.coordinator.neuro.require_key = readiness
            self.assertEqual(self.case.run_tick()['bot_status'], 'ENTRY_PENDING')
            readiness.assert_called_once_with()
            claims = self.case.claims()
            self.assertEqual(claims[0], prior)
            self.assertEqual((claims[1]['state'], claims[1]['decision'], claims[1]['sequence']),
                             ('COMPLETE', 'TAHAN', 2))
            self.assertEqual(claims[1]['next_due'], clock + pending.REVIEW_SECONDS)
            self.assertEqual(self.case.coordinator.ask_count, 1)
            self.assertEqual(self.case.coordinator.gateway.cancel_count, 0)

    def test_unknown_review_is_not_replayed_after_browser_recovers(self):
        self.case.coordinator.decision = 'BATALKAN'
        self.case.coordinator.gateway.cancel_error = True
        with self.assertLogs('agent.native_pending_review', 'WARNING'):
            self.assertEqual(self.case.run_tick()['failure_code'], 'PENDING_REVIEW_OUTCOME_UNKNOWN')
        before_claims = self.case.claims()
        before_api = tuple(self.case.db.execute('SELECT * FROM api_requests').fetchone())
        readiness = Mock(side_effect=Review('NEUROBRO_CHALLENGE_REQUIRED'))
        self.case.coordinator.neuro.require_key = readiness
        self.assertEqual(self.case.run_tick()['failure_code'], 'PENDING_REVIEW_OUTCOME_UNKNOWN')
        readiness.assert_not_called()
        self.assertEqual(self.case.claims(), before_claims)
        self.assertEqual(tuple(self.case.db.execute('SELECT * FROM api_requests').fetchone()), before_api)
        self.assertEqual((self.case.coordinator.ask_count, self.case.coordinator.gateway.cancel_count), (1, 1))

    def test_unresolved_research_precedes_new_review_readiness(self):
        self.case.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            ('original-analysis', None, 'a' * 64, 'NEEDS_REVIEW',
             self.case.now, 1, None, 'NEUROBRO_BROWSER_OUTCOME_UNKNOWN'))
        original = tuple(self.case.db.execute('SELECT * FROM api_requests').fetchone())
        readiness = Mock()
        self.case.coordinator.neuro.require_key = readiness
        self.assertEqual(self.case.run_tick()['failure_code'], 'ROBOT_REQUEST_NEEDS_REVIEW')
        readiness.assert_not_called()
        self.assertEqual(tuple(self.case.db.execute('SELECT * FROM api_requests').fetchone()), original)
        self.assertFalse((self.case.root / pending.JOURNAL_NAME).exists())
        self.assertEqual(self.case.coordinator.ask_count, 0)
        self.assertIn('native_reconcile_ownership_protection', self.case.coordinator.gateway.calls)

    def test_off_and_not_due_skip_readiness(self):
        readiness = Mock()
        self.case.coordinator.neuro.require_key = readiness
        self.case.set_on(False)
        self.assertEqual(self.case.run_tick()['wait_reason'], 'ROBOT_OFF')
        self.assertEqual(self.case.coordinator.gateway.calls, [])
        readiness.assert_not_called()
        self.case.set_on(True)
        self.case.accepted = datetime.fromtimestamp(self.case.now - 60, ZoneInfo('UTC')).isoformat()
        self.assertEqual(self.case.run_tick()['bot_status'], 'ORIGINAL_ADVANCE_UNCHANGED')
        readiness.assert_not_called()
        self.assert_no_discussion()

    def test_owner_off_during_readiness_prevents_materials_and_claim(self):
        self.case.coordinator.neuro.require_key = lambda: self.case.set_on(False)
        with patch.object(self.case.native['analysis'], 'analysis_context') as context:
            self.assertEqual(self.case.run_tick()['wait_reason'], 'ROBOT_OFF')
        context.assert_not_called()
        self.assert_no_discussion()

    def test_unknown_readiness_errors_keep_sanitized_guard(self):
        secret = 'https://example.invalid/?signature=DO_NOT_PRINT'
        for error in (ValueError(secret), Review(secret), ValueError('NEUROBRO_CHALLENGE_REQUIRED')):
            with self.subTest(exception=type(error).__name__):
                self.case.coordinator.neuro.require_key = Mock(side_effect=error)
                with self.assertLogs('agent.native_pending_review', 'WARNING') as logs:
                    result = self.case.run_tick()
                self.assertEqual(result['failure_code'], 'PENDING_REVIEW_STATE_UNVERIFIED')
                text = '\n'.join(logs.output)
                self.assertIn('phase=BROWSER_READINESS ', text)
                self.assertNotIn(secret, text)
                self.assertNotIn('DO_NOT_PRINT', text)
                self.assert_no_discussion()

    def test_mapping_uses_literal_trusted_reason_without_formatting_exception(self):
        class UnprintableReview(Review):
            def __str__(self):
                raise AssertionError('must not format exception text')
        self.case.coordinator.neuro.require_key = Mock(side_effect=UnprintableReview('NEUROBRO_CHALLENGE_REQUIRED'))
        self.assertEqual(self.case.run_tick()['wait_reason'], 'PENDING_REVIEW_CHALLENGE_REQUIRED')
        self.assert_no_discussion()


if __name__ == '__main__':
    unittest.main()
