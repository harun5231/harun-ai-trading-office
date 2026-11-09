"""Actual rejected FIL wording, synthetic exact receipts, no provider/browser calls."""
import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import time
import unittest

from agent import dialogue_service as d
from agent.llm import OpenAIDialogue,web_prompt_allowed
from agent.memory import Memory
from agent.native_prompt_composition import normalize_privacy_reminders,outbound_question
from tests.test_agent_native_market_context import native_context
from agent.native_market_context import convert_native_market_context

FIXTURE=json.loads((Path(__file__).parent/'fixtures/mini_fil_compose_privacy_20261110.json').read_text())
REMINDER='Jangan minta atau sarankan ukuran/quantity/anggaran risiko akun.'


class ComposePrivacyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.memory=Memory(self.base/'agent.sqlite3')
        self.key=self.base/'model.key';self.key.write_text('offline-model-key-value-01234567890123456789');self.key.chmod(0o600)
        self.calls=[];self.authored={'action':'PROMPT','symbols':[],'question':FIXTURE['question']}
        self.model=OpenAIDialogue(self.key,self.memory,transport=self.transport,daily_requests=math.inf,daily_input_tokens=math.inf,daily_output_tokens=math.inf)
        self.engine=d.DialogueEngine(self.memory,self.key,model=self.model)
        now=time.time();native=native_context(now);native['symbol']='FILUSDT';native['mark_price']['price']='0.77725194'
        for frame in native['timeframes'].values():frame['symbol']='FILUSDT'
        context=convert_native_market_context(native,now=now);reply='Analisis FILUSDT Binance Futures, 1H dan15M. Mark price 0.77725194.'
        self.request={'schema_version':1,'request_id':'c'*64,'conversation_id':'compose:offline-FIL',
            'phase':'COMPOSE_ANALYSIS','reply':reply,'reply_sha256':hashlib.sha256(reply.encode()).hexdigest(),
            'market_context':context,'gate':{'robot_on':True,'native_dispatch_verified':True},'deadline_epoch':now+80}
    def tearDown(self):self.memory.close();self.temp.cleanup()
    def transport(self,payload):
        self.calls.append(copy.deepcopy(payload));context={'operation_id':'delegated-model:'+self.request['request_id'],'phase':self.request['phase']}
        dispatch=self.model._api_dispatch(payload,context)
        result={'id':'resp_offline_known_compose','status':'completed','usage':{'input_tokens':23000,'output_tokens':497,'total_tokens':23497},
            'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(self.authored,ensure_ascii=False)}]}]}
        self.model._api_result(dispatch,status=200,result=result,response_received=True)
        return result
    def seed_unknown(self):
        request=self.request;operation='delegated-model:'+request['request_id'];memo=d.dialogue_operation_memo(request)
        def failed_after_received_response():
            d._archive_compose_request(self.memory,operation,request,time.time())
            self.model.plan([],request['reply'],phase=request['phase'],jakarta_day='2026-11-10',context=request['market_context'])
            raise d.DialogueError('DIALOGUE_MODEL_RESULT_INVALID')
        with self.assertRaisesRegex(d.DialogueError,'DIALOGUE_MODEL_RESULT_INVALID'):
            self.memory.operation(operation,'DELEGATED_MODEL_DIALOGUE',memo,failed_after_received_response)
        return operation
    def rows(self):return list(self.memory.db.execute('SELECT * FROM operations'))
    def update_metadata(self,prefix,change,*,rehash=False):
        key,text=self.memory.db.execute('SELECT key,value FROM metadata WHERE key LIKE ?',(prefix+'%',)).fetchone()
        value=json.loads(text);change(value)
        if rehash:
            if prefix=='dialogue-model-raw:':identity={key:item for key,item in value.items()if key!='observed_at'};new=prefix+hashlib.sha256(d.canonical(identity).encode()).hexdigest()
            elif prefix=='dialogue-model-api-result:':new=prefix+hashlib.sha256(d.canonical(value).encode()).hexdigest()
            else:new=key
            self.memory.db.execute('DELETE FROM metadata WHERE key=?',(key,));key=new
        self.memory.db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',(key,d.canonical(value)))
    def test_actual_926_char_question_only_loses_the_value_free_negative_reminder(self):
        raw=FIXTURE['question'];self.assertEqual(len(raw),926);self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(),FIXTURE['question_sha256'])
        self.assertFalse(web_prompt_allowed(raw));self.assertFalse(FIXTURE['before_validation']['validate_raw_decision']['ok'])
        expected=raw.replace(REMINDER+' ','').replace('Risk Manager hanya menormalkan TP ke target bersih 2R saat eksekusi. ','');self.assertEqual(outbound_question(raw),expected)
        decision=d.validate_decision(copy.deepcopy(self.authored),self.request)
        self.assertEqual(decision['question'],expected);self.assertEqual(self.authored['question'],raw)
        self.assertEqual(d.validate_web_question(raw,self.request),expected)
        self.assertIn('mark_price 0.77725194',expected);self.assertIn('FILUSDT',expected)
    def test_normalizer_never_removes_values_fees_or_quantity_execution_directives(self):
        prefix='Analisa FILUSDT, RR 1:2. '
        unsafe=('Jangan minta atau sarankan ukuran/quantity/anggaran risiko akun 5 USDT.',
            'Jangan minta atau sarankan ukuran/quantity/anggaran risiko akun: 5 USDT.',
            'Jangan sebut fee_entry_rate 0.0004.', 'Sarankan quantity 2 FIL.',
            'Risk Manager hanya menormalkan TP eksekusi ke 2188.',
            'Jangan sebut anggaran risiko akun\n5 USDT.',
            'Jangan minta quantity\n2 FIL.',
            'Gunakan quantity 2 FIL.',
            'Jangan minta anggaran risiko akun supaya quantity lebih besar.')
        for sentence in unsafe:
            with self.subTest(sentence=sentence):
                raw=prefix+sentence;self.assertEqual(normalize_privacy_reminders(raw),raw)
                with self.assertRaises(d.DialogueError):d.validate_web_question(raw,self.request)
        mixed=prefix+'Jangan minta quantity atau ubah ENTRY.'
        self.assertEqual(normalize_privacy_reminders(mixed),mixed) # No partial-clause deletion or new policy.
        with self.assertRaises(ValueError):outbound_question(REMINDER)
    def test_web_summary_omits_only_exact_internal_role_clauses_without_hiding_order_facts(self):
        from agent.native_prompt_composition import compact_protocol
        raw='Neurobro menetapkan ENTRY dan SL; Risk Manager hanya menormalkan TP eksekusi. Analisa FILUSDT RR 1:2.'
        self.assertEqual(normalize_privacy_reminders(raw),'Neurobro menetapkan ENTRY dan SL; Analisa FILUSDT RR 1:2.')
        facts='Order terpasang ENTRY 2248, SL 2265, TP 2188; Risk Manager memulihkan proteksi order tersebut.'
        self.assertEqual(normalize_privacy_reminders(facts),facts)
        rendered=compact_protocol('Analisa FILUSDT.\n\nOUTPUT_JSON_SCHEMA:\n{}',{'marker':'owned_marker','output_schema':{'required':['symbol','side','limit_entry','take_profit','stop_loss','risk_reward']}})
        self.assertNotIn('Risk Manager',rendered);self.assertIn('RISK REWARD 1:2',rendered)
        self.assertIn('ENTRY dan SL ditetapkan Neurobro',rendered);self.assertIn('RR 1:3 atau 1:4',rendered)

    def test_known_response_uses_exact_input_and_dispatch_and_retains_unknown_across_restart(self):
        operation=self.seed_unknown();before=self.rows();metadata=list(self.memory.db.execute('SELECT * FROM metadata'));budget=list(self.memory.db.execute('SELECT * FROM budgets'))
        encoded,payload=d.compose_recovery_payload(self.request,[],1024);self.assertEqual(payload,self.calls[0])
        incoming=copy.deepcopy(self.request);incoming['deadline_epoch']+=1
        result=self.engine.process(incoming);self.assertEqual(result['decision']['question'],outbound_question(FIXTURE['question']))
        self.assertEqual(result['request_id'],self.request['request_id']);self.assertEqual(len(self.calls),1);self.assertEqual(self.rows(),before)
        for key,text in metadata:self.assertEqual(self.memory.db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()[0],text)
        audit_key='dialogue-compose-response-recovery-v1:'+operation;audit=self.memory.get(audit_key)
        self.assertEqual(audit['operation_preimage'][2],'UNKNOWN');self.assertEqual(audit['model_input_sha256'],hashlib.sha256(encoded).hexdigest())
        self.assertEqual(list(self.memory.db.execute('SELECT * FROM budgets')),budget);self.assertEqual(self.memory.counts()['messages'],0)
        saved=d.canonical(audit);self.assertEqual(self.engine.process(incoming),result);self.assertEqual(d.canonical(self.memory.get(audit_key)),saved)
        self.memory.close();self.memory=Memory(self.base/'agent.sqlite3');self.model=OpenAIDialogue(self.key,self.memory,transport=self.transport)
        self.engine=d.DialogueEngine(self.memory,self.key,model=self.model)
        self.assertEqual(self.engine.process(incoming),result);self.assertEqual(len(self.calls),1);self.assertEqual(self.rows(),before)
        self.assertEqual(d.canonical(self.memory.get(audit_key)),saved)
    def test_existing_recovery_audit_is_append_only_and_cannot_supply_changed_text(self):
        operation=self.seed_unknown();self.engine.process(self.request)
        key='dialogue-compose-response-recovery-v1:'+operation;audit=self.memory.get(key)
        audit['result']['decision']['question']='Invented changed result'
        changed=d.canonical(audit);self.memory.db.execute('UPDATE metadata SET value=? WHERE key=?',(changed,key))
        before=self.rows()
        with self.assertRaisesRegex(d.DialogueError,'DIALOGUE_OPERATION_UNKNOWN'):self.engine.process(self.request)
        self.assertEqual(self.rows(),before);self.assertEqual(len(self.calls),1)
        self.assertEqual(self.memory.db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()[0],changed)

    def test_missing_archive_legacy_4d_and_missing_api_or_raw_stay_unknown_without_calls(self):
        for missing in('dialogue-compose-request-v1:','dialogue-model-api-result:','dialogue-model-raw:'):
            with self.subTest(missing=missing):
                self.memory.db.execute('DELETE FROM metadata');self.memory.db.execute('DELETE FROM operations');self.memory.db.execute('DELETE FROM budgets');self.calls=[]
                self.seed_unknown();self.memory.db.execute('DELETE FROM metadata WHERE key LIKE ?',(missing+'%',));before=self.rows()
                with self.assertRaisesRegex(d.DialogueError,'DIALOGUE_OPERATION_UNKNOWN'):self.engine.process(self.request)
                self.assertEqual(self.rows(),before);self.assertEqual(len(self.calls),1)
                self.assertEqual(self.memory.db.execute("SELECT COUNT(*) FROM metadata WHERE key LIKE 'dialogue-compose-response-recovery-v1:%'").fetchone()[0],0)
    def test_tampered_api_raw_archive_or_input_never_authorize_recovery(self):
        cases=(('dialogue-model-api-result:',lambda value:value.update(http_status=500),True),
            ('dialogue-model-api-result:',lambda value:value.update(request_sent=None),True),
            ('dialogue-model-raw:',lambda value:value.update(model_input_sha256='0'*64),True),
            ('dialogue-model-raw:',lambda value:value.update(native_reply_sha256='0'*64),True),
            ('dialogue-compose-request-v1:',lambda value:value.update(original_request_sha256='0'*64),False),
            ('dialogue-model-api-dispatch:',lambda value:value.update(request_payload_sha256='0'*64),False))
        for prefix,change,rehash in cases:
            with self.subTest(prefix=prefix):
                self.memory.db.execute('DELETE FROM metadata');self.memory.db.execute('DELETE FROM operations');self.memory.db.execute('DELETE FROM budgets');self.calls=[]
                self.seed_unknown();self.update_metadata(prefix,change,rehash=rehash);before=self.rows()
                with self.assertRaisesRegex(d.DialogueError,'DIALOGUE_OPERATION_UNKNOWN'):self.engine.process(self.request)
                self.assertEqual(self.rows(),before);self.assertEqual(len(self.calls),1)
    def test_actual_private_risk_or_changed_history_is_not_a_recoverable_reminder(self):
        for change in('private','history'):
            with self.subTest(change=change):
                self.memory.db.execute('DELETE FROM metadata');self.memory.db.execute('DELETE FROM operations');self.memory.db.execute('DELETE FROM budgets');self.memory.db.execute('DELETE FROM messages');self.calls=[]
                self.authored['question']=FIXTURE['question']+(' Anggaran risiko akun 5 USDT.'if change=='private'else'')
                self.seed_unknown()
                if change=='history':self.memory.append(self.request['conversation_id'],'user','Unrelated changed composition history',time.time())
                with self.assertRaisesRegex(d.DialogueError,'DIALOGUE_OPERATION_UNKNOWN'):self.engine.process(self.request)
                self.assertEqual(len(self.calls),1);self.assertEqual(self.memory.counts()['operations'],{'UNKNOWN':1})


if __name__=='__main__':unittest.main()
