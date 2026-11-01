"""Authenticated single robot pipeline and cached Binance Futures office reports."""
import hmac
import json
import os
import signal
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit
from .core import Ledger,Review,validated_risk_target,DYNAMIC_RISK_MODEL
from .market import Market
from .research_provider import create_provider, selected_provider
from .neurobrowser_control import session_status, login_grant
from .state import directory
from .robot import Coordinator
from .robot_store import RobotStore
from .order_gateway import OrderGateway
from .binance_private import BinanceReadOnly
from .binance_office import collect
from .pnl_calendar import collect_calendar, unavailable as unavailable_calendar
from .protection_actor import ProtectionActor
from .existing_risk_monitor import refresh_existing_risks

ROBOT_POLL_SECONDS=45
ACCOUNT_POLL_SECONDS=15
HISTORY_POLL_SECONDS=60
ACTOR_STALL_SECONDS=1200
ACCOUNT_STALL_SECONDS=120
PROTECTION_STALL_SECONDS=180
SHUTDOWN_DRAIN_SECONDS=600
SERVICE_FAILURE_CODES=frozenset(('WORKER_STATE_INITIALIZATION_FAILED',
    'WORKER_ACTOR_INITIALIZATION_FAILED','WORKER_ACTOR_UNAVAILABLE',
    'WORKER_ACCOUNT_READER_UNAVAILABLE','ROBOT_COORDINATOR_UNAVAILABLE',
    'WORKER_PROTECTION_MONITOR_UNAVAILABLE'))
ACCOUNT_FAILURE_CODES=frozenset(('ROBOT_ACCOUNT_UNAVAILABLE','BINANCE_NOT_CONFIGURED',
    'BINANCE_AUTH_FAILED','BINANCE_IP_RESTRICTED','BINANCE_PERMISSION_DENIED',
    'BINANCE_CLOCK_ERROR','BINANCE_ACCOUNT_UNAVAILABLE','BINANCE_ENDPOINT_DENIED'))

def validate_settings(value):
    if not isinstance(value,dict) or not value or set(value)-{'robot_on','risk_target_usdt'}:
        raise Review('INVALID_ROBOT_SETTING')
    if 'robot_on' in value and type(value['robot_on']) is not bool:raise Review('INVALID_ROBOT_SETTING')
    if 'risk_target_usdt' in value:
        if type(value['risk_target_usdt']) is not str:raise Review('INVALID_RISK_TARGET')
        validated_risk_target(value['risk_target_usdt'])

def robot_snapshot(controller,result):
    """Show a fixed error instead of a stale successful service observation."""
    with controller.lock:
        worker_failure=controller.worker_failure;account_failure=controller.account_failure
    protection=getattr(controller,'protection',None)
    if protection is not None:
        result['protection_monitor']=dict(status=protection.status,
            user_data_status=protection.feed_status,checked_at=getattr(protection,'checked_at',None),
            cost_history_status=getattr(protection,'cost_history_status','NO_CONFIRMED_EXIT_FILLS'))
    if worker_failure:
        result['failure_code']=worker_failure
        if result.get('robot_on'):result.update(bot_status='NEEDS_REVIEW',wait_reason=None)
    if account_failure:result.update(account_failure_code=account_failure,running_positions=None,
        running_symbols=None,available_slots=None,manual_exposure=[],usdt_wallet_balance=None,
        usdt_available_balance=None,account_checked_at=None)
    return result

def office_snapshot(controller,result):
    result['robot']=robot_snapshot(controller,result['robot'])
    result['research']=session_status()
    with controller.lock:
        account_failure=controller.account_failure;worker_failure=controller.worker_failure
    if account_failure:
        result['account'].update(status='UNAVAILABLE',failure_code=account_failure,checked_at=None,
            usdt_wallet_balance=None,usdt_available_balance=None,active_positions=None,positions=[],
            position_mode=None,can_trade=None,multi_assets_margin=None)
        reports=result.get('reports',{})
        reports.update(status='UNAVAILABLE',checked_at=None,complete=False,pnl_today_usdt=None,
            realized_pnl_today_usdt=None,commission_today_usdt=None,funding_today_usdt=None,trades_today=None)
        result.get('position_history',{}).update(status='UNAVAILABLE',checked_at=None,items=[],complete=False)
        result['pnl_calendar']=unavailable_calendar(account_failure)
    employees=result.get('employees',[])
    for employee in employees:
        if employee.get('status')!='WORKING':continue
        if ((worker_failure and employee.get('id') in ('market','neuro','risk','trading'))
                or (account_failure and employee.get('id')=='position')):
            employee['status']='IDLE'
    if worker_failure or account_failure:
        working=any(employee.get('id')!='boss' and employee.get('status')=='WORKING' for employee in employees)
        for employee in employees:
            if employee.get('id')=='boss':employee['status']='WORKING' if working else 'IDLE'
    return result

class Controller:
    def __init__(self,data):
        try:self.directory=directory(data)
        except Exception:raise RuntimeError('WORKER_STATE_INITIALIZATION_FAILED') from None
        self.lock=threading.Lock();self.state_lock=threading.Lock()
        self.stopping=threading.Event();self.robot_wakeup=threading.Event();self.account_wakeup=threading.Event()
        self.busy=False;self.checked=0;self.worker_failure=None;self.account_failure=None
        self.worker_progress=self.account_progress=time.monotonic()
        # WAL, robot state and paid-request journal are initialized before either
        # actor opens a competing connection. This initialization sends no request.
        try:
            ledger=self.open_ledger()
            try:
                with self.state_lock:create_provider(ledger)
            finally:ledger.db.close()
        except Exception:raise RuntimeError('WORKER_STATE_INITIALIZATION_FAILED') from None
        self.thread=threading.Thread(target=self.loop,daemon=True)
        self.account_thread=threading.Thread(target=self.account_loop,daemon=True)
        self.protection=ProtectionActor(self,Coordinator,OrderGateway)
        self.protection_thread=threading.Thread(target=self.protection.run,daemon=True)
        self.protection_thread.start();self.thread.start();self.account_thread.start()
    def open_ledger(self):
        with self.state_lock:
            ledger=Ledger(self.directory/'ledger.sqlite3')
            try:RobotStore(ledger.db)
            except Exception:
                ledger.db.close();raise
            return ledger
    def failure(self,code,account=False):
        if code not in SERVICE_FAILURE_CODES:code='WORKER_ACTOR_UNAVAILABLE'
        field='account_failure' if account else 'worker_failure'
        with self.lock:
            changed=getattr(self,field)!=code;setattr(self,field,code)
        if changed:print(json.dumps({'event':'WORKER_SERVICE_FAILURE','code':code}),flush=True)
    def clear_failure(self,account=False):
        with self.lock:setattr(self,'account_failure' if account else 'worker_failure',None)
    def progress(self,account=False):
        with self.lock:
            if account:self.account_progress=time.monotonic()
            else:self.worker_progress=time.monotonic()
    def healthy(self):
        with self.lock:
            now=time.monotonic()
            fresh=now-self.worker_progress<ACTOR_STALL_SECONDS and now-self.account_progress<ACCOUNT_STALL_SECONDS
        protection=getattr(self,'protection',None)
        protected=(protection is not None and self.protection_thread.is_alive()
            and now-protection.progress_at<PROTECTION_STALL_SECONDS)
        return fresh and protected and not self.stopping.is_set() and self.thread.is_alive() and self.account_thread.is_alive()
    def drain(self,timeout=SHUTDOWN_DRAIN_SECONDS):
        self.stopping.set();self.robot_wakeup.set();self.account_wakeup.set();self.protection.wakeup.set()
        deadline=time.monotonic()+timeout
        for actor in (self.thread,self.account_thread,self.protection_thread,self.protection.feed_thread):
            if actor is not None:actor.join(timeout=max(0,deadline-time.monotonic()))
    def read_state(self,office=False):
        db=sqlite3.connect((self.directory/'ledger.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=5)
        try:
            db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
            store=RobotStore(db,initialize=False)
            return office_snapshot(self,store.office_snapshot()) if office else robot_snapshot(self,store.snapshot())
        finally:db.close()
    def robot(self,value=None):
        if value is None:return self.read_state()
        validate_settings(value)
        ledger=self.open_ledger()
        try:
            with self.state_lock:
                store=RobotStore(ledger.db)
                was_on=store.settings()['robot_on'];result=store.configure(value)
                if result['robot_on'] and not was_on:self.robot_wakeup.set()
            return robot_snapshot(self,result)
        finally:ledger.db.close()
    def office(self):return self.read_state(office=True)
    def browser_dispatch(self,value):
        """The browser checks current permission immediately before clicking Send."""
        try:
            if selected_provider()!='BROWSER' or self.stopping.is_set():return False
        except Exception:return False
        if not isinstance(value,dict) or set(value)!={'operation','marker','request_sha256'}:return False
        db=sqlite3.connect((self.directory/'ledger.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=5)
        try:
            from .neurobrowser import verify_dispatch
            from .core import day
            db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            settings=db.execute('SELECT enabled FROM robot_settings WHERE id=1').fetchone()
            if not settings or settings['enabled']!=1 or self.stopping.is_set():return False
            return verify_dispatch(db,value['operation'],value['marker'],value['request_sha256'],day(),time.time(),
                max_age=int(os.getenv('OFFICE_MARKET_MAX_AGE','180')))
        except Exception:return False
        finally:db.close()
    def account_loop(self):
        ledger=None;last_history=float('-inf')
        try:
            ledger=self.open_ledger()
            with self.state_lock:
                store=RobotStore(ledger.db)
                recovery_gateway=OrderGateway()
                recovery_coordinator=Coordinator(ledger,None,None,None,
                    stopping=self.stopping,gateway=recovery_gateway)
            while not self.stopping.is_set():
                self.progress(account=True)
                try:
                    # GET recovery uses this existing independent account actor.
                    # A slow REST/history query cannot block stream fill handling.
                    recovery_notice=getattr(self.protection,'account_recovery',None)
                    recovered=bool(recovery_notice is not None and recovery_notice.is_set())
                    if recovered:recovery_notice.clear()
                    self.protection.recover_pending(recovery_coordinator,recovery_gateway)
                    history=time.monotonic()-last_history>=HISTORY_POLL_SECONDS
                    try:
                        client=BinanceReadOnly()
                        observation=collect(client,include_history=history)
                        from .core import day as carry_day
                        store.reconcile_carry(client,carry_day(),[row['symbol'] for row in observation['account']['positions']])
                        if history:
                            observation['pnl_calendar']=collect_calendar(client,self.directory,max_requests=1,budget_seconds=10)
                            if observation['pnl_calendar']['checked_at'] is None:
                                observation['pnl_calendar']['checked_at']=observation['generated_at']
                    except Exception as error:
                        if recovered:recovery_notice.set()
                        reason=str(error)
                        store.report_office_failure(reason if reason in ACCOUNT_FAILURE_CODES else 'BINANCE_ACCOUNT_UNAVAILABLE')
                    else:
                        store.report_office(observation)
                        # Observation only, after account/fill recovery. The
                        # independent priority actor never waits for these GETs.
                        if history or recovered:
                            try:refresh_existing_risks(recovery_coordinator,Market(),client)
                            except Exception:pass
                        if history:last_history=time.monotonic()
                    self.clear_failure(account=True)
                except Exception:self.failure('WORKER_ACCOUNT_READER_UNAVAILABLE',account=True)
                self.progress(account=True)
                self.account_wakeup.wait(ACCOUNT_POLL_SECONDS);self.account_wakeup.clear()
        except Exception:self.failure('WORKER_ACCOUNT_READER_UNAVAILABLE',account=True)
        finally:
            if ledger is not None:ledger.db.close()
    def loop(self):
        ledger=None;initialized=False;last_tick=float('-inf')
        try:
            ledger=self.open_ledger()
            with self.state_lock:
                client=create_provider(ledger)
                market=Market(max(201,int(os.getenv('OFFICE_CANDLE_LOOKBACK','201'))),int(os.getenv('OFFICE_MARKET_MAX_AGE','180')))
                coordinator=Coordinator(ledger,client,market,BinanceReadOnly,stopping=self.stopping,
                    new_setup_policy=DYNAMIC_RISK_MODEL)
            initialized=True
            while not self.stopping.is_set():
                self.progress()
                if self.robot_wakeup.is_set() or time.monotonic()-last_tick>=ROBOT_POLL_SECONDS:
                    self.robot_wakeup.clear();last_tick=time.monotonic()
                    with self.lock:self.busy=True
                    try:coordinator.tick();self.clear_failure()
                    except Exception:self.failure('ROBOT_COORDINATOR_UNAVAILABLE')
                    finally:
                        with self.lock:self.checked=time.time();self.busy=False
                        self.progress()
                self.robot_wakeup.wait(1)
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
            if self.path not in ('/health','/robot/status','/office/status','/robot/settings','/neurobro/status','/neurobro/login/start'):return self.reply(404,{'error':'NOT_FOUND'})
            self.send_response(204)
            if self.headers.get('Origin')==origin:
                self.send_header('Access-Control-Allow-Origin',origin);self.send_header('Vary','Origin')
            self.send_header('Access-Control-Allow-Headers','Authorization, Content-Type')
            self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS');self.end_headers()
        def do_GET(self):
            if not self.authorized():return self.reply(403,{'error':'FORBIDDEN'})
            if self.path=='/health':
                try:
                    gateway=OrderGateway().status();connected=gateway['connected']
                    if type(connected) is not bool:raise ValueError
                except Exception:return self.reply(503,{'error':'GATEWAY_STATUS_UNAVAILABLE','mode':'ORDER_PIPELINE'})
                return self.reply(200,{'status':'ONLINE' if controller.healthy() else 'OFFLINE',
                    'mode':'ORDER_PIPELINE','gateway_connected':connected})
            if self.path in ('/robot/status','/office/status'):
                try:return self.reply(200,controller.office() if self.path=='/office/status' else controller.robot())
                except Exception:return self.reply(503,{'error':'WORKER_STATE_UNAVAILABLE','mode':'ORDER_PIPELINE'})
            if self.path=='/neurobro/status':return self.reply(200,session_status())
            return self.reply(404,{'error':'NOT_FOUND'})
        def do_POST(self):
            if self.path == '/neurobro/probe-dispatch-check':
                from .neurobrowser import helper_token
                from .neurobrowser_probe_guard import ProbeStore
                token = helper_token()
                authorizations = self.headers.get_all('Authorization', [])
                if not token or len(authorizations) != 1 or not hmac.compare_digest(authorizations[0], 'Bearer ' + token):
                    return self.reply(403, {'error': 'FORBIDDEN'})
                try:
                    from .http_client import unique
                    lengths = self.headers.get_all('Content-Length', [])
                    if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 800000 or self.headers.get('Transfer-Encoding') or self.headers.get_all('Content-Type', []) != ['application/json']:
                        raise ValueError
                    self.connection.settimeout(5)
                    raw = self.rfile.read(int(lengths[0]))
                    if len(raw) != int(lengths[0]):
                        raise ValueError
                    value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    store = ProbeStore(controller.directory / 'maintenance' / 'neurobro-off-probe', controller.directory / 'ledger.sqlite3')
                    return self.reply(200, store.callback(value, stopping=controller.stopping.is_set()))
                except Exception:
                    return self.reply(200, {'allowed': False})
            if self.path=='/neurobro/dispatch-check':
                from .neurobrowser import helper_token
                token=helper_token()
                if not token or not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):return self.reply(403,{'error':'FORBIDDEN'})
                try:
                    value=self.json_body()
                    return self.reply(200,{'allowed':controller.browser_dispatch(value)})
                except Exception:return self.reply(200,{'allowed':False})
            if self.path not in ('/robot/settings','/neurobro/login/start'):return self.reply(404,{'error':'NOT_FOUND'})
            if not self.authorized(True) or self.headers.get('Origin')!=origin:return self.reply(403,{'error':'FORBIDDEN'})
            try:
                value=self.json_body()
                if self.path=='/neurobro/login/start':
                    if value!={}:raise ValueError
                    try:return self.reply(200,login_grant())
                    except Exception:return self.reply(503,{'error':'NEUROBRO_LOGIN_UNAVAILABLE'})
                validate_settings(value)
                return self.reply(200,controller.robot(value))
            except Exception:return self.reply(400,{'error':'ROBOT_CONTROL_REJECTED'})
        def json_body(self):
            from .http_client import unique
            length=self.headers.get('Content-Length','')
            if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0<int(length)<=1024 or self.headers.get('Content-Type')!='application/json':raise ValueError
            self.connection.settimeout(5)
            return json.loads(self.rfile.read(int(length)),object_pairs_hook=unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    return Handler

def main():
    os.umask(0o077)
    controller=Controller(os.getenv('OFFICE_DATA_DIR','/data'))
    server=ThreadingHTTPServer(('0.0.0.0',8787),handler(controller,os.environ['OFFICE_READ_TOKEN'],os.environ['OFFICE_CONTROL_TOKEN'],os.getenv('OFFICE_DASHBOARD_ORIGIN','https://harun5231.github.io')))
    stalled_actor=threading.Event()
    def stop(*args):
        controller.stopping.set();controller.robot_wakeup.set();controller.account_wakeup.set();controller.protection.wakeup.set()
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    def watchdog():
        while not controller.stopping.wait(2):
            if not controller.healthy():
                with controller.lock:
                    elapsed=time.monotonic()-controller.worker_progress
                    actor_failed=not controller.thread.is_alive() or elapsed>=ACTOR_STALL_SECONDS
                    protection_failed=(not controller.protection_thread.is_alive()
                        or time.monotonic()-controller.protection.progress_at>=PROTECTION_STALL_SECONDS)
                    if elapsed>=ACTOR_STALL_SECONDS:stalled_actor.set()
                code=('WORKER_PROTECTION_MONITOR_UNAVAILABLE' if protection_failed else
                    'WORKER_ACTOR_UNAVAILABLE' if actor_failed else 'WORKER_ACCOUNT_READER_UNAVAILABLE')
                controller.failure(code,account=not actor_failed and not protection_failed)
                controller.stopping.set();controller.robot_wakeup.set();controller.account_wakeup.set();controller.protection.wakeup.set();server.shutdown();return
    threading.Thread(target=watchdog,daemon=True).start()
    try:server.serve_forever()
    finally:
        controller.stopping.set();controller.robot_wakeup.set();controller.account_wakeup.set();controller.protection.wakeup.set();server.server_close()
        controller.drain(timeout=0 if stalled_actor.is_set() else SHUTDOWN_DRAIN_SECONDS)

from agent.native_status_bridge import install_office_status as _install_native_agent_status
_install_native_agent_status(globals())

if __name__=='__main__':main()
