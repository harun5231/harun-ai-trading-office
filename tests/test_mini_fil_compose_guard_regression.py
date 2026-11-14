"""Verbatim 14-Nov FIL Mini drafts; all transports/market inputs are OFFLINE."""
import copy
import hashlib
import json
import re
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch

from agent import dialogue_service as current
from agent.llm import OpenAIDialogue,web_prompt_allowed
from agent.memory import Memory
from agent.native_prompt_composition import normalize_privacy_reminders,outbound_question
from tests import test_compose_privacy_recovery as legacy_privacy
from tests.test_mini_dialogue_profile import retained_module

FIXTURE=json.loads((Path(__file__).parent/'fixtures/mini_fil_compose_guard_20261114.json').read_text())


class ActualFilComposeGuardTests(unittest.TestCase):
 def setUp(self):self.services=(current,retained_module())

 def request(self,example):
  # These minimal numeric facts are enforced by the native material contract,
  # not a reconstruction of the omitted historical market candle arrays.
  return {**copy.deepcopy(example['original_request']),
   'market_context':{'symbol':'FILUSDT','candle_windows':{'1h':{'closed_count':200},'15m':{'closed_count':200}}}}

 def test_both_verbatim_rejected_drafts_are_accepted_without_changing_original_raw(self):
  for example in FIXTURE['examples']:
   self.assertEqual(hashlib.sha256(example['raw_decision']['question'].encode()).hexdigest(),example['question_sha256'])
   self.assertFalse(example['before_validation']['ok'])
   for service in self.services:
    with self.subTest(source=service.__name__,operation=example['operation_id']):
     raw=copy.deepcopy(example['raw_decision']);before=copy.deepcopy(raw)
     result=service.validate_decision(raw,self.request(example))
     self.assertEqual(result['action'],'PROMPT');self.assertEqual(result['symbols'],[])
     self.assertEqual(result['question'],outbound_question(raw['question']))
     self.assertEqual(raw,before);self.assertTrue(web_prompt_allowed(result['question']))
     self.assertNotIn('Risk Manager',result['question']);self.assertNotIn('quantity',result['question'])
     self.assertIn('FILUSDT',result['question']);self.assertIn('ENTRY',result['question']);self.assertIn('SL',result['question'])

 def test_mini_response_passes_the_browser_local_shared_result_validator(self):
  old=FIXTURE['browser_prompt_before']
  self.assertEqual(old['source_sha256'],'2e8887bd6a7edd4e659e3065c308a0f791a03f5cdbc6dd7cdf101679a0c3928d')
  prompt=types.ModuleType('agent.native_prompt_composition');prompt.re=re;prompt.web_prompt_allowed=web_prompt_allowed
  for name,source in old['functions'].items():
   self.assertEqual(hashlib.sha256(source.encode()).hexdigest(),old['function_sha256'][name])
   exec(compile(source,'source-pinned-old-browser-prompt','exec'),prompt.__dict__)
  for example in FIXTURE['examples']:
   request=dict(self.request(example),conversation_id='compose:offline-source-bound-FIL')
   decision=self.services[1].validate_decision(copy.deepcopy(example['raw_decision']),request)
   result={'schema_version':1,'ok':True,**{key:request[key]for key in('request_id','conversation_id','phase','reply_sha256')},
    'authority':'DIALOGUE_CONTROL_ONLY','trading_authority':False,'decision':decision,'needs_followup':False,'reason':'DIALOGUE_DECISION'}
   # The actual browser uses the generated Mini-only service, not canonical
   # Hermes-compatible source. Only its shared service validator must change;
   # its retained 2e888 prompt module can read the already normalized DTO.
   with patch.dict(sys.modules,{'agent.native_prompt_composition':prompt}):
    self.assertEqual(self.services[1].validate_result(copy.deepcopy(result),request),result)
    self.assertEqual(prompt.outbound_question(result['decision']['question']),result['decision']['question'])
   self.assertEqual(current.validate_result(copy.deepcopy(result),request),result)

 def test_ratio_alternative_is_explicit_rr_only_and_never_exempts_a_new_price(self):
  request={'phase':'COMPOSE_ANALYSIS','reply':'Analisis FILUSDT 1H dan 15M, RISK REWARD 1:2.','market_context':None}
  for service in self.services:
   for question in('RR 1:3 atau 1:4.','R:R 1:3/1:4 atau 1:5.','RISK REWARD minimal 1:3 atau 1:4.'):
    service.validate_question_sources(question,request)
   for question in('ENTRY 1:3 atau 1:4.','SL 1:3 atau 1:4.','TP 1:3 atau 1:4.',
    'RR 1:3 atau 1:4.1.','RR 1:3 atau 1:4, ENTRY 123456.789.',
    'RR 1:3 atau SL 123456.789.','Jangan gunakan ENTRY 123456.789.'):
    with self.subTest(source=service.__name__,question=question),self.assertRaisesRegex(service.DialogueError,'DIALOGUE_PRICE_PROPOSAL_REFUSED'):
     service.validate_question_sources(question,request)

 def test_only_entire_value_free_negative_quantity_clause_is_removed(self):
  for reminder in('Jangan minta atau tentukan quantity/ukuran posisi.','Jangan menentukan quantity.'):
   self.assertEqual(normalize_privacy_reminders('Analisa FILUSDT. '+reminder),'Analisa FILUSDT.')
  example=FIXTURE['examples'][1];request=self.request(example)
  for sentence in('Jangan minta atau tentukan quantity/ukuran posisi 2 FIL.',
   'Jangan minta atau tentukan quantity/ukuran posisi\n2 FIL.','Tentukan quantity 2 FIL.',
   'Berikan rekomendasi ukuran posisi.','Gunakan quantity 2 FIL.',
   'Jangan tentukan quantity atau ubah ENTRY.','Anggaran risiko akun 5 USDT.',
   'Risk Manager hanya menormalkan TP ke eksekusi 2188.'):
   self.assertEqual(normalize_privacy_reminders(sentence),sentence)
   for service in self.services:
    with self.subTest(source=service.__name__,sentence=sentence),self.assertRaises((service.DialogueError,ValueError)):
     service.validate_web_question(sentence,request)


class ActualFilComposeRecoveryTests(unittest.TestCase):
 def setUp(self):
  legacy_privacy.ComposePrivacyRecoveryTests.setUp(self)
  self.service=retained_module()
 def tearDown(self):legacy_privacy.ComposePrivacyRecoveryTests.tearDown(self)

 def transport(self,payload):
  self.calls.append(copy.deepcopy(payload))
  context={'operation_id':'delegated-model:'+self.request['request_id'],'phase':self.request['phase']}
  dispatch=self.model._api_dispatch(payload,context)
  result={'id':'resp_offline_fil_'+self.request['request_id'][:12],'status':'completed',
   'usage':{'input_tokens':23000,'output_tokens':405,'total_tokens':23405},
   'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(self.authored,ensure_ascii=False)}]}]}
  self.model._api_result(dispatch,status=200,result=result,response_received=True)
  return result

 def seed(self,example):
  self.request.update(copy.deepcopy(example['original_request']))
  self.authored=copy.deepcopy(example['raw_decision'])
  service=self.service;operation=example['operation_id'];memo=service.dialogue_operation_memo(self.request)
  def received_but_rejected():
   service._archive_compose_request(self.memory,operation,self.request,time.time())
   self.model.plan([],self.request['reply'],phase=self.request['phase'],jakarta_day='2026-11-14',context=self.request['market_context'],source_facts=self.request.get('source_facts'))
   raise service.DialogueError(example['before_validation']['code'])
  with self.assertRaisesRegex(service.DialogueError,example['before_validation']['code']):
   self.memory.operation(operation,'DELEGATED_MODEL_DIALOGUE',memo,received_but_rejected)
  self.engine=service.DialogueEngine(self.memory,self.key,model=self.model)
  return operation

 def test_exact_received_compose_recovery_is_idempotent_and_survives_restart_without_model_replay(self):
  for example in FIXTURE['examples']:
   with self.subTest(operation=example['operation_id']):
    operation=self.seed(example)
    before=list(self.memory.db.execute('SELECT * FROM operations'))
    metadata=list(self.memory.db.execute('SELECT * FROM metadata'))
    budget=list(self.memory.db.execute('SELECT * FROM budgets'));calls=len(self.calls)
    result=self.engine.process(copy.deepcopy(self.request))
    self.assertEqual(result['decision']['question'],outbound_question(example['raw_decision']['question']))
    self.assertEqual(list(self.memory.db.execute('SELECT * FROM operations')),before)
    for key,text in metadata:self.assertEqual(self.memory.get(key),json.loads(text))
    audit_key='dialogue-compose-response-recovery-v1:'+operation;audit=copy.deepcopy(self.memory.get(audit_key))
    self.assertEqual(audit['operation_preimage'][2],'UNKNOWN');self.assertEqual(audit['policy'],'VALUE_FREE_NEGATIVE_PRIVACY_REMINDER_V1')
    self.assertEqual(self.engine.process(copy.deepcopy(self.request)),result)
    self.memory.close();self.memory=Memory(self.base/'agent.sqlite3')
    self.model=OpenAIDialogue(self.key,self.memory,transport=self.transport)
    self.engine=self.service.DialogueEngine(self.memory,self.key,model=self.model)
    self.assertEqual(self.engine.process(copy.deepcopy(self.request)),result)
    self.assertEqual(self.memory.get(audit_key),audit);self.assertEqual(len(self.calls),calls)
    self.assertEqual(list(self.memory.db.execute('SELECT * FROM budgets')),budget)
    self.assertEqual(list(self.memory.db.execute('SELECT * FROM operations')),before)

 def test_missing_original_api_or_raw_retains_unknown_without_another_call(self):
  self.seed(FIXTURE['examples'][0]);self.memory.db.execute("DELETE FROM metadata WHERE key LIKE 'dialogue-model-api-result:%'")
  before=list(self.memory.db.execute('SELECT * FROM operations'));calls=len(self.calls)
  with self.assertRaisesRegex(self.service.DialogueError,'DIALOGUE_OPERATION_UNKNOWN'):self.engine.process(copy.deepcopy(self.request))
  self.assertEqual(len(self.calls),calls);self.assertEqual(list(self.memory.db.execute('SELECT * FROM operations')),before)


if __name__=='__main__':unittest.main()
