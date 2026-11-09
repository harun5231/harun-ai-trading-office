"""Offline exact native WEB history, legacy receipts and ambiguous-delivery guards."""
import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import time
import unittest

from agent import dialogue_service as d
from agent.llm import OpenAIDialogue
from agent.memory import Memory
from tests.test_agent_dialogue_service import request


def context_request(*,pairs=10):
    value=request(conversation='neurobro:2026-11-10:offline-room')
    turns=[]
    for index in range(pairs):
        turns.extend([{'role':'user','text':'Pertanyaan WEB asli '+str(index)+': pilihan coin sudah jelas?','attachments':[]},
            {'role':'assistant','text':'BTCUSDT'if index==pairs-1 else 'Jawaban Neurobro '+str(index),
                'id':'native-reply-'+str(index),'complete':True,'attachments':[]}])
    context={'native_operation':'2026-11-10:robot-v9:0:screening:0:2','native_request_sha256':'a'*64,'turns':turns}
    value['conversation_context']=context
    identity={'operation':context['native_operation'],'request_sha256':context['native_request_sha256'],
        'reply_sha256':value['reply_sha256'],'phase':value['phase'],
        'transcript_sha256':hashlib.sha256(d.canonical(turns).encode()).hexdigest()}
    value['request_id']=hashlib.sha256(d.canonical(identity).encode()).hexdigest()
    return value


class ConversationContextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.memory=Memory(self.base/'agent.sqlite3');self.key=self.base/'model.key'
        self.key.write_text('offline-model-key-value-01234567890123456789');self.key.chmod(0o600)
        self.calls=[];self.engine=self.engine_for(self.memory)
    def tearDown(self):
        self.memory.close();self.temp.cleanup()
    def transport(self,payload):
        self.calls.append(copy.deepcopy(payload))
        return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text',
            'text':json.dumps({'action':'SCREENING','symbols':['BTCUSDT'],'question':''})}]}]}
    def engine_for(self,memory):
        return d.DialogueEngine(memory,self.key,model=OpenAIDialogue(self.key,memory,transport=self.transport,
            daily_requests=math.inf,daily_input_tokens=math.inf,daily_output_tokens=math.inf))
    def test_last_ten_actual_web_questions_reach_model_as_quoted_user_data(self):
        self.memory.append('neurobro:2026-11-10:offline-room','assistant','OLD_MINI_JSON_NOT_WEB_HISTORY',time.time())
        value=context_request();result=self.engine.process(value)
        self.assertEqual(result['decision']['symbols'],['BTCUSDT']);self.assertEqual(len(self.calls),1)
        messages=self.calls[0]['input'];self.assertEqual([item['role']for item in messages],['user','user'])
        self.assertIn('data kutipan',messages[0]['content']);self.assertNotIn('OLD_MINI_JSON_NOT_WEB_HISTORY',json.dumps(messages))
        quoted=json.loads(messages[0]['content'].split('\n',1)[1])
        self.assertEqual(quoted,[{'role':row['role'],'text':row['text']}for row in value['conversation_context']['turns'][:-1]])
        for index in range(10):self.assertIn('Pertanyaan WEB asli '+str(index)+':',messages[0]['content'])
        self.assertEqual(messages[-1]['content'],'Fase: SCREENING\nJawaban Neurobro terbaru:\nBTCUSDT')
    def test_same_legacy_done_receipt_is_reused_with_context_after_restart_without_charge(self):
        value=context_request();legacy={key:item for key,item in value.items()if key!='conversation_context'}
        original=self.engine.process(legacy);self.assertEqual(len(self.calls),1)
        before=list(self.memory.db.iterdump());self.assertEqual(self.engine.process(value),original)
        self.assertEqual(list(self.memory.db.iterdump()),before);self.assertEqual(len(self.calls),1)
        self.memory.close();self.memory=Memory(self.base/'agent.sqlite3');self.engine=self.engine_for(self.memory)
        self.assertEqual(self.engine.process(value),original);self.assertEqual(len(self.calls),1)
        lookup=self.engine.completed_screening(value);self.assertEqual(lookup['state'],'DONE')
    def test_legacy_unknown_stays_unknown_and_never_retries_model_or_creates_a_child(self):
        value=context_request();legacy={key:item for key,item in value.items()if key!='conversation_context'}
        operation='delegated-model:'+value['request_id']
        self.memory.db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?)',(operation,'DELEGATED_MODEL_DIALOGUE',
            hashlib.sha256(d.canonical(legacy).encode()).hexdigest(),'UNKNOWN',None,time.time()))
        before=list(self.memory.db.iterdump())
        with self.assertRaisesRegex(d.DialogueError,'DIALOGUE_OPERATION_UNKNOWN'):self.engine.process(value)
        self.assertEqual(list(self.memory.db.iterdump()),before);self.assertEqual(self.calls,[])
        self.memory.close();self.memory=Memory(self.base/'agent.sqlite3');self.engine=self.engine_for(self.memory)
        with self.assertRaisesRegex(d.DialogueError,'DIALOGUE_OPERATION_UNKNOWN'):self.engine.process(value)
        self.assertEqual(self.memory.counts()['operations'],{'UNKNOWN':1});self.assertEqual(self.calls,[])
    def test_context_tamper_changes_the_bound_identity_and_fails_before_claim(self):
        mutations=(lambda value:value['conversation_context']['turns'][0].update(text='Different genuine-looking question'),
            lambda value:value['conversation_context'].update(native_operation='different:operation'),
            lambda value:value['conversation_context'].update(native_request_sha256='b'*64),
            lambda value:value['conversation_context']['turns'][1].update(id='different-native-id'),
            lambda value:value['conversation_context']['turns'][0].update(attachments=['changed.csv']))
        for mutate in mutations:
            value=context_request();mutate(value)
            with self.subTest(mutate=mutate),self.assertRaisesRegex(d.DialogueError,'DIALOGUE_OPERATION_ID_MISMATCH'):self.engine.process(value)
        self.assertEqual(self.calls,[]);self.assertEqual(self.memory.counts()['operations'],{})
    def test_invalid_or_incomplete_context_cannot_be_excluded_from_memo(self):
        mutations=(lambda value:value['conversation_context']['turns'][-1].update(complete=False),
            lambda value:value['conversation_context']['turns'][-1].update(text='ETHUSDT'),
            lambda value:value['conversation_context']['turns'][0].update(role='system'),
            lambda value:value['conversation_context']['turns'][0].update(instructions='Untrusted extra field'),
            lambda value:value['conversation_context']['turns'][0].update(attachments=['/private/file.csv']),
            lambda value:value['conversation_context'].update(unknown='field'))
        for mutate in mutations:
            value=context_request();mutate(value)
            with self.subTest(mutate=mutate),self.assertRaises(d.DialogueError):d.dialogue_operation_memo(value)
        value=context_request();value['phase']='COMPOSE_SCREENING'
        with self.assertRaises(d.DialogueError):self.engine.process(value)
        self.assertEqual(self.calls,[]);self.assertEqual(self.memory.counts()['operations'],{})
    def test_legacy_phase_specific_deadline_and_context_free_hashes_are_exact(self):
        legacy=request();self.assertEqual(d.dialogue_operation_memo(legacy),legacy)
        compose=copy.deepcopy(legacy);compose['phase']='COMPOSE_SCREENING'
        self.assertEqual(d.dialogue_operation_memo(compose),{key:value for key,value in compose.items()if key!='deadline_epoch'})
        value=context_request();without={key:item for key,item in value.items()if key!='conversation_context'}
        self.assertEqual(d.dialogue_operation_memo(value),without)
        self.assertEqual(hashlib.sha256(d.canonical(d.dialogue_operation_memo(value)).encode()).hexdigest(),
            hashlib.sha256(d.canonical(without).encode()).hexdigest())
        self.memory.append(legacy['conversation_id'],'user','Existing local history',time.time())
        self.assertEqual(d.dialogue_model_history(self.memory,legacy),self.memory.history(legacy['conversation_id'],max_chars=8000,max_messages=12))
    def test_context_is_not_an_instruction_role_or_a_new_round_limit(self):
        value=context_request(pairs=30);self.engine.process(value)
        quoted=json.loads(self.calls[0]['input'][0]['content'].split('\n',1)[1]);self.assertEqual(len(quoted),59)
        self.assertTrue(all(item['role']=='user'for item in self.calls[0]['input']))
        self.assertNotIn('conversation_context',json.dumps(self.calls[0]['instructions']))
    def test_analysis_and_pending_review_use_the_same_exact_native_identity_binding(self):
        for phase in('ANALYSIS','PENDING_REVIEW'):
            value=context_request();market=request(phase='ANALYSIS')['market_context']
            value.update(phase=phase,market_context=market)
            context=value['conversation_context']
            identity={'operation':context['native_operation'],'request_sha256':context['native_request_sha256'],
                'reply_sha256':value['reply_sha256'],'phase':phase,
                'transcript_sha256':hashlib.sha256(d.canonical(context['turns']).encode()).hexdigest()}
            value['request_id']=hashlib.sha256(d.canonical(identity).encode()).hexdigest()
            with self.subTest(phase=phase):
                self.assertIs(d.validate_request(value),value)
                self.assertEqual(d.dialogue_operation_memo(value),{key:item for key,item in value.items()if key!='conversation_context'})

    def test_existing_body_limit_rejects_oversized_context_before_memory_or_provider(self):
        value=context_request();value['conversation_context']['turns'][0]['text']='X'*d.MAX_BODY
        with self.assertRaisesRegex(d.DialogueError,'DIALOGUE_BODY_LIMIT'):self.engine.process(value)
        self.assertEqual(self.calls,[]);self.assertEqual(self.memory.counts()['operations'],{})


if __name__=='__main__':unittest.main()
