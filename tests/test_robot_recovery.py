"""Offline coordinator recovery: synthetic providers and separate account connections."""
import json
import unittest
from unittest.mock import patch

from worker.core import Ledger
from worker.robot import RobotStore,account_state,SCREENING_ONE
from worker.neuroapi import SETUP_SCHEMA
import test_robot as robot_fixtures

T1='2026-10-06T10:00:00.000100+00:00'
T2='2026-10-06T10:00:00.000200+00:00'
T3='2026-10-06T10:03:00.000300+00:00'

class RobotRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=robot_fixtures.RobotTests('test_default_risk_five');self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
    def restart(self):
        self.fixture.ledger.db.close();self.fixture.ledger=Ledger(self.fixture.path);self.fixture.make()
    def prepare_one_slot(self):
        f=self.fixture;f.account.position('HYPEUSDT','4.16');f.screens=[['BTCUSDT']];f.on()
        with patch('worker.robot.now',return_value=T1):f.robot.tick()
        return f
    def prepare_legacy_partial(self):
        f=self.fixture;f.screens=[['HYPEUSDT','BTCUSDT']];f.on();f.robot.tick();f.robot.tick()
        row=f.ledger.db.execute('SELECT id,data FROM robot_cycles').fetchone()
        data=json.loads(row['data']);data.pop('replacement_due',None)
        f.ledger.db.execute('UPDATE robot_cycles SET data=? WHERE id=?',(json.dumps(data),row['id']))
        return f,row['id'],data
    def test_analysis_final_report_preserves_newer_account_observation(self):
        f=self.prepare_one_slot();clock={'now':T1};original=f.client.transport
        other=Ledger(f.path);self.addCleanup(other.db.close);account_store=RobotStore(other.db)
        observed=[]
        def transport(*args,**kwargs):
            result=original(*args,**kwargs);f.account.position('BTCUSDT','2.5');clock['now']=T2
            observed.append(account_store.report_account(account_state(f.account,account_store,'2026-10-06')))
            clock['now']=T3;return result
        f.client.transport=transport
        with patch('worker.robot.now',side_effect=lambda:clock['now']):result=f.robot.tick()
        self.assertEqual(observed[0]['available_slots'],0)
        self.assertEqual(result['running_symbols'],['BTCUSDT','HYPEUSDT']);self.assertEqual(result['available_slots'],0)
        self.assertEqual(result['account_checked_at'],T2);self.assertEqual(result['checked_at'],T3)
        self.assertIsNone(result['account_failure_code'])
        review=result['setups'][0]['account_review']
        self.assertEqual(review['checked_at'],T2);self.assertTrue(review['symbol_exposed']);self.assertEqual(review['available_slots'],0)
        self.assertEqual(result['bot_status'],'SETUP_READY');self.assertFalse(result['live_execution'])
        self.assertEqual(len(f.calls),2);self.assertEqual(f.store.entries('2026-10-06'),0)
    def test_analysis_final_report_preserves_newer_account_failure(self):
        f=self.prepare_one_slot();clock={'now':T1};original=f.client.transport
        other=Ledger(f.path);self.addCleanup(other.db.close);account_store=RobotStore(other.db)
        def transport(*args,**kwargs):
            result=original(*args,**kwargs);clock['now']=T2
            account_store.report_account(reason='BINANCE_ACCOUNT_UNAVAILABLE');clock['now']=T3;return result
        f.client.transport=transport
        with patch('worker.robot.now',side_effect=lambda:clock['now']):result=f.robot.tick()
        self.assertEqual(result['account_failure_code'],'BINANCE_ACCOUNT_UNAVAILABLE')
        self.assertEqual(result['account_checked_at'],T2);self.assertIsNone(result['running_symbols'])
        self.assertIsNone(result['available_slots']);self.assertIsNone(result['usdt_wallet_balance'])
        review=result['setups'][0]['account_review']
        self.assertEqual(review['checked_at'],T2);self.assertIsNone(review['symbol_exposed']);self.assertIsNone(review['available_slots'])
    def test_report_without_account_does_not_refresh_or_clear_observation(self):
        f=self.prepare_one_slot();before=f.store.snapshot()
        with patch('worker.robot.now',return_value=T3):result=f.store.report('WAITING',reason='WORKER_BUSY')
        self.assertEqual(result['account_checked_at'],T1);self.assertEqual(result['checked_at'],T3)
        self.assertEqual(result['running_symbols'],before['running_symbols'])
        self.assertEqual(result['usdt_wallet_balance'],before['usdt_wallet_balance'])
    def test_delayed_older_account_poll_cannot_replace_newer_poll(self):
        f=self.prepare_one_slot()
        with patch('worker.robot.now',return_value=T1):older=account_state(f.account,f.store,'2026-10-06')
        f.account.position('BTCUSDT','2.5')
        with patch('worker.robot.now',return_value=T2):newer=account_state(f.account,f.store,'2026-10-06')
        f.store.report_account(newer)
        with patch('worker.robot.now',return_value=T3):result=f.store.report_account(older)
        self.assertEqual(result['account_checked_at'],T2);self.assertEqual(result['running_positions'],2)
        self.assertEqual(result['available_slots'],0)
    def test_account_observation_updates_after_utc_clock_moves_backwards(self):
        f=self.prepare_one_slot();f.account.position('BTCUSDT','2.5')
        earlier='2026-10-06T09:00:00.000000+00:00'
        with patch('worker.robot.now',return_value=earlier):
            latest=account_state(f.account,f.store,'2026-10-06');result=f.store.report_account(latest)
        self.assertEqual(result['account_checked_at'],earlier);self.assertEqual(result['available_slots'],0)
        self.assertEqual(result['running_symbols'],['BTCUSDT','HYPEUSDT'])
        self.assertNotIn('_account_generation',result);self.assertNotIn('_account_revision',result)
    def test_restart_generation_supersedes_future_persisted_account_time(self):
        f=self.prepare_one_slot();f.account.position('BTCUSDT','2.5')
        earlier='2026-10-05T09:00:00.000000+00:00'
        with patch('worker.robot.ACCOUNT_GENERATION','new-process-fixture'),patch('worker.robot.now',return_value=earlier):
            self.restart();result=f.store.report_account(account_state(f.account,f.store,'2026-10-06'))
        self.assertEqual(result['account_checked_at'],earlier);self.assertEqual(result['available_slots'],0)
        self.assertEqual(result['running_symbols'],['BTCUSDT','HYPEUSDT'])
    def test_legacy_ticket_review_does_not_use_coordinator_time_as_account_time(self):
        f=self.prepare_one_slot();f.robot.tick()
        row=f.ledger.db.execute('SELECT data FROM robot_status WHERE id=1').fetchone()
        value=json.loads(row[0]);value.pop('account_checked_at');value['checked_at']=T3
        f.ledger.db.execute('UPDATE robot_status SET data=? WHERE id=1',(json.dumps(value),))
        self.assertIsNone(f.store.snapshot()['setups'][0]['account_review']['checked_at'])
    def test_queued_manual_conflict_leaves_free_slot_for_other_queued_candidate(self):
        f=self.fixture;f.on();f.robot.tick();f.account.position('BTCUSDT','3')
        conflict=f.robot.tick();self.assertEqual(conflict['failure_code'],'ROBOT_SYMBOL_EXPOSED')
        self.assertEqual(len(f.calls),1);self.restart();result=f.ticks(4)
        self.assertEqual(result['available_slots'],1);self.assertEqual(result['manual_exposure'],['BTCUSDT'])
        outcomes={row['symbol']:row['status'] for row in result['setups']}
        self.assertEqual(outcomes,{'BTCUSDT':'REJECTED','ETHUSDT':'SETUP_READY'})
        self.assertEqual(len(f.calls),2);self.assertEqual(f.calls[1]['output_schema'],SETUP_SCHEMA)
        self.assertEqual(json.loads(f.calls[1]['message_history'][0]['content'])['symbol'],'ETHUSDT')
        f.ticks(20);self.assertEqual(len(f.calls),2)
        cycle=f.ledger.db.execute('SELECT state,data FROM robot_cycles').fetchone()
        self.assertEqual(cycle['state'],'COMPLETE');self.assertEqual(json.loads(cycle['data'])['target'],2)
        self.assertEqual(f.account.positions[0]['positionAmt'],'3');self.assertTrue(all(m=='GET' for m,_ in f.account.calls))
    def test_ready_ticket_still_reserves_current_free_slot(self):
        f=self.fixture;f.on();f.robot.tick();f.account.position('HYPEUSDT','4.16');f.robot.tick()
        result=f.ticks(5)
        self.assertEqual(result['available_slots'],1);self.assertEqual(len(result['setups']),1)
        self.assertEqual(len(f.calls),2)
        cycle=json.loads(f.ledger.db.execute('SELECT data FROM robot_cycles').fetchone()[0])
        self.assertEqual(cycle['queue'],['ETHUSDT']);self.assertEqual(cycle['target'],2)
    def test_exposed_ready_ticket_does_not_double_reserve_free_slot(self):
        f=self.fixture;f.on();f.robot.tick();f.robot.tick();f.account.position('BTCUSDT','3')
        self.restart();result=f.ticks(4)
        self.assertEqual(result['available_slots'],1);self.assertEqual(result['manual_exposure'],['BTCUSDT'])
        self.assertEqual({r['symbol']:r['status'] for r in result['setups']},{'BTCUSDT':'SETUP_READY','ETHUSDT':'SETUP_READY'})
        self.assertEqual(len(f.calls),3);self.assertEqual(json.loads(f.calls[2]['message_history'][0]['content'])['symbol'],'ETHUSDT')
        self.assertTrue(next(r for r in result['setups'] if r['symbol']=='BTCUSDT')['account_review']['symbol_exposed'])
        cycle=json.loads(f.ledger.db.execute('SELECT data FROM robot_cycles').fetchone()[0])
        self.assertEqual(cycle['target'],2);self.assertEqual(cycle['replacements'],0)
        f.ticks(10);self.assertEqual(len(f.calls),3)
    def test_one_slot_target_does_not_expand_after_manual_position_closes(self):
        f=self.prepare_one_slot();f.ticks(3);f.account.positions=[];result=f.ticks(3)
        self.assertEqual(result['available_slots'],2);self.assertEqual(len(f.calls),2)
        cycle=json.loads(f.ledger.db.execute('SELECT data FROM robot_cycles').fetchone()[0])
        self.assertEqual(cycle['target'],1);self.assertEqual(len(result['setups']),1)
    def test_legacy_partial_exclusion_recovers_without_replaying_ready_analysis(self):
        f,cycle,_=self.prepare_legacy_partial()
        old_plan=f.ledger.db.execute("SELECT plan FROM robot_setups WHERE symbol='BTCUSDT'").fetchone()[0]
        f.screens.append(['ETHUSDT']);self.restart();result=f.ticks(4)
        self.assertEqual(len(f.calls),4);self.assertEqual(f.calls[2]['prompt'],SCREENING_ONE)
        self.assertEqual({s['symbol']:s['status'] for s in result['setups']},{'BTCUSDT':'SETUP_READY','ETHUSDT':'SETUP_READY'})
        self.assertEqual(f.ledger.db.execute("SELECT plan FROM robot_setups WHERE symbol='BTCUSDT'").fetchone()[0],old_plan)
        saved=json.loads(f.ledger.db.execute('SELECT data FROM robot_cycles WHERE id=?',(cycle,)).fetchone()[0])
        self.assertEqual(saved['replacements'],1);self.assertEqual(saved['target'],2)
        self.restart();f.ticks(10);self.assertEqual(len(f.calls),4)
    def test_legacy_partial_recovery_keeps_original_replacement_cap(self):
        f,cycle,data=self.prepare_legacy_partial();data['replacements']=3
        f.ledger.db.execute('UPDATE robot_cycles SET data=? WHERE id=?',(json.dumps(data),cycle))
        result=f.ticks(10)
        self.assertEqual(result['bot_status'],'INSUFFICIENT_ACTIONABLE_SETUPS');self.assertEqual(len(f.calls),2)
        saved=json.loads(f.ledger.db.execute('SELECT data FROM robot_cycles WHERE id=?',(cycle,)).fetchone()[0])
        self.assertEqual(saved['replacements'],3)
    def test_legacy_partial_recovery_requires_completed_screen_evidence(self):
        f,cycle,_=self.prepare_legacy_partial()
        f.ledger.db.execute("UPDATE api_requests SET state='NEEDS_REVIEW' WHERE operation LIKE ?",(cycle+':screening:%',))
        result=f.ticks(5)
        self.assertEqual(len(f.calls),2);self.assertEqual(result['wait_reason'],'ROBOT_CYCLE_COMPLETE')
    def test_legacy_partial_recovery_never_overrides_pending_paid_job(self):
        f,cycle,_=self.prepare_legacy_partial()
        f.ledger.db.execute("UPDATE robot_jobs SET state='PENDING' WHERE operation LIKE ?",(cycle+':screening:%',))
        result=f.ticks(5)
        self.assertEqual(len(f.calls),2);self.assertEqual(result['failure_code'],'ROBOT_REQUEST_NEEDS_REVIEW')

if __name__=='__main__':unittest.main()
