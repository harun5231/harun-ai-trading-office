"""Day exclusions through exact coordinator methods; all replies are local."""
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.native_pending_review import screening_day_exclusions
from worker import robot as public
from worker.neuroapi import SETUP_SCHEMA, canonical_output
from worker.new_setup_policy import stale_unsubmitted

ROOT = Path(__file__).resolve().parents[1]
TODAY = '2026-11-10'
YESTERDAY = '2026-11-09'
TOMORROW = '2026-11-11'
NOW = datetime(2026, 11, 10, 8, tzinfo=timezone.utc).timestamp()


def coordinator_class(native):
    if not native:
        return public.Coordinator
    tree = ast.parse((ROOT / 'vps_native/worker/robot.py').read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Coordinator')
    names = {'screen', 'replaceable_result', 'replacement_count', '_tick'}
    methods = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = dict(vars(public), stale_unsubmitted=stale_unsubmitted)
    exec(compile(ast.Module(body=methods, type_ignores=[]), '<native-day-policy>', 'exec'), namespace)
    return type('NativeDayPolicyCoordinator', (public.Coordinator,), {name: namespace[name] for name in names})


class ScreeningDayPolicyTests(unittest.TestCase):
    def fixture(self, native):
        db = sqlite3.connect(':memory:', isolation_level=None)
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript('''
            CREATE TABLE robot_cycles(id TEXT PRIMARY KEY,day TEXT,entry_epoch INTEGER,state TEXT,data TEXT);
            CREATE TABLE robot_jobs(operation TEXT PRIMARY KEY,cycle TEXT,kind TEXT,symbol TEXT,state TEXT,risk_target TEXT);
            CREATE TABLE api_requests(operation TEXT PRIMARY KEY,idempotency TEXT,body_hash TEXT,state TEXT,created REAL,attempts INTEGER,output TEXT,failure_code TEXT);
            CREATE TABLE robot_candidates(id TEXT PRIMARY KEY,cycle TEXT,symbol TEXT,status TEXT,plan TEXT,failure_code TEXT);
            CREATE TABLE order_intents(id TEXT PRIMARY KEY,candidate_id TEXT,symbol TEXT,state TEXT,payload TEXT,result TEXT,failure_code TEXT,created TEXT,updated TEXT);
            CREATE TABLE robot_entry_receipts(id TEXT PRIMARY KEY,symbol TEXT,entry_day TEXT,confirmed_at TEXT);
        ''')
        robot = object.__new__(coordinator_class(native))
        robot.db = db
        robot.allowed = lambda today: True
        robot.market = SimpleNamespace(catalog=lambda: {symbol: {} for symbol in
            ('FILUSDT', 'ATOMUSDT', 'SUIUSDT', 'BTCUSDT')})
        robot.store = SimpleNamespace(report=lambda status, account=None, reason=None, **kw:
            dict(bot_status=status, failure_code=reason, **kw))
        replies, calls = [], []
        def ask(operation, prompt, schema, validate, context, **options):
            calls.append(dict(operation=operation, prompt=prompt, context=context))
            output = {'symbols': replies.pop(0)}
            validate(output)
            body_hash = hashlib.sha256((operation + prompt + json.dumps(context)).encode()).hexdigest()
            db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
                (operation, None, body_hash, 'COMPLETE', NOW, 1,
                    canonical_output(output, schema, robot.market.catalog()), None))
            return output
        robot.neuro = SimpleNamespace(ask=ask)
        return robot, replies, calls

    def cycle(self, robot, day, epoch=0, *, seen=()):
        cycle = day + ':robot-v9:' + str(epoch)
        data = dict(target=1, queue=[], seen=list(seen), screen=-1, replacements=0, round_symbols=[])
        robot.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',
            (cycle, day, epoch, 'ACTIVE', json.dumps(data)))
        return cycle, data

    def hold(self, robot, day, symbol, *, epoch=0, api_state='COMPLETE', status='HOLD'):
        cycle, _ = self.cycle(robot, day, epoch, seen=[symbol])
        operation = cycle + ':analysis-v9:' + symbol
        output = dict(symbol=symbol, side='HOLD', limit_entry=None, take_profit=None,
            stop_loss=None, risk_reward=None)
        robot.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
            (operation, cycle, symbol, status, None, None))
        robot.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
            (operation, cycle, 'ANALYSIS', symbol, 'COMPLETE', '5'))
        robot.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            (operation, None, 'a' * 64, api_state, NOW-100, 1,
                canonical_output(output, SETUP_SCHEMA), None))
        return operation

    def test_same_day_hold_across_cycles_is_in_prompt_rejected_locally_and_replaced(self):
        for native in (False, True):
            with self.subTest(native=native):
                robot, replies, calls = self.fixture(native)
                self.hold(robot, TODAY, 'FILUSDT')
                cycle, data = self.cycle(robot, TODAY, 1)
                account = {'running_symbols': [], 'open_entry_symbols': []}
                replies.extend([['FILUSDT'], ['ATOMUSDT']])
                self.assertEqual(robot.screen(cycle, data, 1, account, TODAY, True)['wait_reason'],
                    'SCREENING_NO_ELIGIBLE_SYMBOLS')
                self.assertIsNone(calls[0]['context'])
                self.assertIn('FILUSDT', calls[0]['prompt'])
                self.assertEqual(data['queue'], [])
                self.assertEqual(robot.replacement_count(data, [], cycle), 1)
                self.assertEqual(robot.screen(cycle, data, 1, account, TODAY, False)['wait_reason'],
                    'SCREENING_COMPLETE_ANALYSIS_PENDING')
                self.assertEqual(calls[1]['context']['excluded_symbols'], ['FILUSDT', 'HYPEUSDT'])
                self.assertEqual(data['queue'], ['ATOMUSDT'])
                self.assertEqual(robot.db.execute('SELECT count(*) FROM robot_candidates').fetchone()[0], 1)
                self.assertEqual(robot.db.execute('SELECT count(*) FROM order_intents').fetchone()[0], 0)

    def test_yesterday_hold_is_reeligible_but_active_prior_day_entry_stays_exposed(self):
        for native in (False, True):
            with self.subTest(native=native):
                robot, replies, calls = self.fixture(native)
                self.hold(robot, YESTERDAY, 'SUIUSDT')
                cycle, data = self.cycle(robot, TODAY)
                robot.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
                    ('old-order', 'old-order', 'BTCUSDT', 'ENTRY_PENDING', '{}', None, None, YESTERDAY, TODAY))
                account = {'running_symbols': [], 'open_entry_symbols': []}
                self.assertEqual(screening_day_exclusions(robot, TODAY), set())
                self.assertEqual(screening_day_exclusions(robot, YESTERDAY), {'SUIUSDT'})
                self.assertIn('BTCUSDT', robot.exposed_symbols(account))
                replies.append(['SUIUSDT'])
                self.assertEqual(robot.screen(cycle, data, 1, account, TODAY, True)['wait_reason'],
                    'SCREENING_COMPLETE_ANALYSIS_PENDING')
                self.assertEqual(data['queue'], ['SUIUSDT'])
                self.assertNotIn('Hindari coin', calls[0]['prompt'])
                self.assertEqual(screening_day_exclusions(robot, TOMORROW), set())

    def test_unknown_or_technical_rows_cannot_create_day_exclusion_or_replacement(self):
        for native in (False, True):
            with self.subTest(native=native):
                robot, replies, _ = self.fixture(native)
                self.hold(robot, TODAY, 'SUIUSDT', api_state='NEEDS_REVIEW')
                self.hold(robot, TODAY, 'FILUSDT', epoch=1, status='REJECTED')
                cycle, data = self.cycle(robot, TODAY, 2)
                self.assertEqual(screening_day_exclusions(robot, TODAY), set())
                self.assertEqual(robot.replacement_count(data, [], cycle), 0)
                replies.append(['ATOMUSDT'])
                robot.screen(cycle, data, 1, {'running_symbols': [], 'open_entry_symbols': []}, TODAY, True)
                self.assertEqual(robot.replacement_count(data, [], cycle), 0)

    def persisted_queue(self, native):
        robot, replies, calls = self.fixture(native)
        self.hold(robot, TODAY, 'FILUSDT')
        robot.db.execute("UPDATE robot_cycles SET state='COMPLETE'")
        cycle, data = self.cycle(robot, TODAY, 1)
        data.update(queue=['FILUSDT'], round_symbols=['FILUSDT'], screen=0)
        robot.save_cycle(cycle, data)
        operation = cycle + ':screening:0:1'
        robot.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
            (operation, cycle, 'SCREENING', None, 'COMPLETE', None))
        robot.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            (operation, None, 'b' * 64, 'COMPLETE', NOW-50, 1, json.dumps({'symbols': ['FILUSDT']}), None))
        robot.store.settings = lambda: {'robot_on': True}
        robot.store.entries = lambda today: 1
        robot.store.results = lambda value: list(robot.db.execute('SELECT * FROM robot_candidates WHERE cycle=?', (value,)))
        robot.store.report_account = lambda account: None
        robot.stopping = None
        robot.account_factory = lambda: None
        robot.advance_execution = lambda account, today: None
        robot.execution_slots = lambda account, today: 1
        robot.recover_invalid_screening = lambda today, account: None
        robot.neuro.require_key = lambda: None
        robot.analyze = lambda *args: self.fail('Day policy must stop new analysis before dispatch')
        return robot, replies, calls, cycle

    def run_tick(self, robot):
        account = {'running_symbols': [], 'open_entry_symbols': []}
        with patch.dict(robot._tick.__func__.__globals__, {'day': lambda: TODAY,
                'account_state': lambda *args: account}), patch('agent.native_pending_review.review_then_advance',
                    side_effect=lambda coordinator, observed, today: coordinator.advance_execution(observed, today)):
            return robot._tick()

    def test_persisted_prepolicy_queue_is_retired_locally_and_replaced_without_analysis_dispatch(self):
        for native in (False, True):
            with self.subTest(native=native):
                robot, replies, calls, cycle = self.persisted_queue(native)
                before_api = [tuple(row) for row in robot.db.execute('SELECT * FROM api_requests')]
                self.assertEqual(self.run_tick(robot)['failure_code'], 'ROBOT_DAY_EXCLUDED')
                data = json.loads(robot.db.execute('SELECT data FROM robot_cycles WHERE id=?', (cycle,)).fetchone()[0])
                results = robot.store.results(cycle)
                self.assertEqual(data['queue'], [])
                self.assertEqual(robot.research_budget(data, results), 1)
                self.assertEqual(robot.replacement_count(data, results, cycle), 1)
                self.assertEqual([tuple(row) for row in robot.db.execute('SELECT * FROM api_requests')], before_api)
                self.assertEqual(calls, [])
                replies.append(['ATOMUSDT'])
                self.assertEqual(self.run_tick(robot)['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
                self.assertEqual(len(calls), 1)
                self.assertIn('FILUSDT', calls[0]['context']['excluded_symbols'])
                self.assertEqual(robot.db.execute('SELECT count(*) FROM order_intents').fetchone()[0], 0)

    def test_existing_unknown_analysis_or_accepted_intent_is_not_retired_by_day_policy(self):
        for native in (False, True):
            for existing in ('unknown_analysis', 'accepted_intent'):
                with self.subTest(native=native, existing=existing):
                    robot, _, calls, cycle = self.persisted_queue(native)
                    operation = cycle + ':analysis-v9:FILUSDT'
                    if existing == 'unknown_analysis':
                        robot.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
                            (operation, cycle, 'ANALYSIS', 'FILUSDT', 'NEEDS_REVIEW', '5'))
                        robot.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
                            (operation, None, 'c' * 64, 'NEEDS_REVIEW', NOW-10, 1, None, 'NEUROBRO_BROWSER_OUTCOME_UNKNOWN'))
                    else:
                        robot.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
                            (operation, operation, 'FILUSDT', 'ENTRY_PENDING', '{}', None, None, TODAY, TODAY))
                        robot.advance_execution = lambda account, today: {'bot_status': 'ENTRY_PENDING'}
                    before = tuple(robot.db.iterdump())
                    result = self.run_tick(robot)
                    self.assertEqual(result['bot_status'], 'REJECTED' if existing == 'unknown_analysis' else 'ENTRY_PENDING')
                    self.assertEqual(tuple(robot.db.iterdump()), before)
                    self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
