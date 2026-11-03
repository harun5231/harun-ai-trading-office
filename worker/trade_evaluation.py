"""Observation-only post-trade journal. Never calls providers or order machinery.

The append-only SQLite file is separate from the financial/order ledger. A poll's
source snapshots and derived jobs commit together; replay makes no new events.
Historical chart/decision linkage and a Neurobro evaluation protocol are not
available here. Jobs explicitly remain blocked, not 'evaluated'.
"""
import hashlib
import json
import os
import sqlite3
import stat
from decimal import Decimal, InvalidOperation

FILENAME = 'trade_evaluation.sqlite3'


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _latest(db, kind):
    return {key: json.loads(payload) for key, payload in db.execute('''
        SELECT e.entity, e.payload FROM evaluation_events e
        WHERE e.kind=? AND e.seq=(SELECT max(x.seq) FROM evaluation_events x
        WHERE x.kind=e.kind AND x.entity=e.entity) ORDER BY e.entity''', (kind,))}


def _append(db, kind, key, value):
    payload = _json(value)
    old = db.execute('SELECT payload FROM evaluation_events WHERE kind=? AND entity=? ORDER BY seq DESC LIMIT 1',
                     (kind, key)).fetchone()
    if old and old[0] == payload:
        return
    db.execute('INSERT INTO evaluation_events(kind,entity,payload,sha256) VALUES(?,?,?,?)',
               (kind, key, payload, hashlib.sha256(payload.encode()).hexdigest()))


def _snapshot(db):
    trades = _latest(db, 'trade')
    scope = _latest(db, 'scope').get('calendar', {})
    return dict(status='OBSERVATION_ONLY', processed_trade_ids=sorted(trades),
                consecutive_losses=scope.get('consecutive_losses', 0),
                streak_complete=scope.get('streak_complete', False),
                streak_scope='OBSERVED_CLOSED_POSITIONS',
                history_missing_reasons=scope.get('history_missing_reasons', []),
                jobs=list(_latest(db, 'job').values()))


def _private_path(directory, write=False):
    # The actor supplies its existing private state directory, never a caller's
    # path. Refuse aliases so this journal cannot become the financial ledger.
    path = directory / FILENAME
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_RDWR | os.O_CREAT if write else os.O_RDONLY)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise ValueError('EVALUATION_FILE_INVALID')
        if write:
            os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return path


def read_status(directory):
    """Read only; status requests never initialize or mutate any database."""
    path = directory / FILENAME
    if not path.exists():
        return dict(status='NOT_OBSERVED', processed_trade_ids=[], consecutive_losses=0,
                    streak_complete=False, streak_scope='OBSERVED_CLOSED_POSITIONS',
                    history_missing_reasons=['HISTORY_NOT_OBSERVED'], jobs=[])
    db = sqlite3.connect(_private_path(directory).as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        return _snapshot(db)
    finally:
        db.close()


def observe_calendar(directory, calendar):
    """Ingest existing reconstructed *net* PNL, preserving the exact source row."""
    db = sqlite3.connect(_private_path(directory, write=True), timeout=5, isolation_level=None)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS evaluation_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                entity TEXT NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE INDEX IF NOT EXISTS evaluation_entity ON evaluation_events(kind,entity,seq);
            CREATE TRIGGER IF NOT EXISTS evaluation_no_update BEFORE UPDATE ON evaluation_events
                BEGIN SELECT RAISE(ABORT,'APPEND_ONLY_EVALUATION'); END;
            CREATE TRIGGER IF NOT EXISTS evaluation_no_delete BEFORE DELETE ON evaluation_events
                BEGIN SELECT RAISE(ABORT,'APPEND_ONLY_EVALUATION'); END;
        ''')
        db.execute('BEGIN IMMEDIATE')
        for row in calendar.get('positions', []):
            if row.get('status') == 'CLOSED':
                _append(db, 'trade', row['id'], row)
        trades = _latest(db, 'trade')
        losses = 0
        loss_ids = []
        active_streaks = set()
        for row in sorted(trades.values(), key=lambda row: (row['closed_at'], row['id'])):
            pnl = None
            if row.get('complete') is True and row.get('pnl_usdt') is not None:
                try:
                    raw = row['pnl_usdt']
                    if not isinstance(raw, str) or len(raw) > 64:
                        raise ValueError
                    pnl = Decimal(raw)
                    if not pnl.is_finite():
                        raise ValueError
                except (ValueError, InvalidOperation):
                    raise ValueError('CLOSED_NET_PNL_INVALID') from None
            losses = losses + 1 if pnl is not None and pnl < 0 else 0
            missing = ['HISTORICAL_CHART_ARCHIVE_NOT_LINKED', 'ORIGINAL_NEUROBRO_DECISION_NOT_LINKED',
                       'REQUIRES_NEUROBRO_EVALUATION']
            if pnl is None:
                missing += ['CLOSED_NET_PNL_INCOMPLETE'] + row.get('incomplete_reasons', [])
            job = dict(id='post-trade:' + row['id'], trade_ids=[row['id']],
                       status='BLOCKED_MISSING_EVIDENCE', evidence_complete=False,
                       missing_reasons=missing,
                       evidence=dict(closed_position=row, closed_position_sha256=hashlib.sha256(_json(row).encode()).hexdigest()),
                       findings=dict(net_pnl_usdt=row['pnl_usdt'] if pnl is not None else None,
                                     outcome='UNKNOWN' if pnl is None else 'LOSS' if pnl < 0 else 'WIN' if pnl > 0 else 'BREAKEVEN'),
                       experiment_outcome=dict(status='NOT_RUN', reason='REQUIRES_NEUROBRO_EVALUATION'))
            _append(db, 'job', job['id'], job)
            loss_ids = loss_ids + [row['id']] if losses else []
            # Three is an observation trigger, never a trading/risk policy.
            # Each qualifying closing trade owns exactly one stable job ID.
            if losses >= 3:
                streak = dict(job, id='losing-streak:' + row['id'], trade_ids=list(loss_ids),
                              evidence=dict(closed_positions=[trades[key] for key in loss_ids]),
                              findings=dict(consecutive_losses=losses))
                _append(db, 'job', streak['id'], streak)
                active_streaks.add(streak['id'])
        for key, job in _latest(db, 'job').items():
            if key.startswith('losing-streak:') and key not in active_streaks:
                _append(db, 'job', key, dict(job, status='SUPERSEDED_HISTORY_REVISION'))
        history_missing = list(calendar.get('incomplete_reasons', []))
        if any(row.get('complete') is not True or row.get('pnl_usdt') is None for row in trades.values()):
            history_missing.append('CLOSED_NET_PNL_INCOMPLETE')
        _append(db, 'scope', 'calendar', dict(consecutive_losses=losses,
                streak_complete=calendar.get('complete') is True and not history_missing,
                history_missing_reasons=history_missing))
        result = _snapshot(db)
        db.execute('COMMIT')
        return result
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()
