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
