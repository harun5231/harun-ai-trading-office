import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from worker.core import Ledger, D, Review, risk_check
from worker.neuroapi import NeuroAPI, SETUP_SCHEMA, setup
from worker.prompts import ANALYSIS
from worker.analysis import analysis_context, analysis_once, SIZING_CONTRACT
from worker.workflow import Workflow
from test_neuroapi import FakeMarket, GOOD, RULES

class AnalysisContextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.ledger=Ledger(self.root/'ledger.sqlite3');self.addCleanup(self.ledger.db.close)
        self.market=FakeMarket();self.calls=[]
    def client(self,output=None):
        def transport(method,url,headers,body,timeout):
            context=json.loads(body['message_history'][0]['content'])
            self.calls.append((body,context))
            self.assertEqual(body['prompt'].encode(),ANALYSIS.encode())
            self.assertIn('contract_rules',context)
            value={**(output or GOOD),'symbol':context['symbol']}
            return 200,{},dict(mode='smart',answer=None,output=value)
        return NeuroAPI(self.ledger,key='synthetic-analysis-placeholder',transport=transport,sleep=lambda _:None)
    def test_context_exact_exchange_constraints_and_existing_data(self):
        context,rules=analysis_context(self.market,'BTCUSDT')
        c=context['contract_rules'];risk=context['risk_constraints']
        for field,value in {'stepSize':'0.001','minQty':'0.001','maxQty':'1000','tickSize':'0.01','minNotional':'5','minPrice':'0.01','maxPrice':'1000000'}.items():self.assertEqual(c[field],value)
        self.assertEqual(c['quantity_unit'],'base_asset')
        self.assertEqual(risk,dict(quantity_unit='base_asset',margin_mode_target='CROSS',leverage_target=75,maximum_loss_at_sl_usdt='5',minimum_actual_reward_risk=2,target_loss_at_sl_usdt='5',position_sizing_contract=SIZING_CONTRACT))
        self.assertEqual(context['source'],'Binance Futures');self.assertEqual(context['symbol'],'BTCUSDT')
        self.assertEqual(set(context['timeframes']),{'1h','15m'})
        for key in ('mark_price','fetched_at','server_time'):self.assertIn(key,context)
        self.assertTrue(all(method=='GET' for method,_ in self.market.calls))
    def test_percent_price_context_uses_real_rule_values(self):
        rules=replace(RULES(),multiplier_up=D('1.1'),multiplier_down=D('.9'),mark_price=D('100'))
        self.market.rules=lambda symbol:rules
        c,_=analysis_context(self.market,'BTCUSDT')
        self.assertEqual(c['contract_rules']['percent_price'],dict(multiplierUp='1.1',multiplierDown='0.9',reference_mark_price='100',min_entry_price='90.0',max_entry_price='110.0'))
    def test_rules_failure_blocks_paid_analysis(self):
        self.market.rules=lambda symbol:(_ for _ in ()).throw(Review('INVALID_CONTRACT_CONTEXT'))
        result=analysis_once(self.ledger,self.client(),self.market,['BTCUSDT','ETHUSDT'])
        self.assertEqual(self.calls,[]);self.assertTrue(all(r['status']=='REJECT' for r in result))
    def test_one_request_per_coin_duplicate_command_no_replay_no_trade(self):
        client=self.client();symbols=['BTCUSDT','ETHUSDT']
        first=analysis_once(self.ledger,client,self.market,symbols)
        self.assertEqual([r['status'] for r in first],['ACCEPT','ACCEPT']);self.assertEqual(len(self.calls),2)
        second=analysis_once(self.ledger,client,self.market,list(reversed(symbols)))
        self.assertEqual(second,list(reversed(first)));self.assertEqual(len(self.calls),2)
        self.assertEqual(self.ledger.count(),0)
        self.assertFalse(self.ledger.db.execute("SELECT name FROM sqlite_master WHERE name='cycles'").fetchone())
        self.assertEqual(D(first[0]['position_size']),D('2.5'));self.assertEqual(D(first[0]['calculated_risk']),D('5'));self.assertEqual(first[0]['actual_RR'],'2')
    def test_provider_precision_audit_without_extra_request(self):
        result=analysis_once(self.ledger,self.client({**GOOD,'position_size':D('2.0001')}),self.market,['BTCUSDT','ETHUSDT'])
        self.assertEqual(len(self.calls),2)
        self.assertTrue(all(r['status']=='ACCEPT' and D(r['execution_quantity'])==D('2.5') for r in result))
        self.assertTrue(all(r['neurobro_position_size']=='2.0001' for r in result))
    def test_provider_size_audit_and_safe_execution(self):
        result=analysis_once(self.ledger,self.client({**GOOD,'position_size':3}),self.market,['BTCUSDT','ETHUSDT'])
        self.assertTrue(all(r['status']=='ACCEPT' and D(r['calculated_risk'])==D('5') and r['neurobro_position_size']=='3' for r in result))
        self.assertEqual(len(self.calls),2)
    def test_rr_below_two_rejected_without_repair_request(self):
        result=analysis_once(self.ledger,self.client({**GOOD,'take_profit':103}),self.market,['BTCUSDT','ETHUSDT'])
        self.assertTrue(all(r['failure_code']=='RISK_REWARD_BELOW_2' for r in result));self.assertEqual(len(self.calls),2)
    def test_unknown_or_duplicate_symbols_block_all_paid_calls(self):
        for symbols in (['BTCUSDT','FAKEUSDT'],['BTCUSDT','BTCUSDT'],['BTC','ETH'],['BTCUSDT']):
            with self.assertRaises(Review):analysis_once(self.ledger,self.client(),self.market,symbols)
        self.assertEqual(self.calls,[])
    def test_old_api_records_and_cycles_unchanged(self):
        client=self.client()
        self.ledger.db.execute('CREATE TABLE cycles(day TEXT PRIMARY KEY,state TEXT,created REAL)')
        self.ledger.db.execute("INSERT INTO cycles VALUES('2026-10-05','COMPLETE',0)")
        self.ledger.db.execute("INSERT INTO api_requests(operation,state,attempts) VALUES('2026-10-05:analysis:BTCUSDT','NEEDS_REVIEW',1)")
        with patch('worker.analysis.day',return_value='2026-10-05'):analysis_once(self.ledger,client,self.market,['BTCUSDT','ETHUSDT'])
        row=self.ledger.db.execute("SELECT state,attempts FROM api_requests WHERE operation='2026-10-05:analysis:BTCUSDT'").fetchone()
        self.assertEqual(tuple(row),('NEEDS_REVIEW',1))
        self.assertEqual(self.ledger.db.execute('SELECT state FROM cycles').fetchone()[0],'COMPLETE')
    def test_normal_workflow_also_sends_contract_context(self):
        from worker.neuroapi import SCREEN_SCHEMA
        def transport(method,url,headers,body,timeout):
            if body['output_schema']==SCREEN_SCHEMA:value={'symbols':['BTCUSDT','ETHUSDT']}
            else:
                context=json.loads(body['message_history'][0]['content']);self.assertIn('stepSize',context['contract_rules'])
                self.calls.append(context);value={**GOOD,'symbol':context['symbol']}
            return 200,{},dict(mode='smart',answer=None,output=value)
        client=NeuroAPI(self.ledger,key='synthetic-analysis-placeholder',transport=transport)
        result=Workflow(self.ledger,client,self.market,self.root/'snapshot.json').run()
        self.assertEqual(len(self.calls),2);self.assertEqual(result['trades_today'],2);self.assertFalse(result['live_enabled'])
    def test_schema_describes_contract_and_risk_without_changing_prompt(self):
        description=SETUP_SCHEMA['properties']['position_size']['description']
        for word in ('base-asset','Informational','Risk Manager'):self.assertIn(word,description)
        for field in ('limit_entry','take_profit','stop_loss'):self.assertIn('tickSize',SETUP_SCHEMA['properties'][field]['description'])
    def test_interrupted_check_is_not_replayed(self):
        client=self.client()
        self.ledger.db.execute('CREATE TABLE analysis_checks(operation TEXT PRIMARY KEY,day TEXT,state TEXT,result TEXT)')
        self.ledger.db.execute("INSERT INTO analysis_checks VALUES('2026-10-05:analysis-v6:BTCUSDT','2026-10-05','PENDING',NULL)")
        with patch('worker.analysis.day',return_value='2026-10-05'):
            result=analysis_once(self.ledger,client,self.market,['BTCUSDT','ETHUSDT'])
        self.assertEqual(result[0]['failure_code'],'ANALYSIS_CHECK_NEEDS_REVIEW');self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][1]['symbol'],'ETHUSDT')
    def test_cli_accepts_exact_symbols_and_only_invokes_analysis_check(self):
        import contextlib,io
        from unittest.mock import Mock
        from worker.__main__ import main
        out=io.StringIO();check=Mock(return_value=[])
        with patch('sys.argv',['worker','analysis-once','BTCUSDT','ETHUSDT','--data-dir',str(self.root)]),patch('worker.__main__.NeuroAPI'),patch('worker.__main__.Market'),patch('worker.analysis.analysis_once',check),contextlib.redirect_stdout(out):
            self.assertEqual(main(),0)
        self.assertEqual(check.call_args.args[-1],['BTCUSDT','ETHUSDT']);self.assertEqual(json.loads(out.getvalue()),[])
