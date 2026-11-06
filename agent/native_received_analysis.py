"""Observe an already sent analysis on the existing native page thread.

This bounded incident reader never sends, uploads, calls Mini, or completes a
research request. It can seal an uncertain SENT room only from its actual
complete stable native reply and exact prior journal prefix. All current text
is retained privately before a compact result leaves the actor; no old HOLD
becomes a new decision.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
import threading
import time

from .native_daily_room import DailyRoom, identities
from .native_file_upload import FILE_WITNESS
from .native_chart_followup import _directory, _private_file, pairs

ACTION = 'ANALYSIS_HOLD_OBSERVE'
GENERIC_ACTION = 'ANALYSIS_RECEIVED_OBSERVE'
GENERIC_KIND = 'NATIVE_RECEIVED_ANALYSIS_OBSERVATION_V2'
KIND = 'NATIVE_RECEIVED_ANALYSIS_OBSERVATION_V1'
OPERATION = '2026-11-05:robot-v9:0:analysis-v9:RUNEUSDT'
REQUEST_SHA256 = 'f34e17915e4803937cae648e2fb9aa8d46c77e6ab794a9a27dc9e5dd85b8c50b'
MAX_CAPTURE = 8 * 1024 * 1024
_FIELDS = {'operation', 'marker', 'provider', 'contract_version', 'prompt', 'output_schema', 'context', 'request_sha256'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def digest(value): return hashlib.sha256(canonical(value)).hexdigest()
def native_digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),
        ensure_ascii=True,allow_nan=False).encode('ascii')).hexdigest()
def text_sha(value): return hashlib.sha256(value.encode()).hexdigest()


def need(value, code='RECEIVED_ANALYSIS_OBSERVATION_UNVERIFIED'):
    if not value: raise ValueError(code)


def is_observation(value):
    return type(value) is dict and value.get('action') in (ACTION, GENERIC_ACTION,'SCREENING_RECEIVED_OBSERVE')


def validate(value):
    if type(value)is dict and value.get('action')=='SCREENING_RECEIVED_OBSERVE':
        from .native_received_screening import validate as screening_request
        return screening_request(value)
    need(type(value) is dict and set(value) == {'action', 'payload'}
         and value['action'] in (ACTION, GENERIC_ACTION))
    payload = value['payload']
    need(type(payload) is dict and set(payload) == _FIELDS)
    operation, request = payload['operation'], payload['request_sha256']
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}):robot-v9:(?:0|[1-9]\d{0,11}):analysis-v9:([A-Z0-9]{2,18}USDT)', operation)
    need(match is not None)
    from datetime import date
    date.fromisoformat(match[1])
    need(type(request) is str and re.fullmatch('[a-f0-9]{64}',request)
         and payload['provider'] == 'NEUROBRO_WEB'
         and payload['contract_version'] == 'neurobro-browser-decision-v2'
         and payload['marker'] == 'HARUN_' + text_sha(operation)[:32]
         and type(payload['context']) is dict and payload['context'].get('symbol') == match[2]
         and type(payload['output_schema']) is dict
         and set(payload['output_schema'].get('required', [])) == {
             'symbol', 'side', 'limit_entry', 'take_profit', 'stop_loss', 'risk_reward'})
    body = {key: item for key, item in payload.items() if key != 'request_sha256'}
    need(len(canonical(body)) <= 393216 and digest(body) == request)
    if value['action'] == ACTION:
        need(operation == OPERATION and request == REQUEST_SHA256)
    else:
        need(payload['context'].get('source') == 'Binance Futures')
    return payload


def _gate(actor):
    need(actor.thread.ident == threading.get_ident() and actor.claim.locked(),
         'RECEIVED_ANALYSIS_ACTOR_OWNERSHIP_REQUIRED')
    need(not actor.stopping.is_set() and actor._mode() == 'AUTOMATION'
         and not actor.sessions.active(), 'RECEIVED_ANALYSIS_BROWSER_NOT_AUTOMATIC')


def _save(root, capture):
    raw = canonical(capture)
    need(0 < len(raw) <= MAX_CAPTURE, 'RECEIVED_ANALYSIS_CAPTURE_TOO_LARGE')
    key = digest({key:capture[key] for key in ('operation','request_sha256')})[:16]
    name = 'received-analysis-observation-' + key + '-' + hashlib.sha256(raw).hexdigest() + '.json'
    directory = _directory(root); temporary = '.received-analysis-' + os.urandom(16).hex() + '.tmp'
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600,
                     dir_fd=directory)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
            os.fsync(directory)
        except FileExistsError:
            pass
        os.unlink(temporary, dir_fd=directory)
        need(_private_file(directory, name, MAX_CAPTURE) == raw)
        return dict(capture_name=name, capture_sha256=hashlib.sha256(raw).hexdigest(),
                    capture_bytes=len(raw))
    finally:
        try: os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError: pass
        os.close(directory)


def _receipts(root, payload):
    # Fixed native receipt directory/key; the caller cannot select a path.
    key = hashlib.sha256(json.dumps({key:payload[key] for key in ('operation','request_sha256')},
        sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    parent = _directory(root); directory = None
    try:
        try:
            directory = os.open('dialogue-200-followup', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=parent)
        except FileNotFoundError:
            return {}
        info = os.fstat(directory)
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
             and stat.S_IMODE(info.st_mode) == 0o700)
        pattern = re.compile(re.escape(key) + r'(?:\.(?:[2-9]|[1-9][0-9]+))?\.(?:intent|complete)\.json')
        names = sorted(name for name in os.listdir(directory) if pattern.fullmatch(name))
        result = {}; total = 0
        for name in names:
            raw = _private_file(directory, name, 262144)
            total += len(raw); need(total <= 4*1024*1024, 'RECEIVED_ANALYSIS_RECEIPT_BYTE_LIMIT')
            value = json.loads(raw, object_pairs_hook=pairs)
            need(value.get('operation') == payload['operation'] and value.get('request_sha256') == payload['request_sha256']
                 and value.get('marker') == payload['marker'])
            result[name] = dict(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), value=value)
        return result
    finally:
        if directory is not None: os.close(directory)
        os.close(parent)


def _image_observation(image):
    value = dict(alt=image.get_attribute('alt'), visible=image.is_visible(),
        decoded=image.evaluate('(image) => ({complete:image.complete, '
            'width:image.naturalWidth,height:image.naturalHeight})'))
    # Ordinary public loading attributes help explain a pending image. Never
    # retain src/currentSrc or signed image URLs in this observation.
    for name, allowed in (('loading', {'lazy', 'eager', 'auto'}),
            ('decoding', {'sync', 'async', 'auto'}), ('fetchpriority', {'high', 'low', 'auto'})):
        observed = image.get_attribute(name)
        value[name] = observed if observed is None or observed in allowed else 'OTHER'
    return value


def _file_tiles(room, raw, native, rows, receipt_files, *, prepared=(), indices=None):
    groups = []
    for entry in receipt_files.values():
        for key in ('initial_attachment_names', 'followup_attachment_names'):
            names = entry['value'].get(key)
            if type(names) is list and 2 <= len(names) <= 4 and all(type(n) is str and len(n) <= 240 for n in names):
                if names not in groups: groups.append(names)
    for entry in prepared:
        names=entry['value']['attachment_names']
        if names not in groups: groups.append(names)
    nodes = room.original_visible(native.MESSAGE)
    need(len(nodes) == len(rows), 'RECEIVED_ANALYSIS_MESSAGE_SNAPSHOT_CHANGED')
    output = []
    for index, (row, node) in enumerate(zip(rows, nodes)):
        if row.get('role') != 'user' or indices is not None and index not in indices: continue
        images = node.locator('img'); need(images.count() <= 8)
        actual = []
        for number in range(images.count()):
            image = images.nth(number)
            actual.append(_image_observation(image))
        witnesses = [dict(names=names, witness=node.evaluate(FILE_WITNESS,
            dict(names=names, bodySelector=native.USER_BODY))) for names in groups]
        output.append(dict(row=index, prompt_sha256=text_sha(row['text']), images=actual,
                           file_tile_witnesses=witnesses))
    return output


def _materialize_images(actor, room, raw, native, capture, selected):
    """Load only owned pending PNGs through ordinary visible UI scrolling.

    The full pre-action capture is immutable before the first scroll. A decoded
    image is accepted only after a real DOM read; text, IDs, footer completion,
    room and owner-thread authority must survive every action and wait.
    """
    rows = capture['rows']; expected = identities(rows); url = capture['page_url']
    pending = []
    for tile in capture['file_tiles']:
        if tile['row'] not in selected: continue
        images = tile['images']
        if len(images) != 2: continue
        actual_names = [image['alt'] for image in images]
        groups = [group for group in tile['file_tile_witnesses']
            if len(group['names']) == 4 and len(set(group['names'])) == 4
            and set(actual_names) == set(group['names'][:2])
            and len(group['witness']) == 4
            and all(item.get('name') == name and item.get('matches') == 1
                for item, name in zip(group['witness'], group['names']))]
        if len({tuple(group['names']) for group in groups}) != 1: continue
        for image in images:
            state = image['decoded']
            if (image['visible'] is True and state.get('complete') is False
                    and state.get('width') == 0 and state.get('height') == 0):
                pending.append((tile['row'], image['alt']))
    if not pending:
        return rows
    until = min(raw.operation_deadline, raw.clock() + 8)
    capture['image_materialization'] = dict(kind='OWNED_PNG_UI_MATERIALIZATION_V1',
        sends=0, uploads=0, requested=len(pending), actions=[])

    def check():
        _gate(actor)
        need(raw.clock() < until, 'RECEIVED_ANALYSIS_IMAGE_DECODE_PENDING')
        need(raw.page.url == url, 'RECEIVED_ANALYSIS_MESSAGE_SNAPSHOT_CHANGED')
        status = raw.status(maintenance_allowed=lambda: False)
        capture['status'] = status
        capture['session_observation'] = copy.deepcopy(raw.session_observation())
        need(status == 'CONNECTED', 'RECEIVED_ANALYSIS_BROWSER_NOT_READY')
        need(raw.clock() < until, 'RECEIVED_ANALYSIS_IMAGE_DECODE_PENDING')
        observed = room.full_turns()
        need(identities(observed) == expected and raw.page.url == url,
            'RECEIVED_ANALYSIS_MESSAGE_SNAPSHOT_CHANGED')
        need(raw.clock() < until, 'RECEIVED_ANALYSIS_IMAGE_DECODE_PENDING')
        return observed

    def locate(index, name):
        nodes = room.original_visible(native.MESSAGE)
        need(len(nodes) == len(rows), 'RECEIVED_ANALYSIS_MESSAGE_SNAPSHOT_CHANGED')
        images = nodes[index].locator('img')
        need(images.count() == 2, 'RECEIVED_ANALYSIS_IMAGE_IDENTITY_CHANGED')
        matched = [images.nth(i) for i in range(2) if images.nth(i).get_attribute('alt') == name]
        need(len(matched) == 1 and matched[0].is_visible(), 'RECEIVED_ANALYSIS_IMAGE_IDENTITY_CHANGED')
        return matched[0]

    capture['stage'] = 'BEFORE_IMAGE_MATERIALIZATION'
    check(); _save(room.store.root, capture)
    for index, name in pending:
        check(); image = locate(index, name)
        event = dict(global_row=index, name=name, scroll_attempted=True, decoded=False)
        capture['image_materialization']['actions'].append(event)
        # Persist the exact intended UI action too, before it can happen.
        _save(room.store.root, capture)
        check()
        image.scroll_into_view_if_needed(timeout=max(1, min(1500, int((until-raw.clock())*1000))))
        event['scroll_dispatched'] = True
        check()
        image_until = min(until, raw.clock() + 2)
        while True:
            check(); image = locate(index, name)
            event['last_observation'] = _image_observation(image)
            decoded = event['last_observation']['decoded']
            need(raw.clock() <= image_until, 'RECEIVED_ANALYSIS_IMAGE_DECODE_PENDING')
            need(type(decoded) is dict and type(decoded.get('width')) is int
                and type(decoded.get('height')) is int, 'RECEIVED_ANALYSIS_IMAGE_IDENTITY_CHANGED')
            if decoded.get('complete') is True and decoded['width'] > 0 and decoded['height'] > 0:
                event.update(decoded=True, dimensions=[decoded['width'], decoded['height']])
                break
            need(decoded.get('complete') is False and decoded['width'] == decoded['height'] == 0,
                'RECEIVED_ANALYSIS_IMAGE_DECODE_FAILED')
            need(raw.clock() < image_until, 'RECEIVED_ANALYSIS_IMAGE_DECODE_PENDING')
            raw.page.wait_for_timeout(min(100, max(1, int((image_until-raw.clock())*1000))))
            check()
    observed = check()
    capture['rows'] = observed
    capture['stage'] = 'AFTER_IMAGE_MATERIALIZATION'
    capture['file_tiles'] = _file_tiles(room, raw, native, observed, capture['receipt_files'],
        prepared=capture.get('prepared_materials', ()), indices=selected)
    _save(room.store.root, capture)
    return observed


def _room_witness(record, rows, raw, root, cached, receipts, *, payload=None, generic=False):
    binding = {key: record[key] for key in ('first_prompt_sha256', 'marker', 'history_titles')}
    if record['identifier_reconciliation'] is not None:
        binding['identifier_reconciliation'] = record['identifier_reconciliation']
    native_room = DailyRoom.matches(rows, record['anchors'], binding, 'BOUND', archive_root=root,
        prefix_identity=(record['day'],record['room_id']))
    initial = [i for i, row in enumerate(rows) if row.get('role') == 'user'
               and text_sha(row.get('text', '')) == cached['rendered_sha256']]
    followup = []
    for item in receipts.values():
        value = item['value']
        expected = value.get('followup_prompt_sha256')
        if expected is None: continue
        for index, row in enumerate(rows):
            if row.get('role') == 'user' and text_sha(row.get('text', '')) == expected:
                followup.append(index)
    followup = sorted(set(followup))
    operation = OPERATION if payload is None else payload['operation']
    matched = (native_room and raw.page.url.startswith('https://app.neurobro.ai/') and len(initial) == 1
        and cached['rendered_prompt'].count('HARUN_' + text_sha(operation)[:32]) == 1
        and (generic or len(followup) >= 1) and all(i > initial[0] for i in followup))
    return dict(native_room_matches=native_room, journal_url_matches=raw.page.url == record['url'],
                initial_analysis_user_rows=initial,
                followup_user_rows=followup, same_loaded_room_verified=bool(matched))


def _operation_scope(rows, witness, receipts, *, manual_contexts=(), payload=None):
    initial = witness['initial_analysis_user_rows']
    if len(initial) != 1:
        return dict(verified=False,initial_user=None,latest_user=None,user_rows=[],reply_row=None,
                    unmatched_intents=[],reason='INITIAL_PROMPT_NOT_UNIQUE')
    start=initial[0]; users=[start]; unmatched=[]; sequences=[]
    intents=sorted(((entry['value'].get('followup_sequence',1),name,entry['value'])
        for name,entry in receipts.items() if name.endswith('.intent.json')), key=lambda item:item[0])
    for sequence,name,intent in intents:
        sequences.append(sequence)
        matches=[i for i,row in enumerate(rows) if i>start and row.get('role')=='user'
                 and text_sha(row.get('text',''))==intent.get('followup_prompt_sha256')]
        if len(matches)!=1: unmatched.append(name)
        else: users.append(matches[0])
    users=sorted(set(users));latest=users[-1]
    if manual_contexts:
        from .native_received_continuation import locate_manual_contexts
        contexts=locate_manual_contexts(rows,manual_contexts,payload)
        excluded={i for item in contexts for i in(item['global_row'],item['global_row']+1)}
        consecutive=users==[i for i in range(start,latest+1,2)if i not in excluded]
    else:consecutive=users==list(range(start,latest+1,2))
    reply=latest+1 if latest+1<len(rows) else None
    complete=reply is not None and rows[reply].get('role')=='assistant' and rows[reply].get('complete')is True
    chain_complete=sequences==list(range(1,len(sequences)+1))
    verified=bool(witness['same_loaded_room_verified'] and not unmatched and consecutive and chain_complete)
    return dict(verified=verified,initial_user=start,latest_user=latest,user_rows=users,
        reply_row=reply,reply_complete=complete,unmatched_intents=unmatched,
        reason=None if verified else 'OPERATION_TRANSCRIPT_UNVERIFIED')


def _project_operation(capture):
    result=copy.deepcopy(capture);rows=capture.get('rows',[]);scope=capture.get('operation_scope',{})
    if capture.get('kind')==GENERIC_KIND and result.get('review_receipts'):
        reviews=result['review_receipts']
        need(type(reviews)is list and all(type(entry)is dict and type(entry.get('value'))is dict
            and type(entry['value'].get('observed_at'))in(int,float)
            and math.isfinite(entry['value']['observed_at']) and entry['value']['observed_at']>0
            for entry in reviews),'RECEIVED_ANALYSIS_REVIEW_TIME_UNVERIFIED')
        # Full immutable reviews remain in the private capture/archive. Only
        # the newest actual decision is needed for current-turn approval.
        result['review_receipts']=[max(reviews,key=lambda entry:entry['value']['observed_at'])]
    start=scope.get('initial_user');end=scope.get('reply_row')
    if start is None:
        result['rows']=[];result['file_tiles']=[];result['row_offset']=None
        return result
    stop=(end+1)if end is not None else scope['latest_user']+1
    excluded=set();contexts=[]
    if capture.get('manual_context_receipts'):
        from .native_received_continuation import locate_manual_contexts
        payload=dict(operation=capture['operation'],request_sha256=capture['request_sha256'],
            context={'symbol':capture['operation'].rsplit(':',1)[-1]})
        contexts=locate_manual_contexts(rows,capture['manual_context_receipts'],payload)
        excluded={i for item in contexts for i in(item['global_row'],item['global_row']+1)}
        result['manual_context_observations']=[dict(receipt_sha256=item['receipt']['sha256'],
            after_owned_pairs=sum(1 for i in range(start,item['global_row'])if i not in excluded)//2,
            rows=copy.deepcopy(item['rows']))for item in contexts]
    selected=[i for i in range(start,stop)if i not in excluded];remap={old:new for new,old in enumerate(selected)}
    result['rows']=copy.deepcopy([rows[i]for i in selected]);result['row_offset']=start
    result['native_history_prefix_sha256']=digest([text_sha(row.get('text',''))for row in rows[:start]])
    result['file_tiles']=[dict(copy.deepcopy(item),row=remap[item['row']],global_row=item['row'])
        for item in capture.get('file_tiles',[]) if item['row']in remap]
    initial_key=('initial_screening_user_rows'if 'initial_screening_user_rows'in result['room_witness']else'initial_analysis_user_rows')
    result['room_witness'][initial_key]=[remap[i]for i in result['room_witness'][initial_key]]
    result['room_witness']['followup_user_rows']=[remap[i]for i in result['room_witness']['followup_user_rows']]
    result['operation_scope']={**copy.deepcopy(scope),'initial_user':0,
        'latest_user':remap[scope['latest_user']],'user_rows':[remap[i]for i in scope['user_rows']],
        'reply_row':None if end is None else remap[end]}
    return result


def _prior_review_user(root, rows, assistant, review, cache, receipts):
    """Bind a retained author's answer to its exact initial/followup user."""
    value=review['value'];operation=value['operation'];marker='HARUN_'+text_sha(operation)[:32]
    user=assistant-1
    initial=[i for i,row in enumerate(rows[:assistant])if row.get('role')=='user'
        and row.get('text')==cache['rendered_prompt']]
    need(len(initial)==1,'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
    start=initial[0]
    files=receipts(root,dict(operation=operation,request_sha256=value['request_sha256'],marker=marker))
    if start==user:
        first=[(name,entry)for name,entry in files.items()if name.endswith('.intent.json')
            and entry['value'].get('followup_sequence',1)==1]
        if not first:return dict(initial_user=start,followup_receipts=[])
        need(len(first)==1,'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
        name,entry=first[0];intent=entry['value']
        need(intent.get('original_payload_sha256')==value['payload_sha256']
            and intent.get('initial_prompt_sha256')==text_sha(rows[start]['text'])
            and intent.get('initial_reply_sha256')==text_sha(rows[assistant]['text'])
            and intent.get('initial_turns_sha256')==value['transcript_sha256'],
            'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
        return dict(initial_user=start,followup_receipts=[dict(name=name,sha256=entry['sha256'],bytes=entry['bytes'])])
    need(start<user and (user-start)%2==0,'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
    previous=None;witness=[]
    for sequence,index in enumerate(range(start+2,user+1,2),1):
        matches=[(name,entry)for name,entry in files.items()if name.endswith('.intent.json')
            and entry['value'].get('followup_sequence',1)==sequence]
        need(len(matches)==1,'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
        name,entry=matches[0];intent=entry['value']
        need(intent.get('kind')=='NATIVE_200_CHART_FOLLOWUP' and intent.get('schema_version')==1
            and intent.get('original_payload_sha256')==value['payload_sha256']
            and intent.get('initial_prompt_sha256')==text_sha(rows[start]['text'])
            and intent.get('followup_prompt_sha256')==text_sha(rows[index]['text'])
            and intent.get('initial_reply_sha256')==text_sha(rows[index-1]['text'])
            and intent.get('previous_followup_intent_sha256')==previous,
            'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
        previous=native_digest(intent)
        witness.append(dict(name=name,sha256=entry['sha256'],bytes=entry['bytes']))
    complete=[(name,entry)for name,entry in files.items()if name.endswith('.complete.json')
        and entry['value'].get('intent_sha256')==previous]
    need(len(complete)==1,'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
    name,entry=complete[0];closed=entry['value']
    need({key:item for key,item in closed.items()if key not in('intent_sha256','response_id','final_reply_sha256')}==intent
        and closed.get('final_reply_sha256')==text_sha(rows[assistant]['text'])
        and type(closed.get('response_id'))is str and re.fullmatch('[A-Za-z0-9_.:-]{1,160}',closed['response_id']),
        'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
    witness.append(dict(name=name,sha256=entry['sha256'],bytes=entry['bytes']))
    return dict(initial_user=start,followup_receipts=witness)


def _sent_prefix_authority(room, capture, snapshot, prefix_count):
    """Translate only assistant IDs, backed by old native rows or prior reviews."""
    from .native_daily_room import read_prefix_snapshot
    from .dialogue_service import validate_result
    record=capture['room'];rows=capture['rows'];anchors=record['anchors'];actual=identities(rows)
    changed=[i for i in range(prefix_count)if actual[i]!=anchors[i]]
    if not changed:return None
    need(all(i%2==1 and rows[i].get('role')=='assistant'and rows[i].get('complete')is True
        and type(rows[i].get('id'))is str and re.fullmatch('[A-Za-z0-9_.:-]{1,160}',rows[i]['id'])for i in changed),
        'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
    retained=read_prefix_snapshot(room.store.root,record['day'],record['room_id'],anchors)
    if retained is not None:
        old=retained['value']['rows']
        semantic=lambda row:{key:value for key,value in row.items()if key not in('id','attachments')}
        need(all(semantic(old[i])==semantic(rows[i])for i in range(prefix_count)),
            'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
        return dict(kind='EXACT_NATIVE_PREFIX_SNAPSHOT',changed_assistant_rows=changed,
            snapshot={key:retained[key]for key in('name','sha256','bytes')})
    # Older releases kept immutable native Mini reviews but not full row IDs.
    # Those independently bind exact reply text, paired users and this room.
    directory=_directory(room.store.root);available=[];prior_answers={};wanted={text_sha(rows[i]['text'])for i in changed}
    try:
        for name in sorted(os.listdir(directory)):
            if not re.fullmatch(r'(?:screening|analysis)-review-[a-f0-9]{64}-[a-f0-9]{64}\.json',name):continue
            try:
                raw=_private_file(directory,name,512*1024);value=json.loads(raw,object_pairs_hook=pairs)
            except (OSError,ValueError):continue
            if type(value)is dict and type(value.get('operation'))is str and type(value.get('reply_sha256'))is str:
                if (value.get('conversation_id')=='neurobro:'+record['day']+':'+record['room_id']
                        and type(value.get('observed_at'))in(int,float)and 0<value['observed_at']<=snapshot['updated_at']):
                    prior_answers.setdefault(value.get('operation'),set()).add(value.get('reply_sha256'))
                if value.get('reply_sha256')in wanted:
                    available.append(dict(name=name,sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw),value=value))
        authority=[]
        for index in changed:
            choices=[]
            for entry in available:
                value=entry['value']
                if value.get('reply_sha256')!=text_sha(rows[index]['text']):continue
                try:
                    phase=value['phase'];operation=value['operation'];request=value['request'];response=value['response']
                    need(phase in('SCREENING','ANALYSIS')and value['kind']==('NATIVE_SCREENING_MINI_REVIEW_V1'if phase=='SCREENING'else'NATIVE_ANALYSIS_MINI_REVIEW_V1')
                        and value['schema_version']==1 and operation.startswith(record['day']+':robot-v9:')
                        and value['conversation_id']=='neurobro:'+record['day']+':'+record['room_id']
                        and type(value['observed_at'])in(int,float)and 0<value['observed_at']<=snapshot['updated_at']
                        and all(type(value[key])is str and re.fullmatch('[a-f0-9]{64}',value[key])for key in
                            ('request_sha256','payload_sha256','context_sha256','reply_sha256','transcript_sha256','request_id')),
                        'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
                    request_id=native_digest(dict(operation=operation,request_sha256=value['request_sha256'],
                        reply_sha256=value['reply_sha256'],phase=phase,transcript_sha256=value['transcript_sha256']))
                    prefix=('screening'if phase=='SCREENING'else'analysis')+'-review-'+native_digest(
                        dict(operation=operation,request_sha256=value['request_sha256']))+'-'
                    need(entry['name']==prefix+request_id+'.json'and value['request_id']==request_id
                        and request['request_id']==request_id and request['phase']==phase
                        and request['conversation_id']==value['conversation_id']
                        and request['reply']==rows[index]['text'] and request['reply_sha256']==value['reply_sha256']
                        and request['gate']==dict(robot_on=True,native_dispatch_verified=True)
                        and response['reason']=='DIALOGUE_DECISION','RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
                    validate_result(response,request)
                    if phase=='SCREENING':
                        need(re.fullmatch(re.escape(record['day'])+r':robot-v9:(?:0|[1-9]\d{0,11}):screening:(?:0|[1-9]\d{0,11}):[12]',operation)
                            and request['market_context']is None,'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
                        done=value['model_operation']
                        from .dialogue_service import screening_model_operation
                        need(done['state']=='DONE'and done['kind']=='DELEGATED_MODEL_DIALOGUE'
                            and screening_model_operation(done['operation_id'],request_id)
                            and re.fullmatch('[a-f0-9]{64}',done['request_hash'])
                            and json.loads(done['result'])==response and 0<done['updated_at']<=value['observed_at'],
                            'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
                    else:
                        selected=re.fullmatch(re.escape(record['day'])+r':robot-v9:(?:0|[1-9]\d{0,11}):analysis-v9:([A-Z0-9]{2,18}USDT)',operation)
                        need(selected is not None and type(request['market_context'])is dict
                            and request['market_context'].get('symbol')==selected[1],'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
                    cache_name='mini-prompt-'+value['payload_sha256']+'.json'
                    cache_raw=_private_file(directory,cache_name,384*1024);cache=json.loads(cache_raw,object_pairs_hook=pairs)
                    marker='HARUN_'+text_sha(operation)[:32]
                    need(type(cache)is dict and set(cache)=={'schema_version','operation','payload_sha256','model_request_id','rendered_prompt','rendered_sha256'}
                        and cache['schema_version']==1 and cache['operation']==operation and cache['payload_sha256']==value['payload_sha256']
                        and cache['rendered_sha256']==text_sha(cache['rendered_prompt'])
                        and type(cache['model_request_id'])is str and re.fullmatch('[a-f0-9]{64}',cache['model_request_id'])
                        and cache['rendered_prompt'].count(marker)==1,'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
                    association=_prior_review_user(room.store.root,rows,index,entry,cache,_receipts)
                    need(association['followup_receipts']or len(prior_answers.get(operation,()))==1,
                        'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
                    choices.append(dict(index=index,old_anchor=anchors[index],current_anchor=actual[index],
                        review={key:entry[key]for key in('name','sha256','bytes')},
                        cache=dict(name=cache_name,sha256=hashlib.sha256(cache_raw).hexdigest(),bytes=len(cache_raw)),
                        operation=operation,request_sha256=value['request_sha256'],reply_sha256=value['reply_sha256'],
                        transcript_sha256=value['transcript_sha256'],**association))
                except (OSError,ValueError,KeyError,TypeError):continue
            need(choices,'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')
            authority.append(choices[0])
        return dict(kind='PRIOR_NATIVE_REVIEWS_AND_USER_ANCHORS',changed_assistant_rows=changed,rows=authority)
    finally:os.close(directory)


def _sent_window_alignment(room,capture):
    """Preserve only omitted rows from an exact old snapshot, never invent DOM."""
    from .native_daily_room import read_prefix_snapshot,prefix_snapshot_matches
    record=capture['room'];rows=capture['rows'];offset=capture['operation_scope']['latest_user']
    if type(offset)is not int or offset<2 or offset>=len(record['anchors'])or len(rows)!=offset+2:return None
    retained=read_prefix_snapshot(room.store.root,record['day'],record['room_id'],record['anchors'])
    if retained is None:return None
    old=retained['value']['rows'];omitted=len(old)-offset
    if omitted<=0 or omitted%2:return None
    # Check the historical suffix alone: the current operation's pair is
    # independently bound by cache, attachments and four stable native reads.
    if not prefix_snapshot_matches(room.store.root,record['day'],record['room_id'],record['anchors'],rows[:offset]):return None
    return dict(omitted=omitted,rows=old[:omitted]+rows,
        authority=dict(kind='EXACT_NATIVE_PREFIX_SNAPSHOT_WINDOW',visible_row_offset=omitted,
            visible_prior_rows=offset,snapshot={key:retained[key]for key in('name','sha256','bytes')}))


def _seal_sent_room(actor, room, raw, payload, capture, snapshot, *, screening):
    """Append one observed completed turn, never infer whether Send happened."""
    from .native_daily_room import canonical as room_canonical,retain_prefix_snapshot
    from .native_screening_recovery import _room_record
    from .native_analysis_material import validate_prepared
    record=capture['room'];rows=capture['rows'];scope=capture['operation_scope']
    anchors=record['anchors'];offset=scope['latest_user'];cached=capture['rendered_prompt_receipt']
    alignment=_sent_window_alignment(room,capture)
    _gate(actor)
    capture['status']=raw.status(maintenance_allowed=lambda:False)
    capture['session_observation']=copy.deepcopy(raw.session_observation())
    need(capture['status']=='CONNECTED','RECEIVED_ANALYSIS_BROWSER_NOT_READY')
    need(snapshot['state']=='SENT' and capture['stable_snapshot_reads']==4
        and scope['verified'] is True and scope['reply_complete'] is True
        and capture['room_witness']['same_loaded_room_verified'] is True
        and type(anchors)is list and len(anchors)%2==0
        and all(type(anchor)is str and re.fullmatch('[a-f0-9]{64}',anchor)for anchor in anchors)
        and type(offset)is int and offset>=0 and (len(anchors)in(offset,offset+2)or alignment is not None)
        and len(rows)==offset+2
        and rows[offset]['role']=='user' and rows[-1]['role']=='assistant' and rows[-1]['complete']is True
        and type(rows[-1].get('text'))is str and bool(rows[-1]['text'])
        and type(rows[-1].get('id'))is str and re.fullmatch('[A-Za-z0-9_.:-]{1,160}',rows[-1]['id'])
        and raw.page.url==record['url']==capture['page_url']
        and room.full_turns()==rows,'RECEIVED_ANALYSIS_SENT_ROOM_UNVERIFIED')
    # SENT can retain the current user and a last assistant snapshot taken
    # before its final text settled. Preserve every earlier identity, including
    # that user; only this operation's last assistant may finish changing.
    prefix_count=offset+1 if len(anchors)==offset+2 and alignment is None else offset
    prefix_authority=alignment['authority']if alignment is not None else _sent_prefix_authority(room,capture,snapshot,prefix_count)
    start=scope['initial_user']
    need(cached['operation']==payload['operation'] and cached['payload_sha256']==native_digest(payload)
        and cached['rendered_sha256']==text_sha(cached['rendered_prompt'])==text_sha(rows[start]['text'])
        and cached['rendered_prompt'].count(payload['marker'])==1
        and sum(row.get('role')=='user' and row.get('text')==cached['rendered_prompt'] for row in rows)==1,
        'RECEIVED_ANALYSIS_SENT_ROOM_UNVERIFIED')
    if not anchors:
        need(record['first_prompt_sha256']==text_sha(cached['rendered_prompt'].replace('\r\n','\n').strip())
            and record['marker']==payload['marker'],'RECEIVED_ANALYSIS_SENT_ROOM_UNVERIFIED')
    if screening:
        need(rows[offset].get('attachments',[])==[] and capture['file_tiles']==[],
            'RECEIVED_ANALYSIS_SENT_ROOM_UNVERIFIED')
    else:
        materials=[]
        if offset==start:
            for entry in capture.get('prepared_materials',[]):
                value=entry['value'];validate_prepared(value,payload)
                if value['rendered_prompt_sha256']==text_sha(rows[offset]['text']) and value['original_request_matches_material']is True:
                    materials.append(value['attachment_names'])
        else:
            for name,entry in capture['receipt_files'].items():
                value=entry['value']
                if (name.endswith('.intent.json') and value.get('kind')=='NATIVE_200_CHART_FOLLOWUP'
                        and value.get('original_payload_sha256')==native_digest(payload)
                        and value.get('initial_prompt_sha256')==text_sha(rows[start]['text'])
                        and value.get('followup_prompt_sha256')==text_sha(rows[offset]['text'])
                        and value.get('followup_sequence',1)==len(scope['user_rows'])-1):
                    materials.append(value['followup_attachment_names'])
        need(len(materials)==1,'RECEIVED_ANALYSIS_SENT_MATERIAL_UNVERIFIED')
        names=materials[0]
        need(type(names)is list and len(names)==4 and len(set(names))==4,
            'RECEIVED_ANALYSIS_SENT_MATERIAL_UNVERIFIED')
        tiles=[tile for tile in capture['file_tiles'] if tile['row']==offset]
        need(len(tiles)==1 and tiles[0]['prompt_sha256']==text_sha(rows[offset]['text']),
            'RECEIVED_ANALYSIS_SENT_MATERIAL_UNVERIFIED')
        tile=tiles[0]
        witnesses=[group for group in tile['file_tile_witnesses'] if group['names']==names]
        need(len(witnesses)==1 and len(witnesses[0]['witness'])==4
            and all(item.get('name')==name and item.get('ready')is True and item.get('matches')==1
                for item,name in zip(witnesses[0]['witness'],names))
            and len(tile['images'])==2 and {image['alt']for image in tile['images']}==set(names[:2])
            and all(image['visible']is True and image['decoded'].get('complete')is True
                and type(image['decoded'].get('width'))is int and image['decoded']['width']>0
                and type(image['decoded'].get('height'))is int and image['decoded']['height']>0
                for image in tile['images']),'RECEIVED_ANALYSIS_SENT_MATERIAL_UNVERIFIED')
    bound_rows=rows if alignment is None else alignment['rows']
    updated=room.wall_clock();bound_anchors=room_canonical(identities(bound_rows))
    capture['room_recovery']=dict(kind='NATIVE_SENT_ROOM_COMPLETION_SEAL_V1',state='PREPARED',
        operation=payload['operation'],request_sha256=payload['request_sha256'],
        prior_record=copy.deepcopy(snapshot),preserved_prefix_count=prefix_count,
        prior_assistant_anchor=anchors[-1]if len(anchors)==offset+2 and alignment is None else None,
        completed_anchors_sha256=text_sha(bound_anchors),updated_at=updated)
    capture['room_recovery']['prefix_authority']=prefix_authority
    capture['room_recovery']['completed_prefix_snapshot']=retain_prefix_snapshot(room.store.root,record['day'],record['room_id'],bound_rows)
    capture['stage']='SENT_ROOM_SEAL_PREPARED';_save(room.store.root,capture)
    with room.store.open()as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            current=db.execute('SELECT * FROM daily_rooms WHERE day=?',(record['day'],)).fetchone()
            need(current is not None and dict(zip(('day','room_id','state','url','anchors','binding','updated_at'),current))==snapshot,
                'RECEIVED_ANALYSIS_SENT_ROOM_CHANGED')
            _gate(actor)
            capture['status']=raw.status(maintenance_allowed=lambda:False)
            capture['session_observation']=copy.deepcopy(raw.session_observation())
            need(capture['status']=='CONNECTED','RECEIVED_ANALYSIS_BROWSER_NOT_READY')
            need(raw.page.url==record['url'] and room.full_turns()==rows,
                'RECEIVED_ANALYSIS_MESSAGE_SNAPSHOT_CHANGED')
            changed=db.execute('UPDATE daily_rooms SET state=\'BOUND\',anchors=?,updated_at=? '
                'WHERE day=? AND room_id=? AND state=\'SENT\' AND url IS ? AND anchors=? AND binding=? AND updated_at=?',
                (bound_anchors,updated,snapshot['day'],snapshot['room_id'],snapshot['url'],snapshot['anchors'],
                    snapshot['binding'],snapshot['updated_at'])).rowcount
            need(changed==1,'RECEIVED_ANALYSIS_SENT_ROOM_CHANGED')
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK');raise
    capture['room']=_room_record(room.store.root,record['day'])
    capture['room_recovery']['state']='SEALED'


def observe(actor, delegated, value):
    payload = validate(value); _gate(actor)
    screening = value['action']=='SCREENING_RECEIVED_OBSERVE'
    generic = value['action'] == GENERIC_ACTION or screening
    operation, request = payload['operation'], payload['request_sha256']
    kind = ('NATIVE_RECEIVED_SCREENING_OBSERVATION_V1'if screening else GENERIC_KIND) if generic else KIND
    from .native_screening_recovery import _native_source_digest, NATIVE_DOM_SHA256, _room_record
    room, raw, native = delegated.daily_room, delegated.bridge, delegated.native
    need(room is not None and room.bridge is raw and room.native is native
         and _native_source_digest(native) == NATIVE_DOM_SHA256)
    capture = dict(schema_version=2 if generic else 1, kind=kind, operation=operation,
        request_sha256=request, payload_sha256=native_digest(payload) if generic else digest(payload), observed_at=time.time(),
        native_source_sha256=NATIVE_DOM_SHA256, operation_completed=False,
        orders_sent=0, model_calls=0, provider_messages=0, uploads=0,
        history_restore_attempted=False, history_restored=False, status=None,
        stage='BOUND_ROOM_READ', errors=[])
    old_deadline, old_visible = raw.operation_deadline, raw._visible
    with room.lock:
        try:
            raw.operation_deadline = raw.clock() + 25
            raw._visible = room.original_visible
            _gate(actor)
            record = _room_record(room.store.root, operation[:10],allow_sent=generic)
            snapshot=record.pop('_journal_snapshot',None)
            capture['room'] = record
            if snapshot is not None:capture['room_initial_state']=snapshot['state']
            capture['stage'] = 'CURRENT_NATIVE_STATUS'
            # Native maintenance helper cannot click when its predicate is false.
            capture['status'] = raw.status(maintenance_allowed=lambda: False)
            capture['session_observation'] = copy.deepcopy(raw.session_observation())
            capture['initial_page_url'] = raw.page.url
            capture['page_url'] = raw.page.url
            cached = delegated.prompt_cache.read(payload)
            need(cached is not None, 'RECEIVED_ANALYSIS_PROMPT_CACHE_MISSING')
            capture['rendered_prompt_receipt'] = cached
            capture['receipt_files'] = _receipts(room.store.root, payload)
            if generic and not screening:
                from .native_received_continuation import read_manual_contexts
                context_receipts=read_manual_contexts(room.store.root,payload)
                if context_receipts:capture['manual_context_receipts']=context_receipts
            if generic:
                from .native_analysis_material import read_prepared
                capture['prepared_materials'] = [] if screening else read_prepared(room.store.root,payload)
                try:
                    from .native_analysis_receipt import read_reviews
                except ImportError:
                    capture['review_receipts'] = []
                else:
                    capture['review_receipts'] = read_reviews(room.store.root,payload)
            capture['stage'] = 'INITIAL_NATIVE_ROWS'
            rows = room.full_turns(); capture['rows'] = rows
            _save(room.store.root, capture)
            if not rows and capture['status'] == 'CONNECTED':
                restore_state='BOUND'if snapshot is None else snapshot['state']
                capture['history_restore_attempted'] = True; capture['stage'] = restore_state+'_ROOM_RESTORE'
                binding = {key: record[key] for key in ('first_prompt_sha256', 'marker', 'history_titles')}
                if record['identifier_reconciliation'] is not None:
                    binding['identifier_reconciliation'] = record['identifier_reconciliation']
                _gate(actor)
                try:rows = room.restore(record['anchors'], binding, restore_state,
                    prefix_identity=(record['day'],record['room_id']))
                except BaseException:
                    # A history load can expose a challenge or another room.
                    # Retain that actual observation without hiding the refusal.
                    try:
                        capture['status']=raw.status(maintenance_allowed=lambda:False)
                        capture['session_observation']=copy.deepcopy(raw.session_observation())
                        capture['rows']=room.full_turns();capture['page_url']=raw.page.url
                    except Exception:pass
                    raise
                capture['rows'] = rows; capture['history_restored'] = True
                capture['page_url'] = raw.page.url
            _gate(actor); capture['stage'] = 'CURRENT_ATTACHMENT_WITNESSES'
            capture['room_witness'] = _room_witness(record,rows,raw,room.store.root,cached,
                capture['receipt_files'],payload=payload,generic=generic)
            if screening:
                from .native_received_screening import operation_scope
                witness=capture['room_witness'];witness['initial_screening_user_rows']=witness.pop('initial_analysis_user_rows')
                scope=operation_scope(rows,witness,capture.get('review_receipts',[]),payload)
                witness['followup_user_rows']=scope['user_rows'][1:]
            else:scope = _operation_scope(rows,capture['room_witness'],capture['receipt_files'],
                manual_contexts=capture.get('manual_context_receipts',()),payload=payload) if generic else None
            if generic: capture['operation_scope'] = scope
            if generic and not screening:
                from .native_received_continuation import manual_context_tail
                tail=manual_context_tail(capture,payload)
                if tail is not None:capture['manual_context_tail']=tail
            selected = scope['user_rows'] if scope is not None else None
            capture['file_tiles'] = [] if screening else _file_tiles(room, raw, native, rows, capture['receipt_files'],
                prepared=capture.get('prepared_materials',()),indices=selected)
            if (generic and capture['status'] == 'CONNECTED' and scope.get('verified') is True
                    and scope.get('reply_complete') is True):
                rows = _materialize_images(actor, room, raw, native, capture, selected)
            capture['in_process_data_file_deliveries'] = copy.deepcopy(getattr(raw, 'data_file_deliveries', []))
            if not generic:
                capture['room_witness'] = _room_witness(record, rows, raw, room.store.root, cached, capture['receipt_files'])
            need(room.full_turns() == rows and raw.page.url == capture['page_url'],
                 'RECEIVED_ANALYSIS_MESSAGE_SNAPSHOT_CHANGED')
            if generic:
                capture['stable_snapshot_reads'] = 1
                for _ in range(3):
                    _gate(actor); raw.page.wait_for_timeout(250)
                    need(room.full_turns() == rows and raw.page.url == capture['page_url'],
                         'RECEIVED_ANALYSIS_MESSAGE_SNAPSHOT_CHANGED')
                    capture['stable_snapshot_reads'] += 1
                if (snapshot['state']=='SENT' and capture['status']=='CONNECTED'
                        and scope.get('verified')is True and scope.get('reply_complete')is True):
                    _seal_sent_room(actor,room,raw,payload,capture,snapshot,screening=screening)
            _gate(actor); capture['stage'] = 'OBSERVED_WITHOUT_COMPLETION'
        except BaseException as error:
            reason = str(error)
            capture['errors'].append(dict(exception_class=type(error).__name__,
                reason=reason if re.fullmatch('[A-Z][A-Z0-9_]{0,119}', reason) else None,
                stage=capture['stage']))
        finally:
            raw.operation_deadline, raw._visible = old_deadline, old_visible
        capture['finished_at'] = time.time()
        if generic and not screening:
            # A lost HTTP return may hide a positive native NOT_SENT receipt.
            # Expose only the latest independently verified private custody;
            # this passive read cannot admit, replay or complete an operation.
            capture['continuation_not_sent'] = None
            try:
                from .native_received_continuation import read_not_sent
                capture['continuation_not_sent'] = read_not_sent(room.store.root,payload,capture)
            except Exception as error:
                code=str(error)
                capture['continuation_not_sent_error'] = dict(exception_class=type(error).__name__,
                    reason=code if re.fullmatch('[A-Z][A-Z0-9_]{0,119}',code) else None)
        saved = _save(room.store.root, capture)
    rows = capture.get('rows', []); last = rows[-1] if rows else {}
    result = dict(kind=kind, operation=operation, request_sha256=request,
        observed_at=capture['observed_at'], status=capture['status'], stage=capture['stage'],
        operation_completed=False, row_count=len(rows),
        same_loaded_room_verified=capture.get('room_witness', {}).get('same_loaded_room_verified', False),
        last_loaded_row_role=last.get('role'), last_loaded_reply_complete=last.get('complete'),
        last_loaded_reply_sha256=text_sha(last['text']) if type(last.get('text')) is str else None,
        history_restore_attempted=capture['history_restore_attempted'], history_restored=capture['history_restored'],
        error_count=len(capture['errors']), **saved)
    if generic:
        result['observation'] = _project_operation(capture)
        projected = canonical(result['observation'])
        result['observation_sha256'] = hashlib.sha256(projected).hexdigest()
        result['observation_bytes'] = len(projected)
        need(len(canonical(result)) <= 690000, 'RECEIVED_ANALYSIS_PROJECTION_BYTE_LIMIT')
    return result
