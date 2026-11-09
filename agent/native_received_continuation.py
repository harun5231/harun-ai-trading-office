"""Continue an observed analysis inside its existing native dialogue transaction.

The authenticated worker seals one received-turn grant. The existing actor
reobserves that turn before reusing the normal review/fresh-chart/followup
closures. No initial prompt, upload, room creation or order is replayed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import time

from . import native_received_analysis as observer
from .native_chart_followup import _directory, _private_file, pairs
from .native_daily_room import identities

ACTION = 'ANALYSIS_RECEIVED_CONTINUE'
KIND = 'NATIVE_ANALYSIS_CONTINUATION_RESULT_V1'
NOT_SENT = 'NATIVE_ANALYSIS_CONTINUATION_NOT_SENT_V1'
NOT_SENT_CUSTODY = 'NATIVE_ANALYSIS_CONTINUATION_NOT_SENT_CUSTODY_V1'
GRANT_FIELDS = {'schema_version','grant_id','operation','request_sha256','marker',
    'source_capture_name','source_capture_sha256','source_capture_bytes',
    'source_observation_sha256','reply_sha256','latest_user_prompt_sha256',
    'next_followup_sequence','started_at','expires_at'}


def need(value, reason='SCREENING_RECOVERY_CONTINUATION_UNVERIFIED'):
    if not value: raise ValueError(reason)


def finite(value): return type(value) in (int,float) and math.isfinite(value)
def sha(raw): return hashlib.sha256(raw).hexdigest()


def is_continuation(value):
    return type(value) is dict and value.get('action') == ACTION


def validate(value, *, now=None):
    need(type(value) is dict and set(value)=={'action','payload','grant'} and is_continuation(value))
    payload=observer.validate(dict(action=observer.GENERIC_ACTION,payload=value['payload']))
    grant=value['grant'];need(type(grant) is dict and set(grant)==GRANT_FIELDS)
    need(grant['schema_version']==1 and type(grant['schema_version'])is int
        and all(grant[key]==payload[key]for key in('operation','request_sha256','marker')))
    for key in ('grant_id','source_capture_sha256','source_observation_sha256','reply_sha256','latest_user_prompt_sha256'):
        need(type(grant[key])is str and re.fullmatch('[a-f0-9]{64}',grant[key]))
    sequence=grant['next_followup_sequence']
    need(type(sequence)is int and sequence>=1
        and type(grant['source_capture_bytes'])is int and 0<grant['source_capture_bytes']<=observer.MAX_CAPTURE)
    key=observer.digest({key:payload[key]for key in('operation','request_sha256')})[:16]
    need(grant['source_capture_name']=='received-analysis-observation-'+key+'-'+grant['source_capture_sha256']+'.json')
    basis={key:grant[key]for key in('operation','request_sha256','reply_sha256',
        'latest_user_prompt_sha256','next_followup_sequence')}
    need(grant['grant_id']==observer.native_digest(basis))
    now=time.time()if now is None else now
    need(finite(now) and finite(grant['started_at']) and finite(grant['expires_at'])
        and grant['expires_at']==grant['started_at']+900
        and grant['started_at']<=now<grant['expires_at'], 'SCREENING_RECOVERY_CONTINUATION_EXPIRED')
    return payload,grant


def _read_source(root,payload,grant):
    directory=_directory(root)
    try:raw=_private_file(directory,grant['source_capture_name'],observer.MAX_CAPTURE)
    finally:os.close(directory)
    need(len(raw)==grant['source_capture_bytes'] and sha(raw)==grant['source_capture_sha256'])
    capture=json.loads(raw,object_pairs_hook=pairs)
    need(observer.digest(observer._project_operation(capture))==grant['source_observation_sha256'])
    need(capture.get('operation')==payload['operation'] and capture.get('request_sha256')==payload['request_sha256']
        and capture.get('payload_sha256')==observer.native_digest(payload)
        and capture.get('kind')==observer.GENERIC_KIND and capture.get('schema_version')==2
        and capture.get('stage')=='OBSERVED_WITHOUT_COMPLETION' and capture.get('errors')==[]
        and capture.get('stable_snapshot_reads')==4 and capture.get('status')=='CONNECTED'
        and finite(capture.get('finished_at')) and capture['finished_at']<=grant['started_at'])
    return capture


def _semantic(capture):
    projected=observer._project_operation(capture)
    return dict(payload_sha256=capture['payload_sha256'],page_url=capture['page_url'],
        native_source_sha256=capture['native_source_sha256'],
        room={key:capture['room'][key]for key in('day','room_id','url')},
        # An unrelated history prefix may be partially loaded after restart.
        # Bind every owned native message, not its index in that loaded prefix.
        rows=identities(projected['rows']),
        rendered_prompt_receipt=capture['rendered_prompt_receipt'],receipt_files=capture['receipt_files'])


def _native_id_rebinding(source, admitted, current):
    """Qualify UUID-only rerendering under an actual positive NOT_SENT closure.

    Message IDs may be regenerated when the same room is rendered again. The
    room, exact owned text/roles/completion and immutable receipt chain still
    have to agree; this helper never grants a Send or ignores an altered turn.
    """
    captures=(source,admitted,current)
    semantics=[_semantic(value) for value in captures]
    need(all({k:v for k,v in value.items() if k!='rows'}==
        {k:v for k,v in semantics[0].items() if k!='rows'} for value in semantics))
    rows=[observer._project_operation(value)['rows'] for value in captures]
    need(len(rows[0])>=2 and len(rows[0])%2==0 and all(len(value)==len(rows[0])for value in rows))
    bindings=[];seen=[set(),set(),set()];changed=False
    for index,items in enumerate(zip(*rows)):
        cleaned=[{k:v for k,v in row.items() if k not in ('id','attachments')}for row in items]
        need(cleaned[0]==cleaned[1]==cleaned[2]
            and cleaned[0].get('role')==('assistant'if index%2 else'user')
            and type(cleaned[0].get('text'))is str and bool(cleaned[0]['text']))
        if not index%2:
            need(all('id'not in row for row in items));continue
        need(cleaned[0].get('complete')is True)
        ids=[row.get('id')for row in items]
        for group,identifier in zip(seen,ids):
            need(type(identifier)is str and re.fullmatch('[A-Za-z0-9_.:-]{1,160}',identifier)
                and identifier not in group);group.add(identifier)
        if len(set(ids))>1:
            need(all(re.fullmatch('[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',identifier)for identifier in ids))
            changed=True
        bindings.append(dict(row=index,source_id=ids[0],admitted_id=ids[1],current_id=ids[2]))
    need(changed)
    normalized=copy.deepcopy(semantics[0])
    normalized['rows']=identities([{k:v for k,v in row.items() if k!='id'}for row in rows[0]])
    return dict(kind='NATIVE_RENDERED_MESSAGE_IDS_REBOUND_V1',bindings=bindings,
        without_ids_semantics_sha256=observer.native_digest(normalized))


def read_not_sent(root,payload,capture):
    """Recover only the latest positively closed native attempt, without Send.

    A previous closed attempt cannot certify a later unclosed attempt. Private
    capture/receipt bytes and the whole current owned transcript remain bound;
    this reader never grants dispatch, writes a journal, or renews a clock.
    """
    scope=capture.get('operation_scope',{})
    if (capture.get('stage')!='OBSERVED_WITHOUT_COMPLETION' or capture.get('errors')!=[]
            or capture.get('stable_snapshot_reads')!=4 or capture.get('status')!='CONNECTED'
            or scope.get('verified')is not True or scope.get('reply_complete')is not True
            or scope.get('unmatched_intents')!=[]):return None
    projected=observer._project_operation(capture);rows=projected['rows']
    if (not rows or len(rows)%2 or rows[-1].get('role')!='assistant'
            or rows[-1].get('complete')is not True):return None
    sequence=1+sum(name.endswith('.intent.json')for name in capture['receipt_files'])
    basis=dict(operation=payload['operation'],request_sha256=payload['request_sha256'],
        reply_sha256=sha(rows[-1]['text'].encode()),
        latest_user_prompt_sha256=sha(rows[-2]['text'].encode()),next_followup_sequence=sequence)
    grant_id=observer.native_digest(basis);base='analysis-continuation-'+grant_id
    directory=_directory(root)
    try:
        pattern=re.compile(re.escape(base)+r'(?:\.([2-9]|[1-9][0-9]+))?\.(admitted|closed-not-sent|upload-started|send-attempted|completed)\.json')
        artifacts=[(int(match[1]or 1),match[2],name)for name in os.listdir(directory)
            if(match:=pattern.fullmatch(name))]
        attempts=sorted((number,name)for number,kind,name in artifacts if kind=='admitted')
        if not attempts:return None
        number,admission_name=attempts[-1]
        if any(item[0]>number for item in artifacts):return None
        prefix=base+(''if number==1 else'.'+str(number))
        # Positive NOT_SENT is necessary; absence of a Send marker alone never
        # establishes it. Any mutation/completion marker wins conservatively.
        for suffix in ('upload-started','send-attempted','completed'):
            if any(item[0]==number and item[1]==suffix for item in artifacts):return None
        closed_name=prefix+'.closed-not-sent.json'
        if not any(item[2]==closed_name for item in artifacts):return None
        def entry(name):
            raw=_private_file(directory,name,262144)
            return dict(name=name,sha256=sha(raw),bytes=len(raw),value=json.loads(raw,object_pairs_hook=pairs))
        admission=entry(admission_name);disposition=entry(closed_name)
        admitted=admission['value'];closed=disposition['value'];grant=admitted['grant']
        need(admitted['attempt']==number and grant['grant_id']==grant_id
            and all(grant[key]==value for key,value in basis.items()))
        # Validate the immutable historical record at its recorded admission
        # epoch only. This does not alter production time or authorize Send.
        validate(dict(action=ACTION,payload=payload,grant=grant),now=grant['started_at'])
        need(closed['kind']==NOT_SENT and closed['grant_id']==grant_id
            and closed['operation']==payload['operation'] and closed['request_sha256']==payload['request_sha256']
            and closed['admission']=={key:admission[key]for key in('name','sha256','bytes')}
            and all(closed.get(key)is False for key in('send_attempted','new_intent_written','upload_started'))
            and finite(admitted.get('admitted_at')) and finite(closed.get('closed_at'))
            and grant['started_at']<=admitted['admitted_at']<=closed['closed_at']<=capture['finished_at'])
        source=_read_source(root,payload,grant)
        current_name=admitted['current_capture']
        key=observer.digest({key:payload[key]for key in('operation','request_sha256')})[:16]
        match=re.fullmatch('received-analysis-observation-'+key+'-([a-f0-9]{64})'+r'\.json',current_name)
        need(match is not None)
        current_raw=_private_file(directory,current_name,observer.MAX_CAPTURE)
        need(sha(current_raw)==match[1])
        admitted_capture=json.loads(current_raw,object_pairs_hook=pairs)
        need(admitted_capture.get('stage')=='OBSERVED_WITHOUT_COMPLETION' and admitted_capture.get('errors')==[]
            and admitted_capture.get('stable_snapshot_reads')==4 and admitted_capture.get('status')=='CONNECTED'
            and admitted_capture['operation']==payload['operation']
            and admitted_capture['request_sha256']==payload['request_sha256']
            and admitted_capture['finished_at']<=admitted['admitted_at'])
        source_semantics=_semantic(source);admitted_semantics=_semantic(admitted_capture);current_semantics=_semantic(capture)
        rebinding=None
        if not source_semantics==admitted_semantics==current_semantics:
            rebinding=_native_id_rebinding(source,admitted_capture,capture)
        semantics_sha=observer.native_digest(current_semantics)
        result=dict(kind=NOT_SENT_CUSTODY,grant_id=grant_id,latest_attempt=number,
            admission=admission,disposition=disposition,
            admitted_capture=dict(name=current_name,sha256=sha(current_raw),bytes=len(current_raw),semantics_sha256=observer.native_digest(admitted_semantics)),
            source_semantics_sha256=observer.native_digest(source_semantics),current_semantics_sha256=semantics_sha,
            intent_chain_sha256=observer.native_digest(capture['receipt_files']),
            mutation_markers=dict(upload_started=False,send_attempted=False,completed=False))
        if rebinding is not None:result['native_id_rebinding']=rebinding
        return result
    finally:os.close(directory)


MANUAL_CONTEXT_KIND='NATIVE_OBSERVED_MANUAL_CONTEXT_V1'

def _manual_pair(rows,symbol):
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


def _context_rows_digest(rows):
    return observer.native_digest([{k:v for k,v in row.items()if k not in('id','attachments')}for row in rows])


def manual_context_tail(capture,payload):
    scope=capture.get('operation_scope',{});rows=capture.get('rows',[])
    if scope.get('verified')is not True or scope.get('reply_complete')is not True:return None
    end=scope['reply_row'];tail=copy.deepcopy(rows[end+1:])
    if not tail:return None
    try:_manual_pair(tail,payload['context']['symbol'])
    except (ValueError,KeyError,TypeError):return None
    owned=observer._project_operation(capture)['rows']
    return dict(rows=tail,after_owned_pairs=len(owned)//2,
        after_owned_user_prompt_sha256=sha(owned[-2]['text'].encode()),
        after_owned_reply_sha256=sha(owned[-1]['text'].encode()),
        owned_prefix_sha256=_context_rows_digest(owned),semantic_sha256=_context_rows_digest(tail))


def _context_prefix(payload):
    return 'observed-manual-context-'+observer.native_digest({k:payload[k]for k in('operation','request_sha256')})+'-'


def read_manual_contexts(root,payload):
    prefix=_context_prefix(payload);directory=_directory(root)
    try:
        found=[];total=0
        for name in sorted(os.listdir(directory)):
            if not name.startswith(prefix):continue
            need(re.fullmatch(re.escape(prefix)+r'[a-f0-9]{64}\.json',name))
            raw=_private_file(directory,name,262144);total+=len(raw);need(total<=4*1024*1024)
            value=json.loads(raw,object_pairs_hook=pairs)
            need(value.get('kind')==MANUAL_CONTEXT_KIND and value.get('schema_version')==1
                and all(value.get(k)==payload[k]for k in('operation','request_sha256','marker'))
                and value.get('payload_sha256')==observer.native_digest(payload)
                and type(value.get('context'))is dict)
            context=value['context'];_manual_pair(context['rows'],payload['context']['symbol'])
            need(context['semantic_sha256']==_context_rows_digest(context['rows'])
                and name==prefix+observer.native_digest({k:context[k]for k in('after_owned_pairs',
                    'after_owned_user_prompt_sha256','after_owned_reply_sha256','owned_prefix_sha256','semantic_sha256')})+'.json')
            found.append(dict(name=name,sha256=sha(raw),bytes=len(raw),value=value))
        return found
    finally:os.close(directory)


def locate_manual_contexts(rows,entries,payload):
    """Locate each immutable context pair uniquely; it never becomes a trade turn."""
    found=[];used=set()
    ids=[row.get('id')for row in rows if row.get('role')=='assistant']
    need(len(ids)==len(set(ids)))
    for entry in entries:
        value=entry['value'];context=value['context'];expected=context['rows']
        _manual_pair(expected,payload['context']['symbol'])
        matches=[]
        for index in range(2,len(rows)-1):
            pair=rows[index:index+2]
            if _context_rows_digest(pair)!=context['semantic_sha256']:continue
            try:_manual_pair(pair,payload['context']['symbol'])
            except (ValueError,TypeError):continue
            if (sha(rows[index-2].get('text','').encode())==context['after_owned_user_prompt_sha256']
                    and sha(rows[index-1].get('text','').encode())==context['after_owned_reply_sha256']):matches.append(index)
        need(len(matches)==1 and not used.intersection((matches[0],matches[0]+1)))
        index=matches[0];used.update((index,index+1))
        found.append(dict(receipt=copy.deepcopy(entry),global_row=index,rows=copy.deepcopy(rows[index:index+2])))
    return found


def _persist_manual_context(root,payload,grant,source,current):
    candidate=current.get('manual_context_tail')
    if candidate is None:return
    prior=source.get('manual_context_tail')
    need(type(prior)is dict and candidate['semantic_sha256']==prior['semantic_sha256']
        and all(candidate[k]==prior[k]for k in('after_owned_pairs','after_owned_user_prompt_sha256',
            'after_owned_reply_sha256','owned_prefix_sha256')))
    _manual_pair(candidate['rows'],payload['context']['symbol'])
    custody=current.get('continuation_not_sent');need(type(custody)is dict and custody['grant_id']==grant['grant_id']
        and custody['mutation_markers']==dict(upload_started=False,send_attempted=False,completed=False))
    # Re-read the actual latest admission/closure immediately before writing.
    need(read_not_sent(root,payload,current)==custody)
    name=_context_prefix(payload)+observer.native_digest({k:candidate[k]for k in
        ('after_owned_pairs','after_owned_user_prompt_sha256','after_owned_reply_sha256','owned_prefix_sha256','semantic_sha256')})+'.json'
    directory=_directory(root)
    try:
        try:existing=_private_file(directory,name,262144)
        except FileNotFoundError:existing=None
    finally:os.close(directory)
    if existing is not None:
        value=json.loads(existing,object_pairs_hook=pairs)
        need(value['context']['semantic_sha256']==candidate['semantic_sha256']);return
    value=dict(schema_version=1,kind=MANUAL_CONTEXT_KIND,**{k:payload[k]for k in('operation','request_sha256','marker')},
        payload_sha256=observer.native_digest(payload),room={k:current['room'][k]for k in('day','room_id','url')},
        grant=copy.deepcopy(grant),context=copy.deepcopy(prior),observed_rows=copy.deepcopy(candidate['rows']),
        positive_not_sent={k:copy.deepcopy(custody[k])for k in('grant_id','latest_attempt','admission','disposition','mutation_markers')},
        observed_at=current['finished_at'],authority='CONTEXT_ONLY_NO_SEND_NO_APPROVAL')
    _write(root,name,value)
    from .native_daily_room import retain_prefix_snapshot
    retain_prefix_snapshot(root,current['room']['day'],current['room']['room_id'],current['rows'])


def filter_manual_operation_rows(rows,contexts,*,attachment_projection=False):
    """Recheck the exact physical context rows and UUIDs on every hook read."""
    excluded=set()
    for item in contexts:
        index=item['relative_row'];expected=item['rows'];need(index>=0 and index+1<len(rows))
        need([{k:v for k,v in row.items()if k!='attachments'}for row in rows[index:index+2]]==
            [{k:v for k,v in row.items()if k!='attachments'}for row in expected])
        need(rows[index].get('attachments',[])in([],['UNCONFIRMED'])if attachment_projection
            else rows[index].get('attachments',[])==[])
        excluded.update((index,index+1))
    return [copy.deepcopy(row)for index,row in enumerate(rows)if index not in excluded]


def _seed(capture,payload,grant):
    original=copy.deepcopy(capture['rows']);scope=copy.deepcopy(capture['operation_scope'])
    contexts=locate_manual_contexts(original,capture.get('manual_context_receipts',[]),payload)
    excluded={i for item in contexts for i in(item['global_row'],item['global_row']+1)}
    remap={old:new for new,old in enumerate(i for i in range(len(original))if i not in excluded)}
    rows=copy.deepcopy([row for index,row in enumerate(original)if index not in excluded])
    scope={**scope,**{key:remap[scope[key]]for key in('initial_user','latest_user','reply_row')}}
    need(scope['verified']is True and scope['unmatched_intents']==[] and scope['reply_complete']is True
        and scope['reply_row']==len(rows)-1 and scope['latest_user']==len(rows)-2)
    start=scope['initial_user'];owned=rows[start:]
    need(start>=0 and len(owned)>=2 and len(owned)%2==0
        and sha(owned[-1]['text'].encode())==grant['reply_sha256']
        and sha(owned[-2]['text'].encode())==grant['latest_user_prompt_sha256'])
    need(all(row.get('role')==('user'if index%2==0 else'assistant')
        and type(row.get('text'))is str and row['text']
        and (index%2==0 or row.get('complete')is True and type(row.get('id'))is str
            and re.fullmatch('[A-Za-z0-9_.:-]{1,160}',row['id']))for index,row in enumerate(owned)))
    intents=[];completed=[]
    for name,entry in capture['receipt_files'].items():
        value=entry['value']
        encoded=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
        need(entry['sha256']==sha(encoded) and entry['bytes']==len(encoded))
        if name.endswith('.intent.json'):intents.append(value)
        else:completed.append(value['intent_sha256'])
    intents.sort(key=lambda intent:intent.get('followup_sequence',1))
    need(len(owned)==2*(len(intents)+1) and grant['next_followup_sequence']==len(intents)+1)
    cached=capture['rendered_prompt_receipt']
    need(owned[0]['text']==cached['rendered_prompt']
        and cached['rendered_sha256']==sha(owned[0]['text'].encode()))
    if intents:
        names=[intents[0]['initial_attachment_names']]+[intent['followup_attachment_names']for intent in intents]
        initial_charts=intents[0]['initial_chart_evidence'];bound=intents[-1]['followup_chart_evidence']
    else:
        matches=[entry['value']for entry in capture.get('prepared_materials',[])
            if entry['value'].get('rendered_prompt_sha256')==cached['rendered_sha256']
            and entry['value'].get('original_request_matches_material')is True]
        need(len(matches)==1);names=[matches[0]['attachment_names']]
        initial_charts=bound=matches[0]['chart_evidence']
    previous=None
    for index,intent in enumerate(intents,1):
        need(intent['followup_sequence']==index and intent.get('previous_followup_intent_sha256')==previous
            and intent['original_payload_sha256']==observer.native_digest(payload)
            and intent['initial_prompt_sha256']==sha(owned[0]['text'].encode())
            and intent['initial_reply_sha256']==sha(owned[2*index-1]['text'].encode())
            and intent['followup_prompt_sha256']==sha(owned[2*index]['text'].encode())
            and intent['conversation_url_sha256']==sha(capture['page_url'].encode()))
        previous=observer.native_digest(intent)
    # Names become turn projections only after current positive native UI file
    # witnesses, including actual decoded images, establish those attachments.
    for index,expected in enumerate(names):
        need(type(expected)is list and len(expected)==4 and len(set(expected))==4)
        tiles=[item for item in capture['file_tiles']if remap.get(item['row'])==start+2*index]
        need(len(tiles)==1);tile=tiles[0]
        groups=[item['witness']for item in tile['file_tile_witnesses']if item['names']==expected]
        need(len(groups)==1 and len(groups[0])==4 and all(witness.get('name')==name
            and witness.get('ready')is True and witness.get('matches')==1 for witness,name in zip(groups[0],expected)))
        images=tile['images'];need(len(images)==2)
        for name in expected[:2]:
            found=[image for image in images if image.get('alt')==name]
            need(len(found)==1 and found[0].get('visible')is True)
            decoded=found[0]['decoded'];need(decoded.get('complete')is True
                and type(decoded.get('width'))is int and decoded['width']>0
                and type(decoded.get('height'))is int and decoded['height']>0)
        owned[2*index]['attachments']=copy.deepcopy(expected)
    physical_start=capture['operation_scope']['initial_user']
    bound_contexts=[dict(relative_row=item['global_row']-physical_start,rows=item['rows'],receipt=item['receipt'])for item in contexts]
    return dict(rows=owned,full_rows=original,offset=physical_start,manual_contexts=bound_contexts,record=capture['room'],prompt=cached['rendered_prompt'],
        turn_names=names,followup_intents=intents,completed_intents=completed,bound_charts=bound,
        initial_chart_evidence=initial_charts)


def _write(root,name,value):
    raw=observer.canonical(value);directory=_directory(root)
    try:
        fd=os.open(name,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600,dir_fd=directory)
        with os.fdopen(fd,'wb')as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.fsync(directory)
    finally:os.close(directory)
    return dict(name=name,sha256=sha(raw),bytes=len(raw))


def _admit(root,grant,current_capture):
    """Only a positive closed NOT_SENT receipt permits another attempt."""
    base='analysis-continuation-'+grant['grant_id'];directory=_directory(root)
    try:
        pattern=re.compile(re.escape(base)+r'(?:\.([2-9]|[1-9][0-9]+))?\.admitted\.json')
        attempts=sorted((int(match[1]or 1),name)for name in os.listdir(directory)
            if (match:=pattern.fullmatch(name)))
        previous=None;number=1
        if attempts:
            number,name=attempts[-1]
            previous_raw=_private_file(directory,name,262144)
            previous=json.loads(previous_raw,object_pairs_hook=pairs)
            need(previous['grant']['grant_id']==grant['grant_id']
                and previous['grant']['operation']==grant['operation']
                and previous['grant']['request_sha256']==grant['request_sha256'])
            prior=base+(''if number==1 else'.'+str(number))
            try:closed_raw=_private_file(directory,prior+'.closed-not-sent.json',262144)
            except FileNotFoundError:need(False,'SCREENING_RECOVERY_CONTINUATION_ALREADY_ADMITTED')
            closed=json.loads(closed_raw,object_pairs_hook=pairs)
            need(closed['kind']==NOT_SENT and closed['grant_id']==grant['grant_id']
                and closed['admission']['sha256']==sha(previous_raw)
                and closed['send_attempted']is False and closed['new_intent_written']is False
                and closed['upload_started']is False)
            try:_private_file(directory,prior+'.send-attempted.json',262144)
            except FileNotFoundError:pass
            else:need(False,'SCREENING_RECOVERY_CONTINUATION_ALREADY_ADMITTED')
            previous=sha(closed_raw);number+=1
    finally:os.close(directory)
    name=base+(''if number==1 else'.'+str(number))
    admission=_write(root,name+'.admitted.json',dict(grant=grant,attempt=number,
        previous_not_sent_sha256=previous,current_capture=current_capture,admitted_at=time.time()))
    return name,admission


def poll_received(raw,native,payload,prompt,names,*,until):
    """Native ask's existing post-Send four-stable-read/response-ID contract."""
    previous=None;stable=0
    while raw.clock()<until:
        status=raw.status()
        if status=='UNAVAILABLE':
            previous=None;stable=0;raw.page.wait_for_timeout(250);continue
        if status!='CONNECTED':raise native.OutcomeUnknown()
        answer=raw._answer(prompt,names)
        stable=stable+1 if answer is not None and answer==previous else 0;previous=answer
        if answer is not None and stable>=3:
            return dict(output=native.exact_json(answer,payload['marker']),assistant_text=answer,
                response_id='dom-'+sha((raw.page.url+'\n'+payload['marker']+'\n'+answer).encode()),chart_evidence=[])
        raw.page.wait_for_timeout(250)
    raise native.OutcomeUnknown()


def continue_received(actor,delegated,value):
    payload,grant=validate(value);observer._gate(actor)
    need(actor.dispatch_check(payload)is True,'SCREENING_RECOVERY_CONTINUATION_NOT_PERMITTED')
    room=delegated.daily_room;need(room is not None)
    source=_read_source(room.store.root,payload,grant)
    current_result=observer.observe(actor,delegated,dict(action=observer.GENERIC_ACTION,payload=payload))
    directory=_directory(room.store.root)
    try:current_raw=_private_file(directory,current_result['capture_name'],observer.MAX_CAPTURE)
    finally:os.close(directory)
    current=json.loads(current_raw,object_pairs_hook=pairs)
    if current_result.get('status') in ('LOGIN_REQUIRED','CHALLENGE_REQUIRED'):
        need(False,'SCREENING_RECOVERY_'+current_result['status'])
    need(current_result['error_count']==0 and current['stable_snapshot_reads']==4
        and _semantic(source)==_semantic(current),'SCREENING_RECOVERY_CONTINUATION_TURN_CHANGED')
    _persist_manual_context(room.store.root,payload,grant,source,current)
    current['manual_context_receipts']=read_manual_contexts(room.store.root,payload)
    seed=_seed(current,payload,grant)
    need(actor.dispatch_check(payload)is True,'SCREENING_RECOVERY_CONTINUATION_NOT_PERMITTED')
    # A stable grant ID excludes volatile read times. Repeated/uncertain calls
    # cannot create a new admission for the same received turn and intent chain.
    try:base,admission=_admit(room.store.root,grant,current_result['capture_name'])
    except FileExistsError:
        need(False,'SCREENING_RECOVERY_CONTINUATION_ALREADY_ADMITTED')
    sent=False;upload_started=False
    def before_upload():
        nonlocal upload_started
        if not upload_started:
            upload_started=True
            _write(room.store.root,base+'.upload-started.json',dict(grant_id=grant['grant_id'],
                started_at=time.time(),operation=payload['operation'],request_sha256=payload['request_sha256']))
    def before_send():
        nonlocal sent
        if not sent:
            _write(room.store.root,base+'.send-attempted.json',dict(grant_id=grant['grant_id'],
                attempted_at=time.time(),operation=payload['operation'],request_sha256=payload['request_sha256']))
            sent=True
    seed['before_send']=before_send
    seed['before_upload']=before_upload
    raw=delegated.bridge;old=(raw.operation_deadline,raw._operation_contract,raw.uploaded_names)
    deadline=raw.clock()+min(900,grant['expires_at']-time.time())
    try:
        raw.operation_deadline=deadline;raw._operation_contract=raw._dom_contract
        if delegated.native_turn_batch is not None:delegated.native_turn_batch.begin(payload)
        with room.operation(payload):
            record=seed['record'];db=room._connection
            selected=db.execute('SELECT room_id,state,anchors,binding FROM daily_rooms WHERE day=?',(record['day'],)).fetchone()
            need(selected is not None and selected[0]==record['room_id'] and selected[1]=='BOUND')
            need(identities(room.full_turns())==identities(seed['full_rows']))
            room.day=record['day'];room.room_id=record['room_id'];room._binding=json.loads(selected[3])
            room.offset=seed['offset'];room.prefix=identities(seed['full_rows'][:room.offset])
            room.mode='RECEIVED_CONTINUATION';room._prepared=True
            # A new bridge starts with an empty upload hint. The unchanged
            # native reader needs the current positively witnessed turn's
            # names to select its registered PNG/CSV projection.
            raw.uploaded_names=list(seed['turn_names'][-1])
            result=delegated._ask(payload,lambda:actor.dispatch_check(payload),
                chart_evidence=seed['initial_chart_evidence'],deadline=deadline,_received=seed)
        from .native_analysis_receipt import read_reviews
        result={**{key:payload[key]for key in('operation','marker','request_sha256')},**result}
        receipt=dict(kind=KIND,operation=payload['operation'],request_sha256=payload['request_sha256'],
            grant_id=grant['grant_id'],result=result,received_at=time.time(),
            review_receipts=read_reviews(room.store.root,payload),admission=admission)
        _write(room.store.root,base+'.completed.json',receipt)
        return receipt
    except Exception as error:
        # Preserve actual exception/phase evidence through the existing hook.
        # No absence of a result is interpreted as a safe replay opportunity.
        # A caught failure before any new durable intent or Send callback is
        # positively UNSENT. A crash never reaches this proof, and an existing
        # new/uncertain intent always remains blocking.
        try:unchanged=observer._receipts(room.store.root,payload)==current['receipt_files']
        except Exception:unchanged=False
        if not sent and not upload_started and unchanged:
            receipt=dict(kind=NOT_SENT,operation=payload['operation'],request_sha256=payload['request_sha256'],
                grant_id=grant['grant_id'],admission=admission,send_attempted=False,new_intent_written=False,upload_started=False,
                closed_at=time.time(),exception_class=type(error).__name__,
                reason=str(error)if re.fullmatch('[A-Z][A-Z0-9_]{0,119}',str(error))else None)
            saved=_write(room.store.root,base+'.closed-not-sent.json',receipt)
            return {**receipt,'disposition':{**saved,'value':receipt}}
        raise
    finally:raw.operation_deadline,raw._operation_contract,raw.uploaded_names=old
