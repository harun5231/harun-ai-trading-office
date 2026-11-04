"""Pure receipt fixtures only: no filesystem, browser, model or exchange I/O."""
import copy
import hashlib
import math
import unittest

from agent.native_evaluation_protocol import PHASE, native_sha
from worker.trade_evaluation_evidence import EVALUATION_SCHEMA, PROTOCOL, digest, parse_evaluation_reply
from worker.trade_evaluation_recovery import validate_native_completion


TEXT = 'Penutupan eksternal terbukti; penyebab keputusan perlu bukti lain.\nPENILAIAN SETUP: BELUM CUKUP BUKTI'


def proof():
    original = 'Analisis semula yang benar-benar dicatat.'
    original_sha = hashlib.sha256(original.encode()).hexdigest()
    material = dict(files=[dict(kind=kind, timeframe=tf) for kind in ('PNG', 'CSV')
        for tf in ('15m', '1h')], delivery_custody=dict(assistant_sha256=original_sha,
            native_completion_sha256='e' * 64))
    request = dict(protocol=PROTOCOL, phase=PHASE, trading_authority=False,
        input_evidence_sha256='a' * 64, operation='post-trade-evaluation:' + 'a' * 32,
        room_id='b' * 32, prompt='Evaluasi bukti historis, bukan setup baru.',
        output_schema=copy.deepcopy(EVALUATION_SCHEMA), historical_materials=[material],
        source_neurobro_replies=[dict(text=original, sha256=original_sha, trade_id='owned-trade')])
    bound = dict(request=request, request_sha256=digest(request), reserved_at=100)
    payload = dict(operation=request['operation'], marker='HARUN_EVALUATION', request_sha256='c' * 64,
        prompt=request['prompt'], output_schema=copy.deepcopy(EVALUATION_SCHEMA),
        context=dict(historical_evaluation=bound))
    common = dict(schema_version=1, kind='NATIVE_POST_TRADE_EVALUATION', phase=PHASE,
        operation=payload['operation'], marker=payload['marker'], request_sha256=payload['request_sha256'],
        input_request_sha256=bound['request_sha256'], input_evidence_sha256=request['input_evidence_sha256'],
        room_id=request['room_id'], historical_access_sha256=digest(request['historical_materials']),
        conversation_url_sha256='d' * 64)
    raw_sha = hashlib.sha256(TEXT.encode()).hexdigest()
    send = dict(common, action='SEND_TEXT', response_id_kind='NATIVE_UI_MESSAGE_ID',
        followup_sequence=1, previous_followup_intent_sha256=None, material_sha256=None,
        rendered_prompt_sha256='f' * 64, bound_charts=[], attachment_names=[], reused_materials=[
            dict(material_sha256=digest(material), native_completion_sha256='e' * 64,
                assistant_sha256=original_sha, attachment_names=['old15.png', 'old1.png', 'old15.csv', 'old1.csv'])])
    send_complete = dict(send, intent_sha256=native_sha(send), response_id='native-ui-message',
        final_reply_sha256=raw_sha)
    chain = [dict(intent=send, complete=send_complete)]
    result = dict(operation=payload['operation'], marker=payload['marker'],
        request_sha256=payload['request_sha256'], output=parse_evaluation_reply(TEXT),
        assistant_text=TEXT, response_id='native-final-response', chart_evidence=[])
    final = dict(common, action='FINAL_ASSESSMENT', response_id_kind='NATIVE_FINAL_RESPONSE_ID',
        followup_sequence=2, previous_followup_intent_sha256=native_sha(send), reused_materials=[],
        source_chain_sha256=digest(chain), final_ui_message_id='native-ui-message',
        final_ui_reply_sha256=raw_sha, native_result=result, native_received_at=120)
    chain.append(dict(intent=final, complete=dict(final, intent_sha256=native_sha(final),
        response_id=result['response_id'], final_reply_sha256=raw_sha)))
    completion = dict(kind='COMPLETION', operation=payload['operation'],
        request_sha256=payload['request_sha256'], chain=chain)
    return payload, completion


def rebind_final(completion):
    final = completion['chain'][-1]['intent']
    old = completion['chain'][-1]['complete']
    completion['chain'][-1]['complete'] = dict(final, intent_sha256=native_sha(final),
        response_id=old['response_id'], final_reply_sha256=old['final_reply_sha256'])


class NativeEvaluationRecoveryProofTests(unittest.TestCase):
    def test_exact_durable_final_result_returns_independent_copies_without_modifying_inputs(self):
        payload, completion = proof()
        before = copy.deepcopy((payload, completion))
        result = validate_native_completion(payload, completion, now=121)
        self.assertEqual(result, dict(result=completion['chain'][-1]['intent']['native_result'],
            received_at=120, chain=completion['chain']))
        self.assertEqual((payload, completion), before)
        result['result']['assistant_text'] = 'changed copy'
        result['chain'][0]['intent']['room_id'] = 'changed copy'
        self.assertEqual((payload, completion), before)

    def test_rpc_wire_hash_and_operation_cannot_be_relabelled(self):
        for key, value in (('operation', 'post-trade-evaluation:' + 'e' * 32),
                           ('request_sha256', 'e' * 64), ('kind', 'ARCHIVE')):
            with self.subTest(key=key):
                payload, completion = proof()
                completion[key] = value
                with self.assertRaisesRegex(ValueError, 'COMPLETION_MISMATCH'):
                    validate_native_completion(payload, completion)

    def test_chain_common_request_room_material_and_sequence_bindings_are_checked(self):
        changes = [('room_id', '9' * 32), ('request_sha256', '9' * 64),
            ('input_request_sha256', '9' * 64), ('input_evidence_sha256', '9' * 64),
            ('historical_access_sha256', '9' * 64), ('marker', 'OTHER'),
            ('followup_sequence', 3), ('previous_followup_intent_sha256', '9' * 64)]
        for key, value in changes:
            with self.subTest(key=key):
                payload, completion = proof()
                completion['chain'][-1]['intent'][key] = value
                rebind_final(completion)
                with self.assertRaisesRegex(ValueError, 'INPUT_MISMATCH'):
                    validate_native_completion(payload, completion)

    def test_raw_reply_mutation_and_output_fabrication_are_rejected(self):
        for key, value in (('assistant_text', TEXT + '\nnot the recorded reply'),
                ('output', dict(assessment='CONSISTENT', findings=TEXT)), ('response_id', 'other-native-id')):
            with self.subTest(key=key):
                payload, completion = proof()
                completion['chain'][-1]['intent']['native_result'][key] = value
                rebind_final(completion)
                with self.assertRaisesRegex(ValueError, 'NATIVE_RESULT_MISMATCH'):
                    validate_native_completion(payload, completion)

    def test_only_actual_terminal_final_assessment_can_supply_recovery_result(self):
        payload, completion = proof()
        completion['chain'].pop()
        with self.assertRaisesRegex(ValueError, 'FINAL_MISSING'):
            validate_native_completion(payload, completion)
        payload, completion = proof()
        completion['chain'][-1].pop('complete')
        with self.assertRaisesRegex(ValueError, 'CHAIN_INCOMPLETE'):
            validate_native_completion(payload, completion)

    def test_changed_intent_hash_and_missing_native_result_are_rejected(self):
        payload, completion = proof()
        completion['chain'][-1]['complete']['intent_sha256'] = '9' * 64
        with self.assertRaisesRegex(ValueError, 'RECEIPT_INVALID'):
            validate_native_completion(payload, completion)
        payload, completion = proof()
        del completion['chain'][-1]['intent']['native_result']
        rebind_final(completion)
        with self.assertRaisesRegex(ValueError, 'NATIVE_RESULT_MISMATCH'):
            validate_native_completion(payload, completion)

    def test_recorded_receive_time_is_finite_not_boolean_not_before_reservation_or_future(self):
        for value in (True, 0, 99, math.nan, math.inf):
            with self.subTest(value=value):
                payload, completion = proof()
                completion['chain'][-1]['intent']['native_received_at'] = value
                if type(value) in (int, float) and not isinstance(value, bool) and not math.isfinite(value):
                    with self.assertRaises(ValueError):
                        validate_native_completion(payload, completion)
                    continue
                rebind_final(completion)
                with self.assertRaisesRegex(ValueError, 'RECEIVE_TIME_INVALID'):
                    validate_native_completion(payload, completion)
        payload, completion = proof()
        with self.assertRaisesRegex(ValueError, 'RECEIVE_TIME_INVALID'):
            validate_native_completion(payload, completion, now=119)
        self.assertEqual(validate_native_completion(payload, completion)['received_at'], 120)

    def test_final_ui_or_source_material_chain_mismatch_is_not_recovered(self):
        for key, value in (('source_chain_sha256', '9' * 64),
                ('final_ui_message_id', 'other-ui-message'), ('final_ui_reply_sha256', '9' * 64)):
            with self.subTest(key=key):
                payload, completion = proof()
                completion['chain'][-1]['intent'][key] = value
                rebind_final(completion)
                with self.assertRaises(ValueError):
                    validate_native_completion(payload, completion)
        payload, completion = proof()
        completion['chain'][-1]['intent']['native_result']['chart_evidence'] = [{'not': 'delivered'}]
        rebind_final(completion)
        with self.assertRaisesRegex(ValueError, 'INCOMPLETE'):
            validate_native_completion(payload, completion)

    def test_other_conversation_or_extra_native_result_field_cannot_supply_a_valid_final(self):
        payload, completion = proof()
        completion['chain'][-1]['intent']['conversation_url_sha256'] = '9' * 64
        rebind_final(completion)
        with self.assertRaisesRegex(ValueError, 'ROOM_CHANGED'):
            validate_native_completion(payload, completion)
        payload, completion = proof()
        completion['chain'][-1]['intent']['native_result']['sent_this_request'] = True
        rebind_final(completion)
        with self.assertRaisesRegex(ValueError, 'NATIVE_RESULT_MISMATCH'):
            validate_native_completion(payload, completion)


if __name__ == '__main__':
    unittest.main()
