"""Initial native four-file custody; offline records are not runtime proof."""
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent.native_analysis_material import retain_prepared, read_prepared
from agent.native_analysis_receipt import persist_review, read_reviews
from agent.native_chart_followup import canonical, sha
from agent.native_market_context import convert_native_market_context
from tests import test_trade_evaluation_archive as archive_fixtures
from worker import trade_evaluation_archive as archive


def text_sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class InitialEvaluationCustodyTests(unittest.TestCase):
    def setUp(self):
        # Reuse the genuine byte/capture fixture through composition. Its
        # follow-up tests are deliberately not inherited or run a second time.
        self.f = archive_fixtures.HistoricalArchiveCustodyTests(methodName='runTest')
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        f = self.f
        for path in (Path(f.root) / 'dialogue-200-followup').iterdir():
            path.unlink()
        symbol = f.context['symbol']
        f.payload.update(provider='NEUROBRO_WEB', contract_version='neurobro-browser-decision-v2',
            marker='HARUN_' + text_sha(f.payload['operation'])[:32],
            output_schema=dict(required=['symbol', 'side', 'limit_entry', 'take_profit',
                'stop_loss', 'risk_reward']))
        request_body = json.dumps(f.payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        f.request = dict(operation=f.payload['operation'], provider=f.payload['provider'],
            contract_version=f.payload['contract_version'], marker=f.payload['marker'],
            request_body=request_body, request_sha256=text_sha(request_body))
        f.wire = dict(f.payload, request_sha256=f.request['request_sha256'])
        f.capture = f.native.capture(f.native.context, f.context, f.payload['marker'],
            f.request['request_sha256'], archive_root=f.root)
        f.charts = [dict(row, attachment_id='native-' + row['timeframe'])
            for row in f.capture['evidence']]
        f.names = [f.payload['marker'] + '-' + row['timeframe'] + '-' + row['sha256'][:12] + '.png'
            for row in f.charts] + [row['name'] for row in f.csvs]
        self.captured_at = max(datetime.fromisoformat(row['captured_at']).timestamp() for row in f.charts)
        self.reply = ('COIN: ' + symbol + '\nARAH: LONG\nENTRY: 100\nTP: 102\nSL: 99\nRR: 1:2')
        output = dict(symbol=symbol, side='LONG', limit_entry=100, take_profit=102,
            stop_loss=99, risk_reward=2)
        f.response = dict(operation=f.payload['operation'], response_id='original-native-response',
            output_sha256=text_sha(json.dumps(output, sort_keys=True, separators=(',', ':'))),
            assistant_text=self.reply, assistant_sha256=text_sha(self.reply),
            chart_evidence=json.dumps(f.charts, sort_keys=True, separators=(',', ':')),
            received_at=self.captured_at + 10, chart_max_age=180)
        f.index = archive.record_capture_lookup(f.root, f.capture, f.wire)
        self.rendered_prompt = 'Bahan asli untuk ' + symbol + '.\n' + f.payload['marker']
        retain_prepared(f.root, f.wire, f.charts, f.names, f.csvs, self.rendered_prompt,
            observed_at=self.captured_at + 2)
        self.prepared = read_prepared(f.root, f.wire)[0]
        self.cache = dict(schema_version=1, operation=f.payload['operation'], payload_sha256=sha(f.wire),
            model_request_id='1' * 64, rendered_prompt=self.rendered_prompt,
            rendered_sha256=text_sha(self.rendered_prompt))
        self.cache_path = Path(f.root) / ('mini-prompt-' + sha(f.wire) + '.json')
        self.write(self.cache_path, self.cache)
        self.rows = [dict(role='user', text=self.rendered_prompt, attachments=f.names),
            dict(role='assistant', text=self.reply, complete=True, id='actual-initial-reply')]
        request_id = sha(dict(operation=f.payload['operation'], request_sha256=f.request['request_sha256'],
            reply_sha256=text_sha(self.reply), phase='ANALYSIS', transcript_sha256=sha(self.rows)))
        request = dict(schema_version=1, request_id=request_id,
            conversation_id='neurobro:2026-11-05:' + '2' * 32, phase='ANALYSIS', reply=self.reply,
            reply_sha256=text_sha(self.reply), market_context=convert_native_market_context(
                f.context, now=f.context['fetched_at']),
            gate=dict(robot_on=True, native_dispatch_verified=True), deadline_epoch=self.captured_at + 90)
        response = dict(schema_version=1, ok=True, request_id=request_id,
            conversation_id=request['conversation_id'], phase='ANALYSIS', reply_sha256=text_sha(self.reply),
            authority='DIALOGUE_CONTROL_ONLY', trading_authority=False,
            decision=dict(action='COMPLETE', symbols=[symbol], question=''),
            needs_followup=False, reason='DIALOGUE_DECISION')
        persist_review(f.root, f.wire, request, response, self.rows, context=f.context,
            observed_at=self.captured_at + 4)
        self.review = read_reviews(f.root, f.wire)[0]

    @staticmethod
    def write(path, value):
        path.write_bytes(canonical(value))
        path.chmod(0o600)

    def replace_prepared(self, mutate):
        path = Path(self.f.root) / self.prepared['name']
        value = copy.deepcopy(self.prepared['value'])
        mutate(value)
        path.unlink()
        name = self.prepared['name'][:self.prepared['name'].rfind('-') + 1] + sha(value) + '.json'
        self.write(path.with_name(name), value)

    def snapshot(self):
        return {str(path.relative_to(self.f.root)): path.read_bytes()
            for path in Path(self.f.root).rglob('*') if path.is_file()}

    def test_initial_records_bind_four_original_files_with_honest_typed_custody(self):
        result = self.f.read()
        witness = result['delivery']
        self.assertEqual(witness['kind'], 'NATIVE_INITIAL_ANALYSIS_REVIEW_CUSTODY_V1')
        self.assertNotIn('native_intent_sha256', witness)
        self.assertNotIn('native_completion_sha256', witness)
        self.assertEqual(witness['prepared_material_sha256'], self.prepared['sha256'])
        self.assertEqual(witness['native_review_sha256'], self.review['sha256'])
        self.assertEqual(witness['assistant_sha256'], text_sha(self.reply))
        self.assertEqual(witness['csv_sha256'], {row['timeframe']: text_sha(row['buffer'].decode())
            for row in self.f.csvs})
        self.assertEqual(len(result['files']), 4)
        self.assertEqual(self.prepared['value']['state'], 'PREPARED_NOT_SUBMITTED')
        self.assertFalse(self.prepared['value']['send_attempted'])

    def test_bound_initial_material_view_keeps_actual_original_receipts(self):
        manifest = self.f.manifest()
        turn = archive.read_bound_material_turn(self.f.root, manifest)
        material = turn['material']
        self.assertEqual(material['attachment_names'], self.f.names)
        self.assertEqual(material['chart_evidence'], self.f.charts)
        self.assertEqual(material['prompt_sha256'], text_sha(self.rendered_prompt))
        self.assertEqual(material['context_sha256'], sha(self.f.context))
        self.assertNotIn('intent', turn)
        self.assertNotIn('completion', turn)
        self.assertEqual(turn['prepared'], self.prepared['value'])
        self.assertEqual(turn['review'], self.review['value'])
        self.assertEqual(turn['prompt_cache'], self.cache)
        self.assertNotIn('sent_this_request', turn)

    def test_cross_room_material_files_reuse_exact_historical_bytes_and_times(self):
        manifest = self.f.manifest()
        with patch('time.time', return_value=4_000_000_000), patch(
                'agent.native_chart_followup.public_snapshot', side_effect=AssertionError('NO_FRESH_MARKET')):
            result = archive.read_bound_material_files(self.f.root, manifest,
                charts=SimpleNamespace(**self.f.native.namespace))
        self.assertEqual(result['paths'], self.f.capture['paths'])
        self.assertEqual(result['observed_at'], self.f.context['fetched_at'])
        self.assertEqual([row['buffer'] for row in result['pngs']],
            [Path(path).read_bytes() for path in self.f.capture['paths']])
        self.assertEqual([row['buffer'] for row in result['csvs']], [row['buffer'] for row in self.f.csvs])
        self.assertEqual([row['name'] for row in result['pngs'] + result['csvs']], self.f.names)

    def test_repeated_initial_reads_leave_every_retained_byte_unchanged(self):
        before = self.snapshot()
        manifest = self.f.manifest()
        self.assertEqual(self.f.manifest(), manifest)
        archive.read_bound_material_turn(self.f.root, manifest)
        archive.read_bound_material_files(self.f.root, manifest,
            charts=SimpleNamespace(**self.f.native.namespace))
        self.assertEqual(self.snapshot(), before)

    def test_missing_prepared_review_or_prompt_cache_never_binds_initial_delivery(self):
        for path in (Path(self.f.root) / self.prepared['name'],
                Path(self.f.root) / self.review['name'], self.cache_path):
            with self.subTest(receipt=path.name):
                raw = path.read_bytes()
                path.unlink()
                try:
                    with self.assertRaises(ValueError):
                        self.f.read()
                finally:
                    path.write_bytes(raw)
                    path.chmod(0o600)

    def test_changed_response_identity_reply_or_png_upload_id_is_rejected(self):
        original = copy.deepcopy(self.f.response)
        changes = (
            lambda row: row.update(operation='foreign-operation'),
            lambda row: row.update(assistant_text=self.reply + '\nCHANGED'),
            lambda row: row.update(assistant_sha256='9' * 64),
            lambda row: row.update(received_at=self.captured_at + 3),
            lambda row: row.update(chart_evidence=json.dumps([
                dict(self.f.charts[0], attachment_id='different-native-upload'), self.f.charts[1]])),
        )
        for mutate in changes:
            with self.subTest(change=mutate):
                self.f.response = copy.deepcopy(original)
                mutate(self.f.response)
                with self.assertRaises(ValueError):
                    self.f.read()
        self.f.response = original

    def test_changed_prepared_csv_hash_is_not_saved_file_delivery_proof(self):
        self.replace_prepared(lambda value: value['csv_receipts'][0].update(
            sha256=value['csv_receipts'][0]['sha256'][:8] + '9' * 56))
        with self.assertRaises(ValueError):
            self.f.read()

    def test_prepared_and_cached_prompt_mismatch_is_rejected(self):
        changed = dict(self.cache, rendered_prompt=self.rendered_prompt + '\nCHANGED')
        changed['rendered_sha256'] = text_sha(changed['rendered_prompt'])
        self.write(self.cache_path, changed)
        with self.assertRaises(ValueError):
            self.f.read()

    def test_partial_or_corrupt_owned_followup_cannot_fall_back_to_initial(self):
        self.f.record(self.f.intent(), complete=False)
        with self.assertRaisesRegex(ValueError, 'CHAIN_INCOMPLETE'):
            self.f.read()
        directory = Path(self.f.root) / 'dialogue-200-followup'
        for path in directory.iterdir():
            path.unlink()
        key = sha(dict(operation=self.f.payload['operation'], request_sha256=self.f.request['request_sha256']))
        self.write(directory / (key + '.unrecognized.json'), {'not': 'a native receipt'})
        with self.assertRaisesRegex(ValueError, 'CHAIN_INVALID'):
            self.f.read()

    def test_bound_initial_manifest_rejects_changed_review_or_later_unknown_followup(self):
        manifest = self.f.manifest()
        changed = copy.deepcopy(manifest)
        changed['delivery_custody']['native_review_sha256'] = '9' * 64
        with self.assertRaises(ValueError):
            archive.read_bound_material_turn(self.f.root, changed)
        self.f.record(self.f.intent(), complete=False)
        with self.assertRaisesRegex(ValueError, 'CHAIN_INCOMPLETE'):
            archive.read_bound_material_turn(self.f.root, manifest)

    def test_same_room_witness_checks_actual_initial_prompt_and_csv_tiles(self):
        from agent.native_evaluation_browser import HistoricalEvaluation
        manifest = self.f.manifest()
        png_names, csv_names = self.f.names[:2], self.f.names[2:]
        rows = [dict(role='user', text=self.rendered_prompt, attachments=png_names),
            dict(role='assistant', text=self.reply, complete=True)]
        tile = Mock()
        tile.evaluate.return_value = [dict(name=name, ready=True, matches=1) for name in csv_names]
        bridge = SimpleNamespace(uploaded_names=['unrelated-current.png'])
        room = SimpleNamespace(store=SimpleNamespace(root=self.f.root), full_turns=lambda: copy.deepcopy(rows),
            original_visible=lambda _: [tile, Mock()])
        owner = SimpleNamespace(daily_room=room, bridge=bridge,
            native=SimpleNamespace(MESSAGE='native-message', USER_BODY='native-user-body'))
        historical = HistoricalEvaluation.__new__(HistoricalEvaluation)
        historical.owner = owner
        historical.request = dict(historical_materials=[manifest],
            source_neurobro_replies=[dict(text=self.reply, sha256=text_sha(self.reply))])
        witness = historical._turn_witness(0)
        self.assertEqual(witness['attachment_names'], self.f.names)
        self.assertEqual(witness['bound_charts'], self.f.charts)
        self.assertEqual(witness['native_review_sha256'], self.review['sha256'])
        self.assertNotIn('native_completion_sha256', witness)
        self.assertEqual(bridge.uploaded_names, ['unrelated-current.png'])
        from agent.native_evaluation_protocol import validate_material_access_chain, native_sha
        from worker.trade_evaluation_evidence import digest
        final_sha='3'*64
        send=dict(action='SEND_TEXT',response_id_kind='NATIVE_UI_MESSAGE_ID',
            material_sha256=None,rendered_prompt_sha256='4'*64,bound_charts=[],attachment_names=[],
            reused_materials=[witness])
        record=dict(intent=send,complete=dict(send,intent_sha256=native_sha(send),
            response_id='actual-evaluation-ui-reply',final_reply_sha256=final_sha))
        final=dict(action='FINAL_ASSESSMENT',response_id_kind='NATIVE_FINAL_RESPONSE_ID',
            source_chain_sha256=digest([record]),final_ui_message_id='actual-evaluation-ui-reply',
            final_ui_reply_sha256=final_sha)
        terminal=dict(intent=final,complete=dict(final,intent_sha256=native_sha(final),
            response_id='actual-evaluation-final',final_reply_sha256=final_sha))
        request=dict(historical_materials=[manifest])
        response=dict(response_id='actual-evaluation-final',assistant_sha256=final_sha)
        self.assertEqual(validate_material_access_chain(request,[record,terminal],[],final_response=response)
            ['materials'],{digest(manifest):'REUSED'})
        # A fake legacy completion field cannot substitute for the typed actual review.
        bad=copy.deepcopy(record)
        bad['intent']['reused_materials'][0]['native_completion_sha256']=bad['intent']['reused_materials'][0].pop('native_review_sha256')
        bad['complete']=dict(bad['intent'],intent_sha256=native_sha(bad['intent']),
            response_id='actual-evaluation-ui-reply',final_reply_sha256=final_sha)
        with self.assertRaisesRegex(ValueError,'REUSED_MATERIAL_MISMATCH'):
            validate_material_access_chain(request,[bad,terminal],[],final_response=response)
        tile.evaluate.return_value[0]['ready'] = False
        with self.assertRaisesRegex(ValueError, 'FILES_NOT_READABLE_IN_ROOM'):
            historical._turn_witness(0)
        self.assertEqual(bridge.uploaded_names, ['unrelated-current.png'])


if __name__ == '__main__':
    unittest.main()
