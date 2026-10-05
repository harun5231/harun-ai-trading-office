"""Regression: stale markers after graceful manual shutdown; no real login data."""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from worker.session_service import SessionBrowser
from worker.process_safety import prove_quiet


class StaleSingletonTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)
        self.browser=SessionBrowser(self.directory,{})
    def tearDown(self):
        # Remove test-created markers only, so legacy close can release its locks.
        for name in ('SingletonLock','SingletonSocket','SingletonCookie'):
            (self.browser.profile/name).unlink(missing_ok=True)
        self.browser.close();self.temp.cleanup()
    def fake_browser(self, child=False):
        p=self.directory/'fake-browser'
        p.write_text('''#!/usr/bin/env python3
import os,signal,socket,sys,time,subprocess
from pathlib import Path
profile=Path(next(a.split('=',1)[1] for a in sys.argv if a.startswith('--user-data-dir=')))
(profile/'SingletonLock').symlink_to(socket.gethostname()+'-'+str(os.getpid()))
(profile/'SingletonSocket').symlink_to('/tmp/synthetic-dead-socket')
(profile/'SingletonCookie').symlink_to('synthetic-cookie-marker')
(profile/'Cookies').write_text('SYNTHETIC SESSION: KEEP')
'''+('''child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
(profile/'test-child-pid').write_text(str(child.pid))
''' if child else '')+'''
def stop(*_):
    (profile/'saved').write_text('graceful')
    raise SystemExit(0)  # Reproduce Chromium leaving its symlinks behind.
signal.signal(signal.SIGTERM,stop)
while True:time.sleep(.02)
''');p.chmod(0o700);return str(p)
    def start(self,child=False):
        with patch.dict(os.environ,{'OFFICE_CHROMIUM_EXECUTABLE':self.fake_browser(child)}):self.browser.login()
    def markers(self):return [os.path.lexists(self.browser.profile/n) for n in ('SingletonLock','SingletonSocket','SingletonCookie')]
    def test_graceful_stop_stale_recovery_then_same_profile_check(self):
        self.start();self.assertTrue(all(self.markers()))
        self.browser.prepare_check()
        self.assertFalse(any(self.markers()));self.assertFalse(self.browser.manual.active())
        self.assertEqual((self.browser.profile/'saved').read_text(),'graceful')
        self.assertEqual((self.browser.profile/'Cookies').read_text(),'SYNTHETIC SESSION: KEEP')
        with patch('playwright.sync_api.sync_playwright') as factory:
            self.browser.open()
            self.assertEqual(factory.return_value.start.return_value.chromium.launch_persistent_context.call_args.args,(str(self.browser.profile),))
            self.browser.close()
    def test_restart_reacquires_locks_and_recovers_without_relogin(self):
        self.start();self.browser.manual.stop()
        self.browser.owner.close();self.browser.lock.close();self.browser.lock=None
        self.browser=SessionBrowser(self.directory,{})
        self.browser.prepare_check()
        self.assertFalse(any(self.markers()))
        self.assertEqual((self.browser.profile/'Cookies').read_text(),'SYNTHETIC SESSION: KEEP')
    def test_live_profile_user_prevents_cleanup_even_if_not_named_chromium(self):
        self.start();self.browser.manual.stop()
        process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)','--user-data-dir='+str(self.browser.profile)])
        try:
            with self.assertRaisesRegex(RuntimeError,'PROFILE_NOT_RELEASED'):self.browser.owner.recover_stale(self.browser.lock)
            self.assertTrue(all(self.markers()))
        finally:process.terminate();process.wait()
    def test_surviving_descendant_prevents_handoff(self):
        self.start(child=True);pid=int((self.browser.profile/'test-child-pid').read_text())
        try:
            with self.assertRaisesRegex(RuntimeError,'PROFILE_NOT_RELEASED'):self.browser.prepare_check()
            self.assertTrue(all(self.markers()))
        finally:
            os.kill(pid,15)
            for _ in range(100):
                try:prove_quiet(self.browser.profile,self.browser.manual.session_id);break
                except RuntimeError:time.sleep(.01)
    def test_ownership_missing_or_inode_replaced_never_cleans(self):
        self.start();self.browser.manual.stop()
        with self.assertRaisesRegex(RuntimeError,'PROFILE_NOT_RELEASED'):self.browser.owner.recover_stale(None)
        self.assertTrue(all(self.markers()))
        lock=self.browser.profile/'.office-owner.lock';lock.rename(lock.with_suffix('.old'));lock.touch()
        with self.assertRaisesRegex(RuntimeError,'PROFILE_NOT_RELEASED'):self.browser.owner.recover_stale(self.browser.lock)
        self.assertTrue(all(self.markers()))
        lock.unlink();lock.with_suffix('.old').rename(lock)
    def test_foreign_hostname_or_pid_reuse_is_uncertain(self):
        self.start();self.browser.manual.stop();lock=self.browser.profile/'SingletonLock'
        for target in ('foreign-host-999999',socket.gethostname()+'-'+str(os.getpid())):
            lock.unlink();lock.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError,'PROFILE_NOT_RELEASED'):self.browser.owner.recover_stale(self.browser.lock)
            self.assertTrue(all(self.markers()))
    def test_regular_file_is_not_deleted(self):
        self.start();self.browser.manual.stop();p=self.browser.profile/'SingletonCookie'
        p.unlink();p.write_text('not a singleton symlink')
        with self.assertRaisesRegex(RuntimeError,'PROFILE_NOT_RELEASED'):self.browser.owner.recover_stale(self.browser.lock)
        self.assertEqual(p.read_text(),'not a singleton symlink')
    def test_marker_change_during_proof_is_not_deleted(self):
        self.start();self.browser.manual.stop();p=self.browser.profile/'SingletonCookie'
        def change(*_,**__):p.unlink();p.symlink_to('replacement')
        with patch('worker.profile_owner.prove_quiet',side_effect=change):
            with self.assertRaisesRegex(RuntimeError,'PROFILE_NOT_RELEASED'):self.browser.owner.recover_stale(self.browser.lock)
        self.assertEqual(os.readlink(p),'replacement')
    def test_incomplete_proc_visibility_fails_closed(self):
        with patch('worker.process_safety.Path.iterdir',side_effect=PermissionError):
            with self.assertRaisesRegex(RuntimeError,'PROFILE_NOT_RELEASED'):prove_quiet(self.browser.profile)

if __name__=='__main__':unittest.main()
