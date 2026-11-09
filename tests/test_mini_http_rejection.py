"""Provider rejection diagnostics without HTTP, model calls or raw prose."""
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import urllib.error

from agent.llm import OpenAIDialogue
from agent.memory import Memory
from agent.models import AgentError,NotSent


class ErrorBody(io.BytesIO):
    def __init__(self,raw):
        super().__init__(raw)
        self.timeouts=[];self.reads=0;self.bytes_read=0
        self.fp=SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=self.timeouts.append)))

    def read1(self,size):
        self.reads+=1
        result=super().read1(size)
        self.bytes_read+=len(result)
        return result


class MiniHTTPRejectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.memory=Memory(Path(self.temporary.name)/'agent.sqlite3')
        self.model=OpenAIDialogue('/unused-offline-key',self.memory)

    def tearDown(self):
        self.memory.close();self.temporary.cleanup()

    def rejection(self,status=429,*,kind='tokens',code='rate_limit_exceeded',raw=None,headers=None):
        raw=raw if raw is not None else json.dumps({'error':{'type':kind,'code':code,
            'message':'private-provider-message-secret-canary','param':'private-param-canary'},
            'private-extra':'private-body-canary'}).encode()
        body=ErrorBody(raw)
        error=urllib.error.HTTPError('https://private-url-canary.invalid/?secret=canary',status,
            'private-exception-canary',headers or {'x-request-id':'req_offline_safe_123','retry-after':'2.5',
                'authorization':'private-header-canary'},body)
        return error,body

    def dispatch(self,error):
        opener=SimpleNamespace(open=Mock(side_effect=error))
        with patch('agent.llm.read_api_key',return_value='private-key-canary-never-log'),patch(
                'agent.llm.urllib.request.build_opener',return_value=opener):
            try:
                self.model._request({'model':'gpt-5-mini','input':[{'role':'user','content':'private-input-canary'}]})
            finally:
                self.assertEqual(opener.open.call_count,1)

    def evidence(self):
        rows=self.memory.db.execute("SELECT value FROM metadata WHERE key LIKE 'dialogue-model-http-error:%'").fetchall()
        self.assertEqual(len(rows),1)
        return json.loads(rows[0][0])

    def test_confirmed_429_quota_distinct_from_transient_limit_without_retry_or_secret(self):
        for kind,code,expected in (('insufficient_quota','insufficient_quota','MODEL_INSUFFICIENT_QUOTA'),
                ('invalid_request_error','billing_hard_limit_reached','MODEL_INSUFFICIENT_QUOTA'),
                ('tokens','rate_limit_exceeded','MODEL_RATE_LIMITED')):
            with self.subTest(code=code):
                self.memory.db.execute('DELETE FROM metadata')
                error,body=self.rejection(kind=kind,code=code)
                with self.assertRaisesRegex(NotSent,'^'+expected+'$'):self.dispatch(error)
                evidence=self.evidence()
                self.assertEqual(evidence['classification'],expected)
                self.assertEqual(evidence['http_status'],429)
                self.assertEqual(evidence['error_code'],code)
                self.assertEqual(evidence['error_type'],kind)
                self.assertEqual(evidence['provider_request_id'],'req_offline_safe_123')
                self.assertEqual(evidence['retry_after_seconds'],2.5)
                self.assertTrue(evidence['request_sent'] and evidence['provider_response_received'])
                self.assertFalse(evidence['model_output_received'])
                self.assertEqual(evidence['body_state'],'COMPLETE')
                self.assertTrue(body.closed);self.assertTrue(all(0<value<=45 for value in body.timeouts))
                text=json.dumps(evidence)
                for secret in ('private','message','param','authorization','https://','Bearer'):
                    self.assertNotIn(secret,text)
                self.assertFalse(self.model.evidence()['verified'])

    def test_unrecognized_type_code_and_headers_are_not_recorded_as_raw_values(self):
        error,_=self.rejection(kind='private-type-canary',code='private-code-canary',headers={
            'x-request-id':'private-header-canary','retry-after':'Infinity'})
        with self.assertRaisesRegex(NotSent,'MODEL_RATE_LIMITED'):self.dispatch(error)
        evidence=self.evidence()
        self.assertEqual(evidence['error_type'],'UNRECOGNIZED')
        self.assertEqual(evidence['error_code'],'UNRECOGNIZED')
        self.assertNotIn('provider_request_id',evidence)
        self.assertNotIn('retry_after_seconds',evidence)
        self.assertNotIn('private',json.dumps(evidence))

    def test_other_statuses_keep_original_safety_classification(self):
        for status,exception,code in ((401,NotSent,'MODEL_AUTHENTICATION_REJECTED'),
                (403,NotSent,'MODEL_AUTHENTICATION_REJECTED'),(400,NotSent,'MODEL_REQUEST_REJECTED'),
                (422,NotSent,'MODEL_REQUEST_REJECTED'),(500,AgentError,'MODEL_REQUEST_UNCONFIRMED')):
            with self.subTest(status=status):
                self.memory.db.execute('DELETE FROM metadata')
                error,_=self.rejection(status,kind='insufficient_quota',code='insufficient_quota')
                with self.assertRaisesRegex(exception,'^'+code+'$'):self.dispatch(error)
                self.assertEqual(self.evidence()['classification'],code)

    def test_error_body_size_invalid_json_and_unbounded_body_are_conservative(self):
        for raw,state in ((b'x'*9000,'TOO_LARGE'),(b'{invalid','COMPLETE')):
            self.memory.db.execute('DELETE FROM metadata')
            error,body=self.rejection(raw=raw)
            with self.assertRaisesRegex(NotSent,'MODEL_RATE_LIMITED'):self.dispatch(error)
            evidence=self.evidence()
            self.assertEqual(evidence['body_state'],state)
            self.assertIsNone(evidence['error_code'])
            self.assertLessEqual(body.bytes_read,8193)
        self.memory.db.execute('DELETE FROM metadata')
        error,body=self.rejection();body.fp=None
        with self.assertRaisesRegex(NotSent,'MODEL_RATE_LIMITED'):self.dispatch(error)
        self.assertEqual(body.reads,0)
        self.assertEqual(self.evidence()['body_state'],'UNAVAILABLE')

    def test_diagnostic_read_uses_original_absolute_deadline(self):
        error,body=self.rejection(raw=b'x'*8000)
        with patch('agent.llm.time.monotonic',side_effect=[0.0,2.0]):
            result=self.model._http_rejection(error,{'input':[]},1.0)
        self.assertEqual(result,'MODEL_RATE_LIMITED')
        self.assertEqual(body.reads,1)
        self.assertEqual(body.timeouts,[1.0])
        self.assertEqual(self.evidence()['body_state'],'UNAVAILABLE')

    def test_diagnostic_storage_failure_does_not_change_rejection_or_retry(self):
        error,_=self.rejection(kind='insufficient_quota',code='insufficient_quota')
        with patch.object(self.memory,'put',side_effect=OSError('private-storage-canary')):
            with self.assertRaisesRegex(NotSent,'^MODEL_INSUFFICIENT_QUOTA$'):self.dispatch(error)

    def test_rejected_operation_keeps_budget_and_cannot_be_replayed_after_restart(self):
        error,_=self.rejection(kind='insufficient_quota',code='insufficient_quota')
        opener=SimpleNamespace(open=Mock(side_effect=error))
        execute=lambda:self.model.plan([],'BTCUSDT',phase='SCREENING',jakarta_day='2026-11-09')
        with patch('agent.llm.read_api_key',return_value='private-key-canary-never-log'),patch(
                'agent.llm.urllib.request.build_opener',return_value=opener):
            with self.assertRaisesRegex(NotSent,'MODEL_INSUFFICIENT_QUOTA'):
                self.memory.operation('delegated-model:offline','DELEGATED_MODEL_DIALOGUE',{'id':'offline'},execute)
            self.assertEqual(opener.open.call_count,1)
        budget=tuple(self.memory.db.execute('SELECT * FROM budgets').fetchone())
        self.assertEqual((budget[0],budget[1],budget[3]),('2026-11-09',1,1024))
        self.memory.close();self.memory=Memory(Path(self.temporary.name)/'agent.sqlite3')
        with self.assertRaisesRegex(NotSent,'MODEL_INSUFFICIENT_QUOTA'):
            self.memory.operation('delegated-model:offline','DELEGATED_MODEL_DIALOGUE',{'id':'offline'},
                Mock(side_effect=AssertionError('must not replay')))
        self.assertEqual(tuple(self.memory.db.execute('SELECT * FROM budgets').fetchone()),budget)
        self.assertEqual(self.memory.counts()['operations'],{'DONE':1})


if __name__=='__main__':unittest.main()
