"""Compare batched live DOM reads with the installed native f017 reader."""
import base64
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib

from agent.native_turn_batch import NativeTurnBatch, retain_return_failure

REFERENCE=Path(__file__).parent/'fixtures/native_dom_turns_f017.py'
spec=importlib.util.spec_from_file_location('native_f017_turn_reference',REFERENCE)
native=importlib.util.module_from_spec(spec);spec.loader.exec_module(native)


def png():
    def part(kind,raw):return struct.pack('>I',len(raw))+kind+raw+struct.pack('>I',zlib.crc32(kind+raw)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+part(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+part(b'IDAT',zlib.compress(b'\x00\x00\x00\x00'))+part(b'IEND',b'')


def footer():
    return '<div class="flex items-center -mx-1.5"><div class="flex items-center gap-0.5">'+''.join('<button><svg class="lucide-'+name+'"></svg></button>'for name in('copy','thumbs-up','thumbs-down'))+'</div></div>'


def pair(index,images=True):
    data=base64.b64encode(png()).decode()
    pictures=''.join('<img alt="'+name+'" src="data:image/png;base64,'+data+'">'for name in('frame-15m.png','frame-1h.png'))if images else''
    return '<section data-message-id="user-'+str(index)+'"><div class="whitespace-pre-wrap break-words">EXACT USER '+str(index)+'</div>'+pictures+'</section>'+\
      '<section data-message-id="answer-'+str(index)+'"><div class="text-foreground max-w-none"><p>HOLD '+str(index)+'</p><div data-slot="card"><div data-slot="card-header"><div data-slot="card-title">json</div></div><div><code><span class="react-syntax-highlighter-line-number">1</span>{"symbol":"ENAUSDT","side":"HOLD"}</code></div></div></div>'+footer()+'</section>'


@unittest.skipUnless(shutil.which('chromium'),'Existing local Chromium required; no install is attempted')
class ChromiumEquivalence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.playwright.stop()
    def setUp(self):
        self.context=self.browser.new_context()
        self.context.route('**/*',lambda route:route.abort())
        self.page=self.context.new_page()
        self.page.set_content('<style>section{display:block;width:800px}img{width:20px;height:20px}</style>'+''.join(pair(i)for i in range(12)),wait_until='load')
        self.bridge=native.NativeReference(self.page,('frame-15m.png','frame-1h.png'))
        self.reader=NativeTurnBatch(self.bridge,native)
        self.reader.begin({'operation':'owned:ENAUSDT','request_sha256':'a'*64})
    def tearDown(self):self.context.close()
    def equivalent(self):
        self.assertEqual(self.reader.read(),self.bridge._turns())
    def test_complete_daily_room_matches_native_exactly_and_does_not_reparse_it_per_poll(self):
        original=self.bridge._turns();self.assertEqual(len(original),24)
        self.assertEqual(self.reader.read(),original);self.assertTrue(self.reader.verified)
        for _ in range(4):self.assertEqual(self.reader.read(),original)
        self.assertEqual(self.reader.native_reads,1)
        self.assertIn('```json\n{"symbol":"ENAUSDT","side":"HOLD"}\n```',original[-1]['text'])
        self.assertNotIn('\n1',original[-1]['text'])
    def test_current_image_pending_hidden_or_wrong_alt_is_not_forged(self):
        self.reader.read()
        for update in ["image.src='https://invalid.test/pending.png'","image.style.visibility='hidden'","image.alt='foreign.png'"]:
            self.page.evaluate('(code)=>{const image=document.querySelector("[data-message-id=user-11] img");eval(code)}',update)
            self.equivalent();self.assertEqual(self.reader.read()[-2]['attachments'],['UNCONFIRMED'])
    def test_missing_footer_placeholder_and_later_response_remain_actual(self):
        self.reader.read()
        self.page.locator('[data-message-id=answer-11] button').first.evaluate('(button)=>button.remove()')
        self.equivalent();self.assertFalse(self.reader.read()[-1]['complete'])
        self.page.locator('[data-message-id=answer-11] .text-foreground').evaluate('(body)=>body.remove()')
        self.equivalent();self.assertEqual(self.reader.read()[-1],{'role':'unknown','text':'','complete':False})
        self.page.locator('body').evaluate('(body,html)=>body.insertAdjacentHTML("beforeend",html)',pair(12,False))
        self.equivalent();self.assertEqual(len(self.reader.read()),26)
    def test_prefix_mutation_and_hidden_rows_are_not_cached(self):
        self.reader.read()
        self.page.locator('[data-message-id=user-0] .whitespace-pre-wrap').evaluate('(user)=>user.textContent="CHANGED EARLIER USER"')
        self.equivalent();self.assertEqual(self.reader.read()[0]['text'],'CHANGED EARLIER USER')
        self.page.locator('[data-message-id=answer-0]').evaluate('(turn)=>turn.style.display="none"')
        self.equivalent();self.assertEqual(len(self.reader.read()),23)
    def test_native_visibility_matches_contents_opacity_and_content_visibility(self):
        self.reader.read()
        for css in ('opacity:0','display:contents','content-visibility:hidden','visibility:hidden','display:none'):
            self.page.locator('[data-message-id=answer-0]').evaluate('(turn,css)=>turn.setAttribute("style",css)',css)
            self.equivalent()
    def test_duplicate_or_invalid_message_id_refuses_as_native(self):
        self.reader.read()
        for identifier in ('answer-0','bad id'):
            self.page.locator('section').last.evaluate('(turn,id)=>turn.setAttribute("data-message-id",id)',identifier)
            with self.assertRaises(native.NotSubmitted):self.reader.read()
            with self.assertRaises(native.NotSubmitted):self.bridge._turns()
    def test_first_read_parity_mismatch_keeps_only_the_original_authority(self):
        original=self.reader.original_turns
        def changed():
            rows=original();rows[-1]['text']='FORGED OR CONTRACT DRIFT';return rows
        self.reader.original_turns=changed
        self.assertEqual(self.reader.read()[-1]['text'],'FORGED OR CONTRACT DRIFT')
        self.assertFalse(self.reader.verified)
    def test_empty_home_and_growing_reply_do_not_claim_message_parity(self):
        self.page.set_content('<div>empty home</div>')
        self.assertEqual(self.reader.read(),[]);self.assertFalse(self.reader.verified)
        self.page.locator('body').evaluate('(body,html)=>body.innerHTML=html',pair(0))
        self.page.locator('[data-message-id=answer-0] button').first.evaluate('(button)=>button.remove()')
        self.equivalent();self.assertFalse(self.reader.verified)
        self.page.set_content(pair(0),wait_until='load')
        self.reader.read();self.assertTrue(self.reader.verified)
    def test_operation_reset_rechecks_parity_and_writes_private_timing_evidence(self):
        with tempfile.TemporaryDirectory()as root:
            self.reader.archive_root=Path(root);self.reader.read()
            proof=json.loads((Path(root)/'native-turn-batch-latest.json').read_text())
            self.assertTrue(proof['parity_verified']);self.assertEqual(proof['row_count'],24)
            self.assertEqual((Path(root)/'native-turn-batch-latest.json').stat().st_mode&0o777,0o600)
            self.reader.begin({'operation':'owned:ONDOUSDT','request_sha256':'b'*64});self.assertFalse(self.reader.verified)
            self.reader.read();self.assertEqual(self.reader.native_reads,1)
    def test_legacy_contract_uses_the_original_reader(self):
        self.bridge._dom_contract=object()
        self.assertEqual(self.reader.read(),self.bridge._turns());self.assertFalse(self.reader.verified)
    def test_batch_preserves_daily_prefix_and_csv_delivery_checks(self):
        from agent.native_daily_room import DailyRoom, DailyRoomUnavailable, identities
        from agent.native_file_upload import NativeFileTurnReader, FILE_WITNESS
        with tempfile.TemporaryDirectory()as root:
            prefix=self.bridge._turns()[:22]
            self.bridge._turns=self.reader.read
            room=DailyRoom(self.bridge,native,root);room.offset=22;room.prefix=identities(prefix)
            files=('frame-15m.png','frame-1h.png','ENAUSDT-15m-200.csv','ENAUSDT-1h-200.csv')
            self.page.locator('[data-message-id=user-11]').evaluate('(turn)=>turn.insertAdjacentHTML("beforeend",\'<div style="width:300px"><span>ENAUSDT-15m-200.csv</span></div><div style="width:300px"><span>ENAUSDT-1h-200.csv</span></div>\')')
            reader=NativeFileTurnReader(self.bridge,native)
            def witness(turn,names,*,user=False):
                rows=turn.evaluate(FILE_WITNESS,{'bodySelector':native.USER_BODY,'names':list(names)})
                return len(rows)==len(names)and all(row['ready']and row['matches']==1 for row in rows)
            reader.turn_files[files]=(files[:2],files[2:],witness)
            self.bridge.uploaded_names=list(files);self.bridge.clock=time.monotonic;self.bridge.timeout=30
            self.bridge._until=lambda seconds:time.monotonic()+seconds
            self.bridge._turns=reader.read
            rows=reader.read();self.assertEqual(len(rows),2);self.assertEqual(rows[0]['attachments'],list(files))
            self.assertTrue(rows[1]['complete']);self.assertTrue(self.reader.verified)
            self.page.locator('[data-message-id=user-11] span').last.evaluate('(label)=>label.textContent="wrong.csv"')
            self.assertEqual(reader.read()[0]['attachments'],['UNCONFIRMED'])
            self.page.locator('[data-message-id=user-0] .whitespace-pre-wrap').evaluate('(user)=>user.textContent="CHANGED PREFIX"')
            with self.assertRaises(DailyRoomUnavailable):reader.read()


class FailureEvidence(unittest.TestCase):
    def test_evidence_writer_failure_does_not_change_the_hook_exception(self):
        from types import SimpleNamespace
        from tests.test_native_browser_hook import Bridge, client, request, OutcomeUnknown
        from agent.native_browser_hook import DelegatedDOMBridge
        bridge=Bridge();original=OutcomeUnknown('ORIGINAL_OUTCOME');bridge.ask=lambda *args,**kwargs: (_ for _ in ()).throw(original)
        module=SimpleNamespace(image_artifacts=lambda *args:None,exact_json=lambda *args:None,OutcomeUnknown=OutcomeUnknown)
        hook=DelegatedDOMBridge(bridge,client('COMPLETE'),native=module,converter=lambda value,**kwargs:value,monotonic=bridge.clock)
        with patch('agent.native_turn_batch.retain_return_failure',side_effect=OSError('disk full')):
            with self.assertRaises(OutcomeUnknown)as raised:hook.ask(request(),lambda:True,deadline=80)
        self.assertIs(raised.exception,original)
    def test_first_failure_retains_coded_context_without_reply_or_secret(self):
        with tempfile.TemporaryDirectory()as root:
            try:
                try:raise ValueError('NATIVE_MARKET_CONTEXT_UNVERIFIED')
                except ValueError:raise RuntimeError('token=never-export-this')from None
            except RuntimeError as error:
                retain_return_failure(root,{'operation':'owned:ENA','request_sha256':'a'*64},
                    {'phase':'ACCEPT','accepted_reply_sha256':'b'*64,'followup_intents':[{'sequence':1}]},172,195,error)
            path=Path(root)/('native-return-failure-'+'a'*64+'.json');raw=path.read_text();proof=json.loads(raw)
            self.assertEqual([item['class']for item in proof['chain']],['RuntimeError','ValueError'])
            self.assertEqual(proof['chain'][1]['reason'],'NATIVE_MARKET_CONTEXT_UNVERIFIED')
            self.assertNotIn('never-export-this',raw);self.assertEqual(path.stat().st_mode&0o777,0o600)
            first=path.read_bytes()
            retain_return_failure(root,{'operation':'owned:ENA','request_sha256':'a'*64},{'phase':'OTHER'},173,195,RuntimeError('OTHER'))
            self.assertEqual(path.read_bytes(),first)
