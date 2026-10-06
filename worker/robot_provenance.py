"""Verify current order intents without promoting historical setup evidence."""
import json
import re
from .core import Review,D,number,validated_risk_target,Rules,maximum_risk_quantity,verified_rr
from .provenance import digest,request_digest
from .neuroapi import setup

VERSION='analysis-v8'
SIZING='deterministic-order-risk-v1'

def stamp(db,plan,operation):
    try:
        symbol=plan['symbol']
        if plan.get('mode')!='ORDER_INTENT' or not isinstance(symbol,str) or not re.fullmatch(r'[A-Z0-9]{2,18}USDT',symbol):raise ValueError
        if not isinstance(operation,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}:robot-v8:[0-2]:analysis-v8:'+re.escape(symbol),operation):raise ValueError
        previous=plan.get('provenance')
        if previous is not None and (not isinstance(previous,dict) or previous.get('analysis_version')!=VERSION):raise ValueError
        proof={'analysis_version':VERSION,'execution_sizing_version':SIZING,
            'source_type':'NEUROAPI_STRUCTURED','contract_version':'neuroapi-decision-v1',
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
        proof=plan['provenance'];copy={k:v for k,v in plan.items() if k!='provenance'}
        stamp(db,copy,operation)
        if proof!=copy['provenance'] or not proof['request_output_sha256'] or not proof['request_body_sha256']:raise ValueError
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}:robot-v8:[0-2]:analysis-v8:'+re.escape(plan['symbol']),operation):raise ValueError
        source=db.execute("SELECT output FROM api_requests WHERE operation=? AND state='COMPLETE'",(operation,)).fetchone()
        signal=setup(json.loads(source[0],parse_float=D),plan['symbol'])
        if signal.side not in ('LONG','SHORT') or signal.side!=plan['side']:raise ValueError
        for field,value in [('entry',signal.entry),('tp',signal.tp),('sl',signal.sl)]:
            if number(plan[field])!=value:raise ValueError
        if plan['neurobro_position_size']!=(str(signal.quantity) if signal.quantity is not None else None):raise ValueError
        qty=number(plan['execution_quantity']);risk=qty*abs(signal.entry-signal.sl)
        target=validated_risk_target(plan['risk_target_usdt'])
        fields=plan['sizing_rules']
        rules=Rules(**{k:(D(v) if isinstance(v,str) else v) for k,v in fields.items()})
        for v in (rules.step,rules.minimum,rules.maximum,rules.tick):number(v)
        if not rules.min_notional.is_finite() or rules.min_notional<0:raise ValueError
        if qty!=maximum_risk_quantity(signal.entry,signal.sl,rules,target):raise ValueError
        if qty!=number(plan['quantity']) or risk!=number(plan['risk']) or not 0<risk<=target:raise ValueError
        if (plan['mode'],plan['margin_mode'],plan['leverage'],plan['order_type'])!=('ORDER_INTENT','CROSS',75,'LIMIT'):raise ValueError
        verified_rr(plan)
        return True
    except Exception:raise Review('ROBOT_SETUP_UNVERIFIED') from None
