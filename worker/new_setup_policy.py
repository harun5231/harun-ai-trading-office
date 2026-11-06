"""Refresh a definitely unsubmitted V4 candidate; retire proven absent claims.

Accepted/uncertain commands are never migrated or replayed. A fresh negative
exchange proof can retire a claim, preserving its plan/payload and history.
No exchange submission is performed here.
"""
from dataclasses import asdict
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .analysis import analysis_context
from .core import D, Review, RISK_MODEL, DYNAMIC_RISK_MODEL, DYNAMIC_REWARD_RISK_POLICY, risk_check, preflight, deserialize_sizing_rules
from .robot_provenance import source_signal, stamp, verify


NOT_ACCEPTED='ORDER_CONFIRMED_NOT_ACCEPTED'
_NEGATIVE_VERSION='BINANCE_NOT_ACCEPTED_V1'
_GATEWAY_SOURCE='9419df472fb5624d2a707117de08264b2adfac21c181728fa9611b65ecb55169'
_NEGATIVE_PATHS=('/fapi/v1/order','/fapi/v3/positionRisk','/fapi/v1/openOrders',
    '/fapi/v1/openAlgoOrders','/fapi/v1/userTrades','/fapi/v1/order')


def _proof_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _utc_seconds(value):
    instant=datetime.fromisoformat(value)
    if instant.tzinfo is None:raise ValueError()
    seconds=instant.timestamp()
    if not math.isfinite(seconds) or seconds<=0:raise ValueError()
    return seconds


def _no_entry_evidence(db,client,operation):
    if db.execute('SELECT 1 FROM robot_entry_receipts WHERE id IN (?,?)',(client,operation)).fetchone():return False
    for table,column in (('fill_priority','client_id'),('entry_fill_samples','client_id')):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
            if db.execute('SELECT 1 FROM '+table+' WHERE '+column+'=?',(client,)).fetchone():return False
            if table=='fill_priority' and db.execute('SELECT 1 FROM fill_priority WHERE intent_id=?',(operation,)).fetchone():return False
    return True


def _negative_claim(source):
    """Return a clock fence for the native finished/unfinished claim matrix."""
    if source['state']=='NEEDS_REVIEW':
        if (type(source['failure_code'])is str and source['failure_code']
                and source['candidate_status']=='NEEDS_REVIEW'
                and source['candidate_failure_code']==source['failure_code']):return 15
    elif source['state']=='SUBMITTING' and source['failure_code']is None:
        if (source['candidate_status'],source['candidate_failure_code'])in(
                ('READY_FOR_EXECUTION',None),('EXECUTION_BLOCKED','BINANCE_ORDER_GATEWAY_NOT_CONNECTED')):
            # updated marks the start, not completion, of the 120s gateway
            # callback. Never infer completion merely from a restarted process.
            return 135
    return None


def _negative_proof_valid(proof,payload,candidate,intent):
    if (type(proof)is not dict or set(proof)!={'version','intent_id','client_order_id','symbol',
            'payload_sha256','plan_sha256','source_attempt','gateway_source_sha256','server_started_ms',
            'server_finished_ms','history_start_ms','history_end_ms','elapsed_seconds','reads'}):return False
    source=proof['source_attempt'];reads=proof['reads']
    if (proof['version']!=_NEGATIVE_VERSION or proof['gateway_source_sha256']!=_GATEWAY_SOURCE
            or proof['intent_id']!=intent['intent_id'] or proof['client_order_id']!=intent['client_order_id']
            or proof['symbol']!=intent['symbol'] or proof['payload_sha256']!=hashlib.sha256(payload.encode()).hexdigest()
            or proof['plan_sha256']!=hashlib.sha256(candidate['plan'].encode()).hexdigest()
            or type(source)is not dict or set(source)!={'state','failure_code','created','updated','candidate_status','candidate_failure_code'}
            or type(reads)is not list or len(reads)!=6):return False
    fence=_negative_claim(source)
    if fence is None:return False
    created,updated=_utc_seconds(source['created']),_utc_seconds(source['updated'])
    first,last,start,end=(proof[k] for k in ('server_started_ms','server_finished_ms','history_start_ms','history_end_ms'))
    elapsed=proof['elapsed_seconds']
    if (any(type(v)is not int for v in(first,last,start,end)) or not 0<created<=updated
            or not updated*1000+fence*1000<=first<=last or not 0<=last-created*1000<72*3600*1000
            or not 0<start<=created*1000-59000 or end!=first or start>=end
            or type(elapsed)not in(int,float) or not math.isfinite(elapsed) or not 0<=elapsed<=30
            or last-first>30000):return False
    for index,(read,path)in enumerate(zip(reads,_NEGATIVE_PATHS)):
        if index in(0,5):
            if read!={'path':path,'exchange_code':-2013}:return False
        elif (type(read)is not dict or set(read)!=({'path','rows','sha256','zero_amounts'}if index==1 else{'path','rows','sha256'}) or read['path']!=path
                or type(read['rows'])is not int or read['rows']<0 or type(read['sha256'])is not str
                or len(read['sha256'])!=64 or any(c not in '0123456789abcdef' for c in read['sha256'])
                or (index==1 and read['zero_amounts']is not True)
                or (index!=1 and (read['rows']!=0 or read['sha256']!=_proof_hash([])))):return False
    return True


def confirmed_absent_result(coordinator,candidate,*,build_intent):
    """Only a preserved native negative proof releases its replacement slot."""
    try:
        if candidate['status']!='REJECTED' or candidate['failure_code']!=NOT_ACCEPTED:return False
        row=coordinator.db.execute('SELECT * FROM order_intents WHERE id=?',(candidate['id'],)).fetchone()
        intent=build_intent(coordinator.store.verified_plan(candidate),candidate['id'])
        if (row is None or row['candidate_id']!=candidate['id'] or row['symbol']!=candidate['symbol']
                or row['state']!='REJECTED' or row['failure_code']!=NOT_ACCEPTED
                or json.loads(row['payload'])!=intent or not _no_entry_evidence(coordinator.db,intent['client_order_id'],intent['intent_id'])):return False
        result=json.loads(row['result'])
        expected={'source':'BINANCE_FUTURES','state':'REJECTED','outcome':'CONFIRMED_NOT_ACCEPTED',
            'symbol':intent['symbol'],'client_order_id':intent['client_order_id']}
        if (type(result)is not dict or set(result)!=set(expected)|{'observed_at','negative_proof','negative_proof_sha256'}
                or any(result[k]!=v for k,v in expected.items())):return False
        proof=result['negative_proof']
        return (result['negative_proof_sha256']==_proof_hash(proof)
            and _negative_proof_valid(proof,row['payload'],candidate,intent)
            and proof['source_attempt']['created']==row['created']
            and _utc_seconds(result['observed_at'])==proof['server_finished_ms']/1000
            and _utc_seconds(row['updated'])>=_utc_seconds(result['observed_at'])-5)
    except (KeyError,TypeError,ValueError,OverflowError,Review):return False


def retire_confirmed_absent(coordinator,row,account,error,*,clock=time.time):
    """Retire an uncertain entry only after fresh, GET-only nonacceptance proof.

    This never retries an intent, reconciles an order, or changes frozen levels.
    The 72-hour bound is Binance's negative lookup retention, not LIMIT expiry.
    """
    from .fill_priority import owned_intent_lock
    from .research_guard import gateway_transaction_reads,gateway_mutations
    gateway=coordinator.gateway;db=coordinator.db
    try:
        if not isinstance(error,gateway._ExchangeError) or error.code!=-2013:return None
        entry_code=gateway._entry.__func__.__code__
        trace=error.__traceback__;matched=False
        while trace is not None:
            matched=matched or trace.tb_frame.f_code is entry_code;trace=trace.tb_next
        if not matched or hashlib.sha256(Path(entry_code.co_filename).read_bytes()).hexdigest()!=_GATEWAY_SOURCE:return None
        if row['state']not in('NEEDS_REVIEW','SUBMITTING') or row['result']is not None:return None
        intent=coordinator.verified_intent(row);client=intent['client_order_id'];today=datetime.fromtimestamp(clock(),ZoneInfo('Asia/Jakarta')).date().isoformat()
        settings=coordinator.store.settings()
        candidate=db.execute('SELECT * FROM robot_candidates WHERE id=?',(row['candidate_id'],)).fetchone()
        if candidate is None:return None
        candidate=dict(candidate);original=dict(row)
        attempt={k:row[k]for k in('state','failure_code','created','updated')}
        attempt.update(candidate_status=candidate['status'],candidate_failure_code=candidate['failure_code'])
        fence=_negative_claim(attempt)
        if fence is None:return None
        def custody():
            current=db.execute('SELECT * FROM order_intents WHERE id=?',(row['id'],)).fetchone()
            current_candidate=db.execute('SELECT * FROM robot_candidates WHERE id=?',(row['candidate_id'],)).fetchone()
            return (current is not None and dict(current)==original and current_candidate is not None
                and dict(current_candidate)==candidate and coordinator.store.settings()==settings
                and coordinator.allowed(today) and _no_entry_evidence(db,client,intent['intent_id'])
                and coordinator.verified_intent(current)==intent)
        with owned_intent_lock(db,client),gateway_transaction_reads(),gateway_mutations(lambda:False),gateway._operation():
            if db.in_transaction or not custody():return None
            began=time.monotonic();wall_started=clock()
            def server_time():
                began_read=time.monotonic();server=gateway._get_server_time();wall=clock()
                if (time.monotonic()-began_read>2 or type(server)is not int or server<=0
                        or abs(server/1000-wall)>5 or wall<wall_started):raise ValueError()
                return server
            first=server_time();created=_utc_seconds(row['created']);updated=_utc_seconds(row['updated'])
            # recvWindow is 5s; the extra 10s covers allowed clock skew/RTT.
            if (not 0<created<=updated or min(first/1000,wall_started)-updated<fence
                    or not 0<=first/1000-created<72*3600):return None
            def absent():
                try:gateway._request('GET','/fapi/v1/order',{'symbol':intent['symbol'],'origClientOrderId':client})
                except gateway._ExchangeError as failure:
                    if failure.code==-2013:return {'path':'/fapi/v1/order','exchange_code':-2013}
                    raise
                raise ValueError()
            reads=[absent()]
            start=int(created*1000)-60000
            for path in _NEGATIVE_PATHS[1:5]:
                params={'symbol':intent['symbol']}
                if path=='/fapi/v1/userTrades':params.update(startTime=start,endTime=first,limit=1000)
                value=gateway._request('GET',path,params)
                if type(value)is not list or len(value)>=1000:raise ValueError()
                if path=='/fapi/v3/positionRisk':
                    if any(type(item)is not dict or item.get('symbol')!=intent['symbol']
                            or item.get('positionSide')!='BOTH' or gateway._decimal(item.get('positionAmt'))!=0 for item in value):raise ValueError()
                elif value:raise ValueError()
                record={'path':path,'rows':len(value),'sha256':_proof_hash(value)}
                if path=='/fapi/v3/positionRisk':record['zero_amounts']=True
                reads.append(record)
            reads.append(absent());last=server_time();elapsed=time.monotonic()-began
            proof=dict(version=_NEGATIVE_VERSION,intent_id=intent['intent_id'],client_order_id=client,symbol=intent['symbol'],
                payload_sha256=hashlib.sha256(row['payload'].encode()).hexdigest(),
                plan_sha256=hashlib.sha256(candidate['plan'].encode()).hexdigest(),
                source_attempt=attempt,gateway_source_sha256=_GATEWAY_SOURCE,
                server_started_ms=first,server_finished_ms=last,history_start_ms=start,history_end_ms=first,
                elapsed_seconds=elapsed,reads=reads)
            if not _negative_proof_valid(proof,row['payload'],candidate,intent):return None
            observed=datetime.fromtimestamp(last/1000,timezone.utc).isoformat()
            result=dict(source='BINANCE_FUTURES',state='REJECTED',outcome='CONFIRMED_NOT_ACCEPTED',symbol=intent['symbol'],
                client_order_id=client,observed_at=observed,negative_proof=proof,negative_proof_sha256=_proof_hash(proof))
            encoded=json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False)
            if len(encoded)>8192:return None
            db.execute('BEGIN IMMEDIATE')
            try:
                if not custody() or clock()<wall_started or time.monotonic()-began>30:
                    db.execute('ROLLBACK');return None
                changed=datetime.now(timezone.utc).isoformat()
                db.execute('UPDATE order_intents SET state=?,result=?,failure_code=?,updated=? WHERE id=?',
                    ('REJECTED',encoded,NOT_ACCEPTED,changed,row['id']))
                db.execute('UPDATE robot_candidates SET status=?,failure_code=? WHERE id=?',('REJECTED',NOT_ACCEPTED,candidate['id']))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK');raise
        return coordinator.store.report('REJECTED',account,NOT_ACCEPTED)
    except Exception:return None


def request_policy(db, operation):
    """A saved paid request retains its own policy, rather than today's default."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='research_browser_requests'").fetchone():
        return None
    row=db.execute('SELECT request_body FROM research_browser_requests WHERE operation=?',
        (operation,)).fetchone()
    if row is None:
        return None
    try:
        body=json.loads(row[0])
        value=body.get('context',{}).get('risk_constraints',{}).get('risk_model')
        if value==DYNAMIC_RISK_MODEL:
            return DYNAMIC_RISK_MODEL
        if value in (None,RISK_MODEL):
            return None
        raise Review('INVALID_COST_MODEL')
    except (ValueError,TypeError,AttributeError):
        raise Review('ROBOT_REQUEST_NEEDS_REVIEW') from None


def serialize_rules(rules):
    return {k:format(v,'f') if isinstance(v,D) else v for k,v in asdict(rules).items()}


def stale_unsubmitted(coordinator,row,*,build_intent,retired=False,orphan=False,clock=time.time):
    """Prove an expired V4 command never crossed the native submit claim.

    The native coordinator durably claims SUBMITTING before gateway invocation;
    uncertain or accepted submissions never return to an eligible state. Any
    result, entry receipt, identity drift or invalid source plan fails this
    pre-POST custody proof. Frozen plans and payloads stay untouched.
    """
    try:
        if retired:
            if row['status']!='REJECTED' or row['failure_code']!='STALE_ORDER_INTENT':return False
        elif orphan:
            if row['status']!='REJECTED' or row['failure_code']!='ORDER_EVIDENCE_UNVERIFIED':return False
        elif row['status']not in('READY_FOR_EXECUTION','EXECUTION_BLOCKED'):return False
        plan=coordinator.store.verified_plan(row);current=clock();stamp=plan['rules_checked_at']
        if (plan.get('risk_model')!=DYNAMIC_RISK_MODEL or type(current)not in(int,float)
                or type(stamp)not in(int,float)or not math.isfinite(current)or not math.isfinite(stamp)
                or not 0<stamp<=current):return False
        today=datetime.fromtimestamp(current,ZoneInfo('Asia/Jakarta')).date().isoformat()
        if current-stamp<=300 and row['cycle'].split(':',1)[0]==today:return False
        expected=build_intent(plan,row['id']);db=coordinator.db
        intents=list(db.execute('SELECT * FROM order_intents WHERE id=? OR candidate_id=?',(row['id'],row['id'])))
        if len(intents)>1:return False
        if intents:
            intent=intents[0]
            states=('REJECTED',)if retired else('READY_FOR_EXECUTION',)if orphan else('READY_FOR_EXECUTION','EXECUTION_BLOCKED')
            reason=('STALE_ORDER_INTENT'if retired else None if intent['state']=='READY_FOR_EXECUTION'
                else 'BINANCE_ORDER_GATEWAY_NOT_CONNECTED')
            if (intent['id']!=row['id']or intent['candidate_id']!=row['id']or intent['symbol']!=row['symbol']
                    or intent['state']not in states or intent['result']is not None or intent['failure_code']!=reason
                    or json.loads(intent['payload'])!=expected):return False
        elif orphan:return False
        return not db.execute('SELECT 1 FROM robot_entry_receipts WHERE id=?',(expected['client_order_id'],)).fetchone()
    except (KeyError,TypeError,ValueError,OverflowError,Review):return False


def retire_stale_unsubmitted(coordinator,row,account,*,build_intent,orphan=False,clock=time.time):
    """Atomically use the existing reject lifecycle only after fresh custody."""
    db=coordinator.db
    if db.in_transaction:return None
    db.execute('BEGIN IMMEDIATE')
    try:
        current=db.execute('SELECT * FROM robot_candidates WHERE id=?',(row['id'],)).fetchone()
        if (current is None or dict(current)!=dict(row)or not stale_unsubmitted(coordinator,current,
                build_intent=build_intent,orphan=orphan,clock=clock)):
            db.execute('ROLLBACK');return None
        result=coordinator.reject_intent(current,account,'STALE_ORDER_INTENT')
        db.execute('COMMIT');return result
    except BaseException:
        db.execute('ROLLBACK');raise


def refresh_unsubmitted(coordinator, row, plan, *, build_intent, clock=time.time):
    if plan.get('risk_model')!=DYNAMIC_RISK_MODEL:
        return plan, None
    db=coordinator.db
    current=db.execute('SELECT * FROM order_intents WHERE id=?',(row['id'],)).fetchone()
    allowed=('READY_FOR_EXECUTION','EXECUTION_BLOCKED')
    if current is not None and (current['state'] not in allowed or current['result'] is not None):
        raise Review('ACCEPTED_OR_UNCERTAIN_INTENT_IMMUTABLE')
    if current is not None:
        try:
            if (current['candidate_id']!=row['id'] or current['symbol']!=row['symbol']
                    or json.loads(current['payload'])!=build_intent(plan,row['id'])):
                raise Review('ORDER_EVIDENCE_UNVERIFIED')
        except (KeyError,TypeError,ValueError):
            raise Review('ORDER_EVIDENCE_UNVERIFIED') from None
    if row['status'] not in allowed or not 0<=clock()-plan['rules_checked_at']<=300:
        raise Review('STALE_ORDER_INTENT')
    target=plan['risk_target_usdt']
    # A pre-existing V4 candidate retains its frozen sizing arithmetic. Only
    # newly analyzed candidates opt into schema 2 account-margin sizing.
    frozen_rules=deserialize_sizing_rules(plan)
    margin_sizing=frozen_rules.leverage_brackets.get('schema_version')==2
    context,rules=analysis_context(coordinator.market,row['symbol'],target,
        coordinator.account_factory(),policy=DYNAMIC_RISK_MODEL,db=db,margin_sizing=margin_sizing)
    coordinator.market.fresh(context,row['symbol'])
    signal=source_signal(db,row['id'],row['symbol'],normalized=True,risk_model=DYNAMIC_RISK_MODEL)
    fresh=risk_check(signal,rules,target,risk_model=DYNAMIC_RISK_MODEL,
        reward_risk_policy=DYNAMIC_REWARD_RISK_POLICY)
    fresh['risk_target_usdt']=target
    fresh['protection_working_type']=plan.get('protection_working_type','CONTRACT_PRICE')
    fresh['sizing_rules']=serialize_rules(rules)
    preflight(fresh,rules,target);stamp(db,fresh,row['id']);verify(db,fresh,row['id'])
    payload=build_intent(fresh,row['id'])
    db.execute('BEGIN IMMEDIATE')
    try:
        candidate=db.execute('SELECT * FROM robot_candidates WHERE id=?',(row['id'],)).fetchone()
        intent=db.execute('SELECT * FROM order_intents WHERE id=?',(row['id'],)).fetchone()
        if (candidate is None or candidate['plan']!=row['plan'] or candidate['status'] not in allowed
                or candidate['symbol']!=row['symbol'] or candidate['cycle']!=row['cycle']):
            raise Review('ORDER_EVIDENCE_UNVERIFIED')
        if (current is None)!=(intent is None) or current is not None and (
                intent['payload']!=current['payload'] or intent['state'] not in allowed
                or intent['candidate_id']!=row['id'] or intent['symbol']!=row['symbol']
                or intent['result'] is not None):
            raise Review('ACCEPTED_OR_UNCERTAIN_INTENT_IMMUTABLE')
        db.execute('UPDATE robot_candidates SET plan=? WHERE id=?',
            (json.dumps(fresh),row['id']))
        if intent is not None:
            db.execute('UPDATE order_intents SET payload=? WHERE id=?',
                (json.dumps(payload),row['id']))
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK');raise
    return fresh,rules
