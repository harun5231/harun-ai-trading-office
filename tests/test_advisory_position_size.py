"""Offline privacy and authority contracts; no inference or order calls."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from agent import dialogue_service as service
from agent.llm import INSTRUCTIONS, dialogue_schema, web_prompt_allowed, web_source_facts, web_display_text
from agent.native_data_files import compact_summary
from agent.native_prompt_composition import PromptCompositionCache, compact_protocol
from worker import prompts
from worker.account_state import screening_contract


ADVICE = ('Sebagai rekomendasi saja, berapa ukuran posisi LINKUSDT yang sesuai kondisi pasar? '
    'Quantity eksekusi tetap dihitung oleh Risk Manager; rekomendasi ini bukan perintah eksekusi.')


def request(phase='ANALYSIS', *, facts=True):
    value = dict(phase=phase, reply='LINKUSDT ENTRY 17.25, SL 17.00, TP 17.70.',
        market_context={'symbol': 'LINKUSDT', 'mark_price': '17.31'})
    if facts:
        values = {'symbol': 'LINKUSDT', 'quantity': '0.5', 'risk_usdt': '3',
            'execution_tp': '17.85', 'estimated_loss': '3.15',
            'cost_components': {'private_cost_canary': '123.456'}}
        value['source_facts'] = [dict(producer='RISK_MANAGER', operation='owned:analysis:one',
            values=values, sha256=hashlib.sha256(service.canonical(values).encode()).hexdigest())]
    return value


def decision(phase, question):
    return dict(action='PROMPT' if phase.startswith('COMPOSE_') else 'CLARIFY',
        symbols=[], question=question)


class PrivateRiskPromptContractTests(unittest.TestCase):
    def test_even_explicit_advisory_sizing_is_refused_in_every_phase(self):
        messages = (ADVICE, 'Gunakan chart Binance untuk membahas struktur pasar. ' + ADVICE,
            'Tolong tentukan ukuran posisi LINKUSDT.',
            'Dengan modal akun 1000 USDT, hitungkan quantity.',
            'Rekomendasikan position size hanya sebagai bahan diskusi.',
            'Berapa qty yang ideal dari risiko ini?',
            'Quantity berapa yang cocok?', 'Berikan saran lot untuk entry ini.')
        for phase in service.PHASES:
            for text in messages:
                with self.subTest(phase=phase, text=text), self.assertRaises(service.DialogueError):
                    service.validate_web_question(text, request(phase))

    def test_native_summary_and_fallback_request_only_neurobro_setup_authority(self):
        payload = dict(marker='HARUN_private', output_schema={'required': [
            'symbol', 'side', 'limit_entry', 'take_profit', 'stop_loss', 'risk_reward']})
        for text in (compact_summary(payload), compact_protocol(
                prompts.ANALYSIS + '\n\nOUTPUT_JSON_SCHEMA:\n{}', payload)):
            self.assertTrue(web_prompt_allowed(text))
            self.assertNotIn('ukuran posisi', text.lower())
            self.assertNotIn('quantity', text.lower())
            self.assertNotIn('risiko Office', text)
            for clause in ('RISK REWARD 1:2', 'swing high/low', 'likuiditas', 'volatilitas',
                    'wick', 'false breakout', 'invalidasi', 'Risk Manager hanya menormalkan TP',
                    '1:3', '1:4', 'coin pengganti'):
                self.assertIn(clause, text)

    def test_private_budget_is_blocked_even_when_its_number_is_in_market_data(self):
        messages = ('Risiko Office 17.25 USDT.', 'Anggaran risiko 17.25 USDT.',
            'risk_usdt=17.25 dari Risk Manager.', 'Risk budget: 17.25 USDT.',
            'Risiko per transaksi sebesar 17.25 USDT.', 'Modal akun 17.25 USDT.',
            'Saldo akun sebesar 17.25 USDT.', 'Risiko: 17.25 USDT.')
        for phase in ('COMPOSE_ANALYSIS', 'ANALYSIS', 'COMPOSE_PENDING_REVIEW', 'PENDING_REVIEW'):
            for text in messages:
                with self.subTest(phase=phase, text=text), self.assertRaisesRegex(
                        service.DialogueError, 'MODEL_RESULT_INVALID'):
                    service.validate_web_question(text, request(phase))

    def test_private_facts_stay_internal_and_do_not_authorize_invented_levels(self):
        value = request(); before = copy.deepcopy(value['source_facts'])
        projected = web_source_facts(service.validate_source_facts(value['source_facts']))
        self.assertEqual(value['source_facts'], before)
        self.assertEqual(projected[0]['values'], {'symbol': 'LINKUSDT',
            'quantity': '0.5', 'execution_tp': '17.85'})
        self.assertEqual(projected[0]['sha256'], hashlib.sha256(
            service.canonical(projected[0]['values']).encode()).hexdigest())
        for price in ('3', '3.15', '123.456', '999'):
            with self.subTest(price=price), self.assertRaisesRegex(
                    service.DialogueError, 'PRICE_PROPOSAL_REFUSED'):
                service.validate_web_question('Apakah ENTRY ' + price + ' valid?', value)
        answer = decision('ANALYSIS', 'Apakah TP 17.85 dari Risk Manager masih sesuai struktur 15M?')
        self.assertEqual(service.validate_web_question(answer['question'], value), answer['question'])

    def test_existing_order_quantity_can_be_discussed_without_sizing_advice(self):
        text = 'Quantity 0.5 adalah order yang terpasang; apakah ENTRY 17.25 masih sesuai struktur 15M?'
        for phase in ('ANALYSIS', 'PENDING_REVIEW', 'COMPOSE_PENDING_REVIEW'):
            value = decision(phase, text)
            self.assertIs(service.validate_decision(value, request(phase)), value)
        for text in ('Gunakan quantity 0.5 dari Neurobro.',
                     'Pakai ukuran posisi 0.5 untuk entry.',
                     'Ganti quantity eksekusi dengan 0.5 dari rekomendasi Neurobro.'):
            with self.subTest(text=text), self.assertRaises(service.DialogueError):
                service.validate_decision(decision('ANALYSIS', text), request())

    def test_display_only_projection_preserves_actual_fees_pnl_and_decimal_prices(self):
        original = ('ENTRY 17.25, SL 17.00. Anggaran risiko 5.123 USDT. '
            'PNL aktual -5 USDT; fee entry 0.15 USDT; quantity terisi 5. '
            'TP 17.85 berdasarkan struktur 1H.')
        before = hashlib.sha256(original.encode()).hexdigest()
        shown = web_display_text(original)
        self.assertTrue(web_prompt_allowed(shown))
        self.assertIn('ENTRY 17.25, SL 17.00.', shown)
        self.assertIn('PNL aktual -5 USDT;', shown)
        self.assertIn('fee entry 0.15 USDT;', shown)
        self.assertIn('quantity terisi 5.', shown)
        self.assertIn('TP 17.85', shown)
        self.assertIn('[Bagian privat tidak ditampilkan;', shown)
        self.assertNotIn('5.123', shown)
        self.assertEqual(hashlib.sha256(original.encode()).hexdigest(), before)

    def test_schema_keeps_three_fields_and_describes_private_budget_and_no_sizing(self):
        self.assertIn('Jangan meminta rekomendasi quantity atau ukuran posisi', INSTRUCTIONS)
        self.assertIn('ENTRY dan SL struktural Neurobro tidak boleh diubah', INSTRUCTIONS)
        self.assertIn('Tidak ada batas putaran diskusi yang kaku', INSTRUCTIONS)
        for phase in ('ANALYSIS', 'COMPOSE_ANALYSIS'):
            schema = dialogue_schema(phase, request()['reply'], context={'symbol': 'LINKUSDT'})
            self.assertEqual(set(schema['properties']), {'action', 'symbols', 'question'})
            self.assertFalse(schema['additionalProperties'])
            description = schema['properties']['question']['description']
            self.assertIn('Jangan ungkap anggaran risiko pribadi', description)
            self.assertIn('meminta quantity/ukuran posisi', description)
            self.assertIn('RISK REWARD 1:2', description)

    def test_unsafe_old_render_receipt_stays_readable_but_fresh_draft_cannot_reach_web(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            formatter = lambda payload: payload['prompt'] + '\n\nOUTPUT_JSON_SCHEMA:\n{}\n' + payload['marker']
            payload = dict(operation='owned:analysis:one', prompt='Analisa LINKUSDT.',
                marker='HARUN_private', output_schema={'required': ['symbol', 'side']})
            native = SimpleNamespace(user_prompt=formatter)
            cache = PromptCompositionCache(native, root)
            for text in (ADVICE, 'Anggaran risiko 5 USDT.'):
                with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'RENDER_UNVERIFIED'):
                    cache.store(payload, text, 'b' * 64)
            cache.store(payload, prompts.ANALYSIS, 'b' * 64)
            path = root / cache._name(payload)
            record = json.loads(path.read_bytes())
            record['rendered_prompt'] += '\nRisk budget: 5 USDT.'
            record['rendered_sha256'] = hashlib.sha256(record['rendered_prompt'].encode()).hexdigest()
            path.write_bytes(service.canonical(record).encode())
            self.assertEqual(cache.read(payload), record)
            self.assertEqual(native.user_prompt(payload), record['rendered_prompt'])
            self.assertFalse(web_prompt_allowed(native.user_prompt(payload)))

    def test_screening_and_capacity_literals_stay_exact_while_analysis_uses_structure(self):
        original = 'pilihkan 2 coin yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance'
        self.assertEqual(prompts.SCREENING, original)
        self.assertEqual(screening_contract(2)[0], original)
        self.assertEqual(screening_contract(1)[0], original.replace('2 coin', '1 coin', 1))
        self.assertEqual(prompts.REPLACEMENT_SCREENING,
            'pilihkan {count} coin pengganti yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance')
        self.assertTrue(web_prompt_allowed(prompts.ANALYSIS))
        for clause in ('RISK REWARD 1:2', 'struktur pasar', 'swing high/low', 'likuiditas',
                       'volatilitas', 'wick', 'false breakout', 'invalidasi'):
            self.assertIn(clause, prompts.ANALYSIS)


if __name__ == '__main__': unittest.main()
