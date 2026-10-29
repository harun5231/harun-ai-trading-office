"""Existing native image receipts plus real menu/file protocol admission."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from agent.native_file_upload import NativeDataFileDelivery
from agent.native_data_files import deliver_market_data_files
from tests.test_agent_closed_candles_200 import NOW, native_context_200


class NotSubmitted(Exception): pass


class Items:
    def __init__(self, values): self.values = values
    def count(self): return len(self.values)
    def nth(self, index): return self.values[index]


class Image:
    def __init__(self, fixture): self.f = fixture
    def is_visible(self): return True
    def evaluate(self, _):
        return {'complete':self.f.images_loaded,
                'width':200 if self.f.images_loaded else 0,
                'height':100 if self.f.images_loaded else 0}


class Label:
    def __init__(self, fixture, action): self.f, self.action = fixture, action
    def is_visible(self): return True
    def click(self, **_):
        self.f.actions.append(self.action)
        if self.action == 'plus': self.f.menu = True
        else: self.f.menu = False


class Area:
    def __init__(self, fixture, *, user=False): self.f, self.user = fixture, user
    def locator(self, selector):
        if selector == 'button:has(svg.lucide-plus)': return Items([Label(self.f, 'plus')])
        if selector == 'xpath=..': return self
        if selector == 'img': return Items([Image(self.f), Image(self.f)] if self.f.keep_images else [])
        raise AssertionError(selector)
    def get_attribute(self, name):
        assert name=='role'
        return 'presentation'
    def evaluate(self, script, arg):
        # Represents the actual independent attachment area: filenames quoted
        # in the message body do not create file tiles here.
        self.f.witnesses.append(copy.deepcopy(arg))
        present=self.f.file_tiles and self.f.show_file_tiles
        return [{'name':name, 'ready':present and not self.f.files_busy and self.f.current >= .3,
                 'matches':1 if present else 0} for name in arg['names']]


class Fixture:
    def __init__(self):
        self.current=0.; self.menu=False; self.actions=[]; self.witnesses=[]
        self.file_tiles=False; self.keep_images=True; self.uploaded_names=[]
        self.show_file_tiles=True; self.files_busy=False; self.images_loaded=True
        self.input_text=''; self.send_allowed=True; self.send_present=True
        self.sent=0; self.files=None; self.listeners={}; self.rows=[]
        self.timeout=30; self.page=self
        self._dom_contract=SimpleNamespace(area='area',send='send')
        self.composer=SimpleNamespace(locator=lambda _:Area(self),input_value=lambda:self.input_text)
        self._upload=self.native_upload; self._turns=self.native_turns
        self._answer=self.native_answer
    def native_upload(self, *args):
        self.actions.append('native-png-upload')
        self.uploaded_names=['15m.png','1h.png']
        return [{'attachment_id':'png-a'},{'attachment_id':'png-b'}]
    def native_turns(self):
        result=copy.deepcopy(self.rows)
        for row in result:
            if row['role']=='user':
                row['attachments']=(list(self.uploaded_names) if self.uploaded_names==['15m.png','1h.png'] else ['UNCONFIRMED'])
        return result
    def native_answer(self, prompt, attachment_names=()):
        rows=self._turns()
        if (len(rows)!=2 or rows[0]['text']!=prompt
                or rows[0]['attachments']!=list(attachment_names)
                or rows[1].get('complete')is not True): return None
        return rows[1]['text']
    def clock(self): return self.current
    def _until(self, seconds): return self.current+seconds
    def _timeout(self, _): return 5000
    def _visible(self, selector):
        if selector=='send':
            return [SimpleNamespace(is_enabled=lambda:self.send_allowed and bool(self.input_text))] if self.send_present else []
        if selector=='messages': return [Area(self,user=True) for _ in self.rows]
        raise AssertionError(selector)
    def get_by_text(self, label, **kwargs):
        assert label=='File' and kwargs=={'exact':True}
        return Items([Label(self,'File')] if self.menu else [])
    def expect_file_chooser(self, **_):
        fixture=self
        class Choice:
            def __enter__(self): return self
            def __exit__(self,*_): pass
            @property
            def value(self): return self
            def set_files(self, files, **_):
                fixture.files=files; fixture.file_tiles=True; fixture.actions.append('choose-csv')
        return Choice()
    def on(self, event, fn): self.listeners.setdefault(event,[]).append(fn)
    def remove_listener(self,event,fn):
        self.listeners[event].remove(fn)
        if not self.listeners[event]:self.listeners.pop(event)
    def wait_for_timeout(self,milliseconds): self.current+=milliseconds/1000


class DataFileUploadTests(unittest.TestCase):
    def setup(self):
        f=Fixture();native=SimpleNamespace(NotSubmitted=NotSubmitted,MESSAGE='messages',USER_BODY='native-user-body')
        self.gate=Mock()
        delivery=NativeDataFileDelivery(f,native,self.gate,wall_clock=lambda:NOW).install()
        payload={'context':native_context_200(),'operation':'native-analysis','request_sha256':'a'*64}
        return f,delivery,payload

    def test_menu_upload_preserves_two_native_png_receipts_and_uploads_exact_csv_bytes(self):
        f,d,payload=self.setup();before=copy.deepcopy(payload)
        result=f._upload(f.composer,payload,[])
        self.assertEqual(payload,before)
        self.assertEqual(result,[{'attachment_id':'png-a'},{'attachment_id':'png-b'}])
        self.assertEqual(f.actions,['native-png-upload','plus','File','choose-csv'])
        expected=deliver_market_data_files(payload['context'],now=NOW)
        self.assertEqual(f.files,[{k:r[k] for k in ('name','mimeType','buffer')} for r in expected])
        self.assertEqual(f.uploaded_names,['15m.png','1h.png']+[r['name'] for r in expected])
        self.assertGreaterEqual(f.current,.5)
        self.assertTrue(d.observations[0]['preview_verified'])
        self.assertEqual(f.sent,0);self.assertEqual(set(f.listeners),{'response','requestfailed'})
        d.close();self.assertEqual(f._upload,f.native_upload);self.assertEqual(f.listeners,{})
        self.assertEqual(f._turns,d.reader.read)

    def test_prompt_filenames_cannot_attest_missing_csv_attachment_tiles(self):
        f,d,payload=self.setup();f._upload(f.composer,payload,[])
        names=list(f.uploaded_names)
        f.rows=[{'role':'user','text':'Files: '+','.join(names)},
                {'role':'assistant','text':'actual answer','complete':True}]
        self.assertEqual(f._turns()[0]['attachments'],names)
        f.file_tiles=False
        self.assertEqual(f._turns()[0]['attachments'],['UNCONFIRMED'])
        self.assertEqual(f.uploaded_names,names)
        self.assertTrue(all(v['bodySelector']=='native-user-body' for v in f.witnesses if v['bodySelector']))
        f.file_tiles=True;d.close()
        self.assertEqual(f._turns()[0]['attachments'],names)  # Native receipt after ask.

    def test_assistant_arriving_between_snapshots_is_resampled_without_another_send(self):
        f,d,payload=self.setup();f._upload(f.composer,payload,[])
        names=list(f.uploaded_names)
        f.rows=[{'role':'user','text':'actual prompt'}]
        original=d.reader.original_turns;calls=[]
        def arriving():
            result=original();calls.append(len(result))
            if len(calls)==1:
                f.rows.append({'role':'assistant','text':'actual answer','complete':True})
            return result
        d.reader.original_turns=arriving
        self.assertEqual(f._answer('actual prompt',names),'actual answer')
        self.assertEqual(calls,[1,2])
        self.assertEqual(f.uploaded_names,names)
        self.assertEqual(f.sent,0)

    def test_resampling_never_confirms_missing_csv_witnesses(self):
        f,d,payload=self.setup();f._upload(f.composer,payload,[])
        names=list(f.uploaded_names);f.file_tiles=False
        f.rows=[{'role':'user','text':'actual prompt'}]
        original=d.reader.original_turns;calls=[]
        def arriving():
            result=original();calls.append(len(result))
            if len(calls)==1:
                f.rows.append({'role':'assistant','text':'actual answer','complete':True})
            return result
        d.reader.original_turns=arriving
        self.assertIsNone(f._answer('actual prompt',names))
        self.assertEqual(f._turns()[0]['attachments'],['UNCONFIRMED'])
        self.assertEqual(f.sent,0)

    def test_csv_upload_cannot_silently_replace_the_native_images(self):
        f,d,payload=self.setup();f.keep_images=False
        with self.assertRaises(NotSubmitted): f._upload(f.composer,payload,[])
        self.assertFalse(d.observations[0]['preview_verified']);self.assertEqual(f.sent,0)
        self.assertEqual(set(f.listeners),{'response','requestfailed'})
        d.close();self.assertEqual(f.listeners,{})

    def test_empty_composer_can_finish_ready_attachments_before_send_is_enabled(self):
        f,d,payload=self.setup()
        self.assertFalse(f._visible('send')[0].is_enabled())
        f._upload(f.composer,payload,[])
        preview=d.observations[0]['last_preview']
        self.assertTrue(d.observations[0]['preview_verified'])
        self.assertTrue(preview['composer_empty']);self.assertFalse(preview['send_enabled'])
        self.assertEqual(preview['image_count'],2)
        self.assertEqual(preview['images'],[{'complete':True,'width':200,'height':100}]*2)
        self.assertTrue(all(row['ready'] and row['matches']==1 for row in preview['file_witness']))
        self.assertEqual(f.input_text,'');self.assertEqual(f.sent,0)
        f.input_text='actual prompt'
        self.assertTrue(f._visible('send')[0].is_enabled());d.close()

    def test_missing_or_busy_csv_still_refuses_and_retains_last_preview(self):
        for state in ('missing','busy'):
            with self.subTest(state=state):
                f,d,payload=self.setup()
                f.show_file_tiles=state!='missing';f.files_busy=state=='busy'
                with self.assertRaises(NotSubmitted):f._upload(f.composer,payload,[])
                record=d.observations[0];self.assertFalse(record['preview_verified'])
                rows=record['last_preview']['file_witness']
                self.assertTrue(all(not row['ready']for row in rows))
                self.assertEqual([row['matches']for row in rows],[0,0]if state=='missing'else[1,1])
                self.assertEqual(f.sent,0);d.close()

    def test_unloaded_images_or_missing_send_control_still_refuses(self):
        for state in ('unloaded','missing-send'):
            with self.subTest(state=state):
                f,d,payload=self.setup()
                f.images_loaded=state!='unloaded';f.send_present=state!='missing-send'
                with self.assertRaises(NotSubmitted):f._upload(f.composer,payload,[])
                record=d.observations[0];self.assertFalse(record['preview_verified'])
                preview=record['last_preview']
                if state=='unloaded':self.assertEqual(preview['images'][0]['width'],0)
                else:self.assertEqual(preview['send_button_count'],0)
                self.assertEqual(f.sent,0);d.close()

    def test_stopped_dispatch_cannot_open_attachment_menu(self):
        f,d,payload=self.setup()
        def stop(): raise NotSubmitted('STOPPED')
        d.gate=stop
        with self.assertRaisesRegex(NotSubmitted,'STOPPED'):f._upload(f.composer,payload,[])
        self.assertEqual(f.actions,[]);self.assertIsNone(f.files);d.close()

    def test_current_rail_uses_presentation_parent_for_file_and_image_previews(self):
        f,d,payload=self.setup();f._dom_contract.name='RAIL_INPUT_GROUP'
        f._upload(f.composer,payload,[])
        self.assertTrue(d.observations[0]['preview_verified']);d.close()

    def test_same_origin_failure_is_retained_without_headers_or_query_tokens(self):
        f,d,payload=self.setup()
        response=SimpleNamespace(url='https://app.neurobro.ai/api/chat?token=private',status=413,
            request=SimpleNamespace(method='POST'),json=lambda:{'error':{'message':'too long','code':'LENGTH'},'token':'private'})
        d._observe_error_response(response)
        self.assertEqual(d.network_errors,[{'kind':'HTTP_RESPONSE','status':413,'method':'POST',
            'path':'/api/chat','error':{'error':{'message':'too long','code':'LENGTH'}}}])
        self.assertNotIn('private',str(d.network_errors));d.close()


if __name__=='__main__':unittest.main()
