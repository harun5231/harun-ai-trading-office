"""Observe/review a posted screening in its existing BOUND room, without Send."""
from __future__ import annotations
import copy
import hashlib
import json
import re
import time

OBSERVE='SCREENING_RECEIVED_OBSERVE'
REVIEW='SCREENING_RECEIVED_REVIEW'
KIND='NATIVE_RECEIVED_SCREENING_OBSERVATION_V1'
REVIEW_KIND='NATIVE_RECEIVED_SCREENING_REVIEW_V1'
INCOMPLETE='NATIVE_RECEIVED_SCREENING_REVIEW_INCOMPLETE_V1'


def need(value,code='SCREENING_RECOVERY_RECEIVED_SCREENING_UNVERIFIED'):
    if not value:raise ValueError(code)


def validate(value):
    from .native_screening_recovery import canonical,digest,text_sha,_FIELDS
    fields={'action','payload'}
    need(type(value)is dict and value.get('action')in(OBSERVE,REVIEW))
    if value['action']==REVIEW:fields|={'source_capture_name','source_capture_sha256','source_capture_bytes','source_observation_sha256'}
    need(set(value)==fields);payload=value['payload'];need(type(payload)is dict and set(payload)==_FIELDS)
    match=re.fullmatch(r'(\d{4}-\d{2}-\d{2}):robot-v9:(0|[1-9]\d{0,11}):screening:(0|[1-9]\d{0,11}):([12])',payload['operation'])
    need(match is not None)
    from datetime import date
    date.fromisoformat(match[1]);count=int(match[4]);schema=payload['output_schema']
    need(payload['provider']=='NEUROBRO_WEB' and payload['contract_version']=='neurobro-browser-decision-v2'
        and payload['marker']=='HARUN_'+text_sha(payload['operation'])[:32]
        and type(payload['prompt'])is str and 0<len(payload['prompt'])<=16000
        and type(schema)is dict and schema.get('required')==['symbols'] and schema.get('additionalProperties')is False)
    limits=schema.get('properties',{}).get('symbols',{})
    need(limits.get('type')=='array' and limits.get('minItems')==count and limits.get('maxItems')==count
        and limits.get('uniqueItems')is True and limits.get('items',{}).get('type')=='string'
        and limits['items'].get('pattern')==r'^[A-Z0-9]{2,18}USDT$')
    context=payload['context']
    if int(match[3])==0:need(context is None)
    else:
        need(type(context)is dict and set(context)=={'requested_count','excluded_symbols','replacement_instruction'}
            and type(context['requested_count'])is int and context['requested_count']==count
            and type(context['excluded_symbols'])is list
            and context['excluded_symbols']==sorted(set(context['excluded_symbols']))
            and all(type(symbol)is str and re.fullmatch('[A-Z0-9]{2,18}USDT',symbol)for symbol in context['excluded_symbols'])
            and context['replacement_instruction']=='Choose different Binance USD-M USDT perpetual coins that are not in excluded_symbols.')
    need(payload['request_sha256']==digest({key:item for key,item in payload.items()if key!='request_sha256'})
        and len(canonical(payload))<=393216)
    if value['action']==REVIEW:
        for key in ('source_capture_sha256','source_observation_sha256'):
            need(type(value[key])is str and re.fullmatch('[a-f0-9]{64}',value[key]))
        key=digest({key:payload[key]for key in('operation','request_sha256')})[:16]
        need(value['source_capture_name']=='received-analysis-observation-'+key+'-'+value['source_capture_sha256']+'.json'
            and type(value['source_capture_bytes'])is int and 0<value['source_capture_bytes']<=8*1024*1024)
    return payload


def operation_scope(rows,witness,reviews,payload):
    from .native_received_analysis import native_digest,text_sha
    from .native_screening_recovery import clarification
    from .neurobro_reply import parse_reply_for_payload
    initial=witness['initial_screening_user_rows']
    if len(initial)!=1:return dict(verified=False,initial_user=None,latest_user=None,user_rows=[],reply_row=None,reply_complete=False,unmatched_intents=[],reason='INITIAL_PROMPT_NOT_UNIQUE')
    start=initial[0];users=[start];index=start
    while index+2<len(rows):
        prefix=rows[start:index+2];following=rows[index+2]
        if following.get('role')!='user':break
        questions=[]
        for entry in reviews:
            value=entry['value']
            if value.get('transcript_sha256')!=native_digest(prefix):continue
            response=value.get('response',{})
            if response.get('needs_followup')is True and response.get('decision',{}).get('action')=='CLARIFY':
                questions.append(response['decision'].get('question'))
            elif response.get('needs_followup')is False:
                questions.append(clarification(int(payload['operation'][-1])))
        # Keep the existing deterministic coin-summary recovery contract too.
        # It is independently checked against both actual authored selections.
        if index==start and index+3<len(rows) and following.get('text')==clarification(int(payload['operation'][-1])):
            try:
                if parse_reply_for_payload(rows[index+1]['text'],payload)==parse_reply_for_payload(rows[index+3]['text'],payload):
                    questions.append(following['text'])
            except (ValueError,KeyError):pass
        if following.get('text')not in questions:break
        index+=2;users.append(index)
    reply=index+1 if index+1<len(rows)else None
    complete=reply is not None and rows[reply].get('role')=='assistant' and rows[reply].get('complete')is True
    verified=bool(witness['same_loaded_room_verified'] and reply==len(rows)-1 and complete)
    return dict(verified=verified,initial_user=start,latest_user=index,user_rows=users,reply_row=reply,
        reply_complete=complete,unmatched_intents=[],reason=None if verified else'SCREENING_TRANSCRIPT_UNVERIFIED')


def review_received(actor,delegated,value):
    """Authenticated scheduler authorizes Mini only; WEB Send stays forbidden."""
    from . import native_received_analysis as observer
    from .native_received_continuation import _semantic
    from .native_chart_followup import _directory,_private_file,pairs
    from .native_daily_room import identities
    from .native_analysis_receipt import read_reviews
    import os
    payload=validate(value);observer._gate(actor);room=delegated.daily_room;need(room is not None)
    directory=_directory(room.store.root)
    try:raw_source=_private_file(directory,value['source_capture_name'],observer.MAX_CAPTURE)
    finally:os.close(directory)
    need(len(raw_source)==value['source_capture_bytes'] and hashlib.sha256(raw_source).hexdigest()==value['source_capture_sha256'])
    source=json.loads(raw_source,object_pairs_hook=pairs)
    need(observer.digest(observer._project_operation(source))==value['source_observation_sha256'])
    current_result=observer.observe(actor,delegated,dict(action=OBSERVE,payload=payload))
    directory=_directory(room.store.root)
    try:current=json.loads(_private_file(directory,current_result['capture_name'],observer.MAX_CAPTURE),object_pairs_hook=pairs)
    finally:os.close(directory)
    need(current_result['error_count']==0 and current['status']=='CONNECTED'
        and current['stage']=='OBSERVED_WITHOUT_COMPLETION' and current['stable_snapshot_reads']==4
        and _semantic(source)==_semantic(current) and current['operation_scope']['verified']is True)
    scope=current['operation_scope'];all_rows=current['rows'];rows=copy.deepcopy(all_rows[scope['initial_user']:])
    need(rows and len(rows)%2==0 and all(row.get('attachments',[])==[]for row in rows))
    def readonly_permission():
        observer._gate(actor)
        return delegated.bridge.status()=='CONNECTED' and delegated.bridge.page.url==current['page_url']
    def forbidden():raise ValueError('SCREENING_RECOVERY_REVIEW_ONLY_SEND_FORBIDDEN')
    seed=dict(rows=rows,turn_names=[[]for _ in range(len(rows)//2)],prompt=current['rendered_prompt_receipt']['rendered_prompt'],
        followup_intents=[],completed_intents=[],bound_charts=[],before_upload=forbidden,before_send=forbidden,review_only=True)
    raw=delegated.bridge;old=(raw.operation_deadline,raw._operation_contract)
    deadline=raw.clock()+90
    raw.operation_deadline=deadline;raw._operation_contract=raw._dom_contract
    try:
        if delegated.native_turn_batch is not None:delegated.native_turn_batch.begin(payload)
        with room.operation(payload):
            record=current['room'];selected=room._connection.execute('SELECT room_id,state,binding FROM daily_rooms WHERE day=?',(record['day'],)).fetchone()
            need(selected is not None and selected[0]==record['room_id'] and selected[1]=='BOUND')
            need(identities(room.full_turns())==identities(all_rows))
            room.day=record['day'];room.room_id=record['room_id'];room._binding=json.loads(selected[2])
            room.offset=scope['initial_user'];room.prefix=identities(all_rows[:room.offset]);room._prepared=False
            try:result=delegated._ask(payload,readonly_permission,deadline=deadline,_received=seed)
            except ValueError as error:
                if str(error)!='SCREENING_RECOVERY_RECEIVED_REPLY_INCOMPLETE':raise
                return dict(kind=INCOMPLETE,operation=payload['operation'],request_sha256=payload['request_sha256'],
                    operation_completed=False,review_receipts=read_reviews(room.store.root,payload))
        return dict(kind=REVIEW_KIND,operation=payload['operation'],request_sha256=payload['request_sha256'],
            native_result={**{key:payload[key]for key in('operation','marker','request_sha256')},**result},
            received_at=time.time(),review_receipts=read_reviews(room.store.root,payload))
    finally:raw.operation_deadline,raw._operation_contract=old
