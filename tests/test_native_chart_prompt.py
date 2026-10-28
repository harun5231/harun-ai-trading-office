import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_agent_closed_candles_200 import NOW, native_context_200
from agent.native_chart_prompt import install
from agent.native_data_files import deliver_market_data_files
from agent.native_market_context import NativeContextUnavailable, convert_native_market_context


def formatter(payload):
    encode = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return (payload['prompt'] + '\n\nCONTEXT_JSON:\n' + encode(payload['context'])
        + '\n\nOUTPUT_JSON_SCHEMA:\n' + encode(payload['output_schema'])
        + '\n\nWrite ' + payload['marker'] + ' exactly once on its own line.')


class NativeChartPromptTests(unittest.TestCase):
    def payload(self):
        context = native_context_200()
        context['risk_constraints'] = {'private_user_setting': 'not forwarded'}
        return {'prompt': 'Exact approved user prompt.\nDo not change any text.',
            'output_schema': {'required': ['symbol', 'side']}, 'context': context,
            'marker': 'TEST_MARKER_V1', 'request_sha256': 'b' * 64}

    def hook(self, original=formatter):
        namespace = {'user_prompt': original}
        return namespace, install(namespace, expected_prompt=original,
                                  context_key='context', wall_clock=lambda: NOW)

    def test_exact_400_delivered_as_files_with_owner_prompt_and_schema_unchanged(self):
        namespace, hook = self.hook()
        payload = self.payload(); before = copy.deepcopy(payload)
        files = deliver_market_data_files(payload['context'], now=NOW)
        result = hook(payload)
        self.assertEqual(payload, before)
        self.assertTrue(result.startswith(payload['prompt'] + '\n\n'))
        self.assertTrue(result.endswith(formatter({**payload, 'context': None}).split(
            '\n\nOUTPUT_JSON_SCHEMA:\n', 1)[1]))
        self.assertNotIn('CONTEXT_JSON:', result)
        self.assertNotIn('private_user_setting', result)
        self.assertNotIn('"candles"', result)
        self.assertLess(len(result), 4000)
        self.assertIs(namespace['user_prompt'], hook)
        self.assertTrue(hook.__harun_data_files_v1__)
        for file in files:
            self.assertIn(file['name'], result)
            self.assertEqual(file['closed_count'], 200)
            self.assertEqual(len(file['buffer'].decode().splitlines()), 201)

    def test_screening_formatter_retained_byte_for_byte(self):
        _, hook = self.hook(); payload = self.payload(); payload['context'] = None
        self.assertEqual(hook(payload), formatter(payload))

    def test_actual_native_insertion_preserving_encoder_emits_full_canonical_projection(self):
        def native_encode(value):
            if isinstance(value, dict):
                return '{' + ','.join(json.dumps(key) + ':' + native_encode(item)
                    for key, item in value.items()) + '}'
            if isinstance(value, (list, tuple)):
                return '[' + ','.join(native_encode(item) for item in value) + ']'
            return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        def native_formatter(payload):
            return (payload['prompt'] + '\n\nCONTEXT_JSON:\n' + native_encode(payload['context'])
                + '\n\nOUTPUT_JSON_SCHEMA:\n' + native_encode(payload['output_schema'])
                + '\n\nWrite ' + payload['marker'] + ' exactly once on its own line.')
        _, hook = self.hook(native_formatter)
        payload = self.payload(); before = copy.deepcopy(payload)
        rendered = hook(payload)
        self.assertEqual(payload, before)
        self.assertTrue(rendered.startswith(payload['prompt'] + '\n\n'))
        self.assertNotIn('CONTEXT_JSON:', rendered)
        self.assertTrue(rendered.endswith(native_encode(payload['output_schema'])
            + '\n\nWrite ' + payload['marker'] + ' exactly once on its own line.'))
        for file in deliver_market_data_files(payload['context'], now=NOW):
            self.assertIn(file['name'], rendered)
        self.assertLess(len(rendered), 4000)

    def test_truncating_formatter_is_rejected(self):
        _, hook = self.hook(lambda payload: payload['prompt'])
        with self.assertRaisesRegex(ValueError, 'NATIVE_DATA_FILES_UNVERIFIED'):
            hook(self.payload())

    def test_legacy_insufficient_history_rejected_before_formatter(self):
        calls = []
        _, hook = self.hook(lambda p: calls.append(p) or formatter(p))
        payload = self.payload(); payload['context'] = native_context_200(count=100)
        with self.assertRaises(NativeContextUnavailable): hook(payload)
        self.assertEqual(calls, [])

    def test_native_formatter_identity_and_explicit_field_required(self):
        for ns, expected, key in (({}, formatter, 'context'),
                ({'user_prompt': formatter}, lambda p: '', 'context'),
                ({'user_prompt': formatter}, formatter, 'guessed_field')):
            with self.assertRaisesRegex(ValueError, 'CONFIGURATION_UNVERIFIED'):
                install(ns, expected_prompt=expected, context_key=key)

    def test_expired_or_mismatched_context_rejected(self):
        _, hook = self.hook()
        for mutation in (lambda p: p['context'].update(fetched_at=NOW - 181),
                         lambda p: p['context']['timeframes']['1h'].update(symbol='ETHUSDT')):
            payload = self.payload(); mutation(payload)
            with self.assertRaises(NativeContextUnavailable): hook(payload)


if __name__ == '__main__': unittest.main()
