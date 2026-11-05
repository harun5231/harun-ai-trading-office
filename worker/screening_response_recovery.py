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
        and done['operation_id'] == 'delegated-model:' + request_id and result == {
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
