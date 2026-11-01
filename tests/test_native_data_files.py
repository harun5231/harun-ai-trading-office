import copy
import csv
import hashlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_agent_closed_candles_200 import NOW, native_context_200
from agent.native_data_files import (CSV_COLUMNS, CONTEXT_SEPARATOR, SCHEMA_SEPARATOR,
    deliver_market_data_files, prepare_data_files, render_short_prompt)
from agent.native_market_context import (NativeContextUnavailable,
    candle_digest, convert_native_market_context)


class NativeDataFileTests(unittest.TestCase):
    def context(self):
        value = native_context_200()
        for frame in value['timeframes'].values():
            for row in frame['candles']:
                row.update(open='100.00000000000001', high='102.00000000000001',
                    low='99.00000000000001', close='101.00000000000001',
                    volume='12.500000000000000001')
        return value

    def test_exact_400_closed_rows_round_trip_decimal_strings_and_digest(self):
        context = self.context(); before = copy.deepcopy(context)
        projected = convert_native_market_context(context, now=NOW)
        files = deliver_market_data_files(context, now=NOW)
        self.assertEqual(context, before)
        self.assertEqual([row['timeframe'] for row in files], ['15m', '1h'])
        rebuilt = {}
        for record in files:
            self.assertEqual(record['mimeType'], 'text/csv')
            self.assertEqual(record['closed_count'], 200)
            self.assertEqual(record['sha256'], hashlib.sha256(record['buffer']).hexdigest())
            self.assertRegex(record['name'], r'^BTCUSDT_(15m|1h)_200_closed_[a-f0-9]{8}\.csv$')
            reader = csv.DictReader(io.StringIO(record['buffer'].decode('utf-8')))
            self.assertEqual(tuple(reader.fieldnames), CSV_COLUMNS)
            rows = list(reader); self.assertEqual(len(rows), 200)
            native = [{'open_time': int(row['open_time_utc_ms']),
                **{key: row[key] for key in ('open', 'high', 'low', 'close', 'volume')},
                'close_time': int(row['close_time_utc_ms'])} for row in rows]
            rebuilt[record['timeframe']] = native
            self.assertEqual(native, projected['candles'][record['timeframe']])
            self.assertNotIn(projected['running_candles'][record['timeframe']], native)
        self.assertEqual(candle_digest(rebuilt), projected['provenance']['candles_sha256'])
        self.assertEqual(prepare_data_files(projected, now=NOW), files)

    def test_unclosed_insufficient_or_corrupt_source_is_rejected(self):
        for mutate in (
            lambda value: value['timeframes']['15m']['candles'].__delitem__(slice(None, 2)),
            lambda value: value['timeframes']['1h']['candles'][-2].update(
                close_time=value['server_time']),
            lambda value: value['timeframes']['15m']['candles'][0].update(close='1e2'),
        ):
            context = self.context(); mutate(context)
            with self.assertRaises(NativeContextUnavailable):
                deliver_market_data_files(context, now=NOW)

    def test_compact_prompt_keeps_owner_payload_and_marker_without_repeated_wire_schema(self):
        payload = {'prompt': 'Original owner prompt.\nRR 1:2.\n', 'context': self.context(),
            'output_schema': {'required': ['symbol', 'side', 'limit_entry', 'take_profit', 'stop_loss']},
            'marker': 'HARUN_1234567890abcdef1234567890abcdef'}
        before = copy.deepcopy(payload)
        suffix = json.dumps(payload['output_schema']) + '\n\nWrite ' + payload['marker'] + ' exactly once.'
        original = payload['prompt'] + CONTEXT_SEPARATOR + ('large public context' * 4000) + SCHEMA_SEPARATOR + suffix
        files = deliver_market_data_files(payload['context'], now=NOW)
        rendered = render_short_prompt(payload, original, files, now=NOW)
        self.assertTrue(rendered.startswith(payload['prompt'] + '\n\n'))
        self.assertTrue(rendered.endswith('Acuan percakapan: ' + payload['marker']))
        self.assertNotIn('OUTPUT_JSON_SCHEMA', rendered)
        self.assertIn('Quantity dihitung Risk Manager', rendered)
        self.assertEqual(payload, before)
        self.assertNotIn('CONTEXT_JSON', rendered)
        self.assertNotIn('large public context', rendered)
        self.assertIn(files[0]['name'], rendered)
        self.assertIn(files[1]['name'], rendered)
        self.assertLess(len(rendered), 2200)
        # The integration may ask the pinned native formatter to format null
        # context so the old large-context renderer never runs at all.
        null_formatted = payload['prompt'] + CONTEXT_SEPARATOR + 'null' + SCHEMA_SEPARATOR + suffix
        self.assertEqual(render_short_prompt(payload, null_formatted, files, now=NOW), rendered)

    def test_changed_or_missing_file_cannot_be_represented_as_attached_data(self):
        payload = {'prompt': 'Analyze.', 'context': self.context(), 'marker': 'HARUN_TEST'}
        original = payload['prompt'] + CONTEXT_SEPARATOR + '{}' + SCHEMA_SEPARATOR + '{}\nHARUN_TEST'
        files = deliver_market_data_files(payload['context'], now=NOW)
        for supplied in (files[:1], copy.deepcopy(files), list(reversed(files))):
            if len(supplied) == 2 and supplied[0]['timeframe'] == '15m':
                supplied[0]['buffer'] += b'corrupt\n'
            with self.assertRaisesRegex(ValueError, 'NATIVE_DATA_FILES_UNVERIFIED'):
                render_short_prompt(payload, original, supplied, now=NOW)

    def test_foreign_symbol_and_non_native_format_are_rejected(self):
        payload = {'prompt': 'Analyze.', 'context': self.context(), 'marker': 'HARUN_TEST'}
        files = deliver_market_data_files(payload['context'], now=NOW)
        with self.assertRaisesRegex(ValueError, 'NATIVE_DATA_FILES_UNVERIFIED'):
            render_short_prompt(payload, 'wrong original prompt', files, now=NOW)
        changed = copy.deepcopy(files); changed[0]['symbol'] = 'ETHUSDT'
        with self.assertRaisesRegex(ValueError, 'NATIVE_DATA_FILES_UNVERIFIED'):
            render_short_prompt(payload, payload['prompt'] + CONTEXT_SEPARATOR + '{}' + SCHEMA_SEPARATOR + 'HARUN_TEST', changed, now=NOW)


if __name__ == '__main__': unittest.main()
