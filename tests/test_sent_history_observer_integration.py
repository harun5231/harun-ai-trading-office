"""Observe a received screening through real history matching and strict seal."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agent import native_received_analysis as observer
from agent import native_received_screening as screening
from agent.native_daily_room import DailyRoom,identities,retain_prefix_snapshot
from agent.native_screening_recovery import recover
from tests import test_native_daily_room as daily
from tests import test_native_received_analysis as legacy
from worker.account_state import screening_contract


class SentHistoryObserverIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.case=legacy.ReceivedAnalysisObservationTests()
        self.case.setUp();self.addCleanup(self.case.doCleanups);self.c=self.case
        c=self.c;self.fixture=json.loads((Path(__file__).parent/'fixtures/screen3_sent_prefix_render_20261114.json').read_text())
        self.assertEqual(observer.text_sha(self.fixture['current_reply']),self.fixture['current_reply_sha256'])
        first=daily.DailyRoomFirstCandidateEvidenceTests().actual_rows()
        first[0]['text']=first[0]['text'].replace('<SIMBOL_USDT_1>','').replace('<SIMBOL_USDT_2>','')
        first[1]['id']='a5fec3b3-7298-47a0-ae0f-3479692d8866'
        self.assertEqual(identities(first),self.fixture['room']['anchors'][:2])
        rendered=copy.deepcopy(self.fixture['current_user_row'])
        authored={**rendered,'text':rendered['text'].replace('Format: {"SYMBOLS":["",""]}.',
            'Format: {"SYMBOLS":["<SIMBOL_USDT_1>","<SIMBOL_USDT_2>"]}.',1)}
        self.assertEqual(identities([authored])[0],self.fixture['authored_user_identity'])
        # This paired prior assistant is explicitly synthetic: the actual
        # production transcript's omitted text is not reconstructed or invented.
        self.old=first+[authored,{'role':'assistant','text':'Synthetic prior source-bound answer.','complete':True,'id':'synthetic-old-assistant'}]
        prefix=copy.deepcopy(self.old);prefix[2]=rendered;prefix[3]['id']='synthetic-reloaded-assistant'
        operation=self.fixture['operation'];_,schema=screening_contract(2)
        context={'requested_count':2,'excluded_symbols':['DOTUSDT','FILUSDT','HYPEUSDT','ICPUSDT','XLMUSDT'],
            'replacement_instruction':'Choose different Binance USD-M USDT perpetual coins that are not in excluded_symbols.'}
        # The payload is a controlled fixture; production request SHA remains
        # separately retained in fixture provenance and is never forged here.
        body={'operation':operation,'marker':'HARUN_'+observer.text_sha(operation)[:32],'provider':'NEUROBRO_WEB',
            'contract_version':'neurobro-browser-decision-v2','prompt':'Pilih dua coin pengganti.','context':context,'output_schema':schema}
        self.payload={**body,'request_sha256':observer.native_digest(body)}
        cache={'schema_version':1,'operation':operation,'payload_sha256':observer.native_digest(self.payload),'model_request_id':'a'*64,
            'rendered_prompt':self.fixture['current_rendered_prompt'],'rendered_sha256':observer.text_sha(self.fixture['current_rendered_prompt'])}
        path=c.root/c.cache._name(self.payload);path.write_text(json.dumps(cache));path.chmod(0o600)
        self.owned=prefix+[{'role':'user','text':cache['rendered_prompt'],'attachments':[]},
            {'role':'assistant','text':self.fixture['current_reply'],'complete':True,'id':'648eef13-2cbe-4bcc-888d-420a7b9ffd59'}]
        self.binding={key:self.fixture['room'][key]for key in('first_prompt_sha256','marker','history_titles')}
        self.day='2026-11-14';self.room_id=self.fixture['room']['room_id']
        with c.room.store.open()as db:
            db.execute('DELETE FROM daily_rooms')
            db.execute('INSERT INTO daily_rooms VALUES(?,?,?,?,?,?,?)',(self.day,self.room_id,'SENT',c.raw.page.url,
                json.dumps(identities(self.old)),json.dumps(self.binding),1))
        retain_prefix_snapshot(c.root,self.day,self.room_id,self.old)
        self.foreign=[{'role':'user','text':'Foreign manually selected TIA/WLD review.'},
            {'role':'assistant','text':'Unrelated previous decision.','complete':True,'id':'foreign-assistant'}]
        c.raw.messages=copy.deepcopy(self.foreign);c.room.history.rooms=[('Pilih Dua Coin Menguntungkan',copy.deepcopy(self.owned))]
        self.request={'action':screening.OBSERVE,'payload':self.payload}
        self.before=self.row();self.payload_before=copy.deepcopy(self.payload)

    def row(self):
        with self.c.room.store.open()as db:
            values=db.execute('SELECT * FROM daily_rooms').fetchone()
            return dict(zip(('day','room_id','state','url','anchors','binding','updated_at'),values))

    def invoke(self):
        with self.c.actor.claim:return recover(self.c.actor,self.c.delegated,self.request)

    def test_actual_legacy_render_needs_read_only_bound_view_without_changing_sent_state(self):
        args=(self.owned,identities(self.old),self.binding)
        self.assertFalse(DailyRoom.matches(*args,'SENT',archive_root=self.c.root,prefix_identity=(self.day,self.room_id)))
        self.assertTrue(DailyRoom.matches(*args,'BOUND',archive_root=self.c.root,prefix_identity=(self.day,self.room_id)))
        self.assertEqual(self.row(),self.before)

    def test_foreign_nonempty_through_real_restore_scope_strict_seal_and_restart(self):
        original=DailyRoom.matches;events=[];seal=observer._seal_sent_room
        def tracked(rows,anchors,binding,state,**kwargs):
            events.append((state,self.row()['state']))
            return original(rows,anchors,binding,state,**kwargs)
        def checked_seal(*args,**kwargs):
            self.assertEqual(self.row()['state'],'SENT');self.assertEqual(args[4]['stable_snapshot_reads'],4)
            value=seal(*args,**kwargs);self.assertEqual(self.row()['state'],'BOUND');return value
        with patch.object(DailyRoom,'matches',side_effect=tracked),patch.object(observer,'_seal_sent_room',side_effect=checked_seal):
            result=self.invoke()
        self.assertEqual(result['error_count'],0);self.assertTrue(result['history_restored'])
        self.assertTrue(events);self.assertTrue(all(actual=='SENT'for _,actual in events))
        self.assertTrue(all(view=='BOUND'for view,_ in events))
        proof=result['observation'];self.assertEqual(proof['room_initial_state'],'SENT')
        self.assertTrue(proof['operation_scope']['verified']);self.assertTrue(proof['operation_scope']['reply_complete'])
        self.assertEqual(proof['stable_snapshot_reads'],4);self.assertEqual(proof['room_recovery']['state'],'SEALED')
        self.assertEqual(proof['rows'][-1]['text'],self.fixture['current_reply']);self.assertFalse(result['operation_completed'])
        self.assertEqual((proof['model_calls'],proof['provider_messages'],proof['uploads'],proof['orders_sent']),(0,0,0,0))
        after=self.row()
        for key in('day','room_id','url','binding'):self.assertEqual(after[key],self.before[key])
        self.assertEqual(self.payload,self.payload_before)
        # Recreate the existing room wrapper as after a process restart; use
        # the same real matching/restoration code and immutable archive/cache.
        c=self.c;old_room=c.room;c.raw._turns=old_room.original_turns;c.raw._visible=old_room.original_visible
        c.room=DailyRoom(c.raw,c.native,c.root,history=daily.History(c.raw));c.delegated.daily_room=c.room
        c.raw._fresh_conversation=c.raw.ask
        c.room.history.rooms=[('Pilih Dua Coin Menguntungkan',copy.deepcopy(self.owned))];c.raw.messages=copy.deepcopy(self.foreign)
        again=self.invoke();self.assertEqual(again['error_count'],0);self.assertTrue(again['history_restored'])
        self.assertEqual(self.row(),after);self.assertFalse(again['operation_completed'])
        self.assertEqual((again['observation']['model_calls'],again['observation']['provider_messages'],again['observation']['orders_sent']),(0,0,0))

    def test_malformed_prior_user_cannot_restore_or_seal(self):
        self.c.room.history.rooms[0][1][2]['text']+=' Changed excluded coin or price.'
        result=self.invoke()
        self.assertTrue(result['history_restore_attempted']);self.assertFalse(result['history_restored'])
        self.assertGreater(result['error_count'],0);self.assertEqual(self.row(),self.before)
        self.assertFalse(result['operation_completed']);self.assertEqual(self.payload,self.payload_before)

    def test_window_uses_exact_reconstructed_archive_for_visible_legacy_render(self):
        record={**self.binding,'day':self.day,'room_id':self.room_id,'anchors':identities(self.old)}
        value={'room':record,'rows':copy.deepcopy(self.owned[2:]),'operation_scope':{'latest_user':2}}
        before=self.row();result=observer._sent_window_alignment(self.c.room,value)
        self.assertIsNotNone(result);self.assertEqual(result['omitted'],2)
        archived=[{key:item for key,item in row.items()if key!='attachments'}for row in self.old[:2]]
        self.assertEqual(result['rows'],archived+value['rows']);self.assertEqual(self.row(),before)


class SentWindowClosureTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from types import SimpleNamespace
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name);self.room=SimpleNamespace(store=SimpleNamespace(root=self.root))
        marker='HARUN_'+observer.text_sha('original window first')[:32]
        self.old=[]
        for index in range(4):
            self.old.extend([{'role':'user','text':('Original first '+marker)if index==0 else'Prior user '+str(index)},
                {'role':'assistant','text':'Prior answer '+str(index),'complete':True,'id':'answer-'+str(index)}])
        self.record={'day':'2026-11-14','room_id':'a'*32,'anchors':identities(self.old),
            'first_prompt_sha256':observer.text_sha(self.old[0]['text']),'marker':marker,'history_titles':[]}
        self.archive=retain_prefix_snapshot(self.root,self.record['day'],self.record['room_id'],self.old)
        self.new=[{'role':'user','text':'Current verified request'},{'role':'assistant','text':'Current complete reply','complete':True,'id':'current'}]

    def test_shifted_suffix_with_unrelated_final_historical_pair_cannot_close_window(self):
        prior=copy.deepcopy(self.old[-4:]+[self.old[0],{'role':'assistant','text':'Unrelated historical answer','complete':True,'id':'foreign'}])
        value={'room':self.record,'rows':prior+self.new,'operation_scope':{'latest_user':6}}
        self.assertIsNone(observer._sent_window_alignment(self.room,value))

    def test_legitimate_exact_capped_suffix_keeps_original_omitted_archive_rows(self):
        prior=copy.deepcopy(self.old[-6:]);prior[1]['id']='reloaded-existing-assistant'
        value={'room':self.record,'rows':prior+self.new,'operation_scope':{'latest_user':6}}
        before=(self.root/self.archive['name']).read_bytes();result=observer._sent_window_alignment(self.room,value)
        self.assertEqual(result['omitted'],2);self.assertEqual(result['rows'],self.old[:2]+value['rows'])
        self.assertEqual(result['authority']['snapshot'],self.archive)
        self.assertEqual((self.root/self.archive['name']).read_bytes(),before)


if __name__=='__main__':unittest.main()
