"""Reproduce the mixed-day state reported by runtime run 44928069090.

The identities, times and selected symbols mirror the runtime projection;
source-bound reply receipts are offline fixtures, never production claims.
"""
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import test_generic_screening_response_recovery as fixture
from worker import screening_response_recovery as recovery
from worker.account_state import screening_contract
from worker.core import Review


DAY = '2026-11-22'
CYCLE = DAY + ':robot-v9:0'
OP = CYCLE + ':screening:0:2'
CREATED = 1795280511.069296
RECEIVED = 1795280569.9658527
NOW = RECEIVED + 10
OLD_DAY = '2026-11-21'
OLD_CYCLE = OLD_DAY + ':robot-v9:1'
OLD_OP = OLD_CYCLE + ':screening:6:1'


class ClassifiedScreeningRolloverTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing native recorder and original-reply validators.
        clock = patch.multiple(fixture, DAY=DAY, CYCLE=CYCLE, OP=OP, NOW=NOW)
        clock.start(); self.addCleanup(clock.stop)
        c = self.case = fixture.GenericScreeningResponseRecoveryTests()
        c.setUp(); self.addCleanup(c.doCleanups)
        self.db = c.db
        self.assertEqual(c.two_coin_contract(), OP)
        c.created = CREATED
        c.db.execute('UPDATE api_requests SET created=? WHERE operation=?', (CREATED, OP))
        c.catalog.update(ENAUSDT={}, OPUSDT={})
        envelope = c.fixture(operation=OP, symbols=['ENAUSDT', 'OPUSDT'],
            reply='Pilihan final: ENAUSDT dan OPUSDT.\n{"SYMBOLS":["ENAUSDT","OPUSDT"]}')
        self.assertEqual(recovery.recover_received_screening(c.provider, envelope,
            catalog=c.catalog, max_age=180, now=NOW), 'RESUME_CACHED')
        self.assertEqual(self.row('research_browser_responses', OP)['received_at'], RECEIVED)
        self.assertEqual(self.row('robot_jobs', OP)['state'], 'PENDING')
        self.assertEqual(self.row('api_requests', OP)['state'], 'COMPLETE')
        # The prior day's typed provider-failure closure has no fabricated
        # NOEXEC seal. Its original cycle still awaits optional audit sealing.
        _, schema = screening_contract(1)
        context = dict(requested_count=1, excluded_symbols=['INJUSDT'],
            replacement_instruction='Choose different Binance USD-M USDT perpetual coins that are not in excluded_symbols.')
        body, encoded, request_hash = c.source.request_body(OLD_OP, 'Pilih satu coin pengganti.', schema, context)
        self.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)', (OLD_CYCLE, OLD_DAY, 1, 'NEEDS_REVIEW',
            json.dumps(dict(target=2, queue=[], seen=['INJUSDT'], screen=5, replacements=5, round_symbols=[]))))
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
            (OLD_OP, OLD_CYCLE, 'SCREENING', None, 'REQUEST_REJECTED', None))
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            (OLD_OP, None, request_hash, 'REJECTED_PROVIDER_ERROR', 1795274684.6960785,
                1, None, 'NEUROBRO_WEB_PROCESSING_FAILED'))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',
            (OLD_OP, c.source.BROWSER_PROVIDER, c.source.BROWSER_CONTRACT, request_hash, body['marker'], encoded))
        # Existing protected/historical fixtures belong to the old day. They
        # must not manufacture a current-day filled entry or a third slot.
        self.db.execute('UPDATE robot_candidates SET cycle=?', (OLD_CYCLE,))
        self.native = fixture.native_module('worker._classified_error_rollover_native',
            fixture.ROOT / 'vps_native/worker/robot.py')
        self.robot = object.__new__(self.native.Coordinator)
        self.robot.db = self.db; self.robot.neuro = c.provider
        self.robot.stopping = None; self.robot.account_factory = lambda: None
        self.robot.allowed = lambda day: self.row('robot_settings', 1, key='id')['enabled'] == 1
        self.robot.pause_reason = lambda: 'ROBOT_OFF'
        self.robot.execution_slots = lambda account, day: 2
        self.robot.exposed_symbols = lambda account: set()
        self.robot.market = SimpleNamespace(max_age=180, catalog=lambda: c.catalog)
        self.robot.store = SimpleNamespace(
            settings=lambda: dict(robot_on=self.robot.allowed(DAY)),
            entries=lambda day: 0,
            results=lambda cycle: list(self.db.execute('SELECT * FROM robot_candidates WHERE cycle=?', (cycle,))),
            report_account=lambda account: None,
            report=lambda status, account=None, reason=None, **fields: dict(bot_status=status, reason=reason, **fields))
        c.provider.require_key = Mock(return_value=None)  # Existing configuration-only read.
        c.provider.observe_received_screening = Mock(side_effect=Review('SCREENING_RESPONSE_RECOVERY_UNVERIFIED'))
        c.provider.review_received_screening = c.forbid
        c.provider.ask = c.provider.transport = c.forbid
        self.analysis_handoffs = []
        self.robot.analyze = lambda cycle, data, symbol, account, day: self.analysis_handoffs.append(
            (cycle, symbol, day)) or self.robot.store.report('WAITING', wait_reason='OFFLINE_ANALYSIS_BOUNDARY')

    def row(self, table, value, *, key='operation'):
        return recovery._row(self.db, table, key, value)

    def historical(self):
        return dict(api=self.row('api_requests', OLD_OP), job=self.row('robot_jobs', OLD_OP),
            request=self.row('research_browser_requests', OLD_OP), response=self.row('research_browser_responses', OLD_OP),
            cycle=self.row('robot_cycles', OLD_CYCLE, key='id'),
            journal=self.row('screening_response_recoveries', OLD_OP))

    def tick(self, *, now=NOW):
        with patch.object(self.native, 'day', return_value=DAY), \
                patch.object(self.native, 'account_state', return_value={}), \
                patch.object(recovery.time, 'time', return_value=now), \
                patch('agent.native_pending_review.review_then_advance', return_value=None):
            return self.robot._tick()

    def test_previous_day_typed_failure_does_not_observe_old_room_or_starve_fresh_cached_screening(self):
        self.assertEqual(datetime.fromtimestamp(NOW, timezone.utc).date().isoformat(), OLD_DAY)
        history = self.historical(); finances = self.case.finances()
        request = self.row('research_browser_requests', OP); receipt = self.row('research_browser_responses', OP)
        journal = self.row('screening_response_recoveries', OP)
        with patch.object(recovery.time, 'time', return_value=NOW):
            result = recovery.resume_uncertain_screening(self.robot, DAY, {})
        self.assertIsNone(result)
        self.case.provider.observe_received_screening.assert_not_called()
        result = self.tick()
        self.assertEqual(result['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        data = json.loads(self.row('robot_cycles', CYCLE, key='id')['data'])
        self.assertEqual((data['screen'], data['queue'], data['round_symbols']),
            (0, ['ENAUSDT', 'OPUSDT'], ['ENAUSDT', 'OPUSDT']))
        self.assertEqual(self.row('robot_jobs', OP)['state'], 'COMPLETE')
        self.assertEqual(self.row('research_browser_requests', OP), request)
        self.assertEqual(self.row('research_browser_responses', OP), receipt)
        self.assertEqual(self.row('screening_response_recoveries', OP), journal)
        self.assertEqual(self.historical(), history); self.assertEqual(self.case.finances(), finances)
        self.assertEqual(self.tick()['wait_reason'], 'OFFLINE_ANALYSIS_BOUNDARY')
        self.assertEqual(self.analysis_handoffs, [(CYCLE, 'ENAUSDT', DAY)])
        self.case.provider.observe_received_screening.assert_not_called()

    def test_restart_reuses_same_valid_receipt_and_only_consumes_current_screening_once(self):
        restarted = sqlite3.connect(':memory:', isolation_level=None); restarted.row_factory = sqlite3.Row
        self.addCleanup(restarted.close); self.db.backup(restarted)
        self.db = self.case.db = self.case.provider.db = self.robot.db = restarted
        before = self.historical(); journal = self.row('screening_response_recoveries', OP)
        self.assertEqual(self.tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(self.tick()['wait_reason'], 'OFFLINE_ANALYSIS_BOUNDARY')
        self.assertEqual(self.row('screening_response_recoveries', OP), journal)
        self.assertEqual(self.row('api_requests', OP)['attempts'], 1)
        self.assertEqual(self.historical(), before)
        self.case.provider.observe_received_screening.assert_not_called()

    def test_old_true_unknown_is_still_reconciled_and_never_hidden_by_current_day_filter(self):
        for state, job_state, code in (('NEEDS_REVIEW', 'NEEDS_REVIEW', 'NEUROBRO_BROWSER_OUTCOME_UNKNOWN'),
                ('NEEDS_REVIEW', 'NEEDS_REVIEW', 'LOCAL_PROCESSING_FAILED'), ('PENDING', 'PENDING', None)):
            with self.subTest(state=state, code=code):
                self.db.execute('UPDATE api_requests SET state=?,failure_code=? WHERE operation=?', (state, code, OLD_OP))
                self.db.execute('UPDATE robot_jobs SET state=? WHERE operation=?', (job_state, OLD_OP))
                before = tuple(self.db.iterdump()); self.case.provider.observe_received_screening.reset_mock()
                result = self.tick()
                self.assertEqual(result['wait_reason'], 'SCREENING_RESPONSE_RECOVERY_UNVERIFIED')
                self.case.provider.observe_received_screening.assert_called_once_with(OLD_OP)
                self.assertEqual(tuple(self.db.iterdump()), before)
        self.assertEqual(self.row('robot_jobs', OP)['state'], 'PENDING')
        self.assertEqual(self.analysis_handoffs, [])

    def test_stale_current_day_cache_uses_existing_audited_retirement_without_replaying_either_request(self):
        history = self.historical(); finances = self.case.finances()
        request = self.row('research_browser_requests', OP); receipt = self.row('research_browser_responses', OP)
        api = self.row('api_requests', OP); primary = self.row('screening_response_recoveries', OP)
        stale_now = CREATED + self.robot.market.max_age + 1
        self.assertEqual(self.tick(now=stale_now)['wait_reason'], 'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')
        self.assertEqual(self.row('api_requests', OP), api)
        self.assertEqual(self.row('research_browser_requests', OP), request)
        self.assertEqual(self.row('research_browser_responses', OP), receipt)
        self.assertEqual(self.row('screening_response_recoveries', OP), primary)
        audit = self.row('screening_response_recoveries', OP + recovery.COMPLETED_STALE_AUDIT_SUFFIX)
        self.assertEqual(audit['phase'], 'NOEXEC')
        self.assertEqual(json.loads(audit['proof'])['kind'], recovery.COMPLETED_STALE_NOEXEC)
        self.assertEqual(self.row('robot_jobs', OP)['state'], 'COMPLETE')
        data = json.loads(self.row('robot_cycles', CYCLE, key='id')['data'])
        self.assertEqual((data['screen'], data['queue'], data['round_symbols'], data['replacement_due']), (0, [], [], 2))
        self.assertEqual(self.historical(), history); self.assertEqual(self.case.finances(), finances)
        self.assertEqual(self.analysis_handoffs, [])
        before = tuple(self.db.iterdump())
        with patch.object(recovery.time, 'time', return_value=stale_now + 1):
            self.assertIsNone(recovery.resume_uncertain_screening(self.robot, DAY, {}))
            self.assertEqual(self.robot.replacement_count(data, [], CYCLE), 2)
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.case.provider.observe_received_screening.assert_not_called()

    def test_owner_off_does_not_consume_receipt_or_advance_historical_audit(self):
        self.db.execute('UPDATE robot_settings SET enabled=0')
        before = tuple(self.db.iterdump())
        self.assertEqual(self.tick()['wait_reason'], 'ROBOT_OFF')
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.case.provider.observe_received_screening.assert_not_called()


if __name__ == '__main__': unittest.main()
