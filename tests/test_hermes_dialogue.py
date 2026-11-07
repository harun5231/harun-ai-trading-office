"""Offline injected-RPC candidate tests; no real Hermes/OS/runtime proof."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent.hermes_dialogue import HermesDialogue, PROTOCOL, RUNTIME
from agent.memory import Memory
from agent.models import AgentError, AmbiguousOperation, NotSent
from agent.native_market_context import convert_native_market_context
from tests.test_agent_native_market_context import native_context


class HermesDialogueCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.directory=Path(self.tmp.name)
        self.memory=Memory(self.directory/'agent.sqlite3');self.calls=[]
        self.pins=dict(runtime_source_sha256='a'*64,runtime_config_sha256='b'*64,isolation_policy_sha256='c'*64)
        self.output=dict(action='SCREENING',symbols=['BTCUSDT'],question='')
        self.model=self.make()

    def tearDown(self):
        self.memory.close();self.tmp.cleanup()

    def transport(self,request):
        self.calls.append(deepcopy(request))
        return {**{k:request[k] for k in ('request_id','request_sha256','conversation_id','phase','model',*self.pins)},
            'schema_version':1,'protocol':PROTOCOL,'runtime':RUNTIME,'status':'completed',
            'trace_id':'hermes-trace-real-runtime-would-supply','session_id':'hermes-session-fixture',
            'observed_at':time.time(),'decision':deepcopy(self.output)}

    def make(self,**changes):
        # Offline protocol tests explicitly opt out of production verification.
        args=dict(model='gpt-6.1-sol',transport=self.transport,
            require_verified_transport=False,**self.pins);args.update(changes)
        return HermesDialogue('/not-read-by-offline-adapter',self.memory,**args)

    def request(self,phase='SCREENING',reply='BTCUSDT',index=0):
        now=time.time()
        market=None if phase in ('SCREENING','COMPOSE_SCREENING','POST_TRADE_EVALUATION','COMPOSE_POST_TRADE_EVALUATION') else convert_native_market_context(native_context(now),now=now)
        return dict(schema_version=1,request_id=hashlib.sha256(str(index).encode()).hexdigest(),
            conversation_id='neurobro:2026-11-07:daily-room',phase=phase,reply=reply,
            reply_sha256=hashlib.sha256(reply.encode()).hexdigest(),market_context=market,
            gate={'robot_on':True,'native_dispatch_verified':True},deadline_epoch=now+80)

    def plan(self,request=None,history=None,model=None):
        value=request or self.request();model=model or self.model
        model.bind_request(value)
        return model.plan(history or [],value['reply'],phase=value['phase'],jakarta_day='2026-11-07',
            context=value['market_context'],source_facts=value.get('source_facts'))

    def test_bound_rpc_is_explicit_hermes_only_and_does_not_claim_os_proof(self):
        result=self.plan();self.assertEqual(result.action,'SCREENING')
        request=self.calls[0]
        self.assertEqual(request['runtime'],RUNTIME);self.assertEqual(request['model'],'gpt-6.1-sol')
        self.assertEqual(request['request_id'],self.request()['request_id'])
        self.assertFalse(self.model.evidence()['verified'])
        self.assertTrue(self.model.evidence()['runtime_receipt_verified'])
        self.assertNotIn('resp_',self.model.evidence()['trace_id'])
        self.assertNotIn('tools',request);self.assertNotIn('api_key',request)
        with self.assertRaisesRegex(AgentError,'MODEL_IDENTITY_INVALID'):self.make(model='gpt-5-mini')
        with self.assertRaisesRegex(AgentError,'TRANSPORT_REQUIRED'):self.make(transport=None)

    def test_explicit_provider_model_names_do_not_expand_runtime_trace_identity(self):
        model=self.make(model='provider/model-v1+reasoning')
        self.assertEqual(model.model,'provider/model-v1+reasoning')
        with self.assertRaisesRegex(AgentError,'MODEL_IDENTITY_INVALID'):
            self.make(model='provider/model with whitespace')

    def test_every_existing_phase_uses_the_same_rpc_and_indonesian_json_request_policy(self):
        phases=('SCREENING','ANALYSIS','PENDING_REVIEW','POST_TRADE_EVALUATION',
            'COMPOSE_SCREENING','COMPOSE_ANALYSIS','COMPOSE_PENDING_REVIEW','COMPOSE_POST_TRADE_EVALUATION')
        for index,phase in enumerate(phases):
            reply='BTCUSDT TAHAN'if phase=='PENDING_REVIEW'else'BTCUSDT'
            request=self.request(phase,reply,index+10)
            self.output=(dict(action='PROMPT',symbols=[],question=reply if phase=='COMPOSE_SCREENING'
                else'Mohon analisis BTCUSDT dan berikan JSON akhir COIN, ARAH, ENTRY, TP, SL.')
                if phase.startswith('COMPOSE_') else dict(action='SCREENING'if phase=='SCREENING'else'TAHAN'
                    if phase=='PENDING_REVIEW'else'COMPLETE',symbols=['BTCUSDT']if phase in ('SCREENING','ANALYSIS','PENDING_REVIEW')else[],question=''))
            with self.subTest(phase=phase):self.plan(request)
        self.assertEqual(len(self.calls),len(phases))
        for call in self.calls:
            self.assertIn('COIN, ARAH, ENTRY, TP, SL',call['instructions'])
            self.assertIn('RISK REWARD 1:2',call['instructions'])
            self.assertIn('Jangan menulis JSON pengganti',call['instructions'])
            if call['phase'] in ('ANALYSIS','COMPOSE_ANALYSIS'):
                description=call['output_schema']['properties']['question']['description']
                self.assertIn('JSON COIN, ARAH, ENTRY, TP, SL',description)
                self.assertNotIn('Risk Manager',description)

    def test_private_new_inputs_are_projected_without_changing_source_or_mini_history(self):
        request=self.request();values=dict(symbol='BTCUSDT',risk_usdt='PRIVATE_BUDGET_918',quantity='PRIVATE_QTY_743',
            fee_entry_rate='PRIVATE_FEE_569',execution_tp='PRIVATE_EXEC_TP_827',neurobro_tp='101')
        request['source_facts']=[dict(producer='RISK_MANAGER',operation='original-setup',values=values,
            sha256=hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest())]
        history=[dict(role='user',content=json.dumps(dict(COIN='BTCUSDT',ENTRY='100',SL='99',
            quantity='OLD_QTY_CANARY',execution_tp='OLD_EXEC_TP_CANARY'))),
            dict(role='assistant',content='Quantity OLD_RAW_QTY_CANARY. Risiko Office 918.25 USDT.')]
        old=deepcopy((request,history));self.plan(request,history)
        wire=json.dumps(self.calls[0])
        for canary in ('PRIVATE_BUDGET_918','PRIVATE_QTY_743','PRIVATE_FEE_569','PRIVATE_EXEC_TP_827',
                'OLD_QTY_CANARY','OLD_EXEC_TP_CANARY','OLD_RAW_QTY_CANARY','918.25'):
            self.assertNotIn(canary,wire)
        self.assertEqual((request,history),old)
        self.assertEqual(self.calls[0]['source_facts'][0]['values'],{'symbol':'BTCUSDT','neurobro_tp':'101'})
        self.assertIn('100',self.calls[0]['history'][0]['content'])
        self.assertEqual(self.memory.history(request['conversation_id']),[])

    def test_completed_rpc_is_read_after_restart_without_another_model_call(self):
        value=self.request();self.plan(value);self.memory.close()
        self.memory=Memory(self.directory/'agent.sqlite3');model=self.make()
        value['deadline_epoch']=time.time()+80
        result=self.plan(value,model=model)
        self.assertEqual(result.action,'SCREENING');self.assertEqual(len(self.calls),1)

    def test_unknown_rpc_is_never_replayed_or_converted_to_hold(self):
        def failed(request):self.calls.append(request);raise TimeoutError('synthetic injected RPC interruption')
        model=self.make(transport=failed);value=self.request()
        with self.assertRaises(TimeoutError):self.plan(value,model=model)
        self.assertEqual(self.memory.db.execute("SELECT state FROM operations").fetchone()[0],'UNKNOWN')
        with self.assertRaises(AmbiguousOperation):self.plan(value,model=self.make())
        self.assertEqual(len(self.calls),1)

    def test_request_binding_and_foreign_runtime_identity_are_rejected(self):
        value=self.request();self.model.bind_request(value)
        with self.assertRaisesRegex(AgentError,'BINDING_MISMATCH'):
            self.model.plan([],value['reply']+' altered',phase=value['phase'],jakarta_day='2026-11-07')
        self.assertEqual(self.calls,[])
        for index,(field,bad) in enumerate((('model','gpt-5-mini'),('request_id','0'*64),
                ('runtime_source_sha256','d'*64),('trace_id','resp_fake_openai_identity'))):
            def wrong(request,field=field,bad=bad):
                out=self.transport(request);out[field]=bad;return out
            with self.subTest(field=field),self.assertRaises(AgentError):
                self.plan(self.request(index=index+50),model=self.make(transport=wrong))
        self.assertEqual(self.memory.counts()['operations'],{'UNKNOWN':4})

    def test_true_transport_attestation_is_separate_from_model_authored_receipt(self):
        def attestation():
            request=self.calls[-1]
            return dict(verified=True,kernel_peer_verified=True,isolation_verified=True,runtime=RUNTIME,
                model='gpt-6.1-sol',**self.pins,request_id=request['request_id'],request_sha256=request['request_sha256'],
                trace_id='hermes-trace-real-runtime-would-supply',session_id='hermes-session-fixture')
        model=self.make(attestation=attestation);self.plan(model=model)
        self.assertTrue(model.evidence()['verified'])  # Injected fixture, never production proof.
        with patch.object(model,'attestation',return_value={'verified':True}):
            self.assertFalse(model.evidence()['verified'])

    def test_old_mini_receipts_untouched_and_no_expired_dispatch(self):
        self.memory.operation('delegated-model:historical','DELEGATED_MODEL_DIALOGUE',{'old':'input'},
            lambda:dict(model='gpt-5-mini',old_receipt='immutable'))
        before=tuple(self.memory.db.execute("SELECT * FROM operations WHERE operation_id='delegated-model:historical'").fetchone())
        self.model.set_remaining_budget(lambda:0)
        with self.assertRaises(NotSent):self.plan()
        self.assertEqual(self.calls,[])
        after=tuple(self.memory.db.execute("SELECT * FROM operations WHERE operation_id='delegated-model:historical'").fetchone())
        self.assertEqual(before,after)

    def test_private_or_fabricated_price_clarification_never_leaves_adapter(self):
        for index,question in enumerate(('Berikan quantity 5.', 'ENTRY 987654321 belum sesuai.')):
            self.output=dict(action='CLARIFY',symbols=['BTCUSDT'],question=question)
            with self.subTest(question=question),self.assertRaises(AgentError):
                self.plan(self.request(index=index+90))

    def test_production_default_requires_independent_runtime_verification(self):
        with self.assertRaisesRegex(AgentError,'VERIFICATION_REQUIRED'):
            HermesDialogue('/unused',self.memory,model='gpt-6.1-sol',transport=self.transport,**self.pins)
        model=self.make(require_verified_transport=True,attestation=lambda:{},runtime_ready=lambda:False)
        with self.assertRaisesRegex(NotSent,'RUNTIME_UNVERIFIED'):
            self.plan(model=model)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.memory.counts()['operations'],{'DONE':1})

    def test_post_dispatch_unverified_receipt_is_unknown_and_never_replayed(self):
        model=self.make(require_verified_transport=True,attestation=lambda:{},runtime_ready=lambda:True)
        with self.assertRaisesRegex(AgentError,'RUNTIME_UNVERIFIED'):
            self.plan(model=model)
        self.assertEqual(self.memory.counts()['operations'],{'UNKNOWN':1})
        with self.assertRaises(AmbiguousOperation):
            self.plan(model=model)
        self.assertEqual(len(self.calls),1)

    def test_readiness_latency_cannot_extend_native_dispatch_deadline(self):
        request=self.request()
        now=[time.time()]
        request['deadline_epoch']=now[0]+3
        def ready():
            now[0]+=4
            return True
        model=self.make(require_verified_transport=True,attestation=lambda:{},runtime_ready=ready,
            clock=lambda:now[0])
        with self.assertRaisesRegex(NotSent,'DEADLINE_BEFORE_DISPATCH'):
            self.plan(request,model=model)
        self.assertEqual(self.calls,[])

    def test_private_schema_enum_or_nested_public_fact_never_reaches_runtime(self):
        request=self.request('COMPOSE_SCREENING','Pilih BTCUSDT. Risiko Office 918.25 USDT.')
        with self.assertRaisesRegex(AgentError,'PRIVATE_DISCUSSION_REFUSED'):
            self.plan(request)
        self.assertEqual(self.calls,[])
        request=self.request(index=301)
        values=dict(symbol='BTCUSDT',entry_price={'quantity':'NESTED_PRIVATE_CANARY'},
            neurobro_tp='101')
        request['source_facts']=[dict(producer='RISK_MANAGER',operation='existing-order',values=values,
            sha256=hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest())]
        history=[dict(role='user',content=json.dumps(dict(COIN='BTCUSDT',ENTRY='100',
            notional='PRIVATE_NOTIONAL_CANARY',api_key='PRIVATE_KEY_CANARY')))]
        self.plan(request,history)
        wire=json.dumps(self.calls[0])
        for canary in ('NESTED_PRIVATE_CANARY','PRIVATE_NOTIONAL_CANARY','PRIVATE_KEY_CANARY'):
            self.assertNotIn(canary,wire)

    def test_private_caller_number_cannot_authorize_a_new_public_price(self):
        request=self.request(index=401)
        values=dict(symbol='BTCUSDT',risk_usdt='918.25')
        request['source_facts']=[dict(producer='RISK_MANAGER',operation='existing-order',values=values,
            sha256=hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest())]
        self.output=dict(action='CLARIFY',symbols=['BTCUSDT'],question='ENTRY 918.25 belum sesuai.')
        with self.assertRaisesRegex(AgentError,'PRICE_PROPOSAL_REFUSED'):
            self.plan(request)

if __name__=='__main__':unittest.main()
