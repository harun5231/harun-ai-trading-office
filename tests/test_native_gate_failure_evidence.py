"""Record the original rejecting predicate without another authority check."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent.native_browser_hook import DelegatedDOMBridge
from agent.native_turn_batch import retain_return_failure
from tests.test_native_browser_hook import Bridge, client, native, request, NotSubmitted, OutcomeUnknown


class NativeGateFailureEvidenceTests(unittest.TestCase):
    def run_failure(self, *, status='CONNECTED', permission=True, expired=False, changed_url=False):
        bridge, module, reasoner = Bridge(), native(), client('SCREENING')
        bridge.status = Mock(return_value=status)
        gate = Mock(return_value=permission)
        clock = Mock(side_effect=[0.0, 81.0, 82.0]) if expired else bridge.clock
        hook = DelegatedDOMBridge(bridge, reasoner, native=module,
            converter=Mock(), monotonic=clock)
        if changed_url:
            original = reasoner.request.side_effect
            def response(value):
                result = original(value)
                bridge.page.url += '/changed'
                return result
            reasoner.request.side_effect = response
        with tempfile.TemporaryDirectory() as root:
            def retain(_root, *args): retain_return_failure(root, *args)
            with patch('agent.native_turn_batch.retain_return_failure', side_effect=retain):
                with self.assertRaises((NotSubmitted, OutcomeUnknown)):
                    hook.ask(request(), gate, deadline=80)
            record = json.loads((Path(root) / ('native-return-failure-' + 'a' * 64 + '.json')).read_text())
        return record, bridge, gate, reasoner, clock

    def test_expired_original_clock_short_circuits_status_and_dispatch(self):
        record, bridge, gate, reasoner, clock = self.run_failure(expired=True)
        self.assertEqual(record['gate_failure']['reason'], 'DEADLINE')
        self.assertEqual(record['gate_failure']['clock'], 81.0)
        self.assertEqual(record['clock'], 82.0)
        self.assertIsNone(record['gate_failure']['site_status'])
        self.assertIsNone(record['gate_failure']['dispatch'])
        gate.assert_not_called(); reasoner.request.assert_not_called()
        # The one status observation is the existing pre-Send exception mapper,
        # not an extra probe of the expired gate.
        bridge.status.assert_called_once()
        self.assertEqual(clock.call_count, 3)

    def test_site_status_failure_never_calls_dispatch_or_sends(self):
        record, bridge, gate, reasoner, clock = self.run_failure(status='CHALLENGE_REQUIRED')
        self.assertEqual(record['gate_failure']['reason'], 'SITE_STATUS')
        self.assertEqual(record['gate_failure']['site_status'], 'CHALLENGE_REQUIRED')
        self.assertIsNone(record['gate_failure']['dispatch'])
        gate.assert_not_called(); reasoner.request.assert_not_called()
        self.assertEqual(bridge.clicks, 0)

    def test_actual_false_dispatch_is_retained_without_rechecking_it(self):
        record, bridge, gate, reasoner, clock = self.run_failure(permission=False)
        self.assertEqual(record['gate_failure']['reason'], 'DISPATCH_NOT_PERMITTED')
        self.assertIs(record['gate_failure']['dispatch'], False)
        self.assertEqual(record['gate_failure']['site_status'], 'CONNECTED')
        gate.assert_called_once(); reasoner.request.assert_not_called()
        self.assertEqual(bridge.clicks, 0)

    def test_changed_conversation_retains_url_hashes_without_raw_urls(self):
        record, bridge, gate, reasoner, clock = self.run_failure(changed_url=True)
        evidence = record['gate_failure']
        self.assertEqual(evidence['reason'], 'CONVERSATION_URL_CHANGED')
        original = 'https://fixture.invalid/chat/owned'
        self.assertEqual(evidence['initial_url_sha256'], hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual(evidence['current_url_sha256'], hashlib.sha256((original + '/changed').encode()).hexdigest())
        self.assertNotIn('https://', json.dumps(record))
        self.assertIs(evidence['dispatch'], True)
        self.assertEqual(bridge.clicks, 1)
        reasoner.request.assert_called_once()


if __name__ == '__main__':
    unittest.main()
