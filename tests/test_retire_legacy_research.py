"""Offline evidence-preserving retirement; no worker/SDK imports or HTTP calls."""
from contextlib import redirect_stdout
import copy
import hashlib
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
SHADOW_SQL = '''CREATE TABLE shadow_plans(setup_id TEXT PRIMARY KEY,day TEXT NOT NULL,symbol TEXT NOT NULL,plan TEXT NOT NULL,state TEXT NOT NULL,model TEXT NOT NULL,UNIQUE(day,symbol))'''
SHADOW_MODEL = dict(state='PLAN_READY', tp_confirmed=False, sl_confirmed=False,
    model_only=True, fill_confirmed=False)


def historical_shadow_plan(symbol, state='SHADOW_PREFLIGHT_OK'):
    """Published GET-only plan shape, independent of deleted modules/Git history."""
    day = '2026-10-05'
    key = hashlib.sha256(json.dumps([day, symbol, 'LONG', '100', '102', '99', '1'],
        separators=(',', ':')).encode()).hexdigest()
    ids = {leg: 'ho-' + key[:28] + '-' + suffix for leg, suffix in
        (('ENTRY', 'e'), ('TP', 't'), ('SL', 's'))}
    plan = dict(status=state, setup_id=key, business_day=day, symbol=symbol, side='LONG',
        position_mode='ONE_WAY', positionSide='BOTH', margin_target='CROSS', leverage_target=75,
        execution_quantity='1', entry='100', TP='102', SL='99', calculated_risk='1', actual_RR='2',
        protective_side='SELL', client_ids=ids, required_account_mutations=[],
        would_submit=False, live_execution=False, mode='DRY_RUN',
        entry_order=dict(api_family='USD-M_FUTURES', intended_path='/fapi/v1/order',
            payload=dict(symbol=symbol, side='BUY', positionSide='BOTH', type='LIMIT',
                timeInForce='GTC', quantity='1', price='100', newClientOrderId=ids['ENTRY'])),
        failure_policy=dict(uncertain_entry='RECONCILE_SAME_CLIENT_ID_NO_BLIND_RETRY',
            partial_fill='PROTECTION_INCOMPLETE_RECONCILE_AND_PROTECT_FILLED_EXPOSURE',
            protection='REQUIRE_BOTH_ACKNOWLEDGED_LEGS', incomplete='BLOCK_NEXT_SETUP',
            after_exit='RECONCILE_FLAT_AND_CLEAR_SIBLING_BEFORE_NEW_SETUP'),
        future_reconciliation=dict(entry=dict(intended_get_path='/fapi/v1/order',
            lookup=dict(symbol=symbol, origClientOrderId=ids['ENTRY'])), implemented=False),
        future_states=['PLAN_READY', 'ENTRY_SUBMITTED', 'ENTRY_CONFIRMED',
            'PROTECTION_SUBMITTED', 'POSITION_PROTECTED'])
    for field, leg, kind, price in (('take_profit', 'TP', 'TAKE_PROFIT_MARKET', '102'),
            ('stop_loss', 'SL', 'STOP_MARKET', '99')):
        plan[field + '_order'] = dict(api_family='USD-M_ALGO', intended_path='/fapi/v1/algoOrder',
            activation='AFTER_CONFIRMED_ENTRY_FILL', payload=dict(algoType='CONDITIONAL',
                symbol=symbol, side='SELL', positionSide='BOTH', type=kind, triggerPrice=price,
                workingType='MARK_PRICE', closePosition='true', clientAlgoId=ids[leg]))
        plan['future_reconciliation'][field] = dict(intended_get_path='/fapi/v1/algoOrder',
            lookup=dict(clientAlgoId=ids[leg]))
    return plan


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

    def invoke(self, apply=False, prove=False):
        output = io.StringIO()
        with redirect_stdout(output):
            result = tool.main(['--data-dir', str(self.root), *(['--apply'] if apply else []),
                *(['--prove-pre-order-screening-rejection'] if prove else [])])
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


class PreOrderProofTests(unittest.TestCase):
    insert = RetirementTests.insert
    requests = RetirementTests.requests
    names = RetirementTests.names
    invoke = RetirementTests.invoke
    unchanged = RetirementTests.unchanged

    def setUp(self):
        RetirementTests.setUp(self)
        self.db.execute('DELETE FROM api_requests WHERE operation=?', (LEGACY[2],))
        self.db.execute('ALTER TABLE robot_cycles ADD COLUMN data TEXT')
        self.opaque = 'historic-screening-probe-' + SECRET
        self.insert(self.opaque, 'NEEDS_REVIEW', 'INVALID_SCREENING_SYMBOL')
        self.db.execute('UPDATE api_requests SET output=NULL,body_hash=?,attempts=1 WHERE operation=?',
            ('a' * 64, self.opaque))
        self.snapshot = self.trading / 'ledger-pre-order.sqlite3'
        self.make_snapshot()

    def make_snapshot(self):
        # Independent published old core.py schemas; no Git-history dependency.
        with sqlite3.connect(self.snapshot) as archive:
            archive.executescript('''
                CREATE TABLE trades(id TEXT PRIMARY KEY, day TEXT NOT NULL, source TEXT NOT NULL,
                  state TEXT NOT NULL, plan TEXT NOT NULL, exit_price TEXT, pnl TEXT, created TEXT NOT NULL, closed_day TEXT);
                CREATE TABLE events(seq INTEGER PRIMARY KEY,at TEXT,state TEXT,agent TEXT,message TEXT);
                CREATE TABLE robot_entry_receipts(id TEXT PRIMARY KEY,symbol TEXT NOT NULL,
                  entry_day TEXT NOT NULL,confirmed_at TEXT NOT NULL);
            ''')
            archive.execute(self.db.execute("SELECT sql FROM sqlite_master WHERE name='api_requests'").fetchone()[0])
            for row in self.requests():
                if ':robot-v9:' not in row[0]: archive.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?,?)', row)
            archive.execute('INSERT INTO events VALUES(1,?,?,?,?)', (SECRET, 'REJECTED', SECRET, SECRET))
        self.snapshot.chmod(0o600)

    def archive_change(self, command, values=()):
        with sqlite3.connect(self.snapshot) as archive: archive.execute(command, values)

    def test_default_still_refuses_opaque_claim_even_when_snapshot_is_available(self):
        before, names = self.requests(), self.names()
        _, report = self.invoke()
        self.assertEqual(report['eligible_count'], 2)
        self.assertNotIn('pre_order_proof', report)
        self.assertIn('CURRENT_OR_UNKNOWN_REQUEST_UNRESOLVED', report['refusal_reasons'])
        status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'CURRENT_OR_UNKNOWN_REQUEST_UNRESOLVED'))
        self.assertEqual(self.names(), names)
        self.unchanged(before)

    def test_opt_in_inspection_verifies_full_proof_without_changing_files_or_claiming_scope(self):
        before, names, snapshot = self.requests(), self.names(), self.snapshot.read_bytes()
        _, report = self.invoke(prove=True)
        self.assertTrue(report['can_apply'])
        self.assertEqual(report['eligible_count'], 3)
        self.assertEqual(report['eligible_scopes']['pre_order_archive'], 1)
        self.assertEqual(report['pre_order_proof']['status'], 'VERIFIED')
        self.assertEqual(report['pre_order_proof']['matched_count'], 1)
        self.assertEqual(report['pre_order_proof']['archive_sha256'], tool.checksum(snapshot))
        opaque = next(item for item in report['unresolved_requests'] if item['operation_sha256'] == tool.checksum(self.opaque.encode()))
        self.assertEqual(opaque['scope_category'], 'UNRECOGNIZED')
        self.assertEqual(self.snapshot.read_bytes(), snapshot)
        self.assertEqual(self.names(), names)
        self.unchanged(before)

    def test_one_atomic_apply_preserves_complete_rows_typed_archive_private_backup_and_marker(self):
        before, snapshot = self.requests(), self.snapshot.read_bytes()
        status, report = self.invoke(True, True)
        self.assertEqual((status, report['status'], report['retired_count']), (0, 'RETIRED_LEGACY', 3))
        columns = tuple(row[1] for row in self.db.execute('PRAGMA table_info(api_requests)'))
        source = next(row for row in before if row[0] == self.opaque)
        scope, payload = self.db.execute('SELECT scope,original_row FROM legacy_research_archive WHERE operation=?', (self.opaque,)).fetchone()
        self.assertEqual(scope, 'pre_order_archive:' + tool.checksum(snapshot))
        self.assertEqual(payload, tool.encoded(columns, source))
        self.assertEqual([row for row in self.requests() if row[3] == 'COMPLETE'], [row for row in before if row[3] == 'COMPLETE'])
        self.assertFalse(self.db.execute("SELECT 1 FROM api_requests WHERE state IN ('PENDING','NEEDS_REVIEW')").fetchone())
        self.assertEqual(self.db.execute('SELECT enabled FROM robot_settings').fetchone(), (0,))
        self.assertEqual(self.snapshot.read_bytes(), snapshot)
        with sqlite3.connect(report['backup']) as backup: self.assertEqual(self.requests(backup), before)
        names, archived = self.names(), self.db.execute('SELECT * FROM legacy_research_archive ORDER BY operation').fetchall()
        for prove in (False, True):
            _, inspect = self.invoke(prove=prove)
            self.assertEqual(inspect['pre_order_proof']['matched_count'], 1)
            status, repeat = self.invoke(True, prove)
            self.assertEqual((status, repeat), (0, {'status': 'UNCHANGED', 'retired_count': 0}))
        self.assertEqual(self.names(), names)
        self.assertEqual(self.db.execute('SELECT * FROM legacy_research_archive ORDER BY operation').fetchall(), archived)

    def test_missing_snapshot_is_refused_without_mutation(self):
        self.snapshot.unlink()
        before, names = self.requests(), self.names()
        status, error = self.invoke(True, True)
        self.assertEqual((status, error['error']), (1, 'STATE_FILE_MISSING'))
        self.assertEqual(self.names(), names)
        self.unchanged(before)

    def test_schema_and_typed_full_row_mismatch_refuse_even_when_identity_and_state_match(self):
        cases = (
            ('UPDATE api_requests SET unknown_field=? WHERE operation=?', (b'altered-private-byte', self.opaque), 'PRE_ORDER_REQUEST_ROW_MISMATCH'),
            ('UPDATE api_requests SET created=created+0.000001 WHERE operation=?', (self.opaque,), 'PRE_ORDER_REQUEST_ROW_MISMATCH'),
            ('ALTER TABLE api_requests ADD COLUMN unexpected TEXT', (), 'PRE_ORDER_API_SCHEMA_MISMATCH'),
        )
        original = self.snapshot.read_bytes()
        for command, values, expected in cases:
            with self.subTest(command=command):
                self.archive_change(command, values)
                before = self.requests()
                status, error = self.invoke(True, True)
                self.assertEqual((status, error['error']), (1, expected))
                self.unchanged(before)
                self.snapshot.write_bytes(original)

    def test_target_absent_from_snapshot_has_specific_safe_refusal_without_mutation(self):
        self.archive_change('DELETE FROM api_requests WHERE operation=?', (self.opaque,))
        before, names = self.requests(), self.names()
        status, error = self.invoke(True, True)
        self.assertEqual((status, error), (1, {'error': 'PRE_ORDER_REQUEST_NOT_IN_ARCHIVE'}))
        self.assertEqual(self.names(), names)
        self.unchanged(before)

    def test_snapshot_core_schema_new_runtime_tables_triggers_foreign_keys_and_orders_refuse(self):
        cases = (
            ('ALTER TABLE events ADD COLUMN extra TEXT', 'PRE_ORDER_ARCHIVE_SCHEMA_UNSUPPORTED'),
            ('CREATE TABLE office_schema(version INTEGER)', 'PRE_ORDER_ARCHIVE_SCHEMA_UNSUPPORTED'),
            ('CREATE TABLE order_intents(id TEXT)', 'PRE_ORDER_ARCHIVE_SCHEMA_UNSUPPORTED'),
            ('CREATE TABLE robot_candidates(id TEXT)', 'PRE_ORDER_ARCHIVE_SCHEMA_UNSUPPORTED'),
            ('CREATE TABLE office_cache(id TEXT)', 'PRE_ORDER_ARCHIVE_SCHEMA_UNSUPPORTED'),
            ('CREATE TABLE office_activity(id TEXT)', 'PRE_ORDER_ARCHIVE_SCHEMA_UNSUPPORTED'),
            ('CREATE TRIGGER changed AFTER UPDATE ON events BEGIN SELECT 1; END', 'PRE_ORDER_ARCHIVE_SCHEMA_UNSUPPORTED'),
            ('CREATE TABLE cycles(day TEXT REFERENCES api_requests(operation))', 'PRE_ORDER_ARCHIVE_SCHEMA_UNSUPPORTED'),
            ("INSERT INTO trades VALUES('id','day','source','state','{}',NULL,NULL,'time',NULL)", 'PRE_ORDER_ARCHIVE_ORDER_EVIDENCE_PRESENT'),
            ("INSERT INTO robot_entry_receipts VALUES('receipt','BTCUSDT','day','time')", 'PRE_ORDER_ARCHIVE_ORDER_EVIDENCE_PRESENT'),
            ("CREATE TABLE live_actions(id TEXT)", None),
        )
        original = self.snapshot.read_bytes()
        for command, expected in cases:
            with self.subTest(command=command):
                self.archive_change(command)
                if expected is None:
                    self.archive_change("INSERT INTO live_actions VALUES('unknown execution')")
                    expected = 'PRE_ORDER_ARCHIVE_ORDER_EVIDENCE_PRESENT'
                before = self.requests()
                status, error = self.invoke(True, True)
                self.assertEqual((status, error['error']), (1, expected))
                self.unchanged(before)
                self.snapshot.write_bytes(original)

    def test_literal_sql_differences_in_api_constraints_are_not_normalized_away(self):
        with sqlite3.connect(self.snapshot) as archive:
            schema = archive.execute("SELECT sql FROM sqlite_master WHERE name='api_requests'").fetchone()[0]
            rows = self.requests(archive)
            archive.execute('DROP TABLE api_requests')
            archive.execute(schema.replace('unknown_field BLOB', "unknown_field BLOB CHECK(unknown_field IS NOT NULL OR output='AB')"))
            archive.executemany('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?,?)', rows)
        schema = self.db.execute("SELECT sql FROM sqlite_master WHERE name='api_requests'").fetchone()[0]
        rows = self.requests()
        self.db.execute('DROP TABLE api_requests')
        self.db.execute(schema.replace('unknown_field BLOB', "unknown_field BLOB CHECK(unknown_field IS NOT NULL OR output='a b')"))
        self.db.executemany('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?,?)', rows)
        before = self.requests()
        status, error = self.invoke(True, True)
        self.assertEqual((status, error['error']), (1, 'PRE_ORDER_API_SCHEMA_MISMATCH'))
        self.unchanged(before)

    def test_real_migration_snapshot_from_wal_historical_database_passes_closed_proof(self):
        from worker.migration import retire_previous_runtime
        historical = self.root / 'historical'
        historical.mkdir(mode=0o700)
        path = historical / 'ledger.sqlite3'
        source = sqlite3.connect(path, isolation_level=None)
        try:
            self.assertEqual(source.execute('PRAGMA journal_mode=WAL').fetchone(), ('wal',))
            source.executescript('''CREATE TABLE trades(id TEXT PRIMARY KEY, day TEXT NOT NULL, source TEXT NOT NULL,
                state TEXT NOT NULL, plan TEXT NOT NULL, exit_price TEXT, pnl TEXT, created TEXT NOT NULL, closed_day TEXT);
                CREATE TABLE events(seq INTEGER PRIMARY KEY,at TEXT,state TEXT,agent TEXT,message TEXT);''')
            source.execute(self.db.execute("SELECT sql FROM sqlite_master WHERE name='api_requests'").fetchone()[0])
            source.executemany('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?,?)',
                [row for row in self.requests() if ':robot-v9:' not in row[0]])
            retire_previous_runtime(source)
            migrated = historical / 'ledger-pre-order.sqlite3'
            header = migrated.read_bytes()[:100]
            self.assertEqual(header[18:20], b'\x02\x02')
            self.assertTrue(source.execute("SELECT 1 FROM sqlite_master WHERE name='office_schema'").fetchone())
            self.snapshot.write_bytes(migrated.read_bytes())
        finally: source.close()
        self.assertFalse(any(Path(str(self.snapshot) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')))
        before = self.requests()
        status, report = self.invoke(True, True)
        self.assertEqual((status, report['retired_count']), (0, 3))
        self.assertEqual([row for row in self.requests() if row[3] == 'COMPLETE'], [row for row in before if row[3] == 'COMPLETE'])

    def test_exact_old_and_current_direct_or_json_provenance_links_refuse(self):
        original = self.snapshot.read_bytes()
        cases = (
            ('archive', 'CREATE TABLE robot_jobs(operation TEXT PRIMARY KEY,cycle TEXT,state TEXT)',
                'INSERT INTO robot_jobs VALUES(?,?,?)', (self.opaque, 'old', 'COMPLETE')),
            ('archive', 'CREATE TABLE analysis_checks(operation TEXT PRIMARY KEY,day TEXT,state TEXT,result TEXT)',
                'INSERT INTO analysis_checks VALUES(?,?,?,?)', (self.opaque, 'day', 'COMPLETE', '{}')),
            ('archive', 'CREATE TABLE robot_setups(id TEXT PRIMARY KEY,cycle TEXT,plan TEXT)',
                'INSERT INTO robot_setups VALUES(?,?,?)', (self.opaque, 'old', None)),
            ('archive', 'CREATE TABLE robot_simulations(setup_id TEXT,scenario TEXT,ticket_sha256 TEXT,result TEXT,created_at TEXT,last_requested_at TEXT)',
                'INSERT INTO robot_simulations VALUES(?,?,?,?,?,?)',
                ('other', 'SCENARIO', 'hash', json.dumps({'operation': self.opaque}), 'time', 'time')),
            ('archive', 'CREATE TABLE cycles(day TEXT PRIMARY KEY,state TEXT,created REAL,data TEXT)',
                'INSERT INTO cycles VALUES(?,?,?,?)', ('other', 'COMPLETE', 1, json.dumps({'operation': self.opaque}))),
            ('live', None, 'INSERT INTO robot_jobs VALUES(?,?,?)', (self.opaque, 'current', 'COMPLETE')),
            ('live', None, 'INSERT INTO robot_candidates VALUES(?,?,?,?)',
                ('other', 'current', 'REJECTED', json.dumps({'provenance': {'operation': self.opaque}}))),
        )
        for location, create, command, values in cases:
            with self.subTest(location=location, command=command):
                if location == 'archive':
                    if create: self.archive_change(create)
                    self.archive_change(command, values)
                else: self.db.execute(command, values)
                before = self.requests()
                status, error = self.invoke(True, True)
                self.assertEqual((status, error['error']), (1, 'PRE_ORDER_REQUEST_REFERENCED'))
                self.unchanged(before)
                self.snapshot.write_bytes(original)
                self.db.execute('DELETE FROM robot_jobs'); self.db.execute('DELETE FROM robot_candidates')

    def test_second_unknown_or_current_request_never_shares_the_proof_exception(self):
        for operation in ('second-private-opaque', '2026-10-06:robot-v8:0:screening:0:1', '2026-10-06:robot-v9:12:analysis-v9:SOLUSDT'):
            with self.subTest(operation=operation):
                self.insert(operation, 'NEEDS_REVIEW', 'INVALID_SCREENING_SYMBOL')
                before = self.requests()
                status, error = self.invoke(True, True)
                self.assertEqual((status, error['error']), (1, 'PRE_ORDER_CANDIDATE_REQUIRED'))
                self.unchanged(before)
                self.db.execute('DELETE FROM api_requests WHERE operation=?', (operation,))

    def test_pending_on_job_cycle_or_live_order_evidence_still_refuses(self):
        for mutation, expected in (
                ("UPDATE robot_settings SET enabled=1", 'ROBOT_OFF_REQUIRED'),
                ("INSERT INTO robot_jobs VALUES('job','cycle','NEEDS_REVIEW')", 'ROBOT_JOB_UNRESOLVED'),
                ("INSERT INTO robot_cycles VALUES('cycle','day','NEEDS_REVIEW','{}')", 'ROBOT_CYCLE_UNRESOLVED'),
                ("INSERT INTO order_intents VALUES('order','candidate','SUBMITTING','{}')", 'ORDER_EVIDENCE_PRESENT'),
                ("INSERT INTO robot_entry_receipts VALUES('receipt','HYPEUSDT')", 'ORDER_EVIDENCE_PRESENT')):
            with self.subTest(mutation=mutation):
                self.db.execute(mutation)
                before = self.requests()
                status, error = self.invoke(True, True)
                self.assertEqual((status, error['error']), (1, expected))
                self.unchanged(before)
                self.db.execute('UPDATE robot_settings SET enabled=0')
                for table in ('robot_jobs', 'robot_cycles', 'order_intents', 'robot_entry_receipts'): self.db.execute('DELETE FROM ' + table)
        self.insert('pending-private', 'PENDING')
        before = self.requests()
        status, error = self.invoke(True, True)
        self.assertEqual((status, error['error']), (1, 'PENDING_REQUEST_PRESENT'))
        self.unchanged(before)

    def test_invalid_candidate_properties_are_never_inferred_from_date_or_filename(self):
        original = dict(zip((row[1] for row in self.db.execute('PRAGMA table_info(api_requests)')),
            self.db.execute('SELECT * FROM api_requests WHERE operation=?', (self.opaque,)).fetchone()))
        for field, value in (('output', '{}'), ('attempts', 2), ('body_hash', SECRET), ('created', -1),
                ('failure_code', 'NETWORK_UNCERTAIN'), ('operation', ''), ('operation', 'private:opaque'),
                ('operation', 'private-robot-v1-marker'), ('operation', 'private-analysis-v9-marker')):
            with self.subTest(field=field, value=value):
                self.db.execute('UPDATE api_requests SET ' + field + '=? WHERE operation=?', (value, self.opaque))
                before = self.requests()
                status, error = self.invoke(True, True)
                self.assertEqual((status, error['error']), (1, 'PRE_ORDER_CANDIDATE_UNSUPPORTED'))
                self.unchanged(before)
                self.db.execute('UPDATE api_requests SET ' + field + '=? WHERE operation=?',
                    (original[field], value if field == 'operation' else self.opaque))

    def test_existing_opaque_archive_entry_is_a_conflict_before_backup_or_state_changes(self):
        self.db.execute('''CREATE TABLE legacy_research_archive(operation TEXT PRIMARY KEY,
            original_row BLOB NOT NULL,row_sha256 TEXT NOT NULL,scope TEXT NOT NULL,retired_at TEXT NOT NULL)''')
        self.db.execute('INSERT INTO legacy_research_archive VALUES(?,?,?,?,?)',
            (self.opaque, b'private previous marker', 'a' * 64, 'pre_order_archive:' + tool.checksum(self.snapshot.read_bytes()), 'old'))
        before, names = self.requests(), self.names()
        status, report = self.invoke(prove=True)
        self.assertEqual(status, 0)
        self.assertFalse(report['can_apply'])
        self.assertIn('RETIREMENT_MARKER_CONFLICT', report['refusal_reasons'])
        status, error = self.invoke(True, True)
        self.assertEqual((status, error['error']), (1, 'RETIREMENT_MARKER_CONFLICT'))
        self.assertEqual(self.requests(), before)
        self.assertEqual(self.names(), names)

    def test_archive_replacement_or_in_place_change_between_backup_and_writer_refuses(self):
        original_backup = tool.create_backup
        for replacement in (True, False):
            with self.subTest(replacement=replacement):
                original = self.snapshot.read_bytes()
                before = self.requests()
                def race(*args, **kwargs):
                    saved = original_backup(*args, **kwargs)
                    if replacement:
                        changed = self.trading / 'replacement.sqlite3'
                        changed.write_bytes(original); changed.chmod(0o600); changed.replace(self.snapshot)
                    else: self.archive_change('UPDATE events SET message=?', (SECRET + ' changed',))
                    return saved
                with patch.object(tool, 'create_backup', side_effect=race): status, error = self.invoke(True, True)
                self.assertEqual((status, error['error']), (1, 'PRE_ORDER_ARCHIVE_CHANGED'))
                self.unchanged(before)
                self.snapshot.write_bytes(original)

    def test_archive_change_after_state_updates_rolls_back_every_row_and_marker(self):
        before, original_apply = self.requests(), tool.apply_rows
        def changed(db, observation):
            original_apply(db, observation)
            self.archive_change('UPDATE events SET message=?', ('concurrent changed snapshot',))
        with patch.object(tool, 'apply_rows', side_effect=changed): status, error = self.invoke(True, True)
        self.assertEqual((status, error['error']), (1, 'PRE_ORDER_ARCHIVE_CHANGED'))
        self.unchanged(before)

    def test_marker_requires_unchanged_original_row_and_snapshot_on_repeat(self):
        self.invoke(True, True)
        self.db.execute('UPDATE legacy_research_archive SET row_sha256=? WHERE operation=?', ('b' * 64, self.opaque))
        before = self.requests()
        status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'RETIREMENT_MARKER_INVALID'))
        self.assertEqual(self.requests(), before)
        self.db.execute('UPDATE legacy_research_archive SET row_sha256=lower(hex(randomblob(32))) WHERE operation=?', (self.opaque,))
        self.archive_change('UPDATE events SET message=?', ('snapshot changed',))
        status, error = self.invoke(True)
        self.assertEqual((status, error['error']), (1, 'RETIREMENT_MARKER_INVALID'))

    def test_archive_sidecar_symlink_hardlink_public_permissions_and_bad_integrity_refuse(self):
        original = self.snapshot.read_bytes()
        before = self.requests()
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(self.snapshot) + suffix); sidecar.write_bytes(b'private')
            status, error = self.invoke(True, True)
            self.assertEqual((status, error['error']), (1, 'PRE_ORDER_ARCHIVE_SIDECAR_PRESENT'))
            self.unchanged(before); sidecar.unlink()
        linked = self.trading / 'linked.sqlite3'; os.link(self.snapshot, linked)
        status, error = self.invoke(True, True)
        self.assertEqual((status, error['error']), (1, 'UNSAFE_STATE_FILE')); linked.unlink()
        self.snapshot.chmod(0o644)
        status, error = self.invoke(True, True)
        self.assertEqual((status, error['error']), (1, 'PRE_ORDER_ARCHIVE_NOT_PRIVATE')); self.snapshot.chmod(0o600)
        self.snapshot.unlink(); self.snapshot.symlink_to(self.path)
        status, error = self.invoke(True, True)
        self.assertEqual((status, error['error']), (1, 'UNSAFE_STATE_FILE'))
        self.snapshot.unlink(); self.snapshot.write_bytes(original); self.snapshot.chmod(0o600)
        with self.snapshot.open('r+b') as archive: archive.seek(100); archive.write(b'\xff' * 100)
        status, _ = self.invoke(True, True)
        self.assertEqual(status, 1)
        self.unchanged(before)


class IdleShadowArchiveTests(unittest.TestCase):
    insert = RetirementTests.insert
    requests = RetirementTests.requests
    names = RetirementTests.names
    invoke = RetirementTests.invoke
    unchanged = RetirementTests.unchanged
    archive_change = PreOrderProofTests.archive_change
    make_snapshot = PreOrderProofTests.make_snapshot

    def setUp(self):
        PreOrderProofTests.setUp(self)
        self.plans = [historical_shadow_plan(symbol) for symbol in ('BTCUSDT', 'ETHUSDT')]
        with sqlite3.connect(self.snapshot) as archive:
            archive.execute(SHADOW_SQL)
            for plan in self.plans:
                archive.execute('INSERT INTO shadow_plans VALUES(?,?,?,?,?,?)', self.shadow_row(plan))

    @staticmethod
    def shadow_row(plan):
        return (plan['setup_id'], plan['business_day'], plan['symbol'], json.dumps(plan),
            plan['status'], json.dumps(SHADOW_MODEL))

    def plan_change(self, update):
        plan = copy.deepcopy(self.plans[0])
        update(plan)
        self.archive_change('UPDATE shadow_plans SET plan=? WHERE setup_id=?',
            (json.dumps(plan), self.plans[0]['setup_id']))

    def model_change(self, update):
        model = dict(SHADOW_MODEL)
        update(model)
        self.archive_change('UPDATE shadow_plans SET model=? WHERE setup_id=?',
            (json.dumps(model), self.plans[0]['setup_id']))

    def rename_opaque(self, operation):
        self.db.execute('UPDATE api_requests SET operation=? WHERE operation=?', (operation, self.opaque))
        self.archive_change('UPDATE api_requests SET operation=? WHERE operation=?', (operation, self.opaque))
        self.opaque = operation

    def refusal_preserves_everything(self, expected='PRE_ORDER_ARCHIVE_ORDER_EVIDENCE_PRESENT'):
        before, names, snapshot = self.requests(), self.names(), self.snapshot.read_bytes()
        status, error = self.invoke(True, True)
        self.assertEqual((status, error), (1, {'error': expected}))
        self.unchanged(before)
        self.assertEqual(self.names(), names)
        self.assertEqual(self.snapshot.read_bytes(), snapshot)
        self.assertFalse((self.trading / 'legacy-research-backups').exists())

    def test_full_proof_apply_and_repeats_preserve_archive_shadow_manual_cache_and_no_replay(self):
        for number in range(10): self.insert('2026-10-06:analysis-v6:TEST' + str(number) + 'USDT', 'COMPLETE')
        before, names, snapshot = self.requests(), self.names(), self.snapshot.read_bytes()
        status, inspect = self.invoke(prove=True)
        self.assertEqual(status, 0)
        self.assertTrue(inspect['can_apply'])
        self.assertEqual(inspect['eligible_count'], 3)
        self.assertEqual((self.requests(), self.names(), self.snapshot.read_bytes()), (before, names, snapshot))
        status, report = self.invoke(True, True)
        self.assertEqual((status, report['retired_count']), (0, 3))
        self.assertEqual(self.snapshot.read_bytes(), snapshot)
        self.assertEqual([row for row in self.requests() if row[3] == 'COMPLETE'],
            [row for row in before if row[3] == 'COMPLETE'])
        self.assertEqual(self.db.execute('SELECT state,COUNT(*) FROM api_requests GROUP BY state').fetchall(),
            [('COMPLETE', 12), ('RETIRED_LEGACY', 3)])
        self.assertEqual(self.db.execute('SELECT enabled,risk FROM robot_settings').fetchone(), (0, '5'))
        self.assertEqual(self.db.execute('SELECT data FROM office_cache').fetchone(), ('HYPEUSDT manual exposure untouched',))
        for table in ('robot_jobs', 'robot_candidates', 'order_intents', 'robot_entry_receipts'):
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM ' + table).fetchone(), (0,))
        with sqlite3.connect(report['backup']) as backup: self.assertEqual(self.requests(backup), before)
        archived = self.db.execute('SELECT * FROM legacy_research_archive ORDER BY operation').fetchall()
        names = self.names()
        for prove in (False, True):
            status, inspect = self.invoke(prove=prove)
            self.assertEqual((status, inspect['pre_order_proof']['matched_count']), (0, 1))
            self.assertEqual(self.invoke(True, prove), (0, dict(status='UNCHANGED', retired_count=0)))
        self.assertEqual(self.names(), names)
        self.assertEqual(self.db.execute('SELECT * FROM legacy_research_archive ORDER BY operation').fetchall(), archived)
        self.assertEqual(self.snapshot.read_bytes(), snapshot)

    def test_frozen_vps_marker_format_is_readable_without_installer_or_migration(self):
        # The ephemeral VPS addon wrote the unchanged base helper's typed payload,
        # scope and timestamp. Seed that format independently of current apply().
        columns = tuple(row[1] for row in self.db.execute('PRAGMA table_info(api_requests)'))
        original = self.requests()
        self.db.execute('''CREATE TABLE legacy_research_archive(operation TEXT PRIMARY KEY,
            original_row BLOB NOT NULL,row_sha256 TEXT NOT NULL,scope TEXT NOT NULL,retired_at TEXT NOT NULL)''')
        for row in original:
            if row[3] != 'NEEDS_REVIEW': continue
            payload = tool.encoded(columns, row)
            scope = ('pre_order_archive:' + hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
                if row[0] == self.opaque else tool.classify(row[0])[0])
            self.db.execute('INSERT INTO legacy_research_archive VALUES(?,?,?,?,?)',
                (row[0], payload, hashlib.sha256(payload).hexdigest(), scope, '2026-10-07T05:20:00+00:00'))
            self.db.execute("UPDATE api_requests SET state='RETIRED_LEGACY' WHERE operation=?", (row[0],))
        before, names, snapshot = self.requests(), self.names(), self.snapshot.read_bytes()
        for prove in (False, True):
            status, inspect = self.invoke(prove=prove)
            self.assertEqual((status, inspect['pre_order_proof']['matched_count']), (0, 1))
            self.assertEqual(inspect['eligible_count'], 0)
            self.assertEqual(inspect['unresolved_request_count'], 0)
            self.assertEqual(self.invoke(True, prove), (0, dict(status='UNCHANGED', retired_count=0)))
        self.assertEqual((self.requests(), self.names(), self.snapshot.read_bytes()), (before, names, snapshot))
        self.assertFalse((self.trading / 'legacy-research-backups').exists())

    def test_live_flag(self):
        self.plan_change(lambda plan: plan.update(live_execution=True))
        self.refusal_preserves_everything()

    def test_false_plan_flag_must_be_boolean(self):
        self.plan_change(lambda plan: plan.update(would_submit=0))
        self.refusal_preserves_everything()

    def test_default_model_only_flag_is_required(self):
        self.model_change(lambda model: model.update(model_only=False))
        self.refusal_preserves_everything()

    def test_false_model_flag_must_be_boolean(self):
        self.model_change(lambda model: model.update(fill_confirmed=0))
        self.refusal_preserves_everything()

    def test_started_execution_model_is_not_an_idle_plan(self):
        self.model_change(lambda model: model.update(state='ENTRY_SUBMITTED'))
        self.refusal_preserves_everything()

    def test_unknown_schema(self):
        self.archive_change('ALTER TABLE shadow_plans ADD COLUMN unknown TEXT')
        self.refusal_preserves_everything()

    def test_nonempty_malformed_shadow_table_retains_original_evidence_refusal(self):
        self.archive_change('DROP TABLE shadow_plans')
        self.archive_change('CREATE TABLE shadow_plans(id TEXT)')
        self.archive_change('INSERT INTO shadow_plans VALUES(?)', ('unknown shadow evidence',))
        self.refusal_preserves_everything()

    def test_shadow_plan_client_id_reference_blocks_opaque_retirement(self):
        self.rename_opaque(self.plans[0]['client_ids']['ENTRY'])
        self.refusal_preserves_everything('PRE_ORDER_REQUEST_REFERENCED')

    def test_shadow_model_state_reference_blocks_opaque_retirement(self):
        self.rename_opaque('PLAN_READY')
        self.refusal_preserves_everything('PRE_ORDER_REQUEST_REFERENCED')

    def test_current_valid_shadow_plan_reference_is_checked_too(self):
        plans = [historical_shadow_plan(symbol) for symbol in ('SOLUSDT', 'ADAUSDT')]
        self.db.execute(SHADOW_SQL)
        self.db.executemany('INSERT INTO shadow_plans VALUES(?,?,?,?,?,?)', [self.shadow_row(plan) for plan in plans])
        self.rename_opaque(plans[0]['client_ids']['ENTRY'])
        self.refusal_preserves_everything('PRE_ORDER_REQUEST_REFERENCED')

    def test_unknown_top_level_receipt_claim(self):
        self.plan_change(lambda plan: plan.update(order_id='123'))
        self.refusal_preserves_everything()

    def test_receipt_hidden_in_reward_ratio(self):
        self.plan_change(lambda plan: plan.update(actual_RR={'orderId': 123}))
        self.refusal_preserves_everything()

    def test_nested_receipt_claim(self):
        self.plan_change(lambda plan: plan['entry_order']['payload'].update(orderId=123))
        self.refusal_preserves_everything()

    def test_duplicate_json_flags(self):
        value = '{"would_submit":true,' + json.dumps(self.plans[0])[1:]
        self.archive_change('UPDATE shadow_plans SET plan=? WHERE setup_id=?', (value, self.plans[0]['setup_id']))
        self.refusal_preserves_everything()

    def test_nonfinite_json(self):
        self.plan_change(lambda plan: plan.update(failure_code=float('nan')))
        self.refusal_preserves_everything()

    def test_unvalidated_level_identity(self):
        self.plan_change(lambda plan: plan.update(TP='103'))
        self.refusal_preserves_everything()

    def test_implemented_reconciliation(self):
        self.plan_change(lambda plan: plan['future_reconciliation'].update(implemented=True))
        self.refusal_preserves_everything()

    def test_single_plan_does_not_expand_two_row_exception(self):
        self.archive_change('DELETE FROM shadow_plans WHERE setup_id=?', (self.plans[0]['setup_id'],))
        self.refusal_preserves_everything()

    def test_missing_opaque_api_row_is_not_proved_by_shadow_plan(self):
        self.archive_change('DELETE FROM api_requests WHERE operation=?', (self.opaque,))
        self.refusal_preserves_everything('PRE_ORDER_REQUEST_NOT_IN_ARCHIVE')

    def test_on_still_refuses_before_backup(self):
        self.db.execute('UPDATE robot_settings SET enabled=1')
        self.refusal_preserves_everything('ROBOT_OFF_REQUIRED')

    def test_current_pending_intent_still_refuses_before_backup(self):
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?)', ('owned', 'candidate', 'ENTRY_PENDING', '{}'))
        self.refusal_preserves_everything('ORDER_EVIDENCE_PRESENT')

    def test_archived_real_receipt_still_refuses_before_backup(self):
        self.archive_change('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)', ('actual', 'BTCUSDT', '2026-10-05', 'now'))
        self.refusal_preserves_everything()


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
