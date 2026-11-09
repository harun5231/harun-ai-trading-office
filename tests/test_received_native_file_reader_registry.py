"""Exercise resumed turns through the REAL file delivery and native reader."""
import copy
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent.native_browser_hook import DelegatedDOMBridge
from agent.native_file_upload import NativeFileTurnReader, FILE_WITNESS
from agent import native_received_continuation as continuation
from tests import test_native_file_upload as fixtures
from tests.test_agent_closed_candles_200 import NOW, native_context_200
from tests.test_native_browser_hook import OutcomeUnknown


class RefreshReached(Exception): pass


class Image:
    def __init__(self,row,name): self.row,self.name=row,name
    def is_visible(self): return True
    def get_attribute(self,name):
        assert name=='alt';return self.name
    def evaluate(self,script):
        return dict(complete=True,width=1314 if self.row['decoded'] else 0,
                    height=738 if self.row['decoded'] else 0)


class Node:
    def __init__(self,row):self.row=row
    def locator(self,selector):
        assert selector=='img'
        return fixtures.Items([Image(self.row,name)for name in self.row.get('files',[])[:2]])
    def evaluate(self,script,args):
        assert script==FILE_WITNESS
        return [dict(name=name,ready=name in self.row.get('files',[]) and self.row['csv_ready'],
                     matches=int(name in self.row.get('files',[])))for name in args['names']]


class ActualFourFileBridge(fixtures.Fixture):
    def native_turns(self):
        rows=copy.deepcopy(self.rows)
        for row in rows:
            files=row.pop('files',[]);decoded=row.pop('decoded',True);row.pop('csv_ready',None)
            if row['role']=='user':
                # Exact native image contract: four expected names fail against
                # two actual PNGs. The file reader must deliberately split them.
                row['attachments']=list(self.uploaded_names)if self.uploaded_names==files[:2] and decoded else['UNCONFIRMED']
        return rows
    def _visible(self,selector):
        if selector=='messages':return [Node(row)for row in self.rows]
        raise AssertionError('No composer or Send access expected: '+selector)


class ReceivedNativeFileReaderTests(unittest.TestCase):
    def harness(self,*,followup=False,wrong_csv=False,bad_decode=False):
        raw=ActualFourFileBridge();raw.current=.5
        raw.url='https://app.neurobro.ai/';raw.operation_deadline=2;raw._operation_contract=raw._dom_contract
        raw.ask=Mock(side_effect=AssertionError('Never repeat initial Send'))
        def formatted(payload):return payload['prompt']
        formatted.__harun_data_files_v1__=True
        native=SimpleNamespace(NotSubmitted=fixtures.NotSubmitted,OutcomeUnknown=OutcomeUnknown,
            MESSAGE='messages',USER_BODY='native-user-body',MAX_ANSWER=65536,user_prompt=formatted,
            image_artifacts=Mock(return_value=[]),exact_json=Mock(return_value=None))
        payload=dict(operation='2026-11-05:robot-v9:0:analysis-v9:DOGEUSDT',marker='HARUN_fixture',
            request_sha256='a'*64,prompt='Analisis yang sudah terkirim.',context=native_context_200(),
            output_schema=dict(required=['symbol','side','limit_entry','take_profit','stop_loss','risk_reward']))
        names=[['first-15m.png','first-1h.png','first-15m.csv','first-1h.csv']]
        if followup:names.append(['last-15m.png','last-1h.png','last-15m.csv','last-1h.csv'])
        raw.rows=[]
        for index,files in enumerate(names):
            raw.rows.extend([dict(role='user',text=payload['prompt']if index==0 else'Klarifikasi yang sudah dikirim.',
                files=files,decoded=not bad_decode,csv_ready=not wrong_csv),
                dict(role='assistant',text='Lampiran belum dapat dipahami.',id='reply-'+str(index),complete=True)])
        raw.uploaded_names=names[-1]
        owned=raw.native_turns()
        for index,files in enumerate(names):owned[2*index]['attachments']=list(files)
        reasoner=Mock(request=Mock(side_effect=AssertionError('No replayed Mini')))
        refresh=Mock(side_effect=RefreshReached)
        hook=DelegatedDOMBridge(raw,reasoner,native=native,monotonic=raw.clock,wall_clock=lambda:NOW+10,
            fresh_charts=refresh,followup_receipt=Mock())
        seed=dict(rows=owned,turn_names=names,prompt=payload['prompt'],followup_intents=[],
            bound_charts=[],completed_intents=[],before_upload=Mock(),before_send=Mock())
        return raw,hook,payload,seed,refresh,reasoner

    def actor_boundary(self,raw,hook,payload,seed):
        """Inject an already validated grant; exercise the actor-to-real-reader boundary.

        ReceivedProofTests independently cover source/grant/room acceptance.
        No file-delivery, native answer, or delegated polling method is mocked.
        """
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        root=Path(directory.name)
        capture=dict(error_count=0,stable_snapshot_reads=4,receipt_files={})
        path=root/'observed.json';path.write_text(json.dumps(capture));path.chmod(0o600)
        record=dict(day='2026-11-05',room_id='a'*32)
        db=Mock();db.execute.return_value.fetchone.return_value=(record['room_id'],'BOUND','[]','{}')
        room=SimpleNamespace(store=SimpleNamespace(root=root,path=root/'room.sqlite'),_connection=db,
            full_turns=lambda:copy.deepcopy(seed['rows']),operation=lambda _:nullcontext())
        seed.update(record=record,full_rows=copy.deepcopy(seed['rows']),offset=0,initial_chart_evidence=[])
        hook.daily_room=room
        actor=SimpleNamespace(thread=threading.current_thread(),claim=threading.Lock(),
            stopping=threading.Event(),_mode=lambda:'AUTOMATION',sessions=SimpleNamespace(active=lambda:False),
            dispatch_check=lambda _:True)
        grant=dict(grant_id='b'*64,expires_at=3,
            **{key:payload[key]for key in('operation','request_sha256')})
        observed=dict(capture_name=path.name,error_count=0,status='CONNECTED')
        def run():
            with actor.claim,patch.object(continuation,'validate',return_value=(payload,grant)),\
                    patch.object(continuation,'_read_source',return_value=capture),\
                    patch.object(continuation.observer,'observe',return_value=observed),\
                    patch.object(continuation,'_semantic',return_value='same-validated-source'),\
                    patch.object(continuation,'_seed',return_value=seed),\
                    patch.object(continuation.time,'time',return_value=1):
                return continuation.continue_received(actor,hook,dict(action=continuation.ACTION))
        return run,root

    def test_actor_restores_empty_or_foreign_hint_after_real_initial_reader_reaches_refresh(self):
        for hint in ([],['other-1h.png','other-15m.png']):
            with self.subTest(hint=hint):
                raw,hook,payload,seed,refresh,reasoner=self.harness()
                raw.uploaded_names=hint;before=copy.deepcopy(raw.rows)
                deadline,contract=raw.operation_deadline,raw._operation_contract
                run,root=self.actor_boundary(raw,hook,payload,seed)
                result=run()
                self.assertEqual((result['kind'],result['exception_class']),
                                 (continuation.NOT_SENT,'RefreshReached'))
                self.assertIs(raw.uploaded_names,hint)
                self.assertEqual(raw.operation_deadline,deadline);self.assertIs(raw._operation_contract,contract)
                self.assertEqual(raw.rows,before);self.assertLess(raw.current,1)
                self.assertIsInstance(raw._harun_file_turn_reader,NativeFileTurnReader)
                refresh.assert_called_once();reasoner.request.assert_not_called();raw.ask.assert_not_called()
                self.assertEqual(raw.actions,[])
                self.assertFalse(list(root.glob('*.upload-started.json')))
                self.assertFalse(list(root.glob('*.send-attempted.json')))
                # Positive pre-upload NOT_SENT permits a new admission without
                # repeating the already received initial prompt or model call.
                second=run()
                self.assertEqual(second['exception_class'],'RefreshReached')
                self.assertIn('.2.admitted.json',second['admission']['name'])
                self.assertEqual(refresh.call_count,2)
                self.assertIs(raw.uploaded_names,hint);reasoner.request.assert_not_called();raw.ask.assert_not_called()

    def test_actor_hint_does_not_bypass_missing_csv_or_undecoded_image(self):
        for failure in ('csv','decode'):
            with self.subTest(failure=failure):
                raw,hook,payload,seed,refresh,reasoner=self.harness(
                    wrong_csv=failure=='csv',bad_decode=failure=='decode')
                hint=[];raw.uploaded_names=hint
                deadline,contract=raw.operation_deadline,raw._operation_contract
                run,root=self.actor_boundary(raw,hook,payload,seed)
                result=run()
                self.assertEqual((result['kind'],result['exception_class']),
                                 (continuation.NOT_SENT,'OutcomeUnknown'))
                refresh.assert_not_called();reasoner.request.assert_not_called();raw.ask.assert_not_called()
                self.assertIs(raw.uploaded_names,hint)
                self.assertEqual(raw.operation_deadline,deadline);self.assertIs(raw._operation_contract,contract)
                self.assertEqual(raw.actions,[]);self.assertFalse(list(root.glob('*.send-attempted.json')))

    def test_real_delivery_registers_received_initial_and_followup_without_bypassing_native_witnesses(self):
        for followup in (False,True):
            with self.subTest(followup=followup):
                raw,hook,payload,seed,refresh,reasoner=self.harness(followup=followup)
                with self.assertRaises(RefreshReached):hook._ask(payload,lambda:True,deadline=2,_received=seed)
                self.assertIsInstance(raw._harun_file_turn_reader,NativeFileTurnReader)
                registry=raw._harun_file_turn_reader.turn_files
                self.assertEqual(set(registry),{tuple(names)for names in seed['turn_names']})
                for names in seed['turn_names']:
                    self.assertEqual(registry[tuple(names)][:2],(tuple(names[:2]),tuple(names[2:])))
                refresh.assert_called_once();reasoner.request.assert_not_called();raw.ask.assert_not_called()
                self.assertEqual(raw.actions,[]);seed['before_upload'].assert_not_called();seed['before_send'].assert_not_called()
                self.assertLess(raw.current,1)

    def test_registered_names_do_not_accept_missing_csv_or_undecoded_native_png(self):
        for failure in ('csv','decode'):
            with self.subTest(failure=failure):
                raw,hook,payload,seed,refresh,reasoner=self.harness(followup=True,
                    wrong_csv=failure=='csv',bad_decode=failure=='decode')
                with self.assertRaises(OutcomeUnknown):hook._ask(payload,lambda:True,deadline=1,_received=seed)
                refresh.assert_not_called();reasoner.request.assert_not_called();raw.ask.assert_not_called()
                self.assertEqual(raw.actions,[]);seed['before_upload'].assert_not_called();seed['before_send'].assert_not_called()


if __name__=='__main__':unittest.main()
