import copy
import csv
import hashlib
import io
import json
from html.parser import HTMLParser
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_agent_closed_candles_200 import NOW, native_context_200
from agent.native_data_files import (CSV_COLUMNS, CONTEXT_SEPARATOR, SCHEMA_SEPARATOR,
    FINAL_JSON_REQUEST_ENDING, deliver_market_data_files, final_json_prompt,
    prepare_data_files, render_short_prompt)
from agent.native_market_context import (NativeContextUnavailable,
    candle_digest, convert_native_market_context)


class NativeDataFileTests(unittest.TestCase):
    def test_new_json_examples_survive_history_html_rendering_for_every_phase(self):
        class HistoryText(HTMLParser):
            def __init__(self):
                super().__init__(); self.text = []
            def handle_data(self, data): self.text.append(data)
        cases = [
            ({'required':['symbols'], 'properties':{'symbols':{'minItems':count,'maxItems':count}}},
                {'requested_count':count}, {'SYMBOLS':['SIMBOL_USDT_'+str(i+1) for i in range(count)]})
            for count in (1, 2)]
        cases += [
            ({'required':['symbol','side','limit_entry','take_profit','stop_loss','risk_reward']},
                {'symbol':'ATOMUSDT'}, {'COIN':'ATOMUSDT','ARAH':'LONG/SHORT','ENTRY':'harga','TP':'harga','SL':'harga'}),
            ({'required':['symbol','order_ref','decision']}, {'symbol':'FILUSDT'},
                {'COIN':'FILUSDT','KEPUTUSAN':'TAHAN/BATALKAN'}),
            ({'required':['symbol','side']}, {},
                {'COIN':'SIMBOL_USDT','ARAH':'LONG/SHORT','ENTRY':'harga','TP':'harga','SL':'harga'}),
            ({'required':['symbols']}, {}, {'SYMBOLS':['SIMBOL_USDT']})]
        for schema, context, example in cases:
            with self.subTest(schema=schema,context=context):
                payload={'output_schema':schema,'context':context,'marker':'HARUN_'+'a'*32}
                original='Analisis bahan terbaru.\n'+payload['marker']; before=copy.deepcopy(payload)
                result=final_json_prompt(payload,original)
                self.assertEqual(payload,before)
                self.assertTrue(result.startswith(original+'\n\n'))
                self.assertIn(json.dumps(example,separators=(',',':')),result)
                self.assertEqual(result.count(payload['marker']),1)
                self.assertTrue(result.endswith(FINAL_JSON_REQUEST_ENDING))
                self.assertEqual(final_json_prompt(payload,result),result)
                self.assertNotIn('<',result);self.assertNotIn('>',result);self.assertNotIn('`',result)
                rendered=HistoryText();rendered.feed(result);rendered.close()
                self.assertEqual(''.join(rendered.text),result)
                self.assertEqual(hashlib.sha256(''.join(rendered.text).encode()).digest(),
                    hashlib.sha256(result.encode()).digest())

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
            self.assertEqual(record['timestamp_unit'],'UNIX_MILLISECONDS_UTC')
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
        self.assertNotIn('Quantity eksekusi', rendered)
        self.assertNotIn('rekomendasi ukuran posisi', rendered)
        self.assertNotIn('Risk Manager', rendered)
        self.assertEqual(rendered.count('RR 1:2'),1)
        self.assertEqual(payload, before)
        self.assertNotIn('CONTEXT_JSON', rendered)
        self.assertNotIn('large public context', rendered)
        self.assertLess(len(rendered)-len(payload['prompt']),650)
        self.assertIn('chart asli 1H/15M',rendered)
        self.assertIn('CSV masing-masing 200 candle tertutup', rendered)
        self.assertIn('bahan belum terbaca',rendered)
        for admin in ('Unix','milidetik','UTC','.csv','Mark data','bukan HOLD','PNG -15m-',
                'jumlah candle tertutup mengacu','hitungan visual'):
            self.assertNotIn(admin,rendered)
        projected = convert_native_market_context(payload['context'], now=NOW)
        self.assertNotIn(projected['observed_at_utc'],rendered)
        for file in files:
            self.assertNotIn(file['name'],rendered);self.assertNotIn(file['sha256'],rendered)
            self.assertEqual(file['source_candles_sha256'],projected['provenance']['candles_sha256'])
        self.assertEqual(deliver_market_data_files(payload['context'],now=NOW),files)
        # The integration may ask the pinned native formatter to format null
        # context so the old large-context renderer never runs at all.
        null_formatted = payload['prompt'] + CONTEXT_SEPARATOR + 'null' + SCHEMA_SEPARATOR + suffix
        self.assertEqual(render_short_prompt(payload, null_formatted, files, now=NOW), rendered)

    def test_complete_owner_analysis_instructions_are_preserved_without_repetition(self):
        owner='Tentukan COIN LONG/SHORT/HOLD, ENTRY, TP, SL. RR 1:2; SL mempertimbangkan wick dan invalidasi.'
        payload=dict(prompt=owner,context=self.context(),marker='HARUN_OWNER',output_schema={'required':['symbol','side']})
        original=owner+CONTEXT_SEPARATOR+'null'+SCHEMA_SEPARATOR+'{}\n'+payload['marker']
        files=deliver_market_data_files(payload['context'],now=NOW)
        rendered=render_short_prompt(payload,original,files,now=NOW)
        self.assertTrue(rendered.startswith(owner+'\n\n'))
        for word in ('COIN','LONG/SHORT/HOLD','ENTRY','TP','SL.','1:2','wick','invalidasi'):
            self.assertEqual(rendered.count(word),owner.count(word))
        self.assertEqual(rendered.count(payload['marker']),1)

    def test_each_selected_replacement_or_pending_coin_keeps_its_own_exact_csv_material(self):
        from agent.native_pending_protocol import REVIEW_SCHEMA
        for symbol in ('FILUSDT','ATOMUSDT','ETHUSDT'):
            for schema in ({'required':['symbol','side','limit_entry','take_profit','stop_loss','risk_reward']},REVIEW_SCHEMA):
                with self.subTest(symbol=symbol,pending=schema==REVIEW_SCHEMA):
                    context=self.context();context['symbol']=symbol
                    for frame in context['timeframes'].values():frame['symbol']=symbol
                    payload=dict(prompt='Analisis coin pilihan.',context=context,marker='HARUN_'+symbol,output_schema=schema)
                    original=payload['prompt']+CONTEXT_SEPARATOR+'null'+SCHEMA_SEPARATOR+json.dumps(schema)+'\n'+payload['marker']
                    before=copy.deepcopy(payload);files=deliver_market_data_files(context,now=NOW)
                    result=render_short_prompt(payload,original,files,now=NOW)
                    self.assertIn(symbol+' — Binance Futures USDT Perpetual',result)
                    self.assertEqual(payload,before)
                    for file in files:
                        self.assertEqual(file['symbol'],symbol);self.assertNotIn(file['name'],result)
                        self.assertEqual(file['sha256'],hashlib.sha256(file['buffer']).hexdigest())
                        self.assertEqual(len(file['buffer'].decode().splitlines()),201)
                    foreign=deliver_market_data_files(self.context(),now=NOW)
                    with self.assertRaisesRegex(ValueError,'NATIVE_DATA_FILES_UNVERIFIED'):
                        render_short_prompt(payload,original,foreign,now=NOW)

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
