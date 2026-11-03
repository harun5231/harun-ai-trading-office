"""Isolated durable evaluation tests: no providers, exchange or robot startup."""
import importlib
import sqlite3
import tempfile
import unittest
from pathlib import Path


def position(identity='a', pnl='-2', closed='2026-11-01T00:00:00Z', complete=True):
    return dict(id=identity, symbol='ETHUSDT', status='CLOSED', closed_at=closed,
                opened_at='2026-10-31T00:00:00Z', complete=complete,
                pnl_usdt=pnl, known_pnl_usdt='-999', incomplete_reasons=[])


class DurableEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def module(self):
        from worker import trade_evaluation
        return trade_evaluation

    def test_closed_net_pnl_is_durable_and_duplicate_poll_is_noop(self):
        module = self.module()
        calendar = dict(positions=[position()], complete=False,
                        incomplete_reasons=['SYMBOL_DISCOVERY_NOT_EXHAUSTIVE'])
        first = module.observe_calendar(self.path, calendar)
        second = importlib.reload(module).observe_calendar(self.path, calendar)
        self.assertEqual(first, second)
        self.assertEqual(first['processed_trade_ids'], ['a'])
        self.assertEqual(first['consecutive_losses'], 1)
        self.assertFalse(first['streak_complete'])
        job = first['jobs'][0]
        self.assertEqual(job['id'], 'post-trade:a')
        self.assertEqual(job['status'], 'BLOCKED_MISSING_EVIDENCE')
        self.assertEqual(job['findings']['net_pnl_usdt'], '-2')
        self.assertEqual(job['findings']['outcome'], 'LOSS')
        self.assertEqual(job['experiment_outcome']['status'], 'NOT_RUN')
        self.assertIn('REQUIRES_NEUROBRO_EVALUATION', job['missing_reasons'])
        self.assertIn('HISTORICAL_CHART_ARCHIVE_NOT_LINKED', job['missing_reasons'])
        db = sqlite3.connect(self.path / 'trade_evaluation.sqlite3')
        self.addCleanup(db.close)
        self.assertEqual(db.execute('SELECT count(*) FROM evaluation_events').fetchone()[0], 3)
        self.assertEqual(module.read_status(self.path), first)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("DELETE FROM evaluation_events")

    def test_three_losses_queue_one_durable_streak_job_and_backfill_revises_without_duplicates(self):
        module = self.module()
        rows = [position(str(i), closed=f'2026-11-0{i}T00:00:00Z') for i in (1, 2, 3)]
        state = module.observe_calendar(self.path, dict(positions=rows[::-1], complete=True))
        streaks = [job for job in state['jobs'] if job['id'].startswith('losing-streak:')]
        self.assertEqual(len(streaks), 1)
        self.assertEqual(streaks[0]['trade_ids'], ['1', '2', '3'])
        self.assertEqual(streaks[0]['findings']['consecutive_losses'], 3)
        self.assertEqual(streaks[0]['status'], 'BLOCKED_MISSING_EVIDENCE')
        self.assertTrue(state['streak_complete'])
        self.assertEqual(state, module.observe_calendar(self.path, dict(positions=rows, complete=True)))
        # A later-discovered winning position between losses invalidates the old
        # streak's premise without deleting its immutable historical job record.
        win = position('backfill', pnl='1', closed='2026-11-02T12:00:00Z')
        state = module.observe_calendar(self.path, dict(positions=[win], complete=True))
        self.assertEqual(state['consecutive_losses'], 1)
        stale = next(job for job in state['jobs'] if job['id'] == streaks[0]['id'])
        self.assertEqual(stale['status'], 'SUPERSEDED_HISTORY_REVISION')
        self.assertEqual(stale['experiment_outcome']['status'], 'NOT_RUN')

    def test_incomplete_pnl_breaks_streak_and_never_uses_known_subtotal(self):
        module = self.module()
        rows = [position(str(i), closed=f'2026-11-0{i}T00:00:00Z') for i in (1, 2, 3)]
        rows[1].update(complete=False, pnl_usdt=None, incomplete_reasons=['NON_USDT_COMMISSION'])
        state = module.observe_calendar(self.path, dict(positions=rows, complete=True))
        self.assertEqual(state['consecutive_losses'], 1)
        self.assertFalse(state['streak_complete'])
        job = next(job for job in state['jobs'] if job['id'] == 'post-trade:2')
        self.assertEqual(job['findings']['outcome'], 'UNKNOWN')
        self.assertIn('NON_USDT_COMMISSION', job['missing_reasons'])
        rows[1].update(complete=True, pnl_usdt='-1', incomplete_reasons=[])
        state = module.observe_calendar(self.path, dict(positions=rows, complete=True))
        self.assertEqual(state['consecutive_losses'], 3)
        self.assertTrue(state['streak_complete'])
        # Breakeven ends a run, it is not a loss.
        state = module.observe_calendar(self.path, dict(positions=[position('4', pnl='0', closed='2026-11-04T00:00:00Z')]))
        self.assertEqual(state['consecutive_losses'], 0)

    def test_authoritative_native_account_actor_persists_while_off_and_retries_failed_journal(self):
        import ast
        import threading
        from types import SimpleNamespace
        from unittest.mock import patch
        module = self.module()
        source = Path(__file__).resolve().parents[1] / 'vps_native/worker/api_service.py'
        cls = next(node for node in ast.parse(source.read_bytes()).body if isinstance(node, ast.ClassDef) and node.name == 'Controller')
        actor = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'account_loop')
        stop = threading.Event()
        calls = []
        class Store:
            def __init__(self, db): pass
            def reconcile_carry(self, *args): pass
            def report_office(self, observation): calls.append('snapshot')
            def report_office_failure(self, reason): calls.append('account_failure')
        ticks = iter(range(0, 10000, 100))
        polls = []
        def wait(seconds):
            polls.append(seconds)
            if len(polls) == 3: stop.set()
        calendar = dict(positions=[position()], complete=False, checked_at='fixture', incomplete_reasons=['HISTORY_LOADING'])
        scope = dict(__package__='worker', RobotStore=Store,
                     Coordinator=lambda *a, **k: None, OrderGateway=lambda: None,
                     BinanceReadOnly=lambda: None, Market=lambda: None,
                     collect=lambda *a, **k: dict(account=dict(positions=[])),
                     collect_calendar=lambda *a, **k: calendar,
                     refresh_existing_risks=lambda *a: calls.append('risk'),
                     time=SimpleNamespace(monotonic=lambda: next(ticks)),
                     HISTORY_POLL_SECONDS=60, ACCOUNT_POLL_SECONDS=15, ACCOUNT_FAILURE_CODES=set())
        exec(compile(ast.Module(body=[actor], type_ignores=[]), str(source), 'exec'), scope)
        controller = SimpleNamespace(directory=self.path, stopping=stop, state_lock=threading.Lock(),
            open_ledger=lambda: SimpleNamespace(db=SimpleNamespace(close=lambda: None)),
            protection=SimpleNamespace(recover_pending=lambda *a: None),
            account_wakeup=SimpleNamespace(wait=wait, clear=lambda: None), progress=lambda **k: None,
            clear_failure=lambda **k: None, failure=lambda *a, **k: calls.append('service_failure'))
        actual = module.observe_calendar
        attempts = []
        def fail_once(*args):
            attempts.append(1)
            if len(attempts) == 1: raise OSError('PRIVATE_PATH_SECRET')
            return actual(*args)
        with patch.object(module, 'observe_calendar', side_effect=fail_once):
            scope['account_loop'](controller)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(calls, ['snapshot', 'risk'] * 3)
        self.assertEqual(module.read_status(self.path)['processed_trade_ids'], ['a'])
        self.assertIsNone(controller.evaluation_failure)
        db = sqlite3.connect(self.path / module.FILENAME)
        try:
            self.assertEqual(db.execute('SELECT count(*) FROM evaluation_events').fetchone()[0], 3)
        finally:
            db.close()

    def test_native_office_status_exposes_journal_readonly_and_masks_storage_failure(self):
        import ast
        from types import SimpleNamespace
        from unittest.mock import patch
        module = self.module()
        source = Path(__file__).resolve().parents[1] / 'vps_native/worker/api_service.py'
        node = next(node for node in ast.parse(source.read_bytes()).body if isinstance(node, ast.FunctionDef) and node.name == 'office_snapshot')
        scope = dict(__package__='worker', robot_snapshot=lambda c, r: r, session_status=lambda: {})
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), scope)
        import threading
        controller = SimpleNamespace(directory=self.path, lock=threading.Lock(),
            worker_failure=None, account_failure=None, evaluation_failure=None)
        result = scope['office_snapshot'](controller, dict(robot={}, account={}))
        self.assertEqual(result['trade_evaluation']['status'], 'NOT_OBSERVED')
        self.assertFalse((self.path / module.FILENAME).exists())
        module.observe_calendar(self.path, dict(positions=[position()], complete=True))
        before = (self.path / module.FILENAME).read_bytes()
        result = scope['office_snapshot'](controller, dict(robot={}, account={}))
        self.assertEqual(result['trade_evaluation']['processed_trade_ids'], ['a'])
        self.assertEqual((self.path / module.FILENAME).read_bytes(), before)
        controller.evaluation_failure = 'POST_TRADE_EVALUATION_UNAVAILABLE'
        result = scope['office_snapshot'](controller, dict(robot={}, account={}))
        self.assertEqual(result['trade_evaluation']['status'], 'UNAVAILABLE')
        with patch.object(module, 'read_status', side_effect=OSError('SECRET')):
            result = scope['office_snapshot'](controller, dict(robot={}, account={}))
        self.assertEqual(result['trade_evaluation'], dict(status='UNAVAILABLE', failure_code='POST_TRADE_EVALUATION_UNAVAILABLE'))

    def test_journal_is_private_and_refuses_links_to_financial_database(self):
        import os
        module = self.module()
        module.observe_calendar(self.path, dict(positions=[position()]))
        journal = self.path / module.FILENAME
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        journal.unlink()
        ledger = self.path / 'ledger.sqlite3'
        ledger.write_bytes(b'financial sentinel must remain byte-identical')
        for link in ('symlink', 'hardlink'):
            if link == 'symlink': journal.symlink_to(ledger)
            else: os.link(ledger, journal)
            with self.assertRaises((ValueError, OSError)):
                module.observe_calendar(self.path, dict(positions=[position()]))
            with self.assertRaises((ValueError, OSError)):
                module.read_status(self.path)
            self.assertEqual(ledger.read_bytes(), b'financial sentinel must remain byte-identical')
            journal.unlink()

    def test_reusable_snapshot_api_is_filename_scoped_and_rejects_path_traversal(self):
        module = self.module()
        storage = getattr(module, 'snapshot_storage', None)
        self.assertIsNotNone(storage, 'secure snapshot storage must be reusable outside trade evaluation')
        assert storage is not None
        ledger = self.path / 'ledger.sqlite3'
        ledger.write_bytes(b'financial history')
        with storage.snapshot_database(self.path, 'billing-evidence.sqlite3', write=True) as db:
            db.execute('CREATE TABLE evidence(value TEXT)')
            db.execute("INSERT INTO evidence VALUES ('durable')")
        with storage.snapshot_database(self.path, 'billing-evidence.sqlite3') as db:
            self.assertEqual(db.execute('SELECT value FROM evidence').fetchone(), ('durable',))
        image = self.path / 'billing-evidence.sqlite3'
        before = image.read_bytes()
        with self.assertRaises(sqlite3.OperationalError):
            with storage.snapshot_database(self.path, image.name) as db:
                db.execute("INSERT INTO evidence VALUES ('read must not write')")
        with self.assertRaisesRegex(ValueError, 'SNAPSHOT_TRANSACTION_UNCOMMITTED'):
            with storage.snapshot_database(self.path, image.name, write=True) as db:
                db.execute('BEGIN')
                db.execute("INSERT INTO evidence VALUES ('uncommitted')")
        with self.assertRaisesRegex(RuntimeError, 'abort context'):
            with storage.snapshot_database(self.path, image.name, write=True) as db:
                db.execute("INSERT INTO evidence VALUES ('abort')")
                raise RuntimeError('abort context')
        self.assertEqual(image.read_bytes(), before)
        with storage.snapshot_database(self.path, 'never-created.sqlite3') as db:
            self.assertIsNone(db)
        self.assertFalse((self.path / 'never-created.sqlite3').exists())
        for invalid in ('', '.', '..', '../ledger.sqlite3', '/ledger.sqlite3', 'sub/file', 'bad\x00name'):
            with self.subTest(filename=repr(invalid)), self.assertRaises(ValueError):
                with storage.snapshot_database(self.path, invalid, write=True):
                    self.fail('unsafe filename accepted')
        self.assertEqual(ledger.read_bytes(), b'financial history')

    def test_shm_hardlink_never_changes_valid_financial_database(self):
        import os
        module = self.module()
        for operation in ('observe_calendar', 'read_status'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory(dir=self.path) as tmp:
                directory = Path(tmp)
                module.observe_calendar(directory, dict(positions=[position()], complete=True))
                ledger = directory / 'ledger.sqlite3'
                with sqlite3.connect(ledger) as db:
                    db.execute('CREATE TABLE financial_history(value TEXT)')
                    db.execute("INSERT INTO financial_history VALUES ('must survive')")
                db.close()
                before = ledger.read_bytes()
                self.assertEqual(len(before), 8192)
                sidecar = directory / (module.FILENAME + '-shm')
                os.link(ledger, sidecar)
                try:
                    if operation == 'observe_calendar':
                        module.observe_calendar(directory, dict(positions=[position('new')]))
                    else:
                        module.read_status(directory)
                except (OSError, ValueError, sqlite3.DatabaseError):
                    pass  # Refusal is safe; changing financial bytes is not.
                self.assertEqual(ledger.read_bytes(), before)
                with sqlite3.connect(ledger) as db:
                    self.assertEqual(db.execute('SELECT value FROM financial_history').fetchone(), ('must survive',))
                db.close()

    def test_all_sqlite_aliases_preserve_financial_history_for_ingestion_and_status(self):
        import os
        module = self.module()
        for suffix in ('', '-wal', '-shm', '-journal', '-mj-sentinel'):
            for link in ('symlink', 'hardlink'):
                for operation in ('observe_calendar', 'read_status'):
                    with self.subTest(suffix=suffix, link=link, operation=operation):
                        with tempfile.TemporaryDirectory(dir=self.path) as tmp:
                            directory = Path(tmp)
                            module.observe_calendar(directory, dict(positions=[position()]))
                            ledger = directory / 'ledger.sqlite3'
                            with sqlite3.connect(ledger) as db:
                                db.execute('CREATE TABLE financial_history(value TEXT)')
                                db.execute("INSERT INTO financial_history VALUES ('preserved')")
                            db.close()
                            before = ledger.read_bytes()
                            alias = directory / (module.FILENAME + suffix)
                            if alias.exists():
                                alias.unlink()
                            if link == 'symlink':
                                alias.symlink_to(ledger)
                            else:
                                os.link(ledger, alias)
                            try:
                                if operation == 'observe_calendar':
                                    module.observe_calendar(directory, dict(positions=[position('new')]))
                                else:
                                    module.read_status(directory)
                            except (OSError, ValueError, sqlite3.DatabaseError):
                                pass
                            self.assertEqual(ledger.read_bytes(), before)
                            with sqlite3.connect(ledger) as db:
                                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone(), ('ok',))
                                self.assertEqual(db.execute('SELECT value FROM financial_history').fetchone(), ('preserved',))
                            db.close()

    def test_main_path_swap_after_validation_never_reopens_financial_alias(self):
        import os
        from unittest.mock import patch
        module = self.module()
        for link in ('symlink', 'hardlink'):
            for operation in ('observe_calendar', 'read_status'):
                with self.subTest(link=link, operation=operation):
                    with tempfile.TemporaryDirectory(dir=self.path) as tmp:
                        directory = Path(tmp)
                        first = module.observe_calendar(directory, dict(positions=[position()]))
                        ledger = directory / 'ledger.sqlite3'
                        ledger.write_bytes(b'financial history')
                        before = ledger.read_bytes()
                        connect = sqlite3.connect
                        def swap(*args, **kwargs):
                            journal = directory / module.FILENAME
                            journal.unlink()
                            if link == 'symlink':
                                journal.symlink_to(ledger)
                            else:
                                os.link(ledger, journal)
                            return connect(*args, **kwargs)
                        with patch.object(module.snapshot_storage.sqlite3, 'connect', side_effect=swap):
                            if operation == 'observe_calendar':
                                result = module.observe_calendar(directory, dict(positions=[position('new')]))
                                self.assertEqual(result['processed_trade_ids'], ['a', 'new'])
                            else:
                                self.assertEqual(module.read_status(directory), first)
                        self.assertEqual(ledger.read_bytes(), before)

    def test_directory_swap_keeps_reads_and_publication_on_pinned_inode(self):
        import os
        from unittest.mock import patch
        module = self.module()
        for operation in ('observe_calendar', 'read_status'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory(dir=self.path) as tmp:
                root = Path(tmp)
                directory, moved, victim = (root / name for name in ('actor', 'moved', 'financial'))
                directory.mkdir(mode=0o700)
                victim.mkdir(mode=0o700)
                ledger = victim / module.FILENAME
                ledger.write_bytes(b'financial directory sentinel')
                before = ledger.read_bytes()
                first = module.observe_calendar(directory, dict(positions=[position()]))
                connect = sqlite3.connect
                def swap(*args, **kwargs):
                    directory.rename(moved)
                    directory.symlink_to(victim, target_is_directory=True)
                    return connect(*args, **kwargs)
                with patch.object(module.snapshot_storage.sqlite3, 'connect', side_effect=swap):
                    if operation == 'observe_calendar':
                        result = module.observe_calendar(directory, dict(positions=[position('new')]))
                        self.assertEqual(result['processed_trade_ids'], ['a', 'new'])
                    else:
                        self.assertEqual(module.read_status(directory), first)
                self.assertEqual(ledger.read_bytes(), before)
                self.assertEqual(set(victim.iterdir()), {ledger})
                self.assertEqual(module.read_status(moved)['processed_trade_ids'],
                                 ['a', 'new'] if operation == 'observe_calendar' else ['a'])
                with self.assertRaises(OSError):
                    module.read_status(directory)

    def test_snapshot_temp_alias_collision_cannot_overwrite_financial_file(self):
        import os
        from unittest.mock import patch
        module = self.module()
        first = module.observe_calendar(self.path, dict(positions=[position()]))
        ledger = self.path / 'ledger.sqlite3'
        ledger.write_bytes(b'financial history')
        for link in ('symlink', 'hardlink'):
            alias = self.path / '.sqlite-snapshot-collision'
            if link == 'symlink':
                alias.symlink_to(ledger)
            else:
                os.link(ledger, alias)
            with patch.object(module.snapshot_storage.secrets, 'token_hex', return_value='collision'):
                with self.assertRaises(FileExistsError):
                    module.observe_calendar(self.path, dict(positions=[position('new')]))
            self.assertEqual(ledger.read_bytes(), b'financial history')
            self.assertEqual(module.read_status(self.path), first)
            alias.unlink()

    def test_process_crash_rolls_back_trade_and_job_then_replay_recovers_once(self):
        import subprocess
        import sys
        module = self.module()
        base = dict(positions=[position()], complete=True)
        before = module.observe_calendar(self.path, base)
        code = '''
import os, sys
from pathlib import Path
from worker import trade_evaluation as m
original = m._append
def crash(db, kind, key, value):
    original(db, kind, key, value)
    if kind == 'trade': os._exit(73)
m._append = crash
m.observe_calendar(Path(sys.argv[1]), {'positions': [{'id': 'new', 'status': 'CLOSED'}]})
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.path)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual(module.read_status(self.path), before)
        recovered = dict(positions=[position('new', closed='2026-11-02T00:00:00Z')], complete=True)
        after = module.observe_calendar(self.path, recovered)
        self.assertEqual(after['processed_trade_ids'], ['a', 'new'])
        self.assertEqual(after, module.observe_calendar(self.path, recovered))
        db = sqlite3.connect(self.path / module.FILENAME)
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM evaluation_events WHERE kind='trade'").fetchone()[0], 2)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            db.close()

    def test_publication_fsyncs_image_before_replace_and_directory_before_success(self):
        import os
        import stat
        from unittest.mock import patch
        module = self.module()
        events = []
        fsync, replace = os.fsync, os.replace
        def sync(fd):
            events.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'image')
            return fsync(fd)
        def publish(*args, **kwargs):
            events.append('replace')
            return replace(*args, **kwargs)
        with patch.object(module.snapshot_storage.os, 'fsync', side_effect=sync), patch.object(module.snapshot_storage.os, 'replace', side_effect=publish):
            module.observe_calendar(self.path, dict(positions=[position()]))
        self.assertEqual(events, ['image', 'replace', 'directory'])
        first = module.read_status(self.path)
        for fail_at in ('image', 'directory'):
            def fail_sync(fd):
                kind = 'directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'image'
                if kind == fail_at:
                    raise OSError('injected persistence failure')
                return fsync(fd)
            with patch.object(module.snapshot_storage.os, 'fsync', side_effect=fail_sync):
                with self.assertRaises(OSError):
                    module.observe_calendar(self.path, dict(positions=[position('new')]))
            if fail_at == 'image':
                self.assertEqual(module.read_status(self.path), first)
            recovered = module.observe_calendar(self.path, dict(positions=[position('new')]))
            self.assertEqual(recovered['processed_trade_ids'], ['a', 'new'])
            self.assertEqual(module.observe_calendar(self.path, dict(positions=[position('new')])), recovered)

    def test_crash_during_atomic_publication_recovers_old_or_new_complete_snapshot(self):
        import subprocess
        import sys
        module = self.module()
        for stage in ('before_replace', 'after_replace'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(dir=self.path) as tmp:
                directory = Path(tmp)
                module.observe_calendar(directory, dict(positions=[position()], complete=True))
                code = '''
import os, sys
from pathlib import Path
from worker import trade_evaluation as m
replace = m.snapshot_storage.os.replace
def crash(*args, **kwargs):
    if sys.argv[2] == 'after_replace': replace(*args, **kwargs)
    os._exit(74)
m.snapshot_storage.os.replace = crash
m.observe_calendar(Path(sys.argv[1]), {'positions': [
    {'id': 'new', 'status': 'CLOSED', 'closed_at': '2026-11-02T00:00:00Z',
     'complete': True, 'pnl_usdt': '-2'}], 'complete': True})
'''
                result = subprocess.run([sys.executable, '-c', code, str(directory), stage],
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 74, result.stderr)
                expected = ['a', 'new'] if stage == 'after_replace' else ['a']
                self.assertEqual(module.read_status(directory)['processed_trade_ids'], expected)
                replay = dict(positions=[position('new', closed='2026-11-02T00:00:00Z')], complete=True)
                recovered = module.observe_calendar(directory, replay)
                self.assertEqual(recovered['processed_trade_ids'], ['a', 'new'])
                self.assertEqual(module.observe_calendar(directory, replay), recovered)

    def test_process_writers_and_status_readers_share_one_lock_without_lost_trades(self):
        import subprocess
        import sys
        from concurrent.futures import ThreadPoolExecutor
        module = self.module()
        module.observe_calendar(self.path, dict(positions=[position()]))
        code = '''
import sys
from pathlib import Path
from worker import trade_evaluation as m
row = {'id': sys.argv[2], 'status': 'CLOSED', 'closed_at': '2026-11-02T00:00:00Z',
       'complete': True, 'pnl_usdt': '-2'}
for _ in range(4):
    m.observe_calendar(Path(sys.argv[1]), {'positions': [row], 'complete': True})
    assert row['id'] in m.read_status(Path(sys.argv[1]))['processed_trade_ids']
'''
        def run(identity):
            return subprocess.run([sys.executable, '-c', code, str(self.path), identity],
                                  capture_output=True, timeout=15)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, ['b', 'c', 'd', 'e']))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(module.read_status(self.path)['processed_trade_ids'], ['a', 'b', 'c', 'd', 'e'])
        with sqlite3.connect(self.path / module.FILENAME) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM evaluation_events WHERE kind='trade'").fetchone(), (5,))
        db.close()

    def test_busy_journal_fails_with_bounded_wait_so_account_actor_can_retry(self):
        import fcntl
        import os
        import subprocess
        import sys
        module = self.module()
        module.observe_calendar(self.path, dict(positions=[position()]))
        code = '''
import sys
from pathlib import Path
from worker import trade_evaluation as m
m.LOCK_TIMEOUT_SECONDS = 0.03
try:
    if sys.argv[2] == 'write': m.observe_calendar(Path(sys.argv[1]), {'positions': []})
    else: m.read_status(Path(sys.argv[1]))
except TimeoutError as error:
    assert str(error) == 'SNAPSHOT_LOCK_TIMEOUT'
else:
    raise AssertionError('busy journal was not refused')
'''
        fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            for operation in ('write', 'read'):
                try:
                    result = subprocess.run([sys.executable, '-c', code, str(self.path), operation],
                                            capture_output=True, timeout=1)
                except subprocess.TimeoutExpired:
                    self.fail('journal lock blocked the account actor instead of timing out')
                self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.close(fd)

    def test_concurrent_replay_serializes_without_duplicate_events(self):
        from concurrent.futures import ThreadPoolExecutor
        module = self.module()
        calendar = dict(positions=[position()], complete=True)
        # Initialize before readers, as the existing actor does on its first poll.
        first = module.observe_calendar(self.path, calendar)
        with ThreadPoolExecutor(max_workers=4) as pool:
            snapshots = list(pool.map(lambda _: module.observe_calendar(self.path, calendar), range(12)))
        self.assertTrue(all(value == first for value in snapshots))
        db = sqlite3.connect(self.path / module.FILENAME)
        try:
            self.assertEqual(db.execute('SELECT count(*) FROM evaluation_events').fetchone()[0], 3)
        finally:
            db.close()

    def test_invalid_net_pnl_rolls_back_whole_poll(self):
        module = self.module()
        first = module.observe_calendar(self.path, dict(positions=[position()], complete=True))
        for invalid in ('NaN', 'Infinity', '-Infinity', 'not-pnl', 1.25):
            with self.assertRaisesRegex(ValueError, 'CLOSED_NET_PNL_INVALID'):
                module.observe_calendar(self.path, dict(positions=[position('new'), position('bad', pnl=invalid)], complete=True))
            self.assertEqual(module.read_status(self.path), first)


if __name__ == '__main__':
    unittest.main()
