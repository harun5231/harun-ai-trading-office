"""Atomic per-position projection, with no exchange or running controller."""
import copy
import json
import sqlite3
import unittest

from worker.core import Review
from worker.robot_store import RobotStore


def observation(client='hao-one', symbol='BTCUSDT', at='2026-11-01T01:00:00+00:00'):
    return {'source': 'BINANCE_FUTURES', 'state': 'PROTECTION_INCOMPLETE',
        'symbol': symbol, 'client_order_id': client, 'order_id': '11',
        'filled_quantity': '0.4', 'sl_confirmed': True, 'sl_order_id': '22',
        'tp_confirmed': False, 'tp_order_id': None, 'observed_at': at}


class ProtectionObservationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('CREATE TABLE robot_status(id INTEGER PRIMARY KEY,data TEXT); '
            'CREATE TABLE order_intents(id TEXT PRIMARY KEY,state TEXT); '
            "INSERT INTO order_intents VALUES('hao-one','ENTRY_PENDING');")
        self.original = {'bot_status': 'WAITING', 'risk_target_usdt': '5',
            'account_checked_at': '2026-11-01T01:00:00+00:00', 'running_positions': 2}
        self.db.execute('INSERT INTO robot_status VALUES(1,?)', (json.dumps(self.original),))
        self.store = RobotStore(self.db, initialize=False)

    def tearDown(self):
        self.db.close()

    def value(self):
        return json.loads(self.db.execute('SELECT data FROM robot_status').fetchone()[0])

    def record(self, value):
        self.db.execute('BEGIN IMMEDIATE')
        self.store.record_protection_observation(value)
        self.assertTrue(self.db.in_transaction)
        self.db.execute('COMMIT')

    def test_separate_positions_and_account_fields_survive_partial_confirmation(self):
        self.record(observation())
        second = observation('hao-two', 'ETHUSDT')
        second.update(state='POSITION_PROTECTED', tp_confirmed=True, tp_order_id='33')
        self.record(second)
        value = self.value()
        for key, original in self.original.items():
            self.assertEqual(value[key], original)
        self.assertEqual(set(value['protection_observations']), {'hao-one', 'hao-two'})
        self.assertFalse(value['protection_observations']['hao-one']['tp_confirmed'])
        self.assertTrue(value['protection_observations']['hao-two']['tp_confirmed'])
        self.record(dict(observation(), state='CLOSED', observed_at='2026-11-01T01:01:00+00:00'))
        self.assertEqual(set(self.value()['protection_observations']), {'hao-two'})

    def test_stale_incomplete_or_closed_observation_cannot_erase_newer_confirmation(self):
        newer = observation(at='2026-11-01T01:01:00+00:00')
        newer.update(state='POSITION_PROTECTED', tp_confirmed=True, tp_order_id='33')
        self.record(newer)
        before = copy.deepcopy(self.value())
        self.record(observation())
        self.record(dict(observation(), state='CLOSED'))
        self.assertEqual(self.value(), before)

    def test_projection_and_native_intent_share_parent_rollback_boundary(self):
        self.db.execute('BEGIN IMMEDIATE')
        self.db.execute("UPDATE order_intents SET state='PROTECTION_INCOMPLETE'")
        self.store.record_protection_observation(observation())
        self.db.execute('ROLLBACK')
        self.assertEqual(self.value(), self.original)
        self.assertEqual(self.db.execute('SELECT state FROM order_intents').fetchone()[0], 'ENTRY_PENDING')
        with self.assertRaisesRegex(Review, 'TRANSACTION_REQUIRED'):
            self.store.record_protection_observation(observation())
