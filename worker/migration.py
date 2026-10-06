"""One-time retirement of previous runtime tables; archived data is never loaded."""
import os
import sqlite3
import stat
from pathlib import Path

RETIRED_TABLES = frozenset(('trades', 'events', 'cycles', 'analysis_checks',
    'robot_cycles', 'robot_jobs', 'robot_setups', 'robot_decisions', 'robot_simulations',
    'robot_status', 'shadow_plans', 'shadow_events', 'live_records', 'live_actions', 'live_events',
    'live_settings', 'live_requests', 'live_arms', 'live_scheduler'))
RETIRED_CACHES = ('snapshot.json', 'live-status.json')


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
