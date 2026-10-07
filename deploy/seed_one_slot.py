"""One local ON maintenance seed; no SDK import, network, order or existing-row update."""
import argparse, fcntl, hashlib, json, os, re, sqlite3, stat, tempfile, time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

PINS = dict(client='hao-6dd9743f6a90afa1c72f0931932a', proof='3121f0e78cc28215efc852ac0113765f80b8b92e55b8bce0f36b575d3ddad148', payload='b9478a9b5b08ed17704b9b4d286a5c4e7e7523d4d21b95897ed2250f07473879', snapshot='8479bc43a41d19e1f746e71b3ebb3dca03e66a8f5ec744aa6b279c615aac2d98')
RELATIVE = 'maintenance/absent-entry-guuwx8fy'
REASON = 'NO_ACCEPTED_ORDER_OBSERVED'
STAGE = 'SOURCE_MATCH'
class Refused(Exception):pass
def require(value):
    if not value:raise Refused('CYCLE_REPAIR_REFUSED')
def digest(value):return hashlib.sha256(value).hexdigest()
def amount(value):
    require(isinstance(value,str) and len(value)<=64 and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?',value));return Decimal(value)
def packed(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def private(path):
    info=path.lstat();require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid in (0,os.geteuid()) and stat.S_IMODE(info.st_mode)==0o600)
    require(info.st_size<=32*1024*1024)
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
def connect(path,immutable=False):
    db=sqlite3.connect('file:'+str(path)+'?mode=ro'+('&immutable=1' if immutable else ''),uri=True);db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');return db

def origin(trading,pins):
    global STAGE;STAGE='PROOF_BINDING'
    folder=trading/RELATIVE;proof_file=folder/'proof.json';backup=folder/'ledger.before.sqlite3'
    signatures={path:private(path) for path in (proof_file,backup)}
    require(not any(Path(str(backup)+suffix).exists() for suffix in ('-wal','-shm','-journal')))
    proof=json.loads(proof_file.read_text());require(set(proof)=={'status','client_order_id','intent_sha256','snapshot_sha256','proofs','backup','proof_sha256'})
    require(proof['status']=='ABSENCE_VERIFIED' and proof['client_order_id']==pins['client'] and proof['intent_sha256']==pins['payload'] and proof['snapshot_sha256']==pins['snapshot'] and proof['backup']==str(backup))
    require(proof['proof_sha256']==pins['proof']==digest(packed({k:v for k,v in proof.items() if k!='proof_sha256'})))
    rounds=proof['proofs'];require(isinstance(rounds,list) and len(rounds)==2)
    for value in rounds:
        require(set(value)=={'server_ms','start_ms','btc_order_rows','btc_trade_rows','btc_open_orders','btc_open_algos','btc_active_positions'})
        require(all(type(n) is int for n in value.values()) and value['server_ms']-value['start_ms']==72*3600*1000 and all(value[k]==0 for k in value if k not in ('server_ms','start_ms')))
    require(0<=rounds[1]['server_ms']-rounds[0]['server_ms']<=120000)
    with connect(backup,True) as old:
        require(old.execute('PRAGMA quick_check').fetchone()[0]=='ok')
        matches=old.execute("SELECT * FROM order_intents WHERE json_extract(payload,'$.client_order_id')=?",(pins['client'],)).fetchall();require(len(matches)==1);order=dict(matches[0])
        candidate=dict(old.execute('SELECT * FROM robot_candidates WHERE id=?',(order['candidate_id'],)).fetchone())
        settings=[dict(r) for r in old.execute('SELECT enabled,risk FROM robot_settings WHERE id=1')]
        require(settings and settings[0]['enabled']==0 and order['state']=='NEEDS_REVIEW' and order['result'] is None and candidate['status']=='NEEDS_REVIEW')
        require(digest(packed({'order':order,'candidate':candidate,'settings':settings}))==pins['snapshot'] and digest(order['payload'].encode())==pins['payload'])
    require(all(private(path)==value for path,value in signatures.items()))
    return order,candidate,signatures

def inspect(db,old,candidate,pins):
    global STAGE;STAGE='ON_AND_AMBIGUITY'
    require(db.execute('SELECT enabled FROM robot_settings WHERE id=1').fetchone()[0]==1)
    STAGE='BTC_BINDING'
    current=dict(db.execute('SELECT * FROM order_intents WHERE id=?',(old['id'],)).fetchone());saved=dict(db.execute('SELECT * FROM robot_candidates WHERE id=?',(candidate['id'],)).fetchone())
    require(current['state']==saved['status']=='REJECTED' and current['failure_code']==saved['failure_code']==REASON and current['result'] is None)
    require(dict(current,state=old['state'],failure_code=old['failure_code'],updated=old['updated'])==old and dict(saved,status=candidate['status'],failure_code=candidate['failure_code'])==candidate)
    intent=json.loads(current['payload']);require(current['id']==current['candidate_id']==intent['intent_id'] and current['symbol']==saved['symbol']=='BTCUSDT' and pins['client']=='hao-'+digest(current['id'].encode())[:28])
    created=datetime.fromisoformat(current['created']);require(created.tzinfo is not None and 0<=(datetime.now(timezone.utc)-created).total_seconds()<48*3600)
    old_cycle=db.execute('SELECT * FROM robot_cycles WHERE id=?',(candidate['cycle'],)).fetchone();require(old_cycle is not None)
    today=datetime.now(timezone(timedelta(hours=7))).date().isoformat();require(old_cycle['day']==today and old_cycle['id']==today+':robot-v9:0')
    STAGE='ON_AND_AMBIGUITY'
    require(not db.execute("SELECT 1 FROM order_intents WHERE state IN ('SUBMITTING','NEEDS_REVIEW') LIMIT 1").fetchone() and not db.execute("SELECT 1 FROM robot_candidates WHERE status='NEEDS_REVIEW' LIMIT 1").fetchone())
    for table in ('api_requests','robot_jobs'):require(not db.execute('SELECT 1 FROM '+table+" WHERE state IN ('PENDING','NEEDS_REVIEW') LIMIT 1").fetchone())
    require(not db.execute("SELECT 1 FROM robot_cycles WHERE day=? AND id LIKE ? AND state IN ('PENDING','NEEDS_REVIEW') LIMIT 1",(today,today+':robot-v9:%')).fetchone())
    require(not db.execute('SELECT 1 FROM robot_entry_receipts WHERE id=?',(pins['client'],)).fetchone())
    next_id=today+':robot-v9:1';existing=db.execute('SELECT * FROM robot_cycles WHERE id=?',(next_id,)).fetchone()
    entries=db.execute('SELECT COUNT(*) FROM robot_entry_receipts WHERE entry_day=?',(today,)).fetchone()[0]
    eth_rows=db.execute("SELECT * FROM order_intents WHERE symbol='ETHUSDT' AND candidate_id IN (SELECT id FROM robot_candidates WHERE cycle=?)",(old_cycle['id'],)).fetchall();require(len(eth_rows)==1)
    STAGE='ETH_OBSERVATION'
    eth=eth_rows[0];result=json.loads(eth['result']);eth_intent=json.loads(eth['payload']);require(result['source']=='BINANCE_FUTURES' and result['symbol']=='ETHUSDT' and result['client_order_id']==eth_intent['client_order_id'])
    require(eth['id']==eth['candidate_id']==eth_intent['intent_id'] and eth_intent['client_order_id']=='hao-'+digest(eth['id'].encode())[:28])
    if entries>=1 and eth['state'] in ('POSITION_PROTECTED','CLOSED'):
        receipt=db.execute('SELECT * FROM robot_entry_receipts WHERE id=?',(eth_intent['client_order_id'],)).fetchone()
        require(receipt and receipt['symbol']=='ETHUSDT' and receipt['entry_day']==today and receipt['confirmed_at']==result['first_fill_at'] and amount(result['filled_quantity'])>0)
        require(result['state']==eth['state'] and (eth['state']=='CLOSED' or (result.get('sl_confirmed') is True and result.get('tp_confirmed') is True)))
        return next_id,None,'NO_CHANGE_ENTRY_ALREADY_FILLED'
    if existing:
        data=json.loads(existing['data']);require(existing['entry_epoch']==1 and data.get('target')==1 and data.get('repair_origin')==pins['proof'] and data.get('repair_payload')==pins['payload'])
        return next_id,None,'ALREADY_SCHEDULED'
    STAGE='CYCLE_ELIGIBILITY'
    require(entries==0 and old_cycle['state']=='COMPLETE' and old_cycle['entry_epoch']==0);data=json.loads(old_cycle['data']);require(data['target']==2 and data['queue']==[] and data['screen']==0 and data['replacements']==0)
    require(not db.execute("SELECT 1 FROM order_intents WHERE state IN ('READY_FOR_EXECUTION','EXECUTION_BLOCKED') LIMIT 1").fetchone() and not db.execute("SELECT 1 FROM robot_candidates WHERE status IN ('READY_FOR_EXECUTION','EXECUTION_BLOCKED','NEEDS_REVIEW') LIMIT 1").fetchone() and not db.execute("SELECT 1 FROM robot_cycles WHERE day=? AND id LIKE ? AND state='ACTIVE' LIMIT 1",(today,today+':robot-v9:%')).fetchone())
    active=db.execute("SELECT * FROM order_intents WHERE state IN ('ENTRY_PENDING','POSITION_PROTECTED')").fetchall();require(len(active)==1 and active[0]['id']==eth['id'] and eth['state']==result['state']=='ENTRY_PENDING')
    require(amount(result['filled_quantity'])==0 and result.get('sl_confirmed',False) is False and result.get('tp_confirmed',False) is False)
    candidates=[dict(r) for r in db.execute('SELECT * FROM robot_candidates WHERE cycle=?',(old_cycle['id'],))]
    require(len(candidates)==2 and any(r['id']==eth['candidate_id'] and r['symbol']=='ETHUSDT' and r['status']=='ENTRY_PENDING' for r in candidates))
    data=dict(target=1,queue=[],seen=[],screen=-1,replacements=0,round_symbols=[],repair_origin=pins['proof'],repair_payload=pins['payload'])
    return next_id,data,'REPAIR_READY'

def seed(data_dir=Path('/data'),apply=False,pins=PINS,lock_timeout=15):
    global STAGE
    trading=Path(data_dir).resolve()/'trading';old,candidate,signatures=origin(trading,pins)
    lock=os.open(trading/'cycle.lock',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK);db=None
    try:
        STAGE='LOCK';info=os.fstat(lock);require(stat.S_ISREG(info.st_mode) and info.st_nlink==1);deadline=time.monotonic()+lock_timeout
        while True:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise Refused('WORKER_BUSY')
                time.sleep(.2)
        if not apply:
            with connect(trading/'ledger.sqlite3') as readonly:
                readonly.execute('BEGIN');cycle,data,status=inspect(readonly,old,candidate,pins)
            return {'status':status,'target':1,'epoch':1}
        db=sqlite3.connect('file:'+str(trading/'ledger.sqlite3')+'?mode=rw',uri=True,isolation_level=None,timeout=1);db.row_factory=sqlite3.Row
        cycle,data,status=inspect(db,old,candidate,pins)
        if data is None:return {'status':status,'target':1,'epoch':1}
        require(not db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone())
        STAGE='BACKUP'
        folder=Path(tempfile.mkdtemp(prefix='one-slot-cycle-',dir=trading/'maintenance'));folder.chmod(0o700);backup=folder/'ledger.before.sqlite3'
        with sqlite3.connect(backup) as saved:backup.chmod(0o600);db.backup(saved);require(saved.execute('PRAGMA quick_check').fetchone()[0]=='ok')
        STAGE='TRANSACTION';db.execute('BEGIN IMMEDIATE')
        try:
            cycle2,data2,status2=inspect(db,old,candidate,pins);require(cycle2==cycle and data2==data and all(private(path)==value for path,value in signatures.items()))
            STAGE='TRANSACTION';db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',(cycle,cycle.split(':',1)[0],1,'ACTIVE',json.dumps(data,separators=(',',':'))))
            require(all(private(path)==value for path,value in signatures.items()));db.execute('COMMIT')
        except BaseException:db.execute('ROLLBACK');raise
        return {'status':'ONE_REPAIR_CYCLE_SEEDED','target':1,'epoch':1,'backup':str(backup)}
    finally:
        if db is not None:db.close()
        os.close(lock)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',type=Path,default=Path('/data'));parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    try:
        require(digest(Path('/app/worker/robot.py').read_bytes())=='4bb72c65d8eb3bada7c3739058e270cf648d129de64d2042ecee90a4627bbbef' and digest(Path('/app/worker/order_gateway.py').read_bytes())=='7714469daa3c529ee228d8e049cea3b3a52484a46a8429b2b31e9e233573c422')
        report=seed(args.data_dir,args.apply)
    except Exception as error:report={'error':'WORKER_BUSY' if isinstance(error,Refused) and str(error)=='WORKER_BUSY' else 'CYCLE_REPAIR_REFUSED','stage':STAGE}
    print(json.dumps(report,sort_keys=True));raise SystemExit(1 if 'error' in report else 0)
