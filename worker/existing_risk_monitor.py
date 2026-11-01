"""Refresh estimates for owned frozen orders without changing an order.

The existing account actor and five-hour review call this observer. It has no
scheduler, gateway method, entry/exit endpoint, sizing, or TP normalization call.
Only a separate robot_status.risk_estimates projection is written.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
import time

from .analysis import analysis_context
from .core import DYNAMIC_RISK_MODEL, Review, validated_risk_target
from .dynamic_risk import estimate_existing


ACTIVE = ('ENTRY_PENDING', 'POSITION_PROTECTED', 'PROTECTION_INCOMPLETE')
COMPONENTS = ('gross_risk', 'entry_fee_usdt', 'sl_exit_fee_usdt',
    'sl_book_impact_usdt', 'sl_liquidity_uncertainty_usdt')


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _persist(db, row, record):
    """Merge only this observation and recheck unchanged ownership snapshot."""
    key = '$.risk_estimates."' + record['client_order_id'] + '"'
    db.execute('BEGIN IMMEDIATE')
    try:
        current = db.execute('SELECT state,payload FROM order_intents WHERE id=?', (row['id'],)).fetchone()
        if not current or current['payload'] != row['payload'] or current['state'] not in ACTIVE:
            db.execute('ROLLBACK')
            return False
        db.execute('''INSERT OR REPLACE INTO robot_status
            SELECT 1,json_set(COALESCE((SELECT data FROM robot_status WHERE id=1),'{}'),?,json(?))''',
            (key, _encode(record)))
        db.execute('COMMIT')
        return True
    except Exception:
        if db.in_transaction:db.execute('ROLLBACK')
        raise


def estimate_owned_order(coordinator, row, market, account_client, *,
                         context_rules=None, clock=time.time):
    """Estimate the ORIGINAL quantity/ENTRY/SL/TP and original risk budget.

    Failure is an explicit technical observation, never a HOLD or permission to
    cancel/reprice. Inputs, plans, payloads, receipts and protections are untouched.
    ``context_rules`` may reuse an already fresh V4 calculation for this order.
    """
    record = None
    try:
        if row['state'] not in ACTIVE:
            return None
        intent = coordinator.verified_intent(row)
        candidate = coordinator.db.execute('SELECT * FROM robot_candidates WHERE id=?',
            (row['candidate_id'],)).fetchone()
        plan = deepcopy(coordinator.store.verified_plan(candidate))
        if (intent['symbol'] != row['symbol'] or plan['symbol'] != row['symbol']
                or type(intent['client_order_id']) is not str
                or re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', intent['client_order_id']) is None):
            raise Review('ORDER_EVIDENCE_UNVERIFIED')
        observed = datetime.fromtimestamp(clock(), timezone.utc).isoformat()
        record = dict(kind='MONITORING_ESTIMATE_ONLY', source='RISK_MANAGER',
            status='UNAVAILABLE', symbol=intent['symbol'], client_order_id=intent['client_order_id'],
            intent_id=row['id'], intent_state=row['state'], observed_at=observed,
            order_parameters_changed=False, quantity_basis='FROZEN_ACCEPTED_ORDER_QUANTITY')
        target = validated_risk_target(plan['risk_target_usdt'])
        if context_rules is None:
            context, rules = analysis_context(market, intent['symbol'], target,
                account_client, policy=DYNAMIC_RISK_MODEL, db=coordinator.db)
        else:
            context, rules = context_rules
        market.fresh(context, intent['symbol'])
        value = estimate_existing(plan, rules)
        record.update(status='AVAILABLE', estimated_total_risk=value['estimated_total_risk'],
            original_risk_target_usdt=value['original_risk_target_usdt'],
            estimated_budget_difference_usdt=value['estimated_budget_difference_usdt'],
            components={key:value['components'][key] for key in COMPONENTS})
        # Keep the rates/sources/sample counts in the status projection, not
        # hundreds of depth/candle rows in each Office poll.
        evidence = value.get('cost_evidence', {})
        record['cost_evidence'] = {key:deepcopy(item) for key,item in evidence.items() if key != 'market'}
        if type(evidence.get('market')) is dict:
            record['cost_evidence'].update(source=evidence['market'].get('source'),
                observed_at=evidence['market'].get('observed_at'))
        record['cost_evidence_sha256'] = hashlib.sha256(_encode(evidence).encode()).hexdigest()
    except Exception as error:
        if record is None:
            return None  # Unverified ownership never supplies financial facts.
        reason = str(error)
        record.update(status='UNAVAILABLE', failure_code=reason if re.fullmatch(
            r'[A-Z][A-Z0-9_]{0,119}', reason or '') else 'RISK_ESTIMATE_UNAVAILABLE')
    try:
        return record if _persist(coordinator.db, row, record) else None
    except Exception:
        # This observer cannot interrupt account recovery or fill protection.
        return None



def _retire_closed_estimates(db):
    row = db.execute('SELECT data FROM robot_status WHERE id=1').fetchone()
    supplied = json.loads(row[0]).get('risk_estimates', {}) if row else {}
    if type(supplied) is not dict:return
    for client, value in supplied.items():
        if (type(client) is not str or re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', client) is None
                or type(value) is not dict or value.get('client_order_id') != client):continue
        current = db.execute('SELECT state,payload FROM order_intents WHERE id=?',
            (value.get('intent_id'),)).fetchone()
        if (current and current['state'] in ('CLOSED','REJECTED')
                and json.loads(current['payload']).get('client_order_id') == client):
            db.execute("UPDATE robot_status SET data=json_remove(data,?) WHERE id=1",
                ('$.risk_estimates."'+client+'"',))


def refresh_existing_risks(coordinator, market, account_client):
    """Called by the existing history/recovery account pass, never a new actor."""
    reports = []
    try:_retire_closed_estimates(coordinator.db)
    except Exception:pass
    for row in list(coordinator.db.execute("SELECT * FROM order_intents WHERE state IN "
            "('ENTRY_PENDING','POSITION_PROTECTED','PROTECTION_INCOMPLETE')")):
        value = estimate_owned_order(coordinator, row, market, account_client)
        if value is not None:reports.append(value)
    return reports
