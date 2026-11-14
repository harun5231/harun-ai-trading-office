"""The retained Mini reading owns quote aliases; source replies stay exact."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent.neurobro_reply import ReplyIncomplete, parse_screening_reply, screening_interpretation
from tests import test_generic_screening_response_recovery as worker_fixture
from tests import test_native_browser_hook as hook_fixture
from tests.test_semantic_screening import bound, digest, receipt
from worker import screening_response_recovery as recovery
from worker.account_state import screening_contract
from worker.core import Review


FIXTURE = Path(__file__).parent / 'fixtures/neurobro_screening_quote_separator_20261114.json'


class ScreeningQuoteSeparatorTests(unittest.TestCase):
    def setUp(self):
        self.actual = json.loads(FIXTURE.read_text())
        self.reply = self.actual['actual_reply']
        self.symbols = self.actual['retained_screening_reviews'][0]['response']['decision']['symbols']

    def arguments(self, reply=None):
        reply = self.reply if reply is None else reply
        args = bound(reply, self.symbols)
        args[0]['context']['excluded_symbols'] = self.actual['excluded_symbols']
        return args

    def test_exact_production_reply_uses_actual_mini_choices_without_mutation(self):
        self.assertEqual(len(self.reply.encode()), 830)
        self.assertEqual(hashlib.sha256(self.reply.encode()).hexdigest(), self.actual['reply_sha256'])
        self.assertEqual(self.symbols, ['AVAXUSDT', 'NEARUSDT'])
        actual = self.actual['retained_screening_reviews'][0]
        self.assertEqual(actual['identity']['reply_sha256'], self.actual['reply_sha256'])
        self.assertEqual(actual['model_operation']['state'], 'DONE')
        self.assertFalse(actual['response']['needs_followup'])
        args = self.arguments(); before = copy.deepcopy(args)
        self.assertEqual(screening_interpretation(self.reply, *args), {'symbols': self.symbols})
        self.assertEqual(args, before)
        # An unauthenticated parser cannot acquire Mini's alias authority.
        with self.assertRaisesRegex(ReplyIncomplete, 'JSON_SUMMARY_INVALID'):
            parse_screening_reply(self.reply)

    def test_quote_separator_and_outer_space_are_generic_whole_field_aliases(self):
        symbols = ['AAVEUSDT', '1000PEPEUSDT']
        for choices in (['AAVE_USDT', '1000PEPE_USDT'], [' aave_usdt ', '\t1000PEPEUSDT\n'],
                [' AAVE ', '1000PEPE']):
            reply = 'Pilihan saya AAVEUSDT dan 1000PEPEUSDT.\n' + json.dumps({'SYMBOLS': choices})
            with self.subTest(choices=choices):
                self.assertEqual(screening_interpretation(reply, *bound(reply, symbols)), {'symbols': symbols})
                with self.assertRaises(ReplyIncomplete): parse_screening_reply(reply)

    def test_embedded_underscores_foreign_quotes_and_extra_content_do_not_name_a_pair(self):
        for invalid in ('AV_AX_USDT', 'AVAX__USDT', '_AVAX_USDT', 'AVAX_USDT_', 'AVAX_USDC',
                'AVAX_USDT Perpetual', '$AVAX_USDT', 'AVAX_USDT/NEAR_USDT', 'BTC_USDT', None):
            reply = 'AVAXUSDT dan NEARUSDT dipilih.\n' + json.dumps({'SYMBOLS': [invalid, 'NEAR_USDT']})
            with self.subTest(invalid=invalid), self.assertRaises(ReplyIncomplete):
                screening_interpretation(reply, *self.arguments(reply))
        for summary in ('{"SYMBOLS":["AVAX_USDT","AVAXUSDT"]}',
                '{"SYMBOLS":["AVAX_USDT","NEAR_USDT"],"ENTRY":"1"}',
                '{"SYMBOLS":["AVAX_USDT","NEAR_USDT"],"SYMBOLS":["AVAXUSDT","NEARUSDT"]}',
                '{"SYMBOLS":["AVAX_USDT","NEAR_USDT"]',
                '{"SYMBOLS":["AVAX_USDT"]}'):
            reply = 'AVAXUSDT dan NEARUSDT dipilih.\n' + summary
            with self.subTest(summary=summary), self.assertRaises(ReplyIncomplete):
                screening_interpretation(reply, *self.arguments(reply))

    def test_original_source_capacity_exclusion_and_mini_authority_remain_mandatory(self):
        for change in ('source', 'count', 'excluded', 'request', 'transcript', 'hold', 'clarify'):
            reply = '{"SYMBOLS":["AVAX_USDT","NEAR_USDT"]}' if change == 'source' else self.reply
            args = self.arguments(reply)
            if change == 'count': args[0]['context']['requested_count'] = 1
            if change == 'excluded': args[0]['context']['excluded_symbols'].append('NEARUSDT')
            if change == 'request': args[1]['request_id'] = 'e' * 64
            if change == 'transcript': args[3][-1]['text'] += ' changed'
            if change in ('hold', 'clarify'):
                args[2]['decision'].update(action=change.upper(), symbols=[], question='Apa pilihan final?' if change == 'clarify' else '')
                args[2]['needs_followup'] = change == 'clarify'
            with self.subTest(change=change), self.assertRaises(ReplyIncomplete):
                screening_interpretation(reply, *args)

    def test_independent_final_and_prior_json_conflicts_are_not_hidden_by_an_alias(self):
        for prefix in ('Keputusan final\nCOIN: BTCUSDT, ETHUSDT',
                'Keputusan final\nCOIN: BTC_USDT, ETH_USDT',
                'Keputusan final: $BTC_USDT dan $ETH_USDT',
                '{"SYMBOLS":["BTCUSDT","ETHUSDT"]}',
                '{"SYMBOLS":["BTC_USDT","ETH_USDT"]}'):
            reply = ('AVAXUSDT dan NEARUSDT hanya pembanding.\n\n' + prefix
                + '\n\n{"SYMBOLS":["AVAX_USDT","NEAR_USDT"]}')
            with self.subTest(prefix=prefix), self.assertRaises(ReplyIncomplete):
                screening_interpretation(reply, *self.arguments(reply))
        reply = ('Binance Futures: AVAXUSDT dan NEARUSDT.\n\n'
            'Keputusan final\nCOIN: $AVAX_USDT, $NEAR_USDT\n\n'
            'Alasan: lebih layak dibanding BTC_USDT dan ETH_USDT.\n\n'
            '{"SYMBOLS":["AVAX_USDT","NEAR_USDT"]}')
        self.assertEqual(screening_interpretation(reply, *self.arguments(reply)), {'symbols': self.symbols})

    def test_browser_reuses_done_interpretation_without_a_reformat_or_model_call(self):
        payload = self.arguments()[0]
        payload['operation'] = self.actual['operation']
        payload['marker'] = 'HARUN_' + hashlib.sha256(payload['operation'].encode()).hexdigest()[:32]
        case = hook_fixture.ComposedMaterialGateTests(); case.harness(payload)
        self.addCleanup(case.doCleanups)
        case.bridge.initial = self.reply
        case.hook.daily_room.day = '2026-11-14'; case.hook.daily_room.room_id = 'a' * 32
        case.hook.daily_room.conversation_id = 'neurobro:2026-11-14:' + 'a' * 32
        case.hook.prompt_cache.store(payload, payload['prompt'], 'f' * 64)
        def completed(request):
            response = receipt(request, self.symbols)
            return {'result': response, 'operation': {'operation_id': 'delegated-model:' + request['request_id'],
                'kind': 'DELEGATED_MODEL_DIALOGUE', 'request_hash': digest(request), 'state': 'DONE',
                'result': json.dumps(response, sort_keys=True, separators=(',', ':')), 'updated_at': 1793864323.0}}
        case.reasoner.supports_screening_receipt_lookup = True
        case.reasoner.completed_screening = Mock(side_effect=completed)
        result = case.hook.ask(payload, lambda: True, deadline=80)
        self.assertEqual(result['output'], {'symbols': self.symbols})
        self.assertEqual(result['assistant_text'], self.reply)
        self.assertEqual(result['screening_interpretation']['review_receipt']['value']['response']['decision'],
            self.actual['retained_screening_reviews'][0]['response']['decision'])
        self.assertEqual(case.bridge.clicks, 1)  # Only the mocked initial Send, no clarification.
        case.reasoner.request.assert_not_called(); case.reasoner.compose.assert_not_called()
        case.converter.assert_not_called()

    def test_worker_reuses_bound_approval_and_preserves_stale_reply_once_after_restart(self):
        op = self.actual['operation']; cycle = op.rsplit(':screening:', 1)[0]
        now = 1794672444.7094421
        scope = patch.multiple(worker_fixture, NOW=now, DAY=op[:10], CYCLE=cycle, OP=op)
        scope.start(); self.addCleanup(scope.stop)
        case = worker_fixture.GenericScreeningResponseRecoveryTests(); case.setUp()
        self.addCleanup(case.doCleanups)
        _, schema = screening_contract(2)
        case.context = {'requested_count': 2, 'excluded_symbols': self.actual['excluded_symbols'],
            'replacement_instruction': 'Choose different Binance USD-M USDT perpetual coins that are not in excluded_symbols.'}
        case.body, encoded, case.request_hash = case.source.request_body(op, 'Controlled offline replacement screening.', schema, case.context)
        case.payload = {**case.body, 'request_sha256': case.request_hash}
        case.created = 1794671074.217222
        case.db.execute('UPDATE api_requests SET body_hash=?,created=?', (case.request_hash, case.created))
        case.db.execute('UPDATE research_browser_requests SET request_sha256=?,marker=?,request_body=?',
            (case.request_hash, case.body['marker'], encoded))
        case.data.update(screen=4, seen=case.context['excluded_symbols'], replacements=4, round_symbols=[])
        case.db.execute('UPDATE robot_cycles SET data=?', (json.dumps(case.data),))
        envelope = case.fixture(reply=self.reply, operation=op, symbols=self.symbols)
        original = copy.deepcopy(envelope); finances = case.finances()
        catalog = {s: {} for s in self.symbols}
        case.db.execute(recovery.RECEIVED_SCHEMA)
        before = tuple(case.db.iterdump())
        for invalid in ('UNKNOWN', 'wrong-result'):
            forged = copy.deepcopy(envelope); entry = forged['observation']['review_receipts'][0]
            if invalid == 'UNKNOWN': entry['value']['model_operation']['state'] = 'UNKNOWN'
            else: entry['value']['model_operation']['result'] = '{"forged":"approval"}'
            raw = recovery._wire(entry['value'], ascii=True)
            entry.update(sha256=recovery._sha(raw), bytes=len(raw.encode())); case.seal(forged)
            with self.subTest(approval=invalid), self.assertRaises(Review):
                recovery.recover_received_screening(case.provider, forged, catalog=catalog, max_age=180, now=now)
            self.assertEqual(tuple(case.db.iterdump()), before)
        self.assertEqual(recovery.recover_received_screening(case.provider, envelope, catalog=catalog, max_age=180, now=now), 'NOEXEC')
        self.assertEqual(case.row('api_requests', op)['state'], recovery.RECEIVED_TERMINAL)
        self.assertEqual(case.row('api_requests', op)['attempts'], 1)
        self.assertEqual(case.row('robot_jobs', op)['state'], 'COMPLETE')
        actual_response = case.row('research_browser_responses', op)
        self.assertEqual(actual_response['assistant_text'], self.reply)
        self.assertEqual(actual_response['assistant_sha256'], self.actual['reply_sha256'])
        self.assertEqual(envelope, original); self.assertEqual(case.finances(), finances)
        before = tuple(case.db.iterdump())
        self.assertEqual(recovery.recover_received_screening(case.provider, envelope, catalog=catalog, max_age=180, now=now), 'NOEXEC')
        self.assertEqual(tuple(case.db.iterdump()), before)
        with tempfile.TemporaryDirectory() as directory:
            restarted = sqlite3.connect(str(Path(directory) / 'ledger.sqlite3'), isolation_level=None)
            restarted.row_factory = sqlite3.Row; case.db.backup(restarted); self.addCleanup(restarted.close)
            provider = object.__new__(case.native.NeuroBrowser); provider.db = restarted; provider.chart_max_age = 180
            provider.transport = provider.ask = provider.require_key = case.forbid
            changes = restarted.total_changes
            self.assertEqual(recovery.recover_received_screening(provider, envelope, catalog=catalog, max_age=180, now=now+10), 'NOEXEC')
            self.assertEqual(restarted.total_changes, changes)
            self.assertEqual(restarted.execute('SELECT COUNT(*) FROM screening_response_recoveries').fetchone()[0], 1)


if __name__ == '__main__': unittest.main()
