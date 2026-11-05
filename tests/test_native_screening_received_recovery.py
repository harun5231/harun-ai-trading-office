"""Native completion recovery uses the existing room and never sends again."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.native_daily_room import DailyRoom, identities, JOURNAL_NAME
from agent.native_prompt_composition import PromptCompositionCache
from agent.native_screening_recovery import (KIND, NATIVE_DOM_SHA256,
    ScreeningRecoveryUnavailable, canonical, clarification, digest, enqueue,
    read_receipt, recover, text_sha, validate, validate_receipt)
from tests.test_native_daily_room import Bridge, History, native
from worker.account_state import screening_contract

NOW = datetime(2026, 11, 5, 8, tzinfo=timezone.utc).timestamp()
OP = '2026-11-05:robot-v9:0:screening:0:2'


def payload(count=2):
    operation = OP[:-1] + str(count)
    prompt, schema = screening_contract(count)
    body = dict(operation=operation, provider='NEUROBRO_WEB',
        contract_version='neurobro-browser-decision-v2', prompt=prompt,
        output_schema=copy.deepcopy(schema), context=None,
        marker='HARUN_' + text_sha(operation)[:32])
    return dict(body, request_sha256=digest(body))


class ScreeningReceivedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.payload = payload()
        self.raw = Bridge()
        self.native = native()
        self.native.user_prompt = lambda p: p['prompt'] + '\n\nOUTPUT_JSON_SCHEMA:\n' + json.dumps(p['output_schema'])
        self.room = DailyRoom(self.raw, self.native, self.root, wall_clock=lambda: NOW,
                              history=History(self.raw))
        self.cache = PromptCompositionCache(self.native, self.root)
        rendered = self.cache.store(self.payload, self.payload['prompt'], 'a' * 64)
        self.raw.messages = [
            dict(role='user', text=rendered, attachments=[]),
            dict(role='assistant', text='COIN: RUNEUSDT, TIAUSDT', complete=True, id='reply-original'),
            dict(role='user', text=clarification(2), attachments=[]),
            dict(role='assistant', text='Ringkasan akhir\nCOIN: RUNEUSDT, TIAUSDT', complete=True, id='reply-summary')]
        self.bind_room()
        self.delegated = SimpleNamespace(daily_room=self.room, bridge=self.raw,
                                        native=self.native, prompt_cache=self.cache)
        self.actor = SimpleNamespace(_bridge=self.delegated, mode='AUTOMATION',
            ready=True, current_status='CONNECTED', stopping=threading.Event(),
            sessions=SimpleNamespace(active=lambda: False), claim=threading.Lock(),
            lock=threading.RLock(), queue=queue.Queue(), _wake=threading.Event())
        self.actor._mode = lambda: self.actor.mode
        self.clock = patch('agent.native_screening_recovery.time.time', return_value=NOW)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.native_digest = patch('agent.native_screening_recovery._native_source_digest',
                                   return_value=NATIVE_DOM_SHA256)
        self.native_digest.start(); self.addCleanup(self.native_digest.stop)
        self.raw.ask = self.forbid
        self.raw._fresh_conversation = self.forbid
        self.raw.composer.fill = self.forbid
        self.raw.button.click = self.forbid
        self.raw._upload = self.forbid
        self.journal_before = (self.root / JOURNAL_NAME).read_bytes()

    def forbid(self, *args, **kwargs):
        self.fail('Recovery called a browser mutation, upload, Send or normal ask')

    def bind_room(self, state='BOUND', **changes):
        binding = dict(first_prompt_sha256=text_sha(self.raw.messages[0]['text'].strip()),
                       marker=self.payload['marker'], history_titles=['Pilih 2 Coin Binance Futures'])
        fields = dict(day='2026-11-05', room_id='a' * 32, state=state,
            url=self.raw.page.url, anchors=json.dumps(identities(self.raw.messages)),
            binding=json.dumps(binding), updated_at=NOW-900)
        fields.update(changes)
        with self.room.store.open() as db:
            db.execute('DELETE FROM daily_rooms')
            db.execute('INSERT INTO daily_rooms VALUES(?,?,?,?,?,?,?)', tuple(fields.values()))

    def invoke(self):
        with self.actor.claim:
            return recover(self.actor, self.delegated, self.payload)

    def test_native_stable_read_keeps_journal_and_durable_receipt_idempotent(self):
        original = (self.room.offset, self.room.prefix, self.raw._turns,
                    self.raw.operation_deadline, self.raw._operation_contract)
        result = self.invoke()
        self.assertEqual(result['output'], {'symbols': ['RUNEUSDT', 'TIAUSDT']})
        self.assertEqual(result['assistant_text'], self.raw.messages[-1]['text'])
        self.assertEqual(result['response_id'], 'dom-' + text_sha(
            self.raw.page.url + '\n' + self.payload['marker'] + '\n' + result['assistant_text']))
        self.assertEqual(original, (self.room.offset, self.room.prefix, self.raw._turns,
            self.raw.operation_deadline, self.raw._operation_contract))
        self.assertEqual((self.root / JOURNAL_NAME).read_bytes(), self.journal_before)
        proof = read_receipt(self.root, self.payload)
        self.assertEqual(proof['stable_reads'], 4)
        self.assertEqual(proof['native_poll_reads'], 4)
        self.assertEqual(proof['native_received_at'], NOW)
        self.assertEqual(proof['room']['updated_at'], NOW-900)
        self.assertEqual(proof['rows'], self.raw.messages)
        self.assertEqual(proof['initial_reply_sha256'], text_sha(self.raw.messages[1]['text']))
        self.assertEqual(validate_receipt(copy.deepcopy(proof), self.payload), proof)
        receipt_path = next(self.root.glob('screening-recovery-*.json'))
        receipt_before = receipt_path.read_bytes()
        self.raw._answer = self.forbid
        self.assertEqual(self.invoke(), result)
        self.assertEqual(receipt_path.read_bytes(), receipt_before)

    def test_transient_none_does_not_invent_fixed_poll_limit(self):
        answer = self.raw._answer
        calls = []
        def delayed(*args):
            calls.append(1)
            return None if len(calls) == 1 else answer(*args)
        self.raw._answer = delayed
        self.invoke()
        proof = read_receipt(self.root, self.payload)
        self.assertEqual(proof['stable_reads'], 4)
        self.assertEqual(proof['native_poll_reads'], 5)

    def test_current_native_source_mismatch_refuses(self):
        self.native_digest.stop()
        with patch('agent.native_screening_recovery._native_source_digest', return_value='0'*64):
            with self.assertRaises(ScreeningRecoveryUnavailable): self.invoke()
        self.assertFalse(list(self.root.glob('screening-recovery-*.json')))

    def test_wrong_room_state_day_cache_or_actual_row_does_not_write_receipt(self):
        original = copy.deepcopy(self.raw.messages)
        for mutation in ('SENT', 'wrong-day', 'wrong-url', 'modified-prefix', 'manual-followup',
                         'missing-cache', 'partial', 'attachment', 'different-coins'):
            with self.subTest(mutation=mutation):
                self.raw.messages = copy.deepcopy(original)
                self.delegated.prompt_cache = self.cache
                self.bind_room()
                if mutation == 'SENT': self.bind_room(state='SENT')
                if mutation == 'wrong-day': self.bind_room(day='2026-11-04')
                if mutation == 'wrong-url': self.bind_room(url='https://app.neurobro.ai/elsewhere')
                if mutation == 'modified-prefix': self.raw.messages[1]['id'] = 'different-native-id'
                if mutation == 'manual-followup':
                    self.raw.messages[2]['text'] = 'Buy now please'; self.bind_room()
                if mutation == 'missing-cache': self.delegated.prompt_cache = None
                if mutation == 'partial': self.raw.messages[-1]['complete'] = False; self.bind_room()
                if mutation == 'attachment': self.raw.messages[0]['attachments'] = ['UNCONFIRMED']; self.bind_room()
                if mutation == 'different-coins': self.raw.messages[-1]['text'] = 'COIN: BTCUSDT, ETHUSDT'; self.bind_room()
                with self.assertRaises((ScreeningRecoveryUnavailable, ValueError, TypeError)):
                    self.invoke()
                self.assertFalse(list(self.root.glob('screening-recovery-*.json')))

    def test_manual_login_challenge_stopping_and_mid_read_change_refuse(self):
        for state in ('MANUAL', 'LOGIN_REQUIRED', 'CHALLENGE_REQUIRED', 'UNAVAILABLE', 'STOPPING'):
            with self.subTest(state=state):
                self.actor.mode = 'MANUAL' if state == 'MANUAL' else 'AUTOMATION'
                self.actor.stopping.clear()
                if state == 'STOPPING': self.actor.stopping.set()
                self.raw.status = lambda: state if state in ('LOGIN_REQUIRED','CHALLENGE_REQUIRED','UNAVAILABLE') else 'CONNECTED'
                with self.assertRaises(ScreeningRecoveryUnavailable): self.invoke()
        self.actor.mode = 'AUTOMATION'; self.actor.stopping.clear(); self.raw.status = lambda: 'CONNECTED'
        answer = self.raw._answer
        def changed(*args):
            value = answer(*args)
            self.raw.messages[-1]['id'] = 'changed-while-reading'
            return value
        self.raw._answer = changed
        with self.assertRaises(ScreeningRecoveryUnavailable): self.invoke()
        self.assertFalse(list(self.root.glob('screening-recovery-*.json')))

    def test_cached_proof_tamper_or_request_reuse_is_rejected(self):
        self.invoke()
        proof = read_receipt(self.root, self.payload)
        changed = copy.deepcopy(proof); changed['native_result']['output']['symbols'][0] = 'BTCUSDT'
        with self.assertRaises(ScreeningRecoveryUnavailable): validate_receipt(changed, self.payload)
        changed = copy.deepcopy(proof); changed['native_received_at'] = NOW-1000
        with self.assertRaises(ScreeningRecoveryUnavailable): validate_receipt(changed, self.payload)
        for key, value in [('request_sha256','a'*64),('operation',OP[:-1]+'1'),('context',{})]:
            changed = copy.deepcopy(self.payload); changed[key] = value
            with self.assertRaises(ScreeningRecoveryUnavailable): validate(changed)
        changed = copy.deepcopy(proof); changed['rows'][-1]['id'] = 'other-real-id'
        with self.assertRaises(ScreeningRecoveryUnavailable): validate_receipt(changed, self.payload)
        changed = copy.deepcopy(proof); changed['native_result']['response_id'] = 'synthetic-response'
        with self.assertRaises(ScreeningRecoveryUnavailable): validate_receipt(changed, self.payload)

    def test_enqueue_runs_recovery_on_existing_actor_and_releases_claim(self):
        caller = threading.get_ident(); actual_threads = []
        answer = self.raw._answer
        def observed(*args):
            actual_threads.append(threading.get_ident())
            return answer(*args)
        self.raw._answer = observed
        def actor_loop():
            task = self.actor.queue.get(timeout=2)
            try:
                self.assertEqual(task['kind'], KIND)
                task['result'] = recover(self.actor, self.delegated, task['request'])
            except BaseException as error: task['error'] = error
            finally:
                self.actor.claim.release(); task['done'].set(); self.actor.queue.task_done()
        thread = threading.Thread(target=actor_loop); thread.start()
        result = enqueue(self.actor, self.payload)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(set(result), {'operation','marker','request_sha256','output','assistant_text','response_id','chart_evidence'})
        self.assertEqual(result['operation'], OP)
        self.assertTrue(actual_threads and all(identifier != caller for identifier in actual_threads))
        self.assertFalse(self.actor.claim.locked())
        self.assertEqual((self.root / JOURNAL_NAME).read_bytes(), self.journal_before)

    def test_enqueue_busy_or_human_workflow_never_queues(self):
        self.actor.claim.acquire()
        with self.assertRaises(self.native.NotSubmitted): enqueue(self.actor, self.payload)
        self.assertTrue(self.actor.claim.locked()); self.actor.claim.release()
        for status in ('CHALLENGE_REQUIRED','LOGIN_REQUIRED','UNAVAILABLE'):
            self.actor.current_status = status
            with self.assertRaises(self.native.NotSubmitted): enqueue(self.actor, self.payload)
            self.assertTrue(self.actor.queue.empty()); self.assertFalse(self.actor.claim.locked())


if __name__ == '__main__': unittest.main()
