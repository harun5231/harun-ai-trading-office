"""Provider body bounds and exact-context packing, entirely offline."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from worker.core import Review
from worker.neuroapi import SETUP_SCHEMA
from worker.neuroapi_request import (
    ANALYSIS_FORMAT, FRAGMENT_FORMAT, LIMIT_ERROR, MAX_CHARACTERS, MAX_MESSAGES,
    build_request_body,
)
from worker.prompts import ANALYSIS


class NeuroAPIRequestLimitsTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('urllib.request.build_opener', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def context(self, count=100):
        frames = {}
        for timeframe, interval in (('1h', 3_600_000), ('15m', 900_000)):
            candles = [dict(open_time=1_790_000_000_000 + index*interval,
                            open='161.4500', high='162.9300', low='160.3700', close='162.3400',
                            volume='235684.120', close_time=1_790_000_000_000+(index+1)*interval-1)
                       for index in range(count)]
            frames[timeframe] = dict(symbol='SOLUSDT', timeframe=timeframe, candles=candles)
        return dict(source='Binance Futures', symbol='SOLUSDT', fetched_at=1_790_000_000.123,
                    server_time=1_790_000_000_123, mark_price=dict(price='162.34000000', time=1_790_000_000_123),
                    quantity_unit='base_asset', timeframes=frames,
                    contract_rules=dict(source='Binance Futures /fapi/v1/exchangeInfo', symbol='SOLUSDT',
                                        tickSize='0.00100000', stepSize='0.10000000', minQty='0.10000000',
                                        maxQty='1000000.00000000', minPrice='0.00100000', maxPrice='100000.00000000'),
                    risk_constraints=dict(maximum_loss_at_sl_usdt='5', entry_fee_rate='0.0005',
                                          sl_exit_fee_rate='0.0005', tp_exit_fee_rate='0.0005',
                                          position_sizing_contract='fee-inclusive risk and net 1:2 instructions '*40))

    def assert_limits(self, body):
        history = body['message_history']
        self.assertLessEqual(len(history), MAX_MESSAGES)
        self.assertTrue(all(message['role'] == 'user' and 0 < len(message['content']) <= MAX_CHARACTERS
                            for message in history))
        return [json.loads(message['content']) for message in history]

    def reconstruct(self, body):
        packets = self.assert_limits(body)
        total = len(packets)
        self.assertEqual([packet['context_partition']['part'] for packet in packets], list(range(1, total+1)))
        self.assertTrue(all(packet['context_partition']['parts'] == total for packet in packets))
        if packets[0]['context_partition']['format'] == FRAGMENT_FORMAT:
            self.assertTrue(all(packet['context_partition']['format'] == FRAGMENT_FORMAT for packet in packets))
            return json.loads(''.join(packet['context_json_fragment'] for packet in packets))
        self.assertEqual(packets[0]['context_partition']['format'], ANALYSIS_FORMAT)
        self.assertEqual(packets[0]['context_partition']['kind'], 'metadata')
        self.assertIn('ONE complete analysis context', packets[0]['context_partition']['instruction'])
        context = copy.deepcopy(packets[0]['data'])
        context['timeframes'] = {}
        for packet in packets[1:]:
            header, data = packet['context_partition'], packet['data']
            self.assertEqual(header['format'], ANALYSIS_FORMAT)
            self.assertEqual(header['kind'], 'timeframe')
            timeframe = header['timeframe']
            frame = context['timeframes'].setdefault(timeframe, {**data, 'candles': []})
            self.assertEqual({key: value for key, value in frame.items() if key != 'candles'},
                             {key: value for key, value in data.items() if key != 'candles'})
            self.assertEqual(header['candle_start'], len(frame['candles']))
            frame['candles'].extend(data['candles'])
            self.assertEqual(header['candle_end'], len(frame['candles']))
            self.assertLessEqual(header['candle_end'], header['candle_total'])
        return context

    def test_realistic_sol_one_hundred_candles_each_round_trip_without_loss(self):
        context = self.context()
        self.assertGreater(len(json.dumps(context, separators=(',', ':'))), MAX_CHARACTERS)
        before = copy.deepcopy(context)
        body = build_request_body(ANALYSIS, SETUP_SCHEMA, context)
        self.assertEqual(self.reconstruct(body), context)
        self.assertEqual(context, before)
        self.assertEqual(set(body), {'prompt', 'mode', 'stream', 'output_schema', 'message_history'})
        self.assertEqual(body['prompt'], ANALYSIS)
        self.assertIs(body['output_schema'], SETUP_SCHEMA)
        self.assertEqual((body['mode'], body['stream']), ('smart', False))

    def test_five_hundred_candles_each_are_contiguous_and_all_retained(self):
        context = self.context(500)
        body = build_request_body(ANALYSIS, SETUP_SCHEMA, context)
        restored = self.reconstruct(body)
        self.assertEqual(restored, context)
        self.assertGreater(len(body['message_history']), 3)
        self.assertEqual({key: len(frame['candles']) for key, frame in restored['timeframes'].items()},
                         {'1h': 500, '15m': 500})

    def test_small_and_absent_context_keep_exact_legacy_body_and_digest(self):
        for context in (None, {}, self.context(20), {'unicode': 'é😀', 'items': [1, '0.00000001', None]}):
            with self.subTest(context_type=type(context).__name__):
                old = {'prompt': ANALYSIS, 'mode': 'smart', 'stream': False, 'output_schema': SETUP_SCHEMA}
                if context is not None:
                    old['message_history'] = [{'role': 'user', 'content': json.dumps(context, separators=(',', ':'))}]
                new = build_request_body(ANALYSIS, SETUP_SCHEMA, context)
                self.assertEqual(list(new), list(old))
                self.assertEqual(new, old)
                self.assertEqual(json.dumps(new, separators=(',', ':')), json.dumps(old, separators=(',', ':')))
                digest = lambda body: hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
                self.assertEqual(digest(new), digest(old))

    def test_exact_single_content_boundary_keeps_historical_representation(self):
        context = 'x'*(MAX_CHARACTERS-2)  # JSON quote pair also counts toward content.
        body = build_request_body('p', {}, context)
        self.assertEqual(len(body['message_history']), 1)
        self.assertEqual(len(body['message_history'][0]['content']), MAX_CHARACTERS)
        self.assertEqual(body['message_history'][0]['content'], json.dumps(context, separators=(',', ':')))

    def test_generic_oversize_context_and_quote_escape_boundaries_reassemble_exactly(self):
        context = dict(text='"\\😀line\n'*8000, nested=[{'price': '0.00000000123000'}])
        body = build_request_body('p', {}, context)
        packets = self.assert_limits(body)
        self.assertEqual(packets[0]['context_partition']['format'], FRAGMENT_FORMAT)
        text = ''.join(packet['context_json_fragment'] for packet in packets)
        self.assertEqual(text, json.dumps(context, separators=(',', ':')))
        self.assertEqual(self.reconstruct(body), context)

    def test_unicode_characters_are_not_rejected_for_utf8_byte_length(self):
        prompt = '😀'*MAX_CHARACTERS
        self.assertGreater(len(prompt.encode()), MAX_CHARACTERS)
        self.assertEqual(build_request_body(prompt, {})['prompt'], prompt)
        context = self.context()
        context['risk_constraints']['note'] = '😀'*10000
        body = build_request_body('p', {}, context)
        contents = [message['content'] for message in body['message_history']]
        self.assertTrue(any(len(content.encode()) > MAX_CHARACTERS and len(content) <= MAX_CHARACTERS
                            for content in contents))
        self.assertEqual(self.reconstruct(body), context)

    def test_large_frame_metadata_or_one_giant_candle_falls_back_without_loss(self):
        for kind in ('metadata', 'candle'):
            with self.subTest(kind=kind):
                context = self.context()
                if kind == 'metadata':
                    context['risk_constraints']['explanation'] = 'x'*MAX_CHARACTERS
                else:
                    context['timeframes']['1h']['candles'][0]['annotation'] = 'x'*MAX_CHARACTERS
                before = copy.deepcopy(context)
                body = build_request_body('p', {}, context)
                self.assertEqual(self.assert_limits(body)[0]['context_partition']['format'], FRAGMENT_FORMAT)
                self.assertEqual(self.reconstruct(body), context)
                self.assertEqual(context, before)

    def test_invalid_prompt_and_more_than_fifty_messages_refused(self):
        for prompt in ('', 'x'*(MAX_CHARACTERS+1), None, True, [], {}):
            with self.subTest(prompt_type=type(prompt).__name__):
                with self.assertRaisesRegex(Review, '^'+LIMIT_ERROR+'$'):
                    build_request_body(prompt, {}, self.context())
        # Even unwrapped content exceeds the total capacity, so no packing can fit.
        context = {'text': 'x'*(MAX_CHARACTERS*MAX_MESSAGES+1)}
        before = copy.deepcopy(context)
        with self.assertRaisesRegex(Review, '^'+LIMIT_ERROR+'$'):
            build_request_body('p', {}, context)
        self.assertEqual(context, before)


if __name__ == '__main__':
    unittest.main()
