"""Optional exact WEB history never rewrites an already sealed review."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from agent import dialogue_service as d
from agent import native_analysis_receipt as receipt
from tests.test_dialogue_conversation_context import context_request


class ReviewContextCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.request=context_request(pairs=2);ctx=self.request['conversation_context'];self.turns=ctx['turns']
        self.payload={'operation':ctx['native_operation'],'request_sha256':ctx['native_request_sha256'],
            'marker':'offline_native_marker','context':None,'output_schema':{'required':['symbols']}}
        self.response={'schema_version':1,'ok':True,**{key:self.request[key]for key in('request_id','conversation_id','phase','reply_sha256')},
            'authority':'DIALOGUE_CONTROL_ONLY','trading_authority':False,
            'decision':{'action':'SCREENING','symbols':['BTCUSDT'],'question':''},'needs_followup':False,'reason':'DIALOGUE_DECISION'}
        self.observed=time.time()
    def write(self,request,observed=None):
        return receipt.persist_review(self.root,self.payload,request,self.response,self.turns,
            context=None,observed_at=self.observed if observed is None else observed)
    def saved(self):
        rows=receipt.read_reviews(self.root,self.payload);self.assertEqual(len(rows),1)
        path=self.root/rows[0]['name'];return rows,path,path.read_bytes(),path.stat().st_mtime_ns
    def test_legacy_receipt_and_deadline_remain_byte_identical_when_new_context_arrives(self):
        legacy={key:value for key,value in self.request.items()if key!='conversation_context'}
        original=self.write(legacy);rows,path,raw,mtime=self.saved()
        current=copy.deepcopy(self.request);current['deadline_epoch']+=10
        self.assertEqual(self.write(current,self.observed+1),original)
        self.assertEqual(path.read_bytes(),raw);self.assertEqual(path.stat().st_mtime_ns,mtime)
        self.assertEqual(receipt.read_reviews(self.root,self.payload),rows)
        self.assertNotIn('conversation_context',original['request'])
        self.assertEqual(original['request']['deadline_epoch'],legacy['deadline_epoch'])
    def test_new_context_receipt_remains_exact_when_a_legacy_caller_reads_it(self):
        original=self.write(self.request);rows,path,raw,mtime=self.saved()
        legacy={key:value for key,value in self.request.items()if key!='conversation_context'};legacy['deadline_epoch']+=5
        self.assertEqual(self.write(legacy,self.observed+2),original)
        self.assertEqual(path.read_bytes(),raw);self.assertEqual(path.stat().st_mtime_ns,mtime)
        self.assertEqual(receipt.read_reviews(self.root,self.payload),rows)
    def test_context_tamper_is_refused_before_any_receipt_is_created(self):
        value=copy.deepcopy(self.request);value['conversation_context']['turns'][0]['text']='Different WEB question'
        with self.assertRaisesRegex(d.DialogueError,'DIALOGUE_OPERATION_ID_MISMATCH'):self.write(value)
        self.assertEqual(list(self.root.iterdir()),[])
    def test_changed_sealed_context_is_never_hidden_by_optional_field_exclusion(self):
        self.write(self.request);_,path,raw,_=self.saved();value=json.loads(raw)
        value['request']['conversation_context']['turns'][0]['text']='Changed sealed history'
        changed=json.dumps(value,sort_keys=True,separators=(',',':')).encode();path.write_bytes(changed)
        with self.assertRaisesRegex(d.DialogueError,'DIALOGUE_OPERATION_ID_MISMATCH'):self.write(self.request,self.observed+3)
        self.assertEqual(path.read_bytes(),changed)
    def test_context_cannot_be_attached_to_another_payload_or_an_unobserved_turn(self):
        self.payload['request_sha256']='b'*64
        with self.assertRaisesRegex(ValueError,'NATIVE_ANALYSIS_REVIEW_UNVERIFIED'):self.write(self.request)
        self.assertEqual(list(self.root.iterdir()),[])


if __name__=='__main__':unittest.main()
