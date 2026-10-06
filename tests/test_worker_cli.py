"""Read-only CLI and private-state regression tests; all HTTP is mocked."""
import builtins
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from worker.diagnostics import read_records
from worker.state import directory


class WorkerCLITests(unittest.TestCase):
    def isolated_cli(self):
        spec = importlib.util.spec_from_file_location('worker._cli_import_check', Path('worker/__main__.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @contextlib.contextmanager
    def no_runtime(self):
        original_import = builtins.__import__
        forbidden = {'worker.core', 'worker.market', 'worker.neuroapi', 'worker.state',
                     'worker.robot', 'worker.api_service', 'worker.workflow', 'worker.health',
                     'worker.robot_status', 'worker.diagnostics', 'worker.binance_private'}
        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            resolved = 'worker.' + name if level == 1 and globals and globals.get('__package__') == 'worker' else name
            if resolved in forbidden or (resolved == 'worker' and any('worker.' + item in forbidden for item in fromlist)):
                raise AssertionError('CLI imported runtime before selecting a diagnostic command: ' + resolved)
            return original_import(name, globals, locals, fromlist, level)
        with patch('builtins.__import__', side_effect=guarded_import), \
             patch('pathlib.Path.read_text', side_effect=AssertionError('Unexpected state or token read')), \
             patch('sqlite3.connect', side_effect=AssertionError('Unexpected state connection')), \
             patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected HTTP')):
            yield

    def invoke(self, arguments):
        from worker.__main__ import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = main(arguments)
        return result, out.getvalue(), err.getvalue()

    def test_import_default_and_help_need_no_runtime_state_or_secrets(self):
        for arguments in ([], ['--help']):
            with self.subTest(arguments=arguments), self.no_runtime():
                module = self.isolated_cli()
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    try:
                        result = module.main(arguments)
                    except SystemExit as exit_code:
                        result = exit_code.code
                self.assertEqual(result, 0)
                self.assertIn('usage:', out.getvalue())
                for command in ('health', 'status', 'diagnostics'):
                    self.assertIn(command, out.getvalue())

    def test_old_paid_live_and_simulation_commands_are_rejected_before_runtime_import(self):
        retired = ('api-dry-run', 'api-check', 'screening-once', 'analysis-once',
                   'binance-check', 'binance-shadow', 'binance-live-preflight',
                   'binance-arm', 'binance-execute', 'binance-scheduler', 'simulation', 'approval')
        with self.no_runtime():
            module = self.isolated_cli()
            for command in retired:
                with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()), \
                     self.assertRaises(SystemExit) as caught:
                    module.main([command])
                self.assertEqual(caught.exception.code, 2)

    def test_health_reports_online_offline_and_sanitizes_probe_errors(self):
        for online in (True, False):
            with self.subTest(online=online), patch('worker.health.probe_api', return_value=online) as probe:
                code, output, errors = self.invoke(['health'])
            self.assertEqual(code, 0 if online else 1)
            self.assertEqual(json.loads(output)['status'], 'ONLINE' if online else 'OFFLINE')
            self.assertEqual(errors, '')
            probe.assert_called_once_with()
        with patch('worker.health.probe_api', side_effect=OSError('PRIVATE_TOKEN PRIVATE_RESPONSE')):
            code, output, errors = self.invoke(['health'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)['status'], 'OFFLINE')
        self.assertNotIn('PRIVATE', output + errors)

    def test_status_passes_data_root_to_read_only_collector(self):
        with tempfile.TemporaryDirectory() as root:
            result = dict(health='ONLINE', robot=dict(mode='ORDER_PIPELINE', gateway_connected=False))
            with patch('worker.robot_status.collect', return_value=result) as collect:
                code, output, errors = self.invoke(['status', '--data-dir', root])
            collect.assert_called_once_with(root=root)
            self.assertEqual((code, json.loads(output), errors), (0, result, ''))
            self.assertEqual(list(Path(root).iterdir()), [])
            with patch('worker.robot_status.collect', return_value=dict(health='OFFLINE')):
                code, output, _ = self.invoke(['status', '--data-dir', root])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output)['health'], 'OFFLINE')

    def test_status_and_diagnostics_exceptions_only_print_fixed_failure_codes(self):
        for command, target, expected in (
                ('status', 'worker.robot_status.collect', 'WORKER_STATUS_UNAVAILABLE'),
                ('diagnostics', 'worker.diagnostics.read_records', 'DIAGNOSTICS_UNAVAILABLE')):
            with self.subTest(command=command), patch(target, side_effect=OSError('PRIVATE_KEY RAW_PROVIDER_RESPONSE')):
                code, output, errors = self.invoke([command])
            self.assertEqual((code, json.loads(output)), (1, dict(status=expected)))
            self.assertEqual(errors, '')
            self.assertNotIn('PRIVATE_KEY', output)
            self.assertNotIn('RAW_PROVIDER_RESPONSE', output)

    def test_diagnostics_prints_existing_read_only_result_without_other_actions(self):
        result = [dict(operation='2026-10-06:robot-v8:0:screening:0:1', state='PENDING', attempts=1, failure_code=None)]
        with tempfile.TemporaryDirectory() as root, patch('worker.diagnostics.read_records', return_value=result) as records:
            code, output, errors = self.invoke(['diagnostics', '--data-dir', root])
            records.assert_called_once_with(root)
            self.assertEqual((code, json.loads(output), errors), (0, result, ''))
            self.assertEqual(list(Path(root).iterdir()), [])


class ReadOnlyDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'trading' / 'ledger.sqlite3'

    def create_journal(self):
        self.path.parent.mkdir()
        connection = sqlite3.connect(self.path)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('''CREATE TABLE api_requests(
            operation TEXT PRIMARY KEY, state TEXT, attempts, failure_code TEXT,
            output TEXT, body_hash TEXT, idempotency TEXT)''')
        self.addCleanup(connection.close)
        return connection

    def insert(self, connection, operation, state='COMPLETE', attempts=1, failure=None):
        connection.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?)',
            (operation, state, attempts, failure, 'PRIVATE_RAW_OUTPUT', 'PRIVATE_BODY_HASH', 'PRIVATE_IDEMPOTENCY'))
        connection.commit()

    def test_missing_current_database_never_creates_state_or_reads_browser_archive(self):
        browser = self.root / 'browser'
        browser.mkdir()
        archive = browser / 'ledger.sqlite3'
        archive.write_bytes(b'PRIVATE_ARCHIVE_MUST_STAY_UNREAD')
        before = archive.stat().st_mtime_ns
        with patch('worker.diagnostics.sqlite3.connect', side_effect=AssertionError('No current database exists')):
            self.assertEqual(read_records(self.root), [])
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(archive.stat().st_mtime_ns, before)
        self.assertEqual(archive.read_bytes(), b'PRIVATE_ARCHIVE_MUST_STAY_UNREAD')

    def test_current_operations_visible_legacy_and_arbitrary_values_redacted(self):
        connection = self.create_journal()
        current = ('2026-10-06:robot-v8:0:screening:0:1',
                   '2026-10-06:robot-v8:2:screening:3:2',
                   '2026-10-06:robot-v8:1:analysis-v8:BTCUSDT')
        for operation in current:
            self.insert(connection, operation, 'NEEDS_REVIEW', 2, 'NETWORK_UNCERTAIN')
        for operation in ('2026-10-06:robot-v7:0:analysis-v7:BTCUSDT',
                          '2026-10-06:analysis-v6:BTCUSDT', '2026-10-06:manual-screening:v2',
                          '2026-10-06:robot-v8:3:analysis-v8:BTCUSDT', 'PRIVATE_AUTH_TOKEN'):
            self.insert(connection, operation, 'PRIVATE_STATE', 'PRIVATE_ATTEMPTS', 'PRIVATE_FAILURE')
        value = read_records(self.root)
        self.assertEqual({row['operation'] for row in value if row['operation'] != 'OPERATION_REDACTED'}, set(current))
        self.assertTrue(all(set(row) == {'operation', 'state', 'attempts', 'failure_code'} for row in value))
        redacted = [row for row in value if row['operation'] == 'OPERATION_REDACTED']
        self.assertEqual(len(redacted), 5)
        self.assertTrue(all(row['state'] == 'STATE_REDACTED' and row['attempts'] is None and
                            row['failure_code'] == 'VALIDATION_REJECTED' for row in redacted))
        self.assertNotIn('PRIVATE', json.dumps(value))
        self.assertNotIn('analysis-v7', json.dumps(value))
        self.assertNotIn('manual-screening', json.dumps(value))

    def test_uri_query_only_authorizer_and_latest_committed_row(self):
        writer = self.create_journal()
        operation = '2026-10-06:robot-v8:0:analysis-v8:BTCUSDT'
        self.insert(writer, operation, 'PENDING', 1)
        real_connect = sqlite3.connect
        mutations = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                     sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TRIGGER,
                     sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TRIGGER,
                     sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
        denied, statements = [], []
        def connect(path, *args, **kwargs):
            self.assertEqual(path, self.path.as_uri() + '?mode=ro')
            self.assertTrue(kwargs.get('uri'))
            connection = real_connect(path, *args, **kwargs)
            def authorize(action, *details):
                if action in mutations:
                    denied.append((action, details))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            connection.set_trace_callback(statements.append)
            return connection
        with patch('worker.diagnostics.sqlite3.connect', side_effect=connect):
            self.assertEqual(read_records(self.root)[0]['state'], 'PENDING')
            writer.execute("UPDATE api_requests SET state='NEEDS_REVIEW', attempts=2, failure_code='HTTP_503' WHERE operation=?", (operation,))
            writer.commit()
            latest = read_records(self.root)[0]
        self.assertEqual((latest['state'], latest['attempts'], latest['failure_code']), ('NEEDS_REVIEW', 2, 'HTTP_503'))
        self.assertEqual(denied, [])
        self.assertTrue(any(statement.upper().replace(' ', '') == 'PRAGMAQUERY_ONLY=ON' for statement in statements))
        self.assertFalse((self.path.parent / 'migration.lock').exists())
        self.assertFalse((self.path.parent / 'cycle.lock').exists())

    def test_missing_journal_table_and_legacy_schema_do_not_trigger_migration(self):
        self.path.parent.mkdir()
        with sqlite3.connect(self.path) as connection:
            connection.execute('CREATE TABLE historical_note(value TEXT)')
            connection.execute('INSERT INTO historical_note VALUES(?)', ('audit-preserved',))
        self.assertEqual(read_records(self.root), [])
        with sqlite3.connect(self.path) as connection:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            self.assertEqual(tables, ['historical_note'])
            self.assertEqual(connection.execute('SELECT value FROM historical_note').fetchone()[0], 'audit-preserved')


class PrivateStateTests(unittest.TestCase):
    def test_private_root_rejects_repository_and_nested_repository_destination(self):
        repo = Path('worker/state.py').resolve().parent.parent
        for root in (repo, repo / 'private-test-must-not-be-created'):
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, '^PRIVATE_STATE_REQUIRED$'):
                directory(root)
        self.assertFalse((repo / 'private-test-must-not-be-created').exists())

    def test_state_creates_only_private_directory_and_regular_lock_preserving_existing_inode(self):
        with tempfile.TemporaryDirectory() as root:
            private = Path(root) / 'private'
            target = directory(private)
            self.assertEqual(target, private / 'trading')
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            lock = target / 'migration.lock'
            self.assertTrue(stat.S_ISREG(lock.lstat().st_mode))
            self.assertEqual(lock.stat().st_nlink, 1)
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
            lock.write_bytes(b'existing-lock-inode')
            before = lock.stat()
            self.assertEqual(directory(private), target)
            after = lock.stat()
            self.assertEqual((after.st_ino, after.st_size, after.st_mtime_ns),
                             (before.st_ino, before.st_size, before.st_mtime_ns))
            self.assertEqual(lock.read_bytes(), b'existing-lock-inode')
            self.assertEqual({path.name for path in target.iterdir()}, {'migration.lock'})

    def test_state_does_not_import_or_copy_legacy_browser_data_or_attempts(self):
        with tempfile.TemporaryDirectory() as root:
            private = Path(root)
            browser = private / 'browser'
            attempts = browser / 'runner-attempts'
            attempts.mkdir(parents=True)
            archive = browser / 'ledger.sqlite3'
            archive.write_bytes(b'historical-browser-audit')
            marker = attempts / '2026-10-06'
            marker.write_bytes(b'historical-paid-attempt')
            target = directory(private)
            self.assertEqual({path.name for path in target.iterdir()}, {'migration.lock'})
            self.assertEqual(archive.read_bytes(), b'historical-browser-audit')
            self.assertEqual(marker.read_bytes(), b'historical-paid-attempt')
            self.assertFalse((target / 'ledger.sqlite3').exists())
            self.assertFalse((target / 'ledger.migrating').exists())

    def test_symlink_hardlink_and_fifo_locks_are_refused_without_changing_target(self):
        for kind in ('symlink', 'hardlink', 'fifo'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                private = Path(root) / 'private'
                trading = private / 'trading'
                trading.mkdir(parents=True)
                external = Path(root) / 'external-note'
                external.write_bytes(b'private-synthetic-fixture')
                external.chmod(0o640)
                lock = trading / 'migration.lock'
                if kind == 'symlink':
                    lock.symlink_to(external)
                elif kind == 'hardlink':
                    os.link(external, lock)
                else:
                    os.mkfifo(lock, 0o600)
                with self.assertRaises((ValueError, RuntimeError, OSError, SystemExit)):
                    directory(private)
                self.assertEqual(external.read_bytes(), b'private-synthetic-fixture')
                self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o640)
                self.assertFalse((trading / 'ledger.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
