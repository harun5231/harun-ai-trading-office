"""Sidecar-free SQLite snapshots for NEW, non-financial application journals.

snapshot_database(directory, filename, *, write=False, lock_timeout=5) yields a
private in-memory SQLite connection, or None for a missing read-only snapshot.
A successful writer context publishes after explicit transactions are committed;
exceptions publish nothing. No connection escapes the context. Readers cannot
mutate the image. All names in a directory share one cross-process directory lock.

The actor must supply a dedicated snapshot basename, never a financial database
name, and a stable, trusted, worker-owned directory/ancestor path. Cooperating
callers must not ATTACH files or change the memory-only SQLite storage settings.
See docs/durable-trade-evaluation.md for the filesystem and UID threat boundary.
"""
import fcntl
import os
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager


@contextmanager
def snapshot_database(directory, filename, *, write=False, lock_timeout=5):
    """Load one pinned image; optionally publish with file+directory fsync."""
    if (not isinstance(filename, str) or filename in ('', '.', '..')
            or '/' in filename or '\x00' in filename):
        raise ValueError('SNAPSHOT_FILENAME_INVALID')
    # Pin the actor directory, not a checked pathname later reopened by SQLite.
    # flock on this inode is shared by every reader/writer, including processes.
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    db = None
    try:
        info = os.fstat(directory_fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError('SNAPSHOT_DIRECTORY_INVALID')
        deadline = time.monotonic() + lock_timeout
        while True:
            try:
                fcntl.flock(directory_fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('SNAPSHOT_LOCK_TIMEOUT') from None
                time.sleep(min(0.01, remaining))
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory_fd)
        except FileNotFoundError:
            if not write:
                yield None
                return
            image = None
        else:
            with os.fdopen(fd, 'rb') as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.geteuid()):
                    raise ValueError('SNAPSHOT_FILE_INVALID')
                image = source.read()
            # Never recover/migrate a WAL image with external uncheckpointed state.
            if len(image) < 100 or image[18:20] != b'\x01\x01':
                raise ValueError('SNAPSHOT_IMAGE_INVALID')
        # SQLite NEVER receives a filesystem pathname, including on status.
        # WAL, SHM, rollback/super journals cannot be opened by this connection.
        db = sqlite3.connect(':memory:', isolation_level=None)
        db.execute('PRAGMA temp_store=MEMORY')
        if image is not None:
            db.deserialize(image)
        db.execute('PRAGMA journal_mode=MEMORY')
        if not write:
            db.execute('PRAGMA query_only=ON')
        yield db
        if write:
            if db.in_transaction:
                raise ValueError('SNAPSHOT_TRANSACTION_UNCOMMITTED')
            _publish(directory_fd, filename, db.serialize())
    finally:
        if db is not None:
            db.close()
        os.close(directory_fd)


def _publish(directory_fd, filename, image):
    # Never open an existing inode for writing, even after validation. Exclusive
    # creation + dirfd-relative replacement cannot follow a raced link/rename.
    name = '.sqlite-snapshot-' + secrets.token_hex(16)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, 'wb') as target:
            target.write(image)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
