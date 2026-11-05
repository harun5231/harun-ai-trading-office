"""Receipt-bound continuation of an already received, incomplete WEB analysis.

The grant is not a retry of the original Send, a model decision, or an order
permission. The native actor resumes only its existing followup pipeline.
"""
from __future__ import annotations
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from .core import Review
from .neuroapi import SETUP_SCHEMA_V4
from .analysis_response_recovery import (need,wire,sha,digest,_row,_snapshot,validate_observation)

SCHEMA='''CREATE TABLE IF NOT EXISTS analysis_continuation_grants(
 grant_id TEXT PRIMARY KEY,operation TEXT NOT NULL,request_sha256 TEXT NOT NULL,
 grant TEXT NOT NULL,source_proof TEXT NOT NULL,preimage TEXT NOT NULL,
 admitted_cycle TEXT NOT NULL,state TEXT NOT NULL,result TEXT,
 created REAL NOT NULL,updated REAL NOT NULL);'''


def grant_identity(value):
    return digest({key:value[key] for key in ('operation','request_sha256','reply_sha256',
        'latest_user_prompt_sha256','next_followup_sequence')})


def _body(provider,operation):
    from .research_provider import request_body,BROWSER_PROVIDER,BROWSER_CONTRACT
    request=_row(provider.db,'research_browser_requests','operation',operation)
    api=_row(provider.db,'api_requests','operation',operation)
    need(request is not None and api is not None)
    body=json.loads(request['request_body'])
    rebuilt,serialized,wire_hash=request_body(operation,body['prompt'],body['output_schema'],body['context'])
    need(body==rebuilt and serialized==request['request_body']
        and request['request_sha256']==api['body_hash']==wire_hash
        and request['provider']==BROWSER_PROVIDER and request['contract_version']==BROWSER_CONTRACT
        and request['marker']==body['marker'] and body['output_schema']==SETUP_SCHEMA_V4 and api['attempts']==1 and api['idempotency'] is None)
    return {**body,'request_sha256':wire_hash},api


def active_grant(db,operation):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_continuation_grants'").fetchone():return None
    rows=list(db.execute("SELECT * FROM analysis_continuation_grants WHERE operation=? AND state IN ('GRANTED','UNKNOWN','RETURNED','NOT_SENT')",(operation,)))
    need(len(rows)<=1)
    return dict(rows[0]) if rows else None


def admit(provider,envelope,*,now=None,permitted=lambda:True):
    current=time.time() if now is None else now;db=provider.db;operation=envelope.get('operation')
    need(not db.in_transaction and permitted() is True)
    payload,api=_body(provider,operation)
    basis=validate_observation(provider,envelope,payload,api['created'],now=current,repair=True)
    need(datetime.fromtimestamp(current,ZoneInfo('Asia/Jakarta')).date().isoformat()==operation[:10])
    cycle_id=operation.rsplit(':analysis-v9:',1)[0]
    before=_snapshot(db,operation,cycle_id)
    job=before['robot_jobs'];cycle=before['robot_cycles'];candidate=before['robot_candidates']
    need(before['robot_settings']['enabled']==1 and before['research_browser_responses'] is None
        and api['state'] in ('PENDING','NEEDS_REVIEW') and api['output'] is None
        and job['kind']=='ANALYSIS' and job['cycle']==cycle_id and job['symbol']==payload['context']['symbol']
        and job['state'] in ('PENDING','NEEDS_REVIEW') and cycle['state'] in ('ACTIVE','NEEDS_REVIEW')
        and not db.execute('SELECT 1 FROM order_intents WHERE candidate_id=?',(operation,)).fetchone())
    data=json.loads(cycle['data'])
    if candidate is not None:
        need(candidate['status']=='REJECTED' and candidate['plan'] is None
            and candidate['cycle']==cycle_id and candidate['symbol']==job['symbol']
            and job['symbol'] not in data['queue'] and job['symbol'] in data['seen'])
        data['queue'].insert(0,job['symbol']);data['seen'].remove(job['symbol'])
    else:need(data['queue'] and data['queue'][0]==job['symbol'])
    grant=dict(schema_version=1,operation=operation,request_sha256=payload['request_sha256'],
        marker=payload['marker'],source_capture_name=envelope['capture_name'],
        source_capture_sha256=envelope['capture_sha256'],source_capture_bytes=envelope['capture_bytes'],
        source_observation_sha256=envelope['observation_sha256'],reply_sha256=basis['reply_sha256'],
        latest_user_prompt_sha256=basis['latest_user_prompt_sha256'],next_followup_sequence=basis['next_followup_sequence'],
        started_at=current,expires_at=current+900)
    grant['grant_id']=grant_identity(grant)
    db.execute(SCHEMA)
    need(active_grant(db,operation) is None,'ANALYSIS_CONTINUATION_OUTCOME_UNKNOWN')
    need(_row(db,'analysis_continuation_grants','grant_id',grant['grant_id']) is None,
        'ANALYSIS_CONTINUATION_SOURCE_ALREADY_ADMITTED')
    admitted=dict(cycle,state='ACTIVE',data=json.dumps(data))
    db.execute('BEGIN IMMEDIATE')
    try:
        need(permitted() is True,'RESEARCH_PAUSED')
        need(_snapshot(db,operation,cycle_id)==before)
        db.execute('INSERT INTO analysis_continuation_grants VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (grant['grant_id'],operation,payload['request_sha256'],wire(grant),wire(envelope),wire(before),
             wire(admitted),'GRANTED',None,current,current))
        need(db.execute("UPDATE api_requests SET state='PENDING' WHERE operation=? AND state=? AND output IS NULL",
            (operation,api['state'])).rowcount==1)
        need(db.execute("UPDATE robot_jobs SET state='PENDING' WHERE operation=? AND state=?",
            (operation,job['state'])).rowcount==1)
        need(db.execute("UPDATE robot_cycles SET state='ACTIVE',data=? WHERE id=? AND data=? AND state=?",
            (admitted['data'],cycle_id,cycle['data'],cycle['state'])).rowcount==1)
        db.execute('COMMIT')
    except BaseException:db.execute('ROLLBACK');raise
    return grant


def verify_dispatch_grant(db,operation,request_sha256,today,now):
    """Only a sealed current followup admission extends the old Send deadline."""
    try:
        row=active_grant(db,operation)
        if row is None or row['request_sha256']!=request_sha256:return False
        grant=json.loads(row['grant']);source=json.loads(row['source_proof']);before=json.loads(row['preimage'])
        if (grant_identity(grant)!=grant['grant_id'] or grant['grant_id']!=row['grant_id']
                or grant['operation']!=operation or grant['request_sha256']!=request_sha256
                or operation[:10]!=today or not grant['started_at']<=now<=grant['expires_at']
                or grant['expires_at']-grant['started_at']!=900
                or source['observation_sha256']!=grant['source_observation_sha256']
                or sha(wire(source['observation']))!=grant['source_observation_sha256']
                or before['api_requests']['body_hash']!=request_sha256):return False
        cycle=_row(db,'robot_cycles','id',before['robot_cycles']['id'])
        if cycle!=json.loads(row['admitted_cycle']):return False
        return True
    except Exception:return False


def _not_sent(result,grant,now):
    need(type(result) is dict and result.get('kind')=='NATIVE_ANALYSIS_CONTINUATION_NOT_SENT_V1')
    disposition=result.get('disposition');need(type(disposition) is dict and type(disposition.get('value')) is dict)
    value=disposition['value'];raw=wire(value)
    need(disposition.get('sha256')==sha(raw) and disposition.get('bytes')==len(raw.encode())
        and value=={k:v for k,v in result.items() if k!='disposition'}
        and value.get('grant_id')==grant['grant_id'] and value.get('operation')==grant['operation']
        and value.get('request_sha256')==grant['request_sha256']
        and value.get('send_attempted') is False and value.get('new_intent_written') is False
        and value.get('upload_started') is False
        and grant['started_at']<=value['closed_at']<=now)
    import re
    prefix='analysis-continuation-'+grant['grant_id']
    need(type(disposition.get('name')) is str and re.fullmatch(
        re.escape(prefix)+r'(?:\.(?:[2-9]|[1-9][0-9]+))?\.closed-not-sent\.json',disposition['name']))
    admission=value.get('admission');need(type(admission) is dict and re.fullmatch(
        re.escape(prefix)+r'(?:\.(?:[2-9]|[1-9][0-9]+))?\.admitted\.json',admission.get('name',''))
        and re.fullmatch('[a-f0-9]{64}',admission.get('sha256',''))
        and type(admission.get('bytes')) is int and admission['bytes']>0)
    return True


def record_return(provider,grant,result=None,*,now=None):
    """Retain every transport outcome; absence is never a safe retry receipt."""
    current=time.time() if now is None else now;db=provider.db
    row=_row(db,'analysis_continuation_grants','grant_id',grant['grant_id'])
    need(row is not None and row['grant']==wire(grant) and row['state']=='GRANTED')
    state='RETURNED' if result is not None else 'UNKNOWN'
    if type(result) is dict and result.get('kind')=='NATIVE_ANALYSIS_CONTINUATION_NOT_SENT_V1':
        _not_sent(result,grant,current);state='NOT_SENT'
    db.execute("""CREATE TABLE IF NOT EXISTS analysis_continuation_returns(
        grant_id TEXT NOT NULL,sequence INTEGER NOT NULL,state TEXT NOT NULL,response TEXT,
        recorded_at REAL NOT NULL,PRIMARY KEY(grant_id,sequence));""")
    db.execute('BEGIN IMMEDIATE')
    try:
        need(_row(db,'analysis_continuation_grants','grant_id',grant['grant_id'])==row)
        sequence=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM analysis_continuation_returns WHERE grant_id=?',(grant['grant_id'],)).fetchone()[0]
        db.execute('INSERT INTO analysis_continuation_returns VALUES(?,?,?,?,?)',
            (grant['grant_id'],sequence,state,wire(result) if result is not None else None,current))
        need(db.execute('UPDATE analysis_continuation_grants SET state=?,result=?,updated=? WHERE grant_id=? AND state=\'GRANTED\'',
            (state,wire(result) if result is not None else None,current,grant['grant_id'])).rowcount==1)
        db.execute('COMMIT')
    except BaseException:db.execute('ROLLBACK');raise


def retry_not_sent(provider,envelope,*,now=None,permitted=lambda:True):
    """A positive closed pre-upload failure may retry the same stable grant."""
    current=time.time() if now is None else now;db=provider.db;operation=envelope.get('operation')
    row=active_grant(db,operation);need(row is not None and row['state']=='NOT_SENT')
    grant=json.loads(row['grant']);_not_sent(json.loads(row['result']),grant,current)
    payload,api=_body(provider,operation)
    basis=validate_observation(provider,envelope,payload,api['created'],now=current,repair=True)
    need(all(basis[key]==grant[key] for key in ('reply_sha256','latest_user_prompt_sha256','next_followup_sequence')),
        'ANALYSIS_CONTINUATION_SOURCE_CHANGED')
    need(permitted() is True,'RESEARCH_PAUSED')
    before=json.loads(row['preimage'])
    db.execute('BEGIN IMMEDIATE')
    try:
        need(permitted() is True and _row(db,'robot_settings','id',1)['enabled']==1,'RESEARCH_PAUSED')
        need(_row(db,'robot_cycles','id',before['robot_cycles']['id'])==json.loads(row['admitted_cycle'])
            and _row(db,'robot_candidates','id',operation)==before['robot_candidates']
            and _row(db,'api_requests','operation',operation)==dict(before['api_requests'],state='PENDING')
            and _row(db,'robot_jobs','operation',operation)==dict(before['robot_jobs'],state='PENDING')
            and not db.execute('SELECT 1 FROM order_intents WHERE candidate_id=?',(operation,)).fetchone())
        if current>=grant['expires_at']:
            # A positively closed pre-upload attempt can renew only its technical
            # clock after fresh observation of the exact same received turn.
            # UNKNOWN or any upload/intent/possible Send never reaches this path.
            renewed=dict(grant,started_at=current,expires_at=current+900,
                source_capture_name=envelope['capture_name'],source_capture_sha256=envelope['capture_sha256'],
                source_capture_bytes=envelope['capture_bytes'],source_observation_sha256=envelope['observation_sha256'])
            need(grant_identity(renewed)==grant['grant_id'])
            sequence=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM analysis_continuation_returns WHERE grant_id=?',(grant['grant_id'],)).fetchone()[0]
            audit=dict(old_grant=grant,new_grant=renewed,old_source_proof=json.loads(row['source_proof']),
                new_source_proof=envelope,positive_not_sent=json.loads(row['result']),preimage=row)
            db.execute('INSERT INTO analysis_continuation_returns VALUES(?,?,?,?,?)',
                (grant['grant_id'],sequence,'CLOCK_RENEWED_AFTER_PROVED_NOT_SENT',wire(audit),current))
            grant=renewed
        need(db.execute("UPDATE analysis_continuation_grants SET state='GRANTED',grant=?,source_proof=?,updated=? WHERE grant_id=? AND state='NOT_SENT' AND result=?",
            (wire(grant),wire(envelope) if current>=json.loads(row['grant'])['expires_at'] else row['source_proof'],
             current,grant['grant_id'],row['result'])).rowcount==1)
        db.execute('COMMIT')
    except BaseException:db.execute('ROLLBACK');raise
    return grant


def restore_received_checkpoint(provider,envelope,*,now=None,permitted=lambda:True):
    """After a proven new reply, restore only the blocked scheduling preimage.

    Recovery's existing exact native recorder then consumes that new answer.
    UNKNOWN with no new completed turn is NEVER permission to repeat a Send.
    """
    current=time.time() if now is None else now;db=provider.db;operation=envelope.get('operation')
    row=active_grant(db,operation)
    if row is None:return False
    payload,api=_body(provider,operation);before=json.loads(row['preimage']);grant=json.loads(row['grant'])
    # Same strict native UI/material proof, but no fabricated market decision.
    try:basis=validate_observation(provider,envelope,payload,api['created'],now=current,repair=True)
    except Review as error:
        if str(error)!='ANALYSIS_CONTINUATION_NOT_REQUIRED':raise
        result,received,stale=validate_observation(provider,envelope,payload,api['created'],now=current)
        proof=envelope['observation'];basis=dict(reply_sha256=sha(proof['rows'][-1]['text']),
            latest_user_prompt_sha256=sha(proof['rows'][-2]['text']),next_followup_sequence=len(proof['rows'])//2)
    if row['state']=='NOT_SENT' and basis['latest_user_prompt_sha256']==grant['latest_user_prompt_sha256']:
        return False
    need(basis['latest_user_prompt_sha256']!=grant['latest_user_prompt_sha256']
        and basis['next_followup_sequence']>grant['next_followup_sequence'],
        'ANALYSIS_CONTINUATION_OUTCOME_UNKNOWN')
    need(api['state']=='PENDING' and api['output'] is None and permitted() is True)
    original_cycle=before['robot_cycles'];original_job=before['robot_jobs'];original_api=before['api_requests']
    db.execute('BEGIN IMMEDIATE')
    try:
        need(permitted() is True,'RESEARCH_PAUSED')
        need(_row(db,'robot_cycles','id',original_cycle['id'])==json.loads(row['admitted_cycle'])
            and _row(db,'robot_candidates','id',operation)==before['robot_candidates']
            and not db.execute('SELECT 1 FROM order_intents WHERE candidate_id=?',(operation,)).fetchone())
        current_job=_row(db,'robot_jobs','operation',operation)
        need(current_job==dict(original_job,state='PENDING'))
        current_api=_row(db,'api_requests','operation',operation)
        need(current_api==dict(original_api,state='PENDING'))
        need(_row(db,'robot_settings','id',1)['enabled']==1)
        db.execute('UPDATE api_requests SET state=? WHERE operation=?',(original_api['state'],operation))
        db.execute('UPDATE robot_jobs SET state=? WHERE operation=?',(original_job['state'],operation))
        db.execute('UPDATE robot_cycles SET state=?,data=? WHERE id=?',
            (original_cycle['state'],original_cycle['data'],original_cycle['id']))
        need(db.execute("UPDATE analysis_continuation_grants SET state='OBSERVED_NEW_REPLY',updated=? WHERE grant_id=? AND state=?",
            (current,row['grant_id'],row['state'])).rowcount==1)
        db.execute('COMMIT')
    except BaseException:db.execute('ROLLBACK');raise
    return True
