import base64
import hashlib
import json
import socket
import struct
import threading
import unittest
from unittest.mock import patch

from worker.fill_stream import UserDataFeed, UserDataSocket, UserDataUnavailable, listen_key_request


def frame(payload,opcode=1,final=True):
    if isinstance(payload,str):payload=payload.encode()
    header=bytes(((128 if final else 0)|opcode,))
    if len(payload)<126:return header+bytes((len(payload),))+payload
    if len(payload)<65536:return header+bytes((126,))+struct.pack('!H',len(payload))+payload
    return header+bytes((127,))+struct.pack('!Q',len(payload))+payload


class FakeSocket:
    def __init__(self,chunks):self.chunks=list(chunks);self.sent=[];self.closed=False
    def recv(self,size):
        if not self.chunks:raise socket.timeout()
        value=self.chunks.pop(0)
        if isinstance(value,Exception):raise value
        if len(value)>size:self.chunks.insert(0,value[size:]);value=value[:size]
        return value
    def sendall(self,value):self.sent.append(value)
    def close(self):self.closed=True
    def settimeout(self,value):self.timeout=value


class FillStreamTests(unittest.TestCase):
    def test_json_frame_reaches_handler_without_application_writes(self):
        sock=FakeSocket([frame('{"e":"ORDER_TRADE_UPDATE"}')])
        reader=UserDataSocket(sock)
        self.assertEqual(reader.receive(),{'e':'ORDER_TRADE_UPDATE'})
        self.assertEqual(sock.sent,[])

    def test_fragmented_json_and_timeout_keep_buffer(self):
        first=frame('{"e":"ORDER_',final=False)
        second=frame('TRADE_UPDATE"}',opcode=0)
        sock=FakeSocket([first[:1],socket.timeout(),first[1:],second])
        reader=UserDataSocket(sock)
        self.assertIsNone(reader.receive())
        self.assertIsNone(reader.receive())
        self.assertEqual(reader.receive(),{'e':'ORDER_TRADE_UPDATE'})

    def test_ping_has_exact_masked_pong_and_cannot_send_orders(self):
        sock=FakeSocket([frame(b'alive',opcode=9)])
        reader=UserDataSocket(sock)
        self.assertIsNone(reader.receive())
        wire=sock.sent[0]
        self.assertEqual(wire[0],0x8a)
        self.assertEqual(wire[1],0x85)
        self.assertEqual(bytes(v^wire[2+i%4]for i,v in enumerate(wire[6:])),b'alive')
        with self.assertRaisesRegex(UserDataUnavailable,'USER_DATA_CONTROL_INVALID'):
            reader._control(1,b'ORDER')

    def test_invalid_masked_or_large_or_binary_frames_are_refused(self):
        for wire in [b'\x81\x81'+b'0000x',b'\x82\x00',b'\x81\x7f'+struct.pack('!Q',1048577),b'\x09\x00']:
            with self.subTest(wire=wire),self.assertRaises(UserDataUnavailable):
                UserDataSocket(FakeSocket([wire])).receive()

    def test_handshake_checks_accept_and_tls_hostname(self):
        class Handshake(FakeSocket):
            def recv(self,size):
                nonce=self.sent[0].decode().split('Sec-WebSocket-Key: ')[1].split('\r\n')[0]
                accept=base64.b64encode(hashlib.sha1((nonce+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                return ('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+'\r\n\r\n').encode()
        sock=Handshake([])
        with patch('worker.fill_stream.socket.create_connection',return_value=sock)as opened, \
                patch('worker.fill_stream.ssl.create_default_context')as context:
            context.return_value.wrap_socket.return_value=sock
            result=UserDataSocket.connect('listen-key-example-only')
            context.assert_called_once_with()
            context.return_value.wrap_socket.assert_called_once_with(sock,server_hostname='fstream.binance.com')
            opened.assert_called_once_with(('fstream.binance.com',443),timeout=10)
            self.assertIs(result.sock,sock)
            self.assertTrue(sock.sent[0].startswith(b'GET /private/ws/listen-key-example-only HTTP/1.1\r\n'))
            self.assertIn(b'Upgrade: websocket',sock.sent[0])
            self.assertNotIn(b'X-MBX-APIKEY',sock.sent[0])

    def test_token_transport_is_fixed_and_never_uses_order_routes(self):
        class Response:
            status=200
            def __init__(self,value):self.value=value
            def read(self,size):return json.dumps(self.value).encode()
            def __enter__(self):return self
            def __exit__(self,*args):pass
        with patch('worker.fill_stream.build_opener')as opener:
            opener.return_value.open.side_effect=[Response({'listenKey':'listen-key-example-only'}),Response({})]
            self.assertEqual(listen_key_request('POST','not-a-real-api-key'),'listen-key-example-only')
            self.assertIsNone(listen_key_request('PUT','not-a-real-api-key'))
            for call in opener.return_value.open.call_args_list:
                request=call.args[0]
                self.assertEqual(request.full_url,'https://fapi.binance.com/fapi/v1/listenKey')
                self.assertIn(request.method,('POST','PUT'))
                self.assertNotIn('signature',request.full_url)
            with self.assertRaises(UserDataUnavailable):listen_key_request('DELETE','not-a-real-api-key')

    def test_token_errors_never_expose_key(self):
        with patch('worker.fill_stream.build_opener',side_effect=ValueError('private-header-value')):
            with self.assertRaisesRegex(UserDataUnavailable,'^USER_DATA_TOKEN_UNAVAILABLE$')as failed:
                listen_key_request('POST','not-a-real-api-key')
        self.assertNotIn('private-header-value',str(failed.exception))

    def test_reconnection_requests_api_reconciliation_and_preserves_events(self):
        stop=threading.Event();events=[];recoveries=[];statuses=[]
        class Connection:
            closed=False
            def __init__(self,values):self.values=iter(values)
            def receive(self):
                value=next(self.values)
                if isinstance(value,Exception):raise value
                return value
            def close(self):self.closed=True
        connections=[Connection([{'e':'listenKeyExpired'}]),Connection([{'e':'ORDER_TRADE_UPDATE','o':{}}])]
        waits=[]
        class Stop:
            def is_set(self):return stop.is_set()
            def wait(self,delay):waits.append(delay);return stop.is_set()
        def event(value):events.append(value);stop.set()
        feed=UserDataFeed('not-a-real-api-key',event,lambda:recoveries.append('api'),
            token_request=lambda method,key:'listen-key-example-only',
            connect=lambda key:connections.pop(0),monotonic=lambda:0,on_status=statuses.append)
        feed.run(Stop())
        self.assertEqual(events,[{'e':'ORDER_TRADE_UPDATE','o':{}}])
        self.assertGreaterEqual(len(recoveries),3)
        self.assertIn('USER_DATA_RECONCILIATION_REQUIRED',statuses)
        self.assertTrue(all(delay>=1 for delay in waits))

    def test_keepalive_happens_without_waiting_for_analysis(self):
        stop=threading.Event();tokens=[];clock=iter([0,1801,1801])
        class Connection:
            def receive(self):stop.set();return None
            def close(self):pass
        def token(method,key):tokens.append(method);return'listen-key-example-only'
        UserDataFeed('not-a-real-api-key',lambda event:None,lambda:None,
            token_request=token,connect=lambda key:Connection(),monotonic=lambda:next(clock)).run(stop)
        self.assertEqual(tokens,['POST','PUT'])


if __name__=='__main__':unittest.main()
