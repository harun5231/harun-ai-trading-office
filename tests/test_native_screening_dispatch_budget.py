"""Offline native dispatch budget regression; no HTTP, model or order calls."""
import ast,json,os,sqlite3,sys,tempfile,threading,unittest
from pathlib import Path
from types import ModuleType,SimpleNamespace
from unittest.mock import patch

from tests import test_owner_source_protocol as native_fixtures
from worker.account_state import screening_contract
from worker.owner_source_protocol import NEW_OPERATION_SECONDS

ROOT=Path(__file__).resolve().parents[1]


class ScreeningDispatchBudgetTests(unittest.TestCase):
    def setUp(self):
        # The existing fixture executes the exact native pure verifier with an
        # isolated SQL journal, not an application or browser instance.
        self.f=native_fixtures.ActualNativeDispatchTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.verify.__globals__.update(__package__='worker',__name__='worker.native_dispatch_budget_fixture')
        self.db=self.f.db;self.created=self.f.now
        self.seed()

    def seed(self,count=2,index=2):
        for table in ('api_requests','research_browser_requests','robot_jobs','research_browser_responses','robot_cycles'):
            self.db.execute('DELETE FROM '+table)
        self.operation=self.f.cycle+':screening:'+str(index)+':'+str(count)
        prompt,schema=screening_contract(count)
        context=None if index==0 else dict(requested_count=count,excluded_symbols=['BTCUSDT'],
            replacement_instruction='Choose different Binance USD-M USDT perpetual coins that are not in excluded_symbols.')
        self.body,raw,self.digest=self.f.body(self.operation,prompt,schema,context);self.marker=self.body['marker']
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?)',
            (self.operation,'PENDING',1,None,None,self.created,self.digest))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',
            (self.operation,'NEUROBRO_WEB','contract',self.marker,self.digest,raw))
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
            (self.operation,'PENDING',self.f.cycle,'SCREENING',None,None))
        self.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',
            (self.f.cycle,'ACTIVE',self.f.today,0,json.dumps(dict(queue=[],screen=index-1))))
        self.db.commit()

    def check(self,elapsed=228,**kwargs):
        return self.f.verify(self.db,self.operation,self.marker,self.digest,self.f.today,
            self.created+elapsed,**kwargs)

    def test_valid_screening_at_actual_228second_failure_time_remains_permitted(self):
        self.assertTrue(self.check(228))

    def test_one_and_two_coin_screening_use_existing_finite_conversation_budget(self):
        self.assertEqual(NEW_OPERATION_SECONDS,900)
        for count,index in ((1,0),(2,0),(1,2),(2,2)):
            self.seed(count,index)
            for elapsed in (0,180,228,899,900):
                with self.subTest(count=count,index=index,elapsed=elapsed):self.assertTrue(self.check(elapsed))
            self.assertFalse(self.check(901));self.assertFalse(self.check(-1))

    def test_screening_does_not_widen_config_validation_or_allow_clock_day_change(self):
        self.assertTrue(self.check(228,max_age=30));self.assertTrue(self.check(228,max_age=300))
        for value in (0,29,301,900,True,'180'):
            with self.subTest(max_age=value):self.assertFalse(self.check(228,max_age=value))
        self.assertFalse(self.f.verify(self.db,self.operation,self.marker,self.digest,'2000-01-01',self.created+228))

    def test_request_job_cycle_and_receipt_fences_still_deny(self):
        changes=("UPDATE api_requests SET state='NEEDS_REVIEW'",'UPDATE api_requests SET attempts=2',
            "UPDATE api_requests SET body_hash='changed'","UPDATE api_requests SET output='{}'",
            "UPDATE api_requests SET idempotency='another-send'","UPDATE robot_jobs SET state='NEEDS_REVIEW'",
            "UPDATE robot_jobs SET symbol='ETHUSDT'","UPDATE robot_jobs SET risk_target='5'",
            "UPDATE robot_cycles SET state='COMPLETE'","UPDATE robot_cycles SET data='{}'",
            "UPDATE research_browser_requests SET marker='different'",
            "INSERT INTO research_browser_responses VALUES('"+self.operation+"')")
        for sql in changes:
            self.db.execute('SAVEPOINT guard');self.db.execute(sql)
            with self.subTest(sql=sql):self.assertFalse(self.check())
            self.db.execute('ROLLBACK TO guard');self.db.execute('RELEASE guard')
        self.assertFalse(self.f.verify(self.db,self.operation,self.marker,'f'*64,self.f.today,self.created+228))

    def test_legacy_analysis_still_expires_with_market_age_and_v4_budget_is_unchanged(self):
        from worker.neuroapi import SETUP_SCHEMA
        self.f.created=self.created
        self.f.context=native_fixtures.native_context(self.created)
        self.f.context.update(contract_rules=dict(symbol='BTCUSDT',source='Binance Futures /fapi/v1/exchangeInfo',observed_at=self.created),
            risk_constraints=dict(fee_symbol='BTCUSDT',fee_source='fee-source',fee_observed_at=self.created,
                maximum_loss_at_sl_usdt='5',target_loss_at_sl_usdt='5'))
        self.f.record(schema=SETUP_SCHEMA)
        self.assertTrue(self.f.check(self.created+10))
        self.assertFalse(self.f.check(self.created+228))
        self.f.record();self.assertTrue(self.f.check(self.created+228));self.assertFalse(self.f.check(self.created+901))

    def test_actual_worker_on_off_and_stopping_gates_still_control_dispatch(self):
        # Read the real Controller method without constructing its actors.
        tree=ast.parse((ROOT/'vps_native/worker/api_service.py').read_bytes())
        cls=next(node for node in tree.body if isinstance(node,ast.ClassDef)and node.name=='Controller')
        method=next(node for node in cls.body if isinstance(node,ast.FunctionDef)and node.name=='browser_dispatch')
        scope=dict(__package__='worker',sqlite3=sqlite3,os=os,selected_provider=lambda:'BROWSER',
            time=SimpleNamespace(time=lambda:self.created+228))
        exec(compile(ast.Module(body=[method],type_ignores=[]),'<native-controller-dispatch>','exec'),scope)
        module=ModuleType('worker.neurobrowser');module.verify_dispatch=self.f.verify
        with tempfile.TemporaryDirectory()as folder:
            target=Path(folder)/'ledger.sqlite3';copy=sqlite3.connect(target);self.db.backup(copy)
            copy.executescript('CREATE TABLE robot_settings(id INTEGER,enabled INTEGER,risk TEXT);INSERT INTO robot_settings VALUES(1,1,\'5\');');copy.close()
            controller=SimpleNamespace(directory=Path(folder),stopping=threading.Event())
            value=dict(operation=self.operation,marker=self.marker,request_sha256=self.digest)
            with patch.dict(sys.modules,{'worker.neurobrowser':module}),patch('worker.core.day',return_value=self.f.today),patch.dict(os.environ,{'OFFICE_MARKET_MAX_AGE':'180'}):
                check=lambda:scope['browser_dispatch'](controller,value)
                self.assertTrue(check());controller.stopping.set();self.assertFalse(check());controller.stopping.clear()
                connection=sqlite3.connect(target);connection.execute('UPDATE robot_settings SET enabled=0');connection.commit();connection.close()
                self.assertFalse(check())

    def test_verification_is_read_only_and_never_changes_request_clock(self):
        before=[tuple(row)for row in self.db.execute('SELECT * FROM api_requests')];changes=self.db.total_changes
        for elapsed in (228,500,899,901):self.check(elapsed)
        self.assertEqual([tuple(row)for row in self.db.execute('SELECT * FROM api_requests')],before)
        self.assertEqual(self.db.total_changes,changes)
