"""Received screening recovery uses the actual native recorder, never Send."""
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.native_browser_hook import _digest
from agent.native_daily_room import identities
from agent.native_screening_recovery import clarification, PROTOCOL, NATIVE_DOM_SHA256
from worker.account_state import screening_contract
from worker.core import Review
from worker.screening_response_recovery import recover_screening_response

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 5, 8, 30, tzinfo=timezone.utc).timestamp()
DAY = '2026-11-05'
OP = DAY + ':robot-v9:0:screening:0:2'


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def native_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ScreeningResponseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.old_modules = {name: sys.modules.get(name) for name in
            ('worker.research_provider', 'worker.neurobrowser')}
        # The production cutover artifact is filesystem custody, separate from
        # receipt logic. Supply only that installed release value in this fixture.
        with patch('worker.owner_source_protocol.load_owner_release_cutoff',
                   return_value='2026-11-04T00:00:00Z'):
            self.source = native_module('worker.research_provider', ROOT / 'vps_native/worker/research_provider.py')
        self.native = native_module('worker.neurobrowser', ROOT / 'vps_native/worker/neurobrowser.py')
        self.addCleanup(self.restore_modules)
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.source.ensure_journal(self.db)
        self.db.executescript('''
            CREATE TABLE robot_cycles(id TEXT PRIMARY KEY,day TEXT,entry_epoch INTEGER,state TEXT,data TEXT);
            CREATE TABLE robot_jobs(operation TEXT PRIMARY KEY,cycle TEXT,kind TEXT,symbol TEXT,state TEXT,risk_target TEXT);
            CREATE TABLE robot_settings(id INTEGER PRIMARY KEY,enabled INTEGER,risk TEXT);
            INSERT INTO robot_settings VALUES(1,1,'5');
            CREATE TABLE order_intents(id TEXT PRIMARY KEY,state TEXT,payload TEXT);
            INSERT INTO order_intents VALUES('old-wld','CLOSED','original accepted payload');
            CREATE TABLE pending_review(operation TEXT PRIMARY KEY,state TEXT,decision TEXT);
            INSERT INTO pending_review VALUES('old-wld-review-125','ORDER_TERMINAL_NOT_APPLIED','CLOSED');
            CREATE TABLE robot_candidates(id TEXT,cycle TEXT,symbol TEXT,status TEXT,plan TEXT,failure_code TEXT);
        ''')
        self.provider = object.__new__(self.native.NeuroBrowser)
        self.provider.db = self.db
        self.provider.chart_max_age = 180
        self.provider.transport = self.forbid
        self.provider.require_key = self.forbid
        prompt, schema = screening_contract(2)
        self.body, serialized, wire = self.source.request_body(OP, prompt, schema, None)
        self.payload = {**self.body, 'request_sha256': wire}
        self.created = NOW - 90
        self.data = dict(target=2, queue=[], seen=['BTCUSDT'], screen=-1,
            replacements=0, round_symbols=[])
        self.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',
            (DAY + ':robot-v9:0', DAY, 0, 'NEEDS_REVIEW', json.dumps(self.data)))
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
            (OP, DAY + ':robot-v9:0', 'SCREENING', None, 'NEEDS_REVIEW', None))
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            (OP, None, wire, 'NEEDS_REVIEW', self.created, 1, None, 'NEUROBRO_BROWSER_OUTCOME_UNKNOWN'))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',
            (OP, self.source.BROWSER_PROVIDER, self.source.BROWSER_CONTRACT, wire, self.body['marker'], serialized))
        first = (ROOT / 'tests/fixtures/neurobro_screening_table_20261105.txt').read_text()
        rendered = prompt + '\n\n' + self.body['marker']
        rows = [dict(role='user', text=rendered, attachments=[]),
            dict(role='assistant', text=first, complete=True, id='native-first', attachments=[]),
            dict(role='user', text=clarification(2), attachments=[]),
            dict(role='assistant', text='COIN: RUNEUSDT, TIAUSDT', complete=True,
                id='native-summary', attachments=[])]
        room = dict(day=DAY, room_id='a' * 32, url='https://app.neurobro.ai/chat/' + 'a' * 32,
            updated_at=NOW - 20, anchors=identities(rows), first_prompt_sha256=sha(rendered),
            marker=self.body['marker'], history_titles=['Rekomendasi coin futures binance'])
        output = {'symbols': ['RUNEUSDT', 'TIAUSDT']}
        result = {key: self.payload[key] for key in ('operation', 'marker', 'request_sha256')}
        result.update(output=output, assistant_text=rows[3]['text'], chart_evidence=[],
            response_id='dom-' + sha(room['url'] + '\n' + self.body['marker'] + '\n' + rows[3]['text']))
        self.proof = dict(schema_version=1, kind=PROTOCOL,
            payload_sha256=self.source.sha(self.source.encode(self.payload)), room=room, rows=rows,
            rendered_prompt=rendered, initial_reply_sha256=sha(first), final_reply_sha256=sha(rows[3]['text']),
            native_source_sha256=NATIVE_DOM_SHA256, native_received_at=NOW - 10,
            stable_reads=4, native_poll_reads=4, native_result=result,
            history_restore_attempted=False, history_restored=False)
        request_id = sha(json.dumps(dict(operation=OP, request_sha256=wire,
            reply_sha256=sha(first), phase='SCREENING', transcript_sha256=_digest(rows[:2])),
            sort_keys=True, separators=(',', ':')))
        done = dict(schema_version=1, ok=True, request_id=request_id,
            conversation_id='neurobro:' + DAY + ':' + room['room_id'], phase='SCREENING',
            reply_sha256=sha(first), authority='DIALOGUE_CONTROL_ONLY', trading_authority=False,
            decision=dict(action='SCREENING', symbols=output['symbols'], question=''),
            needs_followup=False, reason='DIALOGUE_DECISION')
        self.done = dict(operation_id='delegated-model:' + request_id, kind='DELEGATED_MODEL_DIALOGUE',
            request_hash='b' * 64, state='DONE', result=json.dumps(done), updated_at=NOW - 30)
        self.catalog = {'RUNEUSDT': {}, 'TIAUSDT': {}}

    def restore_modules(self):
        for name, previous in self.old_modules.items():
            if previous is None: sys.modules.pop(name, None)
            else: sys.modules[name] = previous

    def forbid(self, *args, **kwargs):
        raise AssertionError('Recovery must not dispatch provider or orders')

    def row(self, table):
        return dict(self.db.execute('SELECT * FROM ' + table).fetchone())

    def snapshot(self):
        names = [row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: [tuple(row) for row in self.db.execute('SELECT * FROM ' + name)] for name in names}

    def recover(self, **kwargs):
        return recover_screening_response(self.provider, self.proof, self.done,
            catalog=self.catalog, max_age=180, now=NOW, **kwargs)

    def test_real_native_recorder_and_source_proof_complete_only_received_research(self):
        before = self.snapshot(); retained = []
        def persist(value):
            self.assertEqual(self.snapshot(), before)
            retained.append(value)
        result = self.recover(before_write=persist)
        self.assertEqual(result['continuation'], 'RECEIVED_SCREENING_PENDING')
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]['preimages']['api_requests']['state'], 'NEEDS_REVIEW')
        self.assertEqual(self.row('api_requests')['state'], 'COMPLETE')
        self.assertEqual(self.row('api_requests')['created'], self.created)
        self.assertEqual(self.row('api_requests')['attempts'], 1)
        self.assertEqual(self.row('robot_jobs')['state'], 'PENDING')
        self.assertEqual(self.row('robot_cycles')['state'], 'ACTIVE')
        self.assertEqual(json.loads(self.row('robot_cycles')['data']), self.data)
        self.assertEqual(result['source_evidence'], self.source.source_evidence(self.db, OP))
        for table in ('order_intents', 'pending_review', 'robot_settings', 'research_browser_requests'):
            self.assertEqual(self.snapshot()[table], before[table])
        snapshot = self.snapshot()
        self.recover()
        self.assertEqual(self.snapshot(), snapshot)

    def test_crash_after_native_completion_resumes_same_job_without_duplicate_receipt(self):
        self.provider._record_browser_response(OP, self.body, self.payload['request_sha256'],
            self.proof['native_result'], self.body['output_schema'], lambda value: None, None,
            catalog=self.catalog, received_at=self.proof['native_received_at'], recovery=True)
        self.assertEqual(self.row('robot_jobs')['state'], 'NEEDS_REVIEW')
        receipt = self.row('research_browser_responses')
        self.recover()
        self.assertEqual(self.row('research_browser_responses'), receipt)
        self.assertEqual(self.row('robot_jobs')['state'], 'PENDING')

    def test_stale_received_screening_is_retired_without_backdating_or_accepting_old_coins(self):
        self.db.execute('UPDATE api_requests SET created=?', (NOW - 1801,))
        result = self.recover()
        data = json.loads(self.row('robot_cycles')['data'])
        self.assertEqual(result['continuation'], 'CURRENT_REPLACEMENT_PENDING')
        self.assertEqual(self.row('api_requests')['created'], NOW - 1801)
        self.assertEqual(self.row('robot_jobs')['state'], 'COMPLETE')
        self.assertEqual(self.row('robot_cycles')['state'], 'ACTIVE')
        self.assertEqual(data, {**self.data, 'screen': 0, 'replacement_due': 2})
        self.assertEqual(data['queue'], [])
        snapshot = self.snapshot(); self.recover(); self.assertEqual(self.snapshot(), snapshot)

    def test_current_next_operation_uses_existing_coordinator_replacement_path(self):
        from tests.test_native_screening_recovery import coordinator_class
        from worker.neuroapi import ResearchPaused
        self.db.execute('UPDATE api_requests SET created=?', (NOW - 1801,))
        self.recover()
        robot = object.__new__(coordinator_class()); robot.db = self.db
        robot.screen.__func__.__globals__['ResearchPaused'] = ResearchPaused
        robot.allowed = lambda day: day == DAY
        robot.market = SimpleNamespace(catalog=lambda: self.catalog, max_age=180)
        robot.exposed_symbols = lambda account: set()
        robot.store = SimpleNamespace(report=lambda *args, **kw: kw)
        observed = []
        def ask(operation, *args, **kwargs):
            observed.append((operation, args, kwargs))
            raise ResearchPaused('UNAVAILABLE')  # Isolated test: stop before any provider dispatch.
        robot.neuro = SimpleNamespace(ask=ask)
        robot.pause_reason = lambda: 'UNAVAILABLE'
        robot.provider_pause_reason = lambda error, day: 'UNAVAILABLE'
        data = json.loads(self.row('robot_cycles')['data'])
        robot.screen(DAY + ':robot-v9:0', data, 2, {}, DAY, False)
        self.assertEqual([row[0] for row in observed], [DAY + ':robot-v9:0:screening:1:2'])
        self.assertNotEqual(observed[0][0], OP)
        self.assertEqual(self.row('api_requests')['attempts'], 1)
        self.assertEqual(self.row('api_requests')['created'], NOW - 1801)

    def test_failing_forensic_writer_prevents_all_writes(self):
        before = self.snapshot()
        def fail(value): raise OSError('disk full')
        with self.assertRaises(OSError): self.recover(before_write=fail)
        self.assertEqual(self.snapshot(), before)

    def test_unknown_mini_or_wrong_reply_cannot_be_promoted(self):
        before = self.snapshot()
        for key, value in (('state', 'UNKNOWN'), ('operation_id', 'delegated-model:' + 'f' * 64),
                           ('updated_at', NOW + 1)):
            original = self.done[key]; self.done[key] = value
            with self.subTest(key=key), self.assertRaises(Review): self.recover()
            self.assertEqual(self.snapshot(), before); self.done[key] = original
        done = json.loads(self.done['result']); done['reply_sha256'] = 'f' * 64
        self.done['result'] = json.dumps(done)
        with self.assertRaises(Review): self.recover()
        self.assertEqual(self.snapshot(), before)

    def test_invalid_catalog_unrelated_unknown_and_changed_owner_settings_refuse(self):
        before = self.snapshot(); self.catalog.pop('TIAUSDT')
        with self.assertRaises(Review): self.recover()
        self.assertEqual(self.snapshot(), before); self.catalog['TIAUSDT'] = {}
        self.db.execute("INSERT INTO api_requests VALUES('other-unknown',NULL,'x','NEEDS_REVIEW',?,1,NULL,'UNKNOWN')", (NOW,))
        blocked = self.snapshot()
        with self.assertRaises(Review): self.recover()
        self.assertEqual(self.snapshot(), blocked)
        self.db.execute("DELETE FROM api_requests WHERE operation='other-unknown'")
        def owner_change(value): self.db.execute('UPDATE robot_settings SET enabled=0')
        with self.assertRaises(Review): self.recover(before_write=owner_change)
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')
        self.assertEqual(self.row('robot_settings')['enabled'], 0)

    def test_future_receipt_wrong_day_and_partial_transcript_refuse_without_writes(self):
        before = self.snapshot()
        with self.assertRaises(Review):
            recover_screening_response(self.provider, self.proof, self.done,
                catalog=self.catalog, max_age=180, now=NOW + 86400)
        self.assertEqual(self.snapshot(), before)
        self.proof['native_received_at'] = NOW + 1
        with self.assertRaises(Review): self.recover()
        self.assertEqual(self.snapshot(), before)
        self.proof['native_received_at'] = NOW - 10
        self.proof['rows'][3]['complete'] = False
        with self.assertRaises(Review): self.recover()
        self.assertEqual(self.snapshot(), before)

    def test_another_attempt_or_request_hash_is_not_the_known_received_request(self):
        for column, value, original in (('attempts', 2, 1),
                ('body_hash', 'f' * 64, self.payload['request_sha256'])):
            self.db.execute('UPDATE api_requests SET ' + column + '=?', (value,))
            before = self.snapshot()
            with self.subTest(column=column), self.assertRaises(Review): self.recover()
            self.assertEqual(self.snapshot(), before)
            self.db.execute('UPDATE api_requests SET ' + column + '=?', (original,))


if __name__ == '__main__':
    unittest.main()
