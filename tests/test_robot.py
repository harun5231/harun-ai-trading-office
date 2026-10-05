"""All providers simulated; never load credentials or send real requests."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from worker.core import D,Ledger,Review,Signal,day,risk_check,preflight,validated_risk_target
from worker.robot import Coordinator,RobotStore,slots,SCREENING_ONE,screening_contract
from worker.robot_provenance import verify,stamp,VERSION,SIZING
from worker.neuroapi import NeuroAPI,SCREEN_SCHEMA,SCREEN_ONE_SCHEMA,SETUP_SCHEMA,selections
from worker.prompts import SCREENING,ANALYSIS
from test_decisions import ExpandedMarket,hold
from test_neuroapi import GOOD,RULES

class Account:
    def __init__(self):self.positions=[];self.calls=[]
    def check(self):return dict(status='BINANCE_CONNECTED',position_mode='ONE_WAY',multi_assets_margin=False,can_trade=True)
    def sync_time(self):pass
    def signed_get(self,path):
        self.calls.append(('GET',path));return {'positions':copy.deepcopy(self.positions)}
    def position(self,symbol,amount):self.positions.append(dict(symbol=symbol,positionSide='BOTH',positionAmt=str(amount)))

class RobotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger.sqlite3';self.ledger=Ledger(self.path);self.addCleanup(lambda:self.ledger.db.close())
        self.calls=[];self.screens=[['BTCUSDT','ETHUSDT']];self.decisions={};self.account=Account();self.market=ExpandedMarket()
        def transport(method,url,headers,body,timeout):
            self.calls.append(copy.deepcopy(body));schema=body['output_schema']
            if schema in (SCREEN_SCHEMA,SCREEN_ONE_SCHEMA):value={'symbols':self.screens.pop(0)}
            else:
                context=json.loads(body['message_history'][0]['content']);symbol=context['symbol']
                self.assertEqual(body['prompt'],ANALYSIS);self.assertIn('contract_rules',context)
                choice=self.decisions.get(symbol,'LONG')
                if choice=='NETWORK':raise TimeoutError('DO_NOT_PERSIST_PRIVATE')
                value=hold(symbol) if choice=='HOLD' else {**GOOD,'symbol':symbol,'position_size':D('.002')}
                if choice=='SHORT':value.update(side='SHORT',stop_loss=102,take_profit=96)
                if choice=='REJECT':value['take_profit']=101
            return 200,{},dict(mode='smart',answer=None,output=value)
        self.transport=transport;self.make()
    def make(self):
        self.client=NeuroAPI(self.ledger,key='synthetic-robot-key',transport=self.transport)
        self.robot=Coordinator(self.ledger,self.client,self.market,lambda:self.account);self.store=self.robot.store
    def on(self):self.store.configure({'robot_on':True})
    def ticks(self,n=8):
        for _ in range(n):result=self.robot.tick()
        return result
    def ready(self):self.on();return self.ticks(3)
    def entries(self,n,today=None):
        for i in range(n):self.ledger.db.execute('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)',(str(i),'ETHUSDT',today or day(),'offline-fixture'))
    def test_off_zero_screen_and_analysis_and_account(self):
        self.ticks();self.assertEqual(self.calls,[]);self.assertEqual(self.account.calls,[])
    def test_off_ignores_legacy_environment_scheduler_flag(self):
        with patch.dict('os.environ',{'OFFICE_AUTO_DRY_RUN':'true'}):self.ticks()
        self.assertFalse(self.store.settings()['robot_on']);self.assertEqual(self.calls,[])
    def test_settings_persist_restart(self):
        self.store.configure({'robot_on':True,'risk_target_usdt':'10'});self.ledger.db.close();self.ledger=Ledger(self.path);self.make()
        self.assertEqual(self.store.settings(),dict(robot_on=True,risk_target_usdt='10'))
    def test_default_risk_five(self):self.assertEqual(self.store.settings()['risk_target_usdt'],'5')
    def test_risk_setting_rejects_invalid_values(self):
        for value in ('0','-1','NaN','Infinity','100.001',10,True,'1e1'):
            with self.subTest(value=value),self.assertRaises(Review):self.store.configure({'risk_target_usdt':value})
        self.assertEqual(self.store.settings()['risk_target_usdt'],'5')
    def test_slots_zero_running(self):self.assertEqual(slots(0,0),2)
    def test_slots_one_running(self):self.assertEqual(slots(1,0),1)
    def test_slots_two_running_zero_requests(self):
        self.account.position('BTCUSDT',1);self.account.position('HYPEUSDT',2);self.on();r=self.ticks()
        self.assertEqual(self.calls,[]);self.assertEqual(r['available_slots'],0)
    def test_daily_two_closed_entries_still_block(self):
        self.entries(2);self.on();r=self.ticks();self.assertEqual(self.calls,[]);self.assertEqual(r['available_slots'],0)
    def test_yesterday_carried_position_not_today_entry(self):
        self.entries(1,'2001-01-01');self.account.position('ETHUSDT',1);self.screens=[['BTCUSDT']];self.on();r=self.robot.tick()
        self.assertEqual(r['bot_entries_today'],0);self.assertEqual(r['available_slots'],1)
    def test_hype_manual_counted_never_owned(self):
        self.account.position('HYPEUSDT',1);self.screens=[['BTCUSDT']];self.on();r=self.ticks()
        self.assertEqual(r['running_positions'],1);self.assertEqual(r['manual_exposure'],['HYPEUSDT']);self.assertEqual(r['bot_entries_today'],0)
        self.assertEqual(len(r['setups']),1);self.assertTrue(all(m=='GET' for m,_ in self.account.calls))
    def test_hype_cannot_be_analyzed_even_if_returned(self):
        self.account.position('HYPEUSDT',1);self.screens=[['HYPEUSDT']];self.on();self.ticks()
        self.assertEqual(len(self.calls),1);self.assertEqual(self.store.snapshot()['setups'],[])
    def test_prompt_two_exact(self):
        self.ready();self.assertEqual(self.calls[0]['prompt'].encode(),SCREENING.encode())
    def test_prompt_one_exact(self):
        self.account.position('HYPEUSDT',1);self.screens=[['BTCUSDT']];self.on();self.ticks()
        self.assertEqual(self.calls[0]['prompt'],'pilihkan 1 coin yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance')
    def test_one_schema_and_validation(self):
        a=SCREEN_ONE_SCHEMA['properties']['symbols'];self.assertEqual((a['minItems'],a['maxItems']),(1,1));self.assertTrue(a['uniqueItems']);self.assertFalse(SCREEN_ONE_SCHEMA['additionalProperties'])
        self.assertEqual(selections({'symbols':['BTCUSDT']},{'BTCUSDT':{}},1),['BTCUSDT'])
        for value in (['BTCUSDT','ETHUSDT'],['BTC'],['btcusdt'],['FAKEUSDT']):
            with self.assertRaises(Review):selections({'symbols':value},{'BTCUSDT':{}},1)
    def test_two_schema_preserved(self):
        a=SCREEN_SCHEMA['properties']['symbols'];self.assertEqual((a['minItems'],a['maxItems']),(2,2));self.assertTrue(a['uniqueItems'])
    def test_catalog_before_screen_and_usdm_only(self):
        self.ready();self.assertTrue(all(m=='GET' and 'fapi.binance.com/fapi/' in u for m,u in self.market.calls))
    def test_hold_one_remaining_uses_one_prompt(self):
        self.decisions['ETHUSDT']='HOLD';self.screens.append(['SOLUSDT']);self.on();r=self.ticks()
        screens=[b for b in self.calls if b['output_schema']!=SETUP_SCHEMA]
        self.assertEqual([b['prompt'] for b in screens],[SCREENING,SCREENING_ONE])
        self.assertEqual(sum(s['status']=='SETUP_READY' for s in r['setups']),2);self.assertEqual(self.ledger.count(),0);self.assertEqual(r['bot_entries_today'],0)
    def test_hold_does_not_fabricate_levels(self):
        self.decisions['BTCUSDT']='HOLD';r=self.ready();row=next(s for s in r['setups'] if s['symbol']=='BTCUSDT')
        self.assertEqual(row['status'],'HOLD');self.assertNotIn('entry',row);self.assertNotIn('execution_quantity',row)
    def test_max_three_replacements_seen_not_reanalyzed(self):
        self.decisions={'BTCUSDT':'HOLD','ETHUSDT':'HOLD'};self.screens.extend([['BTCUSDT','ETHUSDT']]*3);self.on();r=self.ticks(20)
        self.assertEqual(len(self.calls),6);self.assertEqual(r['bot_status'],'INSUFFICIENT_ACTIONABLE_SETUPS')
        self.assertEqual(sum(b['output_schema']==SETUP_SCHEMA for b in self.calls),2)
    def test_technical_reject_no_replacement(self):
        self.decisions['ETHUSDT']='REJECT';self.on();r=self.ticks();self.assertEqual(len(self.calls),3)
        self.assertEqual(sum(s['status']=='REJECTED' for s in r['setups']),1)
    def test_unknown_network_blocks_no_retry_after_restart(self):
        self.decisions['BTCUSDT']='NETWORK';self.ready();before=len(self.calls);self.make();self.ticks()
        self.assertEqual(len(self.calls),before);self.assertEqual(self.store.snapshot()['bot_status'],'REJECTED')
    def test_pending_crash_claim_blocks_all_paid_requests(self):
        self.on();self.ledger.db.execute("INSERT INTO robot_jobs VALUES('crash','cycle','ANALYSIS','BTCUSDT','PENDING','5')");self.ticks()
        self.assertEqual(self.calls,[])
    def test_two_ready_stop_polling_spend(self):
        r=self.ready();self.assertEqual(len(self.calls),3);self.ticks(30);self.assertEqual(len(self.calls),3)
        self.assertEqual(sum(s['status']=='SETUP_READY' for s in r['setups']),2)
    def test_restart_completed_cycle_no_paid_replay(self):
        self.ready();self.ledger.db.close();self.ledger=Ledger(self.path);self.make();self.ticks();self.assertEqual(len(self.calls),3)
    def test_off_during_queue_stops_analysis(self):
        self.on();self.robot.tick();self.store.configure({'robot_on':False});self.ticks();self.assertEqual(len(self.calls),1)
        self.store.configure({'robot_on':True});self.ticks();self.assertEqual(len(self.calls),3)
    def test_off_during_request_prevents_next_request(self):
        original=self.client.transport
        def stop(*a,**k):
            result=original(*a,**k);self.store.configure({'robot_on':False});return result
        self.client.transport=stop;self.on();self.ticks();self.assertEqual(len(self.calls),1)
    def test_on_is_not_live_authorization(self):
        from worker.live_arm import require_arm,Disarmed
        from worker.live_scheduler import enabled
        self.on();self.assertFalse(enabled())
        with self.assertRaises(Disarmed):require_arm()
        r=self.store.snapshot();self.assertFalse(r['live_execution']);self.assertFalse(r['would_submit'])
    def test_approval_persistent_idempotent_no_entry(self):
        r=self.ready();item=r['setups'][0];self.store.approve(item['id'],'APPROVED');self.store.approve(item['id'],'APPROVED')
        self.make();self.ticks();self.assertEqual(len(self.calls),3);self.assertEqual(self.store.entries(day()),0)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_decisions').fetchone()[0],1)
    def test_conflicting_duplicate_approval_rejected(self):
        item=self.ready()['setups'][0];self.store.approve(item['id'],'APPROVED')
        with self.assertRaises(Review):self.store.approve(item['id'],'USER_REJECTED')
    def test_user_no_replacement_one_only_when_slot_remains(self):
        r=self.ready();self.store.approve(r['setups'][0]['id'],'USER_REJECTED');self.screens.append(['SOLUSDT']);self.ticks()
        self.assertEqual(self.calls[3]['prompt'],SCREENING_ONE);self.assertEqual(len(self.calls),5);self.assertEqual(self.store.entries(day()),0)
    def test_user_no_with_full_account_no_screen(self):
        r=self.ready();self.store.approve(r['setups'][0]['id'],'USER_REJECTED');self.account.position('HYPEUSDT',1);self.account.position('BNBUSDT',1);self.ticks();self.assertEqual(len(self.calls),3)
    def test_user_no_off_does_not_spend(self):
        r=self.ready();self.store.configure({'robot_on':False});self.store.approve(r['setups'][0]['id'],'USER_REJECTED');self.ticks();self.assertEqual(len(self.calls),3)
    def test_approval_is_immutable(self):
        r=self.ready();key=r['setups'][0]['id'];before=self.ledger.db.execute('SELECT plan FROM robot_setups WHERE id=?',(key,)).fetchone()[0]
        self.store.approve(key,'APPROVED');after=self.ledger.db.execute('SELECT plan FROM robot_setups WHERE id=?',(key,)).fetchone()[0];self.assertEqual(before,after)
    def test_v7_provenance_explicit_and_v6_unchanged(self):
        from worker.provenance import ANALYSIS_VERSION,SIZING_VERSION
        self.assertEqual(ANALYSIS_VERSION,'analysis-v6');self.assertEqual(SIZING_VERSION,'deterministic-max-risk-5-v1')
        self.ready();row=self.ledger.db.execute('SELECT * FROM robot_setups LIMIT 1').fetchone();plan=json.loads(row['plan'])
        self.assertEqual(plan['provenance']['analysis_version'],VERSION);self.assertEqual(plan['provenance']['execution_sizing_version'],SIZING)
        self.assertEqual(plan['risk_target_usdt'],'5');self.assertTrue(verify(self.ledger.db,plan,row['id']))
    def test_tampered_target_levels_or_quantity_fail_closed(self):
        self.ready();row=self.ledger.db.execute('SELECT * FROM robot_setups LIMIT 1').fetchone();plan=json.loads(row['plan'])
        for key,value in [('risk_target_usdt','10'),('entry','101'),('execution_quantity','0.002')]:
            altered=copy.deepcopy(plan);altered[key]=value
            with self.assertRaises(Review):verify(self.ledger.db,altered,row['id'])
    def test_risk_change_only_new_analysis(self):
        self.on();self.robot.tick();self.robot.tick();old=self.ledger.db.execute('SELECT plan FROM robot_setups').fetchone()[0]
        self.store.configure({'risk_target_usdt':'10'});self.robot.tick();plans=[json.loads(r[0]) for r in self.ledger.db.execute('SELECT plan FROM robot_setups ORDER BY rowid')]
        self.assertEqual(plans[0]['risk_target_usdt'],'5');self.assertEqual(plans[1]['risk_target_usdt'],'10')
        self.assertEqual(old,self.ledger.db.execute('SELECT plan FROM robot_setups ORDER BY rowid LIMIT 1').fetchone()[0])
        self.assertEqual(D(plans[1]['risk']),10)
    def test_rules_and_risk_in_context_before_call(self):
        self.store.configure({'robot_on':True,'risk_target_usdt':'10'});self.ticks()
        context=json.loads(self.calls[1]['message_history'][0]['content']);self.assertEqual(context['risk_constraints']['target_loss_at_sl_usdt'],'10')
        self.assertEqual(context['risk_constraints']['leverage_target'],75);self.assertEqual(context['risk_constraints']['margin_mode_target'],'CROSS')
        self.assertIn('stepSize',context['contract_rules'])
    def test_entries_and_legacy_ledger_untouched(self):
        self.ledger.db.execute("INSERT INTO trades VALUES('legacy','2001-01-01','LEGACY','CLOSED','{}',NULL,NULL,'time',NULL)")
        self.ready();self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM trades').fetchone()[0],1)
        self.assertEqual(self.store.entries(day()),0)
    def test_daily_boundary_does_not_replay_approved_setup(self):
        self.ready()
        with patch('worker.robot.day',return_value='2099-01-01'):self.ticks()
        self.assertEqual(len(self.calls),3)
    def test_account_invalid_no_neuroapi(self):
        self.account.positions=[{'positionAmt':'NaN'}];self.on();self.ticks();self.assertEqual(self.calls,[])
    def test_concurrent_actor_lock_no_neuroapi(self):
        import fcntl
        self.on()
        with (self.path.parent/'cycle.lock').open('a') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);r=self.robot.tick()
        self.assertEqual(r['failure_code'],'WORKER_BUSY');self.assertEqual(self.calls,[])
    def test_no_private_payload_logged(self):
        self.decisions['BTCUSDT']='NETWORK';self.ready()
        dump='\n'.join(self.ledger.db.iterdump())
        for secret in ('synthetic-robot-key','DO_NOT_PERSIST_PRIVATE',ANALYSIS,SCREENING,'message_history','X-API-Key'):self.assertNotIn(secret,dump)

class ConfigurableRiskTests(unittest.TestCase):
    def signal(self,side='LONG',distance='1.2'):
        d=D(distance);return Signal('BTCUSDT',side,D(100),D(100)+2*d if side=='LONG' else D(100)-2*d,D(100)-d if side=='LONG' else D(100)+d,D('.002'))
    def test_targets_maximum_no_upward_rounding(self):
        for target in ('5','10'):
            for side in ('LONG','SHORT'):
                sig=self.signal(side);rules=replace(RULES(),step=D('.01'));plan=risk_check(sig,rules,target);q=D(plan['quantity'])
                self.assertLessEqual(q*D('1.2'),D(target));self.assertGreater((q+rules.step)*D('1.2'),D(target))
                self.assertEqual(plan['neurobro_position_size'],'0.002');self.assertEqual(D(plan['entry']),sig.entry);self.assertEqual(D(plan['sl']),sig.sl);self.assertEqual(D(plan['tp']),sig.tp)
    def test_five_example_4_992(self):
        plan=risk_check(self.signal(),replace(RULES(),step=D('.01')),'5');self.assertEqual(D(plan['quantity']),D('4.16'));self.assertEqual(D(plan['risk']),D('4.992'))
    def test_far_below_and_above_rejected(self):
        rules=RULES();plan=risk_check(self.signal(),rules,'10')
        for qty in ('0.002','9'):
            bad={**plan,'quantity':qty,'execution_quantity':qty,'risk':str(D(qty)*D('1.2'))}
            with self.assertRaises(Review):preflight(bad,rules,'10')
    def test_maxqty_constraints(self):
        rules=replace(RULES(),maximum=D('1.001'));plan=risk_check(self.signal(),rules,'10');self.assertEqual(D(plan['quantity']),D('1.001'))
    def test_minqty_and_notional(self):
        for rules in (replace(RULES(),minimum=D('20')),replace(RULES(),min_notional=D('2000'))):
            with self.assertRaises(Review):risk_check(self.signal(),rules,'5')
    def test_rr_under_two_still_rejected(self):
        with self.assertRaises(Review):risk_check(replace(self.signal(),tp=D('100.1')),RULES(),'10')
    def test_float_not_accepted(self):
        with self.assertRaises(Review):validated_risk_target(5.0)
    def test_invalid_screen_slot_contract(self):
        for count in (0,3):
            with self.assertRaises(Review):screening_contract(count)
