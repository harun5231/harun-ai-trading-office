"""Pure evidence checks for the proved screening assistant-ID migration.

No filesystem, browser, model, clock, database or order operations. Original
native IDs are not reconstructed: their saved anchors remain evidence, while
current IDs are bound to two unchanged replies and the captured Mini records.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

EVIDENCE = 'SCREENING_ASSISTANT_IDENTITY_EVIDENCE_V1'
RECEIPT = 'NATIVE_SCREENING_ASSISTANT_IDENTIFIERS_RECONCILED_V1'
ALIAS = 'NATIVE_SCREENING_ASSISTANT_IDENTIFIER_ALIAS_V1'
CAPTURE = dict(request_id='41990068843-1',
    source_commit='3043947e9dc5cc76f20b00826af6d676298de2ae',
    source_sha256='536f925ed363f3a93cf0045eda033d05219d1c7a71e27b113ee684ddd2531738')
ROW_FIELDS = {'operation_id', 'kind', 'request_hash', 'state', 'result', 'updated_at'}
RAW_FIELDS = {'schema_version', 'authority', 'phase', 'native_reply_sha256',
    'model_input_sha256', 'raw_output_text', 'raw_decision', 'json_decoded',
    'model_response_id', 'observed_at'}
ROOM_FIELDS = {'day', 'room_id', 'url', 'updated_at', 'anchors',
               'first_prompt_sha256', 'marker', 'history_titles', 'identifier_reconciliation'}


class ScreeningIdentityUnavailable(ValueError):
    pass


def need(value):
    if not value:
        raise ScreeningIdentityUnavailable('SCREENING_ASSISTANT_IDENTITIES_UNVERIFIED')


def canonical(value, *, native=False):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=native,
                      allow_nan=False).encode('ascii' if native else 'utf-8')


def digest(value, *, native=False):
    return hashlib.sha256(canonical(value, native=native)).hexdigest()


def text_sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def hashes(value, count=None):
    return (type(value) is list and (count is None or len(value) == count)
            and all(type(item) is str and re.fullmatch('[a-f0-9]{64}', item) for item in value))


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def unique(items):
    result = {}
    for key, value in items:
        need(key not in result)
        result[key] = value
    return result


def identities(rows):
    return [digest({key: value for key, value in row.items() if key != 'attachments'},
                   native=True) for row in rows]


def current_request_id(payload, rows):
    return digest(dict(operation=payload['operation'], request_sha256=payload['request_sha256'],
        reply_sha256=text_sha(rows[1]['text']), phase='SCREENING',
        transcript_sha256=digest(rows[:2], native=True)), native=True)


def _payload(payload):
    need(type(payload) is dict and set(payload) == {'operation', 'marker', 'provider',
        'contract_version', 'prompt', 'output_schema', 'context', 'request_sha256'})
    need(type(payload['operation']) is str and re.fullmatch(
        r'\d{4}-\d{2}-\d{2}:robot-v9:(?:0|[1-9]\d{0,11}):screening:0:[12]', payload['operation']))
    need(payload['marker'] == 'HARUN_' + text_sha(payload['operation'])[:32]
         and payload['provider'] == 'NEUROBRO_WEB'
         and payload['contract_version'] == 'neurobro-browser-decision-v2'
         and payload['context'] is None
         and payload['request_sha256'] == digest({k: v for k, v in payload.items()
                                                if k != 'request_sha256'}))


def _base_room(room):
    need(type(room) is dict and set(room) == ROOM_FIELDS)
    return dict(room, identifier_reconciliation=None)


def _raw(value, reply_sha, action):
    need(type(value) is dict and set(value) == {'key', 'value'})
    raw = value['value']
    need(type(raw) is dict and set(raw) == RAW_FIELDS and raw['schema_version'] == 1
         and raw['authority'] == 'UNVALIDATED_MODEL_OUTPUT' and raw['phase'] == 'SCREENING'
         and raw['native_reply_sha256'] == reply_sha and raw['json_decoded'] is True
         and number(raw['observed_at'])
         and hashes([raw['model_input_sha256']], 1)
         and type(raw['model_response_id']) is str
         and re.fullmatch(r'resp_[A-Za-z0-9_]{1,160}', raw['model_response_id']) is not None
         and type(raw['raw_output_text']) is str
         and json.loads(raw['raw_output_text'], object_pairs_hook=unique) == raw['raw_decision'])
    need(value['key'] == 'dialogue-model-raw:' + digest(
        {key: item for key, item in raw.items() if key != 'observed_at'}, native=True))
    decision = raw['raw_decision']
    need(type(decision) is dict and set(decision) == {'action', 'symbols', 'question'}
         and decision['action'] == action and type(decision['question']) is str)
    return raw


def validate_evidence(payload, room, rows, evidence):
    """Validate root-attested retained/current custody; return explicit alias core."""
    _payload(payload); base = _base_room(room)
    need(type(evidence) is dict and set(evidence) == {'schema_version', 'protocol',
         'operation', 'request_sha256', 'room_id', 'original_anchors', 'retained_agent',
         'current_agent', 'custody'} and evidence['schema_version'] == 1
         and evidence['protocol'] == EVIDENCE
         and evidence['operation'] == payload['operation']
         and evidence['request_sha256'] == payload['request_sha256']
         and evidence['room_id'] == base['room_id']
         and evidence['original_anchors'] == base['anchors'])
    custody = evidence['custody']
    need(type(custody) is dict and set(custody) == set(CAPTURE) | {
        'browser_capture_sha256', 'agent_capture_sha256'}
        and all(custody[key] == value for key, value in CAPTURE.items())
        and hashes([custody['browser_capture_sha256'], custody['agent_capture_sha256']], 2)
        and all(custody[key] != '0'*64 for key in ('browser_capture_sha256', 'agent_capture_sha256')))
    retained, current = evidence['retained_agent'], evidence['current_agent']
    need(type(retained) is dict and set(retained) == {'first_mini_done', 'final_mini_unknown',
         'raw_receipts'} and retained == current)
    need(type(rows) is list and len(rows) == 4 and all(type(row) is dict for row in rows)
         and all(row.get('role') == ('user' if index % 2 == 0 else 'assistant')
                 and type(row.get('text')) is str and row['text']
                 and row.get('attachments', []) == [] for index, row in enumerate(rows)))
    for index in (1, 3):
        need(rows[index].get('complete') is True and type(rows[index].get('id')) is str
             and re.fullmatch('[A-Za-z0-9_.:-]{1,160}', rows[index]['id']) is not None)
    need(rows[1]['id'] != rows[3]['id'])
    old, new = base['anchors'], identities(rows)
    need(hashes(old, 4) and len(set(old)) == 4 and len(set(new)) == 4
         and old[0] == new[0] and old[2] == new[2] and old != new)
    need(base['day'] == payload['operation'][:10] and base['marker'] == payload['marker']
         and base['first_prompt_sha256'] == text_sha(rows[0]['text'].replace('\r\n', '\n').strip())
         and re.findall(r'(?<![A-Za-z0-9_])HARUN_[a-f0-9]{32}(?![A-Za-z0-9_])',
                        rows[0]['text']) == [payload['marker']])
    first_sha, final_sha = text_sha(rows[1]['text']), text_sha(rows[3]['text'])
    raw_receipts = retained['raw_receipts']
    need(type(raw_receipts) is list and len(raw_receipts) == 2)
    first_raw = _raw(raw_receipts[0], first_sha, 'SCREENING')
    final_raw = _raw(raw_receipts[1], final_sha, 'CLARIFY')
    done, unknown = retained['first_mini_done'], retained['final_mini_unknown']
    need(type(done) is dict and set(done) == ROW_FIELDS and done['kind'] == 'DELEGATED_MODEL_DIALOGUE'
         and done['state'] == 'DONE' and type(done['result']) is str
         and number(done['updated_at']) and hashes([done['request_hash']], 1))
    decision = json.loads(done['result'], object_pairs_hook=unique)
    need(type(decision) is dict and set(decision) == {'schema_version', 'ok', 'reason',
         'request_id', 'conversation_id', 'phase', 'needs_followup', 'decision',
         'reply_sha256', 'authority', 'trading_authority'}
         and decision['schema_version'] == 1 and decision['ok'] is True
         and decision['reason'] == 'DIALOGUE_DECISION'
         and hashes([decision['request_id']], 1)
         and done['operation_id'] == 'delegated-model:' + decision['request_id']
         and decision['conversation_id'] == 'neurobro:' + base['day'] + ':' + base['room_id']
         and decision['phase'] == 'SCREENING' and decision['needs_followup'] is False
         and decision['reply_sha256'] == first_sha and decision['authority'] == 'DIALOGUE_CONTROL_ONLY'
         and decision['trading_authority'] is False)
    selection = decision['decision']; count = int(payload['operation'][-1])
    need(type(selection) is dict and set(selection) == {'action', 'question', 'symbols'}
         and selection['action'] == 'SCREENING' and selection['question'] == ''
         and type(selection['symbols']) is list and len(selection['symbols']) == count
         and len(set(selection['symbols'])) == count
         and all(type(symbol) is str and re.fullmatch('[A-Z0-9]{2,20}USDT', symbol)
                 for symbol in selection['symbols'])
         and first_raw['raw_decision']['symbols'] == selection['symbols']
         and final_raw['raw_decision']['symbols'] == selection['symbols'])
    need(0 <= done['updated_at'] - first_raw['observed_at'] <= 1
         and first_raw['observed_at'] < final_raw['observed_at'])
    need(type(unknown) is dict and set(unknown) == ROW_FIELDS
         and unknown['kind'] == 'DELEGATED_MODEL_DIALOGUE' and unknown['state'] == 'UNKNOWN'
         and unknown['result'] is None and type(unknown['operation_id']) is str
         and re.fullmatch('delegated-model:[a-f0-9]{64}', unknown['operation_id'])
         and unknown['operation_id'] != done['operation_id']
         and hashes([unknown['request_hash']], 1) and number(unknown['updated_at'])
         and 0 <= unknown['updated_at'] - final_raw['observed_at'] <= 1)
    current_id = current_request_id(payload, rows)
    need(current_id != decision['request_id'])
    return dict(protocol=ALIAS, operation=payload['operation'], request_sha256=payload['request_sha256'],
        payload_sha256=digest(payload), day=base['day'], room_id=base['room_id'],
        original_anchors=old, observed_anchors=new, reply_sha256=[first_sha, final_sha],
        original_request_id=decision['request_id'], current_request_id=current_id,
        mini_done_sha256=digest(done), final_unknown_sha256=digest(unknown),
        raw_receipt_sha256=[digest(item) for item in raw_receipts], custody=copy.deepcopy(custody))


def receipt_name(core):
    return 'screening-identifier-reconciliation-' + digest({key: core[key] for key in
        ('operation', 'request_sha256', 'original_anchors', 'observed_anchors')}) + '.json'


def build_receipt(payload, room, rows, evidence, observed_at):
    core = validate_evidence(payload, room, rows, evidence)
    need(number(observed_at) and observed_at >= room['updated_at']
         and observed_at >= evidence['retained_agent']['final_mini_unknown']['updated_at'])
    return dict(schema_version=1, protocol=RECEIPT, payload=copy.deepcopy(payload),
        original_room=_base_room(room), observed_rows=copy.deepcopy(rows),
        identity_evidence=copy.deepcopy(evidence), alias=dict(core, observed_at=observed_at))


def validate_identity_receipt(receipt, payload, room, rows):
    need(type(receipt) is dict and set(receipt) == {'schema_version', 'protocol', 'payload',
        'original_room', 'observed_rows', 'identity_evidence', 'alias'}
        and receipt['schema_version'] == 1 and receipt['protocol'] == RECEIPT
        and receipt['payload'] == payload and receipt['original_room'] == _base_room(room)
        and receipt['observed_rows'] == rows and type(receipt['alias']) is dict)
    core = validate_evidence(payload, room, rows, receipt['identity_evidence'])
    observed_at = receipt['alias'].get('observed_at')
    need(number(observed_at) and observed_at >= room['updated_at']
         and observed_at >= receipt['identity_evidence']['retained_agent']['final_mini_unknown']['updated_at']
         and receipt['alias'] == dict(core, observed_at=observed_at))
    result = dict(receipt['alias'], receipt_name=receipt_name(core), receipt_sha256=digest(receipt))
    existing = room['identifier_reconciliation']
    need(existing is None or existing == result)
    return result


def matches_reconciled_rows(receipt, alias, anchors, binding, rows):
    """An exact native-ID prefix/suffix bound to the immutable migration proof."""
    try:
        need(type(binding) is dict and binding.get('identifier_reconciliation') == alias)
        verified = validate_identity_receipt(receipt, receipt['payload'],
            dict(receipt['original_room'], identifier_reconciliation=alias), receipt['observed_rows'])
        need(verified == alias and anchors == alias['original_anchors']
             and binding.get('first_prompt_sha256') == receipt['original_room']['first_prompt_sha256']
             and binding.get('marker') == receipt['payload']['marker']
             and type(rows) is list and all(type(row) is dict for row in rows))
        current = identities(rows); expected = alias['observed_anchors']
        if len(current) >= 4 and current[:4] == expected:
            return True
        return (len(current) == 2 and current == expected[-2:]
                and rows[-2].get('role') == 'user' and rows[-1].get('role') == 'assistant'
                and rows[-1].get('complete') is True)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
