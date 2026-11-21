"""Actual terminal UI evidence stays technical; no browser/provider calls."""
import copy
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from agent import native_received_analysis as observer
from agent import native_received_screening as screening
from agent.native_daily_room import DailyRoom,DailyRoomUnavailable
from agent.native_daily_room import identities
from agent.native_file_upload import NativeFileTurnReader
from agent.native_browser_hook import DelegatedDOMBridge
from agent.native_screening_recovery import NATIVE_DOM_SHA256
from tests import test_received_screening_rounds as existing_rounds
from tests.test_native_daily_room import History,native
from tests.test_native_received_analysis import ReadBridge

FIXTURE=json.loads((Path(__file__).parent/'fixtures/neurobro_terminal_screening_failure_20261121.json').read_text())


def successor(payload,index=10,*,day='2026-11-05',epoch=0):
    value=copy.deepcopy(payload)
    value['operation']=f'{day}:robot-v9:{epoch}:screening:{index}:1'
    value['marker']='HARUN_'+observer.text_sha(value['operation'])[:32]
    value['request_sha256']=observer.digest({key:item for key,item in value.items()if key!='request_sha256'})
    return value


class TerminalScreeningErrorTests(unittest.TestCase):
    def setUp(self):
        self.case=existing_rounds.ReceivedScreeningRoundsTests('test_round9_reads_actual_owned_reply_in_same_bound_room_without_model_or_web_send')
        self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.c=self.case.case;self.payload=self.case.payload
        self.c.raw.messages.pop();self.case.anchor()
        self.rendered=self.c.cache.read(self.payload)['rendered_prompt']
        self.body='Earlier bound discussion\n'+self.rendered+FIXTURE['suffix']
        self.body_reads=0
        def read(*,timeout):
            self.body_reads+=1;return self.body
        self.c.raw.page.locator=lambda selector:SimpleNamespace(inner_text=read)if selector=='body'else self.fail('Only body read permitted')

    def observe(self):return self.case.invoke()

    def test_actual_suffix_recognized_only_after_unique_exact_current_prompt(self):
        raw=FIXTURE['rendered_prompt'];marker='HARUN_'+observer.text_sha(FIXTURE['native_operation'])[:32]
        self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(),FIXTURE['rendered_prompt_sha256'])
        body='Archived room\n'+raw+FIXTURE['suffix']
        ui=screening.terminal_body_ui(body,raw,marker)
        self.assertEqual(ui['suffix'],FIXTURE['suffix']);self.assertEqual(ui['body_sha256'],observer.text_sha(body))
        self.assertEqual(ui['body_bytes'],len(body.encode()));self.assertNotIn('retry',ui)
        unsafe=(body+'More assistant text',body.replace('15.24','25.24'),body.replace('tidak dapat menjawab','bisa menjawab'),
            raw+'\nQuoted: '+FIXTURE['suffix'],raw+'\n'+raw+FIXTURE['suffix'],marker+'\n'+body,
            FIXTURE['suffix']+'\n'+raw+'\nWaiting for answer')
        for value in unsafe:
            with self.subTest(body=value[-160:]):self.assertIsNone(screening.terminal_body_ui(value,raw,marker))

    def test_existing_observer_seals_negative_four_reads_without_assistant_or_completion(self):
        before=copy.deepcopy(self.c.raw.messages);result=self.observe();proof=result['observation'];error=proof['screening_terminal_error']
        self.assertEqual(self.body_reads,4);self.assertEqual(error['stable_snapshot_reads'],4)
        self.assertFalse(result['operation_completed']);self.assertFalse(proof['operation_scope']['verified'])
        self.assertIsNone(proof['operation_scope']['reply_row']);self.assertFalse(proof['operation_scope']['reply_complete'])
        self.assertEqual(proof['row_offset'],6);self.assertEqual(len(proof['rows']),1)
        self.assertEqual(proof['rows'][0]['role'],'user');self.assertEqual(error['user_row'],6)
        self.assertEqual(error['row_identities'],identities(self.c.raw._turns()))
        self.assertEqual(error['native_source_sha256'],NATIVE_DOM_SHA256)
        self.assertEqual(error['observed_at'],proof['observed_at'])
        self.assertEqual(self.c.raw.messages,before);self.assertEqual(self.c.raw.clicks,0)
        self.assertEqual((proof['model_calls'],proof['provider_messages'],proof['uploads'],proof['orders_sent']),(0,0,0,0))
        path=self.c.root/screening._terminal_error_name(error['room']['day'],error['room']['room_id'],error['row_identities'])
        original=path.read_bytes();info=path.stat()
        screening.retain_terminal_error(self.c.root,{**error,'observed_at':error['observed_at']+1},self.rendered)
        self.assertEqual(path.read_bytes(),original);self.assertEqual(path.stat().st_ino,info.st_ino)

    def test_only_observed_control_slots_are_accepted_without_rewriting_raw_body(self):
        caption='Neurobro tidak dapat menjawab pertanyaan ini'
        for flags in range(8):
            suffix=FIXTURE['suffix']
            if flags&1:suffix=suffix.replace('\n15.24\n','\nTampilkan lebih banyak\n15.24\n')
            if flags&2:suffix=suffix.replace(caption+'\nTanyakan apa saja',caption+'\nCoba lagi\nTanyakan apa saja')
            if flags&4:suffix=suffix.replace('Tanyakan apa saja\n\nNeurobro','Tanyakan apa saja\nRingan\nPrompt\n\nNeurobro')
            with self.subTest(controls=flags):
                body='Unrelated ticker 123.45\n'+self.rendered+suffix
                ui=screening.terminal_body_ui(body,self.rendered,self.payload['marker'])
                self.assertIsNotNone(ui);self.assertEqual(ui['body_text'],body)
                self.assertEqual(ui['body_sha256'],observer.text_sha(body));self.assertEqual(ui['suffix'],suffix)
        invalid=(FIXTURE['suffix'].replace('\n15.24\n','\nCoba lagi\n15.24\n'),
            FIXTURE['suffix'].replace('Tanyakan apa saja\n','Tanyakan apa saja\nRingan\n'),
            FIXTURE['suffix'].replace('Tanyakan apa saja\n','Tanyakan apa saja\nPrompt\n'),
            FIXTURE['suffix'].replace(caption,caption+'\nOther control'),
            '\nQuoted terminal message:\n'+FIXTURE['suffix'])
        for suffix in invalid:
            with self.subTest(invalid=suffix):
                self.assertIsNone(screening.terminal_body_ui(self.rendered+suffix,self.rendered,self.payload['marker']))

    def test_unrelated_prefix_prices_can_change_across_samples_and_after_persistence(self):
        values=iter(['Ticker '+price+'\n'+self.rendered+FIXTURE['suffix']for price in('10.01','10.02','10.03','10.04')])
        self.c.raw.page.locator=lambda _:SimpleNamespace(inner_text=lambda **kwargs:next(values))
        result=self.observe();proof=result['observation'];error=proof['screening_terminal_error']
        self.assertEqual(proof['errors'],[]);self.assertEqual(proof['stable_snapshot_reads'],4)
        self.assertTrue(error['terminal_ui']['body_text'].startswith('Ticker 10.01\n'))
        self.body='Ticker 10.05\n'+self.rendered+FIXTURE['suffix']
        self.c.raw.page.locator=lambda _:SimpleNamespace(inner_text=lambda **kwargs:self.body)
        changed={**error,'observed_at':error['observed_at']+1,
            'terminal_ui':screening.terminal_body_ui(self.body,self.rendered,self.payload['marker'])}
        self.assertNotEqual(error['terminal_ui']['body_sha256'],changed['terminal_ui']['body_sha256'])
        self.assertEqual(screening.terminal_ui_authority(error['terminal_ui']),screening.terminal_ui_authority(changed['terminal_ui']))
        path=self.c.root/screening._terminal_error_name(error['room']['day'],error['room']['room_id'],error['row_identities'])
        original=path.read_bytes();screening.retain_terminal_error(self.c.root,changed,self.rendered)
        self.assertEqual(path.read_bytes(),original)
        room=self.c.room;room.day='2026-11-05';room.room_id='a'*32
        self.assertTrue(screening.terminal_tail_for_successor(room,room.full_turns(),successor(self.payload)))
        self.assertEqual(path.read_bytes(),original)
        for suffix in(FIXTURE['suffix'].replace('15.24','15.25'),FIXTURE['suffix'].replace('tidak dapat menjawab','bisa menjawab')):
            self.body='Ticker 10.06\n'+self.rendered+suffix
            with self.assertRaises(ValueError):screening.terminal_tail_for_successor(room,room.full_turns(),successor(self.payload))
        self.c.raw.messages[-1]['text']+=' changed authored text'
        self.assertFalse(screening.terminal_tail_for_successor(room,room.full_turns(),successor(self.payload)))

    def test_known_caption_with_unknown_suffix_is_diagnostic_only(self):
        self.body+='Unknown trailing UI control'
        result=self.observe();proof=result['observation']
        self.assertNotIn('screening_terminal_error',proof)
        diagnostic=proof['terminal_ui_diagnostic']
        self.assertEqual(diagnostic['kind'],'DIAGNOSTIC_ONLY_TERMINAL_UI_UNVERIFIED')
        self.assertEqual(diagnostic['body_sha256'],observer.text_sha(self.body))
        self.assertEqual(diagnostic['body_tail'],self.body[-4000:])
        self.assertFalse(result['operation_completed']);self.assertFalse(proof['operation_scope']['verified'])

    def test_proof_rejects_changed_source_room_prompt_scope_extra_assistant_or_placeholder(self):
        result=self.observe();full=json.loads((self.c.root/result['capture_name']).read_bytes())
        ui=full['screening_terminal_error']['terminal_ui']
        cases=(lambda value:value.update(native_source_sha256='0'*64),
            lambda value:value['room'].update(day='2026-11-06'),lambda value:value.update(page_url='https://app.neurobro.ai/other'),
            lambda value:value['rows'][-1].update(text='Foreign current prompt'),
            lambda value:value['rows'].append(dict(role='assistant',text='Late actual answer',complete=True,id='late')),
            lambda value:value['rows'].append(dict(role='unknown',text='',complete=False)),
            lambda value:value['rows'].append(copy.deepcopy(value['rows'][-1])),
            lambda value:value.update(stable_snapshot_reads=3),lambda value:value.update(review_receipts=[{}]))
        for change in cases:
            with self.subTest(change=change):
                changed=copy.deepcopy(full);change(changed)
                with self.assertRaises(ValueError):screening.terminal_error_proof(changed,self.payload,ui)

    def test_changed_terminal_ui_during_sampling_remains_unverified(self):
        values=iter([self.body,self.body+' Changed terminal page'])
        self.c.raw.page.locator=lambda _:SimpleNamespace(inner_text=lambda **kwargs:next(values))
        result=self.observe();proof=result['observation']
        self.assertTrue(proof['errors']);self.assertNotIn('screening_terminal_error',proof)
        self.assertFalse(result['operation_completed'])

    def room_at_day(self,raw=None,*,day='2026-11-05'):
        raw=self.c.raw if raw is None else raw
        moment=datetime.fromisoformat(day).replace(hour=12,tzinfo=timezone.utc).timestamp()
        module=self.c.native if raw is self.c.raw else native()
        return DailyRoom(raw,module,self.c.root,wall_clock=lambda:moment,history=History(raw))if raw is not self.c.raw else self.c.room

    def test_restart_successor_preserves_odd_prefix_and_analysis_four_file_scope(self):
        self.observe();before=copy.deepcopy(self.c.raw.messages);raw=ReadBridge();raw.messages=copy.deepcopy(before)
        raw.page.locator=self.c.raw.page.locator;room=self.room_at_day(raw)
        new=successor(self.payload)
        with room.operation(new):
            room.fresh();self.assertEqual(room.offset,7);self.assertEqual(raw._turns(),[])
            raw.messages.extend([dict(role='user',text='Next screening',attachments=[],id='next-user'),
                dict(role='assistant',text='COIN: FILUSDT',complete=True,id='next-answer')])
            self.assertEqual(len(raw._turns()),2);self.assertEqual(raw.messages[:7],before)
        with room.operation(dict(operation='2026-11-05:robot-v9:0:analysis-v9:FILUSDT',marker='analysis-marker',prompt='Analisa FILUSDT')):
            room.fresh();self.assertEqual(room.offset,9)
            names=('FILUSDT_1h.png','FILUSDT_15m.png','FILUSDT_1h.csv','FILUSDT_15m.csv')
            raw.messages.extend([dict(role='user',text='FILUSDT materials',attachments=list(names[:2]),id='data-user'),
                dict(role='assistant',text='HOLD berdasarkan pasar',complete=True,id='data-answer')])
            reader=NativeFileTurnReader(raw,room.native)
            reader.turn_files[names]=(names[:2],names[2:],lambda node,names,user:node.row.get('id')=='data-user')
            raw.uploaded_names=list(names);raw.operation_deadline=100
            actual=reader.read();self.assertEqual(len(actual),2);self.assertEqual(actual[0]['attachments'],list(names))
            self.assertEqual(raw.messages[:7],before)
        self.assertEqual((raw.clicks,raw.fresh_calls),(0,0))

    def test_same_request_skipped_index_cycle_day_or_missing_corrupt_receipt_cannot_admit_tail(self):
        result=self.observe();error=result['observation']['screening_terminal_error']
        room=self.c.room;room.day='2026-11-05';room.room_id='a'*32
        values=(self.payload,successor(self.payload,11),successor(self.payload,10,epoch=1),successor(self.payload,10,day='2026-11-06'))
        for payload in values:
            with self.subTest(operation=payload['operation']):
                with self.assertRaises(ValueError):screening.terminal_tail_for_successor(room,room.full_turns(),payload)
        name=screening._terminal_error_name(room.day,room.room_id,error['row_identities']);path=self.c.root/name
        original=path.read_bytes()
        bad=([],None,{**error,'kind':'NOT_SENT'},{**error,'schema_version':True},
            {**error,'row_count':True},{**error,'stable_snapshot_reads':True},
            {**error,'observed_at':float('nan')},{**error,'observed_at':time.time()+3600},
            {**error,'terminal_ui':None},
            {**error,'terminal_ui':{**error['terminal_ui'],'rendered_prompt_occurrences':True}})
        for invalid in bad:
            with self.subTest(invalid=type(invalid).__name__):
                path.write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):screening.terminal_tail_for_successor(room,room.full_turns(),successor(self.payload))
        path.write_text(json.dumps({**error,'terminal_ui':None}))
        self.body='Earlier bound discussion\n'+self.rendered+'\nNo terminal error'
        with self.assertRaises(ValueError):screening.terminal_tail_for_successor(room,room.full_turns(),successor(self.payload))
        path.unlink();self.assertFalse(screening.terminal_tail_for_successor(room,room.full_turns(),successor(self.payload)))
        path.write_bytes(original);path.chmod(0o600)

    def test_unsealed_tail_stays_blocked_and_late_actual_assistant_uses_normal_path(self):
        self.c.room.wall_clock=lambda:datetime(2026,11,5,12,tzinfo=timezone.utc).timestamp()
        with self.assertRaises(DailyRoomUnavailable),self.c.room.operation(successor(self.payload)):
            self.c.room.fresh()
        self.observe();reads=self.body_reads
        self.c.raw.messages.append(dict(role='assistant',text='Late actual answer',complete=True,id='late-real'))
        with self.c.room.operation(successor(self.payload)):
            self.c.room.fresh();self.assertEqual(self.c.room.offset,8)
        self.assertEqual(self.body_reads,reads)
        self.assertEqual(self.c.raw.messages[-1]['text'],'Late actual answer')
        self.assertEqual((self.c.raw.fresh_calls,self.c.raw.clicks),(0,0))

    def test_rollover_uses_existing_new_day_path_and_does_not_reuse_old_tail_authority(self):
        self.observe();raw=ReadBridge();raw.messages=copy.deepcopy(self.c.raw.messages)
        room=self.room_at_day(raw,day='2026-11-06')
        with room.operation(successor(self.payload,10,day='2026-11-06')):
            room.fresh();self.assertEqual(room.day,'2026-11-06');self.assertEqual(room.offset,0)
        self.assertEqual(raw.fresh_calls,1);self.assertEqual(raw.clicks,0)

    def test_owner_off_before_ask_or_at_final_send_gate_never_sends_successor(self):
        self.observe();before=copy.deepcopy(self.c.raw.messages)
        raw=ReadBridge();raw.messages=copy.deepcopy(before);raw.page.locator=self.c.raw.page.locator
        room=self.room_at_day(raw);client=Mock()
        hook=DelegatedDOMBridge(raw,client,native=room.native,daily_room=room,
            monotonic=raw.clock,wall_clock=room.wall_clock)
        for decisions in((False,),(True,False)):
            with self.subTest(permission=decisions):
                values=iter(decisions)
                permission=Mock(side_effect=lambda:next(values))
                with self.assertRaises(room.native.NotSubmitted):
                    hook.ask(successor(self.payload),permission,deadline=80)
                self.assertEqual(raw.clicks,0);self.assertEqual(raw.messages,before)
        client.request.assert_not_called();client.compose.assert_not_called()


class ActualNativeTurnReaderTests(unittest.TestCase):
    def test_actual_current_reader_accepts_real_odd_prefix_without_fabricating_failure_answer(self):
        class Selection:
            def __init__(self,row,count=1):self.row,self.number=row,count
            def count(self):return self.number
            def text_content(self):return self.row['text']
            def locator(self,_):return Selection(self.row)
        class Turn:
            def __init__(self,row,index):self.row,self.index=row,index
            def get_attribute(self,_):return f'id-{self.index}'
            def locator(self,selector):
                if selector=='user':return Selection(self.row,int(self.row['role']=='user'))
                if selector=='answer':return Selection(self.row,int(self.row['role']=='assistant'))
                if selector=='img':return Selection(self.row,0)
                raise AssertionError(selector)
        namespace=dict(re=re,MESSAGE='messages',USER_BODY='user',ASSISTANT_BODY='answer',COMPLETE_ACTIONS=('copy','like'),
            RAIL_INPUT_GROUP=object(),NotSubmitted=AssertionError)
        exec(FIXTURE['native_turns_source']['source'],namespace)
        rows=[dict(role='user',text='Actual failed user'),dict(role='user',text='New authorized screening'),
            dict(role='assistant',text='Actual subsequent answer')]
        raw=SimpleNamespace(uploaded_names=[],_dom_contract=None,_visible=lambda _:[Turn(row,i)for i,row in enumerate(rows)],
            _assistant_text=lambda answer:answer.row['text'])
        actual=namespace['_turns'](raw)
        self.assertEqual([row['role']for row in actual],['user','user','assistant'])
        self.assertEqual([row['text']for row in actual],[row['text']for row in rows])
        self.assertTrue(actual[-1]['complete']);self.assertEqual(len(actual),3)


if __name__=='__main__':unittest.main()
