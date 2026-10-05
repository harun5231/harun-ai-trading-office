"""Offline decision, sizing, HOLD replacement and restart regressions."""
import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from worker.core import D,Ledger,Review,Signal,risk_check,preflight
from worker.neuroapi import NeuroAPI,SCREEN_SCHEMA,SETUP_SCHEMA,NUMERIC_FIELDS,setup
from worker.workflow import Workflow
from worker.analysis import analysis_once
from worker.prompts import SCREENING,ANALYSIS
from test_neuroapi import FakeMarket,GOOD,RULES,row

SYMBOLS=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','ADAUSDT']
def hold(symbol):return dict(symbol=symbol,side='HOLD',**{k:None for k in NUMERIC_FIELDS})
class ExpandedMarket(FakeMarket):
    def respond(self,method,url,headers=None,body=None,timeout=None):
        if 'exchangeInfo' in url:
            self.calls.append((method,url));return 200,{},dict(symbols=[row(s) for s in SYMBOLS])
        return super().respond(method,url,headers,body,timeout)

class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.ledger=Ledger(self.root/'ledger.sqlite3');self.addCleanup(self.ledger.db.close)
        self.calls=[];self.screens=[];self.market=ExpandedMarket()
    def run_flow(self,decisions,screens=None,fail=None):
        screens=iter(screens or [SYMBOLS[:2]])
        def transport(method,url,headers,body,timeout):
            if body['output_schema']==SCREEN_SCHEMA:
                self.assertEqual(body['prompt'].encode(),SCREENING.encode());self.screens.append(body)
                output={'symbols':next(screens)}
            else:
                context=json.loads(body['message_history'][0]['content']);symbol=context['symbol'];self.calls.append(symbol)
                self.assertEqual(body['prompt'].encode(),ANALYSIS.encode());self.assertIn('contract_rules',context)
                if symbol==fail:raise TimeoutError('synthetic private data')
                decision=decisions.get(symbol,'LONG')
                output=hold(symbol) if decision=='HOLD' else {**GOOD,'symbol':symbol,'position_size':D('.002')}
                if decision=='SHORT':output.update(side='SHORT',take_profit=96,stop_loss=102)
                if decision=='REJECT':output['take_profit']=101
            return 200,{},dict(mode='smart',answer=None,output=output)
        self.client=NeuroAPI(self.ledger,key='synthetic-decision-test',transport=transport)
        self.flow=Workflow(self.ledger,self.client,self.market,self.root/'snapshot.json')
        return self.flow.run()
    def test_long_short_independent_execution_and_immutable_levels(self):
        for side,sl,tp in [('LONG',98,104),('SHORT',102,96)]:
            value={**GOOD,'side':side,'stop_loss':sl,'take_profit':tp,'position_size':D('.002')};original=dict(value)
            signal=setup(value,'BTCUSDT');plan=risk_check(signal,RULES())
            self.assertEqual(value,original);self.assertEqual(plan['side'],side)
            self.assertEqual((D(plan['entry']),D(plan['tp']),D(plan['sl'])),(D(100),D(tp),D(sl)))
            self.assertEqual(plan['neurobro_position_size'],'0.002');self.assertEqual(D(plan['execution_quantity']),D('2.5'))
            self.assertEqual(D(plan['risk']),5);self.assertEqual(plan['margin_mode'],'CROSS');self.assertEqual(plan['leverage'],75)
    def test_execution_tampering_above_or_below_target_rejected(self):
        plan=risk_check(setup(GOOD,'BTCUSDT'),RULES())
        for qty in ('2.501','.002'):
            altered={**plan,'quantity':qty,'execution_quantity':qty,'risk':str(D(qty)*2)}
            with self.assertRaises(Review):preflight(altered,RULES())
    def test_floors_never_exceed_five_and_next_step_exceeds_cap(self):
        for distance in ('1.2','3','7.123','12345'):
            d=D(distance);rules=replace(RULES(),tick=D('.001'),min_notional=D('0'))
            signal=Signal('BTCUSDT','LONG',d*2,d*4,d,None)
            try:plan=risk_check(signal,rules)
            except Review:
                self.assertLess(D('5')/d,rules.minimum);continue
            qty=D(plan['execution_quantity']);self.assertLessEqual(qty*d,5);self.assertGreater((qty+rules.step)*d,5)
    def test_hold_nulls_only_no_invented_levels(self):
        self.assertEqual(setup(hold('BTCUSDT'),'BTCUSDT').side,'HOLD')
        for field in NUMERIC_FIELDS:
            with self.assertRaises(Review):setup({**hold('BTCUSDT'),field:1},'BTCUSDT')
        with self.assertRaises(Review):setup({**GOOD,'limit_entry':None},'BTCUSDT')
    def test_hold_cache_and_analysis_check_no_slot(self):
        self.client=NeuroAPI(self.ledger,key='synthetic-decision-test',transport=lambda m,u,h,b,timeout:(200,{},dict(mode='smart',answer=None,output=hold(json.loads(b['message_history'][0]['content'])['symbol']))))
        result=analysis_once(self.ledger,self.client,self.market,SYMBOLS[:2])
        self.assertEqual([r['status'] for r in result],['HOLD','HOLD']);self.assertEqual(self.ledger.count(),0)
        self.assertTrue(all(r['execution_quantity'] is None and r['entry'] is None for r in result))
        self.client.transport=lambda *a,**k:self.fail('Replay')
        self.assertEqual(result,analysis_once(self.ledger,self.client,self.market,SYMBOLS[:2]))
    def test_hold_replacement_same_prompt_and_first_eligible(self):
        result=self.run_flow({'BTCUSDT':'HOLD'},[SYMBOLS[:2],['BTCUSDT','SOLUSDT']])
        self.assertEqual(self.calls,['BTCUSDT','ETHUSDT','SOLUSDT']);self.assertEqual(result['trades_today'],2)
        self.assertEqual(len(self.screens),2)
        states=[e['state'] for e in result['events']]
        for state in ('HOLD','REPLACEMENT_SCREENING','REPLACEMENT_SELECTED','DRY_RUN_READY'):self.assertIn(state,states)
        self.assertNotIn('BTCUSDT',[r['plan']['symbol'] for r in result['trades']])
    def test_hold_again_bounded_three_replacements(self):
        result=self.run_flow(dict.fromkeys(SYMBOLS,'HOLD'),[SYMBOLS[:2],SYMBOLS[2:4],SYMBOLS[3:5],SYMBOLS[4:6]])
        self.assertEqual(len(self.screens),4);self.assertEqual(len(self.calls),5);self.assertEqual(len(set(self.calls)),5)
        self.assertEqual(result['status'],'INSUFFICIENT_ACTIONABLE_SETUPS');self.assertEqual(result['trades_today'],0)
        self.assertFalse(result['live_enabled'])
    def test_all_replacement_candidates_seen_no_reanalysis(self):
        result=self.run_flow({'BTCUSDT':'HOLD'},[SYMBOLS[:2]]*4)
        self.assertEqual(self.calls,SYMBOLS[:2]);self.assertEqual(len(self.screens),4);self.assertEqual(result['trades_today'],1)
        self.assertEqual(result['status'],'INSUFFICIENT_ACTIONABLE_SETUPS')
    def test_technical_reject_does_not_trigger_replacement(self):
        result=self.run_flow(dict.fromkeys(SYMBOLS[:2],'REJECT'))
        self.assertEqual(len(self.screens),1);self.assertEqual(result['trades_today'],0)
        self.assertNotIn('HOLD',[e['state'] for e in result['events']])
    def test_api_failure_does_not_trigger_replacement(self):
        self.run_flow({},fail='BTCUSDT');self.assertEqual(len(self.screens),1)
    def test_replacement_rejection_does_not_replace_again(self):
        result=self.run_flow({'BTCUSDT':'HOLD','SOLUSDT':'REJECT'},[SYMBOLS[:2],SYMBOLS[2:4]])
        self.assertEqual(len(self.screens),2);self.assertEqual(result['trades_today'],1)
    def test_two_actionable_stop_no_extra_research_and_restart_safe(self):
        result=self.run_flow({'ETHUSDT':'SHORT'})
        self.assertEqual(result['trades_today'],2);self.assertEqual(self.calls,SYMBOLS[:2]);self.assertEqual(len(self.screens),1)
        self.client.transport=lambda *a,**k:self.fail('Replay')
        reopened=Ledger(self.root/'ledger.sqlite3')
        try:
            result=Workflow(reopened,self.client,self.market,self.root/'snapshot.json').run()
            self.assertEqual(result['trades_today'],2);self.assertEqual(result['status'],'LOCKED')
        finally:reopened.db.close()
    def test_interrupted_replacement_cycle_never_replays(self):
        self.run_flow({'BTCUSDT':'HOLD'},[SYMBOLS[:2]]*4)
        self.ledger.db.execute("UPDATE cycles SET state='PENDING'")
        self.client.transport=lambda *a,**k:self.fail('Replay')
        self.assertEqual(self.flow.run()['status'],'ERROR');self.assertEqual(len(self.screens),4)
    def test_binance_requests_public_get_only(self):
        self.run_flow({})
        self.assertTrue(self.market.calls)
        self.assertTrue(all(m=='GET' and '/fapi/v1/' in u and '/order' not in u for m,u in self.market.calls))
    def test_crash_after_replacement_claim_is_not_replayed(self):
        calls=[]
        def transport(m,u,h,b,timeout):
            calls.append(b['prompt'])
            if b['output_schema']==SCREEN_SCHEMA:
                if len(calls)>1:raise KeyboardInterrupt('simulated process stop')
                output={'symbols':SYMBOLS[:2]}
            else:output=hold(json.loads(b['message_history'][0]['content'])['symbol'])
            return 200,{},dict(mode='smart',answer=None,output=output)
        client=NeuroAPI(self.ledger,key='synthetic-decision-test',transport=transport)
        flow=Workflow(self.ledger,client,self.market,self.root/'snapshot.json')
        with self.assertRaises(KeyboardInterrupt):flow.run()
        self.assertEqual(self.ledger.db.execute('SELECT state FROM cycles').fetchone()[0],'PENDING')
        self.assertEqual(self.ledger.db.execute("SELECT state FROM api_requests WHERE operation LIKE '%replacement-screening:1'").fetchone()[0],'PENDING')
        client.transport=lambda *a,**k:self.fail('Replay after crash')
        self.assertEqual(flow.run()['status'],'ERROR');self.assertEqual(self.ledger.count(),0)
    def test_schema_explicitly_supports_hold_without_prices(self):
        self.assertEqual(SETUP_SCHEMA['properties']['side']['enum'],['LONG','SHORT','HOLD'])
        for key in NUMERIC_FIELDS:self.assertIn('null',SETUP_SCHEMA['properties'][key]['type'])
        from worker.neuroapi import canonical_output
        self.assertEqual(json.loads(canonical_output(hold('BTCUSDT'),SETUP_SCHEMA)),hold('BTCUSDT'))
