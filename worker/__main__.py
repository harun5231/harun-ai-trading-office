"""Explicit API-only paper-cycle and key-health commands."""
import argparse
import fcntl
import json
import os
from .core import Ledger,Review,day
from .market import Market
from .neuroapi import NeuroAPI,SCREEN_SCHEMA,selections
from .prompts import SCREENING
from .state import directory
from .workflow import Workflow

def main():
    os.umask(0o077)
    p=argparse.ArgumentParser();p.add_argument('command',choices=['binance-live-preflight','binance-arm','binance-execute','binance-scheduler','binance-shadow','binance-check','api-check','api-dry-run','diagnostics','screening-once','analysis-once']);p.add_argument('symbols',nargs='*');p.add_argument('--data-dir',default=os.getenv('OFFICE_DATA_DIR','/data'));a=p.parse_args()
    if (a.command=='analysis-once' and len(a.symbols)!=2) or (a.command!='analysis-once' and a.symbols):p.error('analysis-once requires exactly two symbols; other commands take none')
    if a.command in ('binance-live-preflight','binance-arm','binance-execute','binance-scheduler'):
        from .binance_live import run
        result=run(a.data_dir,a.command);print(json.dumps(result))
        return 0 if result['status'] in ('LIVE_PREFLIGHT_READY','LIVE_ARMED','LIVE_EXECUTION_REQUESTED','MONITORING','CLOSED','ENTRY_PENDING','POSITION_PROTECTED','SCHEDULER_OFF') else 1
    if a.command=='binance-shadow':
        from .binance_shadow import run
        result=run(a.data_dir);print(json.dumps(result))
        return 0 if result['status']=='SHADOW_PREFLIGHT_OK' else 1
    if a.command=='binance-check':
        from .binance_private import check
        result=check();print(json.dumps(result))
        return 0 if result['status']=='BINANCE_CONNECTED' else 1
    if a.command=='diagnostics':
        from .diagnostics import read_records
        try:print(json.dumps(read_records(a.data_dir)));return 0
        except Exception:print(json.dumps({'status':'DIAGNOSTICS_UNAVAILABLE'}));return 1
    d=directory(a.data_dir)
    with (d/'cycle.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            ledger=Ledger(d/'ledger.sqlite3');client=NeuroAPI(ledger)
            try:
                if a.command=='analysis-once':
                    from .analysis import analysis_once
                    result=analysis_once(ledger,client,Market(int(os.getenv('OFFICE_CANDLE_LOOKBACK','100')),int(os.getenv('OFFICE_MARKET_MAX_AGE','180'))),a.symbols)
                    print(json.dumps(result));return 0
                if a.command=='screening-once':
                    catalog=Market().catalog() # Public exchangeInfo before any paid request.
                    value=client.ask(day()+':manual-screening:v2',SCREENING,SCREEN_SCHEMA,lambda v:selections(v,catalog),catalog=catalog)
                    print(json.dumps(selections(value,catalog)));return 0
                if a.command=='api-check':result={'status':client.health()}
                else:result=Workflow(ledger,client,Market(int(os.getenv('OFFICE_CANDLE_LOOKBACK','100')),int(os.getenv('OFFICE_MARKET_MAX_AGE','180'))),d/'snapshot.json').run()
                print(json.dumps({k:result[k] for k in ('status','mode','locked') if k in result}))
            finally:ledger.db.close()
        except Exception as e:
            print(json.dumps({'status':'NEUROAPI_NOT_CONFIGURED' if isinstance(e,Review) and str(e)=='NEUROAPI_NOT_CONFIGURED' else 'NEEDS_REVIEW','mode':'DRY_RUN'}));return 1
    return 0
if __name__=='__main__':raise SystemExit(main())
