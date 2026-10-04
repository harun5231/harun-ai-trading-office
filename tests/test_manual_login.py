"""Local process/ownership tests only; no assertion of real Neurobro acceptance."""
import builtins
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from worker.manual_browser import ManualBrowser,chromium_executable
from worker.profile_owner import ProfileOwner
from worker.session_service import SessionBrowser,SessionController
from worker.browser import BrowserAdapter


class ManualLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.directory=Path(self.tmp.name)
        self.browser=SessionBrowser(self.directory,{})
    def tearDown(self):
        self.browser.close();self.tmp.cleanup()
    def fake_executable(self):
        # Test-only local executable emulates orderly process exit, not Neurobro.
        p=self.directory/'test-browser'
        p.write_text('''#!/usr/bin/env python3
import signal,time,sys
from pathlib import Path
profile=Path(next(x.split('=',1)[1] for x in sys.argv if x.startswith('--user-data-dir=')))
(profile/'SingletonLock').write_text('synthetic')
def stop(*_):
    (profile/'saved-test-state').write_text('synthetic-only')
    (profile/'SingletonLock').unlink()
    raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
while True:time.sleep(.02)
''');p.chmod(0o700);return str(p)
    def start_fake(self):
        with patch.dict(os.environ,{'OFFICE_CHROMIUM_EXECUTABLE':self.fake_executable()}):
            self.assertEqual(self.browser.login(),'LOGIN_IN_PROGRESS')
    def test_login_starts_ordinary_subprocess_without_importing_playwright(self):
        original=builtins.__import__
        def guarded(name,*args,**kwargs):
            if name.startswith('playwright'):raise AssertionError('Manual login imported Playwright')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=guarded):self.start_fake()
        self.assertTrue(self.browser.manual.active());self.assertIsNone(self.browser.pw)
        self.assertIsNone(self.browser.context)
    def test_active_manual_blocks_session_and_screening_adapter(self):
        self.start_fake()
        with self.assertRaisesRegex(RuntimeError,'WORKER_BUSY'):self.browser.open()
        other=SessionBrowser(self.directory,{})
        with self.assertRaisesRegex(RuntimeError,'WORKER_BUSY'):other.open()
        other.close()
        with self.assertRaisesRegex(RuntimeError,'WORKER_BUSY'):ProfileOwner(self.browser.profile).acquire()
        # Same shared guard is used even when BrowserAdapter is called outside CLI.
        from worker.core import Review
        adapter=BrowserAdapter({'selectors_verified_on':'test-only'},self.browser.profile,self.directory/'artifacts')
        with patch('worker.screening.ChatScreening.validate'),patch('playwright.sync_api.sync_playwright') as factory:
            with self.assertRaises(Review):adapter.open_screening()
            factory.assert_not_called()
    def test_check_waits_for_clean_exit_and_keeps_ownership(self):
        self.start_fake();profile=self.browser.profile
        self.browser.prepare_check()
        self.assertFalse(self.browser.manual.active())
        self.assertTrue((profile/'saved-test-state').exists())
        self.assertFalse((profile/'SingletonLock').exists())
        with self.assertRaisesRegex(RuntimeError,'WORKER_BUSY'):ProfileOwner(profile).acquire()
        self.assertEqual(self.browser.profile,profile)
        self.browser.close()
        new=SessionBrowser(self.directory,{})
        new.acquire();self.assertEqual(new.profile,profile);new.close()
        self.assertTrue((profile/'saved-test-state').exists())
    def test_inherited_guard_keeps_lock_after_controller_descriptor_closes(self):
        self.start_fake()
        self.browser.owner.close();self.browser.lock.close();self.browser.lock=None
        with self.assertRaisesRegex(RuntimeError,'WORKER_BUSY'):ProfileOwner(self.browser.profile).acquire()
        self.browser.manual.stop()
        owner=ProfileOwner(self.browser.profile);owner.acquire();owner.close()
    def test_no_force_kill_or_handoff_on_close_timeout(self):
        process=Mock();process.poll.return_value=None
        process.wait.side_effect=subprocess.TimeoutExpired('synthetic',20)
        manual=ManualBrowser(self.browser.profile);manual.process=process
        with self.assertRaisesRegex(RuntimeError,'MANUAL_BROWSER_NOT_STOPPED'):manual.stop()
        process.terminate.assert_called_once();process.kill.assert_not_called()
        self.assertIs(manual.process,process)
    def test_uncertain_chromium_singleton_is_not_deleted(self):
        marker=self.browser.profile/'SingletonLock';marker.symlink_to('old-host-99999')
        owner=ProfileOwner(self.browser.profile)
        with self.assertRaisesRegex(RuntimeError,'PROFILE_NOT_RELEASED'):owner.acquire()
        self.assertTrue(marker.is_symlink());self.assertIsNone(owner.file)
        marker.unlink()  # synthetic test cleanup only
    def test_no_cdp_stealth_or_identity_flags_and_portrait(self):
        process=Mock();process.poll.return_value=None
        with patch('worker.manual_browser.chromium_executable',return_value='/example/chrome'),patch('worker.manual_browser.subprocess.Popen',return_value=process) as launch,patch('worker.manual_browser.time.sleep'):
            ManualBrowser(self.browser.profile).start((7,8))
        command=launch.call_args.args[0];joined=' '.join(command)
        for forbidden in ('remote-debugging','enable-automation','disable-blink','user-agent','headless'):
            self.assertNotIn(forbidden,joined)
        self.assertIn('--window-size=430,932',command)
        self.assertIn('--user-data-dir='+str(self.browser.profile),command)
        self.assertEqual(launch.call_args.kwargs['pass_fds'],(7,8))
    def test_same_profile_passed_to_playwright_only_after_clean_stop(self):
        self.start_fake();profile=self.browser.profile
        self.browser.prepare_check()
        with patch('playwright.sync_api.sync_playwright') as factory:
            playwright=factory.return_value.start.return_value
            self.browser.open()
            launch=playwright.chromium.launch_persistent_context
            self.assertEqual(launch.call_args.args,(str(profile),))
            self.assertFalse(self.browser.manual.active())
            self.assertTrue((profile/'saved-test-state').exists())
            self.assertTrue(launch.call_args.kwargs['no_viewport'])
            self.browser.close()
    def test_profile_persists_when_controller_is_reconstructed(self):
        self.start_fake();self.browser.close()
        restored=SessionBrowser(self.directory,{})
        self.assertEqual(restored.profile,self.browser.profile)
        self.assertEqual((restored.profile/'saved-test-state').read_text(),'synthetic-only')
        restored.close()
    def test_visible_verification_failure_is_cloudflare_required(self):
        for message in ('Verification failed','Maximum Attempts Reached'):
            page=Mock()
            def locate(text,exact):
                loc=Mock();loc.count.return_value=int(text==message)
                loc.nth.return_value.is_visible.return_value=True
                return loc
            page.get_by_text.side_effect=locate;self.browser.page=page
            self.assertEqual(self.browser.inspect(),'CLOUDFLARE_REQUIRED')
            page.reload.assert_not_called()
        self.browser.page=None
    def test_missing_binary_fails_closed(self):
        with patch.dict(os.environ,{'OFFICE_CHROMIUM_EXECUTABLE':'/nonexistent/chrome'}):
            with self.assertRaisesRegex(RuntimeError,'CHROMIUM_EXECUTABLE'):chromium_executable()
    def test_supervisor_keeps_display_until_session_stops(self):
        text=Path('deploy/supervise.py').read_text().split('    finally:')[1]
        self.assertEqual(text.count('for p in reversed(children):'),1)
        self.assertLess(text.index('p.terminate()'),text.index('p.wait('))
    def test_persistent_volume_scaling_and_dry_run_unchanged(self):
        import yaml
        worker=yaml.safe_load(Path('compose.yaml').read_text())['services']['worker']
        self.assertIn('worker_data:/data',worker['volumes'])
        self.assertEqual(worker['environment']['OFFICE_DESKTOP_HEIGHT'],'${OFFICE_DESKTOP_HEIGHT:-932}')
        self.assertIn('resize=scale&path=desktop/websockify',Path('assets/neurobro.js').read_text())
        self.assertNotIn('docker compose down',Path('deploy/update-portrait.sh').read_text())
        self.assertIn("'browser-dry-run'",Path('worker/runner.py').read_text())


class HandoffControllerTests(unittest.TestCase):
    def test_check_order_and_failed_close_never_opens_automation(self):
        for fail in (False,True):
            events=[]
            class Fake:
                context=None
                def desktop_active(self):return False
                def prepare_check(self):
                    events.append('manual-stop-and-lock-release')
                    if fail:raise RuntimeError('MANUAL_BROWSER_NOT_STOPPED')
                def open(self):events.append('playwright-open-same-profile')
                def refresh(self):events.append('refresh')
                def inspect(self):events.append('inspect');return 'CONNECTED'
                def close(self):events.append('close')
            c=SessionController(Fake(),'https://worker.example')
            try:
                c.submit('check');end=time.monotonic()+2
                while c.snapshot()['busy'] and time.monotonic()<end:time.sleep(.005)
                result=c.snapshot()
                self.assertFalse(result['busy']);self.assertFalse(result['live_enabled'])
                if fail:self.assertNotIn('playwright-open-same-profile',events)
                else:
                    self.assertEqual(events,['manual-stop-and-lock-release','playwright-open-same-profile','inspect','close'])
                    self.assertEqual(result['status'],'CONNECTED')
            finally:c.close()

if __name__=='__main__':unittest.main()
