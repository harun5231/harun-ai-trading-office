"""Persistent bounded research coordinator. No execution, no exchange mutation.

One paid operation per tick, claimed before sending. Unknown outcomes block the
cycle, including after restart. Setup/approval and actual entry receipts are
separate tables. This release has NO writer for actual entry receipts.
"""
from dataclasses import asdict
import fcntl
import json
import re
import time
from pathlib import Path
from .core import D,Review,day,now,validated_risk_target,risk_check,preflight
from .neuroapi import SCREEN_SCHEMA,SCREEN_ONE_SCHEMA,SETUP_SCHEMA,selections,setup
from .prompts import SCREENING,ANALYSIS
from .analysis import analysis_context
from .diagnostics import validation_code
from .robot_provenance import VERSION,stamp,verify

SCREENING_ONE='pilihkan 1 coin yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance'
MANUAL_ONLY_SYMBOLS=frozenset(('HYPEUSDT',))
MAX_REPLACEMENTS=3

def screening_contract(count):
    if count==1:return SCREENING_ONE,SCREEN_ONE_SCHEMA
    if count==2:return SCREENING,SCREEN_SCHEMA
    raise Review('INVALID_SCREENING_COUNT')

def slots(running,entries):return max(0,min(2-running,2-entries))

class RobotStore:
    def __init__(self,db):
        self.db=db
        db.executescript('''
        CREATE TABLE IF NOT EXISTS robot_settings(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL,risk TEXT NOT NULL);
        INSERT OR IGNORE INTO robot_settings VALUES(1,0,'5');
        CREATE TABLE IF NOT EXISTS robot_cycles(id TEXT PRIMARY KEY,day TEXT NOT NULL,entry_epoch INTEGER NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL,UNIQUE(day,entry_epoch));
        CREATE TABLE IF NOT EXISTS robot_jobs(operation TEXT PRIMARY KEY,cycle TEXT NOT NULL,kind TEXT NOT NULL,symbol TEXT,state TEXT NOT NULL,risk_target TEXT);
        CREATE TABLE IF NOT EXISTS robot_setups(id TEXT PRIMARY KEY,cycle TEXT NOT NULL,symbol TEXT NOT NULL,status TEXT NOT NULL,plan TEXT,failure_code TEXT,UNIQUE(cycle,symbol));
        CREATE TABLE IF NOT EXISTS robot_decisions(setup_id TEXT PRIMARY KEY,decision TEXT NOT NULL,at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS robot_entry_receipts(id TEXT PRIMARY KEY,symbol TEXT NOT NULL,entry_day TEXT NOT NULL,confirmed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS robot_status(id INTEGER PRIMARY KEY CHECK(id=1),data TEXT NOT NULL);
        ''')
    def settings(self):
        r=self.db.execute('SELECT * FROM robot_settings WHERE id=1').fetchone()
        return {'robot_on':bool(r['enabled']),'risk_target_usdt':format(validated_risk_target(r['risk']),'f')}
    def configure(self,value):
        if not isinstance(value,dict) or not value or set(value)-{'robot_on','risk_target_usdt'}:raise Review('INVALID_ROBOT_SETTING')
        if 'robot_on' in value and type(value['robot_on']) is not bool:raise Review('INVALID_ROBOT_SETTING')
        if 'risk_target_usdt' in value:
            if not isinstance(value['risk_target_usdt'],str):raise Review('INVALID_RISK_TARGET')
            target=format(validated_risk_target(value['risk_target_usdt']),'f')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if 'robot_on' in value:self.db.execute('UPDATE robot_settings SET enabled=? WHERE id=1',(int(value['robot_on']),))
            if 'risk_target_usdt' in value:self.db.execute('UPDATE robot_settings SET risk=? WHERE id=1',(target,))
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
        return self.snapshot()
    def entries(self,today):return self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts WHERE entry_day=?',(today,)).fetchone()[0]
    def results(self,cycle):return list(self.db.execute('SELECT * FROM robot_setups WHERE cycle=?',(cycle,)))
    def snapshot(self):
        row=self.db.execute('SELECT data FROM robot_status WHERE id=1').fetchone()
        value=json.loads(row[0]) if row else dict(bot_status='WAITING',running_positions=None,available_slots=None,manual_exposure=[],checked_at=None)
        value.update(self.settings(),bot_entries_today=self.entries(day()),mode='DRY_RUN',live_enabled=False,live_execution=False,would_submit=False,scheduler_enabled=False)
        if not value['robot_on']:value['bot_status']='WAITING'
        value['setups']=[]
        for row in self.db.execute("SELECT * FROM robot_setups ORDER BY rowid DESC LIMIT 50"):
            item=dict(id=row['id'],symbol=row['symbol'],status=row['status'],failure_code=row['failure_code'])
            if row['plan']:
                try:
                    plan=json.loads(row['plan'])
                    verify(self.db,plan,row['id'])
                    item.update({k:plan[k] for k in ('side','entry','tp','sl','execution_quantity','risk_target_usdt','risk','rr')})
                except Exception:item.update(status='REJECTED',failure_code='ROBOT_SETUP_UNVERIFIED')
            value['setups'].append(item)
        return value
    def report(self,state,account=None,reason=None):
        value=dict(bot_status=state,failure_code=reason,checked_at=now())
        if account:value.update(account)
        else:value.update(running_positions=None,available_slots=None,manual_exposure=[])
        self.db.execute('INSERT OR REPLACE INTO robot_status VALUES(1,?)',(json.dumps(value),))
        return self.snapshot()
    def approve(self,setup_id,decision):
        if not isinstance(setup_id,str) or len(setup_id)>160 or decision not in ('APPROVED','USER_REJECTED'):raise Review('INVALID_APPROVAL')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute('SELECT * FROM robot_setups WHERE id=?',(setup_id,)).fetchone()
            if not row:raise Review('SETUP_NOT_FOUND')
            old=self.db.execute('SELECT decision FROM robot_decisions WHERE setup_id=?',(setup_id,)).fetchone()
            if old:
                if old[0]!=decision:raise Review('DECISION_ALREADY_RECORDED')
            else:
                if row['status']!='SETUP_READY':raise Review('SETUP_NOT_READY')
                verify(self.db,json.loads(row['plan']),setup_id)
                self.db.execute('INSERT INTO robot_decisions VALUES(?,?,?)',(setup_id,decision,now()))
                self.db.execute('UPDATE robot_setups SET status=? WHERE id=?',(decision,setup_id))
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
        return self.snapshot()

def account_state(client,store,today):
    config=client.check()
    if config.get('status')!='BINANCE_CONNECTED' or config.get('position_mode')!='ONE_WAY' or config.get('multi_assets_margin') is not False or config.get('can_trade') is not True:
        raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
    client.sync_time();value=client.signed_get('/fapi/v3/account')
    rows=value.get('positions') if isinstance(value,dict) else None
    if not isinstance(rows,list):raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
    running=set()
    for row in rows:
        if not isinstance(row,dict) or row.get('positionSide')!='BOTH':raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
        amount=row.get('positionAmt');symbol=row.get('symbol')
        if not isinstance(amount,str) or len(amount)>64 or not re.fullmatch(r'-?\d+(?:\.\d+)?',amount) or not isinstance(symbol,str) or not re.fullmatch(r'[A-Z0-9_]{2,30}',symbol):raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
        if D(amount)!=0:running.add(symbol)
    owned={r[0] for r in store.db.execute('SELECT DISTINCT symbol FROM robot_entry_receipts')}-MANUAL_ONLY_SYMBOLS
    entries=store.entries(today)
    return dict(running_positions=len(running),running_symbols=sorted(running),manual_exposure=sorted(running-owned),
        bot_entries_today=entries,available_slots=slots(len(running),entries))

class Coordinator:
    def __init__(self,ledger,neuro,market,account_factory):
        self.ledger=ledger;self.db=ledger.db;self.store=RobotStore(self.db)
        self.neuro=neuro;self.market=market;self.account_factory=account_factory
    def tick(self):
        # Shares the CLI cycle lock; overlapping coordinators cannot spend twice.
        path=Path(self.db.execute('PRAGMA database_list').fetchone()[2]).parent/'cycle.lock'
        with path.open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return self.store.report('WAITING',reason='WORKER_BUSY')
            try:return self._tick()
            except Exception:return self.store.report('REJECTED',reason='ROBOT_PREFLIGHT_NEEDS_REVIEW')
    def save_cycle(self,cycle,data,state='ACTIVE'):
        self.db.execute('UPDATE robot_cycles SET data=?,state=? WHERE id=?',(json.dumps(data),state,cycle))
    def allowed(self,today):return self.store.settings()['robot_on'] and day()==today
    def lifecycle_blocked(self):
        names={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'shadow_plans' in names:
            from .binance_shadow import ShadowStore
            if ShadowStore(self.db).blocked():return True
        if 'live_records' in names:
            # Retained offline model rows are not executions; unresolved models block research.
            return bool(self.db.execute("SELECT 1 FROM live_records WHERE state NOT IN ('CLOSED','REJECTED') LIMIT 1").fetchone())
        return False
    def _tick(self):
        if not self.store.settings()['robot_on']:return self.store.report('WAITING')
        today=day();account=account_state(self.account_factory(),self.store,today)
        available=account['available_slots']
        if not available or self.lifecycle_blocked():return self.store.report('WAITING',account)
        # An interrupted paid request cannot be automatically replayed under a new ID.
        if self.db.execute("SELECT 1 FROM robot_jobs WHERE state IN ('PENDING','NEEDS_REVIEW') LIMIT 1").fetchone():
            return self.store.report('REJECTED',account,'ROBOT_REQUEST_NEEDS_REVIEW')
        self.neuro.require_key() # Configuration failure claims no paid operation.
        cycle=today+':robot-v7:'+str(account['bot_entries_today'])
        old=self.db.execute('SELECT * FROM robot_cycles WHERE id=?',(cycle,)).fetchone()
        if not old:
            if self.db.execute("SELECT 1 FROM robot_setups WHERE status IN ('SETUP_READY','APPROVED') LIMIT 1").fetchone():return self.store.report('SETUP_READY',account)
            if not self.allowed(today):return self.store.report('WAITING',account)
            data=dict(target=available,queue=[],seen=[],screen=-1,replacements=0)
            self.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',(cycle,today,account['bot_entries_today'],'ACTIVE',json.dumps(data)))
        else:
            data=json.loads(old['data'])
            if old['state']=='NEEDS_REVIEW':return self.store.report('REJECTED',account,'ROBOT_REQUEST_NEEDS_REVIEW')
        results=self.store.results(cycle)
        ready=sum(r['status'] in ('SETUP_READY','APPROVED') for r in results)
        technical=sum(r['status']=='REJECTED' for r in results)
        remaining=min(available,data['target'])-ready-technical
        if remaining<=0:
            self.save_cycle(cycle,data,'COMPLETE')
            return self.store.report('SETUP_READY' if ready else 'REJECTED',account)
        if not self.allowed(today):return self.store.report('WAITING',account)
        if data['queue']:
            symbol=data['queue'][0]
            if symbol in account['running_symbols'] or symbol in MANUAL_ONLY_SYMBOLS:
                data['queue'].pop(0);data['seen'].append(symbol);self.save_cycle(cycle,data)
                # Account conflicts are technical rejections, never replacement triggers.
                self.db.execute('INSERT OR IGNORE INTO robot_setups VALUES(?,?,?,?,?,?)',(cycle+':analysis-v7:'+symbol,cycle,symbol,'REJECTED',None,'ROBOT_SYMBOL_EXPOSED'))
                return self.store.report('REJECTED',account,'ROBOT_SYMBOL_EXPOSED')
            return self.analyze(cycle,data,symbol,account,today)
        initial=data['screen']==-1
        held=any(r['status'] in ('HOLD','USER_REJECTED') for r in results)
        # A replacement that returned only seen/exposed symbols can try again within cap.
        if not initial and not held:return self.store.report('REJECTED' if technical else 'WAITING',account)
        if not initial and data['replacements']>=MAX_REPLACEMENTS:
            self.save_cycle(cycle,data,'COMPLETE')
            return self.store.report('INSUFFICIENT_ACTIONABLE_SETUPS',account)
        return self.screen(cycle,data,remaining,account,today,initial)
    def claim(self,operation,cycle,kind,symbol,today,target=None):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if not self.allowed(today):self.db.execute('ROLLBACK');return False
            self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',(operation,cycle,kind,symbol,'PENDING',target))
            self.db.execute('COMMIT');return True
        except BaseException:self.db.execute('ROLLBACK');raise
    def screen(self,cycle,data,count,account,today,initial):
        catalog=self.market.catalog();prompt,schema=screening_contract(count)
        index=data['screen']+1;operation=cycle+':screening:'+str(index)+':'+str(count)
        if not self.claim(operation,cycle,'SCREENING',None,today):return self.store.report('WAITING',account)
        self.store.report('SCREENING',account)
        try:
            value=self.neuro.ask(operation,prompt,schema,lambda v:selections(v,catalog,count),catalog=catalog)
            coins=selections(value,catalog,count)
            data['screen']=index
            if not initial:data['replacements']+=1
            data['queue']=[s for s in coins if s not in data['seen'] and s not in account['running_symbols'] and s not in MANUAL_ONLY_SYMBOLS]
            self.db.execute('BEGIN IMMEDIATE')
            self.save_cycle(cycle,data)
            self.db.execute("UPDATE robot_jobs SET state='COMPLETE' WHERE operation=?",(operation,))
            self.db.execute('COMMIT')
            if not data['queue'] and initial:self.save_cycle(cycle,data,'COMPLETE')
            return self.store.report('WAITING',account)
        except Exception:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            self.db.execute("UPDATE robot_jobs SET state='NEEDS_REVIEW' WHERE operation=?",(operation,))
            self.save_cycle(cycle,data,'NEEDS_REVIEW')
            return self.store.report('REJECTED',account,'SCREENING_NEEDS_REVIEW')
    def analyze(self,cycle,data,symbol,account,today):
        operation=cycle+':'+VERSION+':'+symbol
        target=self.store.settings()['risk_target_usdt']
        # Symbol catalog/rules/context are read before the paid call, never guessed.
        try:
            if symbol not in self.market.catalog():raise Review('INVALID_SCREENING_SYMBOL')
            context,_=analysis_context(self.market,symbol,target)
        except Exception:
            return self.store.report('REJECTED',account,'MARKET_DATA_UNAVAILABLE')
        if not self.claim(operation,cycle,'ANALYSIS',symbol,today,target):return self.store.report('WAITING',account)
        self.store.report('ANALYZING',account)
        plan=None;state='REJECTED';reason=None;unknown=False
        try:
            value=self.neuro.ask(operation,ANALYSIS,SETUP_SCHEMA,lambda v:setup(v,symbol),context)
            signal=setup(value,symbol)
            if signal.side=='HOLD':state='HOLD'
            else:
                self.market.fresh(context,symbol);rules=self.market.rules(symbol)
                plan=risk_check(signal,rules,target);plan['risk_target_usdt']=target
                plan['sizing_rules']={k:str(v) if isinstance(v,D) else v for k,v in asdict(rules).items()}
                preflight(plan,rules,target);stamp(self.db,plan,operation);verify(self.db,plan,operation)
                state='SETUP_READY'
        except Exception as error:
            reason=validation_code(error)
            row=self.db.execute('SELECT state FROM api_requests WHERE operation=?',(operation,)).fetchone()
            unknown=not row or row[0]!='COMPLETE'
            if row and row[0]=='COMPLETE':self.neuro.record_validation(operation,error)
            plan=None
        data['queue'].pop(0);data['seen'].append(symbol)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO robot_setups VALUES(?,?,?,?,?,?)',(operation,cycle,symbol,state,json.dumps(plan) if plan else None,reason))
            self.save_cycle(cycle,data,'NEEDS_REVIEW' if unknown else 'ACTIVE')
            self.db.execute('UPDATE robot_jobs SET state=? WHERE operation=?',('NEEDS_REVIEW' if unknown else 'COMPLETE',operation))
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
        return self.store.report(state,account,reason)
