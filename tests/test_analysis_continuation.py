"""Same-room followup admissions; no live sockets, accounts, models or orders."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from worker.analysis_continuation import (admit,active_grant,grant_identity,record_return,
    restore_received_checkpoint,verify_dispatch_grant,retry_not_sent)
from worker.analysis_response_recovery import (recover_received_analysis,resume_uncertain_analysis,
    validate_observation,sha,digest,wire)
from worker.core import Review,FEE_SOURCE
from agent.native_analysis_receipt import persist_review,read_reviews
from tests import test_analysis_response_recovery as recovery_fixture
from tests.test_analysis_response_recovery import NOW,DAY,OP,CYCLE,utc
from tests.test_agent_closed_candles_200 import native_context_200


class AnalysisContinuationTests(unittest.TestCase):
    def setUp(self):
        self.f=recovery_fixture.AnalysisResponseRecoveryTests('test_fresh_hold_resumes_existing_cached_operation_and_preserves_original_error')
        self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.db,self.provider,self.native,self.source=self.f.db,self.f.provider,self.f.native,self.f.source
        self.f.created=NOW-2000
        self.f.context=native_context_200(NOW-2001)
        self.f.context['contract_rules']=dict(symbol='BTCUSDT',source='Binance Futures /fapi/v1/exchangeInfo',observed_at=NOW-2001)
        self.f.context['risk_constraints']=dict(risk_model='DYNAMIC_NET_2R_V4',fee_symbol='BTCUSDT',fee_source=FEE_SOURCE,
            fee_observed_at=NOW-2001,maximum_loss_at_sl_usdt='5',target_loss_at_sl_usdt='5')
        from worker.core import DYNAMIC_RISK_MODEL
        self.f.context['risk_constraints']['risk_model']=DYNAMIC_RISK_MODEL
        from worker.neuroapi import SETUP_SCHEMA_V4
        self.f.body,serialized,self.f.request_hash=self.source.request_body(OP,'Prompt asli — analisis',SETUP_SCHEMA_V4,self.f.context)
        self.f.payload={**self.f.body,'request_sha256':self.f.request_hash}
        self.db.execute('UPDATE research_browser_requests SET request_sha256=?,marker=?,request_body=?',(self.f.request_hash,self.f.body['marker'],serialized))
        self.db.execute('UPDATE api_requests SET body_hash=?,created=?',(self.f.request_hash,self.f.created))
        self.f.data['analysis_contexts']={OP:dict(version='ANALYSIS_CONTEXT_FRESHNESS_V1',symbol='BTCUSDT',source='Binance Futures',
            fetched_at=NOW-2001,server_time=self.f.context['server_time'],claimed_at=NOW-2000.5,
            timeframes={tf:dict(symbol='BTCUSDT',timeframe=tf,last_close_time=frame['candles'][-1]['close_time'])
                for tf,frame in self.f.context['timeframes'].items()})}
        self.db.execute('UPDATE robot_cycles SET data=?',(json.dumps(self.f.data),))
        self.envelope=self.f.fixture(reply='Screenshot belum terbaca.\nCOIN: BTCUSDT\nARAH: HOLD',approval=False)

    def new_reply(self):
        envelope=copy.deepcopy(self.envelope);proof=envelope['observation'];rows=proof['rows']
        previous=proof['receipt_files']['native.intent.json']['value']
        context=native_context_200(NOW-5)
        charts=[dict(row,captured_at=utc(NOW-4),sha256=('e' if row['timeframe']=='15m' else 'f')*64,
            attachment_id='fresh-'+row['timeframe'])for row in previous['followup_chart_evidence']]
        names=[self.f.payload['marker']+'-'+row['timeframe']+'-'+row['sha256'][:12]+'.png' for row in charts]
        names+=['BTCUSDT_'+tf+'_200_closed_'+char*8+'.csv'for tf,char in (('15m','e'),('1h','f'))]
        prompt='Berikut chart terbaru sesuai timeframe dan CSV tertutup.\n'+self.f.payload['marker']
        reply='COIN: BTCUSDT\nARAH: HOLD'
        intent=copy.deepcopy(previous)
        intent.update(followup_sequence=2,previous_followup_intent_sha256=digest(previous),
            initial_reply_sha256=sha(rows[-1]['text']),initial_turns_sha256=digest(rows),
            followup_prompt_sha256=sha(prompt),followup_context_sha256=digest(context),
            followup_chart_evidence=charts,followup_attachment_names=names,observed_at=NOW-5,
            material_send_check=dict(symbol='BTCUSDT',running_candles_separate=True,
                closed_candles_per_timeframe=200,closed_candles_total=400,send_checked_at=NOW-2,
                data_observed_at=NOW-5,data_age_at_send_check_seconds=3,screenshots=[]))
        proof['receipt_files']['native.2.intent.json']=dict(value=intent,sha256=digest(intent),bytes=len(wire(intent,ascii=True)))
        rows.extend([dict(role='user',text=prompt,attachments=names),dict(role='assistant',text=reply,complete=True,id='new-final')])
        proof['file_tiles'].append(dict(row=4,prompt_sha256=sha(prompt),
            images=[dict(alt=name,visible=True,decoded=dict(complete=True,width=1314,height=738)) for name in names[:2]],
            file_tile_witnesses=[dict(names=names,witness=[dict(name=name,ready=True,matches=1)for name in names])]))
        proof['room_witness']['followup_user_rows']=[2,4]
        proof.update(observed_at=NOW+1,finished_at=NOW+2)
        request_id=digest(dict(operation=OP,request_sha256=self.f.request_hash,reply_sha256=sha(reply),phase='ANALYSIS',transcript_sha256=digest(rows)))
        request=dict(schema_version=1,request_id=request_id,conversation_id='neurobro:'+DAY+':'+'a'*32,phase='ANALYSIS',
            reply=reply,reply_sha256=sha(reply),market_context={'symbol':'BTCUSDT'},gate={'robot_on':True,'native_dispatch_verified':True},deadline_epoch=NOW+40)
        response=dict(schema_version=1,ok=True,request_id=request_id,conversation_id=request['conversation_id'],phase='ANALYSIS',reply_sha256=sha(reply),
            authority='DIALOGUE_CONTROL_ONLY',trading_authority=False,decision=dict(action='HOLD',symbols=['BTCUSDT'],question=''),needs_followup=False,reason='DIALOGUE_DECISION')
        persist_review(self.f.root.name,self.f.payload,request,response,rows,context=context,observed_at=NOW+1)
        proof['review_receipts']=read_reviews(self.f.root.name,self.f.payload)
        return self.f.seal(envelope)

    def test_original_clock_and_owner_risk_survive_explicit_followup_grant(self):
        original=self.f.row('api_requests');candidate=self.f.row('robot_candidates')
        grant=admit(self.provider,self.envelope,now=NOW)
        self.assertEqual(grant_identity(grant),grant['grant_id'])
        self.assertEqual(self.f.row('api_requests'),dict(original,state='PENDING'))
        self.assertEqual(self.f.row('robot_candidates'),candidate)
        self.assertEqual(self.db.execute('SELECT enabled,risk FROM robot_settings').fetchone()[:],(1,'5'))
        self.assertTrue(verify_dispatch_grant(self.db,OP,self.f.request_hash,DAY,NOW+1))
        self.assertTrue(self.native.verify_dispatch(self.db,OP,self.f.body['marker'],self.f.request_hash,DAY,NOW+1))
        self.assertFalse(self.native.verify_dispatch(self.db,OP,self.f.body['marker'],self.f.request_hash,DAY,NOW+901))
        self.assertFalse(verify_dispatch_grant(self.db,OP,'e'*64,DAY,NOW+1))
        self.assertEqual(self.db.execute('SELECT count(*) FROM order_intents').fetchone()[0],1)

    def test_unknown_attempt_never_resends_same_source(self):
        grant=admit(self.provider,self.envelope,now=NOW);record_return(self.provider,grant,now=NOW+1)
        with self.assertRaisesRegex(Review,'ANALYSIS_CONTINUATION_OUTCOME_UNKNOWN'):
            restore_received_checkpoint(self.provider,self.envelope,now=NOW+2)
        self.assertEqual(active_grant(self.db,OP)['state'],'UNKNOWN')
        with self.assertRaises(Review):admit(self.provider,self.envelope,now=NOW+2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM analysis_continuation_grants').fetchone()[0],1)
        self.assertIsNone(self.f.row('api_requests')['output'])

    def test_new_completed_reply_restores_checkpoint_then_uses_actual_native_recorder(self):
        original=self.f.row('api_requests');before_cycle=self.f.row('robot_cycles')
        grant=admit(self.provider,self.envelope,now=NOW);record_return(self.provider,grant,now=NOW+1)
        envelope=self.new_reply()
        self.assertTrue(restore_received_checkpoint(self.provider,envelope,now=NOW+3))
        self.assertEqual(self.f.row('api_requests'),original)
        self.assertEqual(self.f.row('robot_cycles'),before_cycle)
        self.assertEqual(recover_received_analysis(self.provider,envelope,now=NOW+3),'RESUME_CACHED')
        self.assertEqual(self.f.row('api_requests')['state'],'COMPLETE')
        self.assertEqual(self.f.row('api_requests')['created'],NOW-2000)
        self.assertEqual(self.db.execute('SELECT state FROM analysis_continuation_grants').fetchone()[0],'OBSERVED_NEW_REPLY')
        self.assertIsNone(self.f.row('robot_candidates')['plan'])
        self.assertEqual(json.loads(self.f.row('robot_cycles')['data'])['queue'],['BTCUSDT','ETHUSDT'])

    def not_sent(self,grant,at=NOW+1):
        prefix='analysis-continuation-'+grant['grant_id']
        value=dict(kind='NATIVE_ANALYSIS_CONTINUATION_NOT_SENT_V1',operation=OP,
            request_sha256=self.f.request_hash,grant_id=grant['grant_id'],send_attempted=False,
            new_intent_written=False,upload_started=False,closed_at=at,exception_class='FollowupUnavailable',
            admission=dict(name=prefix+'.admitted.json',sha256='a'*64,bytes=1024))
        return dict(value,disposition=dict(name=prefix+'.closed-not-sent.json',sha256=sha(wire(value)),bytes=len(wire(value).encode()),value=value))

    def test_positive_preupload_not_sent_retries_same_grant_and_retains_every_receipt(self):
        grant=admit(self.provider,self.envelope,now=NOW)
        record_return(self.provider,grant,self.not_sent(grant),now=NOW+1)
        self.assertFalse(restore_received_checkpoint(self.provider,self.envelope,now=NOW+2))
        retried=retry_not_sent(self.provider,self.envelope,now=NOW+2)
        self.assertEqual(retried,grant)
        record_return(self.provider,retried,now=NOW+3)
        self.assertEqual([row[0]for row in self.db.execute('SELECT state FROM analysis_continuation_returns ORDER BY sequence')],['NOT_SENT','UNKNOWN'])
        self.assertEqual(self.f.row('api_requests')['created'],NOW-2000)

    def test_only_proved_not_sent_renews_expired_technical_clock_after_fresh_read(self):
        grant=admit(self.provider,self.envelope,now=NOW)
        record_return(self.provider,grant,self.not_sent(grant),now=NOW+1)
        fresh=copy.deepcopy(self.envelope);fresh['observation'].update(observed_at=NOW+999,finished_at=NOW+1000);self.f.seal(fresh)
        renewed=retry_not_sent(self.provider,fresh,now=NOW+1001)
        self.assertEqual(renewed['grant_id'],grant['grant_id'])
        self.assertEqual(renewed['started_at'],NOW+1001)
        self.assertEqual(renewed['expires_at'],NOW+1901)
        self.assertEqual(self.f.row('api_requests')['created'],NOW-2000)
        audit=json.loads(self.db.execute("SELECT response FROM analysis_continuation_returns WHERE state='CLOCK_RENEWED_AFTER_PROVED_NOT_SENT'").fetchone()[0])
        self.assertEqual(audit['old_grant'],grant)
        self.assertEqual(audit['new_grant'],renewed)
        self.assertTrue(verify_dispatch_grant(self.db,OP,self.f.request_hash,DAY,NOW+1002))

    def test_upload_or_send_started_cannot_claim_safe_not_sent(self):
        grant=admit(self.provider,self.envelope,now=NOW)
        for key in ('upload_started','send_attempted','new_intent_written'):
            result=self.not_sent(grant);result[key]=True;result['disposition']['value'][key]=True
            raw=wire(result['disposition']['value']);result['disposition'].update(sha256=sha(raw),bytes=len(raw.encode()))
            with self.subTest(key=key),self.assertRaises(Review):record_return(self.provider,grant,result,now=NOW+1)
        self.assertEqual(active_grant(self.db,OP)['state'],'GRANTED')

    def test_owner_off_and_incomplete_followup_never_admit(self):
        self.db.execute('UPDATE robot_settings SET enabled=0')
        with self.assertRaises(Review):admit(self.provider,self.envelope,now=NOW)
        self.db.execute('UPDATE robot_settings SET enabled=1')
        envelope=copy.deepcopy(self.envelope);envelope['observation']['rows'][-1]['complete']=False;self.f.seal(envelope)
        with self.assertRaises(Review):admit(self.provider,envelope,now=NOW)
        self.assertEqual(self.f.row('api_requests')['state'],'NEEDS_REVIEW')

    def test_fresh_native_approval_does_not_create_another_followup(self):
        envelope=self.new_reply()
        with self.assertRaisesRegex(Review,'ANALYSIS_CONTINUATION_NOT_REQUIRED'):
            admit(self.provider,envelope,now=NOW+3)
        self.assertEqual(self.f.row('api_requests')['state'],'NEEDS_REVIEW')

    def test_unicode_payload_uses_native_ascii_digest_not_wire_utf8(self):
        self.assertNotEqual(digest(self.f.payload),sha(wire(self.f.payload)))
        proof=self.envelope['observation']
        self.assertEqual(proof['payload_sha256'],digest(self.f.payload))
        self.assertEqual(validate_observation(self.provider,self.envelope,self.f.payload,self.f.created,now=NOW,repair=True)['reason'],'NEUROBRO_INPUT_UNREADABLE')
        proof['payload_sha256']=sha(wire(self.f.payload));self.f.seal(self.envelope)
        with self.assertRaises(Review):admit(self.provider,self.envelope,now=NOW)

    def test_scheduler_continues_once_then_observes_and_resumes_without_another_send(self):
        responses=[self.envelope,self.new_reply()];calls=[]
        self.provider.observe_received_analysis=lambda operation:responses.pop(0)
        self.provider.continue_received_analysis=lambda operation,grant:(calls.append(grant['grant_id'])or dict(kind='NATIVE_ANALYSIS_CONTINUATION_RESULT_V1',operation=OP,request_sha256=self.f.request_hash,grant_id=grant['grant_id']))
        coordinator=SimpleNamespace(neuro=self.provider,db=self.db,allowed=lambda today:True,
            store=SimpleNamespace(report=lambda status,account,**kw:dict(status=status,**kw)),pause_reason=lambda:'ROBOT_OFF')
        with patch('worker.analysis_response_recovery.time.time',return_value=NOW):
            first=resume_uncertain_analysis(coordinator,DAY,{})
        self.assertEqual(first['wait_reason'],'ANALYSIS_CONTINUATION_REPLY_RECONCILING')
        with patch('worker.analysis_response_recovery.time.time',return_value=NOW+3):
            second=resume_uncertain_analysis(coordinator,DAY,{})
        self.assertEqual(second['wait_reason'],'ANALYSIS_RECEIVED_RESUME_PENDING')
        self.assertEqual(len(calls),1)
        self.assertEqual(self.f.row('api_requests')['state'],'COMPLETE')
        self.assertEqual(self.db.execute('SELECT count(*) FROM order_intents').fetchone()[0],1)

if __name__=='__main__':unittest.main()
