"""Offline no-progress custody uses existing reviews and real material intents."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from agent.native_analysis_receipt import (digest,persist_review,read_reviews,prefix,
    question_was_answered,refuse_reviewed_no_progress,PENDING_KIND)
from agent.native_pending_protocol import REVIEW_SCHEMA
from agent.dialogue_service import DialogueError
from tests import test_native_browser_hook as native


class NativeDialogueNoProgressTests(unittest.TestCase):
    def harness(self):
        case=native.NativeFreshFollowupTests();self.addCleanup(case.doCleanups);case.harness()
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        root=Path(directory.name);root.chmod(0o700)
        class Room:
            store=SimpleNamespace(root=root)
            conversation_id='neurobro:2026-11-05:'+'a'*32
            @contextmanager
            def operation(self,payload):yield self
        case.hook.daily_room=Room()
        original=case.bridge._turns
        case.bridge._turns=lambda:[dict(row,role={'native_user_role':'user',
            'native_assistant_role':'assistant'}[row['role']])for row in original()]
        case.bridge.initial=case.bridge.final='BTCUSDT masih belum lengkap'
        return case,root

    def repeated(self):
        case,root=self.harness()
        with self.assertRaises(native.OutcomeUnknown):
            case.hook.ask(case.value,case.gate,deadline=3,
                chart_images=('initial-15m','initial-1h'),chart_evidence=case.initial)
        return case,root

    def intents(self,case):
        return [value for stage,_,value in case.events if stage=='INTENT']

    def rows(self,case):
        # The unchanged fixture's outside-transaction _turns projection uses
        # one global upload hint. Retain actual per-turn witnessed attachments,
        # as the real registered native file reader does after a restart.
        return [dict(copy.deepcopy(row),role={'native_user_role':'user',
            'native_assistant_role':'assistant'}[row['role']])for row in case.bridge.messages]

    def render(self,case,question,context=None):
        return case.module.user_prompt(dict(case.value,prompt=question,
            context=case.fresh['context']if context is None else context))

    def test_unchanged_completed_clarification_stops_before_another_upload_or_send(self):
        case,root=self.repeated()
        # First clarification can be useful. The second identical question
        # would repeat the same completed WEB pair under unchanged material.
        self.assertEqual(case.bridge.clicks,2);self.assertEqual(case.reasoner.request.call_count,2)
        self.assertEqual(len(case.bridge.uploads),1);self.assertEqual(len(self.intents(case)),1)
        self.assertEqual(len(read_reviews(root,case.value)),2)
        self.assertEqual(case.bridge.current,.25)
        self.assertNotIn('HOLD',case.bridge.messages[-1]['text'])

    def test_restart_reads_original_seal_and_blocks_before_any_new_paid_review(self):
        case,root=self.repeated();before={p.name:p.read_bytes()for p in root.iterdir()}
        calls=case.reasoner.request.call_count
        for _ in range(2):
            with self.assertRaisesRegex(ValueError,'NEUROBRO_CLARIFICATION_NO_PROGRESS'):
                refuse_reviewed_no_progress(root,case.value,self.rows(case),
                    context=case.fresh['context'],intents=self.intents(case),
                    render_question=lambda question:self.render(case,question))
        self.assertEqual(case.reasoner.request.call_count,calls)
        self.assertEqual({p.name:p.read_bytes()for p in root.iterdir()},before)

    def test_previous_daily_room_review_is_not_current_room_no_progress_authority(self):
        case,root=self.repeated();before={p.name:p.read_bytes()for p in root.iterdir()}
        calls=case.reasoner.request.call_count
        refuse_reviewed_no_progress(root,case.value,self.rows(case),context=case.fresh['context'],
            intents=self.intents(case),render_question=lambda question:self.render(case,question),
            conversation_id='neurobro:2026-11-06:'+'b'*32)
        self.assertEqual(case.reasoner.request.call_count,calls)
        self.assertEqual({p.name:p.read_bytes()for p in root.iterdir()},before)
        with self.assertRaisesRegex(ValueError,'NEUROBRO_CLARIFICATION_NO_PROGRESS'):
            refuse_reviewed_no_progress(root,case.value,self.rows(case),context=case.fresh['context'],
                intents=self.intents(case),render_question=lambda question:self.render(case,question),
                conversation_id=case.hook.daily_room.conversation_id)

    def received_seed(self,case):
        rows=self.rows(case);intents=self.intents(case)
        return dict(rows=rows,turn_names=[row['attachments']for row in rows[::2]],
            prompt=rows[0]['text'],followup_intents=copy.deepcopy(intents),
            bound_charts=copy.deepcopy(intents[-1]['followup_chart_evidence']),
            before_upload=Mock(),before_send=Mock())

    def resume(self,case):
        seed=self.received_seed(case)
        case.bridge.operation_deadline=80;case.bridge._operation_contract=case.bridge._dom_contract
        return case.hook._ask(case.value,case.gate,deadline=80,chart_evidence=case.initial,_received=seed)

    def test_received_restart_never_repeats_the_same_completed_fresh_material_question(self):
        case,root=self.repeated()
        # First received-turn recovery may supply a DIFFERENT technical chart
        # question. Its actual Send, complete reply and new intent are kept.
        with self.assertRaisesRegex(ValueError,'NEUROBRO_CLARIFICATION_NO_PROGRESS'):
            self.resume(case)
        self.assertEqual(case.bridge.clicks,3)
        before=(case.bridge.clicks,case.reasoner.request.call_count,len(case.bridge.uploads),len(self.intents(case)))
        snapshot={p.name:p.read_bytes()for p in root.iterdir()}
        # A second _ask has entirely new process-local state. Current material
        # still matches; no paid interpretation, upload, intent or Send repeats.
        with self.assertRaisesRegex(ValueError,'NEUROBRO_CLARIFICATION_NO_PROGRESS'):
            self.resume(case)
        self.assertEqual((case.bridge.clicks,case.reasoner.request.call_count,len(case.bridge.uploads),len(self.intents(case))),before)
        self.assertEqual({p.name:p.read_bytes()for p in root.iterdir()},snapshot)

    def test_new_question_actual_answer_or_market_context_is_productive(self):
        case,root=self.repeated();rows=self.rows(case);intents=self.intents(case)
        self.assertFalse(question_was_answered(case.value,rows,context=case.fresh['context'],
            rendered_question=self.render(case,'Bagian volume mana yang belum terbaca?'),intents=intents))
        changed=copy.deepcopy(rows);changed[-1]['text']='BTCUSDT sekarang ada informasi teknikal baru.'
        refuse_reviewed_no_progress(root,case.value,changed,context=case.fresh['context'],intents=intents,
            render_question=lambda question:self.render(case,question))
        context=copy.deepcopy(case.fresh['context']);context['server_time']+=1
        refuse_reviewed_no_progress(root,case.value,rows,context=context,intents=intents,
            render_question=lambda question:self.render(case,question,context))
        self.assertFalse(question_was_answered(case.value,rows,context=context,
            rendered_question=self.render(case,'Coin mana yang perlu diperjelas?',context),intents=intents))

    def test_productive_specific_clarifications_can_continue_with_the_same_answer(self):
        case,root=self.harness();count=[0]
        original=case.reasoner.request.side_effect
        def question(request):
            value=original(request);count[0]+=1
            if value['decision']['action']=='CLARIFY':
                value['decision']['question']='Jelaskan bagian teknikal '+('volume'if count[0]%2 else'wick')+' yang belum terbaca.'
                if count[0]==3:value['decision']['question']='Apa dasar invalidasi setup yang belum disebutkan?'
            return value
        case.reasoner.request.side_effect=question
        after=case.bridge.after_click
        def final(bridge):
            after(bridge)
            if bridge.clicks==4:bridge.messages[-1]['text']='FINAL_NATIVE_REPLY BTCUSDT'
        case.bridge.after_click=final
        result=case.ask()
        self.assertEqual(result['output']['side'],'HOLD')
        self.assertEqual(case.bridge.clicks,4);self.assertEqual(case.reasoner.request.call_count,4)
        self.assertEqual(len(read_reviews(root,case.value)),4)

    def test_uuid_or_receipt_tamper_never_creates_an_alias_or_new_model_operation(self):
        case,root=self.repeated();rows=self.rows(case);before={p.name:p.read_bytes()for p in root.iterdir()}
        changed=copy.deepcopy(rows)
        for row in changed:
            if row['role']=='assistant':row['id']='rerendered-'+row['id']
        with self.assertRaisesRegex(ValueError,'NATIVE_DIALOGUE_REVIEW_LINEAGE_UNVERIFIED'):
            refuse_reviewed_no_progress(root,case.value,changed,context=case.fresh['context'],
                intents=self.intents(case),render_question=lambda question:self.render(case,question))
        self.assertEqual({p.name:p.read_bytes()for p in root.iterdir()},before)
        latest=next(entry for entry in read_reviews(root,case.value)
            if entry['value']['context_sha256']==digest(case.fresh['context']))
        value=copy.deepcopy(latest['value']);value['request']['reply_sha256']='e'*64
        path=root/latest['name'];path.write_text(json.dumps(value,sort_keys=True,separators=(',',':')))
        with self.assertRaisesRegex(DialogueError,'DIALOGUE_MODEL_RESULT_INVALID'):
            refuse_reviewed_no_progress(root,case.value,rows,context=case.fresh['context'],
                intents=self.intents(case),render_question=lambda question:self.render(case,question))
        self.assertEqual(case.reasoner.request.call_count,2)

    def test_unknown_provider_outcome_has_no_sidecar_and_is_not_reclassified(self):
        case,root=self.harness()
        case.reasoner.request.side_effect=RuntimeError('offline ambiguous provider outcome')
        with self.assertRaises(native.OutcomeUnknown):case.ask()
        self.assertEqual(read_reviews(root,case.value),[])
        self.assertEqual(case.bridge.clicks,1);self.assertEqual(case.reasoner.request.call_count,1)
        refuse_reviewed_no_progress(root,case.value,case.bridge._turns(),context=case.value['context'],
            intents=[],render_question=lambda question:self.render(case,question))
        self.assertEqual(read_reviews(root,case.value),[])

    def test_pending_uses_same_immutable_store_with_its_own_phase_and_kind(self):
        case,root=self.repeated();entries=read_reviews(root,case.value)
        source=next(entry['value']for entry in entries if entry['value']['context_sha256']==digest(case.fresh['context']))
        payload=copy.deepcopy(case.value);payload['output_schema']=copy.deepcopy(REVIEW_SCHEMA)
        payload['operation']='pending-review-v1:'+'b'*32+':1'
        request=copy.deepcopy(source['request']);request['phase']='PENDING_REVIEW'
        evidence=request['conversation_context'];evidence['native_operation']=payload['operation']
        request['request_id']=digest(dict(operation=payload['operation'],request_sha256=payload['request_sha256'],
            reply_sha256=request['reply_sha256'],phase=request['phase'],transcript_sha256=source['transcript_sha256']))
        response=copy.deepcopy(source['response']);response.update(phase=request['phase'],request_id=request['request_id'])
        before={p.name:p.read_bytes()for p in root.iterdir()}
        result=persist_review(root,payload,request,response,evidence['turns'],context=case.fresh['context'],observed_at=case.now)
        self.assertEqual(result['kind'],PENDING_KIND);self.assertEqual(result['phase'],'PENDING_REVIEW')
        self.assertTrue(prefix(payload).startswith('pending-review-'))
        self.assertEqual(read_reviews(root,case.value),entries)
        self.assertEqual({name:(root/name).read_bytes()for name in before},before)
        self.assertEqual(persist_review(root,payload,request,response,evidence['turns'],context=case.fresh['context'],
            observed_at=case.now+1),result)
        intents=copy.deepcopy(self.intents(case))
        for intent in intents:intent.update(operation=payload['operation'],original_payload_sha256=digest(payload))
        with self.assertRaisesRegex(ValueError,'NEUROBRO_CLARIFICATION_NO_PROGRESS'):
            refuse_reviewed_no_progress(root,payload,evidence['turns'],context=case.fresh['context'],intents=intents,
                render_question=lambda question:self.render(case,question))


if __name__=='__main__':unittest.main()
