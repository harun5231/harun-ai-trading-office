"""The bounded browser observer searches for the original operation, never sends it."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import native_received_analysis as observer
from agent.native_daily_room import DailyRoom, JOURNAL_NAME, identities, retain_prefix_snapshot
from agent.native_prompt_composition import PromptCompositionCache
from tests import test_received_screening_rounds as rounds
from tests.test_native_daily_room import History
from tests.test_native_received_analysis import ReadBridge
from worker.account_state import screening_contract


class ScreeningHistorySearchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = rounds.ReceivedScreeningRoundsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.case = self.fixture.case
        self.complete = copy.deepcopy(self.case.raw.messages)
        self.prefix = self.complete[:-2]
        self.epoch = 1795710900
        clock = patch.object(observer.time, 'time', side_effect=lambda: self.epoch + self.case.raw.current)
        clock.start()
        self.addCleanup(clock.stop)
        self.case.raw.session_observation = lambda: {'reason': self.case.status, 'counts': {}}
        self.bind(self.prefix)
        self.case.raw.messages = copy.deepcopy(self.prefix)
        self.journal = (self.case.root / JOURNAL_NAME).read_bytes()

    def bind(self, rows, *, day='2026-11-05', room_id='a' * 32, state='BOUND', binding=None):
        with self.case.room.store.open() as db:
            db.execute('UPDATE daily_rooms SET anchors=?, state=? WHERE day=?',
                       (json.dumps(identities(rows)), state, day))
            if binding is not None:
                db.execute('UPDATE daily_rooms SET binding=? WHERE day=?', (json.dumps(binding), day))
        retain_prefix_snapshot(self.case.root, day, room_id, rows)

    def forbidden(self, *args, **kwargs):
        self.fail('history recovery tried to render, send, create, upload, or call Mini')

    def keep_only_reads(self, raw):
        raw.ask = raw._upload = raw._answer = raw._fresh_conversation = self.forbidden
        raw.composer.fill = raw.button.click = self.forbidden

    def observe(self):
        result = self.fixture.invoke()
        proof = result['observation']
        self.assertEqual([proof[key] for key in ('model_calls', 'provider_messages', 'uploads', 'orders_sent')], [0]*4)
        self.assertFalse(result['operation_completed'])
        return result, proof

    def test_matching_prefix_reopens_history_until_original_cached_prompt_is_visible(self):
        raw = self.case.raw
        self.case.room.history.rooms = [('Analisis RUNE', self.prefix)]
        self.case.native.user_prompt = self.forbidden

        def readonly_status(*, maintenance_allowed):
            self.assertFalse(maintenance_allowed())
            return 'CONNECTED'

        raw.status = readonly_status

        def wait(milliseconds):
            raw.wait(milliseconds)
            if 0.75 <= raw.current < 1.0:
                raw.messages = copy.deepcopy(self.complete)

        raw.page.wait_for_timeout = wait
        result, proof = self.observe()
        self.assertEqual(self.case.room.history.selected, ['Analisis RUNE'])
        self.assertEqual(result['error_count'], 0)
        self.assertTrue(proof['operation_scope']['verified'])
        self.assertEqual(proof['rows'][0]['text'], self.complete[-2]['text'])
        self.assertEqual(proof['screening_history_search']['outcome'], 'TARGET_OBSERVED')
        self.assertEqual((self.case.root / JOURNAL_NAME).read_bytes(), self.journal)

    def test_missing_prompt_creates_strict_read_only_negative_proof_and_restart_checkpoint(self):
        original = self.case.raw
        self.case.room.history.rooms = [('Analisis RUNE', self.prefix)]
        result, proof = self.observe()
        search = proof['screening_history_search']
        self.assertTrue(result['history_restored'])
        self.assertEqual(proof['operation_scope']['reason'], 'INITIAL_PROMPT_NOT_FOUND')
        self.assertEqual(proof['rows'], [])
        self.assertEqual(proof['stable_snapshot_reads'], 4)
        self.assertEqual(search['outcome'], 'TARGET_ABSENT')
        self.assertTrue(search['strict_prefix_verified'])
        self.assertTrue(search['history_reselected'])
        self.assertEqual(search['marker_mentions'], 0)
        self.assertEqual(search['anchors_count'], len(self.prefix))
        self.assertGreaterEqual(original.current, 5)
        capture = json.loads((self.case.root / result['capture_name']).read_bytes())
        self.assertEqual(search['rows_sha256'], observer.native_digest(capture['rows']))
        self.assertEqual((self.case.root / JOURNAL_NAME).read_bytes(), self.journal)

        # A fresh process and WIB day still use the original operation/day.
        raw = ReadBridge(self.prefix)
        raw.current = original.current + 1
        raw.status = original.status
        raw.session_observation = original.session_observation
        self.keep_only_reads(raw)
        self.case.raw = raw
        history = History(raw)
        history.rooms = [('Analisis RUNE', self.complete)]
        room = DailyRoom(raw, self.case.native, self.case.root, history=history)
        self.case.room = room
        self.case.delegated.daily_room = room
        self.case.delegated.bridge = raw
        _, second = self.observe()
        self.assertTrue(second['history_search_cooldown'])
        self.assertEqual(history.selected, [])
        self.epoch += 86400
        result, proof = self.observe()
        self.assertEqual(result['error_count'], 0)
        self.assertEqual(proof['room']['day'], '2026-11-05')
        self.assertTrue(proof['operation_scope']['verified'])
        self.assertEqual(proof['screening_history_search']['outcome'], 'TARGET_OBSERVED')

    def test_historical_assistant_ids_can_rehydrate_repeatedly_during_sampling(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' /
            'native_daily_prefix_assistant_ids_20261126.json').read_text())
        self.assertEqual(fixture['operation'], '2026-11-26:robot-v9:0:screening:1:2')
        self.assertEqual(fixture['submitted_state'], 'UNKNOWN')
        raw = self.case.raw
        self.case.room.history.rooms = [('Analisis RUNE', self.complete)]
        changes = []

        def wait(milliseconds):
            raw.wait(milliseconds)
            if self.case.room.history.selected and len(changes) < 3:
                changes.append(raw.current)
                raw.messages[1]['id'] = f'rehydrated-assistant-{len(changes)}'

        raw.page.wait_for_timeout = wait
        result, proof = self.observe()
        self.assertEqual(len(changes), 3)
        self.assertEqual(result['error_count'], 0)
        self.assertTrue(proof['operation_scope']['verified'])
        self.assertEqual(proof['stable_snapshot_reads'], 4)

    def test_wrong_room_with_exact_prompt_does_not_authorize_result(self):
        changed = copy.deepcopy(self.complete)
        changed[1]['text'] = 'Changed historical reply with same first prompt.'
        self.case.room.history.rooms = [('Analisis RUNE', changed)]
        result, proof = self.observe()
        self.assertGreater(result['error_count'], 0)
        self.assertNotIn('screening_history_search', proof)
        self.assertFalse(proof.get('operation_scope', {}).get('verified', False))

    def test_partial_history_with_matching_target_cannot_authorize_result(self):
        self.case.room.history.rooms = [('Other title', self.complete[-4:])]
        result, proof = self.observe()
        self.assertGreater(result['error_count'], 0)
        self.assertFalse(proof.get('operation_scope', {}).get('verified', False))

    def test_marker_seen_in_an_unbound_view_prevents_absent_retirement_proof(self):
        partial = self.complete[-2:]
        self.case.raw.messages = copy.deepcopy(partial)
        self.case.room.history.rooms = [('Analisis RUNE', self.prefix)]
        result, proof = self.observe()
        self.assertEqual(result['error_count'], 0)
        self.assertEqual(proof['screening_history_search']['outcome'], 'INCONCLUSIVE')
        self.assertFalse(proof['operation_scope']['verified'])

    def test_duplicate_target_is_distinct_from_missing_and_never_accepted(self):
        self.case.room.history.rooms = [('Analisis RUNE', self.complete + self.complete[-2:])]
        _, proof = self.observe()
        self.assertEqual(proof['operation_scope']['reason'], 'INITIAL_PROMPT_NOT_UNIQUE')
        self.assertEqual(proof['screening_history_search']['target_matches'], 2)
        self.assertFalse(proof['operation_scope']['verified'])

    def test_pending_original_reply_waits_without_another_history_selection(self):
        partial = self.complete[:-1] + [{**self.complete[-1], 'complete': False}]
        self.case.room.history.rooms = [('Analisis RUNE', partial)]
        _, proof = self.observe()
        self.assertEqual(proof['operation_scope']['reason'], 'SCREENING_REPLY_NOT_COMPLETE')
        self.assertFalse(proof['operation_scope']['verified'])
        self.case.raw.messages = copy.deepcopy(self.complete)
        _, proof = self.observe()
        self.assertTrue(proof['operation_scope']['verified'])
        self.assertEqual(self.case.room.history.selected, ['Analisis RUNE'])

    def test_retained_complete_reply_and_review_reuse_original_capture_after_dom_loss(self):
        self.case.raw.messages = copy.deepcopy(self.complete)
        client = self.fixture.install_review()
        self.fixture.review(self.fixture.invoke())
        original, original_proof = self.observe()
        self.assertEqual(client.calls, 1)
        self.assertTrue(original_proof['review_receipts'])
        self.case.raw.messages = copy.deepcopy(self.prefix)
        self.case.room.history.select = self.forbidden
        resumed, proof = self.observe()
        self.assertEqual(resumed['capture_name'], original['capture_name'])
        self.assertEqual(resumed['capture_sha256'], original['capture_sha256'])
        self.assertEqual(proof, original_proof)
        self.assertEqual(client.calls, 1)

    def test_authentication_required_does_not_select_history(self):
        self.case.status = 'CHALLENGE_REQUIRED'
        self.case.room.history.select = self.forbidden
        result, proof = self.observe()
        self.assertEqual(result['status'], 'CHALLENGE_REQUIRED')
        self.assertFalse(result['history_restore_attempted'])
        self.assertNotIn('screening_history_search', proof)

    def make_unbound_initial(self):
        operation = '2026-11-05:robot-v9:0:screening:0:1'
        prompt, schema = screening_contract(1)
        body = dict(operation=operation, provider='NEUROBRO_WEB',
                    contract_version='neurobro-browser-decision-v2', prompt=prompt,
                    output_schema=schema, context=None, marker='HARUN_' + observer.text_sha(operation)[:32])
        payload = dict(body, request_sha256=observer.digest(body))
        cache = PromptCompositionCache(self.case.native, self.case.root)
        rendered = cache.store(payload, prompt, 'a'*64)
        self.fixture.payload = payload
        self.fixture.request['payload'] = payload
        self.bind([], state='SENT', binding=dict(marker=payload['marker'],
            first_prompt_sha256=observer.text_sha(rendered.replace('\r\n', '\n').strip()), history_titles=[]))
        return rendered

    def test_unbound_initial_exhaustion_is_only_unknown_and_has_no_result(self):
        self.make_unbound_initial()
        self.case.room.history.rooms = [('Earlier chat', self.prefix)]
        result, proof = self.observe()
        self.assertEqual(result['error_count'], 0)
        self.assertTrue(result['history_restore_attempted'])
        self.assertFalse(result['history_restored'])
        self.assertEqual(proof['screening_history_search']['outcome'], 'UNBOUND_INITIAL_EXHAUSTED')
        self.assertEqual(proof['operation_scope']['reason'], 'INITIAL_PROMPT_NOT_FOUND')
        self.assertEqual(proof['room']['anchors'], [])
        self.assertEqual(proof['rows'], [])

    def test_unbound_partial_marker_never_counts_as_exhausted(self):
        self.make_unbound_initial()
        marker = self.fixture.payload['marker']
        self.case.raw.messages = [dict(role='user', text='Partial old display ' + marker, attachments=[])]
        self.case.room.history.rooms = [('Earlier chat', self.prefix)]
        result, proof = self.observe()
        self.assertGreater(result['error_count'], 0)
        self.assertNotIn('screening_history_search', proof)


if __name__ == '__main__':
    unittest.main()
