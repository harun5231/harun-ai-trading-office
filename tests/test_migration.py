"""Retirement preserves exchange/request evidence and refuses incomplete backups."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from worker.migration import retire_previous_runtime, upgrade_cycle_namespaces


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / 'ledger.sqlite3'
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.executescript('''
            CREATE TABLE trades(id TEXT PRIMARY KEY, plan TEXT);
            INSERT INTO trades VALUES('previous', 'old plan');
            CREATE TABLE live_actions(id TEXT PRIMARY KEY, state TEXT);
            INSERT INTO live_actions VALUES('old action', 'PENDING');
            CREATE TABLE robot_simulations(id TEXT PRIMARY KEY, result TEXT);
            INSERT INTO robot_simulations VALUES('old example', 'old result');
            CREATE TABLE api_requests(operation TEXT PRIMARY KEY, state TEXT);
            INSERT INTO api_requests VALUES('paid unknown', 'NEEDS_REVIEW');
            CREATE TABLE robot_entry_receipts(id TEXT PRIMARY KEY, symbol TEXT);
            INSERT INTO robot_entry_receipts VALUES('real receipt', 'BTCUSDT');
            CREATE TABLE robot_settings(id INTEGER PRIMARY KEY, enabled INTEGER, risk TEXT);
            INSERT INTO robot_settings VALUES(1, 1, '20');
            CREATE TABLE operator_audit(data TEXT);
            INSERT INTO operator_audit VALUES('untouched');
        ''')
        self.archive = self.root / 'ledger-pre-order.sqlite3'

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def tables(self):
        return {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def unchanged(self):
        self.assertTrue({'trades', 'live_actions', 'robot_simulations'}.issubset(self.tables()))
        self.assertNotIn('office_schema', self.tables())
        self.assertEqual(self.db.execute('SELECT enabled,risk FROM robot_settings').fetchone(), (1, '20'))

    def make_archive(self):
        output = sqlite3.connect(self.archive)
        try:
            self.db.backup(output)
        finally:
            output.close()

    def test_first_migration_preserves_evidence_and_removes_only_retired_data(self):
        for name in ('snapshot.json', 'live-status.json'):
            (self.root / name).write_text('retired cache')
        unknown = self.root / 'operator-note.txt'
        unknown.write_text('retain')
        retire_previous_runtime(self.db)
        self.assertFalse({'trades', 'live_actions', 'robot_simulations'} & self.tables())
        self.assertEqual(self.db.execute('SELECT * FROM api_requests').fetchall(), [('paid unknown', 'NEEDS_REVIEW')])
        self.assertEqual(self.db.execute('SELECT * FROM robot_entry_receipts').fetchall(), [('real receipt', 'BTCUSDT')])
        self.assertEqual(self.db.execute('SELECT enabled,risk FROM robot_settings').fetchone(), (0, '5'))
        self.assertEqual(self.db.execute('SELECT * FROM operator_audit').fetchall(), [('untouched',)])
        for name in ('snapshot.json', 'live-status.json'):
            self.assertFalse((self.root / name).exists())
        self.assertEqual(unknown.read_text(), 'retain')
        output = sqlite3.connect(self.archive)
        try:
            self.assertEqual(output.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(output.execute('SELECT * FROM trades').fetchall(), [('previous', 'old plan')])
            self.assertEqual(output.execute('SELECT * FROM live_actions').fetchall(), [('old action', 'PENDING')])
        finally:
            output.close()

    def test_restart_never_overwrites_backup_or_disables_robot_again(self):
        self.make_archive()
        before = (self.archive.stat().st_ino, self.archive.read_bytes())
        retire_previous_runtime(self.db)
        self.db.execute('UPDATE robot_settings SET enabled=1')
        retire_previous_runtime(self.db)
        self.assertEqual(self.db.execute('SELECT enabled FROM robot_settings').fetchone()[0], 1)
        self.assertEqual((self.archive.stat().st_ino, self.archive.read_bytes()), before)

    def test_backup_includes_committed_source_wal_evidence(self):
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute("INSERT INTO api_requests VALUES('latest paid', 'PENDING')")
        self.db.execute("INSERT INTO trades VALUES('latest prior', 'wal plan')")
        self.assertTrue(self.path.with_name('ledger.sqlite3-wal').exists())
        retire_previous_runtime(self.db)
        output = sqlite3.connect(self.archive)
        try:
            self.assertEqual(output.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(output.execute("SELECT state FROM api_requests WHERE operation='latest paid'").fetchone(), ('PENDING',))
            self.assertEqual(output.execute("SELECT plan FROM trades WHERE id='latest prior'").fetchone(), ('wal plan',))
        finally:
            output.close()

    def test_empty_existing_backup_cannot_authorize_table_removal(self):
        self.archive.write_bytes(b'')
        with self.assertRaisesRegex(RuntimeError, '^WORKER_ARCHIVE_INVALID$'):
            retire_previous_runtime(self.db)
        self.unchanged()
        self.assertEqual(self.archive.read_bytes(), b'')

    def test_corrupt_existing_backup_cannot_authorize_table_removal(self):
        self.archive.write_bytes(b'interrupted backup')
        with self.assertRaisesRegex(RuntimeError, '^WORKER_ARCHIVE_INVALID$'):
            retire_previous_runtime(self.db)
        self.unchanged()

    def test_valid_sqlite_missing_retired_tables_is_refused(self):
        output = sqlite3.connect(self.archive)
        output.execute('CREATE TABLE trades(id TEXT PRIMARY KEY, plan TEXT)')
        output.close()
        with self.assertRaisesRegex(RuntimeError, '^WORKER_ARCHIVE_INVALID$'):
            retire_previous_runtime(self.db)
        self.unchanged()

    def test_backup_with_fewer_rows_than_active_source_is_refused(self):
        self.make_archive()
        self.db.execute("INSERT INTO trades VALUES('later', 'do not discard')")
        with self.assertRaisesRegex(RuntimeError, '^WORKER_ARCHIVE_INVALID$'):
            retire_previous_runtime(self.db)
        self.unchanged()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM trades').fetchone()[0], 2)

    def test_backup_missing_real_receipts_is_refused(self):
        self.make_archive()
        output = sqlite3.connect(self.archive)
        output.execute('DELETE FROM robot_entry_receipts')
        output.commit()
        output.close()
        with self.assertRaisesRegex(RuntimeError, '^WORKER_ARCHIVE_INVALID$'):
            retire_previous_runtime(self.db)
        self.unchanged()

    def test_backup_links_and_special_files_are_refused(self):
        for kind in ('symlink', 'hardlink', 'fifo'):
            with self.subTest(kind=kind):
                target = self.root / ('private-' + kind)
                target.write_bytes(b'never change')
                if kind == 'symlink':
                    self.archive.symlink_to(target)
                elif kind == 'hardlink':
                    os.link(target, self.archive)
                else:
                    os.mkfifo(self.archive)
                try:
                    with self.assertRaisesRegex(RuntimeError, '^WORKER_ARCHIVE_INVALID$'):
                        retire_previous_runtime(self.db)
                    self.unchanged()
                    self.assertEqual(target.read_bytes(), b'never change')
                finally:
                    self.archive.unlink()

    def test_interrupted_backup_stays_fail_closed_on_restart(self):
        db = self.db

        class InterruptedBackup:
            def execute(self, *args):
                return db.execute(*args)

            def backup(self, output):
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            retire_previous_runtime(InterruptedBackup())
        self.unchanged()
        with self.assertRaisesRegex(RuntimeError, '^WORKER_ARCHIVE_INVALID$'):
            retire_previous_runtime(self.db)
        self.unchanged()

    def test_retired_cache_link_rejects_before_backup_or_any_deletion(self):
        target = self.root / 'private-note'
        target.write_bytes(b'retain')
        first = self.root / 'snapshot.json'
        first.write_bytes(b'old cache')
        (self.root / 'live-status.json').symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, '^WORKER_RETIRED_CACHE_INVALID$'):
            retire_previous_runtime(self.db)
        self.unchanged()
        self.assertFalse(self.archive.exists())
        self.assertEqual(target.read_bytes(), b'retain')
        self.assertEqual(first.read_bytes(), b'old cache')

    def test_fresh_database_cleanup_does_not_create_a_backup(self):
        self.db.close()
        self.path.unlink()
        self.db = sqlite3.connect(self.path, isolation_level=None)
        (self.root / 'snapshot.json').write_bytes(b'old cache')
        retire_previous_runtime(self.db)
        self.assertIn('office_schema', self.tables())
        self.assertFalse(self.archive.exists())
        self.assertFalse((self.root / 'snapshot.json').exists())


class CycleNamespaceUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'ledger.sqlite3'
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.executescript('''
            CREATE TABLE office_schema(version INTEGER NOT NULL);
            INSERT INTO office_schema VALUES(1);
            CREATE TABLE robot_cycles(id TEXT PRIMARY KEY,day TEXT NOT NULL,entry_epoch INTEGER NOT NULL,
                state TEXT NOT NULL,data TEXT NOT NULL,UNIQUE(day,entry_epoch));
            INSERT INTO robot_cycles(rowid,id,day,entry_epoch,state,data)
                VALUES(42,'2026-10-06:robot-v8:0','2026-10-06',0,'ACTIVE','old gross-risk cycle');
            CREATE TABLE robot_settings(id INTEGER PRIMARY KEY,enabled INTEGER,risk TEXT);
            INSERT INTO robot_settings VALUES(1,1,'13.37');
            CREATE TABLE api_requests(operation TEXT PRIMARY KEY,state TEXT,output TEXT);
            INSERT INTO api_requests VALUES('old paid request','NEEDS_REVIEW','original proof');
            CREATE TABLE order_intents(id TEXT PRIMARY KEY,state TEXT,payload TEXT);
            INSERT INTO order_intents VALUES('old submitted','SUBMITTING','original intent');
            INSERT INTO order_intents VALUES('old unknown','NEEDS_REVIEW','original unknown');
            CREATE TABLE robot_candidates(id TEXT PRIMARY KEY,status TEXT,plan TEXT);
            INSERT INTO robot_candidates VALUES('old gross candidate','READY_FOR_EXECUTION','immutable old plan');
            CREATE TABLE robot_entry_receipts(id TEXT PRIMARY KEY,symbol TEXT);
            INSERT INTO robot_entry_receipts VALUES('real fill','BTCUSDT');
            CREATE TABLE office_cache(id INTEGER PRIMARY KEY,data TEXT);
            INSERT INTO office_cache VALUES(1,'manual exposure HYPEUSDT');
            CREATE TABLE operator_audit(data TEXT);
        ''')

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def snapshot(self):
        names = ('robot_cycles', 'robot_settings', 'api_requests', 'order_intents',
                 'robot_candidates', 'robot_entry_receipts', 'office_cache', 'operator_audit')
        return {name: self.db.execute('SELECT rowid,* FROM ' + name).fetchall() for name in names}

    def test_upgrade_preserves_every_row_and_allows_v9_at_same_epoch(self):
        before = self.snapshot()
        upgrade_cycle_namespaces(self.db)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.db.execute('SELECT version FROM office_schema').fetchone(), (2,))
        self.db.execute("INSERT INTO robot_cycles VALUES('2026-10-06:robot-v9:0','2026-10-06',0,'ACTIVE','new fee-inclusive cycle')")
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_cycles').fetchone()[0], 2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO robot_cycles VALUES('2026-10-06:robot-v9:0','2026-10-07',1,'ACTIVE','duplicate ID')")

    def test_upgrade_is_idempotent_and_never_resets_settings(self):
        upgrade_cycle_namespaces(self.db)
        before = self.snapshot()
        upgrade_cycle_namespaces(self.db)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.db.execute('SELECT enabled,risk FROM robot_settings').fetchone(), (1, '13.37'))
        self.assertEqual(self.db.execute('SELECT version FROM office_schema').fetchall(), [(2,)])

    def test_ordinary_indexes_and_triggers_survive_without_running_during_copy(self):
        self.db.executescript('''
            CREATE INDEX robot_cycle_state ON robot_cycles(state) WHERE state='ACTIVE';
            CREATE TRIGGER cycle_updated AFTER UPDATE ON robot_cycles
                BEGIN INSERT INTO operator_audit VALUES(NEW.id); END;
        ''')
        upgrade_cycle_namespaces(self.db)
        self.assertEqual(self.db.execute('SELECT * FROM operator_audit').fetchall(), [])
        definitions = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE tbl_name='robot_cycles'")}
        self.assertIn('robot_cycle_state', definitions)
        self.assertIn('cycle_updated', definitions)
        self.db.execute("UPDATE robot_cycles SET state='COMPLETE' WHERE id='2026-10-06:robot-v8:0'")
        self.assertEqual(self.db.execute('SELECT * FROM operator_audit').fetchall(), [('2026-10-06:robot-v8:0',)])

    def test_explicit_day_epoch_unique_index_is_removed(self):
        self.db.execute('CREATE UNIQUE INDEX extra_cycle_epoch ON robot_cycles(entry_epoch,day)')
        upgrade_cycle_namespaces(self.db)
        self.db.execute("INSERT INTO robot_cycles VALUES('2026-10-06:robot-v9:0','2026-10-06',0,'ACTIVE','new fee-inclusive cycle')")
        indexes = {row[1] for row in self.db.execute('PRAGMA index_list(robot_cycles)')}
        self.assertNotIn('extra_cycle_epoch', indexes)

    def test_interruption_rolls_back_original_table_and_schema_flag(self):
        before = self.snapshot()
        db = self.db

        class InterruptedUpgrade:
            def execute(self, sql, *args):
                if sql.startswith('ALTER TABLE robot_cycles_namespace_upgrade'):
                    raise KeyboardInterrupt()
                return db.execute(sql, *args)

        with self.assertRaises(KeyboardInterrupt):
            upgrade_cycle_namespaces(InterruptedUpgrade())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.db.execute('SELECT version FROM office_schema').fetchone(), (1,))
        names = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('robot_cycles_namespace_upgrade', names)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO robot_cycles VALUES('2026-10-06:robot-v9:0','2026-10-06',0,'ACTIVE','still old unique constraint')")
        upgrade_cycle_namespaces(self.db)
        self.db.execute("INSERT INTO robot_cycles VALUES('2026-10-06:robot-v9:0','2026-10-06',0,'ACTIVE','retry completed')")

    def test_absent_cycles_upgrade_does_not_create_or_reset_runtime_tables(self):
        self.db.execute('DROP TABLE robot_cycles')
        upgrade_cycle_namespaces(self.db)
        names = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('robot_cycles', names)
        self.assertEqual(self.db.execute('SELECT version FROM office_schema').fetchone(), (2,))
        self.assertEqual(self.db.execute('SELECT enabled,risk FROM robot_settings').fetchone(), (1, '13.37'))

    def test_fresh_store_creates_namespace_safe_cycles(self):
        from worker.robot_store import RobotStore

        self.db.close()
        self.path.unlink()
        self.db = sqlite3.connect(self.path, isolation_level=None)
        RobotStore(self.db)
        self.db.execute("INSERT INTO robot_cycles VALUES('2026-10-06:robot-v8:0','2026-10-06',0,'COMPLETE','retained namespace')")
        self.db.execute("INSERT INTO robot_cycles VALUES('2026-10-06:robot-v9:0','2026-10-06',0,'ACTIVE','new namespace')")
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_cycles').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT version FROM office_schema').fetchone(), (2,))

    def test_unknown_extended_cycle_schema_is_preserved_and_refused(self):
        self.db.execute('ALTER TABLE robot_cycles ADD COLUMN operator_note TEXT')
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, '^WORKER_CYCLE_SCHEMA_UNSUPPORTED$'):
            upgrade_cycle_namespaces(self.db)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.db.execute('SELECT version FROM office_schema').fetchone(), (1,))

    def test_referenced_cycles_never_trigger_foreign_key_deletion(self):
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('CREATE TABLE external_references(cycle TEXT REFERENCES robot_cycles(id) ON DELETE CASCADE)')
        self.db.execute("INSERT INTO external_references VALUES('2026-10-06:robot-v8:0')")
        with self.assertRaisesRegex(RuntimeError, '^WORKER_CYCLE_SCHEMA_UNSUPPORTED$'):
            upgrade_cycle_namespaces(self.db)
        self.assertEqual(self.db.execute('SELECT * FROM external_references').fetchall(), [('2026-10-06:robot-v8:0',)])
        self.assertEqual(self.db.execute('SELECT version FROM office_schema').fetchone(), (1,))


if __name__ == '__main__':
    unittest.main()
