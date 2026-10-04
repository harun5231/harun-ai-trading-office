"""Opt-in real Chromium tests on an OFFLINE mock UI, never the Neurobro service.
HARUN_BROWSER_TESTS=1 python -m unittest discover -s tests -v
Optional PLAYWRIGHT_CHROMIUM_EXECUTABLE for the local test runtime only.
"""
import os
import unittest
from worker.core import Review
from worker.prompts import SCREENING
from worker.screening import ChatScreening
from test_screening import CONFIG

HTML = '''<!doctype html><html><body>
<div id="authenticated">Signed in (TEST ONLY)</div>
<button id="new">New chat</button><textarea id="composer"></textarea><button id="send">Send</button>
<div id="messages"></div>
<script>
window.sends=0;
newButton=document.querySelector('#new');
newButton.onclick=()=>document.querySelector('#messages').replaceChildren();
document.querySelector('#send').onclick=()=>{
 window.sends++;
 const user=document.createElement('div');user.className='user';
 user.textContent=document.querySelector('#composer').value;
 document.querySelector('#messages').append(user);
 const reply=document.createElement('article');reply.className='assistant';
 reply.innerHTML='<div class="body" style="white-space:pre-wrap">1. ALPHAUSDT\\n2. BETAUSDT</div><span class="done">Complete</span>';
 document.querySelector('#messages').append(reply);
};
</script></body></html>'''

@unittest.skipUnless(os.getenv('HARUN_BROWSER_TESTS')=='1', 'Opt in to offline Chromium tests with HARUN_BROWSER_TESTS=1')
class BrowserScreeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw=sync_playwright().start()
        options={'headless':True}
        if os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):
            options.update(executable_path=os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE'],
                           args=['--no-sandbox','--disable-dev-shm-usage','--use-gl=angle','--use-angle=swiftshader','--no-zygote','--single-process'])
        cls.browser=cls.pw.chromium.launch(**options)
        cls.context=cls.browser.new_context()
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.page=self.context.new_page()
        # Every network request is fulfilled locally; no production login, cookies or prompt.
        self.page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=HTML))
        self.page.goto(CONFIG['url'])
        self.events=[]
        self.driver=ChatScreening(self.page,CONFIG,lambda state,msg:self.events.append(state),timeout=.35,response_timeout=2)
    def tearDown(self): self.page.close()
    def mutate(self,js): self.page.evaluate('()=>{'+js+'}')
    def test_exact_prompt_once_and_completed_answer(self):
        self.assertEqual(self.driver.run(SCREENING),'1. ALPHAUSDT\n2. BETAUSDT')
        self.assertEqual(self.page.locator('.user').inner_text(),SCREENING)
        self.assertEqual(self.page.evaluate('sends'),1)
        self.assertEqual(self.events,['WAITING_NEUROBRO','SCREENING_SENT','WAITING_RESPONSE'])
    def test_captcha_never_clicked_or_sent(self):
        self.mutate("document.body.insertAdjacentHTML('beforeend','<button id=captcha>Human verification</button>')")
        with self.assertRaisesRegex(Review,'NEEDS_LOGIN: CAPTCHA'): self.driver.run(SCREENING)
        self.assertEqual(self.page.evaluate('sends'),0)
    def test_login_required_before_send(self):
        self.mutate("document.body.insertAdjacentHTML('beforeend','<div id=login>Sign in</div>')")
        with self.assertRaisesRegex(Review,'NEEDS_LOGIN'): self.driver.run(SCREENING)
        self.assertEqual(self.page.evaluate('sends'),0)
    def test_initializing_page_times_out_without_send(self):
        self.mutate("document.body.insertAdjacentHTML('beforeend','<div id=loading>Initializing</div>')")
        with self.assertRaisesRegex(Review,'chatbot belum siap'): self.driver.run(SCREENING)
        self.assertEqual(self.page.evaluate('sends'),0)
    def test_selector_changed_no_send(self):
        self.mutate("document.querySelector('#composer').remove()")
        with self.assertRaisesRegex(Review,'chatbot belum siap'): self.driver.run(SCREENING)
        self.assertEqual(self.page.evaluate('sends'),0)
    def test_stale_reply_not_reused(self):
        self.mutate("document.querySelector('#messages').innerHTML='<article class=assistant>OLD</article>';newButton.onclick=()=>{}")
        with self.assertRaisesRegex(Review,'percakapan baru tidak kosong'): self.driver.run(SCREENING)
        self.assertEqual(self.page.evaluate('sends'),0)
    def test_prompt_modified_rejected(self):
        self.mutate("document.querySelector('#composer').oninput=e=>e.target.value+=' changed'")
        with self.assertRaisesRegex(Review,'isi prompt'): self.driver.run(SCREENING)
        self.assertEqual(self.page.evaluate('sends'),0)
    def test_expired_session_during_response(self):
        self.mutate("document.querySelector('#send').onclick=()=>{sends++;document.querySelector('#authenticated').remove()}")
        with self.assertRaisesRegex(Review,'NEEDS_LOGIN'): self.driver.run(SCREENING)
        self.assertEqual(self.page.evaluate('sends'),1)
    def test_unfinished_response_never_accepted_or_resent(self):
        self.mutate("const send=document.querySelector('#send');const old=send.onclick;send.onclick=()=>{old();document.querySelector('.done').remove()}")
        self.driver.response_timeout=.35
        with self.assertRaisesRegex(Review,'respons belum selesai'): self.driver.run(SCREENING)
        self.assertEqual(self.page.evaluate('sends'),1)
    def test_streaming_blocks_premature_completion(self):
        self.mutate("document.body.insertAdjacentHTML('beforeend','<div id=streaming style=display:none>Typing</div>');const send=document.querySelector('#send');const old=send.onclick;send.onclick=()=>{old();document.querySelector('#streaming').style.display='block'}")
        self.driver.response_timeout=.35
        with self.assertRaisesRegex(Review,'respons belum selesai'): self.driver.run(SCREENING)
    def test_extra_reply_is_ambiguous(self):
        self.mutate("const send=document.querySelector('#send');const old=send.onclick;send.onclick=()=>{old();document.querySelector('#messages').insertAdjacentHTML('beforeend','<article class=assistant>Unexpected</article>')}")
        with self.assertRaisesRegex(Review,'respons tidak tunggal'): self.driver.run(SCREENING)
    def test_mismatched_echo_rejected(self):
        self.mutate("const send=document.querySelector('#send');const old=send.onclick;send.onclick=()=>{old();document.querySelector('.user').textContent='different'}")
        with self.assertRaisesRegex(Review,'pesan terkirim tidak cocok'): self.driver.run(SCREENING)

    def test_pause_and_manual_resume_before_send(self):
        self.mutate("document.body.insertAdjacentHTML('beforeend','<div id=login>Sign in</div>')")
        def manual():
            self.assertEqual(self.page.evaluate('sends'),0)
            self.mutate("document.querySelector('#login').remove()")
            return True
        self.driver.handoff=manual
        self.driver.run(SCREENING)
        self.assertIn('PAUSED_NEEDS_LOGIN',self.events)
        self.assertEqual(self.page.evaluate('sends'),1)

    def test_pause_after_send_resumes_response_without_resending(self):
        self.mutate("const b=document.querySelector('#send');const old=b.onclick;b.onclick=()=>{old();document.body.insertAdjacentHTML('beforeend','<div id=login>Expired</div>')}")
        def manual():
            self.assertEqual(self.page.evaluate('sends'),1)
            self.mutate("document.querySelector('#login').remove()")
            return True
        self.driver.handoff=manual
        self.driver.run(SCREENING)
        self.assertIn('PAUSED_NEEDS_LOGIN',self.events)
        self.assertEqual(self.page.evaluate('sends'),1)

    def test_unresolved_verification_stays_paused(self):
        from worker.screening import PausedNeedsLogin
        self.mutate("document.body.insertAdjacentHTML('beforeend','<div id=captcha>Verify</div>')")
        self.driver.handoff=lambda:True
        with self.assertRaises(PausedNeedsLogin):self.driver.run(SCREENING)
        self.assertEqual(self.events[-1],'PAUSED_NEEDS_LOGIN')
        self.assertEqual(self.page.evaluate('sends'),0)

    def test_valid_session_does_not_request_handoff(self):
        def unexpected():raise AssertionError('Valid session must not ask for login')
        self.driver.handoff=unexpected
        self.driver.run(SCREENING)
        self.assertNotIn('PAUSED_NEEDS_LOGIN',self.events)

    def test_manual_send_during_pause_does_not_duplicate(self):
        self.mutate("document.querySelector('#composer').oninput=()=>document.querySelector('#authenticated').style.display='none'")
        def manual():
            self.mutate("document.querySelector('#authenticated').style.display='block';document.querySelector('#send').click()")
            return True
        self.driver.handoff=manual
        with self.assertRaisesRegex(Review,'dilarang kirim ulang'):self.driver.run(SCREENING)
        self.assertEqual(self.page.evaluate('sends'),1)

    def test_persistent_profile_retains_synthetic_session(self):
        import tempfile,time
        from worker.screening import private_profile
        options={'headless':True}
        if os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):
            options.update(executable_path=os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE'],
                           args=['--no-sandbox','--disable-dev-shm-usage','--no-zygote','--single-process'])
        with tempfile.TemporaryDirectory() as d:
            profile=private_profile(d)
            context=self.pw.chromium.launch_persistent_context(str(profile),**options)
            context.add_cookies([{'name':'offline_test','value':'synthetic_only',
                                 'url':'https://session-test.invalid','expires':time.time()+3600}])
            context.close()
            context=self.pw.chromium.launch_persistent_context(str(profile),**options)
            try:
                self.assertEqual(context.cookies('https://session-test.invalid')[0]['value'],'synthetic_only')
            finally:context.close()

    def test_session_service_inspects_visible_browser_state(self):
        import tempfile
        from worker.session_service import SessionBrowser
        with tempfile.TemporaryDirectory() as d:
            browser=SessionBrowser(d,{'selectors_verified_on':'TEST ONLY','neurobro':CONFIG})
            browser.page=self.page
            self.assertEqual(browser.inspect(),'CONNECTED')
            self.mutate("document.body.insertAdjacentHTML('beforeend','<div id=captcha>Verification</div>')")
            self.assertEqual(browser.inspect(),'CLOUDFLARE_REQUIRED')
            self.mutate("document.querySelector('#captcha').remove();document.body.insertAdjacentHTML('beforeend','<div id=login>Login</div>')")
            self.assertEqual(browser.inspect(),'LOGIN_REQUIRED')
            self.mutate("document.querySelector('#login').remove();document.body.insertAdjacentHTML('beforeend','<div id=loading>Loading</div>')")
            self.assertEqual(browser.inspect(),'LOGIN_IN_PROGRESS')
            browser.config={}
            self.assertEqual(browser.inspect(),'DISCONNECTED')

    def test_dashboard_session_controls_and_fail_closed_network(self):
        import json
        from pathlib import Path
        from datetime import datetime,timezone
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.set_content('<button id=menuToggle></button><div id=menuDrawer><div class=drawer-note></div></div><div id=infoPanel><h2 id=panelTitle></h2><div id=panelContent></div><button id=panelClose></button></div><button id=workflowMenu></button><button data-view=office></button>')
        calls=[];status={'value':'DISCONNECTED'}
        def api(route):
            calls.append((route.request.method,route.request.url))
            if route.request.method=='OPTIONS':
                route.fulfill(status=204,headers={'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'Authorization','Access-Control-Allow-Methods':'GET, POST'});return
            self.assertEqual(route.request.headers.get('authorization'),'Bearer '+'t'*40)
            if route.request.method=='POST':status['value']='LOGIN_IN_PROGRESS'
            data={'status':status['value'],'mode':'DRY_RUN','live_enabled':False,'busy':False,'error':None,'stale':False,'takeover_url':None,'checked_at':datetime.now(timezone.utc).isoformat()}
            route.fulfill(status=200,content_type='application/json',headers={'Access-Control-Allow-Origin':'*'},body=json.dumps(data))
        self.page.route('https://worker.test/**',api)
        self.page.add_script_tag(path=str(Path('assets/neurobro.js').resolve()))
        self.page.click('#neurobroMenu')
        self.assertTrue(self.page.locator('#nbLogin').is_disabled())
        self.assertFalse(self.page.locator('#nbTakeover').is_visible())
        self.page.fill('#nbOrigin','https://worker.test');self.page.fill('#nbToken','t'*40)
        self.page.click('#nbConnect');self.page.wait_for_timeout(150)
        self.assertEqual(self.page.locator('#nbToken').input_value(),'')
        self.page.click('#nbLogin');self.page.wait_for_timeout(150)
        self.assertIn(('POST','https://worker.test/neurobro/login'),calls)
        self.assertNotEqual(self.page.locator('#nbStatus').inner_text(),'NEUROBRO CONNECTED')
        self.page.click('#nbCheck');self.page.wait_for_timeout(150)
        self.assertIn(('POST','https://worker.test/neurobro/check'),calls)
        self.page.click('#nbDisconnect')
        self.assertEqual(self.page.locator('#nbStatus').inner_text(),'DISCONNECTED')
        self.assertEqual(self.page.evaluate('localStorage.length'),0)
        self.assertFalse(any('token=' in url for _,url in calls))

if __name__=='__main__': unittest.main()
