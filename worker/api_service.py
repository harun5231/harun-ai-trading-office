"""Authenticated dashboard API and bounded single-actor paper scheduler."""
import hmac
import json
import os
import queue
import signal
import sqlite3
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from .core import Ledger,Review,day
from .market import Market
from .neuroapi import NeuroAPI,api_key
from .state import directory
from .workflow import Workflow,export
from .robot import RobotStore,Coordinator,account_state
from .binance_private import BinanceReadOnly

ROBOT_POLL_SECONDS=45
# Account GETs, market preparation, and three bounded provider attempts can
# occupy one tick for several minutes. Do not mistake them for a stuck actor.
ACTOR_STALL_SECONDS=600
ACCOUNT_STALL_SECONDS=120
SHUTDOWN_DRAIN_SECONDS=600
SERVICE_FAILURE_CODES=frozenset(('WORKER_STATE_INITIALIZATION_FAILED',
    'WORKER_ACTOR_INITIALIZATION_FAILED','WORKER_ACTOR_UNAVAILABLE',
    'WORKER_ACCOUNT_READER_UNAVAILABLE','ROBOT_COORDINATOR_UNAVAILABLE'))

def robot_snapshot(controller,result):
    """Mask unavailable service observations on every detached robot response."""
    with controller.lock:
        worker_failure=getattr(controller,'worker_failure',None)
        account_failure=getattr(controller,'account_failure',None)
    if worker_failure:result.update(bot_status='REJECTED',failure_code=worker_failure,wait_reason=None)
    if account_failure:
        result.update(account_failure_code=account_failure,running_positions=None,available_slots=None,
            running_symbols=None,manual_exposure=[],usdt_wallet_balance=None,usdt_available_balance=None,account_checked_at=None)
        for setup in result.get('setups',[]):
            if 'account_review' in setup:
                setup['account_review'].update(checked_at=None,available_slots=None,symbol_exposed=None)
    return result

class Controller:
    def __init__(self,data):
        try:self.directory=directory(data)
        except Exception:raise RuntimeError('WORKER_STATE_INITIALIZATION_FAILED') from None
        self.jobs=queue.Queue(maxsize=1);self.lock=threading.Lock()
        self.state_lock=threading.Lock()
        self.status='NEUROAPI_UNCHECKED' if api_key() else 'NEUROAPI_NOT_CONFIGURED'
        self.busy=False;self.checked=0;self.checked_monotonic=float('-inf');self.stopping=threading.Event()
        self.worker_failure=None;self.account_failure=None
        self.robot_wakeup=threading.Event()
        self.worker_progress=self.account_progress=time.monotonic()
        # Establish WAL and all robot tables before competing actor connections.
        # SQLite's initial journal-mode transition can fail immediately with BUSY.
        try:
            ledger=self.open_ledger()
            try:export(ledger,self.directory/'snapshot.json')
            finally:ledger.db.close()
        except Exception:raise RuntimeError('WORKER_STATE_INITIALIZATION_FAILED') from None
        self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()
        self.account_thread=threading.Thread(target=self.account_loop,daemon=True);self.account_thread.start()
    def open_ledger(self):
        # Connections remain thread-owned; only their schema initialization is serialized.
        with getattr(self,'state_lock',self.lock):
            ledger=Ledger(self.directory/'ledger.sqlite3')
            try:RobotStore(ledger.db)
            except Exception:
                ledger.db.close();raise
            return ledger
    def failure(self,code,account=False):
        if code not in SERVICE_FAILURE_CODES:code='WORKER_ACTOR_UNAVAILABLE'
        field='account_failure' if account else 'worker_failure'
        with self.lock:
            changed=getattr(self,field,None)!=code
            setattr(self,field,code)
        # Emit a fixed code once per transition, never exception text, headers or payloads.
        if changed:print(json.dumps({'event':'WORKER_SERVICE_FAILURE','code':code}),flush=True)
    def clear_failure(self,account=False):
        with self.lock:setattr(self,'account_failure' if account else 'worker_failure',None)
    def progress(self,account=False):
        with self.lock:
            if account:self.account_progress=time.monotonic()
            else:self.worker_progress=time.monotonic()
    def healthy(self):
        with self.lock:
            current=time.monotonic()
            fresh=(current-self.worker_progress<ACTOR_STALL_SECONDS and
                   current-self.account_progress<ACCOUNT_STALL_SECONDS)
        return fresh and not self.stopping.is_set() and self.thread.is_alive() and self.account_thread.is_alive()
    def drain(self,timeout=SHUTDOWN_DRAIN_SECONDS):
        self.stopping.set()
        deadline=time.monotonic()+timeout
        for actor in (self.thread,self.account_thread):
            actor.join(timeout=max(0,deadline-time.monotonic()))
    def account_loop(self):
        ledger=None
        try:
            ledger=self.open_ledger()
            with getattr(self,'state_lock',self.lock):store=RobotStore(ledger.db)
            allowed={'ROBOT_ACCOUNT_UNAVAILABLE','BINANCE_NOT_CONFIGURED','BINANCE_AUTH_FAILED','BINANCE_IP_RESTRICTED','BINANCE_PERMISSION_DENIED','BINANCE_CLOCK_ERROR','BINANCE_ACCOUNT_UNAVAILABLE','BINANCE_ENDPOINT_DENIED'}
            while not self.stopping.is_set():
                self.progress(account=True)
                interval=15
                try:
                    if store.settings()['robot_on']:
                        try:store.report_account(account_state(BinanceReadOnly(),store,day()))
                        except Exception as error:
                            reason=str(error)
                            store.report_account(reason=reason if reason in allowed else 'ROBOT_ACCOUNT_UNAVAILABLE')
                        self.clear_failure(account=True)
                    else:interval=2
                except Exception:self.failure('WORKER_ACCOUNT_READER_UNAVAILABLE',account=True)
                self.progress(account=True)
                if self.stopping.wait(interval):break
        except Exception:self.failure('WORKER_ACCOUNT_READER_UNAVAILABLE',account=True)
        finally:
            if ledger is not None:ledger.db.close()
    def snapshot(self):
        with self.lock:return {'status':self.status,'busy':self.busy,'checked_at':self.checked,'mode':'DRY_RUN','live_enabled':False,
            'worker_failure_code':getattr(self,'worker_failure',None),'account_reader_failure_code':getattr(self,'account_failure',None)}
    def submit(self,job):
        with self.lock:
            if self.stopping.is_set() or self.busy or time.monotonic()-getattr(self,'checked_monotonic',float('-inf'))<3:return False
            try:self.jobs.put_nowait(job)
            except queue.Full:return False
            self.busy=True
        return True
    def robot(self,value=None,approval=False,simulation=False):
        if value is None:
            # Status/diagnostic reads never initialize schemas, alter WAL mode,
            # insert settings, or acquire the state-writer initialization lock.
            db=sqlite3.connect((self.directory/'ledger.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=5)
            try:
                db.row_factory=sqlite3.Row
                db.execute('PRAGMA query_only=ON')
                return robot_snapshot(self,RobotStore(db,initialize=False).snapshot())
            finally:db.close()
        ledger=self.open_ledger()
        try:
            with getattr(self,'state_lock',self.lock):store=RobotStore(ledger.db)
            if simulation:return robot_snapshot(self,store.simulate(value))
            if approval:
                if not isinstance(value,dict) or set(value)!={'setup_id','decision'}:raise Review('INVALID_APPROVAL')
                return robot_snapshot(self,store.approve(value['setup_id'],value['decision']))
            was_on=store.settings()['robot_on']
            result=store.configure(value)
            if result['robot_on'] and not was_on:self.robot_wakeup.set()
            return robot_snapshot(self,result)
        finally:ledger.db.close()
    def loop(self):
        ledger=None;initialized=False;last_monitor=0;last_tick=-ROBOT_POLL_SECONDS
        try:
            ledger=self.open_ledger()
            with getattr(self,'state_lock',self.lock):client=NeuroAPI(ledger)
            market=Market(int(os.getenv('OFFICE_CANDLE_LOOKBACK','100')),int(os.getenv('OFFICE_MARKET_MAX_AGE','180')))
            with getattr(self,'state_lock',self.lock):
                robot=Coordinator(ledger,client,market,BinanceReadOnly,stopping=self.stopping)
            initialized=True
            while not self.stopping.is_set():
                self.progress()
                try:job=self.jobs.get(timeout=1)
                except queue.Empty:job=None
                if self.stopping.is_set():break
                # Research poll is independent of the permanently disabled live scheduler.
                # Old OFFICE_AUTO_DRY_RUN flags cannot bypass ROBOT OFF.
                if not job and (self.robot_wakeup.is_set() or time.monotonic()-last_tick>=ROBOT_POLL_SECONDS):
                    self.robot_wakeup.clear();job='robot'
                if not job:
                    if time.monotonic()-last_monitor>=30:
                        last_monitor=time.monotonic()
                        try:Workflow(ledger,client,market,self.directory/'snapshot.json').monitor()
                        except Exception:pass # A failed paper snapshot must not stop robot polling.
                    continue
                with self.lock:self.busy=True
                try:
                    if job=='check':
                        status=client.health()
                        with self.lock:self.status=status
                    else:
                        # Robot state is exposed by /robot/status.  A coordinator
                        # tick must not overwrite the independent NeuroAPI health
                        # indicator shown by the Office.
                        last_tick=time.monotonic();robot.tick();self.clear_failure()
                except Exception:
                    if job=='check':
                        status='NEUROAPI_NOT_CONFIGURED' if not api_key() else 'NEUROAPI_UNAVAILABLE'
                        with self.lock:self.status=status
                    else:self.failure('ROBOT_COORDINATOR_UNAVAILABLE')
                finally:
                    with self.lock:self.checked=time.time();self.checked_monotonic=time.monotonic();self.busy=False
                    self.progress()
        except Exception:self.failure('WORKER_ACTOR_UNAVAILABLE' if initialized else 'WORKER_ACTOR_INITIALIZATION_FAILED')
        finally:
            if ledger is not None:ledger.db.close()

def handler(controller,read_token,control_token,origin):
    u=urlsplit(origin)
    if u.scheme!='https' or not u.netloc or u.path not in ('','/') or u.query or u.fragment or u.username:raise ValueError('INVALID_ORIGIN')
    if min(len(read_token),len(control_token))<32 or hmac.compare_digest(read_token,control_token):raise ValueError('INVALID_WORKER_AUTH')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,code,data):
            raw=json.dumps(data).encode();self.send_response(code)
            self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
            if self.headers.get('Origin')==origin:
                self.send_header('Access-Control-Allow-Origin',origin);self.send_header('Vary','Origin')
            self.send_header('Access-Control-Allow-Headers','Authorization, Content-Type');self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def authorized(self,control=False):
            value=self.headers.get('Authorization','')
            return hmac.compare_digest(value,'Bearer '+control_token) or (not control and hmac.compare_digest(value,'Bearer '+read_token))
        def do_OPTIONS(self):
            # A 204 response must not carry Content-Length or a body.  Sending
            # JSON here breaks strict HTTP/2 clients during the CORS preflight.
            self.send_response(204)
            if self.headers.get('Origin')==origin:
                self.send_header('Access-Control-Allow-Origin',origin);self.send_header('Vary','Origin')
            self.send_header('Access-Control-Allow-Headers','Authorization, Content-Type')
            self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS')
            self.end_headers()
        def do_GET(self):
            if not self.authorized():return self.reply(403,{'error':'FORBIDDEN'})
            if self.path=='/health':return self.reply(200,{'status':'ONLINE' if controller.healthy() else 'OFFLINE','mode':'DRY_RUN','live_enabled':False})
            if self.path=='/robot/status':
                try:return self.reply(200,controller.robot())
                except Exception:return self.reply(503,{'error':'WORKER_STATE_UNAVAILABLE','mode':'DRY_RUN','live_enabled':False})
            if self.path=='/neuroapi/status':return self.reply(200,controller.snapshot())
            if self.path=='/binance/status':
                try:data=json.loads((controller.directory/'live-status.json').read_text())
                except Exception:data={'status':'LIVE_EXECUTION_DISARMED','live_execution':False,'scheduler_enabled':False}
                data.update(live_execution=False,scheduler_enabled=False,status='LIVE_EXECUTION_DISARMED')
                return self.reply(200,data)
            if self.path=='/snapshot':
                try:data=json.loads((controller.directory/'snapshot.json').read_text())
                except Exception:return self.reply(503,{'error':'SNAPSHOT_UNAVAILABLE'})
                return self.reply(200,data)
            self.reply(404,{'error':'NOT_FOUND'})
        def do_POST(self):
            if not self.authorized(True) or self.headers.get('Origin')!=origin:return self.reply(403,{'error':'FORBIDDEN'})
            if self.path in ('/robot/settings','/robot/approval','/robot/simulation'):
                try:
                    from .http_client import unique
                    length=self.headers.get('Content-Length','')
                    if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0<int(length)<=1024 or self.headers.get('Content-Type')!='application/json':raise ValueError
                    self.connection.settimeout(5)
                    value=json.loads(self.rfile.read(int(length)),object_pairs_hook=unique)
                    if not isinstance(value,dict):raise ValueError
                    result=controller.robot(value,approval=self.path=='/robot/approval',simulation=self.path=='/robot/simulation')
                    return self.reply(200,result)
                except Exception:return self.reply(400,{'error':'ROBOT_CONTROL_REJECTED'})
            if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length','0')!='0':return self.reply(400,{'error':'BODY_NOT_ALLOWED'})
            if self.path not in ('/neuroapi/check','/neuroapi/run'):return self.reply(404,{'error':'NOT_FOUND'})
            if not controller.submit(self.path.rsplit('/',1)[1]):return self.reply(409,{'error':'WORKER_BUSY'})
            self.reply(202,controller.snapshot())
    return Handler

def main():
    os.umask(0o077)
    from .live_arm import new_boot
    new_boot() # Restart always revokes local live authorization.
    c=Controller(os.getenv('OFFICE_DATA_DIR','/data'))
    server=ThreadingHTTPServer(('0.0.0.0',8787),handler(c,os.environ['OFFICE_READ_TOKEN'],os.environ['OFFICE_CONTROL_TOKEN'],os.getenv('OFFICE_DASHBOARD_ORIGIN','https://harun5231.github.io')))
    stalled_actor=threading.Event()
    def stop(*args):c.stopping.set();threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    def watchdog():
        while not c.stopping.wait(2):
            if not c.healthy():
                with c.lock:
                    worker_failed=not c.thread.is_alive() or time.monotonic()-c.worker_progress>=ACTOR_STALL_SECONDS
                    if time.monotonic()-c.worker_progress>=ACTOR_STALL_SECONDS:stalled_actor.set()
                c.failure('WORKER_ACTOR_UNAVAILABLE' if worker_failed else 'WORKER_ACCOUNT_READER_UNAVAILABLE',account=not worker_failed)
                # A failed account reader must not cut off a healthy paid call.
                # Only a stalled research actor skips the normal shutdown drain.
                c.stopping.set();server.shutdown();return
    threading.Thread(target=watchdog,daemon=True).start()
    try:server.serve_forever()
    finally:
        c.stopping.set();server.server_close()
        c.drain(timeout=0 if stalled_actor.is_set() else SHUTDOWN_DRAIN_SECONDS)
if __name__=='__main__':main()
