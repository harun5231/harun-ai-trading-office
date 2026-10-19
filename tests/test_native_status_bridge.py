import ast
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent import native_status_bridge as s

ROOT=Path(__file__).resolve().parents[1]
PINS={'source_commit':'a'*40,'image':'ghcr.io/harun5231/harun-neurobro-agent@sha256:'+'b'*64,
      'runtime_config_sha256':'c'*64}
NOW=1792410000


def health():
    return {'service':s.SERVICE,'runtime_implemented':True,'mode':s.MODE,'model':'gpt-5-mini',
        'model_key_state':'AVAILABLE','state':'PAUSED','dialogue_ready':True,'reason':'NATIVE_CALLER_GATED',
        'uid':10002,'process_generation':'1'*32,'observed_at':NOW,
        'source_commit':PINS['source_commit'],'image_digest':PINS['image'],
        'runtime_config_sha256':PINS['runtime_config_sha256'],'integration_verified':False,
        'native_coordinator_preserved':True,'model_authentication_verified':False,
        'dialogue_transport_verified':True,'health_observation_only':True,
        'provider_contact_performed':False,'trading_authority':False}


def browser():
    return {'provider':'NEUROBRO_WEB','status':'CONNECTED','profile_persistent':True,
            'checked_at':datetime.fromtimestamp(NOW,timezone.utc).isoformat()}


class StatusSanitizerTests(unittest.TestCase):
    def test_delegated_liveness_is_visible_but_never_claims_backend_model_or_integration_verified(self):
        value=s.sanitize_delegated_health(health(),expected_release=PINS,browser=browser(),now=NOW)
        self.assertEqual(value['status'],'AVAILABLE')
        self.assertEqual(value['state'],'PAUSED')
        self.assertFalse(value['integration_verified'])
        self.assertTrue(value['dependencies']['browser']['verified'])
        self.assertFalse(value['dependencies']['model']['verified'])
        self.assertFalse(value['dependencies']['backend']['verified'])
        self.assertEqual(set(value),set(s.unavailable()))

    def test_current_authenticated_model_observation_remains_distinct_from_full_integration(self):
        raw=health();raw['model_authentication_verified']=True
        value=s.sanitize_delegated_health(raw,expected_release=PINS,browser=browser(),now=NOW)
        self.assertTrue(value['dependencies']['model']['verified'])
        self.assertFalse(value['integration_verified'])
        raw.update(state='BLOCKED',dialogue_ready=False,model_key_state='MISSING',reason='MODEL_KEY_MISSING')
        value=s.sanitize_delegated_health(raw,expected_release=PINS,now=NOW)
        self.assertEqual(value['state'],'BLOCKED')
        self.assertFalse(value['dependencies']['model']['verified'])

    def test_mismatched_stale_false_authority_and_private_fields_never_export(self):
        for field,value in [('source_commit','d'*40),('image_digest',PINS['image'][:-1]+'e'),
            ('runtime_config_sha256','f'*64),('observed_at',NOW-60),('uid',0),
            ('trading_authority',True),('provider_contact_performed',True),
            ('integration_verified',True),('process_generation','0'*32)]:
            raw=health();raw[field]=value
            with self.subTest(field=field):
                self.assertEqual(s.sanitize_delegated_health(raw,expected_release=PINS,now=NOW),s.unavailable())
        raw={**health(),'api_key':'PRIVATE_KEY_CANARY','prompt':'PRIVATE_PROMPT_CANARY'}
        value=s.sanitize_delegated_health(raw,expected_release=PINS,now=NOW)
        self.assertNotIn('PRIVATE_',json.dumps(value))
        self.assertNotIn('PRIVATE_',json.dumps(s._public_health(raw)))

    def test_binding_is_independent_root_readonly_singlelink_file_with_stable_fd(self):
        raw=s._canonical(PINS)
        info=SimpleNamespace(st_dev=1,st_ino=2,st_mode=stat.S_IFREG|0o440,st_uid=0,st_gid=10001,
            st_nlink=1,st_size=len(raw),st_mtime_ns=3,st_ctime_ns=4)
        parent=SimpleNamespace(st_mode=stat.S_IFDIR|0o755,st_uid=0)
        with patch.object(s.os,'open',return_value=12) as opened, \
             patch.object(s.os,'fstat',return_value=info),patch.object(s.os,'read',return_value=raw), \
             patch.object(s.os,'close'),patch.object(Path,'lstat',lambda p:info if str(p)==s.RELEASE_BINDING else parent):
            self.assertEqual(s.read_release_binding(),PINS)
            self.assertTrue(opened.call_args.args[1]&os.O_NOFOLLOW)
            info.st_nlink=2
            with self.assertRaises(s.StatusUnavailable):s.read_release_binding()

    def test_actor_status_adds_optional_agent_only_after_native_status_returns(self):
        original=browser()
        calls=[]
        class Actor:
            def status(self):
                calls.append('native')
                return original
        client=Mock()
        client.observe.side_effect=lambda **kwargs:(calls.append('agent') or s.unavailable())
        namespace={'BrowserActor':Actor}
        s.install_actor_status(namespace,binding_loader=lambda:PINS,client=client)
        s.install_actor_status(namespace,binding_loader=lambda:PINS,client=client)
        observed=Actor().status()
        self.assertEqual(calls,['native','agent'])
        self.assertEqual({k:v for k,v in observed.items() if k!='agent'},original)
        self.assertNotIn('agent',original)
        client.observe.assert_called_once_with(expected_release=PINS,browser=original)
        client.observe.side_effect=RuntimeError('PRIVATE_FAILURE')
        self.assertEqual(Actor().status()['agent'],s.unavailable())

    def test_worker_optional_cache_is_closed_fresh_and_does_not_retain_native_secret_fields(self):
        clock=[NOW]
        cache=s.AgentStatusCache(clock=lambda:clock[0])
        value=s.sanitize_delegated_health(health(),expected_release=PINS,browser=browser(),now=NOW)
        cache.record(value)
        observed=cache.read()
        self.assertEqual(observed,value)
        observed['dependencies']['browser']['verified']=False
        self.assertTrue(cache.read()['dependencies']['browser']['verified'])
        cache.record({**value,'api_key':'PRIVATE_CANARY'})
        self.assertEqual(cache.read(),s.unavailable())
        cache.record(value)
        clock[0]+=60
        self.assertEqual(cache.read(),s.unavailable())

    def test_office_snapshot_preserves_native_account_risk_research_and_robot_objects(self):
        robot={'robot_on':False,'risk_target_usdt':'5'}
        account={'status':'CONNECTED'}
        snapshot={'robot':robot,'account':account,'research':browser()}
        control=SimpleNamespace(native_agent_status=Mock(return_value=s.sanitize_delegated_health(
            health(),expected_release=PINS,browser=browser(),now=NOW)))
        original=Mock(side_effect=lambda controller,result:result)
        namespace={'office_snapshot':original}
        s.install_office_status(namespace,control=control)
        with patch.object(s.time,'time',return_value=NOW):
            value=namespace['office_snapshot'](object(),snapshot)
        self.assertIs(value['robot'],robot)
        self.assertIs(value['account'],account)
        self.assertEqual(value['research'],browser())
        self.assertNotIn('agent',snapshot)
        self.assertEqual(value['agent']['status'],'AVAILABLE')
        control.native_agent_status.assert_called_once_with()
        control.native_agent_status.side_effect=RuntimeError('PRIVATE_CANARY')
        self.assertEqual(namespace['office_snapshot'](object(),snapshot)['agent'],s.unavailable())

    def test_control_default_capture_preserves_native_return_and_one_original_request(self):
        agent=s.sanitize_delegated_health(health(),expected_release=PINS,browser=browser(),now=NOW)
        raw={**browser(),'agent':agent,'secret':'PRIVATE_CANARY'}
        transport=Mock(return_value=raw)
        native=browser()
        def session_status(transport=transport):
            transport('GET','native-control-route')
            return native
        login_grant=Mock()
        namespace={'session_status':session_status,'helper_control':transport,
                   'SESSION_STATES':frozenset({'CONNECTED','UNAVAILABLE'}),'login_grant':login_grant}
        cache=s.AgentStatusCache(clock=lambda:NOW)
        s.install_control_status(namespace,cache=cache)
        s.install_control_status(namespace,cache=cache)
        value=namespace['session_status']()
        self.assertIs(value,native)
        transport.assert_called_once_with('GET','native-control-route')
        self.assertIs(namespace['login_grant'],login_grant)
        self.assertEqual(namespace['native_agent_status'](),agent)
        self.assertNotIn('PRIVATE_CANARY',json.dumps(cache.value))
        self.assertEqual(raw['secret'],'PRIVATE_CANARY')
        alternate=Mock(return_value=raw)
        namespace['session_status'](alternate)
        alternate.assert_called_once_with('GET','native-control-route')
        self.assertEqual(transport.call_count,1)

    def test_control_native_rejection_or_exception_discards_optional_cache_without_altering_failure(self):
        agent=s.sanitize_delegated_health(health(),expected_release=PINS,now=NOW)
        transport=Mock(return_value={**browser(),'agent':agent})
        def session_status(transport=transport):
            transport('GET','native-control-route')
            return {'provider':'NEUROBRO_WEB','status':'UNAVAILABLE','checked_at':None,'profile_persistent':False}
        namespace={'session_status':session_status,'SESSION_STATES':frozenset({'CONNECTED','UNAVAILABLE'})}
        cache=s.AgentStatusCache(clock=lambda:NOW)
        cache.record(agent)
        s.install_control_status(namespace,cache=cache)
        self.assertEqual(namespace['session_status']()['status'],'UNAVAILABLE')
        self.assertEqual(namespace['native_agent_status'](),s.unavailable())
        transport.side_effect=RuntimeError('PRIVATE_FAILURE')
        with self.assertRaisesRegex(RuntimeError,'PRIVATE_FAILURE'):namespace['session_status']()
        self.assertEqual(namespace['native_agent_status'](),s.unavailable())

    def test_running_main_namespace_explicitly_installs_status_without_reimporting_service(self):
        tree=ast.parse((ROOT/'agent/dialogue_service.py').read_bytes())
        self.assertIsInstance(tree.body[-1],ast.If)
        tree.body.pop()  # Load only public definitions with the real -m namespace.
        namespace={'__name__':'__main__','__package__':'agent'}
        exec(compile(tree,'<public-dialogue-cli-main>','exec'),namespace)
        original_error=namespace['DialogueError']
        events=[]
        class Memory:
            def __init__(self,path):events.append('memory')
            def close(self):events.append('memory_close')
        class Engine:
            def __init__(self,*args):events.append('engine')
        class Server:
            def __init__(self,*args):
                self.server=SimpleNamespace(RequestHandlerClass=type('Handler',(),{'handle':lambda self:None}))
                events.append('server')
            def start(self):return self
            def close(self):events.append('server_close')
        class Health:
            def __init__(self,dialogue,**kwargs):events.append('health')
            def observe(self):return health()
            def start(self):return self
            def close(self):events.append('health_close')
        class Event:
            def wait(self):events.append('wait')
            def set(self):pass
        namespace.update(Memory=Memory,DialogueEngine=Engine,DialogueServer=Server,DialogueHealth=Health)
        with patch.object(namespace['os'],'geteuid',return_value=10002), \
             patch.object(namespace['threading'],'Event',Event),patch.object(namespace['signal'],'signal'):
            result=namespace['main'](['--socket',s.SOCKET,'--data-directory','/data',
                '--token-file',s.TOKEN,'--key-file','/run/secrets/openai_api_key'])
        self.assertEqual(result,0)
        self.assertTrue(namespace['_native_status_protocol_installed'])
        self.assertIs(namespace['DialogueError'],original_error)
        self.assertEqual(events,['memory','engine','server','health','wait',
                                 'health_close','server_close','memory_close'])


class ActualStatusWireTests(unittest.TestCase):
    def setUp(self):
        # Only the public released service definitions are executed. Private VPS
        # sources remain AST-only evidence and are never imported by these tests.
        self.namespace={'__name__':'agent.status_test_runtime','__package__':'agent'}
        exec(compile((ROOT/'agent/dialogue_service.py').read_bytes(),'<public-dialogue-service>','exec'),self.namespace)
        s.install_dialogue_status(self.namespace)
        self.temp=tempfile.TemporaryDirectory(prefix='native-status-')
        base=Path(self.temp.name)
        self.path=base/'dialogue.sock';self.token=base/'token'
        self.token.write_text('native-status-test-token-'+'x'*40);self.token.chmod(0o600)
        self.engine=SimpleNamespace(process=Mock(return_value={'original_post':True}),shutdown=lambda:None,
                                    stopping=threading.Event())
        self.server=self.namespace['DialogueServer'](self.path,self.engine,self.token,
            server_uid=os.geteuid(),caller_uid=os.getegid()).start()
        self.server._native_status_observer=Mock(side_effect=health)
        self.client=s.StatusClient(self.path,self.token,caller_uid=os.geteuid(),server_uid=os.geteuid())

    def tearDown(self):
        self.server.close();self.temp.cleanup()

    def raw(self,method='GET',path='/v1/status',token=None,extra='',body=b''):
        token=self.token.read_text() if token is None else token
        wire=(method+' '+path+' HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '+token+'\r\n'+extra+'\r\n').encode()+body
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as channel:
            channel.settimeout(3);channel.connect(str(self.path));channel.sendall(wire)
            result=b''
            while True:
                try:part=channel.recv(4096)
                except ConnectionResetError:break
                if not part:break
                result+=part
        return result

    def test_actual_authenticated_uds_get_status_while_off_has_zero_engine_model_calls(self):
        value=self.client.observe(expected_release=PINS,browser=browser(),now=NOW)
        self.assertEqual(value['status'],'AVAILABLE')
        self.assertTrue(value['dependencies']['browser']['verified'])
        self.server._native_status_observer.assert_called_once_with()
        self.engine.process.assert_not_called()

    def test_wrong_token_wrong_peer_and_unknown_path_are_denied_without_health_or_model_call(self):
        for kwargs in ({'token':'wrong-'+'x'*40},{'path':'/v1/status?secret=x'},
                       {'extra':'Transfer-Encoding: chunked\r\n'},
                       {'extra':'Authorization: Bearer private-extra\r\n'}):
            with self.subTest(kwargs=kwargs):
                value=self.raw(**kwargs)
                self.assertIn(b'503 Rejected',value)
                self.assertNotIn(b'private-extra',value)
        self.server.caller_uid=os.geteuid()+1
        self.assertIn(b'503 Rejected',self.raw())
        self.server._native_status_observer.assert_not_called()
        self.engine.process.assert_not_called()

    def test_original_post_parser_and_engine_dispatch_are_preserved(self):
        result=self.raw(method='POST',path='/v1/dialogue',
            extra='Content-Type: application/json\r\nContent-Length: 2\r\n',body=b'{}')
        self.assertIn(b'200 OK',result)
        self.assertIn(b'"original_post":true',result)
        self.engine.process.assert_called_once_with({})
        self.server._native_status_observer.assert_not_called()
        self.engine.process.reset_mock()
        result=self.raw(method='POST',path='/v1/dialogue',token='wrong-'+'x'*40,
            extra='Content-Type: application/json\r\nContent-Length: 2\r\n',body=b'{}')
        self.assertIn(b'403 Rejected',result)
        self.engine.process.assert_not_called()

    def test_unready_or_exceptional_observer_is_unavailable_and_never_exports_raw_exception(self):
        self.server._native_status_observer.side_effect=RuntimeError('PRIVATE_CREDENTIAL_CANARY')
        self.assertEqual(self.client.observe(expected_release=PINS,now=NOW),s.unavailable())
        raw=self.raw()
        self.assertNotIn(b'PRIVATE_',raw)
        self.assertIn(b'NATIVE_AGENT_STATUS_UNAVAILABLE',raw)
        self.engine.process.assert_not_called()

    def test_socket_mode_and_peer_authentication_fail_closed(self):
        self.path.chmod(0o666)
        self.assertEqual(self.client.observe(expected_release=PINS,now=NOW),s.unavailable())
        self.server._native_status_observer.assert_not_called()
        self.engine.process.assert_not_called()

    def test_health_constructor_registers_observer_and_install_is_idempotent(self):
        cls=self.namespace['DialogueHealth']
        local=cls(self.server,host='127.0.0.1',port=0)
        try:
            self.assertEqual(self.server._native_status_observer,local.observe)
            initialized=self.namespace['DialogueServer'].__init__
            s.install_dialogue_status(self.namespace)
            self.assertIs(self.namespace['DialogueServer'].__init__,initialized)
        finally:
            local.close()


if __name__=='__main__':unittest.main()
