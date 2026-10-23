#!/usr/bin/env python3
"""Validate the isolated runtime policy; never start or import the app."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


MAX_BYTES = 65536
CONTRACT_PATH = Path(__file__).resolve().parents[1] / 'deploy/neurobro-agent.contract.json'

# These are requirements for future implementation, not observed capabilities.
# Changing this version's authority or readiness requires a reviewed new policy.
POLICY = {'schema_version': 2,
 'investigation': {'repository': 'harun5231/harun-ai-trading-office',
                   'main_commit': 'd99a3dd921d48e6347211a63fff765fecaf5c85d',
                   'agents_md': 'ABSENT_IN_COMPLETE_MAIN_TREE',
                   'deployed_source_match_verified': False},
 'service': {'name': 'harun-neurobro-agent',
             'compose_project': 'harun-neurobro-agent',
             'uid': 10002,
             'gid': 10002,
             'default_state': 'PAUSED',
             'mode': 'ROBOT_GATED_COORDINATION',
             'lifecycle_scope': 'OWNED_AGENT_INSTALL_UPDATE_RESTART_ROLLBACK',
             'state_storage': 'OWN_CONVERSATION_AND_OPERATION_DATABASE_ONLY',
             'proposal_state': 'UNVERIFIED_RESEARCH'},
 'readiness': {'runtime_implemented': True,
               'provider_verified': False,
               'deployment_authorized': False,
               'network_isolation_verified': False},
 'roles': {'market_data': 'BINANCE_FUTURES_PUBLIC',
           'setup_decision': 'NEUROBRO_WEB',
           'agent': 'BOUNDED_DIALOGUE_MEMORY_AND_SOURCE_ATTRIBUTED_HANDOFF',
           'risk_validation': 'EXISTING_RISK_MANAGER',
           'live_execution_authority': 'EXISTING_RISK_MANAGER',
           'live_executor': 'EXISTING_SINGLE_ORDER_GATEWAY'},
 'decision_contract': {'source_levels': ['ENTRY', 'TP', 'SL'],
                       'agent_may_invent_or_override_levels': False,
                       'agent_may_invent_or_override_risk': False,
                       'risk_manager_may_reject': True,
                       'live_requires': ['ROBOT_ON',
                                         'AUTHENTIC_NEUROBRO_SETUP',
                                         'FRESH_RISK_PREFLIGHT'],
                       'agent_may_mark_ready_for_execution': False,
                       'coordinator_tick_reuse': False,
                       'paid_llm_configured': False,
                       'fallback_ai_provider': None,
                       'model': 'gpt-5-mini',
                       'model_key_provisioning': 'DEDICATED_OPENAI_FILE_ONLY_NO_CHAT_NO_BINANCE_KEYS'},
 'public_market': {'source': 'Binance Futures',
                   'origin': 'https://fapi.binance.com',
                   'methods': ['GET'],
                   'paths': ['/fapi/v1/exchangeInfo',
                             '/fapi/v1/klines',
                             '/fapi/v1/premiumIndex',
                             '/fapi/v1/time'],
                   'timeframes': ['1h', '15m'],
                   'max_age_seconds': 180,
                   'matching_closed_candle_numeric_and_image_evidence_required': True,
                   'additional_market_endpoints_require_review': True},
 'browser_provider': {'interface': 'NEUROBRO_WEB_AUTOMATION',
                      'artifact_state': 'ADAPTER_IMPLEMENTED_PRODUCTION_BINDING_UNVERIFIED',
                      'source_commit': None,
                      'artifact_sha256': None,
                      'reconciliation_required': True,
                      'reconciliation_verified': False,
                      'max_critical_clarifications_per_setup': None,
                      'unknown_outcome': 'PAUSE_NO_REPLAY'},
 'sanitized_bridge': {'owner': 'ROOT',
                      'access': 'SANITIZED_SNAPSHOT_AND_ATOMIC_EXISTING_BACKEND_HANDOFF',
                      'required_data': ['ACCOUNT_EXPOSURE',
                                        'ACCOUNT_COMMISSION_RATE',
                                        'ENTRY_COUNT_JAKARTA_DAY',
                                        'PENDING_OWNERSHIP',
                                        'SYMBOL_RULES',
                                        'EXISTING_BACKEND_CAPABILITIES'],
                      'max_age_seconds': 120,
                      'bind_fields': ['source',
                                      'symbol',
                                      'observed_at',
                                      'expires_at',
                                      'evidence_sha256',
                                      'backend_revision',
                                      'account_generation',
                                      'idempotency_key'],
                      'credentials_shared': False,
                      'financial_state_writes': False,
                      'fallback_fee_rate': None,
                      'missing_or_stale_data': 'UNVERIFIED_PAUSE',
                      'artifact_sha256': None},
 'permissions': {'binance_credentials': False,
                 'neurobro_api_key': False,
                 'paid_llm_credentials': True,
                 'production_financial_database': False,
                 'production_browser_profile': False,
                 'order_gateway_access': False,
                 'place_order': False,
                 'cancel_order': False,
                 'transfer_funds': False,
                 'funding_operations': False,
                 'wallet_access': False,
                 'financial_writes': False,
                 'docker_socket': False,
                 'host_filesystem_mounts': False,
                 'production_service_control': False,
                 'privileged_container': False,
                 'public_inbound_port': False},
 'runtime_limits': {'memory_limit_mib': 256,
                    'cpu_millicores': 500,
                    'pids_limit': 64,
                    'tmpfs_limit_mib': 32,
                    'own_storage_limit_mib': 256,
                    'max_concurrent_tasks': 1,
                    'max_tasks_per_tick': 1,
                    'tick_budget_seconds': 120,
                    'market_http_timeout_seconds': 15,
                    'browser_operation_timeout_seconds': 90,
                    'maximum_backoff_seconds': 3600,
                    'max_proven_unsent_retries': 2,
                    'daily_candidate_cap': None,
                    'resource_exhaustion': 'YIELD_WITH_PERSISTED_PROGRESS',
                    'daily_model_requests': 48,
                    'daily_reserved_input_tokens': 200000,
                    'daily_reserved_output_tokens': 24000,
                    'max_critical_clarifications_per_setup': None,
                    'max_hold_replacement_rounds': None,
                    'daily_new_entries': 2,
                    'combined_position_pending_slots': 2,
                    'pending_review_interval_seconds': 18000,
                    'minimum_new_screening_cycle_interval_seconds': 120},
 'health': {'interface': 'LOCAL_READ_ONLY_STATUS',
            'identity_fields': ['service',
                                'compose_project',
                                'uid',
                                'gid',
                                'mode',
                                'state',
                                'source_commit',
                                'image_digest',
                                'runtime_config_sha256',
                                'process_generation',
                                'observed_at',
                                'expires_at',
                                'runtime_implemented',
                                'provider_verified',
                                'deployment_authorized'],
            'max_age_seconds': 60,
            'max_output_bytes': 16384,
            'starts_research': False,
            'starts_screening': False,
            'sends_chat': False,
            'calls_neurobro': False,
            'calls_binance': False,
            'loads_credentials': False,
            'opens_financial_database': False,
            'authorizes_deployment': False},
 'rollback': {'compose_project': 'harun-neurobro-agent',
              'service': 'harun-neurobro-agent',
              'action': 'RESTORE_VERIFIED_OWN_AGENT_PREDECESSOR_OR_REMOVE_OWN_FIRST_INSTALL',
              'own_evidence': 'PRESERVE',
              'requires_predeployment_absence_verified': False,
              'requires_new_service_ownership_proof': True,
              'touch_existing_containers': False,
              'restore_financial_database': False,
              'restore_browser_profile': False,
              'restore_credentials': False,
              'change_robot_state': False,
              'requires_owned_previous_image_config_and_compatible_data_schema': True}}


class ContractRejected(ValueError):
    """A fixed error category; never retain input values or private paths."""


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractRejected('DUPLICATE_JSON_FIELD')
        result[key] = value
    return result


def parse_contract(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise ContractRejected('CONTRACT_SIZE_INVALID')
    try:
        return json.loads(raw, object_pairs_hook=unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              ContractRejected('NONFINITE_JSON_VALUE')))
    except ContractRejected:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise ContractRejected('CONTRACT_JSON_INVALID') from None


def match_policy(value, expected):
    # Strict types prevent False being replaced by numeric zero, or UID by bool.
    if type(value) is not type(expected):
        raise ContractRejected('CONTRACT_POLICY_TYPE_MISMATCH')
    if type(expected) is dict:
        if set(value) != set(expected):
            raise ContractRejected('CONTRACT_FIELDS_MISMATCH')
        for key in expected:
            match_policy(value[key], expected[key])
    elif type(expected) is list:
        if len(value) != len(expected):
            raise ContractRejected('CONTRACT_POLICY_VALUE_MISMATCH')
        for actual, required in zip(value, expected):
            match_policy(actual, required)
    elif value != expected:
        raise ContractRejected('CONTRACT_POLICY_VALUE_MISMATCH')


def validate_contract(value):
    match_policy(value, POLICY)
    return {
        'status': 'RUNTIME_CONTRACT_VALID',
        'validation_only': True,
        'service': POLICY['service']['name'],
        'runtime_implemented': True,
        'provider_verified': False,
        'deployment_authorized': False,
        'production_actions': False,
        'blockers': ['PRODUCTION_BROWSER_BINDING_NOT_RECONCILED',
                     'PRODUCTION_BACKEND_ADAPTER_NOT_INSTALLED_OR_VERIFIED',
                     'OPENAI_KEY_NOT_PROVISIONED', 'ADMINISTRATIVE_BROKER_NOT_PROVISIONED',
                     'VPS_DEPLOYMENT_NOT_PERFORMED'],
    }


def read_contract(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= MAX_BYTES:
            raise ContractRejected('CONTRACT_FILE_UNSAFE')
        raw = os.read(fd, MAX_BYTES + 1)
        after = os.fstat(fd)
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if len(raw) != before.st_size or identity(before) != identity(after):
            raise ContractRejected('CONTRACT_FILE_UNSAFE')
        return raw
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Validate preparation only; never deploy or start research.')
    parser.add_argument('contract', nargs='?', type=Path, default=CONTRACT_PATH)
    args = parser.parse_args(argv)
    try:
        raw = read_contract(args.contract)
        result = validate_contract(parse_contract(raw))
        result['contract_sha256'] = hashlib.sha256(raw).hexdigest()
        exit_code = 0
    except Exception:
        result = {'status': 'PREPARATION_CONTRACT_REJECTED', 'validation_only': True,
                  'runtime_implemented': False, 'provider_verified': False,
                  'deployment_authorized': False, 'production_actions': False}
        exit_code = 1
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
