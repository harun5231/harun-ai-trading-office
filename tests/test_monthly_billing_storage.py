"""Adversarial storage tests use only disposable, valid synthetic financial DBs."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.test_monthly_billing import document
from worker.monthly_billing import Store


FILENAME = 'monthly-billing.sqlite3'


def financial_database(path):
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE ledger(id INTEGER PRIMARY KEY, amount TEXT NOT NULL)')
        db.execute("INSERT INTO ledger(amount) VALUES ('1234.56789012')")
        db.commit()
    return path.read_bytes()


def billing_database(path, *, wal=False):
    db = sqlite3.connect(path)
    try:
        db.execute('CREATE TABLE revisions (month TEXT NOT NULL, version INTEGER NOT NULL, recorded_at TEXT NOT NULL, document TEXT NOT NULL, PRIMARY KEY(month, version))')
        db.commit()
        if wal:
            assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    finally:
        db.close()
    assert path.read_bytes()[18:20] == (b'\x02\x02' if wal else b'\x01\x01')


def access(store, operation):
    if operation == 'save':
        return store.save(document(), 0)
    return getattr(store, operation)('2026-10')


def concurrent_save(directory, barrier, results):
    barrier.wait(timeout=10)
    try:
        result = Store(directory).save(document(), 0)
    except ValueError as error:
        result = str(error)
    results.put(result)


class BillingStorageSecurity(unittest.TestCase):
    def test_financial_aliases_are_never_written(self):
        for operation in ('read', 'history', 'save'):
            for alias in ('main', '-wal', '-shm', '-journal'):
                for link in ('hardlink', 'symlink'):
                    with self.subTest(operation=operation, alias=alias, link=link), tempfile.TemporaryDirectory() as root:
                        directory = Path(root) / 'billing'
                        directory.mkdir(mode=0o700)
                        ledger = Path(root) / 'financial.sqlite3'
                        original = financial_database(ledger)
                        path = directory / FILENAME
                        if alias != 'main':
                            # Real WAL prototype: disk SQLite can write linked WAL/SHM
                            # even when the caller only wants to read history.
                            billing_database(path, wal=alias in ('-wal', '-shm'))
                        target = path if alias == 'main' else Path(str(path) + alias)
                        (os.link if link == 'hardlink' else os.symlink)(ledger, target)
                        failure = None
                        try:
                            access(Store(directory), operation)
                        except Exception as error:
                            failure = error
                        # Check preservation BEFORE refusal, including SQLite errors.
                        self.assertEqual(ledger.read_bytes(), original, 'billing modified the financial inode')
                        with closing(sqlite3.connect(ledger)) as db:
                            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone(), ('ok',))
                            self.assertEqual(db.execute('SELECT amount FROM ledger').fetchall(), [('1234.56789012',)])
                            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('ledger',)])
                        if alias in ('main', '-wal', '-shm'):
                            self.assertIsInstance(failure, (ValueError, OSError), 'unsafe main/WAL prototype must be refused')
                        else:
                            self.assertIsNone(failure, 'valid DELETE-mode snapshot ignores rollback sidecars')

    def test_sidecar_aliases_are_ignored_for_valid_snapshot(self):
        for operation in ('read', 'history', 'save'):
            for suffix in ('-wal', '-shm', '-journal'):
                for link in ('hardlink', 'symlink'):
                    with self.subTest(operation=operation, suffix=suffix, link=link), tempfile.TemporaryDirectory() as root:
                        directory = Path(root) / 'billing'
                        directory.mkdir(mode=0o700)
                        ledger = Path(root) / 'financial.sqlite3'
                        original = financial_database(ledger)
                        store = Store(directory)
                        first = store.save(document(), 0)
                        before = store.path.read_bytes()
                        target = Path(str(store.path) + suffix)
                        (os.link if link == 'hardlink' else os.symlink)(ledger, target)
                        if operation == 'save':
                            second = store.save(document(), 1)
                            self.assertEqual(store.history('2026-10'), [first, second])
                        else:
                            self.assertEqual(access(store, operation), first if operation == 'read' else [first])
                            self.assertEqual(store.path.read_bytes(), before)
                        self.assertEqual(ledger.read_bytes(), original)
                        self.assertTrue(os.path.samefile(target, ledger))
                        self.assertEqual(sorted(p.name for p in directory.iterdir()), [FILENAME, FILENAME + suffix])

    def test_memory_connection_is_query_only_and_context_bound(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            first = store.save(document(), 0)
            before = store.path.read_bytes()
            with store.connect() as db:
                self.assertEqual(db.execute('PRAGMA database_list').fetchall(), [(0, 'main', '')])
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute('DELETE FROM revisions')
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(store.history('2026-10'), [first])

    def test_failed_saves_do_not_publish_or_initialize(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            with self.assertRaisesRegex(ValueError, 'BILLING_VERSION_CONFLICT'):
                store.save(document(), 1)
            self.assertEqual(list(Path(root).iterdir()), [])
            first = store.save(document(), 0)
            before = store.path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'BILLING_VERSION_CONFLICT'):
                store.save(document(), 0)
            with patch('worker.monthly_billing.json.dumps', side_effect=RuntimeError('serialization failed')):
                with self.assertRaisesRegex(RuntimeError, 'serialization failed'):
                    store.save(document(), 1)
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(store.history('2026-10'), [first])
            self.assertEqual([p.name for p in Path(root).iterdir()], [FILENAME])

    def test_cross_process_same_version_saves_have_one_winner(self):
        import multiprocessing
        context = multiprocessing.get_context('spawn')
        with tempfile.TemporaryDirectory() as root:
            barrier = context.Barrier(4)
            results = context.Queue()
            processes = [context.Process(target=concurrent_save, args=(root, barrier, results)) for _ in range(4)]
            try:
                for process in processes:
                    process.start()
                saved = [results.get(timeout=15) for _ in processes]
                for process in processes:
                    process.join(timeout=15)
                    self.assertEqual(process.exitcode, 0)
                winners = [result for result in saved if isinstance(result, dict)]
                self.assertEqual(len(winners), 1)
                self.assertEqual(saved.count('BILLING_VERSION_CONFLICT'), 3)
                self.assertEqual(Store(root).history('2026-10'), winners)
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=5)
                results.close()
                results.join_thread()

    def test_missing_status_does_not_initialize_storage(self):
        for operation in ('read', 'history'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as root:
                result = access(Store(root), operation)
                self.assertEqual(result, [] if operation == 'history' else dict(
                    version=0, recorded_at=None, document=dict(month='2026-10', coverage_confirmed=False,
                    fx_idr_per_usdt=None, fx_source=None, bills=[])))
                self.assertEqual(list(Path(root).iterdir()), [])
