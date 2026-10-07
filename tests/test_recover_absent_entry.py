"""Offline absence recovery: authenticated GET fixtures, private SQLite, no live SDK."""
import contextlib, fcntl, hashlib, importlib.util, json, os, sqlite3, tempfile, unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('recover_absent', ROOT / 'deploy/recover_absent_entry.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
NOW = int(datetime(2026,10,7,7,0,tzinfo=timezone.utc).timestamp()*1000)

class FakeSDK:
    class _ExchangeError(Exception):
        def __init__(self, code): self.code=code
    def __init__(self):
        self.now=NOW; self.calls=[]; self.hook=None; self.exact_code=-2013; self.clock_hook=None
        self.responses={'/fapi/v1/allOrders':[], '/fapi/v1/userTrades':[], '/fapi/v1/openOrders':[], '/fapi/v1/openAlgoOrders':[], '/fapi/v3/positionRisk':[dict(symbol='BTCUSDT',positionSide='BOTH',positionAmt='0.000')]}
    def _get_server_time(self):
        if self.clock_hook: self.clock_hook(self)
        return self.now
    def _sign(self, query): return 'offline-signature'
    def _validate_intent(self, intent):
        assert intent['symbol']=='BTCUSDT' and intent['margin_mode']=='CROSS' and intent['leverage']==75
    def _operation(self): return contextlib.nullcontext()
    def _wire(self, method, path, query='', signed=False):
        self.calls.append((method,path,parse_qs(query),signed))
        if self.hook: self.hook(self,method,path)
        if path=='/fapi/v1/order':
            if self.exact_code is None: return dict(orderId=1337)
            raise self._ExchangeError(self.exact_code)
        return self.responses[path]

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.dir=Path(self.temp.name)
        self.dbpath=self.dir/'ledger.sqlite3'; self.lock=self.dir/'cycle.lock'; self.lock.touch(mode=0o600)
        self.parent=self.dir/'maintenance'; self.sdk=FakeSDK()
        self.operation='verified-btc-operation'; self.client='hao-'+hashlib.sha256(self.operation.encode()).hexdigest()[:28]
        created=datetime.fromtimestamp((NOW-20*60*1000)/1000,timezone.utc).isoformat()
        self.intent=dict(intent_id=self.operation,client_order_id=self.client,symbol='BTCUSDT',margin_mode='CROSS',leverage=75)
        db=sqlite3.connect(self.dbpath)
        db.executescript('''
        CREATE TABLE robot_settings(id INTEGER PRIMARY KEY,enabled INTEGER NOT NULL,risk TEXT NOT NULL);
        CREATE TABLE robot_candidates(id TEXT PRIMARY KEY,cycle TEXT NOT NULL,symbol TEXT NOT NULL,status TEXT NOT NULL,plan TEXT,failure_code TEXT);
        CREATE TABLE order_intents(id TEXT PRIMARY KEY,candidate_id TEXT UNIQUE NOT NULL,symbol TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,result TEXT,failure_code TEXT,created TEXT NOT NULL,updated TEXT NOT NULL);
        CREATE TABLE robot_entry_receipts(id TEXT PRIMARY KEY,symbol TEXT NOT NULL,entry_day TEXT NOT NULL,confirmed_at TEXT NOT NULL);
        CREATE TABLE provider_log(id TEXT PRIMARY KEY,value TEXT);
        INSERT INTO robot_settings VALUES(1,0,'5'); INSERT INTO provider_log VALUES('original','preserved');
        ''')
        db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',(self.operation,'original-cycle','BTCUSDT','NEEDS_REVIEW','original-plan','ORDER_OUTCOME_UNKNOWN'))
        db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',(self.operation,self.operation,'BTCUSDT','NEEDS_REVIEW',json.dumps(self.intent),None,'ORDER_OUTCOME_UNKNOWN',created,created))
        db.commit(); db.close()
        self.original=self.rows()
    def tearDown(self): self.temp.cleanup()
    def sql(self, sql, params=()):
        with sqlite3.connect(self.dbpath) as db: return db.execute(sql,params).fetchall()
    def rows(self):
        with sqlite3.connect(self.dbpath) as db:
            return {name:db.execute('SELECT * FROM '+name).fetchall() for name in ('robot_settings','robot_candidates','order_intents','robot_entry_receipts','provider_log')}
    def recover(self, **kwargs): return m.recover(self.dbpath,self.client,self.sdk,self.parent,wall=lambda:NOW/1000,**kwargs)
    def refused(self, **kwargs):
        before=self.rows(); original_wire=self.sdk._wire
        with self.assertRaises(Exception): self.recover(**kwargs)
        self.assertEqual(before,self.rows()); self.assertEqual(original_wire,self.sdk._wire)
        self.assertTrue(all(c[0]=='GET' for c in self.sdk.calls))
    def test_success_only_original_rows_terminal_and_proof_does_not_claim_commit(self):
        result=self.recover(); rows=self.rows()
        self.assertEqual(result['status'],'ABSENT_ENTRY_REJECTED')
        self.assertEqual(rows['order_intents'][0][3],'REJECTED'); self.assertEqual(rows['order_intents'][0][6],m.REASON)
        self.assertEqual(rows['robot_candidates'][0][3],'REJECTED'); self.assertEqual(rows['robot_candidates'][0][5],m.REASON)
        self.assertIsNone(rows['order_intents'][0][5]); self.assertEqual(rows['order_intents'][0][4],self.original['order_intents'][0][4])
        for name in ('provider_log','robot_entry_receipts','robot_settings'): self.assertEqual(rows[name],self.original[name])
        with sqlite3.connect(result['backup']) as db: self.assertEqual(db.execute('SELECT state FROM order_intents').fetchone()[0],'NEEDS_REVIEW')
        proof=json.loads(Path(result['proof_file']).read_text()); self.assertEqual(proof['status'],'ABSENCE_VERIFIED')
        sha=proof.pop('proof_sha256'); self.assertEqual(sha,hashlib.sha256(json.dumps(proof,sort_keys=True,separators=(',',':')).encode()).hexdigest())
        for path in (Path(result['backup']),Path(result['proof_file'])): self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(Path(result['backup']).parent.stat().st_mode&0o777,0o700)
        self.assertEqual(sum(path=='/fapi/v1/order' for _,path,_,_ in self.sdk.calls),4)
        for method,path,params,signed in self.sdk.calls:
            self.assertEqual(method,'GET'); self.assertTrue(signed); self.assertEqual(params['recvWindow'],['5000'])
            if path in ('/fapi/v1/openOrders','/fapi/v1/openAlgoOrders','/fapi/v3/positionRisk'): self.assertEqual(params['symbol'],['BTCUSDT'])
            if path in ('/fapi/v1/allOrders','/fapi/v1/userTrades'):
                self.assertEqual(params['startTime'],[str(NOW-72*3600*1000)]); self.assertEqual(params['endTime'],[str(NOW)]); self.assertEqual(params['limit'],['1000'])
    def test_one_other_valid_historical_order_is_allowed(self):
        self.sdk.responses['/fapi/v1/allOrders']=[dict(orderId=15,symbol='BTCUSDT',time=NOW-60*1000,clientOrderId='manual-terminal')]
        self.assertEqual(self.recover()['proofs'][0]['btc_order_rows'],1)
    def test_off_required(self):
        self.sql('UPDATE robot_settings SET enabled=1'); self.refused(); self.assertEqual(self.sdk.calls,[])
    def test_cycle_lock_required(self):
        fd=os.open(self.lock,os.O_RDONLY)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); self.refused(); self.assertEqual(self.sdk.calls,[])
        finally: os.close(fd)
    def test_lock_symlink_refused(self):
        self.lock.unlink(); other=self.dir/'other'; other.touch(); self.lock.symlink_to(other); self.refused()
    def test_short_age_refused(self):
        created=datetime.fromtimestamp((NOW-599000)/1000,timezone.utc).isoformat(); self.sql('UPDATE order_intents SET created=?',(created,)); self.refused()
    def test_old_age_refused(self):
        created=datetime.fromtimestamp((NOW-48*3600*1000)/1000,timezone.utc).isoformat(); self.sql('UPDATE order_intents SET created=?',(created,)); self.refused()
    def test_carryover_wib_day_refused(self):
        created=datetime.fromtimestamp((NOW-15*3600*1000)/1000,timezone.utc).isoformat(); self.sql('UPDATE order_intents SET created=?',(created,)); self.refused()
    def test_wall_skew_refused(self): self.sdk.now=NOW+31000; self.refused()
    def test_server_backwards_second_snapshot_refused(self):
        self.refused(after_backup=lambda:setattr(self.sdk,'now',NOW-1000))
    def test_naive_created_refused(self): self.sql("UPDATE order_intents SET created='2026-10-07T06:40:00'"); self.refused()
    def test_invalid_server_type_refused(self): self.sdk.now=float(NOW); self.refused()
    def test_exact_order_present_refused(self): self.sdk.exact_code=None; self.refused()
    def test_exact_order_wrong_error_not_absence(self): self.sdk.exact_code=-2015; self.refused()
    def test_transport_error_refused(self):
        def hook(sdk,method,path): raise TimeoutError()
        self.sdk.hook=hook; self.refused()
    def test_full_history_page_refused(self): self.sdk.responses['/fapi/v1/allOrders']=[{}]*1000; self.refused()
    def test_duplicate_history_ids_refused(self):
        order=dict(orderId=15,symbol='BTCUSDT',time=NOW-60000,clientOrderId='other'); self.sdk.responses['/fapi/v1/allOrders']=[order,order]; self.refused()
    def test_history_wrong_bounds_refused(self):
        self.sdk.responses['/fapi/v1/allOrders']=[dict(orderId=15,symbol='BTCUSDT',time=NOW+1,clientOrderId='other')]; self.refused()
    def test_history_own_client_seen_refused(self):
        self.sdk.responses['/fapi/v1/allOrders']=[dict(orderId=15,symbol='BTCUSDT',time=NOW-60000,clientOrderId=self.client)]; self.refused()
    def test_history_malformed_refused(self): self.sdk.responses['/fapi/v1/allOrders']={}; self.refused()
    def test_nonzero_fills_refused(self): self.sdk.responses['/fapi/v1/userTrades']=[dict(orderId=15,qty='0.047')]; self.refused()
    def test_foreign_open_regular_refused(self): self.sdk.responses['/fapi/v1/openOrders']=[dict(symbol='HYPEUSDT')]; self.refused()
    def test_foreign_open_algo_refused(self): self.sdk.responses['/fapi/v1/openAlgoOrders']=[dict(symbol='HYPEUSDT')]; self.refused()
    def test_foreign_position_refused(self): self.sdk.responses['/fapi/v3/positionRisk']=[dict(symbol='HYPEUSDT',positionAmt='1',positionSide='BOTH')]; self.refused()
    def test_wrong_scoped_symbol_zero_position_refused(self):
        self.sdk.responses['/fapi/v3/positionRisk']=[dict(symbol='HYPEUSDT',positionAmt='0',positionSide='BOTH')]; self.refused()
    def test_unrelated_manual_exposure_and_protections_preserved(self):
        inventories={
            '/fapi/v1/openOrders':[dict(symbol='HYPEUSDT',orderId=11,side='BUY')],
            '/fapi/v1/openAlgoOrders':[dict(symbol='HYPEUSDT',algoId=12,side='SELL')],
            '/fapi/v3/positionRisk':[dict(symbol='HYPEUSDT',positionAmt='9',positionSide='BOTH'),dict(symbol='BTCUSDT',positionAmt='0',positionSide='BOTH')],
        }
        before=json.dumps(inventories,sort_keys=True); wire=self.sdk._wire
        def scoped_wire(method,path,query='',signed=False):
            if path in inventories:
                values=parse_qs(query); self.assertEqual(values.get('symbol'),['BTCUSDT'])
                self.sdk.responses[path]=[row for row in inventories[path] if row['symbol']==values['symbol'][0]]
            return wire(method,path,query,signed)
        self.sdk._wire=scoped_wire
        self.sql("INSERT INTO robot_entry_receipts VALUES('owned-hype-history','HYPEUSDT','2026-10-06','2026-10-06T12:00:00Z')")
        journal=self.rows()['robot_entry_receipts']; result=self.recover()
        self.assertEqual(result['status'],'ABSENT_ENTRY_REJECTED'); self.assertEqual(before,json.dumps(inventories,sort_keys=True))
        self.assertEqual(self.rows()['robot_entry_receipts'],journal); self.assertEqual(self.rows()['provider_log'],self.original['provider_log'])
        self.assertEqual(result['proofs'][0]['btc_active_positions'],0); self.assertNotIn('active_positions',result['proofs'][0])
    def test_invalid_position_refused(self): self.sdk.responses['/fapi/v3/positionRisk']=[dict(symbol='BTCUSDT',positionAmt='NaN',positionSide='BOTH')]; self.refused()
    def test_hedge_position_refused(self): self.sdk.responses['/fapi/v3/positionRisk']=[dict(symbol='BTCUSDT',positionAmt='0',positionSide='LONG')]; self.refused()
    def test_late_acceptance_snapshot_gap_refused(self):
        def hook(sdk,method,path):
            if path=='/fapi/v3/positionRisk': sdk.exact_code=None
        self.sdk.hook=hook; self.refused()
    def test_second_proof_new_accepted_entry_refused(self): self.refused(after_backup=lambda:setattr(self.sdk,'exact_code',None))
    def test_wire_method_fence_refuses_write(self):
        def operation():
            @contextlib.contextmanager
            def context():
                self.sdk._wire('POST','/fapi/v1/order','',True); yield
            return context()
        self.sdk._operation=operation; self.refused(); self.assertEqual(self.sdk.calls,[])
    def test_result_and_receipt_refuse(self):
        for change in ("UPDATE order_intents SET result='{}'", "INSERT INTO robot_entry_receipts VALUES('"+self.client+"','BTCUSDT','2026-10-07','2026-10-07T06:50:00Z')"):
            with self.subTest(change=change):
                self.sql(change); self.refused()
    def test_changed_rows_after_backup_refuse_before_second_proof(self):
        for sql in ("UPDATE robot_settings SET enabled=1", "UPDATE robot_settings SET risk='8'", "UPDATE order_intents SET updated='changed'", "UPDATE robot_candidates SET failure_code='changed'"):
            with self.subTest(sql=sql):
                before=self.rows()
                def change(): self.sql(sql)
                with self.assertRaises(m.Refused): self.recover(after_backup=change)
                self.assertNotEqual(before,self.rows()); self.assertEqual(self.sql('SELECT state FROM order_intents')[0][0],'NEEDS_REVIEW')
                self.assertEqual(sum(path=='/fapi/v1/order' for _,path,_,_ in self.sdk.calls),2)
                for name, values in before.items():
                    with sqlite3.connect(self.dbpath) as db:
                        db.execute('DELETE FROM '+name)
                        if values: db.executemany('INSERT INTO '+name+' VALUES('+','.join('?'*len(values[0]))+')', values)
                self.sdk.calls.clear()
    def test_candidate_update_failure_rolls_back_intent(self):
        self.sql("CREATE TRIGGER block_candidate BEFORE UPDATE ON robot_candidates BEGIN SELECT RAISE(ABORT,'offline-test'); END")
        self.refused(); self.assertEqual(self.sql('SELECT state FROM order_intents')[0][0],'NEEDS_REVIEW')
    def test_audit_write_failure_rolls_back(self):
        with patch('builtins.open',side_effect=OSError('offline-test')): self.refused()
    def test_unrelated_receipt_preserved_and_never_deleted(self):
        self.sql("INSERT INTO robot_entry_receipts VALUES('historical-other','ETHUSDT','2026-10-06','2026-10-06T12:00:00Z')")
        self.assertEqual(self.recover()['status'],'ABSENT_ENTRY_REJECTED'); self.assertEqual(self.sql('SELECT id FROM robot_entry_receipts'),[('historical-other',)])
    def test_id_candidate_binding_refused(self): self.sql("UPDATE order_intents SET candidate_id='foreign'"); self.refused()
    def test_second_run_no_replay_no_terminal_rewrite(self):
        self.recover(); self.sdk.calls.clear(); self.refused(); self.assertEqual(self.sdk.calls,[])

if __name__=='__main__': unittest.main(verbosity=2)
