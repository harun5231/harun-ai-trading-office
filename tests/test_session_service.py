"""API/session tests use a labeled stub browser, never claim real Neurobro login."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from worker.session_service import SessionBrowser, SessionController, handler, public_origin

READ='r'*40;CONTROL='c'*40;ORIGIN='https://harun5231.github.io'
class StubBrowser:
    def __init__(self):self.context=None;self.state='LOGIN_REQUIRED';self.opens=0;self.closes=0;self.manual=False
    def login(self):self.open();self.manual=True;return 'LOGIN_IN_PROGRESS'
    def prepare_check(self):self.manual=False
    def desktop_active(self):return self.manual
    def open(self):self.context=object();self.opens+=1
    def refresh(self):pass
    def inspect(self):return self.state
    def close(self):self.context=None;self.manual=False;self.closes+=1

class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.browser=StubBrowser()
        self.controller=SessionController(self.browser,'https://worker.example')
    def tearDown(self):self.controller.close();self.tmp.cleanup()
    def complete(self,action):
        self.assertTrue(self.controller.submit(action))
        end=time.monotonic()+2
        while self.controller.snapshot()['busy'] and time.monotonic()<end:time.sleep(.005)
        self.assertFalse(self.controller.snapshot()['busy']);return self.controller.snapshot()
    def test_login_is_browser_observation_not_connected_flag(self):
        data=self.complete('login');self.assertEqual(data['status'],'LOGIN_IN_PROGRESS')
        self.assertTrue(data['takeover_url'].startswith('https://worker.example/desktop/'))
        self.assertEqual(self.browser.opens,1);self.assertIsNotNone(self.browser.context)
    def test_check_valid_commits_profile_and_releases_browser(self):
        self.browser.state='CONNECTED';data=self.complete('check')
        self.assertEqual(data['status'],'CONNECTED');self.assertIsNone(data['takeover_url'])
        self.assertEqual(self.browser.closes,1)
        self.assertNotIn('cookie',json.dumps(data));self.assertFalse(data['live_enabled'])
    def test_expired_session_and_cloudflare(self):
        self.browser.state='CONNECTED';self.complete('check')
        self.browser.state='LOGIN_REQUIRED';self.assertEqual(self.complete('check')['status'],'SESSION_EXPIRED')
        self.browser.state='CLOUDFLARE_REQUIRED';self.assertEqual(self.complete('check')['status'],'CLOUDFLARE_REQUIRED')
        self.assertIsNone(self.browser.context)
        self.assertIsNone(self.controller.snapshot()['takeover_url'])
    def test_stale_connected_is_not_trusted(self):
        self.browser.state='CONNECTED';self.complete('check');self.controller.checked=time.time()-61
        self.assertEqual(self.controller.snapshot()['status'],'DISCONNECTED')
        self.assertTrue(self.controller.snapshot()['stale'])
    def test_origin_rejects_credentials_paths_and_http(self):
        for url in ('http://worker.test','https://secret@worker.test','https://worker.test/a','https://worker.test/?token=x'):
            with self.assertRaises(ValueError):public_origin(url)
    def test_browser_errors_sanitized(self):
        self.browser.open=lambda:(_ for _ in ()).throw(RuntimeError('cookie=secret'))
        data=self.complete('login');self.assertEqual(data['status'],'DISCONNECTED')
        self.assertNotIn('secret',json.dumps(data))
    def test_single_inflight_action(self):
        wait=threading.Event();original=self.browser.open
        self.browser.open=lambda:(wait.wait(1),original())
        self.controller.submit('login');self.assertFalse(self.controller.submit('check'));wait.set()
    def test_uses_same_profile_lock_as_screening(self):
        import fcntl
        directory=Path(self.tmp.name)
        lock=(directory/'worker.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            browser=SessionBrowser(directory,{})
            with self.assertRaisesRegex(RuntimeError,'WORKER_BUSY'):browser.open()
            self.assertEqual(browser.profile,directory/'browser-profile')
        finally:lock.close()

class SessionAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.controller=SessionController(StubBrowser(),'https://worker.example')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.controller,Path(self.tmp.name)/'snapshot.json',READ,CONTROL,ORIGIN))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.controller.close();self.tmp.cleanup()
    def call(self,path,method='GET',token=CONTROL,origin=ORIGIN,body=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        headers={'Authorization':'Bearer '+token,'Origin':origin}
        connection.request(method,path,body,headers);response=connection.getresponse()
        result=(response.status,dict(response.getheaders()),json.loads(response.read()));connection.close();return result
    def test_status_requires_auth_no_secrets_and_no_store(self):
        self.assertEqual(self.call('/neurobro/status',token='bad')[0],403)
        code,headers,data=self.call('/neurobro/status',token=READ)
        self.assertEqual(code,200);self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(data['status'],'DISCONNECTED')
        self.assertNotIn(CONTROL,json.dumps(data));self.assertNotIn(READ,json.dumps(data))
    def test_read_only_token_cannot_open_or_check(self):
        for action in ('login','check'):
            self.assertEqual(self.call('/neurobro/'+action,'POST',token=READ)[0],403)
    def test_origin_and_body_rejected(self):
        self.assertEqual(self.call('/neurobro/login','POST',origin='https://evil.test')[0],403)
        self.assertEqual(self.call('/neurobro/login','POST',body='{"password":"no"}')[0],400)
        self.assertEqual(self.call('/neurobro/login?token=x','POST')[0],404)
    def test_real_job_accepted_and_get_does_not_login(self):
        self.assertEqual(self.call('/neurobro/login')[0],404)
        self.assertEqual(self.call('/neurobro/login','POST')[0],202)
    def test_no_prompt_trade_or_remote_command_endpoints(self):
        for action in ('screen','trade','execute','cookies','resume'):
            self.assertEqual(self.call('/neurobro/'+action,'POST')[0],404)
    def test_distinct_control_token_required(self):
        with self.assertRaises(ValueError):handler(self.controller,'unused',READ,READ,ORIGIN)

if __name__=='__main__':unittest.main()
