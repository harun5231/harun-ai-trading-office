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


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def prefix(payload):
    return 'analysis-review-' + digest({key: payload[key] for key in ('operation', 'request_sha256')}) + '-'


def _need(value):
    if not value:
        raise ValueError('NATIVE_ANALYSIS_REVIEW_UNVERIFIED')


def persist_review(root, payload, request, response, turns, *, context, observed_at):
    from .dialogue_service import validate_result
    _need(request.get('phase') == 'ANALYSIS' and type(context) is dict
        and type(observed_at) in (int, float) and math.isfinite(observed_at) and observed_at > 0
        and type(request.get('request_id')) is str
        and re.fullmatch('[a-f0-9]{64}', request['request_id']) is not None)
    validate_result(response, request)
    _need(type(turns) is list and turns and turns[-1].get('role') == 'assistant'
        and turns[-1].get('text') == request['reply']
        and hashlib.sha256(request['reply'].encode()).hexdigest() == request['reply_sha256'])
    transcript = digest(turns)
    expected = digest(dict(operation=payload['operation'], request_sha256=payload['request_sha256'],
        reply_sha256=request['reply_sha256'], phase='ANALYSIS', transcript_sha256=transcript))
    _need(expected == request['request_id'])
    value = dict(schema_version=1, kind=KIND, operation=payload['operation'],
        request_sha256=payload['request_sha256'], payload_sha256=digest(payload),
        request_id=request['request_id'], reply_sha256=request['reply_sha256'],
        context_sha256=digest(context), conversation_id=request['conversation_id'], phase='ANALYSIS',
        request=copy.deepcopy(request), response=copy.deepcopy(response), transcript_sha256=transcript, observed_at=observed_at)
    name = prefix(payload) + request['request_id'] + '.json'
    raw = canonical(value)
    _need(len(raw) <= MAX_REVIEW_BYTES)
    directory = _directory(root)
    try:
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        except FileExistsError:
            existing = json.loads(_private_file(directory, name, MAX_REVIEW_BYTES))
            _need({key: child for key, child in existing.items() if key != 'observed_at'}
                == {key: child for key, child in value.items() if key != 'observed_at'})
            return existing
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.fsync(directory)
        return value
    finally:
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
            _need(value.get('kind') == KIND and value.get('schema_version') == 1
                and value.get('operation') == payload['operation']
                and value.get('request_sha256') == payload['request_sha256']
                and value.get('payload_sha256') == digest(payload)
                and name == prefix(payload) + value.get('request_id', '') + '.json')
            result.append(dict(name=name, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), value=value))
        return result
    finally:
        os.close(directory)
