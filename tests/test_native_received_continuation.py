"""The existing native dialogue resumes a received turn, never its initial Send."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent import native_received_continuation as continuation
from agent import native_received_analysis as observer
from tests import test_native_browser_hook as hook_fixtures
from tests import test_generic_received_analysis as observer_fixtures


class ReceivedHookTests(unittest.TestCase):
    def harness(self, *, prior_followup=False):
        case=hook_fixtures.NativeFreshFollowupTests('test_fresh_400_data_and_two_attachments_are_sent_once_with_native_final_identity')
        self.addCleanup(case.doCleanups);case.harness()
        case.bridge.ask=Mock(side_effect=AssertionError('Initial Send must not run again'))
        case.bridge._fresh_conversation=Mock(side_effect=AssertionError('No new room'))
        first=dict(role='native_user_role',text=case.value['prompt'],attachments=list(case.bridge.uploaded_names))
        answer=dict(role='native_assistant_role',text='Lampiran 1H belum terbaca. Mohon perbaiki.',complete=True,id='old-first')
        rows=[first,answer];intents=[]
        if prior_followup:
            rows.extend([dict(role='native_user_role',text='Klarifikasi yang SUDAH dikirim.',attachments=list(case.bridge.uploaded_names)),
                dict(role='native_assistant_role',text='CSV belum saya pahami; periksa waktu UTC.',complete=True,id='old-last')])
            intents=[dict(followup_sequence=1,operation=case.value['operation'],
                request_sha256=case.value['request_sha256'],marker=case.value['marker'])]
        case.bridge.messages=copy.deepcopy(rows);case.bridge.clicks=1
        case.bridge.operation_deadline=80;case.bridge._operation_contract=case.bridge._dom_contract
        case.bridge.final='COIN: BTCUSDT\nARAH: HOLD\nStruktur pasar belum mendukung entry sekarang.'
        def final_model(request):
            return dict(schema_version=1,ok=True,**{key:request[key]for key in
                ('request_id','conversation_id','phase','reply_sha256')},authority='DIALOGUE_CONTROL_ONLY',
                trading_authority=False,decision=dict(action='HOLD',symbols=['BTCUSDT'],question=''),
                needs_followup=False,reason='DIALOGUE_DECISION')
        case.reasoner.request.side_effect=final_model
        calls=[]
        seed=dict(rows=rows,turn_names=[list(case.bridge.uploaded_names)]*(len(rows)//2),prompt=case.value['prompt'],
            followup_intents=intents,bound_charts=case.initial,completed_intents=[],
            before_upload=lambda:calls.append(('UPLOAD',case.bridge.clicks)),
            before_send=lambda:calls.append(('SEND',case.bridge.clicks)))
        return case,seed,calls

    def run_received(self,case,seed):
        return case.hook._ask(case.value,case.gate,deadline=80,chart_evidence=case.initial,_received=seed)

    def test_received_technical_turn_uses_existing_fresh_pipeline_without_initial_send(self):
        for prior in (False,True):
            with self.subTest(prior_followup=prior):
                case,seed,calls=self.harness(prior_followup=prior);before=copy.deepcopy(case.bridge.messages)
                result=self.run_received(case,seed)
                self.assertEqual(case.bridge.messages[:len(before)],before)
                case.bridge.ask.assert_not_called();case.bridge._fresh_conversation.assert_not_called()
                self.assertEqual(case.bridge.clicks,2);self.assertEqual(len(case.bridge.uploads),1)
                self.assertEqual(calls,[('UPLOAD',1),('SEND',1)])
                self.assertEqual(case.reasoner.request.call_count,1) # New answer only, no old Mini replay.
                self.assertEqual(result['output']['side'],'HOLD')
                self.assertEqual(result['response_id'],'dom-'+hashlib.sha256((case.bridge.page.url+'\n'+
                    case.value['marker']+'\n'+case.bridge.final).encode()).hexdigest())
                intent=next(value for stage,_,value in case.events if stage=='INTENT')
                self.assertEqual(intent['followup_sequence'],2 if prior else 1)
                self.assertEqual(intent['closed_candles_total'],400)
                projected=case.hook.converter(case.bridge.uploads[0][0]['context'],now=case.now)
                self.assertEqual({tf:len(rows)for tf,rows in projected['candles'].items()},{'15m':200,'1h':200})
                self.assertEqual(set(projected['running_candles']),{'15m','1h'})
                self.assertIn('batas waktu candle tertutup UTC',case.bridge.messages[-2]['text'])

    def test_changed_received_prefix_streaming_or_owner_off_never_sends(self):
        for failure in ('prefix','streaming','off'):
            case,seed,calls=self.harness(prior_followup=True)
            if failure=='prefix':case.bridge.messages[0]['text']='Foreign conversation'
            elif failure=='streaming':seed['rows'][-1]['complete']=False
            else:case.enabled=False
            with self.subTest(failure=failure),self.assertRaises((hook_fixtures.OutcomeUnknown,ValueError,hook_fixtures.NotSubmitted)):
                self.run_received(case,seed)
            self.assertEqual(case.bridge.clicks,1);self.assertEqual(calls,[])
            case.bridge.ask.assert_not_called();case.reasoner.request.assert_not_called()

    def test_owner_off_during_durable_send_admission_is_rechecked_before_click(self):
        case,seed,calls=self.harness(prior_followup=True)
        def stop():
            calls.append(('SEND',case.bridge.clicks));case.enabled=False
        seed['before_send']=stop
        with self.assertRaises(hook_fixtures.OutcomeUnknown):self.run_received(case,seed)
        self.assertEqual(case.bridge.clicks,1)
        self.assertEqual(calls,[('UPLOAD',1),('SEND',1)])
        self.assertTrue(any(stage=='INTENT'for stage,_,_ in case.events))


class ReceivedProofTests(unittest.TestCase):
    def setUp(self):
        self.fixture=observer_fixtures.GenericReceivedAnalysisTests('test_doge_latest_reply_and_operation_scoped_proof_are_not_hardcoded_to_rune')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.payload=self.fixture.payload;self.root=self.fixture.case.root
        result=self.fixture.invoke();self.capture=json.loads((self.root/result['capture_name']).read_text())
        # Complete the controlled native INTENT fixture with its actual source
        # bindings. No production transcript or model approval is invented.
        values=self.capture['receipt_files'];name=next(iter(values));intent=values[name]['value']
        intent.update(initial_chart_evidence=[],followup_chart_evidence=[],
            previous_followup_intent_sha256=None,original_payload_sha256=observer.native_digest(self.payload),
            initial_prompt_sha256=observer.text_sha(self.capture['rows'][6]['text']),
            initial_reply_sha256=observer.text_sha(self.capture['rows'][7]['text']),
            conversation_url_sha256=observer.text_sha(self.capture['page_url']))
        raw=json.dumps(intent,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
        values[name].update(sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))
        (self.root/'dialogue-200-followup'/name).write_bytes(raw)
        saved=observer._save(self.root,self.capture)
        self.grant=dict(schema_version=1,operation=self.payload['operation'],request_sha256=self.payload['request_sha256'],
            marker=self.payload['marker'],source_capture_name=saved['capture_name'],source_capture_sha256=saved['capture_sha256'],
            source_capture_bytes=saved['capture_bytes'],source_observation_sha256=observer.digest(observer._project_operation(self.capture)),
            reply_sha256=observer.text_sha(self.capture['rows'][-1]['text']),
            latest_user_prompt_sha256=observer.text_sha(self.capture['rows'][-2]['text']),next_followup_sequence=2,
            started_at=self.capture['finished_at']+1,expires_at=self.capture['finished_at']+901)
        self.grant['grant_id']=observer.native_digest({key:self.grant[key]for key in
            ('operation','request_sha256','reply_sha256','latest_user_prompt_sha256','next_followup_sequence')})

    def test_private_capture_and_current_four_file_native_witness_seed_exact_existing_turns(self):
        request=dict(action=continuation.ACTION,payload=self.payload,grant=self.grant)
        continuation.validate(request,now=self.grant['started_at'])
        source=continuation._read_source(self.root,self.payload,self.grant)
        seed=continuation._seed(source,self.payload,self.grant)
        self.assertEqual(len(seed['rows']),4);self.assertEqual(seed['offset'],6)
        self.assertEqual(seed['rows'][-1]['id'],'final-DOGEUSDT')
        self.assertEqual(seed['turn_names'],[self.fixture.names,self.fixture.names])
        self.assertEqual(seed['rows'][0]['attachments'],self.fixture.names)
        changed=copy.deepcopy(source);changed['file_tiles'][-1]['images'][1]['decoded']['complete']=False
        with self.assertRaises(ValueError):continuation._seed(changed,self.payload,self.grant)
        changed=copy.deepcopy(source);changed['operation_scope']['unmatched_intents']=['uncertain.intent.json']
        with self.assertRaises(ValueError):continuation._seed(changed,self.payload,self.grant)

    def test_changed_payload_wrong_capture_path_or_grant_cannot_admit(self):
        request=dict(action=continuation.ACTION,payload=self.payload,grant=self.grant)
        for mutate in (lambda value:value['payload']['context'].update(symbol='BTCUSDT'),
                lambda value:value['grant'].update(source_capture_name='../private'),
                lambda value:value['grant'].update(next_followup_sequence=3),
                lambda value:value['grant'].update(started_at=1,expires_at=901)):
            value=copy.deepcopy(request);mutate(value)
            with self.assertRaises(ValueError):continuation.validate(value,now=self.grant['started_at'])
        changed=copy.deepcopy(self.grant);changed['source_capture_sha256']='f'*64
        with self.assertRaises((ValueError,FileNotFoundError)):continuation._read_source(self.root,self.payload,changed)

    def test_once_admission_survives_restart_and_only_positive_before_upload_not_sent_allows_retry(self):
        base,admission=continuation._admit(self.root,self.grant,self.grant['source_capture_name'])
        with self.assertRaisesRegex(ValueError,'ALREADY_ADMITTED'):
            continuation._admit(self.root,self.grant,self.grant['source_capture_name'])
        closure=dict(kind=continuation.NOT_SENT,grant_id=self.grant['grant_id'],admission=admission,
            send_attempted=False,new_intent_written=False,upload_started=False)
        continuation._write(self.root,base+'.closed-not-sent.json',closure)
        second,_=continuation._admit(self.root,self.grant,self.grant['source_capture_name'])
        self.assertTrue(second.endswith('.2'))
        with self.assertRaisesRegex(ValueError,'ALREADY_ADMITTED'):
            continuation._admit(self.root,self.grant,self.grant['source_capture_name'])

    def test_volatile_read_times_do_not_weaken_same_actual_identity_comparison(self):
        fresh=copy.deepcopy(self.capture);fresh['observed_at']+=20;fresh['finished_at']+=20
        self.assertEqual(continuation._semantic(fresh),continuation._semantic(self.capture))
        fresh['rows'][-1]['id']='different-assistant'
        self.assertNotEqual(continuation._semantic(fresh),continuation._semantic(self.capture))

    def invoke_failure(self, *, upload=False):
        c=self.fixture.case;c.actor.dispatch_check=lambda payload:True
        c.delegated.native_turn_batch=None
        def failure(payload,check,**kwargs):
            self.assertEqual(c.room.offset,6)
            self.assertEqual(c.room.conversation_id,'neurobro:2026-11-05:'+'a'*32)
            if upload:kwargs['_received']['before_upload']()
            raise RuntimeError('Actual local failure before Send')
        c.delegated._ask=Mock(side_effect=failure)
        value=dict(action=continuation.ACTION,payload=self.payload,grant=self.grant)
        with c.actor.claim,patch.object(continuation.time,'time',return_value=self.grant['started_at']):
            return continuation.continue_received(c.actor,c.delegated,value)

    def test_real_actor_room_produces_positive_unsent_only_before_upload_and_retries_same_grant(self):
        result=self.invoke_failure()
        self.assertEqual(result['kind'],continuation.NOT_SENT)
        self.assertFalse(result['upload_started']);self.assertFalse(result['send_attempted'])
        self.assertEqual(result['disposition']['value']['admission'],result['admission'])
        second=self.invoke_failure()
        self.assertIn('.2.admitted.json',second['admission']['name'])
        self.assertEqual(list(self.root.glob('*.send-attempted.json')),[])
        self.assertEqual(self.fixture.case.raw.messages[-1]['id'],'final-DOGEUSDT')

    def test_after_upload_failure_is_unknown_and_never_replayed(self):
        with self.assertRaises(RuntimeError):self.invoke_failure(upload=True)
        self.assertEqual(list(self.root.glob('*.closed-not-sent.json')),[])
        self.assertEqual(len(list(self.root.glob('*.upload-started.json'))),1)
        with self.assertRaisesRegex(ValueError,'ALREADY_ADMITTED'):self.invoke_failure()


if __name__=='__main__':unittest.main()
