"""Read-only reconnect/restart recovery against the actual native recorder.

No browser, model, live account or order transport is used.
"""
import copy
from datetime import datetime,timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from worker.core import Review,DYNAMIC_RISK_MODEL
from worker.neuroapi import SETUP_SCHEMA_V4
from worker.analysis_response_recovery import (KIND,NATIVE_SOURCE,TERMINAL,REASON,
    wire,sha,digest,validate_observation,recover_received_analysis,record_analysis_candidate,
    received_nonexecuted,resume_uncertain_analysis)
from agent.native_analysis_receipt import persist_review,read_reviews
from tests.test_agent_closed_candles_200 import native_context_200
from tests.test_screening_response_recovery import native_module

ROOT=Path(__file__).resolve().parents[1]
NOW=datetime(2026,11,5,12,0,tzinfo=timezone.utc).timestamp()
DAY='2026-11-05';CYCLE=DAY+':robot-v9:0';OP=CYCLE+':analysis-v9:BTCUSDT'


def utc(epoch):return datetime.fromtimestamp(epoch,timezone.utc).isoformat()


class AnalysisResponseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.old={name:sys.modules.get(name) for name in ('worker.research_provider','worker.neurobrowser')}
        with patch('worker.owner_source_protocol.load_owner_release_cutoff',return_value='2026-11-04T00:00:00Z'):
            self.source=native_module('worker.research_provider',ROOT/'vps_native/worker/research_provider.py')
        self.native=native_module('worker.neurobrowser',ROOT/'vps_native/worker/neurobrowser.py')
        self.addCleanup(self.restore)
        self.db=sqlite3.connect(':memory:',isolation_level=None);self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close);self.source.ensure_journal(self.db)
        self.db.executescript('''CREATE TABLE robot_cycles(id TEXT PRIMARY KEY,day TEXT,entry_epoch INTEGER,state TEXT,data TEXT);
        CREATE TABLE robot_jobs(operation TEXT PRIMARY KEY,cycle TEXT,kind TEXT,symbol TEXT,state TEXT,risk_target TEXT);
        CREATE TABLE robot_settings(id INTEGER PRIMARY KEY,enabled INTEGER,risk TEXT);
        INSERT INTO robot_settings VALUES(1,1,'5');
        CREATE TABLE robot_candidates(id TEXT PRIMARY KEY,cycle TEXT,symbol TEXT,status TEXT,plan TEXT,failure_code TEXT);
        CREATE TABLE order_intents(id TEXT PRIMARY KEY,candidate_id TEXT,symbol TEXT,state TEXT,payload TEXT);
        INSERT INTO order_intents VALUES('old-manual','old-manual','ETHUSDT','CLOSED','accepted immutable payload');''')
        self.provider=object.__new__(self.native.NeuroBrowser);self.provider.db=self.db
        self.provider.chart_max_age=180;self.provider.transport=self.forbid
        self.context=native_context_200(NOW-100)
        self.context['risk_constraints']={'risk_model':DYNAMIC_RISK_MODEL}
        self.body,serialized,self.request_hash=self.source.request_body(OP,'Prompt analisis pemilik — terbaru',SETUP_SCHEMA_V4,self.context)
        self.payload={**self.body,'request_sha256':self.request_hash};self.created=NOW-90
        self.data=dict(target=2,queue=['ETHUSDT'],seen=['BTCUSDT'],screen=0,replacements=0,round_symbols=['BTCUSDT','ETHUSDT'])
        self.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',(CYCLE,DAY,0,'NEEDS_REVIEW',json.dumps(self.data)))
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',(OP,CYCLE,'ANALYSIS','BTCUSDT','NEEDS_REVIEW','5'))
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',(OP,None,self.request_hash,'NEEDS_REVIEW',self.created,1,None,'NEUROBRO_BROWSER_OUTCOME_UNKNOWN'))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',(OP,self.source.BROWSER_PROVIDER,self.source.BROWSER_CONTRACT,self.request_hash,self.body['marker'],serialized))
        self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',(OP,CYCLE,'BTCUSDT','REJECTED',None,'NEUROBRO_BROWSER_OUTCOME_UNKNOWN'))
        self.root=tempfile.TemporaryDirectory();self.addCleanup(self.root.cleanup)
        Path(self.root.name).chmod(0o700)
        self.envelope=self.fixture()

    def restore(self):
        for name,value in self.old.items():
            if value is None:sys.modules.pop(name,None)
            else:sys.modules[name]=value
    def forbid(self,*args,**kwargs):raise AssertionError('Must not send/upload/model/order')
    def row(self,table):return dict(self.db.execute('SELECT * FROM '+table+' WHERE '+('id' if table in ('robot_cycles','robot_candidates') else 'operation')+'=?',(CYCLE if table=='robot_cycles' else OP,)).fetchone())
    def seal(self,envelope):
        proof=envelope['observation'];raw=wire(proof)
        envelope.update(observation_sha256=sha(raw),observation_bytes=len(raw.encode()))
        return envelope
    def fixture(self,reply='COIN: BTCUSDT\nARAH: HOLD',*,stale=False,approval=True):
        check=NOW-60;observed=NOW+300 if stale else NOW-10
        url='https://app.neurobro.ai/chat/'+'a'*32
        initial=[dict(source='BINANCE_USDM_FUTURES_WEBSITE_SCREENSHOT',symbol='BTCUSDT',timeframe=tf,
            captured_at=utc(NOW-80),sha256=char*64,bytes=2048) for tf,char in (('15m','a'),('1h','b'))]
        charts=[dict(row,captured_at=utc(NOW-65),attachment_id='file-'+row['timeframe']) for row in initial]
        names=[self.body['marker']+'-'+row['timeframe']+'-'+row['sha256'][:12]+'.png' for row in charts]
        names+=['BTCUSDT_'+tf+'_200_closed_'+char*8+'.csv' for tf,char in (('15m','c'),('1h','d'))]
        first='COIN: BTCUSDT\nScreenshot belum terbaca.\nARAH: HOLD'
        prompt='Prompt asli\n'+self.body['marker'];question='Berikut lampiran terbaru, mohon nilai ulang.\n'+self.body['marker']
        rows=[dict(role='user',text=prompt,attachments=names),
            dict(role='assistant',text=first,complete=True,id='first'),
            dict(role='user',text=question,attachments=names),
            dict(role='assistant',text=reply,complete=True,id='final')]
        intent=dict(schema_version=1,kind='NATIVE_200_CHART_FOLLOWUP',operation=OP,
            request_sha256=self.request_hash,marker=self.body['marker'],original_payload_sha256=digest(self.payload),
            initial_context_sha256=digest(self.context),followup_context_sha256=digest(self.context),
            projected_context_sha256='e'*64,initial_prompt_sha256=sha(prompt),followup_prompt_sha256=sha(question),
            initial_reply_sha256=sha(first),initial_turns_sha256=digest(rows[:2]),conversation_url_sha256=sha(url),
            initial_chart_evidence=initial,initial_attachment_names=names,
            followup_chart_evidence=charts,followup_attachment_names=names,observed_at=check-10,
            closed_candles_per_timeframe=200,closed_candles_total=400,followup_sequence=1,
            previous_followup_intent_sha256=None,material_send_check=dict(symbol='BTCUSDT',running_candles_separate=True,
            closed_candles_per_timeframe=200,closed_candles_total=400,send_checked_at=check,
            data_age_at_send_check_seconds=40,data_observed_at=NOW-100,screenshots=[]))
        entry=dict(value=intent,sha256=digest(intent),bytes=len(wire(intent,ascii=True).encode()))
        tiles=[]
        for index in (0,2):
            tiles.append(dict(row=index,prompt_sha256=sha(rows[index]['text']),
                images=[dict(alt=name,visible=True,decoded=dict(complete=True,width=1314,height=738)) for name in names[:2]],
                file_tile_witnesses=[dict(names=names,witness=[dict(name=name,ready=True,matches=1) for name in names])]))
        proof=dict(schema_version=2,kind=KIND,operation=OP,request_sha256=self.request_hash,
            payload_sha256=digest(self.payload),native_source_sha256=NATIVE_SOURCE,operation_completed=False,
            orders_sent=0,model_calls=0,provider_messages=0,uploads=0,observed_at=observed-1,finished_at=observed,
            status='CONNECTED',stage='OBSERVED_WITHOUT_COMPLETION',errors=[],rows=rows,
            room=dict(day=DAY,room_id='a'*32,url=url),page_url=url,
            room_witness=dict(native_room_matches=True,same_loaded_room_verified=True,
                initial_analysis_user_rows=[0],followup_user_rows=[2]),
            operation_scope=dict(verified=True,unmatched_intents=[],reply_complete=True),stable_snapshot_reads=4,
            rendered_prompt_receipt=dict(operation=OP,payload_sha256=digest(self.payload),rendered_sha256=sha(prompt),rendered_prompt=prompt),
            receipt_files={'native.intent.json':entry},file_tiles=tiles,prepared_materials=[],review_receipts=[])
        if approval:
            reply_sha=sha(reply);transcript=digest(rows)
            request_id=digest(dict(operation=OP,request_sha256=self.request_hash,reply_sha256=reply_sha,phase='ANALYSIS',transcript_sha256=transcript))
            request=dict(schema_version=1,request_id=request_id,conversation_id='neurobro:'+DAY+':'+'a'*32,
                phase='ANALYSIS',reply=reply,reply_sha256=reply_sha,market_context={'symbol':'BTCUSDT'},
                gate=dict(robot_on=True,native_dispatch_verified=True),deadline_epoch=NOW+50)
            response=dict(schema_version=1,ok=True,request_id=request_id,conversation_id=request['conversation_id'],phase='ANALYSIS',
                reply_sha256=reply_sha,authority='DIALOGUE_CONTROL_ONLY',trading_authority=False,
                decision=dict(action='HOLD' if 'ARAH: HOLD' in reply else 'COMPLETE',symbols=['BTCUSDT'],question=''),
                needs_followup=False,reason='DIALOGUE_DECISION')
            persist_review(self.root.name,self.payload,request,response,rows,context=self.context,observed_at=NOW-20)
            proof['review_receipts']=read_reviews(self.root.name,self.payload)
        return self.seal(dict(kind=KIND,operation=OP,request_sha256=self.request_hash,
            operation_completed=False,observation=proof,capture_name='received-analysis-observation-'+'a'*16+'-'+'b'*64+'.json',
            capture_sha256='b'*64,capture_bytes=100000))

    def test_fresh_hold_resumes_existing_cached_operation_and_preserves_original_error(self):
        before=self.row('robot_candidates');original_api=self.row('api_requests')
        self.assertEqual(recover_received_analysis(self.provider,self.envelope,now=NOW),'RESUME_CACHED')
        self.assertEqual(self.row('api_requests')['state'],'COMPLETE')
        self.assertEqual(self.row('robot_jobs')['state'],'PENDING')
        self.assertEqual(json.loads(self.row('robot_cycles')['data'])['queue'],['BTCUSDT','ETHUSDT'])
        self.assertEqual(self.row('robot_candidates'),before)
        audit=self.db.execute('SELECT * FROM analysis_response_recoveries').fetchone()
        self.assertEqual(json.loads(audit['preimage'])['api_requests'],original_api)
        self.db.execute('BEGIN IMMEDIATE')
        record_analysis_candidate(self.db,(OP,CYCLE,'BTCUSDT','HOLD',None,None))
        self.db.execute('COMMIT')
        self.assertEqual(self.row('robot_candidates')['status'],'HOLD')
        self.assertEqual(self.db.execute('SELECT phase FROM analysis_response_recoveries').fetchone()[0],'CONSUMED')
        self.assertEqual(self.db.execute('SELECT payload FROM order_intents').fetchone()[0],'accepted immutable payload')

    def test_stale_clear_reply_is_nonexecuted_and_replaceable_not_fake_hold(self):
        self.envelope=self.fixture(stale=True,approval=False)
        self.assertEqual(recover_received_analysis(self.provider,self.envelope,now=NOW+301),'NOEXEC')
        self.assertEqual(self.row('api_requests')['state'],TERMINAL)
        self.assertIsNone(self.row('api_requests')['output'])
        self.assertEqual(self.row('robot_candidates')['status'],'REJECTED')
        self.assertTrue(received_nonexecuted(self.db,self.row('robot_candidates')))
        self.assertEqual(json.loads(self.row('robot_cycles')['data'])['queue'],['ETHUSDT'])
        self.assertEqual(self.row('api_requests')['created'],self.created)

    def test_stale_long_never_creates_execution_candidate_or_intent(self):
        self.envelope=self.fixture(reply='COIN: BTCUSDT\nARAH: LONG\nENTRY: 100\nSL: 99\nTP: 102',stale=True,approval=False)
        self.assertEqual(recover_received_analysis(self.provider,self.envelope,now=NOW+301),'NOEXEC')
        self.assertEqual(self.row('robot_candidates')['status'],'REJECTED')
        self.assertIsNone(self.row('robot_candidates')['plan'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM order_intents').fetchone()[0],1)

    def test_fresh_reply_without_actual_mini_receipt_is_not_executable(self):
        self.envelope=self.fixture(approval=False)
        with self.assertRaisesRegex(Review,'ANALYSIS_MINI_APPROVAL_NOT_RETAINED'):
            recover_received_analysis(self.provider,self.envelope,now=NOW)
        self.assertIsNone(self.db.execute('SELECT * FROM research_browser_responses').fetchone())

    def test_technical_hold_final_cannot_clear_unknown_even_when_stale(self):
        self.envelope=self.fixture(reply='Data screenshot belum terbaca.\nCOIN: BTCUSDT\nARAH: HOLD',stale=True,approval=False)
        with self.assertRaisesRegex(Review,'NEUROBRO_INPUT_UNREADABLE'):
            recover_received_analysis(self.provider,self.envelope,now=NOW+301)
        self.assertEqual(self.row('api_requests')['state'],'NEEDS_REVIEW')

    def test_incomplete_challenge_wrong_room_and_unmatched_followup_do_not_write(self):
        changes=(lambda p:p['rows'][-1].update(complete=False),
            lambda p:p.update(status='CHALLENGE_REQUIRED'),
            lambda p:p['room_witness'].update(same_loaded_room_verified=False),
            lambda p:p['operation_scope'].update(unmatched_intents=['unseen.intent.json']),
            lambda p:p['file_tiles'][-1]['images'][0]['decoded'].update(complete=False))
        for change in changes:
            envelope=copy.deepcopy(self.envelope);change(envelope['observation']);self.seal(envelope)
            with self.subTest(change=change),self.assertRaises(Review):
                recover_received_analysis(self.provider,envelope,now=NOW)
            self.assertIsNone(self.db.execute('SELECT * FROM research_browser_responses').fetchone())

    def test_off_and_active_actor_busy_preserve_unknown_and_do_not_send(self):
        self.db.execute('UPDATE robot_settings SET enabled=0')
        with self.assertRaises(Review):recover_received_analysis(self.provider,self.envelope,now=NOW)
        self.db.execute('UPDATE robot_settings SET enabled=1')
        report=lambda status,account,**kw:dict(status=status,**kw)
        coordinator=SimpleNamespace(neuro=self.provider,db=self.db,allowed=lambda today:True,
            store=SimpleNamespace(report=report),pause_reason=lambda:'ROBOT_OFF')
        self.provider.observe_received_analysis=lambda operation:(_ for _ in()).throw(Review('NEUROBRO_BUSY'))
        result=resume_uncertain_analysis(coordinator,DAY,{})
        self.assertEqual(result['wait_reason'],'NEUROBRO_BUSY')
        self.assertEqual(self.row('api_requests')['state'],'NEEDS_REVIEW')

    def test_orphan_pending_after_restart_recovers_same_job_without_replay(self):
        self.db.execute('DELETE FROM robot_candidates') # isolated fixture: emulate no candidate ever written
        self.data['queue'].insert(0,'BTCUSDT');self.data['seen']=[]
        self.db.execute('UPDATE robot_cycles SET data=?,state=\'ACTIVE\'',(json.dumps(self.data),))
        self.db.execute('UPDATE robot_jobs SET state=\'PENDING\'')
        self.db.execute('UPDATE api_requests SET state=\'PENDING\',failure_code=NULL')
        self.assertEqual(recover_received_analysis(self.provider,self.envelope,now=NOW),'RESUME_CACHED')
        self.assertEqual(json.loads(self.row('robot_cycles')['data'])['queue'],['BTCUSDT','ETHUSDT'])

    def test_native_insert_crash_preserves_receipt_and_restarts_idempotently(self):
        original=self.provider._record_browser_response
        def interrupted(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('disconnect after actual native COMPLETE')
        self.provider._record_browser_response=interrupted
        with self.assertRaisesRegex(RuntimeError,'disconnect'):
            recover_received_analysis(self.provider,self.envelope,now=NOW)
        receipt=dict(self.db.execute('SELECT * FROM research_browser_responses').fetchone())
        self.provider._record_browser_response=original
        self.assertEqual(recover_received_analysis(self.provider,self.envelope,now=NOW+1),'RESUME_CACHED')
        self.assertEqual(dict(self.db.execute('SELECT * FROM research_browser_responses').fetchone()),receipt)

    def test_scheduler_automatically_discovers_native_complete_scheduling_crash_gap(self):
        original=self.provider._record_browser_response
        def interrupted(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('crash before scheduling CAS')
        self.provider._record_browser_response=interrupted
        with self.assertRaises(RuntimeError):recover_received_analysis(self.provider,self.envelope,now=NOW)
        self.provider._record_browser_response=original
        self.provider.require_key=lambda:None
        self.provider.observe_received_analysis=self.forbid
        coordinator=SimpleNamespace(neuro=self.provider,db=self.db,allowed=lambda today:True,
            store=SimpleNamespace(report=lambda status,account,**kw:dict(status=status,**kw)),pause_reason=lambda:'ROBOT_OFF')
        with patch('worker.analysis_response_recovery.time.time',return_value=NOW+1):
            result=resume_uncertain_analysis(coordinator,DAY,{})
        self.assertEqual(result['wait_reason'],'ANALYSIS_RECEIVED_RESUME_PENDING')
        self.assertEqual(self.row('robot_jobs')['state'],'PENDING')
        self.assertEqual(json.loads(self.row('robot_cycles')['data'])['queue'],['BTCUSDT','ETHUSDT'])

    def test_long_disconnect_after_native_insert_is_nonexecuted_on_restart(self):
        original=self.provider._record_browser_response
        def interrupted(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('disconnect after native insert')
        self.provider._record_browser_response=interrupted
        with self.assertRaises(RuntimeError):recover_received_analysis(self.provider,self.envelope,now=NOW)
        receipt=dict(self.db.execute('SELECT * FROM research_browser_responses').fetchone())
        self.provider._record_browser_response=original
        self.assertEqual(recover_received_analysis(self.provider,self.envelope,now=NOW+400),'NOEXEC')
        self.assertEqual(dict(self.db.execute('SELECT * FROM research_browser_responses').fetchone()),receipt)
        self.assertEqual(self.row('api_requests')['state'],TERMINAL)
        self.assertIsNone(self.row('api_requests')['output'])
        self.assertTrue(received_nonexecuted(self.db,self.row('robot_candidates')))

    def test_observer_raw_attachment_projection_binds_actual_review(self):
        for row in self.envelope['observation']['rows']:
            if row['role']=='user':row['attachments']=['UNCONFIRMED']
        self.seal(self.envelope)
        self.assertEqual(recover_received_analysis(self.provider,self.envelope,now=NOW),'RESUME_CACHED')

    def test_bare_stale_error_without_receipt_cannot_free_replacement_budget(self):
        self.db.execute('UPDATE api_requests SET state=?,failure_code=?',(TERMINAL,REASON))
        self.db.execute('UPDATE robot_jobs SET state=?',(TERMINAL,))
        self.assertFalse(received_nonexecuted(self.db,self.row('robot_candidates')))

    def test_only_sealed_failed_candidate_can_be_replaced(self):
        self.db.execute('BEGIN IMMEDIATE')
        with self.assertRaises(Review):record_analysis_candidate(self.db,(OP,CYCLE,'BTCUSDT','HOLD',None,None))
        self.db.execute('ROLLBACK')

    def coordinator(self):
        # The captured native source retains this unused legacy import; the
        # standalone repository fixture has already removed that old constant.
        with patch('worker.account_state.MAX_REPLACEMENTS',None,create=True):
            module=native_module('worker.native_robot_recovery_fixture',ROOT/'vps_native/worker/robot.py')
        self.addCleanup(lambda:sys.modules.pop('worker.native_robot_recovery_fixture',None))
        coordinator=object.__new__(module.Coordinator)
        coordinator.db=self.db;coordinator.neuro=self.provider;coordinator.stopping=None
        coordinator.account_factory=lambda:None;coordinator.new_setup_policy=DYNAMIC_RISK_MODEL
        coordinator.allowed=lambda today:True
        coordinator.execution_slots=lambda account,today:2
        coordinator.settle_previous_research=lambda today,account:None
        coordinator.recover_invalid_screening=lambda today,account:None
        coordinator.market=SimpleNamespace(max_age=180,catalog=lambda:{'BTCUSDT':{},'ETHUSDT':{}})
        coordinator.store=SimpleNamespace(settings=lambda:dict(robot_on=True,risk_target_usdt='5'),
            entries=lambda today:0,report_account=lambda account:None,
            results=lambda cycle:list(self.db.execute('SELECT * FROM robot_candidates WHERE cycle=?',(cycle,))),
            report=lambda status,account=None,reason=None,**kw:dict(bot_status=status,reason=reason,**kw))
        coordinator.exposed_symbols=lambda account:set()
        self.provider.require_key=lambda:None
        return module,coordinator

    def test_actual_native_scheduler_drains_recovered_cached_hold_without_resend(self):
        self.assertEqual(recover_received_analysis(self.provider,self.envelope,now=NOW),'RESUME_CACHED')
        module,coordinator=self.coordinator()
        with patch.object(module,'day',return_value=DAY),patch.object(module.time,'time',return_value=NOW), \
                patch.object(module,'account_state',return_value={}), \
                patch('agent.native_pending_review.review_then_advance',return_value=None), \
                patch.object(module,'analysis_context',return_value=(self.context,None)):
            result=coordinator._tick()
        self.assertEqual(result['bot_status'],'HOLD')
        self.assertEqual(self.row('robot_jobs')['state'],'COMPLETE')
        self.assertEqual(self.row('robot_candidates')['status'],'HOLD')
        self.assertEqual(json.loads(self.row('robot_cycles')['data'])['queue'],['ETHUSDT'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM order_intents').fetchone()[0],1)

    def test_actual_native_scheduler_screens_actual_two_free_slots_after_stale_recovery(self):
        envelope=self.fixture(stale=True,approval=False)
        self.assertEqual(recover_received_analysis(self.provider,envelope,now=NOW+301),'NOEXEC')
        data=json.loads(self.row('robot_cycles')['data']);data['queue']=[];data['round_symbols']=['BTCUSDT']
        self.db.execute('UPDATE robot_cycles SET data=?',(json.dumps(data),))
        module,coordinator=self.coordinator();calls=[]
        coordinator.screen=lambda cycle,data,count,account,today,initial:(calls.append(count) or {'count':count})
        with patch.object(module,'day',return_value=DAY),patch.object(module.time,'time',return_value=NOW+302), \
                patch.object(module,'account_state',return_value={}), \
                patch('agent.native_pending_review.review_then_advance',return_value=None):
            result=coordinator._tick()
        self.assertEqual(result,{'count':2});self.assertEqual(calls,[2])

    def test_initial_only_material_proves_posted_attachments_and_stale_nonexecution(self):
        from agent.native_analysis_material import retain_prepared,read_prepared
        from agent.native_data_files import deliver_market_data_files
        envelope=self.fixture(approval=False);proof=envelope['observation']
        chart=proof['receipt_files']['native.intent.json']['value']['followup_chart_evidence']
        files=deliver_market_data_files(self.context,now=NOW-90)
        names=[self.payload['marker']+'-'+row['timeframe']+'-'+row['sha256'][:12]+'.png' for row in chart]+[row['name'] for row in files]
        proof['rows']=proof['rows'][:2];proof['rows'][1]['text']='COIN: BTCUSDT\nARAH: HOLD'
        proof['rows'][0]['attachments']=names
        proof['room_witness']['followup_user_rows']=[]
        proof['receipt_files']={};proof['file_tiles']=proof['file_tiles'][:1]
        tile=proof['file_tiles'][0]
        tile['images']=[dict(alt=name,visible=True,decoded=dict(complete=True,width=1300,height=700)) for name in names[:2]]
        tile['file_tile_witnesses']=[dict(names=names,witness=[dict(name=name,ready=True,matches=1) for name in names])]
        retain_prepared(self.root.name,self.payload,chart,names,files,proof['rows'][0]['text'],observed_at=NOW-60)
        proof['prepared_materials']=read_prepared(self.root.name,self.payload)
        proof['observed_at']=NOW+300;proof['finished_at']=NOW+301
        self.seal(envelope)
        self.assertEqual(recover_received_analysis(self.provider,envelope,now=NOW+302),'NOEXEC')
        self.assertTrue(received_nonexecuted(self.db,self.row('robot_candidates')))

    def test_review_receipt_is_immutable_and_idempotent_for_new_technical_deadline(self):
        original=read_reviews(self.root.name,self.payload)[0]['value']
        request=copy.deepcopy(original['request']);request['deadline_epoch']+=10
        result=persist_review(self.root.name,self.payload,request,original['response'],self.envelope['observation']['rows'],
            context=self.context,observed_at=NOW-10)
        self.assertEqual(result,original)
        self.assertEqual(len(read_reviews(self.root.name,self.payload)),1)
        self.assertFalse(any(path.name.endswith('.tmp') for path in Path(self.root.name).iterdir()))

    def test_native_provider_uses_only_existing_fixed_no_send_rpc(self):
        seen=[];self.provider._token='x'*40;self.provider.require_key=lambda:None
        self.provider.transport=lambda method,url,headers,body,timeout:(seen.append((method,url,body)) or (200,{},self.envelope))
        self.assertEqual(self.provider.observe_received_analysis(OP),self.envelope)
        self.assertEqual(seen, [('POST',self.native.BASE+'/v1/screening-recovery',
            dict(action='ANALYSIS_RECEIVED_OBSERVE',payload=self.payload))])
        with self.assertRaisesRegex(Review,'NEUROBRO_BROWSER_ENDPOINT_DENIED'):
            self.native.browser_request('POST',self.native.BASE+'/v1/screening-recovery',{},dict(action='SEND',payload={}))


if __name__=='__main__':unittest.main()
