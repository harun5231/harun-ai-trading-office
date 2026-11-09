"""Offline tests of the native five-hour lifecycle; no HTTP, browser or orders."""
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from agent import native_pending_review as pending
from agent import native_pending_protocol as protocol
from tests.test_agent_native_market_context import native_context
from worker import research_guard


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class ResearchPaused(Exception):
    pass


class NativeProofStore:
    """A test provenance authority, not an exchange or model implementation."""
    def __init__(self, db, initialize=False):
        assert initialize is False
        self.db = db

    def settings(self):
        row = self.db.execute('SELECT enabled,risk FROM robot_settings WHERE id=1').fetchone()
        return {'robot_on': bool(row['enabled']), 'risk_target_usdt': row['risk']}

    def verified_plan(self, row):
        plan = json.loads(row['plan'])
        if plan.get('proof') != 'offline-native-proof:' + row['id']:
            raise ValueError('NATIVE_PROVENANCE_INVALID')
        return plan

    def report(self, state, account=None, reason=None, wait_reason=None):
        return {'bot_status': state, 'failure_code': reason, 'wait_reason': wait_reason}

    def entries(self, today):
        return self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts WHERE entry_day=?', (today,)).fetchone()[0]

    def record_protection_observation(self, result):
        # The real projection has its own transaction/merge tests. This harness
        # keeps the native pending lifecycle independent of Office rendering.
        self.last_protection_observation = result


def native_build(plan, operation):
    if plan['proof'] != 'offline-native-proof:' + operation:
        raise ValueError('NATIVE_PROVENANCE_INVALID')
    return plan['intent']


def native_request_body(operation, prompt, schema, context):
    value = {'operation': operation, 'marker': 'native-marker:' + digest(operation)[:32],
        'provider': 'NATIVE_OFFLINE_BROWSER', 'contract_version': 'OFFLINE_TEST',
        'prompt': prompt, 'output_schema': schema, 'context': context}
    serialized = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return value, serialized, digest(serialized)


def canonical(value, schema, catalog=None):
    if schema != protocol.REVIEW_SCHEMA:
        raise ValueError('UNEXPECTED_SCHEMA')
    return protocol._canonical_pending(value, ValueError)


def native_methods():
    """Run exact verified native intent/receipt code, never import its app."""
    # Exercise the versioned native candidate, never an unversioned stale
    # workspace copy or the ordinary repository adapter. Do not import its app.
    path = Path(__file__).resolve().parents[1] / 'vps_native/worker/robot.py'
    tree = ast.parse(path.read_bytes())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Coordinator')
    names = {'verified_intent', 'record_gateway_observation', 'mark_unknown'}
    nodes = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {'json': json, 'D': Decimal, 'datetime': datetime, 'timezone': timezone,
        'ZoneInfo': ZoneInfo, 're': __import__('re'), 'Review': ValueError,
        'build_intent': native_build, 'now': lambda: datetime.now(timezone.utc).isoformat(),
        'day': lambda: datetime.now(ZoneInfo('Asia/Jakarta')).date().isoformat()}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<verified-native-receipt>', 'exec'), namespace)
    return {name: namespace[name] for name in names}


class Gateway:
    """Instrument native method boundaries; no wire/client/order API exists."""
    def __init__(self, case):
        self.case, self.calls = case, []
        self.status, self.filled, self.cancel_count = 'NEW', '0', 0
        self.cancel_error = False
        self.turn_off_at_cancel = False

    @contextmanager
    def _operation(self):
        self.calls.append('native_operation')
        yield

    def _validate_intent(self, intent):
        assert intent == self.case.intent
        self.calls.append('native_validate')

    def _account_config(self):
        self.calls.append('native_account_config')

    def _entry(self, intent):
        self._validate_intent(intent)
        self.calls.append('native_owned_entry_get')
        return {'status': self.status, 'executedQty': self.filled,
                'clientOrderId': intent['client_order_id'], 'orderId': '111',
                'time': int(pending._utc_instant(self.case.accepted) * 1000)}

    def _cancel_entry_remainder(self, intent, entry):
        self._validate_intent(intent)
        self.calls.append('native_cancel_remainder')
        if self.turn_off_at_cancel:
            self.case.set_on(False)
        if entry['status'] not in ('NEW', 'PARTIALLY_FILLED'):
            return entry
        if not research_guard.gateway_mutations_allowed():
            raise ValueError('NATIVE_WIRE_MUTATION_DENIED')
        self.cancel_count += 1
        if self.cancel_error:
            raise ValueError('UNKNOWN_CANCEL_OUTCOME')
        self.status = 'CANCELED'
        return self._entry(intent)

    def reconcile(self, intent):
        self._validate_intent(intent)
        self.calls.append('native_reconcile_ownership_protection')
        stamp = datetime.now(timezone.utc).isoformat()
        state = ('POSITION_PROTECTED' if Decimal(self.filled) else
                 'ENTRY_PENDING' if self.status in ('NEW', 'PARTIALLY_FILLED') else 'REJECTED')
        result = {'source': 'BINANCE_FUTURES', 'symbol': intent['symbol'],
            'client_order_id': intent['client_order_id'], 'order_id': '111',
            'state': state, 'filled_quantity': self.filled, 'observed_at': stamp,
            'sl_confirmed': bool(Decimal(self.filled)), 'tp_confirmed': bool(Decimal(self.filled))}
        if Decimal(self.filled):
            result.update(first_fill_at=self.case.fill_time, sl_order_id='222', tp_order_id='333')
        return result


class Coordinator:
    def __init__(self, case):
        self.case, self.db = case, case.db
        self.store = NativeProofStore(self.db, initialize=False)
        self.gateway = Gateway(case)
        self.market = types.SimpleNamespace(fresh=self.market_fresh,
            data=lambda symbol: native_context(time.time()), max_age=180)
        self.neuro = types.SimpleNamespace(ask=self.ask)
        self.ask_count = self.advance_count = 0
        self.decision = 'TAHAN'
        self.after_reply = None
        self.before_dispatch = None
        self.raise_at_ask = None
        self.force_wrong_ref = False

    def record_gateway_execution_costs(self, *args, **kwargs):
        # Priority execution-cost sampling is tested separately; this fixture
        # exercises the native pending transition and immutable order state.
        pass

    def account_factory(self):
        return object()

    def market_fresh(self, context, symbol):
        if context['symbol'] != symbol or not 0 <= time.time() - context['fetched_at'] < 180:
            raise ValueError('STALE_NATIVE_MARKET')

    def execution_slots(self, account, today):
        return 1

    def gateway_allowed(self):
        return self.store.settings()['robot_on']

    def allowed(self, today):
        actual_day = datetime.fromtimestamp(time.time(), ZoneInfo('Asia/Jakarta')).date().isoformat()
        return self.gateway_allowed() and today == actual_day

    def pause_reason(self):
        return 'ROBOT_OFF' if not self.gateway_allowed() else 'ROBOT_DAY_CHANGED'

    def advance_execution(self, account, today):
        self.advance_count += 1
        return {'bot_status': 'ORIGINAL_ADVANCE_UNCHANGED'}

    def unresolved_research(self):
        return bool(self.db.execute("SELECT 1 FROM api_requests WHERE state IN ('PENDING','NEEDS_REVIEW') LIMIT 1").fetchone())

    def ask(self, operation, prompt, schema, validate, context=None, *, continue_if=None):
        self.ask_count += 1
        assert schema == protocol.REVIEW_SCHEMA
        assert continue_if() is True
        if self.raise_at_ask:
            raise self.raise_at_ask
        body, serialized, sha = native_request_body(operation, prompt, schema, context)
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            (operation, None, sha, 'PENDING', time.time(), 1, None, None))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?)',
            (operation, serialized, sha))
        if self.before_dispatch:
            self.before_dispatch()
        dispatch_day = datetime.fromtimestamp(time.time(), ZoneInfo('Asia/Jakarta')).date().isoformat()
        assert pending.verify_pending_dispatch(self.db, operation, body['marker'], sha,
                                               dispatch_day, time.time()) is True
        value = {'symbol': context['symbol'], 'order_ref': context['pending_review']['intent_ref'],
                 'decision': self.decision}
        if self.force_wrong_ref:
            value['order_ref'] = 'f' * 64
        validate(value)
        self.db.execute("UPDATE api_requests SET state='COMPLETE',output=? WHERE operation=?",
                        (canonical(value, schema), operation))
        text = json.dumps(value)
        captured = datetime.fromtimestamp(int(time.time() * 1000) / 1000, timezone.utc).isoformat()
        charts = [{'source': 'BINANCE_USDM_FUTURES_WEBSITE_SCREENSHOT', 'symbol': context['symbol'],
            'timeframe': tf, 'captured_at': captured} for tf in ('1h', '15m')]
        self.db.execute('INSERT INTO research_browser_responses VALUES(?,?,?,?,?,?)',
            (operation, text, digest(text), time.time(), json.dumps(charts), 180))
        if self.after_reply:
            self.after_reply(self)
        return value


class PendingReviewTests(unittest.TestCase):
    def setUp(self):
        # Use a private workspace parent; /tmp is intentionally rejected by the
        # runtime's ancestor policy rather than weakening that check for tests.
        self.temporary = tempfile.TemporaryDirectory(prefix='pending-tests-', dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.temporary.name)
        self.db = sqlite3.connect(self.root / 'ledger.sqlite3', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE robot_settings(id INTEGER PRIMARY KEY,enabled INTEGER,risk TEXT);
        INSERT INTO robot_settings VALUES(1,1,'5');
        CREATE TABLE order_intents(id TEXT PRIMARY KEY,candidate_id TEXT,symbol TEXT,state TEXT,payload TEXT,result TEXT,failure_code TEXT,created TEXT,updated TEXT);
        CREATE TABLE robot_candidates(id TEXT PRIMARY KEY,cycle TEXT,symbol TEXT,status TEXT,plan TEXT,failure_code TEXT);
        CREATE TABLE robot_entry_receipts(id TEXT PRIMARY KEY,symbol TEXT,entry_day TEXT,confirmed_at TEXT);
        CREATE TABLE api_requests(operation TEXT PRIMARY KEY,idempotency TEXT,body_hash TEXT,state TEXT,created REAL,attempts INTEGER,output TEXT,failure_code TEXT);
        CREATE TABLE research_browser_requests(operation TEXT PRIMARY KEY,request_body TEXT,request_sha256 TEXT);
        CREATE TABLE research_browser_responses(operation TEXT PRIMARY KEY,assistant_text TEXT,assistant_sha256 TEXT,received_at REAL,chart_evidence TEXT,chart_max_age INTEGER);
        ''')
        self.now = time.time()
        self.today = datetime.now(ZoneInfo('Asia/Jakarta')).date().isoformat()
        self.created = datetime.fromtimestamp(self.now - pending.REVIEW_SECONDS - 1, timezone.utc).isoformat()
        self.accepted = self.created
        self.fill_time = datetime.fromtimestamp(self.now - 1, timezone.utc).isoformat()
        self.operation = 'previous-day:analysis-v9:BTCUSDT'
        self.intent = {'intent_id': self.operation, 'client_order_id': 'hao-owned-fixture',
            'symbol': 'BTCUSDT', 'side': 'LONG',
            'entry': {'order_type': 'LIMIT', 'price': '100', 'quantity': '1', 'time_in_force': 'GTC'},
            'protection': {'stop_loss': '99', 'take_profit': '102'}}
        payload = json.dumps(self.intent)
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
            (self.operation, self.operation, 'BTCUSDT', 'ENTRY_PENDING', payload, None, None, self.created, self.created))
        plan = {'proof': 'offline-native-proof:' + self.operation, 'intent': self.intent}
        self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
            (self.operation, 'previous-day', 'BTCUSDT', 'ENTRY_PENDING', json.dumps(plan), None))
        self.coordinator = Coordinator(self)
        for name, function in native_methods().items():
            setattr(self.coordinator, name, types.MethodType(function, self.coordinator))
        self.native = {
            'robot': types.SimpleNamespace(RobotStore=NativeProofStore, build_intent=native_build),
            'research_guard': research_guard,
            'analysis': types.SimpleNamespace(analysis_context=lambda market, symbol, risk, client:
                (native_context(time.time()), None)),
            'neuroapi': types.SimpleNamespace(canonical_output=canonical, ResearchPaused=ResearchPaused),
            'research_provider': types.SimpleNamespace(request_body=native_request_body,
                BROWSER_SOURCE='VERIFIED_NATIVE_BROWSER', source_evidence=self.source_evidence)}
        self.patches = patch.object(pending, '_native', return_value=self.native)
        self.patches.start()

    def tearDown(self):
        self.patches.stop()
        self.db.close()
        self.temporary.cleanup()

    def set_on(self, value):
        self.db.execute('UPDATE robot_settings SET enabled=? WHERE id=1', (int(value),))

    def source_evidence(self, db, operation):
        row = db.execute('SELECT state FROM api_requests WHERE operation=?', (operation,)).fetchone()
        assert row['state'] == 'COMPLETE'
        return {'source_type': 'VERIFIED_NATIVE_BROWSER', 'provider_evidence_sha256': 'a' * 64}

    def run_tick(self):
        # Simulate the outer native tick's existing research gate, unchanged.
        with research_guard.research_reads(lambda: self.coordinator.allowed(self.today)):
            return pending.review_then_advance(self.coordinator, {}, self.today)

    def claims(self):
        with pending._journal(self.db) as journal:
            return [] if journal is None else [dict(row) for row in journal.execute('SELECT * FROM pending_reviews')]

    def test_off_does_not_open_journal_reconcile_send_or_cancel(self):
        self.set_on(False)
        self.assertEqual(self.run_tick()['wait_reason'], 'ROBOT_OFF')
        self.assertFalse((self.root / pending.JOURNAL_NAME).exists())
        self.assertEqual(self.coordinator.gateway.calls, [])
        self.assertEqual((self.coordinator.ask_count, self.coordinator.advance_count), (0, 0))

    def test_not_due_preserves_original_advance_and_creates_no_journal(self):
        stamp = datetime.now(timezone.utc).isoformat()
        self.db.execute('UPDATE order_intents SET created=?', (stamp,))
        self.assertEqual(self.run_tick()['bot_status'], 'ORIGINAL_ADVANCE_UNCHANGED')
        self.assertEqual(self.coordinator.ask_count, 0)
        self.assertFalse((self.root / pending.JOURNAL_NAME).exists())

    def test_tahan_uses_bound_request_and_retains_previous_day_pending_for_five_more_hours(self):
        result = self.run_tick()
        self.assertEqual(result['bot_status'], 'ENTRY_PENDING')
        self.assertEqual(self.coordinator.ask_count, 1)
        self.assertEqual(self.coordinator.gateway.cancel_count, 0)
        claim = self.claims()[0]
        self.assertEqual((claim['state'], claim['decision']), ('COMPLETE', 'TAHAN'))
        self.assertGreaterEqual(claim['next_due'], self.now + pending.REVIEW_SECONDS)
        body = json.loads(claim['request_body'])
        self.assertEqual(body['context']['pending_review']['entry_created_at'], self.created)
        self.assertEqual(body['output_schema'], protocol.REVIEW_SCHEMA)
        self.assertEqual(body['context']['pending_review']['intent_ref'], digest(json.dumps(self.intent)))
        self.assertIn('harga LIMIT 100', body['prompt'])
        self.assertNotIn('Return only JSON', body['prompt'])
        self.assertIn('Diterima Binance', body['prompt'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 0)
        self.run_tick()
        self.assertEqual(self.coordinator.ask_count, 1)
        self.assertEqual(self.coordinator.advance_count, 1)
        self.assertEqual(self.db.execute('SELECT created,state FROM order_intents').fetchone()['created'], self.created)

    def test_batalkan_rechecks_native_ownership_before_native_cancel_and_preserves_other_rows(self):
        self.coordinator.decision = 'BATALKAN'
        result = self.run_tick()
        self.assertEqual(result['bot_status'], 'REJECTED')
        self.assertEqual(self.coordinator.gateway.cancel_count, 1)
        calls = self.coordinator.gateway.calls
        cancellation = calls.index('native_cancel_remainder')
        self.assertGreater(calls[:cancellation].count('native_reconcile_ownership_protection'), 1)
        self.assertEqual(calls[cancellation-2], 'native_owned_entry_get')
        self.assertEqual(self.claims()[0]['state'], 'COMPLETE')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_candidates').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 0)

    def test_confirmed_cancel_policy_uses_actual_decision_day_and_only_reads_existing_proof(self):
        self.coordinator.decision = 'BATALKAN'
        self.assertEqual(self.run_tick()['bot_status'], 'REJECTED')
        candidate = self.db.execute('SELECT * FROM robot_candidates WHERE id=?', (self.operation,)).fetchone()
        decision_at = self.db.execute('SELECT received_at FROM research_browser_responses').fetchone()[0]
        day = datetime.fromtimestamp(decision_at, ZoneInfo('Asia/Jakarta')).date().isoformat()
        yesterday = datetime.fromtimestamp(decision_at-86400, ZoneInfo('Asia/Jakarta')).date().isoformat()
        tomorrow = datetime.fromtimestamp(decision_at+86400, ZoneInfo('Asia/Jakarta')).date().isoformat()
        ledger = tuple(self.db.iterdump())
        claim = self.claims()
        calls = tuple(self.coordinator.gateway.calls)

        self.assertTrue(pending.confirmed_cancel_result(self.coordinator, candidate))
        self.assertEqual(pending.screening_day_exclusions(self.coordinator, day), {'BTCUSDT'})
        self.assertEqual(pending.screening_day_exclusions(self.coordinator, yesterday), set())
        self.assertEqual(pending.screening_day_exclusions(self.coordinator, tomorrow), set())

        self.assertEqual(tuple(self.db.iterdump()), ledger)
        self.assertEqual(self.claims(), claim)
        self.assertEqual(tuple(self.coordinator.gateway.calls), calls)
        self.assertEqual((self.coordinator.ask_count, self.coordinator.gateway.cancel_count), (1, 1))

    def test_confirmed_cancel_policy_rejects_unresolved_partial_and_changed_source_or_intent(self):
        self.coordinator.decision = 'BATALKAN'
        self.assertEqual(self.run_tick()['bot_status'], 'REJECTED')
        candidate = self.db.execute('SELECT * FROM robot_candidates WHERE id=?', (self.operation,)).fetchone()
        original = dict(self.db.execute('SELECT * FROM order_intents').fetchone())
        claim = self.claims()[0]
        response = dict(self.db.execute('SELECT * FROM research_browser_responses').fetchone())
        self.assertTrue(pending.confirmed_cancel_result(self.coordinator, candidate))
        for change in ('UNKNOWN', 'PAUSED', 'CANCEL_PREPARED', 'source', 'request', 'intent', 'partial', 'receipt'):
            with self.subTest(change=change):
                with pending._journal(self.db, create=True) as journal:
                    if change in ('UNKNOWN', 'PAUSED', 'CANCEL_PREPARED'):
                        journal.execute('UPDATE pending_reviews SET state=?',
                            ('NEEDS_REVIEW' if change == 'UNKNOWN' else change,))
                    elif change == 'source':
                        journal.execute('UPDATE pending_reviews SET source_sha256=?', ('0'*64,))
                    elif change == 'request':
                        journal.execute('UPDATE pending_reviews SET request_sha256=?', ('0'*64,))
                if change == 'intent':
                    self.db.execute('UPDATE order_intents SET payload=?', ('{}',))
                elif change == 'partial':
                    result = json.loads(original['result']); result['filled_quantity'] = '0.4'
                    self.db.execute('UPDATE order_intents SET result=?', (json.dumps(result),))
                elif change == 'receipt':
                    self.db.execute('UPDATE research_browser_responses SET assistant_sha256=?', ('0'*64,))
                self.assertFalse(pending.confirmed_cancel_result(self.coordinator, candidate))
                self.db.execute('UPDATE order_intents SET payload=?,result=?', (original['payload'], original['result']))
                self.db.execute('UPDATE research_browser_responses SET assistant_sha256=?', (response['assistant_sha256'],))
                with pending._journal(self.db, create=True) as journal:
                    journal.execute('UPDATE pending_reviews SET state=?,source_sha256=?,request_sha256=?',
                        (claim['state'], claim['source_sha256'], claim['request_sha256']))
        self.db.execute('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)',
            (self.intent['client_order_id'], 'BTCUSDT', self.today, self.created))
        self.assertFalse(pending.confirmed_cancel_result(self.coordinator, candidate))
        self.assertEqual((self.coordinator.ask_count, self.coordinator.gateway.cancel_count), (1, 1))

    def test_day_policy_requires_private_journal_if_any_native_pending_attempt_exists(self):
        candidate = self.db.execute('SELECT * FROM robot_candidates').fetchone()
        self.assertFalse(pending.confirmed_cancel_result(self.coordinator, candidate))
        self.assertEqual(pending.screening_day_exclusions(self.coordinator, self.today), set())
        self.assertFalse((self.root / pending.JOURNAL_NAME).exists())
        operation = 'pending-review-v1:' + 'a'*32 + ':1'
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            (operation, None, 'b'*64, 'PENDING', self.now, 1, None, None))
        ledger = tuple(self.db.iterdump())
        with self.assertRaises(pending.PendingReviewUnavailable):
            pending.confirmed_cancel_result(self.coordinator, candidate)
        with self.assertRaises(pending.PendingReviewUnavailable):
            pending.screening_day_exclusions(self.coordinator, self.today)
        self.assertEqual(tuple(self.db.iterdump()), ledger)
        self.assertFalse((self.root / pending.JOURNAL_NAME).exists())
        self.assertEqual((self.coordinator.ask_count, self.coordinator.gateway.cancel_count), (0, 0))

    def test_partial_fill_during_discussion_keeps_native_protection_and_entry_receipt(self):
        self.coordinator.decision = 'BATALKAN'
        def fill(_):
            self.coordinator.gateway.status = 'PARTIALLY_FILLED'
            self.coordinator.gateway.filled = '0.4'
        self.coordinator.after_reply = fill
        result = self.run_tick()
        self.assertEqual(result['bot_status'], 'POSITION_PROTECTED')
        self.assertEqual(self.coordinator.gateway.cancel_count, 0)
        row = self.db.execute('SELECT * FROM order_intents').fetchone()
        self.assertEqual(row['state'], 'POSITION_PROTECTED')
        self.assertEqual(json.loads(row['result'])['filled_quantity'], '0.4')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 1)
        self.assertTrue(json.loads(self.claims()[0]['result'])['decision_not_applied_after_fill'])

    def test_batalkan_cancels_already_protected_partial_remainder_without_changing_existing_fill(self):
        self.coordinator.gateway.status = 'PARTIALLY_FILLED'
        self.coordinator.gateway.filled = '0.4'
        first = self.coordinator.gateway.reconcile(self.intent)
        self.coordinator.record_gateway_observation(self.operation, self.operation, first, {})
        before = dict(self.db.execute('SELECT * FROM order_intents').fetchone())
        receipts = [tuple(row) for row in self.db.execute('SELECT * FROM robot_entry_receipts')]
        self.assertEqual(before['state'], 'POSITION_PROTECTED')
        self.assertEqual(len(receipts), 1)
        self.coordinator.decision = 'BATALKAN'

        self.assertEqual(self.run_tick()['bot_status'], 'POSITION_PROTECTED')

        self.assertEqual(self.coordinator.gateway.cancel_count, 1)
        self.assertEqual(self.coordinator.gateway.status, 'CANCELED')
        self.assertEqual(self.coordinator.gateway.filled, '0.4')
        current = dict(self.db.execute('SELECT * FROM order_intents').fetchone())
        result = json.loads(current['result'])
        self.assertEqual(current['state'], 'POSITION_PROTECTED')
        self.assertEqual((current['created'], current['payload']), (before['created'], before['payload']))
        self.assertEqual(result['filled_quantity'], '0.4')
        self.assertTrue(result['sl_confirmed'])
        self.assertTrue(result['tp_confirmed'])
        self.assertEqual((result['sl_order_id'], result['tp_order_id']), ('222', '333'))
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM robot_entry_receipts')], receipts)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_candidates').fetchone()[0], 1)
        claim = self.claims()[0]
        self.assertEqual((claim['state'], claim['decision']), ('COMPLETE', 'BATALKAN'))
        self.assertEqual(json.loads(claim['result'])['gateway_observation']['filled_quantity'], '0.4')

    def test_confirmed_partial_cancel_stays_excluded_after_close_without_releasing_fill_quota(self):
        self.coordinator.gateway.status = 'PARTIALLY_FILLED'
        self.coordinator.gateway.filled = '0.4'
        first = self.coordinator.gateway.reconcile(self.intent)
        self.coordinator.record_gateway_observation(self.operation, self.operation, first, {})
        receipts = [tuple(row) for row in self.db.execute('SELECT * FROM robot_entry_receipts')]
        self.assertEqual(len(receipts), 1)
        self.coordinator.decision = 'BATALKAN'
        self.assertEqual(self.run_tick()['bot_status'], 'POSITION_PROTECTED')
        claim = self.claims()[0]
        retained = json.loads(claim['result'])
        self.assertEqual(retained['entry_remainder'], {'status': 'CANCELED',
            'client_order_id': self.intent['client_order_id'], 'order_id': '111', 'filled_quantity': '0.4'})
        decision_at = self.db.execute('SELECT received_at FROM research_browser_responses').fetchone()[0]
        today = datetime.fromtimestamp(decision_at, ZoneInfo('Asia/Jakarta')).date().isoformat()
        tomorrow = datetime.fromtimestamp(decision_at+86400, ZoneInfo('Asia/Jakarta')).date().isoformat()
        candidate = self.db.execute('SELECT * FROM robot_candidates').fetchone()
        self.assertFalse(pending.confirmed_cancel_result(self.coordinator, candidate))
        self.assertEqual(pending.screening_day_exclusions(self.coordinator, today), {'BTCUSDT'})

        row = self.db.execute('SELECT * FROM order_intents').fetchone()
        closed = dict(json.loads(row['result']), state='CLOSED', sl_confirmed=False, tp_confirmed=False,
            exit_order_id='444', observed_at=datetime.now(timezone.utc).isoformat(),
            closed_at=datetime.fromtimestamp(time.time()-0.1, timezone.utc).isoformat())
        self.coordinator.record_gateway_observation(self.operation, self.operation, closed, {})
        candidate = self.db.execute('SELECT * FROM robot_candidates').fetchone()
        self.assertEqual(candidate['status'], 'CLOSED')
        self.assertFalse(pending.confirmed_cancel_result(self.coordinator, candidate))
        self.assertEqual(pending.screening_day_exclusions(self.coordinator, today), {'BTCUSDT'})
        self.assertEqual(pending.screening_day_exclusions(self.coordinator, tomorrow), set())
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM robot_entry_receipts')], receipts)
        self.assertEqual(self.coordinator.store.entries(receipts[0][2]), 1)
        self.assertEqual((self.coordinator.ask_count, self.coordinator.gateway.cancel_count), (1, 1))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0], 1)

        historical = dict(retained); historical.pop('entry_remainder')
        with pending._journal(self.db, create=True) as journal:
            journal.execute('UPDATE pending_reviews SET result=? WHERE operation=?',
                (pending._json(historical), claim['operation']))
        self.assertEqual(pending.screening_day_exclusions(self.coordinator, today), set())
        self.assertFalse(pending.confirmed_cancel_result(self.coordinator, candidate))
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM robot_entry_receipts')], receipts)

    def test_tahan_reconciles_a_fill_before_returning_from_discussion(self):
        def fill(_):
            self.coordinator.gateway.status = 'PARTIALLY_FILLED'
            self.coordinator.gateway.filled = '0.4'
        self.coordinator.after_reply = fill
        result = self.run_tick()
        self.assertEqual(result['bot_status'], 'POSITION_PROTECTED')
        self.assertEqual(self.coordinator.gateway.cancel_count, 0)
        self.assertEqual(self.claims()[0]['decision'], 'TAHAN')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 1)

    def test_pending_review_carries_fresh_estimate_but_keeps_original_order_parameters(self):
        estimate = {'status': 'AVAILABLE', 'original_risk_target_usdt': '5',
            'estimated_total_risk': '5.85', 'estimated_budget_difference_usdt': '0.85',
            'cost_evidence_sha256': 'c'*64, 'components': {'gross_risk': '5', 'entry_fee_usdt': '0.1',
                'sl_exit_fee_usdt': '0.1', 'sl_book_impact_usdt': '0.15', 'sl_liquidity_uncertainty_usdt': '0.5'}}
        original = self.db.execute('SELECT payload FROM order_intents').fetchone()[0]
        with patch.object(pending, 'estimate_owned_order', return_value=estimate) as observer:
            self.assertEqual(self.run_tick()['bot_status'], 'ENTRY_PENDING')
        self.assertEqual(observer.call_count, 1)
        body = json.loads(self.claims()[0]['request_body'])
        order = body['context']['pending_order_context']
        self.assertEqual(order['entry_price'], self.intent['entry']['price'])
        self.assertEqual(order['quantity'], self.intent['entry']['quantity'])
        self.assertEqual(order['stop_loss'], self.intent['protection']['stop_loss'])
        self.assertEqual(order['take_profit'], self.intent['protection']['take_profit'])
        self.assertEqual(order['estimated_loss'], '5.85')
        self.assertEqual(order['cost_components']['evidence_sha256'], 'c'*64)
        self.assertNotIn('anggaran', body['prompt'].lower())
        self.assertNotIn('5.85', body['prompt'])
        self.assertNotIn('0.85 USDT', body['prompt'])
        self.assertNotIn('fee entry', body['prompt'])
        self.assertIn('harga LIMIT 100', body['prompt'])
        self.assertIn('SL 99, TP 102', body['prompt'])
        self.assertEqual(self.db.execute('SELECT payload FROM order_intents').fetchone()[0], original)

    def test_pending_prompt_keeps_order_facts_and_omits_private_policy_explanations(self):
        original = json.dumps(self.intent)
        prompt = pending._prompt(self.intent, 'private-order-reference', dict(symbol='BTCUSDT', side='LONG',
            entry_price='100', quantity='1', stop_loss='99', take_profit='102', accepted_at=self.accepted,
            status='PARTIALLY_FILLED', filled_quantity='0.4', age_seconds=18001,
            risk_usdt='5', estimated_loss='5.85', estimated_profit='10.85'))
        for fact in ('BTCUSDT', 'harga LIMIT 100', 'SL 99, TP 102', 'PARTIALLY_FILLED',
                'quantity terisi 0.4', 'TAHAN atau BATALKAN', 'alasan singkat'):
            self.assertIn(fact, prompt)
        for private in ('private-order-reference', '5.85', '10.85', 'lima jam', 'scheduler', 'gateway',
                'Risk Manager', 'bukan alasan pasar', 'Jangan mengubah', 'anggaran'):
            self.assertNotIn(private, prompt)
        self.assertEqual(json.dumps(self.intent), original)

    def test_first_review_uses_exchange_acceptance_not_local_claim_time(self):
        self.accepted = datetime.fromtimestamp(self.now - 60, timezone.utc).isoformat()
        self.assertEqual(self.run_tick()['bot_status'], 'ORIGINAL_ADVANCE_UNCHANGED')
        self.assertEqual(self.coordinator.ask_count, 0)
        self.assertFalse((self.root / pending.JOURNAL_NAME).exists())
        self.assertEqual(self.db.execute('SELECT created FROM order_intents').fetchone()[0], self.created)

    def test_completed_tahan_after_crash_is_drained_without_duplicate_discussion(self):
        def interrupted(_):
            raise KeyboardInterrupt
        self.coordinator.after_reply = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.run_tick()
        self.assertEqual(self.claims()[0]['state'], 'REVIEWING')
        reply_time = self.db.execute('SELECT received_at FROM research_browser_responses').fetchone()[0]
        self.coordinator.after_reply = None
        self.run_tick()
        claim = self.claims()[0]
        self.assertEqual((claim['state'], claim['decision']), ('COMPLETE', 'TAHAN'))
        self.assertEqual(claim['next_due'], reply_time + pending.REVIEW_SECONDS)
        self.assertEqual(self.coordinator.ask_count, 1)
        self.assertEqual(self.coordinator.gateway.cancel_count, 0)

    def test_repeated_tahan_uses_each_decision_time_across_wib_midnight(self):
        # The clock belongs only to this synthetic backend; no production time,
        # order timestamp or order parameter is modified.
        clock = [pending._utc_instant(self.created) + pending.REVIEW_SECONDS + 1]
        with patch.object(pending.time, 'time', side_effect=lambda: clock[0]):
            for index in range(3):
                self.today = datetime.fromtimestamp(clock[0], ZoneInfo('Asia/Jakarta')).date().isoformat()
                self.assertEqual(self.run_tick()['bot_status'], 'ENTRY_PENDING')
                claim = self.claims()[-1]
                self.assertEqual(claim['next_due'], clock[0] + pending.REVIEW_SECONDS)
                clock[0] = claim['next_due'] + 1
        self.assertEqual(self.coordinator.ask_count, 3)
        self.assertEqual(self.coordinator.gateway.cancel_count, 0)
        self.assertEqual([claim['sequence'] for claim in self.claims()], [1, 2, 3])
        row = self.db.execute('SELECT * FROM order_intents').fetchone()
        self.assertEqual(row['created'], self.created)
        self.assertEqual(json.loads(row['payload']), self.intent)

    def test_tahan_reopen_preserves_due_clock_and_schedules_next_review_from_new_decision(self):
        clock = [self.now]
        with patch.object(pending.time, 'time', side_effect=lambda: clock[0]):
            self.assertEqual(self.run_tick()['bot_status'], 'ENTRY_PENDING')
            first = self.claims()[0]
            first_reply = self.db.execute('SELECT received_at FROM research_browser_responses '
                'WHERE operation=?', (first['operation'],)).fetchone()[0]
            self.assertEqual(first['next_due'], first_reply + pending.REVIEW_SECONDS)
            self.assertEqual(self.coordinator.ask_count, 1)
            gateway = self.coordinator.gateway

            # Reopen both local databases through a new Coordinator. The fake
            # exchange retains its existing order, as it would across restart.
            self.db.close()
            self.db = sqlite3.connect(self.root / 'ledger.sqlite3', isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.coordinator = Coordinator(self)
            self.coordinator.gateway = gateway
            for name, function in native_methods().items():
                setattr(self.coordinator, name, types.MethodType(function, self.coordinator))

            clock[0] = first['next_due'] - 1
            self.today = datetime.fromtimestamp(clock[0], ZoneInfo('Asia/Jakarta')).date().isoformat()
            self.assertEqual(self.run_tick()['bot_status'], 'ORIGINAL_ADVANCE_UNCHANGED')
            self.assertEqual(self.coordinator.ask_count, 0)
            self.assertEqual(self.claims(), [first])

            clock[0] = first['next_due'] + 1
            self.today = datetime.fromtimestamp(clock[0], ZoneInfo('Asia/Jakarta')).date().isoformat()
            self.assertEqual(self.run_tick()['bot_status'], 'ENTRY_PENDING')
            claims = self.claims()
            second = claims[-1]
            second_reply = self.db.execute('SELECT received_at FROM research_browser_responses '
                'WHERE operation=?', (second['operation'],)).fetchone()[0]
            self.assertEqual(claims[0], first)
            self.assertEqual([claim['sequence'] for claim in claims], [1, 2])
            self.assertEqual(second['next_due'], second_reply + pending.REVIEW_SECONDS)
            self.assertEqual(second_reply, clock[0])
            self.assertEqual(self.coordinator.ask_count, 1)
            self.assertEqual(self.coordinator.gateway.cancel_count, 0)
            row = self.db.execute('SELECT * FROM order_intents').fetchone()
            self.assertEqual(row['created'], self.created)
            self.assertEqual(json.loads(row['payload']), self.intent)
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 0)

    def test_pending_long_discussion_survives_midnight_with_fresh_final_materials_and_same_order(self):
        clock = [pending._utc_instant('2026-11-01T16:59:00Z')]
        # Keep the accepted-order fixture on the scenario clock, not the wall
        # clock on the machine running this regression days or months later.
        self.now = clock[0]
        self.created = datetime.fromtimestamp(self.now - pending.REVIEW_SECONDS - 1, timezone.utc).isoformat()
        self.accepted = self.created
        self.fill_time = datetime.fromtimestamp(self.now - 1, timezone.utc).isoformat()
        self.db.execute('UPDATE order_intents SET created=?,updated=? WHERE id=?',
                        (self.created, self.created, self.operation))
        self.today = '2026-11-01'
        self.coordinator.before_dispatch = lambda: clock.__setitem__(0, clock[0] + 600)
        original_payload = self.db.execute('SELECT payload FROM order_intents').fetchone()[0]
        with patch.object(pending.time, 'time', side_effect=lambda: clock[0]):
            self.assertEqual(self.run_tick()['bot_status'], 'ENTRY_PENDING')
            self.assertFalse(self.coordinator.allowed(self.today))
            claim = self.claims()[0]
            self.assertEqual(claim['state'], 'COMPLETE')
            self.assertEqual(claim['next_due'], clock[0] + pending.REVIEW_SECONDS)
        self.assertEqual(self.coordinator.ask_count, 1)
        self.assertEqual(self.coordinator.gateway.cancel_count, 0)
        self.assertEqual(self.db.execute('SELECT payload FROM order_intents').fetchone()[0], original_payload)

    def test_already_protected_partial_remainder_is_reviewed_then_reviewed_again_after_tahan(self):
        self.coordinator.gateway.status = 'PARTIALLY_FILLED'
        self.coordinator.gateway.filled = '0.4'
        first = self.coordinator.gateway.reconcile(self.intent)
        self.coordinator.record_gateway_observation(self.operation, self.operation, first, {})
        self.assertEqual(self.db.execute('SELECT state FROM order_intents').fetchone()[0], 'POSITION_PROTECTED')
        self.assertEqual(self.run_tick()['bot_status'], 'POSITION_PROTECTED')
        self.assertEqual(self.coordinator.ask_count, 1)
        with pending._journal(self.db, create=True) as journal:
            journal.execute('UPDATE pending_reviews SET next_due=?', (time.time()-1,))
        self.run_tick()
        self.assertEqual(self.coordinator.ask_count, 2)
        self.assertEqual(len(self.claims()), 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 1)

    def test_fully_filled_protected_position_does_not_starve_original_native_advance(self):
        self.coordinator.gateway.status = 'FILLED'
        self.coordinator.gateway.filled = '1'
        self.assertEqual(self.run_tick()['bot_status'], 'ORIGINAL_ADVANCE_UNCHANGED')
        self.assertEqual(self.coordinator.ask_count, 0)
        self.assertEqual(self.coordinator.advance_count, 1)
        self.assertFalse((self.root / pending.JOURNAL_NAME).exists())

    def test_full_fill_during_discussion_does_not_cancel_filled_position(self):
        self.coordinator.decision = 'BATALKAN'
        self.coordinator.after_reply = lambda _: (setattr(self.coordinator.gateway, 'status', 'FILLED'),
                                                  setattr(self.coordinator.gateway, 'filled', '1'))
        self.assertEqual(self.run_tick()['bot_status'], 'POSITION_PROTECTED')
        self.assertEqual(self.coordinator.gateway.cancel_count, 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 1)

    def test_off_after_reply_never_cancels_and_does_not_replay_stale_decision(self):
        self.coordinator.decision = 'BATALKAN'
        self.coordinator.after_reply = lambda _: self.set_on(False)
        self.assertEqual(self.run_tick()['wait_reason'], 'ROBOT_OFF')
        self.assertEqual(self.coordinator.gateway.cancel_count, 0)
        self.assertEqual(self.claims()[0]['state'], 'PAUSED')
        self.assertEqual(self.db.execute('SELECT state FROM order_intents').fetchone()[0], 'ENTRY_PENDING')

    def test_off_at_wire_boundary_denies_cancel_and_marks_ambiguity_without_replay(self):
        self.coordinator.decision = 'BATALKAN'
        self.coordinator.gateway.turn_off_at_cancel = True
        self.assertEqual(self.run_tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.coordinator.gateway.cancel_count, 0)
        self.assertEqual(self.claims()[0]['state'], 'NEEDS_REVIEW')
        self.set_on(True)
        self.coordinator.gateway.turn_off_at_cancel = False
        self.assertEqual(self.run_tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.coordinator.ask_count, 1)

    def test_unknown_cancellation_is_not_replayed_or_replaced_with_new_screening(self):
        self.coordinator.decision = 'BATALKAN'
        self.coordinator.gateway.cancel_error = True
        self.assertEqual(self.run_tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.coordinator.gateway.cancel_count, 1)
        calls = len(self.coordinator.gateway.calls)
        self.run_tick()
        # The old position/order still gets native protection monitoring, but
        # no new browser dispatch, cancellation or candidate submission occurs.
        self.assertGreater(len(self.coordinator.gateway.calls), calls)
        self.assertEqual(self.coordinator.gateway.cancel_count, 1)
        self.assertEqual(self.coordinator.advance_count, 0)
        self.assertEqual(self.coordinator.ask_count, 1)

    def test_unknown_cancel_resolves_only_after_native_gateway_confirms_terminal_entry(self):
        self.coordinator.decision = 'BATALKAN'
        self.coordinator.gateway.cancel_error = True
        self.assertEqual(self.run_tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.coordinator.gateway.cancel_count, 1)
        # Simulate a delayed exchange acknowledgement visible to the existing
        # native reconciliation. This is not a second cancellation request.
        self.coordinator.gateway.cancel_error = False
        self.coordinator.gateway.status = 'CANCELED'
        self.assertEqual(self.run_tick()['bot_status'], 'ORIGINAL_ADVANCE_UNCHANGED')
        self.assertEqual(self.coordinator.gateway.cancel_count, 1)
        self.assertEqual(self.coordinator.ask_count, 1)
        claim = self.claims()[0]
        self.assertEqual(claim['state'], 'COMPLETE')
        self.assertEqual(json.loads(claim['result'])['resolution'], 'NATIVE_GATEWAY_RECONCILED')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 0)

    def test_unresolved_review_keeps_native_partial_fill_protection_monitoring(self):
        self.coordinator.raise_at_ask = ValueError('UNKNOWN_BROWSER_OUTCOME')
        self.assertEqual(self.run_tick()['bot_status'], 'NEEDS_REVIEW')
        self.coordinator.gateway.status = 'PARTIALLY_FILLED'
        self.coordinator.gateway.filled = '0.4'
        self.run_tick()
        self.assertEqual(self.db.execute('SELECT state FROM order_intents').fetchone()[0], 'POSITION_PROTECTED')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 1)
        self.assertEqual((self.coordinator.ask_count, self.coordinator.advance_count), (1, 0))

    def uncertain_review_then_terminal_order(self, state='CLOSED'):
        self.coordinator.raise_at_ask = ValueError('UNKNOWN_BROWSER_OUTCOME')
        self.assertEqual(self.run_tick()['failure_code'], 'PENDING_REVIEW_OUTCOME_UNKNOWN')
        self.coordinator.raise_at_ask = None
        result = {'source': 'BINANCE_FUTURES', 'symbol': self.intent['symbol'],
            'client_order_id': self.intent['client_order_id'], 'order_id': '111',
            'state': state, 'filled_quantity': '1' if state == 'CLOSED' else '0',
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'sl_confirmed': False, 'tp_confirmed': False}
        if state == 'CLOSED':
            result.update(first_fill_at=self.fill_time, exit_order_id='444',
                closed_at=datetime.fromtimestamp(time.time()-0.1, timezone.utc).isoformat())
        self.assertEqual(self.coordinator.record_gateway_observation(
            self.operation, self.operation, result, {})['bot_status'], state)
        return result

    def add_uncertain_native_review_request(self):
        claim = self.claims()[0]
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            (claim['operation'], None, claim['request_sha256'], 'NEEDS_REVIEW',
             claim['created'] + 0.001, 1, None, 'NEUROBRO_BROWSER_OUTCOME_UNKNOWN'))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?)',
            (claim['operation'], claim['request_body'], claim['request_sha256']))
        return dict(self.db.execute('SELECT * FROM api_requests WHERE operation=?',
                                   (claim['operation'],)).fetchone())

    def test_unknown_discussion_retires_only_after_exact_order_native_terminal_reconciliation(self):
        result = self.uncertain_review_then_terminal_order()
        original_payload = self.db.execute('SELECT payload FROM order_intents').fetchone()[0]
        with pending._journal(self.db, create=True) as journal:
            journal.execute('UPDATE pending_reviews SET result=?',
                (json.dumps({'failure': 'UNKNOWN_BROWSER_OUTCOME'}),))
        with patch.object(self.coordinator.gateway, 'reconcile', return_value=result) as reconcile:
            self.assertEqual(self.run_tick()['bot_status'], 'ORIGINAL_ADVANCE_UNCHANGED')
            claim = self.claims()[0]
            self.assertEqual((claim['state'], claim['decision'], claim['source_sha256']),
                             ('ORDER_TERMINAL_NOT_APPLIED', None, None))
            resolution = json.loads(claim['result'])
            self.assertEqual(resolution['resolution'], 'NATIVE_GATEWAY_ORDER_TERMINAL')
            self.assertEqual(resolution['review_outcome'], 'NOT_INFERRED')
            self.assertEqual(resolution['prior_result'], {'failure': 'UNKNOWN_BROWSER_OUTCOME'})
            self.assertEqual(resolution['gateway_observation']['state'], 'CLOSED')
            self.assertEqual(self.run_tick()['bot_status'], 'ORIGINAL_ADVANCE_UNCHANGED')
            self.assertEqual(reconcile.call_count, 1)
        self.assertEqual((self.coordinator.ask_count, self.coordinator.gateway.cancel_count), (1, 0))
        self.assertEqual(self.db.execute('SELECT payload FROM order_intents').fetchone()[0], original_payload)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM research_browser_responses').fetchone()[0], 0)
        self.assertEqual(len(self.claims()), 1)

    def test_unknown_discussion_for_unfilled_rejected_order_uses_same_native_recovery(self):
        result = self.uncertain_review_then_terminal_order('REJECTED')
        with patch.object(self.coordinator.gateway, 'reconcile', return_value=result):
            self.assertEqual(self.run_tick()['bot_status'], 'ORIGINAL_ADVANCE_UNCHANGED')
        self.assertEqual(self.claims()[0]['state'], 'ORDER_TERMINAL_NOT_APPLIED')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 0)
        self.assertEqual((self.coordinator.ask_count, self.coordinator.gateway.cancel_count), (1, 0))

    def test_terminal_label_without_fresh_valid_native_observation_does_not_release_review(self):
        self.uncertain_review_then_terminal_order()
        # A live/invalid observation cannot undo terminal ownership or be
        # accepted merely because the ledger used to say CLOSED.
        with patch.object(self.coordinator.gateway, 'reconcile', return_value={
                'source': 'UNVERIFIED', 'state': 'CLOSED'}):
            self.assertEqual(self.run_tick()['failure_code'], 'PENDING_REVIEW_OUTCOME_UNKNOWN')
        self.assertEqual(self.claims()[0]['state'], 'NEEDS_REVIEW')
        self.assertEqual(self.db.execute('SELECT state FROM order_intents').fetchone()[0], 'NEEDS_REVIEW')
        self.assertEqual(self.coordinator.advance_count, 0)

    def test_terminal_recovery_requires_same_payload_and_entry_creation(self):
        for column, value in (('intent_ref', 'f'*64), ('entry_created_at', '2000-01-01T00:00:00Z')):
            with self.subTest(column=column):
                # Each subcase starts from the same preserved unknown claim.
                if not self.claims():
                    result = self.uncertain_review_then_terminal_order()
                with pending._journal(self.db, create=True) as journal:
                    original = journal.execute('SELECT '+column+' FROM pending_reviews').fetchone()[0]
                    journal.execute('UPDATE pending_reviews SET '+column+'=?', (value,))
                with patch.object(self.coordinator.gateway, 'reconcile', return_value=result) as reconcile:
                    self.assertEqual(self.run_tick()['failure_code'], 'PENDING_REVIEW_OUTCOME_UNKNOWN')
                    reconcile.assert_not_called()
                with pending._journal(self.db, create=True) as journal:
                    journal.execute('UPDATE pending_reviews SET '+column+'=?', (original,))
                self.assertEqual(self.claims()[0]['state'], 'NEEDS_REVIEW')
        self.assertEqual(self.coordinator.advance_count, 0)

    def test_terminal_recovery_gateway_failure_keeps_original_uncertain_claim(self):
        self.uncertain_review_then_terminal_order()
        before = self.claims()[0]
        with patch.object(self.coordinator.gateway, 'reconcile', side_effect=ValueError('NATIVE_HISTORY_UNAVAILABLE')):
            self.assertEqual(self.run_tick()['failure_code'], 'PENDING_REVIEW_STATE_UNVERIFIED')
        self.assertEqual(self.claims()[0], before)
        self.assertEqual((self.coordinator.ask_count, self.coordinator.advance_count), (1, 0))

    def test_exact_uncertain_native_api_retires_without_faking_an_answer(self):
        result = self.uncertain_review_then_terminal_order()
        before = self.add_uncertain_native_review_request()
        source = tuple(self.db.execute('SELECT * FROM research_browser_requests').fetchone())
        self.assertTrue(self.coordinator.unresolved_research())
        with patch.object(self.coordinator.gateway, 'reconcile', return_value=result):
            self.assertEqual(self.run_tick()['bot_status'], 'ORIGINAL_ADVANCE_UNCHANGED')
        after = dict(self.db.execute('SELECT * FROM api_requests').fetchone())
        self.assertEqual(after, dict(before, state='ORDER_TERMINAL_NOT_APPLIED'))
        self.assertEqual(tuple(self.db.execute('SELECT * FROM research_browser_requests').fetchone()), source)
        self.assertFalse(self.coordinator.unresolved_research())
        claim = self.claims()[0]
        self.assertEqual(claim['state'], 'ORDER_TERMINAL_NOT_APPLIED')
        self.assertEqual(json.loads(claim['result'])['api_before'], before)
        self.assertIsNone(claim['decision'])
        self.assertIsNone(claim['source_sha256'])
        self.assertEqual((self.coordinator.ask_count, self.coordinator.gateway.cancel_count), (1, 0))

    def test_terminal_retirement_crash_before_api_CAS_recovers_without_replay(self):
        result = self.uncertain_review_then_terminal_order()
        before = self.add_uncertain_native_review_request()
        with patch.object(self.coordinator.gateway, 'reconcile', return_value=result):
            with patch.object(pending, '_retire_terminal_review_api', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.run_tick()
            claim = self.claims()[0]
            self.assertEqual(claim['state'], 'NEEDS_REVIEW')
            self.assertEqual(json.loads(claim['result'])['resolution'], 'NATIVE_GATEWAY_ORDER_TERMINAL_PREPARED')
            self.assertEqual(dict(self.db.execute('SELECT * FROM api_requests').fetchone()), before)
            self.assertEqual(self.coordinator.advance_count, 0)
            self.assertEqual(self.run_tick()['bot_status'], 'ORIGINAL_ADVANCE_UNCHANGED')
        self.assertEqual(self.claims()[0]['state'], 'ORDER_TERMINAL_NOT_APPLIED')
        self.assertEqual((self.coordinator.ask_count, self.coordinator.gateway.cancel_count), (1, 0))

    def test_terminal_retirement_crash_after_api_CAS_keeps_review_blocked_then_recovers(self):
        result = self.uncertain_review_then_terminal_order()
        before = self.add_uncertain_native_review_request()
        original_set = pending._set_state
        def interrupted(journal, operation, state, **kwargs):
            if state == 'ORDER_TERMINAL_NOT_APPLIED':
                raise KeyboardInterrupt
            return original_set(journal, operation, state, **kwargs)
        with patch.object(self.coordinator.gateway, 'reconcile', return_value=result):
            with patch.object(pending, '_set_state', side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    self.run_tick()
            self.assertEqual(self.claims()[0]['state'], 'NEEDS_REVIEW')
            self.assertEqual(dict(self.db.execute('SELECT * FROM api_requests').fetchone()),
                             dict(before, state='ORDER_TERMINAL_NOT_APPLIED'))
            self.assertEqual(self.coordinator.advance_count, 0)
            self.assertEqual(self.run_tick()['bot_status'], 'ORIGINAL_ADVANCE_UNCHANGED')
        self.assertEqual(self.claims()[0]['state'], 'ORDER_TERMINAL_NOT_APPLIED')
        self.assertEqual(json.loads(self.claims()[0]['result'])['api_before'], before)
        self.assertEqual((self.coordinator.ask_count, self.coordinator.gateway.cancel_count), (1, 0))

    def test_changed_native_request_hash_cannot_be_retired(self):
        result = self.uncertain_review_then_terminal_order()
        self.add_uncertain_native_review_request()
        self.db.execute("UPDATE api_requests SET body_hash=?", ('f'*64,))
        before = dict(self.db.execute('SELECT * FROM api_requests').fetchone())
        with patch.object(self.coordinator.gateway, 'reconcile', return_value=result):
            self.assertEqual(self.run_tick()['failure_code'], 'PENDING_REVIEW_STATE_UNVERIFIED')
        self.assertEqual(dict(self.db.execute('SELECT * FROM api_requests').fetchone()), before)
        self.assertEqual(self.claims()[0]['state'], 'NEEDS_REVIEW')
        self.assertEqual(self.coordinator.advance_count, 0)

    def test_terminal_recovery_never_clears_an_unrelated_uncertain_operation(self):
        result = self.uncertain_review_then_terminal_order()
        self.add_uncertain_native_review_request()
        unrelated = ('another-unknown-operation', None, 'b'*64, 'NEEDS_REVIEW',
                     time.time(), 1, None, 'NEUROBRO_BROWSER_OUTCOME_UNKNOWN')
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)', unrelated)
        with patch.object(self.coordinator.gateway, 'reconcile', return_value=result):
            self.run_tick()
        self.assertEqual(tuple(self.db.execute('SELECT * FROM api_requests WHERE operation=?',
                                              (unrelated[0],)).fetchone()), unrelated)
        self.assertTrue(self.coordinator.unresolved_research())
        self.assertEqual((self.coordinator.ask_count, self.coordinator.gateway.cancel_count), (1, 0))

    def test_unresolved_native_order_keeps_original_highest_priority_before_new_review(self):
        self.db.execute("UPDATE order_intents SET state='NEEDS_REVIEW'")
        self.assertEqual(self.run_tick()['bot_status'], 'ORIGINAL_ADVANCE_UNCHANGED')
        self.assertEqual(self.coordinator.ask_count, 0)
        self.assertFalse((self.root / pending.JOURNAL_NAME).exists())

    def test_native_unknown_research_blocks_new_chat_but_preserves_order_monitoring(self):
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            ('old-unknown-native-request', None, 'b'*64, 'PENDING', time.time()-1000, 1, None, None))
        self.assertEqual(self.run_tick()['failure_code'], 'ROBOT_REQUEST_NEEDS_REVIEW')
        self.assertIn('native_reconcile_ownership_protection', self.coordinator.gateway.calls)
        self.assertEqual((self.coordinator.ask_count, self.coordinator.advance_count), (0, 0))
        self.assertFalse((self.root / pending.JOURNAL_NAME).exists())

    def test_unverified_native_browser_receipt_does_not_authorize_cancel(self):
        self.coordinator.decision = 'BATALKAN'
        self.native['research_provider'].source_evidence = lambda *args: {
            'source_type': 'WRONG_PROVIDER', 'provider_evidence_sha256': 'a'*64}
        self.assertEqual(self.run_tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.coordinator.gateway.cancel_count, 0)
        self.assertEqual(self.claims()[0]['state'], 'NEEDS_REVIEW')

    def test_proven_unsent_login_pause_is_retryable_and_preserves_receipt(self):
        self.coordinator.raise_at_ask = ResearchPaused('LOGIN_REQUIRED')
        self.assertEqual(self.run_tick()['wait_reason'], 'PENDING_REVIEW_WAITING')
        self.assertEqual(self.claims()[0]['state'], 'UNSENT')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM api_requests').fetchone()[0], 0)
        self.coordinator.raise_at_ask = None
        with pending._journal(self.db, create=True) as journal:
            journal.execute('UPDATE pending_reviews SET next_due=?', (time.time()-1,))
        self.run_tick()
        self.assertEqual(len(self.claims()), 2)
        self.assertEqual([row['sequence'] for row in self.claims()], [1, 2])

    def test_keyboard_interrupt_preserves_inflight_claim_and_next_tick_refuses_replay(self):
        self.coordinator.raise_at_ask = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_tick()
        self.assertEqual(self.claims()[0]['state'], 'REVIEWING')
        self.coordinator.raise_at_ask = None
        self.assertEqual(self.run_tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.coordinator.ask_count, 1)

    def test_wrong_order_reference_or_unverified_source_can_never_cancel(self):
        self.coordinator.decision = 'BATALKAN'
        self.coordinator.force_wrong_ref = True
        self.assertEqual(self.run_tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.coordinator.gateway.cancel_count, 0)
        self.assertEqual(self.claims()[0]['state'], 'NEEDS_REVIEW')

    def test_candidate_native_provenance_tamper_prevents_any_order_reads_or_chat(self):
        self.db.execute("UPDATE robot_candidates SET plan='{}'")
        self.assertEqual(self.run_tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.coordinator.gateway.calls, [])
        self.assertEqual(self.coordinator.ask_count, 0)

    def test_symlink_or_mode_unsafe_private_journal_is_refused_without_chmod(self):
        foreign = self.root / 'foreign'
        foreign.write_bytes(b'untouched')
        path = self.root / pending.JOURNAL_NAME
        path.symlink_to(foreign)
        self.assertEqual(self.run_tick()['bot_status'], 'NEEDS_REVIEW')
        self.assertTrue(path.is_symlink())
        self.assertEqual(foreign.read_bytes(), b'untouched')

    def test_dispatch_rejects_off_day_namespace_intent_and_request_tampering(self):
        captured = []
        def verify_variants(_, operation):
            with pending._journal(self.db) as journal:
                claim = journal.execute('SELECT * FROM pending_reviews WHERE operation=?', (operation,)).fetchone()
                body = json.loads(claim['request_body'])
            return body, claim
        original_ask = self.coordinator.neuro.ask
        def ask(operation, prompt, schema, validate, context=None, **kwargs):
            body, serialized, sha = native_request_body(operation, prompt, schema, context)
            self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
                (operation, None, sha, 'PENDING', time.time(), 1, None, None))
            check = lambda **values: pending.verify_pending_dispatch(self.db,
                values.get('operation', operation), values.get('marker', body['marker']),
                values.get('sha', sha), values.get('today', self.today), time.time())
            captured.append(check())
            for mutation in ({'operation': operation+'x'}, {'marker': 'wrong'}, {'sha': 'b'*64}, {'today': '2000-01-01'}):
                self.assertFalse(check(**mutation))
            self.set_on(False)
            self.assertFalse(check())
            self.set_on(True)
            self.db.execute('UPDATE api_requests SET attempts=2')
            self.assertFalse(check())
            self.db.execute('UPDATE api_requests SET attempts=1')
            self.db.execute("UPDATE order_intents SET payload='{}'")
            self.assertFalse(check())
            raise ValueError('OFFLINE_END_OF_CHECK')
        self.coordinator.neuro.ask = ask
        self.run_tick()
        self.assertEqual(captured, [True])
        self.assertEqual(self.coordinator.gateway.cancel_count, 0)

    def test_current_verified_hook_artifact_sends_pending_phase_to_delegate(self):
        from scripts.patch_native_pending_review import (verify_current_pending_hook,
                                                        CURRENT_HOOK_SOURCE_SHA256)
        from tests.test_native_browser_hook import Bridge, client, native
        raw = (Path(__file__).resolve().parents[1] / 'agent/native_browser_hook.py').read_bytes()
        derived, receipt = verify_current_pending_hook(raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), CURRENT_HOOK_SOURCE_SHA256)
        self.assertEqual(derived, raw)
        self.assertFalse(receipt['source_changed'])
        module = types.ModuleType('agent._pending_hook_offline_fixture')
        module.__package__ = 'agent'
        exec(compile(derived, '<verified-current-pending-hook>', 'exec'), module.__dict__)
        intent_ref = digest(json.dumps(self.intent))
        operation = 'pending-review-v1:' + intent_ref[:32] + ':1'
        context = dict(native_context(time.time()), pending_review={
            'intent_ref': intent_ref, 'symbol': 'BTCUSDT', 'entry_created_at': self.created})
        payload, serialized, request_sha = native_request_body(operation, 'UNCHANGED_NATIVE_REVIEW_PROMPT',
                                                              protocol.REVIEW_SCHEMA, context)
        payload['request_sha256'] = request_sha
        output = {'symbol': 'BTCUSDT', 'order_ref': intent_ref, 'decision': 'TAHAN'}
        bridge = Bridge(initial=json.dumps(output))
        reasoner, native_dom = client('TAHAN'), native()
        native_dom.exact_json.return_value = output
        hook = module.DelegatedDOMBridge(bridge, reasoner, native=native_dom,
                                         monotonic=bridge.clock, wall_clock=time.time)
        result = hook.ask(payload, lambda: True, deadline=80)
        observed = reasoner.request.call_args.args[0]
        self.assertEqual(observed['phase'], 'PENDING_REVIEW')
        self.assertEqual(observed['conversation_id'], operation)
        self.assertEqual(observed['market_context']['provenance']['chart_kind'], 'NATIVE_PUBLIC_KLINES_CONTEXT')
        self.assertEqual(result['assistant_text'], json.dumps(output))
        self.assertEqual(bridge.clicks, 1)
        self.assertEqual(bridge.messages[0]['text'], payload['prompt'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), CURRENT_HOOK_SOURCE_SHA256)


if __name__ == '__main__':
    unittest.main()
