"""Generic schedule fixtures in the existing monthly revision DB; no provider/payments."""
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from worker.monthly_billing import Store,recurrence_spec,validate
from tests.test_simple_monthly_billing import example_document


def initial(month='2027-01',day=31,paid=False):
    value=example_document(month);value['bills']=value['bills'][:1]
    row=value['bills'][0];row.update(due_date=month+'-'+str(day).zfill(2),paid=paid,recurring=True,recurrence_id='sample-monthly')
    value['simple']['recurrence_updates']={'sample-monthly':recurrence_spec(row)}
    return value


class MonthlyRecurrence(unittest.TestCase):
    def test_stable_unpaid_future_occurrence_no_get_write_or_paid_carry(self):
        with tempfile.TemporaryDirectory()as directory:
            store=Store(directory);source=initial(paid=True);store.save(source,0)
            first=store.display_document('2027-02');again=Store(directory).display_document('2027-02')
            self.assertEqual(first,again);self.assertEqual(store.read('2027-02')['version'],0)
            self.assertEqual(store.history('2027-02'),[])
            self.assertEqual(len(first['bills']),1);row=first['bills'][0]
            self.assertFalse(row['paid']);self.assertTrue(row['recurring']);self.assertEqual(row['due_date'],'2027-02-28')
            self.assertEqual(store.display_document('2027-03')['bills'][0]['due_date'],'2027-03-31')
            self.assertEqual(store.display_document('2028-02')['bills'][0]['due_date'],'2028-02-29')
            self.assertEqual(store.display_document('2026-12')['bills'],[])

    def test_paid_schedule_edit_only_future_unpaid_not_settled_financial_rows(self):
        with tempfile.TemporaryDirectory()as directory:
            store=Store(directory);jan=initial(paid=True);store.save(jan,0)
            feb=store.display_document('2027-02');feb['bills'][0]['paid']=True;store.save(feb,0)
            old_jan=deepcopy(jan['bills'][0]);old_feb=deepcopy(feb['bills'][0])
            spec=jan['simple']['recurrence_updates']['sample-monthly'];spec['bill']['amount']=spec['bill']['actual_idr']='180000';spec['day']=15
            store.save(jan,1)
            self.assertEqual(store.display_document('2027-01')['bills'][0],old_jan)
            self.assertEqual(store.display_document('2027-02')['bills'][0],old_feb)
            march=store.display_document('2027-03')['bills'][0]
            self.assertEqual(march['amount'],'180000');self.assertEqual(march['due_date'],'2027-03-15')
            # A payment-only save of an older copied occurrence does not overwrite the schedule.
            store.save(feb,1)
            self.assertEqual(store.display_document('2027-03')['bills'][0]['amount'],'180000')

    def test_stop_delete_persists_and_old_paid_history_unchanged(self):
        with tempfile.TemporaryDirectory()as directory:
            store=Store(directory);jan=initial(paid=True);store.save(jan,0)
            feb=store.display_document('2027-02');feb['bills']=[];feb['simple']['recurrence_updates']={'sample-monthly':None};store.save(feb,0)
            self.assertEqual(Store(directory).display_document('2027-03')['bills'],[])
            self.assertTrue(store.read('2027-01')['document']['bills'][0]['paid'])
            self.assertEqual(store.history('2027-01')[0]['document'],jan)
            self.assertFalse((Path(directory)/'ledger.sqlite3').exists())

    def test_stopped_schedule_removes_saved_unpaid_projection_without_rewriting_raw_history(self):
        with tempfile.TemporaryDirectory()as directory:
            store=Store(directory);jan=initial();store.save(jan,0)
            march=store.display_document('2027-03');store.save(march,0)
            jan['simple']['recurrence_updates']['sample-monthly']=None;jan['bills'][0]['recurring']=False;store.save(jan,1)
            self.assertEqual(store.display_document('2027-03')['bills'],[])
            self.assertEqual(store.read('2027-03')['document'],march)
            self.assertEqual(len(store.display_document('2027-01')['bills']),1)  # owner kept current one-off bill

    def test_unpaid_schedule_updates_saved_future_view_paid_month_stays_original(self):
        with tempfile.TemporaryDirectory()as directory:
            store=Store(directory);jan=initial(paid=True);store.save(jan,0)
            march=store.display_document('2027-03');store.save(march,0)
            jan['simple']['recurrence_updates']['sample-monthly']['bill'].update(name='Edited future subscription',amount='190000',actual_idr='190000')
            store.save(jan,1)
            self.assertEqual(store.display_document('2027-03')['bills'][0]['amount'],'190000')
            self.assertEqual(store.read('2027-03')['document']['bills'][0]['amount'],'160000')
            self.assertEqual(store.read('2027-01')['document']['bills'][0]['amount'],'160000')

    def test_topup_and_debt_are_not_automatic_monthly_subscriptions(self):
        value=initial();value['bills'][0]['kind']='prepaid_topup'
        with self.assertRaisesRegex(ValueError,'INVALID_RECURRENCE'):validate(value)
        value=initial();value['simple']['recurrence_updates']['sample-monthly']['day']=32
        with self.assertRaisesRegex(ValueError,'INVALID_RECURRENCE'):validate(value)
        value=initial();value['bills'][0]['recurring']='true'
        with self.assertRaisesRegex(ValueError,'INVALID_RECURRENCE'):validate(value)

    def test_initial_dates_preserved_with_future_effective_month_and_restart(self):
        with tempfile.TemporaryDirectory()as directory:
            store=Store(directory);jan=initial();store.save(jan,0)
            april=store.display_document('2027-04');april['simple']['recurrence_updates']={'sample-monthly':recurrence_spec(april['bills'][0],day=5)};store.save(april,0)
            self.assertEqual(Store(directory).display_document('2027-03')['bills'][0]['due_date'],'2027-03-31')
            self.assertEqual(Store(directory).display_document('2027-05')['bills'][0]['due_date'],'2027-05-05')
            self.assertEqual(store.read('2027-01')['document']['bills'][0]['due_date'],'2027-01-31')


class SnapshotPolicyDTO(unittest.TestCase):
    def test_capacity_metadata_derives_existing_policy_without_writes(self):
        from worker import robot_store
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.executescript('CREATE TABLE robot_status(id INTEGER,data TEXT);CREATE TABLE robot_candidates(symbol TEXT,status TEXT,failure_code TEXT);')
        store=object.__new__(robot_store.RobotStore);store.db=db
        store.settings=lambda:dict(robot_on=False,risk_target_usdt='7')
        store.entries=lambda today:0
        before=db.total_changes
        with patch.object(robot_store,'OrderGateway',return_value=SimpleNamespace(status=lambda:dict(connected=False,status='UNKNOWN'))),patch.object(robot_store.carry_quota,'ready',return_value=False):
            snapshot=store.snapshot()
        self.assertEqual(snapshot['max_positions'],robot_store.slots(0))
        self.assertEqual(snapshot['daily_entry_limit'],robot_store.slots(0))
        self.assertIsNone(snapshot['running_positions']);self.assertIsNone(snapshot['available_slots'])
        self.assertEqual(db.total_changes,before);self.assertFalse(snapshot['robot_on'])
        db.close()
