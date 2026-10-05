"""Synthetic DOM only. These are NOT verified selectors of the real Neurobro UI."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from worker.selector_discovery import discover,commit_verified,atomic_private,REQUIRED,OPTIONAL
import test_screening_browser as offline

HTML='''<main><form><textarea aria-label="Message"></textarea><button type="submit" aria-label="Send message">Send</button><input type="file"></form><button aria-label="Log out">Logout</button><button aria-label="Sign in" hidden>Login</button><iframe hidden src="https://challenges.cloudflare.com/example"></iframe><div hidden role="progressbar" aria-label="Loading chat"></div><button aria-label="New chat">New</button><div role="log"><article data-message-author-role="assistant"><div data-testid="message-content">SENSITIVE SYNTHETIC MESSAGE</div><span data-status="complete"></span></article><article data-message-author-role="user">PRIVATE SYNTHETIC</article></div></main>'''

@unittest.skipUnless(os.getenv('HARUN_BROWSER_TESTS')=='1','Offline browser opt-in')
class DiscoveryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse the same offline Chromium launch options without inheriting its tests.
        offline.BrowserScreeningTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.page=self.context.new_page();self.requests=[]
        def route(r):
            self.requests.append((r.request.method,r.request.url));r.fulfill(content_type='text/html',body=HTML if r.request.url=='https://app.neurobro.ai/' else '<html></html>')
        self.page.route('**/*',route);self.page.goto('https://app.neurobro.ai/')
    def tearDown(self):self.page.close()
    def test_verified_core_semantics_no_input_send_or_sensitive_output(self):
        before=self.page.locator('textarea').input_value();count=len(self.requests)
        result=discover(self.page)
        self.assertEqual(result['state'],'AUTHENTICATED')
        self.assertTrue(all(result['status'][k]=='VERIFIED' for k in REQUIRED))
        self.assertEqual(result['status']['attachment_ready'],'UNVERIFIED')
        self.assertEqual(result['status']['streaming'],'UNVERIFIED')
        self.assertEqual(self.page.locator('textarea').input_value(),before)
        self.assertEqual(len(self.requests),count)
        self.assertNotIn('SENSITIVE',json.dumps(result));self.assertNotIn('PRIVATE SYNTHETIC',json.dumps(result))
    def test_ambiguous_composer_and_unrelated_send_rejected(self):
        self.page.evaluate("document.querySelector('main').insertAdjacentHTML('beforeend','<textarea aria-label=Message></textarea>')")
        self.assertEqual(discover(self.page)['status']['composer'],'UNVERIFIED')
        self.page.locator('textarea').nth(1).evaluate('(e)=>e.remove()')
        # Specific semantic locator may identify one composer, but unrelated controls do not.
        self.page.evaluate("document.querySelector('form').after(document.querySelector('form button'))")
        result=discover(self.page);self.assertEqual(result['status']['send'],'UNVERIFIED')
        self.assertEqual(result['status']['authenticated'],'UNVERIFIED')
    def test_unlabelled_textarea_is_not_assumed_to_be_chat(self):
        self.page.locator('textarea').evaluate('(e)=>e.removeAttribute("aria-label")')
        result=discover(self.page)
        self.assertEqual(result['status']['composer'],'UNVERIFIED')
        self.assertEqual(result['status']['authenticated'],'UNVERIFIED')
    def test_guest_composer_is_not_authentication(self):
        self.page.locator('[aria-label="Log out"]').evaluate('(e)=>e.remove()')
        self.assertEqual(discover(self.page)['status']['authenticated'],'UNVERIFIED')
    def test_absent_negative_states_are_unverified(self):
        self.page.locator('iframe,[aria-label="Sign in"],[aria-label="Loading chat"]').evaluate_all('(es)=>es.forEach(e=>e.remove())')
        result=discover(self.page)
        for key in ('captcha','login_required','loading'):self.assertEqual(result['status'][key],'UNVERIFIED')
    def test_visible_challenge_stops_without_interaction(self):
        self.page.locator('iframe').evaluate('(e)=>e.hidden=false')
        self.assertEqual(discover(self.page)['state'],'CLOUDFLARE_REQUIRED')
    def test_visible_login_is_login_required(self):
        self.page.locator('[aria-label="Sign in"]').evaluate('(e)=>e.hidden=false')
        self.assertEqual(discover(self.page)['state'],'LOGIN_REQUIRED')
    def test_messages_require_author_and_conversation_scope(self):
        self.page.locator('[role="log"]').evaluate('(e)=>e.removeAttribute("role")')
        result=discover(self.page)
        for key in ('assistant_messages','user_messages','response_text','completed_response'):self.assertEqual(result['status'][key],'UNVERIFIED')

class DiscoveryConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'browser.json'
        self.original={'selectors_verified_on':None,'neurobro':{},'binance':{'unchanged':True}}
        self.path.write_text(json.dumps(self.original));self.path.chmod(0o600)
    def tearDown(self):self.tmp.cleanup()
    def result(self):return {'state':'AUTHENTICATED','status':{k:'VERIFIED' if k in REQUIRED else 'UNVERIFIED' for k in REQUIRED+OPTIONAL},'selectors':{k:'synthetic-'+k for k in REQUIRED}}
    def test_atomic_verified_update_preserves_private_mode_and_other_config(self):
        self.assertTrue(commit_verified(self.path,self.result(),self.original))
        data=json.loads(self.path.read_text());self.assertEqual(data['binance'],self.original['binance'])
        self.assertTrue(data['selectors_verified_on']);self.assertEqual(self.path.stat().st_mode&0o777,0o600)
        self.assertEqual(data['neurobro']['streaming'],'')
    def test_partial_or_challenge_does_not_change_active_config(self):
        for state,key in (('AUTHENTICATED','captcha'),('CLOUDFLARE_REQUIRED',None),('LOGIN_REQUIRED',None)):
            result=self.result();result['state']=state
            if key:result['status'][key]='UNVERIFIED'
            self.assertFalse(commit_verified(self.path,result,self.original))
            self.assertEqual(json.loads(self.path.read_text()),self.original)
    def test_replace_failure_keeps_original_and_removes_temporary(self):
        with patch('worker.selector_discovery.os.replace',side_effect=OSError):
            with self.assertRaises(OSError):atomic_private(self.path,{'new':True})
        self.assertEqual(json.loads(self.path.read_text()),self.original)
        self.assertEqual(list(self.path.parent.iterdir()),[self.path])
    def test_public_permissions_and_concurrent_edit_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):atomic_private(self.path,{})
        self.path.chmod(0o600);self.path.write_text('{}')
        with self.assertRaises(ValueError):commit_verified(self.path,self.result(),self.original)
    def test_local_deployment_no_pull_and_retains_volume(self):
        text=Path('deploy/discover-selectors.sh').read_text()
        self.assertIn('--network=none',text);self.assertIn('--pull never',text)
        self.assertIn('docker compose stop -t 90 worker',text)
        self.assertIn('source=$data,target=/data',text)
        self.assertNotIn('down -v',text);self.assertNotIn('volume rm',text)

if __name__=='__main__':unittest.main()
