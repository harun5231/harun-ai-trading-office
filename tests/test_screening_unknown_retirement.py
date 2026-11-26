"""Source-bound retirement of screening delivery with an unknown outcome."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent import native_received_analysis as observer
from agent import native_received_screening as browser
from agent.native_daily_room import identities
from worker import screening_response_recovery as recovery
from worker.account_state import screening_contract
from worker.core import Review
from tests.test_screening_response_recovery import native_module


ROOT = Path(__file__).resolve().parents[1]
DAY = '2026-11-26'
NEXT_DAY = '2026-11-27'
CYCLE = DAY + ':robot-v9:0'
OPERATION = CYCLE + ':screening:1:2'
CREATED = datetime(2026, 11, 26, 15, 7, 29, tzinfo=timezone.utc).timestamp()
NOW = CREATED + 1500


class ScreeningUnknownRetirementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'ledger.sqlite'
        self.open_db()
        modules = ('worker.research_provider', 'worker.neurobrowser')
        old = {name: sys.modules.get(name) for name in modules}

        def restore_modules():
            for name, previous in old.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

        self.addCleanup(restore_modules)
        with patch('worker.owner_source_protocol.load_owner_release_cutoff', return_value='2026-11-04T00:00:00Z'):
            self.source = native_module(modules[0], ROOT / 'vps_native/worker/research_provider.py')
        self.native = native_module(modules[1], ROOT / 'vps_native/worker/neurobrowser.py')
        self.source.ensure_journal(self.db)
        self.db.executescript('''
            CREATE TABLE robot_cycles(id TEXT PRIMARY KEY,day TEXT,entry_epoch INTEGER,state TEXT,data TEXT);
            CREATE TABLE robot_jobs(operation TEXT PRIMARY KEY,cycle TEXT,kind TEXT,symbol TEXT,state TEXT,risk_target TEXT);
            CREATE TABLE robot_settings(id INTEGER PRIMARY KEY,enabled INTEGER,risk TEXT);
            INSERT INTO robot_settings VALUES(1,1,'5.17');
            CREATE TABLE robot_candidates(id TEXT PRIMARY KEY,cycle TEXT,symbol TEXT,status TEXT,plan TEXT,failure_code TEXT);
            CREATE TABLE order_intents(id TEXT PRIMARY KEY,candidate_id TEXT,symbol TEXT,state TEXT,payload TEXT);
            INSERT INTO order_intents VALUES('old-protected','other','SOLUSDT','POSITION_PROTECTED','original payload');
            CREATE TABLE pending_reviews(id TEXT PRIMARY KEY,action TEXT,state TEXT);
            INSERT INTO pending_reviews VALUES('preserve-review','TAHAN','COMPLETE');
        ''')
        self.data = dict(screen=0, seen=['WIFUSDT', 'SANDUSDT'], queue=[], round_symbols=['WIFUSDT', 'SANDUSDT'],
            replacements=0, target=2)
        self.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',
            (CYCLE, DAY, 0, 'NEEDS_REVIEW', json.dumps(self.data)))
        for symbol in self.data['seen']:
            older = CYCLE + ':analysis-v9:' + symbol
            self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)', (older, CYCLE, 'ANALYSIS', symbol, 'COMPLETE', '5.17'))
            self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)', (older, CYCLE, symbol, 'HOLD', None, None))
        self.context = dict(requested_count=2, excluded_symbols=sorted(self.data['seen']),
            replacement_instruction='Historis: jangan pilih lagi simbol yang dikecualikan.')
        _, schema = screening_contract(2)
        self.body, serialized, self.request_hash = self.source.request_body(OPERATION,
            'Prompt pengganti historis yang tidak dirender ulang.', schema, self.context)
        self.payload = {**self.body, 'request_sha256': self.request_hash}
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)', (OPERATION, CYCLE, 'SCREENING', None, 'NEEDS_REVIEW', None))
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            (OPERATION, None, self.request_hash, 'NEEDS_REVIEW', CREATED, 1, None, recovery.UNKNOWN_REASON))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',
            (OPERATION, self.source.BROWSER_PROVIDER, self.source.BROWSER_CONTRACT,
                self.request_hash, self.body['marker'], serialized))
        self.open_provider()

    def open_db(self):
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)

    def open_provider(self):
        self.provider = object.__new__(self.native.NeuroBrowser)
        self.provider.db = self.db
        self.provider.chart_max_age = 180
        self.provider.transport = self.forbid
        self.provider.ask = self.forbid

    def forbid(self, *_args, **_kwargs):
        self.fail('No Send, model, order or market request permitted on this path')

    def custody(self):
        return {name: [dict(row) for row in self.db.execute('SELECT * FROM ' + name)] for name in (
            'research_browser_requests', 'research_browser_responses', 'robot_candidates',
            'order_intents', 'robot_settings', 'pending_reviews')}

    def row(self, table, key='operation', value=OPERATION):
        return recovery._row(self.db, table, key, value)

    @staticmethod
    def seal(envelope):
        raw = recovery._wire(envelope['observation'])
        envelope.update(observation_sha256=recovery._sha(raw), observation_bytes=len(raw.encode()))
        return envelope

    def envelope(self, now=NOW, *, unbound=False):
        operation = self.body['operation']
        url = 'https://app.neurobro.ai/chat/' + 'c' * 32
        rows = [dict(role='user', text='Diskusi chart SAND sebelumnya.', attachments=[]),
            dict(role='assistant', text='HOLD SANDUSDT', complete=True, id='observed-sand-id')]
        rendered = self.body['prompt'] + '\nAcuan: ' + self.body['marker']
        cached = dict(operation=operation, payload_sha256=observer.native_digest(self.payload),
            rendered_prompt=rendered, rendered_sha256=observer.text_sha(rendered))
        room = dict(day=DAY, room_id='b'*32, url=url, anchors=identities(rows), marker='HARUN_' + 'a'*32,
            first_prompt_sha256=observer.text_sha(rows[0]['text']), history_titles=[], identifier_reconciliation=None)
        if unbound:
            room.update(anchors=[], url='https://app.neurobro.ai/chat', marker=self.body['marker'],
                first_prompt_sha256=observer.text_sha(rendered))
        witness = observer._room_witness(room, rows, SimpleNamespace(page=SimpleNamespace(url=url)),
            self.root, cached, {}, payload=self.payload, generic=True)
        witness['initial_screening_user_rows'] = witness.pop('initial_analysis_user_rows')
        scope = browser.operation_scope(rows, witness, [], self.payload)
        search = browser.history_target(rows, room, cached, self.payload, self.root, url, 'SENT')
        self.assertIs(search.pop('target'), None if unbound else False)
        search.update(kind=browser.HISTORY_SEARCH_KIND, operation=operation, request_sha256=self.request_hash,
            day=DAY, room_id=room['room_id'], rendered_prompt_sha256=cached['rendered_sha256'],
            started_at=now-8, finished_at=now-2, outcome='UNBOUND_INITIAL_EXHAUSTED' if unbound else 'TARGET_ABSENT',
            history_reselected=not unbound,
            actor_owned=True, stage='SCREENING')
        capture = dict(schema_version=2, kind=recovery.RECEIVED_KIND, operation=operation,
            request_sha256=self.request_hash, payload_sha256=observer.native_digest(self.payload),
            native_source_sha256=recovery.RECEIVED_NATIVE_SOURCE, operation_completed=False,
            observed_at=now-9, finished_at=now-1, status='CONNECTED', stage='OBSERVED_WITHOUT_COMPLETION',
            session_observation={'reason': 'CONNECTED'}, errors=[], rows=rows, room=room, page_url=url,
            room_initial_state='SENT', room_witness=witness, rendered_prompt_receipt=cached,
            operation_scope=scope, screening_history_search=search, history_restore_attempted=True,
            history_restored=not unbound, stable_snapshot_reads=4, screening_ended_return=None,
            orders_sent=0, model_calls=0, provider_messages=0, uploads=0,
            review_receipts=[], receipt_files={}, prepared_materials=[], file_tiles=[])
        raw = observer.canonical(capture)
        digest = observer.text_sha(raw.decode())
        key = observer.digest({key: capture[key] for key in ('operation', 'request_sha256')})[:16]
        return self.seal(dict(kind=recovery.RECEIVED_KIND, operation=operation, request_sha256=self.request_hash,
            operation_completed=False, observation=observer._project_operation(capture),
            capture_name=f'received-analysis-observation-{key}-{digest}.json', capture_sha256=digest,
            capture_bytes=len(raw)))

    def test_real_projection_retires_unknown_then_restart_rejects_late_reply(self):
        original = self.custody(); envelope = self.envelope()
        self.assertEqual(recovery.recover_received_screening(self.provider, envelope, catalog={}, max_age=180, now=NOW), 'NOEXEC')
        self.assertEqual(self.custody(), original)
        api = self.row('api_requests'); job = self.row('robot_jobs'); audit = self.row('screening_response_recoveries')
        self.assertEqual((api['state'], api['failure_code'], api['attempts'], api['created'], api['output']),
            (recovery.UNKNOWN_TERMINAL, recovery.UNKNOWN_REASON, 1, CREATED, None))
        self.assertEqual(job['state'], 'REQUEST_REJECTED')
        self.assertEqual(json.loads(audit['proof'])['retirement']['delivery_outcome'], 'UNKNOWN')
        self.assertEqual(json.loads(audit['preimage'])['api_requests']['state'], 'NEEDS_REVIEW')
        data = json.loads(self.row('robot_cycles', 'id', CYCLE)['data'])
        self.assertEqual(data, {**self.data, 'screen': 1, 'replacements': 1, 'round_symbols': [], 'replacement_due': 2})
        with patch.object(recovery.time, 'time', return_value=NOW+30):
            self.assertEqual(recovery.received_screening_replacement_due(self.db, CYCLE, data), 0)
        self.db.close(); self.open_db(); self.open_provider()
        with patch.object(recovery.time, 'time', return_value=NOW+61):
            self.assertEqual(recovery.received_screening_replacement_due(self.db, CYCLE, data), 2)
        late = dict(envelope, observation={'rows': [{'role': 'assistant', 'text': 'late'}]})
        self.assertEqual(recovery.recover_received_screening(self.provider, late, catalog={}, max_age=180, now=NOW+120), 'NOEXEC')
        catalog = {'SUIUSDT': {}, 'AXSUSDT': {}}
        response = dict(operation=OPERATION, request_sha256=self.request_hash, marker=self.body['marker'],
            output={'symbols': list(catalog)}, response_id='late-reply', chart_evidence=[],
            assistant_text='{"symbols":["SUIUSDT","AXSUSDT"]}')
        for recovery_flag in (False, True):
            with self.assertRaises(Review):
                self.provider._record_browser_response(OPERATION, self.body, self.request_hash, response,
                    self.body['output_schema'], self.forbid, self.context, catalog=catalog,
                    received_at=NOW+120, recovery=recovery_flag)
        self.assertEqual(self.row('screening_response_recoveries'), audit)
        self.assertEqual(self.row('api_requests'), api)
        self.assertEqual(self.custody(), original)

    def test_wrong_incomplete_duplicate_or_unowned_proof_never_retires(self):
        envelope = self.envelope(); original = recovery._snapshot(self.db, OPERATION, CYCLE)
        for key, value in {
            'actor_owned': False, 'history_reselected': False, 'strict_prefix_verified': False,
            'journal_url_matches': False, 'rows_count': 1, 'target_matches': 2,
            'marker_mentions': 1, 'rendered_prompt_sha256': 'f'*64, 'request_sha256': 'f'*64,
        }.items():
            with self.subTest(key=key):
                damaged = copy.deepcopy(envelope)
                damaged['observation']['screening_history_search'][key] = value
                with self.assertRaises(Review):
                    recovery.recover_received_screening(self.provider, self.seal(damaged), catalog={}, max_age=180, now=NOW)
        for field, value in (('status', 'CHALLENGE_REQUIRED'), ('review_receipts', [{}]),
                ('receipt_files', {'prior': {}}), ('stable_snapshot_reads', 3)):
            with self.subTest(field=field):
                damaged = copy.deepcopy(envelope); damaged['observation'][field] = value
                with self.assertRaises(Review):
                    recovery.recover_received_screening(self.provider, self.seal(damaged), catalog={}, max_age=180, now=NOW)
        self.assertEqual(recovery._snapshot(self.db, OPERATION, CYCLE), original)
        self.assertIsNone(self.row('screening_response_recoveries'))

    def test_window_remains_open_until_after_historical_dispatch_expiry(self):
        within = CREATED + 930
        with self.assertRaisesRegex(Review, 'SCREENING_UNKNOWN_DISPATCH_WINDOW'):
            recovery.recover_received_screening(self.provider, self.envelope(within), catalog={}, max_age=180, now=within)
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')

    def test_existing_linked_intent_rejects_retirement_without_any_order_write(self):
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?)', ('existing',
            CYCLE + ':analysis-v9:SANDUSDT', 'SANDUSDT', 'NEEDS_REVIEW', 'original order identity'))
        original = self.custody()
        with self.assertRaises(Review):
            recovery.recover_received_screening(self.provider, self.envelope(), catalog={}, max_age=180, now=NOW)
        self.assertEqual(self.custody(), original)
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')
        self.assertIsNone(self.row('screening_response_recoveries'))

    def test_original_ended_return_is_retained_with_unknown_and_write_is_atomic(self):
        envelope = self.envelope()
        failure = dict(schema_version=1, operation=OPERATION, request_sha256=self.request_hash,
            phase='INITIAL', accepted_reply_sha256=None, followup_intent_sha256=[],
            clock=901.1, deadline=900, at=CREATED+901, chain=[])
        raw = recovery._wire(failure, ascii=True)
        entry = dict(kind=recovery.ENDED_RETURN_KIND,
            producer_source_sha256=recovery.ENDED_RETURN_PRODUCER,
            hook_source_sha256=recovery.TERMINAL_PROVIDER_ERROR_HOOK,
            name='native-return-failure-' + self.request_hash + '.json', value=failure,
            sha256=recovery._sha(raw), bytes=len(raw.encode()))
        envelope['observation']['screening_ended_return'] = entry
        self.seal(envelope)
        original = recovery._snapshot(self.db, OPERATION, CYCLE)
        allowed = Mock(side_effect=[True, False])
        with self.assertRaisesRegex(Review, 'RESEARCH_PAUSED'):
            recovery.recover_received_screening(self.provider, envelope, catalog={}, max_age=180, now=NOW, permitted=allowed)
        self.assertEqual(recovery._snapshot(self.db, OPERATION, CYCLE), original)
        self.assertIsNone(self.row('screening_response_recoveries'))
        self.assertEqual(recovery.recover_received_screening(self.provider, envelope, catalog={}, max_age=180, now=NOW), 'NOEXEC')
        sealed = json.loads(self.row('screening_response_recoveries')['proof'])
        self.assertEqual(sealed['observation']['screening_ended_return'], entry)
        self.assertEqual(sealed['retirement']['delivery_outcome'], 'UNKNOWN')

    def test_existing_receipt_or_later_job_refuses_negative_transition(self):
        envelope = self.envelope()
        self.db.execute('INSERT INTO research_browser_responses VALUES(?,?,?,?,?,?,?,?)',
            (OPERATION, 'already-recorded', 'b'*64, 'actual reply', 'c'*64, '[]', NOW-10, 180))
        original = self.custody()
        with self.assertRaises(Review):
            recovery.recover_received_screening(self.provider, envelope, catalog={}, max_age=180, now=NOW)
        self.assertEqual(self.custody(), original)
        self.db.execute('DELETE FROM research_browser_responses WHERE operation=?', (OPERATION,))
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
            (CYCLE + ':analysis-v9:AXSUSDT', CYCLE, 'ANALYSIS', 'AXSUSDT', 'PENDING', '5.17'))
        with self.assertRaises(Review):
            recovery.recover_received_screening(self.provider, envelope, catalog={}, max_age=180, now=NOW)
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')

    def test_crash_pending_and_old_wib_day_never_resume_old_cycle(self):
        next_day = datetime(2026, 11, 26, 17, 1, tzinfo=timezone.utc).timestamp()
        self.db.execute("UPDATE api_requests SET state='PENDING',failure_code=NULL WHERE operation=?", (OPERATION,))
        self.db.execute("UPDATE robot_jobs SET state='PENDING' WHERE operation=?", (OPERATION,))
        with patch.object(recovery.time, 'time', return_value=next_day):
            self.assertEqual(recovery.recover_received_screening(self.provider, self.envelope(next_day),
                catalog={}, max_age=180), 'NOEXEC')
        cycle = self.row('robot_cycles', 'id', CYCLE)
        self.assertEqual(cycle['state'], 'COMPLETE')
        self.assertNotIn('replacement_due', json.loads(cycle['data']))
        self.assertFalse(self.native.verify_dispatch(self.db, OPERATION, self.body['marker'], self.request_hash,
            NEXT_DAY, next_day))
        self.assertIsNone(self.row('robot_cycles', 'id', NEXT_DAY + ':robot-v9:0'))

    def test_coordinator_neither_reads_market_nor_reinterprets_incomplete_or_absent(self):
        absent = self.envelope(); incomplete = copy.deepcopy(absent)
        incomplete['observation']['screening_history_search']['outcome'] = 'TARGET_OBSERVED'
        incomplete['observation']['operation_scope']['reason'] = 'SCREENING_REPLY_NOT_COMPLETE'
        self.seal(incomplete)
        observe = Mock(side_effect=[incomplete, absent])
        self.provider.observe_received_screening = observe
        self.provider.review_received_screening = self.forbid
        coordinator = SimpleNamespace(db=self.db, neuro=self.provider, allowed=lambda day: day == DAY,
            market=SimpleNamespace(catalog=self.forbid, max_age=180),
            store=SimpleNamespace(report=lambda status, _account, **result: (status, result['wait_reason'])))
        account = {}
        with patch.object(recovery.time, 'time', return_value=NOW):
            self.assertEqual(recovery.resume_uncertain_screening(coordinator, DAY, account),
                ('WAITING', 'SCREENING_REPLY_NOT_COMPLETE'))
            self.assertEqual(recovery.resume_uncertain_screening(coordinator, DAY, account),
                ('WAITING', 'SCREENING_UNKNOWN_REPLACEMENT_COOLDOWN'))
            self.assertEqual(recovery.resume_uncertain_screening(coordinator, DAY, account),
                ('WAITING', 'SCREENING_UNKNOWN_REPLACEMENT_COOLDOWN'))
        self.assertEqual([call.args for call in observe.call_args_list], [(OPERATION,), (OPERATION,)])

    def test_unbound_initial_room_is_never_reused_for_replacement(self):
        operation = CYCLE + ':screening:0:2'
        self.db.execute('DELETE FROM robot_jobs'); self.db.execute('DELETE FROM api_requests')
        self.db.execute('DELETE FROM robot_candidates'); self.db.execute('DELETE FROM research_browser_requests')
        data = {**self.data, 'screen': -1, 'seen': [], 'round_symbols': []}
        self.db.execute('UPDATE robot_cycles SET data=? WHERE id=?', (json.dumps(data), CYCLE))
        self.body, serialized, self.request_hash = self.source.request_body(operation,
            'Historical first screen.', self.body['output_schema'], None)
        self.payload = {**self.body, 'request_sha256': self.request_hash}
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)', (operation, CYCLE, 'SCREENING', None, 'NEEDS_REVIEW', None))
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            (operation, None, self.request_hash, 'NEEDS_REVIEW', CREATED, 1, None, recovery.UNKNOWN_REASON))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',
            (operation, self.source.BROWSER_PROVIDER, self.source.BROWSER_CONTRACT, self.request_hash,
                self.body['marker'], serialized))
        envelope = self.envelope(unbound=True)
        damaged = copy.deepcopy(envelope)
        damaged['observation']['room']['first_prompt_sha256'] = 'a'*64
        with self.assertRaises(Review):
            recovery.recover_received_screening(self.provider, self.seal(damaged), catalog={}, max_age=180, now=NOW)
        self.assertEqual(recovery.recover_received_screening(self.provider, envelope, catalog={}, max_age=180, now=NOW), 'NOEXEC')
        audit = self.row('screening_response_recoveries', value=operation)
        rollover = datetime(2026, 11, 26, 17, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(json.loads(audit['proof'])['retirement']['retry_not_before'], rollover)
        cycle = self.row('robot_cycles', 'id', CYCLE)
        self.assertEqual(cycle['state'], 'COMPLETE')
        self.assertNotIn('replacement_due', json.loads(cycle['data']))
        self.provider.observe_received_screening = self.forbid
        coordinator = SimpleNamespace(db=self.db, neuro=self.provider, allowed=lambda day: day in (DAY, NEXT_DAY),
            store=SimpleNamespace(report=lambda status, _account, **result: (status, result['wait_reason'])))
        with patch.object(recovery.time, 'time', return_value=rollover-1):
            self.assertEqual(recovery.resume_uncertain_screening(coordinator, DAY, {}),
                ('WAITING', 'SCREENING_UNKNOWN_AWAITING_WIB_ROLLOVER'))
        with patch.object(recovery.time, 'time', return_value=rollover):
            self.assertIsNone(recovery.resume_uncertain_screening(coordinator, NEXT_DAY, {}))


if __name__ == '__main__':
    unittest.main()
