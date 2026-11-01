"""Refresh only an explicitly V4, definitely unsubmitted candidate.

An accepted, uncertain, or legacy intent is never migrated by this helper.
No exchange submission is performed here.
"""
from dataclasses import asdict
import json
import time

from .analysis import analysis_context
from .core import D, Review, RISK_MODEL, DYNAMIC_RISK_MODEL, DYNAMIC_REWARD_RISK_POLICY, risk_check, preflight
from .robot_provenance import source_signal, stamp, verify


def request_policy(db, operation):
    """A saved paid request retains its own policy, rather than today's default."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='research_browser_requests'").fetchone():
        return None
    row=db.execute('SELECT request_body FROM research_browser_requests WHERE operation=?',
        (operation,)).fetchone()
    if row is None:
        return None
    try:
        body=json.loads(row[0])
        value=body.get('context',{}).get('risk_constraints',{}).get('risk_model')
        if value==DYNAMIC_RISK_MODEL:
            return DYNAMIC_RISK_MODEL
        if value in (None,RISK_MODEL):
            return None
        raise Review('INVALID_COST_MODEL')
    except (ValueError,TypeError,AttributeError):
        raise Review('ROBOT_REQUEST_NEEDS_REVIEW') from None


def serialize_rules(rules):
    return {k:format(v,'f') if isinstance(v,D) else v for k,v in asdict(rules).items()}


def refresh_unsubmitted(coordinator, row, plan, *, build_intent, clock=time.time):
    if plan.get('risk_model')!=DYNAMIC_RISK_MODEL:
        return plan, None
    db=coordinator.db
    current=db.execute('SELECT * FROM order_intents WHERE id=?',(row['id'],)).fetchone()
    allowed=('READY_FOR_EXECUTION','EXECUTION_BLOCKED')
    if current is not None and (current['state'] not in allowed or current['result'] is not None):
        raise Review('ACCEPTED_OR_UNCERTAIN_INTENT_IMMUTABLE')
    if current is not None:
        try:
            if (current['candidate_id']!=row['id'] or current['symbol']!=row['symbol']
                    or json.loads(current['payload'])!=build_intent(plan,row['id'])):
                raise Review('ORDER_EVIDENCE_UNVERIFIED')
        except (KeyError,TypeError,ValueError):
            raise Review('ORDER_EVIDENCE_UNVERIFIED') from None
    if row['status'] not in allowed or not 0<=clock()-plan['rules_checked_at']<=300:
        raise Review('STALE_ORDER_INTENT')
    target=plan['risk_target_usdt']
    context,rules=analysis_context(coordinator.market,row['symbol'],target,
        coordinator.account_factory(),policy=DYNAMIC_RISK_MODEL,db=db)
    coordinator.market.fresh(context,row['symbol'])
    signal=source_signal(db,row['id'],row['symbol'],normalized=True,risk_model=DYNAMIC_RISK_MODEL)
    fresh=risk_check(signal,rules,target,risk_model=DYNAMIC_RISK_MODEL,
        reward_risk_policy=DYNAMIC_REWARD_RISK_POLICY)
    fresh['risk_target_usdt']=target
    fresh['protection_working_type']=plan.get('protection_working_type','CONTRACT_PRICE')
    fresh['sizing_rules']=serialize_rules(rules)
    preflight(fresh,rules,target);stamp(db,fresh,row['id']);verify(db,fresh,row['id'])
    payload=build_intent(fresh,row['id'])
    db.execute('BEGIN IMMEDIATE')
    try:
        candidate=db.execute('SELECT * FROM robot_candidates WHERE id=?',(row['id'],)).fetchone()
        intent=db.execute('SELECT * FROM order_intents WHERE id=?',(row['id'],)).fetchone()
        if (candidate is None or candidate['plan']!=row['plan'] or candidate['status'] not in allowed
                or candidate['symbol']!=row['symbol'] or candidate['cycle']!=row['cycle']):
            raise Review('ORDER_EVIDENCE_UNVERIFIED')
        if (current is None)!=(intent is None) or current is not None and (
                intent['payload']!=current['payload'] or intent['state'] not in allowed
                or intent['candidate_id']!=row['id'] or intent['symbol']!=row['symbol']
                or intent['result'] is not None):
            raise Review('ACCEPTED_OR_UNCERTAIN_INTENT_IMMUTABLE')
        db.execute('UPDATE robot_candidates SET plan=? WHERE id=?',
            (json.dumps(fresh),row['id']))
        if intent is not None:
            db.execute('UPDATE order_intents SET payload=? WHERE id=?',
                (json.dumps(payload),row['id']))
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK');raise
    return fresh,rules
