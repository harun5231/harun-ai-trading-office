"""Pure proof validation for a native evaluation lost before worker receipt.

The caller must obtain ``completion`` from the existing authenticated native
private-receipt reader and separately verify the saved wire request hash.
Neither model output nor an Office body supplies that custody. This function
does no I/O, dispatch, journal mutation or replay; it only returns copies of a
complete, input-bound FINAL_ASSESSMENT and its actual native response.
"""
from copy import deepcopy
import hashlib
import math
import re

from agent.native_evaluation_protocol import (PHASE, envelope, native_sha,
    validate_material_access_chain)
from .trade_evaluation_evidence import digest, need, parse_evaluation_reply


def _sha(value):
    return type(value) is str and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def _instant(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def validate_native_completion(payload, completion, *, now=None):
    """Validate the exact authenticated COMPLETION RPC envelope, without I/O.

    The recorded native receive clock, raw assistant text and complete native
    result must be durable fields of the FINAL_ASSESSMENT intent itself. The
    final completion hash therefore covers them; they cannot be reconstructed
    from a local status flag, a parser replay or a later response projection.
    ``now`` is optional and, when supplied, rejects a future receive timestamp.
    """
    try:
        bound = envelope(payload)
        request = bound['request']
        need(type(payload.get('marker')) is str and payload['marker'] and
            '\n' not in payload['marker'] and _sha(payload.get('request_sha256')),
            'EVALUATION_RECOVERY_WIRE_BINDING_INVALID')
        need(type(completion) is dict and set(completion) ==
            {'kind', 'operation', 'request_sha256', 'chain'} and
            completion['kind'] == 'COMPLETION' and
            completion['operation'] == payload['operation'] and
            completion['request_sha256'] == payload['request_sha256'],
            'EVALUATION_RECOVERY_COMPLETION_MISMATCH')
        chain = completion['chain']
        need(type(chain) is list and bool(chain), 'EVALUATION_RECOVERY_CHAIN_INCOMPLETE')
        previous = None
        conversation = None
        for sequence, record in enumerate(chain, 1):
            need(type(record) is dict and set(record) == {'intent', 'complete'},
                'EVALUATION_RECOVERY_CHAIN_INCOMPLETE')
            intent, receipt = record['intent'], record['complete']
            need(type(intent) is dict and type(receipt) is dict and
                not {'intent_sha256', 'response_id', 'final_reply_sha256'} & set(intent),
                'EVALUATION_RECOVERY_RECEIPT_INVALID')
            need(type(intent.get('schema_version')) is int and intent['schema_version'] == 1
                and intent.get('kind') == 'NATIVE_POST_TRADE_EVALUATION' and intent.get('phase') == PHASE
                and intent.get('operation') == payload['operation']
                and intent.get('request_sha256') == payload['request_sha256']
                and intent.get('marker') == payload['marker']
                and intent.get('input_request_sha256') == bound['request_sha256']
                and intent.get('input_evidence_sha256') == request['input_evidence_sha256']
                and intent.get('room_id') == request['room_id']
                and intent.get('historical_access_sha256') == digest(request['historical_materials'])
                and type(intent.get('followup_sequence')) is int and intent['followup_sequence'] == sequence
                and intent.get('previous_followup_intent_sha256') == previous
                and _sha(intent.get('conversation_url_sha256')),
                'EVALUATION_RECOVERY_INPUT_MISMATCH')
            if conversation is None:
                conversation = intent['conversation_url_sha256']
            need(intent['conversation_url_sha256'] == conversation,
                'EVALUATION_RECOVERY_ROOM_CHANGED')
            need(receipt == dict(intent, intent_sha256=native_sha(intent),
                    response_id=receipt.get('response_id'), final_reply_sha256=receipt.get('final_reply_sha256'))
                and type(receipt.get('response_id')) is str
                and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', receipt['response_id']) is not None
                and _sha(receipt.get('final_reply_sha256')),
                'EVALUATION_RECOVERY_RECEIPT_INVALID')
            if sequence != len(chain):
                need(intent.get('action') != 'FINAL_ASSESSMENT' and
                    'native_result' not in intent and 'native_received_at' not in intent,
                    'EVALUATION_RECOVERY_FINAL_NOT_TERMINAL')
            previous = native_sha(intent)
        final = chain[-1]['intent']
        final_receipt = chain[-1]['complete']
        need(final.get('action') == 'FINAL_ASSESSMENT' and
            final.get('response_id_kind') == 'NATIVE_FINAL_RESPONSE_ID',
            'EVALUATION_RECOVERY_FINAL_MISSING')
        result = final.get('native_result')
        need(type(result) is dict and set(result) == {'operation', 'marker', 'request_sha256',
            'output', 'assistant_text', 'response_id', 'chart_evidence'} and
            result['operation'] == payload['operation'] and result['marker'] == payload['marker'] and
            result['request_sha256'] == payload['request_sha256'] and
            result['response_id'] == final_receipt['response_id'] and
            type(result['assistant_text']) is str and
            hashlib.sha256(result['assistant_text'].encode('utf-8')).hexdigest() ==
                final_receipt['final_reply_sha256'] == final.get('final_ui_reply_sha256') and
            type(result['chart_evidence']) is list and type(result['output']) is dict and
            result['output'] == parse_evaluation_reply(result['assistant_text']),
            'EVALUATION_RECOVERY_NATIVE_RESULT_MISMATCH')
        received_at = final.get('native_received_at')
        need(_instant(received_at) and received_at >= bound['reserved_at'],
            'EVALUATION_RECOVERY_RECEIVE_TIME_INVALID')
        if now is not None:
            need(_instant(now) and received_at <= now, 'EVALUATION_RECOVERY_RECEIVE_TIME_INVALID')
        validate_material_access_chain(request, chain, result['chart_evidence'],
            final_response=dict(response_id=result['response_id'],
                assistant_sha256=final_receipt['final_reply_sha256']))
        return deepcopy(dict(result=result, received_at=received_at, chain=chain))
    except (KeyError, TypeError, AttributeError, OverflowError, UnicodeError):
        raise ValueError('EVALUATION_RECOVERY_PROOF_INVALID') from None
