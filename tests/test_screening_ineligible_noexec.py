"""Known ineligible screening choices retire without executable or HOLD output."""
import copy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_generic_screening_response_recovery as generic
from worker import screening_response_recovery as recovery
from worker.core import Review
from worker.account_state import screening_contract


class IneligibleScreeningNoexecTests(unittest.TestCase):
    def setUp(self):
        self.case = generic.GenericScreeningResponseRecoveryTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        c = self.case
        self.cycle = generic.CYCLE
        self.operation = self.cycle + ':screening:9:2'
        _, schema = screening_contract(2)
        c.context = {**c.context, 'requested_count': 2}
        c.body, serialized, c.request_hash = c.source.request_body(self.operation,
            'Pilih dua coin pengganti aktif.', schema, c.context)
        c.payload = {**c.body, 'request_sha256': c.request_hash}
        c.created = generic.NOW - 20
        c.db.execute('UPDATE robot_jobs SET operation=? WHERE operation=?', (self.operation, generic.OP))
        c.db.execute('UPDATE api_requests SET operation=?,body_hash=?,created=? WHERE operation=?',
            (self.operation, c.request_hash, c.created, generic.OP))
        c.db.execute('UPDATE research_browser_requests SET operation=?,request_sha256=?,marker=?,request_body=? '
            'WHERE operation=?', (self.operation, c.request_hash, c.body['marker'], serialized, generic.OP))
        c.db.execute(recovery.RECEIVED_SCHEMA)
        c.db.executescript("CREATE TABLE robot_entry_receipts(id TEXT PRIMARY KEY,symbol TEXT,entry_day TEXT,confirmed_at REAL);"
            "INSERT INTO robot_entry_receipts VALUES('filled-before-close','INJUSDT','2026-11-05',1);")
        c.catalog = {'CRVUSDT': {}, 'INJUSDT': {}}
        c.provider._record_browser_response = c.forbid
        self.envelope = c.fixture(operation=self.operation,
            reply='Pilihan final: SUIUSDT dan CRVUSDT.', symbols=['SUIUSDT', 'CRVUSDT'])

    @property
    def db(self):
        return self.case.provider.db

    def row(self, table):
        column = 'id' if table == 'robot_cycles' else 'operation'
        value = self.cycle if column == 'id' else self.operation
        row = self.db.execute('SELECT * FROM ' + table + ' WHERE ' + column + '=?', (value,)).fetchone()
        return dict(row) if row else None

    def recover(self, envelope=None, *, catalog=None, now=generic.NOW, permitted=lambda: True):
        return recovery.recover_received_screening(self.case.provider,
            self.envelope if envelope is None else envelope,
            catalog=self.case.catalog if catalog is None else catalog, max_age=180,
            now=now, permitted=permitted)

    def changed(self, mutate):
        envelope = copy.deepcopy(self.envelope)
        mutate(envelope['observation'])
        for entry in envelope['observation']['review_receipts']:
            raw = recovery._wire(entry['value'], ascii=True)
            entry.update(sha256=recovery._sha(raw), bytes=len(raw.encode()))
        return self.case.seal(envelope)

    def assert_refused(self, envelope=None, **kwargs):
        before = tuple(self.db.iterdump())
        with self.assertRaises(Review):
            self.recover(envelope, **kwargs)
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')
        self.assertIsNone(self.row('research_browser_responses'))

    def assert_retired(self, *, age):
        self.case.created = generic.NOW - age
        self.db.execute('UPDATE api_requests SET created=? WHERE operation=?',
            (self.case.created, self.operation))
        finances = self.case.finances(); quota = tuple(self.db.execute('SELECT * FROM robot_entry_receipts')); changes = self.db.total_changes
        self.assertEqual(self.recover(), 'NOEXEC')
        self.assertEqual(self.db.total_changes - changes, 4)
        api = self.row('api_requests')
        self.assertEqual((api['state'], api['failure_code'], api['output'], api['attempts'], api['created']),
            (recovery.RECEIVED_INELIGIBLE_TERMINAL, 'INVALID_SCREENING_SYMBOL', None, 1, self.case.created))
        self.assertIsNone(self.row('research_browser_responses'))
        self.assertEqual(self.row('robot_jobs')['state'], 'COMPLETE')
        data = json.loads(self.row('robot_cycles')['data'])
        self.assertEqual(data, {**self.case.data, 'screen': 9, 'replacements': 9,
            'replacement_due': 2, 'seen': self.case.data['seen'] + ['SUIUSDT']})
        self.assertNotIn('CRVUSDT', data['seen'])
        self.assertEqual(self.case.finances(), finances)
        self.assertEqual(tuple(self.db.execute('SELECT * FROM robot_entry_receipts')), quota)
        self.assertEqual(recovery.received_screening_replacement_due(self.db, generic.CYCLE, data), 2)
        seal = json.loads(self.row('screening_response_recoveries')['proof'])
        retirement = seal['retirement']
        self.assertFalse(retirement['canonical_result'])
        self.assertEqual(retirement['kind'], recovery.INELIGIBLE_NOEXEC)
        self.assertEqual(retirement['selected_symbols'], ['SUIUSDT', 'CRVUSDT'])
        self.assertEqual(retirement['missing_symbols'], ['SUIUSDT'])
        self.assertEqual(retirement['catalog_keys'], ['CRVUSDT', 'INJUSDT'])
        self.assertEqual(retirement['catalog_keys_sha256'], recovery._digest(retirement['catalog_keys']))
        self.assertEqual(seal['observation'], self.envelope['observation'])
        before = tuple(self.db.iterdump()); changes = self.db.total_changes
        self.assertEqual(self.recover(catalog={}, now=generic.NOW + 86400), 'NOEXEC')
        self.assertEqual(self.db.total_changes, changes)
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_actual_ton_crv_source_fixture_retires_only_ineligible_ton_without_rewriting_reply(self):
        path = Path(__file__).parent / 'fixtures/neurobro_ineligible_screening_20261121.json'
        original_bytes = path.read_bytes(); actual = json.loads(original_bytes)
        old_operation = self.operation; old_cycle = self.cycle
        self.operation = actual['operation']; self.cycle = actual['job']['cycle']
        c = self.case; c.body = json.loads(actual['request']['request_body'])
        c.payload = {**c.body, 'request_sha256': actual['request_sha256']}
        c.request_hash = actual['request_sha256']; c.created = actual['api']['created']
        # Only these coordinator rows are synthetic; the archived request,
        # assistant text, source receipt, Mini DONE and their hashes are original.
        c.data = dict(target=2, queue=[], seen=[], screen=-1, replacements=0, round_symbols=[])
        self.db.execute('UPDATE robot_cycles SET id=?,day=?,entry_epoch=0,state=?,data=? WHERE id=?',
            (self.cycle, self.operation[:10], 'NEEDS_REVIEW', json.dumps(c.data), old_cycle))
        self.db.execute('UPDATE robot_jobs SET operation=?,cycle=? WHERE operation=?',
            (self.operation, self.cycle, old_operation))
        api = actual['api']
        self.db.execute('UPDATE api_requests SET operation=?,idempotency=?,body_hash=?,state=?,created=?,attempts=?,output=?,failure_code=? WHERE operation=?',
            tuple(api[key] for key in ('operation','idempotency','body_hash','state','created','attempts','output','failure_code')) + (old_operation,))
        request = actual['request']
        self.db.execute('UPDATE research_browser_requests SET operation=?,provider=?,contract_version=?,request_sha256=?,marker=?,request_body=? WHERE operation=?',
            tuple(request[key] for key in ('operation','provider','contract_version','request_sha256','marker','request_body')) + (old_operation,))
        self.envelope = copy.deepcopy(actual['envelope'])
        now = self.envelope['observation']['finished_at'] + 10
        finances = c.finances(); quota = tuple(self.db.execute('SELECT * FROM robot_entry_receipts')); changes = self.db.total_changes
        self.assertEqual(self.recover(catalog={'CRVUSDT': {}, 'BTCUSDT': {}}, now=now), 'NOEXEC')
        self.assertEqual(self.db.total_changes - changes, 4)
        self.assertEqual(self.row('api_requests')['failure_code'], 'INVALID_SCREENING_SYMBOL')
        self.assertIsNone(self.row('api_requests')['output'])
        self.assertIsNone(self.row('research_browser_responses'))
        current = json.loads(self.row('robot_cycles')['data'])
        self.assertEqual((current['seen'], current['replacement_due'], current['queue']), (['TONUSDT'], 2, []))
        self.assertEqual(recovery.received_screening_replacement_due(self.db, self.cycle, current), 2)
        seal = json.loads(self.row('screening_response_recoveries')['proof'])
        self.assertEqual(seal['observation'], actual['envelope']['observation'])
        self.assertEqual(seal['retirement']['selected_symbols'], ['TONUSDT','CRVUSDT'])
        self.assertEqual(c.finances(), finances)
        self.assertEqual(tuple(self.db.execute('SELECT * FROM robot_entry_receipts')), quota)
        before = tuple(self.db.iterdump()); changes = self.db.total_changes
        self.assertEqual(self.recover(catalog={}, now=now + 86400), 'NOEXEC')
        self.assertEqual(self.db.total_changes, changes)
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_fresh_request_retires_known_ineligible_choice_without_waiting_for_expiry(self):
        self.assert_retired(age=20)

    def test_stale_request_with_fresh_observation_retains_same_noncanonical_rejection(self):
        self.assert_retired(age=390)

    def test_only_missing_symbol_is_appended_without_rewriting_historical_seen_entries(self):
        self.case.data['seen'] = self.case.data['seen'] + ['AVAXUSDT']
        self.db.execute('UPDATE robot_cycles SET data=? WHERE id=?', (json.dumps(self.case.data), self.cycle))
        self.assert_retired(age=20)

    def test_restart_reuses_seal_without_catalog_or_journal_writes(self):
        self.assert_retired(age=20)
        restarted = sqlite3.connect(':memory:', isolation_level=None)
        restarted.row_factory = sqlite3.Row
        self.addCleanup(restarted.close); self.db.backup(restarted)
        old = self.case.provider.db
        self.case.provider.db = restarted
        try:
            before = tuple(restarted.iterdump())
            self.assertEqual(self.recover(catalog={}, now=generic.NOW+172800), 'NOEXEC')
            self.assertEqual(tuple(restarted.iterdump()), before)
            self.assertEqual(restarted.total_changes, 0)
        finally:
            self.case.provider.db = old

    def test_existing_public_validator_still_requires_live_catalog_for_executable_output(self):
        with self.assertRaisesRegex(Review, '^INVALID_SCREENING_SYMBOL$'):
            recovery.validate_received_screening(self.case.provider, self.envelope, self.case.payload,
                self.case.created, catalog=self.case.catalog, max_age=180, now=generic.NOW)
        result, _, stale = recovery.validate_received_screening(self.case.provider, self.envelope,
            self.case.payload, self.case.created, catalog={**self.case.catalog, 'SUIUSDT': {}},
            max_age=180, now=generic.NOW)
        self.assertEqual(result['output'], {'symbols': ['SUIUSDT', 'CRVUSDT']})
        self.assertFalse(stale)

    def test_unknown_or_ambiguous_source_cannot_become_known_ineligible(self):
        for mutate in (
            lambda p: p.update(review_receipts=[]),
            lambda p: p['operation_scope'].update(unmatched_intents=['unknown-followup']),
            lambda p: p['rows'][-1].update(complete=False),
            lambda p: p['review_receipts'][0]['value']['model_operation'].update(state='STARTED'),
        ):
            with self.subTest(mutate=mutate):
                self.assert_refused(self.changed(mutate))

    def test_expired_first_observation_and_empty_catalog_are_not_bypassed(self):
        self.assert_refused(catalog={})
        self.case.created = generic.NOW - 390
        self.db.execute('UPDATE api_requests SET created=? WHERE operation=?',
            (self.case.created, self.operation))
        self.assert_refused(self.changed(lambda p: p.update(observed_at=generic.NOW-131,
            finished_at=generic.NOW-130)))

    def test_clarification_hold_and_excluded_coin_are_not_reclassified(self):
        for reply, symbols, action, needs_followup in (
            ('COIN: SUIUSDT\nCOIN: CRVUSDT', ['SUIUSDT', 'CRVUSDT'], 'CLARIFY', True),
            ('COIN PILIHAN:\nSUIUSDT Perpetual - HOLD, tunggu struktur.\nCRVUSDT Perpetual - HOLD, tunggu momentum.',
                ['SUIUSDT', 'CRVUSDT'], 'SCREENING', False),
            ('Pilihan final: AVAXUSDT dan CRVUSDT.', ['AVAXUSDT', 'CRVUSDT'], 'SCREENING', False),
        ):
            envelope = self.case.fixture(operation=self.operation, reply=reply, symbols=symbols, action=action)
            if needs_followup:
                value = envelope['observation']['review_receipts'][0]['value']
                value['response']['needs_followup'] = True
                value['response']['decision']['question'] = 'Coin mana yang dipilih?'
                value['model_operation']['result'] = recovery._wire(value['response'], ascii=True)
                raw = recovery._wire(value, ascii=True)
                envelope['observation']['review_receipts'][0].update(sha256=recovery._sha(raw), bytes=len(raw.encode()))
                self.case.seal(envelope)
            with self.subTest(action=action, symbols=symbols, reply=reply):
                self.assert_refused(envelope)

    def test_owner_pause_and_accepted_intent_race_leave_original_claim_untouched(self):
        calls = []
        def permitted():
            calls.append(True)
            return len(calls) == 1
        self.assert_refused(permitted=permitted)
        original = recovery._ineligible_cycle_data
        def accepted(before, **kwargs):
            data = original(before, **kwargs)
            self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?)',
                ('accepted-screening-race', self.operation, 'SUIUSDT', 'SUBMITTED', 'immutable accepted intent'))
            return data
        with patch.object(recovery, '_ineligible_cycle_data', accepted), self.assertRaises(Review):
            self.recover()
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')
        self.assertIsNone(self.row('screening_response_recoveries'))
        self.assertEqual(self.db.execute("SELECT payload FROM order_intents WHERE id='accepted-screening-race'").fetchone()[0],
            'immutable accepted intent')

    def test_seal_tampering_is_rejected_and_replacement_due_is_not_faked(self):
        self.assert_retired(age=20)
        journal = self.row('screening_response_recoveries')
        for mutate in (
            lambda r: r.update(missing_symbols=[]),
            lambda r: r.update(catalog_keys=['CRVUSDT', 'INJUSDT', 'SUIUSDT'],
                catalog_keys_sha256=recovery._digest(['CRVUSDT', 'INJUSDT', 'SUIUSDT'])),
            lambda r: r.update(catalog_keys_sha256='0'*64),
            lambda r: r.update(preimage_sha256='0'*64),
        ):
            proof = json.loads(journal['proof']); mutate(proof['retirement']); raw = recovery._wire(proof)
            self.db.execute('UPDATE screening_response_recoveries SET proof=?,proof_sha256=? WHERE operation=?',
                (raw, recovery._sha(raw), self.operation))
            with self.subTest(mutate=mutate), self.assertRaises(Review):
                self.recover()
        self.db.execute('UPDATE screening_response_recoveries SET proof=?,proof_sha256=? WHERE operation=?',
            (journal['proof'], journal['proof_sha256'], self.operation))
        self.db.execute("UPDATE api_requests SET failure_code='OTHER_ERROR' WHERE operation=?", (self.operation,))
        with self.assertRaises(Review):
            self.recover()

    def test_replacement_due_revalidates_seen_and_does_not_rewind_cycle_progress(self):
        self.assert_retired(age=20)
        data = json.loads(self.row('robot_cycles')['data'])
        self.assertEqual(recovery.received_screening_replacement_due(self.db, generic.CYCLE,
            {**data, 'seen': self.case.data['seen']}), 0)
        advanced = {**data, 'screen': 10, 'queue': ['CRVUSDT']}
        self.db.execute('UPDATE robot_cycles SET data=? WHERE id=?', (json.dumps(advanced), generic.CYCLE))
        before = tuple(self.db.iterdump())
        self.assertEqual(self.recover(catalog={}), 'NOEXEC')
        self.assertEqual(recovery.received_screening_replacement_due(self.db, generic.CYCLE, advanced), 0)
        self.assertEqual(tuple(self.db.iterdump()), before)


if __name__ == '__main__':
    unittest.main()
