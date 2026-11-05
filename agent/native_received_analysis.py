"""Observe an already sent analysis on the existing native page thread.

This bounded incident reader never sends, uploads, calls Mini, completes a
research request, or writes a trading/daily-room journal. Ordinary history may
reopen only the existing BOUND room. All current text is retained privately
before a compact result leaves the actor; no old HOLD becomes a new decision.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import threading
import time

from .native_daily_room import DailyRoom
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
def text_sha(value): return hashlib.sha256(value.encode()).hexdigest()


def need(value, code='RECEIVED_ANALYSIS_OBSERVATION_UNVERIFIED'):
    if not value: raise ValueError(code)


def is_observation(value):
    return type(value) is dict and value.get('action') in (ACTION, GENERIC_ACTION)


def validate(value):
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
            actual.append(dict(alt=image.get_attribute('alt'), visible=image.is_visible(),
                decoded=image.evaluate('(image) => ({complete:image.complete, '
                    'width:image.naturalWidth,height:image.naturalHeight})')))
        witnesses = [dict(names=names, witness=node.evaluate(FILE_WITNESS,
            dict(names=names, bodySelector=native.USER_BODY))) for names in groups]
        output.append(dict(row=index, prompt_sha256=text_sha(row['text']), images=actual,
                           file_tile_witnesses=witnesses))
    return output


def _room_witness(record, rows, raw, root, cached, receipts, *, payload=None, generic=False):
    binding = {key: record[key] for key in ('first_prompt_sha256', 'marker', 'history_titles')}
    if record['identifier_reconciliation'] is not None:
        binding['identifier_reconciliation'] = record['identifier_reconciliation']
    native_room = DailyRoom.matches(rows, record['anchors'], binding, 'BOUND', archive_root=root)
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


def _operation_scope(rows, witness, receipts):
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
    consecutive=users==list(range(start,latest+1,2))
    reply=latest+1 if latest+1<len(rows) else None
    complete=reply is not None and rows[reply].get('role')=='assistant' and rows[reply].get('complete')is True
    chain_complete=sequences==list(range(1,len(sequences)+1))
    verified=bool(witness['same_loaded_room_verified'] and not unmatched and consecutive and chain_complete)
    return dict(verified=verified,initial_user=start,latest_user=latest,user_rows=users,
        reply_row=reply,reply_complete=complete,unmatched_intents=unmatched,
        reason=None if verified else 'OPERATION_TRANSCRIPT_UNVERIFIED')


def _project_operation(capture):
    result=copy.deepcopy(capture);rows=capture.get('rows',[]);scope=capture.get('operation_scope',{})
    start=scope.get('initial_user');end=scope.get('reply_row')
    if start is None:
        result['rows']=[];result['file_tiles']=[];result['row_offset']=None
        return result
    stop=(end+1)if end is not None else scope['latest_user']+1
    result['rows']=copy.deepcopy(rows[start:stop]);result['row_offset']=start
    result['native_history_prefix_sha256']=digest([text_sha(row.get('text',''))for row in rows[:start]])
    result['file_tiles']=[dict(copy.deepcopy(item),row=item['row']-start,global_row=item['row'])
        for item in capture.get('file_tiles',[]) if start<=item['row']<stop]
    result['room_witness']['initial_analysis_user_rows']=[i-start for i in result['room_witness']['initial_analysis_user_rows']]
    result['room_witness']['followup_user_rows']=[i-start for i in result['room_witness']['followup_user_rows']]
    result['operation_scope']={**copy.deepcopy(scope),'initial_user':0,
        'latest_user':scope['latest_user']-start,'user_rows':[i-start for i in scope['user_rows']],
        'reply_row':None if end is None else end-start}
    return result


def observe(actor, delegated, value):
    payload = validate(value); _gate(actor)
    generic = value['action'] == GENERIC_ACTION
    operation, request = payload['operation'], payload['request_sha256']
    kind = GENERIC_KIND if generic else KIND
    from .native_screening_recovery import _native_source_digest, NATIVE_DOM_SHA256, _room_record
    room, raw, native = delegated.daily_room, delegated.bridge, delegated.native
    need(room is not None and room.bridge is raw and room.native is native
         and _native_source_digest(native) == NATIVE_DOM_SHA256)
    capture = dict(schema_version=2 if generic else 1, kind=kind, operation=operation,
        request_sha256=request, payload_sha256=digest(payload), observed_at=time.time(),
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
            record = _room_record(room.store.root, operation[:10]); capture['room'] = record
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
            if generic:
                from .native_analysis_material import read_prepared
                capture['prepared_materials'] = read_prepared(room.store.root,payload)
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
                capture['history_restore_attempted'] = True; capture['stage'] = 'BOUND_ROOM_RESTORE'
                binding = {key: record[key] for key in ('first_prompt_sha256', 'marker', 'history_titles')}
                if record['identifier_reconciliation'] is not None:
                    binding['identifier_reconciliation'] = record['identifier_reconciliation']
                _gate(actor)
                rows = room.restore(record['anchors'], binding, 'BOUND')
                capture['rows'] = rows; capture['history_restored'] = True
                capture['page_url'] = raw.page.url
            _gate(actor); capture['stage'] = 'CURRENT_ATTACHMENT_WITNESSES'
            capture['room_witness'] = _room_witness(record,rows,raw,room.store.root,cached,
                capture['receipt_files'],payload=payload,generic=generic)
            scope = _operation_scope(rows,capture['room_witness'],capture['receipt_files']) if generic else None
            if generic: capture['operation_scope'] = scope
            selected = scope['user_rows'] if scope is not None else None
            capture['file_tiles'] = _file_tiles(room, raw, native, rows, capture['receipt_files'],
                prepared=capture.get('prepared_materials',()),indices=selected)
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
            _gate(actor); capture['stage'] = 'OBSERVED_WITHOUT_COMPLETION'
        except BaseException as error:
            reason = str(error)
            capture['errors'].append(dict(exception_class=type(error).__name__,
                reason=reason if re.fullmatch('[A-Z][A-Z0-9_]{0,119}', reason) else None,
                stage=capture['stage']))
        finally:
            raw.operation_deadline, raw._visible = old_deadline, old_visible
        capture['finished_at'] = time.time()
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
