"""Persistent HTTP429 waiting with fake clocks and no provider/model calls."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import urllib.error

from agent.llm import OpenAIDialogue
from agent.memory import Memory
from agent.models import AgentError,AmbiguousOperation,NotSent
from tests.test_mini_http_rejection import ErrorBody


class CompletedBody(ErrorBody):
    def __enter__(self):return self
    def __exit__(self,*_):self.close()


class MiniHTTPCooldownTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.path=Path(self.temporary.name)/'agent.sqlite3'
        self.wall=1800000000.0;self.monotonic=1000.0
        self.memory=Memory(self.path);self.model=OpenAIDialogue('/unused-key',self.memory)
        self.opener=SimpleNamespace(open=Mock(side_effect=self.quota))
        self.patches=[patch('agent.llm.time.time',side_effect=lambda:self.wall),
            patch('agent.llm.time.monotonic',side_effect=lambda:self.monotonic),
            patch('agent.llm.read_api_key',return_value='offline-fake-key-not-a-real-provider-key'),
            patch('agent.llm.urllib.request.build_opener',return_value=self.opener)]
        for item in self.patches:item.start()

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.memory.close();self.temporary.cleanup()

    def quota(self,*_args,**_kwargs):
        raw=json.dumps({'error':{'type':'insufficient_quota','code':'insufficient_quota','message':'never retain'}}).encode()
        raise urllib.error.HTTPError('https://offline.invalid',429,'private provider message',
            {'x-request-id':'req_offline_'+str(self.opener.open.call_count)},ErrorBody(raw))

    def plan(self):
        self.model.bind_operation('offline-cooldown-operation','SCREENING')
        return self.model.plan([],'BTCUSDT',phase='SCREENING',jakarta_day='2026-11-09')

    def reject(self):
        with self.assertRaisesRegex(NotSent,'MODEL_INSUFFICIENT_QUOTA'):self.plan()

    def advance(self,seconds):
        self.wall+=seconds;self.monotonic+=seconds

    def state(self):return self.memory.get(self.model.cooldown_key)

    def budget(self):return tuple(self.memory.db.execute('SELECT * FROM budgets').fetchone())

    def success(self,*_args,**_kwargs):
        value={'status':'completed','model':'gpt-5-mini','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps({'action':'SCREENING','symbols':['BTCUSDT'],'question':''})}]}]}
        return CompletedBody(json.dumps(value).encode())

    def test_repeated_natural_ticks_reserve_nothing_and_make_no_calls_while_held(self):
        self.reject();budget=self.budget();self.assertEqual(self.opener.open.call_count,1)
        self.assertEqual(self.state()['delay_seconds'],300)
        for _ in range(5):
            self.advance(45);self.reject()
            self.assertEqual(self.opener.open.call_count,1);self.assertEqual(self.budget(),budget)
        rows=[json.loads(row[0]) for row in self.memory.db.execute(
            "SELECT value FROM metadata WHERE key LIKE 'dialogue-model-http-error:%'")]
        held=[row for row in rows if row['authority']=='PROVIDER_HTTP_COOLDOWN']
        self.assertTrue(held)
        self.assertTrue(all(row['request_sent'] is False and row['provider_response_received'] is False for row in held))
        self.assertTrue(all(row['classification']=='MODEL_INSUFFICIENT_QUOTA' for row in held))
        self.advance(75);self.reject()
        self.assertEqual(self.opener.open.call_count,2);self.assertEqual(self.budget()[1],budget[1]+1)

    def test_restart_resumes_remaining_wait_and_clock_jumps_do_not_change_live_wait(self):
        self.reject();self.advance(90)
        self.memory.close();self.memory=Memory(self.path);self.model=OpenAIDialogue('/unused-key',self.memory)
        self.reject();self.assertEqual(self.opener.open.call_count,1)
        self.wall+=1000000;self.advance(100);self.reject()
        self.wall-=2000000;self.advance(109);self.reject()
        self.assertEqual(self.opener.open.call_count,1)
        self.advance(1);self.reject();self.assertEqual(self.opener.open.call_count,2)

    def test_restart_with_backward_wall_jump_waits_at_most_one_finite_delay(self):
        self.reject();self.memory.close();self.memory=Memory(self.path)
        self.wall-=1000000;self.model=OpenAIDialogue('/unused-key',self.memory)
        self.reject();self.advance(299);self.reject()
        self.assertEqual(self.opener.open.call_count,1)
        self.advance(1);self.reject();self.assertEqual(self.opener.open.call_count,2)

    def test_last_network_boundary_holds_direct_transport_calls_too(self):
        self.reject();self.opener.open.reset_mock()
        self.model.bind_operation('offline-direct-cooldown-operation','SCREENING')
        with self.assertRaisesRegex(NotSent,'MODEL_INSUFFICIENT_QUOTA'):
            self.model._request({'input':[{'role':'user','content':'Fase: SCREENING\nBTCUSDT'}]})
        self.opener.open.assert_not_called()

    def test_only_valid_live_completed_output_clears_and_next_rejection_starts_at_one(self):
        self.reject();self.advance(300);self.opener.open.side_effect=self.success
        self.assertEqual(self.plan().action,'SCREENING')
        self.assertTrue(self.model.evidence()['verified']);self.assertFalse(self.state()['active'])
        self.assertEqual(self.state()['failures'],0)
        self.opener.open.side_effect=self.quota;self.reject()
        self.assertEqual(self.state()['failures'],1);self.assertEqual(self.state()['delay_seconds'],300)

    def test_wrong_model_or_invalid_output_never_clears_cooldown(self):
        self.reject();self.advance(300)
        self.opener.open.side_effect=lambda *_args,**_kwargs:CompletedBody(json.dumps(
            {'status':'completed','model':'gpt-foreign','output':[]}).encode())
        with self.assertRaisesRegex(AgentError,'MODEL_RESPONSE_IDENTITY_UNVERIFIED'):self.plan()
        self.assertTrue(self.state()['active']);self.assertFalse(self.model.evidence()['verified'])

    def test_clear_write_failure_preserves_valid_response_receipt_without_replay(self):
        self.reject();self.advance(300);self.opener.open.side_effect=self.success
        original=self.memory.put
        def put(key,value):
            if key==self.model.cooldown_key:raise OSError('offline disk failure')
            return original(key,value)
        with patch.object(self.memory,'put',side_effect=put):
            result=self.memory.operation('completed-original','DELEGATED_MODEL_DIALOGUE',{'id':'original'},
                lambda:{'action':self.plan().action})
        self.assertEqual(result,{'action':'SCREENING'});self.assertTrue(self.state()['active'])
        self.assertEqual(self.memory.counts()['operations'],{'DONE':1})
        self.assertEqual(self.memory.operation('completed-original','DELEGATED_MODEL_DIALOGUE',{'id':'original'},
            Mock(side_effect=AssertionError('must not replay'))),result)
        self.assertEqual(self.opener.open.call_count,2)
        self.assertTrue(self.model.evidence()['verified'])

    def test_corrupt_state_after_http_response_cannot_label_completed_model_result_not_sent(self):
        self.reject();self.advance(300)
        def completed(*args,**kwargs):
            self.memory.put(self.model.cooldown_key,{'corrupt-after-paid-response':True})
            return self.success(*args,**kwargs)
        self.opener.open.side_effect=completed
        result=self.memory.operation('completed-corrupt','DELEGATED_MODEL_DIALOGUE',{'id':'original'},
            lambda:{'action':self.plan().action})
        self.assertEqual(result,{'action':'SCREENING'})
        self.assertEqual(self.memory.counts()['operations'],{'DONE':1})
        self.assertEqual(self.opener.open.call_count,2)
        self.assertTrue(self.memory.db.execute("SELECT 1 FROM metadata WHERE key LIKE 'dialogue-model-raw:%'").fetchone())

    def test_failed_hold_write_over_old_inactive_row_cannot_enable_repeated_api_calls(self):
        self.reject();self.advance(300);self.opener.open.side_effect=self.success;self.plan()
        old=self.state();self.assertFalse(old['active'])
        original=self.memory.put
        def put(key,value):
            if key==self.model.cooldown_key:raise OSError('offline disk failure')
            return original(key,value)
        self.opener.open.side_effect=self.quota
        with patch.object(self.memory,'put',side_effect=put):self.reject()
        self.assertEqual(self.state(),old);budget=self.budget()
        for _ in range(5):
            self.advance(45);self.reject()
            self.assertEqual(self.opener.open.call_count,3);self.assertEqual(self.budget(),budget)
        # A separately confirmed, newer 429 is adopted without shortening the
        # remaining in-process wait whose own persistence failed.
        cached,until=self.model._cooldown_cache
        newer={**cached,'last_event':'f'*64,'failures':2,'observed_at':self.wall,
            'delay_seconds':60,'retry_at':self.wall+60}
        self.memory.put(self.model.cooldown_key,newer)
        self.reject()
        self.assertEqual(self.model._cooldown_cache[0]['last_event'],'f'*64)
        self.assertGreaterEqual(self.model._cooldown_cache[1],until)
        self.assertEqual(self.opener.open.call_count,3)

    def test_timeout_stays_unknown_after_wait_without_clearing_or_replaying(self):
        self.reject();self.advance(300);self.opener.open.side_effect=TimeoutError('private timeout')
        execute=lambda:self.plan()
        with self.assertRaisesRegex(AgentError,'MODEL_REQUEST_UNCONFIRMED'):
            self.memory.operation('unknown-original','DELEGATED_MODEL_DIALOGUE',{'id':'original'},execute)
        self.assertEqual(self.opener.open.call_count,2);self.assertTrue(self.state()['active'])
        with self.assertRaises(AmbiguousOperation):
            self.memory.operation('unknown-original','DELEGATED_MODEL_DIALOGUE',{'id':'original'},execute)
        self.assertEqual(self.opener.open.call_count,2)
        self.assertEqual(self.memory.counts()['operations'],{'UNKNOWN':1})

    def rejection_evidence(self,index,*,reason='MODEL_RATE_LIMITED',retry_after=0):
        return {'provider_request_id':'req_parallel_'+str(index),'observed_at':self.wall,
            'model_input_sha256':hashlib.sha256(b'original-input').hexdigest(),
            'classification':reason,'retry_after_seconds':retry_after}

    def test_concurrent_same_response_is_idempotent_and_rate_wait_is_bounded(self):
        evidence=self.rejection_evidence('same')
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _index:self.model._record_http_cooldown(evidence),range(12)))
        self.assertEqual(self.state()['failures'],1);self.assertEqual(self.state()['delay_seconds'],60)
        self.advance(10);before=self.state()
        self.model._record_http_cooldown(evidence);self.assertEqual(self.state(),before)
        for index,delay in enumerate((120,240,480,900,900),start=2):
            self.model._record_http_cooldown(self.rejection_evidence(index))
            self.assertEqual(self.state()['delay_seconds'],delay)
        self.assertEqual(self.opener.open.call_count,0)

    def test_sane_retry_after_extends_finite_wait_but_huge_value_does_not_lock_forever(self):
        self.model._record_http_cooldown(self.rejection_evidence('quota',
            reason='MODEL_INSUFFICIENT_QUOTA',retry_after=420))
        self.assertEqual(self.state()['delay_seconds'],420)
        self.model._record_http_cooldown(self.rejection_evidence('huge',
            reason='MODEL_INSUFFICIENT_QUOTA',retry_after=99999))
        self.assertEqual(self.state()['delay_seconds'],420) # Existing valid wait is never shortened.
        self.assertEqual(self.opener.open.call_count,0)

    def test_http500_does_not_create_cooldown_and_successful_fake_transport_cannot_clear(self):
        self.opener.open.side_effect=urllib.error.HTTPError('https://offline.invalid',500,'private',{},ErrorBody(b'{}'))
        with self.assertRaisesRegex(AgentError,'MODEL_REQUEST_UNCONFIRMED'):self.plan()
        self.assertIsNone(self.state())
        self.opener.open.side_effect=self.quota;self.reject();self.advance(300)
        fake=OpenAIDialogue('/unused',self.memory,transport=lambda _payload:{'status':'completed','output':[
            {'type':'message','content':[{'type':'output_text','text':json.dumps(
                {'action':'SCREENING','symbols':['BTCUSDT'],'question':''})}]}]})
        fake.plan([],'BTCUSDT',phase='SCREENING',jakarta_day='2026-11-09')
        self.assertTrue(self.state()['active']);self.assertEqual(self.opener.open.call_count,2)


if __name__=='__main__':unittest.main()
