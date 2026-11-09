"""Actual draft grammar, with explicit offline public-price source fixtures."""
import hashlib
import json
from pathlib import Path
import unittest

from agent import dialogue_service as current
from agent.native_market_context import convert_native_market_context
from tests.test_agent_native_market_context import native_context
from tests.test_mini_dialogue_profile import retained_module


class DiscussionStructureSourceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=json.loads((Path(__file__).parent/'fixtures/mini_dot_compose_structural_numbers_20261109.json').read_text())
        self.question=self.fixture['question'];now=1794218842.0
        native=native_context(now);native['symbol']='DOTUSDT';native['mark_price']['price']='0.8510'
        # These are OFFLINE source-check fixtures, not a reconstruction of the
        # unknown original request or production Binance chart/candle evidence.
        for frame in native['timeframes'].values():
            frame['symbol']='DOTUSDT'
            for row in frame['candles']:
                row.update(open='0.8506',high='0.8577',low='0.8391',close='0.8522',volume='0.8510')
        self.request={'phase':'COMPOSE_ANALYSIS','reply':'Analisis DOTUSDT dan RISK REWARD 1:2.',
            'market_context':convert_native_market_context(native,now=now)}
        self.services=(current,retained_module())

    def test_exact_actual_draft_preserves_all_sourced_prices_and_discussion_text(self):
        self.assertEqual(hashlib.sha256(self.question.encode()).hexdigest(),self.fixture['question_sha256'])
        decision={'action':'PROMPT','symbols':[],'question':self.question}
        for service in self.services:
            with self.subTest(source=service.__name__):
                service.validate_question_sources(self.question,self.request)
                service.validate_web_question(self.question,self.request)
                # The actual source draft/price fixture remains immutable; only
                # its standalone internal privacy reminder is projected off WEB.
                expected={**decision,'question':self.question.replace(
                    ' jangan minta quantity atau ukuran posisi.','')}
                self.assertEqual(service.validate_decision(decision,self.request),expected)
                self.assertEqual(decision['question'],self.question)

    def test_unsourced_entry_sl_tp_prices_remain_rejected_inside_or_after_lists(self):
        for service in self.services:
            for question in (self.question+' ENTRY 123456.789.',self.question+' SL 123456.789.',
                    self.question+' TP 123456.789.',
                    'Output: 1) ENTRY 123456.789, 2) SL 0.8391, 3) TP 0.8577.'):
                with self.subTest(source=service.__name__,question=question[-65:]),self.assertRaisesRegex(
                        service.DialogueError,'DIALOGUE_PRICE_PROPOSAL_REFUSED'):
                    service.validate_question_sources(question,self.request)

    def test_only_structural_sequential_list_labels_are_exempt(self):
        for service in self.services:
            for question in ('Output yang diminta: 1) ENTRY (harga), 2) SL (harga), 3) TP (harga).',
                    '1) Struktur\n2) Momentum\n3) Invalidasi'):
                service.validate_question_sources(question,self.request)
            for question in ('ENTRY: 1) harga, 2) SL, 3) TP',
                    'Output: 1) fakta, 3) arah','Output: 4) ENTRY',
                    'ENTRY 3; SL 4; TP 6'):
                with self.subTest(source=service.__name__,question=question),self.assertRaisesRegex(
                        service.DialogueError,'DIALOGUE_PRICE_PROPOSAL_REFUSED'):
                    service.validate_question_sources(question,self.request)

    def test_only_explicit_rr_context_is_exempt_not_price_field_colons_or_decimals(self):
        for service in self.services:
            for question in ('Bahas RR 1:3 dan R:R 1:4.','RISK REWARD minimal 1:3/1:4.',
                    'rekomendasi TP (boleh 1:3/1:4).'):
                service.validate_question_sources(question,self.request)
            for question in ('ENTRY 1:3','SL 1:4','TP 1:5','TP (1:3/1:4)','RR 1:3.1415'):
                with self.subTest(source=service.__name__,question=question),self.assertRaisesRegex(
                        service.DialogueError,'DIALOGUE_PRICE_PROPOSAL_REFUSED'):
                    service.validate_question_sources(question,self.request)

    def test_utc_requires_the_exact_sourced_instant_and_privacy_guard_is_unchanged(self):
        for service in self.services:
            service.validate_question_sources(self.question+' Cutoff 2026-11-09 10:07:22 UTC.',self.request)
            with self.assertRaisesRegex(service.DialogueError,'DIALOGUE_PRICE_PROPOSAL_REFUSED'):
                service.validate_question_sources(self.question+' Cutoff 2026-11-09 10:07:23 UTC.',self.request)
            with self.assertRaisesRegex(service.DialogueError,'DIALOGUE_MODEL_RESULT_INVALID'):
                service.validate_web_question(self.question+' Risiko Office 5 USDT.',self.request)


if __name__=='__main__':unittest.main()
