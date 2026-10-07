"""Offline private SDK source upgrade preserves all unrelated bytes."""
import ast
import hashlib
import stat
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from deploy import patch_risk_v3_gateway as patcher
from worker.core import (D, FEE_SOURCE, NORMALIZED_REWARD_RISK_POLICY, RISK_MODEL,
                         Rules, Signal, risk_check)
from worker.order_gateway import build_intent


ROOT = Path(__file__).resolve().parents[1]


def fixture(newline=b'\n'):
    cls = (ROOT / 'deploy/live_gateway_template.py').read_bytes()
    helper = patcher.NEW_METHODS['_validate_v3_costs'].encode()
    assert cls.count(helper) == 1
    cls = cls.replace(helper, b'', 1)
    for name, old in patcher.OLD_METHODS.items():
        new = patcher.NEW_METHODS[name].encode()
        assert cls.count(new) == 1
        cls = cls.replace(new, old.encode(), 1)
    prefix = (b"# Preserve private prefix and value.\nimport hashlib, hmac\n"
              b"from datetime import datetime, timezone\nfrom decimal import Decimal\n"
              b"from .core import D, Review, number, validated_risk_target\n"
              b"PRIVATE_KEY = 'offline-fixture-value'\n"
              b"class _MissingOrderImplementation:\n    pass\n\n")
    suffix = b"\n\ndef private_status():\n    return 'private-custom-status'\n"
    return (prefix + patcher.OLD_BUILD.encode() + b'\n' + cls + suffix).replace(b'\n', newline)


class RiskV3GatewaySourcePatchTests(unittest.TestCase):
    def test_only_approved_spans_change_with_private_prefix_suffix_preserved(self):
        raw = fixture()
        changed = patcher.transform(raw, hashlib.sha256(raw).hexdigest())
        expected = raw.replace(patcher.OLD_BUILD.encode(), patcher.NEW_BUILD.encode(), 1)
        for name, old in patcher.OLD_METHODS.items():
            replacement = patcher.NEW_METHODS[name]
            if name == '_validate_intent':
                replacement += '\n' + patcher.NEW_METHODS['_validate_v3_costs']
            expected = expected.replace(old.encode(), replacement.encode(), 1)
        self.assertEqual(changed, expected)
        self.assertIn(b"PRIVATE_KEY = 'offline-fixture-value'", changed)
        self.assertTrue(changed.endswith(b"def private_status():\n    return 'private-custom-status'\n"))

    def test_crlf_source_is_preserved_including_new_helper_and_builder(self):
        raw = fixture(b'\r\n')
        changed = patcher.transform(raw, hashlib.sha256(raw).hexdigest())
        self.assertNotIn(b'\n', changed.replace(b'\r\n', b''))
        self.assertIn(patcher.NEW_BUILD.replace('\n', '\r\n').encode(), changed)
        self.assertIn(patcher.NEW_METHODS['_validate_v3_costs'].replace('\n', '\r\n').encode(), changed)

    def test_wrong_full_pin_audited_method_bytes_and_duplicate_boundaries_refused(self):
        raw = fixture()
        with self.assertRaisesRegex(ValueError, 'GATEWAY_SOURCE_CHANGED'):
            patcher.transform(raw)
        for changed in (raw.replace(b"    def _request(self", b"    def other_request(self", 1),
                        raw.replace(patcher.OLD_BUILD.encode(), patcher.OLD_BUILD.replace('entry_side =', 'entry_side  =', 1).encode(), 1),
                        raw + b'\ndef build_intent(plan,operation):\n    return None\n'):
            with self.assertRaisesRegex(ValueError, 'GATEWAY_'):
                patcher.transform(changed, hashlib.sha256(changed).hexdigest())

    def test_patched_private_builder_matches_public_legacy_and_v3_contracts(self):
        raw = fixture()
        changed = patcher.transform(raw, hashlib.sha256(raw).hexdigest())
        namespace = {'__name__': 'worker.offline_v3_sdk', '__package__': 'worker'}
        exec(compile(changed, 'order_gateway.py', 'exec'), namespace)
        rules = Rules(D('.001'), D('.001'), D('1000'), D('.01'), D('5'), time.time(),
                      taker_fee_rate=D('.0005'), fee_observed_at=time.time(), fee_symbol='BTCUSDT', fee_source=FEE_SOURCE)
        for v3 in (False, True):
            plan = risk_check(Signal('BTCUSDT', 'LONG', D('100'), D('110'), D('98')), rules,
                              **(dict(risk_model=RISK_MODEL, reward_risk_policy=NORMALIZED_REWARD_RISK_POLICY) if v3 else {}))
            plan.update(protection_working_type='CONTRACT_PRICE', provenance={'payload_sha256': 'a'*64})
            plan['sizing_rules'] = {key: format(value, 'f') if isinstance(value, D) else value for key, value in asdict(rules).items()}
            value = namespace['build_intent'](plan, 'offline-v3-builder')
            self.assertEqual(value, build_intent(plan, 'offline-v3-builder'))
            namespace['OrderGateway'].__new__(namespace['OrderGateway'])._validate_intent(value)

    def test_source_inspection_is_read_only_and_apply_is_atomic_with_metadata(self):
        raw = fixture()
        original = patcher.transform
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'order_gateway.py'
            path.write_bytes(raw)
            path.chmod(0o640)
            before = path.stat()
            with patch.object(patcher, 'transform', side_effect=lambda value: original(value, hashlib.sha256(raw).hexdigest())):
                self.assertEqual(patcher.apply(path)['status'], 'PATCH_READY')
                self.assertEqual(path.read_bytes(), raw)
                with patch.object(patcher.os, 'geteuid', return_value=0):
                    result = patcher.apply(path, True)
            after = path.stat()
            self.assertEqual(result['status'], 'PATCHED')
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), result['after_sha256'])
            self.assertEqual((after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)),
                             (before.st_uid, before.st_gid, 0o640))
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_apply_refuses_concurrent_source_change_without_overwriting(self):
        raw = fixture()
        original = patcher.transform
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'order_gateway.py'
            path.write_bytes(raw)
            def concurrent(value):
                changed = original(value, hashlib.sha256(raw).hexdigest())
                path.write_bytes(raw+b'# concurrent-private-change\n')
                return changed
            with patch.object(patcher, 'transform', side_effect=concurrent), patch.object(patcher.os, 'geteuid', return_value=0):
                with self.assertRaisesRegex(ValueError, 'GATEWAY_SOURCE_CHANGED'):
                    patcher.apply(path, True)
            self.assertEqual(path.read_bytes(), raw+b'# concurrent-private-change\n')
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == '__main__':
    unittest.main()
