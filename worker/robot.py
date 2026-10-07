"""One ON/OFF research pipeline ending at the Binance order gateway."""
from dataclasses import asdict
import fcntl
import json
import math
import re
import time
from pathlib import Path
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from .core import D,Review,day,now,risk_check,preflight,RISK_MODEL,NORMALIZED_REWARD_RISK_POLICY,validated_risk_target
from .account_state import (account_state,screening_contract,MANUAL_ONLY_SYMBOLS,MAX_REPLACEMENTS)
from .robot_store import RobotStore
from .order_gateway import OrderGateway,GatewayUnavailable,NOT_CONNECTED,build_intent,require_implementation
from .neuroapi import SETUP_SCHEMA,ResearchPaused,selections,setup
from .prompts import ANALYSIS,REPLACEMENT_SCREENING
from .analysis import analysis_context
from .diagnostics import validation_code
from .robot_provenance import VERSION,stamp,verify
from .research_guard import research_reads,ResearchReadPaused,gateway_transaction_reads,gateway_mutations

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
    def completed_research(self,operation,cycle,kind,symbol):
        job=self.db.execute('SELECT * FROM robot_jobs WHERE operation=?',(operation,)).fetchone()
        if not job:return None
        row=self.db.execute('SELECT * FROM api_requests WHERE operation=?',(operation,)).fetchone()
        if (job['cycle']!=cycle or job['kind']!=kind or job['symbol']!=symbol
                or job['state'] not in ('PENDING','COMPLETE') or not row
                or row['state']!='COMPLETE' or not isinstance(row['output'],str) or not row['output']
                or not isinstance(row['body_hash'],str) or not re.fullmatch(r'[a-f0-9]{64}',row['body_hash'])
                or type(row['attempts']) is not int or not 1<=row['attempts']<=3
                or type(row['created']) not in (int,float) or not math.isfinite(row['created']) or row['created']<=0):
            raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
        if kind=='ANALYSIS':validated_risk_target(job['risk_target'])
        elif job['risk_target'] is not None:raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
        return row,job
    def research_job(self,job):
        cycle=self.db.execute("SELECT * FROM robot_cycles WHERE id=? AND state IN ('ACTIVE','COMPLETE')",(job['cycle'],)).fetchone()
        if not cycle or not re.fullmatch(r'\d{4}-\d{2}-\d{2}:robot-v9:(?:0|[1-9]\d{0,11})',cycle['id']):raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
        if cycle['day']!=cycle['id'].split(':',1)[0] or cycle['entry_epoch']!=int(cycle['id'].rsplit(':',1)[1]):raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
        datetime.strptime(cycle['day'],'%Y-%m-%d')
        data=json.loads(cycle['data'])
        if job['kind']=='ANALYSIS':
            if not data['queue'] or data['queue'][0]!=job['symbol'] or job['operation']!=cycle['id']+':'+VERSION+':'+job['symbol']:raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
        elif job['kind']=='SCREENING':
            if data['queue'] or job['symbol'] is not None or job['operation'] not in (
                    cycle['id']+':screening:'+str(data['screen']+1)+':1',
                    cycle['id']+':screening:'+str(data['screen']+1)+':2'):raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
        else:raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
        if not self.completed_research(job['operation'],job['cycle'],job['kind'],job['symbol']):raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
        return cycle,data
    def unresolved_research(self):
        if self.db.execute("SELECT 1 FROM api_requests WHERE state IN ('PENDING','NEEDS_REVIEW') LIMIT 1").fetchone():return True
        for job in self.db.execute("SELECT * FROM robot_jobs WHERE state IN ('PENDING','NEEDS_REVIEW')"):
            if job['state']=='NEEDS_REVIEW':return True
            try:self.research_job(job)
            except Exception:return True
        return False
    def settle_previous_research(self,today,account):
        # A complete paid response may outlive a crash at midnight. Its old
        # scheduling/quota context never creates an order on the new day.
        if self.unresolved_research():return None
        for job in self.db.execute("SELECT * FROM robot_jobs WHERE state='PENDING' ORDER BY rowid"):
            cycle,data=self.research_job(job)
            if cycle['day']>=today:continue
            try:
                api,_=self.completed_research(job['operation'],job['cycle'],job['kind'],job['symbol'])
                value=json.loads(api['output'],parse_float=D)
                if job['kind']=='ANALYSIS':
                    setup(value,job['symbol'],require_declared_rr=False,require_gross_rr=False)
                    if self.db.execute('SELECT 1 FROM robot_candidates WHERE id=?',(job['operation'],)).fetchone():raise ValueError
                    if self.db.execute('SELECT 1 FROM order_intents WHERE candidate_id=?',(job['operation'],)).fetchone():raise ValueError
                else:
                    symbols=value.get('symbols',[]) if isinstance(value,dict) else []
                    selections(value,{symbol:{} for symbol in symbols if isinstance(symbol,str)},int(job['operation'].rsplit(':',1)[1]))
            except Exception:return self.store.report('REJECTED',account,'ROBOT_REQUEST_NEEDS_REVIEW')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                if not self.allowed(today):
                    self.db.execute('ROLLBACK');return self.store.report('WAITING',account,wait_reason=self.pause_reason())
                if job['kind']=='ANALYSIS':
                    self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',(job['operation'],cycle['id'],job['symbol'],'REJECTED',None,'STALE_MARKET_CONTEXT'))
                    data['queue'].pop(0);data['seen'].append(job['symbol'])
                else:data['screen']+=1
                self.db.execute("UPDATE robot_jobs SET state='COMPLETE' WHERE operation=? AND state='PENDING'",(job['operation'],))
                self.save_cycle(cycle['id'],data,'COMPLETE');self.db.execute('COMMIT')
            except BaseException:self.db.execute('ROLLBACK');raise
            return self.store.report('REJECTED',account,'STALE_MARKET_CONTEXT')
        return None
    def context_witness(self,context,symbol):
        self.market.fresh(context,symbol)
        frames={tf:dict(symbol=frame['symbol'],timeframe=frame['timeframe'],last_close_time=frame['candles'][-1]['close_time'])
            for tf,frame in context['timeframes'].items()}
        return dict(version='ANALYSIS_CONTEXT_FRESHNESS_V1',source=context['source'],symbol=symbol,
            fetched_at=context['fetched_at'],server_time=context['server_time'],timeframes=frames,claimed_at=time.time())
    def original_context_fresh(self,row,job):
        try:
            data=json.loads(self.db.execute('SELECT data FROM robot_cycles WHERE id=?',(job['cycle'],)).fetchone()[0])
            witness=data['analysis_contexts'][job['operation']]
            if not isinstance(witness,dict) or set(witness)!={'version','source','symbol','fetched_at','server_time','timeframes','claimed_at'}:raise ValueError
            if witness['version']!='ANALYSIS_CONTEXT_FRESHNESS_V1' or witness['symbol']!=job['symbol'] or witness['source']!='Binance Futures':raise ValueError
            fetched,claimed=witness['fetched_at'],witness['claimed_at']
            if any(type(value) not in (int,float) or not math.isfinite(value) or value<=0 for value in (fetched,claimed)):raise ValueError
            if not fetched<=claimed<=row['created']<=time.time() or row['created']-fetched>self.market.max_age:raise ValueError
            if type(witness['server_time']) is not int or witness['server_time']<=0:raise ValueError
            if not isinstance(witness['timeframes'],dict) or set(witness['timeframes'])!={'1h','15m'}:raise ValueError
            frames={}
            for tf,frame in witness['timeframes'].items():
                if not isinstance(frame,dict) or set(frame)!={'symbol','timeframe','last_close_time'}:raise ValueError
                close=frame['last_close_time']
                if type(close) is not int or close<=0:raise ValueError
                frames[tf]=dict(symbol=frame['symbol'],timeframe=frame['timeframe'],candles=[dict(close_time=close)])
            self.market.fresh(dict(source=witness['source'],symbol=witness['symbol'],fetched_at=fetched,timeframes=frames),job['symbol'])
        except Exception:raise Review('STALE_MARKET_CONTEXT') from None
    def completed_value(self,cached,validate):
        row,job=cached
        created=row['created']
        if type(created) not in (int,float) or not 0<=time.time()-created<=self.market.max_age:
            raise Review('STALE_MARKET_CONTEXT')
        if job['kind']=='ANALYSIS':self.original_context_fresh(row,job)
        value=json.loads(row['output'],parse_float=D)
        validate(value)
        return value
    def replaceable_result(self,row):
        if row['status']=='HOLD':return True
        if row['status']!='REJECTED' or row['plan'] is not None or self.db.execute(
                'SELECT 1 FROM order_intents WHERE candidate_id=? LIMIT 1',(row['id'],)).fetchone():return False
        # Only local RR failures or a recorded request-body rejection may
        # select another coin. An unknown outcome never qualifies.
        if row['failure_code'] in ('RISK_REWARD_BELOW_2','NET_RISK_REWARD_BELOW_2','NET_RISK_REWARD_NOT_TARGET_2'):return True
        if row['failure_code']!='HTTP_422':return False
        return bool(self.db.execute('''SELECT 1 FROM api_requests a JOIN robot_jobs j ON j.operation=a.operation
            WHERE a.operation=? AND a.state='REJECTED_REQUEST_VALIDATION' AND a.failure_code='HTTP_422'
            AND a.output IS NULL AND a.idempotency IS NULL AND a.attempts=1
            AND j.cycle=? AND j.symbol=? AND j.kind='ANALYSIS' AND j.state='REQUEST_REJECTED' ''',
            (row['id'],row['cycle'],row['symbol'])).fetchone())
    def replacement_count(self,data,results):
        return sum(r['symbol'] in data.get('round_symbols',[]) and self.replaceable_result(r) for r in results)
    def research_budget(self,data,results):
        ready=sum(r['status'] in ('READY_FOR_EXECUTION','EXECUTION_BLOCKED','ENTRY_PENDING','POSITION_PROTECTED','CLOSED') for r in results)
        technical=sum(r['status']=='REJECTED' and not self.replaceable_result(r) for r in results)
        return data['target']-ready-technical
    def unfinished_cycle(self,row):
        data=json.loads(row['data']);results=self.store.results(row['id'])
        if self.research_budget(data,results)<=0:return False
        if data['queue']:return True
        if row['state']=='ACTIVE' and data['screen']==-1:return True
        return self.replacement_count(data,results)>0 and data['replacements']<MAX_REPLACEMENTS
    def reject_queued_symbol(self,cycle,data,symbol,account,reason):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            data['queue'].pop(0);data['seen'].append(symbol)
            self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
                (cycle+':'+VERSION+':'+symbol,cycle,symbol,'REJECTED',None,reason))
            self.db.execute("""UPDATE robot_jobs SET state='COMPLETE' WHERE operation=? AND state='PENDING'
                AND EXISTS (SELECT 1 FROM api_requests WHERE operation=robot_jobs.operation
                    AND state='COMPLETE' AND output IS NOT NULL)""",(cycle+':'+VERSION+':'+symbol,))
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
        return self.store.available_slots(account['running_positions'],account['running_symbols'],today,
            pending_symbols=account.get('open_entry_symbols') or ())
    def exposed_symbols(self,account):
        # A confirmed adapter observation can precede the next account GET.
        # Reserve both its slot and symbol until that intent is closed/rejected.
        active=self.db.execute("SELECT symbol FROM order_intents WHERE state IN ('ENTRY_PENDING','POSITION_PROTECTED')")
        return set(account['running_symbols'])|set(account.get('open_entry_symbols') or ())|{row['symbol'] for row in active}
    def advance_execution(self,account,today):
        if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        row=self.db.execute("SELECT * FROM order_intents WHERE state IN ('SUBMITTING','NEEDS_REVIEW') ORDER BY rowid LIMIT 1").fetchone()
        if row:
            # An uncertain submission is never sent again. While ON, existing
            # owned exchange evidence may resolve it using GET-only reconciliation.
            reason=row['failure_code'] or 'ORDER_OUTCOME_UNKNOWN'
            try:
                require_implementation(self.gateway,'reconcile')
                intent=self.verified_intent(row)
            except Exception:return self.store.report('NEEDS_REVIEW',account,reason)
            if not self.gateway_allowed():return self.store.report('WAITING',account,wait_reason=self.pause_reason())
            try:
                with gateway_transaction_reads(),gateway_mutations(lambda:False):
                    result=self.gateway.reconcile(intent)
            except Exception:return self.store.report('NEEDS_REVIEW',account,reason)
            if not isinstance(result,dict) or result.get('state') not in ('ENTRY_PENDING','POSITION_PROTECTED','CLOSED'):
                return self.store.report('NEEDS_REVIEW',account,reason)
            return self.record_gateway_observation(row['id'],row['candidate_id'],result,account,preserve_unknown=True)
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
                with gateway_transaction_reads(),gateway_mutations(self.gateway_allowed):
                    result=self.gateway.reconcile(intent)
            except Exception as error:return self.mark_unknown(row['id'],row['candidate_id'],account,reason=self.gateway_failure_code(error))
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
            # GET proofs can drain; every later SDK mutation checks current ON.
            with gateway_transaction_reads(),gateway_mutations(self.gateway_allowed):
                result=self.gateway.submit(intent)
        except GatewayUnavailable:
            # A transport that might have sent a request must report an unknown
            # outcome, never claim this pre-send NOT_CONNECTED exception.
            return self.mark_unknown(row['id'],row['id'],account)
        except Exception as error:return self.mark_unknown(row['id'],row['id'],account,reason=self.gateway_failure_code(error))
        return self.record_gateway_observation(row['id'],row['id'],result,account)
    def reject_intent(self,row,account,reason):
        self.db.execute("UPDATE order_intents SET state='REJECTED',failure_code=?,updated=? WHERE id=?",(reason,now(),row['id']))
        self.db.execute("UPDATE robot_candidates SET status='REJECTED',failure_code=? WHERE id=?",(reason,row['id']))
        return self.store.report('REJECTED',account,reason)
    def mark_unknown(self,intent_id,candidate_id,account,reason='ORDER_OUTCOME_UNKNOWN'):
        self.db.execute("UPDATE order_intents SET state='NEEDS_REVIEW',failure_code=?,updated=? WHERE id=?",(reason,now(),intent_id))
        self.db.execute("UPDATE robot_candidates SET status='NEEDS_REVIEW',failure_code=? WHERE id=?",(reason,candidate_id))
        return self.store.report('NEEDS_REVIEW',account,reason)
    def gateway_failure_code(self,error):
        # Gateway diagnostics v1; uncertainty remains NEEDS_REVIEW and never retries.
        known=frozenset("ACCOUNT_MODE_INVALID CALLBACK_DEADLINE CAPACITY_FULL CLOCK_INVALID CONFIG_NOT_CONFIRMED ENDPOINT_DENIED ENTRY_ALREADY_EXISTS ENTRY_CANCEL_RACE ENTRY_PROOF_INVALID EXIT_CANCEL_OUTCOME_UNKNOWN EXIT_OUTCOME_UNKNOWN EXIT_PROOF_INVALID EXIT_RACE EXIT_RESAMPLE FEE_CHANGED FILL_PROOF_INVALID FILL_RESAMPLE FOREIGN_EXPOSURE FOREIGN_PENDING_ORDER HISTORY_INCOMPLETE INTENT_INVALID LEVERAGE_CONFIG_INVALID LEVERAGE_UNSUPPORTED LIFECYCLE_UNSTABLE LIQUIDATION_BEFORE_SL MARGIN_CONFIG_INVALID MARGIN_INSUFFICIENT NOT_CONFIGURED ORIGIN_DENIED OUTCOME_UNKNOWN PARAMETERS_INVALID PROOF_INVALID PROTECTION_OUTCOME_UNKNOWN PROTECTION_PROOF_INVALID REQUEST_FAILED SYMBOL_OCCUPIED".split())
        phases={'VALIDATE':{'_validate_intent'},'PROTECTION':{'_algo_proof','_ensure_algo','_cancel_algo','_cancel_exit_remainder','_exits','_ownership','_inventory','_reconcile_filled'},'PREFLIGHT':{'_preflight','_bracket','_account_config'},'CONFIGURE':{'_configure'},'ENTRY':{'_submit_once','_entry','_cancel_entry_remainder','_cleanup_entry'}}
        found=set();trace=error.__traceback__
        expected=Path(__file__).with_name('order_gateway.py').absolute()
        while trace is not None:
            code=trace.tb_frame.f_code
            if Path(code.co_filename).absolute()==expected:
                found.update(phase for phase,names in phases.items() if code.co_name in names)
            trace=trace.tb_next
        phase=next((value for value in ('VALIDATE','PROTECTION','PREFLIGHT','CONFIGURE','ENTRY') if value in found),None)
        if phase is None:return 'ORDER_OUTCOME_UNKNOWN'
        exchange=getattr(error,'code',None)
        if type(exchange) is int and -999999<=exchange<=999999 and exchange!=0:
            return 'ORDER_UNKNOWN_'+phase+'_C'+str(abs(exchange))
        if isinstance(error,Review):
            text=str(error)
            if text.startswith('BINANCE_ORDER_') and text[14:] in known:
                return text+('_'+phase if text[14:] in {'OUTCOME_UNKNOWN','REQUEST_FAILED','PROOF_INVALID'} else '')
        return 'ORDER_UNKNOWN_'+phase
    def record_gateway_observation(self,intent_id,candidate_id,result,account,*,preserve_unknown=False):
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
        except Exception:
            if preserve_unknown:return self.store.report('NEEDS_REVIEW',account,row['failure_code'] or 'ORDER_OUTCOME_UNKNOWN')
            return self.mark_unknown(intent_id,candidate_id,account)
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
    def gateway_allowed(self):return not (self.stopping and self.stopping.is_set()) and self.store.settings()['robot_on']
    def allowed(self,today):return self.gateway_allowed() and day()==today
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
        settled=self.settle_previous_research(today,account)
        if settled is not None:return settled
        available=self.execution_slots(account,today)
        if not available:return blocked or self.store.report('WAITING',account,wait_reason='ROBOT_CAPACITY_FULL')
        # An interrupted paid request cannot be automatically replayed under a new ID.
        if self.unresolved_research():
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
        ready_unexposed=sum(r['symbol'] not in self.exposed_symbols(account) for r in unsent)
        technical=sum(r['status']=='REJECTED' and not self.replaceable_result(r) for r in results)
        # Other rejected research opportunities use budget, not a live account slot.
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
        # HOLD and local fee/RR rejections share the same three replacement rounds.
        # Only the latest round triggers screening; other failures consume budget.
        replaceable=self.replacement_count(data,results)
        if not initial and not replaceable:
            self.save_cycle(cycle,data,'COMPLETE')
            return blocked or self.store.report('REJECTED' if technical else 'INSUFFICIENT_ACTIONABLE_SETUPS',account,wait_reason='ROBOT_CYCLE_COMPLETE')
        if not initial and data['replacements']>=MAX_REPLACEMENTS:
            self.save_cycle(cycle,data,'COMPLETE')
            return self.store.report('INSUFFICIENT_ACTIONABLE_SETUPS',account,wait_reason='ROBOT_CYCLE_COMPLETE')
        return self.screen(cycle,data,remaining if initial else min(remaining,replaceable),account,today,initial)
    def claim(self,operation,cycle,kind,symbol,today,target=None,*,data=None,context=None):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if not self.allowed(today):self.db.execute('ROLLBACK');return False
            witness=self.context_witness(context,symbol) if kind=='ANALYSIS' else None
            self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',(operation,cycle,kind,symbol,'PENDING',target))
            if witness is not None:
                if not isinstance(data,dict) or not data['queue'] or data['queue'][0]!=symbol:raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
                data.setdefault('analysis_contexts',{})[operation]=witness
                self.save_cycle(cycle,data)
            self.db.execute('COMMIT');return True
        except BaseException:self.db.execute('ROLLBACK');raise
    def unclaim_unsent_research(self,operation):
        # Called only when no provider request was sent for this local claim.
        self.db.execute("DELETE FROM robot_jobs WHERE operation=? AND state='PENDING' AND NOT EXISTS (SELECT 1 FROM api_requests WHERE operation=?)",(operation,operation))
    def screen(self,cycle,data,count,account,today,initial):
        if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        catalog=self.market.catalog();prompt,schema=screening_contract(count)
        if not initial:prompt=REPLACEMENT_SCREENING.format(count=count)
        index=data['screen']+1;operation=cycle+':screening:'+str(index)+':'+str(count)
        # A paid response received before a crash is drained without rebuilding
        # its historical request body or sending another provider request.
        matching=list(self.db.execute('SELECT operation FROM robot_jobs WHERE cycle=? AND kind=? AND operation IN (?,?)',
            (cycle,'SCREENING',cycle+':screening:'+str(index)+':1',cycle+':screening:'+str(index)+':2')))
        if len(matching)>1:return self.store.report('REJECTED',account,'ROBOT_REQUEST_NEEDS_REVIEW')
        if matching:
            operation=matching[0]['operation'];count=int(operation.rsplit(':',1)[1]);_,schema=screening_contract(count)
        try:cached=self.completed_research(operation,cycle,'SCREENING',None)
        except Exception:return self.store.report('REJECTED',account,'ROBOT_REQUEST_NEEDS_REVIEW')
        if not cached and not self.claim(operation,cycle,'SCREENING',None,today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        self.store.report('SCREENING',account)
        if not self.allowed(today):
            self.unclaim_unsent_research(operation)
            return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        try:
            context=None if initial else dict(requested_count=count,
                excluded_symbols=sorted(set(data['seen'])|self.exposed_symbols(account)|MANUAL_ONLY_SYMBOLS),
                replacement_instruction='Choose different Binance USD-M USDT perpetual coins that are not in excluded_symbols.')
            value=(self.completed_value(cached,lambda v:selections(v,catalog,count)) if cached else
                self.neuro.ask(operation,prompt,schema,lambda v:selections(v,catalog,count),context,catalog=catalog,
                    continue_if=lambda:self.allowed(today)))
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
        except Exception as error:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            if cached and str(error)=='STALE_MARKET_CONTEXT':
                self.db.execute("UPDATE robot_jobs SET state='COMPLETE' WHERE operation=?",(operation,))
                data['screen']=index
                if not initial:data['replacements']+=1
                data['queue']=[];data['round_symbols']=[]
                self.save_cycle(cycle,data,'COMPLETE')
                return self.store.report('REJECTED',account,'STALE_MARKET_CONTEXT')
            self.db.execute("UPDATE robot_jobs SET state='NEEDS_REVIEW' WHERE operation=?",(operation,))
            self.save_cycle(cycle,data,'NEEDS_REVIEW')
            return self.store.report('REJECTED',account,'SCREENING_NEEDS_REVIEW')
    def analyze(self,cycle,data,symbol,account,today):
        if not self.allowed(today):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        operation=cycle+':'+VERSION+':'+symbol
        try:cached=self.completed_research(operation,cycle,'ANALYSIS',symbol)
        except Exception:return self.store.report('REJECTED',account,'ROBOT_REQUEST_NEEDS_REVIEW')
        target=cached[1]['risk_target'] if cached else self.store.settings()['risk_target_usdt']
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
        if not cached and not self.claim(operation,cycle,'ANALYSIS',symbol,today,target,data=data,context=context):return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        self.store.report('ANALYZING',account)
        if not self.allowed(today):
            self.unclaim_unsent_research(operation)
            return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        plan=None;state='REJECTED';reason=None;unknown=False
        try:
            validate=lambda v:setup(v,symbol,require_declared_rr=False,require_gross_rr=False)
            value=(self.completed_value(cached,validate) if cached else
                self.neuro.ask(operation,ANALYSIS,SETUP_SCHEMA,validate,context,
                    continue_if=lambda:self.allowed(today)))
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
                plan=risk_check(signal,rules,target,risk_model=RISK_MODEL,
                    reward_risk_policy=NORMALIZED_REWARD_RISK_POLICY);plan['risk_target_usdt']=target
                # New Binance TP/SL use the requested Last Price trigger.
                # Historical plans remain immutable and retain their own trigger.
                plan['protection_working_type']='CONTRACT_PRICE'
                plan['sizing_rules']={k:str(v) if isinstance(v,D) else v for k,v in asdict(rules).items()}
                preflight(plan,rules,target);stamp(self.db,plan,operation);verify(self.db,plan,operation)
                state='READY_FOR_EXECUTION'
        except ResearchPaused:
            self.unclaim_unsent_research(operation)
            return self.store.report('WAITING',account,wait_reason=self.pause_reason())
        except Exception as error:
            reason=validation_code(error)
            row=self.db.execute('SELECT state,failure_code FROM api_requests WHERE operation=?',(operation,)).fetchone()
            request_rejected=bool(row and row['state']=='REJECTED_REQUEST_VALIDATION' and row['failure_code']=='HTTP_422')
            local_rejected=not row and reason=='NEUROAPI_REQUEST_LIMIT_EXCEEDED'
            unknown=not (row and row['state']=='COMPLETE' or request_rejected or local_rejected)
            if row and row[0]=='COMPLETE':self.neuro.record_validation(operation,error)
            plan=None
        data['queue'].pop(0);data['seen'].append(symbol)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',(operation,cycle,symbol,state,json.dumps(plan) if plan else None,reason))
            self.save_cycle(cycle,data,'NEEDS_REVIEW' if unknown else 'ACTIVE')
            job_state='NEEDS_REVIEW' if unknown else 'REQUEST_REJECTED' if state=='REJECTED' and reason in ('HTTP_422','NEUROAPI_REQUEST_LIMIT_EXCEEDED') else 'COMPLETE'
            self.db.execute('UPDATE robot_jobs SET state=? WHERE operation=?',(job_state,operation))
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
        return self.store.report(state,account,reason)
