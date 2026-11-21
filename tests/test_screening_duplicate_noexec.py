"""Retire an acknowledged, expired screening without selecting a duplicate.

Native text, original request and archived review identity come from the sealed
production capsule. Receipt envelopes and clocks below are offline fixtures;
every provider, upload, model and order method is forbidden.
"""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import unittest

from agent import native_received_analysis as observation
from agent.native_daily_room import identities
from tests import test_generic_screening_response_recovery as generic
from worker import screening_response_recovery as recovery
from worker.core import Review


FIXTURE = Path(__file__).parent / 'fixtures/neurobro_duplicate_screening_20261117.json'
NOW = datetime(2026, 11, 17, 14, 8, tzinfo=timezone.utc).timestamp()


class DuplicateScreeningNoexecTests(unittest.TestCase):
    def setUp(self):
        self.original = json.loads(FIXTURE.read_text())
        self.operation = self.original['operation']
        self.cycle = self.operation.rsplit(':screening:', 1)[0]
        self.case = generic.GenericScreeningResponseRecoveryTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        c = self.case
        request = self.original['request_body']
        c.context = None
        c.body, serialized, c.request_hash = c.source.request_body(self.operation,
            request['prompt'], request['output_schema'], None)
        self.assertEqual(c.body, request)
        self.assertEqual(c.request_hash, self.original['request_sha256'])
        c.payload = {**c.body, 'request_sha256': c.request_hash}
        self.created = NOW - 1000
        c.data = dict(target=2, queue=[], seen=[], screen=-1, replacements=0, round_symbols=[])
        c.db.execute('UPDATE robot_cycles SET id=?,day=?,data=? WHERE id=?',
            (self.cycle, self.operation[:10], json.dumps(c.data), generic.CYCLE))
        c.db.execute('UPDATE api_requests SET operation=?,body_hash=?,created=? WHERE operation=?',
            (self.operation, c.request_hash, self.created, generic.OP))
        c.db.execute('UPDATE robot_jobs SET operation=?,cycle=? WHERE operation=?',
            (self.operation, self.cycle, generic.OP))
        c.db.execute('UPDATE research_browser_requests SET operation=?,request_sha256=?,marker=?,request_body=? '
            'WHERE operation=?', (self.operation, c.request_hash, c.body['marker'], serialized, generic.OP))
        c.db.execute(recovery.RECEIVED_SCHEMA)
        c.provider._record_browser_response = c.forbid
        c.catalog.update(TONUSDT={}, AAVEUSDT={}, INJUSDT={})
        self.envelope = self.make_envelope()

    @property
    def db(self):
        return self.case.provider.db

    def row(self, table):
        key = 'id' if table == 'robot_cycles' else 'operation'
        argument = self.cycle if table == 'robot_cycles' else self.operation
        row = self.db.execute('SELECT * FROM ' + table + ' WHERE ' + key + '=?', (argument,)).fetchone()
        return dict(row) if row else None

    def seal_review(self, proof):
        for entry in proof['review_receipts']:
            raw = recovery._wire(entry['value'], ascii=True)
            entry.update(sha256=recovery._sha(raw), bytes=len(raw.encode()))

    def make_envelope(self):
        c = self.case
        envelope = c.fixture(operation=self.operation, reply=self.original['rows'][1]['text'],
            symbols=['TONUSDT', 'AAVEUSDT'])
        proof = envelope['observation']
        live = copy.deepcopy(self.original['rows'])
        source = copy.deepcopy(live[:2])
        original_review = self.original['review']
        source[1]['id'] = original_review['conversation_context']['turns'][1]['id']
        # Prove which pair was actually read. Reloaded UI IDs remain distinct
        # from the immutable original Mini request; no new review is needed.
        self.assertEqual(recovery._digest(source), original_review['source_identity']['transcript_sha256'])
        self.assertEqual(identities(source), self.original['room']['anchors'])
        self.assertNotEqual(source[1]['id'], live[1]['id'])
        room = copy.deepcopy(self.original['room'])
        room.update(url=self.original['room_url'], updated_at=self.created + 95,
            history_titles=['HARUN ' + self.operation[:10]], identifier_reconciliation=None)
        proof.update(rows=live, room=room, page_url=self.original['page_url'],
            observed_at=NOW-11, finished_at=NOW-10, row_offset=None,
            rendered_prompt_receipt=copy.deepcopy(self.original['cached_rendered_prompt']),
            room_initial_state='BOUND', history_match_state='BOUND',
            history_restore_attempted=False, history_restored=False,
            room_witness=dict(native_room_matches=True, same_loaded_room_verified=False,
                journal_url_matches=True, initial_screening_user_rows=[0, 2], followup_user_rows=[]),
            operation_scope=dict(verified=False, initial_user=None, latest_user=None,
                user_rows=[], reply_row=None, reply_complete=False, unmatched_intents=[],
                reason='INITIAL_PROMPT_NOT_UNIQUE'))
        value = proof['review_receipts'][0]['value']
        source_identity = original_review['source_identity']
        value.update({k: v for k, v in source_identity.items() if k != 'observed_at'})
        value['observed_at'] = self.created + 91
        request = value['request']
        request.update(original_review['request_identity'])
        request.update(reply=source[1]['text'], deadline_epoch=self.created+150,
            conversation_context=dict(native_operation=self.operation,
                native_request_sha256=c.request_hash, turns=copy.deepcopy(source)))
        response = copy.deepcopy(original_review['response'])
        value['response'] = response
        done = copy.deepcopy(original_review['model_operation'])
        done.update(result=recovery._wire(response, ascii=True), updated_at=self.created+90)
        self.assertEqual(recovery._sha(done['result']), original_review['model_result_sha256'])
        value['model_operation'] = done
        proof['review_receipts'][0]['name'] = ('screening-review-' + recovery._digest(dict(
            operation=self.operation, request_sha256=c.request_hash)) + '-' + value['request_id'] + '.json')
        self.seal_review(proof)
        projected = observation._project_operation(proof)
        envelope['observation'] = projected
        key = recovery._digest(dict(operation=self.operation, request_sha256=c.request_hash))[:16]
        envelope['capture_name'] = ('received-analysis-observation-' + key + '-'
            + envelope['capture_sha256'] + '.json')
        return c.seal(envelope)

    def recover(self, envelope=None, *, now=NOW, permitted=lambda: True):
        return recovery.recover_received_screening(self.case.provider,
            self.envelope if envelope is None else envelope, catalog=self.case.catalog,
            max_age=180, now=now, permitted=permitted)

    def changed(self, mutate):
        value = copy.deepcopy(self.envelope)
        mutate(value['observation'])
        self.seal_review(value['observation'])
        return self.case.seal(value)

    def assert_refused_without_progress(self, envelope):
        before = tuple(self.db.iterdump())
        with self.assertRaises(Review):
            self.recover(envelope)
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')

    def test_projection_preserves_all_four_rows_without_claiming_unique_scope(self):
        proof = self.envelope['observation']
        self.assertEqual(proof['rows'], [])
        self.assertEqual(proof['duplicate_screening_rows'], self.original['rows'])
        self.assertFalse(proof['operation_scope']['verified'])
        self.assertEqual(proof['room_witness']['initial_screening_user_rows'], [0, 2])
        self.assertEqual(proof['duplicate_screening_rows'][1]['text'], self.original['rows'][1]['text'])
        self.assertNotEqual(proof['duplicate_screening_rows'][1]['text'], proof['duplicate_screening_rows'][3]['text'])

    def test_original_reviewed_prefix_retires_stale_without_recording_a_canonical_response(self):
        finances = self.case.finances()
        request_before = self.row('research_browser_requests')
        original_mini = copy.deepcopy(self.envelope['observation']['review_receipts'])
        self.assertEqual(self.recover(), 'NOEXEC')
        api = self.row('api_requests')
        self.assertEqual((api['state'], api['failure_code'], api['output'], api['attempts'], api['created']),
            (recovery.RECEIVED_TERMINAL, recovery.RECEIVED_REASON, None, 1, self.created))
        self.assertIsNone(self.row('research_browser_responses'))
        self.assertEqual(self.row('robot_jobs')['state'], 'COMPLETE')
        self.assertEqual(self.row('research_browser_requests'), request_before)
        self.assertEqual(self.case.finances(), finances)
        journal = self.row('screening_response_recoveries')
        self.assertEqual(journal['phase'], 'NOEXEC')
        sealed = json.loads(journal['proof'])
        self.assertEqual(sealed['retirement']['kind'], recovery.DUPLICATE_NOEXEC)
        self.assertEqual(sealed['observation']['duplicate_screening_rows'], self.original['rows'])
        self.assertEqual(sealed['observation']['review_receipts'], original_mini)
        data = json.loads(self.row('robot_cycles')['data'])
        self.assertEqual(data, {**self.case.data, 'screen': 0, 'replacement_due': 2})
        self.assertEqual(data['seen'], [])  # A technical duplicate is not a market HOLD blacklist.
        self.assertEqual(recovery.received_screening_replacement_due(self.db, self.cycle, data), 2)

    def test_restart_reuses_same_noexec_seal_without_new_writes(self):
        self.assertEqual(self.recover(), 'NOEXEC')
        resumed = sqlite3.connect(':memory:', isolation_level=None)
        resumed.row_factory = sqlite3.Row
        self.addCleanup(resumed.close)
        self.db.backup(resumed)
        self.case.provider.db = resumed
        before = tuple(self.db.iterdump())
        self.assertEqual(self.recover(), 'NOEXEC')
        self.assertEqual(tuple(self.db.iterdump()), before)
        data = json.loads(self.row('robot_cycles')['data'])
        self.assertEqual(recovery.received_screening_replacement_due(self.db, self.cycle, data), 2)
        self.assertEqual(recovery.received_screening_replacement_due(self.db, self.cycle, {**data, 'screen': 1}), 0)

    def test_fresh_received_duplicate_cannot_be_used_for_noexec_or_selection(self):
        self.db.execute('UPDATE api_requests SET created=? WHERE operation=?', (NOW-90, self.operation))
        def reclock(proof):
            proof['room']['updated_at'] = NOW-20
            value = proof['review_receipts'][0]['value']
            value['observed_at'] = NOW-15
            value['model_operation']['updated_at'] = NOW-16
            value['request']['deadline_epoch'] = NOW+50
        self.assert_refused_without_progress(self.changed(reclock))

    def test_missing_or_unknown_mini_never_acquires_recovery_authority(self):
        for mode in ('missing', 'unknown', 'nonterminal'):
            with self.subTest(mode=mode):
                def mutate(proof):
                    if mode == 'missing': proof['review_receipts'] = []
                    elif mode == 'unknown':
                        proof['review_receipts'][0]['value']['model_operation'].update(state='UNKNOWN', result=None)
                    else:
                        value = proof['review_receipts'][0]['value']
                        value['response']['decision'] = dict(action='CLARIFY', symbols=[], question='Pilihan yang mana?')
                        value['response']['needs_followup'] = True
                        value['model_operation']['result'] = recovery._wire(value['response'], ascii=True)
                self.assert_refused_without_progress(self.changed(mutate))

    def test_an_additional_unknown_tail_review_cannot_be_ignored(self):
        def mutate(proof):
            extra = copy.deepcopy(proof['review_receipts'][0])
            value = extra['value']
            turns = copy.deepcopy(proof['duplicate_screening_rows'][2:])
            reply_sha = recovery._sha(turns[-1]['text'])
            transcript = recovery._digest(turns)
            request_id = recovery._digest(dict(operation=self.operation,
                request_sha256=self.case.request_hash, reply_sha256=reply_sha,
                phase='SCREENING', transcript_sha256=transcript))
            value.update(request_id=request_id, reply_sha256=reply_sha, transcript_sha256=transcript)
            value['request'].update(request_id=request_id, reply_sha256=reply_sha,
                reply=turns[-1]['text'], conversation_context=dict(native_operation=self.operation,
                    native_request_sha256=self.case.request_hash, turns=turns))
            value['response'].update(request_id=request_id, reply_sha256=reply_sha)
            value['model_operation'].update(operation_id='delegated-model:'+request_id,
                request_hash='1'*64, state='UNKNOWN', result=None)
            extra['name'] = ('screening-review-' + recovery._digest(dict(operation=self.operation,
                request_sha256=self.case.request_hash)) + '-' + request_id + '.json')
            proof['review_receipts'].append(extra)
        self.assert_refused_without_progress(self.changed(mutate))

    def test_a_retained_followup_send_failure_cannot_be_ignored(self):
        for field, value in (
                ('screening_ended_return', dict(kind='NATIVE_SCREENING_RETURN_FAILURE_V1',
                    value=dict(phase='FOLLOWUP_SENT', accepted_reply_sha256=None))),
                ('screening_ended_return_error', dict(exception_class='ValueError',
                    reason='SCREENING_RETURN_FAILURE_UNVERIFIED')),
                ('screening_review_identity', dict(kind='NATIVE_SCREENING_RECEIVED_REVIEW_IDENTITIES_V1'))):
            with self.subTest(field=field):
                self.assert_refused_without_progress(self.changed(lambda proof: proof.update({field: value})))

    def test_fresh_observation_after_four_days_retires_without_resurrecting_old_choices(self):
        later = NOW + 4 * 86400
        envelope = self.changed(lambda proof: proof.update(observed_at=later-11, finished_at=later-10))
        finances = self.case.finances()
        self.assertEqual(self.recover(envelope, now=later), 'NOEXEC')
        self.assertIsNone(self.row('api_requests')['output'])
        self.assertIsNone(self.row('research_browser_responses'))
        self.assertEqual(self.case.finances(), finances)

    def test_wib_rollover_retires_only_research_without_restoring_a_used_entry(self):
        boundary = datetime(2026, 11, 17, 17, tzinfo=timezone.utc).timestamp()
        created = boundary - 30
        self.db.execute('UPDATE api_requests SET created=? WHERE operation=?', (created, self.operation))
        def reclock(proof):
            proof.update(observed_at=boundary+5, finished_at=boundary+6)
            proof['room']['updated_at'] = created+12
            value = proof['review_receipts'][0]['value']
            value['observed_at'] = created+11
            value['model_operation']['updated_at'] = created+10
            value['request']['deadline_epoch'] = created+90
        envelope = self.changed(reclock)
        finances = self.case.finances()
        self.assertLess(boundary+7-created, 180)
        self.assertEqual(self.recover(envelope, now=boundary+7), 'NOEXEC')
        api = self.row('api_requests')
        self.assertEqual((api['attempts'], api['created'], api['output']), (1, created, None))
        self.assertEqual(self.case.finances(), finances)

    def test_tampered_ownership_or_tail_is_not_hidden_by_valid_outer_hash(self):
        def tail_prompt(p): p['duplicate_screening_rows'][2]['text'] += '\nPertanyaan lain.'
        def marker(p):
            for i in (0, 2): p['duplicate_screening_rows'][i]['text'] = p['duplicate_screening_rows'][i]['text'].replace(
                self.case.body['marker'], 'HARUN_'+'f'*32)
        def anchors(p): p['room']['anchors'][1] = 'f'*64
        def repeat_id(p): p['duplicate_screening_rows'][3]['id'] = p['duplicate_screening_rows'][1]['id']
        def ambiguous_same_reply(p): p['duplicate_screening_rows'][3]['text'] = p['duplicate_screening_rows'][1]['text']
        def source_reply(p): p['review_receipts'][0]['value']['request']['conversation_context']['turns'][1]['text'] += ' revisi'
        def stale_capture(p): p.update(observed_at=NOW-200, finished_at=NOW-190)
        def extra_pair(p): p['duplicate_screening_rows'] += copy.deepcopy(p['duplicate_screening_rows'][:2])
        for mutate in (tail_prompt, marker, anchors, repeat_id, ambiguous_same_reply,
                source_reply, stale_capture, extra_pair):
            with self.subTest(case=mutate.__name__):
                self.assert_refused_without_progress(self.changed(mutate))

    def test_reordered_pairs_cannot_select_a_reply_merely_by_mini_text_match(self):
        self.assert_refused_without_progress(self.changed(lambda p: p.update(
            duplicate_screening_rows=p['duplicate_screening_rows'][2:] + p['duplicate_screening_rows'][:2])))

    def test_native_unique_prompt_path_remains_unchanged(self):
        raw = copy.deepcopy(self.envelope['observation'])
        raw['rows'] = copy.deepcopy(self.original['rows'][:2])
        raw.pop('duplicate_screening_rows', None)
        raw['row_offset'] = 0
        raw['room_witness'].update(initial_screening_user_rows=[0], same_loaded_room_verified=True)
        raw['operation_scope'].update(verified=True, initial_user=0, latest_user=0,
            user_rows=[0], reply_row=1, reply_complete=True, reason=None)
        projected = observation._project_operation(raw)
        self.assertEqual(projected['rows'], raw['rows'])
        self.assertNotIn('duplicate_screening_rows', projected)

    def test_permitted_gate_rollback_leaves_original_request_and_finances_intact(self):
        calls = []
        def permitted():
            calls.append(True)
            return len(calls) == 1
        before = tuple(self.db.iterdump())
        with self.assertRaises(Review):
            self.recover(permitted=permitted)
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')

    def test_an_existing_order_intent_prevents_nonexecution_retirement(self):
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?)',
            ('existing-owned-intent', self.operation, 'TONUSDT', 'ACCEPTED', 'accepted immutable order'))
        self.assert_refused_without_progress(self.envelope)


if __name__ == '__main__':
    unittest.main()
