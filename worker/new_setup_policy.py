"""Refresh only an explicitly V4, definitely unsubmitted candidate.

An accepted, uncertain, or legacy intent is never migrated by this helper.
No exchange submission is performed here.
"""
from dataclasses import asdict
import json
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .analysis import analysis_context
from .core import D, Review, RISK_MODEL, DYNAMIC_RISK_MODEL, DYNAMIC_REWARD_RISK_POLICY, risk_check, preflight, deserialize_sizing_rules
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


def stale_unsubmitted(coordinator,row,*,build_intent,retired=False,orphan=False,clock=time.time):
    """Prove an expired V4 command never crossed the native submit claim.

    The native coordinator durably claims SUBMITTING before gateway invocation;
    uncertain or accepted submissions never return to an eligible state. Any
    result, entry receipt, identity drift or invalid source plan fails this
    pre-POST custody proof. Frozen plans and payloads stay untouched.
    """
    try:
        if retired:
            if row['status']!='REJECTED' or row['failure_code']!='STALE_ORDER_INTENT':return False
        elif orphan:
            if row['status']!='REJECTED' or row['failure_code']!='ORDER_EVIDENCE_UNVERIFIED':return False
        elif row['status']not in('READY_FOR_EXECUTION','EXECUTION_BLOCKED'):return False
        plan=coordinator.store.verified_plan(row);current=clock();stamp=plan['rules_checked_at']
        if (plan.get('risk_model')!=DYNAMIC_RISK_MODEL or type(current)not in(int,float)
                or type(stamp)not in(int,float)or not math.isfinite(current)or not math.isfinite(stamp)
                or not 0<stamp<=current):return False
        today=datetime.fromtimestamp(current,ZoneInfo('Asia/Jakarta')).date().isoformat()
        if current-stamp<=300 and row['cycle'].split(':',1)[0]==today:return False
        expected=build_intent(plan,row['id']);db=coordinator.db
        intents=list(db.execute('SELECT * FROM order_intents WHERE id=? OR candidate_id=?',(row['id'],row['id'])))
        if len(intents)>1:return False
        if intents:
            intent=intents[0]
            states=('REJECTED',)if retired else('READY_FOR_EXECUTION',)if orphan else('READY_FOR_EXECUTION','EXECUTION_BLOCKED')
            reason=('STALE_ORDER_INTENT'if retired else None if intent['state']=='READY_FOR_EXECUTION'
                else 'BINANCE_ORDER_GATEWAY_NOT_CONNECTED')
            if (intent['id']!=row['id']or intent['candidate_id']!=row['id']or intent['symbol']!=row['symbol']
                    or intent['state']not in states or intent['result']is not None or intent['failure_code']!=reason
                    or json.loads(intent['payload'])!=expected):return False
        elif orphan:return False
        return not db.execute('SELECT 1 FROM robot_entry_receipts WHERE id=?',(expected['client_order_id'],)).fetchone()
    except (KeyError,TypeError,ValueError,OverflowError,Review):return False


def retire_stale_unsubmitted(coordinator,row,account,*,build_intent,orphan=False,clock=time.time):
    """Atomically use the existing reject lifecycle only after fresh custody."""
    db=coordinator.db
    if db.in_transaction:return None
    db.execute('BEGIN IMMEDIATE')
    try:
        current=db.execute('SELECT * FROM robot_candidates WHERE id=?',(row['id'],)).fetchone()
        if (current is None or dict(current)!=dict(row)or not stale_unsubmitted(coordinator,current,
                build_intent=build_intent,orphan=orphan,clock=clock)):
            db.execute('ROLLBACK');return None
        result=coordinator.reject_intent(current,account,'STALE_ORDER_INTENT')
        db.execute('COMMIT');return result
    except BaseException:
        db.execute('ROLLBACK');raise


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
    # A pre-existing V4 candidate retains its frozen sizing arithmetic. Only
    # newly analyzed candidates opt into schema 2 account-margin sizing.
    frozen_rules=deserialize_sizing_rules(plan)
    margin_sizing=frozen_rules.leverage_brackets.get('schema_version')==2
    context,rules=analysis_context(coordinator.market,row['symbol'],target,
        coordinator.account_factory(),policy=DYNAMIC_RISK_MODEL,db=db,margin_sizing=margin_sizing)
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
