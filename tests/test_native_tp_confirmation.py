"""Owner-authorized Risk Manager TP normalization must not create chat rounds."""
import copy
import unittest
from unittest.mock import patch

from agent import native_browser_hook as helper


class RiskManagerTPOwnershipTests(unittest.TestCase):
    def test_neurobro_tp_is_a_recommendation_not_an_approval_of_execution_math(self):
        output = {'symbol': 'BTCUSDT', 'side': 'LONG', 'limit_entry': '100',
            'take_profit': '100.5', 'stop_loss': '99', 'risk_reward': 0.5}
        before = copy.deepcopy(output)
        self.assertIsNone(helper._neurobro_tp_confirmation(output, None, now=1))
        self.assertEqual(output, before)
        self.assertFalse(hasattr(helper, '_NATIVE_TP_SOURCE'))

    def test_hold_never_becomes_directional_to_satisfy_tp_normalization(self):
        output = {'symbol': 'BTCUSDT', 'side': 'HOLD', 'limit_entry': None,
            'take_profit': None, 'stop_loss': None, 'risk_reward': None}
        before = copy.deepcopy(output)
        self.assertIsNone(helper._neurobro_tp_confirmation(output, {}, now=1))
        self.assertEqual(output, before)

    def test_complete_native_reply_advances_with_original_tp_and_no_extra_tp_turn(self):
        from tests.test_native_browser_hook import NativeFreshFollowupTests, client
        fixture = NativeFreshFollowupTests(
            'test_fresh_400_data_and_two_attachments_are_sent_once_with_native_final_identity')
        fixture.harness()
        fixture.bridge.initial = fixture.bridge.final
        original = {'symbol': 'BTCUSDT', 'side': 'LONG', 'entry_price': '100',
            'limit_entry': '100', 'take_profit': '100.5', 'stop_loss': '99', 'risk_reward': 0.5}
        fixture.module.exact_json.side_effect = lambda text, marker: copy.deepcopy(original)
        fixture.hook.client = client('COMPLETE')
        before = copy.deepcopy(fixture.value)
        with patch.object(helper, '_neurobro_tp_confirmation', side_effect=AssertionError('NO_TP_APPROVAL_TURN')) as approval:
            result = fixture.ask()
        approval.assert_not_called()
        self.assertEqual(result['output']['take_profit'], '100.5')
        self.assertEqual(result['output']['limit_entry'], '100')
        self.assertEqual(result['output']['stop_loss'], '99')
        self.assertEqual(fixture.bridge.clicks, 1)
        fixture.refresh.assert_not_called()
        self.assertEqual(fixture.value, before)


if __name__ == '__main__': unittest.main()
