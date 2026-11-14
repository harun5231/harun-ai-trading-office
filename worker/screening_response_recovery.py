"""Complete a received screening receipt and resume its existing native job.

The caller authenticates private browser/Mini evidence and holds the existing
cycle lock. This function never asks a provider, ticks a coordinator, touches
an order, or rewrites the request clock. Native response/source validation
remains authoritative; only the blocked research job/cycle can be resumed.
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import math
import re
import time
from zoneinfo import ZoneInfo

from .core import Review
from .account_state import screening_contract
from .neuroapi import canonical_output, selections


def _need(value):
    if not value:
        raise Review('SCREENING_RESPONSE_RECOVERY_UNVERIFIED')


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _row(db, table, key, value):
    row = db.execute('SELECT * FROM ' + table + ' WHERE ' + key + '=?', (value,)).fetchone()
    return dict(row) if row is not None else None


def _snapshot(db, operation, cycle):
    return {name: _row(db, name, key, value) for name, key, value in (
        ('api_requests', 'operation', operation), ('robot_jobs', 'operation', operation),
        ('research_browser_requests', 'operation', operation),
        ('research_browser_responses', 'operation', operation),
        ('robot_cycles', 'id', cycle), ('robot_settings', 'id', 1))}


def _proof(proof, payload):
    """Check the browser's private attestation without browser-only imports."""
    from agent.neurobro_reply import parse_reply_for_payload
    fields = {'schema_version', 'kind', 'payload_sha256', 'room', 'rows',
        'rendered_prompt', 'initial_reply_sha256', 'final_reply_sha256',
        'native_source_sha256', 'native_received_at', 'stable_reads', 'native_poll_reads', 'native_result',
        'history_restore_attempted', 'history_restored', 'identity_reconciliation'}
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    _need(type(proof) is dict and set(proof) == fields and type(proof['schema_version']) is int
        and proof['schema_version'] == 1 and proof['kind'] == 'NATIVE_SCREENING_COMPLETION_RECOVERY_V1'
        and proof['payload_sha256'] == _sha(encoded)
        and proof['native_source_sha256'] == '30ea13cd51706b1db65620cdfb5f8db6dec1c9b3b8a20c9b7ca1d6c9db07f72e'
        and type(proof['stable_reads']) is int and proof['stable_reads'] == 4
        and type(proof['native_poll_reads']) is int and proof['native_poll_reads'] >= 4
        and type(proof['history_restore_attempted']) is bool
        and proof['history_restored'] is proof['history_restore_attempted'])
    room, rows, rendered, result = (proof[key] for key in ('room', 'rows', 'rendered_prompt', 'native_result'))
    _need(type(room) is dict and set(room) == {'day', 'room_id', 'url', 'updated_at',
        'anchors', 'first_prompt_sha256', 'marker', 'history_titles', 'identifier_reconciliation'}
        and room['day'] == payload['operation'][:10]
        and type(room['room_id']) is str and re.fullmatch('[a-f0-9]{32}', room['room_id'])
        and type(room['url']) is str and room['url'].startswith('https://app.neurobro.ai/')
        and room['marker'] == payload['marker'] and type(rendered) is str
        and type(room['history_titles']) is list
        and all(type(title) is str and title for title in room['history_titles'])
        and rendered.count(payload['marker']) == 1 and payload['prompt'] in rendered
        and room['first_prompt_sha256'] == _sha(rendered.replace('\r\n', '\n').strip())
        and type(rows) is list and len(rows) == 4)
    for index, row in enumerate(rows):
        _need(type(row) is dict and row.get('role') == ('user' if index % 2 == 0 else 'assistant')
            and type(row.get('text')) is str and row['text'] and row.get('attachments', []) == [])
        if index % 2:
            _need(row.get('complete') is True and type(row.get('id')) is str
                and re.fullmatch('[A-Za-z0-9_.:-]{1,160}', row['id']))
    anchors = [_sha(json.dumps({key: value for key, value in row.items() if key != 'attachments'},
        sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)) for row in rows]
    count = int(payload['operation'][-1])
    clarification = ('Mohon ringkas ' + str(count) + ' coin pilihanmu sesuai kapasitas '
        'yang diminta dalam bentuk COIN: simbol USDT Perpetual. '
        'Jangan sertakan coin pembanding sebagai pilihan.')
    _need(rows[1]['id'] != rows[3]['id']
        and rows[0]['text'].replace('\r\n', '\n').strip() == rendered.replace('\r\n', '\n').strip()
        and rows[2]['text'].replace('\r\n', '\n').strip() == clarification
        and proof['initial_reply_sha256'] == _sha(rows[1]['text'])
        and proof['final_reply_sha256'] == _sha(rows[3]['text'])
        and type(room['updated_at']) in (int, float) and math.isfinite(room['updated_at'])
        and type(proof['native_received_at']) in (int, float) and math.isfinite(proof['native_received_at'])
        and 0 < room['updated_at'] <= proof['native_received_at'])
    output = parse_reply_for_payload(rows[1]['text'], payload)
    _need(parse_reply_for_payload(rows[3]['text'], payload) == output
        and type(result) is dict and set(result) == {'operation', 'marker', 'request_sha256',
            'output', 'assistant_text', 'response_id', 'chart_evidence'}
        and all(result[key] == payload[key] for key in ('operation', 'marker', 'request_sha256'))
        and result['output'] == output and result['assistant_text'] == rows[3]['text']
        and result['chart_evidence'] == []
        and result['response_id'] == 'dom-' + _sha(room['url'] + '\n' + payload['marker'] + '\n' + rows[3]['text']))
    reconciled = proof['identity_reconciliation']
    if reconciled is None:
        _need(room['identifier_reconciliation'] is None and room['anchors'] == anchors)
        return None
    from agent.native_screening_identity import validate_identity_receipt
    _need(type(reconciled) is dict and set(reconciled) == {'alias', 'receipt'})
    alias = validate_identity_receipt(reconciled['receipt'], payload, room, rows)
    _need(alias == reconciled['alias'] == room['identifier_reconciliation'])
    return alias


def _mini_selection(done, proof, payload, identity_alias=None):
    _need(type(done) is dict and set(done) == {'operation_id', 'kind', 'request_hash',
        'state', 'result', 'updated_at'} and done['kind'] == 'DELEGATED_MODEL_DIALOGUE'
        and done['state'] == 'DONE' and type(done['result']) is str
        and type(done['request_hash']) is str and re.fullmatch('[a-f0-9]{64}', done['request_hash']))
    turns = proof['rows'][:2]
    # Exactly the identity authored by native_browser_hook.review(), including
    # the original native message IDs and UTF-8 text through its ASCII digest.
    transcript = _sha(json.dumps(turns, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False))
    request_id = _sha(json.dumps(dict(operation=payload['operation'],
        request_sha256=payload['request_sha256'], reply_sha256=proof['initial_reply_sha256'],
        phase='SCREENING', transcript_sha256=transcript), sort_keys=True, separators=(',', ':')))
    if identity_alias is not None:
        # Reloaded UI IDs are accepted only through the authenticated, durable
        # old-to-current identity receipt. The original DONE request identity
        # and full Mini row stay intact, including its original request hash.
        _need(identity_alias['current_request_id'] == request_id
            and identity_alias['mini_done_sha256'] == _sha(json.dumps(done,
                sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)))
        request_id = identity_alias['original_request_id']
    request = dict(request_id=request_id, conversation_id='neurobro:' + proof['room']['day']
        + ':' + proof['room']['room_id'], phase='SCREENING', reply=turns[1]['text'],
        reply_sha256=proof['initial_reply_sha256'], market_context=None)
    result = json.loads(done['result'])
    _need(type(result) is dict and type(result.get('schema_version')) is int
        and result.get('ok') is True and result.get('trading_authority') is False
        and result.get('needs_followup') is False
        and done['operation_id'] in ('delegated-model:' + request_id,
            'delegated-model-screening-recovery:' + request_id) and result == {
        'schema_version': 1, 'ok': True, 'request_id': request_id,
        'conversation_id': request['conversation_id'], 'phase': 'SCREENING',
        'reply_sha256': proof['initial_reply_sha256'], 'authority': 'DIALOGUE_CONTROL_ONLY',
        'trading_authority': False, 'decision': {'action': 'SCREENING',
            'symbols': proof['native_result']['output']['symbols'], 'question': ''},
        'needs_followup': False, 'reason': 'DIALOGUE_DECISION'}
        and type(done['updated_at']) in (int, float) and math.isfinite(done['updated_at'])
        and 0 < done['updated_at'] <= proof['room']['updated_at'])


def recover_screening_response(provider, proof, mini_done, *, catalog, max_age, now=None, before_write=None):
    """Validate immutable no-send evidence, then resume only its blocked job.

    ``before_write`` may durably retain the returned preimages before any write;
    an exception from it leaves the database untouched. A crash after native
    COMPLETE but before the job CAS is resumable only with the same receipt.
    """
    from .research_provider import (BROWSER_SOURCE, BROWSER_PROVIDER, BROWSER_CONTRACT,
        request_body, source_evidence, validate_chart_evidence)
    _need(type(proof) is dict and type(proof.get('native_result')) is dict)
    operation = proof['native_result'].get('operation')
    _need(type(operation) is str)
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}):robot-v9:(0|[1-9]\d{0,11}):screening:0:([12])', operation)
    _need(match is not None)
    observed = time.time() if now is None else now
    _need(type(observed) in (int, float) and math.isfinite(observed) and observed > 0
        and datetime.fromtimestamp(observed, ZoneInfo('Asia/Jakarta')).date().isoformat() == match[1]
        and type(max_age) is int and 30 <= max_age <= 300
        and (before_write is None or callable(before_write)))
    count = int(match[3]); cycle_id = operation.rsplit(':screening:', 1)[0]
    db = provider.db
    _need(not db.in_transaction)
    before = _snapshot(db, operation, cycle_id)
    api, job, request, receipt, cycle, settings = (before[name] for name in (
        'api_requests', 'robot_jobs', 'research_browser_requests',
        'research_browser_responses', 'robot_cycles', 'robot_settings'))
    _need(all(value is not None for value in (api, job, request, cycle, settings)))
    prompt, schema = screening_contract(count)
    body, serialized, wire = request_body(operation, prompt, schema, None)
    payload = {**body, 'request_sha256': wire}
    identity_alias = _proof(proof, payload)
    _mini_selection(mini_done, proof, payload, identity_alias)
    output = proof['native_result']['output']
    selections(output, catalog, count=count)
    cached = canonical_output(output, schema, catalog)
    created, received = api['created'], proof['native_received_at']
    _need(type(created) in (int, float) and math.isfinite(created)
        and 0 < created <= mini_done['updated_at'] <= received <= observed
        and request == dict(operation=operation, provider=BROWSER_PROVIDER,
            contract_version=BROWSER_CONTRACT, request_sha256=wire,
            marker=body['marker'], request_body=serialized)
        and api['body_hash'] == wire and api['attempts'] == 1 and type(api['attempts']) is int
        and api['idempotency'] is None
        and job['cycle'] == cycle_id and job['kind'] == 'SCREENING'
        and job['symbol'] is None and job['risk_target'] is None
        and cycle['day'] == match[1] and cycle['entry_epoch'] == int(match[2])
        and settings['enabled'] == 1)
    data = json.loads(cycle['data'])
    stale = observed - created > max_age
    _need(type(data) is dict and data.get('queue') == [] and type(data.get('screen')) is int
        and (data['screen'] == -1 or stale and data['screen'] == 0
            and job['state'] == 'COMPLETE' and cycle['state'] == 'ACTIVE'
            and data.get('round_symbols') == [] and data.get('replacement_due') == count))
    _need(job['state'] != 'COMPLETE' or stale and data['screen'] == 0
        and data.get('round_symbols') == [] and data.get('replacement_due') == count)
    unsettled = api['state'] == 'NEEDS_REVIEW'
    if unsettled:
        _need(api['failure_code'] == 'NEUROBRO_BROWSER_OUTCOME_UNKNOWN'
            and api['output'] is None and receipt is None
            and job['state'] == cycle['state'] == 'NEEDS_REVIEW')
    else:
        _need(api['state'] == 'COMPLETE' and api['failure_code'] is None
            and api['output'] == cached and receipt is not None
            and (job['state'], cycle['state']) in {
                ('NEEDS_REVIEW', 'NEEDS_REVIEW'), ('PENDING', 'ACTIVE'),
                ('COMPLETE', 'ACTIVE')})
    pending_api = {row[0] for row in db.execute("SELECT operation FROM api_requests WHERE state IN ('PENDING','NEEDS_REVIEW') AND operation NOT LIKE 'post-trade-evaluation:%'")}
    pending_jobs = {row[0] for row in db.execute("SELECT operation FROM robot_jobs WHERE state IN ('PENDING','NEEDS_REVIEW')")}
    _need(pending_api == ({operation} if unsettled else set())
        and pending_jobs == ({operation} if job['state'] in ('PENDING', 'NEEDS_REVIEW') else set()))
    validate_chart_evidence([], schema, None, created, received, provider.chart_max_age)
    forensic = dict(operation=operation, request_sha256=wire, preimages=copy.deepcopy(before),
        native_proof=copy.deepcopy(proof), mini_done=copy.deepcopy(mini_done))
    if before_write is not None:
        before_write(copy.deepcopy(forensic))
    _need(_snapshot(db, operation, cycle_id) == before)
    if unsettled:
        provider._record_browser_response(operation, body, wire, proof['native_result'], schema,
            lambda value: selections(value, catalog, count=count), None, catalog=catalog,
            received_at=received, recovery=True)
    evidence = source_evidence(db, operation)
    actual_receipt = _row(db, 'research_browser_responses', 'operation', operation)
    _need(evidence.get('source_type') == BROWSER_SOURCE and actual_receipt is not None
        and actual_receipt['response_id'] == proof['native_result']['response_id']
        and actual_receipt['assistant_text'] == proof['native_result']['assistant_text']
        and actual_receipt['assistant_sha256'] == proof['final_reply_sha256']
        and actual_receipt['received_at'] == received and actual_receipt['output_sha256'] == _sha(cached))
    db.execute('BEGIN IMMEDIATE')
    try:
        after = _snapshot(db, operation, cycle_id)
        _need(all(after[name] == before[name] for name in (
            'research_browser_requests', 'robot_jobs', 'robot_cycles', 'robot_settings'))
            and after['research_browser_responses'] == actual_receipt
            and after['api_requests'] == {**api, 'state': 'COMPLETE', 'output': cached, 'failure_code': None})
        if stale and job['state'] != 'COMPLETE':
            # Retire this genuinely received old screening without pretending
            # it is fresh. The existing replacement mechanism owns the next
            # operation; neither the old Send nor its clock is replayed.
            replacement = copy.deepcopy(data)
            replacement.update(screen=0, queue=[], round_symbols=[], replacement_due=count)
            _need(db.execute("UPDATE robot_jobs SET state='COMPLETE' WHERE operation=? AND state=?",
                (operation, job['state'])).rowcount == 1)
            _need(db.execute("UPDATE robot_cycles SET state='ACTIVE',data=? WHERE id=? AND state=? AND data=?",
                (json.dumps(replacement), cycle_id, cycle['state'], cycle['data'])).rowcount == 1)
        elif job['state'] == 'NEEDS_REVIEW':
            _need(db.execute("UPDATE robot_jobs SET state='PENDING' WHERE operation=? AND state='NEEDS_REVIEW'",
                (operation,)).rowcount == 1)
            _need(db.execute("UPDATE robot_cycles SET state='ACTIVE' WHERE id=? AND state='NEEDS_REVIEW' AND data=?",
                (cycle_id, cycle['data'])).rowcount == 1)
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    return dict(state='SCREENING_RESPONSE_RECOVERED', operation=operation,
        symbols=list(output['symbols']), source_evidence=evidence, forensic=forensic,
        original_created_at=created, screening_stale=stale, max_age=max_age,
        continuation='CURRENT_REPLACEMENT_PENDING' if stale else 'RECEIVED_SCREENING_PENDING',
        provider_requests_sent=0, orders_sent=0)


# The receipt above is an immutable legacy round-zero contract. The generic
# observer has its own versioned contract and never broadens that old proof.
RECEIVED_KIND = 'NATIVE_RECEIVED_SCREENING_OBSERVATION_V1'
RECEIVED_TERMINAL = 'RECEIVED_STALE_NOT_EXECUTED'
RECEIVED_REASON = 'SCREENING_RESPONSE_RECEIVED_STALE'
RECEIVED_HOLD_TERMINAL = 'RECEIVED_HOLD_NOT_EXECUTED'
RECEIVED_HOLD_REASON = 'SCREENING_RESPONSE_RECEIVED_HOLD'
ENDED_NOEXEC = 'SCREENING_ENDED_STALE_NOEXEC_V1'
ENDED_RETURN_KIND = 'NATIVE_SCREENING_ENDED_RETURN_V1'
ENDED_RETURN_PRODUCER = '879e885b0a800b3a41a2de8523a706a4147e2214415e9f2a1e6022c5b7e932d5'
ENDED_RETURN_HOOK = 'ca2bccfea0c9f389c066dd15f63b136c60e609a638b80e36ccb1b8bee903784b'
PREVIOUS_ENDED_RETURN_HOOK = '8498214906365ec2081af01a1049f466308cb36498c1dda1f765083afc3d1d49'
CONTEXT_ENDED_RETURN_HOOK = '235fb14a9932269beebf55dc669298cbeeef90d35d22e1b6e413e29129b5a007'
CURRENT_ENDED_RETURN_HOOK = 'f4b5a212c6d5547dc97abe5f230353b1f744dfcc76704f2ee6b9fb578750b768'
PHASE_JSON_ENDED_RETURN_HOOK = '5f73c0629157ee1a44346cc0e31287997a44a4dafd3f4f00e17ba69316617b47'
ENDED_RETURN_SOURCES = frozenset(((ENDED_RETURN_PRODUCER, ENDED_RETURN_HOOK),
    (ENDED_RETURN_PRODUCER, PREVIOUS_ENDED_RETURN_HOOK),
    (ENDED_RETURN_PRODUCER, CONTEXT_ENDED_RETURN_HOOK),
    (ENDED_RETURN_PRODUCER, CURRENT_ENDED_RETURN_HOOK),
    (ENDED_RETURN_PRODUCER, PHASE_JSON_ENDED_RETURN_HOOK)))
RECEIVED_NATIVE_SOURCE = '30ea13cd51706b1db65620cdfb5f8db6dec1c9b3b8a20c9b7ca1d6c9db07f72e'
RECEIVED_SCHEMA = '''CREATE TABLE IF NOT EXISTS screening_response_recoveries(
 operation TEXT PRIMARY KEY,request_sha256 TEXT NOT NULL,proof_sha256 TEXT NOT NULL,
 proof TEXT NOT NULL,preimage TEXT NOT NULL,phase TEXT NOT NULL,created REAL NOT NULL,
 updated REAL NOT NULL);'''
REVIEW_RETURN_KIND = 'NATIVE_RECEIVED_SCREENING_REVIEW_INCOMPLETE_V1'
REVIEW_RETURN_SCHEMA = '''CREATE TABLE IF NOT EXISTS screening_review_returns(
 operation TEXT NOT NULL,request_sha256 TEXT NOT NULL,source_sha256 TEXT NOT NULL,
 source TEXT NOT NULL,returned_sha256 TEXT NOT NULL,returned TEXT NOT NULL,
 recorded REAL NOT NULL,PRIMARY KEY(operation,returned_sha256));'''


def _wire(value, *, ascii=False):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=ascii, allow_nan=False)


def _digest(value):
    return _sha(_wire(value, ascii=True))


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _require(value, code='SCREENING_RESPONSE_RECOVERY_UNVERIFIED'):
    if not value:
        raise Review(code)


def stored_screening_request(db, operation):
    """Validate the actual original request, including replacement metadata.

    The prompt is deliberately read from custody. Rebuilding today's initial
    template would change the identity of historical replacement screenings.
    """
    from .research_provider import request_body, BROWSER_PROVIDER, BROWSER_CONTRACT
    from .neuroapi import screening_count, SYMBOL_PATTERN
    from .http_client import unique
    _require(type(operation) is str)
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}):robot-v9:(0|[1-9]\d{0,11}):screening:(0|[1-9]\d{0,11}):([12])', operation)
    _require(match is not None)
    request = _row(db, 'research_browser_requests', 'operation', operation)
    api = _row(db, 'api_requests', 'operation', operation)
    _require(request is not None and api is not None)
    try:
        body = json.loads(request['request_body'], object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        rebuilt, serialized, request_hash = request_body(operation, body['prompt'], body['output_schema'], body['context'])
    except (KeyError, ValueError, TypeError, OverflowError):
        raise Review('SCREENING_RESPONSE_RECOVERY_UNVERIFIED') from None
    count = int(match[4]); index = int(match[3]); context = body.get('context')
    _require(body == rebuilt and request['request_body'] == serialized
        and request['provider'] == BROWSER_PROVIDER and request['contract_version'] == BROWSER_CONTRACT
        and request['marker'] == body['marker'] and request['request_sha256'] == api['body_hash'] == request_hash
        and screening_count(body['output_schema']) == count)
    if index == 0:
        _require(context is None)
    else:
        _require(type(context) is dict and set(context) == {'requested_count', 'excluded_symbols', 'replacement_instruction'}
            and type(context['requested_count']) is int and context['requested_count'] == count
            and type(context['excluded_symbols']) is list
            and all(type(symbol) is str and re.fullmatch(SYMBOL_PATTERN, symbol) for symbol in context['excluded_symbols'])
            and context['excluded_symbols'] == sorted(set(context['excluded_symbols']))
            and type(context['replacement_instruction']) is str and bool(context['replacement_instruction'].strip()))
    return body, request_hash, match


def _review_entry(entry):
    _require(type(entry) is dict and type(entry.get('value')) is dict)
    value = entry['value']; raw = _wire(value, ascii=True)
    _require(entry.get('sha256') == _sha(raw) and type(entry.get('bytes')) is int
        and entry['bytes'] == len(raw.encode()))
    return value


def _received_screening_source_rows(proof, payload, rows, value):
    """Reuse a sealed author's exact turns after a native assistant UUID rerender.

    This projection grants no new model/native identity.  It is available only
    inside a complete current NO-SEND observation of the same owned room and
    operation.  Minimal interpretation artifacts cannot authorize ID drift.
    Every source field other than completed assistant DOM IDs remains exact.
    """
    users = list(range(0, len(rows), 2)) if type(rows) is list else []
    room = proof.get('room', {}); witness = proof.get('room_witness', {})
    scope = proof.get('operation_scope', {}); cache = proof.get('rendered_prompt_receipt', {})
    response = value.get('response', {}); decision = response.get('decision', {})
    if not (type(room) is dict and type(witness) is dict and type(scope) is dict and type(cache) is dict
            and type(response) is dict and type(decision) is dict
            and decision.get('action') == 'SCREENING' and decision.get('question') == ''
            and response.get('needs_followup') is False):
        return None
    if not (len(rows) >= 2 and len(rows) % 2 == 0 and proof.get('rows') == rows
            and proof.get('kind') == RECEIVED_KIND and type(proof.get('schema_version')) is int
            and proof['schema_version'] == 2
            and proof.get('native_source_sha256') == RECEIVED_NATIVE_SOURCE
            and proof.get('operation') == payload['operation']
            and proof.get('request_sha256') == payload['request_sha256']
            and proof.get('payload_sha256') == _digest(payload)
            and proof.get('status') == 'CONNECTED' and proof.get('stage') == 'OBSERVED_WITHOUT_COMPLETION'
            and proof.get('errors') == [] and type(proof.get('stable_snapshot_reads')) is int
            and proof['stable_snapshot_reads'] == 4
            and proof.get('room_initial_state') == 'BOUND'
            and _finite(proof.get('observed_at')) and _finite(proof.get('finished_at'))
            and 0 < proof['observed_at'] <= proof['finished_at']
            and proof.get('operation_completed') is False
            and all(type(proof.get(key)) is int and proof[key] == 0
                for key in ('orders_sent', 'model_calls', 'provider_messages', 'uploads'))
            and proof.get('file_tiles') == [] and proof.get('receipt_files') == {}
            and proof.get('prepared_materials', []) == []
            and room.get('day') == payload['operation'][:10]
            and type(room.get('room_id')) is str and re.fullmatch('[a-f0-9]{32}', room['room_id'])
            and room.get('url') == proof.get('page_url')
            and type(room['url']) is str and room['url'].startswith('https://app.neurobro.ai/')
            and witness.get('native_room_matches') is True and witness.get('same_loaded_room_verified') is True
            and witness.get('journal_url_matches') is True
            and witness.get('initial_screening_user_rows') == [0] and witness.get('followup_user_rows') == users[1:]
            and scope.get('verified') is True and scope.get('initial_user') == 0
            and scope.get('latest_user') == len(rows) - 2 and scope.get('reply_row') == len(rows) - 1
            and scope.get('user_rows') == users and scope.get('reply_complete') is True
            and scope.get('unmatched_intents') == []
            and cache.get('operation') == payload['operation'] and cache.get('payload_sha256') == _digest(payload)
            and type(cache.get('rendered_prompt')) is str and cache.get('rendered_sha256') == _sha(cache['rendered_prompt'])
            and cache['rendered_prompt'] == rows[0].get('text') and cache['rendered_prompt'].count(payload['marker']) == 1):
        return None
    request = value.get('request', {}); context = request.get('conversation_context')
    if not (type(context) is dict and set(context) == {'native_operation', 'native_request_sha256', 'turns'}
            and context['native_operation'] == payload['operation']
            and context['native_request_sha256'] == payload['request_sha256']
            and type(context['turns']) is list and len(context['turns']) == len(rows)):
        return None
    original = context['turns']; original_ids = []; current_ids = []; changed = False
    for index, (source, current) in enumerate(zip(original, rows)):
        if not (type(source) is dict and type(current) is dict
                and set(source) <= {'role', 'text', 'id', 'complete', 'attachments'}
                and set(current) <= {'role', 'text', 'id', 'complete', 'attachments'}
                and source.get('role') == current.get('role') == ('user' if index % 2 == 0 else 'assistant')
                and type(source.get('text')) is str and bool(source['text'])
                and source.get('attachments', []) == current.get('attachments', []) == []):
            return None
        if index % 2 == 0:
            if source != current:
                return None
        else:
            if not (source.get('complete') is current.get('complete') is True
                    and all(type(row.get('id')) is str and re.fullmatch('[A-Za-z0-9_.:-]{1,160}', row['id'])
                        for row in (source, current))
                    and {key: item for key, item in source.items() if key != 'id'}
                        == {key: item for key, item in current.items() if key != 'id'}):
                return None
            original_ids.append(source['id']); current_ids.append(current['id'])
            changed = changed or source['id'] != current['id']
    if not (changed and len(set(original_ids)) == len(original_ids)
            and len(set(current_ids)) == len(current_ids)):
        return None
    return original


def _screening_review(proof, payload, rows, *, with_request=False):
    """Return only an actual DONE Mini review of this exact native transcript."""
    reply = rows[-1]['text']; reply_sha = _sha(reply); transcript = _digest(rows)
    request_id = _digest(dict(operation=payload['operation'], request_sha256=payload['request_sha256'],
        reply_sha256=reply_sha, phase='SCREENING', transcript_sha256=transcript))
    conversation = 'neurobro:' + proof['room']['day'] + ':' + proof['room']['room_id']
    identity = dict(operation=payload['operation'], request_sha256=payload['request_sha256'],
        payload_sha256=_digest(payload), context_sha256=_digest(payload['context']), phase='SCREENING',
        conversation_id=conversation, transcript_sha256=transcript, request_id=request_id, reply_sha256=reply_sha)
    for entry in proof.get('review_receipts', []):
        value = _review_entry(entry)
        source_rows = rows
        if any(value.get(key) != expected for key, expected in identity.items()):
            if entry.get('name') != ('screening-review-' + _digest({key: payload[key]
                    for key in ('operation', 'request_sha256')}) + '-' + str(value.get('request_id')) + '.json'):
                continue
            source_rows = _received_screening_source_rows(proof, payload, rows, value)
            if source_rows is None:
                continue
            source_transcript = _digest(source_rows)
            source_request_id = _digest(dict(operation=payload['operation'], request_sha256=payload['request_sha256'],
                reply_sha256=reply_sha, phase='SCREENING', transcript_sha256=source_transcript))
            source_identity = {**identity, 'transcript_sha256': source_transcript, 'request_id': source_request_id}
            if any(value.get(key) != expected for key, expected in source_identity.items()):
                continue
        else:
            source_identity = identity
        request_id = source_identity['request_id']
        request = value.get('request'); response = value.get('response'); done = value.get('model_operation')
        _require(value.get('schema_version') == 1 and type(value.get('schema_version')) is int
            and value.get('kind') == 'NATIVE_SCREENING_MINI_REVIEW_V1'
            and _finite(value.get('observed_at')) and value['observed_at'] <= proof['finished_at']
            and type(request) is dict and set(request) in (
                {'schema_version', 'request_id', 'conversation_id', 'phase', 'reply',
                    'reply_sha256', 'market_context', 'gate', 'deadline_epoch'},
                {'schema_version', 'request_id', 'conversation_id', 'phase', 'reply',
                    'reply_sha256', 'market_context', 'gate', 'deadline_epoch', 'conversation_context'})
            and type(request['schema_version']) is int and request['schema_version'] == 1
            and all(request[key] == source_identity[key] for key in ('request_id', 'conversation_id', 'phase', 'reply_sha256'))
            and request['reply'] == reply and request['market_context'] is None
            and request['gate'] == {'robot_on': True, 'native_dispatch_verified': True}
            and all(type(flag) is bool for flag in request['gate'].values())
            and _finite(request['deadline_epoch'])
            and type(response) is dict and set(response) == {'schema_version', 'ok', 'request_id', 'conversation_id',
                'phase', 'reply_sha256', 'authority', 'trading_authority', 'decision', 'needs_followup', 'reason'}
            and type(response['schema_version']) is int and response['schema_version'] == 1 and response['ok'] is True
            and all(response[key] == source_identity[key] for key in ('request_id', 'conversation_id', 'phase', 'reply_sha256'))
            and response['authority'] == 'DIALOGUE_CONTROL_ONLY' and response['trading_authority'] is False
            and response['reason'] == 'DIALOGUE_DECISION' and type(response['needs_followup']) is bool)
        if 'conversation_context' in request:
            context = request['conversation_context']
            _require(type(context) is dict
                and set(context) == {'native_operation', 'native_request_sha256', 'turns'}
                and context['native_operation'] == payload['operation']
                and context['native_request_sha256'] == payload['request_sha256']
                and context['turns'] == source_rows)
        decision = response['decision']
        _require(type(decision) is dict and set(decision) == {'action', 'symbols', 'question'}
            and decision['action'] in ('SCREENING', 'HOLD', 'CLARIFY') and type(decision['symbols']) is list
            and type(decision['question']) is str and len(decision['question']) <= 1600
            and response['needs_followup'] is (decision['action'] == 'CLARIFY')
            and (bool(decision['question'].strip()) if response['needs_followup'] else decision['question'] == '')
            and type(done) is dict and set(done) == {'operation_id', 'kind', 'request_hash', 'state', 'result', 'updated_at'}
            and done['operation_id'] in ('delegated-model:' + request_id,
                'delegated-model-screening-recovery:' + request_id) and done['kind'] == 'DELEGATED_MODEL_DIALOGUE'
            # The authenticated DONE lookup preserves the historical request
            # hash. Its original technical deadline is not stored in the
            # operations row and can differ from today's read-only query.
            and done['state'] == 'DONE' and type(done['request_hash']) is str
            and re.fullmatch('[a-f0-9]{64}', done['request_hash'])
            and type(done['result']) is str and _finite(done['updated_at'])
            and 0 < done['updated_at'] <= value['observed_at'])
        try:
            actual = json.loads(done['result'])
        except (ValueError, TypeError):
            raise Review('SCREENING_RESPONSE_RECOVERY_UNVERIFIED') from None
        _require(actual == response)
        return (request, response) if with_request else response
    return None


def _reviewed_screening_output(proof, payload, rows, *, require_approval=False):
    """Project an actual sealed Mini SCREENING; preserve strict legacy reads."""
    from agent.neurobro_reply import parse_reply_for_payload, screening_interpretation
    receipt = _screening_review(proof, payload, rows, with_request=True)
    request, review = receipt if receipt is not None else (None, None)
    if require_approval:
        # Request an actual read-only Mini review before imposing a local text
        # layout. No symbols or native completion are admitted by this branch.
        _require(review is not None, 'SCREENING_MINI_APPROVAL_NOT_RETAINED')
    if (review is not None and review['decision']['action'] == 'SCREENING'
            and review['needs_followup'] is False):
        try:
            source_rows = request.get('conversation_context', {}).get('turns', rows)
            output = screening_interpretation(rows[-1]['text'], payload, request, review, source_rows)
        except ValueError:
            raise Review('SCREENING_RESPONSE_RECOVERY_UNVERIFIED') from None
    else:
        # Historical observations with no actual matching Mini request and
        # HOLD/CLARIFY decisions cannot acquire semantic selection authority.
        output = parse_reply_for_payload(rows[-1]['text'], payload)
    return output, review


def screening_interpretation_proof_output(proof, payload, text, *, created, received, retained):
    """Validate durable native/Mini provenance without a model or browser call."""
    _require(type(proof) is dict and set(proof) == {'kind', 'operation', 'request_sha256',
        'payload_sha256', 'assistant_sha256', 'room', 'turns', 'observed_at', 'review_receipt'}
        and proof['kind'] == 'NATIVE_SCREENING_INTERPRETATION_V1'
        and proof['operation'] == payload['operation']
        and proof['request_sha256'] == payload['request_sha256']
        and proof['payload_sha256'] == _digest(payload) and proof['assistant_sha256'] == _sha(text)
        and _finite(created) and _finite(received) and _finite(retained) and _finite(proof['observed_at'])
        and 0 < created <= received <= retained and created <= proof['observed_at'] <= retained)
    room, rows, entry = proof['room'], proof['turns'], proof['review_receipt']
    _require(type(room) is dict and set(room) == {'day', 'room_id', 'url'}
        and room['day'] == payload['operation'][:10] and type(room['room_id']) is str
        and re.fullmatch('[a-f0-9]{32}', room['room_id']) and type(room['url']) is str
        and room['url'].startswith('https://app.neurobro.ai/')
        and type(rows) is list and len(rows) >= 2 and len(rows) % 2 == 0
        and type(entry) is dict and set(entry) == {'name', 'sha256', 'bytes', 'value'})
    ids = []
    for index, row in enumerate(rows):
        _require(type(row) is dict and row.get('role') == ('user' if index % 2 == 0 else 'assistant')
            and type(row.get('text')) is str and bool(row['text']) and row.get('attachments', []) == [])
        if index % 2:
            _require(row.get('complete') is True and type(row.get('id')) is str
                and re.fullmatch('[A-Za-z0-9_.:-]{1,160}', row['id']))
            ids.append(row['id'])
    _require(len(ids) == len(set(ids)) and rows[-1]['text'] == text
        and rows[0]['text'].count(payload['marker']) == 1
        and all(payload['marker'] not in row['text'] for row in rows[2::2]))
    native = dict(room=room, rows=rows, finished_at=proof['observed_at'], review_receipts=[entry])
    request, review = _screening_review(native, payload, rows, with_request=True) or (None, None)
    _require(review is not None and review['decision']['action'] == 'SCREENING'
        and review['needs_followup'] is False and entry['value']['observed_at'] == proof['observed_at']
        and entry['name'] == 'screening-review-' + _digest({key: payload[key]
            for key in ('operation', 'request_sha256')}) + '-' + request['request_id'] + '.json')
    from agent.neurobro_reply import screening_interpretation, _TECHNICAL
    _require(_TECHNICAL.search(text) is None, 'NEUROBRO_INPUT_UNREADABLE')
    try:
        return screening_interpretation(text, payload, request, review, rows)
    except ValueError:
        raise Review('SCREENING_RESPONSE_RECOVERY_UNVERIFIED') from None


def _received_interpretation_proof(proof, payload):
    """Copy an existing final DONE review; never manufacture Mini approval."""
    matched = _screening_review(proof, payload, proof['rows'], with_request=True)
    request, review = matched if matched is not None else (None, None)
    if review is None or review['decision']['action'] != 'SCREENING' or review['needs_followup']:
        return None
    entries = [entry for entry in proof['review_receipts']
        if entry['value'].get('request_id') == review['request_id']]
    _require(len(entries) == 1)
    return dict(kind='NATIVE_SCREENING_INTERPRETATION_V1', operation=payload['operation'],
        request_sha256=payload['request_sha256'], payload_sha256=_digest(payload),
        assistant_sha256=_sha(proof['rows'][-1]['text']),
        room={key: proof['room'][key] for key in ('day', 'room_id', 'url')},
        turns=copy.deepcopy(request.get('conversation_context', {}).get('turns', proof['rows'])),
        observed_at=entries[0]['value']['observed_at'],
        review_receipt=copy.deepcopy(entries[0]))


def _received_coin_summary(count):
    _require(type(count)is int and count in(1,2))
    return ('Mohon ringkas '+str(count)+' coin pilihanmu sesuai kapasitas '
        'yang diminta dalam bentuk COIN: simbol USDT Perpetual. '
        'Jangan sertakan coin pembanding sebagai pilihan.')


def _review_source_identity(proof):
    """Bind an owned turn; read clocks and unrelated history offsets may change."""
    return dict(operation=proof['operation'], request_sha256=proof['request_sha256'],
        payload_sha256=proof['payload_sha256'], native_source_sha256=proof['native_source_sha256'],
        page_url=proof['page_url'], room={key: proof['room'][key] for key in ('day', 'room_id', 'url')},
        rows=proof['rows'], operation_scope=proof['operation_scope'],
        rendered_prompt_receipt=proof['rendered_prompt_receipt'])


def _review_return(returned, source, payload, recorded):
    """Only the authenticated review-only RPC can author this positive closure."""
    _require(type(returned) is dict and set(returned) == {
        'kind', 'operation', 'request_sha256', 'operation_completed', 'review_receipts'}
        and returned['kind'] == REVIEW_RETURN_KIND and returned['operation_completed'] is False
        and all(returned[key] == payload[key] for key in ('operation', 'request_sha256'))
        and type(returned['review_receipts']) is list and _finite(recorded)
        and source['finished_at'] <= recorded)
    # The actual review can finish after its source observation. Use the real
    # receipt-return time only to check review chronology, never rewrite capture.
    review = _screening_review({**source, 'review_receipts': returned['review_receipts'],
        'finished_at': recorded}, payload, source['rows'])
    _require(review is not None and review['needs_followup'] is True
        and review['decision']['action'] == 'CLARIFY')
    entries = [entry for entry in returned['review_receipts']
        if entry['value'].get('request_id') == review['request_id']]
    previous = [entry for entry in source['review_receipts']
        if entry['value'].get('request_id') == review['request_id']]
    _require(len(entries) == 1 and (not previous or previous == entries))
    return entries[0]


def retain_screening_review_return(provider, envelope, returned, *, catalog, max_age,
        now=None, permitted=lambda: True):
    """Append the real NO-SEND review return without changing its original job."""
    db = provider.db; current = time.time() if now is None else now
    _require(not db.in_transaction and _finite(current) and permitted() is True)
    operation = envelope.get('operation'); body, request_hash, match = stored_screening_request(db, operation)
    payload = {**body, 'request_sha256': request_hash}; cycle = operation.rsplit(':screening:', 1)[0]
    before = _snapshot(db, operation, cycle)
    api, job, cycle_row, settings = (before[key] for key in ('api_requests', 'robot_jobs', 'robot_cycles', 'robot_settings'))
    _require(all(value is not None for value in (api, job, cycle_row, settings))
        and type(api['attempts']) is int and api['attempts'] == 1 and api['output'] is None
        and api['idempotency'] is None and api['state'] in ('PENDING', 'NEEDS_REVIEW')
        and job['operation'] == operation and job['cycle'] == cycle and job['kind'] == 'SCREENING'
        and job['symbol'] is None and job['risk_target'] is None and job['state'] in ('PENDING', 'NEEDS_REVIEW')
        and cycle_row['id'] == cycle and cycle_row['day'] == match[1] and cycle_row['entry_epoch'] == int(match[2])
        and cycle_row['state'] in ('ACTIVE', 'NEEDS_REVIEW') and settings['enabled'] == 1
        and not db.execute('SELECT 1 FROM order_intents WHERE candidate_id=?', (operation,)).fetchone())
    data = json.loads(cycle_row['data'])
    _require(type(data) is dict and data.get('queue') == [] and type(data.get('screen')) is int
        and data['screen'] == int(match[3]) - 1)
    validate_received_screening(provider, envelope, payload, api['created'], catalog=catalog,
        max_age=max_age, now=current, require_approval=False)
    _review_return(returned, envelope['observation'], payload, current)
    source = _wire(envelope); result = _wire(returned); digest = _sha(result)
    db.execute(REVIEW_RETURN_SCHEMA)
    db.execute('BEGIN IMMEDIATE')
    try:
        _require(permitted() is True and _snapshot(db, operation, cycle) == before, 'RESEARCH_PAUSED')
        existing = db.execute('SELECT * FROM screening_review_returns WHERE operation=? AND returned_sha256=?',
            (operation, digest)).fetchone()
        if existing is None:
            db.execute('INSERT INTO screening_review_returns VALUES(?,?,?,?,?,?,?)',
                (operation, request_hash, _sha(source), source, digest, result, current))
        else:
            _require(existing['request_sha256'] == request_hash and existing['returned'] == result
                and existing['source_sha256'] == _sha(existing['source'])
                and _review_source_identity(json.loads(existing['source'])['observation'])
                    == _review_source_identity(envelope['observation']))
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK'); raise


def _retained_review_return(provider, envelope, payload, review, *, catalog, max_age):
    """A normal CLARIFY receipt alone never proves that a followup was not sent."""
    db = getattr(provider, 'db', None)
    if db is None:
        return False
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='screening_review_returns'").fetchone():
        return False
    for row in db.execute('SELECT * FROM screening_review_returns WHERE operation=?', (payload['operation'],)):
        _require(row['request_sha256'] == payload['request_sha256']
            and row['source_sha256'] == _sha(row['source']) and row['returned_sha256'] == _sha(row['returned'])
            and _finite(row['recorded']))
        source = json.loads(row['source']); returned = json.loads(row['returned'])
        if _review_source_identity(source['observation']) != _review_source_identity(envelope['observation']):
            continue
        _require(row['recorded'] <= envelope['observation']['finished_at'])
        api = _row(db, 'api_requests', 'operation', payload['operation'])
        validate_received_screening(provider, source, payload, api['created'], catalog=catalog,
            max_age=max_age, now=row['recorded'], retained=True, require_approval=False)
        entry = _review_return(returned, source['observation'], payload, row['recorded'])
        _require(entry['value']['response'] == review and entry in envelope['observation']['review_receipts'])
        return True
    return False


def _received_screening_shape(envelope, payload, created, *, max_age, now, retained=False):
    """Shared positive custody, independent of whether text is actionable."""
    _require(type(envelope) is dict and envelope.get('kind') == RECEIVED_KIND
        and envelope.get('operation_completed') is False and envelope.get('operation') == payload['operation']
        and envelope.get('request_sha256') == payload['request_sha256'])
    proof = envelope.get('observation'); raw = _wire(proof)
    _require(envelope.get('observation_sha256') == _sha(raw)
        and type(envelope.get('observation_bytes')) is int and envelope['observation_bytes'] == len(raw.encode())
        and type(envelope.get('capture_name')) is str and type(envelope.get('capture_sha256')) is str
        and re.fullmatch('[a-f0-9]{64}', envelope['capture_sha256'])
        and re.fullmatch(r'received-analysis-observation-[a-f0-9]{16}-' + envelope['capture_sha256'] + r'\.json', envelope['capture_name'])
        and type(envelope.get('capture_bytes')) is int and 0 < envelope['capture_bytes'] <= 8 * 1024 * 1024)
    _require(type(proof) is dict and type(proof.get('schema_version')) is int and proof['schema_version'] == 2
        and proof.get('kind') == RECEIVED_KIND and proof.get('operation') == payload['operation']
        and proof.get('request_sha256') == payload['request_sha256'] and proof.get('payload_sha256') == _digest(payload)
        and proof.get('native_source_sha256') == RECEIVED_NATIVE_SOURCE and proof.get('operation_completed') is False
        and all(type(proof.get(key)) is int and proof[key] == 0 for key in ('orders_sent', 'model_calls', 'provider_messages', 'uploads')))
    _require(proof.get('status') == 'CONNECTED', 'NEUROBRO_' + str(proof.get('status', 'UNAVAILABLE')))
    _require(proof.get('stage') == 'OBSERVED_WITHOUT_COMPLETION' and proof.get('errors') == []
        and _finite(created) and _finite(now) and _finite(proof.get('observed_at')) and _finite(proof.get('finished_at'))
        and 0 < created <= proof['observed_at'] <= proof['finished_at'] <= now
        and (retained or now - proof['finished_at'] <= 120)
        and type(max_age) is int and 30 <= max_age <= 300)
    rows = proof.get('rows'); room = proof.get('room'); witness = proof.get('room_witness'); scope = proof.get('operation_scope')
    _require(type(rows) is list and len(rows) >= 2 and len(rows) % 2 == 0
        and type(room) is dict and type(witness) is dict and type(scope) is dict
        and type(proof.get('row_offset')) is int and proof['row_offset'] >= 0
        and proof.get('file_tiles') == [] and proof.get('receipt_files') == {}
        and proof.get('prepared_materials', []) == [] and type(proof.get('review_receipts')) is list)
    users = list(range(0, len(rows), 2))
    _require(scope.get('verified') is True and scope.get('unmatched_intents') == []
        and scope.get('initial_user') == 0 and scope.get('latest_user') == len(rows) - 2
        and scope.get('user_rows') == users and scope.get('reply_row') == len(rows) - 1
        and scope.get('reply_complete') is True and type(proof.get('stable_snapshot_reads')) is int
        and proof['stable_snapshot_reads'] == 4, 'SCREENING_REPLY_NOT_COMPLETE')
    _require(room.get('day') == payload['operation'][:10] and type(room.get('room_id')) is str
        and re.fullmatch('[a-f0-9]{32}', room['room_id']) and room.get('url') == proof.get('page_url')
        and type(room['url']) is str and room['url'].startswith('https://app.neurobro.ai/')
        and witness.get('native_room_matches') is True and witness.get('same_loaded_room_verified') is True
        and witness.get('initial_screening_user_rows') == [0] and witness.get('followup_user_rows') == users[1:])
    cache = proof.get('rendered_prompt_receipt')
    _require(type(cache) is dict and cache.get('operation') == payload['operation']
        and cache.get('payload_sha256') == proof['payload_sha256'] and type(cache.get('rendered_prompt')) is str
        and cache.get('rendered_sha256') == _sha(cache['rendered_prompt'])
        and cache['rendered_prompt'] == rows[0].get('text') and cache['rendered_prompt'].count(payload['marker']) == 1)
    ids = []
    for index, row in enumerate(rows):
        _require(type(row) is dict and row.get('role') == ('user' if index % 2 == 0 else 'assistant')
            and type(row.get('text')) is str and bool(row['text']) and row.get('attachments', []) == [])
        if index % 2:
            _require(row.get('complete') is True and type(row.get('id')) is str
                and re.fullmatch('[A-Za-z0-9_.:-]{1,160}', row['id']), 'SCREENING_REPLY_NOT_COMPLETE')
            ids.append(row['id'])
    _require(len(ids) == len(set(ids)) and all(payload['marker'] not in row['text'] for row in rows[2::2]))
    return proof


def _ended_screening_return(proof, payload, created, *, max_age):
    """Prove an actual native interpretation ended before another WEB Send.

    This never resolves a model RPC. In particular its original UNKNOWN row is
    left untouched. The pinned native hook changes phase before a followup
    click, so these retained failures prove the current received turn cannot
    have caused another uncertain Send. A CLARIFY alone is not this evidence.
    """
    entry = proof.get('screening_ended_return')
    _require(type(entry) is dict and set(entry) == {'kind', 'name', 'sha256', 'bytes',
        'producer_source_sha256', 'hook_source_sha256', 'value'}
        and entry['kind'] == ENDED_RETURN_KIND
        and entry['name'] == 'native-return-failure-' + payload['request_sha256'] + '.json'
        and type(entry['producer_source_sha256']) is str and type(entry['hook_source_sha256']) is str
        and (entry['producer_source_sha256'], entry['hook_source_sha256']) in ENDED_RETURN_SOURCES)
    value = entry['value']; raw = _wire(value, ascii=True)
    _require(entry['sha256'] == _sha(raw) and type(entry['bytes']) is int
        and entry['bytes'] == len(raw.encode()) and 0 < entry['bytes'] <= 65536
        and type(value) is dict and set(value) in (
            {'schema_version', 'operation', 'request_sha256', 'phase', 'accepted_reply_sha256',
                'followup_intent_sha256', 'clock', 'deadline', 'at', 'chain'},
            {'schema_version', 'operation', 'request_sha256', 'phase', 'accepted_reply_sha256',
                'followup_intent_sha256', 'clock', 'deadline', 'at', 'chain', 'gate_failure'})
        and type(value['schema_version']) is int and value['schema_version'] == 1
        and value['operation'] == payload['operation'] and value['request_sha256'] == payload['request_sha256']
        and value['accepted_reply_sha256'] is None and value['followup_intent_sha256'] == []
        and value['phase'] in ('INITIAL', 'FOLLOWUP_SENT')
        and all(_finite(value[key]) for key in ('clock', 'deadline', 'at'))
        and 0 < value['clock'] < value['deadline']
        and created <= value['at'] <= proof['observed_at'])
    rows = proof['rows']
    _require((value['phase'] == 'INITIAL') is (len(rows) == 2))
    chain = value['chain']
    _require(type(chain) is list and len(chain) == 2)
    def frames(error):
        _require(type(error) is dict and set(error) == {'class', 'reason', 'frames',
            'message_sha256', 'message_characters'} and type(error['frames']) is list
            and 1 <= len(error['frames']) <= 24
            and type(error['message_sha256']) is str and re.fullmatch('[a-f0-9]{64}', error['message_sha256'])
            and type(error['message_characters']) is int and 0 <= error['message_characters'] <= 65536)
        result = []
        for frame in error['frames']:
            _require(type(frame) is dict and set(frame) == {'file', 'function', 'line'}
                and type(frame['file']) is str and frame['file'].startswith('/app/')
                and type(frame['function']) is str and re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', frame['function'])
                and type(frame['line']) is int and frame['line'] > 0)
            expected = {'native_browser_hook.py': '/app/agent/native_browser_hook.py',
                'dialogue_client.py': '/app/agent/dialogue_client.py', 'dom.py': '/app/browser_runtime/dom.py'}
            basename = frame['file'].rsplit('/', 1)[-1]
            _require(basename in expected and frame['file'] == expected[basename])
            result.append((frame['file'].rsplit('/', 1)[-1], frame['function']))
        return result
    outer, inner = frames(chain[0]), frames(chain[1])
    _require(chain[0]['class'] == 'OutcomeUnknown' and chain[0]['reason'] is None
        and outer == [('native_browser_hook.py', '_ask'), ('dom.py', 'ask')])
    prefix = [('dom.py', 'ask'), ('native_browser_hook.py', 'delegated_answer'),
        ('native_browser_hook.py', 'review')]
    if chain[1]['class'] == 'DialogueError':
        _require(chain[1]['reason'] == 'DIALOGUE_MODEL_UNAVAILABLE' and 'gate_failure' not in value
            and inner == prefix + [('dialogue_client.py', 'request'), ('dialogue_client.py', '_request')])
    else:
        gate = value.get('gate_failure')
        _require(chain[1]['class'] == 'OutcomeUnknown' and chain[1]['reason'] is None
            and type(gate) is dict and set(gate) == {'reason', 'clock', 'deadline', 'phase',
                'check_material', 'site_status', 'dispatch', 'initial_url_sha256', 'current_url_sha256'}
            and gate['reason'] == 'DISPATCH_NOT_PERMITTED' and gate['phase'] == value['phase']
            and gate['site_status'] == 'CONNECTED' and gate['dispatch'] is False
            and gate['check_material'] is False and gate['deadline'] == value['deadline']
            and _finite(gate['clock']) and 0 < gate['clock'] <= value['clock']
            and gate['initial_url_sha256'] == _sha(proof['page_url']) and gate['current_url_sha256'] is None
            and inner == prefix + [('native_browser_hook.py', name) for name in ('gate', 'failed', 'unknown')])
    # Every native clarification is durably recorded before its Send. The
    # passive producer reads the complete matching review inventory (or fails),
    # so an additional authorizing review would make another Send uncertain.
    _require(len(proof['review_receipts']) == len(rows) // 2 - 1)
    review_proof = _ended_screening_review_identity(proof, payload, created, max_age=max_age)
    review_rows = review_proof['rows']
    for index in range(2, len(rows), 2):
        review = _screening_review(review_proof, payload, review_rows[:index])
        prior = [entry['value'] for entry in proof['review_receipts']
            if entry['value'].get('request_id') == (review or {}).get('request_id')]
        _require(review is not None and review['needs_followup'] is True
            and review['decision']['action'] == 'CLARIFY'
            and rows[index]['text'].strip() == review['decision']['question'].strip()
            and len(prior) == 1 and created <= prior[0]['observed_at'] <= value['at'])
    return value


def _ended_screening_review_identity(proof, payload, created, *, max_age):
    """Use exact old native rows only as provenance for an existing review."""
    identity = proof.get('screening_review_identity')
    if identity is None:
        return proof
    _require(type(identity) is dict and set(identity) == {'kind', 'source_capture_name',
        'source_capture_sha256', 'source_capture_bytes', 'source_observation',
        'source_observation_sha256', 'source_observation_bytes', 'changed_assistant_rows', 'ended_return_sha256'}
        and identity['kind'] == 'NATIVE_SCREENING_RECEIVED_REVIEW_IDENTITIES_V1'
        and identity['ended_return_sha256'] == proof['screening_ended_return']['sha256'])
    source = identity['source_observation']
    key = _sha(_wire({name: payload[name] for name in ('operation', 'request_sha256')}))[:16]
    _require(identity['source_capture_name'] == 'received-analysis-observation-' + key + '-'
        + str(identity['source_capture_sha256']) + '.json'
        and type(source) is dict and source.get('screening_review_identity') is None)
    envelope = dict(kind=RECEIVED_KIND, operation=payload['operation'], request_sha256=payload['request_sha256'],
        operation_completed=False, observation=source, observation_sha256=identity['source_observation_sha256'],
        observation_bytes=identity['source_observation_bytes'], capture_name=identity['source_capture_name'],
        capture_sha256=identity['source_capture_sha256'], capture_bytes=identity['source_capture_bytes'])
    old = _received_screening_shape(envelope, payload, created, max_age=max_age,
        now=proof['observed_at'], retained=True)
    rows, previous = proof['rows'], old['rows']
    semantic = lambda row: {key: value for key, value in row.items() if key != 'id'}
    _require(len(rows) == len(previous) and all(semantic(left) == semantic(right)
        for left, right in zip(rows, previous))
        and proof['rendered_prompt_receipt'] == old['rendered_prompt_receipt']
        and proof['page_url'] == old['page_url']
        and proof['screening_ended_return']['value']['at'] <= old['observed_at']
        and all(proof['room'][key] == old['room'][key] for key in ('day', 'room_id', 'url'))
        and proof['review_receipts'] == old['review_receipts'])
    changed = [index for index, (current, original) in enumerate(zip(rows, previous)) if current != original]
    _require(changed and changed == identity['changed_assistant_rows']
        and all(index % 2 == 1 and rows[index]['complete'] is True and previous[index]['complete'] is True
            and type(rows[index]['id']) is str and type(previous[index]['id']) is str for index in changed))
    return old


def _validate_ended_screening(envelope, payload, created, *, max_age, now, retained=False):
    proof = _received_screening_shape(envelope, payload, created,
        max_age=max_age, now=now, retained=retained)
    ended = _ended_screening_return(proof, payload, created, max_age=max_age)
    stale = (now - created > max_age or datetime.fromtimestamp(now, ZoneInfo('Asia/Jakarta')).date().isoformat()
        != proof['room']['day'])
    return proof, ended, stale


def _retire_ended_screening(provider, envelope, payload, before, *, index, count, current,
        max_age, permitted):
    """Atomically retain the received text without fabricating typed output."""
    from agent.neurobro_reply import _TECHNICAL
    db = provider.db; operation = payload['operation']; cycle = before['robot_cycles']
    api, job = before['api_requests'], before['robot_jobs']
    proof, ended, stale = _validate_ended_screening(envelope, payload, api['created'],
        max_age=max_age, now=current)
    _require(stale, 'SCREENING_ENDED_RESPONSE_AWAITING_EXPIRY')
    _require(before['research_browser_responses'] is None and api['output'] is None
        and api['state'] in ('PENDING', 'NEEDS_REVIEW')
        and not db.execute('SELECT 1 FROM order_intents WHERE candidate_id=?', (operation,)).fetchone())
    sealed = copy.deepcopy(envelope)
    sealed['retirement'] = dict(kind=ENDED_NOEXEC, canonical_result=False,
        disposition='TECHNICAL_REPLY_RECEIVED' if _TECHNICAL.search(proof['rows'][-1]['text']) else 'REPLY_RECEIVED',
        failure_sha256=proof['screening_ended_return']['sha256'], max_age=max_age,
        preimage_sha256=_sha(_wire(before)))
    raw = _wire(sealed); data = json.loads(cycle['data'])
    data.update(screen=index, replacement_due=count)
    if index: data['replacements'] += 1
    db.execute('BEGIN IMMEDIATE')
    try:
        _require(permitted() is True and _snapshot(db, operation, cycle['id']) == before, 'RESEARCH_PAUSED')
        _require(not db.execute('SELECT 1 FROM order_intents WHERE candidate_id=?', (operation,)).fetchone())
        _require(db.execute('SELECT 1 FROM screening_response_recoveries WHERE operation=?', (operation,)).fetchone() is None)
        db.execute('INSERT INTO screening_response_recoveries VALUES(?,?,?,?,?,?,?,?)',
            (operation, payload['request_sha256'], _sha(raw), raw, _wire(before), 'NOEXEC', current, current))
        _require(db.execute('UPDATE api_requests SET state=?,output=NULL,failure_code=? '
            'WHERE operation=? AND state=? AND body_hash=? AND attempts=1 AND output IS NULL',
            (RECEIVED_TERMINAL, RECEIVED_REASON, operation, api['state'], payload['request_sha256'])).rowcount == 1)
        _require(db.execute("UPDATE robot_jobs SET state='COMPLETE' WHERE operation=? AND state=?",
            (operation, job['state'])).rowcount == 1)
        _require(db.execute("UPDATE robot_cycles SET state='ACTIVE',data=? WHERE id=? AND state=? AND data=?",
            (json.dumps(data), cycle['id'], cycle['state'], cycle['data'])).rowcount == 1)
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction: db.execute('ROLLBACK')
        raise
    return 'NOEXEC'


def _sealed_ended_screening(db, journal, body, request_hash):
    """Validate the non-canonical seal without pretending a response exists."""
    from agent.neurobro_reply import _TECHNICAL
    envelope = json.loads(journal['proof']); before = json.loads(journal['preimage'])
    retirement = envelope.get('retirement'); operation = body['operation']
    _require(type(retirement) is dict and set(retirement) == {'kind', 'canonical_result',
        'disposition', 'failure_sha256', 'max_age', 'preimage_sha256'}
        and retirement['kind'] == ENDED_NOEXEC and retirement['canonical_result'] is False
        and retirement['preimage_sha256'] == _sha(journal['preimage'])
        and journal['phase'] == 'NOEXEC' and journal['request_sha256'] == request_hash
        and journal['proof_sha256'] == _sha(journal['proof'])
        and journal['created'] == journal['updated'] and _finite(journal['created']))
    api = before['api_requests']; job = before['robot_jobs']; cycle = before['robot_cycles']
    _require(api['operation'] == job['operation'] == operation and api['body_hash'] == request_hash
        and type(api['attempts']) is int and api['attempts'] == 1 and api['idempotency'] is None
        and api['state'] in ('PENDING', 'NEEDS_REVIEW') and api['output'] is None
        and api['failure_code'] in (None, 'NEUROBRO_BROWSER_OUTCOME_UNKNOWN', 'LOCAL_PROCESSING_FAILED')
        and job['kind'] == 'SCREENING' and job['symbol'] is None and job['risk_target'] is None
        and job['state'] in ('PENDING', 'NEEDS_REVIEW') and job['cycle'] == cycle['id']
        and cycle['state'] in ('ACTIVE', 'NEEDS_REVIEW') and before['robot_settings']['enabled'] == 1
        and before['research_browser_responses'] is None)
    proof, ended, stale = _validate_ended_screening(envelope, {**body, 'request_sha256': request_hash},
        api['created'], max_age=retirement['max_age'], now=journal['created'], retained=True)
    _require(stale and retirement['failure_sha256'] == proof['screening_ended_return']['sha256']
        and retirement['disposition'] == ('TECHNICAL_REPLY_RECEIVED'
            if _TECHNICAL.search(proof['rows'][-1]['text']) else 'REPLY_RECEIVED')
        and _row(db, 'api_requests', 'operation', operation) == {**api,
            'state': RECEIVED_TERMINAL, 'output': None, 'failure_code': RECEIVED_REASON}
        and _row(db, 'robot_jobs', 'operation', operation) == {**job, 'state': 'COMPLETE'}
        and _row(db, 'research_browser_responses', 'operation', operation) is None
        and not db.execute('SELECT 1 FROM order_intents WHERE candidate_id=?', (operation,)).fetchone())
    return before, envelope


def validate_received_screening(provider, envelope, payload, created, *, catalog, max_age, now,
        retained=False, require_approval=True):
    """Check a no-send observation before admitting any native recorder write."""
    from agent.neurobro_reply import _TECHNICAL
    from .neuroapi import screening_count
    proof = _received_screening_shape(envelope, payload, created,
        max_age=max_age, now=now, retained=retained)
    rows = proof['rows']; room = proof['room']; users = list(range(0, len(rows), 2))
    count = screening_count(payload['output_schema'])
    _require(count in (1, 2) and _TECHNICAL.search(rows[-1]['text']) is None, 'NEUROBRO_INPUT_UNREADABLE')
    output, review = _reviewed_screening_output(proof, payload, rows, require_approval=require_approval)
    selections(output, catalog, count)
    if payload['context'] is not None:
        _require(not set(output['symbols']) & set(payload['context']['excluded_symbols']))
    for index in users[1:]:
        prior = _screening_review(proof, payload, rows[:index])
        if prior is not None and prior['decision']['action'] == 'CLARIFY':
            _require(rows[index]['text'].strip() == prior['decision']['question'].strip())
        else:
            _require(rows[index]['text'].replace('\r\n', '\n').strip() == _received_coin_summary(count)
                and _reviewed_screening_output(proof, payload, rows[:index])[0] == output)
    stale = now - created > max_age or datetime.fromtimestamp(now, ZoneInfo('Asia/Jakarta')).date().isoformat() != room['day']
    if require_approval:
        decision = review['decision']
        from agent.neurobro_reply import screening_market_hold
        held = screening_market_hold(rows[-1]['text'], output['symbols'])
        # A real CLARIFY is not final approval and must never resume a fresh
        # selection. An obsolete initial turn also needs the authenticated
        # review-only NO-SEND return bound to that exact DONE receipt. A normal
        # CLARIFY could precede an uncertain followup Send and proves no closure.
        stale_clarification = (stale and not held and len(rows) == 2
            and review['needs_followup'] is True and decision['action'] == 'CLARIFY'
            and decision['symbols'] in ([], output['symbols']))
        if stale_clarification:
            _require(_retained_review_return(provider, envelope, payload, review,
                catalog=catalog, max_age=max_age), 'SCREENING_STALE_REVIEW_RETURN_NOT_RETAINED')
        _require(stale_clarification or review['needs_followup'] is False and decision['question'] == ''
            and (decision == {'action': 'SCREENING', 'symbols': output['symbols'], 'question': ''}
                or decision['action'] == 'HOLD' and held
                and decision['symbols'] in ([], output['symbols'])), 'NEUROBRO_DECISION_CONFLICT')
    result = {key: payload[key] for key in ('operation', 'marker', 'request_sha256')}
    result.update(output=output, assistant_text=rows[-1]['text'], chart_evidence=[],
        response_id='dom-' + _sha(room['url'] + '\n' + payload['marker'] + '\n' + rows[-1]['text']))
    return result, proof['finished_at'], stale


def _native_screening_receipt(provider, operation, result, received, cached):
    from .research_provider import response_output_text
    receipt = _row(provider.db, 'research_browser_responses', 'operation', operation)
    _require(receipt is not None and receipt['response_id'] == result['response_id']
        and receipt['output_sha256'] == _sha(cached) and receipt['assistant_text'] == result['assistant_text']
        and receipt['assistant_sha256'] == _sha(result['assistant_text'])
        and receipt['chart_evidence'] == response_output_text([]) and receipt['received_at'] == received
        and receipt['chart_max_age'] == provider.chart_max_age)
    return receipt


def completed_screening_market_hold(db, operation):
    """Recognize an accepted all-HOLD response for NO-SEND review, never execute it."""
    from agent.neurobro_reply import parse_reply_for_payload, screening_market_hold
    try:
        body, request_hash, match = stored_screening_request(db, operation)
        api = _row(db, 'api_requests', 'operation', operation)
        receipt = _row(db, 'research_browser_responses', 'operation', operation)
        if (api['state'] != 'COMPLETE' or api['attempts'] != 1 or api['idempotency'] is not None
                or api['failure_code'] is not None or receipt is None
                or receipt['assistant_sha256'] != _sha(receipt['assistant_text'])):
            return False
        from .owner_source_protocol import read_screening_interpretation
        output = read_screening_interpretation(db, receipt['assistant_text'], body)
        if output is None:
            output = parse_reply_for_payload(receipt['assistant_text'], body)
        if len(output['symbols']) != int(match[4]):
            return False
        encoded = json.dumps({'symbols': output['symbols']})
        return (api['body_hash'] == request_hash and api['output'] == encoded
            and receipt['output_sha256'] == _sha(encoded)
            and screening_market_hold(receipt['assistant_text'], output['symbols']))
    except Exception:
        return False


def recover_received_screening(provider, envelope, *, catalog, max_age, now=None, permitted=lambda: True):
    """Seal evidence before native recording, then resume exactly one saved job."""
    db = provider.db; current = time.time() if now is None else now
    _require(not db.in_transaction and _finite(current) and current > 0 and permitted() is True)
    _require(type(envelope) is dict)
    operation = envelope.get('operation')
    body, request_hash, match = stored_screening_request(db, operation)
    index = int(match[3]); count = int(match[4]); cycle_id = operation.rsplit(':screening:', 1)[0]
    payload = {**body, 'request_sha256': request_hash}
    before = _snapshot(db, operation, cycle_id)
    db.execute(RECEIVED_SCHEMA)
    journal = _row(db, 'screening_response_recoveries', 'operation', operation)
    if journal is not None:
        _require(journal['request_sha256'] == request_hash and journal['proof_sha256'] == _sha(journal['proof']))
        if journal['phase'] in ('RESUME_CACHED', 'NOEXEC'):
            if json.loads(journal['proof']).get('retirement', {}).get('kind') == ENDED_NOEXEC:
                _sealed_ended_screening(db, journal, body, request_hash)
            return journal['phase']
        _require(journal['phase'] == 'PREPARED')
        envelope = json.loads(journal['proof']); before = json.loads(journal['preimage'])
    api, job, request, old_receipt, cycle, settings = (before[name] for name in (
        'api_requests', 'robot_jobs', 'research_browser_requests', 'research_browser_responses', 'robot_cycles', 'robot_settings'))
    accepted_hold = api is not None and completed_screening_market_hold(db, operation)
    _require(all(value is not None for value in (api, job, request, cycle, settings))
        and settings['enabled'] == 1 and type(api['attempts']) is int and api['attempts'] == 1
        and api['idempotency'] is None and (api['output'] is None or accepted_hold)
        and (api['state'] in ('PENDING', 'NEEDS_REVIEW') or accepted_hold)
        and api['failure_code'] in (None, 'NEUROBRO_BROWSER_OUTCOME_UNKNOWN', 'LOCAL_PROCESSING_FAILED')
        and job['cycle'] == cycle_id and job['kind'] == 'SCREENING' and job['symbol'] is None and job['risk_target'] is None
        and job['state'] in ('PENDING', 'NEEDS_REVIEW') and cycle['state'] in ('ACTIVE', 'NEEDS_REVIEW')
        and cycle['day'] == match[1] and cycle['entry_epoch'] == int(match[2]))
    data = json.loads(cycle['data'])
    _require(type(data) is dict and data.get('queue') == [] and type(data.get('screen')) is int
        and data['screen'] == index - 1 and type(data.get('replacements')) is int and data['replacements'] >= 0
        and type(data.get('seen')) is list and type(data.get('round_symbols')) is list)
    if index:
        _require(set(data['seen']) <= set(body['context']['excluded_symbols']))
    ended = envelope.get('observation', {}).get('screening_ended_return')
    if (journal is None and type(ended) is dict and type(ended.get('value')) is dict
            and ended['value'].get('accepted_reply_sha256') is None
            and ended['value'].get('phase') in ('INITIAL', 'FOLLOWUP_SENT')
            and len(envelope['observation'].get('rows', [])) >= 2
            and len(envelope['observation'].get('review_receipts', []))
                == len(envelope['observation']['rows']) // 2 - 1):
        proof = _received_screening_shape(envelope, payload, api['created'],
            max_age=max_age, now=current)
        try:
            _ended_screening_return(proof, payload, api['created'], max_age=max_age)
        except Review as error:
            if str(error) != 'SCREENING_RESPONSE_RECOVERY_UNVERIFIED':
                raise
            # A retained timeout is not proof that interpretation ended before
            # another Send. Positive received custody still follows the normal
            # source-bound Mini approval path; never infer NOT_SENT or replay.
            pass
        else:
            return _retire_ended_screening(provider, envelope, payload, before, index=index,
                count=count, current=current, max_age=max_age, permitted=permitted)
    result, received, stale = validate_received_screening(provider, envelope, payload, api['created'],
        catalog=catalog, max_age=max_age, now=current, retained=journal is not None)
    interpretation = _received_interpretation_proof(envelope['observation'], payload)
    if interpretation is not None:
        result['screening_interpretation'] = interpretation
    cached = canonical_output(result['output'], body['output_schema'], catalog)
    from agent.neurobro_reply import screening_market_hold
    held = screening_market_hold(result['assistant_text'], result['output']['symbols'])
    _require(not accepted_hold or held)
    if old_receipt is not None:
        # The native recorder may already have retained the exact raw response
        # before its final parsing/COMPLETE commit. Keep that real receipt clock.
        previous_received = old_receipt.get('received_at')
        _require(_finite(previous_received) and api['created'] <= previous_received <= received)
        _native_screening_receipt(provider, operation, result, previous_received, cached)
        received = previous_received
    if journal is None:
        raw = _wire(envelope)
        db.execute('INSERT INTO screening_response_recoveries VALUES(?,?,?,?,?,?,?,?)',
            (operation, request_hash, _sha(raw), raw, _wire(before), 'PREPARED', current, current))
    _require(permitted() is True, 'RESEARCH_PAUSED')
    native_api = _row(db, 'api_requests', 'operation', operation)
    if native_api == api and not accepted_hold:
        provider._record_browser_response(operation, body, request_hash, result, body['output_schema'],
            lambda value: selections(value, catalog, count), body['context'], catalog=catalog,
            received_at=received, recovery=True)
    else:
        _require((journal is not None or accepted_hold)
            and native_api == {**api, 'state': 'COMPLETE', 'output': cached, 'failure_code': None})
    receipt = _native_screening_receipt(provider, operation, result, received, cached)
    db.execute('BEGIN IMMEDIATE')
    try:
        _require(permitted() is True, 'RESEARCH_PAUSED')
        after = _snapshot(db, operation, cycle_id)
        _require(all(after[name] == before[name] for name in ('research_browser_requests', 'robot_jobs', 'robot_cycles', 'robot_settings'))
            and after['research_browser_responses'] == receipt
            and after['api_requests'] == {**api, 'state': 'COMPLETE', 'output': cached, 'failure_code': None})
        if stale or held:
            data['screen'] = index
            data['replacement_due'] = count
            if index:
                data['replacements'] += 1
            if held:
                data['seen'] = list(dict.fromkeys(data['seen'] + result['output']['symbols']))
                data['queue'] = []; data['round_symbols'] = []; data['replacement_due'] = count
            _require(db.execute('UPDATE api_requests SET state=?,output=NULL,failure_code=? WHERE operation=? AND state=\'COMPLETE\' AND output=?',
                (RECEIVED_HOLD_TERMINAL if held else RECEIVED_TERMINAL,
                 RECEIVED_HOLD_REASON if held else RECEIVED_REASON, operation, cached)).rowcount == 1)
            _require(db.execute('UPDATE robot_jobs SET state=\'COMPLETE\' WHERE operation=? AND state=?', (operation, job['state'])).rowcount == 1)
            _require(db.execute('UPDATE robot_cycles SET state=\'ACTIVE\',data=? WHERE id=? AND state=? AND data=?',
                (json.dumps(data), cycle_id, cycle['state'], cycle['data'])).rowcount == 1)
            phase = 'NOEXEC'
        else:
            _require(db.execute('UPDATE robot_jobs SET state=\'PENDING\' WHERE operation=? AND state=?', (operation, job['state'])).rowcount == 1)
            _require(db.execute('UPDATE robot_cycles SET state=\'ACTIVE\' WHERE id=? AND state=? AND data=?',
                (cycle_id, cycle['state'], cycle['data'])).rowcount == 1)
            phase = 'RESUME_CACHED'
        _require(db.execute('UPDATE screening_response_recoveries SET phase=?,updated=? WHERE operation=? AND phase=\'PREPARED\'',
            (phase, current, operation)).rowcount == 1)
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    return phase


def received_screening_replacement_due(db, cycle_id, data):
    """Only a sealed stale or market-HOLD screening receipt requests a new round."""
    due = data.get('replacement_due')
    index = data.get('screen')
    if (type(due) is not int or due not in (1, 2) or type(index) is not int
            or index < 0 or data.get('queue') != []):
        return 0
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='screening_response_recoveries'").fetchone():
        return 0
    operation = cycle_id + ':screening:' + str(index) + ':' + str(due)
    try:
        journal = _row(db, 'screening_response_recoveries', 'operation', operation)
        if journal is None or journal['phase'] != 'NOEXEC' or journal['proof_sha256'] != _sha(journal['proof']):
            return 0
        body, request_hash, match = stored_screening_request(db, operation)
        before = json.loads(journal['preimage']); envelope = json.loads(journal['proof'])
        if envelope.get('retirement', {}).get('kind') == ENDED_NOEXEC:
            before, envelope = _sealed_ended_screening(db, journal, body, request_hash)
            original_data = json.loads(before['robot_cycles']['data'])
            expected_data = {**original_data, 'screen': index, 'replacement_due': due}
            if index: expected_data['replacements'] += 1
            cycle = _row(db, 'robot_cycles', 'id', cycle_id)
            _require(data == expected_data and json.loads(cycle['data']) == data and cycle['state'] == 'ACTIVE')
            return due
        api = _row(db, 'api_requests', 'operation', operation); job = _row(db, 'robot_jobs', 'operation', operation)
        cycle = _row(db, 'robot_cycles', 'id', cycle_id); receipt = _row(db, 'research_browser_responses', 'operation', operation)
        proof = envelope['observation']; final = proof['rows'][-1]
        original_data = json.loads(before['robot_cycles']['data'])
        held = api['state'] == RECEIVED_HOLD_TERMINAL and api['failure_code'] == RECEIVED_HOLD_REASON
        expected_data = {**original_data, 'screen': index, 'replacement_due': due}
        if index:
            expected_data['replacements'] += 1
        if held:
            from agent.neurobro_reply import screening_market_hold
            output, review = _reviewed_screening_output(proof,
                {**body, 'request_sha256': request_hash}, proof['rows'])
            _require(screening_market_hold(final['text'], output['symbols']) and review is not None
                and review['needs_followup'] is False and review['decision']['question'] == ''
                and (review['decision'] == {'action': 'SCREENING', 'symbols': output['symbols'], 'question': ''}
                    or review['decision']['action'] == 'HOLD'
                    and review['decision']['symbols'] in ([], output['symbols'])))
            expected_data.update(seen=list(dict.fromkeys(original_data['seen'] + output['symbols'])),
                round_symbols=[])
        _require(journal['request_sha256'] == request_hash and envelope['request_sha256'] == request_hash
            and envelope['observation_sha256'] == _sha(_wire(proof)) and proof['payload_sha256'] == _digest({**body, 'request_sha256': request_hash})
            and data == expected_data and json.loads(cycle['data']) == data and cycle['state'] == 'ACTIVE'
            and api == {**before['api_requests'],
                'state': RECEIVED_HOLD_TERMINAL if held else RECEIVED_TERMINAL, 'output': None,
                'failure_code': RECEIVED_HOLD_REASON if held else RECEIVED_REASON}
            and job == {**before['robot_jobs'], 'state': 'COMPLETE'} and job['cycle'] == cycle_id
            and receipt is not None and receipt['assistant_text'] == final['text']
            and receipt['assistant_sha256'] == _sha(final['text'])
            and receipt['response_id'] == 'dom-' + _sha(proof['room']['url'] + '\n' + body['marker'] + '\n' + final['text'])
            and receipt['received_at'] == (before['research_browser_responses']['received_at']
                if before['research_browser_responses'] is not None else proof['finished_at']))
        return due
    except (Review, KeyError, TypeError, ValueError):
        return 0


def resume_uncertain_screening(coordinator, today, account):
    """Observe/review a once-sent screening while its original job stays owned."""
    observe = getattr(coordinator.neuro, 'observe_received_screening', None)
    if not callable(observe):
        return None
    jobs = list(coordinator.db.execute('''SELECT j.* FROM robot_jobs j JOIN api_requests a ON a.operation=j.operation
        WHERE j.kind='SCREENING' AND j.state IN ('PENDING','NEEDS_REVIEW') AND a.attempts=1
        AND a.output IS NULL AND ((a.state='NEEDS_REVIEW' AND a.failure_code IN
        ('NEUROBRO_BROWSER_OUTCOME_UNKNOWN','LOCAL_PROCESSING_FAILED')) OR a.state='PENDING') ORDER BY a.created LIMIT 2'''))
    accepted = list(coordinator.db.execute('''SELECT j.* FROM robot_jobs j JOIN api_requests a ON a.operation=j.operation
        WHERE j.kind='SCREENING' AND j.state='PENDING' AND a.state='COMPLETE'
        AND a.attempts=1 ORDER BY a.created LIMIT 2'''))
    jobs.extend(job for job in accepted if completed_screening_market_hold(coordinator.db, job['operation'])
        and job['operation'] not in {item['operation'] for item in jobs})
    has_recoveries = coordinator.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='screening_response_recoveries'").fetchone()
    if has_recoveries:
        prepared = list(coordinator.db.execute('''SELECT j.* FROM robot_jobs j JOIN api_requests a ON a.operation=j.operation
            JOIN screening_response_recoveries r ON r.operation=j.operation WHERE j.kind='SCREENING'
            AND j.state IN ('PENDING','NEEDS_REVIEW') AND a.state='COMPLETE' AND a.attempts=1
            AND r.phase='PREPARED' AND r.request_sha256=a.body_hash ORDER BY a.created LIMIT 2'''))
        jobs.extend(job for job in prepared if job['operation'] not in {item['operation'] for item in jobs})
    if len(jobs) != 1:
        return None
    operation = jobs[0]['operation']
    if not coordinator.allowed(today):
        return coordinator.store.report('WAITING', account, wait_reason=coordinator.pause_reason())
    try:
        api = _row(coordinator.db, 'api_requests', 'operation', operation)
        if api['state'] == 'COMPLETE':
            coordinator.neuro.require_key()
            journal = _row(coordinator.db, 'screening_response_recoveries', 'operation', operation) if has_recoveries else None
            if journal is not None:
                _require(journal['phase'] == 'PREPARED' and journal['proof_sha256'] == _sha(journal['proof']))
                envelope = json.loads(journal['proof'])
            else:
                _require(completed_screening_market_hold(coordinator.db, operation))
                envelope = observe(operation)
        else:
            envelope = observe(operation)
        try:
            phase = recover_received_screening(coordinator.neuro, envelope, catalog=coordinator.market.catalog(),
                max_age=coordinator.market.max_age, permitted=lambda: coordinator.allowed(today))
        except Review as error:
            if str(error) not in ('SCREENING_MINI_APPROVAL_NOT_RETAINED', 'SCREENING_STALE_REVIEW_RETURN_NOT_RETAINED'):
                raise
            review = getattr(coordinator.neuro, 'review_received_screening', None)
            _require(callable(review), 'SCREENING_MINI_APPROVAL_NOT_RETAINED')
            _require(coordinator.allowed(today), 'RESEARCH_PAUSED')
            reviewed = review(operation, envelope)
            if isinstance(reviewed, dict) and reviewed.get('kind') == REVIEW_RETURN_KIND:
                retain_screening_review_return(coordinator.neuro, envelope, reviewed,
                    catalog=coordinator.market.catalog(), max_age=coordinator.market.max_age,
                    permitted=lambda: coordinator.allowed(today))
            # Re-observe the native footer and durable actual review next tick.
            return coordinator.store.report('WAITING', account, wait_reason=(
                'SCREENING_REVIEW_INCOMPLETE' if isinstance(reviewed, dict)
                and reviewed.get('kind') == 'NATIVE_RECEIVED_SCREENING_REVIEW_INCOMPLETE_V1'
                else 'SCREENING_RECEIVED_REVIEW_RECONCILING'))
    except Exception as error:
        reason = str(error)
        safe = {'NEUROBRO_LOGIN_REQUIRED', 'NEUROBRO_LOGIN_IN_PROGRESS', 'NEUROBRO_CHALLENGE_REQUIRED',
            'NEUROBRO_BUSY', 'NEUROBRO_UNAVAILABLE', 'NEUROBRO_BROWSER_NOT_CONFIGURED', 'RESEARCH_PAUSED',
            'SCREENING_REPLY_NOT_COMPLETE', 'SCREENING_MINI_APPROVAL_NOT_RETAINED', 'NEUROBRO_INPUT_UNREADABLE',
            'SCREENING_ENDED_RESPONSE_AWAITING_EXPIRY',
            'SCREENING_RESPONSE_OBSERVATION_PENDING', 'SCREENING_REVIEW_OUTCOME_UNKNOWN', 'SCREENING_REVIEW_INCOMPLETE'}
        return coordinator.store.report('WAITING', account,
            wait_reason=reason if reason in safe else 'SCREENING_RESPONSE_RECOVERY_UNVERIFIED')
    held = _row(coordinator.db, 'api_requests', 'operation', operation)['state'] == RECEIVED_HOLD_TERMINAL
    return coordinator.store.report('WAITING', account, wait_reason=(
        'SCREENING_HOLD_REPLACEMENT_PENDING' if held else
        'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING' if phase == 'NOEXEC' else 'SCREENING_RECEIVED_RESUME_PENDING'))
