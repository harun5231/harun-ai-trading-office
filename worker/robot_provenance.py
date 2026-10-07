"""Verify current order intents without promoting historical setup evidence."""
import json
import re
from dataclasses import fields as rule_fields
from .core import Review,D,number,validated_risk_target,validated_protection_working_type,Rules,maximum_risk_quantity,verified_rr,risk_costs,validate_reward_risk_policy
from .provenance import digest,request_digest
from .neuroapi import setup

VERSION='analysis-v9'
SIZING='deterministic-order-fee-risk-v2'
SIZING_V3='deterministic-order-fee-slippage-risk-v3'
OPERATION=r'\d{4}-\d{2}-\d{2}:robot-v9:(?:0|[1-9]\d{0,11}):analysis-v9:'

def sizing_rules(plan):
    values=plan['sizing_rules']
    if not isinstance(values,dict) or set(values)!={field.name for field in rule_fields(Rules)}:raise ValueError
    return Rules(**{key:(D(value) if isinstance(value,str) and key not in ('fee_source','fee_symbol') else value)
        for key,value in values.items()})

def source_signal(db,operation,symbol,*,normalized):
    source=db.execute("SELECT output FROM api_requests WHERE operation=? AND state='COMPLETE'",(operation,)).fetchone()
    if not source or not source[0]:raise ValueError
    return setup(json.loads(source[0],parse_float=D),symbol,require_declared_rr=normalized)

def normalized_source(plan,signal):
    """Keep the original provider TP separate from the worker's execution TP."""
    if signal.side not in ('LONG','SHORT') or signal.side!=plan['side']:raise ValueError
    original=plan['tp_normalization']
    for field,value in [('entry',signal.entry),('sl',signal.sl)]:
        if number(plan[field])!=value or number(original['model_'+field])!=value:raise ValueError
    if number(original['model_tp'])!=signal.tp:raise ValueError

def stamp(db,plan,operation):
    try:
        symbol=plan['symbol']
        validated_protection_working_type(plan.get('protection_working_type','MARK_PRICE'))
        normalized='risk_model' in plan
        if normalized:
            from .core import validate_normalized_plan
            validate_normalized_plan(plan,sizing_rules(plan),check_fresh=False)
        elif 'reward_risk_policy' in plan:
            validate_reward_risk_policy(plan,sizing_rules(plan))
        if plan.get('mode')!='ORDER_INTENT' or not isinstance(symbol,str) or not re.fullmatch(r'[A-Z0-9]{2,18}USDT',symbol):raise ValueError
        if not isinstance(operation,str) or not re.fullmatch(OPERATION+re.escape(symbol),operation):raise ValueError
        previous=plan.get('provenance')
        if previous is not None and (not isinstance(previous,dict) or previous.get('analysis_version')!=VERSION):raise ValueError
        sizing_version=SIZING_V3 if normalized else SIZING
        if previous is not None and (normalized or previous.get('execution_sizing_version')==SIZING_V3) and previous.get('execution_sizing_version')!=sizing_version:raise ValueError
        if normalized:normalized_source(plan,source_signal(db,operation,symbol,normalized=True))
        proof={'analysis_version':VERSION,'execution_sizing_version':sizing_version,
            'source_type':'NEUROAPI_STRUCTURED','contract_version':'neuroapi-decision-v2',
            'operation':operation,'risk_target_usdt':plan['risk_target_usdt'],
            'payload_sha256':digest({k:v for k,v in plan.items() if k!='provenance'}),
            'request_output_sha256':request_digest(db,operation),
            'request_body_sha256':db.execute('SELECT body_hash FROM api_requests WHERE operation=?',(operation,)).fetchone()[0]}
        if not proof['request_output_sha256'] or not proof['request_body_sha256']:raise ValueError
    except Exception:raise Review('ROBOT_SETUP_UNVERIFIED') from None
    plan['provenance']=proof
    return plan

def verify(db,plan,operation):
    try:
        validated_protection_working_type(plan.get('protection_working_type','MARK_PRICE'))
        proof=plan['provenance'];copy={k:v for k,v in plan.items() if k!='provenance'}
        stamp(db,copy,operation)
        if proof!=copy['provenance'] or not proof['request_output_sha256'] or not proof['request_body_sha256']:raise ValueError
        if not re.fullmatch(OPERATION+re.escape(plan['symbol']),operation):raise ValueError
        normalized='risk_model' in plan
        signal=source_signal(db,operation,plan['symbol'],normalized=normalized)
        if signal.side not in ('LONG','SHORT') or signal.side!=plan['side']:raise ValueError
        if normalized:normalized_source(plan,signal)
        else:
            for field,value in [('entry',signal.entry),('tp',signal.tp),('sl',signal.sl)]:
                if number(plan[field])!=value:raise ValueError
        qty=number(plan['execution_quantity'])
        target=validated_risk_target(plan['risk_target_usdt'])
        rules=sizing_rules(plan)
        for v in (rules.step,rules.minimum,rules.maximum,rules.tick):number(v)
        if not rules.min_notional.is_finite() or rules.min_notional<0:raise ValueError
        if normalized:
            from .core import validate_normalized_plan
            validate_normalized_plan(plan,rules,check_fresh=False)
        else:
            validate_reward_risk_policy(plan,rules)
            if qty!=maximum_risk_quantity(signal.entry,signal.sl,rules,target,check_fresh=False):raise ValueError
            costs=risk_costs(signal.entry,signal.tp,signal.sl,qty,rules,symbol=signal.symbol,check_fresh=False)
            if any(plan.get(key)!=value for key,value in costs.items()):raise ValueError
        if qty!=number(plan['quantity']) or not 0<number(plan['risk'])<=target:raise ValueError
        if (plan['mode'],plan['margin_mode'],plan['leverage'],plan['order_type'])!=('ORDER_INTENT','CROSS',75,'LIMIT'):raise ValueError
        verified_rr(plan)
        return True
    except Exception:raise Review('ROBOT_SETUP_UNVERIFIED') from None
