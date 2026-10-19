"""Exercise the additive hook, including the actual sanitized native ask AST locally."""
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent.native_browser_hook import DelegatedDOMBridge, install_native_bridge


class NotSubmitted(Exception):pass
class OutcomeUnknown(Exception):pass


class Composer:
 def __init__(self,bridge):self.bridge=bridge;self.value=''
 def input_value(self):return self.value
 def fill(self,value,**kwargs):self.value=value


class Button:
 def __init__(self,bridge):self.bridge=bridge
 def is_enabled(self):return True
 def click(self,**kwargs):
  b=self.bridge;b.clicks+=1
  b.messages.append({'role':'native_user_role','text':b.composer.value,'attachments':[]})
  b.composer.value=''
  text=b.initial if b.clicks==1 else b.final
  b.messages.append({'role':'native_assistant_role','text':text,'complete':True,'id':'native-'+str(b.clicks)})
  if b.after_click:b.after_click(b)


class Page:
 def __init__(self,bridge):self.bridge=bridge;self.url='https://fixture.invalid/chat/owned'
 def wait_for_timeout(self,milliseconds):self.bridge.current+=milliseconds/1000


class Bridge:
 def __init__(self,initial='BTCUSDT needs clarification',final='FINAL_NATIVE_REPLY'):
  self.initial,self.final=initial,final;self.current=0.0;self.clock=lambda:self.current
  self.timeout=90;self.operation_deadline=None;self._operation_contract=None
  self._dom_contract=SimpleNamespace(composer='native-composer',send='native-send')
  self.page=Page(self);self.composer=Composer(self);self.button=Button(self)
  self.uploaded_names=[];self.messages=[];self.clicks=0;self.fresh_calls=0;self.after_click=None
 def status(self):return 'CONNECTED'
 def _fresh_conversation(self):self.fresh_calls+=1;self.messages=[];return self.composer
 def _upload(self,*args):return []
 def _timeout(self,_):return 5000
 def _until(self,seconds):return min(self.current+seconds,self.operation_deadline)
 def _visible(self,selector):return [self.composer] if selector=='native-composer' else [self.button]
 def _turns(self):return copy.deepcopy(self.messages)
 def _answer(self,prompt,attachment_names=()):
  rows=self._turns()
  if len(rows)!=2 or rows[0]['text']!=prompt or rows[1]['complete'] is not True:return None
  return rows[1]['text']
 def ask(self,payload,dispatch_check,*,chart_images=(),chart_evidence=(),deadline=None,receipt_callback=None):
  self.operation_deadline=deadline;self._operation_contract=self._dom_contract
  sent=False
  try:
   composer=self._fresh_conversation();composer.fill(payload['prompt'])
   if dispatch_check() is not True:raise NotSubmitted('ROBOT_OFF')
   sent=True;self.button.click()
   previous=None;stable=0
   until=self._until(self.timeout)
   while self.clock()<until:
    answer=self._answer(payload['prompt'],())
    stable=stable+1 if answer is not None and answer==previous else 0
    previous=answer
    if answer is not None and stable>=3:
     result={'output':None,'assistant_text':answer,
      'response_id':'native:'+hashlib.sha256((self.page.url+'\n'+payload['marker']+'\n'+answer).encode()).hexdigest(),
      'chart_evidence':[]}
     if receipt_callback:receipt_callback(result)
     return result
    self.page.wait_for_timeout(250)
   raise OutcomeUnknown()
  except NotSubmitted:
   if sent:raise OutcomeUnknown() from None
   raise
  except Exception:
   if sent:raise OutcomeUnknown() from None
   raise
  finally:self._operation_contract=None


def request():
 return {'operation':'2026-10-19:screening:1','marker':'native_marker',
  'request_sha256':'a'*64,'prompt':'EXACT ORIGINAL USER PROMPT',
  'output_schema':{'required':['symbols']},'context':None}


def client(action='CLARIFY'):
 def run(value):
  return {'schema_version':1,'ok':True,
   **{key:value[key] for key in ('request_id','conversation_id','phase','reply_sha256')},
   'authority':'DIALOGUE_CONTROL_ONLY','trading_authority':False,
   'decision':{'action':action,'symbols':[],'question':'Coin mana yang perlu diperjelas?' if action=='CLARIFY' else ''},
   'needs_followup':action=='CLARIFY','reason':'DIALOGUE_DECISION'}
 return Mock(request=Mock(side_effect=run))


def native():
 return SimpleNamespace(OutcomeUnknown=OutcomeUnknown,NotSubmitted=NotSubmitted,MAX_ANSWER=65536,
  image_artifacts=Mock(return_value=[]),exact_json=Mock(return_value=None))


class NativeHookTests(unittest.TestCase):
 def setup_hook(self,bridge=None,reasoner=None,module=None):
  self.bridge=bridge or Bridge();self.reasoner=reasoner or client();self.module=module or native()
  self.enabled=True
  self.gate=Mock(side_effect=lambda:self.enabled)
  self.hook=DelegatedDOMBridge(self.bridge,self.reasoner,native=self.module,
   converter=Mock(),monotonic=self.bridge.clock,wall_clock=time.time)
  return self.hook

 def test_initial_prompt_native_response_id_and_one_final_same_chat_reply_preserved(self):
  hook=self.setup_hook();original=self.bridge._answer
  result=hook.ask(request(),self.gate,deadline=80)
  self.assertEqual(self.bridge.clicks,2);self.assertEqual(self.bridge.fresh_calls,1)
  self.assertEqual(self.bridge.messages[0]['text'],request()['prompt'])
  self.assertTrue(self.bridge.messages[2]['text'].startswith('Coin mana'))
  self.assertEqual(result['assistant_text'],self.bridge.final)
  expected=hashlib.sha256((self.bridge.page.url+'\n'+request()['marker']+'\n'+self.bridge.final).encode()).hexdigest()
  self.assertTrue(result['response_id'].endswith(expected))
  self.assertEqual(set(result),{'output','assistant_text','response_id','chart_evidence'})
  self.assertEqual(self.bridge._answer,original)
  self.reasoner.request.assert_called_once();self.assertGreaterEqual(self.gate.call_count,4)
  self.assertTrue(all(item[0][0]['gate']=={'robot_on':True,'native_dispatch_verified':True}
                      for item in self.reasoner.request.call_args_list))

 def test_native_complete_setup_does_not_debate_even_if_model_requests_question(self):
  module=native();module.exact_json.return_value={'symbols':['BTCUSDT']}
  hook=self.setup_hook(module=module)
  result=hook.ask(request(),self.gate,deadline=80)
  self.assertEqual(result['assistant_text'],self.bridge.initial)
  self.assertEqual(self.bridge.clicks,1);self.reasoner.request.assert_called_once()

 def test_malformed_initial_json_can_receive_one_question_without_inventing_output(self):
  module=native();module.exact_json.side_effect=ValueError('malformed initial JSON')
  hook=self.setup_hook(module=module)
  result=hook.ask(request(),self.gate,deadline=80)
  self.assertEqual(result['assistant_text'],self.bridge.final)
  self.assertEqual(self.bridge.clicks,2)

 def test_model_stop_returns_original_reply_and_does_not_supply_prices_or_setup(self):
  hook=self.setup_hook(reasoner=client('COMPLETE'))
  result=hook.ask(request(),self.gate,deadline=80)
  self.assertEqual(self.bridge.clicks,1)
  self.assertEqual(result['assistant_text'],self.bridge.initial)
  self.assertIsNone(result['output'])

 def test_robot_off_before_initial_send_makes_no_browser_or_model_request(self):
  hook=self.setup_hook();self.enabled=False
  with self.assertRaises(NotSubmitted):hook.ask(request(),self.gate,deadline=80)
  self.assertEqual(self.bridge.clicks,0);self.reasoner.request.assert_not_called()

 def test_robot_off_after_model_call_prevents_followup_and_keeps_unknown_semantics(self):
  reasoner=client();original=reasoner.request.side_effect
  def run(value):self.enabled=False;return original(value)
  reasoner.request.side_effect=run
  hook=self.setup_hook(reasoner=reasoner)
  with self.assertRaises(OutcomeUnknown):hook.ask(request(),self.gate,deadline=80)
  self.assertEqual(self.bridge.clicks,1)

 def test_foreign_model_authority_or_prices_are_rejected_before_second_browser_send(self):
  for altered in ({'trading_authority':True},
                  {'decision':{'action':'CLARIFY','symbols':[],'question':'Set ENTRY: 999?'}}):
   reasoner=client();original=reasoner.request.side_effect
   def run(value,altered=altered):result=original(value);result.update(altered);return result
   reasoner.request.side_effect=run;hook=self.setup_hook(reasoner=reasoner)
   with self.subTest(altered=altered),self.assertRaises(OutcomeUnknown):
    hook.ask(request(),self.gate,deadline=80)
   self.assertEqual(self.bridge.clicks,1)

 def test_changed_prefix_or_extra_turns_or_followup_attachments_refuse_final_receipt(self):
  def changed(bridge):
   if bridge.clicks==2:bridge.messages[0]['text']='foreign prompt'
  def extra(bridge):
   if bridge.clicks==2:bridge.messages.append({'role':'foreign','text':'unowned'})
  def uploaded(bridge):
   if bridge.clicks==2:bridge.messages[2]['attachments']=['unexpected.png']
  for change in (changed,extra,uploaded):
   bridge=Bridge();bridge.after_click=change;hook=self.setup_hook(bridge=bridge)
   with self.subTest(change=change.__name__),self.assertRaises(OutcomeUnknown):
    hook.ask(request(),self.gate,deadline=80)

 def test_native_budget_is_never_extended_by_model_or_followup(self):
  reasoner=client();original=reasoner.request.side_effect
  def slow(value):self.bridge.current+=100;return original(value)
  reasoner.request.side_effect=slow;hook=self.setup_hook(reasoner=reasoner)
  with self.assertRaises(OutcomeUnknown):hook.ask(request(),self.gate,deadline=10)
  self.assertEqual(self.bridge.clicks,1)

 def test_probe_callback_and_unconfigured_factory_preserve_existing_native_path(self):
  hook=self.setup_hook();callback=Mock()
  result=hook.ask(request(),self.gate,deadline=80,receipt_callback=callback)
  self.reasoner.request.assert_not_called();callback.assert_called_once()
  self.assertEqual(result['assistant_text'],self.bridge.initial)
  with patch.dict(os.environ,{},clear=True):self.assertIs(install_native_bridge(self.bridge),self.bridge)
  with patch.dict(os.environ,{'HARUN_AGENT_DIALOGUE_SOCKET':'/run/agent.sock'},clear=True):
   with self.assertRaises(ValueError):install_native_bridge(self.bridge)

 def test_analysis_review_uses_actual_native_context_converter_without_private_risk_values(self):
  from agent.native_market_context import convert_native_market_context
  from tests.test_agent_native_market_context import native_context
  hook=self.setup_hook(reasoner=client('COMPLETE'))
  hook.converter=convert_native_market_context
  value=request();value['context']=native_context(time.time())
  value['output_schema']={'required':['symbol','side','entry_price','stop_loss','take_profit','risk_reward']}
  result=hook.ask(value,self.gate,deadline=80,chart_images=('native-one','native-two'),
   chart_evidence=({'timeframe':'1h'},{'timeframe':'15m'}))
  actual=self.reasoner.request.call_args.args[0]
  self.assertEqual(actual['phase'],'ANALYSIS')
  self.assertEqual(actual['market_context']['provenance']['chart_kind'],'NATIVE_PUBLIC_KLINES_CONTEXT')
  self.assertNotIn('risk_constraints',json.dumps(actual))
  self.assertNotIn('CONTRACT_SETTINGS_NOT_FOR_MODEL',json.dumps(actual))
  self.assertEqual(result['assistant_text'],self.bridge.initial)
  self.assertEqual(self.bridge.clicks,1)
  self.module.image_artifacts.assert_any_call(('native-one','native-two'),
   ({'timeframe':'1h'},{'timeframe':'15m'}))

 def test_actual_native_turns_ast_attests_original_and_zero_followup_attachments_separately(self):
  source=Path('/tmp/harun-reconciled-production-source/browser_runtime/dom.py')
  if not source.is_file():self.skipTest('Verified private native source absent in ordinary CI')
  cls=next(node for node in ast.parse(source.read_text()).body if isinstance(node,ast.ClassDef) and node.name=='DOMBridge')
  method=copy.deepcopy(next(node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name=='_turns'))
  contract=object()
  class Items:
   def __init__(self,items):self.items=items
   def count(self):return len(self.items)
   def nth(self,index):return self.items[index]
  class Image:
   def __init__(self,name):self.name=name
   def is_visible(self):return True
   def evaluate(self,_):return True
   def get_attribute(self,_):return self.name
  class Footer:
   def __init__(self,complete):self.complete=complete
   def count(self):return 1 if self.complete else 0
   def locator(self,_):return self
  class Body:
   def __init__(self,row):self.row=row
   def count(self):return 1
   def text_content(self):return self.row['text']
   def locator(self,_):return Footer(self.row.get('complete',False))
  class Turn:
   def __init__(self,row,index):self.row,self.index=row,index
   def get_attribute(self,_):return 'message-'+str(self.index)
   def locator(self,selector):
    if selector=='user-body':return Body(self.row) if self.row['user'] else Items([])
    if selector=='assistant-body':return Items([]) if self.row['user'] else Body(self.row)
    return Items([Image(name) for name in self.row.get('attachments',[])])
  namespace={'MESSAGE':'messages','USER_BODY':'user-body','ASSISTANT_BODY':'assistant-body',
   'RAIL_INPUT_GROUP':contract,'COMPLETE_ACTIONS':('copy','like','share')}
  import re
  namespace['re']=re
  exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),
   '<verified-native-turns-AST>','exec'),namespace)
  rows=[{'user':True,'text':'original','attachments':['chart-one','chart-two']},
        {'user':False,'text':'initial reply','complete':True},
        {'user':True,'text':'question','attachments':[]},
        {'user':False,'text':'final reply','complete':True}]
  bridge=SimpleNamespace(_dom_contract=contract,uploaded_names=['chart-one','chart-two'],
   _visible=lambda _: [Turn(row,index) for index,row in enumerate(rows)],
   _assistant_text=lambda body:body.row['text'])
  with_original=namespace['_turns'](bridge)
  self.assertEqual(with_original[0]['attachments'],['chart-one','chart-two'])
  self.assertNotEqual(with_original[2]['attachments'],[])
  bridge.uploaded_names=[]
  without=namespace['_turns'](bridge)
  self.assertEqual(without[2]['attachments'],[])
  self.assertNotEqual(without[0]['attachments'],['chart-one','chart-two'])
  self.assertEqual(with_original[3],without[3]);self.assertTrue(without[3]['complete'])
  self.assertEqual(with_original[0]['role'],with_original[2]['role'])
  self.assertEqual(with_original[1]['role'],with_original[3]['role'])

 def test_actual_native_ask_ast_generates_final_id_through_unchanged_receipt_code(self):
  source=Path('/tmp/harun-reconciled-production-source/browser_runtime/dom.py')
  if not source.is_file():self.skipTest('Verified private native source is only present in reconciliation workspace')
  syntax=ast.parse(source.read_text())
  cls=next(node for node in syntax.body if isinstance(node,ast.ClassDef) and node.name=='DOMBridge')
  ask=copy.deepcopy(next(node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name=='ask'))
  class Protocol(ast.NodeTransformer):
   def visit_Constant(self,node):
    if node.value in ('__REDACTED_str_138','__REDACTED_str_139','__REDACTED_str_140'):
     return ast.copy_location(ast.Constant('marker'),node)
    return node
  ask=Protocol().visit(ask)
  module=native()
  namespace={'NotSubmitted':NotSubmitted,'OutcomeUnknown':OutcomeUnknown,'hashlib':hashlib,'time':time,
   'image_artifacts':module.image_artifacts,'request_kind':lambda *_:'SCREENING',
   'user_prompt':lambda payload:payload['prompt'],'exact_json':lambda text,marker:{'native_text':text}}
  exec(compile(ast.fix_missing_locations(ast.Module(body=[ask],type_ignores=[])),'<verified-native-ask-AST>','exec'),namespace)
  class ActualAskBridge(Bridge):pass
  ActualAskBridge.ask=namespace['ask']
  bridge=ActualAskBridge();hook=self.setup_hook(bridge=bridge,module=module)
  result=hook.ask(request(),self.gate,deadline=80)
  self.assertEqual(result['assistant_text'],bridge.final)
  self.assertEqual(result['output'],{'native_text':bridge.final})
  self.assertEqual(bridge.clicks,2);self.assertEqual(bridge.fresh_calls,1)
  self.assertTrue(result['response_id'].endswith(hashlib.sha256((bridge.page.url+'\n'+request()['marker']+'\n'+bridge.final).encode()).hexdigest()))


class NativeActorPatchTests(unittest.TestCase):
 def setUp(self):
  path=Path(__file__).resolve().parents[1]/'scripts/patch_native_browser_hook.py'
  spec=importlib.util.spec_from_file_location('native_actor_patch_test',path)
  self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
  self.raw=b'# keep all original bytes\nclass BrowserActor:\n    def _open_automation(self):\n        from .dom import DOMBridge\n        self._bridge = DOMBridge(page)  # existing comment\n        return self._bridge\n'
 def test_one_expression_changes_and_every_other_byte_is_preserved(self):
  changed,receipt=self.module.patch_native_actor(self.raw,_expected_sha256=hashlib.sha256(self.raw).hexdigest())
  self.assertEqual(changed.replace(self.module.PREFIX,b'').replace(b'DOMBridge(page))',b'DOMBridge(page)'),self.raw)
  self.assertEqual(receipt['changed_expression_count'],1)
  self.assertTrue(receipt['all_other_source_bytes_preserved']);ast.parse(changed)
  self.assertIn(b'# existing comment',changed)
 def test_mismatched_source_missing_or_duplicate_site_never_patches(self):
  with self.assertRaises(self.module.PatchRefused):self.module.patch_native_actor(self.raw)
  for raw in (self.raw.replace(b'DOMBridge(page)',b'Other(page)'),
              self.raw.replace(b'        return',b'        self._bridge = DOMBridge(page)\n        return')):
   with self.assertRaises(self.module.PatchRefused):
    self.module.patch_native_actor(raw,_expected_sha256=hashlib.sha256(raw).hexdigest())
 def test_actual_verified_native_actor_ast_has_exactly_one_compatible_call_site(self):
  path=Path('/tmp/harun-reconciled-production-source/browser_runtime/actor.py')
  if not path.is_file():self.skipTest('Private native AST not present in ordinary CI')
  raw=path.read_bytes()
  result,receipt=self.module.patch_native_actor(raw,_expected_sha256=hashlib.sha256(raw).hexdigest())
  self.assertTrue(receipt['all_other_source_bytes_preserved']);self.assertEqual(result.count(self.module.PREFIX),1)
  self.assertEqual(self.module.NATIVE_ACTOR_SHA256,'0ccb1d63cc898af231e813ed8a3934c219fb9f0dff4f2345bb339c26b729e1ad')


if __name__=='__main__':unittest.main()
