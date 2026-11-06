"""Four-file Send leaves an upload hint; actual observer must still read plain context.

Uses real NativeTurnBatch projection, native receipt/review producers, the actor
observer, and the worker recovery/coordinator with temporary archives/SQLite.
No socket, live browser, model, Binance order or production file is used.
"""
import copy
import hashlib
import json
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from agent import native_received_analysis as observer
from agent import native_received_continuation as continuation
from agent.native_analysis_receipt import persist_review
from agent.native_chart_followup import ReceiptStore,canonical,material_dispatch_provenance
from agent.native_daily_room import DailyRoom,identities
from agent.native_turn_batch import NativeTurnBatch
from tests.test_agent_closed_candles_200 import native_context_200
from tests import test_native_received_analysis as native_fixture
from tests.test_native_daily_room import History
from tests import test_lost_not_sent_recovery as recovery_fixture
from tests.test_analysis_response_recovery import NOW,DAY,OP,CYCLE
from worker.analysis_continuation import record_return,restore_received_checkpoint
from worker.analysis_response_recovery import (digest,sha,wire,validate_observation,recover_received_analysis)
from worker.core import Review


class PostSendObserverProjectionTests(unittest.TestCase):
    def setUp(self):
        case=recovery_fixture.NativeAdmissionRenewedCapabilityRegression('test_old_native_admission_survives_capability_renewal_and_reaches_new_admission_once')
        case.setUp();self.addCleanup(case.doCleanups);case.test_old_native_admission_survives_capability_renewal_and_reaches_new_admission_once()
        state=case.lifecycle;self.f=state['f'];self.provider=state['provider'];self.root=state['root'];self.payload=state['payload']
        current=state['current'];self.capability=state['capability'];self.fresh=NOW+1010
        logical=observer._project_operation(current);logical.pop('manual_context_tail',None)
        self.f.envelope=dict(self.f.envelope,observation=logical);self.f.f.seal(self.f.envelope)
        with patch('tests.test_analysis_continuation.NOW',self.fresh):generated=self.f.new_reply()
        proof=generated['observation'];proof.pop('manual_context_tail',None)
        final='{"COIN":"BTCUSDT","ARAH":"HOLD","ENTRY":null,"TP":null,"SL":null}'
        proof['rows'][-1].update(text=final,id='4656d310-891e-412f-8197-f4533aea132a')
        self.answer=final
        intents=[entry['value']for name,entry in proof['receipt_files'].items()if name.endswith('.intent.json')]
        intents.sort(key=lambda entry:entry.get('followup_sequence',1))
        contexts=continuation.read_manual_contexts(self.root,self.payload)
        intents[-1]['manual_context_receipts']=[{key:entry[key]for key in('name','sha256','bytes')}for entry in contexts]
        intents[-1]['material_send_check']=material_dispatch_provenance(native_context_200(self.fresh-5),intents[-1]['followup_chart_evidence'],now=self.fresh-2)
        directory=self.root/'dialogue-200-followup';directory.mkdir(mode=0o700,exist_ok=True)
        key=digest({key:self.payload[key]for key in('operation','request_sha256')})
        response_id='dom-'+sha('https://app.neurobro.ai/chat/'+'a'*32+'\n'+self.payload['marker']+'\n'+final)
        store=ReceiptStore(self.root,monotonic=lambda:0,wall_clock=lambda:self.fresh+2)
        for number,intent in enumerate(intents,1):
            name=key+(''if number==1 else'.'+str(number))+'.intent.json'
            path=directory/name;path.write_bytes(canonical(intent));path.chmod(0o600)
            complete={**intent,'intent_sha256':digest(intent),'response_id':response_id,'final_reply_sha256':sha(final)}
            if number==1:
                # The older fixture intentionally carries the historical
                # compact Send-check shape; keep those exact archived bytes.
                complete_path=directory/(key+'.complete.json');complete_path.write_bytes(canonical(complete));complete_path.chmod(0o600)
            else:self.assertTrue(store(stage='COMPLETE',receipt=complete,deadline=10))
        self.names=intents[-1]['followup_attachment_names']
        turns=copy.deepcopy(proof['rows'])
        for index,names in ((0,intents[0]['initial_attachment_names']),(2,intents[0]['followup_attachment_names']),(4,self.names)):
            turns[index]['attachments']=names
        fresh_context=native_context_200(self.fresh-5)
        from agent.native_market_context import convert_native_market_context
        model_context=convert_native_market_context(fresh_context,now=self.fresh+2)
        identity=digest(dict(operation=OP,request_sha256=self.payload['request_sha256'],reply_sha256=sha(final),phase='ANALYSIS',transcript_sha256=digest(turns)))
        request=dict(schema_version=1,request_id=identity,conversation_id='neurobro:'+DAY+':'+'a'*32,phase='ANALYSIS',
            reply=final,reply_sha256=sha(final),market_context=model_context,gate=dict(robot_on=True,native_dispatch_verified=True),deadline_epoch=self.fresh+40)
        response=dict(schema_version=1,ok=True,request_id=identity,conversation_id=request['conversation_id'],phase='ANALYSIS',reply_sha256=sha(final),
            authority='DIALOGUE_CONTROL_ONLY',trading_authority=False,decision=dict(action='HOLD',symbols=['BTCUSDT'],question=''),needs_followup=False,reason='DIALOGUE_DECISION')
        persist_review(self.root,self.payload,request,response,turns,context=fresh_context,observed_at=self.fresh+2)
        native=native_fixture.ReceivedAnalysisObservationTests('test_current_ten_rows_four_file_tiles_and_status_are_saved_without_completion')
        native.setUp();self.addCleanup(native.doCleanups);self.native=native
        raw=native.raw;raw.page.url=current['room']['url'];raw.messages=[]
        for number in range(8):raw.messages.extend([dict(role='user',text='Earlier context '+str(number),attachments=[]),
            dict(role='assistant',text='Earlier reply '+str(number),id='older-'+str(number),complete=True)])
        raw.messages.extend(copy.deepcopy(turns[:4]));raw.messages.extend(copy.deepcopy(current['rows'][-2:]));raw.messages.extend(copy.deepcopy(turns[4:]))
        for index,names in ((16,intents[0]['initial_attachment_names']),(18,intents[0]['followup_attachment_names']),(22,self.names)):
            raw.messages[index]['display_names']=names
        raw.uploaded_names=list(self.names);self.hint=raw.uploaded_names
        old_locator=getattr(raw.page,'locator',lambda selector:(_ for _ in ()).throw(AssertionError('Unexpected selector '+selector)))
        def snapshot():
            result=[]
            for index,row in enumerate(raw.messages):
                if row['role']=='user':result.append(dict(identifier='user-'+str(index),users=1,answers=0,text=row['text'],
                    images=[dict(alt=name,visible=True,decoded=True)for name in row.get('display_names',[])[:2]]))
                else:result.append(dict(identifier=row['id'],users=0,answers=1,text=row['text'],complete=row['complete']))
            return result
        raw.page.locator=lambda selector:(SimpleNamespace(evaluate_all=lambda script:snapshot())if selector==native.native.MESSAGE else old_locator(selector))
        batch=object.__new__(NativeTurnBatch);batch.bridge=raw;batch.native=native.native;batch.batch_reads=0;batch.batch_seconds=0;batch.script='native-source-fixture'
        room=DailyRoom(raw,native.native,self.root,history=History(raw));room.original_turns=batch._read
        self.raw,self.room,self.batch=raw,room,batch
        with room.store.open()as db:
            db.execute('INSERT INTO daily_rooms VALUES(?,?,?,?,?,?,?)',(DAY,'a'*32,'BOUND',raw.page.url,
                json.dumps(identities(batch._read())),json.dumps(dict(first_prompt_sha256=sha(raw.messages[0]['text']),marker='HARUN_old',history_titles=['Analisis BTC'])),self.fresh+2))
        self.delegated=SimpleNamespace(daily_room=room,bridge=raw,native=native.native,
            prompt_cache=SimpleNamespace(read=lambda payload:copy.deepcopy(current['rendered_prompt_receipt'])))
        self.actor=native.actor;self.actor._bridge=self.delegated
        record_return(self.provider,self.capability,dict(kind=continuation.KIND,operation=OP,
            request_sha256=self.payload['request_sha256'],grant_id=self.capability['grant_id']),now=self.fresh+3)

    def observe(self):
        with self.actor.claim,patch.object(observer.time,'time',return_value=self.fresh+4):
            return observer.observe(self.actor,self.delegated,dict(action=observer.GENERIC_ACTION,payload=self.payload))

    def test_postsend_full_observer_and_native_recorder_consume_current_json_hold_without_another_send(self):
        # This is the actual native producer behavior with the last four-file
        # upload hint still present, not a handwritten UNCONFIRMED test row.
        self.assertEqual(self.batch._read()[20]['attachments'],['UNCONFIRMED'])
        before=identities(self.raw.messages);result=self.observe()
        self.assertIs(self.raw.uploaded_names,self.hint)
        self.assertEqual(self.raw.uploaded_names,self.names)
        self.assertEqual(result['error_count'],0)
        self.assertEqual(result['row_count'],24)
        self.assertEqual(len(result['observation']['rows']),6)
        self.assertEqual(result['observation']['rows'][-1]['text'],self.answer)
        self.assertEqual(identities(self.raw.messages),before)
        output,_,stale=validate_observation(self.provider,result,self.payload,self.f.f.created,now=self.fresh+4)
        self.assertFalse(stale);self.assertEqual(output['output']['side'],'HOLD')
        self.assertTrue(restore_received_checkpoint(self.provider,result,now=self.fresh+4))
        self.assertEqual(recover_received_analysis(self.provider,result,now=self.fresh+4),'RESUME_CACHED')
        self.assertEqual(json.loads(self.f.f.row('api_requests')['output'])['side'],'HOLD')
        self.assertEqual(self.provider.db.execute('SELECT count(*) FROM order_intents').fetchone()[0],1)

    def test_owned_missing_csv_witness_still_rejects_and_restores_original_hint(self):
        self.raw.messages[22]['display_names']=self.names[:-1]
        result=self.observe();self.assertIs(self.raw.uploaded_names,self.hint)
        with self.assertRaises(Review):validate_observation(self.provider,result,self.payload,self.f.f.created,now=self.fresh+4)
        self.assertIsNone(self.f.f.row('api_requests')['output'])

    def test_observer_error_and_absent_hint_restore_exact_previous_state(self):
        old_deadline=self.raw.operation_deadline;old_visible=self.raw._visible
        with patch.object(observer,'_file_tiles',side_effect=ValueError('ACTUAL_TILE_READ_FAILED')):
            result=self.observe()
        self.assertGreater(result['error_count'],0);self.assertIs(self.raw.uploaded_names,self.hint)
        self.assertEqual(self.raw.operation_deadline,old_deadline);self.assertIs(self.raw._visible,old_visible)
        del self.raw.uploaded_names
        self.observe();self.assertFalse(hasattr(self.raw,'uploaded_names'))

    def test_actor_gate_refusal_never_changes_the_existing_upload_hint(self):
        self.actor.mode='MANUAL'
        with self.assertRaises(ValueError):self.observe()
        self.assertIs(self.raw.uploaded_names,self.hint)
        self.assertEqual(self.raw.uploaded_names,self.names)
