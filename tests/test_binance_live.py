"""Offline exchange simulator. No credential files or real network used."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from worker.core import Ledger,D,day
from worker.binance_live import Executor,LiveError,Store,run
from worker.binance_execution_transport import NotFound,valid_contract,BinanceExecution,wire
from worker.binance_shadow import plan_payload,shadow,ShadowError
from worker.live_arm import Disarmed,new_boot,arm,require_arm,write_private
from test_shadow_rr import BTC,ETH,expansion
import test_shadow_rr as rr_fixtures
from test_binance_shadow import ReadAccount
from worker.core import Rules

class Market:
    def catalog(self):return {'BTCUSDT':{},'ETHUSDT':{},'SOLUSDT':{}}
    def rules(self,symbol):return Rules(D('.001'),D('.001'),D('1000'),D('.1'),D('5'),time.time())
    def mark(self,symbol):return {'price':'85920' if symbol=='BTCUSDT' else '2709'}

class Exchange(ReadAccount):
    def __init__(self):
        super().__init__();self.normal={};self.algo={};self.amounts={'HYPEUSDT':'4.16'};self.configs={}
        self.authorize=lambda *a:False;self.events=[];self.fills='FILLED';self.timeout_entry=False;self.drop_entry=False;self.reject_entry=False
        self.fail_leg=None;self.manual_orders={};self.manual_algos={};self.checks=0;self.after_first_balance=None
    def check(self):
        self.checks+=1
        if self.after_first_balance is not None and self.normal:self.account['usdt_available_balance']=self.after_first_balance
        return self.account
    def signed_get(self,path,symbol=None):
        if path=='/fapi/v3/account':return {'positions':[dict(symbol=s,positionSide='BOTH',positionAmt=a) for s,a in self.amounts.items()]}
        if path.endswith('/positionRisk'):return [dict(symbol=symbol,positionSide='BOTH',positionAmt=self.amounts.get(symbol,'0'))]
        if path.endswith('/openOrders'):return [v for v in self.normal.values() if (symbol is None or v['symbol']==symbol) and v['status'] in ('NEW','PARTIALLY_FILLED')]+self.manual_orders.get(symbol,[])
        if path.endswith('/openAlgoOrders'):return [v for v in self.algo.values() if (symbol is None or v['symbol']==symbol) and v['algoStatus']=='NEW']+self.manual_algos.get(symbol,[])
        if path.endswith('/symbolConfig'):
            cfg=self.configs.get(symbol,{'marginType':self.margin,'leverage':self.leverage})
            return [dict(symbol=symbol,**cfg)]
        return super().signed_get(path,symbol)
    def scoped(self,method,path,p):
        valid_contract(method,path,p)
        if method=='GET':
            table=self.normal if path.endswith('/order') else self.algo
            key=p.get('origClientOrderId',p.get('clientAlgoId'))
            if key not in table:raise NotFound('LIVE_ORDER_NOT_FOUND')
            return copy.deepcopy(table[key])
        if not self.authorize(method,path,p):raise Disarmed('LIVE_EXECUTION_DISARMED')
        self.events.append((method,path,copy.deepcopy(p)))
        if path.endswith('/marginType'):
            self.configs.setdefault(p['symbol'],dict(marginType=self.margin,leverage=self.leverage))['marginType']='CROSSED';return {}
        if path.endswith('/leverage'):
            self.configs.setdefault(p['symbol'],dict(marginType=self.margin,leverage=self.leverage))['leverage']=75;return {}
        if path.endswith('/order'):
            if method=='DELETE':self.normal[p['origClientOrderId']]['status']='CANCELED';return {}
            if self.reject_entry:raise LiveError('LIVE_PROVIDER_REJECTED')
            if not self.drop_entry:
                qty=D(p['quantity']) if self.fills=='FILLED' else D(p['quantity'])/2 if self.fills=='PARTIALLY_FILLED' else D(0)
                self.normal[p['newClientOrderId']]={**p,'clientOrderId':p['newClientOrderId'],'origQty':p['quantity'],'executedQty':str(qty),'status':self.fills}
                self.amounts[p['symbol']]=str(qty if p['side']=='BUY' else -qty)
            if self.timeout_entry:raise LiveError('LIVE_REQUEST_UNCERTAIN')
            return {}
        if method=='DELETE':self.algo[p['clientAlgoId']]['algoStatus']='CANCELED';return {}
        if p['type']==self.fail_leg:raise LiveError('LIVE_REQUEST_UNCERTAIN')
        self.algo[p['clientAlgoId']]={**p,'orderType':p['type'],'algoStatus':'NEW','closePosition':True}
        return {}

class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ledger=Ledger(Path(self.tmp.name)/'ledger.sqlite3');self.addCleanup(self.ledger.db.close)
        # Reuse fixture creation only, never inherit/run its tests twice.
        from worker.neuroapi import NeuroAPI
        NeuroAPI(self.ledger,key='')
        self.ledger.db.execute('CREATE TABLE analysis_checks(operation TEXT PRIMARY KEY,day TEXT,state TEXT,result TEXT)')
        rr_fixtures.ExistingV6RRTests.seed_existing(self,BTC,expansion(19,7));rr_fixtures.ExistingV6RRTests.seed_existing(self,ETH,'3')
        self.client=Exchange();self.market=Market();self.armed=True
        def guard():
            if not self.armed:raise Disarmed('LIVE_EXECUTION_DISARMED')
            return True
        self.engine=Executor(self.ledger,self.client,self.market,guard,sleep=lambda _:None)
    def entries(self):return [p for m,path,p in self.client.events if m=='POST' and path=='/fapi/v1/order']
    def test_two_filled_protected_manual_hype_untouched(self):
        result=self.engine.tick();self.assertEqual(len(self.entries()),2)
        self.assertEqual({r['state'] for r in result['bot_positions']},{'POSITION_PROTECTED'})
        self.assertEqual(self.client.amounts['HYPEUSDT'],'4.16')
        for _,_,p in self.client.events:self.assertNotEqual(p.get('symbol'),'HYPEUSDT')
        self.assertEqual([p['quantity'] for p in self.entries()],['0.035','0.555'])
        self.assertEqual([p['price'] for p in self.entries()],['85920','2709'])
        for p in self.entries():self.assertEqual((p['side'],p['type'],p['timeInForce'],p['positionSide']),('BUY','LIMIT','GTC','BOTH'))
        self.assertEqual(self.ledger.db.execute('SELECT SUM(slot) FROM live_records').fetchone()[0],2)
    def test_disarmed_zero_mutations(self):
        self.armed=False
        with self.assertRaises(Disarmed):self.engine.tick()
        self.assertEqual(self.client.events,[])
    def test_command_disarmed_does_not_construct_client(self):
        with patch('worker.binance_live.require_arm',side_effect=Disarmed('LIVE_EXECUTION_DISARMED')),patch('worker.binance_live.BinanceExecution',side_effect=AssertionError('client')):
            self.assertEqual(run('/unused','binance-execute')['status'],'LIVE_EXECUTION_DISARMED')
    def test_candidate_manual_exposure_blocks(self):
        self.client.amounts['BTCUSDT']='1'
        with self.assertRaisesRegex(ShadowError,'SHADOW_EXISTING_EXPOSURE'):self.engine.tick()
        self.assertEqual(self.client.events,[])
    def test_candidate_manual_normal_order_blocks(self):
        self.client.manual_orders['BTCUSDT']=[dict(symbol='BTCUSDT',clientOrderId='manual')]
        with self.assertRaisesRegex(ShadowError,'SHADOW_EXISTING_ORDERS'):self.engine.tick()
        self.assertEqual(self.client.events,[])
    def test_candidate_manual_algo_order_blocks(self):
        self.client.manual_algos['BTCUSDT']=[dict(symbol='BTCUSDT',clientAlgoId='manual')]
        with self.assertRaisesRegex(ShadowError,'SHADOW_EXISTING_ORDERS'):self.engine.tick()
        self.assertEqual(self.client.events,[])
    def test_wallet_not_available_margin(self):
        self.client.account.update(usdt_wallet_balance='1000000',usdt_available_balance='1')
        with self.assertRaisesRegex(ShadowError,'SHADOW_MARGIN_INSUFFICIENT'):self.engine.tick()
        self.assertEqual(self.client.events,[])
    def test_reread_balance_after_first_protected_blocks_second(self):
        self.client.after_first_balance='0'
        with self.assertRaisesRegex(ShadowError,'SHADOW_MARGIN_INSUFFICIENT'):self.engine.tick()
        self.assertEqual(len(self.entries()),1)
        self.assertEqual(self.ledger.db.execute('SELECT state FROM live_records').fetchone()[0],'POSITION_PROTECTED')
    def test_cross_and_leverage_mutate_candidates_only_and_verified(self):
        self.client.margin='ISOLATED';self.client.leverage=20;self.engine.tick()
        for method,path,p in self.client.events:
            self.assertIn(p.get('symbol','BTCUSDT'),('BTCUSDT','ETHUSDT'))
            if path.endswith('/leverage'):self.assertEqual(p['leverage'],75)
            if path.endswith('/marginType'):self.assertEqual(p['marginType'],'CROSSED')
        self.assertEqual(set(self.client.configs),{'BTCUSDT','ETHUSDT'})
    def test_unsupported_75_bracket_blocks_all_mutation(self):
        self.client.max_leverage=50
        with self.assertRaisesRegex(ShadowError,'SHADOW_LEVERAGE_UNSUPPORTED'):self.engine.tick()
        self.assertEqual(self.client.events,[])
    def test_hedge_never_changed(self):
        self.client.account['position_mode']='HEDGE'
        with self.assertRaisesRegex(LiveError,'LIVE_ACCOUNT_UNSAFE'):self.engine.tick()
        self.assertEqual(self.client.events,[])
    def test_entry_timeout_after_acceptance_reconciles_no_retry(self):
        self.client.timeout_entry=True;self.engine.tick();before=len(self.client.events)
        self.engine.tick();self.assertEqual(len(self.client.events),before);self.assertEqual(len(self.entries()),2)
    def test_uncertain_missing_order_blocks_no_retry_or_second(self):
        self.client.timeout_entry=True;self.client.drop_entry=True
        self.assertEqual(self.engine.tick()['status'],'RECONCILIATION_REQUIRED')
        self.engine.tick();self.assertEqual(len(self.entries()),1)
    def test_restart_no_duplicate_entries(self):
        self.engine.tick();before=len(self.client.events)
        second=Executor(self.ledger,self.client,self.market,self.engine.armed,sleep=lambda _:None)
        second.tick();self.assertEqual(len(self.client.events),before)
    def test_ack_is_not_fill(self):
        self.client.fills='NEW';r=self.engine.tick();self.assertEqual(r['status'],'ENTRY_PENDING')
        self.assertEqual(len(self.entries()),1);self.assertEqual(self.client.algo,{})
    def test_partial_cancel_remainder_and_protect_before_second(self):
        self.client.fills='PARTIALLY_FILLED';self.engine.tick()
        events=self.client.events;first_next=next(i for i,(_,path,p) in enumerate(events) if path=='/fapi/v1/order' and p.get('symbol')=='ETHUSDT')
        prior=events[:first_next];self.assertTrue(any(m=='DELETE' and path=='/fapi/v1/order' for m,path,p in prior))
        self.assertEqual(sum(path=='/fapi/v1/algoOrder' for _,path,_ in prior),2)
    def test_partial_uncleared_blocks_next_but_protects(self):
        self.client.fills='PARTIALLY_FILLED';old=self.client.scoped
        def scoped(m,path,p):
            if m=='DELETE' and path.endswith('/order'):raise LiveError('LIVE_REQUEST_UNCERTAIN')
            return old(m,path,p)
        self.client.scoped=scoped
        self.assertEqual(self.engine.tick()['status'],'PROTECTION_INCOMPLETE');self.assertEqual(len(self.entries()),1);self.assertEqual(len(self.client.algo),2)
    def test_protection_failure_blocks_next_no_duplicate(self):
        self.client.fail_leg='TAKE_PROFIT_MARKET'
        self.assertEqual(self.engine.tick()['status'],'PROTECTION_INCOMPLETE');before=len(self.client.events)
        self.engine.tick();self.assertEqual(len(self.client.events),before);self.assertEqual(len(self.entries()),1)
    def test_algo_contract_exact_and_sl_first(self):
        self.engine.tick();legs=[p for m,path,p in self.client.events if path.endswith('/algoOrder')]
        self.assertEqual(legs[0]['type'],'STOP_MARKET')
        for p in legs:
            self.assertEqual((p['algoType'],p['workingType'],p['closePosition'],p['positionSide']),('CONDITIONAL','MARK_PRICE','true','BOTH'))
            self.assertNotIn('quantity',p);self.assertNotIn('reduceOnly',p)
        self.assertEqual([p['triggerPrice'] for p in legs],['85780','86300','2700','2736'])
    def test_sibling_cleanup_only_owned_after_finished_and_flat(self):
        self.engine.tick();self.client.amounts['BTCUSDT']='0'
        tp=next(v for v in self.client.algo.values() if v['symbol']=='BTCUSDT' and v['orderType']=='TAKE_PROFIT_MARKET');tp['algoStatus']='FINISHED'
        before=len(self.client.events);self.engine.tick();events=self.client.events[before:]
        self.assertEqual(len(events),1);self.assertEqual(events[0][0:2],('DELETE','/fapi/v1/algoOrder'))
        self.assertTrue(events[0][2]['clientAlgoId'].endswith('-s'));self.assertEqual(self.client.amounts['HYPEUSDT'],'4.16')
        self.assertEqual(self.ledger.db.execute("SELECT state FROM live_records WHERE symbol='BTCUSDT'").fetchone()[0],'CLOSED')
    def test_flat_without_owned_exit_evidence_cannot_cleanup(self):
        self.engine.tick();self.client.amounts['BTCUSDT']='0';before=len(self.client.events)
        with self.assertRaisesRegex(LiveError,'LIVE_RECONCILIATION_REQUIRED'):self.engine.tick()
        self.assertEqual(len(self.client.events),before)
    def test_manually_changed_same_symbol_amount_blocks_protection_changes(self):
        self.engine.tick();self.client.amounts['BTCUSDT']='10';before=len(self.client.events)
        with self.assertRaisesRegex(LiveError,'LIVE_OWNERSHIP_UNVERIFIED'):self.engine.tick()
        self.assertEqual(len(self.client.events),before)
    def test_rejected_provider_entry_does_not_consume_slot(self):
        self.client.reject_entry=True;self.engine.tick()
        self.assertEqual(self.ledger.db.execute('SELECT SUM(slot) FROM live_records').fetchone()[0],0)
        self.assertEqual(set(r[0] for r in self.ledger.db.execute('SELECT state FROM live_records')),{'REJECTED'})
    def test_persisted_intent_before_mutation_and_no_raw_payload_logs(self):
        old=self.client.scoped
        def scoped(m,path,p):
            if m!='GET':
                key,action=self.engine.active;a=self.engine.store.action(key,action)
                self.assertEqual(a['state'],'PENDING');self.assertTrue(self.engine.authorize(m,path,p))
            return old(m,path,p)
        self.client.scoped=scoped;self.engine.tick()
        dump='\n'.join(self.ledger.db.iterdump())
        for secret in ('X-MBX-APIKEY','signature=','PRIVATE_SECRET'):self.assertNotIn(secret,dump)
    def test_preexisting_matching_id_not_adopted_without_send_intent(self):
        from worker.binance_shadow import load_candidates
        v=plan_payload(load_candidates(self.ledger,day())[0],day());self.client.normal[v['client_ids']['ENTRY']]={}
        with self.assertRaisesRegex(LiveError,'LIVE_OWNERSHIP_UNVERIFIED'):self.engine.tick()
        self.assertEqual(self.client.events,[])
    def test_explicit_hype_candidate_denied(self):
        from worker.binance_shadow import load_candidates
        p=load_candidates(self.ledger,day())[0];p['symbol']='HYPEUSDT'
        with self.assertRaisesRegex(LiveError,'LIVE_SYMBOL_DENIED'):self.engine.start(p)
        self.assertEqual(self.client.events,[])
    def test_private_transport_no_guard_no_mutation(self):
        with patch('worker.binance_private.read_secret',return_value='synthetic-only'):
            c=BinanceExecution(live_transport=Mock(side_effect=AssertionError('wire')))
            with self.assertRaisesRegex(LiveError,'LIVE_SUBMISSION_DISABLED'):c.scoped('POST','/fapi/v1/leverage',{'symbol':'BTCUSDT','leverage':75})
    def test_no_bulk_closeall_transfer_or_market_entry(self):
        for method,path,p in [('DELETE','/fapi/v1/allOpenOrders',{'symbol':'BTCUSDT'}),('DELETE','/fapi/v1/algoOpenOrders',{'symbol':'BTCUSDT'}),('POST','/fapi/v1/positionSide/dual',{'dualSidePosition':'false'}),('POST','/fapi/v1/order',{'symbol':'BTCUSDT','type':'MARKET'})]:
            with self.assertRaises(LiveError):valid_contract(method,path,p)

    def test_short_sell_exact_levels_and_protection_buy(self):
        from worker.binance_shadow import load_candidates
        p=load_candidates(self.ledger,day())[0]
        p.update(side='SHORT',tp='85540',sl='86060')
        self.assertEqual(self.engine.start(p),'POSITION_PROTECTED')
        entry=self.entries()[0];self.assertEqual(entry['side'],'SELL')
        self.assertEqual(entry['quantity'],'0.035');self.assertEqual(entry['price'],'85920')
        legs=[p for m,path,p in self.client.events if path.endswith('/algoOrder')]
        self.assertTrue(all(p['side']=='BUY' for p in legs));self.assertEqual([p['triggerPrice'] for p in legs],['86060','85540'])
    def test_third_entry_slot_denied_persistently(self):
        self.engine.tick()
        from worker.binance_shadow import load_candidates
        p=load_candidates(self.ledger,day())[0];p['symbol']='SOLUSDT'
        key=self.engine.store.claim(p);v=plan_payload(p,day())
        with self.assertRaisesRegex(LiveError,'LIVE_DAILY_LIMIT'):
            Store(self.ledger.db).intent(key,'ENTRY','POST','/fapi/v1/order',v['entry_order']['payload'])
        self.assertEqual(len(self.entries()),2)
    def test_unauthorized_payload_cannot_use_persisted_capability(self):
        from worker.binance_shadow import load_candidates
        p=load_candidates(self.ledger,day())[0];key=self.engine.store.claim(p);v=plan_payload(p,day())
        payload=v['entry_order']['payload'];self.engine.store.intent(key,'ENTRY','POST','/fapi/v1/order',payload)
        self.engine.active=(key,'ENTRY')
        self.assertFalse(self.engine.authorize('POST','/fapi/v1/order',{**payload,'symbol':'HYPEUSDT'}))
        self.assertFalse(self.engine.authorize('POST','/fapi/v1/order',{**payload,'quantity':'100'}))
    def test_no_cancel_all_endpoints_in_live_runtime(self):
        source=Path('worker/binance_live.py').read_text()+Path('worker/binance_execution_transport.py').read_text()
        for path in ('/allOpenOrders','/algoOpenOrders','/batchOrders','/positionSide/dual','/transfer','/withdraw'):
            self.assertNotIn(path,source)
    def test_supervisor_dormant_does_not_construct_live_client(self):
        import threading
        from worker.live_supervisor import LiveSupervisor
        with patch('worker.live_supervisor.execution_requested',return_value=False),patch('worker.live_supervisor.BinanceExecution',side_effect=AssertionError('network')):
            self.assertEqual(LiveSupervisor('/unused',threading.Event()).tick()['status'],'LIVE_EXECUTION_DISARMED')
    def test_arm_expiry_between_reconciliation_and_mutation_blocks(self):
        from worker.binance_shadow import load_candidates
        p=load_candidates(self.ledger,day())[0];key=self.engine.store.claim(p)
        self.armed=False
        with self.assertRaises(Disarmed):self.engine.mutation(key,'MARGIN','POST','/fapi/v1/marginType',{'symbol':'BTCUSDT','marginType':'CROSSED'})
        self.assertEqual(self.client.events,[])

    def test_readonly_preflight_never_enters_mutation_transport(self):
        with patch('worker.binance_shadow.BinanceReadOnly',return_value=self.client),patch('worker.binance_shadow.Market',return_value=self.market),patch('worker.binance_live.directory',return_value=Path(self.tmp.name)),patch('worker.binance_shadow.directory',return_value=Path(self.tmp.name)):
            result=run(self.tmp.name,'binance-live-preflight')
        self.assertEqual(result['status'],'LIVE_PREFLIGHT_READY');self.assertFalse(result['would_submit']);self.assertFalse(result['live_execution']);self.assertEqual(self.client.events,[])
    def test_unverified_config_change_blocks_entry(self):
        self.client.leverage=20;old=self.client.scoped
        def scoped(m,path,p):
            if path.endswith('/leverage') and m=='POST':return {} # ACK alone proves nothing.
            return old(m,path,p)
        self.client.scoped=scoped
        with self.assertRaisesRegex(LiveError,'LIVE_CONFIG_UNCONFIRMED'):self.engine.tick()
        self.assertEqual(self.entries(),[]);self.assertEqual(self.ledger.db.execute('SELECT SUM(slot) FROM live_records').fetchone()[0],0)
    def test_already_margin_response_requires_get_evidence(self):
        self.client.margin='ISOLATED';old=self.client.scoped
        def scoped(m,path,p):
            value=old(m,path,p)
            if m=='POST' and path.endswith('/marginType'):raise LiveError('LIVE_MARGIN_ALREADY_SET')
            return value
        self.client.scoped=scoped;self.engine.tick();self.assertEqual(len(self.entries()),2)
    def test_missing_sibling_after_uncertain_creation_never_marked_closed(self):
        self.engine.tick();self.client.amounts['BTCUSDT']='0'
        for key,o in list(self.client.algo.items()):
            if o['symbol']=='BTCUSDT':
                if o['orderType']=='TAKE_PROFIT_MARKET':o['algoStatus']='FINISHED'
                else:del self.client.algo[key]
        with self.assertRaisesRegex(LiveError,'LIVE_RECONCILIATION_REQUIRED'):self.engine.tick()
        self.assertNotEqual(self.ledger.db.execute("SELECT state FROM live_records WHERE symbol='BTCUSDT'").fetchone()[0],'CLOSED')

class ArmTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        owner_patch=patch('worker.live_arm.os.fchown');owner_patch.start();self.addCleanup(owner_patch.stop)
        new_boot(self.root)
    def activate(self):
        # Forged legacy capability fixture must never activate this release.
        from worker.live_arm import read_private
        boot=read_private(self.root/'live-boot.json')
        write_private(self.root/'live-arm.json',dict(boot=boot['boot'],day=day(),enabled=True))
    def test_default_disarmed(self):
        with self.assertRaises(Disarmed):require_arm(self.root)
    def test_explicit_tty_arm_file_0600(self):
        self.activate()
        with self.assertRaises(Disarmed):require_arm(self.root)
        self.assertEqual((self.root/'live-arm.json').stat().st_mode&0o777,0o600)
    def test_restart_invalidates(self):
        self.activate();new_boot(self.root)
        with self.assertRaises(Disarmed):require_arm(self.root)
    def test_day_change_invalidates(self):
        self.activate()
        with patch('worker.live_arm.day',return_value='2099-01-01'),self.assertRaises(Disarmed):require_arm(self.root)
    def test_non_tty_cannot_arm(self):
        with patch('sys.stdin.isatty',return_value=False),self.assertRaises(Disarmed):arm(self.root)
    def test_wrong_confirmation_cannot_arm(self):
        with patch('sys.stdin.isatty',return_value=True),patch('builtins.input',return_value='yes'),self.assertRaises(Disarmed):arm(self.root)
    def test_world_readable_arm_rejected(self):
        self.activate();(self.root/'live-arm.json').chmod(0o644)
        with self.assertRaises(Disarmed):require_arm(self.root)
    def test_scheduler_default_off_no_neuroapi(self):
        with patch('worker.live_scheduler.enabled',return_value=False),patch('worker.neuroapi.NeuroAPI',side_effect=AssertionError('API')):
            self.assertEqual(run('/unused','binance-scheduler')['status'],'SCHEDULER_OFF')

    def test_execute_request_alone_cannot_arm_and_restart_revokes_request(self):
        from worker.live_arm import request_execution,execution_requested
        with self.assertRaises(Disarmed):request_execution(self.root)
        self.activate()
        with self.assertRaises(Disarmed):request_execution(self.root)
        self.assertFalse(execution_requested(self.root))
        new_boot(self.root);self.assertFalse(execution_requested(self.root))
    def test_symlink_arm_rejected(self):
        self.activate();p=self.root/'live-arm.json';target=self.root/'saved.json';p.rename(target);p.symlink_to(target)
        with self.assertRaises(Disarmed):require_arm(self.root)

    def test_correct_interactive_confirmation_cannot_arm(self):
        before=set(self.root.iterdir())
        with patch('sys.stdin.isatty',return_value=True),patch('builtins.input',return_value='ARM LIVE '+day()) as prompt,self.assertRaises(Disarmed):
            arm(self.root)
        prompt.assert_not_called();self.assertEqual(before,set(self.root.iterdir()))
    def test_scheduler_cannot_be_enabled_by_private_flag_or_environment(self):
        from worker.live_scheduler import run_scheduler
        with patch('worker.live_scheduler.enabled',return_value=True),patch.dict('os.environ',{'OFFICE_LIVE':'true','LIVE_ENABLED':'true'}),patch('worker.neuroapi.NeuroAPI',side_effect=AssertionError('research')):
            self.assertEqual(run_scheduler('/unused')['status'],'SCHEDULER_OFF')
