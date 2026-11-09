"""Funded compose requests retain original identity before any provider call."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

from agent.llm import OpenAIDialogue
from agent.memory import Memory,canonical
from agent.models import NotSent
from tests.test_agent_dialogue_service import request
from tests.test_mini_dialogue_profile import retained_module


class MiniComposeArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.memory=Memory(self.root/'agent.sqlite3');self.key=self.root/'key'
        self.key.write_text('unit-model-key-value-01234567890123456789');self.key.chmod(0o600)
        self.service=retained_module();self.calls=[];self.decision={'action':'PROMPT','symbols':[],
            'question':'Mohon analisis struktur BTCUSDT berdasarkan chart Futures terlampir.'}
        self.model=OpenAIDialogue(self.key,self.memory,transport=self.transport)
        self.engine=self.service.DialogueEngine(self.memory,self.key,model=self.model)
        self.value=request(phase='ANALYSIS',reply='Analisis BTCUSDT berdasarkan chart Futures terlampir.')
        self.value['phase']='COMPOSE_ANALYSIS';self.operation='delegated-model:'+self.value['request_id']
        self.archive_key='dialogue-compose-request-v1:'+self.operation

    def tearDown(self):self.memory.close();self.temporary.cleanup()

    def transport(self,payload):
        self.calls.append(payload)
        archived=self.memory.get(self.archive_key)
        self.assertIsNotNone(archived,'source must be archived BEFORE transport')
        self.assertEqual(self.memory.db.execute('SELECT requests FROM budgets').fetchone()[0],1)
        return {'status':'completed','id':'resp_offline_archive_proof','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps(self.decision)}]}]}

    def test_funded_archive_preserves_exact_request_and_first_deadline_across_replay_restart(self):
        result=self.engine.process(self.value);archive=self.memory.get(self.archive_key)
        self.assertEqual(archive['authority'],'VALIDATED_COMPOSE_REQUEST')
        self.assertEqual(archive['original_request'],self.value)
        self.assertEqual(archive['original_request_sha256'],hashlib.sha256(canonical(self.value).encode()).hexdigest())
        self.assertEqual(archive['request_hash'],self.memory.db.execute('SELECT request_hash FROM operations').fetchone()[0])
        changed=copy.deepcopy(self.value);changed['deadline_epoch']+=1
        self.assertEqual(self.engine.process(changed),result)
        self.memory.close();self.memory=Memory(self.root/'agent.sqlite3')
        self.model=OpenAIDialogue(self.key,self.memory,transport=self.transport)
        self.engine=self.service.DialogueEngine(self.memory,self.key,model=self.model)
        changed['deadline_epoch']+=1;self.assertEqual(self.engine.process(changed),result)
        self.assertEqual(self.memory.get(self.archive_key),archive)
        self.assertEqual(len(self.calls),1)

    def test_exhausted_budget_and_zero_cost_literal_make_no_full_compose_archive(self):
        today=time.strftime('%Y-%m-%d',time.gmtime(time.time()+7*3600))
        old=(today,377,193000,23000);self.memory.db.execute('INSERT INTO budgets VALUES(?,?,?,?)',old)
        with self.assertRaisesRegex(self.service.DialogueError,'DIALOGUE_MODEL_NOT_SENT'):
            self.engine.process(self.value)
        from worker.prompts import SCREENING
        literal=request(index=10,reply=SCREENING);literal['phase']='COMPOSE_SCREENING'
        self.assertEqual(self.engine.process(literal)['decision']['question'],SCREENING)
        self.assertEqual(self.calls,[])
        self.assertIsNone(self.memory.get(self.archive_key))
        self.assertEqual(self.memory.db.execute("SELECT COUNT(*) FROM metadata WHERE key LIKE 'dialogue-compose-request-v1:%'").fetchone()[0],0)
        self.assertEqual(tuple(self.memory.db.execute('SELECT * FROM budgets').fetchone()),old)

    def test_archive_write_failure_prevents_provider_but_keeps_conservative_reservation(self):
        original=self.memory.put
        def put(key,value):
            if key.startswith('dialogue-compose-request-v1:'):raise OSError('offline storage failure')
            return original(key,value)
        with patch.object(self.memory,'put',side_effect=put),self.assertRaisesRegex(
                self.service.DialogueError,'DIALOGUE_MODEL_NOT_SENT'):
            self.engine.process(self.value)
        self.assertEqual(self.calls,[]);self.assertIsNone(self.memory.get(self.archive_key))
        self.assertEqual(self.memory.db.execute('SELECT requests FROM budgets').fetchone()[0],1)
        self.assertEqual(json.loads(self.memory.db.execute('SELECT result FROM operations').fetchone()[0]),
            {'_agent_not_sent':'DIALOGUE_REQUEST_ARCHIVE_UNAVAILABLE'})

    def test_received_invalid_model_output_keeps_source_and_raw_proof_without_replay(self):
        self.decision={'action':'PROMPT','symbols':['BTCUSDT'],'question':'Invalid compose symbol array.'}
        with self.assertRaises(self.service.DialogueError):self.engine.process(self.value)
        archive=self.memory.get(self.archive_key)
        raw=json.loads(self.memory.db.execute("SELECT value FROM metadata WHERE key LIKE 'dialogue-model-raw:%'").fetchone()[0])
        self.assertTrue(raw['json_decoded']);self.assertEqual(raw['native_reply_sha256'],archive['original_request']['reply_sha256'])
        self.assertEqual(self.memory.counts()['operations'],{'UNKNOWN':1})
        self.memory.close();self.memory=Memory(self.root/'agent.sqlite3')
        self.engine=self.service.DialogueEngine(self.memory,self.key,model=OpenAIDialogue(self.key,
            self.memory,transport=Mock(side_effect=AssertionError('must not replay UNKNOWN'))))
        with self.assertRaisesRegex(self.service.DialogueError,'DIALOGUE_OPERATION_UNKNOWN'):
            self.engine.process(self.value)
        self.assertEqual(self.memory.get(self.archive_key),archive);self.assertEqual(len(self.calls),1)

    def test_duplicate_business_request_reuses_first_source_but_conflicting_archive_is_refused(self):
        memo={key:value for key,value in self.value.items() if key!='deadline_epoch'}
        digest=hashlib.sha256(canonical(memo).encode()).hexdigest()
        self.memory.db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?)',
            (self.operation,'DELEGATED_MODEL_DIALOGUE',digest,'STARTED',None,time.time()))
        self.service._archive_compose_request(self.memory,self.operation,self.value,time.time())
        first=self.memory.get(self.archive_key);changed=copy.deepcopy(self.value);changed['deadline_epoch']+=1
        self.service._archive_compose_request(self.memory,self.operation,changed,time.time())
        self.assertEqual(self.memory.get(self.archive_key),first)
        bad=copy.deepcopy(first);bad['original_request']['reply']='Conflicting source.'
        self.memory.put(self.archive_key,bad)
        with self.assertRaisesRegex(NotSent,'DIALOGUE_REQUEST_ARCHIVE_UNAVAILABLE'):
            self.service._archive_compose_request(self.memory,self.operation,changed,time.time())
        self.assertEqual(self.memory.get(self.archive_key),bad);self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
