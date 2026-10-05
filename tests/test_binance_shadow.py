"""Offline persisted-setup shadow contract; no Binance/NeuroAPI calls."""
import contextlib
import io
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock,patch
from worker.core import D,Ledger,Signal,day,risk_check,Review
from worker.neuroapi import NeuroAPI,SETUP_SCHEMA,canonical_output
from worker.binance_shadow import shadow,run,plan_payload,identity,ids,ShadowError,ShadowStore
from worker.provenance import stamp
from worker.execution_model import ExecutionModel,ModelError
from test_neuroapi import GOOD,FakeMarket,RULES

class ReadAccount:
    def __init__(self):
        self.reads=[];self.mutations=[];self.position=[];self.orders=[];self.algos=[]
        self.global_positions=[];self.global_orders=[]
        self.margin='CROSSED';self.leverage=75;self.cap=1000000;self.max_leverage=125
        self.account=dict(status='BINANCE_CONNECTED',can_trade=True,position_mode='ONE_WAY',multi_assets_margin=False,usdt_available_balance='1000')
    def check(self):return self.account
    def sync_time(self):pass
    def signed_get(self,path,symbol=None):
        self.reads.append((path,symbol))
        if path=='/fapi/v3/account':return {'positions':self.global_positions}
        if symbol is None:return self.global_orders
        if path.endswith('/positionRisk'):return self.position
        if path.endswith('/openOrders'):return self.orders
        if path.endswith('/openAlgoOrders'):return self.algos
        if path.endswith('/symbolConfig'):return [dict(symbol=symbol,marginType=self.margin,leverage=self.leverage,maxNotionalValue=str(self.cap))]
        if path.endswith('/leverageBracket'):return [dict(symbol=symbol,brackets=[dict(initialLeverage=self.max_leverage,notionalFloor=0,notionalCap=self.cap)])]
        raise AssertionError('Endpoint not allowed')

class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.ledger=Ledger(self.root/'ledger.sqlite3');self.addCleanup(self.ledger.db.close)
        NeuroAPI(self.ledger,key='') # schema only, no network/key
        self.market=FakeMarket();self.client=ReadAccount()
    def seed(self,symbol='BTCUSDT',side='LONG',kind='trade'):
        value={**GOOD,'symbol':symbol,'side':side,'position_size':D('.002')}
        if side=='SHORT':value.update(take_profit=94,stop_loss=103)
        plan=risk_check(Signal(symbol,side,D(value['limit_entry']),D(value['take_profit']),D(value['stop_loss']),D('.002')),RULES())
        op=day()+(':analysis-v6:' if kind=='check' else ':analysis:')+symbol
        self.ledger.db.execute('INSERT INTO api_requests(operation,state,output) VALUES(?,?,?)',(op,'COMPLETE',canonical_output(value,SETUP_SCHEMA)))
        stamp(self.ledger.db,plan,op)
        if kind=='trade':self.ledger.reserve(plan,'NEUROAPI_DRY_RUN',RULES())
        else:
            self.ledger.db.execute('CREATE TABLE IF NOT EXISTS analysis_checks(operation TEXT PRIMARY KEY,day TEXT,state TEXT,result TEXT)')
            result=dict(status='ACCEPT',symbol=symbol,side=side,entry=plan['entry'],TP=plan['tp'],SL=plan['sl'],execution_quantity=plan['execution_quantity'],calculated_risk=plan['risk'],actual_RR=plan['rr'],neurobro_position_size=plan['neurobro_position_size'],mode='DRY_RUN')
            stamp(self.ledger.db,result,op)
            self.ledger.db.execute('INSERT INTO analysis_checks VALUES(?,?,?,?)',(op,day(),'COMPLETE',json.dumps(result)))
        return plan
    def go(self):return shadow(self.ledger,self.client,self.market)
    def test_long_limit_gtc_exact_levels_and_risk_manager_quantity(self):
        plan=self.seed();out=self.go();p=out['plans'][0];entry=p['entry_order']['payload']
        self.assertEqual(out['status'],'SHADOW_PREFLIGHT_OK')
        self.assertEqual((entry['side'],entry['type'],entry['timeInForce'],entry['positionSide']),('BUY','LIMIT','GTC','BOTH'))
        self.assertEqual(entry['quantity'],plan['execution_quantity']);self.assertNotEqual(entry['quantity'],plan['neurobro_position_size'])
        self.assertEqual((p['entry'],p['TP'],p['SL']),(plan['entry'],plan['tp'],plan['sl']))
        self.assertEqual((p['margin_target'],p['leverage_target']),('CROSS',75));self.assertLessEqual(D(p['calculated_risk']),5);self.assertGreaterEqual(D(p['actual_RR']),2)
        self.assertFalse(out['would_submit']);self.assertFalse(out['live_execution'])
    def test_short_entry_sell_protection_buy(self):
        self.seed(side='SHORT');p=self.go()['plans'][0]
        self.assertEqual(p['entry_order']['payload']['side'],'SELL');self.assertEqual(p['protective_side'],'BUY')
    def test_algo_close_all_no_quantity_reduceonly_or_legacy_stopprice(self):
        self.seed();p=self.go()['plans'][0]
        self.assertEqual(p['entry_order']['intended_path'],'/fapi/v1/order')
        for key,kind,price in [('take_profit_order','TAKE_PROFIT_MARKET',p['TP']),('stop_loss_order','STOP_MARKET',p['SL'])]:
            leg=p[key];v=leg['payload']
            self.assertEqual(leg['intended_path'],'/fapi/v1/algoOrder');self.assertEqual(leg['activation'],'AFTER_CONFIRMED_ENTRY_FILL')
            self.assertEqual(v['algoType'],'CONDITIONAL');self.assertEqual(v['type'],kind);self.assertEqual(v['triggerPrice'],price)
            self.assertEqual(v['side'],'SELL');self.assertEqual(v['symbol'],p['symbol']);self.assertEqual(v['positionSide'],'BOTH');self.assertEqual(v['closePosition'],'true')
            for forbidden in ('quantity','reduceOnly','stopPrice'):self.assertNotIn(forbidden,v)
    def test_hold_no_plan_or_slot(self):
        self.assertIsNone(plan_payload({'symbol':'BTCUSDT','side':'HOLD'},day()))
        with self.assertRaisesRegex(ShadowError,'NO_PERSISTED_SETUP'):self.go()
        self.assertEqual(self.ledger.count(),0)
    def test_account_mismatch_fails_closed(self):
        self.seed();self.client.account['position_mode']='HEDGE'
        with self.assertRaisesRegex(ShadowError,'BINANCE_POSITION_MODE_MISMATCH'):self.go()
    def test_cantrade_false_fails_closed(self):
        self.seed();self.client.account['can_trade']=False
        with self.assertRaisesRegex(ShadowError,'BINANCE_TRADE_DISABLED'):self.go()
    def test_multiasset_fail_closed(self):
        self.seed();self.client.account['multi_assets_margin']=True
        with self.assertRaisesRegex(ShadowError,'BINANCE_MULTI_ASSET_UNSUPPORTED'):self.go()
    def test_required_mutations_are_only_inert_plan_not_shadow_ok(self):
        self.seed();self.client.margin='ISOLATED';self.client.leverage=20
        out=self.go();p=out['plans'][0]
        self.assertEqual(p['required_account_mutations'],['SET_MARGIN_TYPE_CROSS','SET_LEVERAGE_75'])
        self.assertEqual(p['status'],'SHADOW_PLAN_READY');self.assertEqual(out['status'],'NEEDS_REVIEW');self.assertEqual(self.client.mutations,[])
    def test_current_rules_cannot_resize_saved_quantity(self):
        self.seed();self.market.rules=lambda _:replace(RULES(),step=D('1'))
        p=self.go()['plans'][0];self.assertEqual(p['status'],'NEEDS_REVIEW');self.assertEqual(p['failure_code'],'SHADOW_RULES_REJECTED');self.assertEqual(D(p['execution_quantity']),D('2.5'))
    def test_stale_rules_rejected(self):
        self.seed();self.market.rules=lambda _:replace(RULES(),observed_at=time.time()-301)
        self.assertEqual(self.go()['plans'][0]['failure_code'],'SHADOW_RULES_REJECTED')
    def test_inactive_symbol_rejected(self):
        self.seed();self.market.catalog=lambda:{}
        self.assertEqual(self.go()['plans'][0]['failure_code'],'SHADOW_SYMBOL_INACTIVE')
    def test_provider_levels_tampered_rejected(self):
        p=self.seed();p['tp']='106';stamp(self.ledger.db,p,day()+':analysis:BTCUSDT');self.ledger.db.execute('UPDATE trades SET plan=?',(json.dumps(p),))
        with self.assertRaisesRegex(ShadowError,'SHADOW_SETUP_CHANGED'):self.go()
    def test_oversized_quantity_and_low_rr_fail_closed(self):
        p=self.seed();p.update(quantity='3',execution_quantity='3',risk='6');stamp(self.ledger.db,p,day()+':analysis:BTCUSDT');self.ledger.db.execute('UPDATE trades SET plan=?',(json.dumps(p),))
        self.assertEqual(self.go()['plans'][0]['failure_code'],'SHADOW_RULES_REJECTED')
    def test_actual_rr_mismatch_cannot_fake_shadow_ok(self):
        p=self.seed();p['rr']='1.9';stamp(self.ledger.db,p,day()+':analysis:BTCUSDT');self.ledger.db.execute('UPDATE trades SET plan=?',(json.dumps(p),))
        with self.assertRaisesRegex(ShadowError,'SHADOW_SETUP_CHANGED'):self.go()
    def test_existing_position_blocks_closeall_plan(self):
        self.seed();self.client.position=[dict(symbol='BTCUSDT',positionSide='BOTH',positionAmt='0.01')]
        self.assertEqual(self.go()['plans'][0]['failure_code'],'SHADOW_EXISTING_EXPOSURE')
    def test_existing_regular_or_algo_orders_block(self):
        self.seed()
        for attr in ('orders','algos'):
            setattr(self.client,attr,[dict(symbol='BTCUSDT',clientOrderId='PRIVATE_ORDER_ID')])
            out=self.go();self.assertEqual(out['plans'][0]['failure_code'],'SHADOW_EXISTING_ORDERS');self.assertNotIn('PRIVATE_ORDER_ID',json.dumps(out));setattr(self.client,attr,[])
    def test_bad_account_response_fail_closed(self):
        self.seed();self.client.position=[dict(symbol='BTCUSDT',positionAmt='0')]
        self.assertEqual(self.go()['plans'][0]['failure_code'],'SHADOW_ACCOUNT_AMBIGUOUS')
    def test_leverage_not_supported_no_mutation(self):
        self.seed();self.client.max_leverage=50
        self.assertEqual(self.go()['plans'][0]['failure_code'],'SHADOW_LEVERAGE_UNSUPPORTED')
    def test_insufficient_margin_rejected(self):
        self.seed();self.client.account['usdt_available_balance']='0.01'
        self.assertEqual(self.go()['plans'][0]['failure_code'],'SHADOW_MARGIN_INSUFFICIENT')
    def test_price_already_beyond_tp_sl_rejected(self):
        self.seed();self.market.mark=lambda s:dict(price='105')
        self.assertEqual(self.go()['plans'][0]['failure_code'],'SHADOW_LEVELS_ALREADY_CROSSED')
    def test_same_ids_after_restart_and_different_legs_day_setup(self):
        p=self.seed();out=self.go();client_ids=out['plans'][0]['client_ids']
        self.assertEqual(len(set(client_ids.values())),3)
        for value in client_ids.values():self.assertRegex(value,r'^[.A-Z:/a-z0-9_-]{1,36}$')
        db=Ledger(self.root/'ledger.sqlite3')
        try:self.assertEqual(shadow(db,self.client,self.market)['plans'][0]['client_ids'],client_ids)
        finally:db.db.close()
        self.assertNotEqual(ids(identity(p,'2026-10-06')),client_ids)
        self.assertNotEqual(identity({**p,'entry':'101'},day()),identity(p,day()))
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM shadow_plans').fetchone()[0],1)
    def test_duplicate_and_daily_limit_fail_closed(self):
        self.seed();self.seed(kind='check')
        with self.assertRaisesRegex(ShadowError,'SHADOW_DUPLICATE_SETUP'):self.go()
    def test_daily_limit_counts_existing_closed_slots(self):
        self.seed();self.seed(symbol='ETHUSDT');self.ledger.db.execute("UPDATE trades SET state='CLOSED'");self.seed(kind='check')
        with self.assertRaisesRegex(ShadowError,'SHADOW_DAILY_LIMIT'):self.go()
    def test_check_records_no_reserve_and_no_paid_call(self):
        self.seed(kind='check');self.seed(symbol='ETHUSDT',kind='check')
        with patch('worker.neuroapi.NeuroAPI.ask',side_effect=AssertionError('paid call')):out=self.go()
        self.assertEqual(out['status'],'SHADOW_PREFLIGHT_OK');self.assertEqual(len(out['plans']),2);self.assertEqual(self.ledger.count(),0)
    def test_current_analysis_without_execution_quantity_is_not_invented(self):
        self.seed(kind='check');row=self.ledger.db.execute('SELECT result FROM analysis_checks').fetchone();p=json.loads(row[0]);p.pop('execution_quantity');self.ledger.db.execute('UPDATE analysis_checks SET result=?',(json.dumps(p),))
        with self.assertRaisesRegex(ShadowError,'SHADOW_SOURCE_UNVERIFIED'):self.go()
    def test_no_credential_raw_data_in_ledger_or_output(self):
        self.seed();self.client.account['raw']='PRIVATE_SECRET_SIGNATURE';out=self.go()
        self.assertNotIn('PRIVATE_SECRET_SIGNATURE',json.dumps(out));self.assertNotIn('PRIVATE_SECRET_SIGNATURE','\n'.join(self.ledger.db.iterdump()))
    def test_uncertain_model_persisted_blocks_second_setup_after_restart(self):
        self.seed();out=self.go();model=ExecutionModel().step('ENTRY_UNCERTAIN')
        self.ledger.db.execute('UPDATE shadow_plans SET model=?',(json.dumps(model.data()),))
        db=Ledger(self.root/'ledger.sqlite3')
        try:
            with self.assertRaisesRegex(ShadowError,'SHADOW_PROTECTION_INCOMPLETE'):shadow(db,self.client,self.market)
        finally:db.db.close()
    def test_no_live_submitted_state_in_shadow_output(self):
        self.seed();out=self.go();self.assertEqual(out['plans'][0]['status'],'SHADOW_PREFLIGHT_OK')
        self.assertEqual(self.ledger.db.execute('SELECT state FROM trades').fetchone()[0],'ORDER_READY')
        self.assertEqual(json.loads(self.ledger.db.execute('SELECT model FROM shadow_plans').fetchone()[0])['state'],'PLAN_READY')
    def test_cli_only_shadow_no_neuroapi_or_workflow(self):
        from worker.__main__ import main
        out=io.StringIO()
        result=dict(status='SHADOW_PREFLIGHT_OK',live_execution=False,would_submit=False)
        with patch('sys.argv',['worker','binance-shadow']),patch('worker.binance_shadow.run',return_value=result),patch('worker.__main__.NeuroAPI',side_effect=AssertionError('API')),contextlib.redirect_stdout(out):self.assertEqual(main(),0)
        self.assertEqual(json.loads(out.getvalue()),result)

    def test_global_other_symbol_exposure_and_orders_block(self):
        self.seed();self.client.global_positions=[dict(symbol='SOLUSDT',positionSide='BOTH',positionAmt='1')]
        with self.assertRaisesRegex(ShadowError,'SHADOW_EXISTING_EXPOSURE'):self.go()
        self.client.global_positions=[];self.client.global_orders=[dict(symbol='SOLUSDT')]
        with self.assertRaisesRegex(ShadowError,'SHADOW_EXISTING_ORDERS'):self.go()
    def test_persisted_incomplete_protection_blocks_next(self):
        self.seed();self.go()
        model=ExecutionModel().step('ENTRY_ACK').step('CONFIRMED_FULL_FILL').step('PROTECTION_FAILED')
        self.ledger.db.execute('UPDATE shadow_plans SET model=?',(json.dumps(model.data()),))
        with self.assertRaisesRegex(ShadowError,'SHADOW_PROTECTION_INCOMPLETE'):self.go()
    def test_changed_plan_cannot_reuse_day_symbol_identity(self):
        p=self.seed();self.go();value=plan_payload({**p,'tp':'106','rr':'3'},day())
        with self.assertRaisesRegex(ShadowError,'SHADOW_IDENTITY_CONFLICT'):ShadowStore(self.ledger.db).save(value)
    def test_concurrent_ledger_change_invalidates_preflight(self):
        self.seed();old=self.market.mark
        def mark(symbol):
            self.ledger.db.execute("UPDATE trades SET state='CLOSED'")
            return old(symbol)
        self.market.mark=mark
        with self.assertRaises(ShadowError):self.go()
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM shadow_plans').fetchone()[0],0)
    def test_historical_setup_not_replayed(self):
        self.seed();self.ledger.db.execute("UPDATE trades SET day='2000-01-01'")
        with self.assertRaisesRegex(ShadowError,'NO_PERSISTED_SETUP'):self.go()

    def test_client_id_collision_is_rejected(self):
        p=self.seed();out=self.go();other=plan_payload({**p,'symbol':'ETHUSDT'},day())
        other['client_ids']=out['plans'][0]['client_ids']
        with self.assertRaisesRegex(ShadowError,'SHADOW_IDENTITY_CONFLICT'):ShadowStore(self.ledger.db).save(other)
    def test_reconciliation_contract_uses_same_ids_without_call(self):
        self.seed();p=self.go()['plans'][0];r=p['future_reconciliation']
        self.assertEqual(r['entry']['lookup']['origClientOrderId'],p['client_ids']['ENTRY'])
        self.assertEqual(r['take_profit']['lookup']['clientAlgoId'],p['client_ids']['TP'])
        self.assertFalse(r['implemented'])
        self.assertTrue(all(path not in ('/fapi/v1/order','/fapi/v1/algoOrder') for path,_ in self.client.reads))

class ModelTests(unittest.TestCase):
    def test_fill_gates_protection_and_both_acknowledgments_required(self):
        m=ExecutionModel()
        for event in ('TP_CONFIRMED','SL_CONFIRMED','PROTECTION_ACK_PENDING','CONFIRMED_FULL_FILL'):
            with self.assertRaises(ModelError):m.step(event)
        m=m.step('ENTRY_ACK');self.assertTrue(m.blocks_next)
        m=m.step('CONFIRMED_FULL_FILL');self.assertEqual(m.state,'ENTRY_CONFIRMED')
        m=m.step('PROTECTION_ACK_PENDING');self.assertEqual(m.state,'PROTECTION_SUBMITTED')
        m=m.step('TP_CONFIRMED');self.assertEqual(m.state,'PROTECTION_INCOMPLETE');self.assertTrue(m.blocks_next)
        m=m.step('SL_CONFIRMED');self.assertEqual(m.state,'POSITION_PROTECTED');self.assertFalse(m.blocks_next)
    def test_uncertain_needs_reconcile_never_blind_retry(self):
        m=ExecutionModel().step('ENTRY_UNCERTAIN')
        for event in ('ENTRY_ACK','RETRY','RECONCILED_NOT_FOUND','CONFIRMED_FULL_FILL'):
            with self.assertRaises(ModelError):m.step(event)
        self.assertTrue(m.blocks_next)
        self.assertEqual(m.step('RECONCILED_FULL_FILL_SAME_ID').state,'ENTRY_CONFIRMED')
    def test_protection_failure_and_partial_fill_block_next(self):
        m=ExecutionModel().step('ENTRY_ACK').step('PARTIAL_FILL')
        self.assertTrue(m.blocks_next)
        with self.assertRaises(ModelError):m.step('TP_CONFIRMED')
        m=m.step('RECONCILED_FILL_REMAINDER_CLEARED').step('PROTECTION_FAILED')
        self.assertEqual(m.state,'PROTECTION_INCOMPLETE');self.assertTrue(m.blocks_next)
    def test_no_live_model(self):
        with self.assertRaises(ModelError):ExecutionModel(model_only=False).step('ENTRY_ACK')
