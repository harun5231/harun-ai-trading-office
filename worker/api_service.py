"""Authenticated dashboard API and bounded single-actor paper scheduler."""
import hmac
import json
import os
import queue
import signal
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from .core import Ledger,Review
from .market import Market
from .neuroapi import NeuroAPI,api_key
from .state import directory
from .workflow import Workflow,export

class Controller:
    def __init__(self,data):
        self.directory=directory(data);self.jobs=queue.Queue(maxsize=1);self.lock=threading.Lock()
        self.status='NEUROAPI_UNCHECKED' if api_key() else 'NEUROAPI_NOT_CONFIGURED'
        self.busy=False;self.checked=0;self.stopping=threading.Event()
        self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()
    def snapshot(self):
        with self.lock:return {'status':self.status,'busy':self.busy,'checked_at':self.checked,'mode':'DRY_RUN','live_enabled':False}
    def submit(self,job):
        with self.lock:
            if self.busy or time.time()-self.checked<3:return False
            self.busy=True
        self.jobs.put_nowait(job);return True
    def loop(self):
        ledger=Ledger(self.directory/'ledger.sqlite3');last_monitor=0;scheduled=None
        try:
            export(ledger,self.directory/'snapshot.json')
            while not self.stopping.is_set():
                try:job=self.jobs.get(timeout=1)
                except queue.Empty:job=None
                now=datetime.now(ZoneInfo('Asia/Bangkok'))
                if not job and os.getenv('OFFICE_AUTO_DRY_RUN','false')=='true' and scheduled!=now.date():
                    hour,minute=map(int,os.getenv('OFFICE_RUN_AT','08:00').split(':'))
                    if (now.hour,now.minute)>=(hour,minute):scheduled=now.date();job='run'
                if not job:
                    if time.time()-last_monitor>=30:
                        last_monitor=time.time()
                        flow=Workflow(ledger,NeuroAPI(ledger),Market(),self.directory/'snapshot.json');flow.monitor()
                    continue
                with self.lock:self.busy=True
                client=NeuroAPI(ledger)
                try:
                    if job=='check':status=client.health()
                    else:
                        market=Market(int(os.getenv('OFFICE_CANDLE_LOOKBACK','100')),int(os.getenv('OFFICE_MARKET_MAX_AGE','180')))
                        flow=Workflow(ledger,client,market,self.directory/'snapshot.json')
                        status=flow.run()['status']
                except Exception:status='NEUROAPI_NOT_CONFIGURED' if not api_key() else 'NEUROAPI_UNAVAILABLE'
                with self.lock:self.status=status;self.checked=time.time();self.busy=False
        finally:ledger.db.close()

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
            self.send_header('Access-Control-Allow-Headers','Authorization');self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def authorized(self,control=False):
            value=self.headers.get('Authorization','')
            return hmac.compare_digest(value,'Bearer '+control_token) or (not control and hmac.compare_digest(value,'Bearer '+read_token))
        def do_OPTIONS(self):self.reply(204,{})
        def do_GET(self):
            if not self.authorized():return self.reply(403,{'error':'FORBIDDEN'})
            if self.path=='/health':return self.reply(200,{'status':'ONLINE' if controller.thread.is_alive() else 'OFFLINE','mode':'DRY_RUN','live_enabled':False})
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
    def stop(*args):c.stopping.set();threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    def watchdog():
        while not c.stopping.wait(2):
            if not c.thread.is_alive():server.shutdown();return
    threading.Thread(target=watchdog,daemon=True).start()
    try:server.serve_forever()
    finally:server.server_close();c.stopping.set();c.thread.join(timeout=10)
if __name__=='__main__':main()
