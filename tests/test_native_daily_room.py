"""Daily room reuse around the actual native ask/receipt protocol."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import shutil
import subprocess
import unittest
from unittest.mock import Mock

from agent.native_daily_room import DailyRoom, DailyRoomHistory, DailyRoomUnavailable, JOURNAL_NAME, identities, HISTORY_DIAGNOSTIC_JS, HISTORY_OLDER_UI_JS
from agent.native_browser_hook import DelegatedDOMBridge
from agent.native_file_upload import NativeFileTurnReader
from tests.test_native_browser_hook import request as base_request, client, OutcomeUnknown, NotSubmitted


def request():
 value=base_request();value['marker']='HARUN_'+'a'*32;return value


def rendered(payload):
 return payload['prompt']+'\nMarker '+payload['marker']+'\nReturn '+payload['marker']


class Composer:
 def __init__(self):self.value=''
 def input_value(self):return self.value
 def fill(self,value,**kw):self.value=value


class Button:
 def __init__(self,bridge):self.bridge=bridge
 def is_enabled(self):return True
 def click(self,**kw):
  b=self.bridge;b.clicks+=1
  b.messages.extend([{'role':'user','text':b.composer.value,'attachments':[], 'id':'user-'+str(b.serial)},
   {'role':'assistant','text':'FINAL_NATIVE_REPLY BTCUSDT','complete':True,'id':'assistant-'+str(b.serial)}])
  b.composer.value='';b.serial+=1


class Bridge:
 def __init__(self,rows=()):
  self.messages=copy.deepcopy(list(rows));self.timeout=90;self.operation_deadline=None
  self.current=0;self.clock=lambda:self.current
  self._dom_contract=SimpleNamespace(composer='composer',send='send');self._operation_contract=None
  self.page=SimpleNamespace(url='https://app.neurobro.ai/',wait_for_timeout=self.wait)
  self.composer=Composer();self.button=Button(self);self.uploaded_names=[]
  self.serial=10;self.clicks=0;self.fresh_calls=0
 def wait(self,milliseconds):self.current+=milliseconds/1000
 def status(self):return 'CONNECTED'
 def _timeout(self,milliseconds):return milliseconds
 def _until(self,seconds):return min(self.current+seconds,self.operation_deadline)
 def _visible(self,selector):
  if selector=='messages':return self.messages[:]
  return [self.composer]if selector=='composer'else[self.button]
 def _turns(self):return copy.deepcopy(self._visible('messages'))
 def _fresh_conversation(self):self.fresh_calls+=1;self.messages=[];return self.composer
 def _preview_scope(self,*args):return SimpleNamespace(locator=lambda _:SimpleNamespace(count=lambda:0))
 def _upload(self,*args):return []
 def _answer(self,prompt,attachment_names=()):
  rows=self._turns()
  if (len(rows)!=2 or rows[0].get('role')!='user' or rows[0]['text'].strip()!=prompt.strip()
   or sorted(rows[0]['attachments'])!=sorted(attachment_names)
   or rows[1].get('role')!='assistant'or rows[1].get('complete')is not True):return None
  return rows[1]['text']
 def ask(self,payload,gate,*,chart_images=(),chart_evidence=(),deadline=None,receipt_callback=None):
  self.operation_deadline=deadline;self._operation_contract=self._dom_contract
  sent=False
  try:
   composer=self._fresh_conversation();prompt=rendered(payload);composer.fill(prompt)
   if self._turns():raise NotSubmitted('UNAVAILABLE')
   if gate()is not True:raise NotSubmitted('ROBOT_OFF')
   sent=True;self._visible(self._dom_contract.send)[0].click()
   while self.current<deadline:
    text=self._answer(prompt)
    if text is not None:return {'output':{'symbols':['BTCUSDT']},'assistant_text':text,
     'response_id':'native:'+hashlib.sha256((self.page.url+'\n'+payload['marker']+'\n'+text).encode()).hexdigest(),'chart_evidence':[]}
    self.wait(250)
   raise OutcomeUnknown()
  except Exception:
   if sent:raise OutcomeUnknown()from None
   raise
  finally:self._operation_contract=None


def rows():
 return [{'role':'user','text':'Earlier screening','attachments':[],'id':'earlier-user'},
  {'role':'assistant','text':'BTCUSDT selected','complete':True,'id':'earlier-assistant'}]


def native():
 return SimpleNamespace(user_prompt=rendered,RAIL_INPUT_GROUP=object(),MESSAGE='messages',OutcomeUnknown=OutcomeUnknown,NotSubmitted=NotSubmitted,
  MAX_ANSWER=65536,image_artifacts=Mock(return_value=[]),exact_json=Mock(return_value={'symbols':['BTCUSDT']}))


class History:
 def __init__(self, bridge):self.bridge=bridge;self.rooms=[];self.selected=[]
 def rows(self):return [dict(index=i,text=title,label='Hari ini')for i,(title,_)in enumerate(self.rooms)]
 def open_sidebar(self):pass
 def select(self,row,*,occurrence=0):
  matches=[item for item in self.rooms if item[0]==row['text']]
  self.selected.append(row['text']);self.bridge.messages=copy.deepcopy(matches[occurrence][1])


class DailyRoomTests(unittest.TestCase):
 def setUp(self):
  self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
  self.now=datetime(2026,10,30,16,59,tzinfo=timezone.utc).timestamp()
 def setup(self,bridge=None):
  bridge=bridge or Bridge();module=native()
  room=DailyRoom(bridge,module,self.directory.name,wall_clock=lambda:self.now,history=History(bridge))
  reasoner=client('SCREENING')
  hook=DelegatedDOMBridge(bridge,reasoner,native=module,monotonic=bridge.clock,
   wall_clock=lambda:self.now,daily_room=room)
  return bridge,room,hook,reasoner
 def test_screening_replacement_and_analysis_reuse_existing_room_and_mini_identity(self):
  bridge,room,hook,reasoner=self.setup(Bridge(rows()))
  for i in range(8):
   value=request();value['operation']='2026-10-30:screening:'+str(i)
   result=hook.ask(value,lambda:True,deadline=bridge.current+80)
   self.assertEqual(result['assistant_text'],'FINAL_NATIVE_REPLY BTCUSDT')
   self.assertEqual(len(bridge._turns()),2)
  self.assertEqual(bridge.fresh_calls,0)
  self.assertEqual(len(bridge.messages),18)
  calls=[item.args[0]for item in reasoner.request.call_args_list]
  self.assertEqual({item['conversation_id']for item in calls},{room.conversation_id})
  self.assertEqual(room.evidence()['operation_offset'],16)
  self.assertEqual(bridge.messages[:2],rows())
 def test_restart_reopens_persistent_identity_without_new_chat(self):
  bridge,room,hook,reasoner=self.setup()
  hook.ask(request(),lambda:True,deadline=80)
  saved=copy.deepcopy(bridge.messages);identity=room.conversation_id
  restarted,restored,hook2,reasoner2=self.setup(Bridge(saved))
  value=request();value['operation']='2026-10-30:analysis:2'
  hook2.ask(value,lambda:True,deadline=80)
  self.assertEqual(restored.conversation_id,identity)
  self.assertEqual(restarted.fresh_calls,0)
  self.assertEqual(restored.evidence()['mode'],'REUSED')
  self.assertEqual(restored.evidence()['operation_offset'],2)
 def test_wib_rollover_creates_once_and_next_request_reuses_it(self):
  bridge,room,hook,_=self.setup(Bridge(rows()))
  hook.ask(request(),lambda:True,deadline=80);old=room.conversation_id
  self.now+=120
  hook.ask(request(),lambda:True,deadline=bridge.current+80);new=room.conversation_id
  hook.ask(request(),lambda:True,deadline=bridge.current+80)
  self.assertNotEqual(new,old)
  self.assertIn('2026-10-31',new)
  self.assertEqual(bridge.fresh_calls,1)
  self.assertEqual(len(bridge.messages),4)
 def test_foreign_current_room_is_rejected_without_new_chat_or_send(self):
  bridge,room,hook,_=self.setup()
  hook.ask(request(),lambda:True,deadline=80)
  foreign=rows();foreign[0]['id']='foreign-id'
  restarted,_,hook2,_=self.setup(Bridge(foreign))
  with self.assertRaises(DailyRoomUnavailable):hook2.ask(request(),lambda:True,deadline=80)
  self.assertEqual(restarted.clicks,0);self.assertEqual(restarted.fresh_calls,0)
 def test_draft_and_unfinished_answer_are_preserved_before_send(self):
  for mutate in ('draft','streaming'):
   with self.subTest(mutate=mutate):
    bridge=Bridge(rows())
    if mutate=='draft':bridge.composer.value='Owner unfinished text'
    else:bridge.messages[-1]['complete']=False
    # Separate journal: the room has not yet been adopted.
    with tempfile.TemporaryDirectory()as root:
     module=native();room=DailyRoom(bridge,module,root,wall_clock=lambda:self.now,history=History(bridge))
     with self.assertRaises(DailyRoomUnavailable),room.operation(request()):bridge._fresh_conversation()
    self.assertEqual(bridge.fresh_calls,0);self.assertEqual(bridge.clicks,0)
 def test_real_file_reader_nodes_and_rows_share_request_offset(self):
  bridge,room,hook,_=self.setup(Bridge(rows()))
  hook.ask(request(),lambda:True,deadline=80)
  first=copy.deepcopy(bridge.messages)
  with room.operation(request()):
   bridge._fresh_conversation()
   names=('chart15.png','chart1h.png','data15.csv','data1h.csv')
   bridge.messages.extend([{'role':'user','id':'data-user','text':'Current coin chart','attachments':list(names[:2])},
    {'role':'assistant','id':'data-answer','text':'HOLD','complete':True}])
   reader=NativeFileTurnReader(bridge,native())
   reader.turn_files[names]=(names[:2],names[2:],lambda node,names,user:node['id']=='data-user')
   bridge.uploaded_names=list(names)
   projected=reader.read()
   self.assertEqual(len(projected),2)
   self.assertEqual(projected[0]['attachments'],list(names))
   self.assertEqual(bridge._visible('messages')[0]['id'],'data-user')
  self.assertEqual(bridge.messages[:len(first)],first)
 def test_failed_send_keeps_room_identity_and_does_not_reopen_or_erase_history(self):
  bridge,room,hook,_=self.setup(Bridge(rows()))
  with self.assertRaises(NotSubmitted):hook.ask(request(),lambda:False,deadline=80)
  self.assertEqual(bridge.clicks,0);self.assertEqual(bridge.messages,rows())
  self.assertEqual(bridge.fresh_calls,0)
  bridge.composer.value=''
  hook.ask(request(),lambda:True,deadline=80)
  self.assertEqual(bridge.fresh_calls,0)

 def test_blank_home_restart_restores_owned_history_before_next_send(self):
  bridge,room,hook,_=self.setup(Bridge(rows()))
  hook.ask(request(),lambda:True,deadline=80)
  identity=room.conversation_id;owned=copy.deepcopy(bridge.messages)
  restarted,restored,hook2,_=self.setup()
  restored.history.rooms=[('Other room',rows()),('Owned daily room',owned)]
  hook2.ask(request(),lambda:True,deadline=80)
  self.assertEqual(restored.conversation_id,identity)
  self.assertEqual(restarted.fresh_calls,0)
  self.assertEqual(restored.history.selected,['Other room','Owned daily room'])
  self.assertEqual(restarted.messages[:len(owned)],owned)
  self.assertEqual(restarted.clicks,1)

 def test_blank_restart_waits_for_sidebar_rows_after_open_before_same_room_send(self):
  bridge,room,hook,_=self.setup()
  hook.ask(request(),lambda:True,deadline=80)
  owned=copy.deepcopy(bridge.messages);identity=room.conversation_id
  restarted,restored,hook2,_=self.setup()
  restored.history.rooms=[('Owned daily room',owned)]
  original_rows=restored.history.rows
  opened=[]
  restored.history.open_sidebar=lambda:opened.append(restarted.current)
  restored.history.rows=lambda:original_rows()if opened and restarted.current>=0.15 else[]
  hook2.ask(request(),lambda:True,deadline=80)
  self.assertEqual(len(opened),1)
  self.assertGreaterEqual(restarted.current,0.15)
  self.assertEqual(restored.conversation_id,identity)
  self.assertEqual(restored.history.selected,['Owned daily room'])
  self.assertEqual(restarted.messages[:len(owned)],owned)
  self.assertEqual(restarted.fresh_calls,0)
  self.assertEqual(restarted.clicks,1)

 def test_sidebar_wait_stops_on_security_challenge_before_room_selection(self):
  bridge,room,_,_=self.setup();bridge.operation_deadline=80
  room.history.rows=lambda:[]
  room.history.open_sidebar=Mock()
  bridge.status=lambda:'CONNECTED'if bridge.current<0.1 else'CHALLENGE_REQUIRED'
  with self.assertRaises(DailyRoomUnavailable):room.restore([],{},'SENT')
  self.assertAlmostEqual(bridge.current,0.1)
  room.history.open_sidebar.assert_called_once()
  self.assertEqual(room.history.selected,[])
  self.assertEqual(bridge.clicks,0);self.assertEqual(bridge.fresh_calls,0)

 def test_open_sidebar_does_not_toggle_already_expanded_navigation(self):
  for expanded in ('true','false'):
   with self.subTest(expanded=expanded):
    bridge,room,_,_=self.setup()
    navigation=Mock();navigation.is_enabled.return_value=True
    navigation.get_attribute.return_value=expanded
    room.native.CURRENT_CHAT_NAV='navigation'
    room.original_visible=lambda _:[navigation]
    DailyRoomHistory(room).open_sidebar()
    self.assertEqual(navigation.click.call_count,0 if expanded=='true'else 1)

 def test_not_found_retains_private_passive_history_diagnostic(self):
  bridge,room,_,_=self.setup();bridge.operation_deadline=80
  bridge.page.locator=lambda _:SimpleNamespace(evaluate=lambda *args,**kw:{'buttons':[],
   'headings':[{'tag':'H3','text':'Hari ini'}],'widgets':[]})
  with self.assertRaisesRegex(DailyRoomUnavailable,'NEUROBRO_DAILY_ROOM_NOT_FOUND'):
   room.restore([],{},'SENT')
  path=Path(self.directory.name)/'on-daily-room-restore.json'
  self.assertEqual(path.stat().st_mode&0o777,0o600)
  value=json.loads(path.read_text())
  self.assertEqual(value['history_choice_count'],0)
  self.assertEqual(value['controls']['headings'][0]['text'],'Hari ini')
  self.assertTrue(value['composer'][0]['empty'])
  self.assertEqual(value['native_rows'],[])
  self.assertFalse(value['diagnostic_browser_inputs_performed'])
  self.assertEqual(bridge.clicks,0);self.assertEqual(bridge.fresh_calls,0)

 def test_crash_after_first_send_before_anchor_checkpoint_restores_by_exact_prompt(self):
  bridge,room,hook,_=self.setup()
  # Emulate process death by retaining the durable pre-click witness but not
  # running operation's final checkpoint. Native click really creates the rows.
  with room.store.open()as db:
   room._connection=db;room.day='2026-10-30';room._payload=request();room._prepared=False
   bridge._fresh_conversation();bridge.composer.fill(rendered(request()))
   bridge._visible('send')[0].click()
   stored=db.execute('SELECT state,anchors FROM daily_rooms').fetchone()
   self.assertEqual(stored,('SENT','[]'))
   room._connection=None
  owned=copy.deepcopy(bridge.messages);identity=room.conversation_id
  restarted,restored,hook2,_=self.setup()
  restored.history.rooms=[('Owned after interrupted send',owned)]
  hook2.ask(request(),lambda:True,deadline=80)
  self.assertEqual(restored.conversation_id,identity)
  self.assertEqual(restarted.fresh_calls,0)
  self.assertEqual(len(restarted.messages),4)
  self.assertEqual(restarted.messages[:2],owned)

 def test_uncertain_first_send_with_no_history_never_uses_blank_as_new_room(self):
  bridge,room,_,_=self.setup()
  with room.store.open()as db:
   room._connection=db;room.day='2026-10-30';room._payload=request();room._prepared=False
   bridge._fresh_conversation();room.before_send();room._connection=None
  restarted,_,hook2,_=self.setup()
  with self.assertRaises(DailyRoomUnavailable):hook2.ask(request(),lambda:True,deadline=80)
  self.assertEqual(restarted.clicks,0);self.assertEqual(restarted.fresh_calls,0)

 def test_foreign_same_title_history_requires_native_anchor_not_title(self):
  bridge,room,hook,_=self.setup(Bridge(rows()))
  hook.ask(request(),lambda:True,deadline=80);owned=copy.deepcopy(bridge.messages)
  foreign=copy.deepcopy(owned);foreign[-1]['id']='foreign-assistant'
  foreign[-2]['text']=foreign[-2]['text'].replace(request()['marker'],'HARUN_'+'b'*32)
  restarted,restored,hook2,_=self.setup()
  restored.history.rooms=[('Same title',foreign),('Same title',owned)]
  hook2.ask(request(),lambda:True,deadline=80)
  self.assertEqual(restored.history.selected,['Same title','Same title'])
  self.assertEqual(restarted.messages[:len(owned)],owned)
  self.assertEqual(restarted.clicks,1)

 def test_interrupted_selection_without_send_resumes_same_claim_on_empty_home(self):
  bridge,room,_,_=self.setup()
  with room.store.open()as db:
   db.execute('INSERT INTO daily_rooms VALUES(?,?,?,?,?,?,?)',
    ('2026-10-30','1'*32,'CLAIMED',None,'[]',json.dumps({
     'first_prompt_sha256':hashlib.sha256(rendered(request()).encode()).hexdigest(),
     'marker':request()['marker'],'history_titles':[]}),self.now))
  restarted,restored,hook2,_=self.setup()
  hook2.ask(request(),lambda:True,deadline=80)
  self.assertEqual(restored.room_id,'1'*32)
  self.assertEqual(restarted.fresh_calls,0);self.assertEqual(restarted.clicks,1)

 def test_initial_contract_detection_passes_native_visibility_through(self):
  bridge,room,_,_=self.setup()
  bridge._dom_contract=None
  self.assertEqual(bridge._visible('send'),[bridge.button])
  self.assertEqual(bridge._visible('composer'),[bridge.composer])

 def test_history_switch_waits_for_transient_detection_then_reads_owned_rows(self):
  bridge,room,_,_=self.setup()
  bridge.operation_deadline=80
  owned=rows();room.history.rooms=[('Owned',owned)]
  selected=room.history.select
  states=[]
  def switch(row,**kwargs):
   selected(row,**kwargs);states.extend(['UNAVAILABLE','UNAVAILABLE','CONNECTED'])
  room.history.select=switch
  bridge.status=lambda:states.pop(0)if states else 'CONNECTED'
  restored=room.restore(identities(owned),{},'BOUND')
  self.assertEqual(restored,owned)
  self.assertAlmostEqual(bridge.current,0.1)
  self.assertEqual(bridge.clicks,0);self.assertEqual(bridge.fresh_calls,0)

 def test_persistently_unavailable_switch_stops_at_existing_operation_deadline(self):
  bridge,room,_,_=self.setup();bridge.operation_deadline=0.1
  owned=rows();room.history.rooms=[('Owned',owned)]
  selected=room.history.select
  def switch(row,**kwargs):
   selected(row,**kwargs);bridge.status=lambda:'UNAVAILABLE'
  room.history.select=switch
  with self.assertRaises(DailyRoomUnavailable):room.restore(identities(owned),{},'BOUND')
  self.assertAlmostEqual(bridge.current,0.1)
  self.assertEqual(bridge.clicks,0);self.assertEqual(bridge.fresh_calls,0)

 def test_security_or_login_during_history_switch_stops_without_wait_or_interaction(self):
  for state in ('CHALLENGE_REQUIRED','LOGIN_REQUIRED'):
   with self.subTest(state=state):
    bridge,room,_,_=self.setup();bridge.operation_deadline=80
    owned=rows();room.history.rooms=[('Owned',owned)]
    selected=room.history.select
    def switch(row,**kwargs):
     selected(row,**kwargs);bridge.status=lambda:state
    room.history.select=switch
    with self.assertRaises(DailyRoomUnavailable):room.restore(identities(owned),{},'BOUND')
    self.assertEqual(bridge.current,0)
    self.assertEqual(bridge.clicks,0);self.assertEqual(bridge.fresh_calls,0)

 def test_history_selection_resolves_current_button_index_and_duplicate_occurrence(self):
  bridge,room,_,_=self.setup()
  targets=[]
  class Target:
   def __init__(self,index):self.index=index
   def is_visible(self):return True
   def is_enabled(self):return True
   def inner_text(self):return 'Same title'
   def click(self,**kwargs):targets.append(self.index)
  class Buttons:
   def nth(self,index):return Target(index)
  history=DailyRoomHistory(room)
  history.rows=lambda:[{'index':17,'text':'Same title','label':'Hari ini'},
   {'index':23,'text':'Same title','label':'Hari ini'}]
  # The old snapshot had indexes 3/4; loaded message controls changed them.
  bridge.page.locator=lambda _:Buttons()
  history.select({'index':4,'text':'Same title','label':'Hari ini'},occurrence=1)
  self.assertEqual(targets,[23])
  self.assertEqual(bridge.clicks,0);self.assertEqual(bridge.fresh_calls,0)

 def owned_prompt_fixture(self):
  prompt=rendered(request())
  own=[{'role':'user','text':prompt,'attachments':[]},
   {'role':'assistant','text':'Original response','complete':True,'id':'temporary-id'}]
  binding={'first_prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
   'marker':request()['marker']}
  return own,binding

 def test_room_identity_accepts_full_owned_prompt_after_prepended_history_and_reloaded_id(self):
  own,binding=self.owned_prompt_fixture();anchors=identities(own)
  current=rows()+copy.deepcopy(own)
  current[-1].update(id='server-id',text='Current rendered response')
  self.assertNotEqual(identities(current[:2]),anchors)
  self.assertTrue(DailyRoom.matches(current,anchors,binding,'BOUND'))
  self.assertEqual(current[-2]['text'].count(binding['marker']),2)
  # The same restoration still freezes all current rows for the next request.
  bridge,room,_,_=self.setup(Bridge(current))
  room.offset=len(current);room.prefix=identities(current)
  bridge.messages[-1]['text']='Changed ENTRY 999 after request admission'
  with self.assertRaises(DailyRoomUnavailable):room.turns()

 def anchored_history_fixture(self):
  own,binding=self.owned_prompt_fixture()
  for index in range(3):
   own.extend([{'role':'user','text':'Later owned request '+str(index),'id':'owned-user-'+str(index)},
    {'role':'assistant','text':'Later owned answer '+str(index),'complete':True,'id':'owned-answer-'+str(index)}])
  return own,binding

 def test_room_identity_accepts_only_the_complete_known_history_suffix(self):
  own,binding=self.anchored_history_fixture();anchors=identities(own)
  for count in (2,3,4,6):
   with self.subTest(count=count):
    current=copy.deepcopy(own[-count:])
    self.assertNotEqual(identities(current),anchors[:count])
    self.assertFalse(any(binding['marker']in row['text']for row in current))
    self.assertTrue(DailyRoom.matches(current,anchors,binding,'BOUND'))

 def test_restore_known_30_message_window_of_94_saved_anchors(self):
  # Synthetic IDs exercise the observed 94/30 shape, not VPS ID-overlap proof.
  own,binding=self.owned_prompt_fixture()
  for index in range(46):
   own.extend([{'role':'user','text':'Anchored request '+str(index),'id':'saved-user-'+str(index)},
    {'role':'assistant','text':'Anchored answer '+str(index),'complete':True,'id':'saved-answer-'+str(index)}])
  anchors=identities(own);current=copy.deepcopy(own[-30:])
  self.assertEqual(len(anchors),94);self.assertEqual(len(current),30)
  self.assertNotEqual(identities(current),anchors[:len(current)])
  self.assertFalse(any(binding['marker']in row['text']for row in current))
  bridge,room,_,_=self.setup(Bridge())
  bridge.operation_deadline=80
  room.history.rooms=[('Existing daily room',current)]
  self.assertEqual(room.restore(anchors,binding,'BOUND'),current)
  self.assertEqual(room.history.selected,['Existing daily room'])
  self.assertEqual(bridge.fresh_calls,0);self.assertEqual(bridge.clicks,0)

 def test_known_history_suffix_rejects_changed_identity_or_completion(self):
  own,binding=self.anchored_history_fixture();anchors=identities(own)
  mutations=({'id':'foreign-answer'},{'text':'Different setup'},
   {'role':'user'},{'complete':False},{'complete':None})
  for change in mutations:
   with self.subTest(change=change):
    current=copy.deepcopy(own[-4:]);current[-1].update(change)
    self.assertFalse(DailyRoom.matches(current,anchors,binding,'BOUND'))
  for index in range(4):
   for field in ('id','text'):
    with self.subTest(index=index,field=field):
     current=copy.deepcopy(own[-4:]);current[index][field]='changed-known-row'
     self.assertFalse(DailyRoom.matches(current,anchors,binding,'BOUND'))
  current=copy.deepcopy(own[-4:]);current[-2]['role']='assistant'
  self.assertFalse(DailyRoom.matches(current,anchors,binding,'BOUND'))

 def test_known_history_suffix_rejects_foreign_middle_or_unknown_appended_rows(self):
  own,binding=self.anchored_history_fixture();anchors=identities(own)
  cases=[rows(),copy.deepcopy(own[2:6]),copy.deepcopy(own[-2:])+rows()]
  for current in cases:
   with self.subTest(current=current):
    self.assertFalse(DailyRoom.matches(current,anchors,binding,'BOUND'))

 def test_known_history_suffix_requires_a_complete_pair_even_if_hashes_match(self):
  own,binding=self.anchored_history_fixture()
  self.assertFalse(DailyRoom.matches(own[-1:],identities(own),binding,'BOUND'))
  self.assertFalse(DailyRoom.matches([],identities(own),binding,'BOUND'))
  for change in ({'complete':False},{'role':'user'}):
   changed=copy.deepcopy(own);changed[-1].update(change)
   self.assertFalse(DailyRoom.matches(changed[-2:],identities(changed),binding,'BOUND'))
  changed=copy.deepcopy(own);changed[-2]['role']='assistant'
  self.assertFalse(DailyRoom.matches(changed[-2:],identities(changed),binding,'BOUND'))

 def test_known_history_suffix_keeps_active_request_prefix_strict(self):
  own,binding=self.anchored_history_fixture();current=copy.deepcopy(own[-4:])
  self.assertTrue(DailyRoom.matches(current,identities(own),binding,'BOUND'))
  bridge,room,_,_=self.setup(Bridge(current));room.offset=len(current);room.prefix=identities(current)
  bridge.messages[-1]['id']='changed-after-admission'
  with self.assertRaises(DailyRoomUnavailable):room.turns()
  self.assertEqual(bridge.clicks,0);self.assertEqual(bridge.fresh_calls,0)

 def test_restart_with_only_known_history_tail_preserves_daily_identity(self):
  bridge,room,hook,_=self.setup()
  for index in range(1,4):
   payload=request();payload['operation']='2026-10-31:owned:'+str(index)
   payload['marker']='HARUN_'+format(index,'032x')
   hook.ask(payload,lambda:True,deadline=bridge.current+80)
  identity=room.conversation_id;tail=copy.deepcopy(bridge.messages[-4:])
  restarted,restored,hook2,_=self.setup(Bridge(tail))
  payload=request();payload['operation']='2026-10-31:owned:4';payload['marker']='HARUN_'+format(4,'032x')
  hook2.ask(payload,lambda:True,deadline=restarted.current+80)
  self.assertEqual(restored.conversation_id,identity)
  self.assertEqual(restarted.messages[:len(tail)],tail)
  self.assertEqual(restarted.fresh_calls,0);self.assertEqual(restored.history.selected,[])
  self.assertEqual(restored.evidence()['operation_offset'],len(tail))
  self.assertEqual(restarted.clicks,1)

 def test_room_identity_rejects_modified_prompt_substring_and_assistant_quote(self):
  own,binding=self.owned_prompt_fixture();anchors=identities(own)
  for row in ({'role':'user','text':'Extra prefix '+own[0]['text']},
   {'role':'user','text':own[0]['text'].replace('EXACT','Changed ENTRY 999')},
   {'role':'assistant','text':own[0]['text'],'complete':True,'id':'quoted'}):
   with self.subTest(row=row):
    self.assertFalse(DailyRoom.matches(rows()+[row],anchors,binding,'BOUND'))

 def test_room_identity_requires_the_bound_harun_marker_value(self):
  own,binding=self.owned_prompt_fixture();anchors=identities(own)
  for text in (own[0]['text'].replace(binding['marker'],'HARUN_'+'b'*32),
   own[0]['text']+' HARUN_'+'b'*32,
   own[0]['text'].replace(binding['marker'],binding['marker']+'x')):
   # Even a matching full text hash cannot substitute another marker value.
   wrong=dict(binding,first_prompt_sha256=hashlib.sha256(text.encode()).hexdigest())
   self.assertFalse(DailyRoom.matches(rows()+[{'role':'user','text':text}],anchors,wrong,'BOUND'))

 def reloaded_94_fixture(self):
  own,binding=self.owned_prompt_fixture()
  for index in range(46):
   own.extend([{'role':'user','text':'Persisted request '+str(index),'attachments':[]},
    {'role':'assistant','text':'Persisted answer '+str(index),'complete':True,'id':'temporary-'+str(index)}])
  current=copy.deepcopy(own)
  for index,row in enumerate(current):
   if row['role']=='assistant':row['id']='server-'+str(index)
  binding['history_titles']=['Existing daily room']
  return own,current,binding

 def older_ui(self,bridge,room,loads,*,available=True,count_adjustment=0):
  actions=[]
  def evaluate_all(script,request):
   self.assertEqual(script,HISTORY_OLDER_UI_JS)
   container={'available':available,'message_count':len(bridge.messages)+count_adjustment,
    'depth':2,'tag':'DIV','role':None,'slot':None,'overflow_y':'auto','show_oldest':400,
    'client_height':500,'scroll_height':4000,'rectangle':{'x':0,'y':0,'width':900,'height':500},
    'first_message_id':'actual-first','last_message_id':'actual-last'}
   if request['action']=='observe':return container
   self.assertEqual(request['observed'],container)
   retained=json.loads((Path(self.directory.name)/'on-daily-room-window.json').read_text())
   self.assertEqual(retained['windows'][-1]['scroll_container'],container)
   self.assertEqual(retained['windows'][-1]['native_row_count'],len(bridge.messages))
   actions.append('show_oldest')
   if loads:bridge.messages=copy.deepcopy(loads.pop(0))
   return dict(container,scrolled=True)
  bridge.page.locator=lambda selector:SimpleNamespace(evaluate_all=evaluate_all,
   evaluate=lambda *args,**kwargs:{'buttons':[],'headings':[],'widgets':[]})
  return actions

 def test_restore_loads_original_bound_prompt_after_reload_replaced_ids(self):
  own,current,binding=self.reloaded_94_fixture();anchors=identities(own)
  bridge,room,_,_=self.setup();bridge.operation_deadline=80
  room.history.rooms=[('Existing daily room',current[-30:])]
  actions=self.older_ui(bridge,room,[current[-60:],current])
  self.assertFalse(room.matches(current[-30:],anchors,binding,'BOUND'))
  restored=room.restore(anchors,binding,'BOUND')
  self.assertEqual(restored,current);self.assertEqual(actions,['show_oldest','show_oldest'])
  self.assertTrue(room.matches(restored,anchors,binding,'BOUND'))
  self.assertNotEqual(identities(restored),anchors)
  diagnostic=json.loads((Path(self.directory.name)/'on-daily-room-window.json').read_text())
  first=diagnostic['windows'][0]
  self.assertEqual((first['native_row_count'],first['stored_anchor_count']),(30,94))
  self.assertFalse(first['original_prompt_hash_present'])
  self.assertEqual(first['native_rows'][0]['matching_anchor_indices'],[64])
  self.assertTrue(first['native_rows'][0]['expected_tail_match'])
  self.assertEqual(first['native_rows'][1]['matching_anchor_indices'],[])
  self.assertFalse(first['native_rows'][1]['expected_tail_match'])
  self.assertEqual(first['native_rows'][1]['id'],'server-65')
  self.assertNotIn('Persisted request',(Path(self.directory.name)/'on-daily-room-window.json').read_text())
  self.assertEqual((Path(self.directory.name)/'on-daily-room-window.json').stat().st_mode&0o777,0o600)
  self.assertEqual(bridge.clicks,0);self.assertEqual(bridge.fresh_calls,0)
  self.assertFalse(room.store.path.exists())
  # Admitting the original prompt does not relax the active request prefix.
  room.offset=len(restored);room.prefix=identities(restored)
  bridge.messages[-1]['text']='Changed after restoration'
  with self.assertRaises(DailyRoomUnavailable):room.turns()

 def test_older_history_cannot_substitute_foreign_prompt_or_marker(self):
  own,current,binding=self.reloaded_94_fixture();anchors=identities(own)
  current[0]['text']=current[0]['text'].replace(binding['marker'],'HARUN_'+'b'*32)
  bridge,room,_,_=self.setup();bridge.operation_deadline=80
  room.history.rooms=[('Existing daily room',current[-30:])]
  actions=self.older_ui(bridge,room,[current])
  with self.assertRaisesRegex(DailyRoomUnavailable,'NEUROBRO_DAILY_ROOM_NOT_FOUND'):
   room.restore(anchors,binding,'BOUND')
  self.assertEqual(actions,['show_oldest','show_oldest'])
  self.assertFalse(room.matches(bridge.messages,anchors,binding,'BOUND'))
  self.assertEqual(bridge.clicks,0);self.assertEqual(bridge.fresh_calls,0)

 def test_older_history_stops_without_advancement_or_unambiguous_container(self):
  own,current,binding=self.reloaded_94_fixture();anchors=identities(own)
  for available,adjustment in ((True,0),(False,0),(True,1)):
   with self.subTest(available=available,adjustment=adjustment):
    bridge,room,_,_=self.setup(Bridge(current[-30:]));bridge.operation_deadline=80
    actions=self.older_ui(bridge,room,[],available=available,count_adjustment=adjustment)
    self.assertIsNone(room.restore_older(bridge.messages,anchors,binding,'BOUND'))
    self.assertEqual(len(actions),1 if available and not adjustment else 0)
    self.assertLessEqual(bridge.current,5.05)
    if actions:
     retained=json.loads((Path(self.directory.name)/'on-daily-room-window.json').read_text())
     self.assertEqual(retained['windows'][-1]['scroll_container']['phase'],'NO_ADVANCEMENT')
     self.assertTrue(retained['windows'][-1]['scroll_container']['action_result']['scrolled'])
    self.assertEqual(bridge.clicks,0);self.assertEqual(bridge.fresh_calls,0)

 @unittest.skipUnless(shutil.which('chromium'), 'Chromium is needed for the UI round-trip regression')
 def test_scroll_revalidates_null_attributes_through_real_locator_argument(self):
  from playwright.sync_api import sync_playwright
  with sync_playwright()as playwright:
   browser=playwright.chromium.launch(executable_path=shutil.which('chromium'),headless=True,
    args=['--no-sandbox'])
   try:
    page=browser.new_page()
    page.set_content('<style>#panel{height:100px;width:300px;overflow-y:auto}'
     '.message{height:200px}</style><div id="panel"><div style="padding-top:40px">'
     '<div class="message" data-message-id="user">Original prompt</div>'
     '<div class="message" data-message-id="assistant">Original response</div></div></div>')
    panel=page.locator('#panel');panel.evaluate('(e)=>e.scrollTop=e.scrollHeight')
    messages=page.locator('[data-message-id]')
    before=messages.evaluate_all(HISTORY_OLDER_UI_JS,{'action':'observe'})
    self.assertIsNone(before['role']);self.assertIsNone(before['slot'])
    self.assertGreater(before['scroll_top'],0)
    broken=HISTORY_OLDER_UI_JS.replace('(observed[k]??null)===(previous[k]??null)',
     'observed[k]===previous[k]')
    self.assertFalse(messages.evaluate_all(broken,
     {'action':'show_oldest','observed':before})['scrolled'])
    action=messages.evaluate_all(HISTORY_OLDER_UI_JS,{'action':'show_oldest','observed':before})
    self.assertTrue(action['scrolled']);self.assertEqual(action['after_scroll_top'],0)
    self.assertEqual(panel.evaluate('(e)=>e.scrollTop'),0)
    messages.first.evaluate('(e)=>e.setAttribute("data-message-id","changed-user")')
    refused=messages.evaluate_all(HISTORY_OLDER_UI_JS,{'action':'show_oldest','observed':before})
    self.assertFalse(refused['scrolled'])
   finally:
    browser.close()

 def test_older_history_aborts_challenge_and_respects_existing_deadline(self):
  own,current,binding=self.reloaded_94_fixture();anchors=identities(own)
  bridge,room,_,_=self.setup(Bridge(current[-30:]));bridge.operation_deadline=80
  actions=self.older_ui(bridge,room,[])
  bridge.status=lambda:'CHALLENGE_REQUIRED'if actions else'CONNECTED'
  with self.assertRaises(DailyRoomUnavailable):room.restore_older(bridge.messages,anchors,binding,'BOUND')
  self.assertEqual(actions,['show_oldest']);self.assertEqual(bridge.current,0)
  bridge,room,_,_=self.setup(Bridge(current[-30:]));bridge.operation_deadline=0.1
  actions=self.older_ui(bridge,room,[])
  self.assertIsNone(room.restore_older(bridge.messages,anchors,binding,'BOUND'))
  self.assertAlmostEqual(bridge.current,0.1);self.assertEqual(actions,['show_oldest'])

 def test_older_history_does_not_scroll_duplicate_or_unbound_history_title(self):
  own,current,binding=self.reloaded_94_fixture();anchors=identities(own)
  for titles in (['Different room'],['Existing daily room','Existing daily room']):
   with self.subTest(titles=titles):
    bridge,room,_,_=self.setup();bridge.operation_deadline=80
    room.history.rooms=[(title,current[-30:])for title in titles]
    room.restore_older=Mock(side_effect=AssertionError('AMBIGUOUS_HISTORY_MUST_NOT_SCROLL'))
    with self.assertRaises(DailyRoomUnavailable):room.restore(anchors,binding,'BOUND')
    room.restore_older.assert_not_called()
    self.assertEqual(bridge.clicks,0);self.assertEqual(bridge.fresh_calls,0)

 def test_older_history_retention_handles_more_rows_than_saved_anchors(self):
  own,current,binding=self.reloaded_94_fixture()
  bridge,room,_,_=self.setup(Bridge(current))
  room.retain_history_window(current,identities(own[-30:]),binding,{'available':False})
  value=json.loads((Path(self.directory.name)/'on-daily-room-window.json').read_text())
  self.assertEqual(len(value['windows'][0]['native_rows']),94)
  self.assertFalse(value['windows'][0]['native_rows'][0]['expected_tail_match'])



class PassiveHistoryNetworkDiagnosticTests(unittest.TestCase):
 @unittest.skipUnless(shutil.which('node'), 'Node is needed to execute the fixed browser diagnostic')
 def test_observes_page_requests_without_exporting_ids_queries_or_other_resources(self):
  entries=[dict(name='https://app.neurobro.ai/api/chats/ROOM_SECRET?token=TOKEN_SECRET#FRAGMENT_SECRET',
   initiatorType='fetch',startTime=10.4,duration=2.8,responseStatus=403),
   dict(name='https://auth.privy.io/api/session',initiatorType='xmlhttprequest',startTime=15,duration=4,responseStatus=200),
   dict(name='https://app.neurobro.ai/private.js?secret=SCRIPT_SECRET',initiatorType='script',startTime=1,duration=2,responseStatus=200)]
  script=('const location=new URL("https://app.neurobro.ai/");'
   'const performance={now:()=>500,getEntriesByType:()=>'+json.dumps(entries)+'};'
   'const root={querySelectorAll:()=>[]};'
   'const observe='+HISTORY_DIAGNOSTIC_JS+';'
   'process.stdout.write(JSON.stringify(observe(root,"messages")));')
  result=subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)
  value=json.loads(result.stdout)['network'];self.assertTrue(value['available'])
  self.assertEqual(value['request_count'],2)
  self.assertEqual(value['resources'][0],dict(origin='SAME_ORIGIN',path='/api/chats/:redacted',
   initiator='fetch',start_ms=10,duration_ms=3,response_status=403))
  self.assertEqual(value['resources'][1]['origin'],'PRIVY')
  for secret in ('ROOM_SECRET','TOKEN_SECRET','FRAGMENT_SECRET','SCRIPT_SECRET','?token='):
   self.assertNotIn(secret,result.stdout)
  # A busy document remains bounded; resource metadata is not a response body.
  script=script.replace(json.dumps(entries),json.dumps(entries[:1]*80))
  value=json.loads(subprocess.run(['node','-e',script],check=True,capture_output=True,text=True).stdout)['network']
  self.assertEqual(value['request_count'],80);self.assertEqual(len(value['resources']),60)
