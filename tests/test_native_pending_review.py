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
    path = Path('/tmp/harun-reconciled-production-source/worker/robot.py')
    if not path.exists():
        raise unittest.SkipTest('Sanitized authenticated native fixture unavailable')
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
                'clientOrderId': intent['client_order_id']}

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
        self.market = types.SimpleNamespace(fresh=self.market_fresh)
        self.neuro = types.SimpleNamespace(ask=self.ask)
        self.ask_count = self.advance_count = 0
        self.decision = 'TAHAN'
        self.after_reply = None
        self.raise_at_ask = None
        self.force_wrong_ref = False

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
        return self.gateway_allowed() and today == self.case.today

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
        assert pending.verify_pending_dispatch(self.db, operation, body['marker'], sha,
                                               self.case.today, time.time()) is True
        value = {'symbol': context['symbol'], 'order_ref': context['pending_review']['intent_ref'],
                 'decision': self.decision}
        if self.force_wrong_ref:
            value['order_ref'] = 'f' * 64
        validate(value)
        self.db.execute("UPDATE api_requests SET state='COMPLETE',output=? WHERE operation=?",
                        (canonical(value, schema), operation))
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
        ''')
        self.now = time.time()
        self.today = datetime.now(ZoneInfo('Asia/Jakarta')).date().isoformat()
        self.created = datetime.fromtimestamp(self.now - pending.REVIEW_SECONDS - 1, timezone.utc).isoformat()
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
        self.assertIn('"price":"100"', body['prompt'])
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

    def test_partial_fill_during_discussion_keeps_native_protection_and_entry_receipt(self):
        self.coordinator.decision = 'BATALKAN'
        def fill(_):
            self.coordinator.gateway.status = 'PARTIALLY_FILLED'
            self.coordinator.gateway.filled = '0.4'
        self.coordinator.after_reply = fill
        result = self.run_tick()
        self.assertEqual(result['bot_status'], 'POSITION_PROTECTED')
        self.assertEqual(self.coordinator.gateway.cancel_count, 1)
        row = self.db.execute('SELECT * FROM order_intents').fetchone()
        self.assertEqual(row['state'], 'POSITION_PROTECTED')
        self.assertEqual(json.loads(row['result'])['filled_quantity'], '0.4')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 1)

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

    def test_unresolved_review_keeps_native_partial_fill_protection_monitoring(self):
        self.coordinator.raise_at_ask = ValueError('UNKNOWN_BROWSER_OUTCOME')
        self.assertEqual(self.run_tick()['bot_status'], 'NEEDS_REVIEW')
        self.coordinator.gateway.status = 'PARTIALLY_FILLED'
        self.coordinator.gateway.filled = '0.4'
        self.run_tick()
        self.assertEqual(self.db.execute('SELECT state FROM order_intents').fetchone()[0], 'POSITION_PROTECTED')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 1)
        self.assertEqual((self.coordinator.ask_count, self.coordinator.advance_count), (1, 0))

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

    def test_actual_published_hook_artifact_sends_pending_phase_to_delegate(self):
        from scripts.patch_native_pending_review import edits_for, SOURCE_PINS
        from tests.test_native_browser_hook import Bridge, client, native
        raw = (Path(__file__).resolve().parents[1] / 'agent/native_browser_hook.py').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), SOURCE_PINS['agent/native_browser_hook.py'])
        start, end, replacement = edits_for('agent/native_browser_hook.py', raw, ast.parse(raw))[0]
        derived = raw[:start] + replacement + raw[end:]
        module = types.ModuleType('agent._pending_hook_offline_fixture')
        module.__package__ = 'agent'
        exec(compile(derived, '<derived-pending-hook>', 'exec'), module.__dict__)
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
        self.assertEqual(hashlib.sha256(raw).hexdigest(), SOURCE_PINS['agent/native_browser_hook.py'])


if __name__ == '__main__':
    unittest.main()
