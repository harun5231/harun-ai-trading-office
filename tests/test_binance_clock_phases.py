"""Offline clock regressions using real urllib with a synthetic TLS socket."""
import io
import json
import threading
import unittest
from unittest.mock import patch

from worker.binance_private import BinanceCheckError, BinanceReadOnly


SERVER = 1700000000000


class ResponseBytes(io.BytesIO):
    def __init__(self, raw, wire):
        super().__init__(raw)
        self.wire = wire
        self.begun = False
        self.body_begun = False

    def readline(self, *args):
        if not self.begun:
            self.begun = True
            self.wire.elapsed[0] += self.wire.response_seconds
        return super().readline(*args)

    def read(self, *args):
        if not self.body_begun:
            self.body_begun = True
            self.wire.elapsed[0] += self.wire.body_seconds
        return super().read(*args)


class FakeSocket:
    def __init__(self, wire):
        self.wire = wire

    def sendall(self, data):
        self.wire.sent.append(bytes(data))

    def makefile(self, mode, *args):
        body = json.dumps(self.wire.body).encode('ascii')
        raw = (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
               + str(len(body)).encode('ascii') + b'\r\nConnection: close\r\n\r\n' + body)
        return ResponseBytes(raw, self.wire)

    def close(self):
        pass


class FakeTLS:
    """Exercise real HTTPSHandler/HTTPSConnection after replacing TLS connect."""
    def __init__(self, elapsed, *, connect_seconds=0.0, response_seconds=0.1, body_seconds=0.0):
        self.elapsed = elapsed
        self.connect_seconds = connect_seconds
        self.response_seconds = response_seconds
        self.body_seconds = body_seconds
        self.body = {'serverTime': SERVER}
        self.sent = []
        self.connections = 0
        self.connects = 0

    def connect(self, connection):
        self.connections += 1
        self.elapsed[0] += self.connect_seconds
        connection.sock = FakeSocket(self)
        self.connects += 1


class ConnectPhaseClockTests(unittest.TestCase):
    def setUp(self):
        self.elapsed = [10.0]
        with patch('worker.binance_private.read_secret', return_value='offline-fixture-credential'):
            self.client = BinanceReadOnly(clock=lambda: self.elapsed[0])

    def fake_tls(self, **options):
        wire = FakeTLS(self.elapsed, **options)
        self.addCleanup(patch.stopall)
        patch('http.client.HTTPSConnection.connect', new=lambda connection: wire.connect(connection)).start()
        # Both a transport fallback and the sample callback see the same monotonic source.
        patch('worker.binance_private.time.monotonic', new=lambda: self.elapsed[0]).start()
        return wire

    def test_five_second_connect_and_tenth_second_wire_yields_fresh_receipt_anchor(self):
        wire = self.fake_tls(connect_seconds=5.0, response_seconds=0.1)
        with patch('time.time', side_effect=AssertionError('Wall clock used')):
            self.client.sync_time()
            self.assertEqual(self.client.server_time_ms(), SERVER)
            self.elapsed[0] += 1.0
            self.assertEqual(self.client.server_time_ms(), SERVER + 1000)
        self.assertEqual(wire.connects, 1)
        self.assertEqual(len(wire.sent), 1)
        self.assertTrue(wire.sent[0].startswith(b'GET /fapi/v1/time HTTP/1.1\r\n'))
        self.assertNotIn(b'X-Mbx-Apikey:', wire.sent[0])
        self.assertNotIn(b'signature', wire.sent[0])

    def test_two_and_half_second_request_response_still_fails_once(self):
        wire = self.fake_tls(response_seconds=2.5)
        with self.assertRaisesRegex(BinanceCheckError, '^BINANCE_CLOCK_ERROR$'):
            self.client.sync_time()
        self.assertEqual(wire.connects, 1)
        self.assertEqual(len(wire.sent), 1)
        with self.assertRaisesRegex(BinanceCheckError, '^BINANCE_CLOCK_ERROR$'):
            self.client.server_time_ms()

    def test_slow_body_receipt_is_included_in_wire_guard(self):
        wire = self.fake_tls(response_seconds=0.1, body_seconds=2.5)
        with self.assertRaisesRegex(BinanceCheckError, '^BINANCE_CLOCK_ERROR$'):
            self.client.sync_time()
        self.assertEqual(wire.connects, 1)

    def test_failed_slow_sample_preserves_previous_valid_anchor(self):
        wire = self.fake_tls()
        self.client.sync_time()
        previous = self.client.server_time_ms()
        wire.body = {'serverTime': SERVER + 1000000}
        wire.response_seconds = 2.5
        with self.assertRaisesRegex(BinanceCheckError, '^BINANCE_CLOCK_ERROR$'):
            self.client.sync_time()
        self.assertEqual(self.client.server_time_ms(), previous + 2500)
        self.assertEqual(wire.connects, 2)

    def test_invalid_server_sample_preserves_previous_valid_anchor(self):
        wire = self.fake_tls()
        self.client.sync_time()
        previous = self.client.server_time_ms()
        wire.body = {'serverTime': True}
        wire.response_seconds = 0.25
        with self.assertRaisesRegex(BinanceCheckError, '^BINANCE_CLOCK_ERROR$'):
            self.client.sync_time()
        self.assertEqual(self.client.server_time_ms(), previous + 250)
        self.assertEqual(wire.connects, 2)


class SharedClockConcurrencyTests(unittest.TestCase):
    def test_sync_requests_on_one_client_do_not_overlap(self):
        entered = threading.Event()
        release = threading.Event()
        second_started = threading.Event()
        second_entered = threading.Event()
        guard = threading.Lock()
        active = [0]
        maximum = [0]
        calls = []
        errors = []
        def transport(url, headers):
            with guard:
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
                calls.append(threading.current_thread().name)
                first = len(calls) == 1
            if first:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('Fixture release missing')
            else:
                second_entered.set()
            with guard:
                active[0] -= 1
            return {'serverTime': SERVER + 100 * len(calls)}
        with patch('worker.binance_private.read_secret', return_value='offline-fixture-credential'):
            client = BinanceReadOnly(transport=transport, clock=lambda: 10.0)
        def synchronize():
            if threading.current_thread().name == 'clock-second':
                second_started.set()
            try:
                client.sync_time()
            except Exception as error:
                errors.append(error)
        first = threading.Thread(name='clock-first', target=synchronize, daemon=True)
        second = threading.Thread(name='clock-second', target=synchronize, daemon=True)
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        self.assertTrue(second_started.wait(1))
        # Give the contender a scheduling turn while the first sample is in flight.
        second_entered.wait(0.05)
        release.set()
        first.join(1)
        second.join(1)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(maximum[0], 1)
        self.assertEqual(len(calls), 2)


class OuterRecoveryClockTests(unittest.TestCase):
    def test_outer_clock_recovery_signs_a_fresh_retry_exactly_once(self):
        from urllib.parse import parse_qs, urlsplit
        from worker.binance_office import _read
        calls = []
        syncs = [0]
        signed = []
        def transport(url, headers):
            path = urlsplit(url).path
            calls.append(path)
            if path == '/fapi/v1/time':
                syncs[0] += 1
                return {'serverTime': SERVER + 1000 * (syncs[0] - 1)}
            signed.append(parse_qs(urlsplit(url).query))
            if len(signed) == 1:
                raise BinanceCheckError('BINANCE_CLOCK_ERROR')
            return {'assets': [], 'positions': []}
        with patch('worker.binance_private.read_secret', return_value='offline-fixture-credential'):
            client = BinanceReadOnly(transport=transport, clock=lambda: 10.0)
        client.sync_time()
        self.assertEqual(_read(client, '/fapi/v3/account'), {'assets': [], 'positions': []})
        self.assertEqual(calls, ['/fapi/v1/time', '/fapi/v3/account'] * 2)
        self.assertEqual([query['timestamp'] for query in signed], [[str(SERVER)], [str(SERVER + 1000)]])
        self.assertEqual([query['recvWindow'] for query in signed], [['5000'], ['5000']])
        self.assertNotEqual(signed[0]['signature'], signed[1]['signature'])

    def test_outer_repeated_provider_clock_error_stops_after_one_retry(self):
        from urllib.parse import urlsplit
        from worker.binance_office import _read
        calls = []
        def transport(url, headers):
            path = urlsplit(url).path
            calls.append(path)
            if path == '/fapi/v1/time':
                return {'serverTime': SERVER}
            raise BinanceCheckError('BINANCE_CLOCK_ERROR')
        with patch('worker.binance_private.read_secret', return_value='offline-fixture-credential'):
            client = BinanceReadOnly(transport=transport, clock=lambda: 10.0)
        client.sync_time()
        with self.assertRaisesRegex(BinanceCheckError, '^BINANCE_CLOCK_ERROR$'):
            _read(client, '/fapi/v3/account')
        self.assertEqual(calls, ['/fapi/v1/time', '/fapi/v3/account'] * 2)



class SigningAdmissionTests(unittest.TestCase):
    def test_timestamp_follows_local_admission_wait(self):
        from urllib.parse import parse_qs,urlsplit
        elapsed=[10.0];sent=[]
        def transport(url,headers):
            if urlsplit(url).path=='/fapi/v1/time':return {'serverTime':SERVER}
            sent.append(parse_qs(urlsplit(url).query));return {}
        with patch('worker.binance_private.read_secret',return_value='offline-fixture-credential'):
            client=BinanceReadOnly(transport=transport,clock=lambda:elapsed[0])
        client.sync_time()
        def admission():elapsed[0]+=3
        with patch('worker.binance_private.check_research_read',side_effect=admission)as guard:
            client.signed_get('/fapi/v3/account')
        self.assertEqual(guard.call_count,1)
        self.assertEqual(sent[0]['timestamp'],[str(SERVER+3000)])
        self.assertEqual(sent[0]['recvWindow'],['5000'])

    def test_signed_read_waits_for_clock_lock_before_timestamp(self):
        from urllib.parse import parse_qs,urlsplit
        elapsed=[10.0];sent=[];attempted=threading.Event();done=threading.Event();errors=[]
        def transport(url,headers):
            if urlsplit(url).path=='/fapi/v1/time':return {'serverTime':SERVER}
            sent.append(parse_qs(urlsplit(url).query));return {}
        with patch('worker.binance_private.read_secret',return_value='offline-fixture-credential'):
            client=BinanceReadOnly(transport=transport,clock=lambda:elapsed[0])
        client.sync_time()
        def request():
            attempted.set()
            try:client.signed_get('/fapi/v3/account')
            except Exception as error:errors.append(type(error).__name__)
            finally:done.set()
        with client._time_lock:
            thread=threading.Thread(target=request);thread.start();self.assertTrue(attempted.wait(1))
            self.assertFalse(done.wait(.02));elapsed[0]+=4
        thread.join(1);self.assertEqual(errors,[]);self.assertFalse(thread.is_alive())
        self.assertEqual(sent[0]['timestamp'],[str(SERVER+4000)])

if __name__ == '__main__':
    unittest.main()
