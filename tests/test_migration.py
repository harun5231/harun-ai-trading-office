"""Retirement preserves exchange/request evidence and refuses incomplete backups."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from worker.migration import retire_previous_runtime


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


if __name__ == '__main__':
    unittest.main()
