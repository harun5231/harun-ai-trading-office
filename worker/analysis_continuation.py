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
from .analysis_response_recovery import (need,wire,sha,digest,finite,_row,_snapshot,validate_observation)

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


def _observation_semantics(proof):
    """The native reader's stable identities, without browser/file imports."""
    return dict(payload_sha256=proof['payload_sha256'],page_url=proof['page_url'],
        native_source_sha256=proof['native_source_sha256'],
        room={key:proof['room'][key] for key in ('day','room_id','url')},
        rows=[digest({key:value for key,value in row.items() if key!='attachments'}) for row in proof['rows']],
        rendered_prompt_receipt=proof['rendered_prompt_receipt'],
        receipt_files=proof['receipt_files'])


def _known_grant_source(db,row,grant):
    """Find the exact issued capability and its immutable source, including renewal history."""
    sources=[(json.loads(row['grant']),json.loads(row['source_proof']))]
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_continuation_returns'").fetchone():
        for returned in db.execute("SELECT response FROM analysis_continuation_returns WHERE grant_id=? AND state='CLOCK_RENEWED_AFTER_PROVED_NOT_SENT'",(row['grant_id'],)):
            audit=json.loads(returned[0]);sources.extend((audit[g],audit[source])for g,source in
                (('old_grant','old_source_proof'),('new_grant','new_source_proof')))
    need(grant_identity(grant)==row['grant_id'] and grant.get('operation')==row['operation']
        and grant.get('request_sha256')==row['request_sha256'])
    matches=[source for known,source in sources if known==grant]
    need(bool(matches),'ANALYSIS_CONTINUATION_NATIVE_GRANT_UNKNOWN')
    source=matches[-1]
    need(all(source[key]==grant[field]for key,field in(('capture_name','source_capture_name'),
        ('capture_sha256','source_capture_sha256'),('capture_bytes','source_capture_bytes'),
        ('observation_sha256','source_observation_sha256')))
        and sha(wire(source['observation']))==grant['source_observation_sha256'])
    return source


def _native_custody_reference(provider,row,custody):
    admitted=custody.get('admission',{}).get('value',{})
    native_grant=admitted.get('grant');need(type(native_grant)is dict)
    source=_known_grant_source(provider.db,row,native_grant)
    current=json.loads(row['grant'])
    need(all(native_grant[key]==current[key]for key in('grant_id','operation','request_sha256','marker',
        'reply_sha256','latest_user_prompt_sha256','next_followup_sequence')))
    return native_grant,dict(row,source_proof=wire(source))


def _not_sent_for_capability(provider,row,result,now,*,observed=None):
    """A renewed dispatch clock never rewrites an older native closure's clock."""
    current=json.loads(row['grant'])
    if observed is not None:
        custody=observed['observation']['continuation_not_sent']
        native_grant,_=_native_custody_reference(provider,row,custody)
        _not_sent(result,native_grant,now)
        return native_grant
    try:
        _not_sent(result,current,now);return current
    except Review:
        # A direct response to a new dispatch must describe THAT admission.
        # Only retrying an already reconciled NOT_SENT state may read its
        # historical producer reference; UNKNOWN needs fresh observation.
        need(row['state']=='NOT_SENT','ANALYSIS_CONTINUATION_NATIVE_GRANT_UNKNOWN')
        # A historical closure is usable only through the actual audit that
        # reconciled it for THIS capability. A bare old receipt grants nothing.
        if not provider.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_continuation_returns'").fetchone():raise
        for returned in provider.db.execute("SELECT response FROM analysis_continuation_returns WHERE grant_id=? AND state='NATIVE_NOT_SENT_RECEIPT_RECONCILED' ORDER BY sequence DESC",(row['grant_id'],)):
            audit=json.loads(returned[0])
            if audit.get('actual_not_sent')!=result or audit.get('grant_preimage',{}).get('grant')!=row['grant']:continue
            native_grant,_=_native_custody_reference(provider,row,audit['fresh_observation']['observation']['continuation_not_sent'])
            _not_sent(result,native_grant,now);return native_grant
        raise Review('ANALYSIS_CONTINUATION_NATIVE_GRANT_UNKNOWN')from None


def _rebound_not_sent_semantics(row,proof,custody):
    """Independently bind UUID rerendering to the stored grant's exact source."""
    import copy,re
    binding=custody.get('native_id_rebinding')
    need(type(binding)is dict and set(binding)=={'kind','bindings','without_ids_semantics_sha256'}
        and binding['kind']=='NATIVE_RENDERED_MESSAGE_IDS_REBOUND_V1')
    prior=json.loads(row['source_proof'])['observation']
    old=_observation_semantics(prior);new=_observation_semantics(proof)
    need({k:v for k,v in old.items()if k!='rows'}=={k:v for k,v in new.items()if k!='rows'})
    left,right=prior['rows'],proof['rows']
    need(len(left)>=2 and len(left)%2==0 and len(left)==len(right))
    entries=binding['bindings'];need(type(entries)is list and len(entries)==len(left)//2)
    admitted=copy.deepcopy(proof);seen=[set(),set(),set()];changed=False
    for index,(a,b)in enumerate(zip(left,right)):
        clean=lambda value:{k:v for k,v in value.items()if k not in('id','attachments')}
        need(clean(a)==clean(b) and a.get('role')==('assistant'if index%2 else'user')
            and type(a.get('text'))is str and bool(a['text']))
        if not index%2:
            need('id'not in a and 'id'not in b);continue
        need(a.get('complete')is True)
        entry=entries[index//2]
        need(type(entry)is dict and set(entry)=={'row','source_id','admitted_id','current_id'}
            and entry['row']==index and entry['source_id']==a.get('id') and entry['current_id']==b.get('id'))
        ids=[entry[key]for key in('source_id','admitted_id','current_id')]
        for group,identifier in zip(seen,ids):
            need(type(identifier)is str and re.fullmatch('[A-Za-z0-9_.:-]{1,160}',identifier)
                and identifier not in group);group.add(identifier)
        if len(set(ids))>1:
            need(all(re.fullmatch('[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',identifier)for identifier in ids));changed=True
        admitted['rows'][index]['id']=entry['admitted_id']
    normalized=copy.deepcopy(old);normalized['rows']=[digest({k:v for k,v in value.items()if k not in('id','attachments')})for value in left]
    need(changed and binding['without_ids_semantics_sha256']==digest(normalized)
        and custody.get('source_semantics_sha256')==digest(old)
        and custody.get('current_semantics_sha256')==digest(new)
        and custody.get('admitted_capture',{}).get('semantics_sha256')==digest(_observation_semantics(admitted)))
    return digest(new)

import re

def _manual_context_pair(rows,symbol):
    """Only a formatting question and explicit null technical answer: no setup."""
    need(type(rows)is list and len(rows)==2)
    user,answer=rows
    need(user.get('role')=='user' and answer.get('role')=='assistant'
        and user.get('attachments',[])==[] and answer.get('complete')is True
        and type(user.get('text'))is str and 0<len(user['text'])<=256
        and type(answer.get('text'))is str and 0<len(answer['text'])<=4096
        and type(answer.get('id'))is str
        and re.fullmatch('[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',answer['id']))
    words=re.findall(r'[a-z]+',user['text'].lower())
    need('json'in words and 'format'in words and any(word in words for word in('coin','koin'))
        and all(word in {'berikan','tolong','mohon','format','json','coin','koin','nama','arah',
            'entry','tp','sl','keputusan','ringkasan','akhir','dan','dengan','saja','untuk','ini'}for word in words)
        and re.fullmatch(r'[A-Za-z\s,.:/-]+',user['text']))
    text=answer['text'].strip();fenced=re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```',text,re.I)
    if fenced:text=fenced[1].strip()
    def unique(items):
        result={}
        for key,value in items:
            need(type(key)is str and key.lower()not in result);result[key.lower()]=value
        return result
    value=json.loads(text,object_pairs_hook=unique)
    need(type(value)is dict and set(value)=={'coin','keputusan','entry','tp','sl','alasan'}
        and value['coin']==symbol and all(value[key]is None for key in('keputusan','entry','tp','sl'))
        and type(value['alasan'])is str and len(value['alasan'])<=2048
        and re.search(r'\b(?:gambar|chart|screenshot|candle|lampiran)\b',value['alasan'],re.I)
        and re.search(r'\b(?:belum|tidak|kurang|gagal)\b',value['alasan'],re.I))
    return True

def _manual_context_digest(rows):
    return digest([{k:v for k,v in row.items()if k not in('id','attachments')}for row in rows])


def validate_manual_context_candidate(candidate,proof,payload):
    need(type(candidate)is dict and set(candidate)=={'rows','after_owned_pairs',
        'after_owned_user_prompt_sha256','after_owned_reply_sha256','owned_prefix_sha256','semantic_sha256'})
    _manual_context_pair(candidate['rows'],payload['context']['symbol'])
    count=candidate['after_owned_pairs'];rows=proof['rows']
    need(type(count)is int and 1<=count<=len(rows)//2
        and candidate['semantic_sha256']==_manual_context_digest(candidate['rows'])
        and candidate['owned_prefix_sha256']==_manual_context_digest(rows[:count*2])
        and candidate['after_owned_user_prompt_sha256']==sha(rows[count*2-2]['text'])
        and candidate['after_owned_reply_sha256']==sha(rows[count*2-1]['text']))
    return candidate


def validate_manual_context_receipts(provider,proof,payload):
    """Independently verify context against a worker-issued source grant.

    The manual pair has no prices or direction and is never a provider result.
    It authorizes neither a Send nor trading. Every real new followup still
    needs its existing intent, fresh charts, native receipt and Mini review.
    """
    entries=proof.get('manual_context_receipts',[])
    observations=proof.get('manual_context_observations',[])
    need(type(entries)is list and type(observations)is list and len(entries)==len(observations))
    if not entries:return []
    need(provider.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_continuation_grants'").fetchone())
    descriptors=[]
    for entry in entries:
        need(type(entry)is dict and set(entry)=={'name','sha256','bytes','value'})
        value=entry['value'];need(type(value)is dict)
        raw=wire(value);need(entry['sha256']==sha(raw) and entry['bytes']==len(raw.encode()))
        grant=value.get('grant');need(type(grant)is dict and grant_identity(grant)==grant.get('grant_id'))
        need(value.get('schema_version')==1 and value.get('kind')=='NATIVE_OBSERVED_MANUAL_CONTEXT_V1'
            and value.get('authority')=='CONTEXT_ONLY_NO_SEND_NO_APPROVAL'
            and all(value.get(k)==payload[k]for k in('operation','request_sha256','marker'))
            and value.get('payload_sha256')==digest(payload))
        row=_row(provider.db,'analysis_continuation_grants','grant_id',grant['grant_id'])
        need(row is not None and row['operation']==payload['operation']and row['request_sha256']==payload['request_sha256'])
        source=_known_grant_source(provider.db,row,grant)
        original=source['observation'];candidate=validate_manual_context_candidate(original.get('manual_context_tail'),original,payload)
        need(value.get('context')==candidate and finite(value.get('observed_at'))
            and grant['started_at']<=value['observed_at']<=proof['finished_at']
            and value.get('room')=={k:original['room'][k]for k in('day','room_id','url')}
            and value['room']=={k:proof['room'][k]for k in('day','room_id','url')})
        positive=original.get('continuation_not_sent');need(type(positive)is dict)
        expected={k:positive[k]for k in('grant_id','latest_attempt','admission','disposition','mutation_markers')}
        need(value.get('positive_not_sent')==expected and expected['grant_id']==grant['grant_id']
            and expected['mutation_markers']==dict(upload_started=False,send_attempted=False,completed=False))
        closed=expected['disposition']['value']
        need(closed.get('kind')=='NATIVE_ANALYSIS_CONTINUATION_NOT_SENT_V1'
            and all(closed.get(k)is False for k in('send_attempted','new_intent_written','upload_started')))
        _manual_context_pair(value['observed_rows'],payload['context']['symbol'])
        need(_manual_context_digest(value['observed_rows'])==candidate['semantic_sha256'])
        scope=digest({k:payload[k]for k in('operation','request_sha256')})
        identity=digest({k:candidate[k]for k in('after_owned_pairs','after_owned_user_prompt_sha256',
            'after_owned_reply_sha256','owned_prefix_sha256','semantic_sha256')})
        need(entry['name']=='observed-manual-context-'+scope+'-'+identity+'.json')
        seen=[item for item in observations if item.get('receipt_sha256')==entry['sha256']]
        need(len(seen)==1 and seen[0].get('after_owned_pairs')==candidate['after_owned_pairs'])
        _manual_context_pair(seen[0]['rows'],payload['context']['symbol'])
        need(_manual_context_digest(seen[0]['rows'])==candidate['semantic_sha256'])
        validate_manual_context_candidate(candidate,proof,payload)
        descriptors.append(dict(after_owned_pairs=candidate['after_owned_pairs'],
            descriptor={k:entry[k]for k in('name','sha256','bytes')}))
    need(len({item['descriptor']['name']for item in descriptors})==len(descriptors))
    return descriptors


def _lost_not_sent(provider,row,envelope,now):
    """An actual latest native closure can recover a lost HTTP disposition."""
    import re
    grant=json.loads(row['grant']);payload,api=_body(provider,row['operation'])
    basis=validate_observation(provider,envelope,payload,api['created'],now=now,repair=True)
    need(all(basis[key]==grant[key] for key in ('reply_sha256','latest_user_prompt_sha256','next_followup_sequence')),
        'ANALYSIS_CONTINUATION_SOURCE_CHANGED')
    proof=envelope['observation'];custody=proof.get('continuation_not_sent')
    need(type(custody) is dict and custody.get('kind')=='NATIVE_ANALYSIS_CONTINUATION_NOT_SENT_CUSTODY_V1'
        and custody.get('grant_id')==grant['grant_id']
        and type(custody.get('latest_attempt')) is int and custody['latest_attempt']>=1)
    native_grant,native_reference=_native_custody_reference(provider,row,custody)
    if native_grant!=grant:
        need(proof['observed_at']>=row['updated'] and proof['finished_at']>row['updated'],
            'ANALYSIS_CONTINUATION_CLOSURE_ALREADY_CONSUMED')
    semantics=digest(_observation_semantics(proof))
    if custody.get('native_id_rebinding')is not None:
        need(_rebound_not_sent_semantics(native_reference,proof,custody)==semantics)
    else:
        need(custody.get('source_semantics_sha256')==custody.get('current_semantics_sha256')==semantics)
    need(custody.get('intent_chain_sha256')==digest(proof['receipt_files'])
        and custody.get('mutation_markers')==dict(upload_started=False,send_attempted=False,completed=False))
    admission=custody.get('admission');disposition=custody.get('disposition')
    def file_value(entry):
        need(type(entry) is dict and type(entry.get('value')) is dict)
        raw=wire(entry['value'])
        need(entry.get('sha256')==sha(raw) and entry.get('bytes')==len(raw.encode()))
        return entry['value']
    admitted=file_value(admission);closed=file_value(disposition)
    need(admitted.get('grant')==native_grant and type(admitted.get('attempt')) is int
        and admitted['attempt']==custody['latest_attempt']
        and finite(admitted.get('admitted_at')) and finite(closed.get('closed_at'))
        and native_grant['started_at']<=admitted['admitted_at']<=closed['closed_at']<=proof['finished_at'])
    suffix='' if custody['latest_attempt']==1 else '.'+str(custody['latest_attempt'])
    base='analysis-continuation-'+grant['grant_id']+suffix
    need(admission.get('name')==base+'.admitted.json'
        and disposition.get('name')==base+'.closed-not-sent.json'
        and closed.get('admission')=={key:admission[key] for key in ('name','sha256','bytes')})
    capture=custody.get('admitted_capture');need(type(capture) is dict)
    prefix=sha(wire({key:payload[key] for key in ('operation','request_sha256')}))[:16]
    need(capture.get('name')==admitted.get('current_capture')
        and type(capture.get('sha256')) is str and re.fullmatch('[a-f0-9]{64}',capture['sha256'])
        and capture.get('name')=='received-analysis-observation-'+prefix+'-'+capture['sha256']+'.json'
        and type(capture.get('bytes')) is int and 0<capture['bytes']<=8*1024*1024
        and (custody.get('native_id_rebinding')is not None or capture.get('semantics_sha256')==semantics))
    result={**closed,'disposition':disposition};_not_sent(result,native_grant,now)
    # An earlier closure already consumed for a retry cannot settle a later
    # uncertain native attempt, even if its grant ID and clock did not change.
    if provider.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_continuation_returns'").fetchone():
        for old in provider.db.execute("SELECT response FROM analysis_continuation_returns WHERE grant_id=? AND state='NOT_SENT'",(grant['grant_id'],)):
            previous=json.loads(old[0]) if old[0] is not None else None
            need(type(previous) is dict,'ANALYSIS_CONTINUATION_CLOSURE_ALREADY_CONSUMED')
            if previous.get('disposition',{}).get('sha256')==disposition['sha256']:
                # A fresh actor observation sees the same latest positive
                # closure and no later durable admission. Admission is fsynced
                # before every upload/Send. An older observation cannot settle
                # a subsequent transport failure; UNKNOWN history is retained.
                need(proof['finished_at']>row['updated']
                    and proof['observed_at']>=row['updated'],
                    'ANALYSIS_CONTINUATION_CLOSURE_ALREADY_CONSUMED')
    before=json.loads(row['preimage'])
    need(_row(provider.db,'robot_cycles','id',before['robot_cycles']['id'])==json.loads(row['admitted_cycle'])
        and _row(provider.db,'robot_candidates','id',row['operation'])==before['robot_candidates']
        and _row(provider.db,'api_requests','operation',row['operation'])==dict(before['api_requests'],state='PENDING')
        and _row(provider.db,'robot_jobs','operation',row['operation'])==dict(before['robot_jobs'],state='PENDING')
        and not provider.db.execute('SELECT 1 FROM order_intents WHERE candidate_id=?',(row['operation'],)).fetchone())
    return result


def recover_lost_not_sent(provider,envelope,*,now=None,permitted=lambda:True):
    """NO-SEND reconciliation; no receipt or incomplete latest attempt waits."""
    current=time.time() if now is None else now;row=active_grant(provider.db,envelope.get('operation'))
    if row is None or row['state'] not in ('UNKNOWN','GRANTED'):return False
    if envelope.get('observation',{}).get('continuation_not_sent') is None:return False
    need(permitted() is True,'RESEARCH_PAUSED')
    result=_lost_not_sent(provider,row,envelope,current)
    record_return(provider,json.loads(row['grant']),result,now=current,
        observed=envelope,permitted=permitted)
    return True


def record_return(provider,grant,result=None,*,now=None,observed=None,permitted=lambda:True):
    """Retain every transport outcome; absence is never a safe retry receipt."""
    current=time.time() if now is None else now;db=provider.db
    row=_row(db,'analysis_continuation_grants','grant_id',grant['grant_id'])
    need(row is not None and row['grant']==wire(grant)
        and row['state'] in (('GRANTED','UNKNOWN') if observed is not None else ('GRANTED',)))
    if observed is not None:
        need(permitted() is True,'RESEARCH_PAUSED')
        need(result==_lost_not_sent(provider,row,observed,current))
    state='RETURNED' if result is not None else 'UNKNOWN'
    if type(result) is dict and result.get('kind')=='NATIVE_ANALYSIS_CONTINUATION_NOT_SENT_V1':
        _not_sent_for_capability(provider,row,result,current,observed=observed);state='NOT_SENT'
    db.execute("""CREATE TABLE IF NOT EXISTS analysis_continuation_returns(
        grant_id TEXT NOT NULL,sequence INTEGER NOT NULL,state TEXT NOT NULL,response TEXT,
        recorded_at REAL NOT NULL,PRIMARY KEY(grant_id,sequence));""")
    db.execute('BEGIN IMMEDIATE')
    try:
        need(_row(db,'analysis_continuation_grants','grant_id',grant['grant_id'])==row)
        sequence=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM analysis_continuation_returns WHERE grant_id=?',(grant['grant_id'],)).fetchone()[0]
        if observed is not None:
            need(permitted() is True and _row(db,'robot_settings','id',1)['enabled']==1,'RESEARCH_PAUSED')
            need(result==_lost_not_sent(provider,row,observed,current))
            native_grant,_=_native_custody_reference(provider,row,observed['observation']['continuation_not_sent'])
            audit=dict(grant_preimage=row,actual_not_sent=result,fresh_observation=observed,
                native_admission_grant=native_grant)
            db.execute('INSERT INTO analysis_continuation_returns VALUES(?,?,?,?,?)',
                (grant['grant_id'],sequence,'NATIVE_NOT_SENT_RECEIPT_RECONCILED',wire(audit),current))
            sequence+=1
        db.execute('INSERT INTO analysis_continuation_returns VALUES(?,?,?,?,?)',
            (grant['grant_id'],sequence,state,wire(result) if result is not None else None,current))
        need(db.execute('UPDATE analysis_continuation_grants SET state=?,result=?,updated=? WHERE grant_id=? AND state=?',
            (state,wire(result) if result is not None else None,current,grant['grant_id'],row['state'])).rowcount==1)
        db.execute('COMMIT')
    except BaseException:db.execute('ROLLBACK');raise


def retry_not_sent(provider,envelope,*,now=None,permitted=lambda:True):
    """A positive closed pre-upload failure may retry the same stable grant."""
    current=time.time() if now is None else now;db=provider.db;operation=envelope.get('operation')
    row=active_grant(db,operation);need(row is not None and row['state']=='NOT_SENT')
    grant=json.loads(row['grant']);_not_sent_for_capability(provider,row,json.loads(row['result']),current)
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
        rebind=envelope.get('observation',{}).get('continuation_not_sent',{}).get('native_id_rebinding')
        if rebind is not None:
            _,native_reference=_native_custody_reference(provider,row,envelope['observation']['continuation_not_sent'])
            _rebound_not_sent_semantics(native_reference,envelope['observation'],envelope['observation']['continuation_not_sent'])
        candidate=envelope.get('observation',{}).get('manual_context_tail')
        previous_context=json.loads(row['source_proof'])['observation'].get('manual_context_tail')
        if candidate is not None:
            validate_manual_context_candidate(candidate,envelope['observation'],payload)
        context_changed=candidate is not None and candidate!=previous_context
        renew_source=current>=grant['expires_at'] or rebind is not None or context_changed
        if renew_source:
            # A positively closed pre-upload attempt can renew only its technical
            # clock after fresh observation of the exact same received turn.
            # UNKNOWN or any upload/intent/possible Send never reaches this path.
            renewed=dict(grant,started_at=current,expires_at=current+900,
                source_capture_name=envelope['capture_name'],source_capture_sha256=envelope['capture_sha256'],
                source_capture_bytes=envelope['capture_bytes'],source_observation_sha256=envelope['observation_sha256'])
            need(grant_identity(renewed)==grant['grant_id'])
            sequence=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM analysis_continuation_returns WHERE grant_id=?',(grant['grant_id'],)).fetchone()[0]
            audit=dict(old_grant=grant,new_grant=renewed,old_source_proof=json.loads(row['source_proof']),
                new_source_proof=envelope,positive_not_sent=json.loads(row['result']),preimage=row,
                source_rebound_after_not_sent=rebind is not None,manual_context_observed=context_changed)
            db.execute('INSERT INTO analysis_continuation_returns VALUES(?,?,?,?,?)',
                (grant['grant_id'],sequence,'CLOCK_RENEWED_AFTER_PROVED_NOT_SENT',wire(audit),current))
            grant=renewed
        need(db.execute("UPDATE analysis_continuation_grants SET state='GRANTED',grant=?,source_proof=?,updated=? WHERE grant_id=? AND state='NOT_SENT' AND result=?",
            (wire(grant),wire(envelope) if renew_source else row['source_proof'],
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
    accepted_same_turn=False
    try:basis=validate_observation(provider,envelope,payload,api['created'],now=current,repair=True)
    except Review as error:
        if str(error)!='ANALYSIS_CONTINUATION_NOT_REQUIRED':raise
        result,received,stale=validate_observation(provider,envelope,payload,api['created'],now=current)
        proof=envelope['observation'];basis=dict(reply_sha256=sha(proof['rows'][-1]['text']),
            latest_user_prompt_sha256=sha(proof['rows'][-2]['text']),next_followup_sequence=len(proof['rows'])//2)
        # The native hook may finish only its missing Mini review of this
        # already received turn. Accept that without requiring another WEB
        # message, provided actual retained approval and materials are fresh.
        from .owner_source_protocol import chart_windows_current,WEBSITE_SOURCE
        charts=result['chart_evidence']
        latest_material=(chart_windows_current(charts,int(current*1000))
            if all(chart.get('source')==WEBSITE_SOURCE for chart in charts)else
            all(0<=current-datetime.fromisoformat(chart['captured_at'].replace('Z','+00:00')).timestamp()
                <=min(180,provider.chart_max_age)for chart in charts))
        accepted_same_turn=(not stale and all(basis[key]==grant[key] for key in
            ('reply_sha256','latest_user_prompt_sha256','next_followup_sequence'))
            and datetime.fromtimestamp(current,ZoneInfo('Asia/Jakarta')).date().isoformat()==operation[:10]
            and latest_material)
    if row['state']=='NOT_SENT' and not accepted_same_turn and basis['latest_user_prompt_sha256']==grant['latest_user_prompt_sha256']:
        return False
    need(accepted_same_turn or basis['latest_user_prompt_sha256']!=grant['latest_user_prompt_sha256']
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
