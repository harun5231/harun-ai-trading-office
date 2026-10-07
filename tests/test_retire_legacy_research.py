"""Offline evidence-preserving retirement; no worker/SDK imports or HTTP calls."""
from contextlib import redirect_stdout
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('legacy_research_retirement', ROOT / 'deploy/retire_legacy_research.py')
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)
SECRET = 'SYNTHETIC_PRIVATE_OUTPUT_MUST_NOT_APPEAR'
LEGACY = ('2026-10-06:analysis-v6:BTCUSDT', '2026-10-06:robot-v7:0:screening:0:1', '2026-10-05:manual-screening:v2')


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # Some execution environments place their own Git marker in /tmp.
        # This fixture models the independent /data volume, not that checkout.
        checkout = patch.object(tool, 'inside_checkout', return_value=False)
        checkout.start()
        self.addCleanup(checkout.stop)
        self.trading = self.root / 'trading'
        self.trading.mkdir(mode=0o700)
        self.path = self.trading / 'ledger.sqlite3'
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE api_requests(operation TEXT PRIMARY KEY,idempotency TEXT,body_hash TEXT,
                state TEXT,created REAL,attempts INTEGER,output TEXT,failure_code TEXT,unknown_field BLOB);
            CREATE TABLE robot_settings(id INTEGER PRIMARY KEY,enabled INTEGER,risk TEXT);
            INSERT INTO robot_settings VALUES(1,0,'5');
            CREATE TABLE robot_jobs(operation TEXT PRIMARY KEY,cycle TEXT,state TEXT);
            CREATE TABLE robot_candidates(id TEXT PRIMARY KEY,cycle TEXT,status TEXT,plan TEXT);
            CREATE TABLE robot_cycles(id TEXT PRIMARY KEY,day TEXT,state TEXT);
            CREATE TABLE order_intents(id TEXT PRIMARY KEY,candidate_id TEXT,state TEXT,payload TEXT);
            CREATE TABLE robot_entry_receipts(id TEXT PRIMARY KEY,symbol TEXT);
            CREATE TABLE office_cache(id INTEGER PRIMARY KEY,data TEXT);
            INSERT INTO office_cache VALUES(1,'HYPEUSDT manual exposure untouched');
            CREATE TABLE operator_audit(data TEXT);
        ''')
        for operation in LEGACY:
            self.insert(operation, 'NEEDS_REVIEW', failure='INVALID_SCREENING_SYMBOL')
        self.insert('2026-10-06:robot-v9:0:analysis-v9:ETHUSDT', 'COMPLETE')
        self.insert('unknown-complete', 'COMPLETE')
        self.path.chmod(0o600)
        (self.trading / 'cycle.lock').write_bytes(b'unchanged lock inode')
        (self.trading / 'cycle.lock').chmod(0o600)
        self.addCleanup(self.db.close)

    def insert(self, operation, state, failure=None):
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?,?)',
            (operation, None, 'private body hash', state, 1791286400.125, 2,
             SECRET, failure, b'\x00\xff' + SECRET.encode()))

    def requests(self, db=None):
        return (db or self.db).execute('SELECT * FROM api_requests ORDER BY operation').fetchall()

    def names(self):
        return {path.relative_to(self.root).as_posix() for path in self.root.rglob('*')}

    def invoke(self, apply=False):
        output = io.StringIO()
        with redirect_stdout(output):
            result = tool.main(['--data-dir', str(self.root), *(['--apply'] if apply else [])])
        self.assertNotIn(SECRET, output.getvalue())
        for operation in LEGACY: self.assertNotIn(operation, output.getvalue())
        return result, json.loads(output.getvalue())

    def unchanged(self, before):
        self.assertEqual(self.requests(), before)
        self.assertFalse(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_research_archive'").fetchone())

    def test_default_inspection_never_creates_files_or_modifies_ledger(self):
        before = self.requests()
        names = self.names()
        total = self.db.total_changes
        status, report = self.invoke()
        self.assertEqual((status, report['status']), (0, 'INSPECTION_ONLY'))
        self.assertEqual(report['eligible_count'], 3)
        self.assertTrue(report['can_apply'])
        self.assertEqual(report['failure_codes'], {'INVALID_SCREENING_SYMBOL': 3})
        self.assertEqual(self.names(), names)
        self.assertEqual(self.db.total_changes, total)
        self.unchanged(before)

    def test_inspect_distinguishes_legacy_current_and_unknown_without_raw_identifiers(self):
        operations = (
            ('2026-10-06:robot-v8:0:screening:1:2', 'CURRENT_V8'),
            ('2026-10-06:robot-v9:1:analysis-v9:SOLUSDT', 'CURRENT_V9'),
            (SECRET + ':robot-v1:' + SECRET, 'UNRECOGNIZED'),
            ('2026-10-06:robot-v7:3:screening:0:1', 'UNRECOGNIZED'),
        )
        for operation, _ in operations: self.insert(operation, 'NEEDS_REVIEW', SECRET)
        before, names, total = self.requests(), self.names(), self.db.total_changes
        _, report = self.invoke()
        details = {row['operation_sha256']: row for row in report['unresolved_requests']}
        self.assertEqual(report['unresolved_request_count'], 7)
        self.assertEqual(report['unresolved_truncated_count'], 0)
        self.assertFalse(report['can_apply'])
        self.assertIn('CURRENT_OR_UNKNOWN_REQUEST_UNRESOLVED', report['refusal_reasons'])
        for operation, category in (*operations, *((operation, 'LEGACY_RECOGNIZED') for operation in LEGACY)):
            item = details[tool.checksum(operation.encode())]
            self.assertEqual(item['scope_category'], category)
            self.assertEqual(item['state'], 'NEEDS_REVIEW')
            self.assertEqual(item['created_at'], '2026-10-06T11:33:20.125000+00:00')
            self.assertEqual(item['attempts'], 2)
        current = details[tool.checksum(operations[1][0].encode())]
        self.assertEqual(current['shape'], ['DATE', 'ROBOT_V9', 'INTEGER', 'ANALYSIS_V9', 'USDT_SYMBOL'])
        opaque = details[tool.checksum(operations[2][0].encode())]
        self.assertIsNone(opaque['operation_date'])
        for operation, _ in operations: self.assertNotIn(operation, json.dumps(report))
        self.assertNotIn('SOLUSDT', json.dumps(report))
        self.assertEqual((self.names(), self.db.total_changes), (names, total))
        self.unchanged(before)

    def test_unknown_private_metadata_is_redacted_and_malformed_values_are_bounded(self):
        operation = '2026-02-30:' + SECRET + ':analysis-v6:PRIVATEUSDT'
        self.insert(operation, 'PENDING', SECRET.encode())
        self.db.execute('UPDATE api_requests SET created=?,attempts=? WHERE operation=?',
            (SECRET.encode(), SECRET, operation))
        before, names = self.requests(), self.names()
        _, report = self.invoke()
        item = next(row for row in report['unresolved_requests']
            if row['operation_sha256'] == tool.checksum(operation.encode()))
        self.assertEqual(item['scope_category'], 'UNRECOGNIZED')
        self.assertEqual(item['state'], 'PENDING')
        self.assertEqual(item['created_at'], 'INVALID')
        self.assertEqual(item['attempts'], 'INVALID')
        self.assertEqual(item['failure_code'], 'REDACTED')
        self.assertIsNone(item['operation_date'])
        self.assertEqual(item['shape'], ['REDACTED', 'REDACTED', 'ANALYSIS_V6', 'USDT_SYMBOL'])
        self.assertNotIn('PRIVATEUSDT', json.dumps(report))
        self.assertIn('PENDING_REQUEST_PRESENT', report['refusal_reasons'])
        self.assertEqual(self.names(), names)
        self.unchanged(before)

    def test_unresolved_report_has_fifty_item_limit_and_exact_truncated_count(self):
        for offset in range(60): self.insert(SECRET + str(offset), 'NEEDS_REVIEW', SECRET)
        before, names = self.requests(), self.names()
        _, report = self.invoke()
        self.assertEqual(report['unresolved_request_count'], 63)
        self.assertEqual(len(report['unresolved_requests']), 50)
        self.assertEqual(report['unresolved_truncated_count'], 13)
        hashes = [row['operation_sha256'] for row in report['unresolved_requests']]
        self.assertEqual(hashes, sorted(hashes))
        self.assertTrue(all(len(row['shape']) <= 12 for row in report['unresolved_requests']))
        status, error = self.invoke(True)
        self.assertEqual((status, error), (1, {'error': 'CURRENT_OR_UNKNOWN_REQUEST_UNRESOLVED'}))
        self.assertEqual(self.names(), names)
        self.unchanged(before)

    def test_missing_optional_metadata_is_unavailable_without_schema_changes(self):
        self.db.execute('ALTER TABLE api_requests DROP COLUMN created')
        self.db.execute('ALTER TABLE api_requests DROP COLUMN attempts')
        self.db.execute('ALTER TABLE api_requests DROP COLUMN failure_code')
        before, names = self.requests(), self.names()
        _, report = self.invoke()
        self.assertTrue(report['can_apply'])
        self.assertTrue(all(row['created_at'] == 'UNAVAILABLE' and row['attempts'] == 'UNAVAILABLE'
            and row['failure_code'] == 'UNAVAILABLE' for row in report['unresolved_requests']))
        self.assertEqual(self.names(), names)
        self.unchanged(before)

    def test_apply_archives_exact_types_and_private_full_backup_before_only_state_changes(self):
        before = self.requests()
        lock = (self.trading / 'cycle.lock').stat()
        status, report = self.invoke(True)
        self.assertEqual((status, report['status'], report['retired_count']), (0, 'RETIRED_LEGACY', 3))
        expected = [tuple('RETIRED_LEGACY' if index == 3 and row[0] in LEGACY else value for index, value in enumerate(row)) for row in before]
        self.assertEqual(self.requests(), expected)
        self.assertEqual((self.trading / 'cycle.lock').stat().st_ino, lock.st_ino)
        self.assertEqual(self.db.execute('SELECT enabled,risk FROM robot_settings').fetchone(), (0, '5'))
        self.assertEqual(self.db.execute('SELECT data FROM office_cache').fetchone(), ('HYPEUSDT manual exposure untouched',))
        archive = self.db.execute('SELECT operation,original_row,row_sha256 FROM legacy_research_archive ORDER BY operation').fetchall()
        columns = tuple(row[1] for row in self.db.execute('PRAGMA table_info(api_requests)'))
        for operation, payload, digest in archive:
            original = next(row for row in before if row[0] == operation)
            self.assertEqual(payload, tool.encoded(columns, original))
            self.assertEqual(digest, tool.checksum(payload))
            decoded = json.loads(payload)
            self.assertEqual(decoded[1][4], ['real', struct.pack('>d', original[4]).hex()])
            self.assertEqual(decoded[1][-1], ['blob', original[-1].hex()])
        backup = Path(report['backup'])
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        saved = sqlite3.connect(backup)
        try:
            self.assertEqual(saved.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(self.requests(saved), before)
            self.assertEqual(saved.execute('SELECT data FROM office_cache').fetchone(), ('HYPEUSDT manual exposure untouched',))
        finally: saved.close()

    def test_repeat_apply_is_unchanged_with_one_backup_and_same_archive(self):
        self.invoke(True)
        before = self.requests()
        archive = self.db.execute('SELECT * FROM legacy_research_archive ORDER BY operation').fetchall()
        names = self.names()
        status, report = self.invoke(True)
        self.assertEqual((status, report), (0, {'status': 'UNCHANGED', 'retired_count': 0}))
        self.assertEqual(self.requests(), before)
        self.assertEqual(self.db.execute('SELECT * FROM legacy_research_archive ORDER BY operation').fetchall(), archive)
        self.assertEqual(self.names(), names)

    def test_user_pattern_three_legacy_reviews_twelve_complete_only_removes_global_legacy_block(self):
        for index in range(10): self.insert('2026-10-06:analysis-v6:TEST' + str(index) + 'USDT', 'COMPLETE')
        before = self.requests()
        self.assertEqual(len(before), 15)
        status, report = self.invoke(True)
        self.assertEqual((status, report['retired_count']), (0, 3))
        self.assertFalse(self.db.execute("SELECT 1 FROM api_requests WHERE state IN ('PENDING','NEEDS_REVIEW')").fetchone())
        completed = [row for row in self.requests() if row[3] == 'COMPLETE']
        self.assertEqual(completed, [row for row in before if row[3] == 'COMPLETE'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM api_requests').fetchone(), (15,))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_jobs').fetchone(), (0,))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM order_intents').fetchone(), (0,))

    def test_no_eligible_creates_neither_archive_nor_backup(self):
        self.db.execute("UPDATE api_requests SET state='COMPLETE'")
        before = self.requests()
        names = self.names()
        status, report = self.invoke(True)
        self.assertEqual((status, report['status']), (0, 'UNCHANGED'))
        self.assertEqual(self.names(), names)
        self.unchanged(before)

    def test_on_is_reported_and_apply_never_changes_robot_or_requests(self):
        self.db.execute('UPDATE robot_settings SET enabled=1')
        before = self.requests()
        _, report = self.invoke()
        self.assertFalse(report['robot_off'])
        self.assertFalse(report['can_apply'])
        status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'ROBOT_OFF_REQUIRED'))
        self.unchanged(before)
        self.assertEqual(self.db.execute('SELECT enabled FROM robot_settings').fetchone(), (1,))

    def test_pending_any_namespace_and_current_or_unknown_reviews_block_retirement(self):
        cases = (
            ('2026-10-06:analysis-v6:SOLUSDT', 'PENDING', 'PENDING_REQUEST_PRESENT'),
            ('2026-10-06:robot-v9:0:analysis-v9:SOLUSDT', 'NEEDS_REVIEW', 'CURRENT_OR_UNKNOWN_REQUEST_UNRESOLVED'),
            ('2026-10-06:robot-v8:0:analysis-v8:SOLUSDT', 'NEEDS_REVIEW', 'CURRENT_OR_UNKNOWN_REQUEST_UNRESOLVED'),
            ('unrecognized-private-op', 'NEEDS_REVIEW', 'CURRENT_OR_UNKNOWN_REQUEST_UNRESOLVED'),
        )
        for operation, state, code in cases:
            with self.subTest(operation=operation):
                self.insert(operation, state)
                before = self.requests()
                status, error = self.invoke(True)
                self.assertEqual((status, error['error']), (1, code))
                self.unchanged(before)
                self.db.execute('DELETE FROM api_requests WHERE operation=?', (operation,))

    def test_order_intent_or_real_receipt_always_blocks(self):
        for table, query in (
            ('order_intents', "INSERT INTO order_intents VALUES('known','candidate','CLOSED','private')"),
            ('robot_entry_receipts', "INSERT INTO robot_entry_receipts VALUES('real','BTCUSDT')"),
        ):
            with self.subTest(table=table):
                self.db.execute(query)
                before = self.requests()
                status, error = self.invoke(True)
                self.assertEqual((status, error['error']), (1, 'ORDER_EVIDENCE_PRESENT'))
                self.unchanged(before)
                self.db.execute('DELETE FROM ' + table)

    def test_unresolved_job_or_cycle_blocks(self):
        for table, query, code in (
            ('robot_jobs', "INSERT INTO robot_jobs VALUES('current','current','PENDING')", 'ROBOT_JOB_UNRESOLVED'),
            ('robot_cycles', "INSERT INTO robot_cycles VALUES('current','2026-10-06','NEEDS_REVIEW')", 'ROBOT_CYCLE_UNRESOLVED'),
        ):
            with self.subTest(table=table):
                self.db.execute(query)
                before = self.requests()
                status, error = self.invoke(True)
                self.assertEqual((status, error['error']), (1, code))
                self.unchanged(before)
                self.db.execute('DELETE FROM ' + table)

    def test_existing_legacy_references_are_not_bypassed(self):
        operation = LEGACY[0]
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?)', (operation, 'old', 'COMPLETE'))
        before = self.requests()
        status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'LEGACY_REQUEST_REFERENCED'))
        self.unchanged(before)

    def test_trigger_or_fk_cannot_change_other_data_during_retirement(self):
        self.db.execute("CREATE TRIGGER unsafe AFTER UPDATE ON api_requests BEGIN INSERT INTO operator_audit VALUES('changed'); END")
        before = self.requests()
        status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'UNSAFE_RETIREMENT_SCHEMA'))
        self.unchanged(before)
        self.assertEqual(self.db.execute('SELECT * FROM operator_audit').fetchall(), [])
        self.db.execute('DROP TRIGGER unsafe')
        self.db.execute('CREATE TABLE foreign_ref(operation TEXT REFERENCES api_requests(operation))')
        status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'UNSAFE_RETIREMENT_SCHEMA'))
        self.unchanged(before)

    def test_busy_cycle_lock_prevents_backup_and_mutation(self):
        fd = os.open(self.trading / 'cycle.lock', os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = self.requests()
            status, error = self.invoke(True)
            self.assertEqual((status, error['error']), (1, 'CYCLE_LOCK_BUSY'))
            self.unchanged(before)
            self.assertFalse((self.trading / 'legacy-research-backups').exists())
        finally: os.close(fd)

    def test_data_inside_checkout_cannot_create_a_private_backup(self):
        before = self.requests()
        with patch.object(tool, 'inside_checkout', return_value=True):
            status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'BACKUP_INSIDE_CHECKOUT'))
        self.unchanged(before)
        self.assertFalse((self.trading / 'legacy-research-backups').exists())

    def test_changed_off_setting_between_backup_and_writer_is_refused(self):
        backup = tool.create_backup
        before = self.requests()

        def changed(*arguments):
            saved = backup(*arguments)
            self.db.execute('UPDATE robot_settings SET enabled=1')
            return saved

        with patch.object(tool, 'create_backup', side_effect=changed): status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'REQUEST_JOURNAL_CHANGED'))
        self.unchanged(before)
        self.assertEqual(self.db.execute('SELECT enabled FROM robot_settings').fetchone(), (1,))

    def test_changed_payload_between_backup_and_writer_is_preserved(self):
        backup = tool.create_backup

        def changed(*arguments):
            saved = backup(*arguments)
            self.db.execute('UPDATE api_requests SET unknown_field=? WHERE operation=?', (b'new private value', LEGACY[0]))
            return saved

        with patch.object(tool, 'create_backup', side_effect=changed): status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'REQUEST_JOURNAL_CHANGED'))
        self.assertEqual(self.db.execute('SELECT unknown_field,state FROM api_requests WHERE operation=?', (LEGACY[0],)).fetchone(), (b'new private value', 'NEEDS_REVIEW'))
        self.assertFalse(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_research_archive'").fetchone())

    def test_partial_archive_update_failure_rolls_back_every_row(self):
        apply_rows = tool.apply_rows
        before = self.requests()

        def interrupted(db, observation):
            apply_rows(db, observation)
            raise RuntimeError(SECRET)

        with patch.object(tool, 'apply_rows', side_effect=interrupted): status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'LEGACY_RETIREMENT_FAILED'))
        self.unchanged(before)

    def test_backup_failure_leaves_every_active_row_unchanged(self):
        before = self.requests()
        with patch.object(tool, 'create_backup', side_effect=RuntimeError(SECRET)):
            status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'LEGACY_RETIREMENT_FAILED'))
        self.unchanged(before)

    def test_directory_rename_and_same_byte_clone_before_writer_cannot_evade_guard(self):
        backup = tool.create_backup
        before = self.requests()
        moved = self.root / 'moved-trading'

        def swapped(*arguments):
            saved = backup(*arguments)
            self.trading.rename(moved)
            shutil.copytree(moved, self.trading)
            return saved

        with patch.object(tool, 'create_backup', side_effect=swapped): status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'STATE_DIRECTORY_CHANGED'))
        self.unchanged(before)
        clone = sqlite3.connect(self.path)
        try: self.assertEqual(self.requests(clone), before)
        finally: clone.close()

    def test_directory_swap_after_state_updates_rolls_back_before_commit(self):
        apply_rows = tool.apply_rows
        before = self.requests()
        moved = self.root / 'moved-before-commit'

        def swapped(db, observation):
            apply_rows(db, observation)
            self.trading.rename(moved)
            shutil.copytree(moved, self.trading)

        with patch.object(tool, 'apply_rows', side_effect=swapped): status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'STATE_DIRECTORY_CHANGED'))
        self.unchanged(before)

    def test_cycle_hardlink_is_refused_without_replacing_lock_or_mutating_records(self):
        lock = self.trading / 'cycle.lock'
        os.link(lock, self.root / 'lock-alias')
        inode = lock.stat().st_ino
        before = self.requests()
        status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'UNSAFE_STATE_FILE'))
        self.assertEqual(lock.stat().st_ino, inode)
        self.unchanged(before)

    def test_oversized_private_payload_is_refused_without_printing_or_archiving_it(self):
        self.db.execute('UPDATE api_requests SET unknown_field=? WHERE operation=?', (b'x' * (tool.MAX_ROW_BYTES + 1), LEGACY[0]))
        before = self.requests()
        status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'REQUEST_ROW_TOO_LARGE'))
        self.unchanged(before)

    def test_delete_journal_mode_is_supported_without_inspection_creating_sidecars(self):
        self.db.execute('PRAGMA journal_mode=DELETE')
        names = self.names()
        status, report = self.invoke()
        self.assertEqual((status, report['status']), (0, 'INSPECTION_ONLY'))
        self.assertEqual(self.names(), names)
        status, report = self.invoke(True)
        self.assertEqual((status, report['status']), (0, 'RETIRED_LEGACY'))

    def test_closed_wal_header_without_sidecars_is_refused_without_file_creations(self):
        self.db.close()
        names = self.names()
        status, error = self.invoke()
        self.assertEqual((status, error['error']), (1, 'WAL_STATE_UNAVAILABLE'))
        self.assertEqual(self.names(), names)

    def test_unknown_failure_is_retained_privately_and_redacted_in_report(self):
        self.db.execute('UPDATE api_requests SET failure_code=? WHERE operation=?', (SECRET, LEGACY[0]))
        _, report = self.invoke()
        self.assertEqual(report['failure_codes']['REDACTED'], 1)
        self.invoke(True)
        self.assertEqual(self.db.execute('SELECT failure_code FROM api_requests WHERE operation=?', (LEGACY[0],)).fetchone(), (SECRET,))

    def test_invalid_marker_blocks_reusing_a_previous_retirement(self):
        self.invoke(True)
        self.db.execute('UPDATE legacy_research_archive SET row_sha256=?', (SECRET,))
        before = self.requests()
        status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'RETIREMENT_MARKER_INVALID'))
        self.assertEqual(self.requests(), before)


class GrammarTests(unittest.TestCase):
    def test_diagnostic_tokens_are_fixed_and_never_expand_retirement_grammar(self):
        for version in range(1, 10):
            operation = '2026-10-06:robot-v' + str(version) + ':0:analysis-v' + str(version) + ':ETHUSDT'
            item = tool.operation_metadata(operation)
            self.assertEqual(item['shape'], ['DATE', 'ROBOT_V' + str(version), 'INTEGER',
                'ANALYSIS_V' + str(version), 'USDT_SYMBOL'])
            self.assertEqual(item['scope_category'], {7: 'LEGACY_RECOGNIZED', 8: 'CURRENT_V8',
                9: 'CURRENT_V9'}.get(version, 'UNRECOGNIZED'))
            self.assertEqual(bool(tool.classify(operation)), version == 7)
        for operation in ('2026-02-30:robot-v9:0:analysis-v9:ETHUSDT',
                '2026-10-06:robot-v9:0:analysis-v8:ETHUSDT',
                '2026-10-06:robot-v10:0:analysis-v10:ETHUSDT', SECRET * 5, b'private_blob_id'):
            item = tool.operation_metadata(operation)
            self.assertEqual(item['scope_category'], 'UNRECOGNIZED')
            self.assertIsNone(tool.classify(operation))
            self.assertNotIn('ETHUSDT', json.dumps(item))
            self.assertNotIn(SECRET, json.dumps(item))
        for epoch in ('3', '12', '999999999999'):
            operation = '2026-10-06:robot-v9:' + epoch + ':analysis-v9:ETHUSDT'
            item = tool.operation_metadata(operation)
            self.assertEqual(item['scope_category'], 'CURRENT_V9')
            self.assertEqual(item['shape'], ['DATE', 'ROBOT_V9', 'INTEGER', 'ANALYSIS_V9', 'USDT_SYMBOL'])
            self.assertIsNone(tool.classify(operation))
        for epoch in ('00', '01', '1000000000000'):
            item = tool.operation_metadata('2026-10-06:robot-v9:' + epoch + ':analysis-v9:ETHUSDT')
            self.assertEqual(item['scope_category'], 'UNRECOGNIZED')
        self.assertEqual(tool.operation_metadata('2026-10-06:robot-v8:3:analysis-v8:ETHUSDT')['scope_category'], 'UNRECOGNIZED')

    def test_diagnostic_timestamp_and_attempt_values_are_strictly_bounded(self):
        for value in (-1, 4102444801, float('inf'), float('-inf'), float('nan'), SECRET, b'private', True):
            self.assertEqual(tool.public_created(value), 'INVALID')
        self.assertEqual(tool.public_created(None), 'UNAVAILABLE')
        self.assertEqual(tool.public_created(0), '1970-01-01T00:00:00.000000+00:00')
        self.assertEqual(tool.public_created(4102444800), '2100-01-01T00:00:00.000000+00:00')
        columns = ('operation', 'state', 'created', 'attempts', 'failure_code')
        for value, expected in ((None, 'UNAVAILABLE'), (-1, 'INVALID'), (1001, 'INVALID'),
                (2.5, 'INVALID'), (SECRET, 'INVALID'), (b'private', 'INVALID'), (0, 0), (1000, 1000)):
            item = tool.unresolved_details(columns, [('unknown', 'PENDING', None, value, None)])['unresolved_requests'][0]
            self.assertEqual(item['attempts'], expected)

    def test_exact_proven_historical_formats_only(self):
        valid = ('screening', 'replacement-screening:1', 'replacement-screening:3',
            'manual-screening:v2', 'analysis:BTCUSDT', 'analysis-v3:BTCUSDT',
            'analysis-v4:BTCUSDT', 'analysis-v5:BTCUSDT', 'analysis-v6:BTCUSDT',
            'robot-v7:0:screening:0:1', 'robot-v7:2:screening:3:2', 'robot-v7:1:analysis-v7:BTCUSDT')
        for suffix in valid:
            self.assertIsNotNone(tool.classify('2026-10-06:' + suffix), suffix)
        invalid = ('analysis-v1:BTCUSDT', 'analysis-v2:BTCUSDT', 'analysis-v8:BTCUSDT',
            'analysis-v9:BTCUSDT', 'manual-screening', 'robot-v7:3:screening:0:1',
            'robot-v7:0:screening:4:1', 'robot-v7:0:screening:0:3', 'robot-v9:0:analysis-v9:BTCUSDT',
            'analysis:btcUSDT', 'replacement-screening:4', 'screening:0')
        for suffix in invalid:
            self.assertIsNone(tool.classify('2026-10-06:' + suffix), suffix)
        self.assertIsNone(tool.classify('2026-02-30:screening'))
        self.assertIsNone(tool.classify('0000-01-01:screening'))


if __name__ == '__main__':
    unittest.main()
