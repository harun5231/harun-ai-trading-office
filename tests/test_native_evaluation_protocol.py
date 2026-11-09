"""Offline historical phase admission; fixtures do not prove live execution."""
import copy
from datetime import datetime
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from agent import native_evaluation_protocol as protocol
from agent import dialogue_service, llm
from agent.native_pending_protocol import dialogue_phase_for_payload
from agent.neurobro_reply import parse_reply_for_payload
from tests.test_trade_evaluation_evidence import fixtures, NOW
from worker import trade_evaluation as journal
from worker import trade_evaluation_evidence as evidence
from worker import trade_evaluation_runtime as runtime
from worker import neuroapi


ROOM='a'*32
TEXT='PENILAIAN SETUP: BELUM CUKUP BUKTI\nData membuktikan hasil rugi; penyebabnya belum pasti.'


def prepared(directory):
    f=fixtures()
    binding=evidence.bind_closed_trade(f.coordinator,f.position,history=f.history,
        archive_reader=lambda *_:copy.deepcopy(f.archive),source_reader=f.source)
    journal.observe_calendar(directory,{'positions':[f.position]})
    journal.attach_evidence(directory,binding)
    claim=journal.claim_evaluation(directory,'post-trade:'+f.position['id'],ROOM,now=NOW+100)
    return f,claim,protocol.native_arguments(claim)


class EvaluationProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.fixture,self.claim,self.args=prepared(self.temp.name)
        self.addCleanup(self.fixture.coordinator.db.close)

    def test_phase_and_natural_parser_are_separate_from_trading(self):
        self.assertEqual(dialogue_phase_for_payload(self.args,['assessment','findings']),protocol.PHASE)
        value=parse_reply_for_payload(TEXT,self.args)
        self.assertEqual(value['assessment'],'INCONCLUSIVE')
        self.assertNotIn('side',value)
        for change in (lambda a:a.update(operation='2026-11-01:robot-v9:0:analysis-v9:BTCUSDT'),
                       lambda a:a['context']['historical_evaluation']['request'].update(trading_authority=True),
                       lambda a:a['context']['historical_evaluation'].update(request_sha256='f'*64)):
            args=copy.deepcopy(self.args);change(args)
            with self.assertRaises(ValueError):protocol.envelope(args)

    def test_exact_schema_rejects_setup_and_order_authority(self):
        bad=copy.deepcopy(evidence.EVALUATION_SCHEMA);bad['properties']['side']={'type':'string'}
        self.assertFalse(protocol.is_evaluation_schema(bad))
        # Install this fixture's own exception contract independently of any
        # native provider adapter another test may already have installed.
        patcher=patch.object(neuroapi,'canonical_output',inspect.unwrap(neuroapi.canonical_output));patcher.start();self.addCleanup(patcher.stop)
        namespace={'__name__':'worker.research_provider','Review':ValueError,
            'validate_chart_evidence':Mock(return_value='legacy')}
        protocol.install_source_protocol(namespace)
        self.assertEqual(json.loads(neuroapi.canonical_output(evidence.parse_evaluation_reply(TEXT),evidence.EVALUATION_SCHEMA)),evidence.parse_evaluation_reply(TEXT))
        with self.assertRaises(ValueError):neuroapi.canonical_output({'side':'LONG'},evidence.EVALUATION_SCHEMA)
        self.assertEqual(namespace['validate_chart_evidence']([],evidence.EVALUATION_SCHEMA,self.args['context'],NOW+100,NOW+200,180),[])
        with self.assertRaises(ValueError):namespace['validate_chart_evidence']([{'fresh':True}],evidence.EVALUATION_SCHEMA,self.args['context'],NOW+100,NOW+200,180)
        self.assertEqual(namespace['validate_chart_evidence']([],{'legacy':True},{},1,2,180),'legacy')

    def test_dom_phase_requires_no_new_upload_and_human_prompt_has_no_schema(self):
        original=Mock(return_value='legacy')
        namespace={'request_kind':original,'NotSubmitted':ValueError,'user_prompt':lambda _:'legacy prompt'}
        protocol.install_dom_protocol(namespace)
        args={**self.args,'marker':'HARUN-eval-ref'}
        self.assertEqual(namespace['request_kind'](args,[]),protocol.PHASE)
        with self.assertRaises(ValueError):namespace['request_kind'](args,[('new.png',b'x',{})])
        prompt=namespace['user_prompt'](args)
        self.assertIn('evaluasi historis',prompt);self.assertNotIn('OUTPUT_JSON_SCHEMA',prompt)
        self.assertEqual(prompt.count(args['marker']),1)
        self.assertEqual(namespace['user_prompt']({'output_schema':{}}),'legacy prompt')

    def test_once_claim_cannot_be_replayed_or_dispatched_from_changed_history(self):
        claim=journal.read_evaluation_claim(self.temp.name,self.claim['operation'])
        self.assertEqual(claim,self.claim)
        with self.assertRaisesRegex(ValueError,'JOB_NOT_READY'):
            journal.claim_evaluation(self.temp.name,self.claim['job_id'],ROOM,now=NOW+101)
        revised=copy.deepcopy(self.fixture.position);revised['pnl_usdt']='-3'
        journal.observe_calendar(self.temp.name,{'positions':[revised]})
        with self.assertRaisesRegex(ValueError,'HISTORY_REVISION_CHANGED'):
            journal.read_evaluation_claim(self.temp.name,self.claim['operation'])

    def test_evaluator_dialogue_uses_same_service_and_cannot_emit_hold_or_entry(self):
        at=NOW+100
        request=dict(schema_version=1,request_id='b'*64,conversation_id='neurobro:daily:'+ROOM,
            phase=protocol.PHASE,reply=TEXT,reply_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),
            market_context=None,gate={'robot_on':True,'native_dispatch_verified':True},deadline_epoch=at+45)
        self.assertEqual(dialogue_service.validate_request(request,now=at),request)
        self.assertEqual(llm.dialogue_schema(protocol.PHASE,TEXT)['properties']['action']['enum'],['CLARIFY','COMPLETE'])
        dialogue_service.validate_decision({'action':'COMPLETE','symbols':[],'question':''},request)
        for action in ('HOLD','TAHAN','BATALKAN','SCREENING'):
            with self.assertRaises(dialogue_service.DialogueError):
                dialogue_service.validate_decision({'action':action,'symbols':[],'question':''},request)
        bad={**request,'market_context':{'symbol':'BTCUSDT','fetched_at':at}}
        with self.assertRaises(dialogue_service.DialogueError):dialogue_service.validate_request(bad,now=at)

    def test_consumer_unknown_recovers_receipt_only_never_calls_provider(self):
        neuro=SimpleNamespace(require_key=Mock(),ask=Mock())
        coordinator=SimpleNamespace(neuro=neuro,allowed=lambda _:True)
        consumer=runtime.EvaluationConsumer(self.temp.name,archive_reader=Mock(),room_reader=Mock(),
            native_receipt_reader=Mock(side_effect=ValueError('NOT_RECEIVED')))
        result=consumer.advance(coordinator,'2026-11-01')
        self.assertEqual(result['state'],'EVALUATION_OUTCOME_UNKNOWN')
        neuro.ask.assert_not_called();neuro.require_key.assert_not_called();consumer.room_reader.assert_not_called()
        self.assertEqual(journal.evaluation_operation(self.temp.name,self.claim['job_id']),self.claim['operation'])

    def test_missing_transport_never_blocks_trading_result_or_fabricates_evaluation(self):
        coordinator=SimpleNamespace(db=SimpleNamespace(in_transaction=False),unresolved_research=lambda:False)
        idle={'bot_status':'WAITING','wait_reason':'ROBOT_CAPACITY_FULL'}
        runtime.after_trading_tick(coordinator,'2026-11-01',idle)
        self.assertEqual(coordinator.evaluation_status['reason'],'EVALUATION_NATIVE_ARCHIVE_TRANSPORT_NOT_BOUND')
        coordinator.evaluation_consumer=SimpleNamespace(advance=Mock(side_effect=RuntimeError('failure')))
        runtime.after_trading_tick(coordinator,'2026-11-01',idle)
        self.assertEqual(coordinator.evaluation_status['state'],'EVALUATION_UNAVAILABLE')

if __name__=='__main__':unittest.main()


class NativeEvaluationDispatchTests(unittest.TestCase):
    def test_pre_send_requires_actual_wire_hash_and_current_once_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture,claim,args=prepared(directory)
            self.addCleanup(fixture.coordinator.db.close)
            db=sqlite3.connect(str(Path(directory)/'ledger.sqlite3'),isolation_level=None);db.row_factory=sqlite3.Row
            self.addCleanup(db.close)
            db.executescript('CREATE TABLE api_requests(operation TEXT,state TEXT,attempts INT,output TEXT,idempotency TEXT,created REAL,body_hash TEXT);'
                'CREATE TABLE research_browser_requests(operation TEXT,provider TEXT,contract_version TEXT,marker TEXT,request_body TEXT,request_sha256 TEXT);'
                'CREATE TABLE research_browser_responses(operation TEXT);')
            def request_body(operation,prompt,schema,context):
                body=dict(operation=operation,marker='native-ref',provider='NEUROBRO_WEB',contract_version='neurobro-browser-decision-v2',prompt=prompt,output_schema=schema,context=context)
                raw=json.dumps(body,sort_keys=True,separators=(',',':'))
                return body,raw,hashlib.sha256(raw.encode()).hexdigest()
            _,raw,wire=request_body(args['operation'],args['prompt'],args['output_schema'],args['context'])
            db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?)',(args['operation'],'PENDING',1,None,None,NOW+101,wire))
            db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',(args['operation'],'NEUROBRO_WEB','neurobro-browser-decision-v2','native-ref',raw,wire))
            at=NOW+102;today=datetime.fromtimestamp(at,ZoneInfo('Asia/Jakarta')).date().isoformat()
            source=SimpleNamespace(request_body=request_body,marker=lambda _:'native-ref')
            with patch.dict(sys.modules,{'worker.research_provider':source}):
                self.assertTrue(protocol.verify_evaluation_dispatch(db,args['operation'],'native-ref',wire,today,at))
                self.assertFalse(protocol.verify_evaluation_dispatch(db,args['operation'],'native-ref',claim['request_sha256'],today,at))
                db.execute("UPDATE api_requests SET state='NEEDS_REVIEW'")
                self.assertFalse(protocol.verify_evaluation_dispatch(db,args['operation'],'native-ref',wire,today,at))

    def test_idle_consumer_is_after_trading_and_unknown_cannot_hold_trading(self):
        consumer=SimpleNamespace(advance=Mock(),status={'state':'EVALUATED'})
        coordinator=SimpleNamespace(evaluation_consumer=consumer,db=SimpleNamespace(in_transaction=False),
            unresolved_research=lambda:False)
        for result in ({'bot_status':'SCREENING'},{'bot_status':'ANALYZING'},
                       {'bot_status':'WAITING','wait_reason':'PENDING_REVIEW'}):
            runtime.after_trading_tick(coordinator,'2026-11-01',result)
        consumer.advance.assert_not_called()
        coordinator.db.in_transaction=True
        runtime.after_trading_tick(coordinator,'2026-11-01',{'bot_status':'WAITING','wait_reason':'ROBOT_CAPACITY_FULL'})
        consumer.advance.assert_not_called()
        coordinator.db.in_transaction=False
        runtime.after_trading_tick(coordinator,'2026-11-01',{'bot_status':'WAITING','wait_reason':'ROBOT_CAPACITY_FULL'})
        consumer.advance.assert_called_once()

    def test_consumer_recovers_original_claim_and_completion_without_new_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            f=fixtures();self.addCleanup(f.coordinator.db.close)
            binding=evidence.bind_closed_trade(f.coordinator,f.position,history=f.history,
                archive_reader=lambda *_:copy.deepcopy(f.archive),source_reader=f.source)
            journal.observe_calendar(directory,{'positions':[f.position]});journal.attach_evidence(directory,binding)
            claim=journal.claim_evaluation(directory,'post-trade:'+f.position['id'],ROOM)
            def receipt(operation):
                return dict(protocol=evidence.PROTOCOL,phase=protocol.PHASE,provider='NEUROBRO_WEB',operation=operation,
                    request_sha256=claim['request_sha256'],input_evidence_sha256=claim['input_evidence_sha256'],room_id=ROOM,
                    sent_this_request=True,received_at=claim['reserved_at']+1,response_id='native-actual-fixture',
                    source_evidence_sha256='f'*64,assistant_text=TEXT,assistant_sha256=hashlib.sha256(TEXT.encode()).hexdigest())
            room=Mock(side_effect=AssertionError('recovery must not reserve a new room'))
            consumer=runtime.EvaluationConsumer(directory,archive_reader=Mock(),room_reader=room,native_receipt_reader=receipt)
            neuro=SimpleNamespace(require_key=Mock(),ask=Mock())
            before=f.coordinator.db.serialize()
            result=consumer.advance(SimpleNamespace(neuro=neuro,allowed=lambda _:True),'2026-11-01')
            self.assertEqual(result['state'],'EVALUATED');self.assertTrue(result['recovered'])
            neuro.ask.assert_not_called();neuro.require_key.assert_not_called();room.assert_not_called()
            self.assertEqual(before,f.coordinator.db.serialize())
            self.assertEqual(journal.read_status(directory)['jobs'][0]['status'],'EVALUATED_INCONCLUSIVE')


class ExistingBridgeHistoricalPhaseTests(unittest.TestCase):
    def test_existing_bridge_uses_same_mini_service_without_market_refresh_or_new_upload(self):
        from contextlib import nullcontext
        from tests.test_native_browser_hook import Bridge,native,client
        from agent.native_browser_hook import DelegatedDOMBridge
        with tempfile.TemporaryDirectory() as directory:
            f,claim,args=prepared(directory);self.addCleanup(f.coordinator.db.close)
            args.update(marker='owned-evaluation-marker',request_sha256='c'*64)
            bridge=Bridge(initial=TEXT)
            api=native();api.user_prompt=lambda payload:payload['prompt']
            room=SimpleNamespace(operation=lambda _:nullcontext(),conversation_id='neurobro:2026-11-01:'+ROOM,
                full_turns=lambda:[])
            mini=client('COMPLETE');converter=Mock(side_effect=AssertionError('fresh market conversion'))
            fresh=Mock(side_effect=AssertionError('fresh chart capture'))
            receipts=Mock(return_value=True)
            actual=DelegatedDOMBridge(bridge,mini,native=api,converter=converter,
                wall_clock=lambda:NOW+101,monotonic=bridge.clock,daily_room=room,fresh_charts=fresh,followup_receipt=receipts)
            with patch('agent.native_evaluation_browser.HistoricalEvaluation') as historical, patch('agent.native_evaluation_browser.HistoricalFileDelivery') as delivery:
                delivery.return_value.install.return_value=None
                historical.return_value.prepare_room.return_value=([],[])
                historical.return_value.next_material_question.return_value=None
                historical.return_value.complete.return_value=[]
                seen=[];events=[]
                historical.return_value.note_reply.side_effect=lambda *_args,**_kwargs:events.append('actual-reply-receipt')
                run=mini.request.side_effect
                def reviewed(value):
                    events.append('model-review');return run(value)
                mini.request.side_effect=reviewed
                historical.return_value.before_send.side_effect=lambda *_args,**_kwargs:seen.append(bridge.clicks)
                result=actual.ask(args,lambda:True,deadline=70)
                self.assertEqual(seen,[0]);historical.return_value.complete.assert_called_once()
                self.assertEqual(events,['actual-reply-receipt','model-review'])
            self.assertEqual(result['output']['assessment'],'INCONCLUSIVE')
            self.assertEqual(bridge.clicks,1);fresh.assert_not_called();converter.assert_not_called()
            request=mini.request.call_args.args[0]
            self.assertEqual(request['phase'],protocol.PHASE);self.assertIsNone(request['market_context'])
            self.assertEqual(request['conversation_id'],room.conversation_id)

    def test_historical_material_not_in_actual_room_blocks_before_intent_or_send(self):
        from agent.native_evaluation_browser import HistoricalEvaluation
        with tempfile.TemporaryDirectory() as directory:
            f,claim,args=prepared(directory);self.addCleanup(f.coordinator.db.close)
            args.update(marker='owned-evaluation-marker',request_sha256='c'*64)
            db=sqlite3.connect(':memory:');self.addCleanup(db.close)
            db.execute('CREATE TABLE daily_rooms(day TEXT,room_id TEXT,state TEXT)')
            db.execute('INSERT INTO daily_rooms VALUES(?,?,?)',('2026-11-01',ROOM,'BOUND'))
            room=SimpleNamespace(_connection=db,day='2026-11-01',room_id=ROOM,_prepared=True,
                store=SimpleNamespace(root=directory),full_turns=lambda:[],original_visible=lambda _:[],verify_prefix=Mock())
            owner=SimpleNamespace(daily_room=room,followup_receipt=Mock(return_value=True),
                bridge=SimpleNamespace(uploaded_names=[],page=SimpleNamespace(url='https://fixture.invalid/owned'),_fresh_conversation=Mock()),
                native=SimpleNamespace(MESSAGE='message',USER_BODY='user-body',image_artifacts=Mock(),user_prompt=Mock()))
            historical=HistoricalEvaluation(owner,args,100)
            witness=dict(intent=dict(followup_attachment_names=['one.png','two.png','one.csv','two.csv'],
                followup_prompt_sha256='e'*64))
            with patch('worker.trade_evaluation_archive.read_bound_material_turn',return_value=witness):
                with self.assertRaisesRegex(ValueError,'HISTORICAL_TURN_NOT_READABLE_IN_ROOM'):
                    historical._turn_witness(0)
            owner.followup_receipt.assert_not_called()
            db.execute("UPDATE daily_rooms SET room_id=?",('f'*32,))
            with self.assertRaisesRegex(ValueError,'BOUND_ROOM_NOT_AVAILABLE'):HistoricalEvaluation(owner,args,100)
