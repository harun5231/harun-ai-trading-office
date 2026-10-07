"""Record the already verified ETH protection using GET-only reconciliation."""
import fcntl, hashlib, json, os, re, sqlite3, stat, tempfile, time
from decimal import Decimal
from pathlib import Path

def require(value, reason):
    if not value:
        raise ValueError(reason)

def main():
    db = lock = None
    stage = 'SOURCE_CHECK'
    try:
        for name, sha in (('order_gateway.py','7714469daa3c529ee228d8e049cea3b3a52484a46a8429b2b31e9e233573c422'),
                          ('robot.py','4bb72c65d8eb3bada7c3739058e270cf648d129de64d2042ecee90a4627bbbef')):
            require(hashlib.sha256((Path('/app/worker')/name).read_bytes()).hexdigest()==sha, 'SOURCE_CHANGED')
        from worker.robot import Coordinator
        from worker.robot_store import RobotStore
        from worker.order_gateway import OrderGateway
        from worker.binance_private import BinanceReadOnly
        from worker.account_state import account_state
        from worker.core import day
        stage = 'LOCK'
        trading = Path('/data/trading')
        lock = os.open(trading/'cycle.lock',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.geteuid()==10001,'LOCK_INVALID')
        deadline = time.monotonic()+15
        while True:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic()<deadline,'WORKER_BUSY')
                time.sleep(.2)
        info = (trading/'ledger.sqlite3').lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.geteuid()==10001 and not info.st_mode&0o077,'JOURNAL_NOT_PRIVATE')
        stage = 'JOURNAL_CHECK'
        db = sqlite3.connect('file:/data/trading/ledger.sqlite3?mode=rw',uri=True,isolation_level=None,timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA trusted_schema=OFF')
        require(not db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone(),'JOURNAL_TRIGGER_PRESENT')
        rows = db.execute("SELECT * FROM order_intents WHERE symbol='ETHUSDT'").fetchall()
        require(len(rows)==1 and rows[0]['state'] in ('NEEDS_REVIEW','POSITION_PROTECTED'),'ORDER_STATE_CHANGED')
        row = rows[0]
        require(row['failure_code']=='BINANCE_ORDER_PROTECTION_OUTCOME_UNKNOWN' if row['state']=='NEEDS_REVIEW' else row['failure_code'] is None,'ORDER_FAILURE_CHANGED')
        candidate = db.execute('SELECT status,failure_code FROM robot_candidates WHERE id=?',(row['candidate_id'],)).fetchone()
        require(candidate and candidate['status']==row['state'] and candidate['failure_code']==row['failure_code'],'CANDIDATE_CHANGED')
        c = Coordinator.__new__(Coordinator)
        c.db,c.store = db,RobotStore(db,initialize=False)
        intent = c.verified_intent(row)
        require(intent['client_order_id']=='hao-ce5286d37dcdca54a2f1174a2c48','INTENT_CHANGED')
        stage = 'BACKUP'
        folder = Path(tempfile.mkdtemp(prefix='eth-record-',dir=trading/'maintenance'))
        folder.chmod(0o700)
        backup = folder/'ledger.before.sqlite3'
        fd = os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.close(fd)
        with sqlite3.connect(backup) as saved:
            db.backup(saved)
            require(saved.execute('PRAGMA quick_check').fetchone()[0]=='ok','BACKUP_INVALID')
        print('BACKUP',str(backup))
        stage = 'GET_PROOF'
        g = OrderGateway()
        original = g._wire
        def wire(method,path,query='',signed=False):
            require(method=='GET','WRITE_BLOCKED')
            return original(method,path,query,signed)
        g._wire = wire
        account = account_state(BinanceReadOnly(),c.store,day())
        result = g.reconcile(intent)
        require(result['state']=='POSITION_PROTECTED' and result['order_id']=='8389766291635748850' and Decimal(result['filled_quantity'])==Decimal('0.181') and result['sl_order_id']=='4000001952621457' and result['tp_order_id']=='4000001952686853' and result['sl_confirmed'] is True and result['tp_confirmed'] is True,'PROOF_CHANGED')
        stage = 'RECORD'
        require(dict(db.execute('SELECT * FROM order_intents WHERE id=?',(row['id'],)).fetchone())==dict(row),'JOURNAL_CHANGED')
        candidate = db.execute('SELECT status,failure_code FROM robot_candidates WHERE id=?',(row['candidate_id'],)).fetchone()
        require(candidate and candidate['status']==row['state'] and candidate['failure_code']==row['failure_code'],'CANDIDATE_CHANGED')
        try:
            c.record_gateway_observation(row['id'],row['candidate_id'],result,account)
        except Exception:
            pass  # Reporting can fail after COMMIT; check the actual rows below.
        current = db.execute('SELECT * FROM order_intents WHERE id=?',(row['id'],)).fetchone()
        receipt = db.execute('SELECT * FROM robot_entry_receipts WHERE id=?',(intent['client_order_id'],)).fetchone()
        candidate = db.execute('SELECT status,failure_code FROM robot_candidates WHERE id=?',(row['candidate_id'],)).fetchone()
        require(current['state']=='POSITION_PROTECTED' and current['failure_code'] is None and current['payload']==row['payload'] and json.loads(current['result'])==result and candidate['status']=='POSITION_PROTECTED' and candidate['failure_code'] is None and receipt and receipt['confirmed_at']==result['first_fill_at'],'RECORD_NOT_VERIFIED')
        print(json.dumps(dict(status='ETH_PROTECTION_JOURNAL_VERIFIED',sl_confirmed=True,tp_confirmed=True,entry_receipt=True,bot_entries_today=c.store.entries(day())),sort_keys=True))
        return 0
    except Exception as error:
        reason = str(error)
        print(json.dumps(dict(status='RECORD_NOT_COMPLETED',stage=stage,reason=reason if re.fullmatch(r'[A-Z_]{1,100}',reason) else type(error).__name__)))
        return 1
    finally:
        if db is not None:
            db.close()
        if lock is not None:
            os.close(lock)

if __name__ == '__main__':
    raise SystemExit(main())
