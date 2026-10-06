import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from worker.account_state import account_order
from worker.core import Ledger
from worker.robot_store import RobotStore


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
