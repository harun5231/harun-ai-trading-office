"""Offline lifecycle model and GET-only preflight. Live submission is disabled.

Injected exchange simulators exercise retained intent/reconciliation logic; the
production transport cannot submit any mutation and CLI activation always fails.

No import from dashboard/controller. Account/order results are reduced to bounded
states and numeric evidence; raw replies, signed URLs and credentials are not saved.
"""
import fcntl
import hashlib
import json
import time
from .core import Ledger,D,day,number
from .state import directory
from .market import Market
from .binance_private import BinanceReadOnly
from .binance_shadow import load_candidates,plan_payload,symbol_preflight,shadow,ShadowError,safe
from .binance_execution_transport import BinanceExecution,LiveError,NotFound,MANUAL_ONLY_SYMBOLS
from .live_arm import require_arm,arm,Disarmed
from .provenance import digest

TERMINAL=frozenset(('FILLED','CANCELED','EXPIRED','EXPIRED_IN_MATCH','REJECTED'))
ALGO_ACTIVE=frozenset(('NEW',))
ALGO_TERMINAL=frozenset(('CANCELED','FINISHED','REJECTED','EXPIRED'))
LIVE_CODES=frozenset(('LIVE_EXECUTION_DISARMED','LOCAL_TERMINAL_REQUIRED','LIVE_OWNERSHIP_UNVERIFIED',
 'LIVE_DAILY_LIMIT','LIVE_SYMBOL_DENIED','LIVE_ACCOUNT_UNSAFE','LIVE_RECONCILIATION_REQUIRED',
 'LIVE_REQUEST_UNCERTAIN','LIVE_PROVIDER_REJECTED','LIVE_CONFIG_UNCONFIRMED','LIVE_STATE_CHANGED',
 'PROTECTION_INCOMPLETE','LIVE_WORKER_BUSY','LIVE_FAILED_CLOSED','LIVE_SUBMISSION_DISABLED'))

def fail(code):raise LiveError(code)
def clean_error(e):
    code=str(e)
    return code if code in LIVE_CODES else safe(e) if isinstance(e,ShadowError) else 'LIVE_FAILED_CLOSED'

def decimal_amount(value):
    if not isinstance(value,str) or len(value)>64:fail('LIVE_OWNERSHIP_UNVERIFIED')
    import re
    if not re.fullmatch(r'-?\d+(?:\.\d+)?',value):fail('LIVE_OWNERSHIP_UNVERIFIED')
    return D(value)

class Store:
    def __init__(self,db):
        self.db=db
        db.executescript('''CREATE TABLE IF NOT EXISTS live_records(
          id TEXT PRIMARY KEY,day TEXT NOT NULL,symbol TEXT NOT NULL,plan TEXT NOT NULL,
          digest TEXT NOT NULL,state TEXT NOT NULL,slot INTEGER NOT NULL DEFAULT 0,
          filled TEXT NOT NULL DEFAULT '0',reason TEXT,UNIQUE(day,symbol));
          CREATE TABLE IF NOT EXISTS live_actions(
          setup_id TEXT NOT NULL,action TEXT NOT NULL,method TEXT NOT NULL,path TEXT NOT NULL,
          payload_hash TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(setup_id,action));
          CREATE TABLE IF NOT EXISTS live_events(seq INTEGER PRIMARY KEY,setup_id TEXT,state TEXT,at REAL);''')
    def claim(self,p):
        v=plan_payload(p,day());key=v['setup_id'];data=json.dumps(p,sort_keys=True)
        if p['symbol'] in MANUAL_ONLY_SYMBOLS:fail('LIVE_SYMBOL_DENIED')
        old=self.db.execute('SELECT * FROM live_records WHERE id=?',(key,)).fetchone()
        if old:
            if old['digest']!=digest(p):fail('LIVE_STATE_CHANGED')
            return key
        self.db.execute('INSERT INTO live_records(id,day,symbol,plan,digest,state) VALUES(?,?,?,?,?,?)',
            (key,day(),p['symbol'],data,digest(p),'PREPARED'))
        return key
    def row(self,key):
        row=self.db.execute('SELECT * FROM live_records WHERE id=?',(key,)).fetchone()
        if not row:fail('LIVE_OWNERSHIP_UNVERIFIED')
        p=json.loads(row['plan'])
        if row['digest']!=digest(p) or row['symbol']!=p['symbol'] or plan_payload(p,row['day'])['setup_id']!=key:fail('LIVE_STATE_CHANGED')
        return row,p,plan_payload(p,row['day'])
    def state(self,key,state,filled=None,reason=None):
        self.db.execute('UPDATE live_records SET state=?,filled=COALESCE(?,filled),reason=? WHERE id=?',(state,str(filled) if filled is not None else None,reason,key))
        self.db.execute('INSERT INTO live_events(setup_id,state,at) VALUES(?,?,?)',(key,state,time.time()))
    def action(self,key,action):return self.db.execute('SELECT * FROM live_actions WHERE setup_id=? AND action=?',(key,action)).fetchone()
    def intent(self,key,action,method,path,p):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row,_,_=self.row(key)
            if self.action(key,action):fail('LIVE_RECONCILIATION_REQUIRED')
            if action=='ENTRY':
                if row['day']!=day():fail('LIVE_STATE_CHANGED')
                count=self.db.execute('SELECT COUNT(*) FROM live_records WHERE day=? AND slot=1',(day(),)).fetchone()[0]
                if count>=2:fail('LIVE_DAILY_LIMIT')
                self.db.execute('UPDATE live_records SET slot=1 WHERE id=?',(key,))
            self.db.execute('INSERT INTO live_actions VALUES(?,?,?,?,?,?)',(key,action,method,path,digest(p),'PENDING'))
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
    def report(self):
        return [dict(symbol=r['symbol'],state=r['state'],filled=r['filled'],failure_code=r['reason'],business_day=r['day'])
                for r in self.db.execute('SELECT symbol,state,filled,reason,day FROM live_records ORDER BY day,id')]

class Executor:
    def __init__(self,ledger,client,market,armed=require_arm,sleep=time.sleep):
        self.ledger=ledger;self.store=Store(ledger.db);self.client=client;self.market=market;self.armed=armed;self.sleep=sleep;self.active=None
        client.authorize=self.authorize
    def authorize(self,method,path,p):
        if self.armed() is not True:return False
        if not self.active:return False
        key,action=self.active;row,plan,v=self.store.row(key)
        a=self.store.action(key,action)
        if not a or a['state']!='PENDING' or a['method']!=method or a['path']!=path or a['payload_hash']!=digest(p):return False
        if plan['symbol'] in MANUAL_ONLY_SYMBOLS:return False
        expected={
          'MARGIN':('POST','/fapi/v1/marginType',{'symbol':plan['symbol'],'marginType':'CROSSED'}),
          'LEVERAGE':('POST','/fapi/v1/leverage',{'symbol':plan['symbol'],'leverage':75}),
          'ENTRY':('POST','/fapi/v1/order',v['entry_order']['payload']),
          'TP':('POST','/fapi/v1/algoOrder',v['take_profit_order']['payload']),
          'SL':('POST','/fapi/v1/algoOrder',v['stop_loss_order']['payload']),
          'CANCEL_ENTRY':('DELETE','/fapi/v1/order',{'symbol':plan['symbol'],'origClientOrderId':v['client_ids']['ENTRY']}),
          'CANCEL_TP':('DELETE','/fapi/v1/algoOrder',{'clientAlgoId':v['client_ids']['TP']}),
          'CANCEL_SL':('DELETE','/fapi/v1/algoOrder',{'clientAlgoId':v['client_ids']['SL']})}
        return expected.get(action)==(method,path,p)
    def mutation(self,key,action,method,path,p):
        self.armed();self.store.intent(key,action,method,path,p);self.active=(key,action)
        result='ACK'
        try:self.client.scoped(method,path,p)
        except LiveError as e:result='REJECTED' if str(e)=='LIVE_PROVIDER_REJECTED' else 'UNKNOWN'
        except Exception:result='UNKNOWN'
        finally:self.active=None
        self.ledger.db.execute('UPDATE live_actions SET state=? WHERE setup_id=? AND action=?',(result,key,action))
        return result
    def query(self,v,leg):
        if leg=='ENTRY':path='/fapi/v1/order';params={'symbol':v['symbol'],'origClientOrderId':v['client_ids'][leg]}
        else:path='/fapi/v1/algoOrder';params={'clientAlgoId':v['client_ids'][leg]}
        try:return self.client.scoped('GET',path,params)
        except NotFound:return None
    def entry_evidence(self,v,order):
        p=v['entry_order']['payload']
        if not isinstance(order,dict):fail('LIVE_RECONCILIATION_REQUIRED')
        for k in ('symbol','side','positionSide','type','timeInForce'):
            if order.get(k)!=p[k]:fail('LIVE_OWNERSHIP_UNVERIFIED')
        if order.get('clientOrderId')!=p['newClientOrderId']:fail('LIVE_OWNERSHIP_UNVERIFIED')
        if number(order.get('origQty'))!=number(p['quantity']) or number(order.get('price'))!=number(p['price']):fail('LIVE_OWNERSHIP_UNVERIFIED')
        filled=decimal_amount(order.get('executedQty'));status=order.get('status')
        if not 0<=filled<=number(p['quantity']) or status not in TERMINAL|{'NEW','PARTIALLY_FILLED'}:fail('LIVE_OWNERSHIP_UNVERIFIED')
        if status=='FILLED' and filled!=number(p['quantity']):fail('LIVE_OWNERSHIP_UNVERIFIED')
        return status,filled
    def algo_evidence(self,v,leg,order):
        p=v['take_profit_order' if leg=='TP' else 'stop_loss_order']['payload']
        if not isinstance(order,dict):fail('PROTECTION_INCOMPLETE')
        for k in ('symbol','side','positionSide','algoType','workingType','clientAlgoId'):
            if order.get(k)!=p[k]:fail('LIVE_OWNERSHIP_UNVERIFIED')
        if order.get('orderType')!=p['type'] or order.get('closePosition') is not True or number(order.get('triggerPrice'))!=number(p['triggerPrice']):fail('LIVE_OWNERSHIP_UNVERIFIED')
        status=order.get('algoStatus')
        if status not in ALGO_ACTIVE|ALGO_TERMINAL|{'TRIGGERING','TRIGGERED'}:fail('LIVE_OWNERSHIP_UNVERIFIED')
        return status
    def position(self,symbol):
        self.client.sync_time();rows=self.client.signed_get('/fapi/v3/positionRisk',symbol)
        if not isinstance(rows,list) or len(rows)>1:fail('LIVE_OWNERSHIP_UNVERIFIED')
        if not rows:return D(0)
        r=rows[0]
        if r.get('symbol')!=symbol or r.get('positionSide')!='BOTH':fail('LIVE_OWNERSHIP_UNVERIFIED')
        return decimal_amount(r.get('positionAmt'))
    def exclusive_orders(self,key,v):
        for path,field,legs in (('/fapi/v1/openOrders','clientOrderId',('ENTRY',)),('/fapi/v1/openAlgoOrders','clientAlgoId',('TP','SL'))):
            self.client.sync_time();rows=self.client.signed_get(path,v['symbol'])
            if not isinstance(rows,list):fail('LIVE_OWNERSHIP_UNVERIFIED')
            for r in rows:
                leg=next((leg for leg in legs if r.get(field)==v['client_ids'][leg]),None)
                if r.get('symbol')!=v['symbol'] or leg is None or not self.store.action(key,leg):fail('LIVE_OWNERSHIP_UNVERIFIED')
                if leg=='ENTRY':self.entry_evidence(v,r)
                else:self.algo_evidence(v,leg,r)
    def account(self):
        a=self.client.check()
        if a.get('status')!='BINANCE_CONNECTED' or a.get('can_trade') is not True or a.get('position_mode')!='ONE_WAY' or a.get('multi_assets_margin') is not False:fail('LIVE_ACCOUNT_UNSAFE')
        return a
    def start(self,p):
        self.armed()
        if p['symbol'] in MANUAL_ONLY_SYMBOLS:fail('LIVE_SYMBOL_DENIED')
        v=plan_payload(p,day());key=v['setup_id']
        existing=self.ledger.db.execute('SELECT id FROM live_records WHERE id=?',(key,)).fetchone()
        if existing:return self.reconcile(key)
        if self.ledger.db.execute('SELECT COUNT(*) FROM live_records WHERE day=? AND slot=1',(day(),)).fetchone()[0]>=2:fail('LIVE_DAILY_LIMIT')
        # No adoption based only on recognizable IDs. They must be absent before intent.
        for leg in ('ENTRY','TP','SL'):
            if self.query(v,leg) is not None:fail('LIVE_OWNERSHIP_UNVERIFIED')
        mutations,_=symbol_preflight(self.client,self.market,p,self.account())
        key=self.store.claim(p)
        for action in ('MARGIN','LEVERAGE'):
            required='SET_MARGIN_TYPE_CROSS' if action=='MARGIN' else 'SET_LEVERAGE_75'
            if required not in mutations:continue
            # Recheck candidate flat/manual orders before changing its configuration.
            symbol_preflight(self.client,self.market,p,self.account())
            params={'symbol':p['symbol'],'marginType':'CROSSED'} if action=='MARGIN' else {'symbol':p['symbol'],'leverage':75}
            path='/fapi/v1/marginType' if action=='MARGIN' else '/fapi/v1/leverage'
            self.mutation(key,action,'POST',path,params)
            self.client.sync_time();cfg=self.client.signed_get('/fapi/v1/symbolConfig',p['symbol'])
            field,target=('marginType','CROSSED') if action=='MARGIN' else ('leverage',75)
            if not isinstance(cfg,list) or len(cfg)!=1 or cfg[0].get('symbol')!=p['symbol'] or cfg[0].get(field)!=target:fail('LIVE_CONFIG_UNCONFIRMED')
        self.armed()
        mutations,_=symbol_preflight(self.client,self.market,p,self.account())
        if mutations:fail('LIVE_CONFIG_UNCONFIRMED')
        if self.query(v,'ENTRY') is not None:fail('LIVE_OWNERSHIP_UNVERIFIED')
        self.store.state(key,'ENTRY_SUBMITTED')
        self.mutation(key,'ENTRY','POST','/fapi/v1/order',v['entry_order']['payload'])
        return self.reconcile(key)
    def protect(self,key,v):
        self.store.state(key,'PROTECTION_INCOMPLETE')
        statuses={}
        # Stop loss first. Always attempt both legs independently on first send.
        for leg in ('SL','TP'):
            try:
                order=self.query(v,leg)
                if order is None:
                    if self.store.action(key,leg):continue # unresolved send: never retry it
                    payload=v['stop_loss_order' if leg=='SL' else 'take_profit_order']['payload']
                    self.mutation(key,leg,'POST','/fapi/v1/algoOrder',payload)
                    order=self.query(v,leg)
                if not self.store.action(key,leg):fail('LIVE_OWNERSHIP_UNVERIFIED')
                statuses[leg]=self.algo_evidence(v,leg,order)
            except (LiveError,Disarmed):continue
        return statuses=={'SL':'NEW','TP':'NEW'}
    def reconcile(self,key):
        row,p,v=self.store.row(key)
        if row['state'] in ('CLOSED','REJECTED'):return row['state']
        if not self.store.action(key,'ENTRY'):
            self.store.state(key,'NEEDS_REVIEW',reason='LIVE_RECONCILIATION_REQUIRED');return 'NEEDS_REVIEW'
        order=self.query(v,'ENTRY')
        if order is None:
            if self.store.action(key,'ENTRY')['state']=='REJECTED' and self.position(p['symbol'])==0:
                self.ledger.db.execute('UPDATE live_records SET slot=0 WHERE id=?',(key,))
                self.store.state(key,'REJECTED',reason='LIVE_PROVIDER_REJECTED');return 'REJECTED'
            self.store.state(key,'RECONCILIATION_REQUIRED',reason='LIVE_REQUEST_UNCERTAIN');return 'RECONCILIATION_REQUIRED'
        status,filled=self.entry_evidence(v,order)
        self.exclusive_orders(key,v)
        amount=self.position(p['symbol'])
        if filled==0:
            if amount!=0:fail('LIVE_OWNERSHIP_UNVERIFIED')
            state='CLOSED' if status in TERMINAL else 'ENTRY_PENDING'
            self.store.state(key,state,filled);return state
        self.store.state(key,'ENTRY_PARTIAL' if status not in TERMINAL else 'ENTRY_CONFIRMED',filled)
        if status not in TERMINAL:
            # Clear ONLY this bot entry's remainder before permitting another setup.
            if not self.store.action(key,'CANCEL_ENTRY'):
                self.mutation(key,'CANCEL_ENTRY','DELETE','/fapi/v1/order',{'symbol':p['symbol'],'origClientOrderId':v['client_ids']['ENTRY']})
            try:
                order=self.query(v,'ENTRY');status,filled=self.entry_evidence(v,order)
            except LiveError:status='PARTIALLY_FILLED' # Keep known fill; protect it, block next.
            amount=self.position(p['symbol'])
        expected=filled if p['side']=='LONG' else -filled
        if amount==0:
            if status not in TERMINAL:fail('LIVE_RECONCILIATION_REQUIRED')
            # Flat is not enough: prove one owned protective leg finished execution.
            legs={leg:self.query(v,leg) for leg in ('TP','SL')}
            if any(o is None and self.store.action(key,leg) for leg,o in legs.items()):fail('LIVE_RECONCILIATION_REQUIRED')
            evidence={leg:self.algo_evidence(v,leg,o) for leg,o in legs.items() if o is not None and self.store.action(key,leg)}
            if not any(s=='FINISHED' for s in evidence.values()):fail('LIVE_RECONCILIATION_REQUIRED')
            for leg,s in evidence.items():
                if s in ALGO_ACTIVE:
                    action='CANCEL_'+leg
                    if not self.store.action(key,action):self.mutation(key,action,'DELETE','/fapi/v1/algoOrder',{'clientAlgoId':v['client_ids'][leg]})
                    o=self.query(v,leg)
                    if o is None or self.algo_evidence(v,leg,o) not in ALGO_TERMINAL:fail('LIVE_RECONCILIATION_REQUIRED')
                elif s not in ALGO_TERMINAL:fail('LIVE_RECONCILIATION_REQUIRED')
            if self.position(p['symbol'])!=0:fail('LIVE_OWNERSHIP_UNVERIFIED')
            self.store.state(key,'CLOSED',filled);return 'CLOSED'
        if amount!=expected:fail('LIVE_OWNERSHIP_UNVERIFIED')
        protected=self.protect(key,v)
        state='POSITION_PROTECTED' if protected and status in TERMINAL else 'PROTECTION_INCOMPLETE'
        self.store.state(key,state,filled,reason=None if state=='POSITION_PROTECTED' else 'PROTECTION_INCOMPLETE')
        return state
    def tick(self):
        self.armed()
        # Reconcile ALL prior-day lifecycles before considering fresh candidates.
        rows=self.ledger.db.execute("SELECT id FROM live_records WHERE state NOT IN ('CLOSED','REJECTED') ORDER BY day,id").fetchall()
        for row in rows:
            try:state=self.reconcile(row['id'])
            except Exception as e:
                saved,_,_=self.store.row(row['id'])
                self.store.state(row['id'],'PROTECTION_INCOMPLETE' if D(saved['filled'])>0 else 'NEEDS_REVIEW',reason=clean_error(e))
                raise
            if state not in ('POSITION_PROTECTED','CLOSED','REJECTED'):return self.result(state)
        try:plans=load_candidates(self.ledger,day())
        except ShadowError as e:
            if str(e)=='NO_PERSISTED_SETUP' and rows:return self.result('MONITORING')
            raise
        for p in plans:
            key=plan_payload(p,day())['setup_id']
            if self.ledger.db.execute('SELECT id FROM live_records WHERE id=?',(key,)).fetchone():continue
            if self.ledger.db.execute('SELECT COUNT(*) FROM live_records WHERE day=? AND slot=1',(day(),)).fetchone()[0]>=2:fail('LIVE_DAILY_LIMIT')
            state=self.start(p) # Re-reads real available balance for EACH new entry.
            if state not in ('POSITION_PROTECTED','CLOSED','REJECTED'):return self.result(state)
        report=self.store.report()
        return self.result('CLOSED' if report and all(r['state'] in ('CLOSED','REJECTED') for r in report) else 'MONITORING')
    def result(self,state):return dict(status=state,bot_positions=self.store.report(),live_execution=False,would_submit=False,mode='SHADOW',scheduler_enabled=False)

def live_preflight(root):
    # Existing read-only shadow transport remains physically unable to mutate.
    d=directory(root)
    with (d/'cycle.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:fail('LIVE_WORKER_BUSY')
        ledger=Ledger(d/'ledger.sqlite3')
        try:
            exists=ledger.db.execute("SELECT 1 FROM sqlite_master WHERE name='live_records'").fetchone()
            rows=ledger.db.execute("SELECT id FROM live_records WHERE state NOT IN ('CLOSED','REJECTED')").fetchall() if exists else []
            if rows:
                engine=Executor(ledger,BinanceExecution(),Market(),armed=lambda:False)
                engine.account();report=[]
                for row in rows:
                    stored,p,v=engine.store.row(row['id'])
                    order=engine.query(v,'ENTRY')
                    if order is not None:
                        if not engine.store.action(row['id'],'ENTRY'):fail('LIVE_OWNERSHIP_UNVERIFIED')
                        engine.entry_evidence(v,order);engine.exclusive_orders(row['id'],v)
                    report.append(dict(symbol=p['symbol'],state=stored['state'],action='RECONCILE_EXISTING_LIFECYCLE'))
                return dict(status='LIVE_RECONCILIATION_REQUIRED',bot_positions=report,live_execution=False,would_submit=False,scheduler_enabled=False)
        finally:ledger.db.close()
    from .binance_shadow import run
    result=run(root)
    if any(p['symbol'] in MANUAL_ONLY_SYMBOLS for p in result.get('plans',[])):
        result.update(status='NEEDS_REVIEW',failure_code='LIVE_SYMBOL_DENIED');return result
    if result.get('plans') and all(p['status']=='SHADOW_PREFLIGHT_OK' or p.get('failure_code')=='SHADOW_REQUIRED_MUTATIONS' for p in result['plans']):
        result['status']='LIVE_PREFLIGHT_READY'
    result['live_execution']=False;result['would_submit']=False;result['scheduler_enabled']=False
    return result

def run(root,command):
    try:
        if command=='binance-scheduler':
            from .live_scheduler import run_scheduler
            return run_scheduler(root)
        if command=='binance-live-preflight':return live_preflight(root)
        # Local commands are disabled regardless of any pre-existing arm/run file.
        require_arm()

    except Exception as e:return dict(status=clean_error(e),live_execution=False,scheduler_enabled=False)
