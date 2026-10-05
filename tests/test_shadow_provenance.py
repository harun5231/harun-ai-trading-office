"""Legacy ledger preservation and explicit current-producer trust boundary."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from worker.core import Ledger,day,D
from worker.neuroapi import NeuroAPI
from worker.analysis import analysis_once
from worker.binance_shadow import load_candidates,ShadowError,shadow
from worker.provenance import stamp,ANALYSIS_VERSION,CONTRACT_VERSION,SIZING_VERSION
from test_neuroapi import FakeMarket,GOOD
from test_binance_shadow import ReadAccount

class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger.sqlite3'
        self.ledger=Ledger(self.path);self.addCleanup(self.ledger.db.close)
        self.calls=[];self.output={}
        def transport(method,url,headers,body,timeout):
            symbol=json.loads(body['message_history'][0]['content'])['symbol'];self.calls.append(symbol)
            return 200,{},dict(mode='smart',answer=None,output={**GOOD,**self.output,'symbol':symbol})
        self.client=NeuroAPI(self.ledger,key='synthetic-placeholder',transport=transport)
        self.ledger.db.execute('CREATE TABLE analysis_checks(operation TEXT PRIMARY KEY,day TEXT,state TEXT,result TEXT)')
    def batch(self):return analysis_once(self.ledger,self.client,FakeMarket(),['BTCUSDT','ETHUSDT'])
    def load(self):return load_candidates(self.ledger,day())
    def rows(self):return [tuple(r) for r in self.ledger.db.execute('SELECT * FROM analysis_checks ORDER BY operation')]
    def legacy(self,status):
        self.ledger.db.execute('INSERT INTO analysis_checks VALUES(?,?,?,?)',
            (day()+':analysis-v5:'+status,day(),'COMPLETE',json.dumps(dict(status=status,symbol='BTCUSDT',side='LONG'))))
    def edit(self,fn,restamp=False):
        row=self.ledger.db.execute('SELECT operation,result FROM analysis_checks ORDER BY operation LIMIT 1').fetchone()
        result=json.loads(row['result']);fn(result)
        if restamp:stamp(self.ledger.db,result,row['operation'])
        self.ledger.db.execute('UPDATE analysis_checks SET result=? WHERE operation=?',(json.dumps(result),row['operation']))
    def assert_source_failure(self):
        with self.assertRaisesRegex(ShadowError,'SHADOW_SOURCE_UNVERIFIED'):self.load()
    def test_legacy_accept_ignored_and_preserved(self):
        self.legacy('ACCEPT');before=self.rows()
        with self.assertRaisesRegex(ShadowError,'NO_PERSISTED_SETUP'):self.load()
        self.assertEqual(before,self.rows())
    def test_legacy_reject_ignored_and_preserved(self):
        self.legacy('REJECT');before=self.rows()
        with self.assertRaisesRegex(ShadowError,'NO_PERSISTED_SETUP'):self.load()
        self.assertEqual(before,self.rows())
    def test_legacy_hold_ignored_and_preserved(self):
        self.legacy('HOLD');before=self.rows()
        with self.assertRaisesRegex(ShadowError,'NO_PERSISTED_SETUP'):self.load()
        self.assertEqual(before,self.rows())
    def test_legacy_does_not_poison_current_candidates(self):
        for state in ('ACCEPT','HOLD','REJECT'):self.legacy(state)
        before=self.rows();self.batch();self.assertEqual(len(self.load()),2)
        self.assertTrue(all(row in self.rows() for row in before))
    def test_current_accept_full_shadow_readonly(self):
        results=self.batch();out=shadow(self.ledger,ReadAccount(),FakeMarket())
        self.assertEqual(out['status'],'SHADOW_PREFLIGHT_OK');self.assertFalse(out['would_submit']);self.assertFalse(out['live_execution'])
        for r,p in zip(results,self.load()):
            proof=r['provenance']
            self.assertEqual(proof['contract_version'],CONTRACT_VERSION);self.assertEqual(proof['execution_sizing_version'],SIZING_VERSION)
            self.assertEqual(proof['analysis_version'],ANALYSIS_VERSION)
            self.assertEqual((p['entry'],p['tp'],p['sl']),(str(GOOD['limit_entry']),str(GOOD['take_profit']),str(GOOD['stop_loss'])))
            self.assertEqual(D(p['risk']),5);self.assertGreaterEqual(D(p['rr']),2)
            self.assertNotEqual(p['execution_quantity'],p['neurobro_position_size'])
    def test_current_missing_provenance_fails(self):
        self.batch();self.edit(lambda r:r.pop('provenance'));self.assert_source_failure()
    def test_current_false_source_fails(self):
        self.batch();self.edit(lambda r:r['provenance'].update(source_type='LEGACY'));self.assert_source_failure()
    def test_current_incomplete_provenance_fails(self):
        self.batch();self.edit(lambda r:r['provenance'].pop('execution_sizing_version'));self.assert_source_failure()
    def test_current_wrong_operation_fails(self):
        self.batch();self.edit(lambda r:r['provenance'].update(operation='OTHER'));self.assert_source_failure()
    def test_current_missing_execution_quantity_even_with_valid_digest_fails(self):
        self.batch();self.edit(lambda r:r.pop('execution_quantity'),True);self.assert_source_failure()
    def test_current_missing_levels_even_with_valid_digest_fails(self):
        self.batch();self.edit(lambda r:r.pop('TP'),True);self.assert_source_failure()
    def test_current_hold_no_candidate_no_slot(self):
        self.output=dict(side='HOLD',limit_entry=None,take_profit=None,stop_loss=None,position_size=None,risk_reward=None)
        self.assertEqual([r['status'] for r in self.batch()],['HOLD','HOLD'])
        with self.assertRaisesRegex(ShadowError,'NO_PERSISTED_SETUP'):self.load()
        self.assertEqual(self.ledger.count(),0)
    def test_current_reject_no_candidate(self):
        self.output=dict(take_profit=101)
        self.assertEqual([r['status'] for r in self.batch()],['REJECT','REJECT'])
        with self.assertRaisesRegex(ShadowError,'NO_PERSISTED_SETUP'):self.load()
    def test_request_incomplete_fails(self):
        self.batch();self.ledger.db.execute("UPDATE api_requests SET state='NEEDS_REVIEW'");self.assert_source_failure()
    def test_request_missing_fails(self):
        self.batch();self.ledger.db.execute("UPDATE api_requests SET operation='changed:' || operation");self.assert_source_failure()
    def test_request_evidence_changed_fails(self):
        self.batch();self.ledger.db.execute("UPDATE api_requests SET output='{}'");self.assert_source_failure()
    def test_result_levels_changed_without_restamping_fail(self):
        self.batch();self.edit(lambda r:r.update(TP='108'));self.assert_source_failure()
    def test_changed_levels_even_with_restamped_digest_fail_provider_check(self):
        self.batch();self.edit(lambda r:r.update(TP='108'),True)
        with self.assertRaisesRegex(ShadowError,'SHADOW_SETUP_CHANGED'):self.load()
    def test_repeat_after_restart_never_calls_provider_or_deletes_ledger(self):
        self.legacy('ACCEPT');first=self.batch();before=self.rows()
        restarted=Ledger(self.path)
        try:
            with patch.object(self.client,'ask',side_effect=AssertionError('replay')):
                self.assertEqual(analysis_once(restarted,self.client,FakeMarket(),['BTCUSDT','ETHUSDT']),first)
        finally:restarted.db.close()
        self.assertEqual(self.calls,['BTCUSDT','ETHUSDT']);self.assertEqual(before,self.rows())
    def test_v6_pending_claim_never_replayed(self):
        self.ledger.db.execute('INSERT INTO analysis_checks VALUES(?,?,?,?)',(day()+':analysis-v6:BTCUSDT',day(),'PENDING',None))
        result=self.batch();self.assertEqual(self.calls,['ETHUSDT']);self.assertEqual(result[0]['status'],'REJECT')
