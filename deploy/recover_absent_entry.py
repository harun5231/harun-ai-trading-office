"""One OFF/locked maintenance recovery. Only authenticated GETs; never order replay."""
import argparse, fcntl, hashlib, json, os, re, sqlite3, stat, tempfile, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

class Refused(Exception): pass
CLIENT = re.compile(r'hao-[a-f0-9]{28}')
GET_PATHS = {'/fapi/v1/time', '/fapi/v1/order', '/fapi/v1/allOrders',
             '/fapi/v1/userTrades', '/fapi/v1/openOrders', '/fapi/v1/openAlgoOrders', '/fapi/v3/positionRisk'}
REASON = 'NO_ACCEPTED_ORDER_OBSERVED'
STAGE = 'START'

def require(ok):
    if not ok: raise Refused('RECOVERY_REFUSED')

def snapshot(db, client):
    settings = db.execute('SELECT enabled,risk FROM robot_settings WHERE id=1').fetchall()
    require(len(settings)==1 and settings[0]['enabled']==0)
    rows = db.execute("SELECT * FROM order_intents WHERE json_extract(payload,'$.client_order_id')=?",(client,)).fetchall()
    require(len(rows)==1)
    row = dict(rows[0]); require(row['state']=='NEEDS_REVIEW' and row['result'] is None and len(row['payload'])<=131072)
    intent = json.loads(row['payload'])
    candidates = db.execute('SELECT * FROM robot_candidates WHERE id=?',(row['candidate_id'],)).fetchall()
    require(len(candidates)==1)
    candidate = dict(candidates[0])
    require(row['id']==row['candidate_id']==intent['intent_id'] and candidate['symbol']==row['symbol']==intent['symbol']=='BTCUSDT')
    require(candidate['status']=='NEEDS_REVIEW' and intent['client_order_id']==client)
    require(client=='hao-'+hashlib.sha256(row['id'].encode()).hexdigest()[:28])
    require(not db.execute('SELECT 1 FROM robot_entry_receipts WHERE id=?',(client,)).fetchone())
    require(not db.execute("SELECT 1 FROM order_intents WHERE state IN ('SUBMITTING','ENTRY_PENDING','POSITION_PROTECTED') LIMIT 1").fetchone())
    token = json.dumps({'order':row,'candidate':candidate,'settings':[dict(r) for r in settings]},sort_keys=True,separators=(',',':'))
    return row, intent, hashlib.sha256(token.encode()).hexdigest()

def prove(sdk, row, intent, wall=time.time, previous=None):
    global STAGE
    STAGE='CLOCK_AND_AGE'
    now = sdk._get_server_time(); require(type(now) is int and 0<now<253402300800000 and abs(now/1000-wall())<=30)
    created = datetime.fromisoformat(row['created']); require(created.tzinfo is not None)
    age = now/1000-created.timestamp(); require(600<=age<48*3600)
    wib = timezone(timedelta(hours=7))
    require(created.astimezone(wib).date()==datetime.fromtimestamp(now/1000,timezone.utc).astimezone(wib).date())
    require(previous is None or now>=previous)
    start = now-72*3600*1000
    def get(path, params):
        require(path in GET_PATHS and path!='/fapi/v1/time')
        values = dict(params,timestamp=sdk._get_server_time(),recvWindow=5000)
        query = urlencode(sorted(values.items()))
        return sdk._wire('GET',path,query+'&signature='+sdk._sign(query),True)
    def absent():
        try: get('/fapi/v1/order',{'symbol':'BTCUSDT','origClientOrderId':intent['client_order_id']})
        except sdk._ExchangeError as error: require(error.code==-2013)
        else: raise Refused('RECOVERY_REFUSED')
    def rows(path, params):
        data=get(path,params); require(isinstance(data,list) and len(data)<1000 and all(isinstance(x,dict) for x in data)); return data
    STAGE='EXACT_CLIENT_ABSENCE'; absent()
    STAGE='COMPLETE_HISTORY'
    orders=rows('/fapi/v1/allOrders',{'symbol':'BTCUSDT','startTime':start,'endTime':now,'limit':1000})
    ids=set()
    for order in orders:
        oid=order.get('orderId'); require(type(oid) is int and oid>0 and oid not in ids); ids.add(oid)
        require(order.get('symbol')=='BTCUSDT' and type(order.get('time')) is int and start<=order['time']<=now)
        require(isinstance(order.get('clientOrderId'),str) and order['clientOrderId']!=intent['client_order_id'])
    STAGE='ZERO_FILLS'
    trades=rows('/fapi/v1/userTrades',{'symbol':'BTCUSDT','startTime':start,'endTime':now,'limit':1000}); require(not trades)
    STAGE='EMPTY_SYMBOL_ACCOUNT'
    require(not rows('/fapi/v1/openOrders',{'symbol':'BTCUSDT'}) and not rows('/fapi/v1/openAlgoOrders',{'symbol':'BTCUSDT'}))
    positions=rows('/fapi/v3/positionRisk',{'symbol':'BTCUSDT'})
    for position in positions:
        amount=position.get('positionAmt'); require(isinstance(amount,str) and re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?',amount))
        from decimal import Decimal
        require(Decimal(amount)==0 and position.get('positionSide')=='BOTH' and position.get('symbol')=='BTCUSDT')
    STAGE='FINAL_EXACT_CLIENT_ABSENCE'; absent()  # Final exact-client query also closes the history snapshot gap.
    return dict(server_ms=now,start_ms=start,btc_order_rows=len(orders),btc_trade_rows=0,btc_open_orders=0,btc_open_algos=0,btc_active_positions=0)

def recover(path, client, sdk, backup_parent, wall=time.time, after_backup=None):
    global STAGE
    STAGE='CYCLE_LOCK'
    require(bool(CLIENT.fullmatch(client)))
    fd=os.open(str(Path(path).parent/'cycle.lock'),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    db=None
    try:
        info=os.fstat(fd); require(stat.S_ISREG(info.st_mode) and info.st_nlink==1)
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db=sqlite3.connect(path,timeout=15,isolation_level=None); db.row_factory=sqlite3.Row
        STAGE='OFF_AND_ORIGINAL_LEDGER'; row,intent,token=snapshot(db,client)
        STAGE='INTENT_VALIDATION'; sdk._validate_intent(intent)
        original=sdk._wire
        def fenced(method,path,*args,**kwargs):
            require(method=='GET' and path in GET_PATHS)
            return original(method,path,*args,**kwargs)
        sdk._wire=fenced
        try:
            with sdk._operation(): first=prove(sdk,row,intent,wall)
            STAGE='PRIVATE_BACKUP'
            parent=Path(backup_parent); parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            require(stat.S_IMODE(parent.stat().st_mode)==0o700 and not parent.is_symlink())
            directory=Path(tempfile.mkdtemp(prefix='absent-entry-',dir=parent))
            target=directory/'ledger.before.sqlite3'
            with sqlite3.connect(target) as backup:
                os.chmod(target,0o600); db.backup(backup)
                require(backup.execute('PRAGMA quick_check').fetchone()[0]=='ok')
            if after_backup is not None: after_backup()
            db.execute('BEGIN IMMEDIATE')
            try:
                STAGE='UNCHANGED_LEDGER'; row2,intent2,token2=snapshot(db,client); require(token2==token)
                with sdk._operation(): second=prove(sdk,row2,intent2,wall,first['server_ms'])
                require(snapshot(db,client)[2]==token)
                STAGE='TERMINAL_TRANSACTION'
                at=datetime.now(timezone.utc).isoformat()
                require(db.execute("UPDATE order_intents SET state='REJECTED',failure_code=?,updated=? WHERE id=? AND state='NEEDS_REVIEW' AND result IS NULL",(REASON,at,row['id'])).rowcount==1)
                require(db.execute("UPDATE robot_candidates SET status='REJECTED',failure_code=? WHERE id=? AND status='NEEDS_REVIEW'",(REASON,row['candidate_id'])).rowcount==1)
                report=dict(status='ABSENCE_VERIFIED',client_order_id=client,intent_sha256=hashlib.sha256(row['payload'].encode()).hexdigest(),snapshot_sha256=token,proofs=[first,second],backup=str(target))
                raw=json.dumps(report,sort_keys=True,separators=(',',':')).encode()
                report['proof_sha256']=hashlib.sha256(raw).hexdigest()
                audit=directory/'proof.json'
                with open(audit,'x') as output:
                    os.chmod(audit,0o600); output.write(json.dumps(report,sort_keys=True,indent=2)); output.flush(); os.fsync(output.fileno())
                db.execute('COMMIT')
                return dict(report,status='ABSENT_ENTRY_REJECTED',proof_file=str(audit))
            except BaseException:
                db.execute('ROLLBACK'); raise
        finally: sdk._wire=original
    finally:
        if db is not None: db.close()
        os.close(fd)

def main():
    global STAGE
    parser=argparse.ArgumentParser(); parser.add_argument('--sdk-sha',required=True); parser.add_argument('--client-id',required=True)
    args=parser.parse_args()
    try:
        STAGE='PINNED_SDK_SOURCE'; require(bool(re.fullmatch(r'[a-f0-9]{64}',args.sdk_sha)))
        source=Path('/app/worker/order_gateway.py'); raw=source.read_bytes(); require(hashlib.sha256(raw).hexdigest()==args.sdk_sha)
        STAGE='KNOWN_SDK_IMPORT'
        import sys
        sys.path.insert(0,'/app')
        namespace={'__name__':'worker.order_gateway','__package__':'worker','__file__':str(source)}
        exec(compile(raw,str(source),'exec'),namespace)
        report=recover('/data/trading/ledger.sqlite3',args.client_id,namespace['OrderGateway'](),'/data/trading/maintenance')
        print(json.dumps(report,sort_keys=True))
    except Exception:
        print(json.dumps(dict(status='RECOVERY_REFUSED',stage=STAGE),sort_keys=True))
        raise SystemExit(1) from None

if __name__=='__main__': main()
