"""Resume one uncertain native analysis from actual NO-SEND observations.

No trading or provider Send lives here. Native recording, current source/market
validation, and the existing cached coordinator path remain authoritative.
"""
from __future__ import annotations
import copy
from datetime import datetime
import hashlib
import json
import math
import re
import time
from zoneinfo import ZoneInfo
from .core import Review
from .neuroapi import SETUP_SCHEMA_V4, setup, canonical_output

TERMINAL = 'RECEIVED_STALE_NOT_EXECUTED'
REASON = 'ANALYSIS_RECEIVED_MATERIAL_STALE'
KIND = 'NATIVE_RECEIVED_ANALYSIS_OBSERVATION_V2'
NATIVE_SOURCE = '30ea13cd51706b1db65620cdfb5f8db6dec1c9b3b8a20c9b7ca1d6c9db07f72e'
SCHEMA = '''CREATE TABLE IF NOT EXISTS analysis_response_recoveries(
 operation TEXT PRIMARY KEY,request_sha256 TEXT NOT NULL,proof_sha256 TEXT NOT NULL,
 proof TEXT NOT NULL,preimage TEXT NOT NULL,phase TEXT NOT NULL,created REAL NOT NULL,
 updated REAL NOT NULL);'''


def wire(value, *, ascii=False):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=ascii, allow_nan=False)


def sha(value): return hashlib.sha256(value.encode()).hexdigest()
def digest(value): return sha(wire(value, ascii=True))
def finite(value): return type(value) in (int,float) and math.isfinite(value)


def need(value, code='ANALYSIS_RESPONSE_RECOVERY_UNVERIFIED'):
    if not value: raise Review(code)


def _row(db, table, key, value):
    row=db.execute('SELECT * FROM '+table+' WHERE '+key+'=?',(value,)).fetchone()
    return dict(row) if row is not None else None


def _snapshot(db,operation,cycle):
    return {name:_row(db,name,key,value) for name,key,value in (
        ('api_requests','operation',operation),('robot_jobs','operation',operation),
        ('research_browser_requests','operation',operation),('research_browser_responses','operation',operation),
        ('robot_candidates','id',operation),('robot_cycles','id',cycle),('robot_settings','id',1))}


def _entry(entry):
    need(type(entry) is dict and type(entry.get('value')) is dict)
    value=entry['value']; raw=wire(value,ascii=True)
    need(entry.get('sha256')==sha(raw) and entry.get('bytes')==len(raw.encode()))
    return value


def _tiles(proof,index,names,charts):
    rows=[item for item in proof['file_tiles'] if item.get('row')==index]
    need(len(rows)==1)
    tile=rows[0]
    need(tile.get('prompt_sha256')==sha(proof['rows'][index]['text']))
    witnesses=[group for group in tile['file_tile_witnesses'] if group.get('names')==names]
    need(len(witnesses)==1 and len(witnesses[0]['witness'])==4)
    need(all(w.get('name')==name and w.get('ready') is True and w.get('matches')==1
        for w,name in zip(witnesses[0]['witness'],names)))
    images=tile.get('images'); need(type(images) is list and len(images)==2)
    for name,chart in zip(names[:2],charts):
        matches=[image for image in images if image.get('alt')==name]
        need(len(matches)==1 and matches[0].get('visible') is True)
        decoded=matches[0].get('decoded',{})
        need(decoded.get('complete') is True and type(decoded.get('width')) is int
            and type(decoded.get('height')) is int and decoded['width']>0 and decoded['height']>0)
        need(chart['source']=='BINANCE_USDM_FUTURES_WEBSITE_SCREENSHOT'
            and name.endswith('-'+chart['sha256'][:12]+'.png'))


def _names(names,charts,payload):
    symbol=payload['context']['symbol']
    need(type(names) is list and len(names)==4 and len(set(names))==4
        and type(charts) is list and len(charts)==2)
    for index,tf in enumerate(('15m','1h')):
        chart=charts[index]
        need(chart['source']=='BINANCE_USDM_FUTURES_WEBSITE_SCREENSHOT'
            and chart['symbol']==symbol and chart['timeframe']==tf
            and names[index]==payload['marker']+'-'+tf+'-'+chart['sha256'][:12]+'.png'
            and re.fullmatch(re.escape(symbol+'_'+tf+'_200_closed_')+'[a-f0-9]{8}\\.csv',names[index+2]) is not None)


def _material(proof,payload,provider,created):
    """Every posted turn needs its actual matching material, never a preview alone."""
    from .research_provider import validate_chart_evidence
    rows=proof['rows']; receipts=proof['receipt_files']; chain=[]
    for name,entry in receipts.items():
        value=_entry(entry)
        need(value.get('operation')==payload['operation'] and value.get('request_sha256')==payload['request_sha256']
            and value.get('marker')==payload['marker'])
        if name.endswith('.intent.json'):
            chain.append(value)
        else:
            need(name.endswith('.complete.json'))
    chain.sort(key=lambda value:value.get('followup_sequence',1))
    need(len(rows)==2*(len(chain)+1))
    send_checked=created
    if chain:
        initial=chain[0]
        need(initial['initial_context_sha256']==digest(payload['context'])
            and initial['initial_prompt_sha256']==sha(rows[0]['text']))
        charts=initial['initial_chart_evidence'];names=initial['initial_attachment_names']
        _names(names,charts,payload);_tiles(proof,0,names,charts)
    else:
        from agent.native_analysis_material import validate_prepared
        materials=[]
        for entry in proof.get('prepared_materials',[]):
            value=_entry(entry);validate_prepared(value,payload)
            if value['rendered_prompt_sha256']==sha(rows[0]['text']):materials.append(value)
        need(len(materials)==1,'ANALYSIS_INITIAL_MATERIAL_RECEIPT_MISSING')
        value=materials[0]
        need(value['original_request_matches_material'] is True
            and value['context_sha256']==digest(payload['context']))
        charts=value['chart_evidence'];names=value['attachment_names'];checked=value['observed_at'];send_checked=checked
        _names(names,charts,payload);_tiles(proof,0,names,charts)
        validate_chart_evidence(charts,payload['output_schema'],payload['context'],created,checked,provider.chart_max_age)
    previous=None;context_sha=digest(payload['context'])
    for number,intent in enumerate(chain,1):
        index=number*2
        need(intent.get('schema_version')==1 and intent.get('kind')=='NATIVE_200_CHART_FOLLOWUP'
            and intent.get('followup_sequence',1)==number
            and intent.get('previous_followup_intent_sha256')==previous
            and intent['original_payload_sha256']==digest(payload)
            and intent['initial_prompt_sha256']==sha(rows[0]['text'])
            and intent['initial_reply_sha256']==sha(rows[index-1]['text'])
            and intent['followup_prompt_sha256']==sha(rows[index]['text'])
            and intent['conversation_url_sha256']==sha(proof['page_url'])
            and intent['closed_candles_per_timeframe']==200 and intent['closed_candles_total']==400)
        names=intent['followup_attachment_names'];charts=intent['followup_chart_evidence']
        _names(names,charts,payload);_tiles(proof,index,names,charts)
        check=intent['material_send_check'];send_checked=check['send_checked_at']
        need(check['symbol']==payload['context']['symbol'] and check['running_candles_separate'] is True
            and check['closed_candles_per_timeframe']==200 and check['closed_candles_total']==400
            and finite(send_checked) and created<=send_checked<=proof['finished_at']
            and finite(check['data_age_at_send_check_seconds'])
            and 0<=check['data_age_at_send_check_seconds']<=provider.chart_max_age)
        validate_chart_evidence(charts,payload['output_schema'],payload['context'],created,send_checked,provider.chart_max_age)
        previous=digest(intent);context_sha=intent['followup_context_sha256']
    # Completion files may only attest their own existing intent and this answer.
    for name,entry in receipts.items():
        if not name.endswith('.complete.json'):continue
        value=_entry(entry)
        bare={k:v for k,v in value.items() if k not in ('intent_sha256','response_id','final_reply_sha256')}
        need(any(digest(intent)==value.get('intent_sha256')==digest(bare) for intent in chain))
    stale=False
    try:
        validate_chart_evidence(charts,payload['output_schema'],payload['context'],created,proof['finished_at'],provider.chart_max_age)
    except Review as error:
        need(str(error)=='NEUROBRO_CHART_EVIDENCE_INVALID')
        # A metadata mismatch must not masquerade as simple age. Re-run the
        # exact validator at the recorded last Send observation above.
        validate_chart_evidence(charts,payload['output_schema'],payload['context'],created,send_checked,provider.chart_max_age)
        times=[datetime.fromisoformat(row['captured_at'].replace('Z','+00:00')).timestamp() for row in charts]
        need(all(proof['finished_at']-instant>provider.chart_max_age for instant in times))
        stale=True
    return charts,context_sha,stale


def _approval(proof,payload,context_sha,output):
    """A current raw reply cannot invent a missing prior model decision."""
    result_fields={'schema_version','ok','request_id','conversation_id','phase','reply_sha256',
        'authority','trading_authority','decision','needs_followup','reason'}
    final=proof['rows'][-1];reply_sha=sha(final['text'])
    for entry in proof.get('review_receipts',[]):
        value=_entry(entry)
        if value.get('reply_sha256')!=reply_sha:continue
        result=value.get('response'); need(type(result) is dict and set(result)==result_fields)
        request=value.get('request');request_id=value['request_id']
        need(type(request) is dict and request.get('request_id')==request_id
            and request.get('phase')=='ANALYSIS' and request.get('conversation_id')==value['conversation_id']
            and request.get('reply')==final['text'] and request.get('reply_sha256')==reply_sha
            and type(request.get('market_context')) is dict
            and request['market_context'].get('symbol')==payload['context']['symbol'])
        need(value.get('kind')=='NATIVE_ANALYSIS_MINI_REVIEW_V1' and value.get('schema_version')==1
            and value['operation']==payload['operation'] and value['request_sha256']==payload['request_sha256']
            and value['payload_sha256']==digest(payload) and value['context_sha256']==context_sha
            and value['phase']=='ANALYSIS' and value['conversation_id']=='neurobro:'+proof['room']['day']+':'+proof['room']['room_id']
            and value['transcript_sha256']==digest(proof['rows'])
            and request_id==digest(dict(operation=payload['operation'],request_sha256=payload['request_sha256'],
                reply_sha256=reply_sha,phase='ANALYSIS',transcript_sha256=value['transcript_sha256']))
            and finite(value['observed_at']) and value['observed_at']<=proof['finished_at'])
        need(type(result['schema_version']) is int and result['schema_version']==1 and result['ok'] is True and result['request_id']==request_id
            and result['conversation_id']==value['conversation_id'] and result['phase']=='ANALYSIS'
            and result['reply_sha256']==reply_sha and result['authority']=='DIALOGUE_CONTROL_ONLY'
            and result['trading_authority'] is False and result['needs_followup'] is False
            and result['reason']=='DIALOGUE_DECISION'
            and type(result['decision']) is dict and set(result['decision'])=={'action','symbols','question'}
            and result['decision']['action']==('HOLD' if output['side']=='HOLD' else 'COMPLETE')
            and result['decision']['question']==''
            and result['decision']['symbols'] in ([],[payload['context']['symbol']]))
        return True
    return False


def validate_observation(provider,envelope,payload,created,*,now,retained=False,repair=False):
    from agent.neurobro_reply import parse_reply_for_payload, ReplyIncomplete, _TECHNICAL
    need(type(envelope) is dict and envelope.get('kind')==KIND and envelope.get('operation_completed') is False
        and envelope.get('operation')==payload['operation'] and envelope.get('request_sha256')==payload['request_sha256'])
    proof=envelope.get('observation');raw=wire(proof)
    need(envelope.get('observation_sha256')==sha(raw) and envelope.get('observation_bytes')==len(raw.encode())
        and type(envelope.get('capture_name')) is str and type(envelope.get('capture_sha256')) is str
        and re.fullmatch(r'received-analysis-observation-[a-f0-9]{16}-'+envelope['capture_sha256']+r'\.json',envelope['capture_name'])
        and type(envelope.get('capture_bytes')) is int and 0<envelope['capture_bytes']<=8*1024*1024)
    need(type(proof) is dict and proof.get('schema_version')==2 and proof.get('kind')==KIND
        and proof.get('operation')==payload['operation'] and proof.get('request_sha256')==payload['request_sha256']
        and proof.get('payload_sha256')==digest(payload) and proof.get('native_source_sha256')==NATIVE_SOURCE
        and proof.get('operation_completed') is False
        and all(proof.get(key)==0 for key in ('orders_sent','model_calls','provider_messages','uploads')))
    need(proof.get('status')=='CONNECTED','NEUROBRO_'+str(proof.get('status','UNAVAILABLE')))
    need(proof.get('stage')=='OBSERVED_WITHOUT_COMPLETION' and proof.get('errors')==[]
        and finite(proof.get('observed_at')) and finite(proof.get('finished_at'))
        and created<=proof['observed_at']<=proof['finished_at']<=now
        and (retained or now-proof['finished_at']<=120))
    scope=proof.get('operation_scope',{})
    need(scope.get('verified') is True and scope.get('unmatched_intents')==[]
        and scope.get('reply_complete') is True and proof.get('stable_snapshot_reads')==4,
        'ANALYSIS_REPLY_NOT_COMPLETE')
    room=proof['room'];witness=proof['room_witness'];rows=proof['rows'];cache=proof['rendered_prompt_receipt']
    need(room['day']==payload['operation'][:10] and re.fullmatch('[a-f0-9]{32}',room['room_id'])
        and room['url']==proof['page_url'] and room['url'].startswith('https://app.neurobro.ai/')
        and witness['native_room_matches'] is True and witness['same_loaded_room_verified'] is True
        and witness['initial_analysis_user_rows']==[0] and type(rows) is list and len(rows)>=2
        and len(rows)%2==0 and witness['followup_user_rows']==list(range(2,len(rows),2)))
    need(cache['operation']==payload['operation'] and cache['payload_sha256']==proof['payload_sha256']
        and cache['rendered_sha256']==sha(cache['rendered_prompt'])==sha(rows[0]['text'])
        and cache['rendered_prompt'].count(payload['marker'])==1)
    for index,row in enumerate(rows):
        need(row.get('role')==('user' if index%2==0 else 'assistant')
            and type(row.get('text')) is str and row['text'])
        if index%2:
            need(row.get('complete') is True and type(row.get('id')) is str
                and re.fullmatch('[A-Za-z0-9_.:-]{1,160}',row['id']), 'ANALYSIS_REPLY_NOT_COMPLETE')
    charts,context_sha,stale=_material(proof,payload,provider,created)
    reviewed=copy.deepcopy(proof)
    for item in reviewed['file_tiles']:
        index=item['row']
        namesets=[group['names'] for group in item['file_tile_witnesses']
            if len(group.get('names',[]))==4 and len(group.get('witness',[]))==4
            and all(w.get('name')==name and w.get('ready') is True and w.get('matches')==1
                for w,name in zip(group['witness'],group['names']))
            and {image.get('alt') for image in item['images']}==set(group['names'][:2])
            and all(image.get('visible') is True and image.get('decoded',{}).get('complete') is True
                for image in item['images'])]
        unique={tuple(names) for names in namesets}
        if len(unique)==1:reviewed['rows'][index]['attachments']=list(next(iter(unique)))
    # Apply the parser to the ENTIRE current authored response as well as the
    # selected coin. A technical complaint cannot disappear behind its footer.
    technical=_TECHNICAL.search(rows[-1]['text']) is not None
    output=None;reason='NEUROBRO_INPUT_UNREADABLE' if technical else None
    if not technical:
        try:output=parse_reply_for_payload(rows[-1]['text'],payload)
        except ReplyIncomplete as error:reason=str(error)
    if repair:
        # This is permission to continue the SAME conversation, never a market
        # decision. All posted-turn, source, material and identity checks above
        # remain mandatory even when the final answer needs clarification.
        if reason is None:
            setup(output,payload['context']['symbol'],require_declared_rr=False,require_gross_rr=False)
            if not _approval(reviewed,payload,context_sha,output):reason='ANALYSIS_MINI_APPROVAL_NOT_RETAINED'
        need(reason in {'NEUROBRO_INPUT_UNREADABLE','NEUROBRO_REPLY_INCOMPLETE',
            'NEUROBRO_DECISION_CONFLICT','NEUROBRO_LEVEL_INCOMPLETE','NEUROBRO_LEVEL_AMBIGUOUS',
            'NEUROBRO_SETUP_CONDITIONAL','NEUROBRO_FOREIGN_COIN','NEUROBRO_COIN_UNVERIFIED',
            'ANALYSIS_MINI_APPROVAL_NOT_RETAINED'},'ANALYSIS_CONTINUATION_NOT_REQUIRED')
        return dict(reason=reason,reply_sha256=sha(rows[-1]['text']),
            latest_user_prompt_sha256=sha(rows[-2]['text']),next_followup_sequence=len(rows)//2,
            received_at=proof['finished_at'])
    need(not technical,'NEUROBRO_INPUT_UNREADABLE')
    need(reason is None,reason)
    setup(output,payload['context']['symbol'],require_declared_rr=False,require_gross_rr=False)
    approved=_approval(reviewed,payload,context_sha,output)
    need(stale or approved,'ANALYSIS_MINI_APPROVAL_NOT_RETAINED')
    result={key:payload[key] for key in ('operation','marker','request_sha256')}
    result.update(output=output,assistant_text=rows[-1]['text'],chart_evidence=charts,
        response_id='dom-'+sha(room['url']+'\n'+payload['marker']+'\n'+rows[-1]['text']))
    return result,proof['finished_at'],stale


def recover_received_analysis(provider,envelope,*,now=None,permitted=lambda:True):
    """Capture immutable preimages, run the native recorder, then CAS one job.

    A crash after native receipt insert reuses only the same sealed observation.
    A fresh accepted reply is queued for the NORMAL cached coordinator pipeline;
    this function never computes sizing, creates an intent, or invokes a gateway.
    """
    from .research_provider import request_body, BROWSER_PROVIDER, BROWSER_CONTRACT
    db=provider.db;current=time.time() if now is None else now
    need(not db.in_transaction and finite(current) and current>0 and permitted() is True)
    operation=envelope.get('operation')
    need(type(operation) is str)
    match=re.fullmatch(r'(\d{4}-\d{2}-\d{2}):robot-v9:(0|[1-9]\d{0,11}):analysis-v9:([A-Z0-9]{2,18}USDT)',operation)
    need(match is not None)
    cycle_id=operation.rsplit(':analysis-v9:',1)[0];symbol=match[3]
    before=_snapshot(db,operation,cycle_id)
    api,job,request,receipt,candidate,cycle,settings=(before[key] for key in (
        'api_requests','robot_jobs','research_browser_requests','research_browser_responses',
        'robot_candidates','robot_cycles','robot_settings'))
    need(all(item is not None for item in (api,job,request,cycle,settings)) and settings['enabled']==1
        and api['attempts']==1 and api['idempotency'] is None
        and job['cycle']==cycle_id and job['kind']=='ANALYSIS' and job['symbol']==symbol
        and cycle['day']==match[1] and cycle['entry_epoch']==int(match[2])
        and cycle['state'] in ('ACTIVE','NEEDS_REVIEW'))
    body=json.loads(request['request_body'])
    rebuilt,serialized,request_hash=request_body(operation,body['prompt'],body['output_schema'],body['context'])
    need(body==rebuilt and serialized==request['request_body'] and body['output_schema']==SETUP_SCHEMA_V4
        and request['request_sha256']==api['body_hash']==request_hash
        and request['provider']==BROWSER_PROVIDER and request['contract_version']==BROWSER_CONTRACT
        and request['marker']==body['marker'] and body['context']['symbol']==symbol)
    payload={**body,'request_sha256':request_hash}
    need(not db.execute('SELECT 1 FROM order_intents WHERE candidate_id=? LIMIT 1',(operation,)).fetchone())
    db.execute(SCHEMA)
    journal=_row(db,'analysis_response_recoveries','operation',operation)
    if journal:
        need(journal['request_sha256']==request_hash and journal['proof_sha256']==sha(journal['proof']))
        if journal['phase'] in ('RESUME_CACHED','CONSUMED','NOEXEC'):
            return journal['phase']
        need(journal['phase']=='PREPARED')
        envelope=json.loads(journal['proof']);before=json.loads(journal['preimage'])
        api,job,request,old_receipt,candidate,cycle,settings=(before[key] for key in (
            'api_requests','robot_jobs','research_browser_requests','research_browser_responses',
            'robot_candidates','robot_cycles','robot_settings'))
        # Re-reading the sealed capture is for crash recovery, not a new UI
        # completion. Keep its actual receive clock; apply today's freshness.
        validate_now=max(envelope['observation']['finished_at'],current)
    else:validate_now=current
    result,received,stale=validate_observation(provider,envelope,payload,api['created'],now=validate_now,retained=bool(journal))
    # A receipt can also become stale during a crash or between scheduler ticks.
    captured=[datetime.fromisoformat(row['captured_at'].replace('Z','+00:00')).timestamp()
        for row in result['chart_evidence']]
    stale=stale or any(current-instant>provider.chart_max_age for instant in captured)
    stale=stale or datetime.fromtimestamp(current,ZoneInfo('Asia/Jakarta')).date().isoformat()!=match[1]
    data=json.loads(cycle['data']);need(type(data.get('queue')) is list and type(data.get('seen')) is list)
    need(job['state'] in ('PENDING','NEEDS_REVIEW') and api['state'] in ('PENDING','NEEDS_REVIEW')
        and api['output'] is None)
    if candidate is None:
        need(job['state']=='PENDING' and data['queue'] and data['queue'][0]==symbol)
    else:
        need(candidate['cycle']==cycle_id and candidate['symbol']==symbol and candidate['status']=='REJECTED'
            and candidate['plan'] is None and symbol not in data['queue'] and symbol in data['seen'])
    if not journal:
        # Full original error, response, queue, and settings are immutable before
        # the native recorder's FIRST write, including failures after Send.
        raw=wire(envelope);preimage=wire(before)
        db.execute('INSERT INTO analysis_response_recoveries VALUES(?,?,?,?,?,?,?,?)',
            (operation,request_hash,sha(raw),raw,preimage,'PREPARED',current,current))
    need(permitted() is True,'RESEARCH_PAUSED')
    native_api=_row(db,'api_requests','operation',operation)
    # Recover a crash after native COMPLETE, using only its identical response.
    if native_api['state']=='COMPLETE':
        need(native_api['output']==canonical_output(result['output'],body['output_schema'])
            and native_api['body_hash']==request_hash)
        native_receipt=_row(db,'research_browser_responses','operation',operation)
        need(native_receipt is not None and native_receipt['assistant_sha256']==sha(result['assistant_text'])
            and native_receipt['response_id']==result['response_id'])
    else:
        try:
            provider._record_browser_response(operation,body,request_hash,result,body['output_schema'],
                lambda value:setup(value,symbol,require_declared_rr=False,require_gross_rr=False),
                body['context'],received_at=received,recovery=True)
        except Review as error:
            # Only a proved age failure is a terminal non-executable reply.
            need(stale and str(error)=='NEUROBRO_CHART_EVIDENCE_INVALID')
    db.execute('BEGIN IMMEDIATE')
    try:
        need(permitted() is True,'RESEARCH_PAUSED')
        for table,key in (('robot_jobs','operation'),('research_browser_requests','operation'),
                ('robot_candidates','id'),('robot_cycles','id')):
            value=cycle_id if table=='robot_cycles' else operation
            need(_row(db,table,key,value)==before[table])
        need(_row(db,'robot_settings','id',1)['enabled']==1
            and not db.execute('SELECT 1 FROM order_intents WHERE candidate_id=?',(operation,)).fetchone())
        native_api=_row(db,'api_requests','operation',operation)
        need(native_api['body_hash']==request_hash and native_api['attempts']==1)
        if stale:
            db.execute("UPDATE api_requests SET state=?,output=NULL,failure_code=? WHERE operation=? AND state IN ('PENDING','NEEDS_REVIEW','COMPLETE')",(TERMINAL,REASON,operation))
            db.execute('UPDATE robot_jobs SET state=? WHERE operation=?',(TERMINAL,operation))
            if candidate is None:
                db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',(operation,cycle_id,symbol,'REJECTED',None,REASON))
                data['queue'].pop(0)
                if symbol not in data['seen']:data['seen'].append(symbol)
            else:
                db.execute('UPDATE robot_candidates SET failure_code=? WHERE id=? AND status=\'REJECTED\' AND plan IS NULL',(REASON,operation))
            phase='NOEXEC'
        else:
            need(native_api['state']=='COMPLETE')
            db.execute("UPDATE robot_jobs SET state='PENDING' WHERE operation=?",(operation,))
            if candidate is not None:
                data['queue'].insert(0,symbol);data['seen'].remove(symbol)
            phase='RESUME_CACHED'
        db.execute("UPDATE robot_cycles SET state='ACTIVE',data=? WHERE id=?",(json.dumps(data),cycle_id))
        db.execute('UPDATE analysis_response_recoveries SET phase=?,updated=? WHERE operation=? AND phase=\'PREPARED\'',(phase,current,operation))
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK');raise
    return phase


def record_analysis_candidate(db,values):
    """Normal insert, or one receipt-bound replacement of its failed skeleton."""
    operation=values[0];old=_row(db,'robot_candidates','id',operation)
    if old is None:
        db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',values)
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_response_recoveries'").fetchone():
            db.execute("UPDATE analysis_response_recoveries SET phase='CONSUMED',updated=? WHERE operation=? AND phase='RESUME_CACHED'",(time.time(),operation))
        return
    need(db.in_transaction)
    try:journal=_row(db,'analysis_response_recoveries','operation',operation)
    except Exception:journal=None
    need(journal is not None and journal['phase']=='RESUME_CACHED'
        and journal['proof_sha256']==sha(journal['proof']))
    before=json.loads(journal['preimage'])
    need(before['robot_candidates']==old and old['status']=='REJECTED' and old['plan'] is None
        and values[1:3]==(old['cycle'],old['symbol'])
        and not db.execute('SELECT 1 FROM order_intents WHERE candidate_id=?',(operation,)).fetchone())
    need(db.execute('UPDATE robot_candidates SET status=?,plan=?,failure_code=? WHERE id=? AND status=? AND plan IS NULL',
        (values[3],values[4],values[5],operation,old['status'])).rowcount==1)
    need(db.execute("UPDATE analysis_response_recoveries SET phase='CONSUMED',updated=? WHERE operation=? AND phase='RESUME_CACHED'",(time.time(),operation)).rowcount==1)


def received_nonexecuted(db,row):
    """Proof-backed nonexecution may seek a replacement; a label alone cannot."""
    if row['status']!='REJECTED' or row['plan'] is not None:return False
    if db.execute('SELECT 1 FROM order_intents WHERE candidate_id=?',(row['id'],)).fetchone():return False
    try:
        api=_row(db,'api_requests','operation',row['id']);job=_row(db,'robot_jobs','operation',row['id'])
        receipt=_row(db,'research_browser_responses','operation',row['id']);request=_row(db,'research_browser_requests','operation',row['id'])
    except Exception:return False # API-only fixtures have no native browser journal.
    if (not all((api,job,receipt,request)) or api['state']!=TERMINAL or job['state']!=TERMINAL
            or api['output'] is not None or api['attempts']!=1 or api['idempotency'] is not None
            or api['body_hash']!=request['request_sha256'] or job['cycle']!=row['cycle']
            or job['symbol']!=row['symbol'] or job['kind']!='ANALYSIS'):return False
    try:
        from .research_provider import request_body,validate_chart_evidence,response_output_text
        from agent.neurobro_reply import parse_reply_for_payload,_TECHNICAL
        body=json.loads(request['request_body']);rebuilt,serialized,wire_hash=request_body(row['id'],body['prompt'],body['output_schema'],body['context'])
        if body!=rebuilt or serialized!=request['request_body'] or wire_hash!=api['body_hash']:return False
        if receipt['assistant_sha256']!=sha(receipt['assistant_text']) or _TECHNICAL.search(receipt['assistant_text']):return False
        output=parse_reply_for_payload(receipt['assistant_text'],body)
        setup(output,row['symbol'],require_declared_rr=False,require_gross_rr=False)
        if receipt['output_sha256']!=sha(canonical_output(output,body['output_schema'])):return False
        charts=json.loads(receipt['chart_evidence'])
        # Existing historical settlements already hold the native actual reply;
        # verify its recorded stale material and canonical request without
        # rerunning the old incident or changing its original journal.
        try:validate_chart_evidence(charts,body['output_schema'],body['context'],api['created'],receipt['received_at'],receipt['chart_max_age'])
        except Review as error:
            if str(error)!='NEUROBRO_CHART_EVIDENCE_INVALID':return False
            return all(receipt['received_at']-datetime.fromisoformat(chart['captured_at'].replace('Z','+00:00')).timestamp()>receipt['chart_max_age'] for chart in charts)
        # A native COMPLETE may become too old during a process interruption.
        # Only its sealed recovery proof permits this non-executable outcome.
        journal=_row(db,'analysis_response_recoveries','operation',row['id'])
        if not journal or journal['phase']!='NOEXEC' or journal['request_sha256']!=api['body_hash'] or journal['proof_sha256']!=sha(journal['proof']):return False
        from types import SimpleNamespace
        proof=json.loads(journal['proof']);result,received,stale=validate_observation(
            SimpleNamespace(chart_max_age=receipt['chart_max_age']),proof,
            {**body,'request_sha256':api['body_hash']},api['created'],now=journal['updated'],retained=True)
        if result['assistant_text']!=receipt['assistant_text'] or result['response_id']!=receipt['response_id']:return False
        return (datetime.fromtimestamp(journal['updated'],ZoneInfo('Asia/Jakarta')).date().isoformat()!=row['cycle'][:10]
            or any(journal['updated']-datetime.fromisoformat(chart['captured_at'].replace('Z','+00:00')).timestamp()>receipt['chart_max_age'] for chart in charts))
    except Exception:return False


def resume_uncertain_analysis(coordinator,today,account):
    """The ordinary scheduler's read-only observation, before its unknown gate."""
    observe=getattr(coordinator.neuro,'observe_received_analysis',None)
    if not callable(observe):return None
    jobs=list(coordinator.db.execute('''SELECT j.* FROM robot_jobs j JOIN api_requests a ON a.operation=j.operation
        WHERE j.kind='ANALYSIS' AND j.state IN ('PENDING','NEEDS_REVIEW')
        AND a.state IN ('PENDING','NEEDS_REVIEW') AND a.attempts=1 AND a.output IS NULL
        AND a.failure_code IN ('NEUROBRO_BROWSER_OUTCOME_UNKNOWN','LOCAL_PROCESSING_FAILED')
        ORDER BY a.created LIMIT 2'''))
    if not jobs:
        # A process may disappear while PENDING, before its error is recorded.
        jobs=list(coordinator.db.execute('''SELECT j.* FROM robot_jobs j JOIN api_requests a ON a.operation=j.operation
            WHERE j.kind='ANALYSIS' AND j.state='PENDING' AND a.state='PENDING'
            AND a.attempts=1 AND a.output IS NULL ORDER BY a.created LIMIT 2'''))
    if coordinator.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_response_recoveries'").fetchone():
        # The native recorder commits before the scheduling CAS. Discover that
        # exact sealed crash gap too; API COMPLETE alone never clears a job.
        prepared=list(coordinator.db.execute('''SELECT j.* FROM robot_jobs j
            JOIN api_requests a ON a.operation=j.operation
            JOIN analysis_response_recoveries r ON r.operation=j.operation
            WHERE j.kind='ANALYSIS' AND j.state IN ('PENDING','NEEDS_REVIEW')
            AND a.state='COMPLETE' AND a.attempts=1 AND r.phase='PREPARED'
            AND r.request_sha256=a.body_hash ORDER BY a.created LIMIT 2'''))
        jobs.extend(job for job in prepared if job['operation'] not in {row['operation'] for row in jobs})
    if len(jobs)!=1:return None
    operation=jobs[0]['operation']
    if not coordinator.allowed(today):return coordinator.store.report('WAITING',account,wait_reason=coordinator.pause_reason())
    try:
        api=_row(coordinator.db,'api_requests','operation',operation)
        if api['state']=='COMPLETE':
            coordinator.neuro.require_key()
            journal=_row(coordinator.db,'analysis_response_recoveries','operation',operation)
            need(journal is not None and journal['phase']=='PREPARED' and journal['proof_sha256']==sha(journal['proof']))
            envelope=json.loads(journal['proof'])
        else:envelope=observe(operation)
        from .analysis_continuation import restore_received_checkpoint,admit,record_return,active_grant,retry_not_sent
        restore_received_checkpoint(coordinator.neuro,envelope,
            permitted=lambda:coordinator.allowed(today))
        def continue_grant(grant):
            try:result=coordinator.neuro.continue_received_analysis(operation,grant)
            except Exception:
                record_return(coordinator.neuro,grant)
                raise Review('ANALYSIS_CONTINUATION_OUTCOME_UNKNOWN') from None
            record_return(coordinator.neuro,grant,result)
            return coordinator.store.report('WAITING',account,wait_reason='ANALYSIS_CONTINUATION_REPLY_RECONCILING')
        active=active_grant(coordinator.db,operation)
        if active is not None and active['state']=='NOT_SENT':
            # Its admitted queue is intentionally retained. Reconcile this
            # positive pre-upload failure before ordinary cached-job handling.
            grant=retry_not_sent(coordinator.neuro,envelope,permitted=lambda:coordinator.allowed(today))
            return continue_grant(grant)
        try:
            phase=recover_received_analysis(coordinator.neuro,envelope,
                permitted=lambda:coordinator.allowed(today))
        except Review as error:
            repairable={'NEUROBRO_INPUT_UNREADABLE','NEUROBRO_REPLY_INCOMPLETE',
                'NEUROBRO_DECISION_CONFLICT','NEUROBRO_LEVEL_INCOMPLETE','NEUROBRO_LEVEL_AMBIGUOUS',
                'NEUROBRO_SETUP_CONDITIONAL','NEUROBRO_FOREIGN_COIN','NEUROBRO_COIN_UNVERIFIED',
                'ANALYSIS_MINI_APPROVAL_NOT_RETAINED'}
            continuation=getattr(coordinator.neuro,'continue_received_analysis',None)
            if str(error) not in repairable or not callable(continuation):raise
            grant=admit(coordinator.neuro,envelope,permitted=lambda:coordinator.allowed(today))
            return continue_grant(grant)
    except Exception as error:
        reason=str(error)
        safe={'NEUROBRO_LOGIN_REQUIRED','NEUROBRO_LOGIN_IN_PROGRESS','NEUROBRO_CHALLENGE_REQUIRED',
            'NEUROBRO_BUSY','NEUROBRO_UNAVAILABLE','NEUROBRO_BROWSER_NOT_CONFIGURED','RESEARCH_PAUSED',
            'ANALYSIS_REPLY_NOT_COMPLETE','ANALYSIS_MINI_APPROVAL_NOT_RETAINED','NEUROBRO_INPUT_UNREADABLE',
            'NEUROBRO_REPLY_INCOMPLETE','NEUROBRO_DECISION_CONFLICT','NEUROBRO_LEVEL_INCOMPLETE',
            'ANALYSIS_INITIAL_MATERIAL_RECEIPT_MISSING','ANALYSIS_RESPONSE_OBSERVATION_PENDING',
            'ANALYSIS_CONTINUATION_OUTCOME_UNKNOWN','ANALYSIS_CONTINUATION_SOURCE_ALREADY_ADMITTED'}
        return coordinator.store.report('WAITING',account,wait_reason=(reason if reason in safe else 'ANALYSIS_RESPONSE_RECOVERY_UNVERIFIED'))
    return coordinator.store.report('WAITING',account,wait_reason=(
        'ANALYSIS_RECEIVED_STALE_REPLACEMENT_PENDING' if phase=='NOEXEC' else 'ANALYSIS_RECEIVED_RESUME_PENDING'))
