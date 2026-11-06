"""Reconcile a lost positive native return without inferring UNSENT by absence."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_analysis_continuation as continuation_fixture
from tests.test_analysis_response_recovery import NOW,DAY,OP
from worker.analysis_continuation import (admit,active_grant,record_return,retry_not_sent,
    recover_lost_not_sent,_observation_semantics,_not_sent)
from worker.analysis_response_recovery import wire,sha,digest,resume_uncertain_analysis
from worker.core import Review


class LostNotSentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f=continuation_fixture.AnalysisContinuationTests('test_original_clock_and_owner_risk_survive_explicit_followup_grant')
        self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.db,self.provider=self.f.db,self.f.provider
        self.envelope=copy.deepcopy(self.f.envelope)
        self.envelope['observation']['row_offset']=99
        prefix=sha(wire({key:self.f.f.payload[key]for key in('operation','request_sha256')}))[:16]
        self.envelope['capture_name']='received-analysis-observation-'+prefix+'-'+self.envelope['capture_sha256']+'.json'
        self.f.f.seal(self.envelope)
        self.grant=admit(self.provider,self.envelope,now=NOW)
        record_return(self.provider,self.grant,now=NOW+901)
        self.fresh=self.custody(self.envelope,at=NOW+1000)

    def custody(self,envelope,at):
        result=copy.deepcopy(envelope);proof=result['observation']
        proof.update(observed_at=at-1,finished_at=at)
        prefix='analysis-continuation-'+self.grant['grant_id']
        admitted=dict(grant=self.grant,attempt=1,previous_not_sent_sha256=None,
            current_capture=envelope['capture_name'],admitted_at=NOW+1)
        raw=wire(admitted)
        admission=dict(name=prefix+'.admitted.json',sha256=sha(raw),bytes=len(raw.encode()),value=admitted)
        closed=self.f.not_sent(self.grant,at=NOW+100)['disposition']['value']
        closed['admission']={key:admission[key]for key in('name','sha256','bytes')}
        raw=wire(closed)
        disposition=dict(name=prefix+'.closed-not-sent.json',sha256=sha(raw),bytes=len(raw.encode()),value=closed)
        semantic=digest(_observation_semantics(proof))
        proof['continuation_not_sent']=dict(kind='NATIVE_ANALYSIS_CONTINUATION_NOT_SENT_CUSTODY_V1',
            grant_id=self.grant['grant_id'],latest_attempt=1,admission=admission,disposition=disposition,
            source_semantics_sha256=semantic,current_semantics_sha256=semantic,
            intent_chain_sha256=digest(proof['receipt_files']),
            mutation_markers=dict(upload_started=False,send_attempted=False,completed=False),
            admitted_capture=dict(name=envelope['capture_name'],sha256=envelope['capture_sha256'],
                bytes=envelope['capture_bytes'],semantics_sha256=semantic))
        return self.f.f.seal(result)

    def test_lost_http_return_recovers_actual_latest_not_sent_and_preserves_financial_state(self):
        before={table:[tuple(row)for row in self.db.execute('SELECT * FROM '+table)]for table in
            ('api_requests','robot_jobs','robot_cycles','robot_candidates','robot_settings','order_intents')}
        self.assertTrue(recover_lost_not_sent(self.provider,self.fresh,now=NOW+1001))
        self.assertEqual(active_grant(self.db,OP)['state'],'NOT_SENT')
        states=[row[0]for row in self.db.execute('SELECT state FROM analysis_continuation_returns ORDER BY sequence')]
        self.assertEqual(states,['UNKNOWN','NATIVE_NOT_SENT_RECEIPT_RECONCILED','NOT_SENT'])
        audit=json.loads(self.db.execute("SELECT response FROM analysis_continuation_returns WHERE state='NATIVE_NOT_SENT_RECEIPT_RECONCILED'").fetchone()[0])
        self.assertEqual(audit['grant_preimage']['state'],'UNKNOWN')
        self.assertEqual(audit['fresh_observation'],self.fresh)
        self.assertEqual(before,{table:[tuple(row)for row in self.db.execute('SELECT * FROM '+table)]for table in before})
        renewed=retry_not_sent(self.provider,self.fresh,now=NOW+1002)
        self.assertEqual(renewed['grant_id'],self.grant['grant_id'])
        self.assertEqual(renewed['started_at'],NOW+1002)
        self.assertEqual(self.f.f.row('api_requests')['created'],NOW-2000)

    def test_older_positive_closure_never_settles_a_later_uncertain_attempt(self):
        early=self.custody(self.envelope,at=NOW+101)
        self.assertTrue(recover_lost_not_sent(self.provider,early,now=NOW+102))
        retry_not_sent(self.provider,early,now=NOW+103)
        record_return(self.provider,self.grant,now=NOW+104)
        with self.assertRaisesRegex(Review,'ANALYSIS_CONTINUATION_CLOSURE_ALREADY_CONSUMED'):
            recover_lost_not_sent(self.provider,early,now=NOW+105)
        self.assertEqual(active_grant(self.db,OP)['state'],'UNKNOWN')

    def test_fresh_positive_native_custody_after_pre_admission_retry_failure_preserves_unknown_history(self):
        early=self.custody(self.envelope,at=NOW+101)
        self.assertTrue(recover_lost_not_sent(self.provider,early,now=NOW+102))
        retry_not_sent(self.provider,early,now=NOW+103)
        record_return(self.provider,self.grant,now=NOW+104)
        # Unlike a replay of the older envelope, this actor observation occurs
        # after UNKNOWN and proves the SAME latest closed native admission;
        # any .2 admission would make the native reader return no custody.
        latest=self.custody(self.envelope,at=NOW+106)
        self.assertTrue(recover_lost_not_sent(self.provider,latest,now=NOW+107))
        self.assertEqual(active_grant(self.db,OP)['state'],'NOT_SENT')
        states=[r[0]for r in self.db.execute('SELECT state FROM analysis_continuation_returns ORDER BY sequence')]
        self.assertEqual(states.count('UNKNOWN'),2)
        self.assertEqual(states.count('NATIVE_NOT_SENT_RECEIPT_RECONCILED'),2)
        self.assertIsNone(self.f.f.row('api_requests')['output'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM order_intents').fetchone()[0],1)

    def test_partial_history_prefix_offset_changes_but_owned_native_identity_must_match(self):
        # Reloading fewer earlier room rows changes the unrelated prefix count.
        reloaded=copy.deepcopy(self.fresh)
        self.assertEqual(reloaded['observation']['row_offset'],99)
        reloaded['observation']['row_offset']=0
        self.f.f.seal(reloaded)
        self.assertEqual(digest(_observation_semantics(reloaded['observation'])),
            self.fresh['observation']['continuation_not_sent']['source_semantics_sha256'])
        foreign=copy.deepcopy(reloaded)
        foreign['observation']['rows'][-1]['id']='another-native-owned-message'
        self.f.f.seal(foreign)
        with self.assertRaises(Review):recover_lost_not_sent(self.provider,foreign,now=NOW+1001)
        self.assertEqual(active_grant(self.db,OP)['state'],'UNKNOWN')
        self.assertTrue(recover_lost_not_sent(self.provider,reloaded,now=NOW+1001))
        self.assertEqual(active_grant(self.db,OP)['state'],'NOT_SENT')

    def test_missing_latest_closure_or_no_receipt_never_infers_unsent(self):
        envelope=copy.deepcopy(self.fresh);envelope['observation']['continuation_not_sent']=None;self.f.f.seal(envelope)
        self.assertFalse(recover_lost_not_sent(self.provider,envelope,now=NOW+1001))
        envelope=copy.deepcopy(self.fresh);envelope['observation']['continuation_not_sent']['latest_attempt']=2;self.f.f.seal(envelope)
        with self.assertRaises(Review):recover_lost_not_sent(self.provider,envelope,now=NOW+1001)
        self.assertEqual(active_grant(self.db,OP)['state'],'UNKNOWN')

    def test_mismatched_admission_source_marker_or_closure_hash_remains_unknown(self):
        changes=(lambda p:p['continuation_not_sent']['mutation_markers'].update(upload_started=True),
            lambda p:p['continuation_not_sent'].update(source_semantics_sha256='e'*64),
            lambda p:p['continuation_not_sent']['disposition'].update(sha256='e'*64),
            lambda p:p['continuation_not_sent']['admitted_capture'].update(semantics_sha256='e'*64),
            lambda p:p['continuation_not_sent']['admission']['value'].update(attempt=2))
        for change in changes:
            envelope=copy.deepcopy(self.fresh);change(envelope['observation']);self.f.f.seal(envelope)
            with self.subTest(change=change),self.assertRaises(Review):recover_lost_not_sent(self.provider,envelope,now=NOW+1001)
            self.assertEqual(active_grant(self.db,OP)['state'],'UNKNOWN')

    def test_off_and_unknown_bare_return_cannot_recover_not_sent(self):
        self.db.execute('UPDATE robot_settings SET enabled=0')
        with self.assertRaises(Review):recover_lost_not_sent(self.provider,self.fresh,now=NOW+1001,permitted=lambda:False)
        closed=self.fresh['observation']['continuation_not_sent']['disposition']
        result={**closed['value'],'disposition':closed}
        with self.assertRaises(Review):record_return(self.provider,self.grant,result,now=NOW+1001)
        self.assertEqual(active_grant(self.db,OP)['state'],'UNKNOWN')

    def test_normal_scheduler_recovers_lost_disposition_then_renews_and_continues_same_grant(self):
        calls=[];self.provider.observe_received_analysis=lambda operation:self.fresh
        self.provider.continue_received_analysis=lambda operation,grant:(calls.append(grant)or dict(kind='NATIVE_ANALYSIS_CONTINUATION_RESULT_V1',operation=OP,request_sha256=self.f.f.request_hash,grant_id=grant['grant_id']))
        coordinator=SimpleNamespace(neuro=self.provider,db=self.db,allowed=lambda today:True,
            store=SimpleNamespace(report=lambda status,account,**kw:dict(status=status,**kw)),pause_reason=lambda:'ROBOT_OFF')
        with patch('worker.analysis_response_recovery.time.time',return_value=NOW+1001):
            result=resume_uncertain_analysis(coordinator,DAY,{})
        self.assertEqual(result['wait_reason'],'ANALYSIS_CONTINUATION_REPLY_RECONCILING')
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['grant_id'],self.grant['grant_id'])
        self.assertEqual(calls[0]['started_at'],NOW+1001)
        self.assertEqual(active_grant(self.db,OP)['state'],'RETURNED')
        self.assertEqual(self.db.execute('SELECT enabled,risk FROM robot_settings').fetchone()[:],(1,'5'))

if __name__=='__main__':unittest.main()
