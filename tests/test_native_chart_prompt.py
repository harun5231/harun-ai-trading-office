import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_agent_closed_candles_200 import NOW, native_context_200
from agent.native_chart_prompt import install
from agent.native_market_context import NativeContextUnavailable, convert_native_market_context


def formatter(payload):
    encode = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return payload['prompt'] + '\nSCHEMA\n' + encode(payload['output_schema']) + '\nCONTEXT\n' + encode(payload['context']) + '\nMARKER\n' + payload['marker']


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

    def test_full_400_serialized_without_mutating_original_request_or_prompt(self):
        namespace, hook = self.hook()
        payload = self.payload(); before = copy.deepcopy(payload)
        expected = convert_native_market_context(payload['context'], now=NOW)
        result = hook(payload)
        self.assertEqual(payload, before)
        self.assertTrue(result.startswith(payload['prompt'] + '\nSCHEMA\n'))
        self.assertTrue(result.endswith('\nMARKER\nTEST_MARKER_V1'))
        actual = json.loads(result.split('\nCONTEXT\n')[1].split('\nMARKER\n')[0])
        self.assertEqual(actual, expected)
        self.assertEqual(sum(len(rows) for rows in actual['candles'].values()), 400)
        self.assertNotIn('risk_constraints', actual)
        self.assertIs(namespace['user_prompt'], hook)
        for tf in ('1h', '15m'):
            self.assertEqual(len(actual['candles'][tf]), 200)
            self.assertNotIn(actual['running_candles'][tf], actual['candles'][tf])

    def test_screening_formatter_retained_byte_for_byte(self):
        _, hook = self.hook(); payload = self.payload(); payload['context'] = None
        self.assertEqual(hook(payload), formatter(payload))

    def test_truncating_formatter_is_rejected(self):
        _, hook = self.hook(lambda payload: payload['prompt'])
        with self.assertRaisesRegex(ValueError, 'CONTEXT_NOT_SERIALIZED'):
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
