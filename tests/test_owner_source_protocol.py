"""Offline tests of raw native receipts and refreshed material handoff.

No browser, HTTP client, scheduler, gateway, or service is started here. Actual
captured provider source is exercised against an isolated in-memory journal.
"""
import ast
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent import native_pending_protocol as pending
from worker import neuroapi
from worker.core import Review
from worker import owner_source_protocol as owner
from tests.test_agent_native_market_context import native_context

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_793_491_250.0
REF = hashlib.sha256(b'owned-order-test').hexdigest()


def utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace('+00:00', 'Z')


def charts(when=NOW, source=owner.WEBSITE_SOURCE):
    return [dict(source=source, symbol='BTCUSDT', timeframe=tf,
        captured_at=utc(when), sha256=char * 64, bytes=2048, attachment_id='file-' + tf)
        for tf, char in (('1h', 'a'), ('15m', 'b'))]


class ParserTests(unittest.TestCase):
    def legacy(self, text):
        try:
            result = json.loads(text, parse_float=Decimal)
            if type(result) is not dict:
                raise ValueError
            return result
        except Exception:
            raise Review('INVALID_OUTPUT_SCHEMA')

    def read(self, text, body):
        return owner.parse_bound_assistant_text(text, body, self.legacy, max_assistant_bytes=512000)

    def test_natural_analysis_and_legacy_json_use_same_exact_decimal_wire_types(self):
        body = dict(output_schema=neuroapi.SETUP_SCHEMA_V4, context=dict(symbol='BTCUSDT'))
        text = 'Ringkasan akhir\nCOIN: BTCUSDT\nARAH: LONG\nENTRY: 100.25\nSL: 99.25\nTP: 101.75'
        result = self.read(text, body)
        self.assertEqual(result['limit_entry'], Decimal('100.25'))
        self.assertEqual(result['take_profit'], Decimal('101.75'))
        self.assertIsInstance(result['risk_reward'], Decimal)
        self.assertEqual(result, self.read(json.dumps(dict(result), default=float), body))
        self.assertEqual(text, 'Ringkasan akhir\nCOIN: BTCUSDT\nARAH: LONG\nENTRY: 100.25\nSL: 99.25\nTP: 101.75')

    def test_natural_screening_and_pending_bound_to_owned_symbol_reference(self):
        screen = dict(output_schema=neuroapi.SCREEN_ONE_SCHEMA)
        self.assertEqual(self.read('Coin: BTCUSDT', screen), dict(symbols=['BTCUSDT']))
        body = dict(operation='pending-review-v1:' + REF[:32] + ':1',
            output_schema=pending.REVIEW_SCHEMA,
            context=dict(symbol='BTCUSDT', pending_review=dict(symbol='BTCUSDT', intent_ref=REF)))
        self.assertEqual(self.read('COIN: BTCUSDT\nKEPUTUSAN: TAHAN', body),
            dict(symbol='BTCUSDT', order_ref=REF, decision='TAHAN'))
        for text in ('COIN: ETHUSDT\nKEPUTUSAN: BATALKAN',
            'COIN: BTCUSDT\nData screenshot belum terbaca.\nKEPUTUSAN: BATALKAN'):
            with self.assertRaisesRegex(Review, 'INVALID_OUTPUT_SCHEMA'):
                self.read(text, body)

    def test_no_missing_prices_or_technical_hold_or_oversize_invented(self):
        body = dict(output_schema=neuroapi.SETUP_SCHEMA_V4, context=dict(symbol='BTCUSDT'))
        for text in ('COIN: BTCUSDT\nARAH: LONG',
            'COIN: BTCUSDT\nARAH: HOLD\nLampiran belum terbaca.',
            'COIN: ETHUSDT\nARAH: HOLD'):
            with self.subTest(text=text), self.assertRaisesRegex(Review, 'INVALID_OUTPUT_SCHEMA'):
                self.read(text, body)
        with self.assertRaisesRegex(Review, 'INVALID_OUTPUT_SCHEMA'):
            owner.parse_bound_assistant_text('x' * 20, body, self.legacy, max_assistant_bytes=10)


class ReleaseArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'release.json'
        self.fixture_uid = 10001
        self.real_fstat = os.fstat
        self.value = dict(version=owner.RELEASE_VERSION, policy=owner.RELEASE_POLICY,
            office_commit='a' * 40, released_at=utc(NOW))
        self.write()

    def write(self, value=None):
        self.path.write_text(json.dumps(self.value if value is None else value))
        self.path.chmod(0o600)

    def read(self):
        def runtime_metadata(fd):
            # This workspace runs as uid1000. Model the reviewed runtime uid
            # while keeping real file mode/nlink/type/size and open semantics.
            values = list(self.real_fstat(fd));values[4] = self.fixture_uid
            return os.stat_result(values)
        with patch.object(owner.time, 'time', return_value=NOW), patch.object(owner.os, 'fstat', side_effect=runtime_metadata):
            return owner.load_owner_release_cutoff(self.path)

    def test_reads_exact_private_actual_cutover_artifact_without_writing_it(self):
        before = self.path.read_bytes()
        self.assertEqual(self.read(), self.value['released_at'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_symlink_hardlink_public_permissions_or_wrong_policy_refuses(self):
        for mode in (0o644, 0o666, 0o400):
            self.path.chmod(mode)
            with self.subTest(mode=mode), self.assertRaisesRegex(Review, 'OWNER_RELEASE_ARTIFACT_UNVERIFIED'):
                self.read()
        self.path.chmod(0o600)
        self.fixture_uid = 1000
        with self.assertRaisesRegex(Review, 'OWNER_RELEASE_ARTIFACT_UNVERIFIED'):self.read()
        self.fixture_uid = 10001
        other = self.path.with_name('other.json')
        os.link(self.path, other)
        with self.assertRaisesRegex(Review, 'OWNER_RELEASE_ARTIFACT_UNVERIFIED'):self.read()
        other.unlink()
        original = self.path.with_name('original.json');self.path.rename(original)
        self.path.symlink_to(original)
        with self.assertRaisesRegex(Review, 'OWNER_RELEASE_ARTIFACT_UNVERIFIED'):self.read()
        self.path.unlink()
        with self.assertRaisesRegex(Review, 'OWNER_RELEASE_ARTIFACT_UNVERIFIED'):self.read()

    def test_wrong_schema_duplicate_field_future_time_or_nonUTC_refuses(self):
        cases = [dict(self.value, policy='legacy'),dict(self.value, version='old'),
            dict(self.value, office_commit='not-a-commit'),dict(self.value, extra=True),
            dict(self.value, released_at=utc(NOW + 60)),
            dict(self.value, released_at='2026-11-01T01:00:00'),
            dict(self.value, released_at='2026-11-01T01:00:00+01:00')]
        for value in cases:
            self.write(value)
            with self.subTest(value=value),self.assertRaisesRegex(Review, 'OWNER_RELEASE_ARTIFACT_UNVERIFIED'):
                self.read()
        self.path.write_text(json.dumps(self.value)[:-1]+',"policy":"legacy"}')
        with self.assertRaisesRegex(Review, 'OWNER_RELEASE_ARTIFACT_UNVERIFIED'):self.read()


class FreshHandoffTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE research_browser_requests(operation TEXT PRIMARY KEY,request_body TEXT,request_sha256 TEXT);
          CREATE TABLE api_requests(operation TEXT PRIMARY KEY,state TEXT,body_hash TEXT);
          CREATE TABLE research_browser_responses(operation TEXT PRIMARY KEY,received_at REAL,chart_evidence TEXT,chart_max_age INTEGER);
        ''')
        self.addCleanup(self.db.close)
        self.context = native_context(NOW - 600)
        self.context['material_contract'] = owner.MATERIAL_CONTRACT
        self.context['pending_order_context'] = {'entry_price': '100', 'quantity': '1', 'accepted_at': utc(NOW - 20000)}
        self.market = SimpleNamespace(data=Mock(return_value=native_context(NOW)), fresh=Mock())
        self.reader = Mock(return_value=dict(source_type='NEUROBRO_BROWSER', provider_evidence_sha256='f' * 64))
        self.record()

    def record(self, *, schema=None, received=NOW, rows=None):
        body = dict(output_schema=neuroapi.SETUP_SCHEMA_V4 if schema is None else schema,
            context=self.context)
        self.db.execute('INSERT OR REPLACE INTO research_browser_requests VALUES(?,?,?)',
            ('analysis', json.dumps(body), 'a' * 64))
        self.db.execute('INSERT OR REPLACE INTO api_requests VALUES(?,?,?)', ('analysis', 'COMPLETE', 'a' * 64))
        self.db.execute('INSERT OR REPLACE INTO research_browser_responses VALUES(?,?,?,?)',
            ('analysis', received, json.dumps(charts() if rows is None else rows), 180))

    def handoff(self, **kwargs):
        return owner.completed_decision_fresh(self.db, 'analysis', self.market,
            'BTCUSDT', now=NOW, source_reader=self.reader, browser_source='NEUROBRO_BROWSER', **kwargs)

    def test_long_initial_context_requires_fresh_bound_final_website_materials(self):
        self.assertTrue(self.handoff())
        self.reader.assert_called_once_with(self.db, 'analysis')
        self.market.fresh.assert_not_called()
        self.market.data.assert_not_called()
        result = self.handoff(refresh=True)
        self.market.data.assert_called_once_with('BTCUSDT')
        self.market.fresh.assert_called_once_with(self.market.data.return_value, 'BTCUSDT')
        self.assertEqual(result['fetched_at'], NOW)
        self.assertEqual(result['material_contract'], owner.MATERIAL_CONTRACT)
        self.assertEqual(result['pending_order_context'], self.context['pending_order_context'])
        self.assertEqual(self.context['fetched_at'], NOW - 600)

    def test_pending_contract_requires_explicit_version_and_final_native_authority(self):
        self.record(schema=pending.REVIEW_SCHEMA)
        self.assertTrue(self.handoff())
        self.context.pop('material_contract')
        self.record(schema=pending.REVIEW_SCHEMA)
        self.market.fresh.side_effect = Review('LEGACY_STALE')
        with self.assertRaisesRegex(Review, 'LEGACY_STALE'):
            self.handoff()
        self.market.fresh.assert_called_once_with(self.context, 'BTCUSDT')

    def test_legacy_initial_freshness_is_never_relaxed(self):
        self.record(schema=neuroapi.SETUP_SCHEMA)
        self.market.fresh.side_effect = Review('LEGACY_STALE')
        with self.assertRaisesRegex(Review, 'LEGACY_STALE'):
            self.handoff(refresh=True)
        self.market.data.assert_not_called()

    def test_stale_final_response_stale_chart_foreign_source_or_missing_frame_rejected(self):
        variations = [dict(received=NOW - 181), dict(rows=charts(NOW - 181)),
            dict(rows=charts(NOW + 1)), dict(rows=charts(source='BINANCE_FUTURES_200_CLOSED_DATA_RENDER')),
            dict(rows=charts()[:1])]
        for change in variations:
            self.record(**change)
            with self.subTest(change=change), self.assertRaises(Review):
                self.handoff(refresh=True)
        self.market.data.assert_not_called()
        self.record(received=NOW - 181)
        with self.assertRaises(Review):self.handoff(max_age=300)

    def test_hash_or_native_output_authority_failure_never_refreshes_or_writes(self):
        self.reader.side_effect = Review('ROBOT_SETUP_UNVERIFIED')
        before = tuple(self.db.iterdump())
        with self.assertRaisesRegex(Review, 'ROBOT_SETUP_UNVERIFIED'):
            self.handoff(refresh=True)
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.market.data.assert_not_called()
        self.reader.side_effect = None
        self.db.execute("UPDATE api_requests SET body_hash='wrong'")
        with self.assertRaisesRegex(Review, 'ROBOT_REQUEST_NEEDS_REVIEW'):
            self.handoff()


class ActualNativeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.modules = patch.dict(sys.modules)
        self.modules.start();self.addCleanup(self.modules.stop)
        self.canonical = patch.object(neuroapi, 'canonical_output', neuroapi.canonical_output)
        self.canonical.start();self.addCleanup(self.canonical.stop)
        self.cutoff = patch.object(owner, 'load_owner_release_cutoff', return_value=utc(NOW))
        self.cutoff.start();self.addCleanup(self.cutoff.stop)
        def load(name, relative):
            spec = importlib.util.spec_from_file_location(name, ROOT / relative)
            module = importlib.util.module_from_spec(spec);sys.modules[name] = module
            spec.loader.exec_module(module)
            return module
        # Exact checked-in successors of the authenticated captured sources;
        # loading defines validators only and starts no application or client.
        self.provider = load('worker.research_provider', 'vps_native/worker/research_provider.py')
        self.browser = load('worker.neurobrowser', 'vps_native/worker/neurobrowser.py')
        self.db = sqlite3.connect(':memory:', isolation_level=None);self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.text = 'Ringkasan akhir\nCOIN: BTCUSDT\nARAH: LONG\nENTRY: 100.25\nSL: 99.25\nTP: 101.75'
        self.value = dict(symbol='BTCUSDT', side='LONG', limit_entry=Decimal('100.25'),
            stop_loss=Decimal('99.25'), take_profit=Decimal('101.75'), risk_reward=Decimal('1.5'))
        self.calls = []
        def transport(method, url, headers=None, body=None, timeout=15):
            self.calls.append((method, timeout))
            if method == 'GET':
                return 200, {}, dict(provider='NEUROBRO_WEB', status='CONNECTED',
                    checked_at=datetime.now(timezone.utc).isoformat(), profile_persistent=True)
            return 200, {}, dict(operation=body['operation'], marker=body['marker'],
                request_sha256=body['request_sha256'], output=self.value,
                assistant_text=self.text, response_id='offline-native-natural-receipt',
                chart_evidence=charts(NOW - 1))
        self.actor = self.browser.NeuroBrowser(SimpleNamespace(db=self.db), token='x'*32, transport=transport)
        self.context = native_context(NOW - 600)

    def ask(self):
        validate = lambda value: neuroapi.setup(value, 'BTCUSDT', require_declared_rr=False, require_gross_rr=False)
        with patch.object(self.browser.time, 'time', return_value=NOW):
            return self.actor.ask('native-v4-raw-offline', 'Analisis bahan terbaru.',
                neuroapi.SETUP_SCHEMA_V4, validate, self.context, continue_if=lambda: True)

    def test_actual_native_v4_ask_revalidates_raw_receipt_and_cached_result_does_not_resend(self):
        self.assertEqual(self.ask(), self.value)
        self.assertEqual(self.ask(), self.value)
        self.assertEqual([method for method, _ in self.calls].count('POST'), 1)
        self.assertIn(('POST', 930), self.calls)
        self.assertEqual(self.db.execute('SELECT state FROM api_requests').fetchone()['state'], 'COMPLETE')
        self.assertEqual(self.db.execute('SELECT assistant_text FROM research_browser_responses').fetchone()['assistant_text'], self.text)
        proof = self.provider.source_evidence(self.db, 'native-v4-raw-offline')
        self.assertEqual(proof['source_type'], 'NEUROBRO_BROWSER')
        market = SimpleNamespace(fresh=Mock())
        self.assertTrue(owner.completed_decision_fresh(self.db, 'native-v4-raw-offline', market,
            'BTCUSDT', now=NOW, source_reader=self.provider.source_evidence, browser_source='NEUROBRO_BROWSER'))
        market.fresh.assert_not_called()
        self.db.execute("UPDATE research_browser_responses SET assistant_sha256=?", ('0'*64,))
        with self.assertRaisesRegex(Review, 'ROBOT_SETUP_UNVERIFIED'):
            self.provider.source_evidence(self.db, 'native-v4-raw-offline')
        with self.assertRaises(Review):self.ask()
        self.assertEqual([method for method, _ in self.calls].count('POST'), 1)

    def test_actual_native_provider_preserves_old_data_render_receipts_without_admitting_old_live_uploads(self):
        self.ask()
        row = self.db.execute('SELECT chart_evidence FROM research_browser_responses').fetchone()
        old_charts = json.loads(row['chart_evidence'])
        for chart in old_charts:chart['source'] = 'BINANCE_FUTURES_200_CLOSED_DATA_RENDER'
        self.db.execute('UPDATE research_browser_responses SET chart_evidence=?',
            (self.provider.response_output_text(old_charts),))
        self.assertEqual(self.provider.source_evidence(self.db, 'native-v4-raw-offline')['source_type'], 'NEUROBRO_BROWSER')
        with self.assertRaises(Review):
            self.provider.validate_chart_evidence(old_charts, neuroapi.SETUP_SCHEMA_V4,
                self.context, NOW - 600, NOW, 180)


class ActualNativeDispatchTests(unittest.TestCase):
    def setUp(self):
        # Execute only the exact pure dispatch function, not application startup.
        source = ROOT / 'vps_native/worker/neurobrowser.py'
        tree = ast.parse(source.read_bytes())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'verify_dispatch')
        from zoneinfo import ZoneInfo
        namespace = dict(json=json, math=__import__('math'), re=__import__('re'),
            datetime=datetime, ZoneInfo=ZoneInfo, unique=lambda pairs: dict(pairs),
            BROWSER_PROVIDER='NEUROBRO_WEB', BROWSER_CONTRACT='contract',
            SETUP_SCHEMA=neuroapi.SETUP_SCHEMA, SETUP_SCHEMA_V4=neuroapi.SETUP_SCHEMA_V4,
            FEE_SOURCE='fee-source', owner_material_request=owner.owner_material_request,
            NEW_OPERATION_SECONDS=owner.NEW_OPERATION_SECONDS,
            screening_count=neuroapi.screening_count, validated_risk_target=lambda value: Decimal(value))
        def body(operation, prompt, schema, context):
            data = dict(operation=operation, prompt=prompt, output_schema=schema, context=context,
                marker='HARUN_' + hashlib.sha256(operation.encode()).hexdigest()[:32])
            raw = json.dumps(data, sort_keys=True)
            return data, raw, hashlib.sha256(raw.encode()).hexdigest()
        namespace['request_body'] = body
        namespace['marker_for_operation'] = lambda operation: body(operation, '', {}, None)[0]['marker']
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<native-pure-dispatch>', 'exec'), namespace)
        self.verify, self.body = namespace['verify_dispatch'], body
        self.now = NOW
        self.created = self.now - 600
        self.today = datetime.fromtimestamp(self.now, ZoneInfo('Asia/Jakarta')).date().isoformat()
        self.cycle = self.today + ':robot-v9:0'
        self.operation = self.cycle + ':analysis-v9:BTCUSDT'
        self.context = native_context(self.created)
        self.context.update(contract_rules=dict(symbol='BTCUSDT', source='Binance Futures /fapi/v1/exchangeInfo',
            observed_at=self.created), risk_constraints=dict(fee_symbol='BTCUSDT', fee_source='fee-source',
            fee_observed_at=self.created, maximum_loss_at_sl_usdt='5', target_loss_at_sl_usdt='5'))
        self.db = sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.executescript('''
          CREATE TABLE api_requests(operation TEXT,state TEXT,attempts INTEGER,output TEXT,idempotency TEXT,created REAL,body_hash TEXT);
          CREATE TABLE research_browser_requests(operation TEXT,provider TEXT,contract_version TEXT,marker TEXT,request_sha256 TEXT,request_body TEXT);
          CREATE TABLE robot_jobs(operation TEXT,state TEXT,cycle TEXT,kind TEXT,symbol TEXT,risk_target TEXT);
          CREATE TABLE research_browser_responses(operation TEXT);
          CREATE TABLE robot_cycles(id TEXT,state TEXT,day TEXT,entry_epoch INTEGER,data TEXT);
        ''')
        self.addCleanup(self.db.close)
        self.record()

    def record(self, schema=None):
        for table in ('api_requests', 'research_browser_requests', 'robot_jobs', 'robot_cycles'):
            self.db.execute('DELETE FROM ' + table)
        body, raw, digest = self.body(self.operation, 'Analisis terbaru.',
            neuroapi.SETUP_SCHEMA_V4 if schema is None else schema, self.context)
        self.digest, self.marker = digest, body['marker']
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?)',
            (self.operation, 'PENDING', 1, None, None, self.created, digest))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',
            (self.operation, 'NEUROBRO_WEB', 'contract', self.marker, digest, raw))
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
            (self.operation, 'PENDING', self.cycle, 'ANALYSIS', 'BTCUSDT', '5'))
        witness = dict(version='ANALYSIS_CONTEXT_FRESHNESS_V1', symbol='BTCUSDT', source='Binance Futures',
            fetched_at=self.context['fetched_at'], server_time=self.context['server_time'], claimed_at=self.created,
            timeframes={tf:dict(symbol='BTCUSDT',timeframe=tf,last_close_time=self.context['timeframes'][tf]['candles'][-1]['close_time']) for tf in ('1h','15m')})
        data = dict(queue=['BTCUSDT'], analysis_contexts={self.operation:witness})
        self.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',
            (self.cycle, 'ACTIVE', self.today, 0, json.dumps(data)))

    def check(self, now=None):
        return self.verify(self.db, self.operation, self.marker, self.digest, self.today, self.now if now is None else now)

    def test_v4_initial_context_asof_journal_not_now_and_finite900budget(self):
        self.assertTrue(self.check())
        self.assertFalse(self.check(self.created + 901))
        self.record(schema=neuroapi.SETUP_SCHEMA)
        self.assertFalse(self.check())

    def test_new_schema_never_relaxes_current_queue_cycle_witness_hash_or_attempt(self):
        changes = [("UPDATE api_requests SET attempts=2", ()),
            ("UPDATE api_requests SET body_hash='wrong'", ()),
            ("UPDATE robot_cycles SET state='COMPLETE'", ()),
            ("UPDATE robot_cycles SET day='2000-01-01'", ()),
            ("UPDATE robot_jobs SET symbol='ETHUSDT'", ()),
            ("INSERT INTO research_browser_responses VALUES(?)", (self.operation,))]
        for sql, params in changes:
            self.db.execute('SAVEPOINT mutation')
            self.db.execute(sql, params)
            with self.subTest(sql=sql):self.assertFalse(self.check())
            self.db.execute('ROLLBACK TO mutation');self.db.execute('RELEASE mutation')


if __name__ == '__main__':
    unittest.main()
