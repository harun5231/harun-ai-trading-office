"""Local, synthetic UI and adversarial privacy tests; never a real login claim."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import test_screening_browser as offline
from worker.semantic_inventory import discover_phase2,observe,classify
from worker.selector_discovery import commit_verified

HTML='''<main role="application" data-authenticated="true"><form><textarea placeholder="Ask Neurobro anything..."></textarea><button type="submit" aria-label="Send chat">Send</button><input type="file"></form><button aria-label="login" hidden>Login</button><iframe hidden src="https://challenges.cloudflare.com/test?secret=HIDDEN_TOKEN"></iframe><div role="status" aria-label="chat loading" hidden></div></main>'''

@unittest.skipUnless(os.getenv('HARUN_BROWSER_TESTS')=='1','Offline browser opt-in')
class InventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):offline.BrowserScreeningTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.page=self.context.new_page()
        self.page.route('**/*',lambda r:r.fulfill(content_type='text/html',body=HTML if r.request.url=='https://app.neurobro.ai/' else '<html></html>'))
        self.page.goto('https://app.neurobro.ai/')
    def tearDown(self):self.page.close()
    def test_observed_attributes_generate_verified_core_without_logout(self):
        result=discover_phase2(self.page)
        self.assertEqual(result['state'],'AUTHENTICATED')
        for key in ('composer','send','authenticated','login_required','captcha','loading'):
            self.assertEqual(result['status'][key],'VERIFIED',key)
        self.assertIn('Ask Neurobro anything',result['selectors']['composer'])
        self.assertIn('[data-authenticated="true"]',result['selectors']['authenticated'])
        self.assertTrue(any('FORM > TEXTAREA' in row['fingerprint'] for row in result['inventory']))
    def test_private_attributes_content_and_values_never_escape(self):
        secrets=['alice@example.invalid','+628123456789','Bearer topsecret','eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature',
                 '550e8400-e29b-41d4-a716-446655440000','https://example.invalid/?token=secret',
                 'Alice Example','ABCD1234SECRETKEY','SENSITIVE_CHAT_CONTENT','PRIVATE_INPUT_VALUE','HIDDEN_TOKEN']
        self.page.evaluate('''secrets=>{
          const form=document.querySelector('form');
          for(const secret of secrets){const b=document.createElement('button');b.setAttribute('aria-label',secret);b.setAttribute('data-testid',secret);b.textContent=secret;form.append(b);}
          document.querySelector('textarea').value='PRIVATE_INPUT_VALUE';
          document.querySelector('main').insertAdjacentHTML('beforeend','<div role="log"><article data-message-author-role="assistant" aria-label="send your password">SENSITIVE_CHAT_CONTENT</article></div>');
        }''',secrets)
        result=json.dumps(discover_phase2(self.page))
        for secret in secrets:self.assertNotIn(secret,result)
        self.assertNotIn('send your password',result)
        self.assertTrue(any(r['redacted_attributes'] for r in observe(self.page)['inventory']))
    def test_probe_never_reads_sensitive_properties_or_performs_actions(self):
        self.page.evaluate('''()=>{
          const fail=()=>{throw Error('FORBIDDEN_READ');};
          for(const name of ['cookie'])Object.defineProperty(document,name,{get:fail});
          for(const name of ['localStorage','sessionStorage'])Object.defineProperty(window,name,{get:fail});
          for(const [proto,names] of [[Element.prototype,['innerHTML','outerHTML','textContent']],[HTMLElement.prototype,['innerText']],[HTMLInputElement.prototype,['value']],[HTMLTextAreaElement.prototype,['value']]])for(const name of names)Object.defineProperty(proto,name,{get:fail});
          HTMLElement.prototype.click=fail;HTMLFormElement.prototype.submit=fail;
        }''')
        self.assertEqual(discover_phase2(self.page)['state'],'AUTHENTICATED')
    def test_generic_shell_is_candidate_not_authentication(self):
        self.page.locator('main').evaluate('(e)=>e.removeAttribute("data-authenticated")')
        result=discover_phase2(self.page)
        self.assertEqual(result['status']['authenticated'],'CANDIDATE')
        self.assertEqual(result['state'],'UNVERIFIED')
    def test_ambiguous_editable_controls_fail_closed(self):
        self.page.evaluate("document.querySelector('form').insertAdjacentHTML('beforeend','<textarea placeholder=Message></textarea>')")
        self.assertEqual(discover_phase2(self.page)['status']['composer'],'AMBIGUOUS')
    def test_unrelated_submit_does_not_prove_send(self):
        self.page.evaluate("document.querySelector('main').append(document.querySelector('button[type=submit]'))")
        self.assertEqual(discover_phase2(self.page)['status']['send'],'CANDIDATE')
    def test_truncated_inventory_cannot_write_config(self):
        self.page.evaluate("document.querySelector('main').insertAdjacentHTML('beforeend','<button></button>'.repeat(210))")
        result=discover_phase2(self.page);self.assertTrue(result['truncated'])
        self.assertNotEqual(result['status']['composer'],'VERIFIED')
    def test_visible_challenge_or_login_remains_readonly(self):
        self.page.locator('iframe').evaluate('(e)=>e.hidden=false')
        self.assertEqual(discover_phase2(self.page)['state'],'CLOUDFLARE_REQUIRED')
        self.page.locator('iframe').evaluate('(e)=>e.hidden=true')
        self.page.locator('[aria-label=login]').evaluate('(e)=>e.hidden=false')
        self.assertEqual(discover_phase2(self.page)['state'],'LOGIN_REQUIRED')
    def test_message_lifecycle_not_verified_from_metadata_alone(self):
        self.page.locator('main').evaluate('(e)=>e.insertAdjacentHTML("beforeend",\'<article data-message-author-role="assistant" data-state="complete"></article>\')')
        result=discover_phase2(self.page)
        self.assertEqual(result['status']['assistant_messages'],'CANDIDATE')
        self.assertEqual(result['status']['completed_response'],'CANDIDATE')
    def test_atomic_write_requires_six_verified_and_preserves_private_config(self):
        result=discover_phase2(self.page)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'browser.json';original={'neurobro':{},'binance':{'keep':True}}
            path.write_text(json.dumps(original));path.chmod(0o600)
            self.assertTrue(commit_verified(path,result,original))
            saved=json.loads(path.read_text());self.assertEqual(saved['binance'],original['binance'])
            self.assertIn('semantic-inventory-v2',saved['selectors_verified_on'])
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.page.locator('iframe').evaluate('(e)=>e.remove()')
            self.assertFalse(commit_verified(path,discover_phase2(self.page),saved))
    def test_custom_tags_and_unsafe_names_are_not_reflected_in_selector(self):
        self.page.locator('main').evaluate('(e)=>e.insertAdjacentHTML("beforeend",\'<private-account-john aria-label="Message" name="John Smith"></private-account-john>\')')
        result=json.dumps(discover_phase2(self.page))
        self.assertNotIn('private-account-john',result);self.assertNotIn('John Smith',result)

class Phase2DeploymentTests(unittest.TestCase):
    def test_existing_offline_overlay_runs_phase2(self):
        script=Path('deploy/discover-selectors.sh').read_text()
        self.assertIn('--phase2',script);self.assertIn('--network=none',script)
        self.assertIn('--pull never',script);self.assertNotIn('down -v',script)

if __name__=='__main__':unittest.main()
