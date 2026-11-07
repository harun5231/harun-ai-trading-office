"""A credential-owning HTTPS model transport for the isolated Hermes SDK.

The worker speaks ordinary OpenAI-compatible HTTP over a Unix socket using a
dummy API key. This process has the real model credential and one sealed HTTPS
base/model. It neither imports Hermes nor supplies tools, retries, or fallbacks.
SSE is bounded and buffered until the provider's model has been verified; the
original finite event stream is then returned to the SDK without alteration.
"""
import argparse
from dataclasses import dataclass
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import socketserver
import ssl
import stat
import struct
import threading
import time
from urllib.parse import urlsplit


MAX_BODY = 2 * 1024 * 1024
MAX_HEADERS = 16 * 1024
MAX_EVENTS = 4096
ALLOWED_PATHS = frozenset({'/v1/chat/completions', '/v1/responses'})
REQUEST_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z', re.ASCII)
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z', re.ASCII)
CHAT_FIELDS = frozenset({'model', 'messages', 'stream', 'stream_options', 'temperature',
    'top_p', 'max_tokens', 'max_completion_tokens', 'stop', 'presence_penalty',
    'frequency_penalty', 'logit_bias', 'seed', 'response_format', 'reasoning_effort',
    'verbosity', 'service_tier', 'logprobs', 'top_logprobs', 'tools', 'tool_choice',
    'parallel_tool_calls', 'functions', 'function_call', 'store', 'prompt_cache_key', 'user'})
RESPONSES_FIELDS = frozenset({'model', 'input', 'instructions', 'stream', 'temperature',
    'top_p', 'max_output_tokens', 'reasoning', 'text', 'truncation', 'service_tier',
    'tools', 'tool_choice', 'parallel_tool_calls', 'store', 'background', 'include',
    'previous_response_id', 'prompt_cache_key', 'user'})
TOOL_TYPES = frozenset({'function_call', 'tool_call', 'computer_call', 'web_search_call',
    'file_search_call', 'code_interpreter_call', 'image_generation_call', 'mcp_call',
    'mcp_list_tools', 'mcp_approval_request'})


class BrokerError(Exception):
    """Public fixed error codes only; never provider bodies or credentials."""

    def __init__(self, code, status=400):
        super().__init__(code)
        self.status = status


def need(condition, code='HERMES_MODEL_REQUEST_INVALID', status=400):
    if not condition:
        raise BrokerError(code, status)


def _pairs(items):
    value = {}
    for key, child in items:
        need(key not in value, 'HERMES_MODEL_JSON_INVALID')
        value[key] = child
    return value


def decode_json(body):
    try:
        return json.loads(body.decode('utf-8'), object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise BrokerError('HERMES_MODEL_JSON_INVALID') from None


@dataclass(frozen=True)
class BrokerConfig:
    worker_uid: int
    upstream_base_url: str
    model: str
    allowed_paths: tuple = ('/v1/chat/completions',)
    worker_gid: int | None = None
    timeout: float = 45
    max_request_bytes: int = MAX_BODY
    max_response_bytes: int = MAX_BODY
    max_connections: int = 4

    def __post_init__(self):
        need(type(self.worker_uid) is int and self.worker_uid > 0,
             'HERMES_MODEL_CONFIG_INVALID')
        need(self.worker_gid is None or type(self.worker_gid) is int and self.worker_gid > 0,
             'HERMES_MODEL_CONFIG_INVALID')
        need(type(self.upstream_base_url) is str, 'HERMES_MODEL_CONFIG_INVALID')
        try:
            url = urlsplit(self.upstream_base_url)
            valid_url = (url.scheme == 'https' and bool(url.hostname) and not url.username
                and not url.password and not url.query and not url.fragment
                and url.port in (None, 443) and '\\' not in self.upstream_base_url
                and not any(ord(c) <= 32 or ord(c) >= 127 for c in self.upstream_base_url)
                and re.fullmatch(r'(?:/[A-Za-z0-9_.~-]+)*/?', url.path) is not None
                and all(part not in ('.', '..') for part in url.path.split('/')))
        except ValueError:
            valid_url = False
        need(valid_url, 'HERMES_MODEL_CONFIG_INVALID')
        need(type(self.model) is str and MODEL.fullmatch(self.model)
             and not re.search(r'gpt-5-mini(?:$|[-/:])', self.model, re.I),
             'HERMES_MODEL_CONFIG_INVALID')
        need(type(self.allowed_paths) in (tuple, list) and bool(self.allowed_paths)
             and len(self.allowed_paths) == len(set(self.allowed_paths))
             and set(self.allowed_paths) <= ALLOWED_PATHS, 'HERMES_MODEL_CONFIG_INVALID')
        object.__setattr__(self, 'allowed_paths', tuple(self.allowed_paths))
        need(type(self.timeout) in (int, float) and math.isfinite(self.timeout)
             and 0 < self.timeout <= 45, 'HERMES_MODEL_CONFIG_INVALID')
        need(all(type(n) is int and 1 <= n <= MAX_BODY for n in
            (self.max_request_bytes, self.max_response_bytes)), 'HERMES_MODEL_CONFIG_INVALID')
        need(type(self.max_connections) is int and 1 <= self.max_connections <= 8,
             'HERMES_MODEL_CONFIG_INVALID')

    def upstream_url(self, path):
        need(path in self.allowed_paths, 'HERMES_MODEL_PATH_REFUSED', 404)
        return self.upstream_base_url.rstrip('/') + path.removeprefix('/v1')


def validate_request(body, path, config):
    need(path in config.allowed_paths, 'HERMES_MODEL_PATH_REFUSED', 404)
    need(type(body) is bytes and 0 < len(body) <= config.max_request_bytes,
         'HERMES_MODEL_BODY_LIMIT', 413)
    value = decode_json(body)
    fields = CHAT_FIELDS if path.endswith('/chat/completions') else RESPONSES_FIELDS
    need(type(value) is dict and set(value) <= fields)
    need(value.get('model') == config.model, 'HERMES_MODEL_IDENTITY_REFUSED')
    need(type(value.get('stream', False)) is bool)
    need(value.get('tools') in (None, []) and value.get('functions') in (None, [])
         and value.get('tool_choice') in (None, 'none')
         and value.get('function_call') in (None, 'none')
         and value.get('parallel_tool_calls') in (None, False), 'HERMES_MODEL_TOOLS_REFUSED')
    need(value.get('store') in (None, False) and value.get('background') in (None, False)
         and value.get('previous_response_id') is None, 'HERMES_MODEL_STATE_REFUSED')
    if path.endswith('/chat/completions'):
        need(type(value.get('messages')) is list and bool(value['messages']))
        for message in value['messages']:
            need(type(message) is dict and message.get('role') in
                 ('system', 'developer', 'user', 'assistant')
                 and not message.get('tool_calls') and not message.get('function_call'),
                 'HERMES_MODEL_TOOLS_REFUSED')
    else:
        need(type(value.get('input')) in (str, list) and bool(value['input']))
    _verify_tree(value, config.model, request=True)
    return value


def _verify_tree(value, model, *, request=False):
    """Check every structured model/tool identity, including nested SSE events."""
    stack = [(value, 0)]
    models = 0
    while stack:
        child, depth = stack.pop()
        need(depth <= 64, 'HERMES_MODEL_JSON_INVALID')
        if type(child) is dict:
            if 'model' in child:
                need(child['model'] == model, 'HERMES_MODEL_IDENTITY_REFUSED', 502)
                models += 1
            need(not child.get('tool_calls') and child.get('function_call') in (None, '', 'none')
                 and not (type(child.get('type')) is str and child['type'] in TOOL_TYPES)
                 and child.get('role') not in ('tool', 'function'),
                 'HERMES_MODEL_TOOLS_REFUSED', 400 if request else 502)
            stack.extend((item, depth + 1) for item in child.values())
        elif type(child) is list:
            stack.extend((item, depth + 1) for item in child)
        elif type(child) is float:
            need(math.isfinite(child), 'HERMES_MODEL_JSON_INVALID')
    return models


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    headers: dict
    body: bytes


class HttpsModelUpstream:
    """One fixed HTTPS target, no environment proxy, redirects, or retries."""

    def __init__(self, config, credential, *, connection_factory=None):
        need(type(credential) is str and 8 <= len(credential) <= 8192
             and not any(ord(c) <= 32 or ord(c) >= 127 for c in credential),
             'HERMES_MODEL_CREDENTIAL_INVALID')
        self.config = config
        self._credential = credential
        self._connection_factory = connection_factory or http.client.HTTPSConnection

    def __call__(self, path, body, *, deadline):
        url = urlsplit(self.config.upstream_url(path))
        need(time.monotonic() < deadline, 'HERMES_MODEL_TIMEOUT', 504)
        connection = self._connection_factory(url.hostname, port=url.port or 443,
            timeout=max(0.001, deadline - time.monotonic()), context=ssl.create_default_context())
        expired = threading.Event()
        def cancel():
            expired.set()
            stream = connection.sock
            if stream is not None:
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        timer = threading.Timer(max(0.001, deadline - time.monotonic()), cancel)
        timer.daemon = True
        timer.start()
        def remaining():
            seconds = deadline - time.monotonic()
            need(seconds > 0 and not expired.is_set(), 'HERMES_MODEL_TIMEOUT', 504)
            if connection.sock is not None:
                connection.sock.settimeout(seconds)
        try:
            # Connect explicitly: a late DNS/connect result cannot dispatch a
            # provider request after the original request budget has expired.
            connection.connect()
            remaining()
            connection.request('POST', url.path, body=body, headers={
                'Authorization': 'Bearer ' + self._credential,
                'Content-Type': 'application/json', 'Accept-Encoding': 'identity'})
            remaining()
            response = connection.getresponse()
            if response.status != 200:
                return UpstreamResponse(response.status, {}, b'')
            headers = response.getheaders()
            need(len(headers) <= 100 and sum(len(k) + len(v) for k, v in headers) <= MAX_HEADERS,
                 'HERMES_MODEL_RESPONSE_LIMIT', 502)
            lower = {}
            for key, value in headers:
                key = key.lower()
                need(key not in lower, 'HERMES_MODEL_RESPONSE_INVALID', 502)
                lower[key] = value
            need(lower.get('content-encoding', 'identity').lower() == 'identity',
                 'HERMES_MODEL_RESPONSE_INVALID', 502)
            chunks = []
            size = 0
            while True:
                remaining()
                chunk = response.read1(min(65536, self.config.max_response_bytes + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                need(size <= self.config.max_response_bytes, 'HERMES_MODEL_RESPONSE_LIMIT', 502)
                chunks.append(chunk)
            remaining()
            return UpstreamResponse(response.status, lower, b''.join(chunks))
        except BrokerError:
            raise
        except (TimeoutError, socket.timeout):
            raise BrokerError('HERMES_MODEL_TIMEOUT', 504) from None
        except Exception:
            # No exception string: network libraries can include URLs/headers.
            if expired.is_set():
                raise BrokerError('HERMES_MODEL_TIMEOUT', 504) from None
            raise BrokerError('HERMES_MODEL_UPSTREAM_UNAVAILABLE', 502) from None
        finally:
            timer.cancel()
            connection.close()

    def close(self):
        pass


def validate_response(response, *, stream, path, config):
    need(type(response) is UpstreamResponse and response.status == 200,
         'HERMES_MODEL_UPSTREAM_REFUSED', 502)
    need(type(response.body) is bytes and 0 < len(response.body) <= config.max_response_bytes,
         'HERMES_MODEL_RESPONSE_LIMIT', 502)
    headers = {str(key).lower(): value for key, value in response.headers.items()}
    request_id = headers.get('x-request-id') or headers.get('request-id')
    need(type(request_id) is str and REQUEST_ID.fullmatch(request_id),
         'HERMES_MODEL_REQUEST_ID_MISSING', 502)
    content_type = headers.get('content-type', '').split(';', 1)[0].lower()
    if stream:
        need(content_type == 'text/event-stream', 'HERMES_MODEL_RESPONSE_INVALID', 502)
        _validate_sse(response.body, config.model, path)
    else:
        need(content_type == 'application/json', 'HERMES_MODEL_RESPONSE_INVALID', 502)
        value = decode_json(response.body)
        need(type(value) is dict and not value.get('error')
             and value.get('status') not in ('failed', 'incomplete', 'cancelled'),
             'HERMES_MODEL_RESPONSE_INVALID', 502)
        need(_verify_tree(value, config.model) > 0, 'HERMES_MODEL_IDENTITY_MISSING', 502)
    return {'content-type': content_type, 'x-request-id': request_id,
        'x-hermes-verified-model': config.model}


def _validate_sse(body, model, path):
    try:
        text = body.decode('utf-8').replace('\r\n', '\n')
    except UnicodeError:
        raise BrokerError('HERMES_MODEL_RESPONSE_INVALID', 502) from None
    need('\r' not in text and text.endswith('\n\n'), 'HERMES_MODEL_STREAM_INCOMPLETE', 502)
    events = text.split('\n\n')
    need(len(events) <= MAX_EVENTS + 1, 'HERMES_MODEL_RESPONSE_LIMIT', 502)
    models = 0
    finished = done = False
    for event in events:
        data = []
        event_name = None
        for line in event.split('\n'):
            if not line or line.startswith(':'):
                continue
            if line.startswith('data:'):
                data.append(line[5:].removeprefix(' '))
            elif line.startswith('event:'):
                need(event_name is None, 'HERMES_MODEL_RESPONSE_INVALID', 502)
                event_name = line[6:].strip()
            else:
                need(False, 'HERMES_MODEL_RESPONSE_INVALID', 502)
        if not data:
            continue
        need(not done, 'HERMES_MODEL_RESPONSE_INVALID', 502)
        encoded = '\n'.join(data)
        if encoded == '[DONE]':
            done = True
            continue
        value = decode_json(encoded.encode())
        need(type(value) is dict and not value.get('error')
             and value.get('type') not in ('error', 'response.failed', 'response.incomplete')
             and event_name not in ('error', 'response.failed', 'response.incomplete'),
             'HERMES_MODEL_RESPONSE_INVALID', 502)
        models += _verify_tree(value, model)
        if value.get('type') == 'response.completed' or event_name == 'response.completed':
            need(value.get('response', {}).get('status') == 'completed',
                 'HERMES_MODEL_RESPONSE_INVALID', 502)
            finished = True
        choices = value.get('choices', [])
        need(type(choices) is list, 'HERMES_MODEL_RESPONSE_INVALID', 502)
        for choice in choices:
            need(type(choice) is dict, 'HERMES_MODEL_RESPONSE_INVALID', 502)
            if choice.get('finish_reason') is not None:
                finished = True
    need(models > 0, 'HERMES_MODEL_IDENTITY_MISSING', 502)
    need(finished and (done or path.endswith('/responses')),
         'HERMES_MODEL_STREAM_INCOMPLETE', 502)


def verify_peer(connection, expected_uid):
    need(hasattr(socket, 'SO_PEERCRED'), 'HERMES_MODEL_PEER_UNAVAILABLE', 403)
    try:
        pid, uid, gid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET,
            socket.SO_PEERCRED, struct.calcsize('3i')))
    except (OSError, ValueError, struct.error):
        raise BrokerError('HERMES_MODEL_PEER_REFUSED', 403) from None
    need(pid > 0 and uid == expected_uid, 'HERMES_MODEL_PEER_REFUSED', 403)
    return pid, uid, gid


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        def expire_input():
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        input_timer = threading.Timer(min(5, self.server.config.timeout), expire_input)
        input_timer.daemon = True
        input_timer.start()
        try:
            self.connection.settimeout(min(5, self.server.config.timeout))
            request_line = self.rfile.readline(MAX_HEADERS + 1)
            need(len(request_line) <= MAX_HEADERS and request_line.endswith(b'\r\n'),
                 'HERMES_MODEL_HTTP_INVALID')
            try:
                method, path, version = request_line[:-2].decode('ascii').split(' ')
            except (ValueError, UnicodeError):
                raise BrokerError('HERMES_MODEL_HTTP_INVALID') from None
            need(method == 'POST' and version == 'HTTP/1.1', 'HERMES_MODEL_HTTP_INVALID')
            need(path in self.server.config.allowed_paths, 'HERMES_MODEL_PATH_REFUSED', 404)
            headers = {}
            size = len(request_line)
            while True:
                line = self.rfile.readline(MAX_HEADERS + 1)
                size += len(line)
                need(size <= MAX_HEADERS and line.endswith(b'\r\n'), 'HERMES_MODEL_HTTP_INVALID')
                if line == b'\r\n':
                    break
                try:
                    name, value = line[:-2].decode('ascii').split(':', 1)
                except (ValueError, UnicodeError):
                    raise BrokerError('HERMES_MODEL_HTTP_INVALID') from None
                need(re.fullmatch(r'[A-Za-z0-9-]+', name) is not None
                     and not any(ord(c) < 32 and c != '\t' or ord(c) == 127 for c in value),
                     'HERMES_MODEL_HTTP_INVALID')
                name, value = name.lower(), value.strip()
                need(name not in headers, 'HERMES_MODEL_HTTP_INVALID')
                headers[name] = value
            need(headers.get('host') == 'hermes-model' and not
                 {'transfer-encoding', 'content-encoding', 'expect'} & headers.keys(),
                 'HERMES_MODEL_HTTP_INVALID')
            need(headers.get('content-type', '').lower() in
                 ('application/json', 'application/json; charset=utf-8'), 'HERMES_MODEL_HTTP_INVALID')
            length = headers.get('content-length', '')
            need(re.fullmatch(r'[1-9][0-9]{0,8}', length) is not None, 'HERMES_MODEL_HTTP_INVALID')
            length = int(length)
            need(length <= self.server.config.max_request_bytes, 'HERMES_MODEL_BODY_LIMIT', 413)
            body = self.rfile.read(length)
            need(len(body) == length, 'HERMES_MODEL_HTTP_INVALID')
            value = validate_request(body, path, self.server.config)
            input_timer.cancel()
            deadline = time.monotonic() + self.server.config.timeout
            response = self.server.upstream(path, body, deadline=deadline)
            need(time.monotonic() < deadline, 'HERMES_MODEL_TIMEOUT', 504)
            headers = validate_response(response, stream=value.get('stream', False),
                path=path, config=self.server.config)
            self._send(200, headers, response.body)
        except BrokerError as error:
            self._error(error)
        except (TimeoutError, socket.timeout):
            self._error(BrokerError('HERMES_MODEL_TIMEOUT', 504))
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception:
            self._error(BrokerError('HERMES_MODEL_UNAVAILABLE', 502))
        finally:
            input_timer.cancel()

    def _error(self, error):
        body = json.dumps({'error': {'message': str(error), 'type': 'hermes_model_broker',
            'code': str(error)}}).encode()
        try:
            self._send(error.status, {'content-type': 'application/json'}, body)
        except OSError:
            pass

    def _send(self, status, headers, body):
        lines = [f'HTTP/1.1 {status} ' + ('OK' if status == 200 else 'Refused'),
            'connection: close', f'content-length: {len(body)}']
        lines.extend(key + ': ' + value for key, value in headers.items())
        self.wfile.write(('\r\n'.join(lines) + '\r\n\r\n').encode('ascii') + body)


class HermesModelBroker(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """No TCP listener; peer authentication happens before handler construction."""
    daemon_threads = True
    block_on_close = False

    def __init__(self, socket_path, config, upstream, *, secure_socket=True):
        self.config, self.upstream = config, upstream
        self._slots = threading.BoundedSemaphore(config.max_connections)
        path = Path(socket_path)
        need(path.is_absolute(), 'HERMES_MODEL_SOCKET_INVALID')
        if secure_socket:
            for directory in (path.parent, *path.parent.parents):
                parent = directory.lstat()
                need(parent.st_uid in (0, os.geteuid()) and stat.S_ISDIR(parent.st_mode)
                     and not parent.st_mode & 0o022, 'HERMES_MODEL_SOCKET_INVALID')
        need(not path.exists() and not path.is_symlink(), 'HERMES_MODEL_SOCKET_EXISTS')
        super().__init__(str(path), _Handler, bind_and_activate=False)
        self._socket_identity = None
        try:
            self.server_bind()
            info = path.stat()
            self._socket_identity = (info.st_dev, info.st_ino)
            os.chmod(path, 0o660)
            if secure_socket:
                os.chown(path, -1, config.worker_gid or config.worker_uid)
            self.server_activate()
        except Exception:
            self.server_close()
            raise

    def get_request(self):
        connection, address = super().get_request()
        try:
            verify_peer(connection, self.config.worker_uid)
        except BrokerError:
            connection.close()
            raise OSError('HERMES_MODEL_PEER_REFUSED') from None
        return connection, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # socketserver's default traceback can contain transport internals.
        pass

    def server_close(self):
        super().server_close()
        try:
            info = os.lstat(self.server_address)
            if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self._socket_identity:
                os.unlink(self.server_address)
        except (FileNotFoundError, AttributeError):
            pass


def _read_protected_file(path, *, credential=False):
    """Open every component without following links; config stays root sealed."""
    path = Path(path)
    need(path.is_absolute(), 'HERMES_MODEL_CONFIG_INVALID')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    descriptor = None
    try:
        for name in path.parts[1:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            owners = (0, os.geteuid()) if credential else (0,)
            need(info.st_uid in owners and not info.st_mode & 0o022,
                 'HERMES_MODEL_CONFIG_INVALID')
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        info = os.fstat(descriptor)
        owners = (0, os.geteuid()) if credential else (0,)
        maximum = 8193 if credential else 16384
        need(stat.S_ISREG(info.st_mode) and info.st_uid in owners
             and not info.st_mode & 0o022 and info.st_size <= maximum
             and (not credential or not info.st_mode & 0o077), 'HERMES_MODEL_CONFIG_INVALID')
        with os.fdopen(descriptor, 'rb', closefd=True) as stream:
            descriptor = None
            body = stream.read(maximum + 1)
        need(0 < len(body) <= maximum, 'HERMES_MODEL_CONFIG_INVALID')
        return body
    except OSError:
        raise BrokerError('HERMES_MODEL_CONFIG_INVALID') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--credential-file', required=True)
    parser.add_argument('--socket-path', required=True)
    args = parser.parse_args(argv)
    upstream = None
    try:
        need(os.geteuid() != 0, 'HERMES_MODEL_ROOT_REFUSED')
        value = decode_json(_read_protected_file(args.config))
        need(type(value) is dict, 'HERMES_MODEL_CONFIG_INVALID')
        config = BrokerConfig(**value)
        need(config.worker_uid != os.geteuid(), 'HERMES_MODEL_UID_INVALID')
        try:
            credential = _read_protected_file(args.credential_file, credential=True).decode('ascii').strip()
        except UnicodeError:
            raise BrokerError('HERMES_MODEL_CREDENTIAL_INVALID') from None
        upstream = HttpsModelUpstream(config, credential)
        with HermesModelBroker(args.socket_path, config, upstream) as server:
            server.serve_forever(poll_interval=0.25)
    except (BrokerError, TypeError, ValueError):
        # Fixed diagnostics, independent of JSON, credentials, and provider errors.
        raise SystemExit('HERMES_MODEL_BROKER_START_REFUSED') from None
    finally:
        if upstream is not None:
            upstream.close()


if __name__ == '__main__':
    main()
