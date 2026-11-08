from contextlib import ExitStack
import errno
import io
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
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

    def test_invalid_response_predicates_retain_original_code_status_and_only_fixed_details(self):
        terminal={'type':'response.incomplete','response':{'model':'hermes-fixed-model','status':'incomplete',
            'incomplete_details':{'reason':'max_output_tokens','private':'PRIVATE_PROVIDER_TEXT'}}}
        items=((b.UpstreamResponse(200,{**response().headers,'content-type':'text/plain'},response().body),False,
                'NONSTREAM_CONTENT_TYPE_NOT_JSON',None),
            (response(status='incomplete',incomplete_details={'reason':'content_filter','private':'PRIVATE_PROVIDER_TEXT'}),False,
                'JSON_STATUS_INCOMPLETE','CONTENT_FILTER'),
            (b.UpstreamResponse(200,sse().headers,b'event: response.incomplete\ndata: '+json.dumps(terminal).encode()+b'\n\n'),True,
                'SSE_TYPE_RESPONSE_INCOMPLETE','MAX_OUTPUT_TOKENS'),
            (b.UpstreamResponse(200,sse().headers,b'id: PRIVATE_PROVIDER_TEXT\ndata: {}\n\n'),True,
                'SSE_UNSUPPORTED_FIELD',None))
        for upstream,stream,reason,detail in items:
            with self.subTest(reason=reason),self.assertRaises(b.BrokerError)as caught:
                b.validate_response(upstream,stream=stream,path='/v1/responses'if stream else'/v1/chat/completions',config=config())
            self.assertEqual((str(caught.exception),caught.exception.status),('HERMES_MODEL_RESPONSE_INVALID',502))
            self.assertEqual(getattr(caught.exception,'reason',None),reason)
            self.assertEqual(getattr(caught.exception,'reason_detail',None),detail)
            self.assertNotIn('PRIVATE_',str(caught.exception.__dict__))

    def test_unknown_response_diagnostic_values_cannot_escape_fixed_allowlists(self):
        error=b.BrokerError('HERMES_MODEL_RESPONSE_INVALID',502,reason='PRIVATE_PROVIDER_TEXT',reason_detail='PRIVATE_DETAIL')
        self.assertEqual((error.reason,error.reason_detail),(None,None))
        self.assertEqual(str(error),'HERMES_MODEL_RESPONSE_INVALID')
        self.assertEqual(b._terminal_reason_detail({'response':{'incomplete_details':{'reason':'PRIVATE_DETAIL'}}}),'OTHER')
        self.assertIsNone(b._terminal_reason_detail({'response':{'incomplete_details':{'reason':['PRIVATE_DETAIL']}}}))

    def test_json_shape_diagnostics_do_not_add_validation_or_crash_on_container_values(self):
        for value in (None, False, 4, 'PRIVATE_PROVIDER_TEXT', [], [{}]):
            with self.subTest(value=value), self.assertRaises(b.BrokerError) as caught:
                b.validate_response(b.UpstreamResponse(200, response().headers, json.dumps(value).encode()),
                    stream=False, path='/v1/responses', config=config())
            self.assertEqual((str(caught.exception), caught.exception.status, caught.exception.reason),
                ('HERMES_MODEL_RESPONSE_INVALID', 502, 'JSON_ROOT_NOT_OBJECT'))
        # These status values were not rejected by the original predicate. The
        # metadata helper must not add a hashability error or a new refusal.
        for status in (None, False, 4, [], {}):
            with self.subTest(status=status):
                result = b.validate_response(response(status=status), stream=False,
                    path='/v1/responses', config=config())
                self.assertEqual(result['x-hermes-verified-model'], 'hermes-fixed-model')

    def test_sse_refusal_metadata_is_fixed_for_invalid_shape_and_terminal_status(self):
        cases = (([], None, 'SSE_ROOT_NOT_OBJECT'),
            ({'error': {'message': 'PRIVATE_PROVIDER_TEXT'}}, None, 'SSE_ERROR_PRESENT'),
            ({'type': 'response.failed'}, None, 'SSE_TYPE_RESPONSE_FAILED'),
            ({}, 'error', 'SSE_EVENT_ERROR'),
            ({'type': 'response.completed', 'response': {'status': 'incomplete',
                'incomplete_details': {'reason': 'PRIVATE_REASON'}}}, None,
                'SSE_COMPLETED_STATUS_NOT_COMPLETED'),
            ({'choices': {}}, None, 'SSE_CHOICES_NOT_ARRAY'),
            ({'choices': ['PRIVATE_PROVIDER_TEXT']}, None, 'SSE_CHOICE_NOT_OBJECT'))
        for value, event_name, reason in cases:
            raw = ((('event: ' + event_name + '\n').encode() if event_name else b'')
                + b'data: ' + json.dumps(value).encode() + b'\n\n')
            with self.subTest(reason=reason), self.assertRaises(b.BrokerError) as caught:
                b.validate_response(b.UpstreamResponse(200, sse().headers, raw),
                    stream=True, path='/v1/responses', config=config())
            self.assertEqual((str(caught.exception), caught.exception.status, caught.exception.reason),
                ('HERMES_MODEL_RESPONSE_INVALID', 502, reason))
            self.assertNotIn('PRIVATE_', str(caught.exception.__dict__))

    def test_refusal_handler_logs_fixed_predicate_without_changing_error_reply_or_dispatch(self):
        upstream=response(status='incomplete',incomplete_details={'reason':'max_output_tokens','private':'PRIVATE_PROVIDER_TEXT'})
        body=request();calls=[];events=[]
        server=object.__new__(b.HermesModelBroker)
        server.config=config();server._diagnostic_lock=threading.Lock();server._event_counts={};server._diagnostics=events.append
        def send_upstream(*args,**kwargs):calls.append(args);return upstream
        server.upstream=send_upstream
        def run():
            handler=object.__new__(b._Handler);handler.server=server;handler.connection=Mock();handler.client_address=17
            handler.rfile=io.BytesIO(b'POST /v1/chat/completions HTTP/1.1\r\nHost: hermes-model\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
            handler.wfile=io.BytesIO();handler.handle();return handler.wfile.getvalue()
        raw=run();refusal=events[-1]
        self.assertEqual(refusal['event'],'HERMES_MODEL_REFUSED')
        self.assertEqual((refusal['code'],refusal['status']),('HERMES_MODEL_RESPONSE_INVALID',502))
        self.assertEqual((refusal['reason'],refusal['reason_detail']),('JSON_STATUS_INCOMPLETE','MAX_OUTPUT_TOKENS'))
        self.assertEqual(refusal['connection'],17)
        self.assertEqual(json.loads(raw.split(b'\r\n\r\n',1)[1]),{'error':{'message':'HERMES_MODEL_RESPONSE_INVALID',
            'type':'hermes_model_broker','code':'HERMES_MODEL_RESPONSE_INVALID'}})
        self.assertNotIn('PRIVATE_',json.dumps(events));self.assertNotIn(b'PRIVATE_',raw)
        self.assertEqual(len(calls),1)
        server._diagnostics=Mock(side_effect=OSError('PRIVATE_JOURNAL_ERROR'))
        self.assertEqual(run(),raw)
        self.assertEqual(len(calls),2)


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
        broker.event = Mock()
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

    def test_upstream_status_and_transport_errors_never_log_private_text(self):
        factory, connection, received = self.fake_connection(b.UpstreamResponse(429,
            {'x-request-id': 'provider-genuine-429'}, b'PRIVATE_PROVIDER_ERROR_BODY'))
        received.getheader.side_effect = lambda name: {'x-request-id': 'provider-genuine-429'}.get(name)
        events = []
        b._diagnostic_context.sink = lambda event, **metadata: events.append({'event': event, **metadata})
        try:
            upstream = b.HttpsModelUpstream(config(), 'PRIVATE_MODEL_CREDENTIAL', connection_factory=factory)
            result = upstream('/v1/chat/completions', request(), deadline=time.monotonic() + 5)
            self.assertEqual((result.status, result.body), (429, b''))
            self.assertEqual(events, [{'event': 'UPSTREAM_STATUS', 'status': 429,
                'provider_request_id': 'provider-genuine-429'}])
            connection.connect.side_effect = OSError(errno.ENETUNREACH, 'PRIVATE_NETWORK_ERROR_URL_TOKEN')
            with self.assertRaises(b.BrokerError):
                upstream('/v1/chat/completions', request(), deadline=time.monotonic() + 5)
            self.assertEqual(events[-1], {'event': 'TRANSPORT_ERROR',
                'error_class': 'OSError', 'errno': errno.ENETUNREACH})
            self.assertNotIn('PRIVATE_', json.dumps(events))
        finally:
            b._diagnostic_context.sink = None

    def test_request_metadata_has_only_fixed_field_names_and_technical_values(self):
        value = {'model': 'PRIVATE_MODEL_VALUE', 'input': 'PRIVATE_INPUT',
            'instructions': 'PRIVATE_INSTRUCTIONS', 'PRIVATE_UNKNOWN_FIELD': 'PRIVATE_VALUE',
            'tools': [{'name': 'PRIVATE_TOOL_NAME'}], 'tool_choice': 'PRIVATE_TOOL_CHOICE',
            'stream': 'PRIVATE_STREAM', 'background': True, 'store': False,
            'include': ['reasoning.encrypted_content', 'PRIVATE_INCLUDE']}
        metadata = b.request_metadata(value, config())
        self.assertNotIn('PRIVATE_', json.dumps(metadata))
        self.assertEqual(metadata['unknown_field_count'], 1)
        self.assertEqual(metadata['include'], ['reasoning.encrypted_content'])
        self.assertEqual(metadata['unknown_include_count'], 1)
        self.assertEqual(metadata['tools_count'], 1)
        self.assertEqual(metadata['model'], 'MISMATCH_OR_ABSENT')
        self.assertEqual(metadata['stream'], 'INVALID')
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            b._journal_event({'event': 'HERMES_MODEL_REQUEST_OBSERVED', **metadata})
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(output.getvalue())['unknown_field_count'], 1)

    def test_repeated_set_cookie_is_accepted_without_cookie_values_or_unknown_names_in_diagnostics(self):
        factory, connection, received = self.fake_connection()
        received.getheaders.return_value = [*response().headers.items(),
            ('Set-Cookie', 'PRIVATE_COOKIE_ONE'), ('set-cookie', 'PRIVATE_COOKIE_TWO'),
            ('PRIVATE_HEADER_NAME', 'PRIVATE_VALUE_ONE'), ('PRIVATE_HEADER_NAME', 'PRIVATE_VALUE_TWO')]
        events = []
        b._diagnostic_context.sink = lambda event, **metadata: events.append({'event': event, **metadata})
        try:
            upstream = b.HttpsModelUpstream(config(), 'PRIVATE_MODEL_CREDENTIAL', connection_factory=factory)
            result = upstream('/v1/chat/completions', request(), deadline=time.monotonic() + 5)
            b.validate_response(result, stream=False, path='/v1/chat/completions', config=config())
        finally:
            b._diagnostic_context.sink = None
        self.assertEqual(result.body, response().body)
        self.assertNotIn('set-cookie', result.headers)
        self.assertNotIn('PRIVATE_HEADER_NAME', result.headers)
        self.assertEqual(events[-1], {'event': 'UPSTREAM_HEADERS',
            'duplicate_header_names': ['set-cookie'], 'unknown_duplicate_header_count': 1})
        self.assertNotIn('PRIVATE_', json.dumps(events))

    def test_repeated_semantic_response_headers_still_refuse(self):
        for name, value in (('Content-Type', 'application/json'),
                ('X-Request-Id', 'another-provider-id'), ('Content-Encoding', 'identity'),
                ('Content-Length', '100')):
            with self.subTest(header=name):
                factory, _, received = self.fake_connection()
                received.getheaders.return_value = [*response().headers.items(),
                    (name.lower(), value), (name, value)]
                upstream = b.HttpsModelUpstream(config(), 'test-model-token', connection_factory=factory)
                with self.assertRaisesRegex(b.BrokerError, '^HERMES_MODEL_RESPONSE_INVALID$'):
                    upstream('/v1/chat/completions', request(), deadline=time.monotonic() + 5)
                received.read1.assert_not_called()


class StartupDiagnosticTests(unittest.TestCase):
    ARGS = ['--config', '/sealed/config.json', '--credential-file', '/sealed/model.key',
        '--socket-path', '/run/hermes-model/model.sock']

    def test_startup_reports_only_fixed_phase_and_broker_code(self):
        raw = json.dumps(vars(config(worker_uid=10003))).encode()
        token = b'PRIVATE_CREDENTIAL_SENTINEL'
        cases = [
            ('CONFIG_READ', 'HERMES_MODEL_FILE_PARENT_MODE_REFUSED',
                {'_read_protected_file': [b.BrokerError('HERMES_MODEL_FILE_PARENT_MODE_REFUSED')]}),
            ('CONFIG_VALIDATE', 'HERMES_MODEL_JSON_INVALID', {'_read_protected_file': [b'not-json']}),
            ('CREDENTIAL_READ', 'HERMES_MODEL_FILE_OPEN_EACCES',
                {'_read_protected_file': [raw, b.BrokerError('HERMES_MODEL_FILE_OPEN_EACCES')]}),
            ('CREDENTIAL_VALIDATE', 'HERMES_MODEL_CREDENTIAL_INVALID',
                {'_read_protected_file': [raw, b'\xff']}),
            ('UPSTREAM', 'HERMES_MODEL_CREDENTIAL_INVALID',
                {'HttpsModelUpstream': b.BrokerError('HERMES_MODEL_CREDENTIAL_INVALID')}),
            ('SOCKET', 'HERMES_MODEL_SOCKET_INVALID',
                {'HermesModelBroker': b.BrokerError('HERMES_MODEL_SOCKET_INVALID')}),
        ]
        for phase, code, failures in cases:
            with self.subTest(phase=phase), ExitStack() as stack:
                stack.enter_context(patch.object(b.os, 'geteuid', return_value=10004))
                reader = stack.enter_context(patch.object(b, '_read_protected_file', side_effect=[raw, token]))
                upstream = stack.enter_context(patch.object(b, 'HttpsModelUpstream'))
                broker = stack.enter_context(patch.object(b, 'HermesModelBroker'))
                targets = {'_read_protected_file': reader, 'HttpsModelUpstream': upstream,
                    'HermesModelBroker': broker}
                for name, failure in failures.items():
                    targets[name].side_effect = failure
                with self.assertRaises(SystemExit) as caught:
                    b.main(self.ARGS)
                self.assertEqual(str(caught.exception),
                    'HERMES_MODEL_BROKER_START_REFUSED phase=' + phase + ' code=' + code)
                self.assertNotIn(token.decode(), str(caught.exception))
                self.assertNotIn('/sealed', str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_type_value_os_and_unknown_error_text_cannot_escape(self):
        raw = json.dumps(vars(config(worker_uid=10003))).encode()
        cases = [(TypeError('PRIVATE_TYPE_MESSAGE'), 'HERMES_MODEL_TYPE_ERROR'),
            (ValueError('PRIVATE_VALUE_MESSAGE'), 'HERMES_MODEL_VALUE_ERROR'),
            (OSError('PRIVATE_OS_MESSAGE'), 'HERMES_MODEL_OS_ERROR'),
            (b.BrokerError('PRIVATE_CREDENTIAL_SENTINEL'), 'HERMES_MODEL_ERROR_UNCLASSIFIED')]
        for error, code in cases:
            with self.subTest(code=code), patch.object(b.os, 'geteuid', return_value=10004), \
                    patch.object(b, '_read_protected_file', return_value=raw), \
                    patch.object(b, 'BrokerConfig', side_effect=error):
                with self.assertRaises(SystemExit) as caught:
                    b.main(self.ARGS)
                self.assertEqual(str(caught.exception),
                    'HERMES_MODEL_BROKER_START_REFUSED phase=CONFIG_VALIDATE code=' + code)
                self.assertNotIn('PRIVATE_', str(caught.exception))
        self.assertEqual(str(b._startup_refusal('PRIVATE_PHASE', 'PRIVATE_CODE')),
            'HERMES_MODEL_BROKER_START_REFUSED phase=UNCLASSIFIED code=HERMES_MODEL_ERROR_UNCLASSIFIED')

    def test_file_parent_and_credential_permissions_keep_their_exact_refusal(self):
        parent = dict(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
        leaf = dict(st_uid=10004, st_mode=stat.S_IFREG | 0o600, st_size=20)
        cases = [({**parent, 'st_uid': 10003}, leaf, 'HERMES_MODEL_FILE_PARENT_OWNER_REFUSED'),
            ({**parent, 'st_mode': stat.S_IFDIR | 0o775}, leaf, 'HERMES_MODEL_FILE_PARENT_MODE_REFUSED'),
            (parent, {**leaf, 'st_uid': 10003}, 'HERMES_MODEL_FILE_OWNER_REFUSED'),
            (parent, {**leaf, 'st_mode': stat.S_IFREG | 0o620}, 'HERMES_MODEL_FILE_MODE_REFUSED'),
            (parent, {**leaf, 'st_mode': stat.S_IFREG | 0o640}, 'HERMES_MODEL_CREDENTIAL_MODE_REFUSED')]
        for observed_parent, observed_leaf, code in cases:
            with self.subTest(code=code), patch.object(b.os, 'geteuid', return_value=10004), \
                    patch.object(b.os, 'open', side_effect=[91, 92, 93]), \
                    patch.object(b.os, 'close'), patch.object(b.os, 'fstat', side_effect=[
                        SimpleNamespace(**observed_parent), SimpleNamespace(**observed_leaf)]):
                with self.assertRaises(b.BrokerError) as caught:
                    b._read_protected_file('/sealed/model.key', credential=True)
                self.assertEqual(str(caught.exception), code)

    def test_open_permission_error_reports_fixed_errno_without_path_or_message(self):
        error = PermissionError(errno.EACCES, 'PRIVATE_CREDENTIAL_SENTINEL', '/private/model.key')
        with patch.object(b.os, 'open', side_effect=error):
            with self.assertRaises(b.BrokerError) as caught:
                b._read_protected_file('/sealed/model.key', credential=True)
        self.assertEqual(str(caught.exception), 'HERMES_MODEL_FILE_OPEN_EACCES')


class StaleSocketRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'model.sock'

    def tearDown(self):
        self.temp.cleanup()

    def stale_socket(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.bind(str(self.path))

    def test_owned_stale_socket_is_recovered_by_next_broker_bind(self):
        self.stale_socket()
        broker = b.HermesModelBroker(str(self.path), config(), Mock(), secure_socket=False)
        try:
            self.assertTrue(self.path.is_socket())
            self.assertEqual(self.path.stat().st_uid, os.geteuid())
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o660)
        finally:
            broker.server_close()
        self.assertFalse(self.path.exists())

    def test_live_socket_is_never_unlinked(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.path))
            listener.listen(1)
            before = self.path.lstat()
            with self.assertRaisesRegex(b.BrokerError, '^HERMES_MODEL_SOCKET_EXISTS$'):
                b.remove_stale_socket(self.path)
            after = self.path.lstat()
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))

    def test_foreign_socket_is_refused_before_probe(self):
        observed = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660,
            st_uid=os.geteuid() + 1, st_dev=1, st_ino=2)
        with patch.object(Path, 'lstat', return_value=observed), patch.object(b.socket, 'socket') as probe:
            with self.assertRaisesRegex(b.BrokerError, '^HERMES_MODEL_SOCKET_EXISTS$'):
                b.remove_stale_socket(self.path)
            probe.assert_not_called()

    def test_symlink_and_regular_file_are_never_removed_or_followed(self):
        target = self.path.with_name('keep.txt')
        target.write_text('keep')
        self.path.symlink_to(target)
        with self.assertRaisesRegex(b.BrokerError, '^HERMES_MODEL_SOCKET_EXISTS$'):
            b.remove_stale_socket(self.path)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), 'keep')
        self.path.unlink()
        self.path.write_text('keep regular file')
        with self.assertRaisesRegex(b.BrokerError, '^HERMES_MODEL_SOCKET_EXISTS$'):
            b.remove_stale_socket(self.path)
        self.assertEqual(self.path.read_text(), 'keep regular file')

    def test_timeout_and_other_connect_errors_do_not_unlink(self):
        self.stale_socket()
        for error in (TimeoutError(), PermissionError(errno.EACCES, 'private path')):
            with self.subTest(error=type(error).__name__), patch.object(b.socket, 'socket') as factory:
                factory.return_value.__enter__.return_value.connect.side_effect = error
                with self.assertRaisesRegex(b.BrokerError, '^HERMES_MODEL_SOCKET_EXISTS$'):
                    b.remove_stale_socket(self.path)
                self.assertTrue(self.path.is_socket())

    def test_changed_inode_owner_or_type_after_refused_connect_is_never_unlinked(self):
        initial = dict(st_mode=stat.S_IFSOCK | 0o660, st_uid=os.geteuid(), st_dev=1, st_ino=2)
        for changed in ({**initial, 'st_ino': 3}, {**initial, 'st_uid': os.geteuid() + 1},
                {**initial, 'st_mode': stat.S_IFREG | 0o600}):
            with self.subTest(changed=changed), patch.object(Path, 'lstat', side_effect=[
                    SimpleNamespace(**initial), SimpleNamespace(**changed)]), \
                    patch.object(Path, 'unlink') as unlink, patch.object(b.socket, 'socket') as factory:
                factory.return_value.__enter__.return_value.connect.side_effect = ConnectionRefusedError(
                    errno.ECONNREFUSED, 'refused')
                with self.assertRaisesRegex(b.BrokerError, '^HERMES_MODEL_SOCKET_CHANGED$'):
                    b.remove_stale_socket(self.path)
                unlink.assert_not_called()


@unittest.skipIf(os.getuid() == 0, 'production worker UID must be nonroot')
class UnixHttpIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.socket_path = str(Path(self.temp.name) / 'model.sock')
        self.calls = []
        self.events = []
        self.output = response()
        def upstream(path, body, *, deadline):
            self.calls.append((path, body))
            return self.output
        self.server = b.HermesModelBroker(self.socket_path, config(), upstream, secure_socket=False,
            diagnostics=self.events.append)
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

    def test_request_journal_correlates_safe_events_and_never_logs_input_or_headers(self):
        result = self.send(request(messages=[{'role': 'user', 'content': 'PRIVATE_PROMPT_CONTENT'}]),
            headers={'authorization': 'Bearer PRIVATE_WORKER_AUTHORIZATION'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual([row['event'] for row in self.events], [
            'HERMES_MODEL_PEER_ACCEPTED', 'HERMES_MODEL_REQUEST_OBSERVED',
            'HERMES_MODEL_REQUEST_VALIDATED', 'HERMES_MODEL_UPSTREAM_STARTED',
            'HERMES_MODEL_UPSTREAM_STATUS', 'HERMES_MODEL_RESPONSE_VERIFIED'])
        self.assertTrue(all(row.get('connection') == 1 for row in self.events[1:]))
        self.assertTrue(all(row['count'] == 1 for row in self.events))
        self.assertNotIn('PRIVATE_', json.dumps(self.events))
        result = self.send(json.dumps({'model': 'hermes-fixed-model', 'messages': [
            {'role': 'user', 'content': 'PRIVATE_REJECTED_PROMPT'}],
            'PRIVATE_UNKNOWN_FIELD': 'PRIVATE_UNKNOWN_VALUE'}).encode())
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.events[-1]['code'], 'HERMES_MODEL_REQUEST_INVALID')
        self.assertEqual(self.events[-2]['unknown_field_count'], 1)
        self.assertNotIn('PRIVATE_', json.dumps(self.events))

    def test_journal_failure_does_not_change_single_dispatch_or_guards(self):
        self.server._diagnostics = Mock(side_effect=OSError('PRIVATE_JOURNAL_FAILURE'))
        self.assertEqual(self.send(request()).status_code, 200)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.send(request(tools=[{'type': 'function'}])).status_code, 400)
        self.assertEqual(len(self.calls), 1)

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
