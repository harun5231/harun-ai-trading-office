"""Regression coverage for legacy root-owned locks; no provider credentials."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.container_boot import repair_state_locks


class StateLockRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.trading=self.root/'trading'
        self.trading.mkdir(mode=0o700)

    def test_existing_locks_repaired_without_replacing_or_truncating_audit(self):
        locks=[]
        for name in ('migration.lock','cycle.lock'):
            path=self.trading/name
            path.write_bytes(b'existing lock inode')
            path.chmod(0o640)
            locks.append((path,path.stat().st_ino))
        ledger=self.trading/'ledger.sqlite3'
        ledger.write_bytes(b'audit data must remain intact')
        ledger.chmod(0o640)
        before=ledger.stat()
        observed=[]
        def chown(fd,uid,gid):
            observed.append((os.fstat(fd).st_ino,uid,gid))
        with patch('deploy.container_boot.os.fchown',side_effect=chown):
            repair_state_locks(self.trading)
        self.assertEqual(observed,[(inode,10001,10001) for _,inode in locks])
        for path,inode in locks:
            self.assertEqual(path.stat().st_ino,inode)
            self.assertEqual(path.read_bytes(),b'existing lock inode')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        self.assertEqual(ledger.stat(),before)
        self.assertEqual(ledger.read_bytes(),b'audit data must remain intact')

    def test_missing_directory_or_locks_are_not_created(self):
        with patch('deploy.container_boot.os.fchown') as chown:
            repair_state_locks(self.root/'missing')
            repair_state_locks(self.trading)
        chown.assert_not_called()
        self.assertFalse((self.root/'missing').exists())
        self.assertEqual(list(self.trading.iterdir()),[])

    def test_symlink_directory_is_rejected_without_following_target(self):
        alias=self.root/'alias'
        alias.symlink_to(self.trading,target_is_directory=True)
        with patch('deploy.container_boot.os.fchown') as chown:
            with self.assertRaisesRegex(SystemExit,'WORKER_STATE_LOCK_INVALID'):
                repair_state_locks(alias)
        chown.assert_not_called()

    def test_symlink_lock_is_rejected_without_changing_target(self):
        target=self.root/'outside'
        target.write_bytes(b'outside audit')
        target.chmod(0o640)
        (self.trading/'cycle.lock').symlink_to(target)
        before=target.stat()
        with patch('deploy.container_boot.os.fchown') as chown:
            with self.assertRaisesRegex(SystemExit,'WORKER_STATE_LOCK_INVALID'):
                repair_state_locks(self.trading)
        chown.assert_not_called()
        self.assertEqual(target.stat(),before)
        self.assertEqual(target.read_bytes(),b'outside audit')

    def test_hardlinked_lock_is_rejected(self):
        target=self.root/'outside'
        target.write_bytes(b'outside audit')
        os.link(target,self.trading/'cycle.lock')
        with patch('deploy.container_boot.os.fchown') as chown:
            with self.assertRaisesRegex(SystemExit,'WORKER_STATE_LOCK_INVALID'):
                repair_state_locks(self.trading)
        chown.assert_not_called()

    def test_nonregular_lock_is_rejected_without_blocking(self):
        os.mkfifo(self.trading/'cycle.lock',0o600)
        with patch('deploy.container_boot.os.fchown') as chown:
            with self.assertRaisesRegex(SystemExit,'WORKER_STATE_LOCK_INVALID'):
                repair_state_locks(self.trading)
        chown.assert_not_called()

    def test_unrepairable_lock_has_safe_failure(self):
        (self.trading/'cycle.lock').write_bytes(b'lock')
        with patch('deploy.container_boot.os.fchown',side_effect=PermissionError('private detail')):
            with self.assertRaisesRegex(SystemExit,'^WORKER_STATE_LOCK_INVALID$'):
                repair_state_locks(self.trading)
