import ast
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import stat
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent import dialogue_service as d
from agent.llm import OpenAIDialogue
from agent.memory import Memory


def request(*, phase='SCREENING', reply='BTCUSDT', index=0, conversation='native-cycle:one'):
    from tests.test_agent_native_market_context import native_context
    from agent.native_market_context import convert_native_market_context
    now = time.time()
    context = convert_native_market_context(native_context(now), now=now)
    return {'schema_version':1,'request_id':hashlib.sha256(str(index).encode()).hexdigest(),
        'conversation_id':conversation,'phase':phase,'reply':reply,
        'reply_sha256':hashlib.sha256(reply.encode()).hexdigest(),
        'market_context':None if phase=='SCREENING' else context,
        'gate':{'robot_on':True,'native_dispatch_verified':True},'deadline_epoch':time.time()+80}


class DialogueServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.memory=Memory(self.base/'agent.sqlite3')
        self.key=self.base/'model.key'
        self.key.write_text('unit-model-key-value-01234567890123456789')
        self.key.chmod(0o600)
        self.calls=[]
        self.output={'action':'SCREENING','symbols':['BTCUSDT'],'question':''}
        self.model=OpenAIDialogue(self.key,self.memory,transport=self.transport,
            daily_requests=math.inf,daily_input_tokens=math.inf,daily_output_tokens=math.inf)
        self.engine=d.DialogueEngine(self.memory,self.key,model=self.model)

    def tearDown(self):
        self.memory.close()
        self.temp.cleanup()

    def transport(self,payload):
        self.calls.append(payload)
        return {'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps(self.output)}]}]}

    def test_real_model_payload_and_success_never_have_tools_prices_or_trading_authority(self):
        value=self.engine.process(request())
        self.assertEqual(value['authority'],'DIALOGUE_CONTROL_ONLY')
        self.assertFalse(value['trading_authority'])
        self.assertFalse(value['needs_followup'])
        self.assertEqual(set(value['decision']),{'action','symbols','question'})
        payload=self.calls[0]
        self.assertEqual(payload['model'],'gpt-5-mini')
        self.assertFalse(payload['store'])
        self.assertNotIn('tools',payload)
        self.assertEqual(set(payload['text']['format']['schema']['properties']),{'action','symbols','question'})
        self.assertEqual(self.memory.counts()['operations'],{'DONE':1})

    def test_off_unverified_native_dispatch_and_invalid_context_have_zero_model_calls(self):
        values=[]
        value=request();value['gate']['robot_on']=False;values.append(value)
        value=request();value['gate']['native_dispatch_verified']=False;values.append(value)
        value=request(phase='ANALYSIS');value['market_context']=None;values.append(value)
        value=request(phase='ANALYSIS');value['market_context']['provenance']['origin']='https://invalid.test';values.append(value)
        for value in values:
            with self.assertRaises(d.DialogueError):self.engine.process(value)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.memory.counts()['operations'],{})

    def test_strict_hash_schema_json_deadline_and_body_validation(self):
        values=[]
        for key,wrong in [('request_id','invalid'),('reply_sha256','b'*64),('schema_version',True),
                          ('conversation_id','../foreign'),('phase','EXECUTE_ORDER'),
                          ('deadline_epoch',float('nan')),('deadline_epoch',time.time()+91)]:
            value=request();value[key]=wrong;values.append(value)
        value=request();value['extra']='wrong';values.append(value)
        value=request();value['gate']['extra']=True;values.append(value)
        value=request();value['reply']='a'*16001;value['reply_sha256']=hashlib.sha256(value['reply'].encode()).hexdigest();values.append(value)
        for value in values:
            with self.subTest(keys=list(value)),self.assertRaises((d.DialogueError,ValueError)):
                self.engine.process(value)
        with self.assertRaises(d.DialogueError):d.decode_json(b'{"a":1,"a":2}')
        with self.assertRaises(d.DialogueError):d.decode_json(b'{"a":NaN}')
        with self.assertRaises(d.DialogueError):d.decode_json(b' '*(d.MAX_BODY+1))
        self.assertEqual(self.calls,[])

    def test_non_screening_requires_realtime_futures_facts_in_both_timeframes(self):
        self.output={'action':'HOLD','symbols':['BTCUSDT'],'question':''}
        value=self.engine.process(request(phase='ANALYSIS',reply='BTCUSDT HOLD'))
        self.assertEqual(value['decision']['action'],'HOLD')
        text=self.calls[0]['input'][-1]['content']
        self.assertIn('BINANCE_FUTURES_PUBLIC',text)
        self.assertIn('"1h"',text)
        self.assertIn('"15m"',text)
        self.assertIn('101.2300',text)
        self.assertNotIn('101.2300',json.dumps(value))

    def test_full_400_candle_request_over_old_64k_limit_reaches_model_with_bounded_http_body(self):
        from agent.native_market_context import candle_digest
        value = request(phase='ANALYSIS', reply='BTCUSDT HOLD')
        context = value['market_context']
        for rows in context['candles'].values():
            for row in rows:
                row['volume'] = '12345678901234567890123456789012.123456789012345678901234'
        context['provenance']['candles_sha256'] = candle_digest(context['candles'])
        raw = d.canonical(value).encode()
        self.assertGreater(len(raw), 65536)
        self.assertLess(len(raw), d.MAX_BODY)
        wire = (b'POST /v1/dialogue HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n'
                b'Authorization: Bearer unit-token\r\nContent-Length: ' + str(len(raw)).encode() + b'\r\n\r\n' + raw)
        class Connection:
            def __init__(self, data): self.data = data
            def settimeout(self, _): pass
            def recv(self, count):
                result, self.data = self.data[:count], self.data[count:]
                return result
        received = d.http_request(Connection(wire), 'unit-token', deadline=time.monotonic()+10)
        self.assertEqual(received, value)
        self.output = {'action':'HOLD','symbols':['BTCUSDT'],'question':''}
        result = self.engine.process(received)
        self.assertEqual(result['decision']['action'], 'HOLD')
        transmitted = json.loads(self.calls[0]['input'][-1]['content'].rsplit('\n', 1)[1])
        self.assertEqual(transmitted['candles'], context['candles'])
        self.assertEqual(sum(map(len, transmitted['candles'].values())), 400)

    def test_explicit_request_clock_binds_both_deadline_and_market_freshness(self):
        from tests.test_agent_closed_candles_200 import native_context_200, NOW
        from agent.native_market_context import convert_native_market_context
        value = request(phase='ANALYSIS', reply='BTCUSDT HOLD')
        value['market_context'] = convert_native_market_context(native_context_200(NOW), now=NOW)
        value['deadline_epoch'] = NOW + 80
        self.assertIs(d.validate_request(value, now=NOW), value)
        value['deadline_epoch'] = NOW + 200 + 80
        with self.assertRaisesRegex(d.DialogueError, 'MARKET_CONTEXT_INVALID'):
            d.validate_request(value, now=NOW + 200)

    def test_done_replay_does_not_call_model_or_append_history(self):
        value=request()
        first=self.engine.process(value)
        counts=self.memory.counts()
        self.assertEqual(self.engine.process(value),first)
        self.assertEqual(self.calls.__len__(),1)
        self.assertEqual(self.memory.counts(),counts)

    def test_off_and_expired_gates_reject_even_done_replay_without_model(self):
        value=request()
        self.engine.process(value)
        off=copy.deepcopy(value);off['gate']['robot_on']=False
        with self.assertRaisesRegex(d.DialogueError,'ROBOT_OFF'):self.engine.process(off)
        self.engine.clock=lambda:value['deadline_epoch']+1
        with self.assertRaisesRegex(d.DialogueError,'DEADLINE_EXPIRED'):self.engine.process(value)
        self.assertEqual(len(self.calls),1)

    def test_request_id_reuse_with_changed_body_never_reaches_model(self):
        value=request();self.engine.process(value)
        value['reply']='ETHUSDT';value['reply_sha256']=hashlib.sha256(value['reply'].encode()).hexdigest()
        with self.assertRaisesRegex(d.DialogueError,'OPERATION_ID_MISMATCH'):self.engine.process(value)
        self.assertEqual(len(self.calls),1)

    def test_unknown_provider_outcome_persists_and_is_not_replayed_after_restart(self):
        def crash(payload):
            self.calls.append(payload)
            raise KeyboardInterrupt()
        self.model.transport=crash
        value=request()
        with self.assertRaises(KeyboardInterrupt):self.engine.process(value)
        self.assertEqual(self.memory.counts()['operations'],{'UNKNOWN':1})
        self.memory.close()
        self.memory=Memory(self.base/'agent.sqlite3')
        model=OpenAIDialogue(self.key,self.memory,transport=self.transport)
        self.engine=d.DialogueEngine(self.memory,self.key,model=model)
        with self.assertRaisesRegex(d.DialogueError,'OPERATION_UNKNOWN'):self.engine.process(value)
        self.assertEqual(len(self.calls),1)

    def test_missing_model_key_keeps_health_alive_but_not_ready_and_sends_nothing(self):
        self.key.unlink()
        with self.assertRaisesRegex(d.DialogueError,'MODEL_KEY_MISSING'):self.engine.process(request())
        health=object.__new__(d.DialogueHealth)
        health.dialogue=SimpleNamespace(engine=self.engine,runtime_ready=lambda:True)
        health.generation='a'*32
        value=health.observe()
        self.assertEqual(value['model_key_state'],'MISSING')
        self.assertEqual(value['state'],'BLOCKED')
        self.assertTrue(value['runtime_implemented'])
        self.assertFalse(value['dialogue_ready'])
        self.assertFalse(value['provider_contact_performed'])
        self.assertEqual(self.calls,[])

    def test_health_never_exports_unvalidated_environment_or_claims_model_authentication(self):
        health=object.__new__(d.DialogueHealth)
        health.dialogue=SimpleNamespace(engine=self.engine,runtime_ready=lambda:True)
        health.generation='a'*32
        with patch.dict(os.environ,{'HARUN_SOURCE_COMMIT':'PRIVATE_ENV_CANARY',
                                  'HARUN_IMAGE_DIGEST':'PRIVATE_ENV_CANARY'}):
            value=health.observe()
        self.assertNotIn('PRIVATE_ENV_CANARY',json.dumps(value))
        self.assertEqual(value['source_commit'],'UNVERIFIED')
        self.assertFalse(value['model_authentication_verified'])
        self.assertFalse(value['integration_verified'])
        self.assertTrue(value['health_observation_only'])
        self.assertEqual(self.calls,[])

    def test_health_authentication_evidence_is_bound_to_current_private_key_generation(self):
        health=object.__new__(d.DialogueHealth)
        health.dialogue=SimpleNamespace(engine=self.engine,runtime_ready=lambda:True)
        health.generation='a'*32
        self.engine.model.evidence=Mock(return_value={'verified':True,'observed_at':time.time()})
        self.engine._authenticated_key_identity=d.key_identity(self.key)
        self.assertTrue(health.observe()['model_authentication_verified'])
        self.key.write_text('rotated-unit-key-012345678901234567890')
        self.assertFalse(health.observe()['model_authentication_verified'])
        self.key.unlink()
        self.assertFalse(health.observe()['model_authentication_verified'])

    def test_dead_listener_reason_is_transport_unavailable_instead_of_key_available(self):
        health=object.__new__(d.DialogueHealth)
        health.dialogue=SimpleNamespace(engine=self.engine,runtime_ready=lambda:False)
        health.generation='a'*32
        value=health.observe()
        self.assertFalse(value['dialogue_ready'])
        self.assertEqual(value['reason'],'DIALOGUE_TRANSPORT_UNAVAILABLE')

    def test_optional_runtime_config_binds_exact_v2_bytes_model_and_cli_references(self):
        references={'socket':'/run/harun-dialogue/dialogue.sock','data_directory':'/data',
            'token_file':'/run/secrets/dialogue_token','key_file':'/run/secrets/openai_api_key'}
        value={'schema_version':2,'mode':'NATIVE_DELEGATED_DIALOGUE','model':'gpt-5-mini',
            'socket_path':references['socket'],'data_directory':references['data_directory'],
            'dialogue_token_file':references['token_file'],'openai_key_file':references['key_file']}
        config=self.base/'dialogue.json'
        raw=(json.dumps(value,sort_keys=True)+'\n').encode();config.write_bytes(raw);config.chmod(0o440)
        observed=d.runtime_config_sha256(config,references)
        self.assertEqual(observed,hashlib.sha256(raw).hexdigest())
        self.engine.configuration_check=lambda:d.runtime_config_sha256(config,references)==observed
        health=object.__new__(d.DialogueHealth)
        health.dialogue=SimpleNamespace(engine=self.engine,runtime_ready=lambda:True)
        health.generation='a'*32;health.config_sha256=observed
        self.assertEqual(health.observe()['runtime_config_sha256'],observed)
        config.chmod(0o600);config.write_bytes(raw+b' ');config.chmod(0o440)
        self.assertFalse(health.observe()['dialogue_ready'])
        self.assertEqual(health.observe()['runtime_config_sha256'],'UNVERIFIED')
        self.assertEqual(health.observe()['reason'],'RUNTIME_CONFIG_UNVERIFIED')
        with self.assertRaisesRegex(d.DialogueError,'DIALOGUE_UNAVAILABLE'):
            self.engine.process(request())
        self.assertEqual(self.calls,[])
        for key,wrong in (('schema_version',1),('model','other-model'),
            ('socket_path','/wrong.sock'),('dialogue_token_file','/wrong.token'),('private','PRIVATE_CANARY')):
            changed=dict(value);changed[key]=wrong
            config.chmod(0o600);config.write_text(json.dumps(changed));config.chmod(0o440)
            with self.subTest(field=key),self.assertRaises(d.DialogueError):
                d.runtime_config_sha256(config,references)

    def test_runtime_config_permission_and_symlink_refused(self):
        config=self.base/'config.json';config.write_text('{}');config.chmod(0o644)
        refs={'socket':'/run/harun-dialogue/dialogue.sock','data_directory':'/data',
            'token_file':'/run/secrets/dialogue_token','key_file':'/run/secrets/openai_api_key'}
        with self.assertRaises(d.DialogueError):d.runtime_config_sha256(config,refs)
        link=self.base/'config-link';link.symlink_to(config)
        with self.assertRaises((d.DialogueError,OSError)):d.runtime_config_sha256(link,refs)

    def test_production_engine_has_no_daily_business_cap_and_more_than_three_hold_rounds_persist(self):
        default=d.DialogueEngine(self.memory,self.key)
        self.assertTrue(all(math.isinf(value) for value in default.model.limits.values()))
        self.output={'action':'HOLD','symbols':['BTCUSDT'],'question':''}
        for index in range(9):
            value=self.engine.process(request(phase='ANALYSIS',reply='BTCUSDT HOLD',index=index))
            self.assertEqual(value['decision']['action'],'HOLD')
        self.assertEqual(len(self.calls),9)
        self.assertEqual(self.memory.counts()['operations'],{'DONE':9})
        history=self.memory.history('native-cycle:one')
        self.assertEqual(len(history),12)
        self.memory.close();self.memory=Memory(self.base/'agent.sqlite3')
        self.assertEqual(self.memory.counts()['operations'],{'DONE':9})

    def test_clarifications_continue_across_restart_without_a_round_ceiling(self):
        self.output={'action':'CLARIFY','symbols':[],'question':'Mohon lengkapi keputusan Neurobro?'}
        legacy_key='dialogue-clarification:'+hashlib.sha256(b'native-cycle:one\0ANALYSIS').hexdigest()
        self.memory.put(legacy_key, True)
        for index in range(8):
            if index == 4:
                self.memory.close();self.memory=Memory(self.base/'agent.sqlite3')
                self.model=OpenAIDialogue(self.key,self.memory,transport=self.transport,
                    daily_requests=math.inf,daily_input_tokens=math.inf,daily_output_tokens=math.inf)
                self.engine=d.DialogueEngine(self.memory,self.key,model=self.model)
            latest=request(phase='ANALYSIS',reply='BTCUSDT belum lengkap',index=index)
            value=self.engine.process(latest)
            self.assertTrue(value['needs_followup'])
            self.assertEqual(value['reason'],'DIALOGUE_DECISION')
        self.assertTrue(self.memory.get(legacy_key))
        self.assertEqual(self.memory.counts()['operations'],{'DONE':8})
        replay=self.engine.process(latest)
        self.assertEqual(replay,value)
        self.assertEqual(self.memory.counts()['operations'],{'DONE':8})

    def test_foreign_symbols_prices_and_question_foreign_symbols_are_rejected(self):
        cases=[{'action':'SCREENING','symbols':['ETHUSDT'],'question':''},
            {'action':'CLARIFY','symbols':[],'question':'Apakah ENTRY: 100 tepat?'},
            {'action':'CLARIFY','symbols':[],'question':'Apakah ETHUSDT lebih baik?'}]
        for index,output in enumerate(cases):
            self.output=output
            with self.subTest(output=output),self.assertRaises(d.DialogueError):
                self.engine.process(request(index=index))

    def test_explicit_cashtags_in_current_reply_authorize_canonical_screening_symbols(self):
        self.output={'action':'SCREENING','symbols':['ZECUSDT','HYPEUSDT'],'question':''}
        value=self.engine.process(request(reply='Prioritas **$ZEC**, dibandingkan ($HYPE).'))
        self.assertEqual(value['decision'],self.output)
        self.assertFalse(value['trading_authority'])

    def test_cashtag_attribution_keeps_existing_pair_forms_and_prose_punctuation(self):
        decision={'action':'SCREENING','symbols':['ZECUSDT'],'question':''}
        for reply in ('ZECUSDT','ZEC/USDT','ZEC-USDT','ZEC USDT','$ZEC/USDT',
                      '$ZEC','($ZEC),','**$ZEC**.','$ZEC.','$zec!','Pilihan: $ZEC;'):
            with self.subTest(reply=reply):
                self.assertIs(d.validate_decision(decision,{'phase':'SCREENING','reply':reply}),decision)

    def test_cashtags_reject_foreign_substrings_and_compound_tokens(self):
        decision={'action':'SCREENING','symbols':['ZECUSDT'],'question':''}
        for reply in ('$HYPE','ZEC','Zcash','$ZECO','$AZEC','$ZEC2','$ZEC_USDT',
                      '$ZEC/USDC','$ZEC/','$ZEC-USDC','$ZEC.USDC','$$ZEC',
                      'prefix$ZEC','é$ZEC','$ZECé','$ZEC\u0301','é\u0301$ZEC','/$ZEC','-$ZEC'):
            with self.subTest(reply=reply),self.assertRaisesRegex(d.DialogueError,'DIALOGUE_FOREIGN_SYMBOL'):
                d.validate_decision(decision,{'phase':'SCREENING','reply':reply})

    def test_cashtags_do_not_normalize_non_ascii_letters_into_canonical_bases(self):
        for symbol,reply in (('ICPUSDT','$ıcp'),('SOLUSDT','$ſol'),
                             ('FLOKIUSDT','$ﬂoki'),('SSUSDT','$ß')):
            decision={'action':'SCREENING','symbols':[symbol],'question':''}
            with self.subTest(reply=reply),self.assertRaisesRegex(d.DialogueError,'DIALOGUE_FOREIGN_SYMBOL'):
                d.validate_decision(decision,{'phase':'SCREENING','reply':reply})

    def test_question_symbol_requires_the_same_current_reply_cashtag(self):
        decision={'action':'CLARIFY','symbols':[],'question':'Mohon konfirmasi ZECUSDT?'}
        self.assertIs(d.validate_decision(decision,{'phase':'SCREENING','reply':'$ZEC'}),decision)
        with self.assertRaisesRegex(d.DialogueError,'DIALOGUE_FOREIGN_SYMBOL'):
            d.validate_decision(decision,{'phase':'SCREENING','reply':'$HYPE'})

    def test_cashtag_in_dialogue_history_does_not_authorize_a_foreign_current_symbol(self):
        self.output={'action':'SCREENING','symbols':['ZECUSDT'],'question':''}
        self.engine.process(request(reply='$ZEC',index=70))
        # The planner now rejects the history-only symbol before service validation.
        with self.assertRaisesRegex(d.DialogueError,'DIALOGUE_MODEL_UNAVAILABLE'):
            self.engine.process(request(reply='$HYPE',index=71))
        self.assertIn('$ZEC',json.dumps(self.calls[-1]))

    def test_complete_immediately_stops_discussion_and_pending_decision_has_no_order_authority(self):
        self.output={'action':'COMPLETE','symbols':['BTCUSDT'],'question':''}
        value=self.engine.process(request(phase='ANALYSIS',reply='BTCUSDT\nLONG\nENTRY:100\nTP:120\nSL:90'))
        self.assertFalse(value['needs_followup'])
        self.assertEqual(value['decision']['question'],'')
        self.assertNotIn('entry',value['decision'])
        self.output={'action':'BATALKAN','symbols':[],'question':''}
        value=self.engine.process(request(phase='PENDING_REVIEW',reply='KEPUTUSAN: BATALKAN',index=1))
        self.assertEqual(value['decision']['action'],'BATALKAN')
        self.assertFalse(value['trading_authority'])

    def test_complete_is_only_valid_during_analysis(self):
        decision={'action':'COMPLETE','symbols':['BTCUSDT'],'question':''}
        self.assertIs(d.validate_decision(decision,{'phase':'ANALYSIS','reply':'BTCUSDT'}),decision)
        for phase in ('SCREENING','PENDING_REVIEW'):
            with self.subTest(phase=phase),self.assertRaisesRegex(d.DialogueError,'DIALOGUE_MODEL_RESULT_INVALID'):
                d.validate_decision(decision,{'phase':phase,'reply':'BTCUSDT'})

    def test_gate_deadline_rechecked_after_budget_reservation_immediately_before_transport(self):
        value=request()
        now=[time.time()]
        self.engine.clock=lambda:now[0]
        original=self.memory.reserve_budget
        def reserve(*args,**kwargs):
            result=original(*args,**kwargs)
            now[0]=value['deadline_epoch']+1
            return result
        self.memory.reserve_budget=reserve
        with self.assertRaisesRegex(d.DialogueError,'DEADLINE_EXPIRED'):self.engine.process(value)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.memory.counts()['operations'],{'UNKNOWN':1})

    def test_token_owner_mode_hardlink_and_symlink_checks(self):
        token=self.base/'token';token.write_text('x'*48);token.chmod(0o600)
        self.assertEqual(d.private_secret(token),'x'*48)
        token.chmod(0o644)
        with self.assertRaises(d.DialogueError):d.private_secret(token)
        token.chmod(0o600);link=self.base/'token-link';link.symlink_to(token)
        with self.assertRaises((d.DialogueError,OSError)):d.private_secret(link)
        link.unlink();os.link(token,link)
        with self.assertRaises(d.DialogueError):d.private_secret(token)

    def test_http_absolute_read_deadline_cannot_be_reset_by_slow_drip(self):
        connection=Mock();connection.recv.return_value=b'P'
        with patch.object(d.time,'monotonic',side_effect=[0,0.5,1.1]):
            with self.assertRaisesRegex(d.DialogueError,'HTTP_TIMEOUT'):
                d.http_request(connection,'x'*48,deadline=1)
        self.assertEqual(connection.recv.call_count,2)

    def test_no_trading_scheduler_browser_gateway_or_financial_database_imports(self):
        source=Path(d.__file__).read_text();tree=ast.parse(source)
        imports=[node.module for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)]
        self.assertTrue(set(imports).isdisjoint({'controller','risk','bridge','browser_rpc','providers','worker'}))
        self.assertNotIn('order_gateway',source)
        self.assertNotIn('BackendBridge',source)
        self.assertNotIn('Controller(',source)
        self.assertNotIn('sqlite3.connect',source)
        self.assertEqual(d.OpenAIDialogue.endpoint,'https://api.openai.com/v1/responses')


if __name__=='__main__':unittest.main()
