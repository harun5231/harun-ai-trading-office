"""Bounded recovery of legacy state ownership; no credentials or provider calls."""
import fcntl
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.container_boot import repair_state_locks, STATE_FILES


class StateLockRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.trading=self.root/'trading'
        self.trading.mkdir(mode=0o700)

    def observed_chown(self,records):
        def chown(fd,uid,gid):
            records.append((os.fstat(fd).st_ino,uid,gid))
        return chown

    def test_existing_allowlisted_state_repaired_without_replacing_or_truncating_audit(self):
        files=[]
        for name in STATE_FILES:
            path=self.trading/name
            payload=('existing audit bytes: '+name).encode()
            path.write_bytes(payload)
            path.chmod(0o640)
            files.append((path,path.stat().st_ino,payload))
        self.trading.chmod(0o750)
        directory_inode=self.trading.stat().st_ino
        observed=[]
        with patch('deploy.container_boot.os.fchown',side_effect=self.observed_chown(observed)):
            repair_state_locks(self.trading)
        self.assertEqual(observed,[(inode,10001,10001) for _,inode,_ in files]+[(directory_inode,10001,10001)])
        for path,inode,payload in files:
            self.assertEqual(path.stat().st_ino,inode)
            self.assertEqual(path.read_bytes(),payload)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        self.assertEqual(self.trading.stat().st_ino,directory_inode)
        self.assertEqual(stat.S_IMODE(self.trading.stat().st_mode),0o700)

    def test_missing_directory_or_files_are_not_created(self):
        with patch('deploy.container_boot.os.fchown') as chown:
            repair_state_locks(self.root/'missing')
            chown.assert_not_called()
        observed=[]
        with patch('deploy.container_boot.os.fchown',side_effect=self.observed_chown(observed)):
            repair_state_locks(self.trading)
        self.assertEqual(observed,[(self.trading.stat().st_ino,10001,10001)])
        self.assertFalse((self.root/'missing').exists())
        self.assertEqual(list(self.trading.iterdir()),[])

    def test_unknown_artifacts_and_children_are_never_repaired(self):
        nested=self.trading/'unknown-directory'
        nested.mkdir(mode=0o750)
        child=nested/'ledger.sqlite3'
        child.write_bytes(b'unknown nested audit')
        child.chmod(0o640)
        outside=self.root/'outside'
        outside.write_bytes(b'outside audit')
        outside.chmod(0o640)
        alias=self.trading/'unknown-alias'
        alias.symlink_to(outside)
        temporary=self.trading/'.ledger-backup-interrupted'
        temporary.write_bytes(b'unknown historical temporary output')
        temporary.chmod(0o644)
        fifo=self.trading/'unknown-pipe'
        os.mkfifo(fifo,0o640)
        before={path:path.lstat() for path in (nested,child,outside,alias,temporary,fifo)}
        observed=[]
        with patch('deploy.container_boot.os.fchown',side_effect=self.observed_chown(observed)):
            repair_state_locks(self.trading)
        self.assertEqual(observed,[(self.trading.stat().st_ino,10001,10001)])
        for path,info in before.items():self.assertEqual(path.lstat(),info)
        self.assertEqual(child.read_bytes(),b'unknown nested audit')
        self.assertEqual(outside.read_bytes(),b'outside audit')

    def test_sqlite_ledger_wal_and_shm_content_survives_repair(self):
        path=self.trading/'ledger.sqlite3'
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
            db.execute('CREATE TABLE audit(data TEXT)')
            db.execute('INSERT INTO audit VALUES(?)',('preserved audit',))
            db.commit()
            names=('ledger.sqlite3','ledger.sqlite3-wal','ledger.sqlite3-shm')
            before={name:((self.trading/name).stat().st_ino,(self.trading/name).read_bytes()) for name in names}
            with patch('deploy.container_boot.os.fchown'):
                repair_state_locks(self.trading)
            for name,(inode,payload) in before.items():
                self.assertEqual((self.trading/name).stat().st_ino,inode)
                self.assertEqual((self.trading/name).read_bytes(),payload)
                self.assertEqual(stat.S_IMODE((self.trading/name).stat().st_mode),0o600)
            self.assertEqual(db.execute('SELECT data FROM audit').fetchone()[0],'preserved audit')

    def test_held_cycle_lock_remains_same_locked_inode(self):
        path=self.trading/'cycle.lock'
        path.write_bytes(b'existing cycle lock')
        with path.open('r') as holder:
            fcntl.flock(holder,fcntl.LOCK_EX|fcntl.LOCK_NB)
            inode=os.fstat(holder.fileno()).st_ino
            with patch('deploy.container_boot.os.fchown'):
                repair_state_locks(self.trading)
            self.assertEqual(path.stat().st_ino,inode)
            with path.open('r') as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(path.read_bytes(),b'existing cycle lock')

    def test_non_directory_state_path_is_rejected(self):
        path=self.root/'plain-file'
        path.write_bytes(b'outside audit')
        before=path.stat()
        with patch('deploy.container_boot.os.fchown') as chown:
            with self.assertRaisesRegex(SystemExit,'^WORKER_STATE_LOCK_INVALID$'):
                repair_state_locks(path)
        chown.assert_not_called()
        self.assertEqual(path.stat(),before)

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

    def test_all_allowlisted_files_reject_symlinks_hardlinks_fifo_and_directory(self):
        for name in STATE_FILES:
            for kind in ('symlink','hardlink','fifo','directory'):
                with self.subTest(name=name,kind=kind):
                    directory=self.root/(name+'-'+kind)
                    directory.mkdir()
                    target=self.root/(name+'-'+kind+'-outside')
                    target.write_bytes(b'outside audit')
                    target.chmod(0o640)
                    path=directory/name
                    if kind=='symlink':path.symlink_to(target)
                    elif kind=='hardlink':os.link(target,path)
                    elif kind=='fifo':os.mkfifo(path,0o600)
                    else:path.mkdir()
                    before=target.stat()
                    with patch('deploy.container_boot.os.fchown') as chown:
                        with self.assertRaisesRegex(SystemExit,'^WORKER_STATE_LOCK_INVALID$'):
                            repair_state_locks(directory)
                    chown.assert_not_called()
                    self.assertEqual(target.stat(),before)
                    self.assertEqual(target.read_bytes(),b'outside audit')

    def test_invalid_later_file_does_not_partially_repair_valid_locks(self):
        lock=self.trading/'cycle.lock'
        lock.write_bytes(b'held inode')
        lock.chmod(0o640)
        target=self.root/'outside'
        target.write_bytes(b'outside audit')
        (self.trading/'ledger-pre-order.sqlite3').symlink_to(target)
        before=lock.stat()
        with patch('deploy.container_boot.os.fchown') as chown:
            with self.assertRaisesRegex(SystemExit,'^WORKER_STATE_LOCK_INVALID$'):
                repair_state_locks(self.trading)
        chown.assert_not_called()
        self.assertEqual(lock.stat(),before)

    def test_every_open_descriptor_is_closed_on_validation_failure(self):
        (self.trading/'migration.lock').write_bytes(b'valid inode')
        os.mkfifo(self.trading/'ledger-pre-order.sqlite3',0o600)
        opened=[]
        closed=[]
        real_open,real_close=os.open,os.close
        def track_open(*args,**kwargs):
            fd=real_open(*args,**kwargs);opened.append(fd);return fd
        def track_close(fd):closed.append(fd);real_close(fd)
        with patch('deploy.container_boot.os.open',side_effect=track_open),patch('deploy.container_boot.os.close',side_effect=track_close):
            with self.assertRaisesRegex(SystemExit,'^WORKER_STATE_LOCK_INVALID$'):
                repair_state_locks(self.trading)
        self.assertEqual(sorted(opened),sorted(closed))

    def test_repeated_repair_is_idempotent_and_creates_no_artifacts(self):
        lock=self.trading/'cycle.lock'
        lock.write_bytes(b'held inode')
        inode=lock.stat().st_ino
        with patch('deploy.container_boot.os.fchown'):
            repair_state_locks(self.trading)
            repair_state_locks(self.trading)
        self.assertEqual(list(self.trading.iterdir()),[lock])
        self.assertEqual(lock.stat().st_ino,inode)
        self.assertEqual(lock.read_bytes(),b'held inode')

    def test_unrepairable_lock_has_safe_failure(self):
        (self.trading/'cycle.lock').write_bytes(b'lock')
        with patch('deploy.container_boot.os.fchown',side_effect=PermissionError('private detail')):
            with self.assertRaisesRegex(SystemExit,'^WORKER_STATE_LOCK_INVALID$'):
                repair_state_locks(self.trading)

    def test_unrepairable_directory_has_safe_failure(self):
        with patch('deploy.container_boot.os.fchown',side_effect=PermissionError('private detail')):
            with self.assertRaisesRegex(SystemExit,'^WORKER_STATE_LOCK_INVALID$'):
                repair_state_locks(self.trading)

    def test_chmod_failure_does_not_leak_private_details(self):
        (self.trading/'ledger-pre-order.sqlite3').write_bytes(b'private state')
        with patch('deploy.container_boot.os.fchown'),patch('deploy.container_boot.os.fchmod',side_effect=PermissionError('private detail')):
            with self.assertRaisesRegex(SystemExit,'^WORKER_STATE_LOCK_INVALID$'):
                repair_state_locks(self.trading)
