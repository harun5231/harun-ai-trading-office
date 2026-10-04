"""Exclusive profile ownership shared by manual Chromium and automated readers.
Never delete Chromium singleton files: uncertain ownership fails closed.
"""
import fcntl
import os
import time
from pathlib import Path


class ProfileOwner:
    def __init__(self, profile):
        self.profile=Path(profile);self.file=None
    def acquire(self):
        if self.file is not None:return
        handle=(self.profile/'.office-owner.lock').open('a')
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close();raise RuntimeError('WORKER_BUSY') from None
        self.file=handle
        try:self.wait_released(timeout=0)
        except Exception:
            self.close();raise
    def wait_released(self, timeout=10):
        deadline=time.monotonic()+timeout
        while any(os.path.lexists(self.profile/name) for name in ('SingletonLock','SingletonSocket','SingletonCookie')):
            if time.monotonic()>=deadline:raise RuntimeError('PROFILE_NOT_RELEASED')
            time.sleep(.1)
    def close(self):
        if self.file is not None:self.file.close();self.file=None
