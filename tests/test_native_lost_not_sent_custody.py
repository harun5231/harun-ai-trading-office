"""Lost HTTP returns recover only the latest positive native NOT_SENT proof."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from agent import native_received_continuation as continuation
from agent import native_received_analysis as observer
from tests import test_native_received_continuation as fixtures


class LostNotSentCustodyTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.ReceivedProofTests('test_private_capture_and_current_four_file_native_witness_seed_exact_existing_turns')
        self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.result=self.case.invoke_failure()
        self.current=copy.deepcopy(self.case.capture)
        self.current['finished_at']=self.case.grant['started_at']+1
        self.root,self.payload=self.case.root,self.case.payload
        self.base='analysis-continuation-'+self.case.grant['grant_id']

    def read(self):return continuation.read_not_sent(self.root,self.payload,self.current)

    def test_actual_private_positive_closure_is_returned_with_exact_custody_and_no_writes(self):
        paths={p.name:p.read_bytes()for p in self.root.iterdir()if p.is_file()}
        proof=self.read()
        self.assertEqual(proof['kind'],continuation.NOT_SENT_CUSTODY)
        self.assertEqual(proof['latest_attempt'],1)
        self.assertEqual(proof['grant_id'],self.case.grant['grant_id'])
        for key in ('admission','disposition'):
            entry=proof[key];raw=(self.root/entry['name']).read_bytes()
            self.assertEqual(entry['sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(entry['bytes'],len(raw));self.assertEqual(entry['value'],json.loads(raw))
        self.assertEqual(proof['source_semantics_sha256'],proof['current_semantics_sha256'])
        self.assertEqual(proof['mutation_markers'],dict(upload_started=False,send_attempted=False,completed=False))
        self.assertEqual({p.name:p.read_bytes()for p in self.root.iterdir()if p.is_file()},paths)

    def test_latest_unclosed_attempt_hides_older_positive_closure(self):
        continuation._admit(self.root,self.case.grant,self.case.grant['source_capture_name'])
        self.assertIsNone(self.read())

    def test_partial_unrelated_loaded_prefix_keeps_exact_owned_message_custody(self):
        before=continuation._semantic(self.current)
        self.current['rows']=self.current['rows'][2:]
        self.current['file_tiles']=[{**item,'row':item['row']-2}for item in self.current['file_tiles']]
        scope=self.current['operation_scope']
        for key in ('initial_user','latest_user','reply_row'):scope[key]-=2
        scope['user_rows']=[index-2 for index in scope['user_rows']]
        witness=self.current['room_witness']
        for key in ('initial_analysis_user_rows','followup_user_rows'):
            witness[key]=[index-2 for index in witness[key]]
        self.assertEqual(observer._project_operation(self.current)['row_offset'],4)
        self.assertEqual(continuation._semantic(self.current),before)
        self.assertEqual(self.read()['latest_attempt'],1)
        self.current['rows'][-1]['id']='foreign-final-native-id'
        with self.assertRaises(ValueError):self.read()

    def test_any_upload_send_or_completed_marker_prevents_positive_recovery(self):
        for kind in ('upload-started','send-attempted','completed'):
            name=self.base+'.'+kind+'.json'
            continuation._write(self.root,name,dict(grant_id=self.case.grant['grant_id']))
            self.assertIsNone(self.read())
            (self.root/name).unlink()

    def test_foreign_room_changed_intents_or_forged_disposition_cannot_recover(self):
        for mutate in (lambda c:c['room'].update(room_id='f'*32),
                lambda c:next(iter(c['receipt_files'].values()))['value'].update(initial_reply_sha256='f'*64)):
            changed=copy.deepcopy(self.current);mutate(changed)
            with self.assertRaises(ValueError):continuation.read_not_sent(self.root,self.payload,changed)
        path=self.root/(self.base+'.closed-not-sent.json');value=json.loads(path.read_text())
        value['admission']['sha256']='f'*64;path.write_bytes(observer.canonical(value))
        with self.assertRaises(ValueError):self.read()

    def test_observer_exposes_proof_inside_durable_v2_capture_without_resuming(self):
        c=self.case.fixture.case
        with c.actor.claim,patch.object(observer.time,'time',return_value=self.current['finished_at']):
            result=observer.observe(c.actor,c.delegated,dict(action=observer.GENERIC_ACTION,payload=self.payload))
        proof=result['observation']['continuation_not_sent']
        self.assertEqual(proof['kind'],continuation.NOT_SENT_CUSTODY)
        self.assertFalse(result['operation_completed'])
        private=json.loads((self.root/result['capture_name']).read_text())
        self.assertEqual(private['continuation_not_sent'],proof)
        self.assertEqual(result['observation_sha256'],observer.digest(result['observation']))
        self.assertEqual(private['uploads'],0);self.assertEqual(private['provider_messages'],0)


if __name__=='__main__':unittest.main()
