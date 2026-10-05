import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from worker.core import Ledger, Review, D, risk_check
from worker.diagnostics import read_records
from worker.neuroapi import NeuroAPI, SETUP_SCHEMA, SCREEN_SCHEMA, setup
from worker.http_client import request
from test_neuroapi import GOOD, RULES, FakeMarket
from worker.workflow import Workflow

OP='2026-10-05:analysis:BTCUSDT'
class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'trading').mkdir()
        self.db=Ledger(self.root/'trading'/'ledger.sqlite3');self.addCleanup(self.db.db.close)
    def client(self,response):
        def transport(*args,**kwargs):
            if isinstance(response,Exception):raise response
            return response
        return NeuroAPI(self.db,key='synthetic-private-marker',transport=transport,sleep=lambda _:None)
    def check_failure(self,response,expected,operation=OP,validate=None):
        client=self.client(response)
        with self.assertRaises(Review):client.ask(operation,'PRIVATE_PROMPT',SETUP_SCHEMA,validate or (lambda v:setup(v,'BTCUSDT')),{'private':'PRIVATE_CONTEXT'})
        row=self.db.db.execute('SELECT * FROM api_requests WHERE operation=?',(operation,)).fetchone()
        self.assertEqual(row['failure_code'],expected);self.assertEqual(row['state'],'NEEDS_REVIEW');self.assertIsNone(row['output'])
        content=json.dumps(dict(row))
        for value in ('PRIVATE_PROMPT','PRIVATE_CONTEXT','PRIVATE_PROSE','synthetic-private-marker'):self.assertNotIn(value,content)
    def envelope(self,value):return 200,{},dict(mode='smart',answer=None,output=value)
    def test_http_categories_and_bounded_retry(self):
        for code in (400,401,403,409,429,503,422,500):
            with self.subTest(code=code):
                operation=OP+str(code)
                self.check_failure((code,{}, {'answer':'PRIVATE_PROSE'}),'HTTP_'+str(code),operation)
                self.assertEqual(self.db.db.execute('SELECT attempts FROM api_requests WHERE operation=?',(operation,)).fetchone()[0],3 if code in (429,503) else 1)
    def test_network_uncertain(self):self.check_failure(TimeoutError('synthetic-private-marker'),'NETWORK_UNCERTAIN')
    def test_invalid_envelope(self):self.check_failure((200,{}, {'mode':'smart','answer':'PRIVATE_PROSE','output':GOOD}),'INVALID_RESPONSE_ENVELOPE')
    def test_missing_or_wrong_output(self):
        for i,value in enumerate((None,'PRIVATE_PROSE',[],1)):
            self.check_failure(self.envelope(value),'INVALID_OUTPUT_SCHEMA',OP+str(i))
    def test_schema_mismatch(self):self.check_failure(self.envelope({**GOOD,'extra':'PRIVATE_PROSE'}),'INVALID_SETUP_SCHEMA')
    def test_symbol_side_mismatch(self):
        for i,value in enumerate(({**GOOD,'symbol':'ETHUSDT'},{**GOOD,'side':'BUY'})):
            self.check_failure(self.envelope(value),'INVALID_SETUP_SYMBOL_SIDE',OP+str(i))
    def test_numeric_type_mismatch(self):
        for i,value in enumerate(('2',True,None,[],2.0)):
            self.check_failure(self.envelope({**GOOD,'limit_entry':value}),'INVALID_NUMERIC_TYPE',OP+str(i))
    def test_invalid_numeric_values(self):
        for i,value in enumerate((0,-1,D('NaN'),D('Infinity'),D('1e1000'))):
            self.check_failure(self.envelope({**GOOD,'position_size':value}),'INVALID_NUMERIC_VALUE',OP+str(i))
    def test_invalid_price_direction(self):
        for i,value in enumerate(({**GOOD,'stop_loss':100},{**GOOD,'stop_loss':101},{**GOOD,'take_profit':99})):
            self.check_failure(self.envelope(value),'INVALID_ENTRY_TP_SL',OP+str(i))
    def test_actual_rr_below_two(self):self.check_failure(self.envelope({**GOOD,'take_profit':D('103.9999')}),'RISK_REWARD_BELOW_2')
    def test_invalid_declared_rr(self):self.check_failure(self.envelope({**GOOD,'risk_reward':0}),'INVALID_NUMERIC_VALUE')
    def test_unknown_validator_error_is_sanitized(self):
        self.check_failure(self.envelope(GOOD),'VALIDATION_REJECTED',validate=lambda v:(_ for _ in ()).throw(ValueError('PRIVATE_PROSE synthetic-private-marker')))
    def test_actual_rr_is_authority_and_levels_unchanged(self):
        value={**GOOD,'take_profit':D('104.0002')}
        signal=setup(value,'BTCUSDT')
        self.assertEqual(signal.tp,D('104.0002'));self.assertEqual(signal.quantity,D('2.5'))
        from dataclasses import replace
        plan=risk_check(signal,replace(RULES(),tick=D('.0001')))
        self.assertEqual(D(plan['rr']),D('2.0001'));self.assertEqual(plan['tp'],'104.0002');self.assertEqual(D(plan['quantity']),D('2.5'))
    def test_declared_rr_does_not_override_actual_under_two(self):
        with self.assertRaisesRegex(Review,'RISK_REWARD_BELOW_2'):setup({**GOOD,'take_profit':103,'risk_reward':100},'BTCUSDT')
    def test_risk_rejection_is_not_provider_failure(self):
        value={**GOOD,'position_size':10};client=self.client(self.envelope(value))
        output=client.ask(OP,'prompt',SETUP_SCHEMA,lambda v:setup(v,'BTCUSDT'))
        from dataclasses import replace
        with self.assertRaises(Review) as caught:risk_check(setup(output,'BTCUSDT'),replace(RULES(),min_notional=D('1000')))
        client.record_validation(OP,caught.exception)
        row=read_records(self.root)[0]
        self.assertEqual(row['state'],'COMPLETE');self.assertEqual(row['failure_code'],'NO_LEGAL_MAX_RISK_QUANTITY')
        self.assertEqual(value['position_size'],10)
    def test_json_numbers_use_decimal_without_rounding(self):
        response=MagicMock();response.__enter__.return_value=response;response.code=200;response.headers={}
        response.read.return_value=b'{"position_size":2.50000000000000000000000000001,"entry":1e-8}'
        with patch('worker.http_client.build_opener') as factory:
            factory.return_value.open.return_value=response
            _,_,data=request('GET','https://api.neurobro.ai/api/v1/health')
        self.assertEqual(data['position_size'],D('2.50000000000000000000000000001'));self.assertEqual(data['entry'],D('1e-8'))
    def test_invalid_json_has_envelope_code(self):
        response=MagicMock();response.__enter__.return_value=response;response.code=200;response.headers={};response.read.return_value=b'PRIVATE_PROSE'
        with patch('worker.http_client.build_opener') as factory:
            factory.return_value.open.return_value=response
            with self.assertRaisesRegex(Review,'INVALID_RESPONSE_ENVELOPE'):request('GET','https://api.neurobro.ai/api/v1/health')
    def test_canonical_cache_drops_envelope_prose(self):
        value={**GOOD,'take_profit':D('104.0002')}
        client=self.client((200,{},dict(mode='smart',answer=None,output=value,extra='PRIVATE_PROSE')))
        client.ask(OP,'PRIVATE_PROMPT',SETUP_SCHEMA,lambda v:setup(v,'BTCUSDT'))
        row=dict(self.db.db.execute('SELECT * FROM api_requests').fetchone())
        self.assertNotIn('PRIVATE_PROSE',json.dumps(row));self.assertNotIn('PRIVATE_PROMPT',json.dumps(row))
        client.transport=lambda *a,**k:self.fail('Replay')
        cached=client.ask(OP,'PRIVATE_PROMPT',SETUP_SCHEMA,lambda v:setup(v,'BTCUSDT'))
        self.assertEqual(cached['take_profit'],D('104.0002'))
    def test_legacy_readonly_then_migration_preserves_all_records(self):
        self.db.db.execute('CREATE TABLE api_requests(operation TEXT PRIMARY KEY,idempotency TEXT,body_hash TEXT,state TEXT,created REAL,attempts INTEGER,output TEXT)')
        self.db.db.execute('CREATE TABLE cycles(day TEXT PRIMARY KEY,state TEXT,created REAL)')
        self.db.db.execute("INSERT INTO cycles VALUES('2026-10-05','COMPLETE',0)")
        for symbol in ('BTCUSDT','ETHUSDT'):
            self.db.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?)',('2026-10-05:analysis:'+symbol,None,'old','NEEDS_REVIEW',0,1,None))
        self.db.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?)',('2026-10-05:screening',None,'old','COMPLETE',0,1,'{"symbols":["BTCUSDT","ETHUSDT"]}'))
        before=[tuple(r) for r in self.db.db.execute('SELECT * FROM api_requests')]
        records=read_records(self.root)
        self.assertEqual([r['failure_code'] for r in records[:2]],['LEGACY_REASON_UNAVAILABLE']*2)
        self.assertEqual(len(list(self.db.db.execute('PRAGMA table_info(api_requests)'))),7)
        client=self.client(TimeoutError())
        self.assertEqual([tuple(r)[:7] for r in self.db.db.execute('SELECT * FROM api_requests')],before)
        self.assertEqual(self.db.db.execute('SELECT state FROM cycles').fetchone()[0],'COMPLETE')
        client.transport=lambda *a,**k:self.fail('Old request replayed')
        with self.assertRaises(Review):client.ask(OP,'new prompt',SETUP_SCHEMA,lambda v:None)
        self.assertEqual(read_records(self.root)[0]['failure_code'],'LEGACY_REASON_UNAVAILABLE')
    def test_readonly_command_never_initializes_worker_or_reads_secrets(self):
        from worker.__main__ import main
        out=io.StringIO()
        with patch('sys.argv',['worker','diagnostics','--data-dir',str(self.root)]),patch('worker.__main__.directory',side_effect=AssertionError('write')),patch('worker.__main__.NeuroAPI',side_effect=AssertionError('key')),contextlib.redirect_stdout(out):
            self.assertEqual(main(),0)
        self.assertEqual(json.loads(out.getvalue()),[])
    def test_diagnostic_view_redacts_arbitrary_stored_strings(self):
        self.client(TimeoutError())
        self.db.db.execute('INSERT INTO api_requests(operation,state,attempts,failure_code) VALUES(?,?,?,?)',('private@email.invalid','secret',1,'PRIVATE_PROSE'))
        output=json.dumps(read_records(self.root))
        for value in ('private@email.invalid','secret','PRIVATE_PROSE'):self.assertNotIn(value,output)
    def test_workflow_records_risk_rejection_and_never_retries(self):
        calls=[]
        def transport(method,url,headers=None,body=None,timeout=None):
            calls.append(body)
            if body['output_schema']==SCREEN_SCHEMA:value={'symbols':['BTCUSDT','ETHUSDT']}
            else:value={**GOOD,'limit_entry':D('100.001'),'take_profit':105,'symbol':json.loads(body['message_history'][0]['content'])['symbol']}
            return self.envelope(value)
        client=NeuroAPI(self.db,key='synthetic-private-marker',transport=transport)
        flow=Workflow(self.db,client,FakeMarket(),self.root/'snapshot.json')
        result=flow.run();self.assertEqual(result['trades_today'],0)
        rows=[r for r in read_records(self.root) if ':analysis:' in r['operation']]
        self.assertEqual([r['failure_code'] for r in rows],['INVALID_PRICE_FILTER']*2)
        self.assertEqual([r['state'] for r in rows],['COMPLETE']*2)
        flow.run();self.assertEqual(len(calls),3)
    def test_network_and_http_diagnostics_are_not_validation_rejections(self):
        for i,response in enumerate((TimeoutError(),(403,{},None))):
            self.check_failure(response,('NETWORK_UNCERTAIN','HTTP_403')[i],OP+str(i))
    def test_migration_repeated_is_idempotent(self):
        self.client(TimeoutError());self.client(TimeoutError())
        columns=[r[1] for r in self.db.db.execute('PRAGMA table_info(api_requests)')]
        self.assertEqual(columns.count('failure_code'),1)
    def test_setup_schema_uses_json_numbers(self):
        from worker.neuroapi import NUMERIC_FIELDS
        for name in NUMERIC_FIELDS:self.assertEqual(SETUP_SCHEMA['properties'][name]['type'],['number','null'])
    def test_declared_rr_rounding_below_two_does_not_override_actual(self):
        signal=setup({**GOOD,'risk_reward':D('1.9999')},'BTCUSDT')
        self.assertEqual(risk_check(signal,RULES())['rr'],'2')
