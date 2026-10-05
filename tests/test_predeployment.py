"""Additional pre-deployment regressions; no provider credentials or network calls."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from worker.core import Ledger, Review
from worker.neuroapi import NeuroAPI, SCREEN_SCHEMA
from worker.prompts import SCREENING, ANALYSIS

SCREEN_BYTES = b'pilihkan 2 coin yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance'
# Independent expected bytes: seven LF, no leading spaces, one space after bandar.
ANALYSIS_BYTES = b'Aku berikan data chart realtime saat ini 2 time frame 1 jam dan 15 menit, silahkan analisa dengan akurat dan Profitable. aku mau entry di time frame 15 menit untuk scalping.\nTentukan !\nUkuran posisi\nENTRY\nTP\nSL : yang tidak mudah terkena wick atau di jilat para bandar. \naku bermain di cross, aku hanya bisa resikokan 5 usdt per 1 kali SL\nRISK REWARD 1:2'
GOOD = {'mode':'smart','answer':None,'output':{'symbols':['BTCUSDT','ETHUSDT']}}

class FinalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'state.sqlite3'
        self.ledger=Ledger(self.path);self.addCleanup(self.ledger.db.close)
        self.sleep=Mock()
    def client(self,transport):
        return NeuroAPI(self.ledger,key='synthetic-test-placeholder',transport=transport,sleep=self.sleep)
    def ask(self,client,operation='test',prompt=SCREENING):
        return client.ask(operation,prompt,SCREEN_SCHEMA,lambda x:None)
    def test_screening_utf8_exact(self):self.assertEqual(SCREENING.encode('utf-8'),SCREEN_BYTES)
    def test_analysis_utf8_exact_as_pasted(self):self.assertEqual(ANALYSIS.encode('utf-8'),ANALYSIS_BYTES)
    def test_transport_preserves_literal_newlines_without_normalization(self):
        transport=Mock(return_value=(200,{},GOOD))
        self.ask(self.client(transport),prompt=ANALYSIS)
        self.assertEqual(transport.call_args.args[3]['prompt'].encode('utf-8'),ANALYSIS_BYTES)
    def test_only_429_and_503_retry_and_honor_delay(self):
        for status in (429,503):
            with self.subTest(status=status):
                transport=Mock(side_effect=[(status,{'retry-after':'7'},None),(200,{},GOOD)])
                self.ask(self.client(transport),str(status))
                self.assertEqual(transport.call_count,2);self.sleep.assert_called_with(7)
    def test_retry_exhaustion_is_three_calls(self):
        for status in (429,503):
            transport=Mock(return_value=(status,{},None))
            with self.assertRaises(Review):self.ask(self.client(transport),str(status))
            self.assertEqual(transport.call_count,3)
    def test_409_and_other_statuses_never_retry(self):
        for status in (400,401,402,403,404,408,409,422,500,502,504):
            with self.subTest(status=status):
                transport=Mock(return_value=(status,{},None))
                with self.assertRaises(Review):self.ask(self.client(transport),str(status))
                self.assertEqual(transport.call_count,1)
        self.sleep.assert_not_called()
    def test_pending_is_committed_before_provider_call_and_no_header(self):
        def transport(*args,**kwargs):
            other=Ledger(self.path)
            try:self.assertEqual(other.db.execute('SELECT state FROM api_requests').fetchone()[0],'PENDING')
            finally:other.db.close()
            self.assertEqual(set(args[2]),{'Content-Type','X-API-Key'})
            return 200,{},GOOD
        self.ask(self.client(transport))
    def test_timeout_after_received_retryable_response_stops_and_survives_restart(self):
        transport=Mock(side_effect=[(429,{},None),TimeoutError('unknown')])
        with self.assertRaises(Review):self.ask(self.client(transport))
        self.assertEqual(transport.call_count,2)
        other=Ledger(self.path)
        try:
            self.assertEqual(other.db.execute('SELECT state FROM api_requests').fetchone()[0],'NEEDS_REVIEW')
            blocked=Mock();client=NeuroAPI(other,key='synthetic-test-placeholder',transport=blocked)
            with self.assertRaises(Review):self.ask(client)
            blocked.assert_not_called()
        finally:other.db.close()
    def test_unknown_network_outcomes_no_retry_or_secret_output(self):
        for i,error in enumerate((TimeoutError,ConnectionResetError,OSError)):
            transport=Mock(side_effect=error('synthetic-test-placeholder private response'))
            out=io.StringIO()
            with contextlib.redirect_stdout(out),contextlib.redirect_stderr(out):
                with self.assertRaises(Review) as caught:self.ask(self.client(transport),str(i))
            self.assertEqual(transport.call_count,1)
            self.assertEqual(out.getvalue(),'');self.assertEqual(str(caught.exception),'NEUROAPI_REQUEST_NEEDS_REVIEW')
        rows=[dict(r) for r in self.ledger.db.execute('SELECT * FROM api_requests')]
        self.assertNotIn('synthetic-test-placeholder',json.dumps(rows))
        self.assertTrue(all(r['output'] is None for r in rows))
    def test_invalid_or_excessive_retry_after_never_retries_early(self):
        for i,hint in enumerate(('999999999','31','invalid','-1','1.5','Wed, 21 Oct 2026 07:28:00 GMT')):
            transport=Mock(return_value=(429,{'retry-after':hint},None))
            with self.assertRaises(Review):self.ask(self.client(transport),str(i))
            self.assertEqual(transport.call_count,1)
        self.sleep.assert_not_called()
    def test_smart_structured_response_fail_closed_without_retry(self):
        for i,data in enumerate(({**GOOD,'mode':'max'},{**GOOD,'mode':'fast'},{**GOOD,'answer':'prose'},{**GOOD,'output':None},{**GOOD,'output':'{}'})):
            transport=Mock(return_value=(200,{},data))
            with self.assertRaises(Review):self.ask(self.client(transport),str(i))
            self.assertEqual(transport.call_count,1)
    def test_smart_request_schema_nonstreaming(self):
        transport=Mock(return_value=(200,{},GOOD));self.ask(self.client(transport))
        body=transport.call_args.args[3]
        self.assertEqual(body['mode'],'smart');self.assertIs(body['stream'],False)
        self.assertEqual(body['output_schema'],SCREEN_SCHEMA)
    def test_binance_transport_is_public_get_only_without_credentials(self):
        from urllib.parse import urlparse
        from test_neuroapi import FakeMarket
        market=FakeMarket();original=market.transport;observed=[]
        def inspect(method,url,headers=None,body=None,timeout=None):
            observed.append(urlparse(url).path)
            self.assertEqual(method,'GET');self.assertIsNone(body);self.assertFalse(headers)
            self.assertEqual(urlparse(url).netloc,'fapi.binance.com')
            self.assertIn(urlparse(url).path,('/fapi/v1/time','/fapi/v1/exchangeInfo','/fapi/v1/klines','/fapi/v1/premiumIndex'))
            return original(method,url,headers,body,timeout)
        market.transport=inspect
        market.catalog();market.data('BTCUSDT');market.rules('BTCUSDT');market.mark('BTCUSDT')
        self.assertEqual(set(observed),{'/fapi/v1/time','/fapi/v1/exchangeInfo','/fapi/v1/klines','/fapi/v1/premiumIndex'})
