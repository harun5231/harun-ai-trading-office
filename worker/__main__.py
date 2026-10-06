"""Read-only process, account, provider health and journal diagnostics."""
import argparse
import json
import os


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',nargs='?',choices=('health','status','diagnostics','api-check','binance-check'),
        help='Inspect existing worker state; never start research or submit orders.')
    parser.add_argument('--data-dir',default=os.getenv('OFFICE_DATA_DIR','/data'),
        help='Private worker state directory (not created by this command).')
    args=parser.parse_args(argv)
    if args.command is None:
        parser.print_help();return 0
    if args.command=='api-check':
        from .neuroapi import health_check
        try:status=health_check()
        except Exception as error:
            status='NEUROAPI_NOT_CONFIGURED' if str(error)=='NEUROAPI_NOT_CONFIGURED' else 'NEUROAPI_UNAVAILABLE'
        if status not in ('NEUROAPI_CONNECTED','NEUROAPI_NOT_CONFIGURED','NEUROAPI_UNAVAILABLE'):status='NEUROAPI_UNAVAILABLE'
        print(json.dumps({'status':status}));return 0 if status=='NEUROAPI_CONNECTED' else 1
    if args.command=='binance-check':
        from .binance_private import check
        try:result=check()
        except Exception:result={'status':'BINANCE_ACCOUNT_UNAVAILABLE'}
        print(json.dumps(result,ensure_ascii=True));return 0 if result.get('status')=='BINANCE_CONNECTED' else 1
    if args.command=='health':
        from .health import probe_api
        try:online=probe_api()
        except Exception:online=False
        print(json.dumps({'status':'ONLINE' if online else 'OFFLINE'}))
        return 0 if online else 1
    if args.command=='status':
        from .robot_status import collect
        try:result=collect(root=args.data_dir)
        except Exception:
            print(json.dumps({'status':'WORKER_STATUS_UNAVAILABLE'}));return 1
        print(json.dumps(result,ensure_ascii=True))
        return 0 if result.get('health')=='ONLINE' else 1
    from .diagnostics import read_records
    try:result=read_records(args.data_dir)
    except Exception:
        print(json.dumps({'status':'DIAGNOSTICS_UNAVAILABLE'}));return 1
    print(json.dumps(result,ensure_ascii=True));return 0


if __name__=='__main__':raise SystemExit(main())
