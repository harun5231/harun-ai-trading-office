"""One durable cycle/day: official research API -> public data -> immutable paper plan."""
import json
import os
import time
import tempfile
from pathlib import Path
from .core import Review,Locked,day,risk_check
from .neuroapi import SCREEN_SCHEMA,SETUP_SCHEMA,selections,setup
from .prompts import SCREENING,ANALYSIS
from .monitor import PaperMonitor
from .analysis import analysis_context
from .diagnostics import safe_code,validation_code
from .provenance import stamp

SOURCE='NEUROAPI_DRY_RUN'
def export(ledger,path,connected=False):
    data=ledger.snapshot(SOURCE,connected);p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',dir=p.parent,prefix='.snapshot-',delete=False) as f:
        tmp=f.name;json.dump(data,f);f.flush();os.fsync(f.fileno())
    try:os.replace(tmp,p)
    finally:Path(tmp).unlink(missing_ok=True)
    return data

class Workflow:
    def __init__(self,ledger,neuro,market,snapshot_path):
        self.ledger=ledger;self.neuro=neuro;self.market=market;self.output=snapshot_path
        ledger.db.execute('CREATE TABLE IF NOT EXISTS cycles(day TEXT PRIMARY KEY,state TEXT,created REAL)')
    def event(self,state,message):
        agent='Neurobro' if state in ('SCREENING','ANALYZING_COIN_1','ANALYZING_COIN_2') else 'Risk Manager' if state in ('VALIDATING','REJECTED') else 'Coordinator'
        self.ledger.event(state,agent,message);return export(self.ledger,self.output)
    def run(self):
        claimed=False
        try:
            self.neuro.require_key()
            if self.ledger.count()>=2:raise Locked('DAILY_LIMIT')
            today=day()
            # Durable claim BEFORE any paid request; an interrupted cycle is never
            # implicitly recreated under a new operation/idempotency key.
            inserted=self.ledger.db.execute('INSERT OR IGNORE INTO cycles VALUES(?,?,?)',(today,'PENDING',time.time())).rowcount
            if not inserted:raise Review('CYCLE_ALREADY_RECORDED')
            claimed=True
            catalog=self.market.catalog()
            self.event('SCREENING','Prompt literal / smart')
            coins=self.neuro.ask(today+':screening',SCREENING,SCREEN_SCHEMA,lambda v:selections(v,catalog),catalog=catalog)
            selected=selections(coins,catalog);self.event('COINS_SELECTED',', '.join(selected))
            queue=list(selected);seen=set();held=0;replacement_count=0;had_hold=False
            while queue or held:
                if self.ledger.count()>=2:break
                if day()!=today:raise Review('CYCLE_DAY_CHANGED')
                if not queue:
                    if replacement_count>=3:break
                    replacement_count+=1
                    self.event('REPLACEMENT_SCREENING','HOLD replacement '+str(replacement_count)+' / 3')
                    catalog=self.market.catalog()
                    try:
                        replacement=self.neuro.ask(today+':replacement-screening:'+str(replacement_count),SCREENING,SCREEN_SCHEMA,
                            lambda v:selections(v,catalog),catalog=catalog)
                        candidates=selections(replacement,catalog)
                    except Review:
                        self.event('REJECTED','REPLACEMENT_SCREENING_FAILED / no automatic retry cycle')
                        break
                    candidate=next((coin for coin in candidates if coin not in seen),None)
                    if candidate is None:continue
                    queue.append(candidate);held-=1
                    self.event('REPLACEMENT_SELECTED',candidate)
                symbol=queue.pop(0)
                if symbol in seen:continue
                seen.add(symbol)
                index=min(len(seen),2)
                if day()!=today:raise Review('CYCLE_DAY_CHANGED')
                if self.ledger.count()>=2:raise Locked('DAILY_LIMIT')
                try:
                    self.event('MARKET_DATA',symbol+' / 1h + 15m')
                    data,_=analysis_context(self.market,symbol)
                    self.event('ANALYZING_COIN_'+str(index),symbol+' / smart')
                    value=self.neuro.ask(today+':analysis:'+symbol,ANALYSIS,SETUP_SCHEMA,lambda v:setup(v,symbol),data)
                    signal=setup(value,symbol)
                    self.event(signal.side,symbol+' / '+signal.side)
                    if signal.side=='HOLD':
                        held+=1;had_hold=True
                        continue
                    self.market.fresh(data,symbol)
                    self.event('VALIDATING',symbol+' / deterministic execution sizing; provider levels unchanged')
                    rules=self.market.rules(symbol)
                    try:plan=risk_check(signal,rules)
                    except Review as error:
                        self.neuro.record_validation(today+':analysis:'+symbol,error)
                        raise
                    plan['protective_plan']={'activation':'AFTER_CONFIRMED_FILL','take_profit':plan['tp'],'stop_loss':plan['sl'],
                        'exit_side':'SELL' if plan['side']=='LONG' else 'BUY','reduce_only':True,
                        'failure_policy':'LIVE_UNIMPLEMENTED_REQUIRES_SEPARATE_REVIEW','simulated':True}
                    if day()!=today:raise Review('CYCLE_DAY_CHANGED')
                    stamp(self.ledger.db,plan,today+':analysis:'+symbol)
                    self.ledger.reserve(plan,SOURCE,rules)
                    self.event('DRY_RUN_READY',symbol+' / ACCEPT / LIMIT + protective TP/SL plan; no submission')
                except Review as error:
                    diagnostic=self.ledger.db.execute('SELECT failure_code FROM api_requests WHERE operation=?',(today+':analysis:'+symbol,)).fetchone()
                    reason=safe_code(diagnostic[0]) if diagnostic and diagnostic[0] else validation_code(error)
                    self.event('REJECTED',symbol+' / '+reason+' / tanpa perubahan angka atau order')
            if had_hold and self.ledger.count()<2:self.event('INSUFFICIENT_ACTIONABLE_SETUPS','Fewer than two valid actionable setups; no forced trade')
            self.ledger.db.execute("UPDATE cycles SET state='COMPLETE' WHERE day=?",(today,))
        except Exception as exc:
            if claimed:self.ledger.db.execute("UPDATE cycles SET state='NEEDS_REVIEW' WHERE day=?",(today,))
            state='NEUROAPI_NOT_CONFIGURED' if isinstance(exc,Review) and str(exc)=='NEUROAPI_NOT_CONFIGURED' else 'LOCKED' if isinstance(exc,Locked) else 'ERROR'
            self.event(state,state if state!='ERROR' else 'CYCLE_STOPPED_NEEDS_REVIEW')
        return export(self.ledger,self.output)
    def monitor(self):
        monitor=PaperMonitor(self.ledger)
        rows=self.ledger.db.execute("SELECT * FROM trades WHERE source=? AND state IN ('ORDER_READY','POSITION_OPEN')",(SOURCE,)).fetchall()
        for row in rows:
            symbol=json.loads(row['plan'])['symbol']
            try:
                mark=self.market.mark(symbol)
                for state in monitor.quote(row['id'],symbol,mark['price'],SOURCE):self.event(state,symbol+' / sampled mark-price paper simulation')
            except Exception: self.event('ERROR','PAPER_QUOTE_UNAVAILABLE')
        return export(self.ledger,self.output)
