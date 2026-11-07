"""Durable receipt recovery fixtures; no live Hermes/provider execution proof."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent import dialogue_service as service
from agent.hermes_dialogue import HermesDialogue, PROTOCOL, RUNTIME
from agent.llm import validated_market_context
from agent.memory import Memory
from agent.models import AgentError, AmbiguousOperation, NotSent
from agent.native_market_context import convert_native_market_context
from tests.test_agent_native_market_context import native_context


RECOVERY = 'HERMES_RECEIPT_RECOVERY'


class HermesDialogueRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.memory = Memory(self.directory / 'agent.sqlite3')
        self.now = time.time()
        self.clock = lambda: self.now
        self.pins = dict(runtime_source_sha256='a' * 64, runtime_config_sha256='b' * 64,
                         isolation_policy_sha256='c' * 64)
        self.sent = []
        self.lookups = []
        self.runtime_receipts = {}
        self.last_receipt = None
        self.disconnect = True
        self.lookup_state = 'completed'
        self.receipt_mutation = None
        self.verified = True
        self.output = {'action': 'SCREENING', 'symbols': ['BTCUSDT'], 'question': ''}
        self.make_engine()

    def tearDown(self):
        self.memory.close()
        self.temporary.cleanup()

    def request(self, index=0):
        reply = 'BTCUSDT'
        return dict(schema_version=1, request_id=hashlib.sha256(str(index).encode()).hexdigest(),
            conversation_id='recovery:hermes-fixture', phase='SCREENING', reply=reply,
            reply_sha256=hashlib.sha256(reply.encode()).hexdigest(), market_context=None,
            gate={'robot_on': True, 'native_dispatch_verified': True}, deadline_epoch=self.now + 80)

    def transport(self, wire):
        self.sent.append(deepcopy(wire))
        receipt = {**{key: wire[key] for key in ('request_id', 'request_sha256',
            'conversation_id', 'phase', 'model', *self.pins)},
            'schema_version': 1, 'protocol': PROTOCOL, 'runtime': RUNTIME,
            'status': 'completed', 'trace_id': 'hermes-recovery-trace',
            'session_id': 'hermes-recovery-session', 'observed_at': self.now,
            'decision': deepcopy(self.output)}
        self.runtime_receipts[wire['request_id']] = deepcopy(receipt)
        self.last_receipt = deepcopy(receipt)
        if self.disconnect:
            raise TimeoutError('fixture lost reply after durable runtime completion')
        return receipt

    def lookup(self, request_id, request_sha256):
        self.lookups.append((request_id, request_sha256))
        self.last_receipt = None
        if self.lookup_state == 'missing':
            return None
        if self.lookup_state == 'unknown':
            raise AmbiguousOperation('fixture runtime delivery remains UNKNOWN')
        receipt = deepcopy(self.runtime_receipts[request_id])
        if self.receipt_mutation is not None:
            self.receipt_mutation(receipt)
        self.last_receipt = deepcopy(receipt)
        return receipt

    def attestation(self):
        if not self.verified or self.last_receipt is None:
            return {}
        return {**{key: self.last_receipt[key] for key in ('request_id', 'request_sha256',
            'trace_id', 'session_id', 'runtime', 'model', *self.pins)},
            'verified': True, 'kernel_peer_verified': True, 'isolation_verified': True}

    def make_engine(self):
        self.model = HermesDialogue('/missing-and-never-read', self.memory, model='gpt-6.1-sol',
            transport=self.transport, receipt_lookup=self.lookup, attestation=self.attestation,
            runtime_ready=lambda: True, clock=self.clock, **self.pins)
        self.engine = service.DialogueEngine(self.memory, '/missing-and-never-read',
                                             model=self.model, clock=self.clock)

    def restart(self):
        self.memory.close()
        self.memory = Memory(self.directory / 'agent.sqlite3')
        self.last_receipt = None
        self.make_engine()

    def lose_reply(self, request=None):
        request = request or self.request()
        with self.assertRaises(service.DialogueError):
            self.engine.process(request)
        self.assertEqual(len(self.sent), 1)
        rows = self.memory.db.execute('SELECT state FROM operations WHERE operation_id IN (?,?)',
            ('delegated-model:' + request['request_id'], 'hermes-runtime:' + request['request_id'])).fetchall()
        self.assertEqual(sorted(rows), [('UNKNOWN',), ('UNKNOWN',)])
        return request

    def custody(self, request_id):
        return self.engine.process({'kind': RECOVERY, 'request_id': request_id})

    def database_snapshot(self):
        return list(self.memory.db.iterdump())

    def budgets(self):
        return self.memory.db.execute('SELECT * FROM budgets ORDER BY day').fetchall()

    def assert_result(self, result, request):
        service.validate_result(result, request)
        self.assertEqual(result['decision'], {'action': 'SCREENING', 'symbols': ['BTCUSDT'], 'question': ''})
        self.assertFalse(result['trading_authority'])

    def test_lost_runtime_reply_recovers_after_restart_without_execution_or_new_budget(self):
        request = self.lose_reply()
        sent = deepcopy(self.sent[0])
        budget = self.budgets()
        archive = self.model.recovery_archive(request['request_id'])
        self.assertEqual(archive['request'], request)
        self.assertEqual(archive['rpc_payload'], sent)
        self.assertEqual(set(archive), {'schema_version', 'request', 'rpc_payload', 'prepared_at', 'history_anchor'})
        self.restart()
        result = self.engine.recover_hermes(request['request_id'])
        self.assert_result(result, request)
        self.assertEqual(self.lookups, [(request['request_id'], sent['request_sha256'])])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.budgets(), budget)
        self.assertEqual(self.memory.counts()['operations'], {'DONE': 2})
        self.assertEqual(self.memory.history(request['conversation_id']), [
            {'role': 'user', 'content': request['reply']},
            {'role': 'assistant', 'content': json.dumps(result['decision'], sort_keys=True, separators=(',', ':'))}])

    def test_expired_original_deadline_allows_only_explicit_custody_recovery(self):
        request = self.lose_reply()
        self.now += 180
        self.restart()
        with self.assertRaisesRegex(service.DialogueError, 'DEADLINE_EXPIRED'):
            self.engine.process(request)
        self.assertEqual(self.lookups, [])
        envelope = self.custody(request['request_id'])
        self.assertEqual(set(envelope), {'schema_version', 'ok', 'kind', 'request_id', 'receipt',
                                         'dispatch_authorized', 'trading_authority'})
        self.assertTrue(envelope['ok'])
        self.assertEqual(envelope['kind'], RECOVERY)
        self.assertEqual(envelope['request_id'], request['request_id'])
        self.assertIs(envelope['dispatch_authorized'], False)
        self.assertIs(envelope['trading_authority'], False)
        self.assert_result(envelope['receipt'], request)
        self.assertEqual(len(self.sent), 1)

    def test_missing_unknown_foreign_or_unverified_receipt_does_not_change_rows(self):
        request = self.lose_reply()
        self.restart()
        cases = [
            ('missing', None, True), ('unknown', None, True),
            ('completed', lambda value: value.update(request_sha256='f' * 64), True),
            ('completed', lambda value: value.update(model='gpt-5-mini'), True),
            ('completed', None, False),
        ]
        for state, mutation, verified in cases:
            self.lookup_state, self.receipt_mutation, self.verified = state, mutation, verified
            before, budget = self.database_snapshot(), self.budgets()
            with self.subTest(state=state, mutation=mutation, verified=verified), self.assertRaises(Exception):
                self.custody(request['request_id'])
            self.assertEqual(self.database_snapshot(), before)
            self.assertEqual(self.budgets(), budget)
            self.assertEqual(len(self.sent), 1)
            self.assertEqual(self.memory.history(request['conversation_id']), [])

    def test_normal_fresh_request_recovers_original_identity_without_replaying_rpc(self):
        request = self.lose_reply()
        self.now += 180
        self.restart()
        fresh = {**deepcopy(request), 'deadline_epoch': self.now + 80}
        result = self.engine.process(fresh)
        self.assert_result(result, request)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.lookups), 1)

    def test_process_interruption_left_started_rows_recovers_without_reexecution(self):
        request = self.lose_reply()
        self.memory.db.execute('UPDATE operations SET state=? WHERE operation_id IN (?,?)',
            ('STARTED', 'delegated-model:' + request['request_id'], 'hermes-runtime:' + request['request_id']))
        budget = self.budgets()
        self.restart()
        self.assert_result(self.engine.process(request), request)
        self.assertEqual(self.memory.counts()['operations'], {'DONE': 2})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.budgets(), budget)

    def test_normal_recovery_requires_fresh_gates_and_unchanged_source_identity(self):
        request = self.lose_reply()
        self.restart()
        changed = deepcopy(request)
        changed.update(reply='ETHUSDT', reply_sha256=hashlib.sha256(b'ETHUSDT').hexdigest())
        variants = [changed, {**request, 'conversation_id': 'foreign-conversation'},
            {**request, 'gate': {'robot_on': False, 'native_dispatch_verified': True}},
            {**request, 'gate': {'robot_on': True, 'native_dispatch_verified': False}},
            {**request, 'deadline_epoch': self.now - 1}]
        before = self.database_snapshot()
        for incoming in variants:
            with self.subTest(incoming=incoming), self.assertRaises(Exception):
                self.engine.process(incoming)
            self.assertEqual(self.database_snapshot(), before)
            self.assertEqual(self.lookups, [])
            self.assertEqual(len(self.sent), 1)

    def test_inner_completed_receipt_recovers_outer_unknown_without_another_model_call(self):
        request = self.request()
        self.disconnect = False
        with patch.object(self.memory, 'append', side_effect=KeyboardInterrupt('fixture crash before history')):
            with self.assertRaises(KeyboardInterrupt):
                self.engine.process(request)
        self.assertEqual(self.memory.counts()['operations'], {'DONE': 1, 'UNKNOWN': 1})
        self.restart()
        self.assert_result(self.engine.recover_hermes(request['request_id']), request)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.memory.counts()['operations'], {'DONE': 2})
        self.assertEqual(len(self.memory.history(request['conversation_id'])), 2)

    def test_recovery_history_is_atomic_once_after_mid_append_crash_and_repeated_lookup(self):
        request = self.lose_reply()
        self.restart()
        original_append = self.memory.append
        appended = []

        def interrupted_append(*args, **kwargs):
            appended.append(args[1])
            if len(appended) == 2:
                raise KeyboardInterrupt('fixture crash between history rows')
            return original_append(*args, **kwargs)

        with patch.object(self.memory, 'append', side_effect=interrupted_append):
            with self.assertRaises(KeyboardInterrupt):
                self.engine.recover_hermes(request['request_id'])
        self.assertEqual(self.memory.history(request['conversation_id']), [])
        self.restart()
        first = self.custody(request['request_id'])
        self.assertEqual(len(self.memory.history(request['conversation_id'])), 2)
        before = self.database_snapshot()
        self.assertEqual(self.custody(request['request_id']), first)
        self.assertEqual(self.database_snapshot(), before)
        self.assertEqual(len(self.sent), 1)

    def test_later_conversation_history_never_acquires_the_old_recovered_turn_at_its_tail(self):
        request = self.lose_reply()
        self.memory.append(request['conversation_id'], 'user', 'A later independently owned turn', self.now)
        self.memory.append(request['conversation_id'], 'assistant', 'A later completed response', self.now)
        later = self.memory.history(request['conversation_id'])
        self.restart()
        self.assert_result(self.custody(request['request_id'])['receipt'], request)
        self.assertEqual(self.memory.history(request['conversation_id']), later)
        before = self.database_snapshot()
        self.custody(request['request_id'])
        self.assertEqual(self.database_snapshot(), before)
        self.assertEqual(len(self.sent), 1)

    def test_missing_or_corrupt_prospective_archive_cannot_grant_lookup_or_reexecution(self):
        request = self.lose_reply()
        key = service.RECOVERY_PREFIX + request['request_id']
        manifest = self.memory.get(key)
        for missing in (False, True):
            if missing:
                self.memory.db.execute('DELETE FROM metadata WHERE key=?', (key,))
            else:
                self.memory.put(key, {**manifest, 'sha256': 'f' * 64})
            self.restart()
            before = self.database_snapshot()
            with self.subTest(missing=missing), self.assertRaises(Exception):
                self.custody(request['request_id'])
            with self.assertRaises(Exception):
                self.engine.process(request)
            self.assertEqual(self.database_snapshot(), before)
            self.assertEqual(self.lookups, [])
            self.assertEqual(len(self.sent), 1)

    def test_conclusive_not_sent_receipts_never_become_recovery_or_reexecution_grants(self):
        request = self.request()
        attempts = []

        def not_sent(wire):
            attempts.append(deepcopy(wire))
            raise NotSent('fixture conclusive pre-model rejection')

        self.model.transport = not_sent
        with self.assertRaises(service.DialogueError):
            self.engine.process(request)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.memory.counts()['operations'], {'DONE': 2})
        before, budget = self.database_snapshot(), self.budgets()
        self.restart()
        with self.assertRaises(service.DialogueError):
            self.custody(request['request_id'])
        with self.assertRaises(service.DialogueError):
            self.engine.process(request)
        self.assertEqual(self.database_snapshot(), before)
        self.assertEqual(self.budgets(), budget)
        self.assertEqual(self.lookups, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.memory.history(request['conversation_id']), [])

    def test_chunked_analysis_recovery_uses_archived_context_and_keeps_private_facts_local(self):
        # Crossing the next 15-minute boundary makes these exact candles stale.
        self.now = int(self.now // 900) * 900 + 850
        request = self.request(index=75)
        request.update(phase='COMPOSE_ANALYSIS', reply='Analisis BTCUSDT sesuai data Futures terbaru.',
            market_context=convert_native_market_context(native_context(self.now), now=self.now))
        request['reply_sha256'] = hashlib.sha256(request['reply'].encode()).hexdigest()
        private_values = {'symbol': 'BTCUSDT', 'entry_price': '100', 'stop_loss': '99',
            'neurobro_tp': '102', 'risk_usdt': 'LOCAL_PRIVATE_RISK_5A',
            'quantity': 'LOCAL_PRIVATE_QTY_2B', 'fee_entry_rate': 'LOCAL_PRIVATE_FEE_3C',
            'execution_tp': 'LOCAL_PRIVATE_EXEC_TP_4D'}
        request['source_facts'] = [{'producer': 'RISK_MANAGER', 'operation': 'original-setup',
            'values': private_values, 'sha256': hashlib.sha256(json.dumps(private_values,
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()}]
        original_history = [
            ('user', json.dumps({'COIN': 'BTCUSDT', 'ENTRY': '100', 'SL': '99',
                'quantity': 'HISTORY_PRIVATE_QTY_5E', 'fee': 'HISTORY_PRIVATE_FEE_6F'})),
            ('assistant', 'Quantity HISTORY_RAW_QTY_7G. Risiko Office 928.45 USDT.')]
        for role, text in original_history:
            self.memory.append(request['conversation_id'], role, text, self.now)
        self.output = {'action': 'PROMPT', 'symbols': [], 'question':
            'Mohon analisis BTCUSDT, pertimbangkan wick dan likuiditas, RISK REWARD 1:2; '
            'Neurobro tutup keputusan final dengan JSON COIN, ARAH, ENTRY, TP, SL.'}
        original_request = deepcopy(request)
        self.lose_reply(request)
        wire = deepcopy(self.sent[0])
        encoded_wire = json.dumps(wire)
        for canary in ('LOCAL_PRIVATE_RISK_5A', 'LOCAL_PRIVATE_QTY_2B', 'LOCAL_PRIVATE_FEE_3C',
                       'LOCAL_PRIVATE_EXEC_TP_4D', 'HISTORY_PRIVATE_QTY_5E',
                       'HISTORY_PRIVATE_FEE_6F', 'HISTORY_RAW_QTY_7G', '928.45'):
            self.assertNotIn(canary, encoded_wire)
        self.assertEqual(request, original_request)
        self.assertEqual(wire['source_facts'][0]['values'], {
            'symbol': 'BTCUSDT', 'entry_price': '100', 'stop_loss': '99', 'neurobro_tp': '102'})
        manifest = self.memory.get(service.RECOVERY_PREFIX + request['request_id'])
        self.assertGreater(manifest['chunks'], 1)
        budget = self.budgets()
        self.now += 180
        with self.assertRaises(AgentError):
            validated_market_context(request['market_context'], now=self.now)
        self.restart()
        archive = self.model.recovery_archive(request['request_id'])
        self.assertEqual(archive['request'], original_request)
        self.assertEqual(archive['rpc_payload'], wire)
        envelope = self.custody(request['request_id'])
        self.assertFalse(envelope['dispatch_authorized'])
        self.assertFalse(envelope['trading_authority'])
        service.validate_result(envelope['receipt'], original_request)
        self.assertEqual(envelope['receipt']['decision'], self.output)
        self.assertEqual(self.lookups, [(request['request_id'], wire['request_sha256'])])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.budgets(), budget)
        history = self.memory.history(request['conversation_id'])
        self.assertEqual([(item['role'], item['content']) for item in history[:2]], original_history)
        self.assertEqual([(item['role'], item['content']) for item in history[2:]], [
            ('user', request['reply']), ('assistant', json.dumps(self.output, sort_keys=True, separators=(',', ':')))])

    def test_historical_mini_rows_remain_immutable_and_have_no_hermes_recovery_grant(self):
        legacy = self.request(index=55)
        receipt = {'schema_version': 1, 'ok': True,
            **{key: legacy[key] for key in ('request_id', 'conversation_id', 'phase', 'reply_sha256')},
            'authority': 'DIALOGUE_CONTROL_ONLY', 'trading_authority': False,
            'decision': {'action': 'SCREENING', 'symbols': ['BTCUSDT'], 'question': ''},
            'needs_followup': False, 'reason': 'DIALOGUE_DECISION'}
        operation_id = 'delegated-model:' + legacy['request_id']
        self.memory.operation(operation_id, 'DELEGATED_MODEL_DIALOGUE', legacy, lambda: receipt)
        historical = self.memory.db.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
        before = self.database_snapshot()
        with self.assertRaises(Exception):
            self.custody(legacy['request_id'])
        self.assertEqual(self.database_snapshot(), before)
        request = self.lose_reply()
        self.restart()
        self.custody(request['request_id'])
        self.assertEqual(self.memory.db.execute('SELECT * FROM operations WHERE operation_id=?',
                                               (operation_id,)).fetchone(), historical)
        self.assertEqual(self.engine.process(legacy), receipt)


if __name__ == '__main__':
    unittest.main()
