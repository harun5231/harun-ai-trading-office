"""Trading-only paid purposes and observed API usage, without real providers."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import urllib.error

from agent.llm import OpenAIDialogue,PAID_DIALOGUE_PHASES
from agent.memory import Memory,canonical
from agent.models import AgentError,NotSent
from tests.test_mini_dialogue_profile import retained_module,request
from tests.test_mini_http_cooldown import CompletedBody
from tests.test_mini_http_rejection import ErrorBody


class MiniPaidPurposeTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.memory=Memory(self.root/'agent.sqlite3');self.key=self.root/'key'
        self.key.write_text('unit-model-key-value-01234567890123456789');self.key.chmod(0o600)
        self.module=retained_module();self.model=OpenAIDialogue(self.key,self.memory)
        self.engine=self.module.DialogueEngine(self.memory,self.key,model=self.model)

    def tearDown(self):self.memory.close();self.temporary.cleanup()

    def rows(self,prefix):
        return [json.loads(row[0]) for row in self.memory.db.execute(
            'SELECT value FROM metadata WHERE key LIKE ?',(prefix+'%',))]

    def test_nontrading_purposes_never_reserve_or_dispatch_at_either_boundary(self):
        model=OpenAIDialogue(self.key,self.memory,transport=Mock(side_effect=AssertionError('no paid model')))
        for phase in ('POST_TRADE_EVALUATION','COMPOSE_POST_TRADE_EVALUATION','ADMIN','TITLE',
                'MEMORY_CLEANUP','HEARTBEAT','HEALTH','STARTUP'):
            with self.subTest(phase=phase),self.assertRaises((NotSent,ValueError)):
                model.plan([],'Historical/admin text',phase=phase,jakarta_day='2026-11-09')
        self.assertEqual(self.memory.db.execute('SELECT COUNT(*) FROM budgets').fetchone()[0],0)
        with patch('agent.llm.read_api_key',side_effect=AssertionError('must not read key')):
            with self.assertRaisesRegex(NotSent,'DIALOGUE_MODEL_PURPOSE_NOT_ALLOWED'):
                self.model._request({'input':[]})
            self.model.bind_operation('offline-known-trading-operation','SCREENING')
            with self.assertRaisesRegex(NotSent,'DIALOGUE_MODEL_PURPOSE_NOT_ALLOWED'):
                self.model._request({'input':[{'role':'user','content':'Fase: POST_TRADE_EVALUATION\nadmin'}]})
        self.assertEqual(self.rows('dialogue-model-api-'),[])

    def test_live_plan_unbound_or_phase_mismatch_is_denied_before_budget_or_key(self):
        for bound in (None,'SCREENING'):
            self.model._operation_context=None
            if bound:self.model.bind_operation('offline-known-operation',bound)
            with self.subTest(bound=bound),self.assertRaisesRegex(NotSent,'DIALOGUE_MODEL_PURPOSE_NOT_ALLOWED'):
                self.model.plan([],'BTCUSDT',phase='ANALYSIS',jakarta_day='2026-11-09')
        self.assertEqual(self.memory.db.execute('SELECT COUNT(*) FROM budgets').fetchone()[0],0)
        self.assertEqual(self.rows('dialogue-model-api-'),[])

    def test_exact_six_trading_phases_remain_allowed_with_existing_limits(self):
        self.assertEqual(PAID_DIALOGUE_PHASES,{'SCREENING','ANALYSIS','PENDING_REVIEW',
            'COMPOSE_SCREENING','COMPOSE_ANALYSIS','COMPOSE_PENDING_REVIEW'})
        for phase in sorted(PAID_DIALOGUE_PHASES):
            action=('PROMPT' if phase.startswith('COMPOSE_') else
                'SCREENING' if phase=='SCREENING' else 'COMPLETE' if phase=='ANALYSIS' else 'TAHAN')
            value={'action':action,'symbols':[] if action=='PROMPT' else ['BTCUSDT'],
                'question':'BTCUSDT' if action=='PROMPT' else ''}
            model=OpenAIDialogue(self.key,self.memory,transport=lambda _payload:{'status':'completed','output':[
                {'type':'message','content':[{'type':'output_text','text':json.dumps(value)}]}]})
            with self.subTest(phase=phase):
                self.assertEqual(model.plan([],'BTCUSDT',phase=phase,jakarta_day='2026-11-09').action,action)
                self.assertEqual((model.timeout,model.max_output_tokens),(45,1024))
        self.assertEqual(self.memory.db.execute('SELECT requests FROM budgets').fetchone()[0],5)

    def test_owner_literal_one_two_replacement_and_replay_need_no_budget_model_or_new_authentication(self):
        from agent.dialogue_client import DialogueClient
        from worker.prompts import SCREENING,REPLACEMENT_SCREENING
        from worker.account_state import SCREENING_ONE
        opener=SimpleNamespace(open=Mock(side_effect=AssertionError('literal must not use provider')))
        for index,prompt in enumerate((SCREENING,SCREENING_ONE,
                REPLACEMENT_SCREENING.format(count=1),REPLACEMENT_SCREENING.format(count=2))):
            value=request('owner-literal-'+str(index),phase='COMPOSE_SCREENING',reply=prompt)
            with patch('agent.llm.urllib.request.build_opener',return_value=opener):
                result=self.engine.process(value)
                self.assertEqual(self.engine.process({**value,'deadline_epoch':value['deadline_epoch']+1}),result)
            self.assertEqual(result['decision'],{'action':'PROMPT','symbols':[],'question':prompt})
            self.assertIsNone(self.model._operation_context)
        records=self.rows('dialogue-owner-literal:')
        self.assertEqual(len(records),4)
        self.assertEqual({record['source_prompt_sha256'] for record in records},
            {hashlib.sha256(prompt.encode()).hexdigest() for prompt in (SCREENING,SCREENING_ONE,
                REPLACEMENT_SCREENING.format(count=1),REPLACEMENT_SCREENING.format(count=2))})
        self.assertTrue(all(record['authority']=='OWNER_LITERAL_COMPOSITION' and record['model_calls']==0
            and record['operation_id'].startswith('delegated-model:') and 'usage' not in record for record in records))
        self.assertEqual(self.memory.db.execute('SELECT COUNT(*) FROM budgets').fetchone()[0],0)
        self.assertEqual(self.rows('dialogue-model-api-'),[])
        self.assertFalse(self.model.evidence()['verified']);self.assertIsNone(self.model._completed_at)
        token=self.root/'dialogue.token';token.write_text('offline-dialogue-authentication-token-only-12345678')
        token.chmod(0o600);uid=os.geteuid()
        server=self.module.DialogueServer(self.root/'dialogue.sock',self.engine,token,server_uid=uid,caller_uid=uid).start()
        try:
            wire=DialogueClient(self.root/'dialogue.sock',token,server_uid=uid,caller_uid=uid).compose(value)
            self.assertEqual(wire,result)
        finally:server.close()
        opener.open.assert_not_called()
        self.assertEqual(self.memory.counts()['operations'],{'DONE':4})

    def test_literal_still_refuses_private_oversize_or_unbound_input_and_obeys_existing_cooldown(self):
        for index,prompt in enumerate(('Pilih coin dengan risiko Office 5 USDT.','x'*1601)):
            self.model.bind_operation('offline-invalid-literal-'+str(index),'COMPOSE_SCREENING')
            with self.assertRaises(AgentError):
                self.model.plan([],prompt,phase='COMPOSE_SCREENING',jakarta_day='2026-11-09')
        self.model._operation_context=None
        with self.assertRaisesRegex(NotSent,'DIALOGUE_MODEL_PURPOSE_NOT_ALLOWED'):
            self.model.plan([],'Pilih satu coin.',phase='COMPOSE_SCREENING',jakarta_day='2026-11-09')
        self.model.bind_operation('offline-direct-literal','COMPOSE_SCREENING')
        with patch('agent.llm.read_api_key',side_effect=AssertionError('no key read for literal wire')):
            with self.assertRaisesRegex(NotSent,'DIALOGUE_MODEL_PURPOSE_NOT_ALLOWED'):
                self.model._request({'input':[{'role':'user','content':'Fase: COMPOSE_SCREENING\nPilih satu coin.'}]})
        self.model._record_http_cooldown({'provider_request_id':'req_offline_confirmed_quota',
            'observed_at':123.0,'model_input_sha256':'a'*64,'classification':'MODEL_INSUFFICIENT_QUOTA'})
        self.model.bind_operation('offline-held-literal','COMPOSE_SCREENING')
        with self.assertRaisesRegex(NotSent,'MODEL_INSUFFICIENT_QUOTA'):
            self.model.plan([],'Pilih satu coin.',phase='COMPOSE_SCREENING',jakarta_day='2026-11-09')
        self.assertIsNone(self.model._operation_context)
        self.assertEqual(self.memory.db.execute('SELECT COUNT(*) FROM budgets').fetchone()[0],0)
        self.assertEqual(self.rows('dialogue-owner-literal:'),[])
        self.assertEqual(self.rows('dialogue-model-api-'),[])

    def test_generated_engine_denies_before_any_claim_but_preserves_unknown_and_done_history(self):
        value=request('blocked-eval',phase='POST_TRADE_EVALUATION',reply='Tinjau arsip evaluasi.')
        for phase in ('POST_TRADE_EVALUATION','COMPOSE_POST_TRADE_EVALUATION'):
            with self.subTest(phase=phase),self.assertRaisesRegex(NotSent,'DIALOGUE_MODEL_PURPOSE_NOT_ALLOWED'):
                self.engine.process({**value,'phase':phase})
        self.assertEqual(self.memory.counts()['operations'],{})
        self.assertEqual(self.memory.db.execute('SELECT COUNT(*) FROM budgets').fetchone()[0],0)
        self.assertEqual(self.module.safe_error(NotSent('DIALOGUE_MODEL_PURPOSE_NOT_ALLOWED')),
            'DIALOGUE_MODEL_NOT_SENT')
        result={'schema_version':1,'ok':True,
            **{key:value[key] for key in ('request_id','conversation_id','phase','reply_sha256')},
            'authority':'DIALOGUE_CONTROL_ONLY','trading_authority':False,
            'decision':{'action':'COMPLETE','symbols':[],'question':''},'needs_followup':False,'reason':'DIALOGUE_DECISION'}
        row=('delegated-model:'+value['request_id'],'DELEGATED_MODEL_DIALOGUE',
            hashlib.sha256(canonical(value).encode()).hexdigest(),'DONE',json.dumps(result),123.0)
        self.memory.db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?)',row)
        self.key.unlink();before=list(self.memory.db.iterdump())
        self.assertEqual(self.engine.process(value),result)
        self.assertEqual(list(self.memory.db.iterdump()),before)
        self.memory.db.execute("UPDATE operations SET state='UNKNOWN',result=NULL")
        unknown=list(self.memory.db.iterdump())
        with self.assertRaisesRegex(self.module.DialogueError,'DIALOGUE_OPERATION_UNKNOWN'):
            self.engine.process(value)
        self.assertEqual(list(self.memory.db.iterdump()),unknown)

    def test_retained_client_accepts_known_not_sent_wire_code_without_claim_or_model_call(self):
        from agent.dialogue_client import DialogueClient,DialogueError
        token=self.root/'dialogue.token';token.write_text('offline-dialogue-authentication-token-only-12345678')
        token.chmod(0o600);uid=os.geteuid()
        self.model.plan=Mock(side_effect=AssertionError('no model for denied purpose'))
        server=self.module.DialogueServer(self.root/'dialogue.sock',self.engine,token,
            server_uid=uid,caller_uid=uid).start()
        try:
            client=DialogueClient(self.root/'dialogue.sock',token,caller_uid=uid,server_uid=uid)
            with self.assertRaisesRegex(DialogueError,'^DIALOGUE_MODEL_NOT_SENT$'):
                client.request(request('wire-policy-denied',phase='POST_TRADE_EVALUATION'))
        finally:server.close()
        self.model.plan.assert_not_called()
        self.assertEqual(self.memory.counts()['operations'],{})
        self.assertEqual(self.memory.db.execute('SELECT COUNT(*) FROM budgets').fetchone()[0],0)
        denial=self.rows('dialogue-model-purpose-denied:')
        self.assertEqual(len(denial),1)
        self.assertEqual(denial[0]['phase'],'POST_TRADE_EVALUATION')
        self.assertEqual(denial[0]['reason'],'DIALOGUE_MODEL_PURPOSE_NOT_ALLOWED')
        self.assertFalse(denial[0]['request_sent'])

    def live_response(self,usage=None):
        result={'id':'resp_offline_usage','status':'completed','model':'gpt-5-mini','output':[
            {'type':'message','content':[{'type':'output_text','text':json.dumps(
                {'action':'SCREENING','symbols':['BTCUSDT'],'question':''})}]}]}
        if usage is not None:result['usage']=usage
        body=CompletedBody(json.dumps(result).encode());body.headers={'x-request-id':'req_offline_usage'}
        return body

    def test_actual_engine_operation_and_official_usage_record_once_replay_makes_no_paid_call(self):
        usage={'input_tokens':120,'output_tokens':30,'total_tokens':150,
            'input_tokens_details':{'cached_tokens':20},'output_tokens_details':{'reasoning_tokens':10}}
        opener=SimpleNamespace(open=Mock(return_value=self.live_response(usage)))
        value=request('usage-proof')
        with patch('agent.llm.urllib.request.build_opener',return_value=opener):
            result=self.engine.process(value);self.assertEqual(self.engine.process(value),result)
        self.assertEqual(opener.open.call_count,1)
        dispatch=self.rows('dialogue-model-api-dispatch:');response=self.rows('dialogue-model-api-result:')
        self.assertEqual((len(dispatch),len(response)),(1,1))
        for record in (*dispatch,*response):
            self.assertEqual(record['operation_id'],'delegated-model:'+value['request_id'])
            self.assertEqual(record['phase'],'SCREENING');self.assertIsNone(record['cost_usd'])
            self.assertEqual(record['cost_state'],'UNKNOWN')
            self.assertNotIn(value['reply'],json.dumps(record))
        self.assertIsNone(dispatch[0]['request_sent']);self.assertIsNone(dispatch[0]['usage'])
        self.assertEqual(response[0]['usage'],usage)
        self.assertEqual(response[0]['usage_state'],'OPENAI_RESPONSE_USAGE')
        self.assertEqual(response[0]['provider_request_id'],'req_offline_usage')
        self.assertTrue(response[0]['request_sent'])

    def test_missing_or_invalid_usage_remains_unknown_never_derived_from_reservations(self):
        for index,usage in enumerate((None,{}, {'input_tokens':5,'output_tokens':2,'total_tokens':999})):
            opener=SimpleNamespace(open=Mock(return_value=self.live_response(usage)))
            with patch('agent.llm.urllib.request.build_opener',return_value=opener):
                self.engine.process(request('missing-usage-'+str(index)))
        rows=self.rows('dialogue-model-api-result:')
        self.assertEqual(len(rows),3)
        self.assertTrue(all(row['usage'] is None and row['usage_state']=='UNKNOWN' for row in rows))
        self.assertTrue(all(row['cost_usd'] is None for row in rows))
        self.assertEqual(self.memory.db.execute('SELECT requests FROM budgets').fetchone()[0],3)

    def test_rejection_and_timeout_record_unknown_cost_and_never_invent_official_tokens(self):
        errors=[urllib.error.HTTPError('https://offline.invalid',429,'private',
            {'x-request-id':'req_offline_quota'},ErrorBody(json.dumps({'error':{'code':'insufficient_quota'}}).encode())),
            TimeoutError('private timeout')]
        for index,error in enumerate(errors):
            self.memory.db.execute('DELETE FROM metadata WHERE key=?',(self.model.cooldown_key,))
            self.model._cooldown_cache=None
            opener=SimpleNamespace(open=Mock(side_effect=error))
            with patch('agent.llm.urllib.request.build_opener',return_value=opener),self.assertRaises(AgentError):
                self.engine.process(request('failed-usage-'+str(index)))
            self.assertEqual(opener.open.call_count,1)
        rows=self.rows('dialogue-model-api-result:')
        self.assertEqual(len(rows),2)
        self.assertTrue(all(row['usage'] is None and row['cost_state']=='UNKNOWN' for row in rows))
        self.assertEqual({row['stage'] for row in rows},{'PROVIDER_RESPONSE','OUTCOME_UNKNOWN'})

    def test_audit_write_failure_before_dispatch_blocks_network(self):
        opener=SimpleNamespace(open=Mock(side_effect=AssertionError('must not contact provider')))
        original=self.memory.put
        def put(key,value):
            if key.startswith('dialogue-model-api-dispatch:'):raise OSError('offline disk failure')
            return original(key,value)
        with patch.object(self.memory,'put',side_effect=put),patch(
                'agent.llm.urllib.request.build_opener',return_value=opener),self.assertRaisesRegex(
                    self.module.DialogueError,'DIALOGUE_MODEL_NOT_SENT'):
            self.engine.process(request('audit-failed'))
        opener.open.assert_not_called()
        self.assertEqual(self.rows('dialogue-model-api-result:'),[])

    def test_audit_delay_cannot_extend_absolute_deadline_or_create_unknown_wire_result(self):
        clock=[1000.0];original=self.memory.put
        def put(key,value):
            result=original(key,value)
            if key.startswith('dialogue-model-api-dispatch:'):clock[0]+=46
            return result
        opener=SimpleNamespace(open=Mock(side_effect=AssertionError('must not open after deadline')))
        self.model.bind_operation('offline-delayed-audit','SCREENING')
        with patch.object(self.memory,'put',side_effect=put),patch('agent.llm.time.monotonic',
                side_effect=lambda:clock[0]),patch('agent.llm.urllib.request.build_opener',return_value=opener):
            with self.assertRaisesRegex(NotSent,'MODEL_DEADLINE_EXPIRED'):
                self.model.plan([],'BTCUSDT',phase='SCREENING',jakarta_day='2026-11-09')
        opener.open.assert_not_called()
        self.assertEqual(self.rows('dialogue-model-api-dispatch:')[0]['stage'],'DISPATCH_INTENT')
        result=self.rows('dialogue-model-api-result:')
        self.assertEqual(len(result),1);self.assertEqual(result[0]['stage'],'NOT_SENT')
        self.assertFalse(result[0]['request_sent']);self.assertFalse(result[0]['provider_response_received'])
        self.assertIsNone(result[0]['usage']);self.assertEqual(result[0]['cost_state'],'UNKNOWN')


if __name__=='__main__':unittest.main()
