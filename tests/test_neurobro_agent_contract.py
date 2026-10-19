"""Offline preparation-policy denial tests; no app, secrets, or live services."""
import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('agent_contract_validator',
    ROOT / 'scripts/validate_neurobro_agent_contract.py')
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class NeurobroAgentContractTests(unittest.TestCase):
    def contract(self):
        return validator.parse_contract((ROOT / 'deploy/neurobro-agent.contract.json').read_bytes())

    def rejected(self, path, replacement):
        value = self.contract()
        parent = value
        for key in path[:-1]:
            parent = parent[key]
        parent[path[-1]] = replacement
        with self.assertRaises(validator.ContractRejected):
            validator.validate_contract(value)

    def test_fixed_contract_is_valid_but_not_runtime_or_deployment_ready(self):
        result = validator.validate_contract(self.contract())
        self.assertEqual(result['status'], 'RUNTIME_CONTRACT_VALID')
        self.assertIs(result['runtime_implemented'], True)
        for field in ('provider_verified', 'deployment_authorized', 'production_actions'):
            self.assertIs(result[field], False)
        self.assertIn('VPS_DEPLOYMENT_NOT_PERFORMED', result['blockers'])
        self.assertIn('PRODUCTION_BROWSER_BINDING_NOT_RECONCILED', result['blockers'])

    def test_exact_role_assignment_and_original_setup_authority_cannot_be_changed(self):
        for role in self.contract()['roles']:
            with self.subTest(role=role):
                self.rejected(('roles', role), 'AGENT_SELF_AUTHORIZED')
        for field in ('agent_may_invent_or_override_levels', 'agent_may_invent_or_override_risk',
                      'agent_may_mark_ready_for_execution', 'coordinator_tick_reuse'):
            with self.subTest(field=field):
                self.rejected(('decision_contract', field), True)
        self.rejected(('decision_contract', 'live_requires'), ['ROBOT_ON'])
        self.rejected(('decision_contract', 'source_levels'), ['ENTRY', 'SL'])

    def test_credentials_funding_and_all_privileged_permissions_are_denied(self):
        for field in self.contract()['permissions']:
            with self.subTest(permission=field):
                self.rejected(('permissions', field), not self.contract()['permissions'][field])
        self.rejected(('decision_contract', 'paid_llm_configured'), True)
        self.rejected(('decision_contract', 'fallback_ai_provider'), 'PAID_MODEL')

    def test_service_identity_paused_state_and_database_separation_are_pinned(self):
        for key, replacement in (('name', 'worker'), ('compose_project', 'harun-office'),
                ('uid', 0), ('gid', 10001), ('default_state', 'RUNNING'), ('mode', 'LIVE'),
                ('state_storage', 'PRODUCTION_LEDGER'), ('proposal_state', 'READY_FOR_EXECUTION')):
            with self.subTest(key=key):
                self.rejected(('service', key), replacement)

    def test_every_readiness_claim_remains_false(self):
        for field in self.contract()['readiness']:
            with self.subTest(field=field):
                self.rejected(('readiness', field), not self.contract()['readiness'][field])
        self.rejected(('investigation', 'deployed_source_match_verified'), True)

    def test_source_allowlist_rejects_other_origin_signed_paths_or_methods(self):
        for key, replacement in (('origin', 'https://example.com'), ('methods', ['GET', 'POST']),
                ('paths', ['/fapi/v1/order']), ('source', 'BINANCE_PRIVATE'),
                ('timeframes', ['15m']), ('max_age_seconds', 999999),
                ('matching_closed_candle_numeric_and_image_evidence_required', False)):
            with self.subTest(key=key):
                self.rejected(('public_market', key), replacement)

    def test_unavailable_provider_pin_is_explicit_and_never_invents_verification(self):
        self.rejected(('browser_provider', 'artifact_state'), 'VERIFIED')
        self.rejected(('browser_provider', 'source_commit'), 'a' * 40)
        self.rejected(('browser_provider', 'artifact_sha256'), 'b' * 64)
        self.rejected(('browser_provider', 'reconciliation_verified'), True)
        self.rejected(('browser_provider', 'reconciliation_required'), False)
        self.rejected(('browser_provider', 'max_critical_clarifications_per_setup'), 3)
        self.rejected(('browser_provider', 'unknown_outcome'), 'RETRY')

    def test_fee_and_account_bridge_requires_fresh_sanitized_root_owned_evidence(self):
        for key, replacement in (('owner', 'AGENT'), ('access', 'READ_WRITE'),
                ('max_age_seconds', 9999), ('credentials_shared', True),
                ('financial_state_writes', True), ('fallback_fee_rate', '0.0005'),
                ('missing_or_stale_data', 'ASSUME_FEE'), ('bind_fields', ['symbol']),
                ('required_data', []), ('artifact_sha256', 'c' * 64)):
            with self.subTest(key=key):
                self.rejected(('sanitized_bridge', key), replacement)

    def test_runtime_resource_and_retry_limits_cannot_be_inflated_or_disabled(self):
        for key, current in self.contract()['runtime_limits'].items():
            replacement = 999999 if type(current) is int else 'UNBOUNDED'
            with self.subTest(key=key):
                self.rejected(('runtime_limits', key), replacement)

    def test_hold_replacement_rounds_have_no_numeric_maximum(self):
        self.assertIsNone(self.contract()['runtime_limits']['max_hold_replacement_rounds'])
        for cap in (0, 3, 1000000, '3'):
            with self.subTest(cap=cap):
                self.rejected(('runtime_limits', 'max_hold_replacement_rounds'), cap)

    def test_health_never_activates_chat_screening_market_or_credentials(self):
        self.assertTrue({'source_commit', 'image_digest', 'runtime_config_sha256'}
                        <= set(self.contract()['health']['identity_fields']))
        for key, current in self.contract()['health'].items():
            if type(current) is bool:
                with self.subTest(key=key):
                    self.rejected(('health', key), True)
        self.rejected(('health', 'identity_fields'), ['service'])
        self.rejected(('health', 'max_age_seconds'), 3600)
        self.rejected(('health', 'max_output_bytes'), 999999)

    def test_rollback_cannot_touch_worker_finance_profile_or_robot_state(self):
        for key, replacement in (('compose_project', 'harun-office'), ('service', 'worker'),
                ('action', 'RESTORE_BACKUP'), ('own_evidence', 'DELETE'),
                ('touch_existing_containers', True), ('restore_financial_database', True),
                ('restore_browser_profile', True), ('restore_credentials', True),
                ('change_robot_state', True)):
            with self.subTest(key=key):
                self.rejected(('rollback', key), replacement)

    def test_first_install_scope_requires_absence_and_new_service_ownership_proof(self):
        self.rejected(('service', 'lifecycle_scope'), 'UNSCOPED_PRODUCTION_UPDATE')
        self.rejected(('rollback', 'requires_predeployment_absence_verified'), True)
        self.rejected(('rollback', 'requires_new_service_ownership_proof'), False)

    def test_baseline_investigation_is_a_full_commit_not_a_mutable_or_deployed_claim(self):
        self.rejected(('investigation', 'main_commit'), 'main')
        self.rejected(('investigation', 'repository'), 'other/repo')
        self.rejected(('investigation', 'agents_md'), 'NOT_CHECKED')

    def test_closed_fields_reject_additions_and_omissions_at_every_object(self):
        def walk(value, path=()):
            if type(value) is dict:
                yield path
                for key, item in value.items():
                    yield from walk(item, path + (key,))
        for path in walk(self.contract()):
            for action in ('add', 'omit'):
                with self.subTest(path=path, action=action):
                    value = self.contract()
                    obj = value
                    for key in path:
                        obj = obj[key]
                    if action == 'add':
                        obj['unreviewed_capability'] = True
                    else:
                        obj.pop(next(iter(obj)))
                    with self.assertRaises(validator.ContractRejected):
                        validator.validate_contract(value)

    def test_scalar_types_are_strict_including_false_numeric_zero_and_uid_bool(self):
        for path, replacement in ((('readiness', 'deployment_authorized'), 0),
                (('service', 'uid'), True), (('schema_version',), True),
                (('runtime_limits', 'memory_limit_mib'), 256.0),
                (('browser_provider', 'source_commit'), False)):
            with self.subTest(path=path):
                self.rejected(path, replacement)

    def test_duplicate_nonfinite_malformed_and_oversized_json_are_rejected(self):
        for raw in (b'{"schema_version":1,"schema_version":1}', b'{"x":NaN}',
                    b'{"x":Infinity}', b'\xff', b'[', b'x' * 65537, b''):
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(validator.ContractRejected):
                    validator.parse_contract(raw)

    def test_cli_validation_prints_only_preparation_result_and_no_secret_canary(self):
        for valid in (True, False):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'contract.json'
                value = self.contract()
                if not valid:
                    value['permissions']['PRIVATE_CANARY_DO_NOT_PRINT'] = 'PRIVATE_CANARY_DO_NOT_PRINT'
                path.write_text(json.dumps(value))
                before = path.read_bytes()
                output = io.StringIO()
                with patch('sys.stdout', output):
                    code = validator.main([str(path)])
                self.assertEqual(code, 0 if valid else 1)
                result = json.loads(output.getvalue())
                self.assertIs(result['deployment_authorized'], False)
                self.assertIs(result['runtime_implemented'], valid)
                self.assertNotIn('PRIVATE_CANARY', output.getvalue())
                self.assertEqual(path.read_bytes(), before)

    def test_source_read_refuses_symlink_hardlink_and_nonregular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'contract.json'
            path.write_bytes(b'{}')
            link = Path(directory) / 'link.json'
            link.symlink_to(path)
            with self.assertRaises((OSError, validator.ContractRejected)):
                validator.read_contract(link)
            link.unlink()
            os.link(path, link)
            with self.assertRaises(validator.ContractRejected):
                validator.read_contract(path)
            with self.assertRaises((OSError, validator.ContractRejected)):
                validator.read_contract(directory)

    def test_validator_imports_only_stdlib_and_has_no_app_or_network_execution(self):
        source = (ROOT / 'scripts/validate_neurobro_agent_contract.py').read_text()
        tree = ast.parse(source)
        imports = {node.module.split('.')[0] if isinstance(node, ast.ImportFrom) else alias.name.split('.')[0]
            for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in (node.names if isinstance(node, ast.Import) else [None])}
        self.assertEqual(imports, {'argparse', 'hashlib', 'json', 'os', 'pathlib', 'stat', 'sys'})
        prohibited = {'exec', 'eval', '__import__', 'system', 'popen', 'fork', 'setuid', 'setgid',
                      'socket', 'connect', 'urlopen', 'request', 'submit', 'reconcile', 'tick'}
        called = {node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))}
        self.assertFalse(called & prohibited)


if __name__ == '__main__':
    unittest.main()
