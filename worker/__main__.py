"""Explicit API-only paper-cycle and key-health commands."""
import argparse
import fcntl
import json
import os
from .core import Ledger,Review
from .market import Market
from .neuroapi import NeuroAPI
from .state import directory
from .workflow import Workflow

def main():
    os.umask(0o077)
    p=argparse.ArgumentParser();p.add_argument('command',choices=['api-check','api-dry-run','diagnostics']);p.add_argument('--data-dir',default=os.getenv('OFFICE_DATA_DIR','/data'));a=p.parse_args()
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
                if a.command=='api-check':result={'status':client.health()}
                else:result=Workflow(ledger,client,Market(int(os.getenv('OFFICE_CANDLE_LOOKBACK','100')),int(os.getenv('OFFICE_MARKET_MAX_AGE','180'))),d/'snapshot.json').run()
                print(json.dumps({k:result[k] for k in ('status','mode','locked') if k in result}))
            finally:ledger.db.close()
        except Exception as e:
            print(json.dumps({'status':'NEUROAPI_NOT_CONFIGURED' if isinstance(e,Review) and str(e)=='NEUROAPI_NOT_CONFIGURED' else 'NEEDS_REVIEW','mode':'DRY_RUN'}));return 1
    return 0
if __name__=='__main__':raise SystemExit(main())
