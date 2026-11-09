"""Authored market HOLD custody; no model, browser or order calls."""
from pathlib import Path
import json
import hashlib
import unittest

from agent.neurobro_reply import author_market_hold, parse_analysis_reply


class AuthorMarketHoldTests(unittest.TestCase):
    def test_present_final_hold_permits_separate_future_entry_conditions(self):
        cases = (
            'COIN: BTCUSDT\nARAH: HOLD\nJika struktur berubah nanti, evaluasi ulang SHORT.',
            'COIN: BTCUSDT\nKEPUTUSAN: HOLD sekarang; jika breakout terjadi nanti, analisis ulang.',
            'COIN: BTCUSDT\nARAH: HOLD untuk saat ini. Jika swing berubah nanti, evaluasi ulang.',
            'BTC/USDT — HOLD\nSkenario LONG hanya dipertimbangkan apabila swing terkonfirmasi.',
            'Ringkasan: COIN: BTCUSDT | HOLD | ENTRY: — | TP: — | SL: —\n'
            'Alternatif masa depan belum aktif; jika struktur berubah, ambil chart terbaru.',
            'COIN: BTCUSDT\nARAH: SHORT\nSkenario lama bersyarat.\n'
            'Ringkasan akhir:\nCOIN: BTCUSDT\nARAH: HOLD\nJika reclaim terjadi nanti, tinjau lagi.')
        for reply in cases:
            with self.subTest(reply=reply):
                self.assertTrue(author_market_hold(reply, 'BTCUSDT'))
                self.assertEqual(parse_analysis_reply(reply, 'BTCUSDT')['side'], 'HOLD')

    def test_existing_authored_json_and_retained_market_holds_still_prove_hold(self):
        self.assertTrue(author_market_hold(
            '{"COIN":"ETHUSDT","ARAH":"HOLD","ENTRY":null,"TP":null,"SL":null}',
            'ETHUSDT'))
        for filename, symbol in (
            ('neurobro_rune_final_hold_20261105.txt', 'RUNEUSDT'),
            ('neurobro_doge_clear_hold_20261105.txt', 'DOGEUSDT')):
            with self.subTest(filename=filename):
                reply = (Path(__file__).parent / 'fixtures' / filename).read_text()
                self.assertTrue(author_market_hold(reply, symbol))

    def test_actual_rune_final_summary_supersedes_nonfinal_narrative_hold_clause(self):
        path = Path(__file__).parent / 'fixtures/neurobro_market_hold_20261105.json'
        original = path.read_bytes(); saved = json.loads(original)
        self.assertTrue(author_market_hold(saved['reply'], 'RUNEUSDT'))
        self.assertEqual(parse_analysis_reply(saved['reply'], 'RUNEUSDT')['side'], 'HOLD')
        self.assertEqual(path.read_bytes(), original)
        # A conditional final direction cannot borrow the earlier prose HOLD.
        conditional = saved['reply'].replace('| HOLD |', '| HOLD asalkan breakout gagal |')
        self.assertFalse(author_market_hold(conditional, 'RUNEUSDT'))

    def test_captured_fil_perpetual_final_summary_is_current_hold_not_a_technical_failure(self):
        saved=json.loads((Path(__file__).parent/'fixtures/neurobro_fil_captured_hold_20261110.json').read_text())
        source=saved['provenance'];reply=saved['verbatim_summary_fragment']
        self.assertFalse(source['original_raw_bytes_in_fixture'])
        self.assertEqual(source['original_assistant_sha256'],'8345ddf921d190b306b65153a828674de014176ecd4c8ef9e111ec74b03e0466')
        self.assertEqual(hashlib.sha256(saved['display_text'].encode()).hexdigest(),source['display_text_sha256'])
        self.assertIn(reply,saved['display_text']);self.assertTrue(author_market_hold(reply,'FILUSDT'))
        self.assertEqual(parse_analysis_reply(reply,'FILUSDT')['side'],'HOLD')
        # The reader normalized newlines for display. Restore only the known
        # summary boundary for this offline presentation check, not raw proof.
        presentation=saved['display_text'].replace('  Ringkasan:', '\n\nRingkasan:')
        self.assertTrue(author_market_hold(presentation,'FILUSDT'))
        for invalid in(reply.replace('FILUSDT','ATOMUSDT'),reply.replace('| HOLD |','| HOLD asalkan retest terjadi |'),
                'CSV belum diterima.\n'+reply,reply+'\nKEPUTUSAN: SHORT'):
            with self.subTest(invalid=invalid):self.assertFalse(author_market_hold(invalid,'FILUSDT'))

    def test_conditional_direction_and_conflicts_are_not_terminal_market_hold(self):
        cases = (
            'COIN: BTCUSDT\nARAH: HOLD jika reclaim gagal.',
            'COIN: BTCUSDT\nARAH: HOLD asalkan breakout gagal.',
            'COIN: BTCUSDT\nARAH: HOLD assuming the breakout fails.',
            'COIN: BTCUSDT\nARAH: HOLD provided the breakout fails.',
            'COIN: BTCUSDT\nARAH: HOLD?',
            'COIN: BTCUSDT\nARAH: HOLD...',
            'COIN: BTCUSDT\nARAH: HOLD karena alasan yang belum jelas.',
            'COIN: BTCUSDT\nAssuming the breakout fails:\nARAH: HOLD',
            'COIN: BTCUSDT\nKEPUTUSAN: HOLD hanya apabila breakout gagal.',
            'COIN: BTCUSDT\nJika reclaim gagal:\nARAH: HOLD',
            'Jika reclaim gagal, keputusan berikut berlaku:\nCOIN: BTCUSDT\nARAH: HOLD',
            'COIN: BTCUSDT\nARAH: HOLD atau SHORT setelah konfirmasi.',
            'COIN: BTCUSDT\nARAH: HOLD\nKEPUTUSAN: SHORT\nENTRY: 100\nSL: 101\nTP: 98',
            'BTCUSDT sebelumnya HOLD, keputusan sekarang belum jelas.',
            'COIN: BTCUSDT\nARAH: SHORT\nENTRY: 100\nSL: 101\nTP: 98')
        for reply in cases:
            with self.subTest(reply=reply):
                self.assertFalse(author_market_hold(reply, 'BTCUSDT'))

    def test_unreadable_material_foreign_coin_and_invalid_json_remain_unproved(self):
        cases = (
            'Screenshot belum terbaca.\nCOIN: BTCUSDT\nARAH: HOLD',
            'COIN: BTCUSDT\nARAH: HOLD\nCSV belum diterima.',
            'COIN: ETHUSDT\nARAH: HOLD',
            '{"COIN":"BTCUSDT","ARAH":"HOLD","ENTRY":"100","TP":null,"SL":null}')
        for reply in cases:
            with self.subTest(reply=reply):
                self.assertFalse(author_market_hold(reply, 'BTCUSDT'))

    def test_malformed_callers_do_not_supply_hold_authority(self):
        for reply, symbol in ((None, 'BTCUSDT'), ('HOLD', None), ('HOLD', 1)):
            with self.subTest(reply=reply, symbol=symbol):
                self.assertFalse(author_market_hold(reply, symbol))

    def test_other_coin_conditional_reason_does_not_replace_current_final_block(self):
        reply = ('COIN: ETHUSDT\nARAH: HOLD jika breakout gagal.\n'
            'COIN: BTCUSDT\nARAH: HOLD\nMomentum 1H/15M belum memberi setup layak.')
        self.assertTrue(author_market_hold(reply, 'BTCUSDT'))
        self.assertFalse(author_market_hold(reply, 'ETHUSDT'))
