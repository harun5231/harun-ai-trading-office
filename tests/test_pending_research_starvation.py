"""No-send recovery remains reachable behind fully filled protected positions."""
import ast
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from agent import native_pending_review as pending
from tests import test_native_pending_review as fixtures
from worker import screening_response_recovery

ROOT = Path(__file__).resolve().parents[1]


class PendingResearchStarvationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PendingReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.coordinator = self.fixture.coordinator
        self.coordinator.stopping = None
        self.coordinator.store.report_account = lambda account: None
        self.coordinator.settle_previous_research = lambda today, account: None
        # The original native active-order branch returns None once monitoring
        # succeeds and there is no READY candidate. No submit adapter exists.
        self.coordinator.advance_execution = lambda account, today: None
        self.operation = 'current-screening-original-operation'
        self.fixture.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            (self.operation, 'original-idempotency', 'a'*64, 'NEEDS_REVIEW',
             time.time(), 1, None, 'NEUROBRO_BROWSER_OUTCOME_UNKNOWN'))

    def native_tick(self):
        path = ROOT/'vps_native/worker/robot.py'
        cls = next(node for node in ast.parse(path.read_text()).body
                   if isinstance(node, ast.ClassDef) and node.name == 'Coordinator')
        method = next(node for node in cls.body
                      if isinstance(node, ast.FunctionDef) and node.name == '_tick')
        namespace = dict(__name__='worker._pending_research_fixture', __package__='worker',
            day=lambda: self.fixture.today, account_state=lambda *args: {})
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), namespace)
        return namespace['_tick'](self.coordinator)

    def seed_entry(self, status, filled):
        c = self.coordinator
        c.gateway.status, c.gateway.filled = status, filled
        observation = c.gateway.reconcile(self.fixture.intent)
        c.record_gateway_observation(self.fixture.operation, self.fixture.operation, observation, {})
        self.payload = tuple(self.fixture.db.execute('SELECT payload,created FROM order_intents').fetchone())
        self.plan = self.fixture.db.execute('SELECT plan FROM robot_candidates').fetchone()[0]
        self.request = tuple(self.fixture.db.execute('SELECT * FROM api_requests').fetchone())
        c.gateway.calls.clear()

    def assert_no_replay_or_order_mutation(self):
        self.assertEqual(self.coordinator.ask_count, 0)
        self.assertEqual(self.coordinator.gateway.cancel_count, 0)
        self.assertEqual(tuple(self.fixture.db.execute('SELECT payload,created FROM order_intents').fetchone()), self.payload)
        self.assertEqual(self.fixture.db.execute('SELECT plan FROM robot_candidates').fetchone()[0], self.plan)
        self.assertEqual(tuple(self.fixture.db.execute('SELECT * FROM api_requests').fetchone()), self.request)
        self.assertEqual(self.fixture.claims(), [])

    def test_filled_protected_order_does_not_starve_existing_screening_recovery(self):
        self.seed_entry('FILLED', '1')
        recovered = {'bot_status': 'WAITING', 'wait_reason': 'EXISTING_SCREENING_RECOVERY_REACHED'}
        with patch.object(screening_response_recovery, 'resume_uncertain_screening', return_value=recovered) as recovery:
            self.assertEqual(self.native_tick(), recovered)
        recovery.assert_called_once_with(self.coordinator, self.fixture.today, {})
        self.assertIn('native_owned_entry_get', self.coordinator.gateway.calls)
        self.assertEqual(self.fixture.db.execute('SELECT state FROM order_intents').fetchone()[0], 'POSITION_PROTECTED')
        self.assert_no_replay_or_order_mutation()

    def assert_pending_review_keeps_unknown_research_guard(self, status, filled):
        self.seed_entry(status, filled)
        with patch.object(screening_response_recovery, 'resume_uncertain_screening') as recovery:
            result = self.native_tick()
        self.assertEqual((result['bot_status'], result['failure_code']),
                         ('NEEDS_REVIEW', 'ROBOT_REQUEST_NEEDS_REVIEW'))
        recovery.assert_not_called()
        self.assertIn('native_reconcile_ownership_protection', self.coordinator.gateway.calls)
        self.assert_no_replay_or_order_mutation()

    def test_due_new_remainder_cannot_start_review_over_unknown_research(self):
        self.assert_pending_review_keeps_unknown_research_guard('NEW', '0')

    def test_due_partial_remainder_retains_protection_and_unknown_guard(self):
        self.assert_pending_review_keeps_unknown_research_guard('PARTIALLY_FILLED', '0.25')
        row = self.fixture.db.execute('SELECT state,result FROM order_intents').fetchone()
        self.assertEqual(row['state'], 'POSITION_PROTECTED')
        self.assertIn('"sl_confirmed": true', row['result'])
        self.assertIn('"tp_confirmed": true', row['result'])
