"""Observe/review a posted screening in its existing BOUND room, without Send."""
from __future__ import annotations
import copy
import hashlib
import json
import math
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
CONTEXT_RETURN_FAILURE_HOOK='235fb14a9932269beebf55dc669298cbeeef90d35d22e1b6e413e29129b5a007'
CURRENT_RETURN_FAILURE_HOOK='f4b5a212c6d5547dc97abe5f230353b1f744dfcc76704f2ee6b9fb578750b768'
PHASE_JSON_RETURN_FAILURE_HOOK='5f73c0629157ee1a44346cc0e31287997a44a4dafd3f4f00e17ba69316617b47'
TERMINAL_ERROR_RETURN_FAILURE_HOOK='1d516829b20a27229be3a991953b60fb2b78bfe97fa53c4f4c27b4301c7c31ae'
RETURN_FAILURE_SOURCES=frozenset(((RETURN_FAILURE_PRODUCER,RETURN_FAILURE_HOOK),
    (RETURN_FAILURE_PRODUCER,PREVIOUS_RETURN_FAILURE_HOOK),
    (RETURN_FAILURE_PRODUCER,CONTEXT_RETURN_FAILURE_HOOK),
    (RETURN_FAILURE_PRODUCER,CURRENT_RETURN_FAILURE_HOOK),
    (RETURN_FAILURE_PRODUCER,PHASE_JSON_RETURN_FAILURE_HOOK),
    (RETURN_FAILURE_PRODUCER,TERMINAL_ERROR_RETURN_FAILURE_HOOK)))
REVIEW_IDENTITY_KIND='NATIVE_SCREENING_RECEIVED_REVIEW_IDENTITIES_V1'
TERMINAL_ERROR_KIND='NATIVE_SCREENING_TERMINAL_PROVIDER_ERROR_V1'
TERMINAL_BODY_CONTRACT='RENDERED_BODY_TERMINAL_SUFFIX_ID_V1'
TERMINAL_BODY_SUFFIX=(r'\n(?:\n?Tampilkan lebih banyak\n)?(?:[01]?\d|2[0-3])\.[0-5]\d\n'
    r'Neurobro tidak dapat menjawab pertanyaan ini\n(?:\n?Coba lagi\n)?Tanyakan apa saja\n'
    r'(?:\n?Ringan\nPrompt\n)?\n'
    r'Neurobro bisa keliru\. Bukan nasihat keuangan\.\n?')


def need(value,code='SCREENING_RECOVERY_RECEIVED_SCREENING_UNVERIFIED'):
    if not value:raise ValueError(code)


def terminal_body_ui(body,rendered,marker):
    """Recognize the complete observed failure suffix, never quoted error text."""
    from .native_received_analysis import MAX_CAPTURE,text_sha
    if not(type(body)is str and type(rendered)is str and rendered
            and type(marker)is str and 0<len(body.encode())<=MAX_CAPTURE):return None
    body=body.replace('\r\n','\n')
    if body.count(rendered)!=1 or body.count(marker)!=1:return None
    suffix=body.split(rendered,1)[1]
    if re.fullmatch(TERMINAL_BODY_SUFFIX,suffix) is None:return None
    return dict(contract=TERMINAL_BODY_CONTRACT,body_text=body,body_sha256=text_sha(body),
        body_bytes=len(body.encode()),rendered_prompt_occurrences=1,marker_occurrences=1,suffix=suffix)


def read_terminal_body(raw,rendered,marker,*,diagnostics=None):
    """Passive rendered-body read on the existing actor; no Retry or navigation."""
    locator=getattr(raw.page,'locator',None)
    if not callable(locator):return None
    body=locator('body').inner_text(timeout=5000)
    ui=terminal_body_ui(body,rendered,marker)
    if (ui is None and type(diagnostics)is dict and type(body)is str
            and 'Neurobro tidak dapat menjawab pertanyaan ini'in body):
        diagnostics.update(kind='DIAGNOSTIC_ONLY_TERMINAL_UI_UNVERIFIED',body_tail=body[-4000:],
            body_sha256=hashlib.sha256(body.encode()).hexdigest(),body_bytes=len(body.encode()),
            rendered_prompt_occurrences=body.count(rendered),marker_occurrences=body.count(marker))
    return ui


def terminal_ui_authority(ui):
    """A terminal error is not pricing authority for unrelated page widgets."""
    return {key:ui[key]for key in('contract','suffix','rendered_prompt_occurrences','marker_occurrences')}


def valid_terminal_ui(ui,rendered,marker):
    from .native_received_analysis import native_digest
    return bool(type(ui)is dict and type(ui.get('body_bytes'))is int
        and type(ui.get('rendered_prompt_occurrences'))is int and type(ui.get('marker_occurrences'))is int
        and native_digest(terminal_body_ui(ui.get('body_text'),rendered,marker))==native_digest(ui))


def terminal_error_proof(capture,payload,ui):
    """Bind a real posted user and terminal provider failure without an answer."""
    from .native_daily_room import identities
    from .native_received_analysis import native_digest,text_sha
    from .native_screening_recovery import NATIVE_DOM_SHA256
    validate(dict(action=OBSERVE,payload=payload))
    rows=capture.get('rows',[]);scope=capture.get('operation_scope',{});room=capture.get('room',{})
    rendered=capture.get('rendered_prompt_receipt',{}).get('rendered_prompt')
    initial=capture.get('room_witness',{}).get('initial_screening_user_rows')
    need(capture.get('kind')==KIND and capture.get('native_source_sha256')==NATIVE_DOM_SHA256
        and capture.get('operation')==payload['operation']and capture.get('request_sha256')==payload['request_sha256']
        and capture.get('payload_sha256')==native_digest(payload)and capture.get('status')=='CONNECTED'
        and capture.get('errors')==[]and capture.get('operation_completed')is False
        and capture.get('stage')=='OBSERVED_WITHOUT_COMPLETION'and capture.get('stable_snapshot_reads')==4)
    need(rows and initial==[len(rows)-1]and scope.get('initial_user')==scope.get('latest_user')==len(rows)-1
        and scope.get('user_rows')==initial and scope.get('reply_row')is None
        and scope.get('reply_complete')is False and scope.get('verified')is False
        and scope.get('unmatched_intents')==[]and rows[-1].get('role')=='user'
        and rows[-1].get('text')==rendered and rows[-1].get('attachments',[])==[]
        and capture.get('review_receipts')==[]and capture.get('receipt_files')=={}
        and capture.get('file_tiles')==[]and capture.get('prepared_materials')==[]
        and capture.get('room_witness',{}).get('native_room_matches')is True
        and capture.get('room_witness',{}).get('same_loaded_room_verified')is True
        and room.get('day')==payload['operation'][:10]and room.get('url')==capture.get('page_url')
        and type(room.get('room_id'))is str and re.fullmatch('[a-f0-9]{32}',room['room_id'])
        and type(room.get('url'))is str and room['url'].startswith('https://app.neurobro.ai/'))
    need(valid_terminal_ui(ui,rendered,payload['marker']))
    anchors=identities(rows)
    return dict(schema_version=1,kind=TERMINAL_ERROR_KIND,operation=payload['operation'],
        request_sha256=payload['request_sha256'],payload_sha256=native_digest(payload),marker=payload['marker'],
        native_source_sha256=NATIVE_DOM_SHA256,room={key:room[key]for key in('day','room_id','url')},
        rendered_prompt_sha256=text_sha(rendered),user_row=len(rows)-1,row_count=len(rows),
        row_identities=anchors,user_identity_sha256=anchors[-1],rows_identity_sha256=native_digest(anchors),
        prefix_identity_sha256=native_digest(anchors[:-1]),terminal_ui=copy.deepcopy(ui),
        observed_at=capture['observed_at'],stable_snapshot_reads=4)


def _terminal_error_name(day,room_id,anchors):
    from .native_received_analysis import native_digest
    return 'terminal-screening-error-'+native_digest(dict(day=day,room_id=room_id,anchors=anchors))+'.json'


def retain_terminal_error(root,proof,rendered):
    """Keep the first exact terminal observation; it never claims ledger closure."""
    import os
    from .native_turn_batch import _write
    from .native_chart_followup import _directory,_private_file,pairs
    name=_terminal_error_name(proof['room']['day'],proof['room']['room_id'],proof['row_identities'])
    _write(root,name,proof)
    directory=_directory(root)
    try:raw=_private_file(directory,name,8*1024*1024)
    finally:os.close(directory)
    old=json.loads(raw,object_pairs_hook=pairs)
    need(type(old)is dict and {key:value for key,value in old.items()if key not in('observed_at','terminal_ui')}==
        {key:value for key,value in proof.items()if key not in('observed_at','terminal_ui')}
        and valid_terminal_ui(old.get('terminal_ui'),rendered,old.get('marker'))
        and valid_terminal_ui(proof.get('terminal_ui'),rendered,proof.get('marker'))
        and terminal_ui_authority(old['terminal_ui'])==terminal_ui_authority(proof['terminal_ui'])
        and type(old.get('observed_at'))in(int,float)and 0<old['observed_at']<=proof['observed_at'])
    return name


def live_terminal_sample(room,payload,rendered):
    """Sample a posted screening failure; its native result remains UNKNOWN."""
    from .native_daily_room import identities
    from .native_screening_recovery import _native_source_digest,NATIVE_DOM_SHA256
    if room is None or getattr(room,'_connection',None) is None or getattr(room,'_prepared',False) is not True:return None
    validate(dict(action=OBSERVE,payload=payload))
    rows=room.full_turns();room.verify_prefix(rows)
    if (len(rows)!=room.offset+1 or rows[-1].get('role')!='user'
            or rows[-1].get('text')!=rendered or rows[-1].get('attachments',[])!=[]):return None
    selected=room._connection.execute('SELECT room_id,state FROM daily_rooms WHERE day=?',(room.day,)).fetchone()
    need(selected is not None and selected[0]==room.room_id and selected[1]=='SENT'
        and room.day==payload['operation'][:10]and _native_source_digest(room.native)==NATIVE_DOM_SHA256)
    ui=read_terminal_body(room.bridge,rendered,payload['marker'])
    if ui is None:return None
    return dict(operation=payload['operation'],request_sha256=payload['request_sha256'],
        room_id=room.room_id,url=room.bridge.page.url,row_identities=identities(rows),
        terminal_ui=terminal_ui_authority(ui))


def terminal_tail_for_successor(room,rows,payload):
    """Admit only a new native dispatch after an exactly observed failed tail.

    The ordinary worker owns the NOEXEC transition. This browser evidence
    attests the old UI outcome, never that the worker ledger was changed.
    The existing native hook checks the new dispatch before ask and Send.
    """
    import os
    from .native_daily_room import identities
    from .native_chart_followup import _directory,_private_file,pairs
    from .native_received_analysis import native_digest,text_sha
    from .native_screening_recovery import _native_source_digest,NATIVE_DOM_SHA256
    validate(dict(action=OBSERVE,payload=payload))
    anchors=identities(rows);name=_terminal_error_name(room.day,room.room_id,anchors)
    directory=_directory(room.store.root)
    try:
        try:raw=_private_file(directory,name,8*1024*1024)
        except FileNotFoundError:return False
    finally:os.close(directory)
    proof=json.loads(raw,object_pairs_hook=pairs);need(type(proof)is dict)
    old=proof.get('operation','')
    old_match=re.fullmatch(r'(\d{4}-\d{2}-\d{2}:robot-v9:(?:0|[1-9]\d{0,11})):screening:(0|[1-9]\d{0,11}):([12])',old)
    new_match=re.fullmatch(r'(\d{4}-\d{2}-\d{2}:robot-v9:(?:0|[1-9]\d{0,11})):screening:(0|[1-9]\d{0,11}):([12])',payload['operation'])
    fields={'schema_version','kind','operation','request_sha256','payload_sha256','marker','native_source_sha256',
        'room','rendered_prompt_sha256','user_row','row_count','row_identities','user_identity_sha256',
        'rows_identity_sha256','prefix_identity_sha256','terminal_ui','observed_at','stable_snapshot_reads'}
    need(type(proof)is dict and set(proof)==fields
        and all(type(proof.get(key))is int for key in('schema_version','row_count','user_row','stable_snapshot_reads'))
        and type(proof.get('observed_at'))in(int,float)and math.isfinite(proof['observed_at'])
        and 0<proof['observed_at']<=time.time()+5
        and all(type(proof.get(key))is str and re.fullmatch('[a-f0-9]{64}',proof[key])for key in('request_sha256','payload_sha256'))
        and old_match is not None and new_match is not None and old_match[1]==new_match[1]
        and int(new_match[2])==int(old_match[2])+1 and old[:10]==room.day
        and proof.get('kind')==TERMINAL_ERROR_KIND and proof.get('schema_version')==1
        and proof.get('native_source_sha256')==_native_source_digest(room.native)==NATIVE_DOM_SHA256
        and proof.get('room')==dict(day=room.day,room_id=room.room_id,url=room.bridge.page.url)
        and proof.get('row_identities')==anchors and proof.get('row_count')==len(rows)
        and proof.get('user_row')==len(rows)-1 and rows[-1].get('role')=='user'
        and rows[-1].get('attachments',[])==[]and proof.get('user_identity_sha256')==anchors[-1]
        and proof.get('rows_identity_sha256')==native_digest(anchors)
        and proof.get('prefix_identity_sha256')==native_digest(anchors[:-1])
        and proof.get('rendered_prompt_sha256')==text_sha(rows[-1]['text'])
        and proof.get('request_sha256')!=payload['request_sha256']
        and proof.get('marker')=='HARUN_'+text_sha(old)[:32]and proof.get('stable_snapshot_reads')==4)
    current_ui=read_terminal_body(room.bridge,rows[-1]['text'],proof['marker'])
    need(type(current_ui)is dict and valid_terminal_ui(proof.get('terminal_ui'),rows[-1]['text'],proof['marker'])
        and terminal_ui_authority(current_ui)==terminal_ui_authority(proof['terminal_ui']))
    return True


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
