"""Persisted synthetic traces stay separate from actual account/research records."""
import json
import unittest
from unittest.mock import patch

from worker.core import D,Ledger,Review
from worker.manual_ticket import SCENARIOS
import test_robot as robot_fixtures


class RobotSimulationTests(unittest.TestCase):
    def setUp(self):
        self.f=robot_fixtures.RobotTests('test_default_risk_five');self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.account.position('HYPEUSDT','4.16');self.f.screens=[['BTCUSDT']]
        self.f.on();self.status=self.f.ticks(3)
        self.setup_id=self.status['setups'][0]['id']
    def request(self,scenario='FULL_TP'):
        return self.f.store.simulate(dict(setup_id=self.setup_id,scenario=scenario))
    def count(self,table):
        return self.f.ledger.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
    def test_one_manual_slot_exposes_verified_manual_ticket(self):
        row=self.status['setups'][0];ticket=row['ticket']
        self.assertEqual(self.status['available_slots'],1)
        self.assertEqual(self.status['manual_exposure'],['HYPEUSDT'])
        self.assertEqual(ticket['execution_mode'],'MANUAL_ONLY');self.assertFalse(ticket['submission_enabled'])
        self.assertEqual(ticket['entry']['quantity'],row['execution_quantity'])
        self.assertEqual(ticket['stop_loss']['trigger_price'],row['sl'])
        self.assertEqual(ticket['take_profit']['trigger_price'],row['tp'])
        self.assertEqual(ticket['risk_target_usdt'],'5');self.assertTrue(row['account_review']['setup_from_current_day'])
        self.assertFalse(row['account_review']['symbol_exposed'])
    def test_all_scenarios_have_no_exchange_or_research_side_effects(self):
        tables=('robot_jobs','api_requests','robot_entry_receipts','robot_decisions','trades','robot_cycles')
        before={table:self.count(table) for table in tables}
        status_before=self.f.ledger.db.execute('SELECT data FROM robot_status').fetchone()[0]
        calls=(len(self.f.calls),len(self.f.account.calls),len(self.f.market.calls))
        with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('simulation must stay offline')):
            for scenario in SCENARIOS:
                with self.subTest(scenario=scenario):
                    response=self.request(scenario)
                    result=response['simulation']
                    self.assertEqual(result['mode'],'SIMULATION');self.assertTrue(result['synthetic_trace'])
                    self.assertFalse(result['real_order_submitted']);self.assertFalse(response['live_execution'])
                    self.assertEqual(response['usdt_wallet_balance'],'117.25')
                    self.assertEqual(response['running_positions'],1)
        self.assertEqual(before,{table:self.count(table) for table in tables})
        self.assertEqual(status_before,self.f.ledger.db.execute('SELECT data FROM robot_status').fetchone()[0])
        self.assertEqual(calls,(len(self.f.calls),len(self.f.account.calls),len(self.f.market.calls)))
        self.assertEqual(self.count('robot_simulations'),len(SCENARIOS))
    def test_repeated_scenario_is_idempotent_and_displays_requested_result(self):
        first=self.request('FULL_TP')['simulation']
        self.request('FULL_SL')
        replay=self.request('FULL_TP')
        self.assertEqual(replay['simulation'],first)
        self.assertEqual(replay['setups'][0]['simulation'],first)
        self.assertEqual(self.count('robot_simulations'),2)
    def test_restart_preserves_trace_without_research_replay(self):
        result=self.request('FULL_SL')['simulation'];paid=len(self.f.calls)
        self.f.ledger.db.close();self.f.ledger=Ledger(self.f.path);self.f.make()
        reopened=self.f.store.snapshot()
        self.assertEqual(reopened['setups'][0]['simulation'],result)
        self.assertEqual(D(result['pnl_usdt']),D('-5'))
        self.f.ticks(3);self.assertEqual(len(self.f.calls),paid)
    def test_simulation_does_not_block_coordinator_or_change_approval(self):
        self.request('PROTECTION_FAILURE')
        self.assertEqual(self.f.store.snapshot()['setups'][0]['status'],'SETUP_READY')
        paid=len(self.f.calls);status=self.f.ticks(3)
        self.assertEqual(status['bot_status'],'SETUP_READY');self.assertEqual(len(self.f.calls),paid)
        self.f.store.approve(self.setup_id,'APPROVED')
        result=self.request('FULL_TP')
        self.assertEqual(result['setups'][0]['status'],'APPROVED')
        self.assertEqual(self.count('robot_entry_receipts'),0)
    def test_off_does_not_prevent_explicit_offline_simulation(self):
        self.f.store.configure({'robot_on':False});result=self.request()
        self.assertFalse(result['robot_on']);self.assertEqual(result['simulation']['status'],'CLOSED')
        self.assertEqual(result['wait_reason'],'ROBOT_OFF')
    def test_rejected_or_tampered_setup_has_no_ticket_or_simulation(self):
        self.f.store.approve(self.setup_id,'USER_REJECTED')
        with self.assertRaises(Review):self.request()
        self.assertNotIn('ticket',self.f.store.snapshot()['setups'][0])
        self.assertEqual(self.count('robot_simulations'),0)
        row=self.f.ledger.db.execute('SELECT plan FROM robot_setups WHERE id=?',(self.setup_id,)).fetchone()
        plan=json.loads(row['plan']);plan['entry']='101'
        self.f.ledger.db.execute("UPDATE robot_setups SET status='SETUP_READY',plan=? WHERE id=?",(json.dumps(plan),self.setup_id))
        with self.assertRaises(Review):self.request()
        invalid=self.f.store.snapshot()['setups'][0]
        self.assertEqual(invalid['failure_code'],'ROBOT_SETUP_UNVERIFIED');self.assertNotIn('ticket',invalid)
    def test_unknown_invalid_or_excessive_simulation_request_has_no_records(self):
        for value in (None,{},dict(setup_id=self.setup_id,scenario='LIVE'),dict(setup_id=self.setup_id,scenario=True),dict(setup_id='x'*161,scenario='FULL_TP'),dict(setup_id=self.setup_id,scenario='FULL_TP',live=True),dict(setup_id='unknown',scenario='FULL_TP')):
            with self.subTest(value=value),self.assertRaises(Review):self.f.store.simulate(value)
        self.assertEqual(self.count('robot_simulations'),0)
    def test_persisted_result_tampering_does_not_become_execution_evidence(self):
        self.request()
        self.f.ledger.db.execute("UPDATE robot_simulations SET result=?",(json.dumps(dict(mode='LIVE',real_order_submitted=True,pnl_usdt='100000')),))
        row=self.f.store.snapshot()['setups'][0]
        self.assertNotIn('simulation',row);self.assertEqual(row['simulation_failure_code'],'ROBOT_SIMULATION_UNVERIFIED')
        self.assertFalse(self.f.store.snapshot()['live_execution'])
        with self.assertRaises(Review):self.request()
    def test_account_review_reports_full_capacity_without_claiming_order_safety(self):
        self.f.account.position('BTCUSDT','0.1');self.f.robot.tick()
        row=self.f.store.snapshot()['setups'][0]
        self.assertTrue(row['account_review']['symbol_exposed'])
        self.assertEqual(row['account_review']['available_slots'],0)
        self.assertTrue(row['account_review']['review_required']);self.assertFalse(row['ticket']['submission_enabled'])
    def test_changed_setup_symbol_or_cycle_cannot_create_mislabelled_ticket(self):
        row=self.f.ledger.db.execute('SELECT symbol,cycle FROM robot_setups WHERE id=?',(self.setup_id,)).fetchone()
        for field,value in (('symbol','ETHUSDT'),('cycle','unrelated-cycle')):
            with self.subTest(field=field):
                self.f.ledger.db.execute('UPDATE robot_setups SET '+field+'=? WHERE id=?',(value,self.setup_id))
                corrupted=self.f.store.snapshot()['setups'][0]
                self.assertEqual(corrupted['failure_code'],'ROBOT_SETUP_UNVERIFIED');self.assertNotIn('ticket',corrupted)
                with self.assertRaisesRegex(Review,'^ROBOT_SETUP_UNVERIFIED$'):self.request()
                with self.assertRaisesRegex(Review,'^ROBOT_SETUP_UNVERIFIED$'):self.f.store.approve(self.setup_id,'APPROVED')
                self.f.ledger.db.execute('UPDATE robot_setups SET '+field+'=? WHERE id=?',(row[field],self.setup_id))
        self.assertEqual(self.count('robot_simulations'),0);self.assertEqual(self.count('robot_decisions'),0)
