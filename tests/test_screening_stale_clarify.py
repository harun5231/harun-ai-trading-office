"""Stale receipt-only recovery needs a real review-only NO-SEND return."""
import copy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import test_generic_screening_response_recovery as fixture
from worker import screening_response_recovery as recovery
from worker.account_state import screening_contract
from worker.core import Review


class StaleScreeningClarifyTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.GenericScreeningResponseRecoveryTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        c = self.case
        self.operation = fixture.CYCLE + ':screening:0:2'
        prompt, schema = screening_contract(2)
        c.context = None
        c.body, serialized, c.request_hash = c.source.request_body(self.operation, prompt, schema, None)
        c.payload = {**c.body, 'request_sha256': c.request_hash}
        c.catalog.update(DOTUSDT={}, LINKUSDT={})
        c.data = dict(target=2, queue=[], seen=[], screen=-1, replacements=0, round_symbols=[])
        c.db.execute('UPDATE api_requests SET operation=?,body_hash=?,created=? WHERE operation=?',
            (self.operation, c.request_hash, fixture.NOW-1000, fixture.OP))
        c.db.execute('UPDATE robot_jobs SET operation=? WHERE operation=?', (self.operation, fixture.OP))
        c.db.execute('UPDATE research_browser_requests SET operation=?,request_sha256=?,marker=?,request_body=? '
            'WHERE operation=?', (self.operation, c.request_hash, c.body['marker'], serialized, fixture.OP))
        c.db.execute('UPDATE robot_cycles SET data=?', (json.dumps(c.data),))
        c.db.execute(recovery.RECEIVED_SCHEMA)
        c.db.execute(recovery.REVIEW_RETURN_SCHEMA)
        self.reply = (Path(__file__).parent / 'fixtures/neurobro_screening_pick_summary_20261109.txt').read_text()
        self.envelope = self.make_clarification()

    @property
    def db(self):
        return self.case.provider.db

    def make_clarification(self, *, approval=True, followup=False):
        c = self.case
        envelope = c.fixture(operation=self.operation, reply=self.reply,
            approval=approval, followup=followup, symbols=['DOTUSDT', 'LINKUSDT'])
        if approval:
            value = envelope['observation']['review_receipts'][0]['value']
            response = value['response']
            response['decision'] = dict(action='CLARIFY', symbols=[],
                question='Mohon tegaskan dua coin pilihan akhir, tanpa skenario bersyarat.')
            response['needs_followup'] = True
            value['model_operation']['result'] = recovery._wire(response, ascii=True)
            self.seal_review(envelope)
        return c.seal(envelope)

    def seal_review(self, envelope):
        for entry in envelope['observation']['review_receipts']:
            raw = recovery._wire(entry['value'], ascii=True)
            entry.update(sha256=recovery._sha(raw), bytes=len(raw.encode()))
        return self.case.seal(envelope)

    def row(self, table):
        key = 'id' if table == 'robot_cycles' else 'operation'
        value = fixture.CYCLE if table == 'robot_cycles' else self.operation
        return dict(self.db.execute('SELECT * FROM ' + table + ' WHERE ' + key + '=?', (value,)).fetchone())

    def recover(self, envelope=None, *, now=fixture.NOW):
        return recovery.recover_received_screening(self.case.provider, envelope or self.envelope,
            catalog=self.case.catalog, max_age=180, now=now)

    def retain_positive_return(self, envelope=None):
        source = copy.deepcopy(envelope or self.envelope)
        returned = dict(kind=recovery.REVIEW_RETURN_KIND, operation=self.operation,
            request_sha256=self.case.request_hash, operation_completed=False,
            review_receipts=copy.deepcopy(source['observation']['review_receipts']))
        recovery.retain_screening_review_return(self.case.provider, source, returned,
            catalog=self.case.catalog, max_age=180, now=fixture.NOW-8)
        current = copy.deepcopy(source)
        current['observation'].update(observed_at=fixture.NOW-6, finished_at=fixture.NOW-5)
        self.envelope = self.case.seal(current)
        return source, returned

    def restart(self):
        resumed = sqlite3.connect(':memory:', isolation_level=None)
        resumed.row_factory = sqlite3.Row
        self.addCleanup(resumed.close)
        self.db.backup(resumed)
        self.case.provider.db = resumed
        self.case.provider._record_browser_response = self.case.forbid

    def test_normal_clarify_done_alone_cannot_retire_an_uncertain_followup_send(self):
        before = tuple(self.db.iterdump())
        with self.assertRaisesRegex(Review, 'SCREENING_STALE_REVIEW_RETURN_NOT_RETAINED'):
            self.recover()
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')
        self.assertEqual(self.db.execute('SELECT count(*) FROM screening_review_returns').fetchone()[0], 0)

    def test_positive_review_only_return_allows_stale_noexec_without_changing_clarify(self):
        source, returned = self.retain_positive_return()
        finances = self.case.finances()
        self.assertEqual(self.recover(), 'NOEXEC')
        api = self.row('api_requests')
        self.assertEqual((api['operation'], api['state'], api['output'], api['attempts'], api['created']),
            (self.operation, 'RECEIVED_STALE_NOT_EXECUTED', None, 1, fixture.NOW-1000))
        self.assertEqual(self.row('robot_jobs')['state'], 'COMPLETE')
        data = json.loads(self.row('robot_cycles')['data'])
        self.assertEqual(data, {**self.case.data, 'screen': 0, 'replacement_due': 2})
        self.assertEqual(recovery.received_screening_replacement_due(self.db, fixture.CYCLE, data), 2)
        self.assertEqual(self.case.finances(), finances)
        audit = self.db.execute('SELECT source,returned FROM screening_review_returns').fetchone()
        self.assertEqual(json.loads(audit['source']), source)
        self.assertEqual(json.loads(audit['returned']), returned)
        preserved = json.loads(self.db.execute('SELECT proof FROM screening_response_recoveries').fetchone()[0])
        self.assertEqual(preserved['observation']['review_receipts'], returned['review_receipts'])
        self.assertEqual(preserved['observation']['review_receipts'][0]['value']['response']['decision']['action'], 'CLARIFY')
        self.assertEqual(self.row('research_browser_responses')['assistant_text'], self.reply)

    def test_repeat_and_database_restart_preserve_the_original_receipt_and_audit(self):
        self.retain_positive_return()
        self.assertEqual(self.recover(), 'NOEXEC')
        snapshot = tuple(self.db.iterdump())
        self.restart()
        self.assertEqual(self.recover(now=fixture.NOW+1), 'NOEXEC')
        self.assertEqual(self.recover(now=fixture.NOW+2), 'NOEXEC')
        self.assertEqual(tuple(self.db.iterdump()), snapshot)

    def test_crash_after_native_receipt_commit_is_recovered_without_a_second_record(self):
        self.retain_positive_return()
        original = self.case.provider._record_browser_response
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('controlled disappearance after native receipt')
        self.case.provider._record_browser_response = interrupted
        with self.assertRaisesRegex(RuntimeError, 'controlled disappearance'):
            self.recover()
        self.assertEqual(self.row('api_requests')['state'], 'COMPLETE')
        self.assertEqual(self.db.execute('SELECT phase FROM screening_response_recoveries').fetchone()[0], 'PREPARED')
        receipt = self.row('research_browser_responses')
        self.restart()
        self.assertEqual(self.recover(now=fixture.NOW+1), 'NOEXEC')
        self.assertEqual(self.row('research_browser_responses'), receipt)
        self.assertEqual(self.row('api_requests')['attempts'], 1)

    def test_fresh_clarify_never_becomes_final_approval_even_with_positive_return(self):
        self.db.execute('UPDATE api_requests SET created=?', (fixture.NOW-90,))
        self.retain_positive_return()
        snapshot = tuple(self.db.iterdump())
        with self.assertRaisesRegex(Review, 'NEUROBRO_DECISION_CONFLICT'):
            self.recover()
        self.assertEqual(tuple(self.db.iterdump()), snapshot)

    def test_missing_unknown_mutated_or_wrong_transcript_review_cannot_authorize_retirement(self):
        for mutation in ('missing', 'unknown', 'changed_result', 'wrong_transcript', 'wrong_request'):
            with self.subTest(mutation=mutation):
                envelope = copy.deepcopy(self.envelope)
                if mutation == 'missing':
                    envelope['observation']['review_receipts'] = []
                else:
                    value = envelope['observation']['review_receipts'][0]['value']
                    if mutation == 'unknown': value['model_operation']['state'] = 'UNKNOWN'
                    elif mutation == 'changed_result': value['response']['decision']['question'] += ' Berbeda.'
                    elif mutation == 'wrong_transcript': value['transcript_sha256'] = 'c'*64
                    elif mutation == 'wrong_request': value['request_sha256'] = 'c'*64
                    self.seal_review(envelope)
                self.case.seal(envelope)
                snapshot = tuple(self.db.iterdump())
                with self.assertRaises(Review): self.recover(envelope)
                self.assertEqual(tuple(self.db.iterdump()), snapshot)

    def test_inconsistent_return_kind_operation_or_receipt_is_never_retained(self):
        valid = dict(kind=recovery.REVIEW_RETURN_KIND, operation=self.operation,
            request_sha256=self.case.request_hash, operation_completed=False,
            review_receipts=copy.deepcopy(self.envelope['observation']['review_receipts']))
        for mutation in ('kind', 'operation', 'hash', 'complete', 'missing_review', 'changed_result'):
            with self.subTest(mutation=mutation):
                returned = copy.deepcopy(valid)
                if mutation == 'kind': returned['kind'] = 'NATIVE_RECEIVED_SCREENING_REVIEW_V1'
                elif mutation == 'operation': returned['operation'] += '-different'
                elif mutation == 'hash': returned['request_sha256'] = 'c'*64
                elif mutation == 'complete': returned['operation_completed'] = True
                elif mutation == 'missing_review': returned['review_receipts'] = []
                else: returned['review_receipts'][0]['value']['response']['decision']['question'] += ' Changé.'
                before = tuple(self.db.iterdump())
                with self.assertRaises(Review):
                    recovery.retain_screening_review_return(self.case.provider, self.envelope, returned,
                        catalog=self.case.catalog, max_age=180, now=fixture.NOW-8)
                self.assertEqual(tuple(self.db.iterdump()), before)

    def test_changed_turn_after_positive_return_does_not_reuse_old_no_send_proof(self):
        self.retain_positive_return()
        self.envelope['observation']['rows'][0]['text'] += '\nChanged.'
        self.case.seal(self.envelope)
        before = tuple(self.db.iterdump())
        with self.assertRaises(Review): self.recover()
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_retained_return_is_append_only_and_idempotent(self):
        source, returned = self.retain_positive_return()
        before = tuple(self.db.iterdump())
        recovery.retain_screening_review_return(self.case.provider, source, returned,
            catalog=self.case.catalog, max_age=180, now=fixture.NOW-7)
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_missing_validator_database_refuses_with_typed_missing_custody(self):
        with self.assertRaisesRegex(Review, 'SCREENING_STALE_REVIEW_RETURN_NOT_RETAINED'):
            recovery.validate_received_screening(None, self.envelope, self.case.payload,
                fixture.NOW-1000, catalog=self.case.catalog, max_age=180, now=fixture.NOW)

    def test_audit_writer_refuses_changed_api_job_cycle_or_owner_without_state_writes(self):
        returned = dict(kind=recovery.REVIEW_RETURN_KIND, operation=self.operation,
            request_sha256=self.case.request_hash, operation_completed=False,
            review_receipts=copy.deepcopy(self.envelope['observation']['review_receipts']))
        cases = (
            ('api_requests', 'idempotency', None, 'foreign-token'),
            ('api_requests', 'attempts', 1, 2),
            ('robot_jobs', 'kind', 'SCREENING', 'ANALYSIS'),
            ('robot_jobs', 'cycle', fixture.CYCLE, fixture.CYCLE+'-other'),
            ('robot_jobs', 'symbol', None, 'DOTUSDT'),
            ('robot_jobs', 'state', 'NEEDS_REVIEW', 'COMPLETE'),
            ('robot_cycles', 'state', 'NEEDS_REVIEW', 'COMPLETE'),
            ('robot_settings', 'enabled', 1, 0),
        )
        for table, column, original, changed in cases:
            with self.subTest(table=table, column=column):
                self.db.execute('UPDATE ' + table + ' SET ' + column + '=?', (changed,))
                before = tuple(self.db.iterdump())
                with self.assertRaises(Review):
                    recovery.retain_screening_review_return(self.case.provider, self.envelope, returned,
                        catalog=self.case.catalog, max_age=180, now=fixture.NOW-8)
                self.assertEqual(tuple(self.db.iterdump()), before)
                self.db.execute('UPDATE ' + table + ' SET ' + column + '=?', (original,))

    def test_a_real_accepted_intent_prevents_recording_research_recovery_custody(self):
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?)',
            ('foreign-owned-intent', self.operation, 'DOTUSDT', 'ENTRY_PENDING', 'immutable accepted payload'))
        before = tuple(self.db.iterdump())
        with self.assertRaises(Review): self.retain_positive_return()
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_changed_done_metadata_cannot_reuse_a_positive_return_with_the_same_decision(self):
        self.retain_positive_return()
        entry = self.envelope['observation']['review_receipts'][0]
        entry['value']['model_operation']['request_hash'] = 'e'*64
        self.seal_review(self.envelope)
        before = tuple(self.db.iterdump())
        with self.assertRaises(Review): self.recover()
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_corrupt_audit_is_not_treated_as_missing_safe_replay_authority(self):
        self.retain_positive_return()
        self.db.execute('UPDATE screening_review_returns SET returned_sha256=?', ('e'*64,))
        before = tuple(self.db.iterdump())
        with self.assertRaisesRegex(Review, 'SCREENING_RESPONSE_RECOVERY_UNVERIFIED'):
            self.recover()
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_scheduler_retains_cached_review_only_return_then_retires_on_next_observation(self):
        c = self.case
        source = copy.deepcopy(self.envelope)
        returned = dict(kind=recovery.REVIEW_RETURN_KIND, operation=self.operation,
            request_sha256=c.request_hash, operation_completed=False,
            review_receipts=copy.deepcopy(source['observation']['review_receipts']))
        c.provider.observe_received_screening = Mock(side_effect=lambda _: copy.deepcopy(self.envelope))
        c.provider.review_received_screening = Mock(return_value=returned)
        coordinator = SimpleNamespace(db=self.db, neuro=c.provider, allowed=lambda _: True,
            market=SimpleNamespace(catalog=lambda: c.catalog, max_age=180),
            store=SimpleNamespace(report=lambda status, account=None, **kw: dict(bot_status=status, **kw)))
        with patch.object(recovery.time, 'time', return_value=fixture.NOW-8):
            first = recovery.resume_uncertain_screening(coordinator, fixture.DAY, {})
        self.assertEqual(first['wait_reason'], 'SCREENING_REVIEW_INCOMPLETE')
        self.assertEqual(c.provider.review_received_screening.call_count, 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM screening_review_returns').fetchone()[0], 1)
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')
        self.envelope['observation'].update(observed_at=fixture.NOW-6, finished_at=fixture.NOW-5)
        c.seal(self.envelope)
        with patch.object(recovery.time, 'time', return_value=fixture.NOW):
            second = recovery.resume_uncertain_screening(coordinator, fixture.DAY, {})
        self.assertEqual(second['wait_reason'], 'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')
        self.assertEqual(c.provider.review_received_screening.call_count, 1)
        self.assertEqual(c.provider.observe_received_screening.call_count, 2)
        self.assertEqual(self.row('api_requests')['attempts'], 1)


if __name__ == '__main__':
    unittest.main()
