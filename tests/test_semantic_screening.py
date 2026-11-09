"""Mini interprets original screening prose; fixtures never call a provider."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from agent.neurobro_reply import (ReplyIncomplete,parse_screening_reply,
    screening_interpretation,screening_market_hold)
from tests import test_native_browser_hook as fixtures
from worker.account_state import screening_contract

FINAL_DISPLAY=('Ini 2 pilihan final - tanpa koin pembanding:\n\n'
 'COIN: FILUSDT Perpetual - LONG, momentum terkuat + short-squeeze $1.2M. '
 'Entry 0.765 | SL 0.738 | TP 0.80/0.83.\n\n'
 'COIN: ATOMUSDT Perpetual - LONG, gain 24 jam tertinggi + OI tumbuh. '
 'Entry 1.33 | SL 1.27 | TP 1.42/1.47.\n\nJSON sesuai format lo:\n'
 '```json\n[{"COIN":"FILUSDT Perpetual","ARAH":"LONG","ENTRY":0.765,"TP":0.80,"SL":0.738},'
 '{"COIN":"ATOMUSDT Perpetual","ARAH":"LONG","ENTRY":1.33,"TP":1.42,"SL":1.27}]\n```')

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()

def receipt(request,symbols,*,action='SCREENING',question=''):
 return {'schema_version':1,'ok':True,**{key:request[key]for key in('request_id','conversation_id','phase','reply_sha256')},
  'authority':'DIALOGUE_CONTROL_ONLY','trading_authority':False,'decision':{'action':action,'symbols':symbols,'question':question},
  'needs_followup':action=='CLARIFY','reason':'DIALOGUE_DECISION'}

def bound(reply,symbols,*,count=2):
 payload=fixtures.request();_,payload['output_schema']=screening_contract(count)
 payload['context']={'requested_count':count,'excluded_symbols':[]}
 turns=[{'role':'user','text':'Pilih coin sesuai kapasitas.','attachments':[]},
        {'role':'assistant','text':reply,'complete':True,'id':'original-native-id'}]
 sha=hashlib.sha256(reply.encode()).hexdigest()
 identity=digest({'operation':payload['operation'],'request_sha256':payload['request_sha256'],
  'reply_sha256':sha,'phase':'SCREENING','transcript_sha256':digest(turns)})
 request={'schema_version':1,'request_id':identity,'conversation_id':'neurobro:original-room','phase':'SCREENING',
  'reply':reply,'reply_sha256':sha,'market_context':None,'gate':{'robot_on':True,'native_dispatch_verified':True},'deadline_epoch':1}
 return payload,request,receipt(request,symbols),turns

class SemanticScreeningTests(unittest.TestCase):
 def test_actual_nov10_replies_use_mini_semantics_without_asking_the_same_format_again(self):
  fixture=json.loads((Path(__file__).parent/'fixtures/neurobro_screening_semantic_20261110.json').read_text())
  self.assertEqual(len(fixture['replies']),6)
  for row in fixture['replies']:
   with self.subTest(sha256=row['sha256']):
    self.assertEqual(hashlib.sha256(row['reply'].encode()).hexdigest(),row['sha256'])
    self.assertEqual(len(row['reply']),row['characters'])
    symbols=row['mini_decision']['symbols']
    self.assertEqual(screening_interpretation(row['reply'],*bound(row['reply'],symbols)),{'symbols':symbols})
    if row['before_parse']['capacity']['error']:
     with self.assertRaisesRegex(ReplyIncomplete,row['before_parse']['capacity']['error']):parse_screening_reply(row['reply'])

 def test_original_fil_ltc_choice_and_rotation_exclusion_are_different_questions_not_format_errors(self):
  rows=json.loads((Path(__file__).parent/'fixtures/neurobro_screening_semantic_20261110.json').read_text())['replies']
  first=next(r['reply']for r in rows if r['characters']==1413)
  updated=next(r['reply']for r in rows if r['characters']==831)
  args=bound(first,['FILUSDT','LTCUSDT'])
  self.assertEqual(screening_interpretation(first,*args),{'symbols':['FILUSDT','LTCUSDT']})
  args[0]['context']['excluded_symbols']=['LTCUSDT']
  with self.assertRaisesRegex(ReplyIncomplete,'EXCLUDED_SYMBOL'):screening_interpretation(first,*args)
  args=bound(updated,['FILUSDT','ATOMUSDT']);args[0]['context']['excluded_symbols']=['LTCUSDT']
  self.assertIn('LTCUSDT',updated)
  self.assertEqual(screening_interpretation(updated,*args),{'symbols':['FILUSDT','ATOMUSDT']})

 def test_paragraph_table_json_and_final_excerpt_keep_actual_mini_choices_without_prices(self):
  for reply in (FINAL_DISPLAY,
   'BTCUSDT hanya pembanding likuiditas. Untuk kondisi sekarang saya memilih FILUSDT dan ATOMUSDT sebagai dua kandidat final.',
   '| Pilihan akhir | Alasan |\n| FILUSDT | Momentum |\n| ATOMUSDT | Struktur |',
   '[{"COIN":"FILUSDT","ARAH":"LONG","ENTRY":0.765,"TP":0.8,"SL":0.738},{"COIN":"ATOMUSDT","ARAH":"LONG","ENTRY":1.33,"TP":1.42,"SL":1.27}]'):
   with self.subTest(reply=reply):
    args=bound(reply,['FILUSDT','ATOMUSDT']);before=copy.deepcopy(args)
    self.assertEqual(screening_interpretation(reply,*args),{'symbols':['FILUSDT','ATOMUSDT']})
    self.assertEqual(args,before)
  reply='Untuk satu slot, pilihan final saya $LINK; BTCUSDT hanya pembanding.'
  self.assertEqual(screening_interpretation(reply,*bound(reply,['LINKUSDT'],count=1)),{'symbols':['LINKUSDT']})
  reply='Tabel pembanding:\nCOIN: BTCUSDT, ETHUSDT\n\nPilihan final saya FILUSDT dan ATOMUSDT.'
  self.assertEqual(parse_screening_reply(reply),{'symbols':['BTCUSDT','ETHUSDT']})
  self.assertEqual(screening_interpretation(reply,*bound(reply,['FILUSDT','ATOMUSDT'])),{'symbols':['FILUSDT','ATOMUSDT']})

 def test_wrong_source_transcript_receipt_capacity_exclusion_or_foreign_symbol_refuses(self):
  reply='Saya memilih FILUSDT dan ATOMUSDT, bukan BTCUSDT sebagai pembanding.'
  for failure in('operation','wire','reply','message_id','result_id','authority','gate','count','exclusion','foreign','duplicate'):
   with self.subTest(failure=failure):
    payload,request,result,turns=bound(reply,['FILUSDT','ATOMUSDT'])
    if failure=='operation':payload['operation']+='x'
    elif failure=='wire':payload['request_sha256']='e'*64
    elif failure=='reply':request['reply']+=' changed'
    elif failure=='message_id':turns[-1]['id']='different-live-author'
    elif failure=='result_id':result['request_id']='e'*64
    elif failure=='authority':result['trading_authority']=True
    elif failure=='gate':request['gate']['robot_on']=False
    elif failure=='count':payload['context']['requested_count']=1
    elif failure=='exclusion':payload['context']['excluded_symbols']=['FILUSDT']
    elif failure=='foreign':result['decision']['symbols']=['ETHUSDT','ATOMUSDT']
    elif failure=='duplicate':result['decision']['symbols']=['FILUSDT','FILUSDT']
    with self.assertRaises(ReplyIncomplete):screening_interpretation(reply,payload,request,result,turns)

 def test_actual_strong_final_choice_cannot_be_replaced_with_comparison_or_conditional_choice(self):
  reply='BTCUSDT hanya pembanding.\n\nKeputusan final\nCOIN: FILUSDT, ATOMUSDT'
  self.assertEqual(parse_screening_reply(reply),{'symbols':['FILUSDT','ATOMUSDT']})
  with self.assertRaisesRegex(ReplyIncomplete,'DECISION_CONFLICT'):
   screening_interpretation(reply,*bound(reply,['BTCUSDT','ATOMUSDT']))
  for action,question in(('CLARIFY','Pilihan mana yang final jika syarat belum terjadi?'),('HOLD','')):
   payload,request,result,turns=bound('FILUSDT atau ATOMUSDT jika breakout; belum ada pilihan final.',[])
   result=receipt(request,[],action=action,question=question)
   with self.assertRaises(ReplyIncomplete):screening_interpretation(request['reply'],payload,request,result,turns)
  original='COIN PILIHAN:\nARBUSDT Perpetual - HOLD, pasar belum layak.\nZECUSDT Perpetual - HOLD, momentum lemah.'
  self.assertTrue(screening_market_hold(original,['ARBUSDT','ZECUSDT']))
  reply='COIN: SUIUSDT\nFILUSDT hanya pembanding.'
  with self.assertRaisesRegex(ReplyIncomplete,'DECISION_CONFLICT'):
   screening_interpretation(reply,*bound(reply,['FILUSDT'],count=1))
  for reply in ('{"symbols":["FILUSDT","ATOMUSDT"],"comparison":"BTCUSDT"}',
   '[{"COIN":"FILUSDT Perpetual","comparison":"BTCUSDT"},{"COIN":"ATOMUSDT Perpetual"}]',
   'Berikut pilihan akhir saya:\n```json\n[{"COIN":"FILUSDT Perpetual","comparison":"BTCUSDT"},{"COIN":"ATOMUSDT Perpetual"}]\n```'):
   with self.subTest(json_reply=reply),self.assertRaisesRegex(ReplyIncomplete,'DECISION_CONFLICT'):
    screening_interpretation(reply,*bound(reply,['BTCUSDT','ATOMUSDT']))

 def test_existing_catalog_guard_still_rejects_unknown_symbol(self):
  from worker.neuroapi import selections
  from worker.core import Review
  reply='Pilihan final saya UNKNOWNUSDT.'
  output=screening_interpretation(reply,*bound(reply,['UNKNOWNUSDT'],count=1))
  with self.assertRaisesRegex(Review,'INVALID_SCREENING_SYMBOL'):selections(output,{'FILUSDT':{}},1)

class SemanticScreeningHookTests(unittest.TestCase):
 def setup_hook(self,reply):
  payload=fixtures.request();_,payload['output_schema']=screening_contract(2)
  payload['context']={'requested_count':2,'excluded_symbols':['BTCUSDT']}
  case=fixtures.ComposedMaterialGateTests();case.harness(payload);self.addCleanup(case.doCleanups)
  case.bridge.initial=reply
  case.reasoner.request.side_effect=lambda request:receipt(request,['FILUSDT','ATOMUSDT'])
  return case,payload

 def test_valid_semantic_selection_never_sends_a_reformat_question_and_keeps_native_reply(self):
  reply='BTCUSDT pembanding saja; pilihan akhir saya FILUSDT dan ATOMUSDT karena momentum serta struktur.'
  with self.assertRaises(ReplyIncomplete):parse_screening_reply(reply)
  case,payload=self.setup_hook(reply)
  result=case.hook.ask(payload,lambda:True,deadline=80)
  self.assertEqual(result['output'],{'symbols':['FILUSDT','ATOMUSDT']})
  self.assertEqual(result['assistant_text'],reply);self.assertEqual(case.bridge.clicks,1)
  case.reasoner.request.assert_called_once();case.converter.assert_not_called()
  request=case.reasoner.request.call_args.args[0]
  self.assertEqual(request['conversation_context']['turns'],case.bridge._turns())
  self.assertEqual(request['conversation_context']['native_operation'],payload['operation'])

 def test_normal_result_carries_only_actual_sealed_done_review_and_unchanged_native_text(self):
  from agent.native_analysis_receipt import read_reviews
  case,payload=self.setup_hook('Pilihan akhir saya FILUSDT dan ATOMUSDT; BTCUSDT hanya pembanding.')
  payload.update(operation='2026-11-05:robot-v9:0:screening:0:2')
  payload['marker']='HARUN_'+hashlib.sha256(payload['operation'].encode()).hexdigest()[:32]
  case.hook.daily_room.day='2026-11-05';case.hook.daily_room.room_id='a'*32
  case.hook.daily_room.conversation_id='neurobro:2026-11-05:'+'a'*32
  case.reasoner.supports_screening_receipt_lookup=True;stored=[None]
  def model(request):
   response=receipt(request,['FILUSDT','ATOMUSDT'])
   memo={k:v for k,v in request.items()if k not in('deadline_epoch','conversation_context')}
   stored[0]={'result':response,'operation':{'operation_id':'delegated-model:'+request['request_id'],
    'kind':'DELEGATED_MODEL_DIALOGUE','request_hash':digest(memo),'state':'DONE',
    'result':json.dumps(response,sort_keys=True,separators=(',',':')),'updated_at':1793864323.0}}
   return response
  case.reasoner.request.side_effect=model
  case.reasoner.completed_screening=Mock(side_effect=lambda request:copy.deepcopy(stored[0]))
  result=case.hook.ask(payload,lambda:True,deadline=80)
  proof=result['screening_interpretation'];entries=read_reviews(case.hook.daily_room.store.root,payload)
  self.assertEqual(proof['review_receipt'],entries[0])
  self.assertEqual(proof['turns'],case.bridge._turns())
  self.assertEqual(proof['assistant_sha256'],hashlib.sha256(result['assistant_text'].encode()).hexdigest())
  self.assertEqual(proof['review_receipt']['value']['response'],stored[0]['result'])
  self.assertEqual(proof['review_receipt']['value']['model_operation'],stored[0]['operation'])
  self.assertEqual(result['output'],{'symbols':['FILUSDT','ATOMUSDT']})
  self.assertEqual(result['assistant_text'],case.bridge.initial);self.assertEqual(case.bridge.clicks,1)
  case.reasoner.request.assert_called_once();self.assertEqual(case.reasoner.completed_screening.call_count,2)

 def test_identical_already_answered_clarification_is_not_sent_again(self):
  reply='Belum ada pilihan final antara FILUSDT dan ATOMUSDT.'
  case,payload=self.setup_hook(reply);case.bridge.final=reply
  question='Coin mana yang menjadi pilihan akhir?'
  case.reasoner.request.side_effect=lambda request:receipt(request,[],action='CLARIFY',question=question)
  with self.assertRaises(fixtures.OutcomeUnknown):case.hook.ask(payload,lambda:True,deadline=80)
  self.assertEqual(case.bridge.clicks,2);self.assertEqual(case.reasoner.request.call_count,2)
  self.assertEqual([r['text']for r in case.bridge.messages if r['role']=='native_user_role'].count(question),1)
  self.assertNotIn('HOLD',[r['text']for r in case.bridge.messages])

 def test_excluded_actual_choice_gets_one_replacement_question_then_updated_natural_selection(self):
  rows=json.loads((Path(__file__).parent/'fixtures/neurobro_screening_semantic_20261110.json').read_text())['replies']
  first=next(r['reply']for r in rows if r['characters']==1413)
  updated=next(r['reply']for r in rows if r['characters']==831)
  case,payload=self.setup_hook(first);payload['context']['excluded_symbols']=['LTCUSDT'];case.bridge.final=updated
  case.reasoner.request.side_effect=lambda request:receipt(request,
   ['FILUSDT','LTCUSDT']if request['reply']==first else['FILUSDT','ATOMUSDT'])
  result=case.hook.ask(payload,lambda:True,deadline=80)
  self.assertEqual(result['output'],{'symbols':['FILUSDT','ATOMUSDT']});self.assertEqual(result['assistant_text'],updated)
  self.assertEqual(case.bridge.clicks,2);self.assertEqual(case.reasoner.request.call_count,2)
  question=case.bridge.messages[2]['text']
  self.assertIn('LTCUSDT sudah dikecualikan',question);self.assertIn('pengganti',question)
  self.assertNotIn('bentuk COIN:',question)
  self.assertEqual(case.reasoner.request.call_args.args[0]['conversation_context']['turns'],case.bridge._turns())

 def test_missing_coin_count_gets_capacity_feedback_instead_of_reformatting(self):
  first='Pilihan final saya FILUSDT.';updated='Pilihan final saya FILUSDT dan ATOMUSDT.'
  case,payload=self.setup_hook(first);case.bridge.final=updated
  case.reasoner.request.side_effect=lambda request:receipt(request,['FILUSDT']if request['reply']==first else['FILUSDT','ATOMUSDT'])
  result=case.hook.ask(payload,lambda:True,deadline=80)
  self.assertEqual(result['output'],{'symbols':['FILUSDT','ATOMUSDT']});self.assertEqual(case.bridge.clicks,2)
  self.assertIn('dua coin',case.bridge.messages[2]['text']);self.assertIn('lengkapi',case.bridge.messages[2]['text'])
  self.assertNotIn('bentuk COIN:',case.bridge.messages[2]['text'])

 def test_screening_choices_continue_through_existing_four_file_analysis_gate(self):
  from agent.native_data_files import deliver_market_data_files
  from agent.native_market_context import convert_native_market_context
  from agent.neurobro_reply import parse_reply_for_payload
  from tests.test_native_file_reply import FourFileNativeReplyTests
  from tests.test_agent_closed_candles_200 import NOW
  from worker.neuroapi import SETUP_SCHEMA_V4
  # Controlled transport fixtures only: the existing PNG/CSV delivery gate is
  # exercised; no website, model, account, scheduler, or order endpoint is used.
  for sides in (('LONG','SHORT'),('HOLD','HOLD')):
   case,screen_payload=self.setup_hook('Pilihan final saya FILUSDT dan ATOMUSDT; BTCUSDT hanya pembanding.')
   screened=case.hook.ask(screen_payload,lambda:True,deadline=80)
   self.assertEqual(case.bridge.clicks,1);case.reasoner.request.assert_called_once()
   for symbol,side in zip(screened['output']['symbols'],sides):
    with self.subTest(symbol=symbol,side=side):
     fixture=FourFileNativeReplyTests();bridge,client,hook,payload=fixture.setup()
     payload.update(operation='offline-analysis:'+symbol,prompt='Analisis '+symbol+' memakai empat lampiran Futures.',
                    output_schema=copy.deepcopy(SETUP_SCHEMA_V4))
     payload['context']['symbol']=symbol
     for frame in payload['context']['timeframes'].values():frame['symbol']=symbol
     levels=(None,None,None)if side=='HOLD'else('65000','65200','64900')if side=='LONG'else('65000','64800','65100')
     bridge.answer=json.dumps(dict(COIN=symbol,ARAH=side,ENTRY=levels[0],TP=levels[1],SL=levels[2]))
     hook.native.exact_json=lambda answer,marker:parse_reply_for_payload(answer,payload)
     expected=deliver_market_data_files(payload['context'],now=NOW)
     def understand(request):
      self.assertEqual(request['phase'],'ANALYSIS');self.assertEqual(request['market_context']['symbol'],symbol)
      self.assertEqual(bridge.sent,1)
      self.assertEqual(bridge._turns()[0]['attachments'],['15m.png','1h.png']+[r['name']for r in expected])
      self.assertEqual(bridge.files,[{k:r[k]for k in('name','mimeType','buffer')}for r in expected])
      self.assertEqual({tf:len(rows)for tf,rows in request['market_context']['candles'].items()},{'1h':200,'15m':200})
      return receipt(request,[],action='HOLD'if side=='HOLD'else'COMPLETE')
     client.request.side_effect=understand
     before=copy.deepcopy(payload);result=hook.ask(payload,lambda:True,deadline=80)
     self.assertEqual(payload,before);self.assertEqual(result['assistant_text'],bridge.answer)
     self.assertEqual(result['output']['symbol'],symbol);self.assertEqual(result['output']['side'],side)
     if side!='HOLD':
      self.assertEqual(str(result['output']['limit_entry']),levels[0])
      self.assertEqual(str(result['output']['stop_loss']),levels[2])
     self.assertEqual(bridge.sent,1)
     # Source-authored market HOLD is terminal without a paid interpretation;
     # it still passes the same real four-file witness checks below.
     if side=='HOLD':client.request.assert_not_called()
     else:client.request.assert_called_once()
     self.assertEqual(bridge._turns()[0]['attachments'],['15m.png','1h.png']+[r['name']for r in expected])
     self.assertEqual(bridge.files,[{k:r[k]for k in('name','mimeType','buffer')}for r in expected])
     self.assertEqual(bridge.actions,['native-png-upload','plus','File','choose-csv'])
     self.assertEqual(len(bridge.data_file_deliveries),1)
     self.assertTrue(bridge.data_file_deliveries[0]['preview_verified'])
     projected=convert_native_market_context(payload['context'],now=NOW)
     self.assertEqual({tf:len(rows)for tf,rows in projected['candles'].items()},{'1h':200,'15m':200})

if __name__=='__main__':unittest.main()
