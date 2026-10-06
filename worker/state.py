"""Private startup directory and inode-preserving migration lock."""
import fcntl
import os
import stat
from pathlib import Path

def directory(root):
    root=Path(root).resolve();repo=Path(__file__).resolve().parent.parent
    if root==repo or repo in root.parents:raise ValueError('PRIVATE_STATE_REQUIRED')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    dest=root/'trading';dest.mkdir(exist_ok=True,mode=0o700)
    parent=None;lock=None
    try:
        parent=os.open(dest,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        os.fchmod(parent,0o700)
        lock=os.open('migration.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,
            0o600,dir_fd=parent)
        value=os.fstat(lock)
        if not stat.S_ISREG(value.st_mode) or value.st_nlink!=1:
            raise ValueError('WORKER_STATE_LOCK_INVALID')
        os.fchmod(lock,0o600)
        fcntl.flock(lock,fcntl.LOCK_EX)
    except OSError:raise ValueError('WORKER_STATE_LOCK_INVALID') from None
    finally:
        if lock is not None:os.close(lock)
        if parent is not None:os.close(parent)
    return dest
