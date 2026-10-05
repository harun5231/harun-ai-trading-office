"""Persisted-setup shadow plans. Only authenticated GETs; no submission interface."""
import fcntl
import hashlib
import json
import re
import time
from decimal import Context,ROUND_HALF_EVEN
from fractions import Fraction
from pathlib import Path
from .core import D,Ledger,Review,day,number,preflight
from .market import Market
from .neuroapi import setup
from .state import directory
from .binance_private import BinanceReadOnly,BinanceCheckError,CODES as AUTH_CODES
from .execution_model import ExecutionModel
from .provenance import current,ANALYSIS_VERSION

CODES=frozenset(('NO_PERSISTED_SETUP','WORKER_BUSY','BINANCE_POSITION_MODE_MISMATCH','BINANCE_TRADE_DISABLED',
 'BINANCE_MULTI_ASSET_UNSUPPORTED','SHADOW_DATA_INVALID','SHADOW_DUPLICATE_SETUP','SHADOW_DAILY_LIMIT',
 'SHADOW_SETUP_STALE','SHADOW_SETUP_CHANGED','SHADOW_RULES_REJECTED','SHADOW_SYMBOL_INACTIVE',
 'SHADOW_ACCOUNT_AMBIGUOUS','SHADOW_EXISTING_EXPOSURE','SHADOW_EXISTING_ORDERS','SHADOW_LEVERAGE_UNSUPPORTED',
 'SHADOW_MARGIN_INSUFFICIENT','SHADOW_PROTECTION_INCOMPLETE','SHADOW_IDENTITY_CONFLICT','SHADOW_SOURCE_UNVERIFIED',
 'SHADOW_PREFLIGHT_FAILED','SHADOW_LEVELS_ALREADY_CROSSED','SHADOW_REQUIRED_MUTATIONS'))
class ShadowError(Exception):pass

def fail(code):raise ShadowError(code)
def safe(error):
    return str(error) if isinstance(error,(ShadowError,BinanceCheckError)) and str(error) in CODES|AUTH_CODES else 'SHADOW_PREFLIGHT_FAILED'
def identity(plan,business_day):
    values=[business_day,plan['symbol'],plan['side']]+[format(number(plan[k]).normalize(),'f') for k in ('entry','tp','sl','execution_quantity')]
    return hashlib.sha256(json.dumps(values,separators=(',',':')).encode()).hexdigest()
def ids(key):return {leg:'ho-'+key[:28]+'-'+suffix for leg,suffix in (('ENTRY','e'),('TP','t'),('SL','s'))}

def verified_rr(plan):
    """Derived values are not order inputs. Verify exact levels, never resize/reprice.

    analysis-v6 persisted Decimal division at precision 160 / HALF_EVEN. Accept
    either the exact rational or that precisely rounded representation, not an
    arbitrary tolerance or rounding at the number of digits supplied by a caller.
    Fixed context keeps verification independent of ambient Decimal settings.
    """
    try:
        entry,tp,sl=(Fraction(number(plan[k])) for k in ('entry','tp','sl'))
        distance=abs(entry-sl)
        if not distance:raise ValueError
        ratio=abs(tp-entry)/distance
        if ratio<2:raise ValueError
        value=plan['rr']
        if isinstance(value,bool) or not isinstance(value,(str,int,D)):raise ValueError
        text=str(value)
        # Separate bounded parser for worker-derived ratios; core.number unchanged.
        if len(text)>256 or not re.fullmatch(r'\d+(?:\.\d+)?(?:E[+-]?\d{1,3})?',text):raise ValueError
        saved=D(text)
        if not saved.is_finite() or saved<=0 or abs(saved.as_tuple().exponent)>256:raise ValueError
        rounded=Context(prec=160,rounding=ROUND_HALF_EVEN).divide(D(ratio.numerator),D(ratio.denominator))
        if Fraction(saved)!=ratio and saved!=rounded:raise ValueError
        return ratio
    except Exception:fail('SHADOW_SETUP_CHANGED')


def plan_payload(plan,business_day):
    symbol=plan['symbol'];side=plan['side']
    if side=='HOLD':return None
    if not isinstance(symbol,str) or not re.fullmatch(r'[A-Z0-9]{2,18}USDT',symbol) or side not in ('LONG','SHORT'):fail('SHADOW_DATA_INVALID')
    if plan.get('mode')!='DRY_RUN' or plan.get('margin_mode')!='CROSS' or plan.get('leverage')!=75 or plan.get('order_type')!='LIMIT':fail('SHADOW_DATA_INVALID')
    for field in ('entry','tp','sl','quantity','execution_quantity','risk'):number(plan[field])
    verified_rr(plan)
    if D(plan['quantity'])!=D(plan['execution_quantity']):fail('SHADOW_SETUP_CHANGED')
    key=identity(plan,business_day);client_ids=ids(key)
    entry_side='BUY' if side=='LONG' else 'SELL';exit_side='SELL' if side=='LONG' else 'BUY'
    entry=dict(symbol=symbol,side=entry_side,positionSide='BOTH',type='LIMIT',timeInForce='GTC',
        quantity=plan['execution_quantity'],price=plan['entry'],newClientOrderId=client_ids['ENTRY'])
    def leg(kind,price,label):
        return dict(api_family='USD-M_ALGO',intended_path='/fapi/v1/algoOrder',activation='AFTER_CONFIRMED_ENTRY_FILL',
            payload=dict(algoType='CONDITIONAL',symbol=symbol,side=exit_side,positionSide='BOTH',type=kind,
                triggerPrice=price,workingType='MARK_PRICE',closePosition='true',clientAlgoId=client_ids[label]))
    return dict(status='SHADOW_PLAN_READY',setup_id=key,business_day=business_day,symbol=symbol,side=side,
        position_mode='ONE_WAY',positionSide='BOTH',margin_target='CROSS',leverage_target=75,
        execution_quantity=plan['execution_quantity'],entry=plan['entry'],TP=plan['tp'],SL=plan['sl'],
        calculated_risk=plan['risk'],actual_RR=plan['rr'],protective_side=exit_side,client_ids=client_ids,
        entry_order=dict(api_family='USD-M_FUTURES',intended_path='/fapi/v1/order',payload=entry),
        take_profit_order=leg('TAKE_PROFIT_MARKET',plan['tp'],'TP'),stop_loss_order=leg('STOP_MARKET',plan['sl'],'SL'),
        required_account_mutations=[],would_submit=False,live_execution=False,mode='DRY_RUN',
        failure_policy=dict(uncertain_entry='RECONCILE_SAME_CLIENT_ID_NO_BLIND_RETRY',
            partial_fill='PROTECTION_INCOMPLETE_RECONCILE_AND_PROTECT_FILLED_EXPOSURE',
            protection='REQUIRE_BOTH_ACKNOWLEDGED_LEGS',incomplete='BLOCK_NEXT_SETUP',
            after_exit='RECONCILE_FLAT_AND_CLEAR_SIBLING_BEFORE_NEW_SETUP'),
        future_reconciliation=dict(entry=dict(intended_get_path='/fapi/v1/order',lookup={'symbol':symbol,'origClientOrderId':client_ids['ENTRY']}),
            take_profit=dict(intended_get_path='/fapi/v1/algoOrder',lookup={'clientAlgoId':client_ids['TP']}),
            stop_loss=dict(intended_get_path='/fapi/v1/algoOrder',lookup={'clientAlgoId':client_ids['SL']}),
            implemented=False),
        future_states=['PLAN_READY','ENTRY_SUBMITTED','ENTRY_CONFIRMED','PROTECTION_SUBMITTED','POSITION_PROTECTED'])

class ShadowStore:
    def __init__(self,db):
        self.db=db
        db.execute('CREATE TABLE IF NOT EXISTS shadow_plans(setup_id TEXT PRIMARY KEY,day TEXT NOT NULL,symbol TEXT NOT NULL,plan TEXT NOT NULL,state TEXT NOT NULL,model TEXT NOT NULL,UNIQUE(day,symbol))')
    def blocked(self):
        for row in self.db.execute('SELECT model FROM shadow_plans'):
            try:
                model=ExecutionModel(**json.loads(row[0]))
                if model.blocks_next or model.model_only is not True:return True
            except Exception:return True
        return False
    def save(self,value):
        # Store only the reconstructed sanitized plan; never a provider response.
        key=value['setup_id'];old=self.db.execute('SELECT setup_id FROM shadow_plans WHERE day=? AND symbol=?',(value['business_day'],value['symbol'])).fetchone()
        if old and old[0]!=key:fail('SHADOW_IDENTITY_CONFLICT')
        proposed=set(value['client_ids'].values())
        if len(proposed)!=3:fail('SHADOW_IDENTITY_CONFLICT')
        for saved in self.db.execute('SELECT setup_id,plan FROM shadow_plans WHERE setup_id!=?',(key,)):
            if proposed.intersection(json.loads(saved['plan'])['client_ids'].values()):fail('SHADOW_IDENTITY_CONFLICT')
        self.db.execute('INSERT INTO shadow_plans VALUES(?,?,?,?,?,?) ON CONFLICT(setup_id) DO UPDATE SET plan=excluded.plan,state=excluded.state',
            (key,value['business_day'],value['symbol'],json.dumps(value),value['status'],json.dumps(ExecutionModel().data())))


def load_candidates(ledger,today):
    """No reservation/research. Require current-day persisted execution quantity/provenance."""
    db=ledger.db;names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")};items=[]
    trades=list(db.execute('SELECT * FROM trades WHERE day=?',(today,)))
    if len(trades)>2:fail('SHADOW_DAILY_LIMIT')
    for row in trades:
        if row['state']=='ORDER_READY' and row['source']=='NEUROAPI_DRY_RUN':
            p=json.loads(row['plan']);operation=today+':analysis:'+p['symbol']
            try:eligible=current(db,p,operation)
            except Review:fail('SHADOW_SOURCE_UNVERIFIED')
            if eligible:
                try:
                    if not plan_payload(p,today) or 'neurobro_position_size' not in p:raise ValueError
                except ShadowError:raise
                except Exception:fail('SHADOW_SOURCE_UNVERIFIED')
                items.append((p,operation,True))
    if 'analysis_checks' in names:
        for row in db.execute("SELECT operation,result FROM analysis_checks WHERE day=? AND state='COMPLETE'",(today,)):
            result=json.loads(row['result'])
            try:eligible=current(db,result,row['operation'])
            except Review:fail('SHADOW_SOURCE_UNVERIFIED')
            if not eligible:continue
            if row['operation']!=today+':'+ANALYSIS_VERSION+':'+str(result.get('symbol')):fail('SHADOW_SOURCE_UNVERIFIED')
            if result.get('status') not in ('ACCEPT','REJECT','HOLD'):fail('SHADOW_SOURCE_UNVERIFIED')
            if result.get('status')!='ACCEPT':continue
            if result.get('mode')!='DRY_RUN' or not result.get('execution_quantity'):fail('SHADOW_SOURCE_UNVERIFIED')
            try:
                p=dict(symbol=result['symbol'],side=result['side'],entry=result['entry'],tp=result['TP'],sl=result['SL'],
                    quantity=result['execution_quantity'],execution_quantity=result['execution_quantity'],risk=result['calculated_risk'],rr=result.get('actual_RR'),
                    neurobro_position_size=result['neurobro_position_size'],
                    margin_mode='CROSS',leverage=75,order_type='LIMIT',mode='DRY_RUN')
                plan_payload(p,today)
            except ShadowError:raise
            except Exception:fail('SHADOW_SOURCE_UNVERIFIED')
            items.append((p,row['operation'],False))
    if not items:fail('NO_PERSISTED_SETUP')
    if len(trades)+sum(not reserved for _,_,reserved in items)>2:fail('SHADOW_DAILY_LIMIT')
    if len({p['symbol'] for p,_,_ in items})!=len(items):fail('SHADOW_DUPLICATE_SETUP')
    for p,operation,_ in items:
        if 'api_requests' not in names:fail('SHADOW_SOURCE_UNVERIFIED')
        source=db.execute("SELECT output FROM api_requests WHERE operation=? AND state='COMPLETE'",(operation,)).fetchone()
        if not source or not source[0]:fail('SHADOW_SOURCE_UNVERIFIED')
        try:signal=setup(json.loads(source[0],parse_float=D),p['symbol'])
        except Exception:fail('SHADOW_SOURCE_UNVERIFIED')
        if signal.side!=p['side'] or signal.side=='HOLD':fail('SHADOW_SETUP_CHANGED')
        if any(number(p[k])!=v for k,v in (('entry',signal.entry),('tp',signal.tp),('sl',signal.sl))):fail('SHADOW_SETUP_CHANGED')
        plan_payload(p,today) # Strictly sanitized fields before any persistence/output.
    return [p for p,_,_ in items]


def symbol_read(client,path,symbol):
    client.sync_time()
    return client.signed_get(path,symbol)

def symbol_preflight(client,market,plan,account):
    symbol=plan['symbol']
    if symbol not in market.catalog():fail('SHADOW_SYMBOL_INACTIVE')
    rules=market.rules(symbol)
    try:
        preflight(plan,rules)
        verified_rr(plan)
    except Review:fail('SHADOW_RULES_REJECTED')
    mark=number(market.mark(symbol)['price'])
    if not min(number(plan['tp']),number(plan['sl']))<mark<max(number(plan['tp']),number(plan['sl'])):fail('SHADOW_LEVELS_ALREADY_CROSSED')
    for path in ('/fapi/v3/positionRisk','/fapi/v1/openOrders','/fapi/v1/openAlgoOrders'):
        rows=symbol_read(client,path,symbol)
        if not isinstance(rows,list) or any(not isinstance(r,dict) or r.get('symbol')!=symbol for r in rows):fail('SHADOW_ACCOUNT_AMBIGUOUS')
        if path=='/fapi/v3/positionRisk':
            for row in rows:
                value=row.get('positionAmt')
                if row.get('positionSide')!='BOTH' or not isinstance(value,str) or not re.fullmatch(r'-?\d+(?:\.\d+)?',value) or len(value)>64:fail('SHADOW_ACCOUNT_AMBIGUOUS')
                if D(value)!=0:fail('SHADOW_EXISTING_EXPOSURE')
        elif rows:fail('SHADOW_EXISTING_ORDERS')
    configs=symbol_read(client,'/fapi/v1/symbolConfig',symbol)
    if not isinstance(configs,list) or len(configs)!=1 or configs[0].get('symbol')!=symbol:fail('SHADOW_ACCOUNT_AMBIGUOUS')
    config=configs[0]
    if config.get('marginType') not in ('CROSSED','ISOLATED') or type(config.get('leverage')) is not int or config['leverage']<1:fail('SHADOW_ACCOUNT_AMBIGUOUS')
    bracket=symbol_read(client,'/fapi/v1/leverageBracket',symbol)
    if isinstance(bracket,list) and len(bracket)==1:bracket=bracket[0]
    if not isinstance(bracket,dict) or bracket.get('symbol')!=symbol or not isinstance(bracket.get('brackets'),list):fail('SHADOW_ACCOUNT_AMBIGUOUS')
    notional=number(plan['entry'])*number(plan['execution_quantity']);supported=False
    for b in bracket['brackets']:
        cap=D(str(b['notionalCap']));floor=D(str(b['notionalFloor']))
        if not cap.is_finite() or not floor.is_finite() or not 0<=floor<cap or type(b['initialLeverage']) is not int:fail('SHADOW_ACCOUNT_AMBIGUOUS')
        if floor<=notional<cap and b['initialLeverage']>=75:supported=True
    if not supported:fail('SHADOW_LEVERAGE_UNSUPPORTED')
    balance=account.get('usdt_available_balance')
    if balance is None or D(balance)<notional/D(75):fail('SHADOW_MARGIN_INSUFFICIENT')
    mutations=[]
    if config['marginType']!='CROSSED':mutations.append('SET_MARGIN_TYPE_CROSS')
    if config['leverage']!=75:mutations.append('SET_LEVERAGE_75')
    return mutations,notional/D(75)


def shadow(ledger,client,market):
    today=day();started=time.monotonic();store=ShadowStore(ledger.db)
    account=client.check()
    if account.get('status')!='BINANCE_CONNECTED':fail('SHADOW_ACCOUNT_AMBIGUOUS')
    if account.get('position_mode')!='ONE_WAY':fail('BINANCE_POSITION_MODE_MISMATCH')
    if account.get('can_trade') is not True:fail('BINANCE_TRADE_DISABLED')
    if account.get('multi_assets_margin') is not False:fail('BINANCE_MULTI_ASSET_UNSUPPORTED')
    if store.blocked():fail('SHADOW_PROTECTION_INCOMPLETE')
    plans=load_candidates(ledger,today);output=[];total_margin=D(0)
    # Unknown/manual exposure cannot be presumed protected. Inspect the whole account.
    client.sync_time();positions=client.signed_get('/fapi/v3/account').get('positions')
    if not isinstance(positions,list):fail('SHADOW_ACCOUNT_AMBIGUOUS')
    for row in positions:
        if not isinstance(row,dict) or row.get('positionSide')!='BOTH':fail('SHADOW_ACCOUNT_AMBIGUOUS')
        amount=row.get('positionAmt')
        if not isinstance(amount,str) or len(amount)>64 or not re.fullmatch(r'-?\d+(?:\.\d+)?',amount):fail('SHADOW_ACCOUNT_AMBIGUOUS')
        if D(amount)!=0:fail('SHADOW_EXISTING_EXPOSURE')
    for path in ('/fapi/v1/openOrders','/fapi/v1/openAlgoOrders'):
        client.sync_time();orders=client.signed_get(path)
        if not isinstance(orders,list):fail('SHADOW_ACCOUNT_AMBIGUOUS')
        if orders:fail('SHADOW_EXISTING_ORDERS')
    for plan in plans:
        value=plan_payload(plan,today)
        try:
            mutations,margin=symbol_preflight(client,market,plan,account)
            total_margin+=margin
            if total_margin>D(account['usdt_available_balance']):fail('SHADOW_MARGIN_INSUFFICIENT')
            value['required_account_mutations']=mutations
            value['status']='SHADOW_PLAN_READY' if mutations else 'SHADOW_PREFLIGHT_OK'
            if mutations:value['failure_code']='SHADOW_REQUIRED_MUTATIONS'
        except Exception as error:
            value.update(status='NEEDS_REVIEW',failure_code=safe(error))
        output.append(value)
    if today!=day() or time.monotonic()-started>60:fail('SHADOW_SETUP_STALE')
    # Persist identities atomically, and revalidate persisted candidates under a database write lock.
    ledger.db.execute('BEGIN IMMEDIATE')
    try:
        if store.blocked():fail('SHADOW_PROTECTION_INCOMPLETE')
        if day()!=today or load_candidates(ledger,today)!=plans:fail('SHADOW_SETUP_CHANGED')
        for value in output:store.save(value)
        ledger.db.execute('COMMIT')
    except BaseException:ledger.db.execute('ROLLBACK');raise
    return dict(status='SHADOW_PREFLIGHT_OK' if all(v['status']=='SHADOW_PREFLIGHT_OK' for v in output) else 'NEEDS_REVIEW',
        plans=output,would_submit=False,live_execution=False,mode='DRY_RUN')


def run(root):
    try:
        d=directory(root)
        with (d/'cycle.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:fail('WORKER_BUSY')
            ledger=Ledger(d/'ledger.sqlite3')
            try:return shadow(ledger,BinanceReadOnly(),Market())
            finally:ledger.db.close()
    except Exception as error:
        return dict(status='NEEDS_REVIEW',failure_code=safe(error),plans=[],would_submit=False,live_execution=False,mode='DRY_RUN')
