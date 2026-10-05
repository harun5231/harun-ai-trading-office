"""Preserve existing trading/audit tables through a one-time SQLite backup."""
from contextlib import closing
import fcntl
import os
import sqlite3
from pathlib import Path

def directory(root):
    root=Path(root).resolve();repo=Path(__file__).resolve().parent.parent
    if root==repo or repo in root.parents:raise ValueError('PRIVATE_STATE_REQUIRED')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    dest=root/'trading';dest.mkdir(exist_ok=True,mode=0o700)
    with (dest/'migration.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        target=dest/'ledger.sqlite3'
        # Historical path only; retained on disk, never used for authentication.
        old=root/'browser'/'ledger.sqlite3'
        if not target.exists() and old.exists():
            tmp=dest/'ledger.migrating'
            with closing(sqlite3.connect('file:'+str(old)+'?mode=ro',uri=True)) as source,closing(sqlite3.connect(tmp)) as output:source.backup(output)
            tmp.chmod(0o600)
            with tmp.open('rb') as f:os.fsync(f.fileno())
            os.replace(tmp,target)
            fd=os.open(dest,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        # A prior scheduled attempt also consumes this day's cycle allowance.
        attempts=root/'browser'/'runner-attempts'
        if attempts.exists():
            with sqlite3.connect(target) as db:
                db.execute('CREATE TABLE IF NOT EXISTS cycles(day TEXT PRIMARY KEY,state TEXT,created REAL)')
                import re
                for p in attempts.iterdir():
                    if re.fullmatch(r'\d{4}-\d{2}-\d{2}',p.name):db.execute('INSERT OR IGNORE INTO cycles VALUES(?,?,?)',(p.name,'LEGACY_ATTEMPT',0))
    return dest
