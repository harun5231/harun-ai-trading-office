"""GET-fenced closure maintenance uses the real journal/provenance recorder."""
import copy
import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import tempfile
import time
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from support import Account
from worker.account_state import account_state
from worker.core import D, FEE_SOURCE, Ledger, Review, Rules, Signal, day, risk_check
from worker.neuroapi import NeuroAPI
from worker.order_gateway import build_intent
from worker.robot import Coordinator
from worker.robot_provenance import stamp
from worker.robot_store import RobotStore

SOURCE = Path(__file__).resolve().parents[1]/'deploy'/'record_eth_closed.py'
spec = importlib.util.spec_from_file_location('record_eth_closed', SOURCE)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class FakeGateway:
    def __init__(self, observation):
        self.observation = copy.deepcopy(observation)
        self.calls = []
        self.attempt_write = None
        self.on_get = lambda: None

    def _wire(self, method, path, query='', signed=False):
        self.calls.append((method, path))
        return {}

    def reconcile(self, intent):
        self._wire('GET', '/fapi/v1/order')
        self.on_get()
        if self.attempt_write is not None:
            self._wire(self.attempt_write, '/fapi/v1/algoOrder')
        return copy.deepcopy(self.observation)


class RecordEthClosedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trading = self.root/'trading'
        self.trading.mkdir(mode=0o700)
        self.path = self.trading/'ledger.sqlite3'
        self.lock = self.trading/'cycle.lock'
        self.lock.touch(mode=0o600)
        self.ledger = Ledger(self.path)
        self.addCleanup(self.ledger.db.close)
        self.path.chmod(0o600)
        self.db = self.ledger.db
        self.store = RobotStore(self.db)
        NeuroAPI(self.ledger, key='fixture-only-no-network')
        now = time.time()
        rules = Rules(D('.001'), D('.001'), D('1000'), D('.01'), D('5'), now,
                      taker_fee_rate=D('.0005'), fee_observed_at=now,
                      fee_symbol='ETHUSDT', fee_source=FEE_SOURCE)
        signal = Signal('ETHUSDT', 'LONG', D('2610'), D('2730'), D('2585'))
        plan = risk_check(signal, rules)
        self.assertEqual(plan['quantity'], '0.181')
        plan['sizing_rules'] = {key: str(value) if isinstance(value, D) else value
                                for key, value in asdict(rules).items()}
        output = json.dumps(dict(symbol='ETHUSDT', side='LONG', limit_entry=2610,
                                 take_profit=2730, stop_loss=2585, risk_reward=4.8))
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
                        (tool.OPERATION, None, 'a'*64, 'COMPLETE', now, 1, output, None))
        stamp(self.db, plan, tool.OPERATION)
        self.intent = build_intent(plan, tool.OPERATION)
        self.assertEqual(self.intent['client_order_id'], tool.CLIENT)
        self.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',
                        (tool.CASE_DAY+':robot-v9:0', tool.CASE_DAY, 0, 'COMPLETE',
                         '{"target":2,"screen":0,"replacements":0}'))
        self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
                        (tool.OPERATION, tool.CASE_DAY+':robot-v9:0', 'ETHUSDT',
                         'NEEDS_REVIEW', json.dumps(plan), tool.FAILURE))
        self.protected = dict(source='BINANCE_FUTURES', state='POSITION_PROTECTED',
                              symbol='ETHUSDT', client_order_id=tool.CLIENT, order_id=tool.ENTRY_ID,
                              filled_quantity='0.181', first_fill_at=tool.FIRST_FILL,
                              sl_order_id=tool.SL_ID, tp_order_id=tool.TP_ID,
                              sl_confirmed=True, tp_confirmed=True,
                              observed_at='2026-10-07T09:40:08.802484+00:00')
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
                        (tool.OPERATION, tool.OPERATION, 'ETHUSDT', 'NEEDS_REVIEW',
                         json.dumps(self.intent), json.dumps(self.protected), tool.FAILURE,
                         '2026-10-07T06:58:00.000000+00:00', 'before'))
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
                        ('old-btc', 'old-btc', 'BTCUSDT', 'REJECTED', '{}', None,
                         'NO_ACCEPTED_ORDER_OBSERVED', 'before', 'before'))
        self.db.execute('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)',
                        (tool.CLIENT, 'ETHUSDT', tool.CASE_DAY, tool.FIRST_FILL))
        self.db.execute("UPDATE robot_settings SET enabled=1 WHERE id=1")
        sol = tool.CASE_DAY+':robot-v9:1:analysis-v9:SOLUSDT'
        cycle = tool.CASE_DAY+':robot-v9:1'
        self.sol = sol
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
                        (sol, None, 'd'*64, 'NEEDS_REVIEW', now, 1, None, 'HTTP_422'))
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
                        (sol, cycle, 'ANALYSIS', 'SOLUSDT', 'NEEDS_REVIEW', '5'))
        self.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',
                        (cycle, tool.CASE_DAY, 1, 'NEEDS_REVIEW',
                         json.dumps(dict(target=1, screen=2, replacements=2, queue=[],
                                         round_symbols=['SOLUSDT'], seen=['BTCUSDT', 'BNBUSDT', 'SOLUSDT']), indent=2)))
        self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
                        (sol, cycle, 'SOLUSDT', 'REJECTED', None, 'HTTP_422'))
        self.db.execute('CREATE TABLE trades(symbol TEXT,quantity TEXT,state TEXT)')
        self.db.execute("INSERT INTO trades VALUES('HYPEUSDT','4.16','MANUAL')")
        observation = dict(source='BINANCE_FUTURES', state='CLOSED', symbol='ETHUSDT',
                           client_order_id=tool.CLIENT, order_id=tool.ENTRY_ID,
                           filled_quantity='0.181', first_fill_at=tool.FIRST_FILL,
                           exit_order_id=tool.EXIT_ID, closed_at=tool.CLOSED_AT,
                           sl_confirmed=False, tp_confirmed=False,
                           observed_at=datetime.now(timezone.utc).isoformat())
        self.gateway = FakeGateway(observation)
        self.account = Account()
        self.account.position('HYPEUSDT', '4.16')
        self.original_source_check = tool.check_sources
        patches = [
            patch.object(tool, 'EXPECTED_UID', os.geteuid()),
            patch.object(tool, 'check_sources', return_value=None),
            patch.object(tool, 'production', return_value=(Coordinator, RobotStore,
                lambda: self.gateway, lambda: self.account, account_state, day)),
            patch('urllib.request.build_opener', side_effect=AssertionError('network forbidden')),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def record(self, **kwargs):
        return tool.record(self.root, **kwargs)

    def snapshot(self, db=None):
        db = db or self.db
        names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: [tuple(row) for row in db.execute('SELECT * FROM "'+name+'"')]
                for name in names if name not in tool.REPORT_TABLES}

    def test_inspection_proves_closed_with_gets_without_journal_or_backup(self):
        before = self.snapshot()
        result = self.record()
        self.assertEqual(result['status'], 'INSPECTION_ONLY')
        self.assertTrue(result['can_apply'])
        self.assertEqual(result['entries_case_day'], 1)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.trading/'maintenance').exists())
        self.assertEqual(self.gateway.calls, [('GET', '/fapi/v1/order')])
        self.assertTrue(all(method == 'GET' for method, _ in self.account.calls))

    def test_apply_real_recorder_preserves_payload_receipt_sol_counters_settings_and_hype(self):
        before = self.snapshot()
        original = dict(self.db.execute('SELECT * FROM order_intents WHERE id=?', (tool.OPERATION,)).fetchone())
        result = self.record(apply=True)
        self.assertEqual(result['status'], 'ETH_CLOSED_JOURNAL_VERIFIED')
        self.assertEqual(result['entries_case_day'], 1)
        row = self.db.execute('SELECT * FROM order_intents WHERE id=?', (tool.OPERATION,)).fetchone()
        self.assertEqual((row['state'], row['failure_code']), ('CLOSED', None))
        self.assertEqual(row['payload'], original['payload'])
        self.assertEqual(json.loads(row['result']), self.gateway.observation)
        candidate = self.db.execute('SELECT status,failure_code FROM robot_candidates WHERE id=?',
                                    (tool.OPERATION,)).fetchone()
        self.assertEqual(tuple(candidate), ('CLOSED', None))
        after = self.snapshot()
        for name in set(before)-{'order_intents', 'robot_candidates'}:
            self.assertEqual(after[name], before[name], name)
        self.assertEqual([row for row in after['order_intents'] if row[0] != tool.OPERATION],
                         [row for row in before['order_intents'] if row[0] != tool.OPERATION])
        self.assertEqual([row for row in after['robot_candidates'] if row[0] != tool.OPERATION],
                         [row for row in before['robot_candidates'] if row[0] != tool.OPERATION])
        backup = Path(result['backup'])
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        with sqlite3.connect(backup) as saved:
            self.assertEqual(saved.execute('PRAGMA quick_check').fetchone(), ('ok',))
            self.assertEqual(self.snapshot(saved), before)

    def test_repeat_with_fresh_exact_closed_proof_is_a_verified_noop(self):
        self.record(apply=True)
        before = self.snapshot()
        folders = list((self.trading/'maintenance').iterdir())
        result = self.record(apply=True)
        self.assertEqual(result['status'], 'ETH_CLOSED_ALREADY_VERIFIED')
        self.assertNotIn('backup', result)
        self.assertFalse(result['can_apply'])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list((self.trading/'maintenance').iterdir()), folders)
        self.assertEqual(len(self.gateway.calls), 2)

    def test_post_or_delete_from_reconcile_is_blocked_before_any_write(self):
        for method in ('POST', 'DELETE'):
            with self.subTest(method=method):
                self.gateway.attempt_write = method
                before = self.snapshot()
                with self.assertRaisesRegex(tool.Refuse, '^BINANCE_WRITE_BLOCKED$'):
                    self.record(apply=True)
                self.assertEqual(self.snapshot(), before)
                self.assertTrue(all(verb == 'GET' for verb, _ in self.gateway.calls))

    def test_wrong_closure_quantity_identity_dates_or_stale_proof_never_records(self):
        original = copy.deepcopy(self.gateway.observation)
        mutations = (
            ('state', 'POSITION_PROTECTED'), ('symbol', 'BTCUSDT'), ('client_order_id', 'other'),
            ('order_id', '999'), ('exit_order_id', '999'), ('closed_at', tool.FIRST_FILL),
            ('first_fill_at', tool.CLOSED_AT), ('filled_quantity', '0.180'), ('filled_quantity', .181),
            ('sl_confirmed', True), ('observed_at', (datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()),
        )
        for key, value in mutations:
            with self.subTest(key=key):
                self.gateway.observation = {**original, key: value}
                before = self.snapshot()
                with self.assertRaises(tool.Refuse):
                    self.record(apply=True)
                self.assertEqual(self.snapshot(), before)
        self.gateway.observation = original

    def test_wrong_failure_state_or_receipt_is_refused_before_network(self):
        original = self.db.execute('SELECT failure_code FROM order_intents WHERE id=?', (tool.OPERATION,)).fetchone()[0]
        self.db.execute('UPDATE order_intents SET failure_code=? WHERE id=?',
                        ('ORDER_OUTCOME_UNKNOWN', tool.OPERATION))
        with self.assertRaisesRegex(tool.Refuse, '^ETH_STATE_CHANGED$'):
            self.record(apply=True)
        self.assertEqual(self.gateway.calls, [])
        self.db.execute('UPDATE order_intents SET failure_code=? WHERE id=?', (original, tool.OPERATION))
        self.db.execute('UPDATE robot_entry_receipts SET confirmed_at=?', (tool.CLOSED_AT,))
        with self.assertRaisesRegex(tool.Refuse, '^RECEIPT_CHANGED$'):
            self.record(apply=True)
        self.assertEqual(self.gateway.calls, [])

    def test_provenance_intent_tamper_is_refused_before_network(self):
        altered = copy.deepcopy(self.intent)
        altered['protection']['take_profit'] = '2800'
        self.db.execute('UPDATE order_intents SET payload=? WHERE id=?', (json.dumps(altered), tool.OPERATION))
        before = self.snapshot()
        with self.assertRaisesRegex(tool.Refuse, '^INTENT_CHANGED$'):
            self.record(apply=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.gateway.calls, [])

    def test_new_unresolved_order_or_live_eth_position_refuses(self):
        self.db.execute("UPDATE order_intents SET state='NEEDS_REVIEW' WHERE id='old-btc'")
        with self.assertRaisesRegex(tool.Refuse, '^OTHER_UNRESOLVED_ORDER$'):
            self.record(apply=True)
        self.db.execute("UPDATE order_intents SET state='REJECTED' WHERE id='old-btc'")
        self.account.position('ETHUSDT', '0.181')
        before = self.snapshot()
        with self.assertRaisesRegex(tool.Refuse, '^ETH_POSITION_PRESENT$'):
            self.record(apply=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.gateway.calls, [])

    def test_case_or_research_change_during_get_is_cas_refused_without_record(self):
        self.gateway.on_get = lambda: self.db.execute('UPDATE api_requests SET attempts=2 WHERE operation=?', (self.sol,))
        with self.assertRaisesRegex(tool.Refuse, '^JOURNAL_CHANGED$'):
            self.record(apply=True)
        row = self.db.execute('SELECT state,failure_code FROM order_intents WHERE id=?', (tool.OPERATION,)).fetchone()
        self.assertEqual(tuple(row), ('NEEDS_REVIEW', tool.FAILURE))
        self.assertEqual(self.db.execute('SELECT attempts FROM api_requests WHERE operation=?',
                                        (self.sol,)).fetchone()[0], 2)

    def test_byte_identical_journal_inode_swap_during_get_is_refused(self):
        before = self.snapshot()
        def replace_journal():
            clone = self.trading/'ledger-clone.sqlite3'
            with sqlite3.connect(clone) as saved:
                self.db.backup(saved)
                self.assertEqual(self.snapshot(saved), before)
            clone.chmod(0o600)
            os.replace(clone, self.path)
        self.gateway.on_get = replace_journal
        with self.assertRaisesRegex(tool.Refuse, '^JOURNAL_CHANGED$'):
            self.record(apply=True)
        self.assertEqual(self.snapshot(), before)
        with sqlite3.connect(self.path) as current:
            self.assertEqual(self.snapshot(current), before)

    def test_cycle_lock_inode_swap_during_get_is_refused(self):
        before = self.snapshot()
        def replace_lock():
            clone = self.trading/'cycle-lock-clone'
            clone.touch(mode=0o600)
            os.replace(clone, self.lock)
        self.gateway.on_get = replace_lock
        with self.assertRaisesRegex(tool.Refuse, '^LOCK_CHANGED$'):
            self.record(apply=True)
        self.assertEqual(self.snapshot(), before)

    def test_reporting_error_after_verified_atomic_commit_is_distinguished(self):
        original = Coordinator.record_gateway_observation
        def committed_then_report_failed(c, *args):
            original(c, *args)
            raise RuntimeError('private provider text must not be output')
        with patch.object(Coordinator, 'record_gateway_observation', committed_then_report_failed):
            result = self.record(apply=True)
        self.assertEqual(result['status'], 'ETH_CLOSED_JOURNAL_VERIFIED')
        self.assertEqual(result['reporting_error_after_verified_commit'], 'RuntimeError')
        self.assertEqual(self.store.entries(tool.CASE_DAY), 1)

    def test_failed_recorder_or_invalid_observation_is_not_silently_accepted(self):
        with patch.object(Coordinator, 'record_gateway_observation', side_effect=RuntimeError('private text')):
            with self.assertRaisesRegex(tool.Refuse, '^CLOSURE_RECORD_NOT_VERIFIED$'):
                self.record(apply=True)
        self.assertEqual(self.db.execute('SELECT state FROM order_intents WHERE id=?',
                                        (tool.OPERATION,)).fetchone()[0], 'NEEDS_REVIEW')

    def test_filesystem_privacy_and_trigger_guards_refuse_before_network(self):
        self.db.execute('CREATE TRIGGER unintended AFTER UPDATE ON order_intents '
                        "BEGIN UPDATE trades SET state='CHANGED'; END")
        with self.assertRaisesRegex(tool.Refuse, '^JOURNAL_TRIGGER_PRESENT$'):
            self.record(apply=True)
        self.db.execute('DROP TRIGGER unintended')
        self.path.chmod(0o644)
        with self.assertRaisesRegex(tool.Refuse, '^JOURNAL_NOT_PRIVATE$'):
            self.record(apply=True)
        self.path.chmod(0o600)
        extra = self.trading/'ledger-hardlink'
        os.link(self.path, extra)
        with self.assertRaisesRegex(tool.Refuse, '^JOURNAL_NOT_PRIVATE$'):
            self.record(apply=True)
        extra.unlink()
        self.lock.unlink()
        self.lock.symlink_to(self.path)
        with self.assertRaisesRegex(tool.Refuse, '^LOCK_NOT_PRIVATE$'):
            self.record(apply=True)
        self.assertEqual(self.gateway.calls, [])

    def test_mandatory_source_pins_and_optional_full_inventory_are_checked(self):
        app = self.root/'app'
        (app/'worker').mkdir(parents=True)
        (app/'deploy').mkdir()
        sources = {
            'worker/order_gateway.py': 'frozen private SDK',
            'worker/robot.py': 'frozen coordinator',
            'worker/other.py': 'unchanged module',
            'deploy/container_boot.py': 'unchanged boot',
            'deploy/runtime_permissions.py': 'unchanged permissions',
        }
        for name, text in sources.items():
            (app/name).write_text(text)
        expected = {name: hashlib.sha256((app/name).read_bytes()).hexdigest() for name in sources}
        pins = {name: expected[name] for name in ('worker/order_gateway.py', 'worker/robot.py')}
        with patch.object(tool, 'SOURCE_PINS', pins):
            self.original_source_check(app)
            self.original_source_check(app, json.dumps(expected))
            incomplete = {name: value for name, value in expected.items() if name != 'worker/other.py'}
            with self.assertRaisesRegex(tool.Refuse, '^SOURCE_INVENTORY_CHANGED$'):
                self.original_source_check(app, incomplete)
            conflicting = {**expected, 'worker/order_gateway.py': 'f'*64}
            with self.assertRaisesRegex(tool.Refuse, '^SOURCE_MAP_INVALID$'):
                self.original_source_check(app, conflicting)
            (app/'worker/order_gateway.py').write_text('modified private SDK')
            with self.assertRaisesRegex(tool.Refuse, '^SOURCE_CHANGED$'):
                self.original_source_check(app)


if __name__ == '__main__':
    unittest.main()
