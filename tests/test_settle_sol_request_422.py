"""Offline case-bound HTTP-422 retirement; no provider or Binance transport."""
import copy
import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / 'deploy' / 'settle_sol_request_422.py'
spec = importlib.util.spec_from_file_location('settle_sol_request_422', SOURCE)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class SettleSolRequest422Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trading = self.root / 'trading'
        self.trading.mkdir(mode=0o700)
        self.path = self.trading / 'ledger.sqlite3'
        self.lock = self.trading / 'cycle.lock'
        self.lock.touch(mode=0o600)
        self.uid = patch.object(tool, 'EXPECTED_UID', os.geteuid())
        self.uid.start()
        self.addCleanup(self.uid.stop)
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.path.chmod(0o600)
        self.db.executescript('''
        CREATE TABLE api_requests(operation TEXT PRIMARY KEY,idempotency TEXT,body_hash TEXT,
          state TEXT,created REAL,attempts INTEGER,output TEXT,failure_code TEXT);
        CREATE TABLE robot_jobs(operation TEXT PRIMARY KEY,cycle TEXT,kind TEXT,symbol TEXT,
          state TEXT,risk_target TEXT);
        CREATE TABLE robot_cycles(id TEXT PRIMARY KEY,day TEXT,entry_epoch INTEGER,state TEXT,data TEXT);
        CREATE TABLE robot_candidates(id TEXT PRIMARY KEY,cycle TEXT,symbol TEXT,status TEXT,
          plan TEXT,failure_code TEXT);
        CREATE TABLE order_intents(id TEXT PRIMARY KEY,candidate_id TEXT,symbol TEXT,state TEXT,
          payload TEXT,result TEXT,failure_code TEXT,created TEXT,updated TEXT);
        CREATE TABLE robot_entry_receipts(id TEXT PRIMARY KEY,symbol TEXT,entry_day TEXT,confirmed_at TEXT);
        CREATE TABLE trades(symbol TEXT,position TEXT,quantity TEXT);
        CREATE TABLE robot_settings(id INTEGER PRIMARY KEY,enabled INTEGER,risk TEXT);
        INSERT INTO trades VALUES('HYPEUSDT','LONG','4.16');
        INSERT INTO robot_settings VALUES(1,1,'5');
        ''')
        created = datetime(2026, 10, 7, 10, 30, tzinfo=timezone.utc).timestamp()
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
                        (tool.OPERATION, None, 'd'*64, 'NEEDS_REVIEW', created, 1, None, 'HTTP_422'))
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
                        (tool.OPERATION, tool.CYCLE, 'ANALYSIS', 'SOLUSDT', 'NEEDS_REVIEW', '5'))
        self.data = dict(target=1, screen=2, replacements=2, queue=[],
                         round_symbols=['SOLUSDT'], seen=['BTCUSDT', 'BNBUSDT', 'SOLUSDT'],
                         repair_origin={'kind': 'old-proof', 'preserved': True})
        self.raw_data = json.dumps(self.data, indent=2)
        self.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',
                        (tool.CYCLE, tool.CASE_DAY, 1, 'NEEDS_REVIEW', self.raw_data))
        self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
                        (tool.OPERATION, tool.CYCLE, 'SOLUSDT', 'REJECTED', None, 'HTTP_422'))
        self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
                        (tool.CYCLE+':analysis-v9:BNBUSDT', tool.CYCLE, 'BNBUSDT',
                         'REJECTED', None, 'NET_RISK_REWARD_NOT_TARGET_2'))
        payload = dict(client_order_id=tool.ETH_CLIENT, symbol='ETHUSDT', side='LONG',
                       entry=dict(side='BUY', price='2610.0', quantity='0.181'),
                       protection=dict(exit_side='SELL', stop_loss='2585.0',
                                       take_profit='2730.0', working_type='MARK_PRICE'))
        result = dict(source='BINANCE_FUTURES', state='POSITION_PROTECTED', symbol='ETHUSDT',
                      client_order_id=tool.ETH_CLIENT, order_id='8389766291635748850',
                      filled_quantity='0.181', sl_order_id='4000001952621457',
                      tp_order_id='4000001952686853', sl_confirmed=True, tp_confirmed=True,
                      first_fill_at='2026-10-07T08:18:23.715000+00:00',
                      observed_at='2026-10-07T09:40:08.802484+00:00')
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
                        ('eth', 'eth', 'ETHUSDT', 'POSITION_PROTECTED', json.dumps(payload),
                         json.dumps(result), None, '2026-10-07T07:00:00+00:00', 'now'))
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
                        ('btc', 'btc', 'BTCUSDT', 'REJECTED',
                         json.dumps(dict(client_order_id=tool.BTC_CLIENT)), None,
                         'NO_ACCEPTED_ORDER_OBSERVED', '2026-10-07T05:40:14+00:00', 'now'))
        self.db.execute('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)',
                        (tool.ETH_CLIENT, 'ETHUSDT', tool.CASE_DAY, result['first_fill_at']))
        self.db.commit()

    def settle(self, **kwargs):
        return tool.settle(self.root, **kwargs)

    def snapshot(self, db=None):
        db = db or self.db
        names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: list(db.execute('SELECT * FROM "'+name+'"')) for name in names}

    def mutate(self, statement, args=()):
        self.db.execute(statement, args)
        self.db.commit()

    def test_inspection_is_read_only_and_creates_no_backup(self):
        before = self.snapshot()
        result = self.settle()
        self.assertEqual(result['status'], 'INSPECTION_ONLY')
        self.assertTrue(result['can_apply'])
        self.assertEqual(result['billing_outcome'], 'UNKNOWN')
        self.assertEqual(result['replacements'], 2)
        self.assertEqual(result['maximum_replacements'], 3)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.trading / 'maintenance').exists())

    def test_apply_changes_only_three_states_and_preserves_every_other_value(self):
        before = self.snapshot()
        result = self.settle(apply=True)
        self.assertEqual(result['status'], 'SOL_HTTP_422_SETTLED')
        expected = copy.deepcopy(before)
        request = list(expected['api_requests'][0])
        request[3] = tool.REQUEST_TERMINAL
        expected['api_requests'][0] = tuple(request)
        job = list(expected['robot_jobs'][0])
        job[4] = tool.JOB_TERMINAL
        expected['robot_jobs'][0] = tuple(job)
        cycle = list(expected['robot_cycles'][0])
        cycle[3] = 'ACTIVE'
        expected['robot_cycles'][0] = tuple(cycle)
        self.assertEqual(self.snapshot(), expected)
        self.assertEqual(self.db.execute('SELECT data FROM robot_cycles').fetchone()[0], self.raw_data)
        self.assertEqual(self.db.execute('SELECT attempts,body_hash,output,failure_code,idempotency '
                                        'FROM api_requests').fetchone(), (1, 'd'*64, None, 'HTTP_422', None))
        self.assertEqual(self.db.execute('SELECT * FROM trades').fetchone(), ('HYPEUSDT', 'LONG', '4.16'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone(), (1,))
        backup = Path(result['backup'])
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        with sqlite3.connect(backup) as saved:
            self.assertEqual(saved.execute('PRAGMA quick_check').fetchone(), ('ok',))
            self.assertEqual(self.snapshot(saved), before)

    def test_immediate_repeat_is_verified_noop_with_no_extra_backup_or_request(self):
        self.settle(apply=True)
        before = self.snapshot()
        backups = list((self.trading / 'maintenance').iterdir())
        result = self.settle(apply=True)
        self.assertEqual(result['status'], 'ALREADY_SETTLED')
        self.assertFalse(result['can_apply'])
        self.assertNotIn('backup', result)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list((self.trading / 'maintenance').iterdir()), backups)

    def closed_eth(self):
        result = dict(source='BINANCE_FUTURES', state='CLOSED', symbol='ETHUSDT',
                      client_order_id=tool.ETH_CLIENT, order_id='8389766291635748850',
                      filled_quantity='0.181', sl_confirmed=False, tp_confirmed=False,
                      first_fill_at='2026-10-07T08:18:23.715000+00:00',
                      exit_order_id='8389766291712624741',
                      closed_at='2026-10-07T10:07:39.052000+00:00',
                      observed_at='2026-10-07T10:45:10.744673+00:00')
        self.mutate("UPDATE order_intents SET state='CLOSED',result=? WHERE id='eth'", (json.dumps(result),))
        self.mutate('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
                    ('eth', '2026-10-07:robot-v9:0', 'ETHUSDT', 'CLOSED', '{}', None))
        return result

    def test_known_recorded_eth_closure_preserves_closed_finance_and_one_entry_receipt(self):
        self.closed_eth()
        finance = tool.financial_digest(self.db)
        receipt = self.db.execute('SELECT * FROM robot_entry_receipts').fetchall()
        result = self.settle(apply=True)
        self.assertEqual(result['status'], 'SOL_HTTP_422_SETTLED')
        self.assertEqual(tool.financial_digest(self.db), finance)
        self.assertEqual(self.db.execute('SELECT * FROM robot_entry_receipts').fetchall(), receipt)
        self.assertEqual(self.db.execute("SELECT state FROM order_intents WHERE id='eth'").fetchone(), ('CLOSED',))
        self.assertEqual(self.db.execute('SELECT data FROM robot_cycles').fetchone()[0], self.raw_data)

    def test_ambiguous_or_different_closure_cannot_settle_research(self):
        result = self.closed_eth()
        for key, value in (('exit_order_id', '999'), ('closed_at', '2026-10-07T10:07:40+00:00'),
                           ('first_fill_at', '2026-10-07T08:18:24+00:00'), ('sl_confirmed', 0),
                           ('tp_confirmed', True), ('state', 'POSITION_PROTECTED')):
            with self.subTest(key=key):
                self.mutate("UPDATE order_intents SET result=? WHERE id='eth'", (json.dumps({**result, key: value}),))
                before = self.snapshot()
                with self.assertRaises(tool.Refuse):self.settle(apply=True)
                self.assertEqual(self.snapshot(), before)
        self.mutate("UPDATE order_intents SET result=? WHERE id='eth'", (json.dumps(result),))
        self.mutate("UPDATE robot_candidates SET status='NEEDS_REVIEW' WHERE id='eth'")
        with self.assertRaisesRegex(tool.Refuse, '^ETH_CLOSURE_CHANGED$'):self.settle(apply=True)

    def test_request_evidence_tampering_or_unknown_outcome_refuses_without_changes(self):
        cases = (
            ('attempts', 2), ('attempts', 0), ('body_hash', 'not-a-hash'),
            ('failure_code', 'NETWORK_UNCERTAIN'), ('failure_code', 'HTTP_503'),
            ('output', '{}'), ('idempotency', 'unexpected'), ('created', 1),
            ('state', 'PENDING'), ('state', 'COMPLETE'),
        )
        for column, value in cases:
            with self.subTest(column=column, value=value):
                old = self.db.execute('SELECT '+column+' FROM api_requests').fetchone()[0]
                self.mutate('UPDATE api_requests SET '+column+'=?', (value,))
                before = self.snapshot()
                with self.assertRaises(tool.Refuse):
                    self.settle(apply=True)
                self.assertEqual(self.snapshot(), before)
                self.mutate('UPDATE api_requests SET '+column+'=?', (old,))

    def test_old_operation_or_other_unresolved_request_is_not_retired(self):
        self.mutate('UPDATE api_requests SET operation=?', ('2026-10-06:robot-v9:1:analysis-v9:SOLUSDT',))
        before = self.snapshot()
        with self.assertRaisesRegex(tool.Refuse, '^CASE_ROW_MISSING$'):
            self.settle(apply=True)
        self.assertEqual(self.snapshot(), before)
        self.mutate('UPDATE api_requests SET operation=?', (tool.OPERATION,))
        self.mutate('INSERT INTO api_requests SELECT ?,idempotency,body_hash,state,created,attempts,output,'
                    'failure_code FROM api_requests', ('other-current-operation',))
        with self.assertRaisesRegex(tool.Refuse, '^OTHER_REQUEST_UNRESOLVED$'):
            self.settle(apply=True)

    def test_cycle_counters_queue_seen_and_partial_terminal_pairs_refuse(self):
        changes = (dict(replacements=0), dict(replacements=3), dict(screen=3), dict(target=2),
                   dict(queue=['SOLUSDT']), dict(round_symbols=['BNBUSDT']),
                   dict(seen=['BTCUSDT', 'SOLUSDT', 'BNBUSDT']))
        for values in changes:
            with self.subTest(values=values):
                data = {**self.data, **values}
                self.mutate('UPDATE robot_cycles SET data=?', (json.dumps(data),))
                before = self.snapshot()
                with self.assertRaisesRegex(tool.Refuse, '^CYCLE_COUNTER_CHANGED$'):
                    self.settle(apply=True)
                self.assertEqual(self.snapshot(), before)
        self.mutate('UPDATE robot_cycles SET data=?', (self.raw_data,))
        self.mutate('UPDATE api_requests SET state=?', (tool.REQUEST_TERMINAL,))
        with self.assertRaisesRegex(tool.Refuse, '^CASE_STATE_CHANGED$'):
            self.settle(apply=True)

    def test_candidate_plan_or_order_prevents_retirement(self):
        self.mutate('UPDATE robot_candidates SET plan=? WHERE id=?', ('{}', tool.OPERATION))
        with self.assertRaisesRegex(tool.Refuse, '^CANDIDATE_CHANGED$'):
            self.settle(apply=True)
        self.mutate('UPDATE robot_candidates SET plan=NULL WHERE id=?', (tool.OPERATION,))
        self.mutate('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
                    ('sol', tool.OPERATION, 'SOLUSDT', 'REJECTED', '{}', None, 'rejected', 'created', 'updated'))
        before = self.snapshot()
        with self.assertRaisesRegex(tool.Refuse, '^SOL_ORDER_PRESENT$'):
            self.settle(apply=True)
        self.assertEqual(self.snapshot(), before)

    def test_missing_eth_protection_receipt_or_foreign_order_uncertainty_refuses(self):
        original = self.db.execute("SELECT result FROM order_intents WHERE id='eth'").fetchone()[0]
        changed = json.loads(original)
        changed['tp_confirmed'] = False
        self.mutate("UPDATE order_intents SET result=? WHERE id='eth'", (json.dumps(changed),))
        with self.assertRaisesRegex(tool.Refuse, '^ETH_PROTECTION_CHANGED$'):
            self.settle(apply=True)
        self.mutate("UPDATE order_intents SET result=? WHERE id='eth'", (original,))
        self.mutate('UPDATE robot_entry_receipts SET entry_day=?', ('2026-10-06',))
        with self.assertRaisesRegex(tool.Refuse, '^ETH_RECEIPT_CHANGED$'):
            self.settle(apply=True)
        self.mutate('UPDATE robot_entry_receipts SET entry_day=?', (tool.CASE_DAY,))
        self.mutate("UPDATE order_intents SET state='NEEDS_REVIEW' WHERE id='btc'")
        with self.assertRaisesRegex(tool.Refuse, '^UNRESOLVED_ORDER$'):
            self.settle(apply=True)

    def test_trigger_symlink_hardlink_or_public_ledger_refuses(self):
        self.mutate('CREATE TRIGGER unwanted AFTER UPDATE ON api_requests '
                    'BEGIN UPDATE order_intents SET state=\'CLOSED\'; END')
        with self.assertRaisesRegex(tool.Refuse, '^JOURNAL_TRIGGER_PRESENT$'):
            self.settle(apply=True)
        self.mutate('DROP TRIGGER unwanted')
        self.path.chmod(0o644)
        with self.assertRaisesRegex(tool.Refuse, '^JOURNAL_NOT_PRIVATE$'):
            self.settle(apply=True)
        self.path.chmod(0o600)
        other = self.trading / 'extra-link'
        os.link(self.path, other)
        with self.assertRaisesRegex(tool.Refuse, '^JOURNAL_NOT_PRIVATE$'):
            self.settle(apply=True)
        other.unlink()
        self.lock.unlink()
        self.lock.symlink_to(self.path)
        with self.assertRaisesRegex(tool.Refuse, '^LOCK_NOT_PRIVATE$'):
            self.settle(apply=True)

    def test_failed_post_update_verification_rolls_back_all_three_states(self):
        before = self.snapshot()
        original = tool.case_rows
        calls = 0
        def refused_after_updates(db):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise tool.Refuse('INJECTED_VERIFICATION_FAILURE')
            return original(db)
        with patch.object(tool, 'case_rows', side_effect=refused_after_updates):
            with self.assertRaisesRegex(tool.Refuse, '^INJECTED_VERIFICATION_FAILURE$'):
                self.settle(apply=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(list((self.trading / 'maintenance').iterdir())), 1)

    def test_compare_and_swap_rejects_changes_after_backup(self):
        original = tool.backup_database
        def changed_after_backup(db, trading):
            backup = original(db, trading)
            self.mutate('UPDATE api_requests SET attempts=2')
            return backup
        with patch.object(tool, 'backup_database', side_effect=changed_after_backup):
            with self.assertRaisesRegex(tool.Refuse, '^REQUEST_EVIDENCE_CHANGED$'):
                self.settle(apply=True)
        self.assertEqual(self.db.execute('SELECT state,attempts FROM api_requests').fetchone(), ('NEEDS_REVIEW', 2))
        self.assertEqual(self.db.execute('SELECT state FROM robot_jobs').fetchone(), ('NEEDS_REVIEW',))
        self.assertEqual(self.db.execute('SELECT state FROM robot_cycles').fetchone(), ('NEEDS_REVIEW',))

    def test_optional_source_map_checks_content_before_any_write(self):
        code_root = self.root / 'app'
        worker = code_root / 'worker'
        worker.mkdir(parents=True)
        source = worker / 'robot.py'
        source.write_text('case source')
        expected = {'worker/robot.py': hashlib.sha256(source.read_bytes()).hexdigest()}
        result = self.settle(source_hashes=json.dumps(expected), source_root=code_root)
        self.assertEqual(result['status'], 'INSPECTION_ONLY')
        before = self.snapshot()
        source.write_text('changed')
        with self.assertRaisesRegex(tool.Refuse, '^SOURCE_CHANGED$'):
            self.settle(apply=True, source_hashes=expected, source_root=code_root)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.trading / 'maintenance').exists())


if __name__ == '__main__':
    unittest.main()
