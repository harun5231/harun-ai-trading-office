"""Offline boundary tests. Fixtures are not evidence of installed/OS isolation."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent.hermes_runtime import (HermesRPCServer, HermesRuntime, HermesRuntimeClient,
    NativeEvaluationMemory, ProviderEvidence, RequestJournal, RuntimeFailure, PROTOCOL, RUNTIME, SDK_COMMIT,
    broker_uds_transport, canonical, configured_reasoning, decode, digest, peer_credentials, read_secret,
    receive_frame, remove_stale_socket, send_frame, verify_dependency_selection)
from agent.models import AmbiguousOperation, NotSent


class EvidenceFixture:
    def __init__(self, model):
        self.model, self.starts = model, 0

    def begin(self, request):
        self.starts += 1

    def finish(self):
        return dict(model=self.model, broker_model_verified=True,
            provider_response_id='actual-provider-response-fixture', provider_request_id='actual-request-fixture')

    def end(self):
        pass


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.pins = dict(runtime_source_sha256='a'*64, runtime_config_sha256='b'*64,
            isolation_policy_sha256='c'*64)
        self.model = 'gpt-6.1-sol'
        self.calls, self.agents = [], []
        self.output = dict(action='SCREENING', symbols=['BTCUSDT'], question='')
        self.failure = None
        self.extra_tools = []
        self.evidence = EvidenceFixture(self.model)
        self.journal = RequestJournal(self.path/'journal.sqlite3')
        self.runtime = self.make_runtime()

    def tearDown(self):
        self.journal.close()
        self.tmp.cleanup()

    def make_runtime(self):
        return HermesRuntime(model=self.model, pins=self.pins, journal=self.journal,
            agent_factory=self.factory, provider_evidence=self.evidence)

    def factory(self, **kwargs):
        test = self
        class AgentFixture:
            def __init__(self):
                self.model, self.session_id = kwargs['model'], kwargs['session_id']
                self.tools, self.enabled_toolsets = test.extra_tools, kwargs['enabled_toolsets']
                self._fallback_chain, self.closed = [], False
                self.kwargs = deepcopy(kwargs)

            def run_conversation(self, **turn):
                test.calls.append(deepcopy(turn))
                if test.failure:
                    raise test.failure
                self._current_api_request_id = 'sdk-generated-turn-fixture:api:1'
                return dict(final_response=json.dumps(test.output), completed=True)

            def close(self):
                self.closed = True
        agent = AgentFixture()
        self.agents.append(agent)
        return agent

    def request(self, index=1):
        value = dict(schema_version=1, protocol=PROTOCOL, runtime=RUNTIME,
            request_id=digest({'request':index}), conversation_id='neurobro:2026-11-07:daily-room',
            phase='SCREENING', source_request_sha256='d'*64, model=self.model, **self.pins,
            instructions='Only output dialogue control JSON.', history=[], reply='BTCUSDT',
            market_context=None, source_facts=[],
            output_schema={'properties':{'action':{'enum':['SCREENING','CLARIFY','HOLD']}}},
            max_output_tokens=512)
        value['request_sha256'] = digest(value)
        value['deadline_epoch'] = time.time()+40
        return value

    def test_genuine_sdk_facade_contract_and_explicit_empty_tools(self):
        request = self.request()
        envelope = self.runtime.execute(request)
        receipt = envelope['receipt']
        self.assertEqual(receipt['decision'], self.output)
        self.assertEqual(receipt['trace_id'], 'sdk-generated-turn-fixture:api:1')
        self.assertEqual(receipt['session_id'], self.agents[0].session_id)
        self.assertTrue(self.agents[0].closed)
        self.assertEqual(self.agents[0].kwargs['enabled_toolsets'], [])
        self.assertEqual(self.agents[0].kwargs['fallback_model'], {})
        self.assertTrue(self.agents[0].kwargs['skip_memory'])
        self.assertEqual(set(self.calls[0]), {'user_message','system_message','conversation_history','task_id'})
        self.assertEqual(self.calls[0]['system_message'], request['instructions'])
        self.assertNotIn('api_key', canonical(request))
        self.assertEqual(envelope['provider_evidence']['model'], self.model)

    def test_completed_result_survives_restart_and_new_deadline_without_second_call(self):
        request = self.request()
        first = self.runtime.execute(request)
        self.journal.close()
        self.journal = RequestJournal(self.path/'journal.sqlite3')
        self.runtime = self.make_runtime()
        request['deadline_epoch'] = time.time()+42
        self.assertEqual(self.runtime.execute(request), first)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_is_never_resent_after_restart(self):
        request = self.request()
        self.failure = TimeoutError('provider interruption fixture')
        with self.assertRaises(TimeoutError):
            self.runtime.execute(request)
        self.journal.close()
        self.journal = RequestJournal(self.path/'journal.sqlite3')
        self.runtime = self.make_runtime()
        self.failure = None
        with self.assertRaisesRegex(RuntimeFailure, 'OPERATION_UNKNOWN') as caught:
            self.runtime.execute(request)
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assertEqual(len(self.calls), 1)

    def test_unknown_remains_unknown_for_invalid_control_or_unverified_model(self):
        self.output = {'COIN':'BTC', 'ARAH':'LONG', 'ENTRY':100, 'TP':102, 'SL':99}
        with self.assertRaises(RuntimeFailure) as caught:
            self.runtime.execute(self.request())
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.output = dict(action='SCREENING', symbols=['BTCUSDT'], question='')
        self.evidence.model = 'foreign-model'
        with self.assertRaisesRegex(RuntimeFailure, 'PROVIDER_MODEL_UNVERIFIED') as caught:
            self.runtime.execute(self.request(2))
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assertEqual(self.journal.db.execute('SELECT state FROM runtime_requests').fetchall(),
            [('UNKNOWN',),('UNKNOWN',)])

    def test_phase_schema_foreign_symbol_and_question_enum_are_enforced(self):
        request = self.request()
        request['output_schema']['properties'].update(
            symbols={'maxItems':0}, question={'enum':['exact source prompt']})
        request['request_sha256'] = digest({k:v for k,v in request.items()
            if k not in ('request_sha256','deadline_epoch')})
        with self.assertRaisesRegex(RuntimeFailure,'CONTROL_RESULT_INVALID'):
            self.runtime.execute(request)

    def test_request_id_cannot_be_rebound_to_different_content(self):
        request = self.request()
        self.runtime.execute(request)
        request['reply'] = 'ETHUSDT'
        request['request_sha256'] = digest({k:v for k,v in request.items()
            if k not in ('request_sha256','deadline_epoch')})
        with self.assertRaisesRegex(RuntimeFailure, 'REQUEST_ID_MISMATCH'):
            self.runtime.execute(request)
        self.assertEqual(len(self.calls), 1)

    def test_nonempty_actual_tool_surface_fails_before_model_and_journal(self):
        self.extra_tools = [{'function':{'name':'terminal'}}]
        with self.assertRaisesRegex(RuntimeFailure, 'CAPABILITY_MISMATCH'):
            self.runtime.execute(self.request())
        self.assertEqual(self.calls, [])
        self.assertTrue(self.agents[0].closed)
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM runtime_requests').fetchone()[0], 0)

    def test_expiry_and_changed_pins_fail_before_model(self):
        request = self.request()
        request['deadline_epoch'] = time.time()-1
        with self.assertRaisesRegex(RuntimeFailure, 'DEADLINE_INVALID'):
            self.runtime.execute(request)
        request = self.request()
        request['runtime_source_sha256'] = 'e'*64
        with self.assertRaisesRegex(RuntimeFailure, 'BINDING_MISMATCH'):
            self.runtime.execute(request)
        self.assertEqual(self.calls, [])

    def test_runtime_has_own_durable_conversation_and_stable_sdk_session(self):
        first = self.runtime.execute(self.request())
        second = self.runtime.execute(self.request(2))
        self.assertEqual(first['receipt']['session_id'], second['receipt']['session_id'])
        self.assertEqual(len(self.calls[1]['conversation_history']), 2)
        self.assertEqual(self.calls[1]['conversation_history'][1]['content'], json.dumps(self.output))
        self.assertEqual(self.runtime.ready()['server_nonce'], self.runtime.ready()['server_nonce'])
        self.assertNotEqual(self.runtime.server_nonce, self.make_runtime().server_nonce)

    def test_observed_evaluation_uses_native_session_api_and_reenters_future_day_as_evidence(self):
        class NativeSessionDBFixture:
            def __init__(self):self.rows=[];self.sessions=[]
            def create_session(self,*args,**kwargs):self.sessions.append((args,kwargs))
            def append_message(self,session_id,**kwargs):
                self.rows.append(dict(id=len(self.rows)+1,session_id=session_id,**kwargs))
            def get_messages(self,session_id,**kwargs):
                return [deepcopy(row) for row in self.rows if row['session_id']==session_id][-kwargs['limit']:]
        native = NativeSessionDBFixture()
        self.runtime.native_memory = NativeEvaluationMemory(native,self.model)
        request = self.request()
        request['phase'] = 'POST_TRADE_EVALUATION'
        request['reply'] = 'Observasi: wick menyapu SL. Hipotesis belum diuji.'
        request['output_schema']['properties']['action']['enum']=['COMPLETE']
        request['request_sha256'] = digest({k:v for k,v in request.items()
            if k not in ('request_sha256','deadline_epoch')})
        self.output = dict(action='COMPLETE',symbols=[],question='')
        self.runtime.execute(request)
        self.assertTrue(native.rows[0]['observed'])
        saved = decode(native.rows[0]['content'])
        self.assertEqual(saved['reply'],request['reply'])
        self.assertEqual(saved['source_request_sha256'],request['source_request_sha256'])
        self.assertEqual(saved['status'],'OBSERVED_SOURCE_NOT_VERIFIED_EXPERIMENT')
        # Recreate the memory adapter, preserving the native state object as a restart would.
        self.runtime.native_memory = NativeEvaluationMemory(native,self.model)
        later = self.request(2)
        later['conversation_id']='neurobro:2026-11-08:daily-room'
        later['request_sha256'] = digest({k:v for k,v in later.items()
            if k not in ('request_sha256','deadline_epoch')})
        self.output = dict(action='SCREENING',symbols=['BTCUSDT'],question='')
        receipt = self.runtime.execute(later)
        actual_input = decode(self.calls[-1]['user_message'])
        self.assertEqual(actual_input['observed_evaluations'][0]['reply'],request['reply'])
        self.assertEqual(actual_input['observed_evaluations'][0]['native_message_id'],1)
        self.assertEqual(receipt['provider_evidence']['evaluation_memory_message_ids'],[1])
        self.assertEqual(len(native.rows),1)

    def test_duplicate_keys_and_nonfinite_json_are_rejected(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}'):
            with self.assertRaises(RuntimeFailure):
                decode(raw)


class ProviderEvidenceTests(unittest.TestCase):
    def setUp(self):
        import httpx
        self.httpx = httpx
        self.evidence = ProviderEvidence('explicit-model')
        self.evidence.begin({'max_output_tokens':512})

    def request(self, **changes):
        payload = dict(model='explicit-model', messages=[dict(role='user',content='data')], max_tokens=512)
        payload.update(changes)
        return self.httpx.Request('POST','http://hermes-model/v1/chat/completions', json=payload)

    def response(self, **changes):
        payload = dict(model='explicit-model', id='chatcmpl-provider-real-fixture')
        payload.update(changes)
        return self.httpx.Response(200, json=payload, headers={
            'x-hermes-verified-model':'explicit-model','x-request-id':'provider-request-fixture'})

    def test_observes_actual_provider_model_and_id_from_sdk_http_response(self):
        self.evidence.request_hook(self.request(reasoning_effort='medium'))
        self.evidence.response_hook(self.response())
        proof = self.evidence.finish()
        self.assertEqual(proof['provider_response_id'], 'chatcmpl-provider-real-fixture')
        self.assertEqual(proof['model'], 'explicit-model')
        self.assertTrue(proof['broker_model_verified'])
        self.assertEqual(proof['requested_reasoning_effort'],'medium')

    def test_model_mismatch_missing_broker_proof_and_second_request_are_refused(self):
        for change in ({'model':'other'}, {'tools':[{'name':'terminal'}]}, {'stream':True}):
            with self.subTest(change=change), self.assertRaises(RuntimeFailure):
                self.evidence.request_hook(self.request(**change))
        self.evidence.request_hook(self.request())
        with self.assertRaisesRegex(RuntimeFailure, 'REPEAT_REFUSED'):
            self.evidence.request_hook(self.request())
        with self.assertRaisesRegex(RuntimeFailure, 'MODEL_UNVERIFIED'):
            self.evidence.response_hook(self.response(model='other'))
        response = self.response()
        response.headers.pop('x-hermes-verified-model')
        with self.assertRaises(RuntimeFailure):
            self.evidence.response_hook(response)

    def test_actual_provider_url_is_preserved_until_fixed_unix_transport(self):
        from agent.hermes_model_broker import BrokerConfig, HermesModelBroker, UpstreamResponse
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'model.sock'
            calls = []
            def upstream(route,body,*,deadline):
                calls.append((route,decode(body)))
                return UpstreamResponse(200,{'content-type':'application/json','x-request-id':'provider-fixture'},
                    canonical({'model':'explicit-model','id':'chatcmpl-model-fixture','choices':[]}).encode())
            config = BrokerConfig(worker_uid=max(os.getuid(),1),upstream_base_url='https://api.openai.com/v1',
                model='explicit-model',allowed_paths=('/v1/chat/completions','/v1/responses'))
            with patch('agent.hermes_model_broker.verify_peer'):
                server = HermesModelBroker(str(path),config,upstream,secure_socket=False)
                thread = threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
                thread.start()
                try:
                    for route,body in (('/v1/chat/completions',{
                            'model':'explicit-model','messages':[{'role':'user','content':'synthetic health check'}],
                            'max_completion_tokens':512}),('/v1/responses',{
                            'model':'explicit-model','input':'synthetic health check','max_output_tokens':512,
                            'reasoning':{'effort':'medium'},'store':False})):
                        evidence = ProviderEvidence('explicit-model','https://api.openai.com/v1')
                        evidence.begin({'max_output_tokens':512})
                        with self.httpx.Client(transport=broker_uds_transport(self.httpx,str(path),
                                'https://api.openai.com/v1'), event_hooks={
                                'request':[evidence.request_hook],'response':[evidence.response_hook]}) as client:
                            response = client.post('https://api.openai.com'+route,json=body)
                            self.assertEqual(response.request.url.host,'api.openai.com')
                        self.assertEqual(calls[-1][0],route)
                        self.assertEqual(evidence.finish()['model'],'explicit-model')
                    self.assertEqual(calls[0][1]['max_completion_tokens'],512)
                    self.assertEqual(evidence.finish()['requested_reasoning_effort'],'medium')
                finally:
                    server.shutdown();server.server_close();thread.join(2)


class DependencySelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)
        self.environment=self.path/'generation'
        self.site=self.environment/f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
        self.site.mkdir(parents=True)
        self.cfg=self.environment/'pyvenv.cfg'
        self.cfg.write_text(f'version = {sys.version_info.major}.{sys.version_info.minor}.0\n')
        original='/root/.hermes/installs/8a4017c4cabfe15f/environments/observed-generation'
        self.selection=dict(schema_version=1,sdk_commit=SDK_COMMIT,source_root='/root/.hermes/hermes-agent',
            facts_file='/root/.hermes/installs/8a4017c4cabfe15f/facts.json',facts_sha256='e'*64,
            environment=original,site_packages=original+f'/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages',
            mounted_environment=str(self.environment),pyvenv_cfg_sha256=hashlib.sha256(self.cfg.read_bytes()).hexdigest())
        self.selection_file=self.path/'selection.json'
        self.config={'dependency_selection_file':str(self.selection_file),'runtime_site_packages':[str(self.site)]}
        self.write_selection()

    def tearDown(self):self.tmp.cleanup()

    def write_selection(self):
        self.selection_file.write_text(canonical(self.selection))
        self.config['dependency_selection_sha256']=hashlib.sha256(self.selection_file.read_bytes()).hexdigest()

    def test_actual_committed_generation_metadata_selects_only_mounted_dependencies_without_pth(self):
        canary=self.path/'pth-executed'
        (self.site/'unprocessed.pth').write_text(f'import pathlib; pathlib.Path({str(canary)!r}).touch()\n')
        self.assertEqual(verify_dependency_selection(self.config),[str(self.site)])
        self.assertFalse(canary.exists())

    def test_changed_pointer_generation_and_abi_fail_closed(self):
        self.config['dependency_selection_sha256']='0'*64
        with self.assertRaisesRegex(RuntimeFailure,'PIN_MISMATCH'):
            verify_dependency_selection(self.config)
        self.write_selection()
        self.selection['environment']='/root/.hermes/another-environment'
        self.write_selection()
        with self.assertRaisesRegex(RuntimeFailure,'SELECTION_INVALID'):
            verify_dependency_selection(self.config)
        self.selection['environment']='/root/.hermes/installs/8a4017c4cabfe15f/environments/observed-generation'
        self.cfg.write_text('version = 9.99.0\n')
        self.selection['pyvenv_cfg_sha256']=hashlib.sha256(self.cfg.read_bytes()).hexdigest()
        self.write_selection()
        with self.assertRaisesRegex(RuntimeFailure,'ABI_MISMATCH'):
            verify_dependency_selection(self.config)

    def test_reasoning_unset_stays_unset_and_explicit_selection_is_preserved(self):
        self.assertIsNone(configured_reasoning({}))
        self.assertIsNone(configured_reasoning({'reasoning_config':None}))
        explicit={'enabled':True,'effort':'medium'}
        observed=configured_reasoning({'reasoning_config':explicit})
        self.assertEqual(observed,explicit)
        self.assertIsNot(observed,explicit)
        self.assertEqual(configured_reasoning({'reasoning_config':{'enabled':False}}),{'enabled':False})
        with self.assertRaisesRegex(RuntimeFailure,'REASONING_CONFIG_INVALID'):
            configured_reasoning({'reasoning_config':{'enabled':True,'effort':'invented'}})


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.token = self.path/'token'
        self.token.write_text('t'*48)
        self.token.chmod(0o600)
        self.pins = dict(runtime_source_sha256='a'*64,runtime_config_sha256='b'*64,
            isolation_policy_sha256='c'*64)
        self.config = dict(model='explicit-model', hermes=dict(socket_path=str(self.path/'socket'),
            token_file=str(self.token),server_uid=10003,**self.pins,
            isolation_policy_file=str(self.path/'policy.json')))
        self.hello = dict(protocol=PROTOCOL,runtime=RUNTIME,sdk_commit=SDK_COMMIT,
            model='explicit-model',**self.pins,tools=[],fallback_chain=[],server_nonce='f'*64)
        self.calls, self.verifications = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def verifier(self, **kwargs):
        self.verifications.append(kwargs)
        return dict(verified=True,kernel_peer_verified=True,isolation_verified=True)

    def exchange_fixture(self, operation, payload=None, **kwargs):
        self.calls.append(operation)
        self.client._exchange_attestation = self.verifier(server_nonce='f'*64)
        if operation == 'ready':
            return deepcopy(self.hello)
        return dict(receipt=dict(runtime=RUNTIME,model='explicit-model',**self.pins,
            request_id=payload['request_id'],request_sha256=payload['request_sha256'],
            trace_id='sdk-turn:api:1',session_id='sdk-session',schema_version=1,protocol=PROTOCOL,
            conversation_id=payload.get('conversation_id','fixture-room'),phase=payload.get('phase','SCREENING'),
            status='completed',observed_at=time.time(),decision={'action':'SCREENING','symbols':['BTCUSDT'],'question':''}),
            provider_evidence=dict(model='explicit-model',broker_model_verified=True,
                provider_response_id='provider-fixture',provider_request_id='request-fixture'))

    def make(self):
        self.client = HermesRuntimeClient(self.config,peer_verifier=self.verifier)
        return self.client

    def test_client_receipt_attestation_survives_status_readiness_probe(self):
        client = self.make()
        with patch.object(client,'_exchange',side_effect=self.exchange_fixture):
            receipt = client({'request_id':'1'*64,'request_sha256':'2'*64,'deadline_epoch':time.time()+20})
            attestation = client.attestation()
            self.assertTrue(attestation['verified'])
            self.assertEqual(attestation['trace_id'], receipt['trace_id'])
            self.assertTrue(client.ready())
            self.assertEqual(client.attestation(), attestation)

    def test_lookup_recovers_durable_completed_receipt_after_lost_response_and_client_restart(self):
        client = self.make()
        request = dict(request_id='1'*64,request_sha256='2'*64,conversation_id='fixture-room',
            phase='SCREENING',deadline_epoch=time.time()+20)
        journal_path = self.path/'runtime.sqlite3'
        journal = RequestJournal(journal_path)
        saved = {}
        def lost_response(operation,payload=None,**kwargs):
            value = self.exchange_fixture(operation,payload,**kwargs)
            if operation == 'execute':
                saved.update(deepcopy(value))
                journal.reserve(payload)
                journal.complete(payload,value,[])
                raise AmbiguousOperation('response lost after durable completion')
            return value
        try:
            with patch.object(client,'_exchange',side_effect=lost_response):
                with self.assertRaises(AmbiguousOperation):client(request)
            self.assertEqual(self.calls.count('execute'),1)
            journal.close()
            journal = RequestJournal(journal_path)
            client = self.make()  # No cached receipt or attestation survives this restart.
            def recovery(operation,payload=None,**kwargs):
                if operation == 'ready':return self.exchange_fixture(operation,payload,**kwargs)
                self.assertEqual(operation,'lookup')
                self.calls.append(operation)
                client._exchange_attestation = self.verifier(server_nonce='f'*64)
                return journal.lookup(**payload)
            with patch.object(client,'_exchange',side_effect=recovery):
                receipt = client.lookup(request['request_id'],request['request_sha256'])
            self.assertEqual(receipt,saved['receipt'])
            attestation = client.attestation()
            self.assertTrue(all(attestation[key] for key in
                ('verified','kernel_peer_verified','isolation_verified')))
            for key in ('request_id','request_sha256','trace_id','session_id'):
                self.assertEqual(attestation[key],receipt[key])
            self.assertEqual(self.calls.count('execute'),1)
            self.assertEqual(self.calls.count('lookup'),1)
        finally:
            journal.close()

    def test_missing_unknown_and_mismatched_lookup_never_authorize_execute(self):
        client = self.make()
        request = dict(request_id='1'*64,request_sha256='2'*64)
        journal = RequestJournal(self.path/'runtime.sqlite3')
        def recovery(operation,payload=None,**kwargs):
            if operation == 'ready':return self.exchange_fixture(operation,payload,**kwargs)
            self.assertEqual(operation,'lookup')
            self.calls.append(operation)
            client._exchange_attestation = self.verifier(server_nonce='f'*64)
            return journal.lookup(**payload)
        try:
            with patch.object(client,'_exchange',side_effect=recovery):
                self.assertIsNone(client.lookup(**request))
                self.assertEqual(client.attestation(),{})
                journal.reserve(request)
                with self.assertRaises(AmbiguousOperation):client.lookup(**request)
                with self.assertRaises(AmbiguousOperation):client.lookup(request['request_id'],'3'*64)
            self.assertEqual(self.calls.count('execute'),0)
            self.assertEqual(journal.db.execute('SELECT state FROM runtime_requests').fetchone()[0],'UNKNOWN')
        finally:
            journal.close()

    def test_lookup_rejects_foreign_receipt_proof_and_unverified_peer(self):
        client = self.make()
        request = dict(request_id='1'*64,request_sha256='2'*64)
        for index,(container,key,value) in enumerate((
                ('receipt','request_sha256','4'*64),('receipt','runtime_config_sha256','5'*64),
                ('receipt','status','UNKNOWN'),('receipt','trace_id','resp_fabricated_trace'),
                ('provider_evidence','model','foreign-model'),('provider_evidence','broker_model_verified',False))):
            def recovery(operation,payload=None,**kwargs):
                value_envelope = self.exchange_fixture(operation,payload,**kwargs)
                if operation == 'lookup':value_envelope[container][key]=value
                return value_envelope
            with self.subTest(index=index),patch.object(client,'_exchange',side_effect=recovery):
                with self.assertRaises(AmbiguousOperation):client.lookup(**request)
                self.assertEqual(client.attestation(),{})
        def unverified(operation,payload=None,**kwargs):
            value = self.exchange_fixture(operation,payload,**kwargs)
            if operation == 'lookup':client._exchange_attestation={'verified':True}
            return value
        with patch.object(client,'_exchange',side_effect=unverified):
            with self.assertRaises(AmbiguousOperation):client.lookup(**request)
        self.assertNotIn('execute',self.calls)

    def test_failed_lookup_transport_is_ambiguous_for_the_original_operation(self):
        client = self.make()
        def unavailable(operation,payload=None,**kwargs):
            if operation == 'ready':return self.exchange_fixture(operation,payload,**kwargs)
            self.calls.append(operation)
            raise NotSent('lookup socket unavailable')
        with patch.object(client,'_exchange',side_effect=unavailable):
            with self.assertRaises(AmbiguousOperation):client.lookup('1'*64,'2'*64)
        self.assertNotIn('execute',self.calls)
        self.assertEqual(client.attestation(),{})

    def test_missing_or_foreign_runtime_readiness_never_executes(self):
        client = self.make()
        for key,value in [('model','other'),('runtime_config_sha256','0'*64),('tools',[{}])]:
            old = deepcopy(self.hello)
            self.hello[key] = value
            with patch.object(client,'_exchange',side_effect=self.exchange_fixture):
                with self.assertRaises(NotSent):
                    client({'deadline_epoch':time.time()+20})
            self.hello = old
        self.assertNotIn('execute', self.calls)

    def test_peer_verifier_receives_nonce_even_when_container_peer_pid_is_zero(self):
        client = self.make()
        client._server_nonce = 'f'*64
        with patch('agent.hermes_runtime.peer_credentials',return_value=(0,10003,10003)):
            result = client._verify_peer(object())
        self.assertTrue(result['verified'])
        self.assertEqual(self.verifications[-1]['server_nonce'], 'f'*64)
        with patch('agent.hermes_runtime.peer_credentials',return_value=(0,0,0)):
            with self.assertRaises(RuntimeFailure):
                client._verify_peer(object())

    def test_unverified_kernel_policy_cannot_be_replaced_by_server_assertion(self):
        client = self.make()
        client._peer_verifier = lambda **_:dict(verified=True)
        with patch('agent.hermes_runtime.peer_credentials',return_value=(123,10003,10003)):
            with self.assertRaisesRegex(RuntimeFailure,'ISOLATION_UNVERIFIED'):
                client._verify_peer(object())

    def test_real_socket_framing_and_real_kernel_peer_credentials(self):
        left,right = socket.socketpair()
        try:
            self.assertEqual(peer_credentials(left)[1], os.getuid())
            send_frame(left, {'hello':'json'}, 100)
            self.assertEqual(receive_frame(right,100), {'hello':'json'})
        finally:
            left.close();right.close()

    def test_world_readable_token_and_symlink_are_refused(self):
        self.token.chmod(0o644)
        with self.assertRaises(RuntimeFailure):
            read_secret(self.token)
        link = self.path/'link'
        link.symlink_to(self.token)
        with self.assertRaises(OSError):
            read_secret(link)

    def test_lost_response_after_execute_stays_unknown(self):
        client = self.make()
        class ConnectionFixture:
            def settimeout(self, _):pass
            def connect(self, _):pass
            def sendall(self, _):pass
            def recv(self, _):return b''
            def close(self):pass
        with patch('agent.hermes_runtime.socket.socket',return_value=ConnectionFixture()), \
                patch.object(client,'_verify_peer',return_value=self.verifier()):
            with self.assertRaises(AmbiguousOperation):
                client._exchange('execute',{},deadline=time.time()+20)

    def test_authenticated_explicit_predispatch_rejection_is_not_sent(self):
        client = self.make()
        class ConnectionFixture:
            def settimeout(self, _):pass
            def connect(self, _):pass
            def close(self):pass
        with patch('agent.hermes_runtime.socket.socket',return_value=ConnectionFixture()), \
                patch.object(client,'_verify_peer',return_value=self.verifier()), \
                patch('agent.hermes_runtime.send_frame'), \
                patch('agent.hermes_runtime.receive_frame',return_value={
                    'ok':False,'error':'HERMES_REQUEST_INVALID','state':'NOT_SENT'}):
            with self.assertRaises(NotSent):
                client._exchange('execute',{},deadline=time.time()+20)


class RPCServerTests(unittest.TestCase):
    def test_restart_removes_only_owned_stale_socket_and_refuses_live_or_foreign_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'socket'
            stale = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            stale.bind(str(path))
            stale.close()
            remove_stale_socket(path)
            self.assertFalse(path.exists())
            live = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            try:
                live.bind(str(path));live.listen(1)
                identity = path.stat().st_ino
                with self.assertRaises(RuntimeFailure):
                    remove_stale_socket(path)
                self.assertEqual(path.stat().st_ino,identity)
            finally:
                live.close()
            path.unlink()
            path.write_text('unrelated file')
            with self.assertRaises(RuntimeFailure):
                remove_stale_socket(path)
            self.assertEqual(path.read_text(),'unrelated file')

    def test_real_authenticated_uds_protocol_refuses_bad_token_before_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            token = path/'token'
            token.write_text('t'*48)
            token.chmod(0o600)
            calls = []
            class RuntimeFixture:
                def ready(self):return {'server_nonce':'f'*64}
                def execute(self,payload):
                    calls.append(payload)
                    return {'offline_fixture':True}
            server = HermesRPCServer(str(path/'socket'),RuntimeFixture(),str(token),os.getuid())
            thread = threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
            thread.start()
            def invoke(secret):
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                    connection.settimeout(2)
                    connection.connect(str(path/'socket'))
                    send_frame(connection,{'token':secret,'operation':'execute','payload':{'data':'bounded'}},1024)
                    return receive_frame(connection,1024)
            try:
                self.assertEqual(invoke('wrong-token')['state'],'NOT_SENT')
                self.assertEqual(calls,[])
                self.assertEqual(invoke('t'*48),{'ok':True,'value':{'offline_fixture':True}})
                self.assertEqual(calls,[{'data':'bounded'}])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)


if __name__ == '__main__':
    unittest.main()
