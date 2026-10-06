"""One ON/OFF research pipeline ending at the Binance order gateway."""
from dataclasses import asdict
import fcntl
import json
import re
import time
from pathlib import Path
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from .core import D,Review,day,now,risk_check,preflight
from .account_state import (account_state,screening_contract,MANUAL_ONLY_SYMBOLS,MAX_REPLACEMENTS)
from .robot_store import RobotStore
from .order_gateway import OrderGateway,GatewayUnavailable,NOT_CONNECTED,build_intent,require_implementation
from .neuroapi import SETUP_SCHEMA,ResearchPaused,selections,setup
from .prompts import ANALYSIS
from .analysis import analysis_context
from .diagnostics import validation_code
from .robot_provenance import VERSION,stamp,verify
from .research_guard import research_reads,ResearchReadPaused,gateway_transaction_reads

class Coordinator:
    def __init__(self,ledger,neuro,market,account_factory,stopping=None,gateway=None):
        self.ledger=ledger;self.db=ledger.db;self.store=RobotStore(self.db)
        self.neuro=neuro;self.market=market;self.account_factory=account_factory;self.stopping=stopping
        self.gateway=gateway if gateway is not None else OrderGateway()
    def tick(self):
        # Processes share the cycle lock; overlapping coordinators cannot spend twice.
        path=Path(self.db.execute('PRAGMA database_list').fetchone()[2]).parent/'cycle.lock'
        try:lock=path.open('a')
        except OSError:return self.store.report('REJECTED',reason='ROBOT_CYCLE_LOCK_UNAVAILABLE')
        with lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return self.store.report('WAITING',reason='WORKER_BUSY')
            except OSError:return self.store.report('REJECTED',reason='ROBOT_CYCLE_LOCK_UNAVAILABLE')
            today=day()
            try:
                with research_reads(lambda:self.allowed(today)):return self._tick()
            except ResearchReadPaused:
                return self.store.report('WAITING',wait_reason=self.pause_reason())
            except Exception as error:
                reason=str(error)
                allowed={'ROBOT_ACCOUNT_UNAVAILABLE','BINANCE_NOT_CONFIGURED','BINANCE_AUTH_FAILED','BINANCE_IP_RESTRICTED','BINANCE_PERMISSION_DENIED','BINANCE_CLOCK_ERROR','BINANCE_ACCOUNT_UNAVAILABLE','BINANCE_ENDPOINT_DENIED'}
                return self.store.report('REJECTED',reason=reason if reason in allowed else 'ROBOT_PREFLIGHT_NEEDS_REVIEW')
    def save_cycle(self,cycle,data,state='ACTIVE'):
        self.db.execute('UPDATE robot_cycles SET data=?,state=? WHERE id=?',(json.dumps(data),state,cycle))
    def research_budget(self,data,results):
        ready=sum(r['status'] in ('READY_FOR_EXECUTION','EXECUTION_BLOCKED','ENTRY_PENDING','POSITION_PROTECTED','CLOSED') for r in results)
        technical=sum(r['status']=='REJECTED' for r in results)
        return data['target']-ready-technical
    def unfinished_cycle(self,row):
        data=json.loads(row['data']);results=self.store.results(row['id'])
        if self.research_budget(data,results)<=0:return False
        if data['queue']:return True
        if row['state']=='ACTIVE' and data['screen']==-1:return True
        held=any(r['status']=='HOLD' and r['symbol'] in data.get('round_symbols',[]) for r in results)
        return held and data['replacements']<MAX_REPLACEMENTS
    def reject_queued_symbol(self,cycle,data,symbol,account,reason):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            data['queue'].pop(0);data['seen'].append(symbol)
            self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
                (cycle+':'+VERSION+':'+symbol,cycle,symbol,'REJECTED',None,reason))
            self.save_cycle(cycle,data)
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
        return self.store.report('REJECTED',account,reason)
    def verified_intent(self,row):
        candidate=self.db.execute('SELECT * FROM robot_candidates WHERE id=?',(row['candidate_id'],)).fetchone()
        if not candidate or row['id']!=candidate['id'] or row['symbol']!=candidate['symbol']:raise Review('ORDER_EVIDENCE_UNVERIFIED')
        expected=build_intent(self.store.verified_plan(candidate),candidate['id'])
        if json.loads(row['payload'])!=expected:raise Review('ORDER_EVIDENCE_UNVERIFIED')
        return expected
    def execution_slots(self,account,today):
        # Recompute reservations/receipts after every gateway observation;
        # account GET snapshots may precede an actual pending entry fill.
        return self.store.available_slots(account['running_positions'],account['running_symbols'],today)
    def exposed_symbols(self,account):
        # A confirmed adapter observation can precede the next account GET.
        # Reserve both its slot and symbol until that intent is closed/rejected.
        active=self.db.execute("SELECT symbol FROM order_intents WHERE state IN ('ENTRY_PENDING','POSITION_PROTECTED')")
        return set(account['running_symbols'])|{row['symbol'] for row in active}
    def advance_execution(self,account,today):
        if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        row=self.db.execute("SELECT * FROM order_intents WHERE state IN ('SUBMITTING','NEEDS_REVIEW') ORDER BY rowid LIMIT 1").fetchone()
        if row:return self.store.report('NEEDS_REVIEW',account,row['failure_code'] or 'ORDER_OUTCOME_UNKNOWN')
        rows=list(self.db.execute("SELECT * FROM order_intents WHERE state IN ('ENTRY_PENDING','POSITION_PROTECTED') ORDER BY rowid LIMIT 2"))
        for row in rows:
            if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
            try:require_implementation(self.gateway,'reconcile')
            except GatewayUnavailable:return self.store.report('NEEDS_REVIEW',account,NOT_CONNECTED)
            try:intent=self.verified_intent(row)
            except Exception:return self.mark_unknown(row['id'],row['candidate_id'],account,'ORDER_EVIDENCE_UNVERIFIED')
            if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
            try:
                # A started adapter transaction may need reads to finish protection.
                with gateway_transaction_reads():result=self.gateway.reconcile(intent)
            except Exception:return self.mark_unknown(row['id'],row['candidate_id'],account)
            observed=self.record_gateway_observation(row['id'],row['candidate_id'],result,account)
            if observed['bot_status']=='NEEDS_REVIEW':return observed
            if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        row=self.db.execute("SELECT * FROM robot_candidates WHERE status IN ('READY_FOR_EXECUTION','EXECUTION_BLOCKED') ORDER BY rowid LIMIT 1").fetchone()
        if not row:return None
        try:plan=self.store.verified_plan(row);intent=build_intent(plan,row['id'])
        except Exception:
            self.db.execute("UPDATE robot_candidates SET status='REJECTED',failure_code='ORDER_EVIDENCE_UNVERIFIED' WHERE id=?",(row['id'],))
            return self.store.report('REJECTED',account,'ORDER_EVIDENCE_UNVERIFIED')
        self.db.execute('INSERT OR IGNORE INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
            (row['id'],row['id'],row['symbol'],'READY_FOR_EXECUTION',json.dumps(intent),None,None,now(),now()))
        try:self.verified_intent(self.db.execute('SELECT * FROM order_intents WHERE id=?',(row['id'],)).fetchone())
        except Exception:return self.mark_unknown(row['id'],row['id'],account,'ORDER_EVIDENCE_UNVERIFIED')
        # Expired/exposed unsent intents retire even while the adapter is absent.
        # Otherwise yesterday's blocked intent could stop today's research.
        if row['cycle'].split(':',1)[0]!=today or not 0<=time.time()-plan['rules_checked_at']<=300:
            return self.reject_intent(row,account,'STALE_ORDER_INTENT')
        if row['symbol'] in self.exposed_symbols(account) or row['symbol'] in MANUAL_ONLY_SYMBOLS:
            return self.reject_intent(row,account,'ROBOT_SYMBOL_EXPOSED')
        try:require_implementation(self.gateway)
        except GatewayUnavailable:
            self.db.execute("UPDATE order_intents SET state='EXECUTION_BLOCKED',failure_code=?,updated=? WHERE id=?",(NOT_CONNECTED,now(),row['id']))
            self.db.execute("UPDATE robot_candidates SET status='EXECUTION_BLOCKED',failure_code=? WHERE id=?",(NOT_CONNECTED,row['id']))
            return self.store.report('EXECUTION_BLOCKED',account,NOT_CONNECTED)
        if not self.execution_slots(account,today):return self.store.report('WAITING',account,wait_reason='ROBOT_CAPACITY_FULL')
        if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        try:
            target=plan['risk_target_usdt']
            context,rules=analysis_context(self.market,row['symbol'],target,self.account_factory())
            self.market.fresh(context,row['symbol']);preflight(plan,rules,target)
        except ResearchReadPaused:raise
        except Exception:return self.reject_intent(row,account,'ORDER_PREFLIGHT_REJECTED')
        if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        # Market/commission GETs may take time while a human opens a position.
        # Refresh the live account after those reads and before claiming submit.
        account=account_state(self.account_factory(),self.store,today)
        self.store.report_account(account)
        if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        if row['symbol'] in self.exposed_symbols(account) or row['symbol'] in MANUAL_ONLY_SYMBOLS:
            return self.reject_intent(row,account,'ROBOT_SYMBOL_EXPOSED')
        if not self.execution_slots(account,today):return self.store.report('WAITING',account,wait_reason='ROBOT_CAPACITY_FULL')
        previous_state=self.db.execute('SELECT state FROM order_intents WHERE id=?',(row['id'],)).fetchone()[0]
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if not self.allowed(today):
                self.db.execute('ROLLBACK');return self.store.report('WAITING',account,wait_reason=self.pause_reason())
            changed=self.db.execute("UPDATE order_intents SET state='SUBMITTING',failure_code=NULL,updated=? WHERE id=? AND state IN ('READY_FOR_EXECUTION','EXECUTION_BLOCKED')",(now(),row['id'])).rowcount
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
        if not changed:return self.store.report('NEEDS_REVIEW',account,'ORDER_OUTCOME_UNKNOWN')
        self.store.report('EXECUTING',account)
        if not self.allowed(today):
            # The adapter has not been invoked: this local claim is proven
            # unsent. An already-started request is never reset or replayed.
            self.db.execute("UPDATE order_intents SET state=?,failure_code=?,updated=? WHERE id=? AND state='SUBMITTING'",
                (previous_state,NOT_CONNECTED if previous_state=='EXECUTION_BLOCKED' else None,now(),row['id']))
            return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        try:
            # OFF stops the next callback; an invoked entry/protection callback drains.
            with gateway_transaction_reads():result=self.gateway.submit(intent)
        except GatewayUnavailable:
            # A transport that might have sent a request must report an unknown
            # outcome, never claim this pre-send NOT_CONNECTED exception.
            return self.mark_unknown(row['id'],row['id'],account)
        except Exception:return self.mark_unknown(row['id'],row['id'],account)
        return self.record_gateway_observation(row['id'],row['id'],result,account)
    def reject_intent(self,row,account,reason):
        self.db.execute("UPDATE order_intents SET state='REJECTED',failure_code=?,updated=? WHERE id=?",(reason,now(),row['id']))
        self.db.execute("UPDATE robot_candidates SET status='REJECTED',failure_code=? WHERE id=?",(reason,row['id']))
        return self.store.report('REJECTED',account,reason)
    def mark_unknown(self,intent_id,candidate_id,account,reason='ORDER_OUTCOME_UNKNOWN'):
        self.db.execute("UPDATE order_intents SET state='NEEDS_REVIEW',failure_code=?,updated=? WHERE id=?",(reason,now(),intent_id))
        self.db.execute("UPDATE robot_candidates SET status='NEEDS_REVIEW',failure_code=? WHERE id=?",(reason,candidate_id))
        return self.store.report('NEEDS_REVIEW',account,reason)
    def record_gateway_observation(self,intent_id,candidate_id,result,account):
        row=self.db.execute('SELECT * FROM order_intents WHERE id=?',(intent_id,)).fetchone()
        try:
            intent=self.verified_intent(row)
            if row['candidate_id']!=candidate_id:raise ValueError
            if not isinstance(result,dict) or result.get('source')!='BINANCE_FUTURES':raise ValueError
            state=result['state']
            if state not in ('ENTRY_PENDING','POSITION_PROTECTED','CLOSED','REJECTED'):raise ValueError
            if result['symbol']!=intent['symbol'] or result['client_order_id']!=intent['client_order_id']:raise ValueError
            if not isinstance(result['order_id'],str) or not re.fullmatch(r'[1-9][0-9]{0,29}',result['order_id']):raise ValueError
            quantity=result['filled_quantity']
            if not isinstance(quantity,str) or not re.fullmatch(r'\d+(?:\.\d+)?',quantity):raise ValueError
            filled=D(quantity)
            if not 0<=filled<=D(intent['entry']['quantity']):raise ValueError
            at=datetime.fromisoformat(result['observed_at'])
            if at.tzinfo is None or not 0<=(datetime.now(timezone.utc)-at).total_seconds()<=120:raise ValueError
            if state=='POSITION_PROTECTED':
                if not filled or result.get('sl_confirmed') is not True or result.get('tp_confirmed') is not True:raise ValueError
                for key in ('sl_order_id','tp_order_id'):
                    if not isinstance(result.get(key),str) or not re.fullmatch(r'[1-9][0-9]{0,29}',result[key]):raise ValueError
                if len({result['order_id'],result['sl_order_id'],result['tp_order_id']})!=3:raise ValueError
            if state=='ENTRY_PENDING' and filled>0:raise ValueError
            if state=='REJECTED' and filled>0:raise ValueError
            if state=='CLOSED' and not filled:raise ValueError
            entry_at=datetime.fromisoformat(result['first_fill_at']) if filled else None
            created=datetime.fromisoformat(row['created'])
            if entry_at and (entry_at.tzinfo is None or created.tzinfo is None or not created<=entry_at<=at):raise ValueError
            if state=='CLOSED':
                closed=datetime.fromisoformat(result['closed_at'])
                exit_id=result['exit_order_id']
                if closed.tzinfo is None or not entry_at<=closed<=at:raise ValueError
                if not isinstance(exit_id,str) or not re.fullmatch(r'[1-9][0-9]{0,29}',exit_id) or exit_id==result['order_id']:raise ValueError
            previous=json.loads(row['result']) if row['result'] else None
            if previous:
                transitions={'ENTRY_PENDING':('ENTRY_PENDING','POSITION_PROTECTED','CLOSED','REJECTED'),
                    'POSITION_PROTECTED':('POSITION_PROTECTED','CLOSED'),'CLOSED':('CLOSED',),'REJECTED':('REJECTED',)}
                if state not in transitions[previous['state']] or result['order_id']!=previous['order_id']:raise ValueError
                if filled<D(previous['filled_quantity']) or at<datetime.fromisoformat(previous['observed_at']):raise ValueError
                if D(previous['filled_quantity']) and entry_at!=datetime.fromisoformat(previous['first_fill_at']):raise ValueError
            # Cache only the explicit verified-observation fields, never raw
            # Binance envelopes, URLs, headers, credentials, or adapter prose.
            allowed=('source','state','symbol','client_order_id','order_id','filled_quantity',
                     'observed_at','first_fill_at','sl_confirmed','tp_confirmed','sl_order_id','tp_order_id',
                     'exit_order_id','closed_at')
            recorded={key:result[key] for key in allowed if key in result}
            entry_day=entry_at.astimezone(ZoneInfo('Asia/Bangkok')).date().isoformat() if filled else None
            receipt=self.db.execute('SELECT * FROM robot_entry_receipts WHERE id=?',(intent['client_order_id'],)).fetchone()
            if receipt and (not filled or receipt['symbol']!=intent['symbol'] or receipt['entry_day']!=entry_day or
                datetime.fromisoformat(receipt['confirmed_at'])!=entry_at):raise ValueError
        except Exception:return self.mark_unknown(intent_id,candidate_id,account)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('UPDATE order_intents SET state=?,result=?,failure_code=NULL,updated=? WHERE id=?',
                (state,json.dumps(recorded),now(),intent_id))
            self.db.execute('UPDATE robot_candidates SET status=?,failure_code=NULL WHERE id=?',(state,candidate_id))
            if filled:
                self.db.execute('INSERT OR IGNORE INTO robot_entry_receipts VALUES(?,?,?,?)',
                    (intent['client_order_id'],intent['symbol'],entry_day,result['first_fill_at']))
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
        account=dict(account,bot_entries_today=self.store.entries(day()))
        account['available_slots']=self.execution_slots(account,day())
        return self.store.report(state,account)
    def allowed(self,today):return not (self.stopping and self.stopping.is_set()) and self.store.settings()['robot_on'] and day()==today
    def pause_reason(self):
        if self.stopping and self.stopping.is_set():return 'ROBOT_STOPPING'
        return 'ROBOT_OFF' if not self.store.settings()['robot_on'] else 'ROBOT_DAY_CHANGED'
    def _tick(self):
        if not self.store.settings()['robot_on']:return self.store.report('OFF',wait_reason='ROBOT_OFF')
        if self.stopping and self.stopping.is_set():return self.store.report('WAITING',wait_reason='ROBOT_STOPPING')
        today=day();account=account_state(self.account_factory(),self.store,today)
        self.store.report_account(account)
        if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        advanced=self.advance_execution(account,today)
        # An absent order adapter does not stop analyzing the rest of an already
        # screened batch or replacing actual HOLD decisions. Unknown order or
        # provider outcomes still stop the pipeline immediately.
        blocked=advanced if advanced is not None and advanced['bot_status']=='EXECUTION_BLOCKED' else None
        if advanced is not None and blocked is None:return advanced
        available=self.execution_slots(account,today)
        if not available:return blocked or self.store.report('WAITING',account,wait_reason='ROBOT_CAPACITY_FULL')
        # An interrupted paid request cannot be automatically replayed under a new ID.
        if self.db.execute("SELECT 1 FROM robot_jobs WHERE state IN ('PENDING','NEEDS_REVIEW') LIMIT 1").fetchone() or self.db.execute("SELECT 1 FROM api_requests WHERE state IN ('PENDING','NEEDS_REVIEW') LIMIT 1").fetchone():
            return self.store.report('REJECTED',account,'ROBOT_REQUEST_NEEDS_REVIEW')
        self.neuro.require_key() # Configuration failure claims no paid operation.
        prefix=today+':robot-v9:'
        cycle=prefix+str(self.store.entries(today))
        old=self.db.execute('SELECT * FROM robot_cycles WHERE id=?',(cycle,)).fetchone()
        if not old or old['state']!='NEEDS_REVIEW':
            # A first fill changes the epoch. Finish older paid queues first,
            # including COMPLETE rows written by the previous capacity bug.
            # Exhausted budgets/HOLD rounds are never reopened here.
            prior=list(self.db.execute("SELECT * FROM robot_cycles WHERE day=? AND state IN ('ACTIVE','COMPLETE') AND id LIKE ? ORDER BY entry_epoch,rowid",(today,prefix+'%')))
            for row in prior:
                if self.unfinished_cycle(row):
                    old=row;cycle=row['id'];break
            if not old:
                old=next((row for row in prior if row['state']=='ACTIVE'),None)
                if old:cycle=old['id']
        if not old:
            if self.db.execute("SELECT 1 FROM robot_candidates WHERE status IN ('READY_FOR_EXECUTION','EXECUTION_BLOCKED','NEEDS_REVIEW') LIMIT 1").fetchone():return self.store.report('WAITING',account,wait_reason='ROBOT_CYCLE_COMPLETE')
            if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
            data=dict(target=available,queue=[],seen=[],screen=-1,replacements=0,round_symbols=[])
            self.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',(cycle,today,self.store.entries(today),'ACTIVE',json.dumps(data)))
        else:
            data=json.loads(old['data'])
            if old['state']=='NEEDS_REVIEW':return self.store.report('REJECTED',account,'ROBOT_REQUEST_NEEDS_REVIEW')
            if old['state']=='COMPLETE' and self.unfinished_cycle(old):self.save_cycle(cycle,data)
        results=self.store.results(cycle)
        ready=sum(r['status'] in ('READY_FOR_EXECUTION','EXECUTION_BLOCKED','ENTRY_PENDING','POSITION_PROTECTED','CLOSED') for r in results)
        unsent=self.db.execute("SELECT symbol FROM robot_candidates WHERE status IN ('READY_FOR_EXECUTION','EXECUTION_BLOCKED')")
        ready_unexposed=sum(r['symbol'] not in account['running_symbols'] for r in unsent)
        technical=sum(r['status']=='REJECTED' for r in results)
        # A rejected research opportunity uses budget, not a live account slot.
        # Ready intents reserve current free slots; the original target stays fixed.
        # A matching running position already occupies its real account slot.
        budget=self.research_budget(data,results)
        if budget<=0:
            self.save_cycle(cycle,data,'COMPLETE')
            return blocked or self.store.report('WAITING' if ready else 'REJECTED',account,wait_reason='ROBOT_CYCLE_COMPLETE')
        remaining=min(available-ready_unexposed,budget)
        if remaining<=0:
            self.save_cycle(cycle,data)
            return blocked or self.store.report('WAITING',account,wait_reason='ROBOT_CAPACITY_FULL')
        if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        if data['queue']:
            symbol=data['queue'][0]
            if symbol in self.exposed_symbols(account) or symbol in MANUAL_ONLY_SYMBOLS:
                # Account conflicts are technical rejections, never replacement triggers.
                return self.reject_queued_symbol(cycle,data,symbol,account,'ROBOT_SYMBOL_EXPOSED')
            return self.analyze(cycle,data,symbol,account,today)
        initial=data['screen']==-1
        # Only HOLD decisions from the just-analyzed round are replaceable.
        # A rejected/duplicate/exposed screening result is not a new HOLD, and
        # historical HOLDs must not trigger another paid round after rejection.
        held=sum(r['status']=='HOLD' and r['symbol'] in data.get('round_symbols',[]) for r in results)
        if not initial and not held:
            self.save_cycle(cycle,data,'COMPLETE')
            return blocked or self.store.report('REJECTED' if technical else 'INSUFFICIENT_ACTIONABLE_SETUPS',account,wait_reason='ROBOT_CYCLE_COMPLETE')
        if not initial and data['replacements']>=MAX_REPLACEMENTS:
            self.save_cycle(cycle,data,'COMPLETE')
            return self.store.report('INSUFFICIENT_ACTIONABLE_SETUPS',account,wait_reason='ROBOT_CYCLE_COMPLETE')
        return self.screen(cycle,data,remaining if initial else min(remaining,held),account,today,initial)
    def claim(self,operation,cycle,kind,symbol,today,target=None):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if not self.allowed(today):self.db.execute('ROLLBACK');return False
            self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',(operation,cycle,kind,symbol,'PENDING',target))
            self.db.execute('COMMIT');return True
        except BaseException:self.db.execute('ROLLBACK');raise
    def unclaim_unsent_research(self,operation):
        # Called only when no provider request was sent for this local claim.
        self.db.execute("DELETE FROM robot_jobs WHERE operation=? AND state='PENDING' AND NOT EXISTS (SELECT 1 FROM api_requests WHERE operation=?)",(operation,operation))
    def screen(self,cycle,data,count,account,today,initial):
        if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        catalog=self.market.catalog();prompt,schema=screening_contract(count)
        index=data['screen']+1;operation=cycle+':screening:'+str(index)+':'+str(count)
        if not self.claim(operation,cycle,'SCREENING',None,today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        self.store.report('SCREENING',account)
        if not self.allowed(today):
            self.unclaim_unsent_research(operation)
            return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        try:
            value=self.neuro.ask(operation,prompt,schema,lambda v:selections(v,catalog,count),catalog=catalog,
                continue_if=lambda:self.allowed(today))
            coins=selections(value,catalog,count)
            data['screen']=index
            if not initial:data['replacements']+=1
            exposed=self.exposed_symbols(account)
            eligible=[s for s in coins if s not in data['seen'] and s not in exposed and s not in MANUAL_ONLY_SYMBOLS]
            rejected=[s for s in coins if s not in eligible]
            for symbol in rejected:
                if symbol not in data['seen']:data['seen'].append(symbol)
            data['queue']=eligible
            data['round_symbols']=eligible
            data.pop('replacement_due',None)
            self.db.execute('BEGIN IMMEDIATE')
            self.save_cycle(cycle,data,'ACTIVE')
            self.db.execute("UPDATE robot_jobs SET state='COMPLETE' WHERE operation=?",(operation,))
            self.db.execute('COMMIT')
            return self.store.report('WAITING',account,wait_reason='SCREENING_COMPLETE_ANALYSIS_PENDING' if eligible else 'SCREENING_NO_ELIGIBLE_SYMBOLS')
        except ResearchPaused:
            self.unclaim_unsent_research(operation)
            return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        except Exception:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            self.db.execute("UPDATE robot_jobs SET state='NEEDS_REVIEW' WHERE operation=?",(operation,))
            self.save_cycle(cycle,data,'NEEDS_REVIEW')
            return self.store.report('REJECTED',account,'SCREENING_NEEDS_REVIEW')
    def analyze(self,cycle,data,symbol,account,today):
        if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        operation=cycle+':'+VERSION+':'+symbol
        target=self.store.settings()['risk_target_usdt']
        # Symbol catalog/rules/context are read before the paid call, never guessed.
        try:
            catalog=self.market.catalog()
        except ResearchReadPaused:raise
        except Exception:return self.store.report('REJECTED',account,'MARKET_DATA_UNAVAILABLE')
        if symbol not in catalog:
            return self.reject_queued_symbol(cycle,data,symbol,account,'INVALID_SCREENING_SYMBOL')
        try:
            context,rules=analysis_context(self.market,symbol,target,self.account_factory())
        except ResearchReadPaused:raise
        except Exception:
            return self.store.report('REJECTED',account,'MARKET_DATA_UNAVAILABLE')
        if not self.claim(operation,cycle,'ANALYSIS',symbol,today,target):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        self.store.report('ANALYZING',account)
        if not self.allowed(today):
            self.unclaim_unsent_research(operation)
            return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        plan=None;state='REJECTED';reason=None;unknown=False
        try:
            value=self.neuro.ask(operation,ANALYSIS,SETUP_SCHEMA,lambda v:setup(v,symbol),context,
                continue_if=lambda:self.allowed(today))
            signal=setup(value,symbol)
            if signal.side=='HOLD':state='HOLD'
            else:
                self.store.report('VALIDATING',account)
                self.market.fresh(context,symbol)
                # A received response must be journaled even after OFF. Finish
                # local validation with the rules already read; start no GET.
                if self.allowed(today):
                    try:_,refreshed=analysis_context(self.market,symbol,target,self.account_factory())
                    except ResearchReadPaused:pass
                    else:rules=refreshed
                plan=risk_check(signal,rules,target);plan['risk_target_usdt']=target
                plan['sizing_rules']={k:str(v) if isinstance(v,D) else v for k,v in asdict(rules).items()}
                preflight(plan,rules,target);stamp(self.db,plan,operation);verify(self.db,plan,operation)
                state='READY_FOR_EXECUTION'
        except ResearchPaused:
            self.unclaim_unsent_research(operation)
            return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        except Exception as error:
            reason=validation_code(error)
            row=self.db.execute('SELECT state FROM api_requests WHERE operation=?',(operation,)).fetchone()
            unknown=not row or row[0]!='COMPLETE'
            if row and row[0]=='COMPLETE':self.neuro.record_validation(operation,error)
            plan=None
        data['queue'].pop(0);data['seen'].append(symbol)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',(operation,cycle,symbol,state,json.dumps(plan) if plan else None,reason))
            self.save_cycle(cycle,data,'NEEDS_REVIEW' if unknown else 'ACTIVE')
            self.db.execute('UPDATE robot_jobs SET state=? WHERE operation=?',('NEEDS_REVIEW' if unknown else 'COMPLETE',operation))
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
        return self.store.report(state,account,reason)
