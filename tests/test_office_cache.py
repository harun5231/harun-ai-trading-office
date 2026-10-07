import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from worker.account_state import account_order
from worker.core import Ledger,day,now
from unittest.mock import patch
from worker.robot_store import RobotStore
from worker.pnl_calendar import unavailable as unavailable_calendar


class OfficeCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Ledger(Path(self.temp.name) / 'ledger.sqlite3')
        self.addCleanup(self.ledger.db.close)
        self.store = RobotStore(self.ledger.db)

    def observation(self, at=None):
        at = at or datetime.now(timezone.utc).isoformat()
        return dict(generated_at=at, account=dict(status='CONNECTED', checked_at=at,
            usdt_wallet_balance='100', usdt_available_balance='90', active_positions=1,
            positions=[dict(symbol='BTCUSDT')], **account_order()),
            reports=dict(status='PARTIAL', checked_at=at, pnl_today_usdt='4', complete=False),
            position_history=dict(status='PARTIAL', checked_at=at,
                items=[dict(id='1', symbol='BTCUSDT')], complete=False, kind='BINANCE_FILLS'))

    def test_delayed_observation_cannot_revive_account_after_failure(self):
        observation = self.observation()
        self.store.report_office(observation)
        self.store.report_office_failure('BINANCE_ACCOUNT_UNAVAILABLE')
        self.store.report_office(observation)
        snapshot = self.store.office_snapshot()
        for section in ('account', 'reports', 'position_history'):
            self.assertEqual(snapshot[section]['status'], 'UNAVAILABLE')
        self.assertIsNone(snapshot['reports']['pnl_today_usdt'])
        self.assertEqual(snapshot['position_history']['items'], [])

    def test_older_history_does_not_regress_current_reports(self):
        observation = self.observation()
        self.store.report_office(observation)
        earlier = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        older = copy.deepcopy(observation)
        older['generated_at'] = earlier
        older['reports'].update(checked_at=earlier, pnl_today_usdt='1')
        older['position_history'].update(checked_at=earlier, items=[])
        older.pop('account')
        self.store.report_office(older)
        snapshot = self.store.office_snapshot()
        self.assertEqual(snapshot['generated_at'], observation['generated_at'])
        self.assertEqual(snapshot['reports']['pnl_today_usdt'], '4')
        self.assertEqual(len(snapshot['position_history']['items']), 1)

    def test_past_receipt_does_not_take_ownership_of_new_manual_position(self):
        self.ledger.db.execute('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)',
            ('prior-entry', 'BTCUSDT', '2026-01-01', '2026-01-01T00:00:00+00:00'))
        self.store.report_office(self.observation())
        self.assertEqual(self.store.snapshot()['manual_exposure'], ['BTCUSDT'])

    def test_fresh_refresh_recovers_after_account_failure(self):
        self.store.report_office_failure('BINANCE_ACCOUNT_UNAVAILABLE')
        self.store.report_office(self.observation())
        snapshot = self.store.office_snapshot()
        self.assertEqual(snapshot['account']['status'], 'CONNECTED')
        self.assertEqual(snapshot['reports']['pnl_today_usdt'], '4')
        self.assertIsNone(snapshot['robot']['account_failure_code'])

    def test_off_keeps_fresh_running_position_visible_and_every_employee_idle(self):
        observation=self.observation()
        self.store.configure({'robot_on':True})
        self.store.report_office(observation)
        self.assertEqual(next(row['status'] for row in self.store.office_snapshot()['employees'] if row['id']=='position'),'WORKING')
        self.store.configure({'robot_on':False})
        # Completion of already-started work must not make an OFF worker active.
        self.store.report('ANALYZING')
        snapshot=self.store.office_snapshot()
        self.assertEqual(snapshot['robot']['bot_status'],'OFF')
        self.assertTrue(all(row['status']=='IDLE' for row in snapshot['employees']))
        self.assertEqual(snapshot['account'],{key:value for key,value in observation['account'].items() if not key.startswith('_')})
        self.assertEqual(snapshot['account']['active_positions'],1)
        self.assertEqual(snapshot['position_history']['items'],observation['position_history']['items'])

    def test_closed_entries_keep_daily_quota_spent_and_day_rollover_recomputes_slots(self):
        observation=self.observation()
        self.store.report_office(observation)
        for index in range(2):
            self.ledger.db.execute('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)',
                ('closed-'+str(index),'ETHUSDT',day(),now()))
        current=self.store.snapshot()
        self.assertEqual((current['bot_entries_today'],current['available_slots']),(2,0))
        # A cached account may still be current across midnight. New quota is
        # derived from the new day, while the carried position remains present.
        with patch('worker.robot_store.day',return_value='2099-01-01'):
            current=self.store.snapshot()
        self.assertEqual((current['bot_entries_today'],current['running_positions'],current['available_slots']),(0,1,1))

    def test_pending_entry_reserves_daily_quota_and_is_not_double_counted_as_a_position(self):
        self.store.report_office(self.observation())
        self.ledger.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
            ('pending','pending','BTCUSDT','ENTRY_PENDING','{}',None,None,now(),now()))
        self.assertEqual(self.store.snapshot()['available_slots'],1)
        self.ledger.db.execute('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)',
            ('today-filled','ETHUSDT',day(),now()))
        self.assertEqual(self.store.snapshot()['available_slots'],0)

    def test_old_office_snapshot_gets_calendar_contract_without_schema_migration(self):
        observation=self.observation()
        self.store.report_office(observation)
        snapshot=self.store.office_snapshot()
        self.assertEqual(snapshot['pnl_calendar']['start_date'],'2026-10-01')
        self.assertEqual(snapshot['pnl_calendar']['kind'],'CLOSED_POSITIONS_FROM_FILLS')
        self.assertEqual(snapshot['pnl_calendar']['positions'],[])

    def test_calendar_refresh_does_not_regress_to_older_history(self):
        observation=self.observation()
        calendar=unavailable_calendar()
        calendar.update(status='PARTIAL',checked_at=observation['generated_at'],positions=[dict(id='verified-closed')])
        observation['pnl_calendar']=calendar
        self.store.report_office(observation)
        older=copy.deepcopy(observation)
        older['pnl_calendar'].update(checked_at='2020-01-01T00:00:00Z',positions=[])
        self.store.report_office(older)
        self.assertEqual(self.store.office_snapshot()['pnl_calendar']['positions'],[dict(id='verified-closed')])

    def test_account_failure_clears_current_calendar_without_changing_receipts(self):
        observation=self.observation()
        observation['pnl_calendar']=dict(unavailable_calendar(),checked_at=observation['generated_at'],status='PARTIAL')
        self.store.report_office(observation)
        self.ledger.db.execute('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)',('prior','ETHUSDT',day(),now()))
        self.store.report_office_failure('BINANCE_ACCOUNT_UNAVAILABLE')
        snapshot=self.store.office_snapshot()
        self.assertEqual(snapshot['pnl_calendar']['status'],'UNAVAILABLE')
        self.assertEqual(snapshot['pnl_calendar']['positions'],[])
        self.assertEqual(self.store.entries(day()),1)
