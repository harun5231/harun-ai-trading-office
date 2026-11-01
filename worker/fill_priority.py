"""Persistent fill wakeups for the existing gateway's reconciliation callback.

This module never constructs an order or calls a Binance transport. A user-data
receiver admits owned entry fills here; the existing lifecycle reconciler is the
only supplied callback. Its frozen intent is preserved across retries/restarts.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import fcntl
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid


class FillEventError(ValueError):
    pass


_locks_guard = threading.Lock()
_locks = {}
_held = threading.local()


@contextmanager
def owned_intent_lock(db, client_order_id):
    """Serialize only one owned intent, never the analysis/research cycle.

    File locks cover separate database connections/processes. The local RLock
    also handles nested use by one callback and in-memory test databases.
    """
    path = db.execute('PRAGMA database_list').fetchone()[2]
    _need(type(client_order_id) is str and client_order_id, 'FILL_LOCK_ID_INVALID')
    key = (path or str(id(db)), client_order_id)
    with _locks_guard:
        local = _locks.setdefault(key, threading.RLock())
    with local:
        held = getattr(_held, 'keys', set())
        if not path or key in held:
            yield
            return
        directory = Path(path).parent / 'intent-locks'
        directory.mkdir(mode=0o700, exist_ok=True)
        meta = directory.lstat()
        _need(stat.S_ISDIR(meta.st_mode) and meta.st_uid == os.geteuid()
            and stat.S_IMODE(meta.st_mode) == 0o700, 'FILL_LOCK_DIRECTORY_INVALID')
        parent = os.open(directory, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
        fd = None
        try:
            name = hashlib.sha256(client_order_id.encode()).hexdigest() + '.lock'
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            meta = os.fstat(fd)
            _need(stat.S_ISREG(meta.st_mode) and meta.st_uid == os.geteuid()
                and meta.st_nlink == 1 and stat.S_IMODE(meta.st_mode) == 0o600, 'FILL_LOCK_FILE_INVALID')
            fcntl.flock(fd, fcntl.LOCK_EX)
            held.add(key)
            _held.keys = held
            try:
                yield
            finally:
                held.remove(key)
        finally:
            if fd is not None:
                os.close(fd)
            os.close(parent)


def _need(value, reason):
    if not value:
        raise FillEventError(reason)


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _quantity(value):
    _need(type(value) is str and len(value) <= 64 and re.fullmatch(r'\d+(?:\.\d+)?', value), 'FILL_QUANTITY_INVALID')
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise FillEventError('FILL_QUANTITY_INVALID') from None
    _need(result.is_finite() and result >= 0, 'FILL_QUANTITY_INVALID')
    return result


def _utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def protection_state(result):
    """A sent request or one confirmed exit is never a protected position."""
    filled = _quantity(result.get('filled_quantity'))
    if not filled:
        return 'ENTRY_PENDING' if result.get('state') == 'ENTRY_PENDING' else result.get('state')
    if result.get('state') == 'CLOSED':
        return 'CLOSED'
    ids = [result.get(name) for name in ('order_id', 'sl_order_id', 'tp_order_id')]
    both = (result.get('state') in ('POSITION_PROTECTED', 'POSITION_OPEN_PROTECTED')
        and result.get('sl_confirmed') is True and result.get('tp_confirmed') is True
        and all(type(value) is str and re.fullmatch(r'[1-9]\d{0,29}', value) for value in ids)
        and len(set(ids)) == 3)
    return 'POSITION_OPEN_PROTECTED' if both else 'PROTECTION_INCOMPLETE'


class FillPriority:
    """One durable, coalesced reconciliation request per accepted entry intent.

    The connection belongs to the caller's priority actor. Concurrent user-data
    and recovery actors can use separate connections to the same WAL database.
    Network callbacks always run outside the short SQLite claim transaction.
    """
    def __init__(self, db, *, wall_clock=time.time):
        self.db, self.clock = db, wall_clock
        db.execute('CREATE TABLE IF NOT EXISTS fill_priority('
            'client_id TEXT PRIMARY KEY,intent_id TEXT NOT NULL,order_id TEXT NOT NULL,'
            'symbol TEXT NOT NULL,payload TEXT NOT NULL,payload_sha256 TEXT NOT NULL,'
            'cumulative_quantity TEXT NOT NULL,exchange_fill_ms INTEGER,'
            'detected_at REAL NOT NULL,state TEXT NOT NULL,claim TEXT,'
            'last_result TEXT,last_error TEXT,reconciled_at REAL)')
        db.execute('CREATE TABLE IF NOT EXISTS fill_priority_observations('
            'claim TEXT PRIMARY KEY,client_id TEXT NOT NULL,exchange_fill_ms INTEGER,'
            'detected_at REAL NOT NULL,started_at REAL NOT NULL,completed_at REAL NOT NULL,'
            'state TEXT NOT NULL,result TEXT)')

    def admit(self, event, owned_intent):
        """Admit only a real entry TRADE event matching an existing frozen intent."""
        _need(type(event) is dict and event.get('e') == 'ORDER_TRADE_UPDATE', 'FILL_EVENT_INVALID')
        order = event.get('o')
        _need(type(order) is dict and order.get('x') == 'TRADE'
            and order.get('X') in ('PARTIALLY_FILLED', 'FILLED')
            and order.get('o') == 'LIMIT' and order.get('R') is False,
            'FILL_EVENT_NOT_ENTRY')
        _need(type(owned_intent) is dict, 'FILL_INTENT_NOT_OWNED')
        frozen = copy.deepcopy(owned_intent)
        client, symbol = frozen.get('client_order_id'), frozen.get('symbol')
        _need(order.get('c') == client and order.get('s') == symbol
            and order.get('S') == frozen.get('entry', {}).get('side')
            and order.get('ps') == frozen.get('position_side') == 'BOTH', 'FILL_INTENT_MISMATCH')
        quantity = _quantity(order.get('z'))
        _need(0 < quantity <= _quantity(frozen['entry']['quantity']), 'FILL_QUANTITY_EXCEEDS_INTENT')
        oid, at = order.get('i'), order.get('T', event.get('T'))
        _need(type(oid) in (int, str) and re.fullmatch(r'[1-9]\d{0,29}', str(oid)), 'FILL_ORDER_ID_INVALID')
        _need(type(at) is int and at > 0, 'FILL_EXCHANGE_TIME_INVALID')
        return self.enqueue(frozen, str(oid), str(quantity), exchange_fill_ms=at)

    def enqueue(self, frozen_intent, order_id, cumulative_quantity='0', *, exchange_fill_ms=None,
            recovery=False):
        """Queue stream or API-recovery evidence without changing order parameters."""
        payload = _wire(frozen_intent)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        client, operation, symbol = (frozen_intent[key] for key in
            ('client_order_id', 'intent_id', 'symbol'))
        quantity = _quantity(cumulative_quantity)
        _need(quantity <= _quantity(frozen_intent['entry']['quantity']), 'FILL_QUANTITY_EXCEEDS_INTENT')
        _need(type(order_id) is str and re.fullmatch(r'[1-9]\d{0,29}', order_id), 'FILL_ORDER_ID_INVALID')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT payload_sha256,order_id,cumulative_quantity,state,detected_at,exchange_fill_ms '
                'FROM fill_priority WHERE client_id=?', (client,)).fetchone()
            if old:
                _need(old[0] == digest and old[1] == order_id, 'FILL_FROZEN_INTENT_CHANGED')
                if quantity <= _quantity(old[2]) and not recovery:
                    self.db.execute('COMMIT')
                    return False
                advanced = quantity > _quantity(old[2])
                quantity = max(quantity, _quantity(old[2]))
                self.db.execute("UPDATE fill_priority SET cumulative_quantity=?,exchange_fill_ms=?,"
                    "detected_at=?,state=CASE WHEN state='RUNNING' THEN state ELSE 'PENDING' END "
                    'WHERE client_id=?', (str(quantity), exchange_fill_ms if exchange_fill_ms is not None else old[5],
                        self.clock() if advanced else old[4], client))
            else:
                self.db.execute('INSERT INTO fill_priority VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (client, operation, order_id, symbol, payload, digest, str(quantity),
                        exchange_fill_ms, self.clock(), 'PENDING', None, None, None, None))
            self.db.execute('COMMIT')
            return True
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def recover_interrupted(self):
        # The callback is reconciliation, never another entry submit. A crashed
        # ambiguous protection request must query its deterministic ID first.
        self.db.execute("UPDATE fill_priority SET state='PENDING',claim=NULL,"
            "last_error='RESTART_RECONCILE_REQUIRED' WHERE state='RUNNING'")

    def run_one(self, reconcile_owned, persist_observation):
        """Execute the existing lifecycle callback immediately after a wakeup."""
        claim = uuid.uuid4().hex
        self.db.execute('BEGIN IMMEDIATE')
        try:
            cursor = self.db.execute("SELECT * FROM fill_priority WHERE state='PENDING' "
                'ORDER BY CASE WHEN CAST(cumulative_quantity AS REAL)>0 THEN 0 ELSE 1 END,detected_at,client_id LIMIT 1')
            values = cursor.fetchone()
            if values is None:
                self.db.execute('COMMIT')
                return None
            row = dict(zip((item[0] for item in cursor.description), values))
            self.db.execute("UPDATE fill_priority SET state='RUNNING',claim=? WHERE client_id=?",
                (claim, row['client_id']))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        started = self.clock()
        try:
            intent = json.loads(row['payload'])
            _need(hashlib.sha256(_wire(intent).encode()).hexdigest() == row['payload_sha256'],
                'FILL_FROZEN_INTENT_CHANGED')
            with owned_intent_lock(self.db, row['client_id']):
                result = reconcile_owned(copy.deepcopy(intent))
                _need(type(result) is dict and result.get('source') == 'BINANCE_FUTURES'
                    and result.get('client_order_id') == row['client_id']
                    and result.get('symbol') == row['symbol'] and result.get('order_id') == row['order_id'],
                    'FILL_RECONCILE_IDENTITY_INVALID')
                _need(result.get('state') in ('ENTRY_PENDING', 'POSITION_PROTECTED',
                    'POSITION_OPEN_PROTECTED', 'PROTECTION_INCOMPLETE', 'CLOSED', 'REJECTED'),
                    'FILL_RECONCILE_STATE_INVALID')
                filled = _quantity(result.get('filled_quantity'))
                _need(filled <= _quantity(intent['entry']['quantity']), 'FILL_QUANTITY_EXCEEDS_INTENT')
                _need(filled >= _quantity(row['cumulative_quantity']), 'FILL_RECONCILE_BEHIND_EVENT')
                state = protection_state(result)
                persisted = persist_observation(row['intent_id'], copy.deepcopy(result))
                _need(persisted is not False and not (isinstance(persisted, dict)
                    and persisted.get('bot_status') == 'NEEDS_REVIEW'), 'FILL_OBSERVATION_NOT_PERSISTED')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                latest = self.db.execute('SELECT cumulative_quantity FROM fill_priority WHERE client_id=?',
                    (row['client_id'],)).fetchone()
                pending = state == 'PROTECTION_INCOMPLETE' or filled < _quantity(latest[0])
                self.db.execute('UPDATE fill_priority SET state=?,claim=NULL,last_result=?,last_error=?,reconciled_at=? '
                    'WHERE client_id=? AND claim=?', ('PENDING' if pending else 'COMPLETE', _wire(result),
                        'PROTECTION_INCOMPLETE' if pending else None, self.clock(), row['client_id'], claim))
                self.db.execute('INSERT INTO fill_priority_observations VALUES(?,?,?,?,?,?,?,?)',
                    (claim, row['client_id'], row['exchange_fill_ms'], row['detected_at'], started,
                        self.clock(), state, _wire(result)))
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            return {'client_order_id': row['client_id'], 'state': state,
                'exchange_fill_at': _utc(row['exchange_fill_ms'] / 1000) if row['exchange_fill_ms'] else None,
                'detected_at': _utc(row['detected_at']), 'reconcile_started_at': _utc(started),
                'reconcile_completed_at': _utc(self.clock()), 'retry_required': pending}
        except Exception as error:
            code = str(error)
            if not re.fullmatch('[A-Z][A-Z0-9_]{0,119}', code):
                code = type(error).__name__
            self.db.execute("UPDATE fill_priority SET state='PENDING',claim=NULL,last_error=?,reconciled_at=? "
                'WHERE client_id=? AND claim=?', (code, self.clock(), row['client_id'], claim))
            self.db.execute('INSERT INTO fill_priority_observations VALUES(?,?,?,?,?,?,?,?)',
                (claim, row['client_id'], row['exchange_fill_ms'], row['detected_at'], started,
                    self.clock(), 'RECONCILIATION_REQUIRED', _wire({'failure_code': code})))
            return {'client_order_id': row['client_id'], 'state': 'RECONCILIATION_REQUIRED',
                'failure_code': code, 'retry_required': True}
