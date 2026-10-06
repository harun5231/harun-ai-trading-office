"""Read-only process, cached account and provider-journal diagnostics."""
import argparse
import json
import os


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',nargs='?',choices=('health','status','diagnostics'),
        help='Inspect existing worker state; never start research or submit orders.')
    parser.add_argument('--data-dir',default=os.getenv('OFFICE_DATA_DIR','/data'),
        help='Private worker state directory (not created by this command).')
    args=parser.parse_args(argv)
    if args.command is None:
        parser.print_help();return 0
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
