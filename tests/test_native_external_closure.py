"""Offline signed-receipt-shaped fixtures; no live application or network."""
import copy
from datetime import datetime, timezone
import json
import time
import unittest
from zoneinfo import ZoneInfo

from worker.core import D, Rules, Signal, Review, RISK_MODEL, NORMALIZED_REWARD_RISK_POLICY, risk_check
from worker.research_guard import gateway_mutations
from tests import test_native_gateway_owner_revision as native
from tests.test_confirmed_exit_costs import ProvenExitExchange


def wld_plan():
    clock = time.time()
    rules = Rules(D('1'), D('1'), D('100000'), D('.00001'), D('1'), clock,
        taker_fee_rate=D('.0005'), fee_observed_at=clock, fee_symbol='WLDUSDT',
        fee_source='BINANCE_FUTURES_COMMISSION_RATE')
    plan = risk_check(Signal('WLDUSDT', 'SHORT', D('.4035'), D('.38'), D('.4085')),
        rules, D('5'), risk_model=RISK_MODEL, reward_risk_policy=NORMALIZED_REWARD_RISK_POLICY)
    plan['sizing_rules'] = native.serialized_rules(rules)
    plan['protection_working_type'] = 'CONTRACT_PRICE'
    plan['provenance'] = {'payload_sha256': 'a'*64}
    return plan


class ManualClosureExchange(ProvenExitExchange):
    def __init__(self, intent):
        super().__init__(intent)
        self.entry['orderId'] = 24568311277
        self.entry_trade.update(id=1404505230, orderId=24568311277, price='0.4035000',
            commission='0.05414970', realizedPnl='0')
        self.exit.update(orderId=24608046761, clientOrderId='ios_Cr14PWeVjKT6XeWwNYUM')
        self.exit_trade.update(id=1404814267, orderId=24608046761, price='0.3863000',
            realizedPnl='11.54120000', commission='0.12960364')
        self.history = [self.entry_trade, self.exit_trade]
        self.position = []
        self.algos = []
        self.open_orders = []
        self.truncated = False

    def _request(self, method, path, params):
        self.calls.append((method, path, copy.deepcopy(params)))
        if method != 'GET':
            raise AssertionError('manual closure must be strictly GET-only')
        if path == '/fapi/v1/accountConfig':
            return dict(canTrade=True, dualSidePosition=False, multiAssetsMargin=False)
        if path == '/fapi/v1/order':
            return copy.deepcopy(self.exit if 'orderId' in params else self.entry)
        if path == '/fapi/v1/allAlgoOrders':
            return copy.deepcopy(self.algos)
        if path == '/fapi/v1/algoOrder':
            return copy.deepcopy(self.algo)
        if path == '/fapi/v1/openOrders':
            return copy.deepcopy(self.open_orders)
        if path == '/fapi/v1/openAlgoOrders':
            return []
        if path == '/fapi/v3/positionRisk':
            return copy.deepcopy(self.position)
        if path == '/fapi/v1/userTrades':
            rows = self.history
            if 'orderId' in params:
                rows = [r for r in rows if str(r['orderId']) == str(params['orderId'])]
            elif self.truncated:
                rows = rows * 500
            return copy.deepcopy(rows)
        raise AssertionError(path)


class NativeExternalClosureTests(unittest.TestCase):
    def fixture(self):
        intent = native.gateway.build_intent(wld_plan(), 'offline-WLD-manual-close')
        self.assertEqual(intent['entry']['quantity'], '671')
        return intent, ManualClosureExchange(intent)

    def reconcile(self, intent, exchange):
        with gateway_mutations(lambda: False):
            return exchange.reconcile_protection(intent)

    def test_wld_exact_full_manual_reduce_only_closure_preserves_receipts_not_algo_costs(self):
        intent, exchange = self.fixture()
        frozen = copy.deepcopy(intent)
        result = self.reconcile(intent, exchange)
        self.assertEqual(result['state'], 'CLOSED')
        self.assertEqual(result['closure_source'], 'EXTERNAL_REDUCE_ONLY')
        self.assertEqual(result['filled_quantity'], '671')
        self.assertEqual(result['exit_order_id'], '24608046761')
        receipt = result['closure_receipt']
        self.assertEqual(receipt['exit_trades'][0]['realizedPnl'], '11.54120000')
        self.assertEqual(receipt['exit_trades'][0]['commission'], '0.12960364')
        self.assertEqual(receipt['exit_trades'][0]['price'], '0.3863000')
        self.assertEqual(receipt['entry_trades'][0]['time'], exchange.first)
        self.assertEqual(result['closed_at'], datetime.fromtimestamp(
            exchange.exit_trade['time']/1000, timezone.utc).isoformat())
        self.assertFalse(result['sl_confirmed'] or result['tp_confirmed'])
        self.assertEqual(exchange.confirmed_execution_observations(), [])
        self.assertEqual(intent, frozen)
        self.assertTrue(all(m == 'GET' for m, _, _ in exchange.calls))
    def test_rejects_ambiguous_or_incomplete_exchange_proofs_without_writes(self):
        mutations = {
            'unowned_entry': lambda e: e.entry.update(clientOrderId='manual-entry'),
            'non_reduce_only': lambda e: e.exit.update(reduceOnly=False),
            'bool_impostor': lambda e: e.exit.update(reduceOnly=1),
            'wrong_exit_side': lambda e: e.exit.update(side='SELL'),
            'partial_exit': lambda e: (e.exit.update(executedQty='670'), e.exit_trade.update(qty='670')),
            'nonzero_position': lambda e: e.position.append(dict(symbol='WLDUSDT', positionSide='BOTH', positionAmt='1', liquidationPrice='0')),
            'truncated_history': lambda e: setattr(e, 'truncated', True),
            'mismatched_order': lambda e: e.exit.update(orderId=999),
            'wrong_order_symbol': lambda e: e.exit.update(symbol='OTHERUSDT'),
            'not_filled': lambda e: e.exit.update(status='PARTIALLY_FILLED'),
            'native_client': lambda e: e.exit.update(clientOrderId='hao-another-native-owner'),
            'another_entry': lambda e: e.history.append(dict(e.entry_trade, id=999, orderId=123)),
            'duplicate_trade': lambda e: e.history.append(copy.deepcopy(e.exit_trade)),
            'missing_pnl': lambda e: e.exit_trade.pop('realizedPnl'),
            'bad_fee': lambda e: e.exit_trade.update(commission='NaN'),
            'overclose': lambda e: (e.exit.update(origQty='672', executedQty='672'), e.exit_trade.update(qty='672')),
            'exit_before_entry_complete': lambda e: e.exit_trade.update(time=e.first),
            'partial_entry': lambda e: e.entry.update(status='PARTIALLY_FILLED'),
            'live_native_algo': lambda e: (e.algo.update(algoStatus='NEW', actualOrderId=0), e.algos.append(e.algo)),
            'manual_pending_order': lambda e: e.open_orders.append(dict(symbol='WLDUSDT', orderId=999, clientOrderId='ios-pending')),
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                intent, exchange = self.fixture()
                mutate(exchange)
                with self.assertRaises(Review):
                    self.reconcile(intent, exchange)
                self.assertEqual(exchange.confirmed_execution_observations(), [])
                self.assertTrue(all(m == 'GET' for m, _, _ in exchange.calls))

    def coordinator_fixture(self):
        from pathlib import Path
        import tempfile
        from datetime import timedelta
        from worker.core import Ledger, day
        from tests.test_coordinator_protection_revision import native_coordinator
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        ledger = Ledger(Path(temporary.name)/'isolated.sqlite3')
        self.addCleanup(ledger.db.close)
        plan = wld_plan()
        operation = 'offline-WLD-manual-close'
        intent = native.gateway.build_intent(plan, operation)
        exchange = ManualClosureExchange(intent)
        coordinator = native_coordinator()(ledger, None, None, None, gateway=exchange)
        coordinator.store.verified_plan = lambda candidate: copy.deepcopy(plan)
        coordinator.store.configure({'robot_on': False})
        created = (datetime.now(timezone.utc)-timedelta(seconds=20)).isoformat()
        ledger.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
            (operation, day()+':robot-v9:0', plan['symbol'], 'NEEDS_REVIEW', json.dumps(plan), 'ORDER_OUTCOME_UNKNOWN'))
        ledger.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
            (operation, operation, plan['symbol'], 'NEEDS_REVIEW', json.dumps(intent), None,
                'ORDER_OUTCOME_UNKNOWN', created, created))
        coordinator.store.report('OFF')
        return coordinator, exchange, intent

    def test_existing_protection_observation_can_advance_to_external_closure(self):
        coordinator, exchange, intent = self.coordinator_fixture()
        result = self.reconcile(intent, exchange)
        previous = {k: v for k, v in result.items() if k not in ('closure_source', 'closure_receipt', 'closed_at', 'exit_order_id')}
        previous.update(state='POSITION_PROTECTED', sl_confirmed=True, tp_confirmed=True,
            sl_order_id='202', tp_order_id='203')
        coordinator.record_gateway_observation(intent['intent_id'], intent['intent_id'], previous, None)
        self.assertEqual(coordinator.db.execute('SELECT state FROM order_intents').fetchone()[0], 'POSITION_PROTECTED')
        coordinator.record_gateway_observation(intent['intent_id'], intent['intent_id'], result, None)
        self.assertEqual(coordinator.db.execute('SELECT state FROM order_intents').fetchone()[0], 'CLOSED')
        status = json.loads(coordinator.db.execute('SELECT data FROM robot_status').fetchone()[0])
        self.assertNotIn(intent['client_order_id'], status.get('protection_observations', {}))

    def test_same_day_manual_close_releases_reservation_without_resetting_filled_quota(self):
        from contextlib import closing
        import sqlite3
        from worker.robot_store import RobotStore
        from tests.test_carry_quota import Client
        coordinator, exchange, intent = self.coordinator_fixture()
        coordinator.store.configure({'robot_on': True})
        result = self.reconcile(intent, exchange)
        today = datetime.fromtimestamp(exchange.first/1000, timezone.utc).astimezone(
            ZoneInfo('Asia/Jakarta')).date().isoformat()
        previous = {key: value for key, value in result.items()
            if key not in ('closure_source', 'closure_receipt', 'closed_at', 'exit_order_id')}
        previous.update(state='POSITION_PROTECTED', sl_confirmed=True, tp_confirmed=True,
            sl_order_id='202', tp_order_id='203')
        coordinator.record_gateway_observation(intent['intent_id'], intent['intent_id'], previous, None)
        account = Client([dict(symbol=intent['symbol'], positionSide='BOTH',
            positionAmt='-'+intent['entry']['quantity'], updateTime=exchange.first)],
            [exchange.entry_trade])
        account.anchor = exchange.now
        self.assertTrue(coordinator.store.reconcile_carry(account,today,[intent['symbol']]))
        self.assertEqual(coordinator.store.entries(today),1)
        self.assertEqual(coordinator.store.available_slots(1,[intent['symbol']],today),1)
        receipts = [tuple(row) for row in coordinator.db.execute('SELECT * FROM robot_entry_receipts')]
        frozen = coordinator.db.execute('SELECT payload FROM order_intents').fetchone()[0]
        for _ in range(2):
            coordinator.record_gateway_observation(intent['intent_id'], intent['intent_id'], result, None)
            self.assertEqual(coordinator.db.execute('SELECT state FROM order_intents').fetchone()[0],'CLOSED')
            self.assertEqual(coordinator.db.execute("SELECT COUNT(*) FROM order_intents WHERE state IN "
                "('ENTRY_PENDING','POSITION_PROTECTED','PROTECTION_INCOMPLETE')").fetchone()[0],0)
            self.assertEqual(coordinator.store.available_slots(0,[],today),1)
            self.assertEqual(coordinator.store.entries(today),1)
        self.assertEqual(coordinator.db.execute('SELECT payload FROM order_intents').fetchone()[0],frozen)
        path = coordinator.db.execute('PRAGMA database_list').fetchone()[2]
        with closing(sqlite3.connect(path,isolation_level=None)) as restarted:
            restarted.row_factory = sqlite3.Row
            store = RobotStore(restarted)
            self.assertTrue(store.settings()['robot_on'])
            self.assertEqual(store.available_slots(0,[],today),1)
            self.assertEqual(store.entries(today),1)
            self.assertEqual([tuple(row) for row in restarted.execute('SELECT * FROM robot_entry_receipts')],receipts)
            self.assertEqual(restarted.execute('SELECT state FROM order_intents').fetchone()[0],'CLOSED')
        self.assertTrue(all(method == 'GET' for method,_,_ in exchange.calls))

    def test_competing_local_owners_stay_needs_review(self):
        for other_state in ('NEEDS_REVIEW', 'SUBMITTING', 'POSITION_PROTECTED', 'CLOSED'):
            with self.subTest(other_state=other_state):
                coordinator, exchange, intent = self.coordinator_fixture()
                result = self.reconcile(intent, exchange)
                row = coordinator.db.execute('SELECT * FROM order_intents').fetchone()
                coordinator.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
                    ('other-owner', 'other-owner', intent['symbol'], other_state, row['payload'],
                     json.dumps(result) if other_state == 'CLOSED' else None, None, row['created'], row['updated']))
                coordinator.record_gateway_observation(intent['intent_id'], intent['intent_id'], result, None)
                original = coordinator.db.execute('SELECT * FROM order_intents WHERE id=?', (intent['intent_id'],)).fetchone()
                self.assertEqual(original['state'], 'NEEDS_REVIEW')
                self.assertIsNone(original['result'])
                self.assertEqual(coordinator.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 0)

    def test_same_entry_claimed_by_closed_owner_rejects_even_with_disjoint_dates(self):
        from datetime import timedelta
        coordinator, exchange, intent = self.coordinator_fixture()
        result = self.reconcile(intent, exchange)
        other = copy.deepcopy(result)
        other['first_fill_at'] = (datetime.fromisoformat(result['first_fill_at'])-timedelta(days=2)).isoformat()
        other['closed_at'] = (datetime.fromisoformat(result['closed_at'])-timedelta(days=2)).isoformat()
        row = coordinator.db.execute('SELECT * FROM order_intents').fetchone()
        coordinator.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
            ('duplicate-owner', 'duplicate-owner', intent['symbol'], 'CLOSED', row['payload'],
             json.dumps(other), None, row['created'], row['updated']))
        coordinator.record_gateway_observation(intent['intent_id'], intent['intent_id'], result, None)
        original = coordinator.db.execute('SELECT * FROM order_intents WHERE id=?', (intent['intent_id'],)).fetchone()
        self.assertEqual(original['state'], 'NEEDS_REVIEW')
        self.assertIsNone(original['result'])

    def test_same_exit_claimed_by_disjoint_closed_owner_is_not_charged_twice(self):
        from datetime import timedelta
        coordinator, exchange, intent = self.coordinator_fixture()
        result = self.reconcile(intent, exchange)
        other = copy.deepcopy(result)
        other.update(order_id='999', client_order_id='hao-other-entry', closure_source='NATIVE')
        other['first_fill_at'] = (datetime.fromisoformat(result['first_fill_at'])-timedelta(days=2)).isoformat()
        other['closed_at'] = (datetime.fromisoformat(result['closed_at'])-timedelta(days=2)).isoformat()
        row = coordinator.db.execute('SELECT * FROM order_intents').fetchone()
        coordinator.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
            ('duplicate-exit', 'duplicate-exit', intent['symbol'], 'CLOSED', row['payload'],
             json.dumps(other), None, row['created'], row['updated']))
        coordinator.record_gateway_observation(intent['intent_id'], intent['intent_id'], result, None)
        original = coordinator.db.execute('SELECT * FROM order_intents WHERE id=?', (intent['intent_id'],)).fetchone()
        self.assertEqual(original['state'], 'NEEDS_REVIEW')
        self.assertIsNone(original['result'])

    def test_owner_appearing_before_commit_cannot_claim_same_close(self):
        coordinator, exchange, intent = self.coordinator_fixture()
        result = self.reconcile(intent, exchange)
        connection = coordinator.db
        original = connection.execute('SELECT * FROM order_intents').fetchone()
        class ConcurrentOwner:
            def execute(self, sql, params=()):
                if sql == 'BEGIN IMMEDIATE':
                    connection.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
                        ('racing-owner', 'racing-owner', intent['symbol'], 'NEEDS_REVIEW',
                         original['payload'], None, 'ORDER_OUTCOME_UNKNOWN', original['created'], original['updated']))
                return connection.execute(sql, params)
            def __getattr__(self, key):
                return getattr(connection, key)
        coordinator.db = ConcurrentOwner()
        with self.assertRaises(ValueError):
            coordinator.record_gateway_observation(intent['intent_id'], intent['intent_id'], result, None)
        row = connection.execute('SELECT * FROM order_intents WHERE id=?', (intent['intent_id'],)).fetchone()
        self.assertEqual(row['state'], 'NEEDS_REVIEW')
        self.assertIsNone(row['result'])
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 0)

    def test_coordinator_rejects_subdecimal_quantity_mismatch_exactly(self):
        coordinator, exchange, intent = self.coordinator_fixture()
        result = self.reconcile(intent, exchange)
        result['closure_receipt']['exit_trades'][0]['qty'] = '671.00000000000000000000000000000000001'
        coordinator.record_gateway_observation(intent['intent_id'], intent['intent_id'], result, None)
        row = coordinator.db.execute('SELECT * FROM order_intents').fetchone()
        self.assertEqual(row['state'], 'NEEDS_REVIEW')
        self.assertIsNone(row['result'])

    def test_replayed_economics_cannot_overwrite_original_closure(self):
        coordinator, exchange, intent = self.coordinator_fixture()
        result = self.reconcile(intent, exchange)
        coordinator.record_gateway_observation(intent['intent_id'], intent['intent_id'], result, None)
        changed = copy.deepcopy(result)
        changed['closure_receipt']['exit_trades'][0]['commission'] = '999'
        coordinator.record_gateway_observation(intent['intent_id'], intent['intent_id'], changed, None)
        row = coordinator.db.execute('SELECT * FROM order_intents').fetchone()
        self.assertEqual(row['state'], 'NEEDS_REVIEW')
        self.assertEqual(json.loads(row['result'])['closure_receipt'], result['closure_receipt'])
        self.assertEqual(coordinator.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 1)

    def test_coordinator_persists_actual_external_receipts_with_idempotent_replay_and_robot_off(self):
        coordinator, exchange, intent = self.coordinator_fixture()
        result = self.reconcile(intent, exchange)
        for _ in range(2):
            report = coordinator.record_gateway_observation(intent['intent_id'], intent['intent_id'], result, None)
            self.assertEqual(report['bot_status'], 'OFF')
            row = coordinator.db.execute('SELECT * FROM order_intents').fetchone()
            recorded = json.loads(row['result'])
            self.assertEqual(recorded.get('closure_source'), 'EXTERNAL_REDUCE_ONLY')
            self.assertEqual(recorded['closure_receipt'], result['closure_receipt'])
            self.assertEqual(json.loads(row['payload']), intent)
        self.assertEqual(coordinator.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 1)
        self.assertFalse(coordinator.store.settings()['robot_on'])
        self.assertIsNone(coordinator.db.execute("SELECT name FROM sqlite_master WHERE name='execution_cost_observations'").fetchone())


if __name__ == '__main__':
    unittest.main()
