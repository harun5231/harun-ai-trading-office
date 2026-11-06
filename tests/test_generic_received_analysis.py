"""Any canonical analysis can be observed without resending or parsing HOLD."""
import copy
import json
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

from agent import native_received_analysis as observer
from agent.native_daily_room import identities, JOURNAL_NAME
from agent.native_screening_recovery import recover,enqueue
from agent.native_screening_recovery import _room_record,ScreeningRecoveryUnavailable
from agent import native_analysis_material as material
from tests import test_native_received_analysis as legacy


class GenericReceivedAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.case=legacy.ReceivedAnalysisObservationTests('test_current_ten_rows_four_file_tiles_and_status_are_saved_without_completion')
        self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.configure('DOGEUSDT')

    def configure(self,symbol,*,followup=True):
        c=self.case
        body={k:v for k,v in c.payload.items()if k!='request_sha256'}
        body.update(operation='2026-11-05:robot-v9:0:analysis-v9:'+symbol,
                    context=dict(symbol=symbol,source='Binance Futures'),prompt='Analisa '+symbol)
        body['marker']='HARUN_'+observer.text_sha(body['operation'])[:32]
        self.payload={**body,'request_sha256':observer.digest(body)}
        self.rendered=body['prompt']+'\nAcuan percakapan: '+body['marker']
        cache=dict(schema_version=1,operation=body['operation'],payload_sha256=observer.digest(self.payload),
                   model_request_id='a'*64,rendered_prompt=self.rendered,rendered_sha256=observer.text_sha(self.rendered))
        path=c.root/c.cache._name(self.payload);path.write_text(json.dumps(cache));path.chmod(0o600)
        names=[body['marker']+'-15m-'+('a'*12)+'.png',body['marker']+'-1h-'+('b'*12)+'.png',
               symbol+'_15m_200_closed_12345678.csv',symbol+'_1h_200_closed_12345678.csv']
        c.raw.messages[6].update(text=self.rendered,display_names=names)
        c.raw.messages[7].update(text='TECHNICAL HOLD: satu screenshot salah timeframe.',id='initial-'+symbol)
        self.names=names;self.followup='Berikut screenshot 1H dan 15M terbaru untuk '+symbol+' '+body['marker']
        c.raw.messages[8].update(text=self.followup,display_names=names)
        c.raw.messages[9].update(text='Jawaban FINAL yang perlu dibaca, bukan jawaban awal.',id='final-'+symbol)
        if not followup:c.raw.messages=c.raw.messages[:8]
        with c.room.store.open()as db:
            db.execute('UPDATE daily_rooms SET anchors=?',(json.dumps(identities(c.raw._turns())),))
        self.before=(c.root/JOURNAL_NAME).read_bytes()
        directory=c.root/'dialogue-200-followup';key=observer.text_sha(json.dumps(
            dict(operation=body['operation'],request_sha256=self.payload['request_sha256']),sort_keys=True,separators=(',',':')))
        if followup:
            receipt=dict(operation=body['operation'],request_sha256=self.payload['request_sha256'],marker=body['marker'],
                followup_sequence=1,followup_prompt_sha256=observer.text_sha(self.followup),
                initial_attachment_names=names,followup_attachment_names=names)
            p=directory/(key+'.intent.json');p.write_text(json.dumps(receipt));p.chmod(0o600)
        self.request=dict(action=observer.GENERIC_ACTION,payload=self.payload)

    def invoke(self):
        c=self.case
        with c.actor.claim:return recover(c.actor,c.delegated,self.request)

    def test_doge_latest_reply_and_operation_scoped_proof_are_not_hardcoded_to_rune(self):
        result=self.invoke();proof=result['observation']
        self.assertEqual(result['kind'],observer.GENERIC_KIND)
        self.assertEqual(result['operation'],self.payload['operation'])
        self.assertEqual(proof['row_offset'],6);self.assertEqual(len(proof['rows']),4)
        self.assertEqual(proof['rows'][-1]['id'],'final-DOGEUSDT')
        self.assertNotEqual(proof['rows'][-1]['text'],proof['rows'][1]['text'])
        self.assertEqual(proof['operation_scope']['latest_user'],2)
        self.assertTrue(proof['operation_scope']['verified']);self.assertTrue(proof['operation_scope']['reply_complete'])
        self.assertEqual(proof['stable_snapshot_reads'],4)
        self.assertEqual(result['observation_sha256'],observer.digest(proof))
        self.assertEqual(result['observation_bytes'],len(observer.canonical(proof)))
        private=json.loads((self.case.root/result['capture_name']).read_text())
        self.assertEqual(len(private['rows']),10)
        self.assertFalse(result['operation_completed']);self.assertNotIn('output',result)
        self.assertEqual((self.case.root/JOURNAL_NAME).read_bytes(),self.before)

    def test_initial_only_another_coin_and_incomplete_reply_remain_observation(self):
        self.configure('TIAUSDT',followup=False)
        result=self.invoke();proof=result['observation']
        self.assertEqual(proof['row_offset'],6);self.assertEqual(len(proof['rows']),2)
        self.assertTrue(proof['operation_scope']['verified'])
        self.assertEqual(proof['room_witness']['followup_user_rows'],[])
        self.assertFalse(result['operation_completed'])
        self.case.raw.messages[-1]['complete']=False
        result=self.invoke();self.assertFalse(result['observation']['operation_scope']['reply_complete'])
        self.assertFalse(result['operation_completed'])

    def test_unmatched_latest_intent_cannot_fall_back_to_initial_technical_hold(self):
        self.case.raw.messages=self.case.raw.messages[:8]
        result=self.invoke();scope=result['observation']['operation_scope']
        self.assertFalse(scope['verified']);self.assertTrue(scope['unmatched_intents'])
        self.assertFalse(result['operation_completed'])

    def test_changed_symbol_or_hash_refuses_without_dom_and_missing_material_is_not_invented(self):
        for mutate in (lambda p:p['context'].update(symbol='BTCUSDT'),lambda p:p.update(request_sha256='0'*64)):
            value=copy.deepcopy(self.request);mutate(value['payload'])
            with self.case.actor.claim:
                with self.assertRaises(ValueError):recover(self.case.actor,self.case.delegated,value)
        result=self.invoke();proof=result['observation']
        self.assertEqual(proof['prepared_materials'],[])
        self.assertEqual(proof['review_receipts'],[])
        self.assertEqual(proof['rows'][0]['attachments'],['UNCONFIRMED'])
        self.assertTrue(proof['file_tiles'][0]['file_tile_witnesses'])

    def test_generic_busy_reason_survives_existing_sanitized_service_contract(self):
        c=self.case;c.actor.mode='MANUAL'
        with self.assertRaisesRegex(ValueError,'SCREENING_RECOVERY_BUSY'):
            enqueue(c.actor,self.request)
        self.assertFalse(c.actor.claim.locked())

    def test_more_followups_preserve_context_without_fixed_discussion_count(self):
        c=self.case;directory=c.root/'dialogue-200-followup'
        key=observer.text_sha(json.dumps(dict(operation=self.payload['operation'],request_sha256=self.payload['request_sha256']),sort_keys=True,separators=(',',':')))
        for sequence in range(2,12):
            prompt='Klarifikasi '+str(sequence)+' '+self.payload['marker']
            c.raw.messages.extend([dict(role='user',text=prompt,attachments=['UNCONFIRMED'],display_names=self.names),
                dict(role='assistant',text='Jawaban '+str(sequence),complete=True,id='reply-'+str(sequence))])
            value=dict(operation=self.payload['operation'],request_sha256=self.payload['request_sha256'],marker=self.payload['marker'],
                followup_sequence=sequence,followup_prompt_sha256=observer.text_sha(prompt),
                initial_attachment_names=self.names,followup_attachment_names=self.names)
            p=directory/(key+'.'+str(sequence)+'.intent.json');p.write_text(json.dumps(value));p.chmod(0o600)
        result=self.invoke();scope=result['observation']['operation_scope']
        self.assertTrue(scope['verified']);self.assertEqual(len(scope['user_rows']),12)
        self.assertEqual(result['observation']['rows'][-1]['id'],'reply-11')


class SentRoomCompletionObservationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=GenericReceivedAnalysisTests()
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.fixture.configure('SUIUSDT',followup=False)
        self.c=self.fixture.case;self.payload=self.fixture.payload
        self.names=self.fixture.names
        first_marker='HARUN_'+'e'*32
        self.c.raw.messages[0]['text']+=' '+first_marker
        with self.c.room.store.open()as db:
            binding=json.loads(db.execute('SELECT binding FROM daily_rooms').fetchone()[0])
            binding.update(first_prompt_sha256=observer.text_sha(self.c.raw.messages[0]['text']),marker=first_marker)
            db.execute("UPDATE daily_rooms SET state='SENT',anchors=?,binding=?",
                (json.dumps(identities(self.c.raw._turns()[:6])),json.dumps(binding)))
        self.before=self.row()
        charts=[dict(symbol='SUIUSDT',timeframe=tf,source='BINANCE_USDM_FUTURES_WEBSITE_SCREENSHOT',
            sha256=char*64,attachment_id='uploaded-'+tf,captured_at='2026-11-05T11:00:00Z',bytes=2048)
            for tf,char in (('15m','a'),('1h','b'))]
        files=[dict(symbol='SUIUSDT',timeframe=tf,name=self.names[index+2],mimeType='text/csv',
            closed_count=200,timestamp_unit='UNIX_MILLISECONDS_UTC',sha256='12345678'+'a'*56,
            series_sha256='c'*64,source_candles_sha256='d'*64,bytes=20000)for index,tf in enumerate(('15m','1h'))]
        prepared=dict(schema_version=1,kind=material.KIND,state='PREPARED_NOT_SUBMITTED',send_attempted=False,
            operation=self.payload['operation'],request_sha256=self.payload['request_sha256'],marker=self.payload['marker'],
            symbol='SUIUSDT',observed_at=1793871126,context_sha256=material.digest(self.payload['context']),
            material_payload_sha256=material.digest(self.payload),original_request_matches_material=True,
            rendered_prompt_sha256=observer.text_sha(self.fixture.rendered),chart_evidence=charts,
            csv_receipts=files,attachment_names=self.names,closed_candles_per_timeframe=200,
            closed_candles_total=400,running_candles_separate=True,preview_verified=True)
        material.validate_prepared(prepared,self.payload)
        path=self.c.root/(material.prefix(self.payload)+material.digest(prepared)+'.json')
        path.write_bytes(material.canonical(prepared));path.chmod(0o600)

    def row(self):
        with self.c.room.store.open()as db:
            return dict(zip(('day','room_id','state','url','anchors','binding','updated_at'),
                db.execute('SELECT * FROM daily_rooms').fetchone()))

    def invoke(self):return self.fixture.invoke()

    def test_complete_sent_turn_is_sealed_from_stable_native_rows_and_real_file_witnesses(self):
        with self.assertRaises(ScreeningRecoveryUnavailable):_room_record(self.c.root,'2026-11-05')
        result=self.invoke();proof=result['observation'];after=self.row()
        self.assertEqual(result['error_count'],0)
        self.assertEqual(after['state'],'BOUND')
        self.assertEqual(json.loads(after['anchors']),identities(self.c.raw._turns()))
        self.assertEqual(json.loads(after['anchors'])[:6],json.loads(self.before['anchors']))
        for key in ('day','room_id','url','binding'):self.assertEqual(after[key],self.before[key])
        self.assertEqual(proof['room_initial_state'],'SENT')
        self.assertEqual(proof['room_recovery']['state'],'SEALED')
        self.assertEqual(proof['stable_snapshot_reads'],4)
        self.assertEqual(proof['row_offset'],6)
        self.assertFalse(result['operation_completed'])
        self.assertEqual((proof['provider_messages'],proof['uploads'],proof['model_calls'],proof['orders_sent']),(0,0,0,0))
        saved=[json.loads(path.read_text()) for path in self.c.root.glob('received-analysis-observation-*.json')]
        self.assertTrue(any(value.get('room_recovery',{}).get('state')=='PREPARED' for value in saved))
        self.assertEqual(_room_record(self.c.root,'2026-11-05')['anchors'],identities(self.c.raw._turns()))

    def test_sent_eight_anchor_checkpoint_preserves_current_user_while_last_assistant_finishes(self):
        partial=self.c.raw._turns()
        partial[-1].update(text='Jawaban yang masih diselesaikan.',complete=False)
        prior_anchors=identities(partial)
        with self.c.room.store.open()as db:
            db.execute('UPDATE daily_rooms SET anchors=?',(json.dumps(prior_anchors),))
        before=self.row()
        result=self.invoke();proof=result['observation'];after=self.row()
        self.assertEqual(result['error_count'],0)
        self.assertEqual(after['state'],'BOUND')
        self.assertEqual(json.loads(after['anchors'])[:7],prior_anchors[:7])
        self.assertNotEqual(json.loads(after['anchors'])[-1],prior_anchors[-1])
        self.assertEqual(proof['room_recovery']['prior_record'],before)
        self.assertEqual(proof['room_recovery']['preserved_prefix_count'],7)
        self.assertEqual(proof['room_recovery']['prior_assistant_anchor'],prior_anchors[-1])

    def test_current_user_tamper_in_eight_anchor_checkpoint_cannot_be_hidden_by_prompt_cache(self):
        with self.c.room.store.open()as db:
            db.execute('UPDATE daily_rooms SET anchors=?',(json.dumps(identities(self.c.raw._turns())),))
        before=self.row()
        self.c.raw.messages[6]['text']+=' Changed user body.'
        path=self.c.root/self.c.cache._name(self.payload);cached=json.loads(path.read_text())
        cached['rendered_prompt']=self.c.raw.messages[6]['text']
        cached['rendered_sha256']=observer.text_sha(cached['rendered_prompt'])
        path.write_text(json.dumps(cached))
        result=self.invoke()
        self.assertEqual(self.row(),before)
        self.assertEqual(result['observation']['errors'][0]['reason'],'RECEIVED_ANALYSIS_SENT_ROOM_UNVERIFIED')

    def test_changed_snapshot_during_four_reads_retains_sent_and_actual_failure(self):
        wait=self.c.raw.page.wait_for_timeout
        def changing_wait(milliseconds):
            wait(milliseconds)
            self.c.raw.messages[-1]['text']+=' Streaming change.'
        self.c.raw.page.wait_for_timeout=changing_wait
        result=self.invoke()
        self.assertEqual(self.row(),self.before)
        self.assertEqual(result['observation']['errors'][0]['reason'],'RECEIVED_ANALYSIS_MESSAGE_SNAPSHOT_CHANGED')

    def test_missing_user_or_incomplete_reply_or_challenge_never_promotes_or_resends(self):
        original=copy.deepcopy(self.c.raw.messages)
        for mode in ('no-user','empty-dom','streaming','challenge'):
            with self.subTest(mode=mode):
                self.c.raw.messages=copy.deepcopy(original);self.c.status='CONNECTED'
                if mode=='no-user':self.c.raw.messages=self.c.raw.messages[:6]
                if mode=='empty-dom':self.c.raw.messages=[]
                if mode=='streaming':self.c.raw.messages[-1]['complete']=False
                if mode=='challenge':self.c.status='CHALLENGE_REQUIRED'
                result=self.invoke()
                self.assertEqual(self.row(),self.before)
                self.assertFalse(result['operation_completed'])
                self.assertEqual(result['status'],self.c.status)
                self.assertFalse(result['history_restore_attempted'])

    def test_wrong_room_changed_prefix_duplicate_prompt_or_missing_files_cannot_seal(self):
        original=copy.deepcopy(self.c.raw.messages);url=self.c.raw.page.url
        for mode in ('url','prefix','duplicate-prompt','missing-csv'):
            with self.subTest(mode=mode):
                self.c.raw.messages=copy.deepcopy(original);self.c.raw.page.url=url
                if mode=='url':self.c.raw.page.url='https://app.neurobro.ai/chat/'+'f'*32
                if mode=='prefix':self.c.raw.messages[1]['text']='Different prior room response.'
                if mode=='duplicate-prompt':self.c.raw.messages+=copy.deepcopy(original[-2:])
                if mode=='missing-csv':self.c.raw.messages[6]['display_names']=self.names[:3]
                result=self.invoke()
                self.assertEqual(self.row(),self.before)
                self.assertFalse(result['operation_completed'])
                self.assertNotEqual(result['observation'].get('room_recovery',{}).get('state'),'SEALED')

    def test_binding_race_is_not_overwritten_by_sent_room_cas(self):
        save=observer._save
        def racing_save(root,capture):
            result=save(root,capture)
            if capture['stage']=='SENT_ROOM_SEAL_PREPARED':
                with self.c.room.store.open()as db:
                    binding=json.loads(self.before['binding']);binding['history_titles']=['Concurrent legitimate update']
                    db.execute('UPDATE daily_rooms SET binding=?',(json.dumps(binding),))
            return result
        with patch.object(observer,'_save',side_effect=racing_save):result=self.invoke()
        self.assertEqual(result['error_count'],1)
        self.assertEqual(result['observation']['errors'][0]['reason'],'RECEIVED_ANALYSIS_SENT_ROOM_CHANGED')
        self.assertEqual(self.row()['state'],'SENT')
        self.assertEqual(self.row()['anchors'],self.before['anchors'])
        self.assertNotEqual(self.row()['binding'],self.before['binding'])


if __name__=='__main__':unittest.main()
