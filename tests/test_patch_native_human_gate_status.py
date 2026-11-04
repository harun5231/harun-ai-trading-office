import ast
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch as mocked

from scripts import patch_native_human_gate_status as p


FIXTURE=json.loads((Path(__file__).parent/'fixtures/native_human_gate_status_abi.json').read_text())


def native_source():
    # These are the EXACT methods captured from image072/c842 in root41821380923,
    # rather than a recreated version of the native status decision.
    text="MESSAGE = '[data-message-id]'\nRAIL_INPUT_GROUP = 'RAIL'\n"
    text+='def detect(page, visible, observation):\n    return (\'CURRENT\',\'CONNECTED\')\n'
    text+='class DOMBridge:\n'
    text+='    '+FIXTURE['_visible']['source']+'\n'
    text+='    '+FIXTURE['status']['source']+'\n'
    return text.encode()


def patched_bridge():
    raw=native_source()
    after=p.patch(raw,expected_sha256=hashlib.sha256(raw).hexdigest())
    namespace={}
    exec(compile(after,'actual-native-status-fixture','exec'),namespace)
    bridge=namespace['DOMBridge']()
    bridge.page=object()
    bridge._origin_valid=lambda:True
    bridge._operation_contract=None
    bridge._dom_contract=None
    bridge._current_secure_loader_blocking=lambda:False
    bridge._visible=lambda selector:[]
    return bridge,namespace


class NativeStatusPatchTests(unittest.TestCase):
    def test_exact_native_methods_and_source_pin_are_fresh_evidence(self):
        self.assertEqual(FIXTURE['actual_module_sha256'],p.DOM_BEFORE_SHA256)
        self.assertEqual(FIXTURE['source_job'],'41821380923-1')
        self.assertEqual(hashlib.sha256(FIXTURE['status']['source'].encode()).hexdigest(),p.STATUS_BEFORE_SHA256)
        self.assertEqual(hashlib.sha256(FIXTURE['_visible']['source'].encode()).hexdigest(),p.VISIBLE_BEFORE_SHA256)
        self.assertEqual(FIXTURE['selectors']['MESSAGE'],'[data-message-id]')

    def test_patch_adds_only_the_guard_and_preserves_original_native_source(self):
        raw=native_source()
        after=p.patch(raw,expected_sha256=hashlib.sha256(raw).hexdigest())
        inserted=("        from agent.native_human_gate_status import visible_human_gate\n"
            "        if visible_human_gate(self.page, message_selector=MESSAGE):\n"
            "            observation['reason'] = 'CHALLENGE_VISIBLE'\n"
            "            return 'CHALLENGE_REQUIRED'\n").encode()
        self.assertEqual(after.count(inserted),1)
        self.assertEqual(after.replace(inserted,b''),raw)
        ast.parse(after)

    def test_changed_parent_native_methods_and_message_contract_are_refused(self):
        raw=native_source()
        with self.assertRaisesRegex(ValueError,'HUMAN_GATE_PARENT_SOURCE_UNVERIFIED'):
            p.patch(raw)
        for before,replacement,reason in (
            (b"reason='OBSERVATION_EXCEPTION'",b"reason='OTHER'",'HUMAN_GATE_NATIVE_CONTRACT_UNVERIFIED'),
            (b'return [locator.nth(index)',b'return [locator.first', 'HUMAN_GATE_NATIVE_CONTRACT_UNVERIFIED'),
            (b"MESSAGE = '[data-message-id]'",b"MESSAGE = '[data-other]'",'HUMAN_GATE_MESSAGE_CONTRACT_UNVERIFIED')):
            changed=raw.replace(before,replacement)
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError,reason):
                    p.patch(changed,expected_sha256=hashlib.sha256(changed).hexdigest())

    def test_double_patch_is_refused(self):
        raw=native_source();after=p.patch(raw,expected_sha256=hashlib.sha256(raw).hexdigest())
        with self.assertRaisesRegex(ValueError,'HUMAN_GATE_NATIVE_CONTRACT_UNVERIFIED'):
            p.patch(after,expected_sha256=hashlib.sha256(after).hexdigest())

    def test_current_gate_cannot_be_cached_as_connected_when_composer_survives(self):
        bridge,_=patched_bridge()
        with mocked('agent.native_human_gate_status.visible_human_gate',return_value=True)as observed:
            self.assertEqual(bridge.status(),'CHALLENGE_REQUIRED')
        self.assertEqual(bridge._session_observation,{'reason':'CHALLENGE_VISIBLE','counts':{}})
        self.assertIsNone(bridge._dom_contract)
        observed.assert_called_once_with(bridge.page,message_selector='[data-message-id]')

    def test_origin_and_original_iframe_checks_keep_their_original_priority(self):
        bridge,_=patched_bridge()
        bridge._origin_valid=lambda:False
        with mocked('agent.native_human_gate_status.visible_human_gate',side_effect=AssertionError('must not run')):
            self.assertEqual(bridge.status(),'UNAVAILABLE')
            self.assertEqual(bridge._session_observation['reason'],'ORIGIN_UNAPPROVED')
        bridge._origin_valid=lambda:True
        bridge._visible=lambda selector:[object()]if selector==p.KNOWN_CHALLENGE_SELECTOR else[]
        with mocked('agent.native_human_gate_status.visible_human_gate',side_effect=AssertionError('must not run')):
            self.assertEqual(bridge.status(),'CHALLENGE_REQUIRED')
            self.assertEqual(bridge._session_observation['reason'],'CHALLENGE_VISIBLE')

    def test_false_guard_preserves_login_and_ordinary_blocking_dialog_behavior(self):
        bridge,_=patched_bridge()
        with mocked('agent.native_human_gate_status.visible_human_gate',return_value=False):
            bridge._visible=lambda selector:[object()]if selector.startswith('input[')else[]
            self.assertEqual(bridge.status(),'LOGIN_REQUIRED')
            self.assertEqual(bridge._session_observation['reason'],'LOGIN_FIELDS_VISIBLE')
            bridge._visible=lambda selector:[object()]if selector.startswith('[role=')else[]
            self.assertEqual(bridge.status(),'UNAVAILABLE')
            self.assertEqual(bridge._session_observation['reason'],'VISIBLE_BLOCKING_DIALOG')

    def test_false_guard_preserves_connected_and_operation_contract_gates(self):
        bridge,_=patched_bridge()
        with mocked('agent.native_human_gate_status.visible_human_gate',return_value=False):
            self.assertEqual(bridge.status(),'CONNECTED')
            self.assertEqual(bridge._dom_contract,'CURRENT')
            bridge._operation_contract='OTHER'
            self.assertEqual(bridge.status(),'UNAVAILABLE')
            self.assertEqual(bridge._session_observation['reason'],'OPERATION_LAYOUT_CHANGED')

    def test_failed_observation_does_not_invent_login_or_connected(self):
        bridge,_=patched_bridge()
        with mocked('agent.native_human_gate_status.visible_human_gate',side_effect=ValueError('HUMAN_GATE_UI_OBSERVATION_UNVERIFIED')):
            with self.assertRaisesRegex(ValueError,'HUMAN_GATE_UI_OBSERVATION_UNVERIFIED'):
                bridge.status()
        self.assertIsNone(bridge._dom_contract)
        self.assertEqual(bridge._session_observation['reason'],'OBSERVATION_EXCEPTION')


if __name__=='__main__':unittest.main()
