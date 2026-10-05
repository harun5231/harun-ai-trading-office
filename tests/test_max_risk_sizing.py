import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from worker.core import D,Signal,Rules,Review,Ledger,risk_check,maximum_risk_quantity
from worker.analysis import analysis_context,analysis_once,SIZING_CONTRACT
from worker.neuroapi import NeuroAPI
from worker.diagnostics import read_records
from test_neuroapi import FakeMarket,GOOD

class MaxRiskTests(unittest.TestCase):
    def setUp(self):
        self.rules=Rules(D('.01'),D('.01'),D('1000'),D('.01'),D('5'),time.time())
        self.signal=Signal('BTCUSDT','LONG',D('100'),D('102.40'),D('98.80'),D('4.16'))
    def test_largest_step_quantity_risk_4_992_accepted(self):
        plan=risk_check(self.signal,self.rules)
        self.assertEqual(D(plan['risk']),D('4.992'));self.assertEqual(plan['quantity'],'4.16')
        self.assertEqual(maximum_risk_quantity(self.signal.entry,self.signal.sl,self.rules),D('4.16'))
    def test_provider_above_five_kept_as_audit_execution_safe(self):
        signal=replace(self.signal,quantity=D('4.17'))
        self.assertEqual(D(risk_check(signal,self.rules)['risk']),D('4.992'))
        self.assertEqual(signal.quantity,D('4.17'))
    def test_small_provider_quantities_do_not_limit_execution(self):
        for qty in ('4.15','.05'):
            signal=replace(self.signal,quantity=D(qty))
            self.assertEqual(risk_check(signal,self.rules)['quantity'],'4.16')
            self.assertEqual(signal.quantity,D(qty))
    def test_max_quantity_cap_allows_lower_risk_when_binding(self):
        rules=replace(self.rules,maximum=D('2.005'))
        signal=replace(self.signal,quantity=D('2.00'))
        self.assertEqual(risk_check(signal,rules)['quantity'],'2.00')
        self.assertEqual(risk_check(replace(signal,quantity=D('2.01')),rules)['quantity'],'2.00')
    def test_min_quantity_impossible_is_rejected(self):
        rules=replace(self.rules,minimum=D('5'))
        with self.assertRaisesRegex(Review,'NO_LEGAL_MAX_RISK_QUANTITY'):maximum_risk_quantity(self.signal.entry,self.signal.sl,rules)
        with self.assertRaises(Review):risk_check(self.signal,rules)
    def test_min_notional_impossible_is_rejected(self):
        rules=replace(self.rules,min_notional=D('500'))
        with self.assertRaises(Review):risk_check(self.signal,rules)
        with self.assertRaisesRegex(Review,'NO_LEGAL_MAX_RISK_QUANTITY'):maximum_risk_quantity(self.signal.entry,self.signal.sl,rules)
    def test_off_step_provider_quantity_is_audit_only(self):
        self.assertEqual(risk_check(replace(self.signal,quantity=D('4.166')),self.rules)['quantity'],'4.16')
    def test_rr_six_or_eight_is_valid_below_two_is_not(self):
        for tp in ('107.20','109.60'):risk_check(replace(self.signal,tp=D(tp)),self.rules)
        with self.assertRaises(Review):risk_check(replace(self.signal,tp=D('102.39')),self.rules)
    def test_small_eth_audit_size_and_deterministic_execution(self):
        rules=replace(self.rules,step=D('.001'),minimum=D('.001'))
        signal=Signal('ETHUSDT','LONG',D('2715'),D('2730'),D('2712.5'),D('.028'))
        self.assertEqual(risk_check(signal,rules)['neurobro_position_size'],'0.028')
        self.assertEqual(D(risk_check(replace(signal,quantity=D('2')),rules)['risk']),D('5'))
    def test_exact_floor_near_step_boundary(self):
        distance=D('1.000000000000000000000000000000000000000000000000000000000000001')
        rules=replace(self.rules,step=D('1'))
        self.assertEqual(maximum_risk_quantity(distance*2,distance,rules),D('4'))
    def test_context_contains_literal_target_contract(self):
        context,_=analysis_context(FakeMarket(),'BTCUSDT')
        self.assertEqual(context['risk_constraints']['target_loss_at_sl_usdt'],'5')
        self.assertEqual(context['risk_constraints']['position_sizing_contract'],SIZING_CONTRACT)
        self.assertIn('largest valid Binance base-asset quantity',SIZING_CONTRACT)
    def test_v6_new_check_preserves_v5_and_never_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'trading').mkdir();ledger=Ledger(root/'trading'/'ledger.sqlite3')
            try:
                ledger.db.execute('CREATE TABLE analysis_checks(operation TEXT PRIMARY KEY,day TEXT,state TEXT,result TEXT)')
                for symbol in ('BTCUSDT','ETHUSDT'):ledger.db.execute('INSERT INTO analysis_checks VALUES(?,?,?,?)',('2026-10-05:analysis-v5:'+symbol,'2026-10-05','COMPLETE','{}'))
                calls=[]
                def transport(method,url,headers,body,timeout):
                    import json
                    symbol=json.loads(body['message_history'][0]['content'])['symbol'];calls.append(symbol)
                    return 200,{},dict(mode='smart',answer=None,output={**GOOD,'symbol':symbol})
                client=NeuroAPI(ledger,key='synthetic-sizing-placeholder',transport=transport)
                with patch('worker.analysis.day',return_value='2026-10-05'):
                    first=analysis_once(ledger,client,FakeMarket(),['BTCUSDT','ETHUSDT'])
                    self.assertEqual([r['status'] for r in first],['ACCEPT','ACCEPT'])
                    self.assertEqual(first,analysis_once(ledger,client,FakeMarket(),['BTCUSDT','ETHUSDT']))
                self.assertEqual(calls,['BTCUSDT','ETHUSDT']);self.assertEqual(ledger.count(),0)
                self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM analysis_checks WHERE operation LIKE '%:analysis-v5:%' AND result='{}'").fetchone()[0],2)
                self.assertTrue(all(':analysis-v6:' in r['operation'] for r in read_records(root)))
            finally:ledger.db.close()
