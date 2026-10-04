"""Authenticated login control only. No credential fields, prompts or trading endpoints.
Playwright is owned by one actor thread; HTTP handlers never touch browser objects.
"""
import fcntl
import hmac
import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .screening import private_profile

STATES = {'DISCONNECTED','LOGIN_REQUIRED','LOGIN_IN_PROGRESS','CONNECTED',
          'SESSION_EXPIRED','CLOUDFLARE_REQUIRED'}


def public_origin(value):
    u=urlsplit(value)
    if (u.scheme!='https' or not u.hostname or u.username or u.password
            or u.path not in ('','/') or u.query or u.fragment):
        raise ValueError('Origin HTTPS tanpa path/kredensial wajib')
    return value.rstrip('/')


class SessionBrowser:
    """Same profile and process lock as the existing screening CLI."""
    def __init__(self, directory, config):
        self.directory=private_profile(directory)
        self.profile=private_profile(self.directory/'browser-profile')
        self.config=config;self.page=None;self.context=None;self.pw=None;self.lock=None
    def open(self):
        if self.context is not None:return
        self.lock=(self.directory/'worker.lock').open('a')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close();self.lock=None
            raise RuntimeError('WORKER_BUSY') from None
        try:
            from playwright.sync_api import sync_playwright
            self.pw=sync_playwright().start()
            self.context=self.pw.chromium.launch_persistent_context(str(self.profile),headless=False,accept_downloads=False)
            self.context.set_default_timeout(3000)
            self.page=self.context.new_page()
            self.page.goto('https://app.neurobro.ai/',wait_until='domcontentloaded',timeout=45000)
        except Exception:
            self.close();raise
    def visible(self, selector, scope=None):
        if not selector:return False
        loc=(scope or self.page).locator(selector)
        return any(loc.nth(i).is_visible() for i in range(loc.count()))
    def refresh(self):
        # Explicit CEK SESI revalidates the app against its server, without a prompt.
        # Do not interrupt a user still on an external identity-provider page.
        u=urlsplit(self.page.url)
        if u.scheme=='https' and u.hostname=='app.neurobro.ai':
            self.page.reload(wait_until='domcontentloaded',timeout=45000)
    def inspect(self):
        n=self.config.get('neurobro',{})
        # Inspect visible challenges only; never click/solve one.
        if self.visible('iframe[src*="challenges.cloudflare.com"]'):
            return 'CLOUDFLARE_REQUIRED'
        for frame in self.page.frames:
            if self.visible(n.get('captcha'),frame):return 'CLOUDFLARE_REQUIRED'
        u=urlsplit(self.page.url)
        if u.scheme!='https' or u.hostname!='app.neurobro.ai':return 'LOGIN_IN_PROGRESS'
        if self.visible(n.get('login_required')):return 'LOGIN_REQUIRED'
        required=('authenticated','login_required','captcha','loading','composer')
        if not self.config.get('selectors_verified_on') or not all(n.get(k) for k in required):
            return 'DISCONNECTED'  # No guessed login selector, no pretend success.
        if self.visible(n['loading']):return 'LOGIN_IN_PROGRESS'
        c=self.page.locator(n['composer'])
        if self.visible(n['authenticated']) and c.count()==1 and c.is_visible() and c.is_editable():
            return 'CONNECTED'
        return 'DISCONNECTED'
    def close(self):
        try:
            if self.context:self.context.close()
        finally:
            self.context=None;self.page=None
            try:
                if self.pw:self.pw.stop()
            finally:
                self.pw=None
                if self.lock:self.lock.close();self.lock=None


class SessionController:
    def __init__(self, browser, worker_origin):
        self.browser=browser;self.origin=public_origin(worker_origin)
        self.mu=threading.Lock();self.jobs=queue.Queue(maxsize=1)
        self.busy=False;self.state='DISCONNECTED';self.checked=0;self.ever_connected=False
        self.error=None;self.desktop=False
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def snapshot(self):
        with self.mu:
            stale=self.state=='CONNECTED' and time.time()-self.checked>60
            return {'status':'DISCONNECTED' if stale else self.state,
                    'checked_at':datetime.fromtimestamp(self.checked,timezone.utc).isoformat() if self.checked else None,
                    'stale':stale,'busy':self.busy,'error':self.error,
                    'takeover_url':self.origin+'/desktop/vnc.html#autoconnect=1&resize=remote&path=desktop/websockify' if self.desktop else None,
                    'mode':'DRY_RUN','live_enabled':False}
    def submit(self, action):
        if action not in ('login','check'):raise ValueError('Unknown action')
        with self.mu:
            if self.busy:return False
            self.busy=True;self.error=None
            self.state='LOGIN_IN_PROGRESS'
            self.jobs.put_nowait(action)
        return True
    def run(self):
        while True:
            action=self.jobs.get()
            if action is None:
                self.browser.close();return
            try:
                self.browser.open()
                if action=='check':self.browser.refresh()
                state=self.browser.inspect()
                # Login leaves a real headed browser for remote takeover. Check commits
                # a validated session to disk and frees the existing CLI's process lock.
                if action=='check' and state=='CONNECTED':self.browser.close()
                with self.mu:
                    if state=='LOGIN_REQUIRED' and self.ever_connected:state='SESSION_EXPIRED'
                    self.ever_connected |= state=='CONNECTED'
                    self.state=state;self.checked=time.time()
                    self.desktop=self.browser.context is not None
            except Exception as exc:
                try:self.browser.close()
                except Exception:pass
                with self.mu:
                    self.state='DISCONNECTED';self.desktop=False;self.checked=time.time()
                    self.error='WORKER_BUSY' if str(exc)=='WORKER_BUSY' else 'BROWSER_OR_CONFIG_ERROR'
            finally:
                with self.mu:self.busy=False
    def close(self):
        self.jobs.put(None);self.thread.join(timeout=55)


def handler(controller, snapshot_path, read_token, control_token, dashboard_origin):
    if min(len(read_token),len(control_token))<32 or hmac.compare_digest(read_token,control_token):
        raise ValueError('Dua token worker berbeda, masing-masing minimal 32 karakter, wajib')
    origin=public_origin(dashboard_origin)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,status,data=None):
            body=json.dumps(data or {}).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            if self.headers.get('Origin')==origin:
                self.send_header('Access-Control-Allow-Origin',origin);self.send_header('Vary','Origin')
            self.send_header('Access-Control-Allow-Headers','Authorization, Content-Type')
            self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def authorized(self,control=False):
            incoming=self.headers.get('Origin')
            if incoming is not None and incoming!=origin:return False
            auth=self.headers.get('Authorization','')
            return hmac.compare_digest(auth,'Bearer '+control_token) if control else (
                hmac.compare_digest(auth,'Bearer '+read_token) or hmac.compare_digest(auth,'Bearer '+control_token))
        def do_OPTIONS(self):
            if self.headers.get('Origin')!=origin:return self.reply(403)
            self.reply(200)
        def do_GET(self):
            if not self.authorized():return self.reply(403)
            if self.path=='/neurobro/status':return self.reply(200,controller.snapshot())
            if self.path=='/snapshot':
                try:return self.reply(200,json.loads(Path(snapshot_path).read_text()))
                except (OSError,ValueError):return self.reply(503,{'error':'SNAPSHOT_NOT_AVAILABLE'})
            self.reply(404)
        def do_POST(self):
            if not self.authorized(control=True):return self.reply(403)
            if self.path not in ('/neurobro/login','/neurobro/check'):return self.reply(404)
            # No user-supplied URL, selector, credential or command is accepted.
            if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length','0')!='0':
                self.close_connection=True;return self.reply(400,{'error':'BODY_NOT_ALLOWED'})
            if not controller.submit(self.path.rsplit('/',1)[-1]):return self.reply(409,{'error':'BUSY'})
            self.reply(202,controller.snapshot())
    return Handler


def main():
    import argparse
    os.umask(0o077)
    p=argparse.ArgumentParser(description='Private Neurobro session service; no orders or prompts')
    p.add_argument('--data-dir',default=str(Path.home()/'.local/state/harun-office'))
    p.add_argument('--config',required=True)
    p.add_argument('--port',type=int,default=8787)
    a=p.parse_args()
    repo=Path(__file__).resolve().parent.parent;config=Path(a.config).expanduser().resolve()
    if config==repo or repo in config.parents:raise SystemExit('Config harus di luar repository')
    directory=private_profile(Path(a.data_dir).expanduser()/'browser')
    # Prevent competing session daemons, even while the Chromium lock is released.
    service_lock=(directory/'session-service.lock').open('a')
    try:fcntl.flock(service_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('Session service sudah berjalan')
    origin=os.environ['OFFICE_WORKER_ORIGIN']
    controller=SessionController(SessionBrowser(directory,json.loads(config.read_text())),origin)
    try:
        server=ThreadingHTTPServer(('127.0.0.1',a.port),handler(controller,directory/'snapshot.json',
            os.environ['OFFICE_READ_TOKEN'],os.environ['OFFICE_CONTROL_TOKEN'],
            os.environ.get('OFFICE_DASHBOARD_ORIGIN','https://harun5231.github.io')))
        server.serve_forever()
    finally:controller.close();service_lock.close()

if __name__=='__main__':main()
