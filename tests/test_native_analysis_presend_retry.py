"""Exercise the deployed native request path without browser or order access."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.test_screening_response_recovery import ROOT, native_module
from worker import neuroapi
from worker.core import DYNAMIC_RISK_MODEL, Ledger, Review
from worker.neuroapi import ResearchPaused, SETUP_SCHEMA_V4


DAY = '2026-11-27'
CYCLE = DAY + ':robot-v9:0'
SYMBOL = 'ENAUSDT'
OPERATION = CYCLE + ':analysis-v9:' + SYMBOL
NOW = datetime(2026, 11, 26, 19, tzinfo=timezone.utc).timestamp()


class NativeAnalysisPresendRetryTests(unittest.TestCase):
    def setUp(self):
        names = ('worker.research_provider', 'worker.neurobrowser', 'worker.native_robot_presend_fixture')
        old_modules = {name: sys.modules.get(name) for name in names}
        old_canonical = neuroapi.canonical_output

        def restore():
            neuroapi.canonical_output = old_canonical
            for name, module in old_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        self.addCleanup(restore)
        self.enterContext(patch('worker.owner_source_protocol.load_owner_release_cutoff',
            return_value='2026-11-04T00:00:00Z'))
        self.source = native_module(names[0], ROOT / 'vps_native/worker/research_provider.py')
        self.native = native_module(names[1], ROOT / 'vps_native/worker/neurobrowser.py')
        with patch('worker.account_state.MAX_REPLACEMENTS', None, create=True):
            self.robot = native_module(names[2], ROOT / 'vps_native/worker/robot.py')
        self.clock = NOW
        self.day = DAY
        self.posts = []
        self.context_reads = 0
        self.refusal = 'UNAVAILABLE'
        self.submitted = False
        self.return_hook = lambda _: None
        self.enterContext(patch.object(self.native.time, 'time', side_effect=lambda: self.clock))
        self.enterContext(patch.object(self.robot, 'day', side_effect=lambda: self.day))
        self.enterContext(patch.object(self.robot, 'analysis_context', side_effect=self.context))
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / 'ledger.sqlite3'
        self.open_ledger()
        self.addCleanup(lambda: self.ledger.db.close())
        self.coordinator.store.configure({'robot_on': True})
        self.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)', (CYCLE, DAY, 0, 'ACTIVE',
            json.dumps(dict(queue=[SYMBOL], seen=[], screen=0))))

    def open_ledger(self):
        self.ledger = Ledger(self.path)
        self.db = self.ledger.db
        self.provider = self.native.NeuroBrowser(self.ledger, token='x' * 32, transport=self.transport)
        self.provider.require_key = lambda: None
        market = SimpleNamespace(catalog=lambda: {SYMBOL: {}}, fresh=lambda *_: None)
        self.coordinator = self.robot.Coordinator(self.ledger, self.provider, market,
            lambda: None, new_setup_policy=DYNAMIC_RISK_MODEL)

    def context(self, *_args, **_kwargs):
        self.context_reads += 1
        frames = {tf: dict(symbol=SYMBOL, timeframe=tf, candles=[dict(close_time=int(self.clock * 1000))])
            for tf in ('1h', '15m')}
        return (dict(symbol=SYMBOL, source='Binance Futures', fetched_at=self.clock,
            server_time=int(self.clock * 1000), timeframes=frames), None)

    def transport(self, method, url, headers, body, timeout):
        self.assertEqual((method, url), ('POST', self.native.BASE + '/v1/ask'))
        self.posts.append(body)
        self.return_hook(body)
        return 409, {}, {**{key: body[key] for key in ('operation', 'marker', 'request_sha256')},
            'submitted': self.submitted, 'reason': self.refusal}

    def analyze(self):
        data = json.loads(self.db.execute('SELECT data FROM robot_cycles WHERE id=?', (CYCLE,)).fetchone()[0])
        return self.coordinator.analyze(CYCLE, data, SYMBOL, {}, DAY)

    def history(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM analysis_presend_failures ORDER BY sequence')]

    def local_claim(self, context=None):
        context = self.context()[0] if context is None else context
        data = json.loads(self.db.execute('SELECT data FROM robot_cycles WHERE id=?', (CYCLE,)).fetchone()[0])
        self.assertTrue(self.coordinator.claim(OPERATION, CYCLE, 'ANALYSIS', SYMBOL, DAY, '5',
            data=data, context=context))
        return context

    def ask(self, context):
        return self.provider.ask(OPERATION, self.robot.ANALYSIS, SETUP_SCHEMA_V4, lambda _: None, context)

    def test_wait_projection_and_distinct_attempt_history_survive_restart(self):
        first = self.analyze()
        self.assertEqual(first['bot_status'], 'WAITING')
        self.assertEqual(first['wait_reason'], 'ANALYSIS_PRESEND_UNAVAILABLE')
        retry = first['analysis_retry']
        self.assertIs(retry['submitted'], False)
        self.assertEqual(retry['operation'], OPERATION)
        self.assertEqual(retry['phase'], 'BEFORE_SEND')
        self.assertEqual(retry['next_check_at'],
            datetime.fromtimestamp(NOW + 45, timezone.utc).isoformat())
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.history()), 1)
        self.ledger.db.close()
        self.open_ledger()
        self.clock += 44
        self.assertEqual(self.analyze()['analysis_retry'], retry)
        self.assertEqual(self.context_reads, 1)
        self.assertEqual(len(self.posts), 1)

        self.clock += 1
        second = self.analyze()['analysis_retry']
        history = self.history()
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(history[0]['request_sha256'], self.posts[0]['request_sha256'])
        self.assertEqual(history[1]['request_sha256'], self.posts[1]['request_sha256'])
        self.assertNotEqual(history[0]['request_sha256'], history[1]['request_sha256'])
        self.assertEqual(history[1]['request_created'], NOW + 45)
        self.assertEqual(second['first_started_at'], retry['first_started_at'])
        self.assertEqual(second['attempts'], 2)
        self.assertEqual(second['next_check_at'], datetime.fromtimestamp(NOW + 135, timezone.utc).isoformat())
        self.coordinator.store.report('HOLD')
        self.assertNotIn('analysis_retry', self.coordinator.store.snapshot())
        self.assertEqual(self.history(), history)

    def test_proven_refusal_releases_job_without_coordinator_cleanup(self):
        context = self.local_claim()
        with self.assertRaisesRegex(ResearchPaused, '^UNAVAILABLE$'):
            self.ask(context)
        for table in ('api_requests', 'research_browser_requests', 'robot_jobs'):
            self.assertIsNone(self.db.execute('SELECT * FROM ' + table).fetchone())
        self.assertEqual(self.history()[0]['submitted'], 0)
        self.assertFalse(self.coordinator.unresolved_research())
        self.clock += 45
        self.local_claim(context)
        with self.assertRaisesRegex(ResearchPaused, '^UNAVAILABLE$'):
            self.ask(context)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.history()), 1)

    def test_refusal_cannot_erase_concurrent_received_receipt(self):
        def receive_late(_):
            self.db.execute('''INSERT INTO research_browser_responses
                (operation,response_id,output_sha256,assistant_text,assistant_sha256)
                VALUES(?,?,?,?,?)''', (OPERATION, 'existing-id', 'output-hash', 'existing-answer', 'answer-hash'))

        self.return_hook = receive_late
        with self.assertRaisesRegex(Review, '^NEUROBRO_BROWSER_OUTCOME_UNKNOWN$'):
            self.ask(self.local_claim())
        self.assertEqual(self.history(), [])
        self.assertEqual(self.db.execute('SELECT body_hash FROM api_requests').fetchone()[0],
            self.posts[0]['request_sha256'])
        self.assertEqual(self.db.execute('SELECT response_id FROM research_browser_responses').fetchone()[0],
            'existing-id')
        self.assertTrue(self.coordinator.unresolved_research())

    def test_ambiguous_return_keeps_original_claim_for_recovery(self):
        self.submitted = True
        context = self.local_claim()
        with self.assertRaisesRegex(Review, '^NEUROBRO_BROWSER_OUTCOME_UNKNOWN$'):
            self.ask(context)
        self.assertEqual(self.history(), [])
        row = self.db.execute('SELECT state,body_hash FROM api_requests').fetchone()
        self.assertEqual(tuple(row), ('NEEDS_REVIEW', self.posts[0]['request_sha256']))
        self.assertEqual(self.db.execute('SELECT request_sha256 FROM research_browser_requests').fetchone()[0],
            row['body_hash'])
        with self.assertRaisesRegex(Review, '^NEUROBRO_BROWSER_REQUEST_NEEDS_REVIEW$'):
            self.ask(context)
        self.assertEqual(len(self.posts), 1)

    def test_transport_loss_does_not_create_before_send_evidence(self):
        def lose_reply(_):
            raise TimeoutError

        self.return_hook = lose_reply
        with self.assertRaisesRegex(Review, '^NEUROBRO_BROWSER_OUTCOME_UNKNOWN$'):
            self.ask(self.local_claim())
        self.assertEqual(self.history(), [])
        self.assertTrue(self.coordinator.unresolved_research())
        self.assertEqual(self.db.execute('SELECT count(*) FROM research_browser_requests').fetchone()[0], 1)

    def test_old_callback_cannot_create_a_receipt_for_fresh_retry(self):
        self.analyze()
        previous = self.posts[0]
        self.clock += 45

        def late_receipt(current):
            self.assertNotEqual(previous['request_sha256'], current['request_sha256'])
            old_result = {key: previous[key] for key in ('operation', 'marker', 'request_sha256')}
            old_result.update(response_id='native-old-callback', assistant_text='Prior result',
                output=dict(symbol=SYMBOL, side='HOLD', limit_entry=None, take_profit=None,
                    stop_loss=None, risk_reward=None))
            with self.assertRaises(Review):
                self.provider._record_browser_response(OPERATION, previous, previous['request_sha256'],
                    old_result, SETUP_SCHEMA_V4, lambda _: None, previous['context'])
            self.assertIsNone(self.db.execute('SELECT * FROM research_browser_responses').fetchone())
            self.assertEqual(self.db.execute('SELECT body_hash FROM api_requests').fetchone()[0],
                current['request_sha256'])

        self.return_hook = late_receipt
        self.assertEqual(self.analyze()['analysis_retry']['attempts'], 2)

    def test_backoff_caps_and_separate_busy_and_day_changes(self):
        for delay in (45, 90, 180, 360, 900, 900):
            self.assertEqual(self.analyze()['analysis_retry']['reason'], 'UNAVAILABLE')
            row = self.history()[-1]
            self.assertEqual(row['next_retry_at'] - row['failed_at'], delay)
            self.clock += delay
        self.refusal = 'BUSY'
        self.assertEqual(self.analyze()['wait_reason'], 'NEUROBRO_BUSY')
        self.assertIsNone(self.provider.analysis_presend_retry(OPERATION))
        self.clock += 45
        self.refusal = 'UNAVAILABLE'
        self.assertEqual(self.analyze()['analysis_retry']['consecutive_failures'], 1)
        previous_posts = len(self.posts)
        self.day = '2026-11-28'
        self.assertIsNone(self.provider.analysis_presend_retry(OPERATION.replace(DAY, self.day, 1)))
        self.assertEqual(self.analyze()['wait_reason'], 'ROBOT_DAY_CHANGED')
        self.assertEqual(len(self.posts), previous_posts)
