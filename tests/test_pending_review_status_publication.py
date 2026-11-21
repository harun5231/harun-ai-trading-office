"""Offline regression for passive native receipts masking a pending-review guard.

The receipt methods come unchanged from vps_native/worker/robot.py through the
existing native pending fixture. RobotStore reporting, ProtectionActor._record
and FillPriority.run_one are real. Only plan provenance and the exchange/browser
boundaries are synthetic; no service, network, model or order is contacted.
"""
import copy
from datetime import datetime
import hashlib
import json
import sqlite3
import types
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from agent import native_pending_review as pending
from tests import test_native_pending_review as fixtures
from worker import carry_quota, robot_store
from worker.fill_priority import FillPriority
from worker.protection_actor import ProtectionActor


class _OfflinePlanStore(robot_store.RobotStore):
    def verified_plan(self, row):
        return fixtures.NativeProofStore.verified_plan(self, row)


class PendingReviewStatusPublicationTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.PendingReviewTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.coordinator = self.case.coordinator
        self.db = self.case.db
        self.gateway_status = patch.object(robot_store, 'OrderGateway',
            return_value=types.SimpleNamespace(status=lambda: 'OFFLINE_NATIVE_GATEWAY'))
        self.gateway_status.start()
        self.addCleanup(self.gateway_status.stop)
        self.db.executescript('''
            CREATE TABLE robot_status(id INTEGER PRIMARY KEY,data TEXT NOT NULL);
            CREATE TABLE office_activity(seq INTEGER PRIMARY KEY,at TEXT NOT NULL,
                state TEXT NOT NULL,agent TEXT NOT NULL,message TEXT NOT NULL);
        ''' + carry_quota.SCHEMA)
        self.actor = ProtectionActor.__new__(ProtectionActor)
        self.actor.db, self.actor.coordinator = self.db, self.coordinator
        self.queue = FillPriority(self.db)

    def seed_guard(self, *, paused=False):
        # Let the real pending-review code persist the claim and select its
        # guard. A synthetic browser exception is not a claimed provider reply.
        self.coordinator.raise_at_ask = (fixtures.ResearchPaused('NEUROBRO_CHALLENGE_REQUIRED')
            if paused else ValueError('OFFLINE_UNCERTAIN_DISCUSSION'))
        with self.assertLogs('agent.native_pending_review', 'WARNING'):
            guard = self.case.run_tick()
        claim = self.case.claims()[-1]
        self.assertEqual(claim['state'], 'UNSENT' if paused else 'NEEDS_REVIEW')
        self.coordinator.store = _OfflinePlanStore(self.db, initialize=False)
        self.coordinator.store.report(guard['bot_status'], {},
            reason=guard.get('failure_code'), wait_reason=guard.get('wait_reason'))
        self.guard = self.persisted_status()
        self.claim_digest = self.claims_digest()
        self.frozen = tuple(self.db.execute('SELECT payload,created FROM order_intents').fetchone())
        return guard

    def persisted_status(self):
        return json.loads(self.db.execute('SELECT data FROM robot_status WHERE id=1').fetchone()[0])

    def claims_digest(self):
        return hashlib.sha256(json.dumps(self.case.claims(), sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()

    def passive_receipt(self, *, filled='0', incomplete=False):
        self.coordinator.gateway.filled = filled
        self.coordinator.gateway.status = 'PARTIALLY_FILLED' if filled != '0' else 'NEW'
        result = self.coordinator.gateway.reconcile(self.case.intent)
        if incomplete:
            result.update(state='PROTECTION_INCOMPLETE', tp_confirmed=False,
                tp_order_id=None)
        self.queue.enqueue(self.case.intent, '111', filled, recovery=True)
        acknowledgements = []

        def persist(operation, observed):
            acknowledged = self.actor._record(operation, observed)
            acknowledgements.append(acknowledged)
            return acknowledged

        queued = self.queue.run_one(lambda intent: copy.deepcopy(result), persist)
        self.assertEqual(len(acknowledgements), 1)
        self.assertNotEqual(acknowledgements[0].get('bot_status'), 'NEEDS_REVIEW')
        self.assertNotEqual(queued.get('failure_code'), 'FILL_OBSERVATION_NOT_PERSISTED')
        self.assertEqual(tuple(self.db.execute('SELECT payload,created FROM order_intents').fetchone()), self.frozen)
        self.assertEqual(self.claims_digest(), self.claim_digest)
        self.assertEqual(self.coordinator.gateway.cancel_count, 0)
        return queued

    def assert_guard_preserved(self):
        actual = self.persisted_status()
        for key in ('bot_status', 'failure_code', 'wait_reason', 'checked_at'):
            self.assertEqual(actual.get(key), self.guard.get(key), key)

    def test_passive_pending_receipt_keeps_uncertain_review_visible_and_priority_complete(self):
        self.seed_guard()
        before_events = self.db.execute('SELECT COUNT(*) FROM office_activity').fetchone()[0]
        queued = self.passive_receipt()
        self.assertEqual(queued['state'], 'ENTRY_PENDING')
        self.assertFalse(queued['retry_required'])
        self.assertEqual(self.db.execute('SELECT state FROM fill_priority').fetchone()[0], 'COMPLETE')
        self.assert_guard_preserved()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM office_activity').fetchone()[0], before_events)

    def test_passive_pending_receipt_keeps_unsent_review_wait_and_original_timestamp(self):
        self.seed_guard(paused=True)
        self.assertEqual(self.guard['wait_reason'], 'PENDING_REVIEW_WAITING')
        self.passive_receipt()
        self.assert_guard_preserved()

    def test_protected_partial_receipt_does_not_clear_other_pending_review_work(self):
        self.seed_guard()
        queued = self.passive_receipt(filled='0.25')
        self.assertEqual(queued['state'], 'POSITION_OPEN_PROTECTED')
        self.assertFalse(queued['retry_required'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 1)
        observation = json.loads(self.db.execute('SELECT result FROM order_intents').fetchone()[0])
        self.assertTrue(observation['sl_confirmed'] and observation['tp_confirmed'])
        self.assert_guard_preserved()

    def test_new_pending_error_between_passive_read_and_write_is_not_lost(self):
        self.seed_guard()
        self.coordinator.store.report('ENTRY_PENDING', {})
        # Two real SQLite connections deterministically interleave the pending
        # writer after report's Python read. A stale Python-only condition would
        # pass sequential tests but overwrite this newer coordinator failure.
        other_db = sqlite3.connect(self.case.root / 'ledger.sqlite3', isolation_level=None)
        other_db.row_factory = sqlite3.Row
        other_store = _OfflinePlanStore(other_db, initialize=False)
        main_db = self.db
        case = self

        class InterleavedConnection:
            injected = False

            def execute(self, sql, *args, **kwargs):
                if not self.injected and 'ROBOT_STATUS' in sql.upper() and any(
                        token in sql.upper() for token in ('INSERT', 'UPDATE')):
                    self.injected = True
                    other_store.report('NEEDS_REVIEW', {},
                        reason='PENDING_REVIEW_OUTCOME_UNKNOWN')
                    case.guard = case.persisted_status()
                return main_db.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(main_db, name)

        interleaved = InterleavedConnection()
        self.coordinator.store.db = interleaved
        try:
            self.passive_receipt()
            self.assertTrue(interleaved.injected)
            self.assert_guard_preserved()
        finally:
            self.coordinator.store.db = main_db
            other_db.close()

    def test_critical_incomplete_protection_overrides_pending_review_guard(self):
        self.seed_guard()
        queued = self.passive_receipt(filled='0.25', incomplete=True)
        self.assertEqual(queued['state'], 'PROTECTION_INCOMPLETE')
        self.assertTrue(queued['retry_required'])
        self.assertEqual(self.persisted_status()['bot_status'], 'PROTECTION_INCOMPLETE')
        observation = json.loads(self.db.execute('SELECT result FROM order_intents').fetchone()[0])
        self.assertTrue(observation['sl_confirmed'])
        self.assertFalse(observation['tp_confirmed'])

    def test_owner_off_is_not_changed_by_background_pending_reconciliation(self):
        self.seed_guard()
        self.case.set_on(False)
        self.passive_receipt()
        self.assertEqual(tuple(self.db.execute('SELECT enabled,risk FROM robot_settings').fetchone()), (0, '5'))
        public = self.coordinator.store.snapshot()
        self.assertFalse(public['robot_on'])
        self.assertEqual((public['bot_status'], public['wait_reason']), ('OFF', 'ROBOT_OFF'))

    def test_verified_tahan_recovery_can_clear_old_wait_without_replaying_old_claim(self):
        self.seed_guard(paused=True)
        old = self.case.claims()[0]
        self.coordinator.raise_at_ask = None
        due = old['next_due'] + 1
        self.case.today = datetime.fromtimestamp(due, ZoneInfo('Asia/Jakarta')).date().isoformat()
        with patch.object(pending.time, 'time', return_value=due):
            result = self.case.run_tick()
        self.assertEqual(result['bot_status'], 'ENTRY_PENDING')
        claims = self.case.claims()
        self.assertEqual((claims[0]['operation'], claims[0]['state']), (old['operation'], 'UNSENT'))
        self.assertEqual((claims[-1]['state'], claims[-1]['decision']), ('COMPLETE', 'TAHAN'))
        self.assertEqual(len(claims), 2)
        self.assertEqual(self.persisted_status()['bot_status'], 'ENTRY_PENDING')
        self.assertIsNone(self.persisted_status()['failure_code'])
        self.assertIsNone(self.persisted_status()['wait_reason'])
        self.assertEqual(self.coordinator.gateway.cancel_count, 0)


if __name__ == '__main__':
    unittest.main()
