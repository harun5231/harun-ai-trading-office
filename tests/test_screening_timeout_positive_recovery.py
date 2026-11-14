"""An actual DOM timeout cannot bypass positive receipt/Mini approval recovery."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from tests import test_generic_screening_response_recovery as fixture
from tests import test_screening_ended_noexec as ended_fixture
from worker import screening_response_recovery as recovery
from worker.account_state import screening_contract
from worker.core import Review


class ScreeningTimeoutPositiveRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.actual=json.loads((Path(__file__).parent/'fixtures/screen3_received_dom_timeout_20261114.json').read_text())
        self.dialogue=json.loads((Path(__file__).parent/'fixtures/screen3_sent_prefix_render_20261114.json').read_text())
        ended=self.actual['original_ended_return'];raw=recovery._wire(ended['value'],ascii=True)
        self.assertEqual(len(raw.encode()),843);self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(),ended['sha256'])
        self.now=self.actual['positive_capture']['finished_at']+1;self.op=ended['value']['operation'];self.cycle=self.op.rsplit(':screening:',1)[0]
        self.scope=patch.multiple(fixture,NOW=self.now,DAY='2026-11-14',CYCLE=self.cycle,OP=self.op)
        self.scope.start();self.addCleanup(self.scope.stop)
        self.case=fixture.GenericScreeningResponseRecoveryTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        c=self.case;_,schema=screening_contract(2)
        c.context={'requested_count':2,'excluded_symbols':['DOTUSDT','FILUSDT','HYPEUSDT','ICPUSDT','XLMUSDT'],
            'replacement_instruction':'Choose different Binance USD-M USDT perpetual coins that are not in excluded_symbols.'}
        # Rebind only request identity to a controlled local DB fixture. The
        # real843-byte return and source capture remain independently hashed.
        c.body,encoded,c.request_hash=c.source.request_body(self.op,'Controlled offline request.',schema,c.context)
        c.payload={**c.body,'request_sha256':c.request_hash};c.created=self.actual['original_created']
        c.db.execute('UPDATE api_requests SET body_hash=?,created=?',(c.request_hash,c.created))
        c.db.execute('UPDATE research_browser_requests SET request_sha256=?,marker=?,request_body=?',(c.request_hash,c.body['marker'],encoded))
        c.data={**c.data,'seen':c.context['excluded_symbols'],'screen':2,'replacements':2,'round_symbols':[]}
        c.db.execute('UPDATE robot_cycles SET data=?',(json.dumps(c.data),))
        c.provider.transport=Mock(side_effect=c.forbid);c.provider.ask=Mock(side_effect=c.forbid);c.provider.require_key=Mock(side_effect=c.forbid)
        self.envelope=self.envelope_for(approval=False);self.finances=c.finances()
        self.api=c.row('api_requests',self.op);self.job=c.row('robot_jobs',self.op)

    def envelope_for(self,*,approval):
        c=self.case;value=c.fixture(approval=False,reply=self.dialogue['current_reply'],operation=self.op)
        proof=value['observation'];source=self.actual['positive_capture']
        proof.update(observed_at=source['observed_at'],finished_at=source['finished_at'],row_offset=14)
        proof['rows'][0]['text']=self.dialogue['current_rendered_prompt'];proof['rows'][1]['id']='648eef13-2cbe-4bcc-888d-420a7b9ffd59'
        proof['room'].update(room_id=self.dialogue['room']['room_id'],url='https://app.neurobro.ai/');proof['page_url']='https://app.neurobro.ai/'
        proof['rendered_prompt_receipt'].update(rendered_prompt=proof['rows'][0]['text'],rendered_sha256=recovery._sha(proof['rows'][0]['text']))
        ended=copy.deepcopy(self.actual['original_ended_return']);ended['value']['request_sha256']=c.request_hash
        ended['name']='native-return-failure-'+c.request_hash+'.json';raw=recovery._wire(ended['value'],ascii=True)
        ended.update(sha256=recovery._sha(raw),bytes=len(raw.encode()));proof['screening_ended_return']=ended
        if approval:
            entry=ended_fixture.review_entry(c.payload,proof['rows'],'',source['observed_at']-1,symbols=['INJUSDT','AVAXUSDT'])
            review=entry['value'];conversation='neurobro:2026-11-14:'+proof['room']['room_id']
            review['conversation_id']=conversation;review['request']['conversation_id']=conversation;review['response']['conversation_id']=conversation
            review['response']['decision'].update(action='SCREENING',question='');review['response']['needs_followup']=False
            review['model_operation'].update(request_hash=recovery._digest(review['request']),result=recovery._wire(review['response'],ascii=True))
            raw=recovery._wire(review,ascii=True);entry.update(sha256=recovery._sha(raw),bytes=len(raw.encode()),
                name='screening-review-'+recovery._digest({'operation':self.op,'request_sha256':c.request_hash})+'-'+review['request_id']+'.json')
            proof['review_receipts']=[entry]
        return c.seal(value)

    def recover(self,envelope=None,**kwargs):
        return recovery.recover_received_screening(self.case.provider,envelope or self.envelope,
            catalog=self.case.catalog,max_age=180,now=self.now,**kwargs)

    def assert_unchanged(self):
        self.assertEqual(self.case.finances(),self.finances)
        self.assertEqual(self.case.row('api_requests',self.op),self.api);self.assertEqual(self.case.row('robot_jobs',self.op),self.job)
        for method in('transport','ask','require_key'):getattr(self.case.provider,method).assert_not_called()
        self.assertEqual(self.case.db.execute('SELECT COUNT(*)FROM screening_response_recoveries').fetchone()[0],0)

    def test_actual_expired_dom_timeout_requires_normal_source_bound_mini_approval(self):
        positive=recovery._received_screening_shape(self.envelope,self.case.payload,self.case.created,max_age=180,now=self.now)
        self.assertEqual(positive['errors'],[]);self.assertTrue(positive['operation_scope']['verified'])
        self.assertEqual(recovery._sha(positive['rows'][-1]['text']),self.actual['assistant']['sha256'])
        self.assertGreater(positive['screening_ended_return']['value']['clock'],positive['screening_ended_return']['value']['deadline'])
        with self.assertRaisesRegex(Review,'^SCREENING_RESPONSE_RECOVERY_UNVERIFIED$'):
            recovery._ended_screening_return(positive,self.case.payload,self.case.created,max_age=180)
        with self.assertRaisesRegex(Review,'^SCREENING_MINI_APPROVAL_NOT_RETAINED$'):self.recover()
        self.assert_unchanged()

    def test_bound_approval_retains_stale_reply_and_recovers_once_after_restart(self):
        envelope=self.envelope_for(approval=True);self.assertEqual(self.recover(envelope),'NOEXEC')
        c=self.case;api=c.row('api_requests',self.op);journal=c.db.execute('SELECT *FROM screening_response_recoveries WHERE operation=?',(self.op,)).fetchone()
        self.assertEqual(api['state'],recovery.RECEIVED_TERMINAL);self.assertEqual(api['failure_code'],recovery.RECEIVED_REASON)
        self.assertIsNone(api['output']);self.assertEqual(api['attempts'],1);self.assertEqual(c.row('robot_jobs',self.op)['state'],'COMPLETE')
        response=c.row('research_browser_responses',self.op);self.assertEqual(response['assistant_sha256'],self.actual['assistant']['sha256'])
        proof=json.loads(journal['proof']);self.assertNotIn('retirement',proof);self.assertEqual(journal['phase'],'NOEXEC')
        self.assertEqual(proof['observation']['screening_ended_return'],envelope['observation']['screening_ended_return'])
        self.assertEqual(c.finances(),self.finances);before=tuple(c.db.iterdump())
        self.assertEqual(self.recover(envelope),'NOEXEC');self.assertEqual(tuple(c.db.iterdump()),before)
        restarted=object.__new__(c.native.NeuroBrowser);restarted.db=c.db;restarted.chart_max_age=180
        restarted.transport=restarted.ask=restarted.require_key=c.forbid;c.provider=restarted
        self.assertEqual(self.recover(envelope),'NOEXEC');self.assertEqual(tuple(c.db.iterdump()),before)

    def test_invalid_positive_capture_never_falls_through_or_requests_approval(self):
        for field in('errors','ownership','source'):
            value=copy.deepcopy(self.envelope)
            if field=='errors':value['observation']['errors']=[{'reason':'UNVERIFIED'}]
            if field=='ownership':value['observation']['room_witness']['same_loaded_room_verified']=False
            if field=='source':value['observation']['native_source_sha256']='f'*64
            self.case.seal(value)
            with self.subTest(field=field),patch.object(recovery,'_ended_screening_return')as ended,patch.object(recovery,'validate_received_screening')as normal:
                with self.assertRaises(Review):self.recover(value)
                ended.assert_not_called();normal.assert_not_called()
            self.assert_unchanged()

    def test_ambiguous_end_cannot_retire_without_positive_mini_approval(self):
        value=copy.deepcopy(self.envelope);ended=value['observation']['screening_ended_return']
        ended['value']['clock']=ended['value']['deadline']-1
        raw=recovery._wire(ended['value'],ascii=True);ended.update(sha256=recovery._sha(raw),bytes=len(raw.encode()))
        self.case.seal(value)
        with self.assertRaisesRegex(Review,'^SCREENING_MINI_APPROVAL_NOT_RETAINED$'):self.recover(value)
        self.assert_unchanged()

    def test_forged_or_unknown_mini_approval_never_admits_a_stale_response(self):
        for change in('result','operation_id','unknown'):
            value=self.envelope_for(approval=True);entry=value['observation']['review_receipts'][0]
            done=entry['value']['model_operation']
            if change=='result':done['result']='{"forged":"approval"}'
            if change=='operation_id':done['operation_id']='delegated-model:'+'f'*64
            if change=='unknown':done['state']='UNKNOWN'
            raw=recovery._wire(entry['value'],ascii=True);entry.update(sha256=recovery._sha(raw),bytes=len(raw.encode()))
            self.case.seal(value)
            with self.subTest(change=change),self.assertRaises(Review):self.recover(value)
            self.assert_unchanged()

    def test_scheduler_requests_original_review_once_then_reconciles_and_survives_restart(self):
        c=self.case;approved=self.envelope_for(approval=True)
        def review(operation,envelope):
            self.assertEqual(operation,self.op);self.assertEqual(envelope,self.envelope)
            return {'kind':ended_fixture.native.REVIEW_KIND,'operation':self.op,'request_sha256':c.request_hash,
                'operation_completed':False,'review_receipts':copy.deepcopy(approved['observation']['review_receipts'])}
        c.provider.review_received_screening=Mock(side_effect=review)
        c.provider.observe_received_screening=Mock(side_effect=lambda operation:copy.deepcopy(
            approved if c.provider.review_received_screening.call_count else self.envelope))
        coordinator=SimpleNamespace(db=c.db,neuro=c.provider,allowed=lambda _:True,
            market=SimpleNamespace(catalog=lambda:c.catalog,max_age=180),
            store=SimpleNamespace(report=lambda status,account=None,**kw:{'bot_status':status,**kw}))
        with patch.object(recovery.time,'time',return_value=self.now):
            first=recovery.resume_uncertain_screening(coordinator,'2026-11-14',{})
            self.assertEqual(first['wait_reason'],'SCREENING_RECEIVED_REVIEW_RECONCILING')
            self.assertEqual(c.provider.review_received_screening.call_count,1);self.assert_unchanged()
            second=recovery.resume_uncertain_screening(coordinator,'2026-11-14',{})
            self.assertEqual(second['wait_reason'],'SCREENING_RECEIVED_STALE_REPLACEMENT_PENDING')
            self.assertEqual(c.provider.observe_received_screening.call_count,2);self.assertEqual(c.provider.review_received_screening.call_count,1)
            self.assertEqual(c.row('robot_jobs',self.op)['state'],'COMPLETE');self.assertEqual(c.row('api_requests',self.op)['attempts'],1)
            before=tuple(c.db.iterdump());self.assertIsNone(recovery.resume_uncertain_screening(coordinator,'2026-11-14',{}))
            restarted=object.__new__(c.native.NeuroBrowser);restarted.db=c.db;restarted.chart_max_age=180
            restarted.observe_received_screening=Mock(side_effect=c.forbid);restarted.review_received_screening=Mock(side_effect=c.forbid)
            restarted.transport=restarted.ask=restarted.require_key=c.forbid;coordinator.neuro=restarted
            self.assertIsNone(recovery.resume_uncertain_screening(coordinator,'2026-11-14',{}))
            restarted.observe_received_screening.assert_not_called();restarted.review_received_screening.assert_not_called()
            self.assertEqual(tuple(c.db.iterdump()),before);self.assertEqual(c.finances(),self.finances)

    def test_unrelated_review_and_runtime_errors_propagate_without_fallback(self):
        for error in(Review('RESEARCH_PAUSED'),RuntimeError('CONTROLLED_FAILURE')):
            with self.subTest(error=type(error).__name__),patch.object(recovery,'_ended_screening_return',side_effect=error),patch.object(recovery,'validate_received_screening')as normal:
                with self.assertRaises(type(error)):self.recover()
                normal.assert_not_called()
            self.assert_unchanged()

    def test_valid_ended_model_proof_keeps_existing_retirement_without_approval(self):
        value=copy.deepcopy(self.envelope)
        value['observation']['screening_ended_return']=ended_fixture.failure(self.case.payload,value['observation']['page_url'],
            phase='INITIAL',model=True,at=self.case.created+100,hook=ended_fixture.native.PHASE_JSON_RETURN_FAILURE_HOOK)
        self.case.seal(value);self.assertEqual(self.recover(value),'NOEXEC')
        row=self.case.db.execute('SELECT proof FROM screening_response_recoveries WHERE operation=?',(self.op,)).fetchone()
        self.assertEqual(json.loads(row['proof'])['retirement']['kind'],recovery.ENDED_NOEXEC)
        self.assertEqual(self.case.finances(),self.finances)


if __name__=='__main__':unittest.main()
