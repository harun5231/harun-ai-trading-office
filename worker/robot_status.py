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
