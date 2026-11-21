"""A completed native screening must not strand a still-available entry slot.

The source-bound response is produced by the existing native recorder fixture.
All model, browser Send, upload and order boundaries are forbidden.
"""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent import native_pending_review as pending
from tests import test_generic_screening_response_recovery as fixtures
from tests.test_screening_response_recovery import native_module
from worker.account_state import screening_contract
from worker import screening_response_recovery as recovery
from worker.neuroapi import selections


class CompletedStaleScreeningResumeTests(unittest.TestCase):
    def setUp(self):
        case = self.case = fixtures.GenericScreeningResponseRecoveryTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        # Reuse the native request/receipt fixture, then model the production
        # epoch after one already-counted daily entry has closed.
        self.db = case.db
        self.db.execute('DELETE FROM robot_candidates')
        self.db.execute('DELETE FROM order_intents')
        self.cycle = fixtures.DAY + ':robot-v9:1'
        self.operation = self.cycle + ':screening:0:1'
        prompt, schema = screening_contract(1)
        case.context = None
        case.body, encoded, case.request_hash = case.source.request_body(
            self.operation, prompt, schema, None)
        case.payload = {**case.body, 'request_sha256': case.request_hash}
        self.data = dict(target=1, queue=[], seen=[], screen=-1,
            replacements=0, round_symbols=[])
        self.db.execute('UPDATE robot_cycles SET id=?,entry_epoch=1,data=?',
            (self.cycle, json.dumps(self.data)))
        self.db.execute('UPDATE robot_jobs SET operation=?,cycle=?',
            (self.operation, self.cycle))
        self.db.execute('UPDATE api_requests SET operation=?,body_hash=?',
            (self.operation, case.request_hash))
        self.db.execute('UPDATE research_browser_requests SET operation=?,request_sha256=?,marker=?,request_body=?',
            (self.operation, case.request_hash, case.body['marker'], encoded))
        envelope = case.fixture(operation=self.operation)
        from worker.screening_response_recovery import recover_received_screening
        self.assertEqual(recover_received_screening(case.provider, envelope,
            catalog=case.catalog, max_age=180, now=fixtures.NOW), 'RESUME_CACHED')
        self.primary_recovery = dict(self.db.execute(
            'SELECT * FROM screening_response_recoveries WHERE operation=?',
            (self.operation,)).fetchone())
        # Production's direct native completion has the same validated source
        # rows, without the fixture's additional uncertain-response seal.
        self.db.execute('DELETE FROM screening_response_recoveries')
        self.assertEqual(case.source.source_evidence(self.db, self.operation)['source_type'],
            case.source.BROWSER_SOURCE)
        self.native = native_module('worker._completed_stale_screening_fixture',
            fixtures.ROOT / 'vps_native/worker/robot.py')
        self.addCleanup(__import__('sys').modules.pop,
            'worker._completed_stale_screening_fixture', None)
        self.account = dict(running_positions=0, running_symbols=[],
            open_entry_symbols=[], available_slots=1, bot_entries_today=1)
        self.readiness_calls = 0
        self.robot = object.__new__(self.native.Coordinator)
        self.robot.db = self.db
        self.robot.stopping = None
        self.robot.account_factory = lambda: self.account
        self.robot.allowed = lambda day: day == fixtures.DAY and bool(
            self.db.execute('SELECT enabled FROM robot_settings').fetchone()[0])
        self.robot.execution_slots = lambda account, day: account['available_slots']
        self.robot.market = SimpleNamespace(catalog=lambda: case.catalog, max_age=180)
        self.robot.store = SimpleNamespace(
            settings=lambda: dict(robot_on=bool(self.db.execute(
                'SELECT enabled FROM robot_settings').fetchone()[0]), risk_target_usdt='5'),
            entries=lambda day: 1,
            results=lambda cycle: list(self.db.execute(
                'SELECT * FROM robot_candidates WHERE cycle=?', (cycle,))),
            report_account=lambda account: None,
            report=lambda state, account=None, reason=None, **kwargs:
                dict(bot_status=state, failure_code=reason, **kwargs))
        self.robot.neuro = case.provider
        case.provider.require_key = self.ready
        self.clock = fixtures.NOW + 181
        self.index = 0

    def ready(self):
        # Existing read-only readiness is an independent deterministic fixture;
        # every provider/model/order operation remains case.forbid.
        self.readiness_calls += 1

    def row(self, table):
        return dict(self.db.execute('SELECT * FROM ' + table + ' WHERE operation=?',
            (self.operation,)).fetchone())

    def source_rows(self):
        return {table: self.row(table) for table in (
            'api_requests', 'robot_jobs', 'research_browser_requests',
            'research_browser_responses')}

    def make_historical_stall(self):
        # Invoke the exact protected native screen method, rather than writing
        # an invented terminal state. Its cached-response age check is the
        # production transition being reproduced.
        with patch.object(self.native.time, 'time', return_value=self.clock):
            result = self.robot.screen(self.cycle, copy.deepcopy(self.data), 1,
                self.account, fixtures.DAY, self.index == 0)
        self.assertEqual(result['failure_code'], 'STALE_MARKET_CONTEXT')
        cycle = dict(self.db.execute('SELECT * FROM robot_cycles WHERE id=?',
            (self.cycle,)).fetchone())
        self.assertEqual(cycle['state'], 'COMPLETE')
        self.assertEqual(json.loads(cycle['data']), {**self.data, 'screen': self.index,
            'replacements': self.index, 'round_symbols': []})
        self.assertEqual(self.row('api_requests')['state'], 'COMPLETE')
        self.assertEqual(self.row('robot_jobs')['state'], 'COMPLETE')
        return self.source_rows()

    def run_tick(self):
        with patch.object(pending, 'review_then_advance', return_value=None), \
                patch.dict(self.native.__dict__, day=lambda: fixtures.DAY,
                    account_state=lambda client, store, day: self.account), \
                patch.object(self.native.time, 'time', return_value=self.clock):
            return self.robot._tick()

    def test_completed_stale_screening_is_retired_for_replacement_without_replay(self):
        source = self.make_historical_stall()
        result = self.run_tick()
        # RED on the current deployed source: it instead reports
        # INSUFFICIENT_ACTIONABLE_SETUPS / ROBOT_CYCLE_COMPLETE indefinitely.
        self.assertEqual(result['wait_reason'],
            'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')
        self.assertEqual(self.source_rows(), source)
        cycle = dict(self.db.execute('SELECT * FROM robot_cycles WHERE id=?',
            (self.cycle,)).fetchone())
        self.assertEqual(cycle['state'], 'ACTIVE')
        self.assertEqual(json.loads(cycle['data']),
            {**self.data, 'screen': 0, 'replacement_due': 1})
        self.assertEqual(self.db.execute('SELECT count(*) FROM robot_candidates').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM order_intents').fetchone()[0], 0)

    def local_reply(self, operation, prompt, schema, validate, context, *, catalog,
                    continue_if=None, reply='COIN: BCHUSDT'):
        """An explicitly injected local native reply, with no transport client."""
        if continue_if is not None:
            self.assertTrue(continue_if())
        self.case.catalog['BCHUSDT'] = {}
        body, encoded, request_hash = self.case.source.request_body(operation, prompt, schema, context)
        output = {'symbols': ['BCHUSDT']}
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            (operation, None, request_hash, 'PENDING', self.clock, 1, None, None))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',
            (operation, self.case.source.BROWSER_PROVIDER, self.case.source.BROWSER_CONTRACT,
                request_hash, body['marker'], encoded))
        result = {key: body[key] for key in ('operation', 'marker')}
        result.update(request_sha256=request_hash, output=output, assistant_text=reply,
            response_id='dom-' + recovery._sha('https://app.neurobro.ai/chat/' + 'a' * 32
                + '\n' + body['marker'] + '\n' + reply), chart_evidence=[])
        return self.case.provider._record_browser_response(operation, body, request_hash,
            result, schema, validate, context, catalog=catalog, received_at=self.clock)

    def next_receipt_without_apply(self, *, reply='COIN: BCHUSDT'):
        """Crash boundary after a real native recorder, before screen consumes it."""
        cycle = self.db.execute('SELECT * FROM robot_cycles WHERE id=?', (self.cycle,)).fetchone()
        data = json.loads(cycle['data']); self.index = data['screen'] + 1
        operation = self.cycle + ':screening:' + str(self.index) + ':1'
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
            (operation, self.cycle, 'SCREENING', None, 'PENDING', None))
        context = dict(requested_count=1, excluded_symbols=sorted(set(data['seen']) | {'HYPEUSDT'}),
            replacement_instruction='Choose different Binance USD-M USDT perpetual coins that are not in excluded_symbols.')
        _, schema = screening_contract(1)
        self.local_reply(operation, 'Pilih satu coin pengganti yang valid.', schema,
            lambda value: selections(value, self.case.catalog, 1), context,
            catalog=self.case.catalog, reply=reply)
        return operation

    def test_pending_completed_receipt_is_settled_before_cached_expiry_loses_work(self):
        source = self.source_rows()
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')
        self.assertEqual(self.row('robot_jobs'), {**source['robot_jobs'], 'state': 'COMPLETE'})
        for table in source.keys() - {'robot_jobs'}:
            self.assertEqual(self.row(table), source[table])

    def test_restart_revalidates_seal_then_native_tick_uses_only_a_new_operation(self):
        source = self.make_historical_stall()
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')
        with tempfile.TemporaryDirectory() as directory:
            reopened = sqlite3.connect(Path(directory) / 'ledger.sqlite', isolation_level=None)
            reopened.row_factory = sqlite3.Row
            self.db.backup(reopened)
            self.addCleanup(reopened.close)
            self.db = reopened; self.case.provider.db = reopened
            restarted = object.__new__(self.native.Coordinator)
            restarted.__dict__.update(self.robot.__dict__); restarted.db = reopened
            self.robot = restarted
            snapshot = tuple(self.db.iterdump())
            with patch.object(recovery.time, 'time', return_value=self.clock):
                self.assertIsNone(recovery._resume_completed_stale_screening(
                    self.robot, fixtures.DAY, self.account))
            self.assertEqual(tuple(self.db.iterdump()), snapshot)
            calls = []
            def local_only(*args, **kwargs):
                calls.append(args[0]); return self.local_reply(*args, **kwargs)
            self.case.provider.ask = local_only
            self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
            self.assertEqual(calls, [self.cycle + ':screening:1:1'])
            self.assertEqual(self.source_rows(), source)
            self.assertEqual(json.loads(self.db.execute('SELECT data FROM robot_cycles').fetchone()[0])['queue'], ['BCHUSDT'])

    def test_next_completed_stale_replacement_keeps_verified_predecessor_due(self):
        self.make_historical_stall()
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')
        original = self.source_rows()
        next_operation = self.next_receipt_without_apply()
        # Prefer this next unconsumed request over the old COMPLETE sealed one.
        self.clock += 181
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')
        self.assertEqual(self.source_rows(), original)
        self.assertEqual(self.db.execute('SELECT state FROM robot_jobs WHERE operation=?',
            (next_operation,)).fetchone()[0], 'COMPLETE')
        data = json.loads(self.db.execute('SELECT data FROM robot_cycles WHERE id=?', (self.cycle,)).fetchone()[0])
        self.assertEqual((data['screen'], data['replacements'], data['replacement_due']), (1, 1, 1))
        self.assertEqual(recovery.received_screening_replacement_due(self.db, self.cycle, data), 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM screening_response_recoveries').fetchone()[0], 2)

    def test_custom_initial_prompt_is_recovered_from_original_custody(self):
        request = self.row('research_browser_requests'); api = self.row('api_requests')
        body = json.loads(request['request_body'])
        prompt = body['prompt'] + '\nHindari ZECUSDT dan INJUSDT yang sudah HOLD/BATALKAN hari ini.'
        _, encoded, request_hash = self.case.source.request_body(self.operation, prompt,
            body['output_schema'], None)
        self.db.execute('UPDATE research_browser_requests SET request_body=?,request_sha256=?',
            (encoded, request_hash))
        self.db.execute('UPDATE api_requests SET body_hash=?', (request_hash,))
        # Regenerate the fixture's genuine Mini/source-bound custody under the
        # custom request, rather than authorizing a hash edit to old evidence.
        self.db.execute('DELETE FROM research_browser_responses')
        self.db.execute('DELETE FROM screening_interpretations')
        self.db.execute("UPDATE api_requests SET state='NEEDS_REVIEW',output=NULL,failure_code='NEUROBRO_BROWSER_OUTCOME_UNKNOWN'")
        self.case.body = json.loads(encoded); self.case.request_hash = request_hash
        self.case.payload = {**self.case.body, 'request_sha256': request_hash}
        envelope = self.case.fixture(operation=self.operation)
        self.assertEqual(recovery.recover_received_screening(self.case.provider, envelope,
            catalog=self.case.catalog, max_age=180, now=fixtures.NOW), 'RESUME_CACHED')
        self.db.execute('DELETE FROM screening_response_recoveries')
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')
        self.assertEqual(self.row('research_browser_requests')['request_body'], encoded)
        self.assertEqual(self.row('api_requests'), {**api, 'body_hash': request_hash})

    def test_normal_consumed_hold_does_not_become_a_recovery_error(self):
        self.db.execute("UPDATE robot_jobs SET state='COMPLETE'")
        data = {**self.data, 'screen': 0, 'seen': ['SUIUSDT'], 'round_symbols': ['SUIUSDT']}
        self.db.execute("UPDATE robot_cycles SET state='ACTIVE',data=?", (json.dumps(data),))
        self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
            (self.cycle + ':analysis-v9:SUIUSDT', self.cycle, 'SUIUSDT', 'HOLD', None, None))
        with patch.object(recovery.time, 'time', return_value=self.clock):
            self.assertIsNone(recovery._resume_completed_stale_screening(
                self.robot, fixtures.DAY, self.account))

    def test_rollback_after_cycle_write_failure_preserves_all_original_evidence(self):
        self.make_historical_stall(); before = tuple(self.db.iterdump())
        self.db.execute("CREATE TRIGGER no_cycle_update BEFORE UPDATE ON robot_cycles "
            "BEGIN SELECT RAISE(ABORT,'local interrupted fixture'); END")
        source = self.source_rows()
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_COMPLETED_STALE_RECOVERY_UNVERIFIED')
        self.assertEqual(self.source_rows(), source)
        self.db.execute('DROP TRIGGER no_cycle_update')
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')

    def test_tampered_source_and_unresolved_other_operation_never_authorize_retirement(self):
        self.make_historical_stall()
        source = self.source_rows()
        self.db.execute("UPDATE research_browser_responses SET assistant_text='foreign reply'")
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_COMPLETED_STALE_RECOVERY_UNVERIFIED')
        self.db.execute('UPDATE research_browser_responses SET assistant_text=?',
            (source['research_browser_responses']['assistant_text'],))
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            ('another-unknown', None, 'a' * 64, 'NEEDS_REVIEW', self.clock, 1, None, 'OUTCOME_UNKNOWN'))
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_COMPLETED_STALE_RECOVERY_UNVERIFIED')
        self.assertEqual(self.db.execute('SELECT state FROM api_requests WHERE operation=?',
            ('another-unknown',)).fetchone()[0], 'NEEDS_REVIEW')
        self.assertEqual(self.source_rows(), source)

    def test_off_capacity_and_new_day_remain_authoritative(self):
        self.make_historical_stall(); source = self.source_rows()
        self.db.execute('UPDATE robot_settings SET enabled=0')
        self.assertEqual(self.run_tick()['bot_status'], 'OFF')
        self.db.execute('UPDATE robot_settings SET enabled=1')
        self.account['available_slots'] = 0
        self.assertEqual(self.run_tick()['wait_reason'], 'ROBOT_CAPACITY_FULL')
        self.account['available_slots'] = 1
        with patch.object(recovery.time, 'time', return_value=self.clock):
            self.assertIsNone(recovery._resume_completed_stale_screening(
                self.robot, '2026-11-06', self.account))
        self.assertEqual(self.source_rows(), source)

    def test_historical_source_bound_market_hold_ends_without_debate_and_excludes_coin(self):
        reply = 'COIN PILIHAN:\nSUIUSDT Perpetual - HOLD, tunggu struktur dan momentum yang jelas.'
        self.db.execute('DELETE FROM research_browser_responses')
        self.db.execute('DELETE FROM screening_interpretations')
        self.db.execute("UPDATE api_requests SET state='PENDING',output=NULL")
        result = {key: self.case.body[key] for key in ('operation', 'marker')}
        result.update(request_sha256=self.case.request_hash, output={'symbols': ['SUIUSDT']},
            assistant_text=reply, chart_evidence=[], response_id='dom-' + recovery._sha(
                'https://app.neurobro.ai/chat/' + 'a' * 32 + '\n' + self.case.body['marker'] + '\n' + reply))
        self.case.provider._record_browser_response(self.operation, self.case.body,
            self.case.request_hash, result, self.case.body['output_schema'],
            lambda value: selections(value, self.case.catalog, 1), None,
            catalog=self.case.catalog, received_at=fixtures.NOW - 10)
        self.assertTrue(recovery.completed_screening_market_hold(self.db, self.operation))
        source = self.make_historical_stall()
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')
        self.assertEqual(self.source_rows(), source)
        data = json.loads(self.db.execute('SELECT data FROM robot_cycles').fetchone()[0])
        self.assertEqual(data['seen'], ['SUIUSDT'])
        seal = self.db.execute('SELECT proof FROM screening_response_recoveries').fetchone()[0]
        self.assertIs(json.loads(seal)['market_hold'], True)
        asks = []
        def new_coin(*args, **kwargs):
            asks.append((args[0], args[4])); return self.local_reply(*args, **kwargs)
        self.case.provider.ask = new_coin
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(len(asks), 1)
        self.assertIn('SUIUSDT', asks[0][1]['excluded_symbols'])
        self.assertEqual(self.source_rows(), source)

    def test_fresh_cached_receipt_advances_without_another_provider_call(self):
        self.clock = fixtures.NOW + 1
        before = self.source_rows()
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        for table in before.keys() - {'robot_jobs'}:
            self.assertEqual(self.row(table), before[table])
        data = json.loads(self.db.execute('SELECT data FROM robot_cycles').fetchone()[0])
        self.assertEqual(data['queue'], ['SUIUSDT'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM screening_response_recoveries').fetchone()[0], 0)

    def test_fresh_next_receipt_after_retirement_is_consumed_without_old_seal_blocking(self):
        self.make_historical_stall()
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')
        source = self.source_rows(); next_operation = self.next_receipt_without_apply()
        self.clock += 1
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(self.source_rows(), source)
        self.assertEqual(self.db.execute('SELECT state FROM robot_jobs WHERE operation=?',
            (next_operation,)).fetchone()[0], 'COMPLETE')
        data = json.loads(self.db.execute('SELECT data FROM robot_cycles').fetchone()[0])
        self.assertEqual((data['screen'], data['queue']), (1, ['BCHUSDT']))
        self.assertNotIn('replacement_due', data)

    def test_corrupted_retirement_seal_does_not_hide_as_cycle_complete(self):
        self.make_historical_stall()
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')
        self.db.execute('UPDATE screening_response_recoveries SET proof_sha256=?', ('0' * 64,))
        before = tuple(self.db.iterdump())
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_COMPLETED_STALE_RECOVERY_UNVERIFIED')
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_capacity_change_and_unknown_created_before_write_abort_retirement(self):
        self.make_historical_stall(); source = self.source_rows()
        slots = iter([1, 0])
        self.robot.execution_slots = lambda account, day: next(slots)
        with patch.object(recovery.time, 'time', return_value=self.clock):
            result = recovery._resume_completed_stale_screening(self.robot, fixtures.DAY, self.account)
        self.assertEqual(result['wait_reason'], 'SCREENING_COMPLETED_STALE_RECOVERY_UNVERIFIED')
        self.assertEqual(self.source_rows(), source)
        self.robot.execution_slots = lambda account, day: 1
        original_check = recovery._completed_stale_no_other_pending
        checks = []
        def interleave(db, operation):
            original_check(db, operation)
            if not checks:
                checks.append(operation)
                db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
                    ('new-unknown-before-transaction', None, 'b' * 64, 'NEEDS_REVIEW',
                        self.clock, 1, None, 'OUTCOME_UNKNOWN'))
        with patch.object(recovery, '_completed_stale_no_other_pending', side_effect=interleave), \
                patch.object(recovery.time, 'time', return_value=self.clock):
            result = recovery._resume_completed_stale_screening(self.robot, fixtures.DAY, self.account)
        self.assertEqual(result['wait_reason'], 'SCREENING_COMPLETED_STALE_RECOVERY_UNVERIFIED')
        self.assertEqual(self.source_rows(), source)
        self.assertEqual(self.db.execute('SELECT count(*) FROM screening_response_recoveries').fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT state FROM api_requests WHERE operation='new-unknown-before-transaction'").fetchone()[0], 'NEEDS_REVIEW')

    def test_existing_partial_fill_or_analysis_is_never_retired_as_empty_research(self):
        self.make_historical_stall()
        candidate = self.cycle + ':analysis-v9:SUIUSDT'
        self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
            (candidate, self.cycle, 'SUIUSDT', 'POSITION_PROTECTED', 'original accepted plan', None))
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?)',
            (candidate, candidate, 'SUIUSDT', 'POSITION_PROTECTED', 'original partial-filled payload'))
        before = tuple(self.db.iterdump())
        with patch.object(recovery.time, 'time', return_value=self.clock):
            self.assertIsNone(recovery._resume_completed_stale_screening(
                self.robot, fixtures.DAY, self.account))
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_legacy_nonbrowser_complete_response_is_not_admitted(self):
        self.make_historical_stall(); source = self.source_rows()
        self.db.execute("UPDATE research_browser_requests SET provider='LEGACY_STRUCTURED'")
        with patch.object(recovery.time, 'time', return_value=self.clock):
            self.assertIsNone(recovery._resume_completed_stale_screening(
                self.robot, fixtures.DAY, self.account))
        self.assertEqual(self.row('api_requests'), source['api_requests'])
        self.db.execute('DROP TABLE research_browser_requests')
        self.db.execute('DROP TABLE research_browser_responses')
        with patch.object(recovery.time, 'time', return_value=self.clock):
            self.assertIsNone(recovery._resume_completed_stale_screening(
                self.robot, fixtures.DAY, self.account))

    def retain_original_resume_cached(self):
        self.db.execute('INSERT INTO screening_response_recoveries VALUES(?,?,?,?,?,?,?,?)',
            tuple(self.primary_recovery[name] for name in ('operation', 'request_sha256',
                'proof_sha256', 'proof', 'preimage', 'phase', 'created', 'updated')))

    def test_existing_resume_cached_is_preserved_with_separate_audit_then_restart_and_new_request(self):
        self.retain_original_resume_cached()
        source = self.make_historical_stall()
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')
        self.assertEqual(self.source_rows(), source)
        primary = dict(self.db.execute('SELECT * FROM screening_response_recoveries WHERE operation=?',
            (self.operation,)).fetchone())
        self.assertEqual(primary, self.primary_recovery)
        audit = dict(self.db.execute('SELECT * FROM screening_response_recoveries WHERE operation=?',
            (self.operation + recovery.COMPLETED_STALE_AUDIT_SUFFIX,)).fetchone())
        proof = json.loads(audit['proof'])
        self.assertEqual(proof['operation'], self.operation)
        self.assertEqual(proof['primary_recovery_sha256'], recovery._sha(recovery._wire(primary)))
        self.assertEqual(self.db.execute('SELECT count(*) FROM screening_response_recoveries').fetchone()[0], 2)
        with tempfile.TemporaryDirectory() as directory:
            reopened = sqlite3.connect(Path(directory) / 'restart.sqlite', isolation_level=None)
            reopened.row_factory = sqlite3.Row
            self.db.backup(reopened); self.addCleanup(reopened.close)
            self.db = reopened; self.case.provider.db = reopened
            restarted = object.__new__(self.native.Coordinator)
            restarted.__dict__.update(self.robot.__dict__); restarted.db = reopened
            self.robot = restarted
            data = json.loads(reopened.execute('SELECT data FROM robot_cycles').fetchone()[0])
            self.assertEqual(recovery.received_screening_replacement_due(reopened, self.cycle, data), 1)
            snapshot = tuple(reopened.iterdump())
            with patch.object(recovery.time, 'time', return_value=self.clock):
                self.assertIsNone(recovery._resume_completed_stale_screening(
                    self.robot, fixtures.DAY, self.account))
            self.assertEqual(tuple(reopened.iterdump()), snapshot)
            calls = []
            def new_only(*args, **kwargs):
                calls.append(args[0]); return self.local_reply(*args, **kwargs)
            self.case.provider.ask = new_only
            self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
            self.assertEqual(calls, [self.cycle + ':screening:1:1'])
            self.assertEqual(self.source_rows(), source)
            self.assertEqual(dict(reopened.execute('SELECT * FROM screening_response_recoveries '
                'WHERE operation=?', (self.operation,)).fetchone()), primary)

    def test_pending_cached_recovery_settles_once_without_overwriting_primary(self):
        self.retain_original_resume_cached(); source = self.source_rows()
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')
        self.assertEqual(dict(self.db.execute('SELECT * FROM screening_response_recoveries '
            'WHERE operation=?', (self.operation,)).fetchone()), self.primary_recovery)
        self.assertEqual(self.row('robot_jobs'), {**source['robot_jobs'], 'state': 'COMPLETE'})
        self.assertEqual(self.row('api_requests'), source['api_requests'])

    def test_prior_recovery_corruption_or_prepared_state_cannot_authorize_an_audit(self):
        self.retain_original_resume_cached(); self.make_historical_stall()
        for field, value in (('phase', 'PREPARED'), ('proof_sha256', '0' * 64),
                ('request_sha256', '0' * 64)):
            with self.subTest(field=field):
                self.db.execute('UPDATE screening_response_recoveries SET ' + field + '=? WHERE operation=?',
                    (value, self.operation))
                snapshot = tuple(self.db.iterdump())
                self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_COMPLETED_STALE_RECOVERY_UNVERIFIED')
                self.assertEqual(tuple(self.db.iterdump()), snapshot)
                self.db.execute('UPDATE screening_response_recoveries SET ' + field + '=? WHERE operation=?',
                    (self.primary_recovery[field], self.operation))

    def test_malformed_audit_cannot_fall_back_to_valid_original_cached_recovery(self):
        self.retain_original_resume_cached(); self.make_historical_stall()
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')
        audit_key = self.operation + recovery.COMPLETED_STALE_AUDIT_SUFFIX
        self.db.execute('UPDATE screening_response_recoveries SET proof_sha256=? WHERE operation=?',
            ('0' * 64, audit_key))
        snapshot = tuple(self.db.iterdump())
        self.assertEqual(self.run_tick()['wait_reason'], 'SCREENING_COMPLETED_STALE_RECOVERY_UNVERIFIED')
        self.assertEqual(tuple(self.db.iterdump()), snapshot)


if __name__ == '__main__':
    unittest.main(verbosity=2)
