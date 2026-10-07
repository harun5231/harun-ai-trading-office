#!/usr/bin/env python3
"""Inspect, or explicitly retire proven pre-v8 research claims without replay.

Use container Python -I -B -S as UID10001. No SDK/provider imports, credentials,
network calls, settings updates, order changes, or generic journal clearing.
"""
import argparse
from collections import Counter
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
import uuid

ARCHIVE = 'legacy_research_archive'
ARCHIVE_COLUMNS = ('operation', 'original_row', 'row_sha256', 'scope', 'retired_at')
LEGACY = re.compile(r'([0-9]{4}-[0-9]{2}-[0-9]{2}):('
    r'robot-v7:[0-2]:(?:screening:[0-3]:[12]|analysis-v7:[A-Z0-9]{2,18}USDT)'
    r'|manual-screening:v2|analysis-v[3-6]:[A-Z0-9]{2,18}USDT'
    r'|screening|replacement-screening:[1-3]|analysis:[A-Z0-9]{2,18}USDT)')
PUBLIC_FAILURES = frozenset(('NETWORK_UNCERTAIN', 'LOCAL_PROCESSING_FAILED',
    'INVALID_RESPONSE_ENVELOPE', 'INVALID_SETUP_SCHEMA', 'INVALID_OUTPUT_SCHEMA',
    'INVALID_SCREENING_SCHEMA', 'INVALID_SCREENING_SYMBOL', 'INVALID_SETUP_SYMBOL_SIDE', 'MARKET_DATA_UNAVAILABLE',
    'BINANCE_NOT_CONFIGURED', 'NEUROAPI_NOT_CONFIGURED', 'RATE_LIMITED',
    'RESEARCH_PAUSED', 'HTTP_401', 'HTTP_403', 'HTTP_429', 'HTTP_500', 'HTTP_503'))
MAX_ROWS = 10000
MAX_ROW_BYTES = 2 * 1024 * 1024


class Refuse(Exception):
    pass


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def checksum(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(columns, row):
    cells = []
    for value in row:
        if value is None: cell = ['null', None]
        elif type(value) is int: cell = ['integer', value]
        elif type(value) is float: cell = ['real', struct.pack('>d', value).hex()]
        elif type(value) is str: cell = ['text', value]
        elif type(value) is bytes: cell = ['blob', value.hex()]
        else: raise Refuse('UNSUPPORTED_SQL_VALUE')
        cells.append(cell)
    payload = json.dumps([list(columns), cells], ensure_ascii=True, separators=(',', ':')).encode('ascii')
    if len(payload) > MAX_ROW_BYTES: raise Refuse('REQUEST_ROW_TOO_LARGE')
    return payload


def classify(operation):
    if not isinstance(operation, str) or len(operation) > 120: return None
    match = LEGACY.fullmatch(operation)
    if not match: return None
    try: date.fromisoformat(match[1])
    except ValueError: return None
    tail = match[2]
    if tail.startswith('robot-v7:'):
        return ('robot_v7_screening' if ':screening:' in tail else 'robot_v7_analysis', match[1], operation.rsplit(':screening:', 1)[0] if ':screening:' in tail else operation.rsplit(':analysis-v7:', 1)[0])
    if tail.startswith('analysis-v'): return ('analysis_' + tail.split(':')[0].replace('-', '_'), match[1], match[1])
    if tail.startswith('analysis:'): return ('legacy_analysis', match[1], match[1])
    if tail.startswith('replacement-screening:'): return ('legacy_replacement_screening', match[1], match[1])
    if tail == 'manual-screening:v2': return ('legacy_manual_screening_v2', match[1], match[1])
    return ('legacy_screening', match[1], match[1])


def identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid,
            stat.S_IMODE(info.st_mode), info.st_nlink)


def directory(path):
    path = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        info = os.fstat(fd)
        if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
            raise Refuse('UNSAFE_STATE_DIRECTORY')
        return path, fd
    except BaseException:
        os.close(fd)
        raise


def inside_checkout(root):
    return any((ancestor / '.git').exists() for ancestor in (root, *root.parents))


def checked_file(parent, name, missing=False):
    try: fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        if missing: return None
        raise Refuse('STATE_FILE_MISSING') from None
    except OSError: raise Refuse('UNSAFE_STATE_FILE') from None
    try:
        value = os.fstat(fd)
        if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
                or value.st_uid not in (0, os.geteuid()) or value.st_mode & 0o7022):
            raise Refuse('UNSAFE_STATE_FILE')
        return fd
    except BaseException:
        os.close(fd)
        raise


def validate_files(parent, files, writing=False):
    for name, fd in files.items():
        try: current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if fd is None: continue
            raise Refuse('STATE_FILE_CHANGED') from None
        if fd is None and writing and name in ('ledger.sqlite3-wal', 'ledger.sqlite3-shm', 'ledger.sqlite3-journal'):
            checked = checked_file(parent, name)
            os.close(checked)
            continue
        if fd is None or identity(current) != identity(os.fstat(fd)):
            raise Refuse('STATE_FILE_CHANGED')


def validate_directories(root, root_fd, parent):
    for path, fd in ((root, root_fd), (root / 'trading', parent)):
        try: current = path.lstat()
        except OSError: raise Refuse('STATE_DIRECTORY_CHANGED') from None
        if not stat.S_ISDIR(current.st_mode) or identity(current) != identity(os.fstat(fd)):
            raise Refuse('STATE_DIRECTORY_CHANGED')


def connect(path, readonly=True, immutable=False):
    uri = path.as_uri() + ('?mode=ro' if readonly else '?mode=rw')
    if immutable: uri += '&immutable=1'
    db = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
    if readonly: db.execute('PRAGMA query_only=ON')
    return db


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def rowset(db):
    if 'api_requests' not in tables(db): raise Refuse('REQUEST_SCHEMA_UNAVAILABLE')
    info = db.execute('PRAGMA table_info(api_requests)').fetchall()
    columns = tuple(row[1] for row in info)
    if not {'operation', 'state'}.issubset(columns) or not any(row[1] == 'operation' and row[5] == 1 for row in info):
        raise Refuse('REQUEST_SCHEMA_UNSUPPORTED')
    rows = db.execute('SELECT * FROM api_requests ORDER BY operation LIMIT ?', (MAX_ROWS + 1,)).fetchall()
    if len(rows) > MAX_ROWS: raise Refuse('REQUEST_JOURNAL_TOO_LARGE')
    return columns, rows


def unsafe_schema(db, names):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN (?,?) LIMIT 1", ('api_requests', ARCHIVE)).fetchone():
        return True
    for name in names:
        foreign = db.execute('PRAGMA foreign_key_list(' + quoted(name) + ')').fetchall()
        if name in ('api_requests', ARCHIVE) and foreign or any(row[2] in ('api_requests', ARCHIVE) for row in foreign):
            return True
    return False


def observe(db):
    names = tables(db)
    columns, rows = rowset(db)
    index = {name: offset for offset, name in enumerate(columns)}
    selected = []
    states, scopes, days, failures = Counter(), Counter(), Counter(), Counter()
    reasons = set()
    row = db.execute('SELECT enabled FROM robot_settings WHERE id=1').fetchone() if 'robot_settings' in names else None
    off = row == (0,)
    if not off: reasons.add('ROBOT_OFF_REQUIRED')
    for table in ('order_intents', 'robot_entry_receipts'):
        if table not in names: reasons.add('ORDER_EVIDENCE_SCHEMA_UNAVAILABLE')
        elif db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone(): reasons.add('ORDER_EVIDENCE_PRESENT')
    if 'robot_jobs' not in names or 'robot_cycles' not in names or 'robot_candidates' not in names:
        reasons.add('ROBOT_JOURNAL_SCHEMA_UNAVAILABLE')
    else:
        if db.execute("SELECT 1 FROM robot_jobs WHERE state IN ('PENDING','NEEDS_REVIEW') LIMIT 1").fetchone(): reasons.add('ROBOT_JOB_UNRESOLVED')
        if db.execute("SELECT 1 FROM robot_cycles WHERE state IN ('PENDING','NEEDS_REVIEW') LIMIT 1").fetchone(): reasons.add('ROBOT_CYCLE_UNRESOLVED')
    archive_rows = {}
    if ARCHIVE in names:
        if tuple(row[1] for row in db.execute('PRAGMA table_info(' + ARCHIVE + ')')) != ARCHIVE_COLUMNS:
            reasons.add('RETIREMENT_ARCHIVE_SCHEMA_UNSUPPORTED')
        else:
            archive_rows = {row[0]: row for row in db.execute('SELECT operation,original_row,row_sha256,scope,retired_at FROM ' + ARCHIVE)}
    if unsafe_schema(db, names): reasons.add('UNSAFE_RETIREMENT_SCHEMA')
    for row in rows:
        operation, state = row[index['operation']], row[index['state']]
        recognized = classify(operation)
        states[state if state in ('PENDING', 'NEEDS_REVIEW', 'COMPLETE', 'RETIRED_LEGACY') else 'OTHER'] += 1
        if state == 'PENDING': reasons.add('PENDING_REQUEST_PRESENT')
        if state not in ('PENDING', 'NEEDS_REVIEW', 'COMPLETE', 'RETIRED_LEGACY'): reasons.add('UNSUPPORTED_REQUEST_STATE')
        if state == 'NEEDS_REVIEW':
            if recognized:
                selected.append((operation, recognized[0], tuple(row)))
                scopes[recognized[0]] += 1
                days[recognized[1]] += 1
                failure = row[index['failure_code']] if 'failure_code' in index else None
                failures[failure if failure in PUBLIC_FAILURES else 'UNAVAILABLE' if failure is None else 'REDACTED'] += 1
            else: reasons.add('CURRENT_OR_UNKNOWN_REQUEST_UNRESOLVED')
        if state == 'RETIRED_LEGACY':
            original = list(row)
            original[index['state']] = 'NEEDS_REVIEW'
            payload = encoded(columns, original)
            archived = archive_rows.get(operation)
            if (not recognized or not archived or archived[1] != payload
                    or archived[2] != checksum(payload) or archived[3] != recognized[0]):
                reasons.add('RETIREMENT_MARKER_INVALID')
    for operation, scope, source in selected:
        cycle = classify(operation)[2]
        if operation in archive_rows: reasons.add('RETIREMENT_MARKER_CONFLICT')
        for table, condition, arguments in (
            ('robot_jobs', 'operation=? OR cycle=?', (operation, cycle)),
            ('robot_candidates', 'id=? OR cycle=?', (operation, cycle)),
            ('robot_cycles', 'id=?', (cycle,)),
            ('analysis_checks', 'operation=?', (operation,)),
            ('cycles', 'day=?', (cycle,)),
        ):
            if table in names and db.execute('SELECT 1 FROM ' + table + ' WHERE ' + condition + ' LIMIT 1', arguments).fetchone():
                reasons.add('LEGACY_REQUEST_REFERENCED')
    fingerprint = checksum(b'\n'.join(encoded(columns, row) for row in rows))
    report = dict(status='INSPECTION_ONLY', eligible_count=len(selected),
        request_states=dict(states), eligible_scopes=dict(scopes), eligible_dates=dict(days),
        failure_codes=dict(failures), journal_sha256=fingerprint, robot_off=off,
        can_apply=not reasons and bool(selected), refusal_reasons=sorted(reasons))
    return dict(columns=columns, selected=selected, report=report, fingerprint=fingerprint)


def create_backup(source, directory_fd, path, observation):
    try: os.mkdir('legacy-research-backups', 0o700, dir_fd=directory_fd)
    except FileExistsError: pass
    destination = os.open('legacy-research-backups', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
    fd = None
    output = None
    try:
        info = os.fstat(destination)
        if info.st_uid not in (0, os.geteuid()) or stat.S_IMODE(info.st_mode) != 0o700:
            raise Refuse('UNSAFE_BACKUP_DIRECTORY')
        name = 'before-retirement-' + uuid.uuid4().hex + '.sqlite3'
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=destination)
        os.fchmod(fd, 0o600)
        output = sqlite3.connect('file:/proc/self/fd/' + str(fd) + '?mode=rw', uri=True)
        source.backup(output)
        if output.execute('PRAGMA integrity_check').fetchall() != [('ok',)]: raise Refuse('BACKUP_VERIFICATION_FAILED')
        copied = observe(output)
        if copied['fingerprint'] != observation['fingerprint'] or copied['report'] != observation['report']:
            raise Refuse('BACKUP_VERIFICATION_FAILED')
        output.close()
        output = None
        os.fsync(fd)
        os.fsync(destination)
        os.fsync(directory_fd)
        return path / 'legacy-research-backups' / name
    finally:
        if output is not None: output.close()
        if fd is not None: os.close(fd)
        os.close(destination)


def apply_rows(db, observation):
    db.execute('''CREATE TABLE IF NOT EXISTS legacy_research_archive(
        operation TEXT PRIMARY KEY,original_row BLOB NOT NULL,row_sha256 TEXT NOT NULL,
        scope TEXT NOT NULL,retired_at TEXT NOT NULL)''')
    state_index = observation['columns'].index('state')
    retired_at = datetime.now(timezone.utc).isoformat()
    for operation, scope, row in observation['selected']:
        payload = encoded(observation['columns'], row)
        db.execute('INSERT INTO legacy_research_archive VALUES(?,?,?,?,?)',
            (operation, payload, checksum(payload), scope, retired_at))
        changed = db.execute("UPDATE api_requests SET state='RETIRED_LEGACY' WHERE operation=? AND state='NEEDS_REVIEW'", (operation,))
        if changed.rowcount != 1: raise Refuse('REQUEST_JOURNAL_CHANGED')
        after = db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone()
        expected = list(row)
        expected[state_index] = 'RETIRED_LEGACY'
        if encoded(observation['columns'], after) != encoded(observation['columns'], expected):
            raise Refuse('REQUEST_JOURNAL_CHANGED')


def run(data_dir, apply=False):
    root, root_fd = directory(data_dir)
    parent = None
    source = None
    writer = None
    files = {}
    try:
        parent = os.open('trading', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        info = os.fstat(parent)
        if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022: raise Refuse('UNSAFE_STATE_DIRECTORY')
        path = root / 'trading'
        for name in ('ledger.sqlite3', 'ledger.sqlite3-wal', 'ledger.sqlite3-shm', 'ledger.sqlite3-journal', 'cycle.lock'):
            files[name] = checked_file(parent, name, missing=name != 'ledger.sqlite3')
        validate_directories(root, root_fd, parent)
        if files['ledger.sqlite3-journal'] is not None: raise Refuse('LEDGER_JOURNAL_PRESENT')
        header = os.pread(files['ledger.sqlite3'], 100, 0)
        if header[:16] != b'SQLite format 3\x00' or len(header) != 100: raise Refuse('INVALID_SQLITE_LEDGER')
        if (header[18] == 2 or files['ledger.sqlite3-wal'] is not None) and (files['ledger.sqlite3-wal'] is None or files['ledger.sqlite3-shm'] is None):
            raise Refuse('WAL_STATE_UNAVAILABLE')
        source = connect(path / 'ledger.sqlite3')
        source.execute('BEGIN')
        observation = observe(source)
        validate_directories(root, root_fd, parent)
        validate_files(parent, files)
        if not apply: return observation['report']
        if observation['report']['refusal_reasons']: raise Refuse(observation['report']['refusal_reasons'][0])
        if not observation['selected']: return dict(status='UNCHANGED', retired_count=0)
        if inside_checkout(root):
            raise Refuse('BACKUP_INSIDE_CHECKOUT')
        lock = files['cycle.lock']
        if lock is None: raise Refuse('CYCLE_LOCK_MISSING')
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise Refuse('CYCLE_LOCK_BUSY') from None
        validate_files(parent, files)
        # Re-read under the cycle lock before creating the private backup.
        source.execute('ROLLBACK')
        source.execute('BEGIN')
        locked = observe(source)
        if locked != observation: raise Refuse('REQUEST_JOURNAL_CHANGED')
        validate_directories(root, root_fd, parent)
        saved = create_backup(source, parent, path, observation)
        source.close()
        source = None
        validate_directories(root, root_fd, parent)
        validate_files(parent, files)
        writer = connect(path / 'ledger.sqlite3', readonly=False)
        writer.execute('BEGIN IMMEDIATE')
        try:
            validate_directories(root, root_fd, parent)
            validate_files(parent, files, writing=True)
            current = observe(writer)
            if current != observation: raise Refuse('REQUEST_JOURNAL_CHANGED')
            apply_rows(writer, current)
            validate_directories(root, root_fd, parent)
            validate_files(parent, files, writing=True)
            writer.execute('COMMIT')
        except BaseException:
            writer.execute('ROLLBACK')
            raise
        return dict(status='RETIRED_LEGACY', retired_count=len(observation['selected']),
            backup=str(saved), retired_scopes=observation['report']['eligible_scopes'])
    finally:
        if writer is not None: writer.close()
        if source is not None: source.close()
        for fd in files.values():
            if fd is not None: os.close(fd)
        if parent is not None: os.close(parent)
        os.close(root_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('/data'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try: result = run(args.data_dir, args.apply)
    except Refuse as error:
        print(json.dumps({'error': str(error)}, sort_keys=True))
        return 1
    except Exception:
        print('{"error":"LEGACY_RETIREMENT_FAILED"}')
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
