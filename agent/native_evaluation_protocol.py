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

from worker.trade_evaluation_evidence import (EVALUATION_SCHEMA, PROTOCOL, INITIAL_CUSTODY,
    digest, need, parse_evaluation_reply)

PHASE = 'POST_TRADE_EVALUATION'
PREFIX = 'post-trade-evaluation:'
_INSTALLED = '__harun_historical_evaluation_v1__'


def native_sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',', ':'),
        ensure_ascii=True,allow_nan=False).encode('ascii')).hexdigest()


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
            need(type(value) is list and created > 0 and received_at >= created,
                'EVALUATION_CHART_EVIDENCE_INVALID')
            expected=[r for m in request['historical_materials'] for r in m['files'] if r['kind']=='PNG']
            for row in value:
                need(type(row) is dict and set(row)=={'source','symbol','timeframe','captured_at','sha256','bytes','attachment_id'}
                    and any(all(row[k]==r[k] for k in ('source','timeframe','captured_at','sha256','bytes')) for r in expected)
                    and type(row['attachment_id']) is str and re.fullmatch('[A-Za-z0-9_.:-]{1,160}',row['attachment_id']),
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
            need(type(artifacts) is list and len(artifacts) in (0,2), 'EVALUATION_MATERIAL_MODE_INVALID')
            expected=[r for m in payload['context']['historical_evaluation']['request']['historical_materials']
                for r in m['files'] if r['kind']=='PNG']
            for item in artifacts:
                need(type(item) in (tuple,list) and len(item)==3 and type(item[1]) is bytes
                    and type(item[2]) is dict and set(item[2])=={'source','symbol','timeframe','captured_at','sha256','bytes'}
                    and hashlib.sha256(item[1]).hexdigest()==item[2]['sha256'] and len(item[1])==item[2]['bytes']
                    and any(all(item[2][k]==r[k] for k in ('source','timeframe','captured_at','sha256','bytes')) for r in expected),
                    'EVALUATION_MATERIAL_MODE_INVALID')
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


def validate_material_access_chain(request, chain, chart_evidence, *, final_response):
    """Every historical input must have actual reuse or re-delivery custody.

    PREPARE_MATERIAL is durable before upload. SEND_MATERIAL retains the actual
    returned upload IDs before Send; neither overwrites the earlier intent.
    Unknown/truncated chains cannot satisfy this final receipt projection.
    """
    expected={digest(value):value for value in request['historical_materials']}
    need(len(expected)==len(request['historical_materials']), 'EVALUATION_DUPLICATE_MATERIAL_INPUT')
    covered={};prepared=None;delivered=[];sends=0;last_ui=None;final=None;seen_ui=set()
    for index,record in enumerate(chain):
        intent=record['intent'];action=intent.get('action');complete=record.get('complete')
        need(type(complete) is dict and complete==dict(intent,intent_sha256=native_sha(intent),
            response_id=complete.get('response_id'),final_reply_sha256=complete.get('final_reply_sha256'))
            and type(complete.get('response_id')) is str and re.fullmatch('[A-Za-z0-9_.:-]{1,160}',complete['response_id'])
            and type(complete.get('final_reply_sha256')) is str and re.fullmatch('[a-f0-9]{64}',complete['final_reply_sha256']),
            'EVALUATION_REPLY_RECEIPT_UNVERIFIED')
        if action=='FINAL_ASSESSMENT':
            need(index==len(chain)-1 and prepared is None and last_ui is not None
                and intent.get('response_id_kind')=='NATIVE_FINAL_RESPONSE_ID'
                and intent.get('source_chain_sha256')==digest(chain[:-1])
                and intent.get('final_ui_message_id')==last_ui[0]
                and intent.get('final_ui_reply_sha256')==last_ui[1]
                and complete['response_id']==final_response['response_id']
                and complete['final_reply_sha256']==final_response['assistant_sha256']==last_ui[1],
                'EVALUATION_FINAL_RESPONSE_UNVERIFIED')
            final=record;continue
        need(intent.get('response_id_kind')=='NATIVE_UI_MESSAGE_ID'
            and action in {'PREPARE_MATERIAL','SEND_MATERIAL','SEND_TEXT'},
            'EVALUATION_MATERIAL_RECEIPT_INVALID')
        for witness in intent.get('reused_materials',[]):
            key=witness.get('material_sha256');material=expected.get(key)
            need(material is not None and key not in covered
                and (witness.get('native_review_sha256')==material['delivery_custody'].get('native_review_sha256')
                    and type(witness.get('native_review_sha256'))is str
                    if material['delivery_custody'].get('kind')==INITIAL_CUSTODY else
                    witness.get('native_completion_sha256')==material['delivery_custody']['native_completion_sha256'])
                and witness.get('assistant_sha256')==material['delivery_custody']['assistant_sha256']
                and type(witness.get('attachment_names')) is list and len(witness['attachment_names'])==4
                and len(set(witness['attachment_names']))==4,
                'EVALUATION_REUSED_MATERIAL_MISMATCH')
            covered[key]='REUSED'
        key=intent.get('material_sha256')
        if action=='PREPARE_MATERIAL':
            need(prepared is None and key in expected and key not in covered,
                'EVALUATION_MATERIAL_PREPARE_MISMATCH')
            planned=intent.get('planned_files')
            need(type(planned) is list and sorted(planned,key=lambda r:(r['kind'],r['timeframe']))
                == sorted([{k:r[k] for k in ('kind','timeframe','sha256','bytes')}
                    for r in expected[key]['files']],key=lambda r:(r['kind'],r['timeframe'])),
                'EVALUATION_MATERIAL_PREPARE_MISMATCH')
            prepared=(key,native_sha(intent),(complete['response_id'],complete['final_reply_sha256']))
            continue
        sends+=1
        last_ui=(complete['response_id'],complete['final_reply_sha256'])
        need(last_ui[0] not in seen_ui,'EVALUATION_DUPLICATE_UI_RESPONSE')
        seen_ui.add(last_ui[0])
        need(type(intent.get('rendered_prompt_sha256')) is str
            and re.fullmatch('[a-f0-9]{64}',intent['rendered_prompt_sha256']),
            'EVALUATION_MATERIAL_TURN_UNVERIFIED')
        if action=='SEND_TEXT':
            need(prepared is None and key is None and not intent.get('bound_charts')
                and intent.get('attachment_names')==[], 'EVALUATION_TEXT_TURN_INVALID')
            continue
        need(prepared==(key,intent.get('prepared_intent_sha256'),last_ui) and key not in covered,
            'EVALUATION_MATERIAL_SEND_MISMATCH')
        material=expected[key];pngs={r['timeframe']:r for r in material['files'] if r['kind']=='PNG'}
        csvs={r['timeframe']:r for r in material['files'] if r['kind']=='CSV'}
        bound=intent.get('bound_charts');names=intent.get('attachment_names')
        need(type(bound) is list and len(bound)==2 and [r.get('timeframe') for r in bound]==['15m','1h']
            and type(names) is list and len(names)==len(set(names))==4,
            'EVALUATION_MATERIAL_SEND_MISMATCH')
        for row in bound:
            png=pngs[row['timeframe']]
            need(set(row)=={'source','symbol','timeframe','captured_at','sha256','bytes','attachment_id'}
                and all(row[k]==png[k] for k in ('source','captured_at','sha256','bytes'))
                and type(row['attachment_id']) is str
                and re.fullmatch('[A-Za-z0-9_.:-]{1,160}',row['attachment_id']),
                'EVALUATION_MATERIAL_SEND_MISMATCH')
        expected_names=[intent['marker']+'-'+row['timeframe']+'-'+row['sha256'][:12]+'.png' for row in bound]
        expected_names.extend(csvs[tf]['name'] for tf in ('15m','1h'))
        need(names==expected_names,'EVALUATION_MATERIAL_NAMES_MISMATCH')
        delivered.extend(bound);covered[key]='DELIVERED';prepared=None
    need(final is not None and prepared is None and sends>0 and set(covered)==set(expected)
        and chart_evidence==delivered,'EVALUATION_HISTORICAL_MATERIAL_INCOMPLETE')
    return dict(materials=covered,send_count=sends,delivered_chart_evidence=delivered)
