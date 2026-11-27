"""Source-proven terminal UI failures retire research, never invent a reply.

The Indonesian terminal suffix is the actual 21 November production capture.
All model, browser Send, upload and order entry paths are forbidden fixtures.
"""
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


ACTUAL = json.loads((Path(__file__).parent / 'fixtures/neurobro_terminal_screening_failure_20261121.json').read_text())
# Reproduce the public control sequence of the 27 November capture (run
# 46050215013). Earlier room text, prompt and clock label use fixture values.
RETRY_TOOLBAR_SUFFIX = ('\nTampilkan lebih banyak\n15.24\nNeurobro tidak dapat menjawab pertanyaan ini\n'
    'Coba lagi\nRingan\nPrompt\nNeurobro bisa keliru. Bukan nasihat keuangan.')


class TerminalProviderErrorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.GenericScreeningResponseRecoveryTests()
        self.case.setUp(); self.addCleanup(self.case.doCleanups)
        self.db = self.case.db
        self.db.execute(recovery.RECEIVED_SCHEMA)
        self.operation = fixture.OP
        self.envelope = self.negative()
        self.case.provider._record_browser_response = self.case.forbid

    def negative(self, *, operation=None, observed=fixture.NOW - 11):
        c = self.case; operation = operation or self.operation
        envelope = c.fixture(operation=operation, approval=False)
        proof = envelope['observation']; proof['rows'] = proof['rows'][:1]
        proof.update(room_initial_state='SENT', observed_at=observed, finished_at=observed + 1,
            operation_scope=dict(verified=False, initial_user=0, latest_user=0,
                user_rows=[0], reply_row=None, reply_complete=False, unmatched_intents=[],
                reason='SCREENING_TRANSCRIPT_UNVERIFIED'))
        proof['room_witness'].update(journal_url_matches=True)
        user_identity = recovery._digest({key: value for key, value in proof['rows'][0].items() if key != 'attachments'})
        identifiers = [recovery._digest({'older_source_row': i}) for i in range(proof['row_offset'])] + [user_identity]
        rendered = proof['rendered_prompt_receipt']['rendered_prompt']
        body = 'Earlier source-bound room discussion\n' + rendered + ACTUAL['suffix']
        proof['screening_terminal_error'] = dict(schema_version=1, kind=recovery.PROVIDER_ERROR_KIND,
            operation=operation, request_sha256=c.request_hash, payload_sha256=recovery._digest(c.payload),
            marker=c.payload['marker'], native_source_sha256=fixture.NATIVE_SOURCE,
            room={key: proof['room'][key] for key in ('day', 'room_id', 'url')},
            rendered_prompt_sha256=recovery._sha(rendered), user_row=proof['row_offset'],
            row_count=len(identifiers), user_identity_sha256=user_identity,
            rows_identity_sha256=recovery._digest(identifiers), prefix_identity_sha256=recovery._digest(identifiers[:-1]),
            row_identities=identifiers, observed_at=observed, stable_snapshot_reads=4,
            terminal_ui=dict(contract='RENDERED_BODY_TERMINAL_SUFFIX_ID_V1', body_text=body,
                body_sha256=recovery._sha(body), body_bytes=len(body.encode()),
                rendered_prompt_occurrences=1, marker_occurrences=1, suffix=ACTUAL['suffix']))
        return c.seal(envelope)

    def row(self, table):
        key, value = ('id', fixture.CYCLE) if table == 'robot_cycles' else ('operation', self.operation)
        return recovery._row(self.db, table, key, value)

    def recover(self, envelope=None, *, now=fixture.NOW, permitted=lambda: True):
        return recovery.recover_received_screening(self.case.provider, envelope or self.envelope,
            catalog={}, max_age=180, now=now, permitted=permitted)

    def coordinator(self, *, observe=None, allowed=None):
        c = self.case
        c.provider.observe_received_screening = Mock(return_value=self.envelope) if observe is None else observe
        return SimpleNamespace(db=self.db, neuro=c.provider,
            market=SimpleNamespace(catalog=Mock(side_effect=c.forbid), max_age=180),
            allowed=Mock(side_effect=lambda day: recovery._row(self.db, 'robot_settings', 'id', 1)['enabled'] == 1)
                if allowed is None else allowed,
            pause_reason=lambda: 'ROBOT_OFF',
            store=SimpleNamespace(report=lambda status, account, **fields: dict(status=status, **fields)))

    def test_actual_provider_error_retires_request_once_without_inventing_response_or_touching_finances(self):
        c = self.case; before_api = self.row('api_requests'); before_request = self.row('research_browser_requests')
        finances = c.finances()
        self.assertEqual(self.recover(), 'NOEXEC')
        self.assertEqual(self.row('api_requests'), {**before_api, 'state': recovery.PROVIDER_ERROR_TERMINAL,
            'failure_code': recovery.PROVIDER_ERROR_REASON})
        self.assertEqual(self.row('research_browser_requests'), before_request)
        self.assertIsNone(self.row('research_browser_responses'))
        self.assertEqual(self.row('robot_jobs')['state'], 'REQUEST_REJECTED')
        self.assertEqual(self.row('robot_cycles')['state'], 'ACTIVE')
        data = json.loads(self.row('robot_cycles')['data'])
        self.assertEqual(data, {**c.data, 'screen': 9, 'replacement_due': 1,
            'replacements': 9, 'queue': [], 'round_symbols': []})
        self.assertEqual(c.finances(), finances)
        journal = self.row('screening_response_recoveries'); sealed = json.loads(journal['proof'])
        self.assertEqual(journal['phase'], 'NOEXEC'); self.assertEqual(journal['created'], journal['updated'])
        self.assertEqual(sealed['observation'], self.envelope['observation'])
        self.assertFalse(sealed['retirement']['canonical_result'])
        self.assertEqual(sealed['retirement']['kind'], recovery.PROVIDER_ERROR_NOEXEC)
        self.assertEqual((sealed['retirement']['failure_streak'], sealed['retirement']['retry_delay_seconds'],
            sealed['retirement']['retry_not_before']), (1, 60, fixture.NOW + 60))
        self.assertEqual(json.loads(journal['preimage'])['api_requests'], before_api)

    def test_native_incomplete_scope_preserves_independently_proved_terminal_error(self):
        from agent.native_received_screening import operation_scope

        proof = self.envelope['observation']
        proof['operation_scope'] = operation_scope(proof['rows'], proof['room_witness'], [], self.case.payload)
        self.assertEqual(proof['operation_scope']['reason'], 'SCREENING_REPLY_NOT_COMPLETE')
        self.assertEqual(self.recover(self.case.seal(self.envelope)), 'NOEXEC')
        self.assertEqual(self.row('api_requests')['state'], recovery.PROVIDER_ERROR_TERMINAL)
        self.assertIsNone(self.row('research_browser_responses'))

    def test_repeat_and_restart_read_same_append_only_seal_without_replay(self):
        self.assertEqual(self.recover(), 'NOEXEC'); before = tuple(self.db.iterdump())
        self.assertEqual(self.recover(), 'NOEXEC'); self.assertEqual(tuple(self.db.iterdump()), before)
        restarted = sqlite3.connect(':memory:', isolation_level=None); restarted.row_factory = sqlite3.Row
        self.addCleanup(restarted.close); self.db.backup(restarted)
        self.db = self.case.provider.db = restarted
        # A process restart does not need another capture or a live browser.
        self.assertEqual(self.recover({'operation': self.operation}), 'NOEXEC')
        self.assertEqual(tuple(restarted.iterdump()), before)
        self.db.execute("UPDATE robot_settings SET risk='8.75'")
        self.assertEqual(self.recover({'operation': self.operation}), 'NOEXEC')
        self.assertEqual(recovery._row(self.db, 'robot_settings', 'id', 1)['risk'], '8.75')
        self.db.execute("UPDATE robot_cycles SET data=?", (json.dumps(dict(screen=10, queue=['SUIUSDT'])),))
        progress = tuple(self.db.iterdump())
        self.assertEqual(self.recover({'operation': self.operation}), 'NOEXEC')
        self.assertEqual(tuple(self.db.iterdump()), progress)
        self.assertEqual(recovery.received_screening_replacement_due(self.db, fixture.CYCLE,
            json.loads(self.row('robot_cycles')['data'])), 0)

    def test_legacy_operator_classification_requires_real_negative_proof_before_unblocking(self):
        self.db.execute('UPDATE api_requests SET state=?,failure_code=?',
            (recovery.PROVIDER_ERROR_TERMINAL, recovery.PROVIDER_ERROR_REASON))
        self.db.execute("UPDATE robot_jobs SET state='REQUEST_REJECTED'")
        before = tuple(self.db.iterdump())
        for value in (self.case.fixture(approval=False), self.case.fixture()):
            with self.assertRaises(Review): self.recover(value)
            self.assertEqual(tuple(self.db.iterdump()), before)
        coordinator = self.coordinator()
        with patch.object(recovery.time, 'time', return_value=fixture.NOW):
            result = recovery.resume_uncertain_screening(coordinator, fixture.DAY, {})
        self.assertEqual(result['wait_reason'], 'NEUROBRO_WEB_PROCESSING_FAILED_RETRY_PENDING')
        self.assertEqual(self.row('robot_cycles')['state'], 'ACTIVE')
        self.assertIsNone(self.row('api_requests')['output'])
        self.assertEqual(self.row('api_requests')['attempts'], 1)
        coordinator.market.catalog.assert_not_called()
        self.assertEqual(json.loads(self.row('screening_response_recoveries')['preimage'])['api_requests']['state'],
            recovery.PROVIDER_ERROR_TERMINAL)

    def test_missing_error_or_any_assistant_does_not_authorize_negative_recovery(self):
        changes = (
            lambda p: p.pop('screening_terminal_error'),
            lambda p: p['rows'].append(dict(role='assistant', text='An actual answer', id='late', complete=True)),
            lambda p: p['rows'].append(dict(role='assistant', text='Streaming', id='partial', complete=False)),
            lambda p: p['rows'].append(dict(role='unknown', text='', complete=False)),
            lambda p: p['rows'].append(dict(p['rows'][0])),
            lambda p: p['operation_scope'].update(verified=True, reply_complete=True, reply_row=1),
            lambda p: p['rows'][0].update(attachments=['unconfirmed.png']),
            lambda p: p.update(review_receipts=[{}]),
            lambda p: p.update(room_initial_state='FRESH'),
            lambda p: p.update(room_initial_state='CLAIMED'),
            lambda p: p.update(room_initial_state='PREPARED'),
            lambda p: p.update(room_initial_state=None),
        )
        before = tuple(self.db.iterdump())
        for change in changes:
            with self.subTest(change=change):
                envelope = copy.deepcopy(self.envelope); change(envelope['observation']); self.case.seal(envelope)
                with self.assertRaises(Review): self.recover(envelope)
                self.assertEqual(tuple(self.db.iterdump()), before)

    def test_corrupt_provenance_or_ui_never_retires_unknown(self):
        changes = (
            lambda p: p.update(native_source_sha256='0' * 64),
            lambda p: p['screening_terminal_error'].update(native_source_sha256='0' * 64),
            lambda p: p['screening_terminal_error'].update(operation=self.operation + ':wrong'),
            lambda p: p['screening_terminal_error'].update(request_sha256='0' * 64),
            lambda p: p['screening_terminal_error'].update(payload_sha256='0' * 64),
            lambda p: p['screening_terminal_error'].update(marker='HARUN_wrong'),
            lambda p: p['screening_terminal_error']['room'].update(room_id='b' * 32),
            lambda p: p['room_witness'].update(journal_url_matches=False),
            lambda p: p.update(stable_snapshot_reads=3),
            lambda p: p['screening_terminal_error'].update(stable_snapshot_reads=3),
            lambda p: p['screening_terminal_error'].update(observed_at=fixture.NOW),
            lambda p: p['screening_terminal_error']['row_identities'].__setitem__(-1, '0' * 64),
            lambda p: p['screening_terminal_error'].update(prefix_identity_sha256='0' * 64),
            lambda p: p['screening_terminal_error'].update(user_row=0),
            lambda p: p['screening_terminal_error'].update(row_count=1),
            lambda p: p['screening_terminal_error']['terminal_ui'].update(body_sha256='0' * 64),
            lambda p: p['screening_terminal_error']['terminal_ui'].update(body_bytes=1),
            lambda p: p['screening_terminal_error']['terminal_ui'].update(suffix='Neurobro tidak dapat menjawab pertanyaan ini'),
            lambda p: p['screening_terminal_error']['terminal_ui'].update(marker_occurrences=0),
            lambda p: p.update(status='CHALLENGE_REQUIRED'),
            lambda p: p.update(errors=[{'kind': 'read_failed'}]),
        )
        before = tuple(self.db.iterdump())
        for change in changes:
            with self.subTest(change=change):
                envelope = copy.deepcopy(self.envelope); change(envelope['observation']); self.case.seal(envelope)
                with self.assertRaises(Review): self.recover(envelope)
                self.assertEqual(tuple(self.db.iterdump()), before)

    def test_matching_hashes_do_not_make_partial_or_quoted_or_ambiguous_ui_terminal(self):
        for suffix in (ACTUAL['suffix'] + 'Late assistant answer', ACTUAL['suffix'].replace('15.24', '25.24'),
                '\nQuoted:\n' + ACTUAL['suffix'], '\nWaiting for response\n', ACTUAL['suffix'].replace('tidak dapat', 'dapat')):
            with self.subTest(suffix=suffix):
                envelope = copy.deepcopy(self.envelope); error = envelope['observation']['screening_terminal_error']
                body = 'Earlier room\n' + envelope['observation']['rows'][0]['text'] + suffix
                error['terminal_ui'].update(body_text=body, body_sha256=recovery._sha(body),
                    body_bytes=len(body.encode()), suffix=suffix)
                self.case.seal(envelope)
                with self.assertRaises(Review): self.recover(envelope)
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')

    def test_screenshot_known_rendered_controls_are_accepted_without_permitting_arbitrary_words(self):
        from agent.native_received_screening import TERMINAL_BODY_SUFFIX, terminal_body_ui
        self.assertEqual(recovery.PROVIDER_ERROR_SUFFIX, TERMINAL_BODY_SUFFIX)
        suffix = ('\nTampilkan lebih banyak\n15.24\nNeurobro tidak dapat menjawab pertanyaan ini\n'
            'Coba lagi\nTanyakan apa saja\nRingan\nPrompt\n\nNeurobro bisa keliru. Bukan nasihat keuangan.\n')
        padded = RETRY_TOOLBAR_SUFFIX.replace('\nCoba lagi\nRingan', '\n\nCoba lagi\n\nRingan')
        padded = padded.replace('\nPrompt\nNeurobro', '\nPrompt\n\nNeurobro') + '\n'
        rejected = (
            suffix.replace('Coba lagi', 'Unknown UI control'),
            suffix.replace('Ringan\nPrompt', 'Ringan\nBuy now'),
            RETRY_TOOLBAR_SUFFIX.replace('Coba lagi\n', ''),
            RETRY_TOOLBAR_SUFFIX.replace('Ringan\n', ''),
            RETRY_TOOLBAR_SUFFIX.replace('Prompt\n', ''),
            RETRY_TOOLBAR_SUFFIX.replace('Coba lagi', 'Coba lagi.'),
            RETRY_TOOLBAR_SUFFIX.replace('Prompt', 'Prompt\nStreaming'),
            RETRY_TOOLBAR_SUFFIX.replace('Neurobro bisa keliru.', 'Neurobro bisa keliru'),
            RETRY_TOOLBAR_SUFFIX + '\n{"SYMBOLS":["SOLUSDT"]}',
            padded.replace('\n\nRingan', '\n\n\nRingan'),
        )
        for text, allowed in [(value, True) for value in (suffix, RETRY_TOOLBAR_SUFFIX, padded)] + [
                (value, False) for value in rejected]:
            with self.subTest(suffix=text):
                envelope = copy.deepcopy(self.envelope)
                ui = envelope['observation']['screening_terminal_error']['terminal_ui']
                rendered = envelope['observation']['rows'][0]['text']
                body = 'Known historical discussion\n' + rendered + text
                self.assertEqual(terminal_body_ui(body, rendered, self.case.payload['marker']) is not None, allowed)
                ui.update(body_text=body, body_sha256=recovery._sha(body), body_bytes=len(body.encode()), suffix=text)
                self.case.seal(envelope)
                if allowed:
                    recovery._terminal_provider_error_shape(envelope, self.case.payload, self.case.created,
                        max_age=180, now=fixture.NOW)
                else:
                    with self.assertRaises(Review):
                        recovery._terminal_provider_error_shape(envelope, self.case.payload, self.case.created,
                            max_age=180, now=fixture.NOW)

    def test_authorized_cached_composition_is_bound_without_requiring_verbatim_owner_template(self):
        envelope = copy.deepcopy(self.envelope); proof = envelope['observation']
        # Mini's existing compose path may legitimately rephrase the template.
        # Custody is the immutable original payload hash and actual cached
        # rendered prompt, never a newly reconstructed current-day prompt.
        rendered = 'Pilih satu kandidat berdasarkan likuiditas dan momentum terbaru.\n' + self.case.payload['marker']
        proof['rows'][0]['text'] = rendered
        cache = proof['rendered_prompt_receipt']
        cache.update(rendered_prompt=rendered, rendered_sha256=recovery._sha(rendered))
        error = proof['screening_terminal_error']; ids = error['row_identities']
        ids[-1] = recovery._digest({key: value for key, value in proof['rows'][0].items() if key != 'attachments'})
        error.update(rendered_prompt_sha256=recovery._sha(rendered), user_identity_sha256=ids[-1],
            rows_identity_sha256=recovery._digest(ids))
        body = 'Earlier room\n' + rendered + ACTUAL['suffix']
        error['terminal_ui'].update(body_text=body, body_sha256=recovery._sha(body), body_bytes=len(body.encode()))
        self.case.seal(envelope)
        self.assertNotIn(self.case.payload['prompt'], rendered)
        self.assertEqual(self.recover(envelope), 'NOEXEC')

    def test_permission_cas_receipt_candidate_and_intent_guards_preserve_all_original_state(self):
        before = tuple(self.db.iterdump())
        with self.assertRaises(Review): self.recover(permitted=lambda: False)
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.db.execute('UPDATE robot_settings SET enabled=0')
        with self.assertRaises(Review): self.recover()
        self.db.execute('UPDATE robot_settings SET enabled=1')
        self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
            (self.operation, fixture.CYCLE, 'SUIUSDT', 'ACCEPTED', 'immutable plan', None))
        before = tuple(self.db.iterdump())
        with self.assertRaises(Review): self.recover()
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.db.execute('DELETE FROM robot_candidates WHERE id=?', (self.operation,))
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?)',
            ('actual-intent', self.operation, 'SUIUSDT', 'OPEN', 'immutable order'))
        before = tuple(self.db.iterdump())
        with self.assertRaises(Review): self.recover()
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_expired_observation_is_not_silently_used_for_new_negative_closure(self):
        with self.assertRaises(Review): self.recover(now=fixture.NOW + 121)
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')

    def test_actual_received_response_prevents_negative_closure_even_if_ui_later_shows_error(self):
        self.db.execute('INSERT INTO research_browser_responses '
            '(operation,response_id,output_sha256,assistant_text,assistant_sha256) VALUES(?,?,?,?,?)',
            (self.operation, 'actual-original-answer', 'a' * 64, 'COIN: SUIUSDT', recovery._sha('COIN: SUIUSDT')))
        before = tuple(self.db.iterdump())
        with self.assertRaises(Review): self.recover()
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_owner_setting_change_before_cas_is_preserved_and_does_not_seal_stale_snapshot(self):
        original = recovery._provider_error_backoff
        def owner_changed(*args):
            result = original(*args)
            self.db.execute("UPDATE robot_settings SET risk='9.25'")
            return result
        with patch.object(recovery, '_provider_error_backoff', side_effect=owner_changed), self.assertRaises(Review):
            self.recover()
        self.assertEqual(recovery._row(self.db, 'robot_settings', 'id', 1)['risk'], '9.25')
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')
        self.assertEqual(self.row('robot_cycles')['state'], 'NEEDS_REVIEW')
        self.assertIsNone(self.row('screening_response_recoveries'))

    def test_atomic_failure_rolls_back_and_restart_after_committed_interruption_does_not_repeat(self):
        self.db.execute("CREATE TRIGGER stop_cycle BEFORE UPDATE ON robot_cycles BEGIN SELECT RAISE(ABORT,'test interruption'); END")
        before = tuple(self.db.iterdump())
        with self.assertRaises(sqlite3.IntegrityError): self.recover()
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.db.execute('DROP TRIGGER stop_cycle')
        class AfterCommit(sqlite3.Connection):
            crash = False
            def execute(db, sql, *args):
                value = super().execute(sql, *args)
                if sql == 'COMMIT' and db.crash:
                    db.crash = False
                    raise RuntimeError('simulated process interruption after commit')
                return value
        restarted = sqlite3.connect(':memory:', isolation_level=None, factory=AfterCommit)
        restarted.row_factory = sqlite3.Row; self.addCleanup(restarted.close)
        self.db.backup(restarted); self.db = self.case.provider.db = restarted
        restarted.crash = True
        with self.assertRaisesRegex(RuntimeError, 'after commit'): self.recover()
        self.assertEqual(self.row('api_requests')['state'], recovery.PROVIDER_ERROR_TERMINAL)
        self.assertEqual(self.row('robot_cycles')['state'], 'ACTIVE')
        before = tuple(self.db.iterdump())
        self.assertEqual(self.recover({'operation': self.operation}), 'NOEXEC')
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_initial_two_coin_screening_error_keeps_capacity_two_and_does_not_add_blacklist(self):
        c = self.case; old_operation = self.operation
        self.operation = fixture.CYCLE + ':screening:0:2'
        _, schema = screening_contract(2)
        c.body, encoded, c.request_hash = c.source.request_body(self.operation, 'Pilih dua coin.', schema, None)
        c.payload = {**c.body, 'request_sha256': c.request_hash}
        c.data.update(screen=-1, replacements=0, seen=[], round_symbols=[])
        self.db.execute('UPDATE robot_cycles SET data=?', (json.dumps(c.data),))
        for table in ('robot_jobs', 'api_requests', 'research_browser_requests'):
            self.db.execute('UPDATE ' + table + ' SET operation=? WHERE operation=?', (self.operation, old_operation))
        self.db.execute('UPDATE api_requests SET body_hash=?', (c.request_hash,))
        self.db.execute('UPDATE research_browser_requests SET request_sha256=?,marker=?,request_body=?',
            (c.request_hash, c.body['marker'], encoded))
        self.envelope = self.negative()
        self.assertEqual(self.recover(), 'NOEXEC')
        data = json.loads(self.row('robot_cycles')['data'])
        self.assertEqual(data, {**c.data, 'screen': 0, 'replacement_due': 2})
        with patch.object(recovery.time, 'time', return_value=fixture.NOW + 60):
            self.assertEqual(recovery.received_screening_replacement_due(self.db, fixture.CYCLE, data), 2)

    def test_cooldown_survives_ticks_restart_owner_pause_and_wib_rollover_without_outbound_work(self):
        self.assertEqual(self.recover(), 'NOEXEC')
        observe = Mock(side_effect=self.case.forbid); coordinator = self.coordinator(observe=observe)
        data = json.loads(self.row('robot_cycles')['data']); before = tuple(self.db.iterdump())
        for current in (fixture.NOW, fixture.NOW + 1, fixture.NOW + 59):
            with patch.object(recovery.time, 'time', return_value=current):
                self.assertEqual(recovery.received_screening_replacement_due(self.db, fixture.CYCLE, data), 0)
                self.assertEqual(recovery.resume_uncertain_screening(coordinator, fixture.DAY, {})['wait_reason'],
                    'NEUROBRO_WEB_PROCESSING_FAILED_RETRY_PENDING')
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.db.execute('UPDATE robot_settings SET enabled=0')
        with patch.object(recovery.time, 'time', return_value=fixture.NOW + 10):
            self.assertEqual(recovery.resume_uncertain_screening(coordinator, fixture.DAY, {})['wait_reason'], 'ROBOT_OFF')
        with patch.object(recovery.time, 'time', return_value=fixture.NOW + 60), \
                patch.object(recovery, '_resume_completed_stale_screening', return_value=None):
            self.assertEqual(recovery.received_screening_replacement_due(self.db, fixture.CYCLE, data), 1)
            self.assertIsNone(recovery.resume_uncertain_screening(coordinator, fixture.DAY, {}))
        with patch.object(recovery.time, 'time', return_value=fixture.NOW + 10), \
                patch.object(recovery, '_resume_completed_stale_screening', return_value=None):
            self.assertIsNone(recovery.resume_uncertain_screening(coordinator, '2026-11-06', {}))
        observe.assert_not_called(); coordinator.market.catalog.assert_not_called()

    def next_claim(self, index, *, now):
        c = self.case; self.operation = fixture.CYCLE + ':screening:' + str(index) + ':1'
        _, schema = screening_contract(1)
        c.body, encoded, c.request_hash = c.source.request_body(self.operation, 'Pilih coin pengganti setelah layanan pulih.', schema, c.context)
        c.payload = {**c.body, 'request_sha256': c.request_hash}
        data = json.loads(self.row('robot_cycles')['data'])
        self.db.execute("UPDATE robot_cycles SET state='NEEDS_REVIEW',data=?", (json.dumps(data),))
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
            (self.operation, fixture.CYCLE, 'SCREENING', None, 'NEEDS_REVIEW', None))
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            (self.operation, None, c.request_hash, 'NEEDS_REVIEW', now - 90, 1, None, 'NEUROBRO_BROWSER_OUTCOME_UNKNOWN'))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',
            (self.operation, c.source.BROWSER_PROVIDER, c.source.BROWSER_CONTRACT, c.request_hash, c.body['marker'], encoded))
        self.envelope = self.negative(operation=self.operation, observed=now - 11)

    def test_repeated_proven_provider_failures_back_off_and_success_resets_without_coin_or_question_cap(self):
        now = fixture.NOW
        for index, delay in enumerate((60, 120, 240, 480, 900, 900), start=9):
            if index > 9: self.next_claim(index, now=now)
            self.assertEqual(self.recover(now=now), 'NOEXEC')
            retirement = json.loads(self.row('screening_response_recoveries')['proof'])['retirement']
            self.assertEqual(retirement['retry_delay_seconds'], delay)
            self.assertEqual(retirement['failure_streak'], min(index - 8, 5))
            data = json.loads(self.row('robot_cycles')['data'])
            with patch.object(recovery.time, 'time', return_value=now + delay - 1):
                self.assertEqual(recovery.received_screening_replacement_due(self.db, fixture.CYCLE, data), 0)
            with patch.object(recovery.time, 'time', return_value=now + delay):
                self.assertEqual(recovery.received_screening_replacement_due(self.db, fixture.CYCLE, data), 1)
            now += delay + 100
        # An intervening genuine other/successful research job ends the outage
        # streak; the journal remains unchanged and does not cap further work.
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
            (fixture.CYCLE + ':analysis-v9:SUIUSDT', fixture.CYCLE, 'ANALYSIS', 'SUIUSDT', 'COMPLETE', '5'))
        self.next_claim(15, now=now)
        self.assertEqual(self.recover(now=now), 'NOEXEC')
        self.assertEqual(json.loads(self.row('screening_response_recoveries')['proof'])['retirement']['retry_delay_seconds'], 60)

    def test_resume_observes_negative_proof_without_binance_catalog_or_mini(self):
        coordinator = self.coordinator()
        with patch.object(recovery.time, 'time', return_value=fixture.NOW):
            result = recovery.resume_uncertain_screening(coordinator, fixture.DAY, {})
        self.assertEqual(result['wait_reason'], 'NEUROBRO_WEB_PROCESSING_FAILED_RETRY_PENDING')
        coordinator.neuro.observe_received_screening.assert_called_once_with(self.operation)
        coordinator.market.catalog.assert_not_called()
        self.assertEqual(self.row('api_requests')['state'], recovery.PROVIDER_ERROR_TERMINAL)
        self.assertIsNone(self.row('research_browser_responses'))

    def test_actual_observer_projection_is_compatible_with_worker_without_synthetic_assistant(self):
        from tests.test_native_terminal_screening_error import TerminalScreeningErrorTests
        browser = TerminalScreeningErrorTests('test_existing_observer_seals_negative_four_reads_without_assistant_or_completion')
        browser.setUp(); self.addCleanup(browser.doCleanups)
        payload = browser.payload; c = self.case
        c.body, encoded, c.request_hash = c.source.request_body(self.operation,
            payload['prompt'], payload['output_schema'], payload['context'])
        self.assertEqual(c.request_hash, payload['request_sha256'])
        c.payload = payload
        c.data['seen'] = payload['context']['excluded_symbols']
        self.db.execute('UPDATE robot_cycles SET data=?', (json.dumps(c.data),))
        self.db.execute('UPDATE api_requests SET body_hash=?', (c.request_hash,))
        self.db.execute('UPDATE research_browser_requests SET request_sha256=?,marker=?,request_body=?',
            (c.request_hash, c.body['marker'], encoded))
        browser.body = 'Earlier bound discussion\n' + browser.rendered + RETRY_TOOLBAR_SUFFIX
        result = browser.observe()
        self.assertEqual(result['observation']['room_initial_state'], 'BOUND')
        self.assertEqual(browser.body_reads, 4)
        now = result['observation']['finished_at'] + 1
        finances = c.finances()
        self.assertEqual(self.recover(result, now=now), 'NOEXEC')
        self.assertEqual(self.row('api_requests')['state'], recovery.PROVIDER_ERROR_TERMINAL)
        self.assertIsNone(self.row('api_requests')['output'])
        self.assertIsNone(self.row('research_browser_responses'))
        self.assertEqual(len(json.loads(self.row('screening_response_recoveries')['proof'])['observation']['rows']), 1)
        # An actual author reply that arrives after NOEXEC has no execution
        # rights. Re-observing it cannot replace the already sealed result.
        protected = tuple(self.db.iterdump())
        browser.c.raw.messages.append(dict(role='assistant', text='{"SYMBOLS":["SOLUSDT"]}',
            id='late-provider-answer', complete=True, attachments=[]))
        late = browser.observe()
        self.assertTrue(late['observation']['operation_scope']['reply_complete'])
        self.assertNotIn('screening_terminal_error', late['observation'])
        self.assertEqual(self.recover(late, now=late['observation']['finished_at'] + 1), 'NOEXEC')
        self.assertEqual(tuple(self.db.iterdump()), protected)
        self.assertEqual(c.finances(), finances)

    def test_native_coordinator_reads_terminal_research_and_next_replacement_without_touching_owned_position(self):
        self.db.execute("UPDATE order_intents SET state='POSITION_PROTECTED' WHERE id='accepted-inj'")
        self.db.execute("UPDATE robot_candidates SET status='POSITION_PROTECTED' WHERE symbol='INJUSDT'")
        finances = self.case.finances(); self.assertEqual(self.recover(), 'NOEXEC')
        native = fixture.native_module('worker._terminal_error_native_coordinator',
            fixture.ROOT / 'vps_native/worker/robot.py')
        coordinator = object.__new__(native.Coordinator); coordinator.db = self.db
        self.assertFalse(coordinator.unresolved_research())
        with self.assertRaises(Review):
            coordinator.completed_research(self.operation, fixture.CYCLE, 'SCREENING', None)
        data = json.loads(self.row('robot_cycles')['data'])
        with patch.object(recovery.time, 'time', return_value=fixture.NOW + 59):
            self.assertEqual(coordinator.replacement_count(data, [], fixture.CYCLE), 0)
        with patch.object(recovery.time, 'time', return_value=fixture.NOW + 60):
            self.assertEqual(coordinator.replacement_count(data, [], fixture.CYCLE), 1)
        self.assertEqual(self.case.finances(), finances)


if __name__ == '__main__': unittest.main()
