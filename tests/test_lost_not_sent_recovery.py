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

class NativeAdmissionRenewedCapabilityRegression(unittest.TestCase):
    """Native-produced old closure + renewed worker capability + UUID re-render.

    Reproduces the recorded LINK grant18:32/admission1 vs capability19:54
    boundary. The native filesystem and worker journal both run their actual
    producers/consumers, isolated in temporary files and SQLite; no sockets.
    """
    def test_old_native_admission_survives_capability_renewal_and_reaches_new_admission_once(self):
        import pathlib
        from agent import native_received_continuation as native
        from agent import native_received_analysis as observer
        f=continuation_fixture.AnalysisContinuationTests('test_original_clock_and_owner_risk_survive_explicit_followup_grant')
        f.setUp();self.addCleanup(f.doCleanups)
        provider=f.provider;root=pathlib.Path(f.f.root.name);payload=f.f.payload
        source=copy.deepcopy(f.envelope['observation'])
        source.update(observed_at=NOW-3,finished_at=NOW-2)
        source['operation_scope'].update(initial_user=0,latest_user=2,reply_row=3,user_rows=[0,2],reason=None)
        source['rows'][1]['id']='08274614-8cf6-4078-a32e-70de1e8b75f9'
        source['rows'][3]['id']='0f1aae4d-3a09-4954-a997-511a9a0e9e77'
        for entry in source['receipt_files'].values():
            if entry['value'].get('followup_sequence',1)==1:
                entry['value']['initial_turns_sha256']=digest(source['rows'][:2])
                raw=wire(entry['value'],ascii=True);entry.update(sha256=sha(raw),bytes=len(raw.encode()))
        def envelope(capture):
            saved=observer._save(root,capture)
            result=dict(f.envelope,**saved,observation=observer._project_operation(capture))
            return f.f.seal(result)
        first=envelope(source)
        original=admit(provider,first,now=NOW)
        with patch.object(native.time,'time',return_value=NOW+1):
            base,admission=native._admit(root,original,first['capture_name'])
        closed=dict(kind=native.NOT_SENT,operation=OP,request_sha256=payload['request_sha256'],
            grant_id=original['grant_id'],admission=admission,send_attempted=False,
            new_intent_written=False,upload_started=False,closed_at=NOW+2)
        native._write(root,base+'.closed-not-sent.json',closed)
        record_return(provider,original,now=NOW+3)
        original_files={p.name:p.read_bytes()for p in root.glob('analysis-continuation-*.json')}
        # The real page re-render changed only assistant UUIDs. A manual request
        # and its complete null technical JSON remain physically after ownership.
        current=copy.deepcopy(source)
        current['rows'][1]['id']='52b5a34a-3a7a-45dd-af3c-853300e55019'
        current['rows'][3]['id']='51d97732-e503-4022-b231-c2b9005f90e5'
        current['rows'].extend([dict(role='user',text='berikan format json coin tp sl',attachments=[]),
            dict(role='assistant',id='72356a4f-79d6-4744-bbca-5a75394ab61d',complete=True,
                text=json.dumps(dict(coin='BTCUSDT',keputusan=None,entry=None,tp=None,sl=None,
                    alasan='Belum menetapkan setup pasar: gambar belum membuktikan bahwa 200 candle tertutup terlihat.')))])
        current.update(observed_at=NOW+1000,finished_at=NOW+1001)
        current['continuation_not_sent']=native.read_not_sent(root,payload,current)
        fresh=envelope(current)
        self.assertTrue(recover_lost_not_sent(provider,fresh,now=NOW+1002))
        capability=retry_not_sent(provider,fresh,now=NOW+1003)
        self.assertEqual(capability['grant_id'],original['grant_id'])
        self.assertNotEqual(capability['started_at'],original['started_at'])
        # Exact production pattern: worker renewed its dispatch capability,
        # then failed BEFORE writing native admission2. Its original native
        # closure still describes admission1 and must retain that original clock.
        record_return(provider,capability,now=NOW+1004)
        with self.assertRaises(Review):recover_lost_not_sent(provider,fresh,now=NOW+1005)
        self.assertEqual(active_grant(provider.db,OP)['state'],'UNKNOWN')
        current.update(observed_at=NOW+1005,finished_at=NOW+1006)
        current['manual_context_tail']=native.manual_context_tail(current,payload)
        current['continuation_not_sent']=native.read_not_sent(root,payload,current)
        actual=envelope(current)
        # An old native admission is not interchangeable with an arbitrary
        # clock. Without the exact recorded renewal/source lineage it refuses.
        renewal=provider.db.execute("SELECT * FROM analysis_continuation_returns WHERE state='CLOCK_RENEWED_AFTER_PROVED_NOT_SENT'").fetchone()
        provider.db.execute('DELETE FROM analysis_continuation_returns WHERE grant_id=? AND sequence=?',(renewal['grant_id'],renewal['sequence']))
        with self.assertRaises(Review):recover_lost_not_sent(provider,actual,now=NOW+1007)
        self.assertEqual(active_grant(provider.db,OP)['state'],'UNKNOWN')
        provider.db.execute('INSERT INTO analysis_continuation_returns VALUES(?,?,?,?,?)',tuple(renewal))
        self.assertTrue(recover_lost_not_sent(provider,actual,now=NOW+1007))
        next_capability=retry_not_sent(provider,actual,now=NOW+1008)
        self.assertEqual(next_capability['grant_id'],original['grant_id'])
        # The browser consumes its source through the actual immutable reader.
        native_source=native._read_source(root,payload,next_capability)
        current.update(observed_at=NOW+1009,finished_at=NOW+1010)
        current['continuation_not_sent']=native.read_not_sent(root,payload,current)
        native._persist_manual_context(root,payload,next_capability,native_source,current)
        current['manual_context_receipts']=native.read_manual_contexts(root,payload)
        seed=native._seed(current,payload,next_capability)
        self.assertEqual(len(seed['rows']),4);self.assertEqual(len(seed['full_rows']),6)
        from worker.analysis_continuation import validate_manual_context_receipts
        self.assertEqual(len(validate_manual_context_receipts(provider,observer._project_operation(current),payload)),1)
        with patch.object(native.time,'time',return_value=NOW+1011):
            second,_=native._admit(root,next_capability,actual['capture_name'])
        self.assertTrue(second.endswith('.2'))
        self.assertEqual(original_files,{p.name:p.read_bytes()for p in root.glob('analysis-continuation-*.json')if p.name in original_files})
        self.assertEqual(list(root.glob('*.2.send-attempted.json')),[])
        self.assertEqual(list(root.glob('*.2.upload-started.json')),[])
        self.assertIsNone(native.read_not_sent(root,payload,current))
        states=[r[0]for r in provider.db.execute('SELECT state FROM analysis_continuation_returns ORDER BY sequence')]
        self.assertEqual(states.count('UNKNOWN'),2)
        self.assertEqual(states.count('NATIVE_NOT_SENT_RECEIPT_RECONCILED'),2)
        self.assertEqual(provider.db.execute('SELECT count(*) FROM order_intents').fetchone()[0],1)
        self.assertEqual(provider.db.execute('SELECT enabled,risk FROM robot_settings').fetchone()[:],(1,'5'))
        self.assertIsNone(f.f.row('api_requests')['output'])
        self.assertIsNone(f.f.row('robot_candidates')['plan'])
