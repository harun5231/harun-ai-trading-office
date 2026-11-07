"""Service cutover tests with an injected bridge, never installed-Hermes proof."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent import dialogue_service as d
from agent.hermes_dialogue import HermesDialogue, PROTOCOL, RUNTIME
from agent.llm import OpenAIDialogue
from agent.memory import Memory
from tests.test_agent_dialogue_service import request


class HermesServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.memory = Memory(self.base/'agent.sqlite3')
        self.calls = []
        self.pins = dict(runtime_source_sha256='a'*64, runtime_config_sha256='b'*64,
            isolation_policy_sha256='c'*64)
        self.available = True
        self.last = None
        self.model = HermesDialogue('/missing-and-not-read', self.memory, model='gpt-6.1-sol',
            transport=self.transport, attestation=self.attestation,
            runtime_ready=lambda:self.available, **self.pins)
        self.engine = d.DialogueEngine(self.memory, '/missing-and-not-read', model=self.model)

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def transport(self, payload):
        self.calls.append(deepcopy(payload))
        self.last = {**{key:payload[key] for key in ('request_id','request_sha256',
                'conversation_id','phase','model',*self.pins)}, 'schema_version':1,
            'protocol':PROTOCOL,'runtime':RUNTIME,'status':'completed','trace_id':'hermes-trace-test',
            'session_id':'hermes-session-test','observed_at':time.time(),
            'decision':dict(action='SCREENING',symbols=['BTCUSDT'],question='')}
        return deepcopy(self.last)

    def attestation(self):
        if self.last is None:
            return {}
        return {**{key:self.last[key] for key in ('request_id','request_sha256',
            'trace_id','session_id','runtime','model',*self.pins)},
            'verified':True,'kernel_peer_verified':True,'isolation_verified':True}

    def health(self):
        health = object.__new__(d.DialogueHealth)
        health.dialogue = SimpleNamespace(engine=self.engine, runtime_ready=lambda:True)
        health.generation = 'a'*32
        return health.observe()

    def configuration(self):
        references = dict(socket='/run/harun-dialogue/dialogue.sock',data_directory='/data',
            token_file='/run/secrets/dialogue_token',key_file='/run/secrets/openai_api_key')
        value = dict(schema_version=3,mode='NATIVE_DELEGATED_DIALOGUE',backend=d.HERMES_BACKEND,
            model='gpt-6.1-sol',socket_path=references['socket'],data_directory=references['data_directory'],
            dialogue_token_file=references['token_file'],openai_key_file=references['key_file'],
            hermes=dict(socket_path='/run/hermes-trading/runtime.sock',
                token_file='/run/secrets/hermes_rpc_token',server_uid=10003,
                isolation_policy_file='/etc/hermes-trading/isolation.json',**self.pins))
        path = self.base/'dialogue.json'
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path, references, value

    def test_service_binds_authenticated_request_and_reports_real_model_without_reading_root_key(self):
        with patch.object(d,'key_state',side_effect=AssertionError('Hermes cannot read root model key')), \
                patch.object(d,'key_identity',side_effect=AssertionError('Hermes cannot bind root model key')):
            result = self.engine.process(request())
            health = self.health()
        self.assertEqual(result['decision']['symbols'],['BTCUSDT'])
        self.assertEqual(self.calls[0]['request_id'],request()['request_id'])
        self.assertEqual(self.engine.backend,d.HERMES_BACKEND)
        self.assertEqual(health['model'],'gpt-6.1-sol')
        self.assertTrue(health['model_authentication_verified'])
        self.assertTrue(health['dialogue_ready'])
        self.assertNotIn('api_key',self.calls[0])

    def test_unverified_bridge_blocks_without_operation_budget_or_mini_fallback(self):
        self.available = False
        with patch.object(d,'OpenAIDialogue',side_effect=AssertionError('no Mini fallback')):
            with self.assertRaisesRegex(d.DialogueError,'MODEL_UNAVAILABLE'):
                self.engine.process(request())
        self.assertEqual(self.calls,[])
        self.assertEqual(self.memory.counts()['operations'],{})
        self.assertFalse(self.health()['dialogue_ready'])
        self.assertFalse(self.health()['model_authentication_verified'])

    def test_unknown_hermes_operation_never_uses_mini_screening_recovery(self):
        self.model.transport = Mock(side_effect=TimeoutError('injected transport ambiguity'))
        value = request()
        with self.assertRaisesRegex(d.DialogueError,'MODEL_UNAVAILABLE'):
            self.engine.process(value)
        self.engine._received_screening_child = Mock(side_effect=AssertionError('no Mini recovery'))
        self.engine._rejected_screening_child = Mock(side_effect=AssertionError('no Mini recovery'))
        with self.assertRaisesRegex(d.DialogueError,'OPERATION_UNKNOWN'):
            self.engine.process(value)
        self.assertEqual(self.model.transport.call_count,1)
        self.assertEqual(self.memory.counts()['operations'],{'UNKNOWN':2})

    def test_completed_historical_mini_receipt_stays_readable_without_relabel_or_rpc(self):
        value = request()
        decision = dict(action='SCREENING',symbols=['BTCUSDT'],question='')
        legacy = dict(schema_version=1,ok=True,
            **{key:value[key] for key in ('request_id','conversation_id','phase','reply_sha256')},
            authority='DIALOGUE_CONTROL_ONLY',trading_authority=False,decision=decision,
            needs_followup=False,reason='DIALOGUE_DECISION')
        self.memory.operation('delegated-model:'+value['request_id'],'DELEGATED_MODEL_DIALOGUE',value,
            lambda:legacy)
        before = self.memory.db.execute('SELECT * FROM operations').fetchall()
        self.assertEqual(self.engine.process(value),legacy)
        self.available = False
        lookup = self.engine.process(dict(kind=d.SCREENING_RECEIPT_LOOKUP,request=value))
        self.assertEqual(lookup['receipt']['result'],legacy)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.memory.db.execute('SELECT * FROM operations').fetchall(),before)

    def test_schema_three_explicit_selection_and_factory_have_no_implicit_fallback(self):
        path, references, value = self.configuration()
        observed, digest, backend = d.read_runtime_config(path,references)
        self.assertEqual(observed,value)
        self.assertEqual(backend,d.HERMES_BACKEND)
        self.assertEqual(digest,hashlib.sha256(path.read_bytes()).hexdigest())
        with self.assertRaises(d.DialogueError):
            d.read_runtime_config(path,references,backend=d.MINI_BACKEND)
        bridge = Mock(attestation=lambda:{},ready=lambda:False)
        constructor = Mock(return_value=bridge)
        module = SimpleNamespace(HermesRuntimeClient=constructor)
        with patch.dict(sys.modules,{'agent.hermes_runtime':module}), \
                patch.object(d,'OpenAIDialogue',side_effect=AssertionError('no Mini construction')):
            engine = d.create_dialogue_engine(self.memory,'/missing',backend=backend,config=value)
        constructor.assert_called_once_with(value,clock=time.time)
        self.assertIsInstance(engine.model,HermesDialogue)
        self.assertFalse(engine.model.runtime_ready())
        for field, bad in (('backend',d.MINI_BACKEND),('model','gpt-5-mini'),('schema_version',2)):
            changed = deepcopy(value)
            changed[field] = bad
            path.write_text(json.dumps(changed))
            with self.subTest(field=field),self.assertRaises(d.DialogueError):
                d.read_runtime_config(path,references)
        with self.assertRaises(d.DialogueError):
            d.create_dialogue_engine(self.memory,'/missing',backend=d.HERMES_BACKEND)

    def test_dedicated_rpc_paths_cannot_alias_control_token_or_native_socket(self):
        path, references, value = self.configuration()
        for field, alias in (('token_file','/run/secrets/./dialogue_token'),
                ('token_file','/run//secrets/openai_api_key'),
                ('socket_path','/run/harun-dialogue/./dialogue.sock')):
            changed = deepcopy(value)
            changed['hermes'][field] = alias
            path.write_text(json.dumps(changed))
            with self.subTest(field=field,alias=alias),self.assertRaises(d.DialogueError):
                d.read_runtime_config(path,references)

    def test_mini_does_not_receive_hermes_bind_request_hook(self):
        key = self.base/'model.key'
        key.write_text('unit-model-key-value-01234567890123456789')
        key.chmod(0o600)
        response = dict(status='completed',output=[dict(type='message',content=[dict(type='output_text',
            text=json.dumps(dict(action='SCREENING',symbols=['BTCUSDT'],question='')))])])
        model = OpenAIDialogue(key,self.memory,transport=lambda _:response)
        model.bind_request = Mock(side_effect=AssertionError('legacy model has no binding hook'))
        engine = d.DialogueEngine(self.memory,key,model=model)
        engine.process(request())
        model.bind_request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
