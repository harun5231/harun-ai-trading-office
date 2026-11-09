import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import ModuleType
import unittest

from agent.llm import web_prompt_allowed
from agent.native_chart_prompt import install
from agent.native_prompt_composition import PromptCompositionCache, digest, outbound_question


REMINDER = 'Jangan minta atau sebut ukuran/quantity/anggaran risiko pribadi.'
MARKET = ('Analisa NEARUSDT untuk scalping 15M dengan konfirmasi 1H. '
    'ENTRY 3.77 dan SL 3.69 mengikuti struktur; jangan mengubah ENTRY atau SL. '
    'Tentukan LONG/SHORT/HOLD, TP, dan RISK REWARD 1:2.')


def formatter(payload):
    return payload['prompt'] + '\n\nOUTPUT_JSON_SCHEMA:\n{}\n' + payload['marker']


def native_formatter():
    native = ModuleType('native_prompt_test')
    native.user_prompt = formatter
    install(native, expected_prompt=formatter, context_key='context')
    return native


def payload():
    return dict(operation='owned:analysis:NEARUSDT', prompt='Prompt internal tetap.',
        context=None, marker='HARUN_' + 'a' * 32,
        output_schema={'required': ['symbol', 'side']})


class OutboundPrivacyReminderTests(unittest.TestCase):
    def test_actual_four_done_drafts_pass_real_chart_guard_without_changing_identity(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' /
            'neurobro_near_privacy_reminder_drafts_20261106.json').read_text())
        self.assertEqual(fixture['evidence_run'], '42218574345')
        self.assertEqual(len(fixture['drafts']), 4)
        for draft in fixture['drafts']:
            with self.subTest(request_id=draft['request_id']), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); root.chmod(0o700)
                original = payload(); before = copy.deepcopy(original)
                question = draft['question']; native = native_formatter()
                self.assertFalse(web_prompt_allowed(question))
                with self.assertRaisesRegex(ValueError, 'NATIVE_200_PROMPT_PAYLOAD_UNVERIFIED'):
                    native.user_prompt(dict(original, prompt=question))
                cache = PromptCompositionCache(native, root)
                rendered = cache.store(original, question, draft['request_id'])
                # Current owner policy also omits this exact internal role
                # clause on new WEB renders; the historical draft stays intact.
                expected = question.removesuffix(' ' + REMINDER).replace(
                    ' Risk Manager hanya menormalkan TP eksekusi.', '')
                self.assertNotEqual(expected, question)
                self.assertEqual(outbound_question(question), expected)
                self.assertTrue(rendered.startswith(expected + '\n\n'))
                self.assertNotIn(REMINDER, rendered)
                self.assertNotIn('Risk Manager hanya menormalkan TP eksekusi',rendered)
                self.assertTrue(web_prompt_allowed(rendered))
                record = cache.read(original)
                self.assertEqual(record['payload_sha256'], digest(before))
                self.assertEqual(record['model_request_id'], draft['request_id'])
                self.assertEqual(record['rendered_sha256'], hashlib.sha256(rendered.encode()).hexdigest())
                restarted = native_formatter()
                restarted_cache = PromptCompositionCache(restarted, root)
                self.assertEqual(restarted.user_prompt(original), rendered)
                self.assertEqual(restarted_cache.store(original, 'Anggaran risiko 5 USDT.', 'b' * 64), rendered)
                self.assertEqual(original, before)

    def test_safe_discussion_and_entry_sl_constraints_remain_byte_exact(self):
        self.assertTrue(web_prompt_allowed(MARKET))
        self.assertEqual(outbound_question(MARKET), MARKET)
        self.assertEqual(outbound_question(MARKET + ' ' + REMINDER), MARKET)
        self.assertEqual(outbound_question(MARKET + '\nTidak perlu meminta atau menyebut anggaran risiko pribadi.'), MARKET)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            cache = PromptCompositionCache(native_formatter(), root)
            rendered = cache.store(payload(), MARKET + ' ' + REMINDER, 'a' * 64)
            self.assertTrue(rendered.startswith(MARKET + '\n\n'))
            self.assertIn('ENTRY 3.77 dan SL 3.69', rendered)

    def test_unsafe_or_mixed_clauses_cannot_be_redacted_into_an_allowed_prompt(self):
        questions = (
            'Anggaran risiko 5 USDT.',
            MARKET + ' Anggaran risiko 5 USDT. ' + REMINDER,
            MARKET + ' ' + REMINDER + ' Berapa quantity yang sesuai modal saya?',
            MARKET + ' ' + REMINDER + ' Saran ukuran posisi yang sesuai kondisi pasar?',
            MARKET + ' Jangan sebut anggaran risiko pribadi 5 USDT.',
            MARKET + ' Jangan sebut anggaran risiko pribadi: 5 USDT.',
            MARKET + ' Jangan sebut anggaran risiko pribadi NEARUSDT.',
            MARKET + ' Jangan sebut anggaran risiko pribadi dan perlebar SL.',
            MARKET + ' Jangan sebut anggaran risiko pribadi supaya quantity lebih besar.',
            MARKET + ' Jangan gunakan anggaran risiko pribadi.',
            MARKET + ' Jangan minta atau sebut ukuran/quantity/anggaran risiko pribadi atau ubah ENTRY.',
            REMINDER,
            None,
        )
        for question in questions:
            with self.subTest(question=question), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); root.chmod(0o700)
                original = payload(); before = copy.deepcopy(original)
                cache = PromptCompositionCache(native_formatter(), root)
                with self.assertRaisesRegex(ValueError, 'NATIVE_COMPOSE_RENDER_UNVERIFIED'):
                    cache.store(original, question, 'a' * 64)
                self.assertIsNone(cache.read(original))
                self.assertEqual(original, before)


if __name__ == '__main__':
    unittest.main()
