"""Offline checks for the combined VPS patch, preserving the private adapter."""
import ast
import base64
import hashlib
import json
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'deploy/apply_rr_replacement.sh').read_text()
PATCHER = SCRIPT.split("<<'HARUN_GATEWAY_PATCH'\n", 1)[1].split('\nHARUN_GATEWAY_PATCH', 1)[0]
NAMESPACE = {'__name__': 'offline_gateway_patch'}
exec(compile(PATCHER, 'gateway_patch.py', 'exec'), NAMESPACE)
TRANSFORM = NAMESPACE['transform_gateway']


OLD_BUILD = r'''def build_intent(plan, operation):
    """Immutable internal order command, never a user ticket or exchange receipt."""
    if not isinstance(plan, dict) or plan.get('mode') != 'ORDER_INTENT' or plan.get('symbol') == 'HYPEUSDT':
        raise Review('INVALID_ORDER_CONTRACT')
    try:
        target = validated_risk_target(plan['risk_target_usdt'])
        risk = number(plan['risk'])
    except (KeyError, TypeError, Review): raise Review('INVALID_ORDER_CONTRACT') from None
    if plan.get('side') not in ('LONG', 'SHORT') or not D('0') < risk <= target:
        raise Review('INVALID_ORDER_CONTRACT')
    entry_side = 'BUY' if plan['side'] == 'LONG' else 'SELL'
    client_id = 'hao-' + hashlib.sha256(operation.encode()).hexdigest()[:28]
    return dict(intent_id=operation, client_order_id=client_id,
        symbol=plan['symbol'], position_side='BOTH', side=plan['side'],
        entry=dict(order_type='LIMIT', side=entry_side, price=plan['entry'],
                   quantity=plan['execution_quantity'], time_in_force='GTC'),
        protection=dict(exit_side='SELL' if entry_side == 'BUY' else 'BUY',
                        stop_loss=plan['sl'], take_profit=plan['tp'], working_type='MARK_PRICE'),
        margin_mode=plan['margin_mode'], leverage=plan['leverage'],
        risk_target_usdt=plan['risk_target_usdt'], risk_usdt=plan['risk'],
        gross_risk_usdt=plan['gross_risk'],entry_fee_usdt=plan['entry_fee_usdt'],
        sl_exit_fee_usdt=plan['sl_exit_fee_usdt'],tp_exit_fee_usdt=plan['tp_exit_fee_usdt'],
        net_reward_usdt=plan['net_reward'],net_reward_risk=plan['net_rr'],
        fee_evidence=dict(source=plan['fee_source'],symbol=plan['fee_symbol'],
            observed_at=plan['fee_observed_at'],taker_rate=plan['entry_fee_rate']),
        excluded_costs=plan['excluded_costs'],
        evidence_sha256=plan['provenance']['payload_sha256'])'''
OLD_VALIDATE = r'''def _validate_intent(self, intent):
        import re
        from fractions import Fraction
        try:
            if not isinstance(intent, dict):
                raise ValueError()
            symbol, client = intent['symbol'], intent['client_order_id']
            if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9]{2,18}USDT', symbol) or symbol == 'HYPEUSDT':
                raise ValueError()
            if not isinstance(client, str) or not re.fullmatch(r'hao-[a-f0-9]{28}', client):
                raise ValueError()
            operation = intent['intent_id']
            if not isinstance(operation, str) or client != 'hao-' + hashlib.sha256(operation.encode()).hexdigest()[:28]:
                raise ValueError()
            if intent['position_side'] != 'BOTH' or intent['margin_mode'] != 'CROSS' or type(intent['leverage']) is not int or intent['leverage'] != 75:
                raise ValueError()
            if intent['side'] not in ('LONG', 'SHORT'):
                raise ValueError()
            entry, protection = intent['entry'], intent['protection']
            side = 'BUY' if intent['side'] == 'LONG' else 'SELL'
            if entry['side'] != side or entry['order_type'] != 'LIMIT' or entry['time_in_force'] != 'GTC':
                raise ValueError()
            if protection['exit_side'] != ('SELL' if side == 'BUY' else 'BUY') or protection['working_type'] != 'MARK_PRICE':
                raise ValueError()
            e, q, sl, tp = (Fraction(self._decimal(v, positive=True)) for v in
                             (entry['price'], entry['quantity'], protection['stop_loss'], protection['take_profit']))
            if not (sl < e < tp if side == 'BUY' else tp < e < sl):
                raise ValueError()
            fees = intent['fee_evidence']
            rate = Fraction(self._decimal(fees['taker_rate']))
            if fees['source'] != 'BINANCE_FUTURES_COMMISSION_RATE' or fees['symbol'] != symbol or not 0 <= rate < 1:
                raise ValueError()
            target = Fraction(validated_risk_target(intent['risk_target_usdt']))
            gross = q * abs(e - sl)
            entry_fee, sl_fee, tp_fee = q*e*rate, q*sl*rate, q*tp*rate
            risk, reward = gross + entry_fee + sl_fee, q*abs(tp-e) - entry_fee - tp_fee
            expected = {'risk_usdt': risk, 'gross_risk_usdt': gross,
                        'entry_fee_usdt': entry_fee, 'sl_exit_fee_usdt': sl_fee,
                        'tp_exit_fee_usdt': tp_fee, 'net_reward_usdt': reward}
            if not 0 < risk <= target or reward < 2*risk:
                raise ValueError()
            if any(Fraction(self._decimal(intent[key])) != val for key, val in expected.items()):
                raise ValueError()
            ratio = intent['net_reward_risk']
            if not isinstance(ratio, str) or len(ratio) > 256 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:E[+-]?[0-9]{1,3})?', ratio):
                raise ValueError()
            if not Decimal(ratio).is_finite() or Fraction(Decimal(ratio)) < 2 or intent['excluded_costs'] != ['SLIPPAGE', 'FUNDING', 'GAPS']:
                raise ValueError()
            if not re.fullmatch(r'[a-f0-9]{64}', intent['evidence_sha256']):
                raise ValueError()
        except Exception:
            raise Review('BINANCE_ORDER_INTENT_INVALID') from None'''


OLD_CANCEL = '''def _cancel_algo(self, intent, algo):
        kind = 'sl' if algo['clientAlgoId'].startswith('hao-sl-') else 'tp'
        quantity = self._decimal(algo['quantity'], positive=True)
        current = self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': algo['clientAlgoId']})
        self._algo_proof(intent, kind, quantity, current)
        if current['algoStatus'] != 'NEW':
            if current['algoStatus'] not in ('CANCELED', 'EXPIRED', 'REJECTED') or current.get('actualOrderId') not in ('', '0', 0, None):
                raise Review('BINANCE_ORDER_EXIT_RACE')
            return
        self._request('DELETE', '/fapi/v1/algoOrder', {'algoId': self._id(current['algoId'])})
        after = self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': algo['clientAlgoId']})
        self._algo_proof(intent, kind, quantity, after)
        if after['algoStatus'] != 'CANCELED' or after.get('actualOrderId') not in ('', '0', 0, None):
            raise Review('BINANCE_ORDER_EXIT_RACE')'''


def fixture():
    template = (ROOT / 'deploy/live_gateway_template.py').read_bytes()
    cls = next(node for node in ast.parse(template).body if isinstance(node, ast.ClassDef))
    start, end = NAMESPACE['span'](template, cls)
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_validate_intent')
    first, last = NAMESPACE['span'](template, method)
    block = template[start:first] + OLD_VALIDATE.encode() + template[last:end]
    changes = (
        (b"workingType=intent['protection']['working_type'], reduceOnly=True", b"workingType='MARK_PRICE', reduceOnly=True"),
        (b"'workingType': intent['protection']['working_type'], 'clientAlgoId': client", b"'workingType': 'MARK_PRICE', 'clientAlgoId': client"),
    )
    for after, before in changes:
        assert block.count(after) == 1
        block = block.replace(after, before, 1)
    # Preserve this historical installer's audited cancellation method even
    # after a later runtime adds bounded GET-only cancellation readbacks.
    historical_class = next(node for node in ast.parse(block).body if isinstance(node, ast.ClassDef))
    cancel = next(node for node in historical_class.body if isinstance(node, ast.FunctionDef) and node.name == '_cancel_algo')
    first, last = NAMESPACE['span'](block, cancel)
    block = block[:first] + OLD_CANCEL.encode() + block[last:]
    prefix = b"# Private VPS prefix, preserve bytes.\nimport hashlib\nfrom .core import D, Review, number, validated_risk_target\nfrom decimal import Decimal\nPRIVATE_KEY = 'offline-secret-literal'\nclass _MissingOrderImplementation:\n    pass\n\n"
    suffix = b"\n\ndef private_status():\n    return 'configured-private-value'\n"
    return prefix + OLD_BUILD.encode() + b'\n\n' + block + suffix, prefix, suffix


class CombinedVpsPatchTests(unittest.TestCase):
    def test_transform_preserves_unknown_prefix_suffix_and_every_other_method(self):
        raw, prefix, suffix = fixture()
        changed = TRANSFORM(raw, hashlib.sha256(raw).hexdigest())
        self.assertTrue(changed.startswith(prefix))
        self.assertTrue(changed.endswith(suffix))
        self.assertIn(b'    from .core import validated_protection_working_type\n', changed)
        old_tree, new_tree = ast.parse(raw), ast.parse(changed)
        old_class = next(n for n in old_tree.body if isinstance(n, ast.ClassDef) and n.name == 'OrderGateway')
        new_class = next(n for n in new_tree.body if isinstance(n, ast.ClassDef) and n.name == 'OrderGateway')
        affected = {'_validate_intent', '_algo_proof', '_ensure_algo'}
        for old, new in zip(old_class.body, new_class.body):
            if isinstance(old, ast.FunctionDef) and old.name in affected:
                continue
            self.assertEqual(ast.dump(old), ast.dump(new))
        self.assertEqual(NAMESPACE['shape_digest'](new_class), NAMESPACE['NEW_CLASS_SHAPE'])

    def test_crlf_private_source_stays_crlf(self):
        raw, prefix, suffix = fixture()
        raw = raw.replace(b'\n', b'\r\n')
        changed = TRANSFORM(raw, hashlib.sha256(raw).hexdigest())
        self.assertTrue(changed.startswith(prefix.replace(b'\n', b'\r\n')))
        self.assertTrue(changed.endswith(suffix.replace(b'\n', b'\r\n')))
        self.assertNotIn(b'\n', changed.replace(b'\r\n', b''))

    def test_wrong_fingerprint_or_changed_ast_is_refused_before_transformation(self):
        raw, _, _ = fixture()
        with self.assertRaisesRegex(AssertionError, 'GATEWAY_SOURCE_CHANGED'):
            TRANSFORM(raw)
        changed = raw.replace(b'    def status(self)', b'    def different_status(self)', 1)
        with self.assertRaisesRegex(AssertionError, 'GATEWAY_SHAPE_CHANGED'):
            TRANSFORM(changed, hashlib.sha256(changed).hexdigest())
        duplicate = raw + b'\ndef build_intent(plan, operation):\n    return None\n'
        with self.assertRaisesRegex(AssertionError, 'GATEWAY_BOUNDARIES_CHANGED'):
            TRANSFORM(duplicate, hashlib.sha256(duplicate).hexdigest())

    def test_patched_private_builder_uses_local_import_for_legacy_and_last_price(self):
        from worker.core import D, Signal, risk_check
        from support import RULES
        from worker.order_gateway import build_intent
        raw, _, _ = fixture()
        changed = TRANSFORM(raw, hashlib.sha256(raw).hexdigest())
        module = {'__name__': 'worker.offline_private_gateway', '__package__': 'worker'}
        exec(compile(changed, 'order_gateway.py', 'exec'), module)
        self.assertNotIn('validated_protection_working_type', module)
        plan = risk_check(Signal('BTCUSDT', 'LONG', D('100'), D('104'), D('98')), RULES())
        plan.update(risk_target_usdt='5', provenance={'payload_sha256': 'offline-proof'})
        command = module['build_intent'](plan, 'offline-operation')
        self.assertEqual(command, build_intent(plan, 'offline-operation'))
        self.assertEqual(command['protection']['working_type'], 'MARK_PRICE')
        plan['protection_working_type'] = 'CONTRACT_PRICE'
        command = module['build_intent'](plan, 'offline-operation')
        self.assertEqual(command, build_intent(plan, 'offline-operation'))
        self.assertEqual(command['protection']['working_type'], 'CONTRACT_PRICE')

    def test_patched_builder_and_sdk_validate_new_policy_without_weakening_risk_cap(self):
        from dataclasses import asdict
        from worker.core import D, Signal, risk_check, REWARD_RISK_POLICY, Review
        from support import RULES
        raw, _, _ = fixture()
        changed = TRANSFORM(raw, hashlib.sha256(raw).hexdigest())
        module = {'__name__': 'worker.offline_private_gateway', '__package__': 'worker'}
        exec(compile(changed, 'order_gateway.py', 'exec'), module)
        rules = RULES()
        plan = risk_check(Signal('BTCUSDT', 'LONG', D('100'), D('104'), D('98')), rules,
                          reward_risk_policy=REWARD_RISK_POLICY)
        plan.update(risk_target_usdt='5', protection_working_type='CONTRACT_PRICE',
                    provenance={'payload_sha256': 'a' * 64})
        plan['sizing_rules'] = {key: str(value) if isinstance(value, D) else value
                                for key, value in asdict(rules).items()}
        command = module['build_intent'](plan, 'offline-operation')
        self.assertEqual(command['reward_risk_policy'], REWARD_RISK_POLICY)
        self.assertEqual(command['tp_tick_size'], '0.01')
        gateway = module['OrderGateway'].__new__(module['OrderGateway'])
        gateway._validate_intent(command)
        command['risk_target_usdt'] = '4'
        with self.assertRaisesRegex(Review, 'BINANCE_ORDER_INTENT_INVALID'):
            gateway._validate_intent(command)
        command['risk_target_usdt'] = '5'
        command['protection']['take_profit'] = '105'
        with self.assertRaisesRegex(Review, 'NET_RISK_REWARD_NOT_TARGET_2'):
            gateway._validate_intent(command)

    def test_runtime_patch_matches_frozen_targets(self):
        patch = SCRIPT.split("<<'HARUN_PATCH'\n", 1)[1].split('\nHARUN_PATCH', 1)[0] + '\n'
        old_pins = {
            'worker/robot.py': '4bb72c65d8eb3bada7c3739058e270cf648d129de64d2042ecee90a4627bbbef',
            'worker/core.py': '11df540717e6e89a7da2777fa373723562f4ca05b87e3540c36c1902dd7cf9df',
            'worker/robot_provenance.py': 'f539eecb35ca57f8a8f91e9cbbcae2900f31e9dcfcd72246070d3d967582cb15',
            'worker/analysis.py': 'f9712ce490f99061a45e32962d6489dd372a74aa18213001cc5d167208543a6d',
            'worker/diagnostics.py': 'cf6802f0b5692e69f6573a36509828c70cdf226acd1894aff3b068a9c98fe190',
        }
        new_pins = {
            'worker/robot.py': 'cc0e1c6b68581fa23402dd61c9de023941a1f13489137282c83ae32ccf0dfa8d',
            'worker/core.py': '4b08349cc49470c47d2d29f0e6e2d72e086356f42d90b1afab92f98a4f0c6b5e',
            'worker/robot_provenance.py': '2975732f7f3888147ae50c81b2b53e85c8c0930c47310f88246378a53ceac082',
            'worker/analysis.py': '1777b60c663fd971969b526a7c2c785eed3cd057d0edc70fb55453377428b97e',
            'worker/diagnostics.py': '2b28a42b0f03c76c97dc5111a33b2be167d346f8aba1103a7c0be10f08382f41',
        }
        # This historical VPS patch has fixed input/output bytes, independent
        # of subsequent runtime changes or the checkout's current Git history.
        snapshot = json.loads((ROOT / 'tests/fixtures/rr_replacement_base.json').read_text())
        self.assertEqual(snapshot['source_commit'], 'c02ed13cc95766a87cbb37f9e3fe1ccc25c46e3e')
        self.assertEqual(snapshot['encoding'], 'zlib+base64')
        self.assertEqual(set(snapshot['files']), set(old_pins))
        base = {name: zlib.decompress(base64.b64decode(encoded, validate=True))
                for name, encoded in snapshot['files'].items()}
        for name, expected in old_pins.items():
            self.assertEqual(hashlib.sha256(base[name]).hexdigest(), expected, name)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'worker').mkdir()
            for name, raw in base.items():
                (root / name).write_bytes(raw)
            subprocess.run(['git', 'apply', '--check', '-'], cwd=root, input=patch.encode(), check=True, capture_output=True)
            subprocess.run(['git', 'apply', '-'], cwd=root, input=patch.encode(), check=True, capture_output=True)
            for name, expected in new_pins.items():
                self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), expected, name)
            subprocess.run(['git', 'apply', '--reverse', '-'], cwd=root, input=patch.encode(), check=True, capture_output=True)
            for name, raw in base.items():
                self.assertEqual((root / name).read_bytes(), raw, name)

    def test_shell_syntax_and_unresolved_order_guard_are_present(self):
        subprocess.run(['bash', '-n', str(ROOT / 'deploy/apply_rr_replacement.sh')], check=True, capture_output=True)
        checker = SCRIPT.split("<<'HARUN_CHECK'\n", 1)[1].split('\nHARUN_CHECK', 1)[0]
        self.assertIn("state IN ('SUBMITTING','NEEDS_REVIEW')", checker)
        self.assertEqual(SCRIPT.count('/app preflight <'), 2)
        self.assertIn('trap \'harun_abort "$?"\' ERR', SCRIPT)
        self.assertNotIn('worker.robot tick', SCRIPT)


if __name__ == '__main__':
    unittest.main()
