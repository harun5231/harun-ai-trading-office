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
from agent import native_analysis_receipt as analysis_receipt
from tests.test_agent_closed_candles_200 import native_context_200
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

    def bind_first_prompt_identity(self):
        # Tail-mutation tests must identify the existing owned room by a real
        # first prompt/marker too; HARUN_old is deliberately not a valid marker.
        c=self.case;marker='HARUN_'+observer.text_sha('owned-generic-first-screen')[:32]
        c.raw.messages[0]['text']+=' '+marker
        with c.room.store.open()as db:
            binding=json.loads(db.execute('SELECT binding FROM daily_rooms').fetchone()[0])
            binding.update(first_prompt_sha256=observer.text_sha(c.raw.messages[0]['text']),marker=marker)
            db.execute('UPDATE daily_rooms SET anchors=?,binding=?',(json.dumps(identities(c.raw._turns())),json.dumps(binding)))

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
        self.bind_first_prompt_identity()
        result=self.invoke();proof=result['observation']
        self.assertEqual(proof['row_offset'],6);self.assertEqual(len(proof['rows']),2)
        self.assertTrue(proof['operation_scope']['verified'])
        self.assertEqual(proof['room_witness']['followup_user_rows'],[])
        self.assertFalse(result['operation_completed'])
        self.case.raw.messages[-1]['complete']=False
        result=self.invoke();self.assertFalse(result['observation']['operation_scope']['reply_complete'])
        self.assertFalse(result['operation_completed'])

    def test_unmatched_latest_intent_cannot_fall_back_to_initial_technical_hold(self):
        self.bind_first_prompt_identity()
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

    def test_large_review_history_stays_private_while_latest_whole_receipt_fits_transport(self):
        # Each synthetic receipt carries the real-sized two-timeframe candle
        # fixture, as actual Mini review requests do. No model call is made.
        for sequence in range(12):
            context=native_context_200();context['symbol']='DOGEUSDT'
            request_id=observer.text_sha('review-'+str(sequence))
            value=dict(schema_version=1,kind=analysis_receipt.KIND,
                operation=self.payload['operation'],request_sha256=self.payload['request_sha256'],
                payload_sha256=observer.digest(self.payload),request_id=request_id,
                observed_at=1793969000+sequence,request=dict(market_context=context))
            name=analysis_receipt.prefix(self.payload)+request_id+'.json'
            path=self.case.root/name;path.write_bytes(observer.canonical(value));path.chmod(0o600)
        archived={path.name:path.read_bytes() for path in self.case.root.glob('analysis-review-*.json')}
        reviews=analysis_receipt.read_reviews(self.case.root,self.payload)
        newest=max(reviews,key=lambda entry:entry['value']['observed_at'])
        result=self.invoke();proof=result['observation']
        private=json.loads((self.case.root/result['capture_name']).read_text())
        self.assertGreater(len(observer.canonical(private)),690000)
        self.assertEqual(private['review_receipts'],reviews)
        self.assertEqual(proof['review_receipts'],[newest])
        self.assertLess(len(observer.canonical(result)),690000)
        for field in ('receipt_files','prepared_materials','rendered_prompt_receipt'):
            self.assertEqual(proof[field],private[field])
        self.assertEqual(proof['rows'],private['rows'][6:])
        self.assertEqual({path.name:path.read_bytes() for path in self.case.root.glob('analysis-review-*.json')},archived)
        # Screening retains its existing projection and review semantics.
        private['kind']='NATIVE_RECEIVED_SCREENING_OBSERVATION_V1'
        self.assertEqual(observer._project_operation(private)['review_receipts'],reviews)

    def test_invalid_review_timestamp_cannot_select_an_approval(self):
        for timestamp in (None,True,'1793969000',float('nan'),float('inf'),-1):
            with self.subTest(timestamp=timestamp),self.assertRaisesRegex(ValueError,'RECEIVED_ANALYSIS_REVIEW_TIME_UNVERIFIED'):
                observer._project_operation(dict(kind=observer.GENERIC_KIND,
                    review_receipts=[dict(value=dict(observed_at=timestamp))]))

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
        self.assertEqual(result['observation']['errors'][0]['reason'],'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED')

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
        for mode in ('no-user','streaming','challenge'):
            with self.subTest(mode=mode):
                self.c.raw.messages=copy.deepcopy(original);self.c.status='CONNECTED'
                if mode=='no-user':self.c.raw.messages=self.c.raw.messages[:6]
                if mode=='streaming':self.c.raw.messages[-1]['complete']=False
                if mode=='challenge':self.c.status='CHALLENGE_REQUIRED'
                result=self.invoke()
                self.assertEqual(self.row(),self.before)
                self.assertFalse(result['operation_completed'])
                self.assertEqual(result['status'],self.c.status)
                self.assertFalse(result['history_restore_attempted'])

    def test_empty_connected_sent_page_reopens_only_existing_matching_history_and_seals(self):
        saved=copy.deepcopy(self.c.raw.messages)
        partial=self.c.raw._turns();partial[-1].update(text='Belum selesai.',complete=False)
        with self.c.room.store.open()as db:
            db.execute('UPDATE daily_rooms SET anchors=?',(json.dumps(identities(partial)),))
        before=self.row()
        self.c.room.history.rooms=[('Analisis SUI existing',saved)]
        self.c.raw.messages=[]
        with patch.object(self.c.room,'restore',wraps=self.c.room.restore)as restore:
            result=self.invoke()
        self.assertEqual(restore.call_args.args[2],'BOUND')
        self.assertEqual(result['observation']['room_initial_state'],'SENT')
        self.assertTrue(result['history_restore_attempted']);self.assertTrue(result['history_restored'])
        self.assertEqual(self.c.room.history.selected,['Analisis SUI existing'])
        after=self.row();self.assertEqual(after['state'],'BOUND')
        for key in ('day','room_id','url','binding'):self.assertEqual(after[key],before[key])
        self.assertEqual(result['observation']['room_recovery']['preserved_prefix_count'],7)
        self.assertEqual(result['observation']['stable_snapshot_reads'],4)
        self.assertEqual(result['error_count'],0)
        self.assertFalse(result['operation_completed'])

    def test_empty_sent_history_cannot_be_bound_from_title_foreign_rows_or_a_challenge(self):
        saved=copy.deepcopy(self.c.raw.messages)
        for mode in ('missing','wrong-room','missing-current-user','challenge'):
            with self.subTest(mode=mode):
                self.c.status='CONNECTED';self.c.raw.messages=[];self.c.room.history.rooms=[]
                rows=copy.deepcopy(saved)
                if mode=='wrong-room':rows[0]['text']='Foreign first prompt without its original marker.'
                if mode=='missing-current-user':rows=rows[:6]
                if mode!='missing':self.c.room.history.rooms=[('Analisis SUI existing',rows)]
                select=self.c.room.history.select
                def select_challenge(row,*,occurrence=0):
                    select(row,occurrence=occurrence);self.c.status='CHALLENGE_REQUIRED'
                with patch.object(self.c.room.history,'select',side_effect=select_challenge if mode=='challenge'else select):
                    result=self.invoke()
                self.assertEqual(self.row(),self.before)
                self.assertTrue(result['history_restore_attempted'])
                self.assertFalse(result['operation_completed'])
                if mode=='challenge':self.assertEqual(result['status'],'CHALLENGE_REQUIRED')

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


class PriorAssistantIdentifierRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.base=SentRoomCompletionObservationTests();self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.c=self.base.c;self.review_files=[]
        self.screen=self.payload('screening:1:2','SCREENING')
        self.analysis=self.payload('analysis-v9:AVAXUSDT','ANALYSIS')
        screen_prompt=self.c.cache.store(self.screen,'Pilih dua coin Futures.','a'*64)
        analysis_prompt=self.c.cache.store(self.analysis,'Analisa AVAXUSDT.','b'*64)
        self.c.raw.messages[2]['text']=screen_prompt
        self.c.raw.messages[3]['text']='COIN: AVAXUSDT, SUIUSDT'
        self.c.raw.messages[4]['text']=analysis_prompt
        self.c.raw.messages[5]['text']='COIN: AVAXUSDT\nARAH: SHORT\nENTRY: 8.38\nSL: 8.54\nTP: 7.98'
        self.c.raw.messages[6:6]=[dict(role='user',text='Berikut chart AVAXUSDT terbaru. '+self.analysis['marker'],attachments=[]),
            dict(role='assistant',text='Ringkasan akhir\nCOIN: AVAXUSDT\nARAH: SHORT\nENTRY: 8.38\nSL: 8.54\nTP: 7.98',complete=True,id='original-final-avax')]
        original=self.c.raw._turns()
        self.retain_review(self.screen,original[2:4],'SCREENING')
        first=self.retain_review(self.analysis,original[4:6],'COMPLETE')
        self.retain_review(self.analysis,original[4:8],'COMPLETE')
        intent=dict(schema_version=1,kind='NATIVE_200_CHART_FOLLOWUP',operation=self.analysis['operation'],
            request_sha256=self.analysis['request_sha256'],marker=self.analysis['marker'],
            original_payload_sha256=observer.native_digest(self.analysis),initial_prompt_sha256=observer.text_sha(original[4]['text']),
            followup_prompt_sha256=observer.text_sha(original[6]['text']),initial_reply_sha256=observer.text_sha(original[5]['text']),
            initial_turns_sha256=first['transcript_sha256'],followup_sequence=1,previous_followup_intent_sha256=None)
        complete={**intent,'intent_sha256':observer.native_digest(intent),'response_id':'dom-old-final',
            'final_reply_sha256':observer.text_sha(original[7]['text'])}
        key=observer.native_digest({k:self.analysis[k]for k in('operation','request_sha256')})
        self.intent=self.c.root/'dialogue-200-followup'/(key+'.intent.json')
        for suffix,value in (('intent',intent),('complete',complete)):
            path=self.c.root/'dialogue-200-followup'/(key+'.'+suffix+'.json')
            path.write_text(json.dumps(value));path.chmod(0o600)
        with self.c.room.store.open()as db:
            db.execute('UPDATE daily_rooms SET anchors=?',(json.dumps(identities(original[:8])),))
        self.before=self.base.row()
        for index in(3,5,7):self.c.raw.messages[index]['id']='00000000-0000-4000-8000-'+str(index).zfill(12)

    def payload(self,suffix,phase):
        operation='2026-11-05:robot-v9:0:'+suffix
        body=dict(operation=operation,marker='HARUN_'+observer.text_sha(operation)[:32],provider='NEUROBRO_WEB',
            contract_version='neurobro-browser-decision-v2',prompt='Bahan percakapan '+suffix,
            output_schema={'required':['symbols']}if phase=='SCREENING'else self.base.payload['output_schema'],
            context=None if phase=='SCREENING'else{'symbol':'AVAXUSDT','source':'Binance Futures'})
        return {**body,'request_sha256':observer.digest(body)}

    def retain_review(self,payload,turns,action):
        from agent.native_analysis_receipt import persist_review,prefix
        phase='SCREENING'if action=='SCREENING'else'ANALYSIS';reply=turns[-1]['text']
        reply_sha=observer.text_sha(reply);transcript=observer.native_digest(turns)
        identity=observer.native_digest(dict(operation=payload['operation'],request_sha256=payload['request_sha256'],
            reply_sha256=reply_sha,phase=phase,transcript_sha256=transcript))
        conversation='neurobro:2026-11-05:'+'a'*32
        request=dict(schema_version=1,request_id=identity,conversation_id=conversation,phase=phase,reply=reply,
            reply_sha256=reply_sha,market_context=None if phase=='SCREENING'else{'symbol':'AVAXUSDT'},
            gate={'robot_on':True,'native_dispatch_verified':True},deadline_epoch=self.base.before['updated_at']+30)
        response=dict(schema_version=1,ok=True,request_id=identity,conversation_id=conversation,phase=phase,
            reply_sha256=reply_sha,authority='DIALOGUE_CONTROL_ONLY',trading_authority=False,
            decision={'action':action,'symbols':['AVAXUSDT','SUIUSDT']if phase=='SCREENING'else['AVAXUSDT'],'question':''},
            needs_followup=False,reason='DIALOGUE_DECISION')
        done=dict(operation_id='delegated-model:'+identity,kind='DELEGATED_MODEL_DIALOGUE',request_hash='e'*64,
            state='DONE',result=json.dumps(response),updated_at=self.base.before['updated_at']-10)
        value=persist_review(self.c.root,payload,request,response,turns,context=payload['context'],
            observed_at=self.base.before['updated_at']-5,model_operation=done if phase=='SCREENING'else None)
        self.review_files.append(self.c.root/(prefix(payload)+identity+'.json'))
        return value

    def test_actual_shape_three_assistant_uuid_changes_require_each_retained_authority(self):
        result=self.base.invoke();proof=result['observation'];after=self.base.row()
        self.assertEqual(result['error_count'],0)
        self.assertEqual(after['state'],'BOUND')
        witness=proof['room_recovery']['prefix_authority']
        self.assertEqual(witness['kind'],'PRIOR_NATIVE_REVIEWS_AND_USER_ANCHORS')
        self.assertEqual(witness['changed_assistant_rows'],[3,5,7])
        self.assertEqual([item['index']for item in witness['rows']],[3,5,7])
        self.assertEqual([len(item['followup_receipts'])for item in witness['rows']],[0,1,2])
        self.assertEqual(proof['room_recovery']['prior_record'],self.before)
        self.assertEqual(json.loads(after['anchors']),identities(self.c.raw._turns()))

    def test_changed_initial_text_cannot_reuse_valid_final_review_or_skip_missing_authority(self):
        original=copy.deepcopy(self.c.raw.messages)
        review=self.review_files[0];saved=review.read_bytes();intent=self.intent.read_bytes()
        cache=self.c.root/self.c.cache._name(self.screen);cache_bytes=cache.read_bytes()
        for mode in('changed-text','reuse-final-reply','missing-review','missing-intent','changed-user',
                'review-after-sent','foreign-room','unknown-model','extra-cache-field'):
            with self.subTest(mode=mode):
                self.c.raw.messages=copy.deepcopy(original)
                for path,raw in((review,saved),(self.intent,intent),(cache,cache_bytes)):
                    path.write_bytes(raw);path.chmod(0o600)
                if mode=='changed-text':self.c.raw.messages[3]['text']='A different selection.'
                if mode=='reuse-final-reply':self.c.raw.messages[5]['text']=self.c.raw.messages[7]['text']
                if mode=='missing-review':review.unlink()
                if mode=='missing-intent':self.intent.unlink()
                if mode=='changed-user':self.c.raw.messages[2]['text']+=' Changed.'
                if mode in('review-after-sent','foreign-room','unknown-model'):
                    value=json.loads(saved)
                    if mode=='review-after-sent':value['observed_at']=self.before['updated_at']+1
                    if mode=='foreign-room':value['conversation_id']='neurobro:2026-11-05:'+'f'*32
                    if mode=='unknown-model':value['model_operation']['state']='UNKNOWN'
                    review.write_text(json.dumps(value))
                if mode=='extra-cache-field':
                    value=json.loads(cache_bytes);value['unexpected']='not native cache';cache.write_text(json.dumps(value))
                result=self.base.invoke()
                self.assertEqual(self.base.row(),self.before)
                self.assertTrue(result['error_count'])
                self.assertFalse(result['operation_completed'])

    def test_future_reload_uses_exact_native_snapshot_without_old_review_fallback(self):
        self.assertEqual(self.base.invoke()['error_count'],0)
        for path in self.review_files:path.unlink()
        with self.c.room.store.open()as db:db.execute("UPDATE daily_rooms SET state='SENT'")
        for index in(3,5,7):self.c.raw.messages[index]['id']='another-native-id-'+str(index)
        result=self.base.invoke()
        self.assertEqual(result['error_count'],0)
        self.assertEqual(result['observation']['room_recovery']['prefix_authority']['kind'],'EXACT_NATIVE_PREFIX_SNAPSHOT')

    def test_loaded_thirty_row_window_without_first_marker_reopens_from_exact_snapshot(self):
        from agent.native_daily_room import retain_prefix_snapshot
        # Normal prepare persists the actually loaded tail, rather than all
        # older room history. Its original first marker can be outside it.
        prior=[]
        for number in range(11):
            prior.extend([dict(role='user',text='Prior window user '+str(number),attachments=[]),
                dict(role='assistant',text='Prior window answer '+str(number),id='window-'+str(number),complete=True)])
        self.c.raw.messages=prior+self.c.raw.messages
        old=self.c.raw._turns()[:30]
        with self.c.room.store.open()as db:
            binding=json.loads(self.before['binding']);binding.update(first_prompt_sha256=observer.text_sha('Older unloaded original prompt'),marker='HARUN_'+'f'*32)
            db.execute('UPDATE daily_rooms SET anchors=?,binding=?',(json.dumps(identities(old)),json.dumps(binding)))
        before=self.base.row();retain_prefix_snapshot(self.c.root,'2026-11-05','a'*32,old)
        for index in range(1,30,2):self.c.raw.messages[index]['id']='reloaded-window-'+str(index)
        # The reload is capped at thirty total rows: the old suffix of 28
        # followed by the real new SUI pair, rather than all 32 messages.
        saved=copy.deepcopy(self.c.raw.messages[-30:]);self.c.room.history.rooms=[('Existing partial window',saved)]
        self.c.raw.messages=[]
        result=self.base.invoke()
        self.assertEqual(result['error_count'],0)
        self.assertTrue(result['history_restored'])
        self.assertEqual(result['observation']['row_offset'],28)
        self.assertEqual(result['row_count'],30)
        self.assertEqual(result['observation']['room_recovery']['prefix_authority']['kind'],'EXACT_NATIVE_PREFIX_SNAPSHOT_WINDOW')
        self.assertEqual(result['observation']['room_recovery']['prefix_authority']['visible_row_offset'],2)
        self.assertEqual(len(json.loads(self.base.row()['anchors'])),32)
        self.assertEqual(self.base.row()['binding'],before['binding'])

    def test_exact_snapshot_rejects_changed_text_user_or_completion(self):
        from agent.native_daily_room import retain_prefix_snapshot
        # A future release already holds all original native IDs, unlike the
        # legacy migration above. Semantic drift cannot use a review fallback.
        rows=self.c.raw._turns()
        old=copy.deepcopy(rows[:8])
        for index in(3,5,7):old[index]['id']='snapshot-original-'+str(index)
        anchors=identities(old);retain_prefix_snapshot(self.c.root,'2026-11-05','a'*32,old)
        capture={'room':{'day':'2026-11-05','room_id':'a'*32,'anchors':anchors},'rows':rows}
        for mode in('text','user','complete'):
            value=copy.deepcopy(capture)
            if mode=='text':value['rows'][3]['text']='Changed reviewed text.'
            if mode=='user':value['rows'][2]['text']='Changed original user.'
            if mode=='complete':value['rows'][3]['complete']=False
            with self.subTest(mode=mode),self.assertRaises(ValueError):
                observer._sent_prefix_authority(self.c.room,value,self.before,8)


if __name__=='__main__':unittest.main()
