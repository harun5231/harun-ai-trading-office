"""PNL deployment/source rollback and financial preservation without Docker/network."""
import ast
import base64
import hashlib
import json
import lzma
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def helper_source():
    script = (ROOT / 'deploy/apply_pnl_calendar.sh').read_text()
    marker = "<<'HARUN_PNL_HELPER'\n"
    start = script.index(marker) + len(marker)
    return script[start:script.index('\nHARUN_PNL_HELPER\n', start) + 1]


def namespace():
    result = {'__name__': 'test_installer'}
    exec(compile(helper_source(), 'pnl-install.py', 'exec'), result)
    return result


@contextmanager
def postflight_fixture(direct, office):
    """A private cache exists, but collector and HTTP response are independent."""
    ns = namespace()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        trading = root / 'trading'
        trading.mkdir(mode=0o700)
        for name in ('pnl-history-v1.json', 'pnl-history.lock'):
            path = trading / name
            path.write_text('{}')
            path.chmod(0o600)
        ledger = trading / 'ledger.sqlite3'
        ledger.write_bytes(b'unchanged financial journal')
        original_path = type(Path())
        class TokenPath(original_path):
            def read_text(self, *args, **kwargs):
                if str(self) == '/run/office/read_token': return 'isolated-test-token'
                return super().read_text(*args, **kwargs)
        ns.update(UID=os.geteuid(), Path=TokenPath)
        body = json.dumps(dict(robot={'robot_on': False}, pnl_calendar=office)).encode()
        class Response:
            code = 200
            def __enter__(self): return self
            def __exit__(self, *unused): pass
            def read(self, size): return body
        requests = []
        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                return Response()
        with patch('worker.binance_private.BinanceReadOnly', return_value=object()), \
             patch('worker.pnl_calendar.collect_calendar', return_value=direct), \
             patch('urllib.request.build_opener', return_value=Opener()):
            yield ns, lambda: ns['calendar_postflight'](ROOT, root), requests, ledger


class PnlInstallerSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.backup = Path(self.temp.name) / 'backup'
        self.backup.mkdir(mode=0o700)
        self.ns = namespace()
        base = {}
        files = {'worker/__init__.py': '', 'worker/robot.py': 'secret_robot=1\n',
                 'worker/order_gateway.py': 'PRIVATE_ADAPTER_SECRET=1\n',
                 'worker/api_service.py': 'old=1\n', 'deploy/container_boot.py': '',
                 'deploy/runtime_permissions.py': '', 'index.html': '<h1>Old</h1>\n'}
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_text(text)
            if name.endswith('.py'): base[name] = hashlib.sha256(text.encode()).hexdigest()
        contents = {'worker/api_service.py': 'new=1\n', 'worker/pnl_calendar.py': 'calendar=1\n',
                    'index.html': '<h1>Calendar</h1>\n'}
        target = dict(base)
        target.update({name: hashlib.sha256(text.encode()).hexdigest() for name, text in contents.items()
                       if name.startswith('worker/')})
        self.ns.update(BASE_RUNTIME=base, TARGET_RUNTIME=target, TARGET_CONTENTS=contents,
                       STATIC_BASE={'index.html': hashlib.sha256(files['index.html'].encode()).hexdigest()})

    def test_backup_apply_and_restore_preserve_private_trading_source(self):
        private = (self.root / 'worker/order_gateway.py').read_bytes()
        robot = (self.root / 'worker/robot.py').read_bytes()
        self.ns['backup_sources'](self.root, self.backup)
        self.ns['apply_sources'](self.root, self.backup)
        self.assertEqual((self.root / 'index.html').read_text(), '<h1>Calendar</h1>\n')
        self.assertTrue((self.root / 'worker/pnl_calendar.py').is_file())
        self.assertEqual((self.root / 'worker/order_gateway.py').read_bytes(), private)
        self.assertEqual((self.root / 'worker/robot.py').read_bytes(), robot)
        self.ns['restore_sources'](self.root, self.backup)
        self.assertEqual((self.root / 'index.html').read_text(), '<h1>Old</h1>\n')
        self.assertFalse((self.root / 'worker/pnl_calendar.py').exists())
        self.assertEqual((self.root / 'worker/order_gateway.py').read_bytes(), private)
        self.assertEqual((self.root / 'worker/robot.py').read_bytes(), robot)

    def test_exact_rerun_is_supported_without_source_reset(self):
        self.ns['backup_sources'](self.root, self.backup)
        self.ns['apply_sources'](self.root, self.backup)
        self.assertEqual(self.ns['choose_sources'](self.root), self.ns['TARGET_RUNTIME'])

    def test_unknown_frontend_edit_refused_before_backup(self):
        (self.root / 'index.html').write_text('user edit')
        with self.assertRaisesRegex(self.ns['Refuse'], 'SOURCE_CHANGED'):
            self.ns['backup_sources'](self.root, self.backup)
        self.assertEqual(list(self.backup.iterdir()), [])

    def test_private_sdk_change_refused_without_reading_key_in_output(self):
        (self.root / 'worker/order_gateway.py').write_text('API_SECRET=changed')
        with self.assertRaisesRegex(self.ns['Refuse'], 'RUNTIME_SOURCE_MISMATCH'):
            self.ns['choose_sources'](self.root)

    def test_restore_validates_entire_set_before_overwriting_any_file(self):
        self.ns['backup_sources'](self.root, self.backup)
        self.ns['apply_sources'](self.root, self.backup)
        (self.root / 'index.html').write_text('concurrent user edit')
        with self.assertRaisesRegex(self.ns['Refuse'], 'SOURCE_CHANGED_DURING_UPDATE'):
            self.ns['restore_sources'](self.root, self.backup)
        self.assertEqual((self.root / 'worker/api_service.py').read_text(), 'new=1\n')
        self.assertEqual((self.root / 'index.html').read_text(), 'concurrent user edit')

    def test_apply_refuses_changes_after_backup_before_any_write(self):
        self.ns['backup_sources'](self.root, self.backup)
        (self.root / 'index.html').write_text('concurrent user edit')
        with self.assertRaises(self.ns['Refuse']):
            self.ns['apply_sources'](self.root, self.backup)
        self.assertEqual((self.root / 'worker/api_service.py').read_text(), 'old=1\n')

    def test_symlink_or_hardlinked_sources_are_refused(self):
        path = self.root / 'index.html'
        path.unlink()
        path.symlink_to(self.root / 'worker/order_gateway.py')
        with self.assertRaises((self.ns['Refuse'], OSError)):
            self.ns['choose_sources'](self.root)
        path.unlink()
        path.write_text('<h1>Old</h1>\n')
        os.link(path, self.root / 'linked-index.html')
        with self.assertRaisesRegex(self.ns['Refuse'], 'SOURCE_PATH_INVALID'):
            self.ns['choose_sources'](self.root)


class PnlInstallerJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'trading'
        self.directory.mkdir(mode=0o700)
        self.db = sqlite3.connect(self.directory / 'ledger.sqlite3')
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE office_schema(version INTEGER); INSERT INTO office_schema VALUES(2);
            CREATE TABLE robot_settings(id INTEGER,enabled INTEGER,risk TEXT);
            INSERT INTO robot_settings VALUES(1,0,'5');
            CREATE TABLE order_intents(symbol TEXT,state TEXT,payload TEXT);
            INSERT INTO order_intents VALUES('ETHUSDT','CLOSED','immutable payload');
            CREATE TABLE api_requests(state TEXT,body TEXT); INSERT INTO api_requests VALUES('COMPLETE','paid body');
            CREATE TABLE robot_cycles(id TEXT,data TEXT); INSERT INTO robot_cycles VALUES('cycle','replacements=3');
            CREATE TABLE robot_jobs(state TEXT); INSERT INTO robot_jobs VALUES('COMPLETE');
            CREATE TABLE robot_candidates(symbol TEXT); INSERT INTO robot_candidates VALUES('BTCUSDT');
            CREATE TABLE robot_entry_receipts(entry_day TEXT,id TEXT); INSERT INTO robot_entry_receipts VALUES('2026-10-07','entry');
            CREATE TABLE office_cache(data TEXT); INSERT INTO office_cache VALUES('old cache');
            CREATE TABLE office_activity(data TEXT); INSERT INTO office_activity VALUES('old display');
            CREATE TABLE robot_status(data TEXT); INSERT INTO robot_status VALUES('old status');
        ''')
        self.db.commit()
        self.ns = namespace()

    def test_calendar_and_display_cache_do_not_change_financial_digest(self):
        before = self.ns['snapshot'](self.db)
        (self.directory / 'pnl-history-v1.json').write_text('separate history cache')
        self.db.execute("UPDATE office_cache SET data='calendar display'")
        self.db.execute("UPDATE robot_status SET data='fresh OFF'")
        after = self.ns['snapshot'](self.db)
        self.assertEqual(before['journal_sha256'], after['journal_sha256'])
        self.assertEqual(before['table_counts'], after['table_counts'])

    def test_risk_receipts_counter_claim_and_order_changes_are_not_caches(self):
        queries = ("UPDATE robot_settings SET risk='6'", "UPDATE robot_entry_receipts SET entry_day='2026-10-08'",
                   "UPDATE robot_cycles SET data='replacements=0'", "UPDATE api_requests SET body='changed'",
                   "UPDATE order_intents SET payload='changed'")
        for query in queries:
            with self.subTest(query=query):
                before = self.ns['snapshot'](self.db)
                self.db.execute('SAVEPOINT mutation')
                self.db.execute(query)
                after = self.ns['snapshot'](self.db)
                self.assertNotEqual(before['journal_sha256'], after['journal_sha256'])
                self.db.execute('ROLLBACK TO mutation')
                self.db.execute('RELEASE mutation')

    def test_off_and_unresolved_claims_are_required(self):
        self.db.execute('UPDATE robot_settings SET enabled=1')
        with self.assertRaisesRegex(self.ns['Refuse'], 'ROBOT_OFF_REQUIRED'):
            self.ns['snapshot'](self.db)
        self.db.execute('UPDATE robot_settings SET enabled=0')
        self.db.execute("UPDATE api_requests SET state='NEEDS_REVIEW'")
        with self.assertRaisesRegex(self.ns['Refuse'], 'UNRESOLVED_PROVIDER_REQUEST'):
            self.ns['snapshot'](self.db)


class PnlInstallerBundleTests(unittest.TestCase):
    def test_direct_exchange_and_bound_errors_refuse_even_with_a_private_cache(self):
        from worker.binance_private import CODES
        from worker.pnl_calendar import unavailable
        for reason in sorted(set(CODES) | {'PNL_VALUE_LIMIT', 'PNL_CACHE_LIMIT'}):
            with self.subTest(reason=reason):
                with postflight_fixture(unavailable(reason), unavailable('HISTORY_LOADING')) as (ns, run, requests, ledger):
                    with self.assertRaisesRegex(ns['Refuse'], 'CALENDAR_COLLECTION_FAILED'):
                        run()
                    self.assertEqual(requests, [])
                    self.assertEqual(ledger.read_bytes(), b'unchanged financial journal')

    def test_authenticated_office_exchange_and_bound_errors_are_also_refused(self):
        from worker.binance_private import CODES
        from worker.pnl_calendar import unavailable
        for reason in sorted(set(CODES) | {'PNL_VALUE_LIMIT', 'PNL_CACHE_LIMIT'}):
            with self.subTest(reason=reason):
                with postflight_fixture(unavailable('HISTORY_LOADING'), unavailable(reason)) as (ns, run, requests, ledger):
                    with self.assertRaisesRegex(ns['Refuse'], 'CALENDAR_COLLECTION_FAILED'):
                        run()
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0].get_method(), 'GET')
                    self.assertEqual(ledger.read_bytes(), b'unchanged financial journal')

    def test_loading_budget_and_limited_scope_remain_valid_partial_history(self):
        from worker.pnl_calendar import unavailable
        direct = unavailable('HISTORY_LOADING')
        direct['incomplete_reasons'] += ['HISTORY_TIME_BUDGET', 'SYMBOL_DISCOVERY_NOT_EXHAUSTIVE', 'INCOME_PAGE_LIMIT']
        with postflight_fixture(direct, unavailable('HISTORY_LOADING')) as (ns, run, requests, ledger):
            result = run()
            self.assertEqual(result['status'], 'CALENDAR_READ_VERIFIED')
            self.assertEqual(result['collection_status'], 'UNAVAILABLE')
            self.assertFalse(result['complete'])
            self.assertEqual(len(requests), 1)
            self.assertNotIn('isolated-test-token', json.dumps(result))
            self.assertEqual(ledger.read_bytes(), b'unchanged financial journal')

    def test_postflight_checks_private_cache_and_uses_only_authenticated_get(self):
        ns = namespace()
        from worker.pnl_calendar import unavailable
        calendar = unavailable('HISTORY_LOADING')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trading = root / 'trading'
            trading.mkdir(mode=0o700)
            for name in ('pnl-history-v1.json', 'pnl-history.lock'):
                path = trading / name
                path.write_text('{}')
                path.chmod(0o600)
            private = trading / 'ledger.sqlite3'
            private.write_bytes(b'existing financial ledger is not opened for write')
            original_path = type(Path())
            class TokenPath(original_path):
                def read_text(self, *args, **kwargs):
                    if str(self) == '/run/office/read_token': return 'not-a-real-token'
                    return super().read_text(*args, **kwargs)
            ns.update(UID=os.geteuid(), Path=TokenPath)
            body = json.dumps(dict(robot={'robot_on': False}, pnl_calendar=calendar)).encode()
            class Response:
                code = 200
                def __enter__(self): return self
                def __exit__(self, *unused): pass
                def read(self, size):
                    self_read_sizes.append(size)
                    return body
            requests, self_read_sizes = [], []
            class Opener:
                def open(self, request, timeout):
                    requests.append(request)
                    return Response()
            with patch('worker.binance_private.BinanceReadOnly', return_value=object()), \
                 patch('worker.pnl_calendar.collect_calendar', return_value=calendar) as collect, \
                 patch('urllib.request.build_opener', return_value=Opener()):
                summary = ns['calendar_postflight'](ROOT, root)
            self.assertTrue(summary['office_http_get_verified'])
            self.assertEqual(requests[0].get_method(), 'GET')
            self.assertEqual(requests[0].full_url, 'http://127.0.0.1:8787/office/status')
            self.assertIsNone(requests[0].data)
            self.assertEqual(self_read_sizes, [2_000_001])
            self.assertNotIn('not-a-real-token', json.dumps(summary))
            self.assertEqual(collect.call_args.kwargs, dict(max_requests=4, budget_seconds=15))
            self.assertEqual(private.read_bytes(), b'existing financial ledger is not opened for write')

    def test_postflight_refuses_public_history_cache_modes(self):
        ns = namespace()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            root.chmod(0o700)
            path = root / 'pnl-history-v1.json'
            path.write_text('{}')
            path.chmod(0o644)
            ns['UID'] = os.geteuid()
            with self.assertRaisesRegex(ns['Refuse'], 'JOURNAL_FILE_INVALID'):
                ns['validate_cache'](path)

    def test_broken_cache_contract_is_not_reported_as_verified_history(self):
        ns = namespace()
        from worker.pnl_calendar import unavailable
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'trading').mkdir(mode=0o700)
            ns['UID'] = os.geteuid()
            with patch('worker.binance_private.BinanceReadOnly', return_value=object()), \
                 patch('worker.pnl_calendar.collect_calendar', return_value=unavailable('PNL_CACHE_INVALID')):
                with self.assertRaisesRegex(ns['Refuse'], 'CALENDAR_COLLECTION_FAILED'):
                    ns['calendar_postflight'](ROOT, root)

    def test_partial_calendar_contract_and_sanitized_day_summary(self):
        ns = namespace()
        calendar = dict(kind='CLOSED_POSITIONS_FROM_FILLS', timezone='Asia/Jakarta',
            start_date='2026-10-01', status='PARTIAL', complete=False,
            incomplete_reasons=['SYMBOL_DISCOVERY_NOT_EXHAUSTIVE'],
            days=[dict(date='2026-10-07', pnl_usdt='-6.52', known_pnl_usdt='-6.52',
                       closed_positions=1, complete=False, secret='never logged')])
        ns['calendar_contract'](calendar)
        summary = ns['calendar_summary'](calendar)
        self.assertEqual(summary['days'][0]['pnl_usdt'], '-6.52')
        self.assertEqual(summary['days'][0]['closed_positions'], 1)
        self.assertNotIn('secret', summary['days'][0])

    def test_calendar_does_not_accept_arbitrary_reason_or_number_text(self):
        ns = namespace()
        from worker.pnl_calendar import unavailable
        valid = unavailable('HISTORY_LOADING')
        valid['incomplete_reasons'] = ['api-key-secret']
        with self.assertRaisesRegex(ns['Refuse'], 'CALENDAR_CONTRACT_INVALID'):
            ns['calendar_contract'](valid)
        valid['incomplete_reasons'] = ['HISTORY_LOADING']
        valid['days'] = [dict(date='2026-10-07', pnl_usdt='secret', known_pnl_usdt='0',
                             closed_positions=1, complete=False)]
        with self.assertRaisesRegex(ns['Refuse'], 'CALENDAR_CONTRACT_INVALID'):
            ns['calendar_contract'](valid)

    def test_bash_syntax_and_private_gateway_and_robot_are_not_targets(self):
        path = ROOT / 'deploy/apply_pnl_calendar.sh'
        result = subprocess.run(['bash', '-n', str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        ns = namespace()
        self.assertIn('deploy/container_boot.py', ns['BASE_RUNTIME'])
        self.assertIn('deploy/runtime_permissions.py', ns['BASE_RUNTIME'])
        self.assertNotIn('worker/order_gateway.py', ns['TARGET_CONTENTS'])
        self.assertNotIn('worker/robot.py', ns['TARGET_CONTENTS'])
        self.assertEqual(ns['BASE_RUNTIME']['worker/order_gateway.py'], ns['TARGET_RUNTIME']['worker/order_gateway.py'])
        self.assertEqual(ns['BASE_RUNTIME']['worker/robot.py'], ns['TARGET_RUNTIME']['worker/robot.py'])

    def test_embedded_helper_hash_is_verified_before_all_runs(self):
        script = (ROOT / 'deploy/apply_pnl_calendar.sh').read_text()
        self.assertIn(hashlib.sha256(helper_source().encode()).hexdigest(), script)
        self.assertIn('harun_pnl_check_helper || return 1', script)
        self.assertNotIn('git fetch', script)
        self.assertNotIn('git pull', script)

    def test_rollback_stays_armed_until_calendar_and_journal_checks_pass(self):
        script = (ROOT / 'deploy/apply_pnl_calendar.sh').read_text()
        off = script.index('HARUN_PNL_STAGE=FRESH_OFF_STATUS')
        calendar = script.index('HARUN_PNL_STAGE=CALENDAR_POSTFLIGHT')
        final = script.index('HARUN_PNL_STAGE=FINAL_OFF_JOURNAL_CHECK')
        disarm = script.index('HARUN_PNL_RESTORE_READY=0', script.index('HARUN_PNL_RESTORE_READY=1'))
        self.assertLess(off, calendar)
        self.assertLess(calendar, final)
        self.assertLess(final, disarm)
        self.assertIn('harun_pnl_host --mode restore-sources', script)
        self.assertIn('docker image tag "$HARUN_PNL_OLD_IMAGE" harun-office-worker:latest', script)
        self.assertIn('exchange_changed_during_update', helper_source())

    def test_artifact_roundtrip_matches_selfcontained_installer(self):
        artifact = Path('/workspace/vps-order-recovery/termius-pnl-calendar.txt')
        if not artifact.is_file(): self.skipTest('Termius handoff is not part of shallow CI')
        source = artifact.read_text()
        marker = "<<'HARUN_PNL_PAYLOAD'\n"
        start = source.index(marker) + len(marker)
        block = source[start:source.index('\nHARUN_PNL_PAYLOAD\n', start)]
        tree = ast.parse(block)
        packed = next(ast.literal_eval(node.value) for node in tree.body
                      if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'packed' for target in node.targets))
        data = lzma.decompress(base64.b64decode(packed), format=lzma.FORMAT_ALONE)
        self.assertEqual(data, (ROOT / 'deploy/apply_pnl_calendar.sh').read_bytes())
        self.assertIn(hashlib.sha256(data).hexdigest(), block)


if __name__ == '__main__': unittest.main()
