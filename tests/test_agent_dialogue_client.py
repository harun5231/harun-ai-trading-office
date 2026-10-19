import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent import dialogue_client as c
from agent import dialogue_service as d
from agent.llm import OpenAIDialogue
from agent.memory import Memory


def request(index=0):
    text='BTCUSDT'
    return {'schema_version':1,'request_id':hashlib.sha256(str(index).encode()).hexdigest(),
        'conversation_id':'native-cycle:uds','phase':'SCREENING','reply':text,
        'reply_sha256':hashlib.sha256(text.encode()).hexdigest(),'market_context':None,
        'gate':{'robot_on':True,'native_dispatch_verified':True},'deadline_epoch':time.time()+80}


class DialogueClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='harun-dialogue-')
        self.base=Path(self.temp.name)
        self.socket=self.base/'dialogue.sock'
        self.token=self.base/'token';self.token.write_text('unit-dialogue-token-'+'x'*40);self.token.chmod(0o600)
        self.key=self.base/'model.key';self.key.write_text('unit-model-key-012345678901234567890');self.key.chmod(0o600)
        self.memory=Memory(self.base/'agent.sqlite3')
        self.calls=[]
        def transport(payload):
            self.calls.append(payload)
            return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text',
                'text':json.dumps({'action':'SCREENING','symbols':['BTCUSDT'],'question':''})}]}]}
        model=OpenAIDialogue(self.key,self.memory,transport=transport)
        self.engine=d.DialogueEngine(self.memory,self.key,model=model)
        # Production CLI fixes UIDs 10002/10001. These local-only constructors
        # let the actual Linux UDS protocol run under the workspace account.
        self.server=d.DialogueServer(self.socket,self.engine,self.token,
            server_uid=os.geteuid(),caller_uid=os.getegid()).start()
        self.client=c.DialogueClient(self.socket,self.token,
            caller_uid=os.geteuid(),server_uid=os.geteuid())

    def tearDown(self):
        self.server.close()
        self.memory.close()
        self.temp.cleanup()

    def raw(self,wire):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(3);connection.connect(str(self.socket));connection.sendall(wire)
            parts=[]
            while True:
                try:raw=connection.recv(4096)
                except ConnectionResetError:
                    if parts:break
                    raise
                if not raw:break
                parts.append(raw)
            raw=b''.join(parts)
        header,body=raw.split(b'\r\n\r\n',1)
        return header,json.loads(body)

    def test_actual_linux_uds_http_roundtrip_and_idempotent_replay(self):
        value=request()
        first=self.client.request(value)
        self.assertEqual(first['decision']['symbols'],['BTCUSDT'])
        self.assertEqual(self.client.request(value),first)
        self.assertEqual(len(self.calls),1)
        self.assertFalse(first['trading_authority'])
        self.assertEqual(stat.S_IMODE(self.socket.lstat().st_mode),0o660)

    def test_actual_uds_wrong_bearer_is_rejected_with_zero_model_calls(self):
        other=self.base/'wrong';other.write_text('wrong-dialogue-token-'+'y'*40);other.chmod(0o600)
        client=c.DialogueClient(self.socket,other,caller_uid=os.geteuid(),server_uid=os.geteuid())
        with self.assertRaisesRegex(d.DialogueError,'AUTHENTICATION_FAILED'):client.request(request())
        self.assertEqual(self.calls,[])

    def test_server_kernel_peer_uid_check_rejects_before_body_and_token(self):
        with patch.object(d,'peer_uid',return_value=os.geteuid()+1):
            header,value=self.raw(b'POST /v1/dialogue HTTP/1.1\r\n\r\n')
        self.assertIn(b'403',header)
        self.assertEqual(value['error'],'DIALOGUE_PEER_REFUSED')
        self.assertEqual(self.calls,[])

    def test_client_verifies_server_kernel_uid_without_exposing_transport_errors(self):
        with patch.object(c,'peer_uid',return_value=os.geteuid()+1):
            with self.assertRaisesRegex(d.DialogueError,'PEER_REFUSED'):self.client.request(request())
        self.assertEqual(self.calls,[])

    def test_off_and_expired_native_requests_do_not_reach_service_model(self):
        value=request();value['gate']['robot_on']=False
        with self.assertRaisesRegex(d.DialogueError,'ROBOT_OFF'):self.client.request(value)
        value=request();value['deadline_epoch']=time.time()-1
        with self.assertRaisesRegex(d.DialogueError,'DEADLINE_EXPIRED'):self.client.request(value)
        self.assertEqual(self.calls,[])

    def test_closed_http_protocol_rejects_oversize_duplicate_headers_and_foreign_path(self):
        token=self.token.read_text()
        head=('POST /v1/dialogue HTTP/1.1\r\nContent-Type: application/json\r\n'
            'Authorization: Bearer '+token+'\r\n')
        cases=[(head+'Content-Length: 65537\r\n\r\n').encode(),
            (head+'Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}').encode(),
            b'POST /orders HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}',
            (head+'Transfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n{}').encode()]
        for wire in cases:
            with self.subTest(wire=wire[:30]):
                _,value=self.raw(wire)
                self.assertFalse(value['ok'])
                self.assertIn(value['error'],d.PUBLIC_ERRORS)
        self.assertEqual(self.calls,[])

    def test_key_missing_over_actual_uds_is_classified_without_secret_or_model_contact(self):
        self.key.unlink()
        with self.assertRaisesRegex(d.DialogueError,'MODEL_KEY_MISSING'):self.client.request(request())
        self.assertEqual(self.calls,[])

    def test_socket_symlink_foreign_owner_and_missing_token_fail_without_raw_paths(self):
        for mutation in ('missing-token','symlink'):
            with self.subTest(mutation=mutation):
                if mutation=='missing-token':
                    client=c.DialogueClient(self.socket,self.base/'PRIVATE_PATH_CANARY',
                        caller_uid=os.geteuid(),server_uid=os.geteuid())
                else:
                    path=self.base/'link';path.symlink_to(self.socket)
                    client=c.DialogueClient(path,self.token,caller_uid=os.geteuid(),server_uid=os.geteuid())
                with self.assertRaises(d.DialogueError) as caught:client.request(request())
                self.assertNotIn('PRIVATE_PATH_CANARY',str(caught.exception))
        self.assertEqual(self.calls,[])

    def test_second_live_service_cannot_replace_existing_socket_or_break_first(self):
        before=self.socket.lstat().st_ino
        with self.assertRaises((d.DialogueError,BlockingIOError)):
            d.DialogueServer(self.socket,self.engine,self.token,
                server_uid=os.geteuid(),caller_uid=os.getegid())
        self.assertEqual(self.socket.lstat().st_ino,before)
        self.assertTrue(self.client.request(request())['ok'])

    def test_owned_dead_socket_can_resume_after_crash_without_manual_removal(self):
        self.server.close()
        dead=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        dead.bind(str(self.socket));self.socket.chmod(0o660);dead.close()
        self.engine=d.DialogueEngine(self.memory,self.key,model=self.engine.model)
        self.server=d.DialogueServer(self.socket,self.engine,self.token,
            server_uid=os.geteuid(),caller_uid=os.getegid()).start()
        self.assertTrue(self.client.request(request())['ok'])

    def test_health_stops_claiming_ready_when_token_socket_or_lock_becomes_unusable(self):
        health=object.__new__(d.DialogueHealth)
        health.dialogue=self.server;health.generation='a'*32
        self.assertTrue(health.observe()['dialogue_ready'])
        self.token.chmod(0o644)
        self.assertFalse(health.observe()['dialogue_ready'])
        self.token.chmod(0o600)
        self.assertTrue(health.observe()['dialogue_ready'])
        self.socket.chmod(0o600)
        self.assertFalse(health.observe()['dialogue_ready'])
        self.socket.chmod(0o660)
        self.server.lock_path.chmod(0o644)
        self.assertFalse(health.observe()['dialogue_ready'])
        self.server.lock_path.chmod(0o600)
        self.token.unlink()
        self.assertFalse(health.observe()['dialogue_ready'])

    def test_real_sigterm_during_stalled_local_transport_exits_bounded_and_preserves_ambiguous_receipt(self):
        from agent.models import AmbiguousOperation
        import threading
        import signal
        directory=self.base/'inflight';directory.mkdir(mode=0o700)
        data=directory/'data';data.mkdir(mode=0o700)
        token=directory/'token';token.write_text('unit-inflight-token-'+'x'*40);token.chmod(0o600)
        key=directory/'key';key.write_text('unit-model-key-012345678901234567890');key.chmod(0o600)
        source='''import os,signal,sys,threading,time
from pathlib import Path
from agent.dialogue_service import DialogueEngine,DialogueServer
from agent.llm import OpenAIDialogue
from agent.memory import Memory
base=Path(sys.argv[1]);memory=Memory(base/'data/agent.sqlite3')
def stalled(payload):
    (base/'dispatch').write_text('started')
    time.sleep(120)
model=OpenAIDialogue(base/'key',memory,transport=stalled)
engine=DialogueEngine(memory,base/'key',model=model)
server=DialogueServer(base/'inflight.sock',engine,base/'token',server_uid=os.geteuid(),caller_uid=os.getegid()).start()
stop=threading.Event()
signal.signal(signal.SIGTERM,lambda *_:stop.set())
(base/'ready').write_text('ready')
stop.wait()
server.close();memory.close()
'''
        child=subprocess.Popen([sys.executable,'-c',source,str(directory)],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        failure=[]
        value=request(999)
        def invoke():
            try:
                c.DialogueClient(directory/'inflight.sock',token,
                    caller_uid=os.geteuid(),server_uid=os.geteuid()).request(value)
            except d.DialogueError as error:failure.append(str(error))
        caller=None
        try:
            limit=time.monotonic()+5
            while not (directory/'ready').exists():
                self.assertIsNone(child.poll());self.assertLess(time.monotonic(),limit);time.sleep(0.02)
            caller=threading.Thread(target=invoke,daemon=True);caller.start()
            while not (directory/'dispatch').exists():
                self.assertIsNone(child.poll());self.assertLess(time.monotonic(),limit);time.sleep(0.02)
            start=time.monotonic();child.send_signal(signal.SIGTERM)
            self.assertEqual(child.wait(timeout=5),0)
            self.assertLess(time.monotonic()-start,4)
            caller.join(timeout=3)
            self.assertTrue(failure)
            receipt=Memory(data/'agent.sqlite3')
            try:
                self.assertEqual(receipt.counts()['operations'],{'STARTED':1})
                with self.assertRaises(AmbiguousOperation):
                    receipt.operation('delegated-model:'+value['request_id'],
                        'DELEGATED_MODEL_DIALOGUE',value,lambda:self.fail('must not replay'))
            finally:receipt.close()
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=5)
            if caller is not None:caller.join(timeout=3)


if __name__=='__main__':unittest.main()
