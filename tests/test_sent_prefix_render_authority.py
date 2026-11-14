"""An exact archived screening example rendering may seal an owned SENT room."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from agent import native_received_analysis as observation
from agent.native_daily_room import identities,retain_prefix_snapshot,read_prefix_snapshot,prefix_snapshot_matches
from tests import test_generic_received_analysis as generic


class SentPrefixRenderAuthorityTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.room=SimpleNamespace(store=SimpleNamespace(root=self.root))
        self.fixture=json.loads((Path(__file__).parent/'fixtures/screen3_sent_prefix_render_20261114.json').read_text())
        rendered=self.fixture['current_user_row'];self.current_user={key:rendered[key]for key in('role','text')}
        rendered_clause='Format: {"SYMBOLS":["",""]}.'
        authored_clause='Format: {"SYMBOLS":["<SIMBOL_USDT_1>","<SIMBOL_USDT_2>"]}.'
        self.assertEqual(rendered['text'].count(rendered_clause),1)
        self.authored_user={**self.current_user,'text':rendered['text'].replace(rendered_clause,authored_clause,1)}
        self.assertEqual(identities([self.current_user])[0],self.fixture['current_user_identity'])
        self.assertEqual(identities([self.authored_user])[0],self.fixture['authored_user_identity'])
        marker='HARUN_'+hashlib.sha256(b'source-first-prompt').hexdigest()[:32]
        self.old=[{'role':'user','text':'Original screening '+marker},
            {'role':'assistant','text':'Original prior response.','complete':True,'id':'prior-first'},self.authored_user,
            {'role':'assistant','text':'Original paired second response.','complete':True,'id':'prior-second'}]
        self.current=copy.deepcopy(self.old);self.current[2]=self.current_user
        self.current[3]['id']='reloaded-second'
        self.record={'day':'2026-11-14','room_id':'44a82f8094e64429b39f610b23cab583','anchors':identities(self.old),
            'first_prompt_sha256':hashlib.sha256(self.old[0]['text'].encode()).hexdigest(),'marker':marker,
            'history_titles':['Existing owned room'],'identifier_reconciliation':None}
        self.capture={'room':self.record,'rows':self.current}
        self.snapshot=retain_prefix_snapshot(self.root,self.record['day'],self.record['room_id'],self.old)

    def authority(self,count=4):
        return observation._sent_prefix_authority(self.room,self.capture,{'updated_at':1},count)

    def test_production_later_user_render_is_accepted_only_with_whole_archived_prefix(self):
        result=self.authority()
        self.assertEqual(result['kind'],'EXACT_NATIVE_PREFIX_SNAPSHOT_RENDERED_SCREENING')
        self.assertEqual(result['changed_prefix_rows'],[2,3]);self.assertEqual(result['snapshot'],self.snapshot)
        self.assertEqual(read_prefix_snapshot(self.root,self.record['day'],self.record['room_id'],self.record['anchors'])['value']['rows'],self.old)

    def test_missing_or_wrong_archive_cannot_grant_render_authority(self):
        path=self.root/self.snapshot['name'];raw=path.read_bytes();path.unlink()
        with self.assertRaisesRegex(ValueError,'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED'):self.authority()
        path.write_bytes(raw);path.chmod(0o600);self.record['room_id']='f'*32
        with self.assertRaisesRegex(ValueError,'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED'):self.authority()

    def test_changed_prompt_marker_price_number_or_assistant_text_remains_refused(self):
        for mutate in(lambda:self.current[2].update(text=self.current[2]['text'].replace('HYPEUSDT','BTCUSDT')),
            lambda:self.current[2].update(text=self.current[2]['text'].replace('tepat 2','tepat 1')),
            lambda:self.current[2].update(text=self.current[2]['text'].replace('HARUN_ca4591178093c5328078cf7d453f8284','HARUN_'+'f'*32)),
            lambda:self.current[2].update(text=self.current[2]['text']+' ENTRY 10.25 SL 10.05'),
            lambda:self.current[3].update(text='Different final answer.'),
            lambda:self.current[3].update(complete=False),
            lambda:self.record.update(marker='HARUN_'+'f'*32)):
            original=copy.deepcopy(self.current);record=copy.deepcopy(self.record)
            mutate()
            with self.assertRaisesRegex(ValueError,'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED'):self.authority()
            self.current[:]=original;self.record.clear();self.record.update(record)

    def test_repeated_placeholder_or_unverified_tail_and_identity_alias_are_refused(self):
        with self.assertRaisesRegex(ValueError,'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED'):self.authority(3)
        self.record['identifier_reconciliation']={'unverified':'alias'}
        with self.assertRaisesRegex(ValueError,'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED'):self.authority()
        self.record['identifier_reconciliation']=None
        self.current[2]['text']+=' <SIMBOL_USDT_1>'
        with self.assertRaisesRegex(ValueError,'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED'):self.authority()

    def test_assistant_only_translation_and_exact_prefix_keep_existing_authority(self):
        self.current[2]=copy.deepcopy(self.authored_user)
        result=self.authority();self.assertEqual(result['kind'],'EXACT_NATIVE_PREFIX_SNAPSHOT')
        self.assertEqual(result['changed_assistant_rows'],[3])
        self.current[:]=copy.deepcopy(self.old);self.assertIsNone(self.authority())

    def test_capped_suffix_and_unrelated_final_pair_never_authorize_a_whole_sent_prefix(self):
        # Existing room restoration may identify a capped suffix. A SENT seal
        # must verify the whole prior prefix rather than the suffix alone.
        binding={key:self.record[key]for key in('first_prompt_sha256','marker','history_titles')}
        self.current[:]=copy.deepcopy(self.old[2:]+[self.old[0],{'role':'assistant','text':'Unrelated final pair.','complete':True,'id':'unrelated'}])
        arguments=(self.root,self.record['day'],self.record['room_id'],self.record['anchors'],self.current)
        self.assertTrue(prefix_snapshot_matches(*arguments,binding=binding))
        self.assertFalse(prefix_snapshot_matches(*arguments,binding=binding,allow_window=False))
        with self.assertRaisesRegex(ValueError,'RECEIVED_ANALYSIS_SENT_PREFIX_UNVERIFIED'):self.authority()
        tail=self.old[-2:]
        self.assertTrue(prefix_snapshot_matches(self.root,self.record['day'],self.record['room_id'],self.record['anchors'],tail,binding=binding))
        self.assertFalse(prefix_snapshot_matches(self.root,self.record['day'],self.record['room_id'],self.record['anchors'],tail,binding=binding,allow_window=False))

    def test_existing_sent_seal_and_restart_remain_idempotent_without_order_or_model(self):
        case=generic.SentRoomCompletionObservationTests();case.setUp();self.addCleanup(case.doCleanups)
        c=case.c;old=c.raw._turns()[:6]
        old[2]=copy.deepcopy(self.authored_user);old[3]['attachments']=[]
        current=c.raw.messages;current[2]=copy.deepcopy(self.current_user);current[3]['id']='reloaded-history-reply'
        with c.room.store.open()as db:
            row=db.execute('SELECT * FROM daily_rooms').fetchone()
            db.execute('UPDATE daily_rooms SET anchors=?',(json.dumps(identities(old)),))
        retain_prefix_snapshot(c.root,'2026-11-05','a'*32,old)
        before=case.row();result=case.invoke();after=case.row()
        self.assertEqual(result['error_count'],0);self.assertEqual(after['state'],'BOUND')
        self.assertEqual(result['observation']['room_recovery']['prefix_authority']['kind'],'EXACT_NATIVE_PREFIX_SNAPSHOT_RENDERED_SCREENING')
        for key in('day','room_id','url','binding'):self.assertEqual(after[key],before[key])
        self.assertFalse(result['operation_completed']);self.assertEqual(case.invoke()['error_count'],0);self.assertEqual(case.row(),after)
        self.assertEqual((result['observation']['model_calls'],result['observation']['provider_messages'],result['observation']['orders_sent']),(0,0,0))


if __name__=='__main__':unittest.main()
