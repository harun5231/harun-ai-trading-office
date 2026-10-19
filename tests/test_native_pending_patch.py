"""Offline source-pinned pending patch preparation; never import native modules."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import unittest

from scripts.patch_native_pending_review import (BROWSER_HOOK_PREFIX,
    PendingPatchRefused, SOURCE_PINS, edits_for, patch_native_pending_sources)


def fixtures():
    return {
        'worker/robot.py': b'''# preserve coordinator and gateway authority
class Coordinator:
    def _tick(self):
        if not self.store.settings()['robot_on']:
            return 'OFF'
        advanced = self.advance_execution(account, today)
        return advanced
''',
        'worker/neurobrowser.py': b'''# keep request and journal implementation
class NeuroBrowser:
    def ask(self, operation, prompt, schema, validate, context=None):
        if not permitted():
            raise ValueError('OFF')
        if schema != SETUP_SCHEMA and screening_count(schema) is None:
            raise ValueError('unknown schema')
        return source_evidence(self.db, operation)
''',
        'worker/research_provider.py': b'''# unchanged provider
def request_body(*args): pass
def validate_chart_evidence(*args): pass
def source_evidence(*args): pass
''',
        'browser_runtime/actor.py': b'''# unchanged native profile and screenshot implementation
class BrowserActor:
    def _research(self, playwright, bridge, value):
        if not self.dispatch_check(value):
            raise ValueError('OFF')
        required = value.get('output_schema', {}).get('required')
        if isinstance(required, list) and set(required) == {'symbol', 'side', 'entry', 'tp', 'sl', 'reason'}:
            from .charts import capture, close_resource
            images = capture(value)
        return bridge.ask(value, lambda: self.dispatch_check(value))
    def _open_automation(self):
        from .dom import DOMBridge
        self._bridge = DOMBridge(page)  # retain original comment
''',
        'browser_runtime/dom.py': b'''# preserve native DOM selectors, profile and receipt code
def request_kind(*args): pass
def user_prompt(*args): pass
def exact_json(*args): pass
class DOMBridge:
    def ask(self, *args): return 'native'
''',
        'agent/native_browser_hook.py': b'''# preserve the published native hook
class DelegatedDOMBridge:
    def ask(self, payload, *args):
        def review(answer):
            required = payload['output_schema']['required']
            phase = 'SCREENING' if set(required) == {'symbols'} else 'ANALYSIS'
            return phase
        return review(answer)
''',
    }


def pins(sources):
    return {path: hashlib.sha256(raw).hexdigest() for path, raw in sources.items()}


class NativePendingPatchTests(unittest.TestCase):
    def setUp(self):
        self.sources = fixtures()

    def patch(self, sources=None):
        value = self.sources if sources is None else sources
        return patch_native_pending_sources(value, _expected_sha256=pins(value))

    def test_fixed_pin_set_contains_only_six_authenticated_files(self):
        self.assertEqual(set(SOURCE_PINS), set(self.sources))
        for value in SOURCE_PINS.values():
            self.assertRegex(value, r'^[a-f0-9]{64}$')
        self.assertEqual(SOURCE_PINS['agent/native_browser_hook.py'],
            '6d398a62cc87b182e84c532511178d42592b89c608c6a525b763dac6dd2878dd')

    def test_hash_and_complete_input_set_are_required(self):
        with self.assertRaises(PendingPatchRefused):
            patch_native_pending_sources(self.sources)
        for changed in ({**self.sources, 'worker/order_gateway.py': b'pass\n'},
                        {key:value for key,value in self.sources.items() if key != 'worker/robot.py'}):
            with self.assertRaises(PendingPatchRefused):
                self.patch(changed)

    def test_every_original_byte_outside_reviewed_spans_is_preserved(self):
        before = copy.deepcopy(self.sources)
        result, receipt = self.patch()
        self.assertEqual(before, self.sources)
        self.assertFalse(receipt['production_deployment_performed'])
        self.assertEqual(set(result), set(self.sources))
        expected_counts = {'worker/robot.py':1, 'worker/neurobrowser.py':2,
            'worker/research_provider.py':1, 'browser_runtime/actor.py':2,
            'browser_runtime/dom.py':1, 'agent/native_browser_hook.py':1}
        for path, raw in self.sources.items():
            edits = sorted(edits_for(path, raw, ast.parse(raw)))
            cursor_original = cursor_patched = 0
            for start, end, replacement in edits:
                unchanged = raw[cursor_original:start]
                self.assertEqual(result[path][cursor_patched:cursor_patched+len(unchanged)], unchanged)
                cursor_patched += len(unchanged)
                self.assertEqual(result[path][cursor_patched:cursor_patched+len(replacement)], replacement)
                cursor_patched += len(replacement)
                cursor_original = end
            self.assertEqual(result[path][cursor_patched:], raw[cursor_original:])
            ast.parse(result[path])
            self.assertEqual(receipt['sources'][path]['reviewed_span_count'], expected_counts[path])
            self.assertTrue(receipt['sources'][path]['all_other_source_bytes_preserved'])
            self.assertEqual(receipt['sources'][path]['source_sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(receipt['sources'][path]['patched_sha256'], hashlib.sha256(result[path]).hexdigest())

    def test_only_native_tick_call_is_replaced_and_off_gate_remains_first(self):
        result, _ = self.patch()
        patched = result['worker/robot.py']
        self.assertIn(b"if not self.store.settings()['robot_on']", patched)
        self.assertLess(patched.index(b"if not self.store.settings()"), patched.index(b'review_then_advance'))
        self.assertIn(b'review_then_advance(self, account, today)', patched)
        self.assertNotIn(b'self.advance_execution(account, today)', patched)

    def test_native_browser_gate_widens_only_exact_schema_admission(self):
        result, _ = self.patch()
        value = result['worker/neurobrowser.py']
        self.assertIn(b'(schema != SETUP_SCHEMA and screening_count(schema) is None) and not ', value)
        self.assertIn(b'is_pending_schema(schema)', value)
        self.assertIn(b'install_worker_browser_protocol(globals())', value)
        self.assertIn(b'return source_evidence(self.db, operation)', value)

    def test_actor_has_one_pending_capture_extension_and_existing_hook(self):
        result, _ = self.patch()
        value = result['browser_runtime/actor.py']
        self.assertEqual(value.count(BROWSER_HOOK_PREFIX), 1)
        self.assertEqual(value.count(b'is_pending_browser_request(value)'), 1)
        self.assertLess(value.index(b'not self.dispatch_check(value)'), value.index(b'is_pending_browser_request'))
        self.assertIn(b'# retain original comment', value)

    def test_protocol_installers_and_pending_dialogue_phase_are_additive(self):
        result, _ = self.patch()
        self.assertTrue(result['worker/research_provider.py'].startswith(self.sources['worker/research_provider.py']))
        self.assertTrue(result['browser_runtime/dom.py'].startswith(self.sources['browser_runtime/dom.py']))
        self.assertIn(b'install_source_protocol(globals())', result['worker/research_provider.py'])
        self.assertIn(b'install_dom_protocol(globals())', result['browser_runtime/dom.py'])
        self.assertIn(b'dialogue_phase_for_payload(payload, required)', result['agent/native_browser_hook.py'])

    def test_double_patch_is_refused_even_if_override_hashes_match(self):
        result, _ = self.patch()
        with self.assertRaises(PendingPatchRefused):
            self.patch(result)

    def test_missing_duplicate_or_changed_tick_call_is_refused(self):
        original = self.sources['worker/robot.py']
        for raw in (original.replace(b'advance_execution(account, today)', b'advance_execution(account, tomorrow)'),
                    original.replace(b'        return advanced', b'        self.advance_execution(account, today)\n        return advanced')):
            changed = {**self.sources, 'worker/robot.py': raw}
            with self.subTest(raw=raw), self.assertRaises(PendingPatchRefused):
                self.patch(changed)

    def test_missing_native_dispatch_gate_or_nonunique_chart_condition_refuses(self):
        original = self.sources['browser_runtime/actor.py']
        for raw in (original.replace(b'not self.dispatch_check(value)', b'not self.other_gate(value)'),
                    original.replace(b"'reason'}:", b"'reason', 'other'}:"),
                    original.replace(b'self._bridge = DOMBridge(page)', b'self._bridge = OtherBridge(page)')):
            with self.subTest(raw=raw), self.assertRaises(PendingPatchRefused):
                self.patch({**self.sources, 'browser_runtime/actor.py':raw})

    def test_invalid_ast_size_or_pin_override_refuses_without_output(self):
        for raw in (b'not valid Python :', b'', b'x' * 262145):
            with self.subTest(raw_length=len(raw)), self.assertRaises(PendingPatchRefused):
                self.patch({**self.sources, 'worker/robot.py':raw})
        with self.assertRaises(PendingPatchRefused):
            patch_native_pending_sources(self.sources, _expected_sha256={})

    def test_actual_sanitized_private_sources_have_exact_compatible_spans(self):
        directory = Path('/tmp/harun-reconciled-production-source')
        if not directory.is_dir():
            self.skipTest('Sanitized private source only exists in reconciliation workspace')
        published_hook = Path(__file__).resolve().parents[1]/'agent/native_browser_hook.py'
        original_hook = published_hook.read_bytes()
        sources = {path:(published_hook if path.startswith('agent/') else directory/path).read_bytes()
                   for path in SOURCE_PINS}
        patched, receipt = self.patch(sources)
        self.assertEqual(published_hook.read_bytes(), original_hook)
        self.assertEqual(set(patched), set(SOURCE_PINS))
        self.assertEqual(receipt['sources']['browser_runtime/actor.py']['reviewed_span_count'], 2)
        for value in patched.values():
            ast.parse(value)

    def test_actual_original_production_pins_match_authenticated_inventory(self):
        inventory = [Path('/tmp/harun-native-source-adapter-worker-layout.json'),
                     Path('/tmp/harun-native-source-adapter-browser-layout.json')]
        if not all(path.is_file() for path in inventory):
            self.skipTest('Authenticated private inventories are not available in ordinary CI')
        for file in inventory:
            document = json.loads(file.read_bytes())
            for item in document['files']:
                prefix = 'worker' if document['role'] == 'worker' else 'browser_runtime'
                name = prefix+'/'+item['name']
                if name in SOURCE_PINS:
                    self.assertEqual(SOURCE_PINS[name], item['sha256'])


if __name__ == '__main__':
    unittest.main()
