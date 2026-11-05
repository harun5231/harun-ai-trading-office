"""Observe current received analysis without treating an old HOLD as completion."""
import copy
import json
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.native_daily_room import DailyRoom, JOURNAL_NAME, identities
from agent.native_prompt_composition import PromptCompositionCache
from agent.native_file_upload import FILE_WITNESS
from agent import native_received_analysis as observation
from agent.native_screening_recovery import recover, enqueue, NATIVE_DOM_SHA256
from tests.test_native_daily_room import Bridge, History, native


class Images:
    def __init__(self, names): self.names = names
    def count(self): return len(self.names)
    def nth(self, index):
        return SimpleNamespace(get_attribute=lambda key: self.names[index], is_visible=lambda: True,
            evaluate=lambda script: dict(complete=True, width=600, height=400),
            scroll_into_view_if_needed=lambda **kw: (_ for _ in ()).throw(AssertionError('No scrolling')))


class Message:
    def __init__(self, row): self.row = row
    def locator(self, selector):
        assert selector == 'img'
        return Images(self.row.get('display_names', [])[:2])
    def evaluate(self, script, args):
        assert script == FILE_WITNESS
        return [dict(name=name, ready=name in self.row.get('display_names', []),
                     matches=int(name in self.row.get('display_names', []))) for name in args['names']]


class ReadBridge(Bridge):
    def _turns(self):
        return [{key: value for key, value in row.items() if key != 'display_names'}
                for row in copy.deepcopy(self.messages)]
    def _visible(self, selector):
        if selector == 'messages': return [Message(row) for row in self.messages]
        return super()._visible(selector)


class ReceivedAnalysisObservationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name); self.raw = ReadBridge(); self.native = native()
        self.native.USER_BODY = 'user-body'
        body = dict(operation=observation.OPERATION,
            marker='HARUN_' + observation.text_sha(observation.OPERATION)[:32],
            provider='NEUROBRO_WEB', contract_version='neurobro-browser-decision-v2',
            prompt='Analisa RUNEUSDT dari dua chart Futures.', context=dict(symbol='RUNEUSDT'),
            output_schema=dict(required=['symbol','side','limit_entry','take_profit','stop_loss','risk_reward']))
        self.payload = dict(body, request_sha256=observation.digest(body))
        # Controlled request hash; production remains pinned to the actual f34e request.
        self.pin = patch.object(observation, 'REQUEST_SHA256', self.payload['request_sha256'])
        self.pin.start(); self.addCleanup(self.pin.stop)
        self.source = patch('agent.native_screening_recovery._native_source_digest', return_value=NATIVE_DOM_SHA256)
        self.source.start(); self.addCleanup(self.source.stop)
        self.native.user_prompt = lambda p: p['prompt'] + '\nAcuan percakapan: ' + p['marker']
        self.room = DailyRoom(self.raw, self.native, self.root, history=History(self.raw))
        self.cache = PromptCompositionCache(self.native, self.root)
        rendered = self.cache.store(self.payload, self.payload['prompt'], 'a'*64)
        self.names = ['rune-15m.png', 'rune-1h.png', 'RUNEUSDT_15m_200_closed_12345678.csv', 'RUNEUSDT_1h_200_closed_12345678.csv']
        self.new_names = ['rune-new-15m.png', 'rune-new-1h.png', 'RUNEUSDT_15m_200_closed_87654321.csv', 'RUNEUSDT_1h_200_closed_87654321.csv']
        self.followup = 'Berikut chart terbaru, lanjutkan pembahasan RUNEUSDT.\nAcuan ' + self.payload['marker']
        self.raw.messages = [dict(role='user', text='Screening lama', attachments=[]),
            dict(role='assistant', text='Dua coin dipilih.', complete=True, id='screen-1'),
            dict(role='user', text='Klarifikasi coin lama', attachments=[]),
            dict(role='assistant', text='RUNEUSDT dan TIAUSDT.', complete=True, id='screen-2'),
            dict(role='user', text='Diskusi terdahulu', attachments=[]),
            dict(role='assistant', text='Konteks terdahulu.', complete=True, id='older'),
            dict(role='user', text=rendered, attachments=['UNCONFIRMED'], display_names=self.names),
            dict(role='assistant', text='HOLD awal. Kedua chart terbaca.', complete=True, id='initial-rune'),
            dict(role='user', text=self.followup, attachments=['UNCONFIRMED'], display_names=self.new_names),
            dict(role='assistant', text='Balasan FINAL TERBARU yang belum dipahami robot.', complete=True, id='latest-rune')]
        with self.room.store.open() as db:
            db.execute('INSERT INTO daily_rooms VALUES(?,?,?,?,?,?,?)', ('2026-11-05','a'*32,'BOUND',
                self.raw.page.url, json.dumps(identities(self.raw._turns())), json.dumps(dict(
                    first_prompt_sha256=observation.text_sha('Screening lama'), marker='HARUN_old',
                    history_titles=['Analisis RUNE'])), 1793871127))
        self.before = (self.root / JOURNAL_NAME).read_bytes()
        followup_dir = self.root / 'dialogue-200-followup'; followup_dir.mkdir(mode=0o700)
        key = observation.text_sha(json.dumps(dict(operation=observation.OPERATION,
            request_sha256=self.payload['request_sha256']),sort_keys=True,separators=(',',':')))
        receipt = dict(operation=observation.OPERATION, request_sha256=self.payload['request_sha256'],
            marker=self.payload['marker'], followup_prompt_sha256=observation.text_sha(self.followup),
            initial_attachment_names=self.names, followup_attachment_names=self.new_names)
        path=followup_dir / (key+'.intent.json'); path.write_text(json.dumps(receipt)); path.chmod(0o600)
        self.status = 'CONNECTED'; self.status_calls = []
        def status(*, maintenance_allowed=None):
            if maintenance_allowed is not None:
                self.assertTrue(callable(maintenance_allowed)); self.assertFalse(maintenance_allowed())
            self.status_calls.append(1); return self.status
        self.raw.status = status
        self.raw.session_observation = lambda: dict(reason='CURRENT_OBSERVED_UI', counts=dict(messages=10))
        self.actor = SimpleNamespace(_bridge=None, thread=SimpleNamespace(ident=threading.get_ident()),
            stopping=threading.Event(), claim=threading.Lock(), mode='AUTOMATION', ready=True,
            current_status='CONNECTED', sessions=SimpleNamespace(active=lambda:False), lock=threading.RLock(),
            queue=queue.Queue(), _wake=threading.Event())
        self.actor._mode = lambda: self.actor.mode
        self.delegated = SimpleNamespace(daily_room=self.room, bridge=self.raw, native=self.native, prompt_cache=self.cache)
        self.actor._bridge = self.delegated
        def forbidden(*args, **kwargs): self.fail('Observer sent, uploaded, typed, created a room, or called Mini')
        self.raw.ask = self.raw._upload = self.raw._answer = self.raw._fresh_conversation = forbidden
        self.raw.composer.fill = self.raw.button.click = forbidden
        self.request = dict(action=observation.ACTION, payload=self.payload)

    def invoke(self):
        with self.actor.claim: return recover(self.actor, self.delegated, self.request)

    def capture(self, result):
        return json.loads((self.root / result['capture_name']).read_text())

    def test_current_ten_rows_four_file_tiles_and_status_are_saved_without_completion(self):
        old = (self.raw._visible, self.raw.operation_deadline)
        result = self.invoke(); capture = self.capture(result)
        self.assertEqual(result['row_count'],10); self.assertEqual(result['status'],'CONNECTED')
        self.assertTrue(result['same_loaded_room_verified']); self.assertFalse(result['operation_completed'])
        self.assertNotIn('response_id', result); self.assertNotIn('assistant_text', result); self.assertNotIn('output',result)
        self.assertEqual(capture['rows'][-1]['id'],'latest-rune')
        self.assertIn('FINAL TERBARU',capture['rows'][-1]['text'])
        self.assertEqual(capture['session_observation']['reason'],'CURRENT_OBSERVED_UI')
        self.assertEqual(len(capture['receipt_files']),1)
        tiles = {item['row']:item for item in capture['file_tiles']}
        self.assertEqual(len(tiles[8]['images']),2)
        self.assertTrue(all(row['ready'] for item in tiles[8]['file_tile_witnesses']
            if item['names'] == self.new_names for row in item['witness']))
        self.assertEqual(capture['provider_messages'],0); self.assertEqual(capture['model_calls'],0)
        self.assertEqual((self.root/JOURNAL_NAME).read_bytes(),self.before)
        self.assertEqual((self.raw._visible,self.raw.operation_deadline),old)

    def test_manual_changed_payload_and_wrong_actor_thread_refuse_before_dom(self):
        for change in ('manual','human-session','thread','payload'):
            self.actor.mode='AUTOMATION'; self.actor.sessions=SimpleNamespace(active=lambda:False)
            self.actor.thread=SimpleNamespace(ident=threading.get_ident())
            request=copy.deepcopy(self.request)
            if change=='manual': self.actor.mode='MANUAL'
            if change=='human-session': self.actor.sessions=SimpleNamespace(active=lambda:True)
            if change=='thread': self.actor.thread.ident+=1
            if change=='payload': request['payload']['prompt']+=' changed'
            with self.actor.claim:
                with self.assertRaises(ValueError): recover(self.actor,self.delegated,request)
        self.assertEqual(self.status_calls,[])
        self.assertFalse(list(self.root.glob('received-analysis-observation-*.json')))

    def test_current_verification_is_observed_without_interacting_or_claiming_connected(self):
        self.status='CHALLENGE_REQUIRED'
        result=self.invoke(); capture=self.capture(result)
        self.assertEqual(result['status'],'CHALLENGE_REQUIRED')
        self.assertFalse(result['operation_completed']); self.assertEqual(capture['uploads'],0)
        self.assertEqual((self.root/JOURNAL_NAME).read_bytes(),self.before)

    def test_existing_bound_history_can_be_reopened_after_restart_without_new_chat(self):
        self.room.history.rooms=[('Analisis RUNE',copy.deepcopy(self.raw.messages))]
        self.raw.messages=[]
        result=self.invoke()
        self.assertEqual(result['row_count'],10); self.assertTrue(result['history_restored'])
        self.assertTrue(result['same_loaded_room_verified'])
        self.assertEqual(self.room.history.selected,['Analisis RUNE'])
        self.assertEqual((self.root/JOURNAL_NAME).read_bytes(),self.before)

    def test_tile_failure_keeps_current_reply_before_error_and_never_completes(self):
        with patch.object(observation,'_file_tiles',side_effect=ValueError('ATTACHMENT_WITNESS_UNAVAILABLE')):
            result=self.invoke()
        capture=self.capture(result)
        self.assertEqual(result['error_count'],1)
        self.assertEqual(capture['rows'][-1]['text'],self.raw.messages[-1]['text'])
        self.assertEqual(capture['errors'][0]['reason'],'ATTACHMENT_WITNESS_UNAVAILABLE')
        self.assertFalse(result['operation_completed'])
        self.assertEqual((self.root/JOURNAL_NAME).read_bytes(),self.before)

    def test_observation_uses_existing_actor_queue_and_allows_fresh_status_read(self):
        self.actor.current_status='UNAVAILABLE'
        def loop():
            self.actor.thread=threading.current_thread(); task=self.actor.queue.get(timeout=2)
            try: task['result']=recover(self.actor,self.delegated,task['request'])
            except BaseException as error: task['error']=error
            finally: self.actor.claim.release(); task['done'].set(); self.actor.queue.task_done()
        thread=threading.Thread(target=loop); thread.start()
        result=enqueue(self.actor,self.request); thread.join(timeout=2)
        self.assertEqual(result['status'],'CONNECTED'); self.assertFalse(self.actor.claim.locked())
        self.assertEqual(result['kind'],observation.KIND)
        self.assertFalse(result['operation_completed'])


if __name__=='__main__': unittest.main()
