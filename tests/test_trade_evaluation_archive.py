"""Existing private receipt/capture formats only; no runtime/provider proof."""
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from agent.native_chart_followup import ReceiptStore, sha
from agent.native_chart_capture import WEBSITE_SIDECAR
from agent.native_data_files import deliver_market_data_files
from agent.native_market_context import convert_native_market_context
from tests import test_binance_website_chart as website_fixtures
from worker import trade_evaluation_archive as archive
from worker import trade_evaluation_evidence as evidence


class HistoricalArchiveCustodyTests(unittest.TestCase):
    def setUp(self):
        self.native = website_fixtures.WebsiteCaptureFixtureTests()
        self.native.setUp()
        self.addCleanup(self.native.tearDown)
        self.root = self.native.root
        self.context = self.native.native_context
        self.payload = dict(operation='original-owned-analysis', marker='HARUN_TEST',
            context=self.context, prompt='Bahan asli', output_schema={'required': ['symbol']})
        self.request = dict(request_body=json.dumps(self.payload, sort_keys=True,
            separators=(',', ':')), request_sha256='a' * 64)
        self.wire = dict(self.payload, request_sha256=self.request['request_sha256'])
        self.native.capture_now += 0.1234567
        self.capture = self.native.capture(self.native.context, self.context,
            self.payload['marker'], self.request['request_sha256'], archive_root=self.root)
        self.charts = [dict(row, attachment_id='native-' + row['timeframe'])
            for row in self.capture['evidence']]
        self.response = dict(response_id='original-native-response', assistant_sha256='b' * 64,
            chart_evidence=json.dumps(self.charts), received_at=max(
                datetime.fromisoformat(r['captured_at'].replace('Z', '+00:00')).timestamp()
                for r in self.charts) + 10)
        self.csvs = deliver_market_data_files(self.context, now=self.context['fetched_at'])
        self.names = [self.payload['marker']+'-'+row['timeframe']+'-'+row['sha256'][:12]+'.png'
            for row in self.charts] + [f['name'] for f in self.csvs]
        self.receipts = ReceiptStore(self.root, monotonic=lambda: 1)
        self.first = self.intent()
        self.record(self.first)
        # The REAL website-capture wrapper must produce its own index; the
        # idempotent call below is not allowed to conceal a missing callsite.
        self.assertTrue((Path(self.root) / archive.CAPTURE_INDEX).is_dir())
        self.index = archive.record_capture_lookup(self.root, self.capture, self.wire)
        def artifacts(paths, rows):
            return [(Path(p).name, Path(p).read_bytes(), row) for p, row in zip(paths, rows)]
        self.reader = archive.NativeHistoricalArchiveReader(SimpleNamespace(**self.native.namespace),
            self.root, image_artifacts=artifacts)

    def intent(self, *, number=1, previous=None):
        return dict(schema_version=1, kind='NATIVE_200_CHART_FOLLOWUP',
            operation=self.payload['operation'], request_sha256=self.request['request_sha256'],
            marker=self.payload['marker'], original_payload_sha256=sha(self.wire),
            initial_context_sha256=sha(self.context), followup_context_sha256=sha(self.context),
            projected_context_sha256=sha(convert_native_market_context(self.context, now=self.context['fetched_at'])),
            initial_prompt_sha256='c' * 64, followup_prompt_sha256='d' * 64,
            initial_reply_sha256='e' * 64, initial_turns_sha256='f' * 64,
            conversation_url_sha256='1' * 64,
            initial_chart_evidence=copy.deepcopy(self.capture['evidence']),
            initial_attachment_names=list(self.names), followup_chart_evidence=copy.deepcopy(self.charts),
            followup_attachment_names=list(self.names), observed_at=self.context['fetched_at'],
            closed_candles_per_timeframe=200, closed_candles_total=400,
            followup_sequence=number, previous_followup_intent_sha256=previous)

    def record(self, intent, *, complete=True):
        self.receipts(stage='INTENT', receipt=intent, deadline=3)
        if complete:
            self.receipts(stage='COMPLETE', receipt=dict(intent, intent_sha256=sha(intent),
                response_id=self.response['response_id'],
                final_reply_sha256=self.response['assistant_sha256']), deadline=3)

    def read(self):
        return self.reader(self.payload['operation'], self.request, self.response)

    def manifest(self):
        return evidence._materials(self.payload['operation'], self.request, self.response, self.reader)

    def test_actual_receipt_store_chain_and_index_bind_original_four_bytes(self):
        result = self.read()
        self.assertEqual(len(result['files']), 4)
        self.assertEqual(result['delivery']['native_intent_sha256'], sha(self.first))
        self.assertEqual(result['delivery']['csv_sha256'],
            {r['timeframe']: hashlib.sha256(r['buffer']).hexdigest() for r in self.csvs})
        self.assertEqual(archive.lookup_capture_paths(self.root, self.payload['operation'],
            self.request, self.response), self.capture['paths'])
        self.assertEqual(archive.record_capture_lookup(self.root, self.capture, self.wire), self.index)

    def test_reader_uses_produced_index_and_does_not_search_random_capture_directories(self):
        directory = Path(self.root) / archive.CAPTURE_INDEX
        for path in directory.iterdir():
            path.unlink()
        with self.assertRaisesRegex(ValueError, 'OWNED_CAPTURE_NOT_INDEXED'):
            self.read()
        self.assertTrue(Path(self.capture['paths'][0]).is_file())

    def test_later_incomplete_native_intent_blocks_older_completed_leaf(self):
        self.record(self.intent(number=2, previous=sha(self.first)), complete=False)
        with self.assertRaisesRegex(ValueError, 'CHAIN_INCOMPLETE'):
            self.read()

    def test_changed_reply_or_csv_name_is_not_four_file_delivery_authority(self):
        self.response['assistant_sha256'] = '9' * 64
        with self.assertRaisesRegex(ValueError, 'CHAIN_INVALID'):
            self.read()
        self.response['assistant_sha256'] = 'b' * 64
        root = Path(self.root) / 'dialogue-200-followup'
        for path in root.iterdir():
            path.unlink()
        changed = self.intent()
        changed['followup_attachment_names'][-1] = 'different-coin.csv'
        self.record(changed)
        with self.assertRaisesRegex(ValueError, 'FOUR_FILE_RECEIPT_MISMATCH'):
            self.read()

    def test_replaced_sidecar_is_refused(self):
        sidecar = Path(self.capture['paths'][0]).parent / WEBSITE_SIDECAR
        sidecar.write_bytes(sidecar.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'INDEX_CHANGED'):
            self.read()

    def test_symlink_index_is_refused(self):
        index = next((Path(self.root) / archive.CAPTURE_INDEX).iterdir())
        raw = index.read_bytes()
        real = index.with_name('different-private-file')
        real.write_bytes(raw)
        real.chmod(0o600)
        index.unlink()
        index.symlink_to(real)
        with self.assertRaises(OSError):
            self.read()

    def test_complete_later_chain_projects_the_exact_final_leaf(self):
        second = self.intent(number=2, previous=sha(self.first))
        self.record(second)
        result = self.read()
        self.assertEqual(result['delivery']['native_intent_sha256'], sha(second))
        manifest = self.manifest()
        turn = archive.read_bound_material_turn(self.root, manifest)
        self.assertEqual(turn['intent']['followup_sequence'], 2)

    def test_same_room_reader_returns_original_native_turn_without_claiming_UI_presence(self):
        manifest = self.manifest()
        result = archive.read_bound_material_turn(self.root, manifest)
        self.assertEqual(result['intent'], self.first)
        self.assertEqual(result['completion']['response_id'], self.response['response_id'])
        self.assertNotIn('sent_this_request', result)
        changed = copy.deepcopy(manifest)
        changed['delivery_custody']['native_completion_sha256'] = '9' * 64
        with self.assertRaisesRegex(ValueError, 'BOUND_MATERIAL_MISMATCH'):
            archive.read_bound_material_turn(self.root, changed)

    def test_material_turn_reader_also_blocks_new_unknown_turn(self):
        manifest = self.manifest()
        self.record(self.intent(number=2, previous=sha(self.first)), complete=False)
        with self.assertRaisesRegex(ValueError, 'CHAIN_INCOMPLETE'):
            archive.read_bound_material_turn(self.root, manifest)

    def test_original_bound_files_rebuild_exact_csv_and_keep_old_capture_times(self):
        from unittest.mock import patch
        manifest = self.manifest()
        with patch('time.time', return_value=4_000_000_000), patch(
                'agent.native_chart_followup.public_snapshot', side_effect=AssertionError('NO_FRESH_MARKET')):
            result = archive.read_bound_material_files(self.root, manifest,
                charts=SimpleNamespace(**self.native.namespace))
        self.assertEqual(result['paths'], self.capture['paths'])
        self.assertEqual(result['observed_at'], self.context['fetched_at'])
        self.assertEqual(result['captured_at'], {r['timeframe']: r['captured_at'] for r in self.charts})
        self.assertEqual([r['buffer'] for r in result['csvs']], [r['buffer'] for r in self.csvs])
        self.assertEqual([r['name'] for r in result['csvs']], self.names[2:])
        self.assertEqual([r['name'] for r in result['pngs']], self.names[:2])
        self.assertEqual([r['buffer'] for r in result['pngs']],
            [Path(p).read_bytes() for p in self.capture['paths']])
        self.assertTrue(all(len(r['buffer'].decode().splitlines()) == 201 for r in result['csvs']))
        self.assertTrue(all('attachment_id' not in r for r in result['chart_evidence']))

    def test_bound_file_read_refuses_later_unknown_receipt_and_changed_original_png(self):
        manifest = self.manifest()
        png = Path(self.capture['paths'][0])
        original = png.read_bytes()
        png.write_bytes(original + b'CHANGED')
        with self.assertRaisesRegex(ValueError, 'HISTORICAL_PNG_MISMATCH'):
            archive.read_bound_material_files(self.root, manifest, charts=SimpleNamespace(**self.native.namespace))
        png.write_bytes(original)
        self.record(self.intent(number=2, previous=sha(self.first)), complete=False)
        with self.assertRaisesRegex(ValueError, 'CHAIN_INCOMPLETE'):
            archive.read_bound_material_files(self.root, manifest, charts=SimpleNamespace(**self.native.namespace))

    def test_bound_file_read_checks_full_csv_hash_beyond_filename_prefix(self):
        manifest = self.manifest()
        row = next(r for r in manifest['files'] if r['kind'] == 'CSV' and r['timeframe'] == '15m')
        row['sha256'] = row['sha256'][:8] + '9' * 56
        manifest['delivery_custody']['csv_sha256']['15m'] = row['sha256']
        # The native receipt retains the actual filename. The byte reader
        # additionally checks the whole SHA rather than its display prefix.
        with self.assertRaisesRegex(ValueError, 'HISTORICAL_CSV_MISMATCH'):
            archive.read_bound_material_files(self.root, manifest, charts=SimpleNamespace(**self.native.namespace))

    def test_bound_file_read_rechecks_website_facts_not_only_sidecar_hash(self):
        manifest = self.manifest()
        sidecar = Path(self.capture['paths'][0]).parent / WEBSITE_SIDECAR
        value = json.loads(sidecar.read_bytes())
        value['charts']['15m']['exchange_website_screenshot'] = False
        from agent.native_chart_followup import canonical
        sidecar.write_bytes(canonical(value))
        new_hash = hashlib.sha256(sidecar.read_bytes()).hexdigest()
        manifest['archive_sidecar_sha256'] = new_hash
        index = next((Path(self.root) / archive.CAPTURE_INDEX).iterdir())
        indexed = json.loads(index.read_bytes())
        indexed['sidecar_sha256'] = new_hash
        index.write_bytes(canonical(indexed))
        with self.assertRaisesRegex(ValueError, 'WEBSITE_SCREENSHOT_INVALID'):
            archive.read_bound_material_files(self.root, manifest, charts=SimpleNamespace(**self.native.namespace))


if __name__ == '__main__':
    unittest.main()
