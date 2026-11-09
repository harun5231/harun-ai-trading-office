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
RETURN_FAILURE_KIND='NATIVE_SCREENING_ENDED_RETURN_V1'
RETURN_FAILURE_PRODUCER='879e885b0a800b3a41a2de8523a706a4147e2214415e9f2a1e6022c5b7e932d5'
RETURN_FAILURE_HOOK='ca2bccfea0c9f389c066dd15f63b136c60e609a638b80e36ccb1b8bee903784b'
PREVIOUS_RETURN_FAILURE_HOOK='8498214906365ec2081af01a1049f466308cb36498c1dda1f765083afc3d1d49'
CURRENT_RETURN_FAILURE_HOOK='235fb14a9932269beebf55dc669298cbeeef90d35d22e1b6e413e29129b5a007'
RETURN_FAILURE_SOURCES=frozenset(((RETURN_FAILURE_PRODUCER,RETURN_FAILURE_HOOK),
    (RETURN_FAILURE_PRODUCER,PREVIOUS_RETURN_FAILURE_HOOK),
    (RETURN_FAILURE_PRODUCER,CURRENT_RETURN_FAILURE_HOOK)))
REVIEW_IDENTITY_KIND='NATIVE_SCREENING_RECEIVED_REVIEW_IDENTITIES_V1'


def need(value,code='SCREENING_RECOVERY_RECEIVED_SCREENING_UNVERIFIED'):
    if not value:raise ValueError(code)


def read_ended_return(root,payload):
    """Passively retain the first native return; it never claims model success.

    The native producer links this immutable file exclusively on its first
    return failure. Keeping its exact bytes and pinned producer/hook identity
    lets the worker distinguish an ended interpretation from an uncertain Send.
    Only the worker's narrower stale-retirement validator grants any transition.
    """
    import os
    from pathlib import Path
    from .native_chart_followup import _directory,_private_file,pairs
    name='native-return-failure-'+payload['request_sha256']+'.json'
    directory=_directory(root)
    try:
        try:raw=_private_file(directory,name,65536)
        except FileNotFoundError:return None
    finally:os.close(directory)
    value=json.loads(raw,object_pairs_hook=pairs)
    need(type(value)is dict and value.get('schema_version')==1
        and type(value.get('schema_version'))is int
        and value.get('operation')==payload['operation']
        and value.get('request_sha256')==payload['request_sha256'],
        'SCREENING_RETURN_FAILURE_BINDING_UNVERIFIED')
    # These reviewed hooks retain the same no-further-send phase contract.
    # Refuse every other producer/hook pair; an unknown hook may reorder Send.
    source=Path(__file__).resolve().parent
    producer=hashlib.sha256((source/'native_turn_batch.py').read_bytes()).hexdigest()
    hook=hashlib.sha256((source/'native_browser_hook.py').read_bytes()).hexdigest()
    need((producer,hook)in RETURN_FAILURE_SOURCES,
        'SCREENING_RETURN_FAILURE_PRODUCER_CHANGED')
    canonical=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    need(canonical==raw,'SCREENING_RETURN_FAILURE_BYTES_UNVERIFIED')
    return dict(kind=RETURN_FAILURE_KIND,name=name,sha256=hashlib.sha256(raw).hexdigest(),
        bytes=len(raw),producer_source_sha256=producer,hook_source_sha256=hook,value=value)


def review_identity_rows(root,payload,rows,witness,reviews,cached,record,page_url,observed_at,check):
    """Bind reloaded UUIDs to an existing complete private native observation.

    This witness never rewrites a review or aliases model operation IDs. It
    retains the old rows solely to check an already authored prior CLARIFY;
    current rows, IDs, room and text remain the live observation.
    """
    import os
    from . import native_received_analysis as observer
    from .native_chart_followup import _directory,_private_file,pairs
    initial=witness['initial_screening_user_rows']
    if len(initial)!=1 or witness.get('same_loaded_room_verified')is not True:return None
    current=rows[initial[0]:]
    if len(current)<4 or len(current)%2:return None
    ended=read_ended_return(root,payload)
    if ended is None:return None
    def semantic(row):
        return {key:value for key,value in row.items()if key!='id'}
    directory=_directory(root)
    key=observer.digest({name:payload[name]for name in('operation','request_sha256')})[:16]
    pattern='received-analysis-observation-'+key+'-'
    try:
        names=[]
        for name in os.listdir(directory):
            if not re.fullmatch(re.escape(pattern)+'[a-f0-9]{64}'+r'\.json',name):continue
            info=os.stat(name,dir_fd=directory,follow_symlinks=False)
            names.append((info.st_mtime_ns,name))
        # The earliest complete capture predates a restart. New failed reads
        # cannot push that evidence out of a fixed latest-N window.
        for _,name in sorted(names):
            check();raw=_private_file(directory,name,observer.MAX_CAPTURE)
            need(name==pattern+hashlib.sha256(raw).hexdigest()+'.json',
                'SCREENING_REVIEW_IDENTITY_SOURCE_HASH_CHANGED')
            source=json.loads(raw,object_pairs_hook=pairs)
            if not(type(source)is dict and source.get('kind')==KIND and source.get('schema_version')==2
                    and source.get('operation')==payload['operation']
                    and source.get('request_sha256')==payload['request_sha256']
                    and source.get('payload_sha256')==observer.native_digest(payload)
                    and source.get('native_source_sha256')=='30ea13cd51706b1db65620cdfb5f8db6dec1c9b3b8a20c9b7ca1d6c9db07f72e'
                    and source.get('status')=='CONNECTED'and source.get('errors')==[]
                    and source.get('stage')=='OBSERVED_WITHOUT_COMPLETION'
                    and source.get('operation_completed')is False
                    and type(source.get('finished_at'))in(int,float)
                    and 0<source['finished_at']<=observed_at
                    and source.get('stable_snapshot_reads')==4
                    and source.get('operation_scope',{}).get('verified')is True
                    and source['operation_scope'].get('reply_complete')is True
                    and source['operation_scope'].get('unmatched_intents')==[]
                    and source.get('screening_review_identity')is None):continue
            prior=observer._project_operation(source);old=prior.get('rows',[])
            if (len(old)!=len(current)or any(semantic(left)!=semantic(right)for left,right in zip(old,current))
                    or prior.get('rendered_prompt_receipt')!=cached or prior.get('page_url')!=page_url
                    or any(prior.get('room',{}).get(name)!=record.get(name)for name in('day','room_id','url'))
                    or prior.get('review_receipts')!=reviews):continue
            changed=[index for index,(left,right)in enumerate(zip(old,current))if left!=right]
            need(changed and all(index%2==1 and old[index].get('complete')is True
                and current[index].get('complete')is True and type(current[index].get('id'))is str
                and re.fullmatch('[A-Za-z0-9_.:-]{1,160}',current[index]['id'])for index in changed),
                'SCREENING_REVIEW_IDENTITY_CHANGE_UNVERIFIED')
            projection=observer.canonical(prior)
            return dict(kind=REVIEW_IDENTITY_KIND,source_capture_name=name,
                source_capture_sha256=hashlib.sha256(raw).hexdigest(),source_capture_bytes=len(raw),
                source_observation=prior,source_observation_sha256=hashlib.sha256(projection).hexdigest(),
                source_observation_bytes=len(projection),changed_assistant_rows=changed,
                ended_return_sha256=ended['sha256'])
    finally:os.close(directory)
    return None


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


def operation_scope(rows,witness,reviews,payload,*,identity_rows=None):
    from .native_received_analysis import native_digest,text_sha
    from .native_screening_recovery import clarification
    from .neurobro_reply import parse_reply_for_payload
    initial=witness['initial_screening_user_rows']
    if len(initial)!=1:return dict(verified=False,initial_user=None,latest_user=None,user_rows=[],reply_row=None,reply_complete=False,unmatched_intents=[],reason='INITIAL_PROMPT_NOT_UNIQUE')
    start=initial[0];users=[start];index=start
    while index+2<len(rows):
        prefix=rows[start:index+2];following=rows[index+2]
        review_prefix=identity_rows[:index-start+2]if identity_rows is not None else prefix
        if following.get('role')!='user':break
        questions=[]
        for entry in reviews:
            value=entry['value']
            if value.get('transcript_sha256')!=native_digest(review_prefix):continue
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
