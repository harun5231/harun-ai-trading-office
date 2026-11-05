"""Exact retained/current evidence reconciles only regenerated assistant IDs."""
import ast
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agent.native_daily_room import DailyRoom, JOURNAL_NAME, identities
from agent.native_screening_recovery import (canonical, digest, read_receipt, recover,
    validate_receipt)
from agent import native_screening_identity as identity
from tests import test_native_screening_received_recovery as recovery_tests

FIXTURE = Path(__file__).parent / 'fixtures' / 'native_screening_assistant_identity_20261105.json'


class NativeScreeningIdentityTests(unittest.TestCase):
    def setUp(self):
        self.case = recovery_tests.ScreeningReceivedRecoveryTests('test_native_stable_read_keeps_journal_and_durable_receipt_idempotent')
        self.case.setUp(); self.addCleanup(self.case.doCleanups)
        self.saved = json.loads(FIXTURE.read_text())
        c = self.case; c.payload = self.saved['payload']; c.raw.messages = copy.deepcopy(self.saved['rows'])
        room = self.saved['room']
        binding = {key: room[key] for key in ('first_prompt_sha256', 'marker', 'history_titles')}
        c.bind_room(day=room['day'], room_id=room['room_id'], url=room['url'],
            anchors=json.dumps(room['anchors']), binding=json.dumps(binding), updated_at=room['updated_at'])
        # Restore the exact runtime cache receipt in this isolated private directory.
        cache = dict(schema_version=1, operation=c.payload['operation'],
            payload_sha256=identity.digest(c.payload, native=True), model_request_id='f555395a6d3130aee060edce866b8b1f3a2d8f322c7869dbe873f506193e4604',
            rendered_prompt=self.saved['rendered_prompt'], rendered_sha256=identity.text_sha(self.saved['rendered_prompt']))
        (c.root / c.cache._name(c.payload)).write_text(json.dumps(cache))
        self.before = (c.root / JOURNAL_NAME).read_bytes()
        self.request = dict(payload=c.payload, identity_evidence=copy.deepcopy(self.saved['evidence']))

    def invoke(self, request=None):
        c = self.case
        with c.actor.claim:
            return recover(c.actor, c.delegated, self.request if request is None else request)

    def journal(self):
        import sqlite3
        with sqlite3.connect(self.case.root / JOURNAL_NAME) as db:
            db.row_factory = sqlite3.Row
            row = dict(db.execute('SELECT * FROM daily_rooms').fetchone())
        return row, json.loads(row['binding'])

    def test_real_same_texts_regenerated_ids_gain_explicit_durable_alias(self):
        result = self.invoke(); c = self.case
        self.assertEqual(result['output'], {'symbols': ['RUNEUSDT', 'TIAUSDT']})
        proof = read_receipt(c.root, c.payload)
        migration = proof['identity_reconciliation']; alias = migration['alias']
        self.assertEqual(alias['original_request_id'], '72a5a5eba8004b41f4132f8a9eb8b559c440f37704efaca5b8ca8b36a5f20d64')
        self.assertEqual(alias['current_request_id'], '44083c7c0e93bfcc40a90ead2b7f118e3efd09afd5b349ef08ec408f948fe7a0')
        self.assertNotEqual(alias['original_anchors'][1::2], alias['observed_anchors'][1::2])
        self.assertEqual(alias['original_anchors'][::2], alias['observed_anchors'][::2])
        self.assertEqual(migration['receipt']['observed_rows'], self.saved['rows'])
        self.assertEqual(alias['receipt_sha256'], digest(migration['receipt']))
        stored = (c.root / alias['receipt_name']).read_bytes()
        self.assertEqual(stored, canonical(migration['receipt']))
        row, binding = self.journal()
        self.assertEqual(json.loads(row['anchors']), self.saved['room']['anchors'])
        self.assertEqual(row['updated_at'], self.saved['room']['updated_at'])
        self.assertEqual(binding['identifier_reconciliation'], alias)
        self.assertEqual(row['room_id'], self.saved['room']['room_id'])
        self.assertEqual(validate_receipt(proof, c.payload), proof)
        self.assertTrue(DailyRoom.matches(c.raw.messages, json.loads(row['anchors']), binding,
                                       'BOUND', archive_root=c.root))
        self.assertTrue(DailyRoom.matches(c.raw.messages[-2:], json.loads(row['anchors']), binding,
                                       'BOUND', archive_root=c.root))
        for change in ('id', 'text', 'binding', 'alias'):
            rows = copy.deepcopy(c.raw.messages); changed_binding = copy.deepcopy(binding)
            if change == 'id': rows[-1]['id'] = 'unrelated-native-id'
            if change == 'text': rows[-1]['text'] += ' altered'
            if change == 'binding': changed_binding['marker'] = 'HARUN_' + '0'*32
            if change == 'alias': changed_binding['identifier_reconciliation']['original_request_id'] = '0'*64
            self.assertFalse(DailyRoom.matches(rows, json.loads(row['anchors']), changed_binding,
                                             'BOUND', archive_root=c.root))

    def test_home_restore_checks_attested_changed_ids_before_selecting_existing_room(self):
        c = self.case
        c.room.history.rooms = [('Pilih 2 Coin Binance Futures', copy.deepcopy(self.saved['rows']))]
        c.raw.messages = []
        result = self.invoke()
        self.assertEqual(result['output'], {'symbols': ['RUNEUSDT', 'TIAUSDT']})
        self.assertEqual(c.room.history.selected, ['Pilih 2 Coin Binance Futures'])
        proof = read_receipt(c.root, c.payload)
        self.assertTrue(proof['history_restored'])
        self.assertIsNotNone(proof['identity_reconciliation'])
        row, _ = self.journal()
        self.assertEqual(json.loads(row['anchors']), self.saved['room']['anchors'])
        self.assertEqual(row['updated_at'], self.saved['room']['updated_at'])

    def test_mismatched_reply_or_unattested_metadata_cannot_change_binding(self):
        c = self.case
        for change in ('reply', 'user', 'metadata-key', 'raw-phase', 'network-unknown',
                       'retained-current', 'source-custody', 'mini-not-done', 'foreign-room'):
            with self.subTest(change=change):
                request = copy.deepcopy(self.request); c.raw.messages = copy.deepcopy(self.saved['rows'])
                evidence = request['identity_evidence']
                if change == 'reply': c.raw.messages[-1]['text'] += ' altered'
                if change == 'user': c.raw.messages[2]['text'] += ' altered'
                if change == 'metadata-key':
                    for source in ('retained_agent', 'current_agent'):
                        evidence[source]['raw_receipts'][1]['key'] = 'dialogue-model-raw:' + '0'*64
                if change == 'raw-phase':
                    for source in ('retained_agent', 'current_agent'):
                        raw = evidence[source]['raw_receipts'][1]
                        raw['value']['phase'] = 'ANALYSIS'
                        raw['key'] = 'dialogue-model-raw:' + identity.digest({k:v for k,v in raw['value'].items()
                            if k != 'observed_at'}, native=True)
                if change == 'network-unknown':
                    for source in ('retained_agent', 'current_agent'):
                        evidence[source]['final_mini_unknown']['updated_at'] += 10
                if change == 'retained-current': evidence['current_agent']['first_mini_done']['request_hash'] = '0'*64
                if change == 'source-custody': evidence['custody']['request_id'] = 'other-untrusted-capture'
                if change == 'mini-not-done':
                    for source in ('retained_agent', 'current_agent'):
                        evidence[source]['first_mini_done']['state'] = 'UNKNOWN'
                if change == 'foreign-room': evidence['room_id'] = 'f'*32
                with self.assertRaises(ValueError): self.invoke(request)
                self.assertIsNone(read_receipt(c.root, c.payload))
                self.assertFalse(list(c.root.glob('screening-identifier-reconciliation-*.json')))
                self.assertEqual((c.root / JOURNAL_NAME).read_bytes(), self.before)

    def test_crash_after_annotation_recovers_from_sealed_receipt_without_new_dto(self):
        c = self.case
        with patch('agent.native_screening_recovery._save_receipt', side_effect=RuntimeError('controlled handoff crash')):
            with self.assertRaises(RuntimeError): self.invoke()
        self.assertIsNone(read_receipt(c.root, c.payload))
        row, binding = self.journal(); alias = binding['identifier_reconciliation']
        identity_bytes = (c.root / alias['receipt_name']).read_bytes()
        result = self.invoke(c.payload)
        self.assertEqual(result['output'], {'symbols': ['RUNEUSDT', 'TIAUSDT']})
        self.assertEqual((c.root / alias['receipt_name']).read_bytes(), identity_bytes)
        self.assertEqual(self.journal()[0], row)
        self.assertIsNotNone(read_receipt(c.root, c.payload))

    def test_ordinary_checkpoint_can_advance_native_anchors_and_retain_original_alias(self):
        self.invoke(); c = self.case
        row, binding = self.journal()
        rows = c.raw.messages + [dict(role='user', text='New analysis request', attachments=[]),
            dict(role='assistant', text='Current new analysis', complete=True, id='next-native-reply')]
        advanced = identities(rows)
        self.assertTrue(DailyRoom.matches(rows, advanced, binding, 'BOUND', archive_root=c.root))
        self.assertEqual(binding['identifier_reconciliation']['original_anchors'], self.saved['room']['anchors'])
        wrong = advanced[:]; wrong[0] = '0'*64
        self.assertFalse(DailyRoom.matches(rows, wrong, binding, 'BOUND', archive_root=c.root))

    def test_shared_identity_validator_has_only_standard_library_imports(self):
        tree = ast.parse(Path(identity.__file__).read_text())
        imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertLessEqual(imported, {'__future__', 'copy', 'hashlib', 'json', 'math', 're'})


if __name__ == '__main__': unittest.main()
