"""Durable retirement and lossless namespace upgrades; archives are never loaded."""
import os
import sqlite3
import stat
from pathlib import Path

RETIRED_TABLES = frozenset(('trades', 'events', 'cycles', 'analysis_checks',
    'robot_cycles', 'robot_jobs', 'robot_setups', 'robot_decisions', 'robot_simulations',
    'robot_status', 'shadow_plans', 'shadow_events', 'live_records', 'live_actions', 'live_events',
    'live_settings', 'live_requests', 'live_arms', 'live_scheduler'))
RETIRED_CACHES = ('snapshot.json', 'live-status.json')
CYCLE_NAMESPACE_SCHEMA_VERSION = 2


def _checked_file(directory, name, code):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError:
        raise RuntimeError(code) from None
    try:
        value = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise RuntimeError(code) from None
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        os.close(fd)
        raise RuntimeError(code) from None
    return fd


def _validate_archive(db, directory, retired):
    """Validate a complete backup before any destructive schema operation."""
    fd = _checked_file(directory, 'ledger-pre-order.sqlite3', 'WORKER_ARCHIVE_INVALID')
    if fd is None:
        raise RuntimeError('WORKER_ARCHIVE_INVALID')
    archive = None
    try:
        # Read the checked inode, never a replacement path or an archive WAL.
        archive = sqlite3.connect('file:/proc/self/fd/' + str(fd) + '?mode=ro&immutable=1', uri=True)
        if archive.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RuntimeError('WORKER_ARCHIVE_INVALID')
        archived_tables = {row[0] for row in archive.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = set(retired)
        active_tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required.update(active_tables & {'api_requests', 'robot_entry_receipts'})
        if not required.issubset(archived_tables):
            raise RuntimeError('WORKER_ARCHIVE_INVALID')
        for name in sorted(required):
            source_count = db.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]
            archive_count = archive.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]
            if archive_count < source_count:
                raise RuntimeError('WORKER_ARCHIVE_INVALID')
    except (sqlite3.Error, OSError):
        raise RuntimeError('WORKER_ARCHIVE_INVALID') from None
    finally:
        if archive is not None:
            archive.close()
        os.close(fd)


def _create_archive(db, directory):
    try:
        fd = os.open('ledger-pre-order.sqlite3', os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
    except FileExistsError:
        return
    except OSError:
        raise RuntimeError('WORKER_ARCHIVE_INVALID') from None
    output = None
    try:
        output = sqlite3.connect('file:/proc/self/fd/' + str(fd) + '?mode=rw', uri=True)
        db.backup(output)
        output.close()
        output = None
        os.fsync(fd)
        os.fsync(directory)
    except (sqlite3.Error, OSError):
        raise RuntimeError('WORKER_ARCHIVE_INVALID') from None
    finally:
        if output is not None:
            output.close()
        os.close(fd)


def _remove_retired_caches(directory, caches):
    for name, fd in caches:
        expected = os.fstat(fd)
        try:
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                    or current.st_ino != expected.st_ino or current.st_dev != expected.st_dev):
                raise RuntimeError('WORKER_RETIRED_CACHE_INVALID')
            os.unlink(name, dir_fd=directory)
        except OSError:
            raise RuntimeError('WORKER_RETIRED_CACHE_INVALID') from None


def retire_previous_runtime(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'office_schema' in tables:
        return
    retired = tables & RETIRED_TABLES
    path = Path(db.execute('PRAGMA database_list').fetchone()[2])
    directory = None
    caches = []
    try:
        try:
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise RuntimeError('WORKER_MIGRATION_DIRECTORY_INVALID') from None
        # Validate the entire cleanup set before backup or schema changes.
        for name in RETIRED_CACHES:
            fd = _checked_file(directory, name, 'WORKER_RETIRED_CACHE_INVALID')
            if fd is not None:
                caches.append((name, fd))
        if retired:
            _create_archive(db, directory)
        db.execute('BEGIN IMMEDIATE')
        try:
            # Source is now write-locked. Empty/interrupted or stale backups
            # cannot authorize dropping the active tables on a later restart.
            if retired:
                _validate_archive(db, directory, retired)
            for name in sorted(retired):
                db.execute('DROP TABLE "' + name + '"')
            if 'robot_settings' in tables:
                db.execute("UPDATE robot_settings SET enabled=0,risk='5' WHERE id=1")
            _remove_retired_caches(directory, caches)
            db.execute('CREATE TABLE office_schema(version INTEGER NOT NULL)')
            db.execute('INSERT INTO office_schema VALUES(1)')
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    finally:
        for _, fd in caches:
            os.close(fd)
        if directory is not None:
            os.close(directory)


def upgrade_cycle_namespaces(db):
    """Allow a new request namespace without changing existing execution evidence.

    Version 1 tied cycle identity to (day, entry_epoch). That uniqueness would
    reuse a previous gross-risk request when the fee-inclusive contract starts
    at the same observed entry epoch. Cycle IDs already contain the contract
    namespace, so only their primary key must remain unique.
    """
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'office_schema' not in tables:
        # The audited first-retirement migration must run before this upgrade.
        raise RuntimeError('WORKER_SCHEMA_BASELINE_REQUIRED')
    db.execute('BEGIN IMMEDIATE')
    try:
        versions = db.execute('SELECT version FROM office_schema').fetchall()
        if len(versions) != 1 or type(versions[0][0]) is not int or versions[0][0] < 1:
            raise RuntimeError('WORKER_SCHEMA_INVALID')
        if versions[0][0] >= CYCLE_NAMESPACE_SCHEMA_VERSION:
            db.execute('COMMIT')
            return
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'robot_cycles' in tables:
            obsolete_indexes = set()
            for index in db.execute('PRAGMA index_list(robot_cycles)').fetchall():
                if not index[2]:
                    continue
                columns = [row[2] for row in db.execute('PRAGMA index_info("' + index[1].replace('"', '""') + '")')]
                if len(columns) == 2 and set(columns) == {'day', 'entry_epoch'}:
                    obsolete_indexes.add(index[1])
            if obsolete_indexes:
                columns = db.execute('PRAGMA table_info(robot_cycles)').fetchall()
                if [row[1] for row in columns] != ['id', 'day', 'entry_epoch', 'state', 'data']:
                    raise RuntimeError('WORKER_CYCLE_SCHEMA_UNSUPPORTED')
                if 'robot_cycles_namespace_upgrade' in tables:
                    raise RuntimeError('WORKER_CYCLE_SCHEMA_UNSUPPORTED')
                # No table may lose rows through DROP-triggered FK cascades.
                for table in tables:
                    name = table.replace('"', '""')
                    if any(row[2] == 'robot_cycles' for row in db.execute('PRAGMA foreign_key_list("' + name + '")')):
                        raise RuntimeError('WORKER_CYCLE_SCHEMA_UNSUPPORTED')
                definitions = [(row[0], row[1]) for row in db.execute(
                    "SELECT name,sql FROM sqlite_master WHERE tbl_name='robot_cycles' AND type IN ('index','trigger') AND sql IS NOT NULL")
                    if row[0] not in obsolete_indexes]
                db.execute('''CREATE TABLE robot_cycles_namespace_upgrade(
                    id TEXT PRIMARY KEY,day TEXT NOT NULL,entry_epoch INTEGER NOT NULL,
                    state TEXT NOT NULL,data TEXT NOT NULL)''')
                db.execute('''INSERT INTO robot_cycles_namespace_upgrade(rowid,id,day,entry_epoch,state,data)
                    SELECT rowid,id,day,entry_epoch,state,data FROM robot_cycles''')
                db.execute('DROP TABLE robot_cycles')
                db.execute('ALTER TABLE robot_cycles_namespace_upgrade RENAME TO robot_cycles')
                for _, definition in definitions:
                    db.execute(definition)
        db.execute('UPDATE office_schema SET version=?', (CYCLE_NAMESPACE_SCHEMA_VERSION,))
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
