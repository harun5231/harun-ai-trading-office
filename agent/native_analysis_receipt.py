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


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def prefix(payload):
    label='screening'if payload.get('output_schema',{}).get('required')==['symbols']else'analysis'
    return label+'-review-' + digest({key: payload[key] for key in ('operation', 'request_sha256')}) + '-'


def _need(value):
    if not value:
        raise ValueError('NATIVE_ANALYSIS_REVIEW_UNVERIFIED')


def persist_review(root, payload, request, response, turns, *, context, observed_at,model_operation=None):
    from .dialogue_service import validate_result, screening_model_operation
    screening=payload.get('output_schema',{}).get('required')==['symbols']
    phase='SCREENING'if screening else'ANALYSIS';kind=SCREENING_KIND if screening else KIND
    _need(request.get('phase') == phase and (context==payload.get('context') if screening else type(context) is dict)
        and type(observed_at) in (int, float) and math.isfinite(observed_at) and observed_at > 0
        and type(request.get('request_id')) is str
        and re.fullmatch('[a-f0-9]{64}', request['request_id']) is not None)
    validate_result(response, request)
    _need(type(turns) is list and turns and turns[-1].get('role') == 'assistant'
        and turns[-1].get('text') == request['reply']
        and hashlib.sha256(request['reply'].encode()).hexdigest() == request['reply_sha256'])
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
            expected=SCREENING_KIND if payload.get('output_schema',{}).get('required')==['symbols']else KIND
            _need(value.get('kind') == expected and value.get('schema_version') == 1
                and value.get('operation') == payload['operation']
                and value.get('request_sha256') == payload['request_sha256']
                and value.get('payload_sha256') == digest(payload)
                and name == prefix(payload) + value.get('request_id', '') + '.json')
            result.append(dict(name=name, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), value=value))
        return result
    finally:
        os.close(directory)
