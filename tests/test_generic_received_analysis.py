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


if __name__=='__main__':unittest.main()
