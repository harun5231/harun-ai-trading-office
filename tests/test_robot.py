"""All providers simulated; never load credentials or send real requests."""
import copy
import json
import tempfile
import threading
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
from test_neuroapi import GOOD,RULES,row

class RobotMarket(ExpandedMarket):
    def respond(self,method,url,headers=None,body=None,timeout=None):
        code,response_headers,value=super().respond(method,url,headers,body,timeout)
        if 'exchangeInfo' in url:value['symbols'].append(row('HYPEUSDT'))
        return code,response_headers,value

class Account:
    def __init__(self):self.positions=[];self.calls=[]
    def check(self):return dict(status='BINANCE_CONNECTED',position_mode='ONE_WAY',multi_assets_margin=False,can_trade=True,usdt_wallet_balance='117.25',usdt_available_balance='109.50')
    def sync_time(self):pass
    def signed_get(self,path):
        self.calls.append(('GET',path));return {'positions':copy.deepcopy(self.positions)}
    def position(self,symbol,amount):self.positions.append(dict(symbol=symbol,positionSide='BOTH',positionAmt=str(amount)))

class RobotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger.sqlite3';self.ledger=Ledger(self.path);self.addCleanup(lambda:self.ledger.db.close())
        self.calls=[];self.screens=[['BTCUSDT','ETHUSDT']];self.decisions={};self.account=Account();self.market=RobotMarket()
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
        r=self.ticks();self.assertEqual(self.calls,[]);self.assertEqual(self.account.calls,[])
        self.assertEqual(r['wait_reason'],'ROBOT_OFF');self.assertIsNone(r['failure_code'])
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
        self.assertEqual(r['wait_reason'],'ROBOT_CAPACITY_FULL');self.assertIsNone(r['failure_code'])
    def test_lifecycle_block_reports_reason_without_new_request(self):
        self.account.position('HYPEUSDT',1);self.on()
        with patch.object(self.robot,'lifecycle_blocked',return_value=True):r=self.robot.tick()
        self.assertEqual(r['bot_status'],'WAITING');self.assertEqual(r['available_slots'],1)
        self.assertEqual(r['wait_reason'],'ROBOT_LIFECYCLE_NEEDS_REVIEW');self.assertIsNone(r['failure_code'])
        self.assertEqual(self.calls,[])
    def test_daily_two_closed_entries_still_block(self):
        self.entries(2);self.on();r=self.ticks();self.assertEqual(self.calls,[]);self.assertEqual(r['available_slots'],0)
    def test_yesterday_carried_position_not_today_entry(self):
        self.entries(1,'2001-01-01');self.account.position('ETHUSDT',1);self.screens=[['BTCUSDT']];self.on();r=self.robot.tick()
        self.assertEqual(r['bot_entries_today'],0);self.assertEqual(r['available_slots'],1)
    def test_hype_manual_counted_never_owned(self):
        self.account.position('HYPEUSDT',1);self.screens=[['BTCUSDT']];self.on();r=self.ticks()
        self.assertEqual(r['running_positions'],1);self.assertEqual(r['manual_exposure'],['HYPEUSDT']);self.assertEqual(r['bot_entries_today'],0)
        self.assertEqual(r['usdt_wallet_balance'],'117.25');self.assertEqual(r['usdt_available_balance'],'109.50')
        self.assertEqual(len(r['setups']),1);self.assertTrue(all(m=='GET' for m,_ in self.account.calls))
    def test_zero_amount_non_both_row_does_not_hide_real_position(self):
        self.account.positions=[
            dict(symbol='BTCUSDT',positionSide='LONG',positionAmt='0'),
            dict(symbol='HYPEUSDT',positionSide='BOTH',positionAmt='4.16'),
        ]
        self.screens=[['ETHUSDT']];self.on();r=self.robot.tick()
        self.assertEqual(r['running_positions'],1);self.assertEqual(r['running_symbols'],['HYPEUSDT']);self.assertEqual(r['available_slots'],1)
    def test_account_report_preserves_robot_state(self):
        self.on()
        self.robot.store.report('REJECTED',reason='ROBOT_REQUEST_NEEDS_REVIEW')
        account=dict(running_positions=1,running_symbols=['HYPEUSDT'],manual_exposure=['HYPEUSDT'],bot_entries_today=0,available_slots=1,usdt_wallet_balance='117.25',usdt_available_balance='109.50')
        r=self.robot.store.report_account(account)
        self.assertEqual(r['bot_status'],'REJECTED')
        self.assertEqual(r['failure_code'],'ROBOT_REQUEST_NEEDS_REVIEW')
        self.assertEqual(r['running_positions'],1)
        self.assertEqual(r['manual_exposure'],['HYPEUSDT'])
        self.assertEqual(r['usdt_wallet_balance'],'117.25')
    def test_account_merge_cannot_overwrite_concurrent_screening_status(self):
        self.on();self.store.report('WAITING',wait_reason='SCREENING_COMPLETE_ANALYSIS_PENDING')
        read=threading.Event();writer_attempted=threading.Event();writer_finished=threading.Event();ready=threading.Event();errors=[]
        class Connection:
            def __init__(proxy,db,account_writer):proxy.db=db;proxy.account_writer=account_writer
            def __getattr__(proxy,name):return getattr(proxy.db,name)
            def execute(proxy,sql,*args):
                if not proxy.account_writer and sql.startswith('INSERT OR REPLACE INTO robot_status'):
                    writer_attempted.set()
                cursor=proxy.db.execute(sql,*args)
                if proxy.account_writer and sql=='SELECT data FROM robot_status WHERE id=1':
                    class Cursor:
                        def fetchone(cursor_proxy):
                            value=cursor.fetchone();read.set()
                            if not writer_attempted.wait(2):raise RuntimeError('writer did not reach status update')
                            # Without the merge transaction the other connection
                            # publishes SCREENING here and the stale merge erases it.
                            writer_finished.wait(.15)
                            return value
                    return Cursor()
                return cursor
        def publish():
            ledger=None
            try:
                ledger=Ledger(self.path);store=RobotStore(ledger.db);store.db=Connection(ledger.db,False);ready.set()
                if not read.wait(2):raise RuntimeError('account merge did not read status')
                store.report('SCREENING');writer_finished.set()
            except Exception as error:errors.append(error)
            finally:
                if ledger:ledger.db.close()
        def merge():
            ledger=None
            try:
                ledger=Ledger(self.path);store=RobotStore(ledger.db);store.db=Connection(ledger.db,True)
                store.report_account(dict(running_positions=1,available_slots=1,manual_exposure=['HYPEUSDT']))
            except Exception as error:errors.append(error)
            finally:
                if ledger:ledger.db.close()
        writer=threading.Thread(target=publish,daemon=True);writer.start();self.assertTrue(ready.wait(2))
        account_writer=threading.Thread(target=merge,daemon=True);account_writer.start()
        account_writer.join(3);writer.join(3)
        self.assertFalse(account_writer.is_alive());self.assertFalse(writer.is_alive());self.assertEqual(errors,[])
        self.assertEqual(self.store.snapshot()['bot_status'],'SCREENING')

    def test_hype_cannot_be_analyzed_even_if_returned(self):
        self.account.position('HYPEUSDT',1);self.screens=[['HYPEUSDT'],['BTCUSDT']];self.on();r=self.ticks()
        screens=[b for b in self.calls if b['output_schema']!=SETUP_SCHEMA]
        self.assertEqual([b['prompt'] for b in screens],[SCREENING_ONE,SCREENING_ONE])
        self.assertFalse(any(s['symbol']=='HYPEUSDT' for s in r['setups']))
        self.assertTrue(any(s['symbol']=='BTCUSDT' for s in r['setups']))
    def test_persisted_empty_screen_cycle_recovers(self):
        self.account.position('HYPEUSDT',1);self.screens=[['BTCUSDT']];self.on()
        cycle=day()+':robot-v7:0'
        data=dict(target=1,queue=[],seen=[],screen=0,replacements=0)
        self.ledger.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',(cycle,day(),0,'COMPLETE',json.dumps(data)))
        r=self.ticks(3)
        self.assertEqual(self.calls[0]['prompt'],SCREENING_ONE)
        self.assertTrue(any(s['symbol']=='BTCUSDT' for s in r['setups']))
    def test_persisted_active_empty_screen_cycle_recovers(self):
        self.account.position('HYPEUSDT',1);self.screens=[['BTCUSDT']];self.on()
        cycle=day()+':robot-v7:0';data=dict(target=1,queue=[],seen=[],screen=0,replacements=0)
        self.ledger.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',(cycle,day(),0,'ACTIVE',json.dumps(data)))
        r=self.ticks(3)
        self.assertEqual(self.calls[0]['prompt'],SCREENING_ONE)
        self.assertEqual(r['bot_status'],'SETUP_READY');self.assertEqual(len(r['setups']),1)
    def test_persisted_empty_screen_cycle_exhaustion_is_stable(self):
        self.account.position('HYPEUSDT',1);self.on();cycle=day()+':robot-v7:0'
        for state in ('ACTIVE','COMPLETE'):
            with self.subTest(state=state):
                data=dict(target=1,queue=[],seen=[],screen=3,replacements=3)
                self.ledger.db.execute('INSERT OR REPLACE INTO robot_cycles VALUES(?,?,?,?,?)',(cycle,day(),0,state,json.dumps(data)))
                r=self.ticks(4)
                self.assertEqual(r['bot_status'],'INSUFFICIENT_ACTIONABLE_SETUPS')
                self.assertEqual(r['wait_reason'],'ROBOT_CYCLE_COMPLETE');self.assertIsNone(r['failure_code'])
                self.assertEqual(self.calls,[])
                saved=self.ledger.db.execute('SELECT state,data FROM robot_cycles WHERE id=?',(cycle,)).fetchone()
                self.assertEqual(saved['state'],'COMPLETE');self.assertEqual(json.loads(saved['data'])['replacements'],3)
    def test_persisted_empty_cycle_with_pending_paid_job_is_not_recovered(self):
        self.account.position('HYPEUSDT',1);self.on();cycle=day()+':robot-v7:0'
        data=dict(target=1,queue=[],seen=[],screen=0,replacements=0)
        self.ledger.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',(cycle,day(),0,'ACTIVE',json.dumps(data)))
        self.ledger.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',('uncertain',cycle,'SCREENING',None,'PENDING',None))
        r=self.ticks(3)
        self.assertEqual(r['failure_code'],'ROBOT_REQUEST_NEEDS_REVIEW');self.assertEqual(self.calls,[])
        self.assertEqual(json.loads(self.ledger.db.execute('SELECT data FROM robot_cycles WHERE id=?',(cycle,)).fetchone()[0]),data)
    def test_partial_manual_only_exclusion_replaces_one_after_restart(self):
        self.screens=[['HYPEUSDT','BTCUSDT'],['ETHUSDT']];self.on();screen=self.robot.tick()
        self.assertEqual(screen['wait_reason'],'SCREENING_COMPLETE_ANALYSIS_PENDING');self.assertIsNone(screen['failure_code'])
        self.ledger.db.close();self.ledger=Ledger(self.path);self.make();r=self.ticks(4)
        screens=[b for b in self.calls if b['output_schema']!=SETUP_SCHEMA]
        self.assertEqual([b['prompt'] for b in screens],[SCREENING,SCREENING_ONE])
        self.assertEqual({s['symbol'] for s in r['setups']},{'BTCUSDT','ETHUSDT'})
        self.assertTrue(all(s['status']=='SETUP_READY' for s in r['setups']))
        self.assertEqual(r['wait_reason'],'ROBOT_CYCLE_COMPLETE')
        self.ledger.db.close();self.ledger=Ledger(self.path);self.make();self.ticks(5)
        self.assertEqual(len(self.calls),4);self.assertEqual(self.store.entries(day()),0)
    def test_one_manual_slot_screen_analysis_and_restart_without_replay(self):
        self.account.position('HYPEUSDT','4.16');self.screens=[['BTCUSDT']];self.on();screen=self.robot.tick()
        self.assertEqual(screen['available_slots'],1);self.assertEqual(screen['manual_exposure'],['HYPEUSDT'])
        self.assertEqual(screen['wait_reason'],'SCREENING_COMPLETE_ANALYSIS_PENDING');self.assertIsNone(screen['failure_code'])
        self.ledger.db.close();self.ledger=Ledger(self.path);self.make();r=self.ticks(3)
        self.assertEqual(r['bot_status'],'SETUP_READY');self.assertEqual(r['wait_reason'],'ROBOT_CYCLE_COMPLETE')
        self.assertEqual(len(r['setups']),1);self.assertEqual(r['setups'][0]['symbol'],'BTCUSDT')
        self.assertEqual(r['setups'][0]['risk_target_usdt'],'5');self.assertEqual(D(r['setups'][0]['risk']),D('5'))
        self.assertFalse(r['live_execution']);self.assertFalse(r['would_submit']);self.assertEqual(len(self.calls),2)
        self.ledger.db.close();self.ledger=Ledger(self.path);self.make();self.ticks(8)
        self.assertEqual(len(self.calls),2);self.assertTrue(all(method=='GET' for method,_ in self.account.calls))
    def test_shutdown_during_screen_preflight_claims_no_paid_operation(self):
        self.robot.stopping=threading.Event();catalog=self.market.catalog
        def stop_after_catalog():
            result=catalog();self.robot.stopping.set();return result
        self.market.catalog=stop_after_catalog;self.on();r=self.robot.tick()
        self.assertEqual(r['wait_reason'],'ROBOT_STOPPING');self.assertEqual(self.calls,[])
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_jobs').fetchone()[0],0)
    def test_shutdown_during_analysis_preflight_claims_no_paid_operation(self):
        self.on();self.robot.tick();self.robot.stopping=threading.Event()
        from worker.robot import analysis_context
        def stop_after_context(*args):
            result=analysis_context(*args);self.robot.stopping.set();return result
        with patch('worker.robot.analysis_context',side_effect=stop_after_context):r=self.robot.tick()
        self.assertEqual(r['wait_reason'],'ROBOT_STOPPING');self.assertEqual(len(self.calls),1)
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM robot_jobs WHERE kind='ANALYSIS'").fetchone()[0],0)

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
    def test_cycle_lock_permission_failure_is_reported_without_private_error(self):
        self.on()
        with patch('worker.robot.Path.open',side_effect=PermissionError('DO_NOT_PERSIST_PRIVATE /run/secrets/private-file')):
            r=self.robot.tick()
        self.assertEqual(r['bot_status'],'REJECTED');self.assertEqual(r['failure_code'],'ROBOT_CYCLE_LOCK_UNAVAILABLE')
        self.assertIsNotNone(r['checked_at']);self.assertEqual(self.calls,[]);self.assertEqual(self.account.calls,[])
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_jobs').fetchone()[0],0)
        self.assertNotIn('DO_NOT_PERSIST_PRIVATE','\n'.join(self.ledger.db.iterdump()))
        self.assertNotIn('/run/secrets/private-file',json.dumps(r))
    def test_cycle_lock_missing_parent_is_reported_without_paid_request(self):
        self.on();missing_path=self.path.parent/'missing-parent'/'ledger.sqlite3'
        with patch('worker.robot.Path',return_value=missing_path):r=self.robot.tick()
        self.assertEqual(r['bot_status'],'REJECTED');self.assertEqual(r['failure_code'],'ROBOT_CYCLE_LOCK_UNAVAILABLE')
        self.assertIsNotNone(r['checked_at']);self.assertEqual(self.calls,[]);self.assertEqual(self.account.calls,[])
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM robot_jobs').fetchone()[0],0)
        self.assertFalse(missing_path.parent.exists());self.assertNotIn(str(missing_path.parent),json.dumps(r))
    def test_cycle_lock_flock_failure_closes_handle_without_paid_request(self):
        self.on();lock=(self.path.parent/'cycle.lock').open('a')
        self.addCleanup(lambda:lock.close() if not lock.closed else None)
        with patch('worker.robot.Path.open',return_value=lock),patch('worker.robot.fcntl.flock',side_effect=PermissionError('DO_NOT_PERSIST_PRIVATE')):
            r=self.robot.tick()
        self.assertTrue(lock.closed);self.assertEqual(r['failure_code'],'ROBOT_CYCLE_LOCK_UNAVAILABLE')
        self.assertEqual(self.calls,[]);self.assertEqual(self.account.calls,[])
        self.assertNotIn('DO_NOT_PERSIST_PRIVATE','\n'.join(self.ledger.db.iterdump()))
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
