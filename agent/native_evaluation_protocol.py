"""Add historical evaluation to the EXISTING native journal/DOM protocol.

This namespace cannot become a setup or pending-order decision. It uses the
same provider, DailyRoom and once-admission as research; it has no exchange API.
"""
from __future__ import annotations

import functools
import hashlib
import importlib
import json
import math
from pathlib import Path
import re

from worker.trade_evaluation_evidence import (EVALUATION_SCHEMA, PROTOCOL,
    digest, need, parse_evaluation_reply)

PHASE = 'POST_TRADE_EVALUATION'
PREFIX = 'post-trade-evaluation:'
_INSTALLED = '__harun_historical_evaluation_v1__'


def is_evaluation_schema(schema):
    return type(schema) is dict and schema == EVALUATION_SCHEMA


def envelope(payload):
    """Validate internal input binding; this alone never proves delivery."""
    need(type(payload) is dict and is_evaluation_schema(payload.get('output_schema')),
        'EVALUATION_SCHEMA_UNVERIFIED')
    context = payload.get('context')
    need(type(context) is dict and set(context) == {'historical_evaluation'},
        'EVALUATION_CONTEXT_UNVERIFIED')
    value = context['historical_evaluation']
    need(type(value) is dict and set(value) == {'request', 'request_sha256', 'reserved_at'},
        'EVALUATION_CONTEXT_UNVERIFIED')
    request = value['request']
    need(type(request) is dict and request.get('protocol') == PROTOCOL
        and request.get('phase') == PHASE and request.get('trading_authority') is False
        and request.get('output_schema') == EVALUATION_SCHEMA
        and type(request.get('input_evidence_sha256')) is str
        and re.fullmatch('[a-f0-9]{64}', request['input_evidence_sha256'])
        and request.get('operation') == PREFIX + request['input_evidence_sha256'][:32]
        and payload.get('operation') == request['operation']
        and type(request.get('room_id')) is str and re.fullmatch('[a-f0-9]{32}', request['room_id'])
        and request.get('prompt') == payload.get('prompt')
        and value['request_sha256'] == digest(request)
        and type(value['reserved_at']) in (int, float) and math.isfinite(value['reserved_at'])
        and value['reserved_at'] > 0,
        'EVALUATION_INPUT_BINDING_UNVERIFIED')
    materials = request.get('historical_materials')
    replies = request.get('source_neurobro_replies')
    need(type(materials) is list and materials and type(replies) is list
        and len(materials) == len(replies), 'EVALUATION_HISTORICAL_MATERIAL_REQUIRED')
    for material, reply in zip(materials, replies):
        need(type(material) is dict and type(material.get('files')) is list
            and {(r.get('kind'), r.get('timeframe')) for r in material['files']}
                == {(k, tf) for k in ('PNG', 'CSV') for tf in ('15m', '1h')}
            and len(material['files']) == 4 and type(material.get('delivery_custody')) is dict
            and type(reply) is dict and type(reply.get('text')) is str
            and hashlib.sha256(reply['text'].encode()).hexdigest() == reply.get('sha256')
            and reply['sha256'] == material['delivery_custody'].get('assistant_sha256'),
            'EVALUATION_HISTORICAL_MATERIAL_REQUIRED')
    return value


def native_arguments(intent):
    request = intent['request']
    value = dict(operation=request['operation'], prompt=request['prompt'],
        output_schema=request['output_schema'], context={'historical_evaluation': {
            'request': request, 'request_sha256': intent['request_sha256'],
            'reserved_at': intent['reserved_at']}})
    envelope(value)
    return value


def verify_evaluation_dispatch(db, operation, marker, request_sha256, today, now):
    """Read-only pre-Send check, beside SCREENING/ANALYSIS/pending admission."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from worker.trade_evaluation import read_evaluation_claim
        from worker.research_provider import request_body, marker as marker_for
        if (type(now) not in (int, float) or not math.isfinite(now) or now <= 0
                or today != datetime.fromtimestamp(now, ZoneInfo('Asia/Jakarta')).date().isoformat()
                or marker != marker_for(operation)):
            return False
        path = next(row[2] for row in db.execute('PRAGMA database_list') if row[1] == 'main')
        if not path or not Path(path).is_absolute(): return False
        claim = read_evaluation_claim(Path(path).parent, operation)
        if claim is None: return False
        body = native_arguments(claim)
        rebuilt, serialized, wire_sha = request_body(operation, body['prompt'], body['output_schema'], body['context'])
        row = db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone()
        source = db.execute('SELECT * FROM research_browser_requests WHERE operation=?', (operation,)).fetchone()
        return bool(row and source and row['state'] == 'PENDING' and row['attempts'] == 1
            and row['output'] is None and row['idempotency'] is None
            and claim['reserved_at'] <= row['created'] <= now
            and 0 <= now - row['created'] <= 900
            and source['provider'] == 'NEUROBRO_WEB'
            and source['contract_version'] == 'neurobro-browser-decision-v2'
            and source['marker'] == marker and source['request_body'] == serialized
            and source['request_sha256'] == row['body_hash'] == wire_sha == request_sha256
            and not db.execute('SELECT 1 FROM research_browser_responses WHERE operation=?', (operation,)).fetchone())
    except Exception:
        return False


def _canonical(value, exception):
    try:
        need(type(value) is dict and set(value) == {'assessment', 'findings'}
            and parse_evaluation_reply(value['findings']) == value, 'EVALUATION_REPLY_INVALID')
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    except Exception:
        raise exception('INVALID_EVALUATION_OUTPUT') from None


def _install_canonical(namespace):
    name = namespace['__name__'].split('.')[0] + '.neuroapi'
    api = importlib.import_module(name)
    original = api.canonical_output
    if not getattr(original, _INSTALLED, False):
        @functools.wraps(original)
        def canonical(value, schema, catalog=None):
            if is_evaluation_schema(schema): return _canonical(value, namespace['Review'])
            return original(value, schema, catalog)
        setattr(canonical, _INSTALLED, True)
        api.canonical_output = canonical
    if 'canonical_output' in namespace: namespace['canonical_output'] = api.canonical_output


def install_worker_protocol(namespace):
    _install_canonical(namespace)
    previous = namespace['verify_dispatch']
    if getattr(previous, _INSTALLED, False): return
    @functools.wraps(previous)
    def verify(db, operation, marker, request_sha256, today, now, *, max_age=180):
        if type(operation) is str and operation.startswith(PREFIX):
            return verify_evaluation_dispatch(db, operation, marker, request_sha256, today, now)
        return previous(db, operation, marker, request_sha256, today, now, max_age=max_age)
    setattr(verify, _INSTALLED, True)
    namespace['verify_dispatch'] = verify


def install_source_protocol(namespace):
    _install_canonical(namespace)
    previous = namespace['validate_chart_evidence']
    if getattr(previous, _INSTALLED, False): return
    @functools.wraps(previous)
    def validate(value, schema, context, created, received_at, max_age):
        if not is_evaluation_schema(schema):
            return previous(value, schema, context, created, received_at, max_age)
        try:
            request = context['historical_evaluation']['request']
            envelope(dict(operation=request['operation'], prompt=request['prompt'],
                output_schema=schema, context=context))
            need(value == [] and type(value) is list and created > 0 and received_at >= created,
                'EVALUATION_CHART_EVIDENCE_INVALID')
            # These are historical references in the SAME verified room, not
            # newly uploaded charts or an assertion of live market freshness.
            return value
        except Exception:
            raise namespace['Review']('INVALID_EVALUATION_EVIDENCE') from None
    setattr(validate, _INSTALLED, True)
    namespace['validate_chart_evidence'] = validate


def install_dom_protocol(namespace):
    previous = namespace['request_kind']
    if getattr(previous, _INSTALLED, False): return
    @functools.wraps(previous)
    def request_kind(payload, artifacts):
        if not is_evaluation_schema(payload.get('output_schema')):
            return previous(payload, artifacts)
        try:
            envelope(payload)
            need(type(artifacts) is list and not artifacts, 'EVALUATION_MATERIAL_MODE_INVALID')
            return PHASE
        except Exception:
            raise namespace['NotSubmitted']('UNAVAILABLE') from None
    setattr(request_kind, _INSTALLED, True)
    namespace['request_kind'] = request_kind
    render = namespace['user_prompt']
    @functools.wraps(render)
    def prompt(payload):
        if not is_evaluation_schema(payload.get('output_schema')): return render(payload)
        envelope(payload)
        return (payload['prompt'] + '\n\nAkhiri PENILAIAN SETUP: SESUAI BUKTI, '
            'TIDAK SESUAI BUKTI, atau BELUM CUKUP BUKTI, beserta alasan. '
            'Ini evaluasi historis; bukan setup atau instruksi transaksi baru.\n'
            'Acuan percakapan: ' + payload['marker'])
    namespace['user_prompt'] = prompt
