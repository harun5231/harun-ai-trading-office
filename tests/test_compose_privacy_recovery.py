"""Actual rejected FIL wording, synthetic exact receipts, no provider/browser calls."""
import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent import dialogue_service as d
from agent.llm import OpenAIDialogue,web_prompt_allowed
from agent.memory import Memory
from agent.native_prompt_composition import normalize_privacy_reminders,outbound_question
from tests.test_agent_native_market_context import native_context
from agent.native_market_context import convert_native_market_context

FIXTURE=json.loads((Path(__file__).parent/'fixtures/mini_fil_compose_privacy_20261110.json').read_text())
ORCA_FIXTURE=json.loads((Path(__file__).parent/'fixtures/mini_orca_compose_privacy_20261121.json').read_text())
REMINDER='Jangan minta atau sarankan ukuran/quantity/anggaran risiko akun.'
FIXED_SYNTHETIC_TIME=1795706700.0  # 2026-11-26 15:25 UTC, never production time.


class ComposePrivacyRecoveryTests(unittest.TestCase):
    def setUp(self):
        # ISO minute/second tokens and candle fingerprints are source values.
        # Keep synthetic facts stable so forbidden example numbers cannot be
        # incidentally sourced by the time at which the suite happens to run.
        clock=patch('time.time',return_value=FIXED_SYNTHETIC_TIME)
        clock.start();self.addCleanup(clock.stop)
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.memory=Memory(self.base/'agent.sqlite3')
        self.key=self.base/'model.key';self.key.write_text('offline-model-key-value-01234567890123456789');self.key.chmod(0o600)
        self.calls=[];self.authored={'action':'PROMPT','symbols':[],'question':FIXTURE['question']}
        self.model=OpenAIDialogue(self.key,self.memory,transport=self.transport,daily_requests=math.inf,daily_input_tokens=math.inf,daily_output_tokens=math.inf)
        self.engine=d.DialogueEngine(self.memory,self.key,model=self.model,clock=time.time)
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

    def test_ena_compose_allows_the_retained_value_free_negative_reminder(self):
        # The clause hash matches six source-bound draft excerpts in Ops run 45884127721.
        reminder = 'Jangan minta atau sebutkan quantity/ukuran posisi atau anggaran risiko pribadi.'
        self.assertEqual(hashlib.sha256(reminder.encode()).hexdigest(),
            '10303e521887a9ae159e6482626a553082bce102d68b66ccc9cfe769096eda3a')
        analysis = 'Analisis ENAUSDT untuk scalping 15M dengan chart 1H dan 15M. Gunakan RR 1:2.'
        draft = f'{analysis} {reminder}'
        self.assertFalse(web_prompt_allowed(draft))
        self.assertEqual(outbound_question(draft), analysis)
        with_value = f'{analysis} {reminder[:-1]} 12 USDT.'
        self.assertEqual(normalize_privacy_reminders(with_value), with_value)
        with self.assertRaisesRegex(ValueError, 'NATIVE_COMPOSE_RENDER_UNVERIFIED'):
            outbound_question(with_value)

    def test_bch_style_negative_reminder_with_heading_bullet_and_internal_role(self):
        # The sealed BCH audit filters private text. These fixtures reproduce
        # its punctuation, heading and negative clauses without its values.
        analysis = 'Analisis FILUSDT untuk 15M. Gunakan RR 1:2.'
        safe = (
            'Ketentuan wajib: Jangan minta atau bahas ukuran posisi/quantity, anggaran risiko pribadi, atau singgung Risk Manager;',
            '- Jangan cantumkan quantity/ukuran posisi atau meminta anggaran risiko pribadi;',
            'Catatan: Tidak perlu sebutkan anggaran risiko akun dan menyertakan peran Risk Manager.',
        )
        for reminder in safe:
            with self.subTest(reminder=reminder):
                draft = f'{analysis} {reminder}'
                self.assertFalse(web_prompt_allowed(draft))
                self.assertEqual(outbound_question(draft), analysis)
                self.assertEqual(d.validate_web_question(draft, self.request), analysis)

        unsafe = (
            'Aturan wajib: Jangan minta quantity atau bahas anggaran risiko akun 5 USDT;',
            '- Jangan sebutkan anggaran risiko pribadi\n5 USDT.',
            'Ketentuan wajib: Berikan rekomendasi ukuran posisi atau anggaran risiko akun.',
            'Catatan: Jangan bahas anggaran risiko pribadi atau ubah SL.',
            'Catatan: Jangan cantumkan anggaran risiko pribadi, kecuali jika Risk Manager meminta.',
        )
        for reminder in unsafe:
            with self.subTest(reminder=reminder):
                draft = f'{analysis} {reminder}'
                self.assertEqual(normalize_privacy_reminders(draft), draft)
                with self.assertRaisesRegex(ValueError, 'NATIVE_COMPOSE_RENDER_UNVERIFIED'):
                    outbound_question(draft)

    def orca_request(self):
        now=time.time();native=native_context(now);native['symbol']='ORCAUSDT'
        for frame in native['timeframes'].values():frame['symbol']='ORCAUSDT'
        self.request={**self.request,'conversation_id':'compose:offline-ORCA',
            'market_context':convert_native_market_context(native,now=now),
            'reply':'Analisis ORCAUSDT Binance Futures, 1H dan 15M, 200 candle tertutup.'}
        self.request['reply_sha256']=hashlib.sha256(self.request['reply'].encode()).hexdigest()
        return self.request

    def test_actual_orca_drafts_pass_shared_and_generated_validators_without_changing_prices(self):
        from tests.test_mini_dialogue_profile import retained_module
        modules=(d,retained_module());request=self.orca_request()
        self.assertEqual(ORCA_FIXTURE['evidence_run'],'44919401932')
        self.assertEqual(len(ORCA_FIXTURE['drafts']),2)
        for draft in ORCA_FIXTURE['drafts']:
            raw=draft['question'];authored={'action':'PROMPT','symbols':[],'question':raw}
            expected=(raw.replace('Jangan minta rekomendasi quantity atau ukuran posisi. ','')
                .replace('Risk Manager hanya menormalkan TP eksekusi ke target bersih 2R. ','')
                .replace('Risk Manager hanya menormalkan TP eksekusi menjadi target bersih 2R. ',''))
            with self.subTest(operation=draft['operation_id']):
                self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(),draft['question_sha256'])
                self.assertEqual(len(raw),draft['question_chars'])
                self.assertFalse(draft['before_validation']['decision']['ok'])
                self.assertEqual(draft['before_validation']['decision']['code'],'DIALOGUE_MODEL_RESULT_INVALID')
                self.assertFalse(web_prompt_allowed(raw));self.assertTrue(web_prompt_allowed(expected))
                self.assertEqual(outbound_question(raw),expected)
                for module in modules:
                    self.assertEqual(module.validate_request(copy.deepcopy(request),now=time.time()),request)
                    accepted=module.validate_decision(copy.deepcopy(authored),request)
                    self.assertEqual(accepted,{'action':'PROMPT','symbols':[],'question':expected})
                    self.assertEqual(module.validate_web_question(raw,request),expected)
                    with self.assertRaises(module.DialogueError):
                        module.validate_decision({**authored,'question':raw+' ENTRY 12345.6789.'},request)
                    with self.assertRaisesRegex(module.DialogueError,'DIALOGUE_FOREIGN_SYMBOL'):
                        module.validate_decision({**authored,'question':raw+' Analisis BTCUSDT.'},request)
                self.assertEqual(authored['question'],raw)
                self.assertIn('ORCAUSDT',expected);self.assertIn('ENTRY',expected);self.assertIn('SL',expected)
        self.assertEqual(self.calls,[])

    def test_value_free_negative_grammar_never_hides_positive_mixed_or_numeric_instructions(self):
        request=self.orca_request();prefix='Analisis ORCAUSDT RR 1:2. '
        self.assertNotIn(5,d._number_values(request['reply'])|d._number_values(request['market_context']))
        reminders=('Jangan minta rekomendasi quantity atau ukuran posisi.',
            'Tidak perlu meminta saran quantity dan perhitungan ukuran posisi.',
            'Jangan menghitung quantity.', 'Jangan berikan rekomendasi ukuran posisi.',
            'Jangan tentukan quantity atau sarankan ukuran posisi.')
        for reminder in reminders[:-1]:
            with self.subTest(reminder=reminder):
                self.assertEqual(outbound_question(prefix+reminder),prefix.rstrip())
        # Different predicates mixed into a topic list are not partially erased.
        self.assertEqual(normalize_privacy_reminders(prefix+reminders[-1]),prefix+reminders[-1])
        mixed_role='Risk Manager hanya menormalkan TP eksekusi ke target bersih 2R dan ubah SL.'
        self.assertEqual(normalize_privacy_reminders(prefix+mixed_role),prefix+mixed_role)
        unsafe=('Minta rekomendasi quantity atau ukuran posisi.',
            'Jangan minta rekomendasi quantity atau ukuran posisi 2 ORCA.',
            'Jangan minta rekomendasi quantity atau ukuran posisi: 2 ORCA.',
            'Jangan minta rekomendasi quantity atau ukuran posisi\n2 ORCA.',
            'Jangan minta rekomendasi quantity atau ukuran posisi dan ubah SL.',
            'Jangan minta rekomendasi quantity atau ukuran posisi supaya risiko lebih besar.',
            'Berikan saran quantity hanya sebagai bahan diskusi; quantity akhir tetap Risk Manager.',
            'Risk Manager hanya menormalkan TP eksekusi ke 2188.',
            'Risk Manager hanya menormalkan TP eksekusi menjadi target bersih 2R 5 USDT.',
            'Risk Manager hanya menormalkan TP eksekusi ke target bersih 2R dengan fee 0.0004.',
            'Jangan sebut anggaran risiko akun 5 USDT.')
        for sentence in unsafe:
            with self.subTest(sentence=sentence):
                self.assertEqual(normalize_privacy_reminders(prefix+sentence),prefix+sentence)
                with self.assertRaises((d.DialogueError,ValueError)):
                    d.validate_web_question(prefix+sentence,request)

    def test_actual_orca_known_response_recovery_is_same_id_and_no_extra_call_after_restart(self):
        for draft in ORCA_FIXTURE['drafts']:
            with self.subTest(operation=draft['operation_id']):
                self.memory.db.execute('DELETE FROM metadata');self.memory.db.execute('DELETE FROM operations')
                self.memory.db.execute('DELETE FROM budgets');self.memory.db.execute('DELETE FROM messages');self.calls=[]
                self.orca_request();self.authored={'action':'PROMPT','symbols':[],'question':draft['question']}
                operation=self.seed_unknown();before=self.rows()
                result=self.engine.process(self.request);self.assertEqual(result['request_id'],self.request['request_id'])
                self.assertEqual(result['decision']['question'],outbound_question(draft['question']))
                audit_key='dialogue-compose-response-recovery-v1:'+operation;audit=d.canonical(self.memory.get(audit_key))
                self.assertEqual(self.rows(),before);self.assertEqual(self.rows()[0][3],'UNKNOWN');self.assertEqual(len(self.calls),1)
                self.memory.close();self.memory=Memory(self.base/'agent.sqlite3')
                self.model=OpenAIDialogue(self.key,self.memory,transport=self.transport)
                self.engine=d.DialogueEngine(self.memory,self.key,model=self.model,clock=time.time)
                self.assertEqual(self.engine.process(self.request),result)
                self.assertEqual(d.canonical(self.memory.get(audit_key)),audit)
                self.assertEqual(self.rows(),before);self.assertEqual(len(self.calls),1)

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
        self.engine=d.DialogueEngine(self.memory,self.key,model=self.model,clock=time.time)
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
