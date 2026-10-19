"""Journal crash recovery without native source fixtures or application imports."""
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent import native_pending_review as pending


class PendingJournalInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='journal-init-tests-',
            dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.temporary.name)
        self.db = sqlite3.connect(self.root / 'ledger.sqlite3', isolation_level=None)
        self.db.execute('CREATE TABLE api_requests(operation TEXT PRIMARY KEY,state TEXT)')
        self.path = self.root / pending.JOURNAL_NAME
        # Any production import/provider invocation fails this independent suite.
        self.no_native = patch.object(pending, '_native', side_effect=AssertionError('NO_NATIVE_IMPORT'))
        self.no_native.start()

    def tearDown(self):
        self.no_native.stop()
        self.db.close()
        self.temporary.cleanup()

    def empty_file(self):
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)

    def fingerprint(self):
        metadata = self.path.lstat()
        return ((metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid,
                 stat.S_IMODE(metadata.st_mode), metadata.st_size, metadata.st_mtime_ns),
                hashlib.sha256(self.path.read_bytes()).hexdigest())

    def native_attempt(self, state='COMPLETE'):
        self.db.execute('INSERT INTO api_requests VALUES(?,?)',
                        ('pending-review-v1:' + 'a'*32 + ':1', state))

    def test_interrupted_after_exclusive_file_creation_is_read_only_absent_then_recoverable(self):
        self.empty_file()
        original = self.fingerprint()
        with pending._journal(self.db) as journal:
            self.assertIsNone(journal)
        self.assertEqual(self.fingerprint(), original)
        with pending._journal(self.db, create=True) as journal:
            self.assertEqual(journal.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(journal.execute('SELECT COUNT(*) FROM pending_reviews').fetchone()[0], 0)
        current = self.path.lstat()
        self.assertEqual((current.st_dev, current.st_ino), original[0][:2])
        self.assertEqual(stat.S_IMODE(current.st_mode), 0o600)
        with pending._journal(self.db) as journal:
            self.assertIsNotNone(journal)

    def test_real_process_exit_during_schema_transaction_recovers_without_prior_dispatch(self):
        self.empty_file()
        code = ('import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1],isolation_level=None);'
                "c.execute('BEGIN IMMEDIATE');c.execute(sys.argv[2]);"
                "c.execute('PRAGMA user_version=1');os._exit(91)")
        result = subprocess.run([sys.executable, '-I', '-B', '-S', '-c', code,
            str(self.path), pending._SCHEMA], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False)
        self.assertEqual(result.returncode, 91)
        with pending._journal(self.db) as journal:
            self.assertIsNone(journal)
        with pending._journal(self.db, create=True) as journal:
            self.assertEqual(journal.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(journal.execute('SELECT COUNT(*) FROM pending_reviews').fetchone()[0], 0)

    def test_empty_sqlite_header_without_schema_is_also_recoverable(self):
        self.empty_file()
        with sqlite3.connect(self.path) as connection:
            connection.execute('PRAGMA user_version=0')
        original = self.fingerprint()
        with pending._journal(self.db) as journal:
            self.assertIsNone(journal)
        self.assertEqual(self.fingerprint(), original)
        with pending._journal(self.db, create=True) as journal:
            self.assertEqual(journal.execute('PRAGMA user_version').fetchone()[0], 1)

    def test_new_initialization_fsyncs_file_and_parent_after_atomic_commit(self):
        original_fsync, observed = os.fsync, []
        def fsync(fd):
            metadata = os.fstat(fd)
            observed.append('directory' if stat.S_ISDIR(metadata.st_mode) else 'file')
            return original_fsync(fd)
        with patch.object(pending.os, 'fsync', side_effect=fsync):
            with pending._journal(self.db, create=True) as journal:
                self.assertEqual(journal.execute('PRAGMA user_version').fetchone()[0], 1)
        self.assertEqual(observed, ['file', 'directory'])

    def test_every_native_attempt_state_refuses_empty_reinitialization_and_preserves_file(self):
        self.empty_file()
        original = self.fingerprint()
        for state in ('PENDING', 'NEEDS_REVIEW', 'COMPLETE', 'REJECTED_REQUEST_VALIDATION', None):
            with self.subTest(state=state):
                self.db.execute('DELETE FROM api_requests')
                self.native_attempt(state)
                for create in (False, True):
                    with self.subTest(create=create):
                        with self.assertRaisesRegex(pending.PendingReviewUnavailable,
                                                    'INITIALIZATION_HAS_NATIVE_ATTEMPT'):
                            with pending._journal(self.db, create=create):
                                self.fail('Lost prior attempt must not become initialized')
                self.assertEqual(self.fingerprint(), original)

    def test_missing_journal_with_historical_native_attempt_refuses_without_creating_file(self):
        self.native_attempt()
        with self.assertRaises(pending.PendingReviewUnavailable):
            with pending._journal(self.db):
                self.fail('Missing prior journal must not appear unused')
        self.assertFalse(self.path.exists())

    def test_missing_create_with_native_attempt_refuses_before_creating_placeholder(self):
        self.native_attempt('PENDING')
        with self.assertRaises(pending.PendingReviewUnavailable):
            with pending._journal(self.db, create=True):
                self.fail('Missing journal must not discard unknown native attempt')
        self.assertFalse(self.path.exists())

    def test_version_one_missing_table_is_refused_even_without_api_attempt(self):
        self.empty_file()
        with sqlite3.connect(self.path) as connection:
            connection.execute('PRAGMA user_version=1')
        original = self.fingerprint()
        for create in (False, True):
            with self.assertRaisesRegex(pending.PendingReviewUnavailable, 'SCHEMA_UNVERIFIED'):
                with pending._journal(self.db, create=create):
                    self.fail('Version1 table loss must require review')
        self.assertEqual(self.fingerprint(), original)

    def test_unknown_schema_or_version_is_refused_and_never_deleted(self):
        for version, schema in ((0, 'CREATE TABLE foreign_history(value TEXT)'),
                                (0, pending._SCHEMA), (2, pending._SCHEMA)):
            with self.subTest(version=version, schema=schema):
                if self.path.exists():
                    self.path.unlink()  # Local test fixture only, never runtime code.
                self.empty_file()
                with sqlite3.connect(self.path) as connection:
                    connection.execute(schema)
                    connection.execute('PRAGMA user_version=' + str(version))
                original = self.fingerprint()
                for create in (False, True):
                    with self.assertRaisesRegex(pending.PendingReviewUnavailable, 'SCHEMA_UNVERIFIED'):
                        with pending._journal(self.db, create=create):
                            self.fail('Unknown journal may not be reset')
                self.assertEqual(self.fingerprint(), original)

    def test_version_one_initialized_journal_keeps_records_despite_native_attempts(self):
        with pending._journal(self.db, create=True) as journal:
            journal.execute('INSERT INTO pending_reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('evidence', 'native-intent', 'a'*64, 1, '2026-10-19T00:00:00+00:00',
                 1.0, 2.0, 'CANCEL_PREPARED', 'BATALKAN', '{}', 'b'*64, None, None))
        self.native_attempt('PENDING')
        before = self.fingerprint()
        for create in (False, True):
            with pending._journal(self.db, create=create) as journal:
                row = journal.execute('SELECT state FROM pending_reviews').fetchone()
                self.assertEqual(row['state'], 'CANCEL_PREPARED')
                self.assertEqual(journal.execute('PRAGMA user_version').fetchone()[0], 1)
        self.assertEqual(self.fingerprint(), before)

    def test_unrelated_native_research_rows_do_not_claim_review_was_dispatched(self):
        self.db.execute('INSERT INTO api_requests VALUES(?,?)',
                        ('2026-10-19:robot-v9:0:screening:1:2', 'COMPLETE'))
        self.empty_file()
        with pending._journal(self.db) as journal:
            self.assertIsNone(journal)
        with pending._journal(self.db, create=True) as journal:
            self.assertEqual(journal.execute('PRAGMA user_version').fetchone()[0], 1)

    def test_api_schema_missing_is_not_mistaken_for_proof_of_no_dispatch(self):
        self.empty_file()
        self.db.execute('DROP TABLE api_requests')
        original = self.fingerprint()
        for create in (False, True):
            with self.assertRaises(sqlite3.OperationalError):
                with pending._journal(self.db, create=create):
                    self.fail('Missing native API journal is no absence proof')
        self.assertEqual(self.fingerprint(), original)


if __name__ == '__main__':
    unittest.main()
