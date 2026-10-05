"""Authenticated login control only. No credential fields, prompts or trading endpoints.
Playwright is owned by one actor thread; HTTP handlers never touch browser objects.
"""
from .desktop import browser_options
from .profile_owner import ProfileOwner
from .manual_browser import ManualBrowser
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
        self.owner=ProfileOwner(self.profile);self.manual=ManualBrowser(self.profile)
    def acquire(self, *, allow_stale=False):
        if self.lock is not None:return
        self.lock=(self.directory/'worker.lock').open('a')
        try:
            fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.owner.acquire(allow_stale=allow_stale)
        except Exception:
            self.lock.close();self.lock=None
            raise RuntimeError('WORKER_BUSY') from None
    def login(self):
        if self.context is not None:raise RuntimeError('WORKER_BUSY')
        self.acquire()
        self.manual.start((self.lock.fileno(),self.owner.file.fileno()))
        return 'LOGIN_IN_PROGRESS'  # Only the user observes the manual page.
    def desktop_active(self):return self.manual.active()
    def prepare_check(self):
        # Keep BOTH ownership locks throughout handoff; no scheduler can race us.
        self.acquire(allow_stale=True)
        self.manual.stop()
        self.owner.recover_stale(self.lock,self.manual.session_id)
        self.owner.wait_released()
    def open(self):
        if self.manual.active():raise RuntimeError('WORKER_BUSY')
        if self.context is not None:return
        self.acquire()
        self.owner.wait_released()
        try:
            from playwright.sync_api import sync_playwright
            self.pw=sync_playwright().start()
            self.context=self.pw.chromium.launch_persistent_context(str(self.profile),headless=False,accept_downloads=False,**browser_options())
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
        for message in ('Verification failed','Maximum Attempts Reached'):
            loc=self.page.get_by_text(message,exact=True)
            if any(loc.nth(i).is_visible() for i in range(loc.count())):
                return 'CLOUDFLARE_REQUIRED'
        if self.visible('iframe[src*="challenges.cloudflare.com"]'):
            return 'CLOUDFLARE_REQUIRED'
        for frame in self.page.frames:
            if self.visible(n.get('captcha'),frame):return 'CLOUDFLARE_REQUIRED'
        u=urlsplit(self.page.url)
        if u.scheme!='https' or u.hostname!='app.neurobro.ai':return 'LOGIN_IN_PROGRESS'
        if self.visible(n.get('login_required')):return 'LOGIN_REQUIRED'
        required=('authenticated','login_required','captcha','loading','composer')
        proof=self.config.get('session_selector_verification',{})
        if proof.get('version')=='structural-readonly-v3' and proof.get('session_check_ready'):
            # Re-prove structural relationships on the current page, not just a
            # saved boolean or a matching generic application element.
            from .structural_discovery import discover_phase3
            result=discover_phase3(self.page)
            if result['session_check_ready']:return 'CONNECTED'
            return {'LOGIN_REQUIRED':'LOGIN_REQUIRED','CLOUDFLARE_REQUIRED':'CLOUDFLARE_REQUIRED',
                    'LOADING':'LOGIN_IN_PROGRESS'}.get(result['state'],'DISCONNECTED')
        if not self.config.get('selectors_verified_on') or not all(n.get(k) for k in required):
            return 'DISCONNECTED'  # No guessed login selector, no pretend success.
        if self.visible(n['loading']):return 'LOGIN_IN_PROGRESS'
        c=self.page.locator(n['composer'])
        if self.visible(n['authenticated']) and c.count()==1 and c.is_visible() and c.is_editable():
            return 'CONNECTED'
        return 'DISCONNECTED'
    def close(self):
        # On shutdown/timeout retain locks unless the browser has really stopped.
        self.manual.stop()
        if self.context:self.context.close()
        self.context=None;self.page=None
        if self.pw:self.pw.stop()
        self.pw=None
        if self.owner.file is not None:
            self.owner.recover_stale(self.lock,self.manual.session_id)
            self.owner.wait_released()
            self.owner.close()
        if self.lock:self.lock.close();self.lock=None


class SessionController:
    def __init__(self, browser, worker_origin):
        self.browser=browser;self.origin=public_origin(worker_origin)
        self.mu=threading.Lock();self.jobs=queue.Queue(maxsize=1)
        self.busy=False;self.state='DISCONNECTED';self.checked=0;self.ever_connected=False
        self.error=None;self.desktop=False
        self.heartbeat=time.monotonic();self.browser_health='IDLE';self.persistence_ok=False
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def snapshot(self):
        with self.mu:
            stale=self.state=='CONNECTED' and time.time()-self.checked>60
            return {'status':'DISCONNECTED' if stale else self.state,
                    'checked_at':datetime.fromtimestamp(self.checked,timezone.utc).isoformat() if self.checked else None,
                    'stale':stale,'busy':self.busy,'error':self.error,
                    'takeover_url':self.origin+'/desktop/vnc.html#autoconnect=1&resize=scale&path=desktop/websockify' if self.desktop else None,
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
        if os.environ.get('OFFICE_MANAGED')=='1':
            try:
                # Offline runtime probe: no Neurobro navigation or authentication retry.
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(headless=True)
                    page=browser.new_page();page.evaluate('1');browser.close()
            except Exception:
                self.browser_health='ERROR'
                return
        while True:
            self.heartbeat=time.monotonic()
            from .health import persistent_probe
            directory=getattr(self.browser,'directory',None)
            self.persistence_ok=persistent_probe(directory) if directory is not None else True
            try:action=self.jobs.get(timeout=5)
            except queue.Empty:
                try:
                    self.browser_health='ALIVE' if self.browser.desktop_active() else 'IDLE'
                except Exception:self.browser_health='ERROR'
                continue
            if action is None:
                self.browser.close();return
            try:
                if action=='login':
                    state=self.browser.login()
                else:
                    self.browser.prepare_check()
                    try:
                        self.browser.open()
                        state=self.browser.inspect()
                    finally:
                        self.browser.close()
                    # No automatic verification retries or challenge interaction.
                    # User may press LOGIN again to reopen ordinary Chromium.
                self.browser_health='ALIVE' if self.browser.desktop_active() else 'IDLE'
                with self.mu:
                    if state=='LOGIN_REQUIRED' and self.ever_connected:state='SESSION_EXPIRED'
                    self.ever_connected |= state=='CONNECTED'
                    self.state=state;self.checked=time.time()
                    self.desktop=self.browser.desktop_active()
            except Exception as exc:
                try:self.browser.close()
                except Exception:pass
                with self.mu:
                    self.state='DISCONNECTED';self.desktop=False;self.checked=time.time()
                    self.browser_health='IDLE' if str(exc)=='WORKER_BUSY' else 'ERROR'
                    self.error=str(exc) if str(exc) in {'WORKER_BUSY','PROFILE_NOT_RELEASED',
                        'MANUAL_BROWSER_NOT_STOPPED','MANUAL_BROWSER_EXITED',
                        'CHROMIUM_EXECUTABLE_NOT_FOUND_OR_AMBIGUOUS'} else 'BROWSER_OR_CONFIG_ERROR'
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
            if self.path=='/health':
                from .health import report
                return self.reply(200,report(controller))
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
    p.add_argument('--host',choices=['127.0.0.1','0.0.0.0'],default='127.0.0.1')
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
        server=ThreadingHTTPServer((a.host,a.port),handler(controller,directory/'snapshot.json',
            os.environ['OFFICE_READ_TOKEN'],os.environ['OFFICE_CONTROL_TOKEN'],
            os.environ.get('OFFICE_DASHBOARD_ORIGIN','https://harun5231.github.io')))
        import signal
        signal.signal(signal.SIGTERM,lambda *_:threading.Thread(target=server.shutdown,daemon=True).start())
        server.serve_forever()
        server.server_close()
    finally:controller.close();service_lock.close()

if __name__=='__main__':main()
