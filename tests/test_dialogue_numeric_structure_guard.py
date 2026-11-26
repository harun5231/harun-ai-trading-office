"""Independent adversarial checks: discussion notation never hides price data.

These are local source-attribution fixtures. No model, browser or order call is
needed to distinguish a list marker or complete RR range from a quoted price.
"""
import unittest
from agent import dialogue_service as service


class NumericDiscussionGuardTests(unittest.TestCase):
    def setUp(self):
        self.request={'phase':'COMPOSE_ANALYSIS','reply':'WIFUSDT, harga referensi 0.1343; RISK REWARD 1:2.',
                      'market_context':{'symbol':'WIFUSDT','mark_price':'0.1343'}}

    def valid(self,question):
        before=dict(self.request)
        service.validate_question_sources(question,self.request)
        self.assertEqual(service.validate_web_question(question,self.request),question)
        self.assertEqual(self.request,before)

    def rejected(self,question):
        with self.assertRaisesRegex(service.DialogueError,'DIALOGUE_PRICE_PROPOSAL_REFUSED'):
            service.validate_question_sources(question,self.request)
        with self.assertRaisesRegex(service.DialogueError,'DIALOGUE_PRICE_PROPOSAL_REFUSED'):
            service.validate_web_question(question,self.request)

    def test_complete_rr_ranges_accept_supported_dash_variants_without_consuming_other_values(self):
        for dash in ('-','\u2013','\u2014'):
            for spaces in ('',' '):
                question='Bahas RR 1:3'+spaces+dash+spaces+'1:4 berdasarkan mark price 0.1343.'
                with self.subTest(dash=dash,spaces=spaces):self.valid(question)
                with self.subTest(unsourced_price=dash):self.rejected(question+' ENTRY 987.654.')
                self.assertIn('0.1343',service._without_discussion_structure(question))

    def test_decimal_price_fields_and_incomplete_rr_ranges_are_still_numeric_source_checks(self):
        for question in ('ENTRY 1:3–1:4.', 'SL 1:3-1:4.', 'TP (1:3—1:4).',
                         'RR 1:3–1:987.654.', 'RR 1:3.14159–1:4.',
                         'RR 1:3–ENTRY 987.654.', 'RR 1:3. 987.654.',
                         'RR 1:3–1:4,12345.', 'RR 1:3–1:4; SL 987.654.'):
            with self.subTest(question=question):self.rejected(question)

    def test_period_separated_sequential_instructions_do_not_turn_into_price_proposals(self):
        for intro in ('Persyaratan','Output','Syarat'):
            question=intro+': 1) Jelaskan struktur dan wick. 2) Nilai likuiditas. 3) Tetapkan invalidasi.'
            with self.subTest(intro=intro):self.valid(question)
        question='Persyaratan: 1) Jelaskan struktur. 2) Nilai SL berdasarkan chart. 3) Bahas RR 1:3–1:4.'
        self.valid(question)

    def test_periods_in_decimal_values_and_price_text_inside_lists_are_not_exempt(self):
        for question in ('Persyaratan: 1) Jelaskan ENTRY 987.654. 2) Nilai likuiditas. 3) Tetapkan SL.',
                         'Persyaratan: 1) Jelaskan struktur 0.3) dan wick. 2) Nilai likuiditas. 3) Tetapkan invalidasi.',
                         'ENTRY: 1) Jelaskan struktur. 2) Nilai likuiditas. 3) Tetapkan invalidasi.',
                         'Persyaratan ENTRY: 1) Jelaskan struktur. 2) Nilai likuiditas. 3) Tetapkan invalidasi.',
                         'Persyaratan: 1) Jelaskan struktur. 3) Tetapkan invalidasi.',
                         'Persyaratan: 1) Jelaskan struktur. 2) Nilai likuiditas. 4) Tetapkan invalidasi.'):
            with self.subTest(question=question):self.rejected(question)

    def test_unicode_numeric_lookalikes_and_private_budget_still_fail(self):
        for question in ('Bahas RR １:３–１:４.',
                         'Persyaratan: １) Jelaskan struktur. ２) Tetapkan invalidasi.'):
            with self.subTest(question=question):self.rejected(question)
        with self.assertRaises(service.DialogueError):
            service.validate_web_question('Bahas RR 1:3–1:4. Risiko Office 5 USDT.',self.request)


if __name__=='__main__':unittest.main()
