"""Exclusive profile ownership shared by manual Chromium and automated readers.
Only explicit session handoff can recover proven stale singleton symlinks.
"""
import fcntl
import os
import time
import stat
import socket
from .process_safety import prove_quiet
from pathlib import Path


class ProfileOwner:
    def __init__(self, profile):
        self.profile=Path(profile);self.file=None
    def acquire(self, *, allow_stale=False):
        if self.file is not None:return
        handle=(self.profile/'.office-owner.lock').open('a')
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close();raise RuntimeError('WORKER_BUSY') from None
        self.file=handle
        try:
            if not allow_stale:self.wait_released(timeout=0)
        except Exception:
            self.close();raise
    def wait_released(self, timeout=10):
        deadline=time.monotonic()+timeout
        while any(os.path.lexists(self.profile/name) for name in ('SingletonLock','SingletonSocket','SingletonCookie')):
            if time.monotonic()>=deadline:raise RuntimeError('PROFILE_NOT_RELEASED')
            time.sleep(.1)
    def recover_stale(self, worker_lock, session_id=None):
        """Caller has stopped manual browser; retain both locks until handoff ends."""
        def owns(handle,path):
            if handle is None or handle.closed:raise RuntimeError('PROFILE_NOT_RELEASED')
            try:
                held=os.fstat(handle.fileno());current=path.stat(follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or (held.st_dev,held.st_ino)!=(current.st_dev,current.st_ino):
                    raise RuntimeError('PROFILE_NOT_RELEASED')
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except (OSError,ValueError):raise RuntimeError('PROFILE_NOT_RELEASED') from None
        def ownership():
            owns(self.file,self.profile/'.office-owner.lock')
            owns(worker_lock,self.profile.parent/'worker.lock')
        def snapshot():
            result={}
            for name in ('SingletonLock','SingletonSocket','SingletonCookie'):
                path=self.profile/name
                try:info=path.lstat()
                except FileNotFoundError:continue
                if not stat.S_ISLNK(info.st_mode):raise RuntimeError('PROFILE_NOT_RELEASED')
                result[name]=(info.st_dev,info.st_ino,info.st_mtime_ns,os.readlink(path))
            return result
        ownership()
        before=snapshot();pid=None
        if 'SingletonLock' in before:
            target=before['SingletonLock'][3]
            try:host,pid=target.rsplit('-',1);pid=int(pid)
            except (ValueError,TypeError):raise RuntimeError('PROFILE_NOT_RELEASED') from None
            if host!=socket.gethostname() or pid<=0:raise RuntimeError('PROFILE_NOT_RELEASED')
        # This also handles an old container's stale markers without deleting its
        # profile: all browser processes must be absent in the current namespace.
        prove_quiet(self.profile,session_id,dead_pid=pid)
        ownership()
        if snapshot()!=before:raise RuntimeError('PROFILE_NOT_RELEASED')
        prove_quiet(self.profile,session_id,dead_pid=pid)
        for name,expected in before.items():
            ownership()
            if snapshot().get(name)!=expected:raise RuntimeError('PROFILE_NOT_RELEASED')
            (self.profile/name).unlink()  # symlink itself only; never its target
    def close(self):
        if self.file is not None:self.file.close();self.file=None
