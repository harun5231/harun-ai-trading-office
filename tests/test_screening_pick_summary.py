"""Read the actual received pick summary without promoting prose to a setup."""
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest

from agent.neurobro_reply import (
    ReplyIncomplete, parse_analysis_reply, parse_reply_for_payload,
    parse_screening_reply, screening_market_hold,
)
from worker.account_state import screening_contract


FIXTURE = Path(__file__).parent / 'fixtures/neurobro_screening_pick_summary_20261109.txt'


class ScreeningPickSummaryTests(unittest.TestCase):
    def setUp(self):
        self.reply = FIXTURE.read_text()
        self.summary = ('Ringkasan pick\n\n'
            '$DOTUSDT Perpetual - LONG, squeeze dari funding sangat negatif\n'
            '$LINKUSDT Perpetual - LONG, likuiditas top + short mulai kejepit')

    def payload(self, count):
        _, schema = screening_contract(count)
        return {'output_schema': schema}

    def test_original_received_reply_yields_only_the_two_authored_choices(self):
        self.assertEqual(hashlib.sha256(self.reply.encode()).hexdigest(),
            '1eb7348314017f039c07c51ac584e598541fcb4edf5465dfd8c4062c6cd264e6')
        self.assertEqual(parse_reply_for_payload(self.reply, self.payload(2)),
            {'symbols': ['DOTUSDT', 'LINKUSDT']})
        # Screening prose contains prices, but this contract returns no setup.
        for symbol in ('DOTUSDT', 'LINKUSDT'):
            with self.subTest(symbol=symbol), self.assertRaises(ReplyIncomplete):
                parse_analysis_reply(self.reply, symbol)

    def test_capacity_still_comes_from_the_original_request(self):
        with self.assertRaisesRegex(ReplyIncomplete, 'SCREENING_COUNT_MISMATCH'):
            parse_reply_for_payload(self.reply, self.payload(1))
        one = 'Ringkasan pick:\n$DOTUSDT Perpetual - LONG, struktur kuat.'
        self.assertEqual(parse_reply_for_payload(one, self.payload(1)), {'symbols': ['DOTUSDT']})
        with self.assertRaisesRegex(ReplyIncomplete, 'SCREENING_COUNT_MISMATCH'):
            parse_reply_for_payload(one, self.payload(2))

    def test_bare_tickers_and_comparison_prose_are_not_an_explicit_summary(self):
        prefix = 'Perbandingan BTCUSDT dan ETHUSDT, bukan pilihan.\n\n'
        self.assertEqual(parse_screening_reply(prefix + self.summary),
            {'symbols': ['DOTUSDT', 'LINKUSDT']})
        for reply in (prefix, '$DOTUSDT Perpetual - LONG\n$LINKUSDT Perpetual - LONG',
                self.summary.replace('Ringkasan pick', 'Ringkasan pick yang mungkin dipilih')):
            with self.subTest(reply=reply), self.assertRaises(ReplyIncomplete):
                parse_screening_reply(reply)

    def test_comparison_or_foreign_coin_in_a_selection_reason_remains_ambiguous(self):
        for reason in ('LONG dibanding BTCUSDT', 'LONG atau BTCUSDT',
                'LONG, pantau BTCUSDT', 'LONG, alternatif untuk besok'):
            reply = 'Ringkasan pick\n$DOTUSDT Perpetual - ' + reason
            with self.subTest(reason=reason), self.assertRaises(ReplyIncomplete):
                parse_screening_reply(reply)

    def test_conflicting_prior_selection_requires_an_explicit_final_override(self):
        conflict = 'COIN: BTCUSDT\n\n' + self.summary
        with self.assertRaisesRegex(ReplyIncomplete, 'SCREENING_AMBIGUOUS'):
            parse_screening_reply(conflict)
        self.assertEqual(parse_screening_reply('COIN: BTCUSDT\nRingkasan akhir\n' + self.summary),
            {'symbols': ['DOTUSDT', 'LINKUSDT']})
        self.assertEqual(parse_screening_reply('COIN: DOTUSDT, LINKUSDT\n\n' + self.summary),
            {'symbols': ['DOTUSDT', 'LINKUSDT']})

    def test_duplicate_or_multiple_explicit_summaries_are_not_silently_selected(self):
        for reply in (
                self.summary.replace('$LINKUSDT', '$DOTUSDT'),
                self.summary + '\n\nRingkasan pick:\n$BTCUSDT Perpetual - LONG',
                self.summary + '\n\nCOIN YANG DIPILIH:\nBTCUSDT Perpetual - LONG',
                self.summary + '\n\nCOIN: BTCUSDT'):
            with self.subTest(reply=reply), self.assertRaises(ReplyIncomplete):
                parse_screening_reply(reply)

    def test_conditional_selection_cannot_become_a_final_pick(self):
        for reply in (
                'Jika support bertahan, daftar ini berlaku.\n\n' + self.summary,
                self.summary.replace('LONG, squeeze', 'LONG jika breakout, squeeze'),
                self.summary + '\nApabila breakout terjadi, gunakan daftar ini.'):
            with self.subTest(reply=reply), self.assertRaises(ReplyIncomplete):
                parse_screening_reply(reply)

    def test_extra_or_malformed_selection_rows_are_not_hidden_by_a_paragraph(self):
        for suffix in (
                '\n$BTCUSDT Perpetual - LONG',
                '\n$BTCUSDT Perpetual LONG',
                '\n\nCatatan lain.\n$BTCUSDT Perpetual - LONG',
                '\n\nBTC: ETHUSDT Perpetual - LONG'):
            with self.subTest(suffix=suffix), self.assertRaises(ReplyIncomplete):
                parse_screening_reply(self.summary + suffix)

    def test_explicit_market_hold_uses_the_same_pick_heading_without_inventing_hold(self):
        held = self.summary.replace('LONG, squeeze dari funding sangat negatif', 'HOLD, tunggu struktur')
        held = held.replace('LONG, likuiditas top + short mulai kejepit', 'HOLD, momentum belum jelas')
        selected = ['DOTUSDT', 'LINKUSDT']
        self.assertTrue(screening_market_hold(held, selected))
        self.assertFalse(screening_market_hold(self.summary, selected))
        self.assertFalse(screening_market_hold(held.replace('HOLD, momentum', 'LONG, momentum'), selected))
        self.assertFalse(screening_market_hold('Data chart belum terbaca.\n' + held, selected))

    def test_original_pick_reply_still_requires_real_review_and_recovers_once_after_restart(self):
        from tests import test_generic_screening_response_recovery as generic
        from worker.core import Review
        from worker import screening_response_recovery as recovery
        case = generic.GenericScreeningResponseRecoveryTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        operation = generic.CYCLE + ':screening:9:2'
        case.context['requested_count'] = 2
        case.context['excluded_symbols'].remove('LINKUSDT')
        _, schema = screening_contract(2)
        case.body, encoded, case.request_hash = case.source.request_body(
            operation, case.body['prompt'], schema, case.context)
        case.payload = {**case.body, 'request_sha256': case.request_hash}
        case.catalog.update(DOTUSDT={}, LINKUSDT={})
        case.db.execute('UPDATE api_requests SET operation=?,body_hash=?,created=? WHERE operation=?',
            (operation, case.request_hash, generic.NOW-1000, generic.OP))
        case.db.execute('UPDATE robot_jobs SET operation=? WHERE operation=?', (operation, generic.OP))
        case.db.execute('UPDATE research_browser_requests SET operation=?,request_sha256=?,marker=?,request_body=? '
            'WHERE operation=?', (operation, case.request_hash, case.body['marker'], encoded, generic.OP))
        case.db.execute('UPDATE robot_cycles SET data=?', (json.dumps(case.data),))
        missing = case.fixture(approval=False, operation=operation, reply=self.reply)
        with self.assertRaisesRegex(Review, 'SCREENING_MINI_APPROVAL_NOT_RETAINED'):
            recovery.recover_received_screening(case.provider, missing, catalog=case.catalog,
                max_age=180, now=generic.NOW)
        self.assertEqual(case.db.execute('SELECT count(*) FROM research_browser_responses').fetchone()[0], 0)
        envelope = case.fixture(operation=operation, reply=self.reply, symbols=['DOTUSDT', 'LINKUSDT'])
        record = case.provider._record_browser_response
        def interrupted(*args, **kwargs):
            record(*args, **kwargs)
            raise RuntimeError('controlled restart after original native receipt')
        case.provider._record_browser_response = interrupted
        with self.assertRaisesRegex(RuntimeError, 'controlled restart'):
            recovery.recover_received_screening(case.provider, envelope, catalog=case.catalog,
                max_age=180, now=generic.NOW)
        receipt = tuple(case.db.execute('SELECT * FROM research_browser_responses').fetchone())
        restarted = sqlite3.connect(':memory:', isolation_level=None)
        restarted.row_factory = sqlite3.Row
        self.addCleanup(restarted.close)
        case.db.backup(restarted)
        case.provider.db = restarted
        case.provider._record_browser_response = case.forbid
        self.assertEqual(recovery.recover_received_screening(case.provider, envelope,
            catalog=case.catalog, max_age=180, now=generic.NOW+1), 'NOEXEC')
        self.assertEqual(tuple(restarted.execute('SELECT * FROM research_browser_responses').fetchone()), receipt)
        self.assertEqual(tuple(restarted.execute('SELECT operation,state,attempts FROM api_requests').fetchone()),
            (operation, 'RECEIVED_STALE_NOT_EXECUTED', 1))
        self.assertEqual(restarted.execute('SELECT phase FROM screening_response_recoveries').fetchone()[0], 'NOEXEC')
        before = tuple(restarted.iterdump())
        self.assertEqual(recovery.recover_received_screening(case.provider, envelope,
            catalog=case.catalog, max_age=180, now=generic.NOW+2), 'NOEXEC')
        self.assertEqual(tuple(restarted.iterdump()), before)


if __name__ == '__main__':
    unittest.main()
