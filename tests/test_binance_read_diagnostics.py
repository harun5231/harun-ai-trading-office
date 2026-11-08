"""Real urllib, fake TLS/DNS only: no private account or order is contacted."""
import errno
import hashlib
import hmac
import io
import json
import socket
import ssl
import traceback
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from worker import binance_private as private
from worker.research_guard import ResearchReadPaused, research_reads
from tests.test_binance_clock_phases import FakeTLS,FakeSocket


class ReadDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.elapsed = [10.0]
        with patch('worker.binance_private.read_secret', return_value='offline-fixture-credential'):
            self.client = private.BinanceReadOnly(clock=lambda: self.elapsed[0])
        self.wire = FakeTLS(self.elapsed)
        operation = patch('http.client.HTTPSConnection.connect', new=lambda connection: self.wire.connect(connection))
        operation.start(); self.addCleanup(operation.stop)
        operation = patch('worker.binance_private.time.monotonic', new=lambda: self.elapsed[0])
        operation.start(); self.addCleanup(operation.stop)
        self.client.sync_time()
        self.anchor = self.client._server, self.client._synced
        self.wire.body = {'assets': [], 'positions': []}
        self.connect_calls = 0

    def fail_connect(self, failures, error=None, duration=4.0):
        error = error if error is not None else socket.gaierror(socket.EAI_NONAME, 'PRIVATE_DNS_REASON signature=PRIVATE')
        def connect(connection):
            self.connect_calls += 1
            if self.connect_calls <= failures:
                self.elapsed[0] += duration
                raise error
            return self.wire.connect(connection)
        operation = patch('http.client.HTTPSConnection.connect', new=connect)
        operation.start(); self.addCleanup(operation.stop)

    def signed(self):
        return self.client.signed_get('/fapi/v3/account')

    def assert_sanitized(self, value):
        value = value if isinstance(value, str) else json.dumps(value)
        for forbidden in ('PRIVATE', 'offline-fixture-credential', 'signature=', 'https://'):
            self.assertNotIn(forbidden, value)

    def test_proven_dns_noname_before_write_retries_once_with_fresh_signature(self):
        self.fail_connect(1)
        self.wire.connect_seconds = .5
        with patch('worker.binance_private.signature', wraps=private.signature) as signer:
            with self.assertLogs('worker.binance_private', level='WARNING') as captured:
                self.assertEqual(self.signed(), self.wire.body)
        self.assertEqual(self.connect_calls, 2)
        self.assertEqual(len(self.wire.sent), 2)  # One original time GET and one successful account GET.
        self.assertEqual((self.client._server, self.client._synced), self.anchor)
        self.assertEqual(sum(b'GET /fapi/v1/time ' in row for row in self.wire.sent), 1)
        queries = [call.args[1] for call in signer.call_args_list]
        self.assertNotEqual(parse_qs(queries[0])['timestamp'], parse_qs(queries[1])['timestamp'])
        selector = self.wire.sent[-1].split(b' ', 2)[1].decode('ascii')
        unsigned, actual = urlsplit(selector).query.rsplit('&signature=', 1)
        self.assertEqual(actual, hmac.new(b'offline-fixture-credential', unsigned.encode(), hashlib.sha256).hexdigest())
        self.assertEqual(unsigned, queries[-1])
        self.assertEqual(parse_qs(unsigned)['recvWindow'], ['5000'])
        self.assert_sanitized('\n'.join(captured.output))
        self.assertIsNone(private._read_diagnostic.get())
        self.assertIsNone(private._signed_request.get())

    def test_two_dns_failures_stop_preserve_anchor_and_propagate_metadata(self):
        self.fail_connect(2)
        with self.assertLogs('worker.binance_private', level='WARNING') as captured:
            with self.assertRaises(private.BinanceCheckError) as caught: self.signed()
        self.assertEqual(caught.exception.args, ('BINANCE_ACCOUNT_UNAVAILABLE',))
        value = caught.exception.transport_diagnostics
        self.assertEqual(value['phase'], 'CONNECT'); self.assertEqual(value['delivery'], 'NOT_SENT')
        self.assertEqual(value['exception_class'], 'gaierror'); self.assertEqual(value['errno'], socket.EAI_NONAME)
        self.assertEqual(value['attempts'], 2); self.assertEqual(value['retry_count'], 1)
        self.assertEqual(self.connect_calls, 2); self.assertEqual(len(self.wire.sent), 1)
        self.assertEqual((self.client._server, self.client._synced), self.anchor)
        self.assert_sanitized(value); self.assert_sanitized('\n'.join(captured.output))
        self.assertIsNone(private._read_diagnostic.get())

    def test_direct_signed_url_without_prepare_context_is_not_replayed(self):
        query='recvWindow=5000&timestamp=1700000000000'
        url=private.BASE+'/fapi/v3/account?'+query+'&signature='+private.signature('offline-fixture-credential',query)
        headers={'X-MBX-APIKEY':'offline-fixture-credential','Accept':'application/json','User-Agent':'fixture'}
        self.fail_connect(1)
        with self.assertLogs('worker.binance_private',level='WARNING')as captured:
            with self.assertRaises(private.BinanceCheckError)as caught:private.read_only_get(url,headers)
        self.assertEqual(self.connect_calls,1);self.assertEqual(len(self.wire.sent),1)
        self.assertEqual(caught.exception.transport_diagnostics['attempts'],1)
        self.assert_sanitized('\n'.join(captured.output))

    def test_remaining_deadline_reduces_socket_timeout_after_tls_and_before_headers(self):
        self.wire.connect_seconds=29.2;timeouts=[]
        original=FakeSocket.sendall
        def send(sock,data):
            original(sock,data);self.elapsed[0]+=.25
        def settimeout(sock,value):sock.fixture_timeout=value;timeouts.append(value)
        with patch.object(FakeSocket,'gettimeout',lambda sock:getattr(sock,'fixture_timeout',15),create=True),\
             patch.object(FakeSocket,'settimeout',settimeout,create=True),\
             patch.object(FakeSocket,'sendall',send):
            self.assertEqual(self.signed(),self.wire.body)
        self.assertEqual(len(timeouts),2)
        self.assertTrue(0<timeouts[1]<timeouts[0]<1)
        self.assertEqual(len(self.wire.sent),2)

    def test_budget_exhausted_before_open_does_not_send_or_leave_context(self):
        def delayed_guard():self.elapsed[0]+=31
        with patch('worker.binance_private.check_research_read',side_effect=delayed_guard),\
             patch('worker.binance_private.build_opener')as opener,\
             self.assertLogs('worker.binance_private',level='WARNING'):
            with self.assertRaises(private.BinanceCheckError)as caught:
                private.read_only_get(private.BASE+private.TIME_PATH,{})
        self.assertEqual(str(caught.exception),'BINANCE_ACCOUNT_UNAVAILABLE')
        self.assertEqual(caught.exception.transport_diagnostics['delivery'],'NOT_SENT')
        opener.assert_not_called();self.assertIsNone(private._read_diagnostic.get())

    def test_dns_again_is_the_only_other_allowlisted_resolver_failure(self):
        self.fail_connect(1, socket.gaierror(socket.EAI_AGAIN, 'PRIVATE'))
        with self.assertLogs('worker.binance_private', level='WARNING'):
            self.assertEqual(self.signed(), self.wire.body)
        self.assertEqual(self.connect_calls, 2)

    def test_exhausted_monotonic_budget_prevents_extra_attempt(self):
        self.fail_connect(2, duration=31)
        with self.assertLogs('worker.binance_private', level='WARNING'):
            with self.assertRaises(private.BinanceCheckError) as caught: self.signed()
        self.assertEqual(self.connect_calls, 1); self.assertEqual(len(self.wire.sent), 1)
        self.assertEqual(caught.exception.transport_diagnostics['attempts'], 1)
        self.assertEqual((self.client._server, self.client._synced), self.anchor)

    def test_no_retry_for_timeout_tcp_ssl_generic_os_or_other_dns_error(self):
        for error in (TimeoutError('PRIVATE'), ConnectionRefusedError(errno.ECONNREFUSED, 'PRIVATE'),
                ssl.SSLEOFError(ssl.SSL_ERROR_EOF, 'PRIVATE'), OSError(socket.EAI_NONAME, 'PRIVATE'),
                socket.gaierror(socket.EAI_FAIL, 'PRIVATE'), URLError('PRIVATE_EAI_NONAME')):
            with self.subTest(error=type(error).__name__):
                self.connect_calls = 0; self.fail_connect(4, error)
                with self.assertLogs('worker.binance_private', level='WARNING'):
                    with self.assertRaises(private.BinanceCheckError) as caught: self.signed()
                self.assertEqual(self.connect_calls, 1)
                self.assertEqual(caught.exception.transport_diagnostics['delivery'], 'NOT_SENT')
                self.assertEqual(caught.exception.transport_diagnostics['attempts'], 1)
                self.assert_sanitized(caught.exception.transport_diagnostics)

    def test_http_policy_errors_keep_classification_without_dns_retry(self):
        for status, code, expected in ((401, -2015, 'BINANCE_AUTH_FAILED'),
                (403, -1002, 'BINANCE_PERMISSION_DENIED'), (429, -1003, 'BINANCE_ACCOUNT_UNAVAILABLE'),
                (500, -1001, 'BINANCE_ACCOUNT_UNAVAILABLE'), (400, -1021, 'BINANCE_CLOCK_ERROR')):
            with self.subTest(status=status):
                self.wire.responder = lambda request: (status, {'code': code, 'msg': 'PRIVATE_PROVIDER'})
                count = len(self.wire.sent)
                with self.assertLogs('worker.binance_private', level='WARNING'):
                    with self.assertRaises(private.BinanceCheckError) as caught: self.signed()
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual(len(self.wire.sent), count + 1)
                value = caught.exception.transport_diagnostics
                self.assertEqual(value['delivery'], 'RESPONSE_RECEIVED')
                self.assertEqual(value['http_status'], status); self.assertEqual(value['binance_code'], code)
                self.assertEqual(value['attempts'], 1); self.assert_sanitized(value)

    def test_header_failure_after_write_stays_unconfirmed_without_retry(self):
        def timeout(request): raise TimeoutError('PRIVATE_HEADER_TIMEOUT')
        self.wire.responder = timeout
        count = len(self.wire.sent)
        with self.assertLogs('worker.binance_private', level='WARNING'):
            with self.assertRaises(private.BinanceCheckError) as caught: self.signed()
        value = caught.exception.transport_diagnostics
        self.assertEqual(value['phase'], 'HEADERS'); self.assertEqual(value['delivery'], 'DELIVERY_UNCONFIRMED')
        self.assertEqual(value['exception_class'], 'TimeoutError')
        self.assertEqual(value['attempts'], 1); self.assertEqual(len(self.wire.sent), count + 1)

    def test_body_dns_shaped_error_is_not_retried_after_response(self):
        with patch('tests.test_binance_clock_phases.ResponseBytes.read', side_effect=socket.gaierror(socket.EAI_NONAME, 'PRIVATE_BODY')):
            with self.assertLogs('worker.binance_private', level='WARNING'):
                with self.assertRaises(private.BinanceCheckError) as caught: self.signed()
        value = caught.exception.transport_diagnostics
        self.assertEqual(value['phase'], 'BODY'); self.assertEqual(value['delivery'], 'RESPONSE_RECEIVED')
        self.assertEqual(value['errno'], socket.EAI_NONAME); self.assertEqual(value['attempts'], 1)

    def test_research_pause_between_attempts_prevents_second_network_operation(self):
        self.fail_connect(1)
        with research_reads(lambda: self.elapsed[0] < 14):
            with self.assertLogs('worker.binance_private', level='WARNING'):
                with self.assertRaises(ResearchReadPaused): self.signed()
        self.assertEqual(self.connect_calls, 1); self.assertEqual(len(self.wire.sent), 1)
        self.assertIsNone(private._read_diagnostic.get()); self.assertIsNone(private._signed_request.get())

    def test_failed_time_dns_sync_never_replaces_previous_anchor(self):
        self.fail_connect(2)
        with self.assertLogs('worker.binance_private', level='WARNING'):
            with self.assertRaises(private.BinanceCheckError): self.client.sync_time()
        self.assertEqual((self.client._server, self.client._synced), self.anchor)
        self.assertEqual(self.connect_calls, 2); self.assertEqual(len(self.wire.sent), 1)

    def test_new_client_after_interruption_reads_once_without_replaying_failed_send(self):
        self.fail_connect(2)
        with self.assertLogs('worker.binance_private', level='WARNING'):
            with self.assertRaises(private.BinanceCheckError): self.signed()
        failed_count = len(self.wire.sent)
        with patch('worker.binance_private.read_secret', return_value='offline-fixture-credential'):
            fresh = private.BinanceReadOnly(clock=lambda: self.elapsed[0])
        self.wire.body = {'serverTime': 1700000100000}; fresh.sync_time()
        self.wire.body = {'assets': [], 'positions': []}
        self.assertEqual(fresh.signed_get('/fapi/v3/account'), self.wire.body)
        self.assertEqual(len(self.wire.sent), failed_count + 2)
        self.assertIsNone(private._read_diagnostic.get())

    def test_safe_helper_unwraps_urlerror_without_messages_or_unknown_names(self):
        error = URLError(socket.gaierror(socket.EAI_NONAME, 'PRIVATE_URL?signature=PRIVATE'))
        value = private.transport_error_metadata(error, 'CONNECT', 'NOT_SENT')
        self.assertEqual(value['exception_chain'], ['URLError', 'gaierror'])
        self.assertTrue(private.dns_not_sent_retryable(value)); self.assert_sanitized(value)
        for phase, delivery in (('READ', 'NOT_SENT'), ('CONNECT', 'DELIVERY_UNCONFIRMED'), ('BODY', 'RESPONSE_RECEIVED')):
            self.assertFalse(private.dns_not_sent_retryable(private.transport_error_metadata(error, phase, delivery)))
        failure = type('PRIVATE_CLASS', (OSError,), {})(987654, 'PRIVATE')
        value = private.safe_transport_error(failure)
        self.assertEqual(value['exception_class'], 'Exception'); self.assertIsNone(value['errno'])
        self.assert_sanitized(value)

    def test_get_preserves_only_finite_safe_metadata_and_single_code_argument(self):
        error = private.BinanceCheckError('BINANCE_ACCOUNT_UNAVAILABLE')
        error.transport_diagnostics = dict(phase='CONNECT', delivery='NOT_SENT', exception_class='gaierror',
            errno=socket.EAI_NONAME, attempts=2, retry_count=1, raw='PRIVATE', url='https://PRIVATE')
        self.client._transport = Mock(side_effect=error)
        with self.assertRaises(private.BinanceCheckError) as caught: self.signed()
        self.assertEqual(caught.exception.args, ('BINANCE_ACCOUNT_UNAVAILABLE',))
        self.assertEqual(caught.exception.transport_diagnostics['errno'], socket.EAI_NONAME)
        self.assertNotIn('raw', caught.exception.transport_diagnostics)
        self.assertNotIn('url', caught.exception.transport_diagnostics)
        self.assert_sanitized(caught.exception.transport_diagnostics)
        self.assertIsNone(private._signed_request.get())


if __name__ == '__main__': unittest.main()
