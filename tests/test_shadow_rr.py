"""Offline reproduction of existing v6 evidence; never calls a provider."""
import json
import tempfile
import time
import unittest
from decimal import Context,ROUND_HALF_EVEN,localcontext,ROUND_DOWN
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch
from worker.core import D,Ledger,Rules,Review,day,number
from worker.binance_shadow import verified_rr,load_candidates,shadow,ShadowError
from worker.neuroapi import NeuroAPI,SETUP_SCHEMA,canonical_output
from worker.provenance import stamp,current
from test_binance_shadow import ReadAccount

BTC=dict(symbol='BTCUSDT',entry='85920',TP='86300',SL='85780',execution_quantity='0.035',calculated_risk='4.900')
ETH=dict(symbol='ETHUSDT',entry='2709',TP='2736',SL='2700',execution_quantity='0.555',calculated_risk='4.995')

def expansion(n,d):return str(Context(prec=160,rounding=ROUND_HALF_EVEN).divide(D(n),D(d)))
def ratio_plan(entry='85920',tp='86300',sl='85780',rr=None):
    return dict(entry=entry,tp=tp,sl=sl,rr=expansion(19,7) if rr is None else rr)

class DerivedRRTests(unittest.TestCase):
    def test_integer_three(self):self.assertEqual(verified_rr(ratio_plan('2709','2736','2700','3')),3)
    def test_terminating_decimal(self):self.assertEqual(verified_rr(ratio_plan('100','105','98','2.5')),Fraction(5,2))
    def test_repeating_nineteen_sevenths(self):self.assertEqual(verified_rr(ratio_plan()),Fraction(19,7))
    def test_valid_expansion_over_64_characters(self):
        p=ratio_plan();self.assertGreater(len(p['rr']),64);self.assertEqual(verified_rr(p),Fraction(19,7))
    def test_below_two_rejected_even_if_decimal_rounds_to_two(self):
        # Exact rational is below 2 despite its first 16 decimal digits.
        p=ratio_plan('1','2.99999999999999999999999999999999999999999999999999999999999','0.000000000000000000000000000000000000000000000000000000000001','2')
        with self.assertRaisesRegex(ShadowError,'SHADOW_SETUP_CHANGED'):verified_rr(p)
    def test_mismatch_rejected(self):
        with self.assertRaisesRegex(ShadowError,'SHADOW_SETUP_CHANGED'):verified_rr(ratio_plan(rr='3'))
    def test_last_digit_tamper_rejected(self):
        text=expansion(19,7);wrong=text[:-1]+str((int(text[-1])+1)%10)
        with self.assertRaisesRegex(ShadowError,'SHADOW_SETUP_CHANGED'):verified_rr(ratio_plan(rr=wrong))
    def test_short_approximation_not_arbitrary_tolerance(self):
        with self.assertRaisesRegex(ShadowError,'SHADOW_SETUP_CHANGED'):verified_rr(ratio_plan(rr='2.714285714285714'))
    def test_float_rr_and_levels_rejected(self):
        for p in (ratio_plan(rr=2.714285714285714),ratio_plan(entry=85920.0)):
            with self.assertRaisesRegex(ShadowError,'SHADOW_SETUP_CHANGED'):verified_rr(p)
    def test_global_order_numeric_protections_unchanged(self):
        with self.assertRaisesRegex(Review,'INVALID_NUMBER_SIZE'):number(expansion(19,7))
        with self.assertRaises(Review):number(3.0)
    def test_ambient_decimal_precision_and_rounding_do_not_change_verification(self):
        p=ratio_plan()
        with localcontext() as ctx:
            ctx.prec=6;ctx.rounding=ROUND_DOWN
            self.assertEqual(verified_rr(p),Fraction(19,7))
    def test_ratio_resource_bounds_and_ambiguous_values_fail_closed(self):
        for text in ('3'*257,'3E999','NaN','Infinity',' 3','3 ','19/7',True,None):
            with self.subTest(text=str(text)[:20]),self.assertRaisesRegex(ShadowError,'SHADOW_SETUP_CHANGED'):
                verified_rr(ratio_plan(rr=text) if text is not None else {**ratio_plan(),'rr':None})
    def test_zero_distance_rejected(self):
        with self.assertRaisesRegex(ShadowError,'SHADOW_SETUP_CHANGED'):verified_rr(ratio_plan(sl='85920'))

class ExistingV6RRTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ledger=Ledger(Path(self.tmp.name)/'ledger.sqlite3');self.addCleanup(self.ledger.db.close)
        NeuroAPI(self.ledger,key='') # Only creates empty local schema. No API call.
        self.ledger.db.execute('CREATE TABLE analysis_checks(operation TEXT PRIMARY KEY,day TEXT,state TEXT,result TEXT)')
    def seed_existing(self,fixture,rr):
        # Reproduce the old producer's persisted bytes and stamp once at creation.
        # The reader must work without updating these bytes or calling stamp again.
        result=dict(status='ACCEPT',side='LONG',position_size=fixture['execution_quantity'],neurobro_position_size='0.002',
                    actual_RR=rr,failure_code=None,mode='DRY_RUN',**fixture)
        op=day()+':analysis-v6:'+fixture['symbol']
        output=dict(symbol=fixture['symbol'],side='LONG',position_size=D('.002'),limit_entry=D(fixture['entry']),
                    take_profit=D(fixture['TP']),stop_loss=D(fixture['SL']),risk_reward=D('2'))
        self.ledger.db.execute('INSERT INTO api_requests(operation,state,output) VALUES(?,?,?)',(op,'COMPLETE',canonical_output(output,SETUP_SCHEMA)))
        stamp(self.ledger.db,result,op)
        self.ledger.db.execute('INSERT INTO analysis_checks VALUES(?,?,?,?)',(op,day(),'COMPLETE',json.dumps(result)))
        return result
    def load_without_writes(self):
        before=list(self.ledger.db.iterdump())
        with patch('worker.provenance.stamp',side_effect=AssertionError('restamp forbidden')),patch.object(NeuroAPI,'ask',side_effect=AssertionError('NeuroAPI forbidden')):
            plans=load_candidates(self.ledger,day())
        self.assertEqual(before,list(self.ledger.db.iterdump()))
        return plans
    def test_existing_btc_fixture_loads_without_restamp_or_migration(self):
        r=self.seed_existing(BTC,expansion(19,7));self.assertTrue(current(self.ledger.db,r,r['provenance']['operation']))
        p=self.load_without_writes()[0]
        self.assertEqual((p['entry'],p['tp'],p['sl'],p['execution_quantity'],p['risk']),('85920','86300','85780','0.035','4.900'))
        self.assertEqual(p['rr'],r['actual_RR'])
    def test_existing_eth_fixture_loads_without_restamp_or_migration(self):
        self.seed_existing(ETH,'3');p=self.load_without_writes()[0]
        self.assertEqual((p['entry'],p['tp'],p['sl'],p['execution_quantity'],p['risk']),('2709','2736','2700','0.555','4.995'))
    def test_valid_provenance_but_wrong_rr_is_setup_changed_not_source_error(self):
        self.seed_existing(BTC,'3')
        with self.assertRaisesRegex(ShadowError,'SHADOW_SETUP_CHANGED'):self.load_without_writes()
    def test_missing_derived_rr_is_setup_error_not_provenance_error(self):
        self.seed_existing(BTC,None)
        with self.assertRaisesRegex(ShadowError,'SHADOW_SETUP_CHANGED'):self.load_without_writes()
    def test_shadow_both_existing_setups_no_mutations_or_neuroapi(self):
        self.seed_existing(BTC,expansion(19,7));self.seed_existing(ETH,'3')
        before=[tuple(r) for r in self.ledger.db.execute('SELECT * FROM analysis_checks')]
        class Market:
            def catalog(self):return {'BTCUSDT':{},'ETHUSDT':{}}
            def rules(self,symbol):return Rules(D('.001'),D('.001'),D('1000'),D('.1'),D('5'),time.time())
            def mark(self,symbol):return {'price':'85920' if symbol=='BTCUSDT' else '2709'}
        client=ReadAccount()
        with patch.object(NeuroAPI,'ask',side_effect=AssertionError('NeuroAPI forbidden')),patch('worker.provenance.stamp',side_effect=AssertionError('restamp forbidden')):
            out=shadow(self.ledger,client,Market())
        self.assertEqual(out['status'],'SHADOW_PREFLIGHT_OK');self.assertEqual(len(out['plans']),2)
        self.assertFalse(out['would_submit']);self.assertFalse(out['live_execution']);self.assertEqual(out['mode'],'DRY_RUN')
        self.assertEqual(client.mutations,[])
        self.assertTrue(all(path in ('/fapi/v3/account','/fapi/v3/positionRisk','/fapi/v1/openOrders','/fapi/v1/openAlgoOrders','/fapi/v1/symbolConfig','/fapi/v1/leverageBracket') for path,_ in client.reads))
        self.assertEqual(before,[tuple(r) for r in self.ledger.db.execute('SELECT * FROM analysis_checks')])
