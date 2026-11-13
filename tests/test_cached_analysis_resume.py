"""Sealed cached decisions survive one-slot budgets, restart and rollover.

All exchange, browser, model and order transports are forbidden fixtures.
"""
import json
import unittest
from unittest.mock import Mock,patch

from worker.analysis_response_recovery import (
    recover_received_analysis,received_nonexecuted,resume_uncertain_analysis,wire,sha)
from tests import test_analysis_response_recovery as recovery_fixtures

CYCLE,DAY,NOW,OP=(getattr(recovery_fixtures,name)for name in('CYCLE','DAY','NOW','OP'))


class CachedAnalysisResumeTests(unittest.TestCase):
    def setUp(self):
        self.case=recovery_fixtures.AnalysisResponseRecoveryTests()
        self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.db=self.case.db

    def recovered(self,*,side='HOLD'):
        data=dict(target=1,queue=[],seen=['BTCUSDT'],screen=0,replacements=0,
            round_symbols=['BTCUSDT'])
        self.db.execute('UPDATE robot_cycles SET data=? WHERE id=?',(json.dumps(data),CYCLE))
        if side!='HOLD':
            self.case.envelope=self.case.fixture(reply='COIN: BTCUSDT\nARAH: SHORT\nENTRY: 100\nSL: 101\nTP: 98')
        self.assertEqual(recover_received_analysis(self.case.provider,self.case.envelope,now=NOW),'RESUME_CACHED')
        module,coordinator=self.case.coordinator()
        coordinator.execution_slots=lambda account,today:1
        return module,coordinator

    def tick(self,module,coordinator,*,at=NOW,today=DAY):
        with patch.object(module,'day',return_value=today),patch.object(module.time,'time',return_value=at), \
                patch.object(module,'account_state',return_value={}), \
                patch('agent.native_pending_review.review_then_advance',return_value=None), \
                patch.object(module,'analysis_context',side_effect=self.case.forbid):
            return coordinator._tick()

    def read(self,table):return self.case.row(table)

    def test_one_slot_native_tick_consumes_actual_cached_hold_before_budget_and_replaces(self):
        module,coordinator=self.recovered()
        before={table:self.read(table)for table in('api_requests','research_browser_requests','research_browser_responses')}
        intents=list(self.db.execute('SELECT * FROM order_intents'))
        data=json.loads(self.read('robot_cycles')['data'])
        # Keep the ordinary technical-budget guard unchanged. The verified
        # cached-reply lane must run before this old skeleton blocks it.
        self.assertEqual(coordinator.research_budget(data,coordinator.store.results(CYCLE)),0)
        self.case.provider.require_key=self.case.forbid
        self.case.provider.observe_received_analysis=self.case.forbid
        self.assertEqual(self.tick(module,coordinator)['bot_status'],'HOLD')
        self.assertEqual(self.read('robot_candidates')['status'],'HOLD')
        self.assertEqual(self.read('robot_jobs')['state'],'COMPLETE')
        self.assertEqual(self.db.execute('SELECT phase FROM analysis_response_recoveries').fetchone()[0],'CONSUMED')
        for table,row in before.items():self.assertEqual(self.read(table),row)
        self.assertEqual(list(self.db.execute('SELECT * FROM order_intents')),intents)
        data=json.loads(self.read('robot_cycles')['data'])
        self.assertEqual((data['queue'],data['seen']),([],['BTCUSDT']))
        self.assertEqual(self.read('robot_cycles')['state'],'ACTIVE')
        from agent.native_pending_review import screening_day_exclusions
        self.assertIn('BTCUSDT',screening_day_exclusions(coordinator,DAY))
        self.case.provider.require_key=lambda:None
        coordinator.screen=Mock(return_value={'next':'existing-one-coin-replacement'})
        self.assertEqual(self.tick(module,coordinator),{'next':'existing-one-coin-replacement'})
        self.assertEqual(coordinator.screen.call_args.args[2],1)

    def test_restart_stale_cached_hold_is_final_without_refresh_or_another_model_call(self):
        self.recovered()
        self.db.execute("UPDATE robot_cycles SET state='COMPLETE' WHERE id=?",(CYCLE,))
        module,coordinator=self.case.coordinator()
        coordinator.execution_slots=lambda account,today:1
        self.case.provider.require_key=self.case.forbid
        self.case.provider.observe_received_analysis=self.case.forbid
        coordinator.market.catalog=self.case.forbid
        coordinator.market.data=self.case.forbid
        output=self.read('api_requests')['output'];receipt=self.read('research_browser_responses')
        result=self.tick(module,coordinator,at=NOW+400)
        self.assertEqual(result['bot_status'],'HOLD')
        self.assertEqual(self.read('api_requests')['output'],output)
        self.assertEqual(self.read('research_browser_responses'),receipt)
        self.assertEqual(self.read('robot_candidates')['status'],'HOLD')
        changes=self.db.total_changes
        self.assertIsNone(resume_uncertain_analysis(coordinator,DAY,{}))
        self.assertEqual(self.db.total_changes,changes)

    def test_stale_actionable_reply_never_reaches_sizing_or_gateway(self):
        module,coordinator=self.recovered(side='SHORT')
        coordinator.analyze=self.case.forbid
        self.case.provider.require_key=self.case.forbid
        self.case.provider.observe_received_analysis=self.case.forbid
        output=self.read('api_requests')['output'];receipt=self.read('research_browser_responses')
        result=self.tick(module,coordinator,at=NOW+400)
        self.assertEqual(result['wait_reason'],'ANALYSIS_RECEIVED_STALE_REPLACEMENT_PENDING')
        self.assertEqual(self.read('robot_candidates')['failure_code'],'STALE_MARKET_CONTEXT')
        self.assertEqual(self.read('api_requests')['output'],output)
        self.assertEqual(self.read('research_browser_responses'),receipt)
        with patch('worker.analysis_response_recovery.time.time',return_value=NOW+400):
            self.assertTrue(received_nonexecuted(self.db,self.read('robot_candidates')))
        self.assertEqual(self.db.execute('SELECT count(*) FROM order_intents').fetchone()[0],1)

    def test_fresh_actionable_reply_uses_only_existing_verified_native_cached_analysis(self):
        module,coordinator=self.recovered(side='SHORT')
        coordinator.analyze=Mock(return_value={'next':'existing-native-cached-analysis'})
        self.case.provider.require_key=self.case.forbid
        self.case.provider.observe_received_analysis=self.case.forbid
        result=self.tick(module,coordinator)
        self.assertEqual(result,{'next':'existing-native-cached-analysis'})
        coordinator.analyze.assert_called_once()
        self.assertEqual(coordinator.analyze.call_args.args[0],CYCLE)
        self.assertEqual(coordinator.analyze.call_args.args[2],'BTCUSDT')

    def test_fresh_actionable_reply_cannot_bypass_an_existing_symbol_exposure(self):
        module,coordinator=self.recovered(side='SHORT')
        coordinator.exposed_symbols=lambda account:{'BTCUSDT'}
        coordinator.analyze=Mock(side_effect=self.case.forbid)
        before='\n'.join(self.db.iterdump())
        result=self.tick(module,coordinator)
        self.assertEqual(result['wait_reason'],'ANALYSIS_RESPONSE_RECOVERY_UNVERIFIED')
        coordinator.analyze.assert_not_called()
        self.assertEqual('\n'.join(self.db.iterdump()),before)

    def test_live_risk_setting_and_original_request_risk_remain_unchanged(self):
        module,coordinator=self.recovered()
        self.db.execute("UPDATE robot_settings SET risk='7.25'")
        self.assertEqual(self.tick(module,coordinator)['bot_status'],'HOLD')
        self.assertEqual(self.db.execute('SELECT risk FROM robot_settings').fetchone()[0],'7.25')
        self.assertEqual(self.read('robot_jobs')['risk_target'],'5')
        self.assertIsNone(self.read('robot_candidates')['plan'])

    def test_transaction_failure_rolls_back_and_restart_consumes_once(self):
        from worker import analysis_response_recovery as recovery
        module,coordinator=self.recovered()
        before='\n'.join(self.db.iterdump());record=recovery.record_analysis_candidate
        def interrupted(*args,**kwargs):
            record(*args,**kwargs)
            raise RuntimeError('offline interruption before research commit')
        with patch.object(recovery,'record_analysis_candidate',side_effect=interrupted):
            result=self.tick(module,coordinator)
        self.assertEqual(result['wait_reason'],'ANALYSIS_RESPONSE_RECOVERY_UNVERIFIED')
        self.assertEqual('\n'.join(self.db.iterdump()),before)
        module,restarted=self.case.coordinator()
        restarted.execution_slots=lambda account,today:1
        self.assertEqual(self.tick(module,restarted)['bot_status'],'HOLD')
        changes=self.db.total_changes
        self.assertIsNone(resume_uncertain_analysis(restarted,DAY,{}))
        self.assertEqual(self.db.total_changes,changes)
        self.assertEqual(self.db.execute('SELECT count(*) FROM research_browser_responses').fetchone()[0],1)

    def test_original_native_tick_drains_old_day_hold_before_legacy_candidate_guard(self):
        module,coordinator=self.recovered()
        # Restore the real native method; the older fixture stubs unrelated
        # rollover work so it could not detect this earlier settlement gate.
        coordinator.settle_previous_research=module.Coordinator.settle_previous_research.__get__(coordinator)
        self.db.execute("UPDATE robot_cycles SET state='COMPLETE' WHERE id=?",(CYCLE,))
        self.case.provider.require_key=self.case.forbid
        self.case.provider.observe_received_analysis=self.case.forbid
        result=self.tick(module,coordinator,at=NOW+86400,today='2026-11-06')
        self.assertEqual(result['bot_status'],'HOLD')
        self.assertEqual(self.read('robot_candidates')['status'],'HOLD')
        self.assertEqual(self.read('robot_jobs')['state'],'COMPLETE')
        self.assertEqual(self.read('robot_cycles')['state'],'COMPLETE')
        self.assertEqual(self.db.execute('SELECT phase FROM analysis_response_recoveries').fetchone()[0],'CONSUMED')
        from agent.native_pending_review import screening_day_exclusions
        # Historical HOLD does not become a permanent next-day blacklist.
        self.assertNotIn('BTCUSDT',screening_day_exclusions(coordinator,'2026-11-06'))

    def test_original_native_tick_never_executes_old_day_actionable_reply(self):
        module,coordinator=self.recovered(side='SHORT')
        coordinator.settle_previous_research=module.Coordinator.settle_previous_research.__get__(coordinator)
        coordinator.analyze=self.case.forbid
        result=self.tick(module,coordinator,at=NOW+86400,today='2026-11-06')
        self.assertEqual(result['wait_reason'],'ANALYSIS_RECEIVED_STALE_REPLACEMENT_PENDING')
        self.assertEqual(self.read('robot_candidates')['failure_code'],'STALE_MARKET_CONTEXT')
        self.assertEqual(self.read('robot_cycles')['state'],'COMPLETE')

    def test_unbound_or_changed_cached_receipt_cannot_remove_the_failed_skeleton(self):
        for change in('proof_hash','preimage','output','response','request','material','approval','technical','other_error',
                'order','queue','off','unknown'):
            case=recovery_fixtures.AnalysisResponseRecoveryTests();case.setUp()
            try:
                data=dict(target=1,queue=[],seen=['BTCUSDT'],screen=0,replacements=0,round_symbols=['BTCUSDT'])
                case.db.execute('UPDATE robot_cycles SET data=?',(json.dumps(data),))
                self.assertEqual(recover_received_analysis(case.provider,case.envelope,now=NOW),'RESUME_CACHED')
                module,coordinator=case.coordinator();db=case.db
                if change=='proof_hash':db.execute("UPDATE analysis_response_recoveries SET proof_sha256=?",('0'*64,))
                elif change=='preimage':
                    row=dict(db.execute('SELECT * FROM analysis_response_recoveries').fetchone());before=json.loads(row['preimage'])
                    before['robot_candidates']['failure_code']='other'
                    db.execute('UPDATE analysis_response_recoveries SET preimage=?',(wire(before),))
                elif change=='output':db.execute("UPDATE api_requests SET output='{}'")
                elif change=='response':db.execute("UPDATE research_browser_responses SET output_sha256=?",('0'*64,))
                elif change=='request':db.execute("UPDATE research_browser_requests SET request_sha256=?",('0'*64,))
                elif change in('material','approval','technical'):
                    row=dict(db.execute('SELECT * FROM analysis_response_recoveries').fetchone());proof=json.loads(row['proof'])
                    if change=='material':proof['observation']['file_tiles'][0]['file_tile_witnesses'][0]['witness'][0]['ready']=False
                    elif change=='approval':proof['observation']['review_receipts']=[]
                    else:proof['observation']['rows'][-1]['text']='CSV belum terbaca.\n'+proof['observation']['rows'][-1]['text']
                    case.seal(proof);raw=wire(proof)
                    db.execute('UPDATE analysis_response_recoveries SET proof=?,proof_sha256=?',(raw,sha(raw)))
                elif change=='other_error':db.execute("UPDATE api_requests SET failure_code='OTHER_VALIDATION_FAILURE'")
                elif change=='order':db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?)',(OP,OP,'BTCUSDT','NEEDS_REVIEW','accepted intent'))
                elif change=='queue':
                    data=json.loads(case.row('robot_cycles')['data']);data['queue']=['ETHUSDT','BTCUSDT']
                    db.execute('UPDATE robot_cycles SET data=?',(json.dumps(data),))
                elif change=='off':db.execute('UPDATE robot_settings SET enabled=0')
                else:db.execute("UPDATE api_requests SET state='NEEDS_REVIEW'")
                before='\n'.join(db.iterdump());case.provider.require_key=case.forbid;case.provider.observe_received_analysis=case.forbid
                with self.subTest(change=change),patch('worker.analysis_response_recovery.time.time',return_value=NOW):
                    result=resume_uncertain_analysis(coordinator,DAY,{})
                    allowed={'approval':'ANALYSIS_MINI_APPROVAL_NOT_RETAINED','technical':'NEUROBRO_INPUT_UNREADABLE'}
                    self.assertTrue(result is None or result.get('wait_reason')==allowed.get(change,'ANALYSIS_RESPONSE_RECOVERY_UNVERIFIED'))
                    self.assertEqual('\n'.join(db.iterdump()),before)
                    self.assertEqual(case.row('robot_candidates')['status'],'REJECTED')
            finally:case.doCleanups()


if __name__=='__main__':unittest.main()
