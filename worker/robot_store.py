"""Persistent research/order journal and read-only Binance office snapshots."""
import json
from datetime import datetime, timezone
from .core import D, Review, day, now
from .account_state import (empty_account, account_observation, newer_account,
                            newer_account_json, account_order, account_timestamp, slots)
from .migration import retire_previous_runtime
from .order_gateway import OrderGateway
from .robot_provenance import verify

ACTIVE_ORDERS = ('READY_FOR_EXECUTION', 'EXECUTION_BLOCKED', 'SUBMITTING',
                 'ENTRY_PENDING', 'POSITION_PROTECTED', 'NEEDS_REVIEW')


def public(value):
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items() if not key.startswith('_')}
    if isinstance(value, list): return [public(item) for item in value]
    return value


def unavailable_office():
    return dict(source='BINANCE_FUTURES', generated_at=None,
        account=dict(status='UNAVAILABLE', checked_at=None, usdt_wallet_balance=None,
                     usdt_available_balance=None, positions=[], active_positions=None),
        reports=dict(status='UNAVAILABLE', checked_at=None, pnl_today_usdt=None,
                     trades_today=None, complete=False),
        position_history=dict(status='UNAVAILABLE', checked_at=None, items=[],
                              complete=False, kind='BINANCE_FILLS'))


def newer_section(incoming, previous):
    checked = account_timestamp(incoming.get('checked_at'))
    prior = account_timestamp(previous.get('checked_at'))
    return checked is not None and (prior is None or checked >= prior)


class RobotStore:
    def __init__(self, db, initialize=True):
        self.db = db
        db.create_function('robot_account_newer', 2, newer_account_json)
        if not initialize: return
        retire_previous_runtime(db)
        db.executescript('''
        CREATE TABLE IF NOT EXISTS robot_settings(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL,risk TEXT NOT NULL);
        INSERT OR IGNORE INTO robot_settings VALUES(1,0,'5');
        CREATE TABLE IF NOT EXISTS robot_cycles(id TEXT PRIMARY KEY,day TEXT NOT NULL,entry_epoch INTEGER NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL,UNIQUE(day,entry_epoch));
        CREATE TABLE IF NOT EXISTS robot_jobs(operation TEXT PRIMARY KEY,cycle TEXT NOT NULL,kind TEXT NOT NULL,symbol TEXT,state TEXT NOT NULL,risk_target TEXT);
        CREATE TABLE IF NOT EXISTS robot_candidates(id TEXT PRIMARY KEY,cycle TEXT NOT NULL,symbol TEXT NOT NULL,status TEXT NOT NULL,plan TEXT,failure_code TEXT,UNIQUE(cycle,symbol));
        CREATE TABLE IF NOT EXISTS order_intents(id TEXT PRIMARY KEY,candidate_id TEXT UNIQUE NOT NULL,symbol TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,result TEXT,failure_code TEXT,created TEXT NOT NULL,updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS robot_entry_receipts(id TEXT PRIMARY KEY,symbol TEXT NOT NULL,entry_day TEXT NOT NULL,confirmed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS robot_status(id INTEGER PRIMARY KEY CHECK(id=1),data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS office_cache(id INTEGER PRIMARY KEY CHECK(id=1),data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS office_activity(seq INTEGER PRIMARY KEY,at TEXT NOT NULL,state TEXT NOT NULL,agent TEXT NOT NULL,message TEXT NOT NULL);
        ''')

    def settings(self):
        row = self.db.execute('SELECT enabled FROM robot_settings WHERE id=1').fetchone()
        return dict(robot_on=bool(row['enabled']), risk_target_usdt='5')

    def configure(self, value):
        if not isinstance(value, dict) or set(value) != {'robot_on'} or type(value['robot_on']) is not bool:
            raise Review('INVALID_ROBOT_SETTING')
        self.db.execute('UPDATE robot_settings SET enabled=? WHERE id=1', (int(value['robot_on']),))
        self.event('ON' if value['robot_on'] else 'OFF', 'Coordinator', 'Pilihan robot disimpan.')
        return self.snapshot()

    def entries(self, today):
        return self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts WHERE entry_day=?', (today,)).fetchone()[0]

    def results(self, cycle):
        return list(self.db.execute('SELECT * FROM robot_candidates WHERE cycle=?', (cycle,)))

    def verified_plan(self, row):
        try:
            plan = json.loads(row['plan'])
            if row['symbol'] != plan['symbol'] or row['cycle'] != row['id'].rsplit(':analysis-v8:', 1)[0]: raise ValueError
            verify(self.db, plan, row['id'])
            return plan
        except Exception: raise Review('ORDER_EVIDENCE_UNVERIFIED') from None

    def event(self, state, agent, message):
        self.db.execute('INSERT INTO office_activity(at,state,agent,message) VALUES(?,?,?,?)',
                        (now(), state, agent, message))

    def snapshot(self):
        row = self.db.execute('SELECT data FROM robot_status WHERE id=1').fetchone()
        value = empty_account()
        value.update(json.loads(row[0]) if row else dict(bot_status='OFF', checked_at=None, failure_code=None, wait_reason='ROBOT_OFF'))
        value.update(self.settings(), bot_entries_today=self.entries(day()), execution_gateway=OrderGateway().status())
        if not value['robot_on']: value.update(bot_status='OFF', wait_reason='ROBOT_OFF')
        row = self.db.execute('SELECT symbol,status,failure_code FROM robot_candidates ORDER BY rowid DESC LIMIT 1').fetchone()
        value['last_decision'] = dict(row) if row else None
        return public(value)

    def report(self, state, account=None, reason=None, wait_reason=None):
        observation = account_observation(account) if account else empty_account()
        encoded = json.dumps(observation)
        previous = self.db.execute('SELECT data FROM robot_status WHERE id=1').fetchone()
        previous = json.loads(previous[0]) if previous else {}
        self.db.execute('''INSERT OR REPLACE INTO robot_status
          SELECT 1,json_set(CASE WHEN ? AND robot_account_newer(?,previous)
            THEN json_patch(previous,?) ELSE previous END,
            '$.bot_status',?,'$.failure_code',?,'$.wait_reason',?,'$.checked_at',?)
          FROM (SELECT COALESCE((SELECT data FROM robot_status WHERE id=1),?) AS previous)''',
          (bool(account), encoded, encoded, state, reason, wait_reason, now(), json.dumps(empty_account())))
        if (previous.get('bot_status'), previous.get('failure_code'), previous.get('wait_reason')) != (state, reason, wait_reason):
            agent = {'SCREENING': 'Market Analyst', 'ANALYZING': 'Neurobro',
                     'VALIDATING': 'Risk Manager', 'EXECUTING': 'Trading Agent',
                     'EXECUTION_BLOCKED': 'Trading Agent'}.get(state, 'Coordinator')
            self.event(state, agent, state + (': ' + reason if reason else ''))
        return self.snapshot()

    def report_account(self, account=None, reason=None):
        observation = account_observation(account, reason, stamp=True)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT data FROM robot_status WHERE id=1').fetchone()
            value = json.loads(row[0]) if row else dict(bot_status='OFF', failure_code=None, wait_reason='ROBOT_OFF', checked_at=None)
            if newer_account(observation, value): value.update(observation)
            self.db.execute('INSERT OR REPLACE INTO robot_status VALUES(1,?)', (json.dumps(value),))
            self.db.execute('COMMIT')
        except BaseException: self.db.execute('ROLLBACK'); raise

    def report_office(self, observation):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT data FROM office_cache WHERE id=1').fetchone()
            value = json.loads(row[0]) if row else unavailable_office()
            incoming = dict(observation)
            # History collection can finish after a later account observation.
            if 'account' in incoming and not newer_account(incoming['account'], value['account']):
                incoming.pop('account')
            for section in ('reports', 'position_history'):
                if section in incoming and not newer_section(incoming[section], value[section]):
                    incoming.pop(section)
            generated = account_timestamp(incoming.get('generated_at'))
            previous_generated = account_timestamp(value.get('generated_at'))
            if previous_generated and (generated is None or generated < previous_generated):
                incoming.pop('generated_at', None)
            value.update(incoming)
            self.db.execute('INSERT OR REPLACE INTO office_cache VALUES(1,?)', (json.dumps(value),))
            self.db.execute('COMMIT')
        except BaseException: self.db.execute('ROLLBACK'); raise
        account = value['account']
        if account.get('status') != 'CONNECTED': return
        running = [row['symbol'] for row in account['positions']]
        summary = dict(running_positions=account['active_positions'], running_symbols=running,
            manual_exposure=sorted(set(running)), available_slots=slots(account['active_positions'], self.entries(day())),
            usdt_wallet_balance=account['usdt_wallet_balance'], usdt_available_balance=account['usdt_available_balance'],
            account_checked_at=account['checked_at'], **{key: account[key] for key in ('_account_generation', '_account_revision') if key in account})
        self.report_account(summary)

    def report_office_failure(self, reason):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT data FROM office_cache WHERE id=1').fetchone()
            value = json.loads(row[0]) if row else unavailable_office()
            value['account'] = unavailable_office()['account']
            failed_at = now()
            completion = account_order()
            value['account'].update(checked_at=failed_at, failure_code=reason, **completion)
            value['reports'] = unavailable_office()['reports']
            # An unavailable refresh must not present cached account fills as
            # the current account observation.
            value['position_history'] = unavailable_office()['position_history']
            for section in ('reports', 'position_history'):
                value[section].update(checked_at=failed_at, failure_code=reason)
            value.update(generated_at=failed_at)
            self.db.execute('INSERT OR REPLACE INTO office_cache VALUES(1,?)', (json.dumps(value),))
            self.db.execute('COMMIT')
        except BaseException: self.db.execute('ROLLBACK'); raise
        self.report_account(dict(account_checked_at=failed_at, **completion), reason=reason)

    def office_snapshot(self):
        row = self.db.execute('SELECT data FROM office_cache WHERE id=1').fetchone()
        value = json.loads(row[0]) if row else unavailable_office()
        robot = self.snapshot()
        value.update(schema_version=2, source='BINANCE_FUTURES', robot=robot)
        value['activity'] = [dict(row) for row in self.db.execute('SELECT at,state,agent,message FROM office_activity ORDER BY seq DESC LIMIT 100')][::-1]
        state = robot['bot_status']
        tasks = {'market': state == 'SCREENING', 'neuro': state == 'ANALYZING',
                 'risk': state == 'VALIDATING', 'trading': state == 'EXECUTING'}
        try:
            checked = datetime.fromisoformat(value['account']['checked_at'])
            fresh = 0 <= (datetime.now(timezone.utc) - checked).total_seconds() <= 120
        except (ValueError, TypeError): fresh = False
        tasks['position'] = fresh and value['account'].get('status') == 'CONNECTED' and value['account'].get('active_positions', 0) > 0
        names = [('market', 'Market Analyst'), ('neuro', 'Neurobro'), ('risk', 'Risk Manager'),
                 ('trading', 'Trading Agent'), ('position', 'Position Monitor'),
                 ('reviewer', 'Trade Reviewer'), ('report', 'Report Manager'), ('boss', 'BOSS (Kamu)')]
        value['employees'] = [dict(id=key, name=name, status=('BLOCKED' if key == 'trading' and state == 'EXECUTION_BLOCKED' else
            'WORKING' if tasks.get(key) or key == 'boss' and any(tasks.values()) else 'IDLE')) for key, name in names]
        return public(value)
