"""Fault injection at every stage. No real Neurobro login or VPS claims."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from worker.discovery_preflight import DiscoveryJob
from worker import selector_diagnostic as diagnostic
from worker.selector_discovery import REQUIRED, OPTIONAL

SECRET='https://example.invalid/?token=SECRET alice@example.org PRIVATE_CHAT password=SECRET'
METHODS={'PRIVATE_CONFIG_CHECK':'private_check','SERVICE_LOCK_ACQUIRE':'service_lock',
 'PROFILE_OWNER_ACQUIRE':'owner_lock','STALE_SINGLETON_RECOVERY':'recovery','PROFILE_RELEASE_CHECK':'released',
 'PLAYWRIGHT_START':'start','PERSISTENT_CONTEXT_OPEN':'context','NEUROBRO_NAVIGATION':'navigate',
 'DOM_DISCOVERY':'discover','BROWSER_CLOSE':'close_browser'}
TARGETS={'EVIDENCE_SANITIZE':'worker.discovery_preflight.evidence.sanitize',
 'EVIDENCE_WRITE':'worker.discovery_preflight.evidence.write_private',
 'CONFIG_COMMIT':'worker.discovery_preflight.commit_session_verified'}
RESULT={'version':'structural-readonly-v3','state':'UNVERIFIED','selectors':{},
 'status':{k:'UNVERIFIED' for k in REQUIRED+OPTIONAL},'evidence':{k:{'status':'UNVERIFIED','evidence':[],'candidates':[]} for k in REQUIRED+OPTIONAL},
 'inventory':[],'truncated':False,'session_check_ready':False,'screening_ready':False}

class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.config=self.root/'browser.json'
        self.config.write_text('{"neurobro":{},"binance":{"mode":"DRY_RUN"}}');self.config.chmod(0o600)
        self.profile=self.root/'data/browser/browser-profile';self.profile.mkdir(parents=True,mode=0o700)
        self.job=DiscoveryJob(self.config,self.root/'data')
        self.diagnostic=self.root/'selector-discovery-diagnostic.json'
    def run_job(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):rc=self.job.run()
        return rc,json.loads(out.getvalue())
    def mocks(self,stack):
        mocks={}
        for stage,method in METHODS.items():
            if stage=='PRIVATE_CONFIG_CHECK':continue
            mocks[stage]=stack.enter_context(patch.object(self.job,method,return_value=RESULT if method=='discover' else None))
        return mocks
    def test_every_stage_failure_has_safe_code_and_persistent_diagnostic(self):
        for stage,reason in diagnostic.REASONS.items():
            with self.subTest(stage=stage),contextlib.ExitStack() as stack:
                self.job=DiscoveryJob(self.config,self.root/'data');self.diagnostic.unlink(missing_ok=True)
                mocks=self.mocks(stack)
                if stage=='PRIVATE_CONFIG_CHECK':stack.enter_context(patch.object(self.job,'private_check',side_effect=RuntimeError(SECRET)))
                elif stage in mocks:mocks[stage].side_effect=RuntimeError(SECRET)
                else:stack.enter_context(patch(TARGETS[stage],side_effect=RuntimeError(SECRET)))
                rc,out=self.run_job();self.assertEqual(rc,1);self.assertEqual(out['stage'],stage);self.assertEqual(out['reason'],reason)
                self.assertNotIn(SECRET,json.dumps(out));self.assertFalse(out['config_updated'])
                self.assertEqual(json.loads(self.config.read_text())['binance']['mode'],'DRY_RUN')
                if stage!='PRIVATE_CONFIG_CHECK':
                    saved=json.loads(self.diagnostic.read_text());self.assertEqual(saved['stage'],stage);self.assertNotIn(SECRET,self.diagnostic.read_text())
                    self.assertEqual(self.diagnostic.stat().st_mode&0o777,0o600)
                    self.assertEqual(self.diagnostic.stat().st_uid,self.config.stat().st_uid)
                if stage in ('SERVICE_LOCK_ACQUIRE','PROFILE_OWNER_ACQUIRE','STALE_SINGLETON_RECOVERY','PROFILE_RELEASE_CHECK'):
                    mocks['PLAYWRIGHT_START'].assert_not_called()
    def test_error_preserves_previous_evidence(self):
        evidence=self.root/'selector-discovery.json';evidence.write_text('{"state":"UNVERIFIED"}');evidence.chmod(0o600)
        with contextlib.ExitStack() as stack:
            mocks=self.mocks(stack);mocks['NEUROBRO_NAVIGATION'].side_effect=RuntimeError(SECRET)
            self.run_job()
        self.assertEqual(evidence.read_text(),'{"state":"UNVERIFIED"}')
    def test_readonly_private_mount_stops_before_browser(self):
        with patch('worker.discovery_preflight.tempfile.NamedTemporaryFile',side_effect=OSError(30,SECRET)),patch.object(self.job,'start') as start:
            rc,out=self.run_job()
        self.assertEqual(out['reason'],'PRIVATE_DESTINATION_UNWRITABLE');self.assertFalse(out['diagnostic_saved']);start.assert_not_called()
    def test_unsafe_config_and_missing_profile(self):
        self.config.chmod(0o644)
        rc,out=self.run_job();self.assertEqual(out['reason'],'PRIVATE_CONFIG_UNSAFE')
        self.config.chmod(0o600);self.profile.rmdir()
        rc,out=self.run_job();self.assertEqual(out['reason'],'PROFILE_DIRECTORY_UNAVAILABLE');self.assertTrue(out['diagnostic_saved'])
    def test_real_service_lock_excludes_discovery(self):
        import fcntl
        with (self.profile.parent/'session-service.lock').open('a') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with patch.object(self.job,'start') as start:
                rc,out=self.run_job();start.assert_not_called()
        self.assertEqual(out['stage'],'SERVICE_LOCK_ACQUIRE');self.assertEqual(out['reason'],'WORKER_BUSY')
    def test_real_owner_lock_excludes_discovery(self):
        import fcntl
        with (self.profile/'.office-owner.lock').open('a') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with patch.object(self.job,'start') as start:
                rc,out=self.run_job();start.assert_not_called()
        self.assertEqual(out['stage'],'PROFILE_OWNER_ACQUIRE');self.assertEqual(out['reason'],'WORKER_BUSY')
    def test_quiet_proof_failure_prevents_recovery_and_browser(self):
        with patch('worker.discovery_preflight.prove_quiet',side_effect=RuntimeError(SECRET)),patch.object(self.job,'start') as start:
            rc,out=self.run_job();start.assert_not_called()
        self.assertEqual(out['stage'],'STALE_SINGLETON_RECOVERY')
        self.assertIsNone(json.loads(self.diagnostic.read_text())['browser_process_detected'])
    def test_diagnostic_write_failure_stays_short_and_safe(self):
        with contextlib.ExitStack() as stack:
            mocks=self.mocks(stack);mocks['NEUROBRO_NAVIGATION'].side_effect=RuntimeError(SECRET)
            stack.enter_context(patch('worker.selector_diagnostic.save',side_effect=RuntimeError(SECRET)))
            rc,out=self.run_job()
        self.assertFalse(out['diagnostic_saved']);self.assertEqual(out['reason'],'NAVIGATION_FAILED')
    def test_viewer_after_process_exit_and_atomic_replace(self):
        d=diagnostic.record('DOM_DISCOVERY','DOM_DISCOVERY_FAILED',self.profile)
        diagnostic.save(self.config,d);inode=self.diagnostic.stat().st_ino
        diagnostic.save(self.config,d);self.assertNotEqual(inode,self.diagnostic.stat().st_ino)
        r=subprocess.run([sys.executable,'-m','worker.selector_diagnostic','--path',str(self.diagnostic)],capture_output=True,text=True)
        self.assertEqual(r.returncode,0);self.assertEqual(json.loads(r.stdout),d)
        with patch('worker.selector_evidence.os.replace',side_effect=OSError(SECRET)):
            with self.assertRaises(OSError):diagnostic.save(self.config,d)
        self.assertEqual(json.loads(self.diagnostic.read_text()),d)
    def test_schema_rejects_all_extra_sensitive_keys_and_strings(self):
        d=diagnostic.record('DOM_DISCOVERY','DOM_DISCOVERY_FAILED',self.profile)
        for key in ('cookie','token','localStorage','sessionStorage','value','email','name','url','innerText','textContent','credential','command_line'):
            with self.subTest(key=key),self.assertRaises(ValueError):diagnostic.validate({**d,key:SECRET})
        for key in d:
            with self.subTest(key=key),self.assertRaises(ValueError):diagnostic.validate({**d,key:SECRET})
    def test_viewer_rejects_unsafe_file_without_echo(self):
        self.diagnostic.write_text(json.dumps({'cookie':SECRET}));self.diagnostic.chmod(0o600)
        r=subprocess.run([sys.executable,'-m','worker.selector_diagnostic','--path',str(self.diagnostic)],capture_output=True,text=True)
        self.assertEqual(r.returncode,1);self.assertNotIn(SECRET,r.stdout+r.stderr)
    def test_symlink_never_overwrites_evidence(self):
        self.diagnostic.symlink_to(self.config)
        rc,out=self.run_job();self.assertEqual(out['reason'],'PRIVATE_CONFIG_UNSAFE');self.assertTrue(self.diagnostic.is_symlink())
    def test_worker_mount_readonly_job_mount_writable_offline(self):
        compose=Path('compose.yaml').read_text();script=Path('deploy/discover-selectors.sh').read_text()
        self.assertIn('target: /private\n        read_only: true',compose)
        self.assertIn('--mount "type=bind,source=$private,target=/private"',script)
        self.assertIn('{{.State.Running}}',script);self.assertIn('--network=none',script)
        self.assertNotIn('down -v',script)
    def test_success_writes_diagnostic_without_claiming_verified(self):
        with contextlib.ExitStack() as stack:
            self.mocks(stack);rc,out=self.run_job()
        self.assertEqual(rc,2);self.assertTrue(out['evidence_saved']);self.assertTrue(out['diagnostic_saved'])
        self.assertFalse(out['config_updated']);self.assertEqual(out['reason'],'COMPLETED')
    def test_uncertain_shutdown_retains_locks_until_job_exit(self):
        from unittest.mock import Mock
        b=Mock();b.context=None;b.pw=None;b.close.side_effect=RuntimeError(SECRET)
        self.job.browser=b;self.job.browser_started=True
        with self.assertRaises(RuntimeError):self.job.close_browser()
        self.assertTrue(self.job.browser_started)
        with contextlib.ExitStack() as stack:
            mocks=self.mocks(stack);mocks['BROWSER_CLOSE'].side_effect=RuntimeError(SECRET)
            release=stack.enter_context(patch.object(self.job,'release'))
            rc,out=self.run_job();release.assert_not_called()
        self.assertEqual(out['reason'],'BROWSER_CLOSE_FAILED')
        self.assertTrue(json.loads(self.diagnostic.read_text())['cleanup_failed'])
    def test_runtime_order_has_all_preflight_before_playwright(self):
        order=[]
        with contextlib.ExitStack() as stack:
            self.mocks(stack)
            original=self.job.step
            def step(stage,action):order.append(stage);return original(stage,action)
            stack.enter_context(patch.object(self.job,'step',side_effect=step))
            self.run_job()
        self.assertEqual(order[:6],list(diagnostic.REASONS)[:6])
        self.assertLess(order.index('PROFILE_RELEASE_CHECK'),order.index('PLAYWRIGHT_START'))
        self.assertLess(order.index('EVIDENCE_SANITIZE'),order.index('EVIDENCE_WRITE'))
        self.assertLess(order.index('EVIDENCE_WRITE'),order.index('CONFIG_COMMIT'))
