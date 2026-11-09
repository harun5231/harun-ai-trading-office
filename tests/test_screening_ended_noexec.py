"""Known received stale turns are retired without inventing output or resending."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent import native_received_screening as native
from agent import native_received_analysis as observer
from tests import test_generic_screening_response_recovery as fixture
from worker import screening_response_recovery as recovery
from worker.account_state import screening_contract
from worker.core import Review


ACTUAL = json.loads((Path(__file__).parent / 'fixtures/neurobro_screening_technical_followup_20261109.json').read_text())


def review_entry(payload, rows, question, observed, *, symbols=None):
    reply = rows[-1]['text']; transcript = recovery._digest(rows)
    rid = recovery._digest(dict(operation=payload['operation'], request_sha256=payload['request_sha256'],
        reply_sha256=recovery._sha(reply), phase='SCREENING', transcript_sha256=transcript))
    conversation = 'neurobro:' + payload['operation'][:10] + ':' + 'a' * 32
    request = dict(schema_version=1, request_id=rid, conversation_id=conversation, phase='SCREENING',
        reply=reply, reply_sha256=recovery._sha(reply), market_context=None,
        gate=dict(robot_on=True, native_dispatch_verified=True), deadline_epoch=observed + 90)
    response = dict(schema_version=1, ok=True, request_id=rid, conversation_id=conversation,
        phase='SCREENING', reply_sha256=request['reply_sha256'], authority='DIALOGUE_CONTROL_ONLY',
        trading_authority=False, decision=dict(action='CLARIFY', symbols=['ATOMUSDT', 'ONDOUSDT'] if symbols is None else symbols, question=question),
        needs_followup=True, reason='DIALOGUE_DECISION')
    value = dict(schema_version=1, kind='NATIVE_SCREENING_MINI_REVIEW_V1', operation=payload['operation'],
        request_sha256=payload['request_sha256'], payload_sha256=recovery._digest(payload),
        request_id=rid, reply_sha256=request['reply_sha256'], context_sha256=recovery._digest(payload['context']),
        conversation_id=conversation, phase='SCREENING', request=request, response=response,
        transcript_sha256=transcript, observed_at=observed,
        model_operation=dict(operation_id='delegated-model:' + rid, kind='DELEGATED_MODEL_DIALOGUE',
            request_hash=recovery._digest(request), state='DONE', result=recovery._wire(response, ascii=True),
            updated_at=observed))
    raw = recovery._wire(value, ascii=True)
    return dict(name='screening-review-' + rid + '.json', sha256=recovery._sha(raw), bytes=len(raw.encode()), value=value)


def failure(payload, url, *, phase='FOLLOWUP_SENT', at=fixture.NOW - 772, model=False,
        hook=native.RETURN_FAILURE_HOOK):
    files = {'hook': '/app/agent/native_browser_hook.py', 'dom': '/app/browser_runtime/dom.py',
        'client': '/app/agent/dialogue_client.py'}
    def error(kind, reason, stack):
        return dict(class_=kind, reason=reason, frames=[dict(file=files[file], function=fn, line=10 + index)
            for index, (file, fn) in enumerate(stack)], message_sha256=hashlib.sha256(b'').hexdigest(), message_characters=0)
    outer = error('OutcomeUnknown', None, [('hook', '_ask'), ('dom', 'ask')])
    inner = error('DialogueError' if model else 'OutcomeUnknown', 'DIALOGUE_MODEL_UNAVAILABLE' if model else None,
        [('dom', 'ask'), ('hook', 'delegated_answer'), ('hook', 'review')]
        + ([('client', 'request'), ('client', '_request')] if model else
            [('hook', 'gate'), ('hook', 'failed'), ('hook', 'unknown')]))
    for entry in (outer, inner): entry['class'] = entry.pop('class_')
    value = dict(schema_version=1, operation=payload['operation'], request_sha256=payload['request_sha256'],
        phase=phase, accepted_reply_sha256=None, followup_intent_sha256=[], clock=5002., deadline=6000.,
        at=at, chain=[outer, inner])
    if not model:
        value['gate_failure'] = dict(reason='DISPATCH_NOT_PERMITTED', clock=5001., deadline=6000., phase=phase,
            check_material=False, site_status='CONNECTED', dispatch=False,
            initial_url_sha256=recovery._sha(url), current_url_sha256=None)
    raw = recovery._wire(value, ascii=True)
    return dict(kind=native.RETURN_FAILURE_KIND, name='native-return-failure-' + payload['request_sha256'] + '.json',
        sha256=recovery._sha(raw), bytes=len(raw.encode()), producer_source_sha256=native.RETURN_FAILURE_PRODUCER,
        hook_source_sha256=hook, value=value)


class EndedScreeningTests(unittest.TestCase):
    def setUp(self):
        c = self.case = fixture.GenericScreeningResponseRecoveryTests()
        c.setUp(); self.addCleanup(c.doCleanups)
        self.operation = fixture.CYCLE + ':screening:2:2'
        _, schema = screening_contract(2)
        c.context = dict(requested_count=2, excluded_symbols=['HYPEUSDT'],
            replacement_instruction='Choose different Binance USD-M USDT perpetual coins that are not in excluded_symbols.')
        c.body, encoded, c.request_hash = c.source.request_body(self.operation, 'Pilih dua pengganti.', schema, c.context)
        c.payload = {**c.body, 'request_sha256': c.request_hash}
        c.catalog.update(ATOMUSDT={}, ONDOUSDT={})
        c.data = {**c.data, 'seen': [], 'screen': 1, 'replacements': 1, 'round_symbols': []}
        for table in ('api_requests', 'robot_jobs', 'research_browser_requests'):
            c.db.execute('UPDATE ' + table + ' SET operation=? WHERE operation=?', (self.operation, fixture.OP))
        c.db.execute('UPDATE api_requests SET body_hash=?,created=?', (c.request_hash, fixture.NOW - 1000))
        c.db.execute('UPDATE research_browser_requests SET request_sha256=?,marker=?,request_body=?',
            (c.request_hash, c.body['marker'], encoded))
        c.db.execute('UPDATE robot_cycles SET data=?', (json.dumps(c.data),))
        c.db.execute(recovery.RECEIVED_SCHEMA)
        self.envelope = c.fixture(operation=self.operation, reply=ACTUAL['author_replies'][0], approval=False)
        proof = self.envelope['observation']; rows = proof['rows']
        proof['review_receipts'] = [review_entry(c.payload, rows, ACTUAL['clarification'], fixture.NOW - 850)]
        rows += [dict(role='user', text=ACTUAL['clarification'], attachments=[]),
            dict(role='assistant', text=ACTUAL['author_replies'][1], id='actual-final', complete=True, attachments=[])]
        self.resize_scope()
        proof['screening_ended_return'] = failure(c.payload, proof['page_url'])
        self.seal()
        c.provider._record_browser_response = c.forbid

    @property
    def db(self): return self.case.provider.db

    def seal(self, envelope=None): return self.case.seal(envelope or self.envelope)

    def resize_scope(self):
        proof = self.envelope['observation']; count = len(proof['rows'])
        proof['operation_scope'].update(latest_user=count - 2, reply_row=count - 1,
            user_rows=list(range(0, count, 2)))
        proof['room_witness']['followup_user_rows'] = list(range(2, count, 2))

    def row(self, table):
        key, value = ('id', fixture.CYCLE) if table == 'robot_cycles' else ('operation', self.operation)
        row = self.db.execute('SELECT * FROM ' + table + ' WHERE ' + key + '=?', (value,)).fetchone()
        return dict(row) if row else None

    def recover(self, envelope=None):
        return recovery.recover_received_screening(self.case.provider, envelope or self.envelope,
            catalog=self.case.catalog, max_age=180, now=fixture.NOW)

    def finances(self):
        return {name: [tuple(row) for row in self.db.execute('SELECT * FROM ' + name)]
            for name in ('order_intents', 'pending_review', 'robot_candidates', 'robot_settings')}

    def assert_sealed(self):
        self.assertEqual(self.row('api_requests'), {**self.pre_api, 'state': recovery.RECEIVED_TERMINAL,
            'failure_code': recovery.RECEIVED_REASON, 'output': None})
        self.assertIsNone(self.row('research_browser_responses'))
        self.assertEqual(self.row('robot_jobs')['state'], 'COMPLETE')
        data = json.loads(self.row('robot_cycles')['data'])
        self.assertEqual(data, {**self.case.data, 'screen': 2, 'replacements': 2, 'replacement_due': 2})
        self.assertEqual(recovery.received_screening_replacement_due(self.db, fixture.CYCLE, data), 2)
        journal = self.row('screening_response_recoveries')
        self.assertEqual(journal['phase'], 'NOEXEC')
        proof = json.loads(journal['proof'])
        self.assertFalse(proof['retirement']['canonical_result'])
        self.assertEqual(proof['observation']['rows'], self.envelope['observation']['rows'])
        self.assertEqual(journal['created'], journal['updated'])

    def test_actual_technical_reply_is_preserved_without_a_hold_or_canonical_result(self):
        self.assertEqual([recovery._sha(text) for text in ACTUAL['author_replies']], ACTUAL['author_reply_sha256'])
        self.pre_api = self.row('api_requests'); finances = self.finances()
        self.assertEqual(self.recover(), 'NOEXEC'); self.assert_sealed()
        proof = json.loads(self.row('screening_response_recoveries')['proof'])
        self.assertEqual(proof['retirement']['disposition'], 'TECHNICAL_REPLY_RECEIVED')
        self.assertEqual(proof['observation']['review_receipts'][0]['value']['response']['decision']['action'], 'CLARIFY')
        self.assertEqual(self.finances(), finances)

    def test_current_hook_failure_retires_once_without_sending_or_changing_finances(self):
        self.envelope['observation']['screening_ended_return']['hook_source_sha256'] = native.CURRENT_RETURN_FAILURE_HOOK
        self.seal(); self.pre_api = self.row('api_requests'); finances = self.finances()
        self.assertEqual(self.recover(), 'NOEXEC'); self.assert_sealed()
        before = tuple(self.db.iterdump())
        self.assertEqual(self.recover(), 'NOEXEC')
        self.assertEqual(tuple(self.db.iterdump()), before); self.assertEqual(self.finances(), finances)

    def test_original_model_unknown_is_not_replayed_and_no_approval_is_invented(self):
        proof = self.envelope['observation']; proof['rows'] = proof['rows'][:2]
        proof['review_receipts'] = []; self.resize_scope()
        proof['screening_ended_return'] = failure(self.case.payload, proof['page_url'], phase='INITIAL', model=True)
        self.seal(); self.pre_api = self.row('api_requests')
        self.assertEqual(self.recover(), 'NOEXEC'); self.assert_sealed()
        self.assertEqual(json.loads(self.row('screening_response_recoveries')['proof'])['retirement']['disposition'], 'REPLY_RECEIVED')

    def test_six_owned_rows_use_each_actual_clarify_without_a_round_cap(self):
        proof = self.envelope['observation']; question = 'Apa bukti data yang benar-benar tersedia?'
        proof['review_receipts'].append(review_entry(self.case.payload, proof['rows'], question, fixture.NOW - 800))
        proof['rows'] += [dict(role='user', text=question, attachments=[]),
            dict(role='assistant', text='Data struktur belum tersedia; bukan keputusan HOLD pasar.',
                id='third-final', complete=True, attachments=[])]
        self.resize_scope(); self.seal(); self.reload_ids(); self.pre_api = self.row('api_requests')
        self.assertEqual(self.recover(), 'NOEXEC'); self.assert_sealed()

    def test_repeat_restart_and_later_cycle_progress_do_not_rewrite_the_seal(self):
        self.assertEqual(self.recover(), 'NOEXEC'); before = tuple(self.db.iterdump())
        resumed = sqlite3.connect(':memory:', isolation_level=None); resumed.row_factory = sqlite3.Row
        self.addCleanup(resumed.close); self.db.backup(resumed); self.case.provider.db = resumed
        self.assertEqual(self.recover(), 'NOEXEC'); self.assertEqual(tuple(self.db.iterdump()), before)
        data = json.loads(self.row('robot_cycles')['data']); data['screen'] = 3
        self.db.execute('UPDATE robot_cycles SET data=?', (json.dumps(data),))
        after_progress = tuple(self.db.iterdump()); self.assertEqual(self.recover(), 'NOEXEC')
        self.assertEqual(tuple(self.db.iterdump()), after_progress)
        self.assertEqual(recovery.received_screening_replacement_due(self.db, fixture.CYCLE, data), 0)

    def test_fresh_ended_reply_waits_without_approval_or_reinterpretation(self):
        self.db.execute('UPDATE api_requests SET created=?', (fixture.NOW - 90,))
        proof = self.envelope['observation']
        proof['review_receipts'] = [review_entry(self.case.payload, proof['rows'][:2], ACTUAL['clarification'], fixture.NOW - 40)]
        proof['screening_ended_return'] = failure(self.case.payload, proof['page_url'], at=fixture.NOW - 20)
        self.seal(); before = tuple(self.db.iterdump())
        with self.assertRaisesRegex(Review, 'SCREENING_ENDED_RESPONSE_AWAITING_EXPIRY'): self.recover()
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_missing_wrong_or_ambiguous_custody_stays_unresolved(self):
        for mutation in ('missing', 'operation', 'hash', 'publisher', 'hook', 'publisher_type', 'hook_type', 'phase', 'chain_send',
                'accepted', 'intent', 'question', 'unknown_review', 'extra_next_review', 'body', 'scope', 'incomplete'):
            with self.subTest(mutation=mutation):
                envelope = copy.deepcopy(self.envelope); proof = envelope['observation']; ended = proof['screening_ended_return']
                if mutation == 'missing': del proof['screening_ended_return']
                elif mutation == 'operation': ended['value']['operation'] += '-wrong'
                elif mutation == 'hash': ended['sha256'] = 'e' * 64
                elif mutation == 'publisher': ended['producer_source_sha256'] = 'e' * 64
                elif mutation == 'hook': ended['hook_source_sha256'] = 'e' * 64
                elif mutation == 'publisher_type': ended['producer_source_sha256'] = {}
                elif mutation == 'hook_type': ended['hook_source_sha256'] = []
                elif mutation == 'phase': ended['value']['phase'] = 'ACCEPT'
                elif mutation == 'chain_send':
                    ended['value']['chain'][1]['frames'].insert(3, dict(file='/app/agent/native_browser_hook.py', function='send_followup', line=500))
                elif mutation == 'accepted': ended['value']['accepted_reply_sha256'] = 'e' * 64
                elif mutation == 'intent': ended['value']['followup_intent_sha256'] = ['e' * 64]
                elif mutation == 'question': proof['rows'][2]['text'] += ' changed'
                elif mutation == 'unknown_review': proof['review_receipts'][0]['value']['model_operation']['state'] = 'UNKNOWN'
                elif mutation == 'extra_next_review': proof['review_receipts'].append(review_entry(self.case.payload, proof['rows'], 'Another Send?', fixture.NOW - 780))
                elif mutation == 'body': proof['rendered_prompt_receipt']['rendered_prompt'] += ' changed'
                elif mutation == 'scope': proof['operation_scope']['unmatched_intents'] = ['uncertain-send']
                elif mutation == 'incomplete': proof['rows'][-1]['complete'] = False
                if mutation in ('operation', 'phase', 'chain_send', 'accepted', 'intent'):
                    raw = recovery._wire(ended['value'], ascii=True); ended.update(sha256=recovery._sha(raw), bytes=len(raw.encode()))
                if mutation == 'unknown_review':
                    entry = proof['review_receipts'][0]; raw = recovery._wire(entry['value'], ascii=True)
                    entry.update(sha256=recovery._sha(raw), bytes=len(raw.encode()))
                self.seal(envelope); before = tuple(self.db.iterdump())
                with self.assertRaises(Review): self.recover(envelope)
                self.assertEqual(tuple(self.db.iterdump()), before)

    def test_accepted_intent_or_owner_pause_prevents_retirement(self):
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?)',
            ('unexpected', self.operation, 'ONDOUSDT', 'ENTRY_PENDING', 'existing accepted payload'))
        before = tuple(self.db.iterdump())
        with self.assertRaises(Review): self.recover()
        self.assertEqual(tuple(self.db.iterdump()), before)

        self.db.execute("DELETE FROM order_intents WHERE id='unexpected'")
        calls = []
        def allowed():
            calls.append(True); return len(calls) == 1
        before = tuple(self.db.iterdump())
        with self.assertRaisesRegex(Review, 'RESEARCH_PAUSED'):
            recovery.recover_received_screening(self.case.provider, self.envelope,
                catalog=self.case.catalog, max_age=180, now=fixture.NOW, permitted=allowed)
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_unrelated_partial_fill_and_its_accepted_payload_are_not_changed(self):
        self.db.execute("UPDATE order_intents SET state='PARTIALLY_FILLED' WHERE id='accepted-inj'")
        before=self.finances()
        self.assertEqual(self.recover(),'NOEXEC')
        self.assertEqual(self.finances(),before)
        self.assertEqual(self.db.execute("SELECT state,payload FROM order_intents WHERE id='accepted-inj'").fetchone()[:],
            ('PARTIALLY_FILLED','accepted immutable INJ order'))

    def test_interruption_before_commit_rolls_back_then_recovers_once(self):
        self.db.execute("CREATE TEMP TRIGGER interrupt_noexec BEFORE UPDATE ON robot_jobs BEGIN SELECT RAISE(ABORT,'controlled crash'); END")
        before = tuple(self.db.iterdump())
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'controlled crash'): self.recover()
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.db.execute('DROP TRIGGER interrupt_noexec')
        self.assertEqual(self.recover(), 'NOEXEC')
        self.assertEqual(self.db.execute('SELECT count(*) FROM screening_response_recoveries').fetchone()[0], 1)

    def test_new_wib_day_retires_only_the_received_research_without_touching_trade_history(self):
        current=datetime(2026,11,5,17,0,5,tzinfo=timezone.utc).timestamp()
        created=current-25;self.db.execute('UPDATE api_requests SET created=?',(created,))
        proof=self.envelope['observation'];proof.update(observed_at=current-10,finished_at=current-5)
        proof['review_receipts']=[review_entry(self.case.payload,proof['rows'][:2],ACTUAL['clarification'],current-20)]
        proof['screening_ended_return']=failure(self.case.payload,proof['page_url'],at=current-15)
        self.seal();before=self.finances()
        self.assertEqual(recovery.recover_received_screening(self.case.provider,self.envelope,
            catalog=self.case.catalog,max_age=180,now=current),'NOEXEC')
        self.assertEqual(self.row('api_requests')['created'],created)
        self.assertEqual(self.row('api_requests')['attempts'],1)
        self.assertEqual(self.row('robot_cycles')['day'],fixture.DAY)
        self.assertEqual(self.finances(),before)
        self.assertIsNone(self.row('research_browser_responses'))

    def test_interruption_after_commit_is_idempotent_on_restart(self):
        class AfterCommit(sqlite3.Connection):
            interrupt = True
            def execute(connection, sql, parameters=()):
                result = super().execute(sql, parameters)
                if sql == 'COMMIT' and connection.interrupt:
                    connection.interrupt = False; raise RuntimeError('controlled crash after commit')
                return result
        resumed = sqlite3.connect(':memory:', isolation_level=None, factory=AfterCommit); resumed.row_factory = sqlite3.Row
        self.addCleanup(resumed.close); self.db.backup(resumed); self.case.provider.db = resumed
        with self.assertRaisesRegex(RuntimeError, 'controlled crash after commit'): self.recover()
        before = tuple(self.db.iterdump()); self.assertEqual(self.recover(), 'NOEXEC')
        self.assertEqual(tuple(self.db.iterdump()), before)

    def reload_ids(self):
        source = copy.deepcopy(self.envelope['observation'])
        for index, row in enumerate(self.envelope['observation']['rows']):
            if index % 2: row['id'] = 'reloaded-' + str(index)
        current = self.envelope['observation']; current.update(observed_at=fixture.NOW-6, finished_at=fixture.NOW-5)
        raw = recovery._wire(source)
        key = recovery._sha(recovery._wire({name: self.case.payload[name] for name in ('operation', 'request_sha256')}))[:16]
        current['screening_review_identity'] = dict(kind=native.REVIEW_IDENTITY_KIND,
            source_capture_name='received-analysis-observation-' + key + '-' + 'b' * 64 + '.json',
            source_capture_sha256='b'*64, source_capture_bytes=10000,
            source_observation=source, source_observation_sha256=recovery._sha(raw),
            source_observation_bytes=len(raw.encode()), changed_assistant_rows=list(range(1,len(current['rows']),2)),
            ended_return_sha256=current['screening_ended_return']['sha256'])
        self.seal()

    def test_reloaded_uuids_use_exact_old_native_rows_without_aliasing_the_model_id(self):
        original_rid = self.envelope['observation']['review_receipts'][0]['value']['request_id']
        self.reload_ids(); self.pre_api = self.row('api_requests')
        self.assertEqual(self.recover(), 'NOEXEC'); self.assert_sealed()
        retained = json.loads(self.row('screening_response_recoveries')['proof'])['observation']
        self.assertEqual(retained['rows'][1]['id'], 'reloaded-1')
        self.assertNotEqual(retained['screening_review_identity']['source_observation']['rows'][1]['id'], 'reloaded-1')
        self.assertEqual(retained['review_receipts'][0]['value']['request_id'], original_rid)
        self.assertEqual(retained['screening_review_identity']['source_observation']['review_receipts'], retained['review_receipts'])

    def test_uuid_witness_rejects_changed_text_room_review_or_wrong_source_hash(self):
        self.reload_ids()
        for mutation in ('missing', 'text', 'room', 'review', 'hash', 'capture', 'extra_user'):
            with self.subTest(mutation=mutation):
                envelope = copy.deepcopy(self.envelope); proof = envelope['observation']
                identity = proof['screening_review_identity']; source = identity['source_observation']
                if mutation == 'missing': del proof['screening_review_identity']
                elif mutation == 'text': source['rows'][-1]['text'] += ' changed'
                elif mutation == 'room': source['room']['room_id'] = 'c' * 32
                elif mutation == 'review': source['review_receipts'] = []
                elif mutation == 'hash': identity['source_observation_sha256'] = 'c' * 64
                elif mutation == 'capture': identity['source_capture_name'] = 'foreign.json'
                else: proof['rows'].insert(2, dict(role='user', text='foreign turn', attachments=[]))
                if mutation in ('text', 'room', 'review'):
                    raw = recovery._wire(source); identity.update(source_observation_sha256=recovery._sha(raw), source_observation_bytes=len(raw.encode()))
                self.seal(envelope); before = tuple(self.db.iterdump())
                with self.assertRaises(Review): self.recover(envelope)
                self.assertEqual(tuple(self.db.iterdump()), before)


class PassiveEndedCaptureTests(unittest.TestCase):
    def test_first_failure_reader_is_private_bounded_and_does_not_write(self):
        payload = dict(operation='2026-11-05:robot-v9:0:screening:2:2', request_sha256='a' * 64)
        record = failure(payload, 'https://app.neurobro.ai/chat/' + 'a'*32,
            hook=native.CURRENT_RETURN_FAILURE_HOOK)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); name = record['name']; path = root/name
            self.assertIsNone(native.read_ended_return(root,payload))
            raw = recovery._wire(record['value'], ascii=True).encode()
            path.write_bytes(raw); path.chmod(0o600)
            before = path.stat(); result = native.read_ended_return(root,payload)
            self.assertEqual(result,record)
            after = path.stat(); self.assertEqual((before.st_ino,before.st_mtime_ns,before.st_size), (after.st_ino,after.st_mtime_ns,after.st_size))
            path.chmod(0o644)
            with self.assertRaises(ValueError): native.read_ended_return(root,payload)
            path.chmod(0o600); hardlink=root/'another'; hardlink.hardlink_to(path)
            with self.assertRaises(ValueError): native.read_ended_return(root,payload)
            hardlink.unlink(); path.unlink(); path.symlink_to(root/'absent')
            with self.assertRaises(OSError): native.read_ended_return(root,payload)

    def test_failed_capture_binding_or_changed_native_producer_is_refused(self):
        payload = dict(operation='2026-11-05:robot-v9:0:screening:2:2', request_sha256='a'*64)
        record = failure(payload,'https://app.neurobro.ai/chat/'+'a'*32)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/record['name']
            changed=copy.deepcopy(record['value']);changed['operation']+='-wrong'
            path.write_text(recovery._wire(changed,ascii=True));path.chmod(0o600)
            with self.assertRaisesRegex(ValueError,'SCREENING_RETURN_FAILURE_BINDING_UNVERIFIED'):
                native.read_ended_return(root,payload)
            path.write_text(recovery._wire(record['value'],ascii=True))
            with patch.object(native,'RETURN_FAILURE_SOURCES',frozenset()),self.assertRaisesRegex(ValueError,'SCREENING_RETURN_FAILURE_PRODUCER_CHANGED'):
                native.read_ended_return(root,payload)

    def test_only_exact_historical_and_current_source_pairs_are_read_without_rewriting(self):
        self.assertEqual(native.RETURN_FAILURE_SOURCES, recovery.ENDED_RETURN_SOURCES)
        self.assertEqual(native.RETURN_FAILURE_SOURCES, frozenset((
            (native.RETURN_FAILURE_PRODUCER, native.RETURN_FAILURE_HOOK),
            (native.RETURN_FAILURE_PRODUCER, native.PREVIOUS_RETURN_FAILURE_HOOK),
            (native.RETURN_FAILURE_PRODUCER, native.CURRENT_RETURN_FAILURE_HOOK))))
        source = Path(native.__file__).resolve().parent
        producer_bytes = (source/'native_turn_batch.py').read_bytes()
        hook_bytes = (source/'native_browser_hook.py').read_bytes(); original_sha256 = hashlib.sha256
        payload = dict(operation='2026-11-05:robot-v9:0:screening:2:2', request_sha256='a'*64)
        record = failure(payload, 'https://app.neurobro.ai/chat/'+'a'*32)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); path=root/record['name']
            path.write_text(recovery._wire(record['value'],ascii=True)); path.chmod(0o600)
            before=path.read_bytes(); stamp=path.stat()
            for producer,hook in (*sorted(native.RETURN_FAILURE_SOURCES),
                    ('e'*64,native.CURRENT_RETURN_FAILURE_HOOK),(native.RETURN_FAILURE_PRODUCER,'e'*64)):
                def source_digest(value):
                    if value==producer_bytes:return Mock(hexdigest=lambda:producer)
                    if value==hook_bytes:return Mock(hexdigest=lambda:hook)
                    return original_sha256(value)
                with self.subTest(producer=producer,hook=hook),patch.object(native.hashlib,'sha256',side_effect=source_digest):
                    if (producer,hook)in native.RETURN_FAILURE_SOURCES:
                        result=native.read_ended_return(root,payload)
                        self.assertEqual((result['producer_source_sha256'],result['hook_source_sha256']),(producer,hook))
                        self.assertEqual(result['value'],record['value'])
                    else:
                        with self.assertRaisesRegex(ValueError,'SCREENING_RETURN_FAILURE_PRODUCER_CHANGED'):
                            native.read_ended_return(root,payload)
                self.assertEqual(path.read_bytes(),before)
                after=path.stat();self.assertEqual((after.st_ino,after.st_mtime_ns,after.st_size),
                    (stamp.st_ino,stamp.st_mtime_ns,stamp.st_size))

    def test_actual_passive_observer_recovers_reload_ids_from_existing_capture_only(self):
        from tests.test_received_screening_rounds import ReceivedScreeningRoundsTests
        from agent.native_analysis_receipt import prefix
        from agent.native_daily_room import retain_prefix_snapshot
        case=ReceivedScreeningRoundsTests()
        case.setUp();self.addCleanup(case.doCleanups)
        c=case.case; initial=case.invoke()['observation']['rows']; question='Tegaskan coin pilihan yang dimaksud.'
        entry=review_entry(case.payload,initial,question,time.time()-10,symbols=[])
        root=c.room.store.root;path=root/(prefix(case.payload)+entry['value']['request_id']+'.json')
        path.write_text(recovery._wire(entry['value'],ascii=True));path.chmod(0o600)
        c.raw.messages += [dict(role='user',text=question,attachments=[],display_names=[]),
            dict(role='assistant',text='Data struktur belum tersedia, bukan HOLD pasar.',complete=True,id='original-last')]
        case.anchor()
        ended=failure(case.payload,c.raw.page.url,at=time.time()-5)
        path=root/ended['name'];path.write_text(recovery._wire(ended['value'],ascii=True));path.chmod(0o600)
        original=case.invoke()
        self.assertTrue(original['observation']['operation_scope']['verified'])
        # The production DailyRoom checkpoint retains this native prefix. The
        # legacy fake's anchor() helper writes only its SQL row, so preserve the
        # real existing snapshot too before simulating a browser restart.
        retain_prefix_snapshot(root,case.payload['operation'][:10],'a'*32,c.raw._turns())
        original_ids=[row.get('id')for row in original['observation']['rows']]
        for index,row in enumerate(c.raw.messages):
            if row.get('role')=='assistant':row['id']='reloaded-native-'+str(index)
        unchanged_messages=copy.deepcopy(c.raw.messages)
        current=case.invoke();proof=current['observation']
        self.assertEqual(c.raw.messages,unchanged_messages)
        self.assertTrue(proof['operation_scope']['verified'])
        self.assertEqual(len(proof['rows']),4)
        self.assertNotEqual([row.get('id')for row in proof['rows']],original_ids)
        witness=proof['screening_review_identity']
        self.assertEqual([row.get('id')for row in witness['source_observation']['rows']],original_ids)
        self.assertEqual(witness['source_observation']['review_receipts'],proof['review_receipts'])
        # Exercise the same worker-side proof verifier against the real passive
        # actor output, without a provider, model, ledger or native response.
        recovery._validate_ended_screening(current,case.payload,time.time()-1000,
            max_age=180,now=time.time()+1)
        # Same room/IDs are insufficient when text, review custody or the
        # original private capture changes. None of these can be rebound.
        c.raw.messages[-1]['text']+=' changed'
        denied=case.invoke()['observation']
        self.assertFalse(denied['operation_scope']['verified'])
        self.assertNotIn('screening_review_identity',denied)
        c.raw.messages=copy.deepcopy(unchanged_messages)
        changed=copy.deepcopy(entry['value']);changed['response']['decision']['question']+=' changed'
        path=root/(prefix(case.payload)+entry['value']['request_id']+'.json')
        path.write_text(recovery._wire(changed,ascii=True))
        denied=case.invoke()['observation']
        self.assertFalse(denied['operation_scope']['verified'])
        self.assertNotIn('screening_review_identity',denied)
        path.write_text(recovery._wire(entry['value'],ascii=True))
        source=root/original['capture_name'];source.write_bytes(source.read_bytes()+b' ')
        denied=case.invoke()['observation']
        self.assertFalse(denied['operation_scope']['verified'])
        self.assertTrue(any(error.get('reason')=='SCREENING_REVIEW_IDENTITY_SOURCE_HASH_CHANGED'
            for error in denied['errors']))


if __name__ == '__main__': unittest.main()
