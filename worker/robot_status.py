"""Read-only localhost diagnostics for the sole robot pipeline and Binance cache."""
import json
import os
import re
import stat
from pathlib import Path
from urllib.request import Request,urlopen

def safe_value(value):
    if value is None or type(value) in (bool,int):return value
    if isinstance(value,str) and len(value)<=80 and re.fullmatch(r'[A-Za-z0-9_.:+/ -]+',value):return value
    return 'VALUE_REDACTED'

def selected(value,fields):
    return {key:safe_value(value.get(key)) for key in fields} if isinstance(value,dict) else {}

def collect(root='/data',token_path='/run/office/read_token',opener=urlopen):
    """Use existing GETs only: no state initialization, migration or research."""
    token=Path(token_path).read_text().strip()
    if len(token)<32:raise ValueError('WORKER_AUTH_UNAVAILABLE')
    responses={}
    for name,path in (('health','/health'),('office','/office/status')):
        request=Request('http://127.0.0.1:8787'+path,headers={'Authorization':'Bearer '+token})
        with opener(request,timeout=10) as response:value=json.load(response)
        if not isinstance(value,dict):raise ValueError('WORKER_STATUS_UNAVAILABLE')
        responses[name]=value
    office=responses['office']
    if office.get('schema_version')!=2 or office.get('source')!='BINANCE_FUTURES':raise ValueError('WORKER_STATUS_UNAVAILABLE')
    result={'uid':os.geteuid(),'gid':os.getegid(),'health':safe_value(responses['health'].get('status')),
        'source':'BINANCE_FUTURES','generated_at':safe_value(office.get('generated_at')),
        'robot':selected(office.get('robot'),('robot_on','bot_status','checked_at','account_checked_at',
            'wait_reason','failure_code','account_failure_code','risk_target_usdt','running_positions',
            'bot_entries_today','available_slots')),
        'account':selected(office.get('account'),('status','checked_at','failure_code','usdt_wallet_balance',
            'usdt_available_balance','active_positions','position_mode','can_trade','multi_assets_margin')),
        'reports':selected(office.get('reports'),('status','checked_at','pnl_today_usdt',
            'realized_pnl_today_usdt','commission_today_usdt','funding_today_usdt','trades_today','complete')),
        'position_history':selected(office.get('position_history'),('status','checked_at','kind','complete'))}
    result['execution_gateway']=selected(office.get('robot',{}).get('execution_gateway'),('status','connected','failure_code'))
    result['robot']['protection_observations']={}
    protections=office.get('robot',{}).get('protection_observations',{})
    if isinstance(protections,dict):
        for key,row in protections.items():
            if (isinstance(key,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}',key)
                    and isinstance(row,dict) and row.get('client_order_id')==key
                    and row.get('source')=='BINANCE_FUTURES'
                    and row.get('state') in ('POSITION_PROTECTED','PROTECTION_INCOMPLETE')):
                result['robot']['protection_observations'][key]=selected(row,('source','state','symbol',
                    'client_order_id','order_id','filled_quantity','sl_confirmed','tp_confirmed',
                    'sl_order_id','tp_order_id','observed_at','first_fill_at','fill_detected_at',
                    'sl_submitted_at','tp_submitted_at','sl_confirmed_at','tp_confirmed_at','accepted_at'))
    result['robot']['risk_estimates']={}
    estimates=office.get('robot',{}).get('risk_estimates',{})
    if isinstance(estimates,dict):
        for key,row in estimates.items():
            if (isinstance(key,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}',key)
                    and isinstance(row,dict) and row.get('client_order_id')==key
                    and row.get('kind')=='MONITORING_ESTIMATE_ONLY' and row.get('source')=='RISK_MANAGER'):
                projected=selected(row,('kind','source','status','symbol','client_order_id','intent_id',
                    'intent_state','observed_at','estimated_total_risk','original_risk_target_usdt',
                    'estimated_budget_difference_usdt','failure_code','order_parameters_changed','quantity_basis'))
                projected['components']=selected(row.get('components'),('gross_risk','entry_fee_usdt',
                    'sl_exit_fee_usdt','sl_book_impact_usdt','sl_liquidity_uncertainty_usdt'))
                result['robot']['risk_estimates'][key]=projected
    history=office.get('position_history',{}).get('items',[])
    result['position_history']['items_count']=len(history) if isinstance(history,list) else None
    exposure=office.get('robot',{}).get('manual_exposure',[])
    result['robot']['manual_exposure']=[value for value in exposure[:20]
        if isinstance(value,str) and re.fullmatch(r'[A-Z0-9_]{2,30}',value)] if isinstance(exposure,list) else []
    directory=Path(root)/'trading'
    result['state_writable']=os.access(directory,os.R_OK|os.W_OK|os.X_OK)
    result['locks']={}
    for name in ('migration.lock','cycle.lock'):
        try:value=(directory/name).lstat()
        except FileNotFoundError:
            result['locks'][name]={'status':'MISSING'};continue
        regular=stat.S_ISREG(value.st_mode) and value.st_nlink==1
        result['locks'][name]={'status':'REGULAR' if regular else 'INVALID','owner_uid':value.st_uid,
            'mode':format(stat.S_IMODE(value.st_mode),'04o'),
            'writable':regular and os.access(directory/name,os.R_OK|os.W_OK)}
    return result

def main():
    try:result=collect(os.getenv('OFFICE_DATA_DIR','/data'))
    except Exception:
        print(json.dumps({'status':'WORKER_STATUS_UNAVAILABLE'}));return 1
    print(json.dumps(result,ensure_ascii=True));return 0 if result['health']=='ONLINE' else 1
if __name__=='__main__':raise SystemExit(main())
