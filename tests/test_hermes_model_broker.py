import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from agent import hermes_model_broker as b


def config(**kwargs):
    values = dict(worker_uid=max(1, os.getuid()), worker_gid=max(1, os.getgid()),
        upstream_base_url='https://model-provider.invalid/api/v1', model='hermes-fixed-model',
        allowed_paths=('/v1/chat/completions', '/v1/responses'))
    values.update(kwargs)
    return b.BrokerConfig(**values)


def request(**kwargs):
    value = dict(model='hermes-fixed-model', messages=[{'role': 'user', 'content': 'hello'}])
    value.update(kwargs)
    return json.dumps(value).encode()


def response(**kwargs):
    value = dict(model='hermes-fixed-model', id='chatcmpl-genuine-provider-id',
        choices=[{'index': 0, 'message': {'role': 'assistant', 'content': 'hello'},
            'finish_reason': 'stop'}])
    value.update(kwargs)
    return b.UpstreamResponse(200, {'content-type': 'application/json',
        'x-request-id': 'genuine-provider-request-123'}, json.dumps(value).encode())


def sse(*, model='hermes-fixed-model', done=True):
    chunks = [dict(id='chatcmpl-id', model='hermes-fixed-model',
        choices=[{'index': 0, 'delta': {'role': 'assistant', 'content': 'hello'},
            'finish_reason': None}]),
        dict(id='chatcmpl-id', model=model,
            choices=[{'index': 0, 'delta': {}, 'finish_reason': 'stop'}])]
    body = b''.join(b'data: ' + json.dumps(chunk).encode() + b'\n\n' for chunk in chunks)
    if done:
        body += b'data: [DONE]\n\n'
    return b.UpstreamResponse(200, {'content-type': 'text/event-stream; charset=utf-8',
        'x-request-id': 'provider-stream-request-123'}, body)


class BrokerValidationTests(unittest.TestCase):
    def test_only_fixed_sealed_https_base_and_model(self):
        for value in ('http://provider.invalid/v1', 'https://name:secret@provider.invalid/v1',
                'https://provider.invalid/v1?target=other', 'https://provider.invalid/v1#x',
                'https://provider.invalid:8443/v1', 'https://provider.invalid/../v1',
                'https://provider.invalid/%2fother', 'https://provider.invalid/\nother'):
            with self.subTest(base=value), self.assertRaises(b.BrokerError):
                config(upstream_base_url=value)
        self.assertEqual(config().upstream_url('/v1/responses'),
            'https://model-provider.invalid/api/v1/responses')
        for model in ('gpt-5-mini', 'gpt-5-mini-2025', 'other model'):
            with self.subTest(model=model), self.assertRaises(b.BrokerError):
                config(model=model)

    def test_no_unknown_config_fields_or_routes(self):
        with self.assertRaises(TypeError):
            b.BrokerConfig(**dict(worker_uid=10003, model='fixed',
                upstream_base_url='https://provider.invalid/v1', fallback_model='other'))
        for paths in ((), ('/v1/models',), ('/v1/responses?url=http://other',)):
            with self.subTest(paths=paths), self.assertRaises(b.BrokerError):
                config(allowed_paths=paths)

    def test_models_and_tools_fail_before_upstream(self):
        for extra in ({'model': 'different'}, {'model': 'gpt-5-mini'},
                {'tools': [{'type': 'function', 'function': {'name': 'shell'}}]},
                {'functions': [{'name': 'shell'}]}, {'tool_choice': 'auto'},
                {'parallel_tool_calls': True}, {'base_url': 'https://other.invalid/v1'},
                {'messages': [{'role': 'tool', 'content': 'result'}]},
                {'messages': [{'role': 'assistant', 'content': '',
                    'tool_calls': [{'id': 'x', 'type': 'function'}]}]},
                {'store': True}, {'background': True}, {'previous_response_id': 'resp_old'}):
            with self.subTest(fields=tuple(extra)), self.assertRaises(b.BrokerError):
                b.validate_request(request(**extra), '/v1/chat/completions', config())
        self.assertEqual(b.validate_request(request(tools=[], stream=True,
            stream_options={'include_usage': True}), '/v1/chat/completions', config())['stream'], True)

    def test_bounded_utf8_json_rejects_duplicate_nan_and_wrong_structure(self):
        for body in (b'{"model":"hermes-fixed-model","model":"other"}', b'[]',
                b'{"temperature":NaN}', b'\xff', '{"x":1}'.encode('utf-16')):
            with self.subTest(body=body), self.assertRaises(b.BrokerError):
                b.validate_request(body, '/v1/chat/completions', config())
        with self.assertRaisesRegex(b.BrokerError, 'BODY_LIMIT'):
            b.validate_request(request(), '/v1/chat/completions', config(max_request_bytes=10))
        for path in ('https://other.invalid/v1/chat/completions', '/v1/chat/completions?x=1',
                '/v1/models', '/v1/responses/../chat/completions'):
            with self.subTest(path=path), self.assertRaises(b.BrokerError):
                b.validate_request(request(), path, config())

    def test_pinned_f0d_native_openai_chat_prompt_cache_key_is_allowed(self):
        # f0d agent/transports/chat_completions.py _finish_kwargs always calls
        # _add_prompt_cache_key for the native OpenAI route. This is a routing
        # hint on the fixed model endpoint, not a URL or a capability selection.
        value = {'model': 'gpt-6.1-sol', 'messages': [
            {'role': 'system', 'content': 'Return only dialogue-control JSON.'},
            {'role': 'user', 'content': 'Review the observed public analysis.'}],
            'max_completion_tokens': 1024, 'prompt_cache_key': 'pck_' + 'a' * 24}
        parsed = b.validate_request(json.dumps(value).encode(), '/v1/chat/completions',
            config(model='gpt-6.1-sol', upstream_base_url='https://api.openai.com/v1'))
        self.assertEqual(parsed, value)

    def test_pinned_f0d_native_openai_responses_default_fields_are_allowed(self):
        # f0d ResponsesApiTransport.build_kwargs on the official API, tools=[],
        # default reasoning, no native compaction, and nonstream SDK execution.
        value = {'model': 'gpt-6.1-sol', 'instructions': 'Return dialogue-control JSON.',
            'input': [{'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'Review the observed public analysis.'}]}],
            'store': False, 'prompt_cache_key': 'pck_' + 'a' * 24,
            'reasoning': {'effort': 'medium', 'summary': 'auto'},
            'include': ['reasoning.encrypted_content'], 'max_output_tokens': 1024}
        cfg = config(model='gpt-6.1-sol', upstream_base_url='https://api.openai.com/v1',
            allowed_paths=('/v1/responses',))
        self.assertEqual(b.validate_request(json.dumps(value).encode(), '/v1/responses', cfg), value)
        with self.assertRaisesRegex(b.BrokerError, 'TOOLS_REFUSED'):
            b.validate_request(json.dumps({**value, 'tools': [{'type': 'web_search'}]}).encode(),
                '/v1/responses', cfg)

    def test_actual_response_model_and_genuine_request_id_are_required(self):
        headers = b.validate_response(response(), stream=False,
            path='/v1/chat/completions', config=config())
        self.assertEqual(headers['x-hermes-verified-model'], 'hermes-fixed-model')
        self.assertEqual(headers['x-request-id'], 'genuine-provider-request-123')
        for upstream in (response(model='different'), response(model=None),
                b.UpstreamResponse(200, {'content-type': 'application/json'}, response().body),
                b.UpstreamResponse(302, {'location': 'https://other.invalid'}, b'redirect'),
                b.UpstreamResponse(200, {'content-type': 'application/json',
                    'x-request-id': 'bad\r\nheader'}, response().body)):
            with self.subTest(upstream=upstream.status), self.assertRaises(b.BrokerError):
                b.validate_response(upstream, stream=False,
                    path='/v1/chat/completions', config=config())
        missing = json.loads(response().body)
        del missing['model']
        with self.assertRaisesRegex(b.BrokerError, 'IDENTITY_MISSING'):
            b.validate_response(b.UpstreamResponse(200, response().headers, json.dumps(missing).encode()),
                stream=False, path='/v1/chat/completions', config=config())

    def test_provider_tool_output_is_not_relayed(self):
        upstream = response(choices=[{'message': {'role': 'assistant',
            'tool_calls': [{'type': 'function', 'function': {'name': 'shell'}}]}}])
        with self.assertRaisesRegex(b.BrokerError, 'TOOLS_REFUSED'):
            b.validate_response(upstream, stream=False,
                path='/v1/chat/completions', config=config())

    def test_finite_sse_requires_same_model_through_last_event(self):
        b.validate_response(sse(), stream=True, path='/v1/chat/completions', config=config())
        for upstream in (sse(model='different'), sse(done=False),
                b.UpstreamResponse(200, sse().headers, sse().body + b'data: {}\n\n'),
                b.UpstreamResponse(200, sse().headers, b'data: {"error":"private"}\n\n'),
                b.UpstreamResponse(200, sse().headers, b'data: [DONE]\n\n')):
            with self.subTest(body_length=len(upstream.body)), self.assertRaises(b.BrokerError):
                b.validate_response(upstream, stream=True,
                    path='/v1/chat/completions', config=config())

    def test_responses_sse_verifies_nested_model_and_completion(self):
        item = {'type': 'response.completed', 'response': {'id': 'resp_real',
            'model': 'hermes-fixed-model', 'status': 'completed',
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'ok'}]}]}}
        raw = b'event: response.completed\ndata: ' + json.dumps(item).encode() + b'\n\n'
        b.validate_response(b.UpstreamResponse(200, sse().headers, raw), stream=True,
            path='/v1/responses', config=config())
        item['response']['model'] = 'other'
        with self.assertRaisesRegex(b.BrokerError, 'IDENTITY_REFUSED'):
            b.validate_response(b.UpstreamResponse(200, sse().headers,
                b'data: ' + json.dumps(item).encode() + b'\n\n'), stream=True,
                path='/v1/responses', config=config())

    def test_response_limit_and_incomplete_sse_fail_closed(self):
        with self.assertRaisesRegex(b.BrokerError, 'RESPONSE_LIMIT'):
            b.validate_response(response(), stream=False,
                path='/v1/chat/completions', config=config(max_response_bytes=10))
        with self.assertRaisesRegex(b.BrokerError, 'STREAM_INCOMPLETE'):
            b.validate_response(b.UpstreamResponse(200, sse().headers, sse().body[:-1]),
                stream=True, path='/v1/chat/completions', config=config())


class PeerCredentialTests(unittest.TestCase):
    def test_kernel_peer_identity_from_real_unix_socket(self):
        left, right = socket.socketpair(socket.AF_UNIX)
        try:
            pid, uid, gid = b.verify_peer(left, os.getuid())
            self.assertEqual((pid, uid, gid), (os.getpid(), os.getuid(), os.getgid()))
        finally:
            left.close()
            right.close()

    def test_wrong_peer_is_closed_before_any_body_or_headers_are_read(self):
        connection = Mock()
        connection.getsockopt.return_value = struct.pack('3i', 77, 99999, 99999)
        broker = object.__new__(b.HermesModelBroker)
        broker.config = config()
        with patch.object(b.socketserver.UnixStreamServer, 'get_request',
                return_value=(connection, '')):
            with self.assertRaises(OSError):
                broker.get_request()
        connection.close.assert_called_once()
        connection.recv.assert_not_called()
        connection.makefile.assert_not_called()


class UpstreamTests(unittest.TestCase):
    def fake_connection(self, upstream=None):
        upstream = upstream or response()
        received = Mock()
        received.status = upstream.status
        received.getheaders.return_value = list(upstream.headers.items())
        received.read1.side_effect = [upstream.body, b'']
        connection = Mock()
        connection.getresponse.return_value = received
        factory = Mock(return_value=connection)
        return factory, connection, received

    def test_https_uses_only_configured_target_and_broker_authorization(self):
        secret = 'test-broker-only-model-token'
        factory, connection, received = self.fake_connection()
        upstream = b.HttpsModelUpstream(config(), secret, connection_factory=factory)
        result = upstream('/v1/chat/completions', request(), deadline=time.monotonic() + 5)
        self.assertEqual(result.body, response().body)
        args, kwargs = factory.call_args
        self.assertEqual(args, ('model-provider.invalid',))
        self.assertEqual(kwargs['port'], 443)
        self.assertEqual(kwargs['context'].verify_mode, b.ssl.CERT_REQUIRED)
        self.assertTrue(kwargs['context'].check_hostname)
        connection.request.assert_called_once_with('POST', '/api/v1/chat/completions',
            body=request(), headers={'Authorization': 'Bearer ' + secret,
                'Content-Type': 'application/json', 'Accept-Encoding': 'identity'})
        connection.close.assert_called_once()

    def test_redirect_is_not_followed_or_reflected(self):
        factory, connection, received = self.fake_connection(b.UpstreamResponse(302,
            {'location': 'https://other.invalid'}, b'provider-private-error'))
        upstream = b.HttpsModelUpstream(config(), 'test-model-token',
            connection_factory=factory)
        result = upstream('/v1/chat/completions', request(), deadline=time.monotonic() + 5)
        connection.request.assert_called_once()
        received.read1.assert_not_called()
        self.assertEqual((result.status, result.headers, result.body), (302, {}, b''))

    def test_provider_errors_never_expose_exception_text(self):
        factory, connection, _ = self.fake_connection()
        connection.connect.side_effect = RuntimeError('private credential or authorization header')
        upstream = b.HttpsModelUpstream(config(), 'test-model-token', connection_factory=factory)
        with self.assertRaisesRegex(b.BrokerError, '^HERMES_MODEL_UPSTREAM_UNAVAILABLE$'):
            upstream('/v1/chat/completions', request(), deadline=time.monotonic() + 5)

    def test_expired_connect_never_dispatches_late_request(self):
        factory, connection, _ = self.fake_connection()
        connection.connect.side_effect = lambda: time.sleep(.03)
        upstream = b.HttpsModelUpstream(config(), 'test-model-token', connection_factory=factory)
        with self.assertRaisesRegex(b.BrokerError, '^HERMES_MODEL_TIMEOUT$'):
            upstream('/v1/chat/completions', request(), deadline=time.monotonic() + .01)
        connection.request.assert_not_called()
        connection.close.assert_called_once()

    def test_compressed_upstream_is_rejected_before_reading(self):
        factory, connection, received = self.fake_connection(b.UpstreamResponse(200,
            {**response().headers, 'content-encoding': 'gzip'}, b'compressed'))
        upstream = b.HttpsModelUpstream(config(), 'test-model-token', connection_factory=factory)
        with self.assertRaisesRegex(b.BrokerError, 'RESPONSE_INVALID'):
            upstream('/v1/chat/completions', request(), deadline=time.monotonic() + 5)
        received.read1.assert_not_called()

    def test_stdlib_only_entrypoint_loads_under_isolated_no_site_python(self):
        result = subprocess.run([sys.executable, '-I', '-S', str(Path(b.__file__)), '--help'],
            capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'--credential-file', result.stdout)


@unittest.skipIf(os.getuid() == 0, 'production worker UID must be nonroot')
class UnixHttpIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.socket_path = str(Path(self.temp.name) / 'model.sock')
        self.calls = []
        self.output = response()
        def upstream(path, body, *, deadline):
            self.calls.append((path, body))
            return self.output
        self.server = b.HermesModelBroker(self.socket_path, config(), upstream, secure_socket=False)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.temp.cleanup()

    def send(self, body, *, path='/v1/chat/completions', headers=None):
        import httpx
        with httpx.Client(transport=httpx.HTTPTransport(uds=self.socket_path),
                base_url='http://hermes-model') as client:
            return client.post(path, content=body, headers={
                'content-type': 'application/json', 'authorization': 'Bearer isolated-dummy-key',
                'x-request-id': 'forged-worker-id', **(headers or {})})

    def test_real_httpx_unix_transport_retains_provider_receipt(self):
        result = self.send(request())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content, self.output.body)
        self.assertEqual(result.headers['x-request-id'], 'genuine-provider-request-123')
        self.assertEqual(result.headers['x-hermes-verified-model'], 'hermes-fixed-model')
        self.assertEqual(self.calls, [('/v1/chat/completions', request())])
        self.assertNotIn(b'isolated-dummy-key', result.content)
        self.assertEqual(os.stat(self.socket_path).st_mode & 0o777, 0o660)

    def test_nonempty_tools_or_wrong_path_make_no_upstream_call(self):
        self.assertEqual(self.send(request(tools=[{'type': 'function'}])).status_code, 400)
        self.assertEqual(self.send(request(), path='/v1/models').status_code, 404)
        self.assertEqual(self.calls, [])

    def test_entire_sse_validated_before_sdk_receives_any_event(self):
        self.output = sse(model='different')
        result = self.send(request(stream=True))
        self.assertEqual(result.status_code, 502)
        self.assertNotIn(b'data:', result.content)
        self.assertNotIn(b'hello', result.content)
        self.assertNotIn('x-hermes-verified-model', result.headers)
        self.output = sse()
        result = self.send(request(stream=True))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content, self.output.body)

    def test_chunked_or_duplicate_lengths_make_no_upstream_call(self):
        connection = socket.socket(socket.AF_UNIX)
        connection.connect(self.socket_path)
        connection.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: hermes-model\r\n'
            b'Content-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}')
        result = connection.recv(4096)
        connection.close()
        self.assertIn(b'400 Refused', result)
        self.assertEqual(self.calls, [])

    def test_socket_cleanup_does_not_unlink_replacement(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        os.unlink(self.socket_path)
        Path(self.socket_path).write_text('replacement')
        self.server.server_close()
        self.assertEqual(Path(self.socket_path).read_text(), 'replacement')


if __name__ == '__main__':
    unittest.main()
