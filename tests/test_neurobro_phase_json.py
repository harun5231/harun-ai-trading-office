"""Public final JSON is author evidence, never a replacement provider receipt."""
import hashlib
import json
from pathlib import Path
import unittest

from agent.neurobro_reply import (ReplyIncomplete, author_market_hold,
    parse_analysis_reply, parse_pending_reply, parse_reply_for_payload,
    parse_screening_reply)


def screening(count=2, excluded=()):
    return {'operation': '2026-11-11:screening:0:' + str(count),
        'context': {'requested_count': count, 'excluded_symbols': list(excluded)},
        'output_schema': {'required': ['symbols'], 'properties': {'symbols': {
            'minItems': count, 'maxItems': count}}}}


def pending():
    reference = 'a' * 64
    return {'operation': 'pending-review-v1:' + reference[:32] + ':2',
        'context': {'symbol': 'LINKUSDT', 'pending_review': {
            'symbol': 'LINKUSDT', 'intent_ref': reference}},
        'output_schema': {'required': ['symbol', 'order_ref', 'decision']}}


class PublicPhaseJsonTests(unittest.TestCase):
    def test_actual_link_reply_and_original_mini_receipt_choose_only_the_screening_coin(self):
        from agent.neurobro_reply import screening_interpretation
        from tests.test_semantic_screening import digest
        path = Path(__file__).parent / 'fixtures/neurobro_link_screening_original_20261111.json'
        original = path.read_bytes()
        saved = json.loads(original)
        reply = saved['reply']
        self.assertEqual(len(reply.encode('utf-8')), 1235)
        self.assertEqual(hashlib.sha256(reply.encode('utf-8')).hexdigest(),
            '15e3e25ac11373ef7c57efee46c155f26e6780e83babb07a44653904bf33e95a')
        self.assertEqual(saved['provenance']['review_file']['sha256'],
            '92a5dfaef2886baa8bb7df436a6cdc957ff96997dcb4e5d33cb4a475e5ae53a9')
        turns = [{'role': 'user', 'text': saved['latest_user'], 'attachments': []},
            {'role': 'assistant', 'text': reply, 'complete': True,
                'id': saved['assistant_message_id']}]
        self.assertEqual(digest(turns), saved['transcript_sha256'])
        payload = screening(1)
        payload.update(operation=saved['operation'], request_sha256=saved['request_sha256'])
        request = {'schema_version': 1, 'phase': 'SCREENING', 'market_context': None,
            'gate': {'robot_on': True, 'native_dispatch_verified': True},
            'request_id': saved['request_id'], 'conversation_id': saved['conversation_id'],
            'reply': reply, 'reply_sha256': saved['provenance']['reply_sha256']}
        self.assertEqual(digest(dict(operation=payload['operation'], request_sha256=payload['request_sha256'],
            reply_sha256=request['reply_sha256'], phase='SCREENING', transcript_sha256=digest(turns))),
            saved['request_id'])
        # Mini had already read this exact reply correctly. Missing public
        # JSON does not explain the missing native response commit.
        self.assertEqual(screening_interpretation(reply, payload, request,
            saved['mini_response'], turns), {'symbols': ['LINKUSDT']})
        self.assertEqual(parse_reply_for_payload(reply, payload), {'symbols': ['LINKUSDT']})
        # Unsolicited screening prices cannot become an analysis setup. The
        # existing four-file ANALYSIS admission still owns that next phase.
        with self.assertRaises(ReplyIncomplete):
            parse_analysis_reply(reply, 'LINKUSDT')
        self.assertEqual(path.read_bytes(), original)

    def test_screening_final_object_after_natural_explanation_and_fence(self):
        for count, names in ((1, ['LINKUSDT']), (2, ['ATOMUSDT', 'FILUSDT'])):
            body = json.dumps({'SYMBOLS': names})
            for final in (body, '```json\n' + body + '\n```'):
                reply = 'Pilihan ini memiliki struktur dan likuiditas terbaik saat ini.\n\n' + final
                digest = hashlib.sha256(reply.encode()).hexdigest()
                with self.subTest(names=names, final=final):
                    self.assertEqual(parse_screening_reply(reply), {'symbols': names})
                    self.assertEqual(parse_reply_for_payload(reply, screening(count)), {'symbols': names})
                    self.assertEqual(hashlib.sha256(reply.encode()).hexdigest(), digest)

    def test_native_symbols_abi_without_marker_requires_actual_mini_review(self):
        from agent.neurobro_reply import screening_interpretation
        from tests.test_semantic_screening import bound
        reply = 'Pilihan coin sudah jelas.\n```json\n{"symbols":["ATOMUSDT","FILUSDT"]}\n```'
        with self.assertRaisesRegex(ReplyIncomplete, 'SCREENING_INCOMPLETE'):
            parse_reply_for_payload(reply, screening())
        # The real source-bound Mini request/result still authorizes semantic
        # reading of historical JSON; no receipt is invented by the parser.
        self.assertEqual(screening_interpretation(reply,
            *bound(reply, ['ATOMUSDT', 'FILUSDT'])), {'symbols': ['ATOMUSDT', 'FILUSDT']})
        self.assertEqual(parse_reply_for_payload(reply.replace('"symbols"', '"SYMBOLS"'), screening()),
            {'symbols': ['ATOMUSDT', 'FILUSDT']})
        invalid = reply.replace('"symbols"', '"SYMBOLS"').replace(']}', '],"comparison":"BTCUSDT"}')
        with self.assertRaises(ReplyIncomplete):
            screening_interpretation(invalid, *bound(invalid, ['ATOMUSDT', 'FILUSDT']))

    def test_screening_keeps_actual_capacity_exclusion_and_unique_symbol_guards(self):
        cases = (
            ('{"SYMBOLS":["ATOMUSDT","ATOMUSDT"]}', screening()),
            ('{"SYMBOLS":["ATOMUSDT","atomusdt"]}', screening()),
            ('{"SYMBOLS":["LINKUSDT"]}', screening()),
            ('{"SYMBOLS":["ATOMUSDT","FILUSDT"]}', screening(excluded=['FILUSDT'])),
            ('{"SYMBOLS":["LINKUSDT"],"ENTRY":"10.2"}', screening(1)),
            ('{"COIN":"LINKUSDT","ARAH":"LONG","ENTRY":"10.2","TP":"10.6","SL":"10.05"}', screening(1)),
            ('{"SYMBOLS":[123]}', screening(1)),
            ('{"SYMBOLS":["LINKUSDT"],"symbols":["FILUSDT"]}', screening(1)),
            ('{"ignored":{"SYMBOLS":["LINKUSDT"]}', screening(1)),
            ('{"SYMBOLS":["LINKUSDT"]', screening(1)),
        )
        for reply, payload in cases:
            with self.subTest(reply=reply), self.assertRaises(ReplyIncomplete):
                parse_reply_for_payload(reply, payload)

    def test_screening_does_not_silently_replace_a_final_prose_choice(self):
        reply = 'COIN: ATOMUSDT\n\n{"SYMBOLS":["LINKUSDT"]}'
        with self.assertRaisesRegex(ReplyIncomplete, 'DECISION_CONFLICT'):
            parse_reply_for_payload(reply, screening(1))

    def test_screening_final_json_does_not_promote_an_earlier_comparison_table(self):
        reply = ('Tabel pembanding likuiditas:\nCOIN: BTCUSDT, ETHUSDT\n\n'
            'Pilihan final saya tetap berdasarkan struktur terbaru.\n'
            '{"SYMBOLS":["ATOMUSDT","FILUSDT"]}')
        self.assertEqual(parse_reply_for_payload(reply, screening()),
            {'symbols': ['ATOMUSDT', 'FILUSDT']})
        self.assertEqual(parse_reply_for_payload('Catatan [pasar] sudah dipertimbangkan.\n'
            '{"SYMBOLS":["LINKUSDT"]}', screening(1)), {'symbols': ['LINKUSDT']})

    def test_phase_recap_json_does_not_supply_the_next_phase_decision_or_prices(self):
        setup = '{"COIN":"LINKUSDT","ARAH":"LONG","ENTRY":"10.20","TP":"10.60","SL":"10.05"}'
        # Even unsolicited levels during screening confer only a coin choice;
        # no price field can escape the screening output ABI.
        self.assertEqual(parse_reply_for_payload(setup + '\n{"SYMBOLS":["LINKUSDT"]}', screening(1)),
            {'symbols': ['LINKUSDT']})
        result = parse_analysis_reply('{"SYMBOLS":["LINKUSDT"]}\n' + setup, 'LINKUSDT')
        self.assertEqual((result['side'], result['limit_entry']), ('LONG', '10.20'))
        self.assertEqual(parse_reply_for_payload(setup + '\n{"COIN":"LINKUSDT","KEPUTUSAN":"TAHAN"}', pending()),
            {'symbol': 'LINKUSDT', 'order_ref': 'a' * 64, 'decision': 'TAHAN'})

    def test_analysis_final_long_short_and_hold_after_explanation(self):
        for side, entry, target, stop in (
                ('LONG', '10.20', '10.60', '10.05'),
                ('SHORT', '10.20', '9.80', '10.35'),
                ('HOLD', None, None, None)):
            body = json.dumps({'COIN': 'LINKUSDT', 'ARAH': side,
                'ENTRY': entry, 'TP': target, 'SL': stop})
            for final in (body, '```json\n' + body + '\n```'):
                reply = 'Struktur LINKUSDT dan swing 1H/15M mendasari keputusan saat ini.\n\n' + final
                with self.subTest(side=side, final=final):
                    output = parse_analysis_reply(reply, 'LINKUSDT')
                    self.assertEqual(output['side'], side)
                    self.assertEqual((output['limit_entry'], output['take_profit'], output['stop_loss']),
                        (entry, target, stop))
                    if side == 'HOLD':
                        self.assertTrue(author_market_hold(reply, 'LINKUSDT'))

    def test_final_analysis_json_does_not_hide_conflicting_final_direction_or_levels(self):
        body = '{"COIN":"LINKUSDT","ARAH":"LONG","ENTRY":"10.20","TP":"10.60","SL":"10.05"}'
        cases = (
            'COIN: LINKUSDT\nARAH: SHORT\n' + body,
            'Keputusan final:\nCOIN: LINKUSDT\nARAH: HOLD\n' + body,
            'COIN: LINKUSDT\nARAH: LONG\nENTRY: 10.25\n' + body,
            'COIN: LINKUSDT\nARAH: LONG\nSL: 10.10\n' + body,
            'COIN: ATOMUSDT\nARAH: LONG\n' + body,
            'Jika breakout terjadi, gunakan skenario ini:\n' + body,
            'CSV belum terbaca.\nKeputusan final:\n' + body,
        )
        for reply in cases:
            with self.subTest(reply=reply), self.assertRaises(ReplyIncomplete):
                parse_analysis_reply(reply, 'LINKUSDT')

    def test_public_analysis_json_is_not_a_screening_setup_or_invalid_hold(self):
        for reply in (
                '{"COIN":"LINKUSDT","ARAH":"HOLD","ENTRY":"10.2","TP":null,"SL":null}',
                '{"COIN":"LINKUSDT","ARAH":"LONG","ENTRY":"10.2","TP":"10.6","SL":null}',
                '{"COIN":"LINKUSDT","ARAH":"LONG","ENTRY":"10.2","TP":"10.6","SL":true}',
                '{"COIN":"LINKUSDT","ARAH":"LONG","ENTRY":10.2,"TP":NaN,"SL":10.05}',
                '{"COIN":"LINKUSDT","ARAH":"LONG","ENTRY":"10.2","TP":"10.6","SL":"10.05"',
                '{"SYMBOLS":["LINKUSDT"]}'):
            with self.subTest(reply=reply), self.assertRaises(ReplyIncomplete):
                parse_analysis_reply(reply, 'LINKUSDT')

    def test_same_authored_prices_in_numeric_and_string_json_are_not_conflicts(self):
        first = '{"COIN":"LINKUSDT","ARAH":"LONG","ENTRY":10.2,"TP":10.6,"SL":10.05}'
        final = '{"coin":"LINKUSDT","arah":"long","entry":"10.20","tp":"10.60","sl":"10.05"}'
        result = parse_analysis_reply(first + '\n\n' + final, 'LINKUSDT')
        self.assertEqual((result['side'], result['limit_entry'], result['take_profit'], result['stop_loss']),
            ('LONG', '10.20', '10.60', '10.05'))

    def test_separate_future_caveat_does_not_reopen_a_current_final_json_hold(self):
        reply = ('COIN: LINKUSDT\nARAH: HOLD\n'
            'Jika struktur berubah nanti, evaluasi lagi SHORT pada setup baru.\n\n'
            '{"COIN":"LINKUSDT","ARAH":"HOLD","ENTRY":null,"TP":null,"SL":null}')
        self.assertTrue(author_market_hold(reply, 'LINKUSDT'))
        conditional = reply.replace('ARAH: HOLD', 'ARAH: HOLD jika reclaim gagal')
        self.assertFalse(author_market_hold(conditional, 'LINKUSDT'))

    def test_contrary_present_prose_does_not_borrow_json_hold_shortcut(self):
        body = '{"COIN":"LINKUSDT","ARAH":"HOLD","ENTRY":null,"TP":null,"SL":null}'
        reply = ('Keputusan akhir saat ini adalah LONG untuk LINKUSDT; '
            'entry 10.20, target 10.60 dan stop 10.05.\n' + body)
        self.assertEqual(parse_analysis_reply(reply, 'LINKUSDT')['side'], 'HOLD')
        self.assertFalse(author_market_hold(reply, 'LINKUSDT'))
        # An explicit final boundary still supersedes an older scenario.
        final = reply[:-len(body)] + 'Ringkasan akhir:\n' + body
        self.assertTrue(author_market_hold(final, 'LINKUSDT'))

    def test_pending_json_maps_only_to_the_verified_original_order(self):
        for decision in ('TAHAN', 'BATALKAN'):
            body = json.dumps({'COIN': 'LINKUSDT', 'KEPUTUSAN': decision})
            for final in (body, '```json\n' + body + '\n```'):
                reply = 'Struktur LINKUSDT terbaru mendukung keputusan berikut.\n\n' + final
                with self.subTest(decision=decision, final=final):
                    output = parse_reply_for_payload(reply, pending())
                    self.assertEqual(output, {'symbol': 'LINKUSDT', 'order_ref': 'a' * 64,
                        'decision': decision})
            for change in ('operation', 'intent_ref', 'symbol'):
                payload = pending()
                if change == 'operation':
                    payload['operation'] = 'other-order'
                elif change == 'intent_ref':
                    payload['context']['pending_review']['intent_ref'] = 'b' * 64
                else:
                    payload['context']['symbol'] = 'ATOMUSDT'
                with self.subTest(change=change), self.assertRaisesRegex(ReplyIncomplete, 'ORDER_UNVERIFIED'):
                    parse_reply_for_payload(body, payload)

    def test_pending_json_rejects_wrong_coin_conditional_and_conflicting_decision(self):
        body = '{"COIN":"LINKUSDT","KEPUTUSAN":"TAHAN"}'
        for reply in (body.replace('LINKUSDT', 'ATOMUSDT'),
                'COIN: LINKUSDT\nKEPUTUSAN: BATALKAN\n' + body,
                'Jika breakout terjadi:\n' + body,
                'Chart belum terbaca.\n' + body,
                body.replace('"TAHAN"', '"HOLD"'),
                body.replace('}', ',"ORDER_REF":"fake"}')):
            with self.subTest(reply=reply), self.assertRaises(ReplyIncomplete):
                parse_pending_reply(reply, 'LINKUSDT')

    def test_pending_json_keeps_a_separate_future_reassessment_caveat(self):
        reply = ('COIN: LINKUSDT\nKEPUTUSAN: TAHAN\n'
            'Jika struktur berubah nanti, tinjau kembali pada evaluasi berikutnya.\n'
            '{"COIN":"LINKUSDT","KEPUTUSAN":"TAHAN"}')
        self.assertEqual(parse_pending_reply(reply, 'LINKUSDT'), 'TAHAN')

    def test_clear_natural_decisions_remain_compatible_without_json(self):
        self.assertEqual(parse_screening_reply('COIN: ATOMUSDT, FILUSDT'),
            {'symbols': ['ATOMUSDT', 'FILUSDT']})
        self.assertEqual(parse_analysis_reply('COIN: LINKUSDT\nARAH: HOLD\nStruktur belum layak.',
            'LINKUSDT')['side'], 'HOLD')
        self.assertEqual(parse_pending_reply('COIN: LINKUSDT\nKEPUTUSAN: BATALKAN',
            'LINKUSDT'), 'BATALKAN')

    def test_native_screening_review_reads_final_json_without_a_reformat_send(self):
        from tests.test_semantic_screening import SemanticScreeningHookTests
        fixture = SemanticScreeningHookTests()
        self.addCleanup(fixture.doCleanups)
        reply = ('BTCUSDT hanya pembanding; momentum dan struktur mendukung dua pilihan ini.\n\n'
            '```json\n{"SYMBOLS":["FILUSDT","ATOMUSDT"]}\n```')
        case, payload = fixture.setup_hook(reply)
        result = case.hook.ask(payload, lambda: True, deadline=80)
        self.assertEqual(result['output'], {'symbols': ['FILUSDT', 'ATOMUSDT']})
        self.assertEqual(result['assistant_text'], reply)
        self.assertEqual(case.bridge.clicks, 1)
        case.reasoner.request.assert_called_once()
        self.assertEqual(case.reasoner.request.call_args.args[0]['reply'], reply)

    def test_native_final_hold_json_after_explanation_does_not_open_another_dialog(self):
        from tests.test_native_browser_hook import NativeFreshFollowupTests
        case = NativeFreshFollowupTests()
        self.addCleanup(case.doCleanups)
        case.harness()
        reply = ('Struktur BTCUSDT belum menyediakan entry yang layak.\n\n'
            '```json\n{"COIN":"BTCUSDT","ARAH":"HOLD","ENTRY":null,"TP":null,"SL":null}\n```')
        case.bridge.initial = reply
        result = case.ask()
        self.assertEqual(result['assistant_text'], reply)
        self.assertEqual(result['output']['side'], 'HOLD')
        self.assertEqual(case.bridge.clicks, 1)
        case.reasoner.request.assert_not_called()
        case.refresh.assert_not_called()

    def test_explained_json_hold_remains_terminal_after_closed_candle_advances(self):
        from tests.test_native_browser_hook import NativeFreshFollowupTests
        for suffix in ('', ' Jika struktur berubah nanti, evaluasi ulang SHORT.'):
            case = NativeFreshFollowupTests()
            self.addCleanup(case.doCleanups)
            case.harness()
            reply = ('Struktur BTCUSDT belum menyediakan entry yang layak.' + suffix + '\n'
                '{"COIN":"BTCUSDT","ARAH":"HOLD","ENTRY":null,"TP":null,"SL":null}')
            case.bridge.initial = reply
            after_click = case.bridge.after_click
            def cross_boundary(bridge):
                after_click(bridge)
                case.now = (case.now // 900 + 1) * 900 + 1
            case.bridge.after_click = cross_boundary
            result = case.ask()
            self.assertEqual(result['assistant_text'], reply)
            self.assertEqual(result['output']['side'], 'HOLD')
            self.assertEqual(case.bridge.clicks, 1)
            case.reasoner.request.assert_not_called()
            case.refresh.assert_not_called()

    def test_present_prose_json_conflict_reaches_existing_semantic_review_once(self):
        from tests.test_native_browser_hook import NativeFreshFollowupTests
        hold = '{"COIN":"BTCUSDT","ARAH":"HOLD","ENTRY":null,"TP":null,"SL":null}'
        long = '{"COIN":"BTCUSDT","ARAH":"LONG","ENTRY":"65000","TP":"65200","SL":"64900"}'
        for reply in ('Keputusan saat ini adalah LONG untuk BTCUSDT.\n' + hold,
                'Keputusan saat ini adalah HOLD untuk BTCUSDT.\n' + long):
            case = NativeFreshFollowupTests()
            self.addCleanup(case.doCleanups)
            case.harness()
            case.bridge.initial, case.bridge.final = reply, hold
            review = case.reasoner.request.side_effect
            def clarify(request):
                result = review(request)
                result['decision']['question'] = ('Penjelasan dan JSON berbeda. '
                    'Apa keputusan akhir untuk BTCUSDT?')
                return result
            case.reasoner.request.side_effect = clarify
            result = case.ask()
            self.assertEqual(result['assistant_text'], hold)
            self.assertEqual(result['output']['side'], 'HOLD')
            case.reasoner.request.assert_called_once()
            self.assertEqual(case.reasoner.request.call_args.args[0]['reply'], reply)
            self.assertEqual(case.bridge.clicks, 2)
            case.refresh.assert_called_once()

