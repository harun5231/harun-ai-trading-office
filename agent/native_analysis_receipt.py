"""Immutable actual dialogue reviews for recovery of the same native operation.

A prepared/uploaded material receipt is not proof of Send or model approval.
Only this hook's validated, unchanged transcript can create a review receipt.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import os
import re
from .native_chart_followup import _directory, _private_file, canonical

MAX_REVIEW_BYTES = 512 * 1024
MAX_REVIEWS_BYTES = 16 * 1024 * 1024
KIND = 'NATIVE_ANALYSIS_MINI_REVIEW_V1'
SCREENING_KIND = 'NATIVE_SCREENING_MINI_REVIEW_V1'
PENDING_KIND = 'NATIVE_PENDING_REVIEW_MINI_REVIEW_V1'


def _phase_kind(payload):
    from .native_pending_protocol import is_pending_schema
    if payload.get('output_schema',{}).get('required')==['symbols']:
        return 'SCREENING',SCREENING_KIND,'screening'
    if is_pending_schema(payload.get('output_schema')):
        return 'PENDING_REVIEW',PENDING_KIND,'pending'
    return 'ANALYSIS',KIND,'analysis'


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def prefix(payload):
    _,_,label=_phase_kind(payload)
    return label+'-review-' + digest({key: payload[key] for key in ('operation', 'request_sha256')}) + '-'


def _need(value):
    if not value:
        raise ValueError('NATIVE_ANALYSIS_REVIEW_UNVERIFIED')


def persist_review(root, payload, request, response, turns, *, context, observed_at,model_operation=None):
    from .dialogue_service import (validate_result, screening_model_operation,
        validate_conversation_context, dialogue_operation_memo)
    phase,kind,_=_phase_kind(payload);screening=phase=='SCREENING'
    _need(request.get('phase') == phase and (context==payload.get('context') if screening else type(context) is dict)
        and type(observed_at) in (int, float) and math.isfinite(observed_at) and observed_at > 0
        and type(request.get('request_id')) is str
        and re.fullmatch('[a-f0-9]{64}', request['request_id']) is not None)
    validate_result(response, request)
    _need(type(turns) is list and turns and turns[-1].get('role') == 'assistant'
        and turns[-1].get('text') == request['reply']
        and hashlib.sha256(request['reply'].encode()).hexdigest() == request['reply_sha256'])
    bound_turns=validate_conversation_context(request)
    if bound_turns is not None:
        evidence=request['conversation_context']
        _need(bound_turns==turns and evidence['native_operation']==payload['operation']
            and evidence['native_request_sha256']==payload['request_sha256'])
    transcript = digest(turns)
    expected = digest(dict(operation=payload['operation'], request_sha256=payload['request_sha256'],
        reply_sha256=request['reply_sha256'], phase=phase, transcript_sha256=transcript))
    _need(expected == request['request_id'])
    value = dict(schema_version=1, kind=kind, operation=payload['operation'],
        request_sha256=payload['request_sha256'], payload_sha256=digest(payload),
        request_id=request['request_id'], reply_sha256=request['reply_sha256'],
        context_sha256=digest(context), conversation_id=request['conversation_id'], phase=phase,
        request=copy.deepcopy(request), response=copy.deepcopy(response), transcript_sha256=transcript, observed_at=observed_at)
    if screening:
        if model_operation is not None:
            _need(set(model_operation)=={'operation_id','kind','request_hash','state','result','updated_at'}
                and screening_model_operation(model_operation['operation_id'],request['request_id'])
                and model_operation['kind']=='DELEGATED_MODEL_DIALOGUE' and model_operation['state']=='DONE'
                and type(model_operation['request_hash'])is str and re.fullmatch('[a-f0-9]{64}',model_operation['request_hash'])
                and json.loads(model_operation['result'])==response
                and type(model_operation['updated_at'])in(int,float)and 0<model_operation['updated_at']<=observed_at)
        value['model_operation']=copy.deepcopy(model_operation)
    name = prefix(payload) + request['request_id'] + '.json'
    raw = canonical(value)
    _need(len(raw) <= MAX_REVIEW_BYTES)
    directory = _directory(root)
    temporary = '.analysis-review-' + os.urandom(16).hex() + '.tmp'
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.fsync(directory)
        except FileExistsError:
            pass
        os.unlink(temporary, dir_fd=directory)
        existing = json.loads(_private_file(directory, name, MAX_REVIEW_BYTES))
        def identity(record):
            copied = copy.deepcopy(record); copied.pop('observed_at', None)
            # A cached result for the same exact native transcript may be read
            # again with a new technical call deadline. Keep the first receipt.
            # Optional WEB context is already bound by this same native ID;
            # validate it before comparing with a legacy receipt without it.
            copied['request']=dialogue_operation_memo(copied['request'])
            copied['request'].pop('deadline_epoch', None)
            return copied
        _need(identity(existing) == identity(value))
        return existing
    finally:
        try: os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError: pass
        os.close(directory)


def read_reviews(root, payload):
    directory = _directory(root)
    try:
        result = []; total = 0
        for name in sorted(os.listdir(directory)):
            if not name.startswith(prefix(payload)):
                continue
            _need(re.fullmatch(re.escape(prefix(payload)) + '[a-f0-9]{64}\\.json', name) is not None)
            raw = _private_file(directory, name, MAX_REVIEW_BYTES)
            total += len(raw); _need(total <= MAX_REVIEWS_BYTES)
            value = json.loads(raw)
            _,expected,_=_phase_kind(payload)
            _need(value.get('kind') == expected and value.get('schema_version') == 1
                and value.get('operation') == payload['operation']
                and value.get('request_sha256') == payload['request_sha256']
                and value.get('payload_sha256') == digest(payload)
                and name == prefix(payload) + value.get('request_id', '') + '.json')
            result.append(dict(name=name, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), value=value))
        return result
    finally:
        os.close(directory)


def question_was_answered(payload,turns,*,context,rendered_question,intents):
    """Recognize an unchanged completed WEB pair, never authorize a result.

    A material INTENT alone is not Send proof. The actual complete user/answer
    pair must still be present with its exact source-bound prefix and context.
    """
    if type(turns)is not list or len(turns)<2 or len(turns)%2:return False
    answer=turns[-1].get('text');user_role=turns[0].get('role');assistant_role=turns[1].get('role')
    if user_role==assistant_role or turns[-1].get('complete')is not True:return False
    context_sha=digest(context);payload_sha=digest(payload)
    phase,_,_=_phase_kind(payload)
    for index in range(0,len(turns),2):
        user,reply=turns[index:index+2]
        if not(user.get('role')==user_role and reply.get('role')==assistant_role
                and user.get('text')==rendered_question and reply.get('complete')is True
                and reply.get('text')==answer):continue
        if phase=='SCREENING':return True
        if index==0:
            if context_sha==digest(payload.get('context')):return True
            continue
        matches=[intent for intent in intents if intent.get('kind')=='NATIVE_200_CHART_FOLLOWUP'
            and intent.get('operation')==payload['operation']
            and intent.get('request_sha256')==payload['request_sha256']
            and intent.get('original_payload_sha256')==payload_sha
            and intent.get('followup_prompt_sha256')==hashlib.sha256(user['text'].encode()).hexdigest()
            and intent.get('followup_context_sha256')==context_sha
            and intent.get('initial_reply_sha256')==hashlib.sha256(turns[index-1]['text'].encode()).hexdigest()]
        if not matches:continue
        _need(len(matches)==1)
        if matches[0].get('initial_turns_sha256')!=digest(turns[:index]):
            # A rerendered UUID is not permission to invent an old request ID.
            raise ValueError('NATIVE_DIALOGUE_REVIEW_LINEAGE_UNVERIFIED')
        return True
    return False


def refuse_reviewed_no_progress(root,payload,turns,*,context,intents,render_question,conversation_id=None,
        parsed_setup=False):
    """Avoid another paid interpretation of a proven redundant clarification.

    First-generation legacy receipts remain readable. UNKNOWN has no accepted
    sidecar and is left to the existing provider reconciliation, never retried.
    """
    from .dialogue_service import (DialogueError,validate_result,validate_conversation_context,
        validate_web_question)
    phase,_,_=_phase_kind(payload)
    for entry in read_reviews(root,payload):
        value=entry['value'];request=value.get('request',{});response=value.get('response',{})
        if not(value.get('phase')==phase and value.get('context_sha256')==digest(context)
                and (conversation_id is None or value.get('conversation_id')==conversation_id)
                and request.get('reply')==turns[-1].get('text')):continue
        validate_result(response,request)
        prior=validate_conversation_context(request)
        transcript=value.get('transcript_sha256')
        expected=digest(dict(operation=payload['operation'],request_sha256=payload['request_sha256'],
            reply_sha256=value.get('reply_sha256'),phase=phase,transcript_sha256=transcript))
        _need(request.get('request_id')==value.get('request_id')==expected
            and request.get('phase')==phase and request.get('reply_sha256')==value.get('reply_sha256')
            and request.get('conversation_id')==value.get('conversation_id')
            and value.get('reply_sha256')==hashlib.sha256(request['reply'].encode()).hexdigest()
            and (prior is None or digest(prior)==transcript))
        decision=response['decision']
        if not(response['needs_followup']and decision['action']=='CLARIFY'):continue
        same=transcript==digest(turns)
        if not same:
            # Only detect an unresolved UUID-only lineage, never alias it into
            # a new positive receipt or replay the old interpretation.
            semantic=lambda rows:[{key:item for key,item in row.items()if key!='id'}for row in rows]
            extends_prior = (parsed_setup and prior is not None and len(turns)>len(prior)
                and turns[:len(prior)]==prior)
            if not extends_prior and (prior is None or semantic(prior)!=semantic(turns)):continue
        try:question=validate_web_question(decision['question'],request)
        except DialogueError:continue  # Historical private questions are never new WEB authority.
        rendered=render_question(question)
        if question_was_answered(payload,turns,context=context,rendered_question=rendered,intents=intents):
            if not same and not extends_prior:
                raise ValueError('NATIVE_DIALOGUE_REVIEW_LINEAGE_UNVERIFIED')
            raise ValueError('NEUROBRO_CLARIFICATION_NO_PROGRESS')
