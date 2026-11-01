"""Read-only Binance USD-M user-data feed for existing-entry fill wakeups.

No entry or exit order can be sent by this transport. Its only REST operations
create/keep alive a listen key, and its WebSocket can send only control frames.
TLS certificate/hostname verification uses the normal system CA configuration.
The production controller owns activation; tests supply fake transports.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


REST_BASE = 'https://fapi.binance.com'
LISTEN_KEY_PATH = '/fapi/v1/listenKey'
STREAM_HOST = 'fstream.binance.com'
STREAM_PATH_PREFIX = '/private/ws/'
MAX_MESSAGE_BYTES = 1048576


class UserDataUnavailable(ValueError):
    pass


def _need(value, code):
    if not value:
        raise UserDataUnavailable(code)


def _listen_key(value):
    _need(type(value) is str and re.fullmatch(r'[A-Za-z0-9_-]{16,512}', value),
        'USER_DATA_LISTEN_KEY_INVALID')
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def listen_key_request(method, api_key):
    """Fixed-origin credential upkeep only; never an order endpoint."""
    _need(method in ('POST', 'PUT'), 'USER_DATA_METHOD_DENIED')
    _need(type(api_key) is str and 16 <= len(api_key) <= 512
        and all(33 <= ord(c) < 127 for c in api_key), 'USER_DATA_API_KEY_INVALID')
    try:
        request = Request(REST_BASE + LISTEN_KEY_PATH, data=b'', method=method,
            headers={'X-MBX-APIKEY': api_key, 'Accept': 'application/json',
                'User-Agent': 'harun-office-user-data/1.0'})
        try:
            response = build_opener(_NoRedirect()).open(request, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(8193)
            _need(response.status == 200 and len(raw) <= 8192, 'USER_DATA_TOKEN_UNAVAILABLE')
            value = json.loads(raw)
        _need(type(value) is dict, 'USER_DATA_TOKEN_UNAVAILABLE')
        if method == 'POST':
            return _listen_key(value.get('listenKey'))
        return None
    except UserDataUnavailable:
        raise
    except Exception:
        # Never chain a URL/header/body containing account credentials.
        raise UserDataUnavailable('USER_DATA_TOKEN_UNAVAILABLE') from None


class UserDataSocket:
    """Small RFC6455 receiver; client application-data sends are not exposed."""
    def __init__(self, sock):
        self.sock = sock
        self.buffer = bytearray()
        self.fragment = bytearray()
        self.fragment_active = False

    @classmethod
    def connect(cls, listen_key):
        key = _listen_key(listen_key)
        connection = raw = None
        try:
            raw = socket.create_connection((STREAM_HOST, 443), timeout=10)
            context = ssl.create_default_context()
            connection = context.wrap_socket(raw, server_hostname=STREAM_HOST)
            nonce = base64.b64encode(os.urandom(16)).decode()
            request = ('GET ' + STREAM_PATH_PREFIX + key + ' HTTP/1.1\r\nHost: ' + STREAM_HOST
                + '\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13'
                + '\r\nSec-WebSocket-Key: ' + nonce + '\r\n\r\n')
            connection.sendall(request.encode('ascii'))
            incoming = bytearray()
            while b'\r\n\r\n' not in incoming:
                chunk = connection.recv(4096)
                _need(chunk and len(incoming) + len(chunk) <= 32768, 'USER_DATA_HANDSHAKE_INVALID')
                incoming.extend(chunk)
            header, remainder = bytes(incoming).split(b'\r\n\r\n', 1)
            lines = header.decode('ascii').split('\r\n')
            _need(lines[0].split()[:2] == ['HTTP/1.1', '101'], 'USER_DATA_HANDSHAKE_INVALID')
            headers = {}
            for line in lines[1:]:
                name, value = line.split(':', 1)
                _need(name.lower() not in headers, 'USER_DATA_HANDSHAKE_INVALID')
                headers[name.lower()] = value.strip()
            expected = base64.b64encode(hashlib.sha1((nonce
                + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            _need(headers.get('sec-websocket-accept') == expected
                and headers.get('upgrade', '').lower() == 'websocket'
                and 'upgrade' in headers.get('connection', '').lower()
                and not headers.get('sec-websocket-extensions'), 'USER_DATA_HANDSHAKE_INVALID')
            connection.settimeout(1)
            result = cls(connection)
            result.buffer.extend(remainder)
            return result
        except Exception:
            if connection is not None:
                connection.close()
            elif raw is not None:
                raw.close()
            raise UserDataUnavailable('USER_DATA_CONNECT_UNAVAILABLE') from None

    def _control(self, opcode, payload=b''):
        _need(opcode in (8, 10) and len(payload) <= 125, 'USER_DATA_CONTROL_INVALID')
        mask = os.urandom(4)
        encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.sock.sendall(bytes((0x80 | opcode, 0x80 | len(payload))) + mask + encoded)

    def _frame(self):
        while True:
            if len(self.buffer) >= 2:
                first, second = self.buffer[0], self.buffer[1]
                _need(not first & 0x70 and not second & 0x80, 'USER_DATA_FRAME_INVALID')
                opcode, size, offset = first & 15, second & 127, 2
                if size == 126:
                    if len(self.buffer) < 4:
                        self._receive()
                        continue
                    size, offset = struct.unpack('!H', self.buffer[2:4])[0], 4
                elif size == 127:
                    if len(self.buffer) < 10:
                        self._receive()
                        continue
                    size, offset = struct.unpack('!Q', self.buffer[2:10])[0], 10
                _need(size <= MAX_MESSAGE_BYTES, 'USER_DATA_MESSAGE_TOO_LARGE')
                if opcode >= 8:
                    _need(first & 0x80 and size <= 125, 'USER_DATA_CONTROL_INVALID')
                if len(self.buffer) >= offset + size:
                    payload = bytes(self.buffer[offset:offset + size])
                    del self.buffer[:offset + size]
                    return bool(first & 0x80), opcode, payload
            self._receive()

    def _receive(self):
        chunk = self.sock.recv(65536)
        _need(chunk, 'USER_DATA_DISCONNECTED')
        self.buffer.extend(chunk)
        _need(len(self.buffer) <= MAX_MESSAGE_BYTES + 65546, 'USER_DATA_MESSAGE_TOO_LARGE')

    def receive(self):
        """Return one JSON event or None after timeout/control, retaining fragments."""
        try:
            final, opcode, payload = self._frame()
        except socket.timeout:
            return None
        if opcode == 8:
            raise UserDataUnavailable('USER_DATA_DISCONNECTED')
        if opcode == 9:
            self._control(10, payload)
            return None
        if opcode == 10:
            return None
        _need(opcode in (0, 1), 'USER_DATA_FRAME_INVALID')
        if opcode == 1:
            _need(not self.fragment_active, 'USER_DATA_FRAGMENT_INVALID')
            self.fragment_active = True
        else:
            _need(self.fragment_active, 'USER_DATA_FRAGMENT_INVALID')
        self.fragment.extend(payload)
        _need(len(self.fragment) <= MAX_MESSAGE_BYTES, 'USER_DATA_MESSAGE_TOO_LARGE')
        if not final:
            return None
        raw = bytes(self.fragment)
        self.fragment.clear()
        self.fragment_active = False
        try:
            event = json.loads(raw.decode('utf-8'))
        except Exception:
            raise UserDataUnavailable('USER_DATA_JSON_INVALID') from None
        _need(type(event) is dict, 'USER_DATA_JSON_INVALID')
        return event

    def close(self):
        self.sock.close()


class UserDataFeed:
    def __init__(self, api_key, on_event, on_recovery, *, token_request=listen_key_request,
            connect=UserDataSocket.connect, monotonic=time.monotonic, on_status=lambda code: None):
        self.api_key, self.on_event, self.on_recovery = api_key, on_event, on_recovery
        self.token_request, self.connect, self.monotonic = token_request, connect, monotonic
        self.on_status = on_status

    def run(self, stopping):
        delay = 1
        while not stopping.is_set():
            connection = None
            try:
                key = self.token_request('POST', self.api_key)
                connection = self.connect(key)
                self.on_status('USER_DATA_CONNECTED')
                self.on_recovery()
                delay = 1
                keepalive = self.monotonic() + 1800
                while not stopping.is_set():
                    if self.monotonic() >= keepalive:
                        self.token_request('PUT', self.api_key)
                        keepalive = self.monotonic() + 1800
                    event = connection.receive()
                    if event is None:
                        continue
                    if event.get('e') == 'listenKeyExpired':
                        raise UserDataUnavailable('USER_DATA_LISTEN_KEY_EXPIRED')
                    if event.get('e') == 'ORDER_TRADE_UPDATE':
                        self.on_event(event)
            except Exception:
                try:
                    self.on_status('USER_DATA_RECONCILIATION_REQUIRED')
                    self.on_recovery()
                except Exception:
                    pass  # The independent API recovery actor also remains live.
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            if stopping.wait(delay):
                break
            delay = min(30, delay * 2)
