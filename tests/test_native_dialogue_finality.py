"""Offline native dialog finality; no model, provider, or exchange requests."""
from unittest import TestCase

from tests import test_native_browser_hook as native


class NativeDialogueFinalityTests(TestCase):
    def harness(self):
        case = native.NativeFreshFollowupTests()
        self.addCleanup(case.doCleanups)
        case.harness()
        return case

    def test_final_hold_with_future_condition_is_not_debated(self):
        case = self.harness()
        answer = ('COIN: BTCUSDT\nARAH: HOLD\nStruktur belum mendukung entry. '
                  'Jika likuiditas tersapu nanti, evaluasi pada setup baru.')
        case.bridge.initial = answer
        result = case.ask()
        self.assertEqual(result['assistant_text'], answer)
        self.assertEqual(result['output']['side'], 'HOLD')
        self.assertEqual(case.bridge.clicks, 1)
        case.refresh.assert_not_called()
        case.reasoner.request.assert_not_called()

    def test_author_final_hold_is_not_changed_by_mini_complete(self):
        case = self.harness()
        answer = 'COIN: BTCUSDT\nARAH: HOLD\nMomentum melemah, tidak ada entry yang layak.'
        case.bridge.initial = answer
        case.reasoner = native.client('COMPLETE')
        case.hook.client = case.reasoner
        result = case.ask()
        self.assertEqual(result['assistant_text'], answer)
        self.assertEqual(result['output']['side'], 'HOLD')
        self.assertEqual(case.bridge.clicks, 1)
        case.refresh.assert_not_called()

    def test_final_hold_at_candle_boundary_does_not_request_a_new_setup(self):
        from tests.test_agent_closed_candles_200 import native_context_200
        case = self.harness()
        boundary = (case.now // 900 + 1) * 900
        case.now = boundary - .1
        case.value['context'] = native_context_200(case.now)
        original = case.bridge.after_click
        def complete_after_boundary(bridge):
            original(bridge)
            case.now = boundary + .1
        case.bridge.after_click = complete_after_boundary
        answer = 'COIN: BTCUSDT\nARAH: HOLD\nStruktur saat ini belum layak.'
        case.bridge.initial = answer
        result = case.ask()
        self.assertEqual(result['assistant_text'], answer)
        self.assertEqual(result['output']['side'], 'HOLD')
        self.assertEqual(case.bridge.clicks, 1)
        case.refresh.assert_not_called()

    def test_unresolved_wick_clarification_never_accepts_parsed_setup(self):
        case = self.harness()
        answer = ('COIN: BTCUSDT\nARAH: SHORT\nENTRY: 65000\nTP: 64000\nSL: 65010')
        case.bridge.initial = case.bridge.final = answer
        original = case.reasoner.request.side_effect
        def review(request):
            response = original(request)
            response['decision']['question'] = ('SL 65010 berada di bawah swing 65100 pada chart. '
                'Apa dasar invalidasinya dan bagaimana menghindari sapuan wick?')
            return response
        case.reasoner.request.side_effect = review
        with self.assertRaises(native.OutcomeUnknown):
            case.ask()
        self.assertEqual(case.bridge.clicks, 2)
        self.assertEqual(case.reasoner.request.call_count, 2)
        # The fresh material provider is consulted before comparing source
        # hashes. Its unchanged answer is rejected before a second upload or
        # Send; a genuinely changed chart can support another useful review.
        self.assertEqual(case.refresh.call_count, 2)
        self.assertEqual(len(case.bridge.uploads), 1)
        self.assertEqual(case.bridge.messages[-1]['text'], answer)

    def test_unresolved_parsed_setup_is_not_reviewed_again_after_restart(self):
        from tests.test_native_dialogue_no_progress import NativeDialogueNoProgressTests
        fixture = NativeDialogueNoProgressTests()
        self.addCleanup(fixture.doCleanups)
        case, root = fixture.harness()
        answer = ('COIN: BTCUSDT\nARAH: SHORT\nENTRY: 65000\nTP: 64000\nSL: 65010')
        case.bridge.initial = case.bridge.final = answer
        original = case.reasoner.request.side_effect
        def review(request):
            response = original(request)
            response['decision']['question'] = 'Apa dasar invalidasi SL 65010 terhadap swing dan wick?'
            return response
        case.reasoner.request.side_effect = review
        with self.assertRaises(native.OutcomeUnknown):
            case.ask()
        calls = case.reasoner.request.call_count
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'NEUROBRO_CLARIFICATION_NO_PROGRESS'):
                fixture.resume(case)
            self.assertEqual(case.reasoner.request.call_count, calls)

