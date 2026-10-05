"""Synthetic offline evidence, privacy, structural and persistence regressions."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import test_screening_browser as offline
import test_semantic_inventory as phase2
from worker.structural_discovery import discover_phase3
from worker.selector_evidence import save, sanitize
from worker.selector_discovery import commit_session_verified

@unittest.skipUnless(os.getenv('HARUN_BROWSER_TESTS')=='1','Offline browser opt-in')
class StructuralTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): offline.BrowserScreeningTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.page=self.context.new_page()
        self.page.route('**/*',lambda r:r.fulfill(content_type='text/html',body=phase2.HTML if r.request.url=='https://app.neurobro.ai/' else '<html></html>'))
        self.page.goto('https://app.neurobro.ai/')
    def tearDown(self): self.page.close()
    def result(self): return discover_phase3(self.page)
    def test_readiness_separate_and_sanitizable(self):
        r=self.result();self.assertTrue(r['session_check_ready']);self.assertFalse(r['screening_ready']);self.assertEqual(sanitize(r),r)
    def test_unrelated_editable_and_upload_disambiguated(self):
        self.page.evaluate("document.querySelector('main').insertAdjacentHTML('beforeend','<form><textarea placeholder=Search></textarea><input type=file></form>')")
        r=self.result();self.assertEqual(r['status']['composer'],'VERIFIED');self.assertEqual(r['status']['upload'],'VERIFIED');self.assertIn('form:has(',r['selectors']['upload'])
        # A globally unique structural selector binds upload to the verified form.
    def test_equally_qualified_composers_ambiguous(self):
        self.page.evaluate("document.querySelector('form').insertAdjacentHTML('beforeend','<textarea placeholder=Message></textarea>')")
        self.assertEqual(self.result()['status']['composer'],'AMBIGUOUS')
    def test_send_outside_form_not_verified(self):
        self.page.evaluate("document.querySelector('main').append(document.querySelector('button[type=submit]'))")
        self.assertFalse(self.result()['session_check_ready'])
    def test_two_send_controls_ambiguous(self):
        self.page.evaluate("document.querySelector('form').insertAdjacentHTML('beforeend','<button aria-label=Send></button>')")
        r=self.result();self.assertFalse(r['session_check_ready']);self.assertEqual(r['status']['send'],'AMBIGUOUS')
    def test_composite_application_positive(self):
        self.page.evaluate('''()=>{let m=document.querySelector('main');m.removeAttribute('data-authenticated');m.setAttribute('aria-label','Neurobro chat');m.insertAdjacentHTML('beforeend','<div role="log"></div><button aria-label="New chat"></button>');}''')
        self.assertTrue(self.result()['session_check_ready'])
    def test_generic_guest_shell_not_proof(self):
        self.page.locator('main').evaluate('(e)=>e.removeAttribute("data-authenticated")')
        self.assertFalse(self.result()['session_check_ready'])
    def test_absent_challenge_definition_is_not_challenge(self):
        self.page.locator('iframe').evaluate('(e)=>e.remove()')
        r=self.result();self.assertEqual(r['status']['captcha'],'VERIFIED');self.assertEqual(r['evidence']['captcha']['match_count'],0)
        self.assertTrue(r['session_check_ready'])
    def test_missing_login_definition_fail_closed(self):
        self.page.locator('[aria-label=login]').evaluate('(e)=>e.remove()')
        self.assertFalse(self.result()['session_check_ready'])
    def test_visible_challenge_blocks(self):
        self.page.locator('iframe').evaluate('(e)=>e.hidden=false')
        r=self.result();self.assertEqual(r['state'],'CLOUDFLARE_REQUIRED');self.assertFalse(r['session_check_ready'])
    def test_no_forbidden_reads_or_actions(self):
        phase2.InventoryTests.test_probe_never_reads_sensitive_properties_or_performs_actions(self)
        self.assertTrue(self.result()['session_check_ready'])
    def test_atomic_evidence_config_and_restart(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'browser.json';original={'neurobro':{},'binance':{'mode':'DRY_RUN'}}
            p.write_text(json.dumps(original));p.chmod(0o600)
            r=self.result();dest=Path(save(p,r));self.assertEqual(dest.stat().st_mode&0o777,0o600)
            self.assertEqual(dest.stat().st_uid,p.stat().st_uid)
            self.assertTrue(commit_session_verified(p,r,original));config=json.loads(p.read_text())
            self.assertIsNone(config['selectors_verified_on']);self.assertEqual(config['binance'],original['binance'])
            # A new process can read saved evidence after the producer exits.
            proc=subprocess.run([sys.executable,'-m','worker.selector_evidence','--path',str(dest)],capture_output=True,text=True)
            self.assertEqual(proc.returncode,0);self.assertEqual(json.loads(proc.stdout),r)
            inode=dest.stat().st_ino;save(p,r);self.assertNotEqual(inode,dest.stat().st_ino)
    def test_failed_replace_keeps_both_files(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'browser.json';p.write_text('{}');p.chmod(0o600);r=self.result();dest=Path(save(p,r));before=dest.read_bytes()
            with patch('worker.selector_evidence.os.replace',side_effect=OSError('disk')):
                with self.assertRaises(OSError):save(p,r)
            self.assertEqual(dest.read_bytes(),before);self.assertEqual(p.read_text(),'{}')
    def test_session_only_connected_requires_fresh_proof(self):
        from worker.session_service import SessionBrowser
        with tempfile.TemporaryDirectory() as d:
            b=SessionBrowser(Path(d)/'browser',{'neurobro':{},'selectors_verified_on':None,
                'session_selector_verification':{'version':'structural-readonly-v3','session_check_ready':True}})
            b.page=self.page
            self.assertEqual(b.inspect(),'CONNECTED')
            self.page.locator('main').evaluate('(e)=>e.removeAttribute("data-authenticated")')
            self.assertEqual(b.inspect(),'DISCONNECTED')
    def test_scoped_upload_selector_is_sanitizable(self):
        self.page.evaluate("document.querySelector('main').insertAdjacentHTML('beforeend','<form><input type=file></form>')")
        r=self.result();self.assertEqual(sanitize(r),r)
        self.assertEqual(self.page.locator(r['selectors']['upload']).count(),1)
    def test_unverified_cannot_commit(self):
        r=self.result();r['status']['composer']='AMBIGUOUS'
        self.assertFalse(commit_session_verified('/nonexistent',r,{}))

class EvidencePrivacyTests(unittest.TestCase):
    def test_sensitive_keys_rejected_recursively(self):
        for key in ('value','innerText','textContent','cookie','token','authorization','password','localStorage','sessionStorage','email','html','screenshot'):
            with self.subTest(key=key),self.assertRaises(ValueError):sanitize({'evidence':{key:'chat'}})
    def test_private_strings_rejected(self):
        for value in ('alice@example.org','+6281234567','Bearer abc','eyJ.eyJ.signature','550e8400-e29b-41d4-a716-446655440000','https://app.neurobro.ai/?token=x','Alice Example','PRIVATE_CHAT','<html>'):
            with self.subTest(value=value),self.assertRaises(ValueError):sanitize({'selector':value})
    def test_offline_and_durable_launcher(self):
        s=Path('deploy/discover-selectors.sh').read_text()
        self.assertIn('--phase3',s);self.assertIn('--network=none',s);self.assertNotIn('down -v',s)
        self.assertIn('systemd-run',Path('deploy/SELECTOR_DISCOVERY.md').read_text())
