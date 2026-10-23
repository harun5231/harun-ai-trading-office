"""Public-data fixtures, private provenance admission and real local SDK cleanup.

No exchange account, model, Neurobro or production browser is contacted.
"""
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import agent.native_chart_followup as followup
from agent.native_chart_capture import PHASES, SIDECAR, SOURCE, install
from agent.native_chart_rpc import make_rpc
from agent.native_market_context import convert_native_market_context
from agent.market import BinanceFuturesMarket
from agent.models import AgentError
from tests.test_agent_closed_candles_200 import NOW, native_context_200, public_transport
from tests.test_native_chart_capture import Context, Harness, png_from_rgb


def market_snapshot(now=NOW):
    return BinanceFuturesMarket(transport=lambda path, params: public_transport(path, params, now=now),
        clock=lambda: now).snapshot('BTCUSDT')


def capture_namespace(*, real=False):
    harness = Harness()
    namespace = harness.native
    original_archive = namespace['archive_images']
    def archive(*args):
        value = original_archive(*args)
        return {'paths': value['reviewed_images_key'], 'evidence': value['evidence']}
    namespace['archive_images'] = archive
    rpc = make_rpc(expected_playwright_version=version('playwright')) if real else harness.rpc
    install(namespace, expected_source=SOURCE, expected_capture=namespace['capture'],
        images_key='paths', phase_map={phase: phase for phase in PHASES}, rpc=rpc,
        monotonic=time.monotonic if real else lambda: harness.current,
        wall_clock=lambda: NOW)
    return harness, SimpleNamespace(**namespace)


class PublicSnapshotTests(unittest.TestCase):
    def test_actual_six_public_get_fixture_roundtrips_all_400_values_and_running_rows(self):
        calls = []
        def public(path, params):
            calls.append((path, copy.deepcopy(params)))
            return public_transport(path, params)
        observed = BinanceFuturesMarket(transport=public, clock=lambda: NOW).snapshot('BTCUSDT')
        context = followup.native_snapshot(observed, now=NOW)
        copied = convert_native_market_context(context, now=NOW)
        self.assertEqual(len(calls), 6)
        self.assertEqual([params['limit'] for path, params in calls if path.endswith('klines')], [202, 202])
        self.assertEqual(copied['candles'], observed['candles'])
        self.assertEqual(copied['running_candles'], observed['running_candles'])
        self.assertEqual({tf: len(frame['candles']) for tf, frame in context['timeframes'].items()},
            {'15m': 201, '1h': 201})
        self.assertEqual(set(context), {'source', 'symbol', 'fetched_at', 'server_time',
            'mark_price', 'quantity_unit', 'timeframes'})
        self.assertFalse(any('signature' in params or 'timestamp' in params for _, params in calls))

    def test_changed_closed_series_final_clock_or_missing_running_never_becomes_native_data(self):
        for mutation in (lambda value: value['candles']['15m'].pop(0),
                lambda value: value.update(collection_finished_server_time_ms=value['server_time_ms']+900000),
                lambda value: value['running_candles'].update(**{'1h': None}),
                lambda value: value.update(mark_time_ms=value['collection_finished_server_time_ms']+1)):
            value = market_snapshot(); mutation(value)
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, AgentError)):
                followup.native_snapshot(value, now=NOW)

    def process(self, output, *, hung=False):
        class Process:
            returncode = None
            def __init__(self):
                self.stdin, self.stdout = io.BytesIO(), io.BytesIO()
                self.inputs, self.waits = [], []
                self.terminated = self.killed = False
            def communicate(self, input=None, timeout=None):
                self.inputs.append(input)
                if len(self.inputs) == 1 or hung:
                    raise subprocess.TimeoutExpired('fixture', timeout)
                self.returncode = 0
                return output, None
            def poll(self): return self.returncode
            def terminate(self): self.terminated = True
            def kill(self): self.killed = True
            def wait(self, timeout=None):
                self.waits.append(timeout)
                if hung and not self.killed: raise subprocess.TimeoutExpired('fixture', timeout)
                self.returncode = -9 if self.killed else -15
                return self.returncode
        return Process()

    def test_child_has_no_credential_environment_and_reuses_one_bounded_communicate(self):
        value = market_snapshot(); process = self.process(followup.canonical(value))
        with patch.object(followup.subprocess, 'Popen', return_value=process) as create:
            actual = followup.public_snapshot('BTCUSDT', deadline=time.monotonic()+5, gate=lambda: True)
        self.assertEqual(actual, value); create.assert_called_once()
        command = create.call_args.args[0]; kwargs = create.call_args.kwargs
        self.assertEqual(command[1:5], ['-I', '-B', '-S', '-c'])
        self.assertEqual(kwargs['env'], {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'})
        self.assertTrue(kwargs['close_fds']); self.assertIs(kwargs['stderr'], subprocess.DEVNULL)
        self.assertIsNotNone(process.inputs[0]); self.assertIsNone(process.inputs[1])
        self.assertTrue(process.stdin.closed and process.stdout.closed)
        self.assertFalse(process.terminated)

    def test_off_before_child_or_after_timeout_does_not_retry_and_kills_only_owned_child(self):
        with patch.object(followup.subprocess, 'Popen') as create:
            with self.assertRaises(followup.FollowupUnavailable):
                followup.public_snapshot('BTCUSDT', deadline=time.monotonic()+5, gate=lambda: False)
            create.assert_not_called()
        process = self.process(b'', hung=True); gates = iter((True, True, False))
        with patch.object(followup.subprocess, 'Popen', return_value=process) as create:
            with self.assertRaises(followup.FollowupUnavailable):
                followup.public_snapshot('BTCUSDT', deadline=time.monotonic()+5, gate=lambda: next(gates))
        create.assert_called_once(); self.assertEqual(len(process.inputs), 1)
        self.assertTrue(process.terminated and process.killed)
        self.assertEqual(process.waits, [2, 2])
        self.assertTrue(process.stdin.closed and process.stdout.closed)

    def test_invalid_symbol_or_deadline_has_no_child_invocation(self):
        for symbol, deadline in (('BTCUSDT?secret=x', 10), ('BTCUSDT', True), ('BTCUSDT', float('nan'))):
            with self.subTest(symbol=symbol, deadline=str(deadline)), patch.object(followup.subprocess, 'Popen') as create:
                with self.assertRaises(followup.FollowupUnavailable):
                    followup.public_snapshot(symbol, deadline=deadline, gate=lambda: True, monotonic=lambda: 0)
                create.assert_not_called()

    def test_nonzero_empty_oversized_or_nonobject_child_output_is_never_retried(self):
        for code, output in ((1, b'{}'), (0, b''), (0, b'x'*(followup.MAX_BYTES+1)), (0, b'[]')):
            process = self.process(output)
            def completed(input=None, timeout=None):
                process.inputs.append(input); process.returncode = code
                return output, None
            process.communicate = completed
            with self.subTest(code=code, bytes=len(output)), patch.object(followup.subprocess, 'Popen', return_value=process) as create:
                with self.assertRaises(followup.FollowupUnavailable):
                    followup.public_snapshot('BTCUSDT', deadline=time.monotonic()+5, gate=lambda: True)
            create.assert_called_once(); self.assertEqual(len(process.inputs), 1)
            self.assertTrue(process.stdin.closed and process.stdout.closed)


class CaptureProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.root = Path(cls.fixture.name)/'archive'
        _, cls.charts = capture_namespace()
        cls.context = native_context_200()
        cls.payload = {'marker': 'followup_fixture', 'request_sha256': 'a'*64}
        cls.result = cls.charts.capture(Context(), cls.context, cls.payload['marker'],
            cls.payload['request_sha256'], archive_root=str(cls.root), deadline=30)
    @classmethod
    def tearDownClass(cls): cls.fixture.cleanup()
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.copy_root = Path(self.folder.name)/'archive'
        shutil.copytree(self.root, self.copy_root)
        self.value = copy.deepcopy(self.result)
        self.value['paths'] = [str(self.copy_root/Path(path).relative_to(self.root)) for path in self.result['paths']]
        self.sidecar = Path(self.value['paths'][0]).parent/SIDECAR
    def verify(self):
        return followup.verify_capture(self.charts, self.value, self.context, self.payload,
            archive_root=str(self.copy_root), now=NOW, gate=lambda: True)
    def mutate(self, change):
        sidecar = json.loads(self.sidecar.read_text()); change(sidecar)
        self.sidecar.write_bytes(followup.canonical(sidecar))

    def test_actual_capture_sidecar_and_decoded_pngs_bind_full_400_and_exact_native_context(self):
        result = self.verify(); self.assertEqual(result, self.value)
        self.assertIsNot(result, self.value)
        sidecar = json.loads(self.sidecar.read_text())
        self.assertEqual(sidecar['native_context_canonical_sha256'], followup.sha(self.context))
        self.assertEqual(sum(len(rows) for rows in sidecar['public_market_context']['candles'].values()), 400)

    def test_wrong_request_context_source_provenance_or_truth_flags_are_refused(self):
        mutations = [lambda value: value.update(marker='other_marker'),
            lambda value: value.update(request_sha256='b'*64),
            lambda value: value.update(native_context_canonical_sha256='c'*64),
            lambda value: value.update(source='BINANCE_USDM_FUTURES_WEB'),
            lambda value: value.update(network_navigation_performed=True),
            lambda value: value.update(persistent_browser_context_used=True),
            lambda value: value['public_market_context']['candles']['15m'].pop(0),
            lambda value: value['charts']['15m']['provenance'].update(closed_candle_count=199),
            lambda value: value['charts']['15m'].update(exchange_website_screenshot=True),
            lambda value: value['charts']['15m'].update(screenshot_rgb_sha256='d'*64),
            lambda value: value.update(unreviewed_field=True)]
        original = self.sidecar.read_bytes()
        for index, change in enumerate(mutations):
            self.sidecar.write_bytes(original); self.mutate(change)
            with self.subTest(index=index), self.assertRaises(followup.FollowupUnavailable): self.verify()

    def test_valid_png_with_wrong_pixels_and_rehashed_receipts_is_still_refused(self):
        target = Path(self.value['paths'][0]); raw = png_from_rgb(target.read_bytes(), wrong_pixel=True)
        target.write_bytes(raw); digest = hashlib.sha256(raw).hexdigest()
        self.value['evidence'][0].update(sha256=digest, bytes=len(raw))
        def changed(value):
            value['legacy_evidence'] = copy.deepcopy(self.value['evidence'])
            value['charts']['15m']['screenshot_png_sha256'] = digest
        self.mutate(changed)
        with self.assertRaises(followup.FollowupUnavailable): self.verify()

    def test_sidecar_mode_symlink_hardlink_and_duplicate_keys_are_refused(self):
        original = self.sidecar.read_bytes()
        for kind in ('mode', 'symlink', 'hardlink', 'duplicate'):
            self.sidecar.unlink(missing_ok=True); self.sidecar.write_bytes(original); self.sidecar.chmod(0o600)
            other = Path(self.folder.name)/'other'
            other.unlink(missing_ok=True)
            if kind == 'mode': self.sidecar.chmod(0o644)
            elif kind == 'duplicate': self.sidecar.write_bytes(b'{"source":"a","source":"b"}')
            elif kind == 'symlink':
                other.write_bytes(original); self.sidecar.unlink(); self.sidecar.symlink_to(other)
            else: os.link(self.sidecar, other)
            with self.subTest(kind=kind), self.assertRaises((OSError, ValueError)): self.verify()

    def test_png_symlink_or_archive_escape_is_refused(self):
        target = Path(self.value['paths'][0]); other = Path(self.folder.name)/'outside.png'
        shutil.copyfile(target, other); target.unlink(); target.symlink_to(other)
        with self.assertRaises((OSError, ValueError)): self.verify()
        self.value['paths'][0] = str(other)
        with self.assertRaises(followup.FollowupUnavailable): self.verify()

    def test_off_during_verification_refuses_result(self):
        calls = []
        def gate(): calls.append(True); return len(calls) < 3
        with self.assertRaises(followup.FollowupUnavailable):
            followup.verify_capture(self.charts, self.value, self.context, self.payload,
                archive_root=str(self.copy_root), now=NOW, gate=gate)


class ReceiptStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)/'archive'; self.root.mkdir(mode=0o700)
        self.store = followup.ReceiptStore(self.root, monotonic=lambda: 1)
        self.intent = {'schema_version': 1, 'operation': 'native:analysis:owned',
            'request_sha256': 'a'*64, 'kind': 'NATIVE_200_CHART_FOLLOWUP', 'proof': 'fixture'}
        self.complete = {**self.intent, 'intent_sha256': followup.sha(self.intent),
            'response_id': 'native:response', 'final_reply_sha256': 'b'*64}
        self.key = followup.sha({key: self.intent[key] for key in ('operation', 'request_sha256')})
    def test_intent_and_complete_are_private_exclusive_and_bound_without_overwrite(self):
        self.assertTrue(self.store(stage='INTENT', receipt=self.intent, deadline=10))
        path = self.root/'dialogue-200-followup'/(self.key+'.intent.json')
        before = path.read_bytes(); self.assertEqual(before, followup.canonical(self.intent))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600); self.assertEqual(path.stat().st_nlink, 1)
        with self.assertRaises(FileExistsError): self.store(stage='INTENT', receipt=self.intent, deadline=10)
        self.assertTrue(self.store(stage='COMPLETE', receipt=self.complete, deadline=10))
        with self.assertRaises(FileExistsError): self.store(stage='COMPLETE', receipt=self.complete, deadline=10)
        self.assertEqual(path.read_bytes(), before)
    def test_incomplete_intent_is_preserved_after_bad_complete_and_cannot_be_replayed(self):
        self.store(stage='INTENT', receipt=self.intent, deadline=10)
        for changed in ({**self.complete, 'intent_sha256': 'c'*64},
                {**self.complete, 'proof': 'tampered'}):
            with self.subTest(changed=changed), self.assertRaises(followup.FollowupUnavailable):
                self.store(stage='COMPLETE', receipt=changed, deadline=10)
        directory = self.root/'dialogue-200-followup'
        self.assertEqual(sorted(path.name for path in directory.iterdir()), [self.key+'.intent.json'])
        with self.assertRaises(FileExistsError): self.store(stage='INTENT', receipt=self.intent, deadline=10)

    def test_later_turns_chain_exclusive_receipts_and_preserve_legacy_first_intent(self):
        self.store(stage='INTENT',receipt=self.intent,deadline=10)
        second={**self.intent,'followup_sequence':2,
            'previous_followup_intent_sha256':followup.sha(self.intent),'proof':'second'}
        self.assertTrue(self.store(stage='INTENT',receipt=second,deadline=10))
        with self.assertRaises(FileExistsError):
            self.store(stage='INTENT',receipt=second,deadline=10)
        third={**second,'followup_sequence':3,
            'previous_followup_intent_sha256':followup.sha(second),'proof':'third'}
        with self.assertRaises(followup.FollowupUnavailable):
            self.store(stage='INTENT',receipt={**third,'previous_followup_intent_sha256':'c'*64},deadline=10)
        self.assertTrue(self.store(stage='INTENT',receipt=third,deadline=10))
        for row in (self.intent,second,third):
            complete={**row,'intent_sha256':followup.sha(row),
                'response_id':'native:final','final_reply_sha256':'d'*64}
            self.assertTrue(self.store(stage='COMPLETE',receipt=complete,deadline=10))
        directory=self.root/'dialogue-200-followup'
        self.assertEqual(len(list(directory.iterdir())),6)
        self.assertEqual((directory/(self.key+'.intent.json')).read_bytes(),followup.canonical(self.intent))
        restarted=followup.ReceiptStore(self.root,monotonic=lambda:1)
        with self.assertRaises(FileExistsError):
            restarted(stage='INTENT',receipt=self.intent,deadline=10)
    def test_missing_intent_symlink_directory_or_unsafe_existing_intent_is_refused(self):
        with self.assertRaises(FileNotFoundError): self.store(stage='COMPLETE', receipt=self.complete, deadline=10)
        directory = self.root/'dialogue-200-followup'
        directory.rmdir(); outside = Path(self.folder.name)/'outside'; outside.mkdir(mode=0o700)
        directory.symlink_to(outside)
        with self.assertRaises(OSError): self.store(stage='INTENT', receipt=self.intent, deadline=10)
        directory.unlink(); self.store(stage='INTENT', receipt=self.intent, deadline=10)
        target = directory/(self.key+'.intent.json'); target.chmod(0o644)
        with self.assertRaises(followup.FollowupUnavailable): self.store(stage='COMPLETE', receipt=self.complete, deadline=10)
    def test_deadline_after_durable_intent_retains_it_and_refuses_another_attempt(self):
        clock = iter((1, 1, 1, 11))
        store = followup.ReceiptStore(self.root, monotonic=lambda: next(clock))
        with self.assertRaises(followup.FollowupUnavailable): store(stage='INTENT', receipt=self.intent, deadline=10)
        target = self.root/'dialogue-200-followup'/(self.key+'.intent.json')
        self.assertEqual(target.read_bytes(), followup.canonical(self.intent))
        with self.assertRaises(FileExistsError): self.store(stage='INTENT', receipt=self.intent, deadline=10)


try:
    from playwright.sync_api import BrowserType, sync_playwright
    from playwright._impl._browser import Browser as ImplBrowser
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

CHROMIUM = Path(os.environ.get('HARUN_TEST_CHROMIUM_EXECUTABLE', '/usr/bin/chromium'))

@unittest.skipUnless(HAS_PLAYWRIGHT and CHROMIUM.is_absolute() and CHROMIUM.is_file(),
    'Local Playwright and Chromium required')
class ActualActorChartsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = sync_playwright().start()
    @classmethod
    def tearDownClass(cls): cls.manager.stop()
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.persistent = self.manager.chromium.launch_persistent_context(
            str(Path(self.folder.name)/'fixture-profile'), executable_path=str(CHROMIUM),
            chromium_sandbox=True, headless=True, args=['--disable-dev-shm-usage'])
        self.addCleanup(self.persistent.close)
        self.persistent.pages[0].set_content('<p>LOCAL PERSISTENT PROFILE FIXTURE</p>')
        self.actor = SimpleNamespace(_playwright=self.manager, _context=self.persistent,
            executable=str(CHROMIUM), _chart_cleanup_failed=False)
        harness, self.charts = capture_namespace(real=True)
        self.closed = []
        self.requests = []
        original_capture = self.charts.capture
        def capture(context, *args, **kwargs):
            context.on('request', lambda request: self.requests.append(request.url))
            return original_capture(context, *args, **kwargs)
        self.charts.capture = capture
        def close_resource(resource):
            async def close(): return await resource._impl_obj.close()
            resource._sync(asyncio.wait_for(close(), timeout=5)); self.closed.append(resource)
        harness.native['close_resource'] = close_resource
        self.charts.close_resource = close_resource
        self.snapshot_calls = []
        def snapshot(symbol, **kwargs):
            self.snapshot_calls.append(symbol); self.assertIs(kwargs['gate'](), True)
            return market_snapshot()
        self.callback = followup.ActorCharts(self.actor, self.charts,
            archive_root=str(Path(self.folder.name)/'archive'),
            expected_playwright_version=version('playwright'), snapshot=snapshot, wall_clock=lambda: NOW)
        self.payload = {'context': native_context_200(NOW-2),
            'marker': 'local_actor_followup', 'request_sha256': 'a'*64}
    def call(self):
        return self.callback(payload=self.payload, deadline=time.monotonic()+30,
            dispatch_check=lambda: True)
    def test_real_sdk_isolated_capture_provenance_and_cleanup_preserve_persistent_profile(self):
        before = copy.deepcopy(self.payload)
        result = self.call()
        self.assertEqual(self.payload, before); self.assertEqual(self.snapshot_calls, ['BTCUSDT'])
        self.assertEqual(len(result['chart_images']), 2)
        self.assertEqual([row['timeframe'] for row in result['chart_evidence']], ['15m', '1h'])
        self.assertEqual(len(self.closed), 3)
        self.assertFalse(self.closed[-1].is_connected())
        self.assertTrue(self.closed[-2].pages == [])
        self.assertFalse(self.actor._chart_cleanup_failed)
        self.assertFalse(any(url.startswith(('http:', 'https:', 'ws:', 'wss:')) for url in self.requests))
        self.assertIs(self.actor._context, self.persistent)
        self.assertEqual(self.persistent.pages[0].locator('p').text_content(), 'LOCAL PERSISTENT PROFILE FIXTURE')
    def test_unknown_context_creation_is_cancelled_then_known_browser_closed(self):
        cancelled = []
        async def hung(self, ignoreHTTPSErrors=None, viewport=None, deviceScaleFactor=None, serviceWorkers=None):
            try: await asyncio.Event().wait()
            finally: cancelled.append(True)
        started = time.monotonic()
        with patch.object(ImplBrowser, 'new_context', hung), patch.object(followup, 'MAX_SECONDS', 12):
            with self.assertRaises(TimeoutError): self.call()
        self.assertLess(time.monotonic()-started, 8)
        self.assertEqual(cancelled, [True]); self.assertEqual(len(self.closed), 1)
        self.assertFalse(self.closed[0].is_connected()); self.assertFalse(self.actor._chart_cleanup_failed)
        self.assertFalse(self.persistent.pages[0].is_closed())
    def test_unknown_launch_refuses_future_native_chart_dispatch_without_touching_persistent(self):
        with patch.object(BrowserType, 'launch', side_effect=TimeoutError('fixture')):
            with self.assertRaises(TimeoutError): self.call()
        self.assertTrue(self.actor._chart_cleanup_failed); self.assertEqual(self.closed, [])
        self.assertFalse(self.persistent.pages[0].is_closed())


if __name__ == '__main__': unittest.main()
