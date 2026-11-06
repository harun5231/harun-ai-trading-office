"""Receipt-bound rejection uses isolated SQLite and never real provider/orders."""
import ast
import copy
import hashlib
import importlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from worker.core import D, Review
from worker.account_state import screening_contract, MANUAL_ONLY_SYMBOLS
from worker.neuroapi import selections
from worker.prompts import REPLACEMENT_SCREENING
from worker.research_guard import ResearchReadPaused

DAY='2026-10-31'
PROVIDER='NEUROBRO_WEB'
CONTRACT='neurobro-browser-decision-v2'
SOURCE=Path(__file__).resolve().parents[1]/'vps_native/worker/robot.py'

def encode(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def digest(text):return hashlib.sha256(text.encode()).hexdigest()
def parse(text):return json.loads(re.findall(r'```json\n(.*?)```',text,re.S)[0])

def coordinator_class():
    tree=ast.parse(SOURCE.read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef)and n.name=='Coordinator')
    scope=dict(__name__='worker.native_screening_fixture',__package__='worker',
        json=json,hashlib=hashlib,re=re,math=math,time=time,D=D,Review=Review,
        ResearchReadPaused=ResearchReadPaused,parse_assistant_text=parse,response_output_text=encode,
        BROWSER_PROVIDER=PROVIDER,BROWSER_CONTRACT=CONTRACT,
        screening_contract=screening_contract,REPLACEMENT_SCREENING=REPLACEMENT_SCREENING,
        MANUAL_ONLY_SYMBOLS=MANUAL_ONLY_SYMBOLS,selections=selections,day=lambda:DAY,
        account_state=lambda account,store,today:account,
        source_evidence=lambda db,operation:{'fixture':True})
    exec(compile(ast.Module(body=[cls],type_ignores=[]),str(SOURCE),'exec'),scope)
    return scope['Coordinator']


class NativeScreeningRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:',isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE robot_cycles(id TEXT PRIMARY KEY,day TEXT,entry_epoch INTEGER,state TEXT,data TEXT);
            CREATE TABLE robot_jobs(operation TEXT PRIMARY KEY,cycle TEXT,kind TEXT,symbol TEXT,state TEXT,risk_target TEXT);
            CREATE TABLE api_requests(operation TEXT PRIMARY KEY,idempotency TEXT,body_hash TEXT,state TEXT,created REAL,attempts INTEGER,output TEXT,failure_code TEXT);
            CREATE TABLE research_browser_requests(operation TEXT PRIMARY KEY,provider TEXT,contract_version TEXT,request_sha256 TEXT,marker TEXT,request_body TEXT);
            CREATE TABLE research_browser_responses(operation TEXT PRIMARY KEY,response_id TEXT,output_sha256 TEXT,assistant_text TEXT,assistant_sha256 TEXT,chart_evidence TEXT,received_at REAL,chart_max_age INTEGER);
            CREATE TABLE order_intents(id TEXT,candidate_id TEXT,symbol TEXT,state TEXT);
            CREATE TABLE robot_candidates(id TEXT,cycle TEXT,symbol TEXT,status TEXT,plan TEXT,failure_code TEXT);
        ''')
        self.cycle=DAY+':robot-v9:0';self.operation=self.cycle+':screening:0:2'
        self.on=True;self.catalog={'PENDLEUSDT':{},'BTCUSDT':{},'ETHUSDT':{}}
        self.catalog_calls=0;self.requests=[];self.next_symbols=['PENDLEUSDT','BTCUSDT']
        self.account=dict(running_positions=0,running_symbols=[],open_entry_symbols=[],available_slots=2)
        store=SimpleNamespace(settings=lambda:{'robot_on':self.on,'risk_target_usdt':'5'},
            report=lambda state,account=None,reason=None,wait_reason=None:dict(bot_status=state,failure_code=reason,wait_reason=wait_reason),
            report_account=lambda account:None,entries=lambda today:0,results=lambda cycle:[])
        robot=object.__new__(coordinator_class());robot.db=self.db;robot.store=store
        robot.stopping=None;robot.account_factory=lambda:self.account
        robot.allowed=lambda today:self.on and today==DAY
        robot.pause_reason=lambda:'ROBOT_OFF'if not self.on else'ROBOT_DAY_CHANGED'
        robot.execution_slots=lambda account,today:account['available_slots']
        robot.market=SimpleNamespace(catalog=self.get_catalog,max_age=180)
        robot.neuro=SimpleNamespace(require_key=lambda:None,ask=self.ask)
        self.robot=robot
        self.seed()

    def get_catalog(self):
        self.catalog_calls+=1
        return self.catalog

    def seed(self,symbols=None,day=DAY,count=2):
        symbols=symbols if symbols is not None else['PENDLEUSDT','MPLXUSDT']
        self.cycle=day+':robot-v9:0';self.operation=self.cycle+':screening:0:'+str(count)
        prompt,schema=screening_contract(count)
        marker='HARUN_'+digest(self.operation)[:32]
        body=dict(provider=PROVIDER,contract_version=CONTRACT,operation=self.operation,
            marker=marker,prompt=prompt,output_schema=schema,context=None)
        serialized=encode(body);reply=marker+'\n\nChosen coins.\n```json\n'+encode({'symbols':symbols})+'\n```'
        data=dict(target=count,queue=[],seen=[],screen=-1,replacements=0,round_symbols=[])
        created=time.time()-60
        self.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',(self.cycle,day,0,'NEEDS_REVIEW',json.dumps(data)))
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',(self.operation,self.cycle,'SCREENING',None,'NEEDS_REVIEW',None))
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',(self.operation,None,digest(serialized),'NEEDS_REVIEW',created,1,None,'INVALID_OUTPUT_SCHEMA'))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',(self.operation,PROVIDER,CONTRACT,digest(serialized),marker,serialized))
        self.db.execute('INSERT INTO research_browser_responses VALUES(?,?,?,?,?,?,?,?)',(self.operation,'dom-'+digest(reply),digest(encode({'symbols':symbols})),reply,digest(reply),'[]',created+30,180))

    def ask(self,operation,prompt,schema,validate,context=None,*,catalog=None,continue_if=None):
        self.assertTrue(continue_if())
        self.requests.append(dict(operation=operation,prompt=prompt,schema=schema,context=context))
        value={'symbols':list(self.next_symbols)};validate(value)
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',(operation,None,'a'*64,'COMPLETE',time.time(),1,json.dumps(value),None))
        return value

    def row(self,table,key='operation'):
        return dict(self.db.execute('SELECT * FROM '+table+' WHERE '+key+'=?',(self.cycle if key=='id'else self.operation,)).fetchone())

    def frozen(self):
        return {name:[tuple(r)for r in self.db.execute('SELECT * FROM '+name)]for name in
            ('robot_cycles','robot_jobs','api_requests','research_browser_requests','research_browser_responses','order_intents','robot_candidates')}

    def recover(self):return self.robot.recover_invalid_screening(DAY,self.account)

    def seed_native_choice(self):
        case=json.loads((SOURCE.parents[2]/'tests/fixtures/neurobro_pepe_invalid_native_choice_20261107.json').read_text())
        for table in ('robot_cycles','robot_jobs','api_requests','research_browser_requests','research_browser_responses','order_intents','robot_candidates'):
            self.db.execute('DELETE FROM '+table)
        native_day=case['operation'].split(':',1)[0]
        self.seed(symbols=case['symbols'],day=native_day,count=1)
        self.assertEqual(self.operation,case['operation'])
        self.assertEqual(self.row('research_browser_requests')['request_sha256'],case['request_sha256'])
        self.assertEqual(digest(case['reply']),case['reply_sha256'])
        request=self.row('research_browser_requests')
        self.assertEqual(case['response_id'],'dom-'+digest('https://app.neurobro.ai/\n'+request['marker']+'\n'+case['reply']))
        self.db.execute('UPDATE research_browser_responses SET response_id=?,assistant_text=?,assistant_sha256=? WHERE operation=?',
            (case['response_id'],case['reply'],case['reply_sha256'],self.operation))
        self.catalog={'1000PEPEUSDT':{},'BTCUSDT':{}}
        self.next_symbols=['BTCUSDT']
        self.account.update(running_positions=1,running_symbols=['LTCUSDT'],available_slots=1,carry_entries_today=1)
        self.carry_id='2026-11-06:robot-v9:0:analysis-v9:LTCUSDT'
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?)',(self.carry_id,self.carry_id,'LTCUSDT','POSITION_PROTECTED'))
        self.robot.allowed=lambda today:self.on and today==native_day
        self.robot.pause_reason=lambda:'ROBOT_OFF'if not self.on else'ROBOT_DAY_CHANGED'
        return case,native_day

    def recover_native_choice(self,native_day):
        return self.robot.recover_invalid_screening(native_day,self.account)

    def test_actual_native_pepe_reply_releases_one_slot_without_mapping_or_replaying(self):
        case,native_day=self.seed_native_choice()
        request=self.row('research_browser_requests');receipt=self.row('research_browser_responses')
        carry=tuple(self.db.execute('SELECT * FROM order_intents WHERE id=?',(self.carry_id,)).fetchone())
        settings=self.robot.store.settings();account=copy.deepcopy(self.account)
        result=self.recover_native_choice(native_day)
        self.assertEqual(result['wait_reason'],'SCREENING_INVALID_SYMBOL_REPLACEMENT_PENDING')
        self.assertEqual((self.row('api_requests')['state'],self.row('api_requests')['failure_code'],self.row('api_requests')['output']),
            ('REJECTED_RESPONSE_VALIDATION','INVALID_SCREENING_SYMBOL',None))
        self.assertEqual(self.row('robot_jobs')['state'],'REQUEST_REJECTED')
        data=json.loads(self.row('robot_cycles','id')['data'])
        self.assertEqual((data['seen'],data['replacement_due'],data['queue']),(['PEPEUSDT'],1,[]))
        self.assertNotIn('1000PEPEUSDT',data['seen'])
        self.assertEqual(self.row('research_browser_requests'),request)
        self.assertEqual(self.row('research_browser_responses'),receipt)
        self.assertEqual(tuple(self.db.execute('SELECT * FROM order_intents WHERE id=?',(self.carry_id,)).fetchone()),carry)
        self.assertEqual(self.robot.store.settings(),settings);self.assertEqual(self.account,account)
        self.assertEqual(self.requests,[])
        self.assertEqual(self.db.execute('SELECT count(*) FROM robot_candidates').fetchone()[0],0)

    def test_native_invalid_choice_survives_restart_and_original_tick_selects_new_one_coin(self):
        case,native_day=self.seed_native_choice();self.recover_native_choice(native_day)
        old_operation=self.operation
        successor=object.__new__(coordinator_class());successor.__dict__.update(self.robot.__dict__);self.robot=successor
        pending=importlib.import_module('agent.native_pending_review')
        with patch.object(pending,'review_then_advance',return_value=None),patch.dict(self.robot._tick.__globals__,{'day':lambda:native_day}):
            result=self.robot._tick()
        self.assertEqual(result['wait_reason'],'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(len(self.requests),1)
        request=self.requests[0]
        self.assertEqual(request['operation'],self.cycle+':screening:1:1')
        self.assertNotEqual(request['operation'],old_operation)
        self.assertEqual(request['prompt'],REPLACEMENT_SCREENING.format(count=1))
        self.assertEqual(request['context']['requested_count'],1)
        self.assertIn('PEPEUSDT',request['context']['excluded_symbols'])
        self.assertIn('LTCUSDT',request['context']['excluded_symbols'])
        self.assertEqual(json.loads(self.row('robot_cycles','id')['data'])['queue'],['BTCUSDT'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM order_intents').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT state FROM order_intents WHERE id=?',(self.carry_id,)).fetchone()[0],'POSITION_PROTECTED')

    def test_native_looking_json_without_internal_marker_is_not_a_natural_receipt(self):
        case,native_day=self.seed_native_choice()
        text='Chosen coins.\n```json\n'+encode({'symbols':['PEPEUSDT']})+'\n```'
        marker=self.row('research_browser_requests')['marker']
        self.db.execute('UPDATE research_browser_responses SET response_id=?,assistant_text=?,assistant_sha256=? WHERE operation=?',
            ('dom-'+digest('https://app.neurobro.ai/\n'+marker+'\n'+text),text,digest(text),self.operation))
        before=self.frozen()
        self.assertIsNone(self.recover_native_choice(native_day));self.assertEqual(self.frozen(),before)

    def test_native_dom_identity_and_all_original_hashes_must_match(self):
        mutations=(
            ('UPDATE research_browser_responses SET response_id=?',('dom-'+'0'*64,)),
            ('UPDATE research_browser_responses SET response_id=?',('other-native-receipt',)),
            ('UPDATE research_browser_responses SET assistant_sha256=?',('0'*64,)),
            ('UPDATE research_browser_responses SET output_sha256=?',('0'*64,)),
            ('UPDATE research_browser_requests SET request_sha256=?',('0'*64,)),
            ('UPDATE api_requests SET body_hash=?',('0'*64,)),
        )
        for sql,args in mutations:
            with self.subTest(sql=sql,args=args):
                _,native_day=self.seed_native_choice();self.db.execute(sql,args);before=self.frozen()
                self.assertIsNone(self.recover_native_choice(native_day));self.assertEqual(self.frozen(),before)

    def test_native_valid_catalog_choice_is_not_reclassified(self):
        _,native_day=self.seed_native_choice();self.catalog['PEPEUSDT']={};before=self.frozen()
        self.assertIsNone(self.recover_native_choice(native_day));self.assertEqual(self.frozen(),before)

    def test_native_invalid_choice_does_not_clear_other_unknown_or_repeat_recovery(self):
        _,native_day=self.seed_native_choice()
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            ('other-operation',None,'b'*64,'NEEDS_REVIEW',time.time(),1,None,'NETWORK_UNCERTAIN'))
        self.recover_native_choice(native_day);after=self.frozen()
        self.assertIsNone(self.recover_native_choice(native_day));self.assertEqual(self.frozen(),after)
        self.assertTrue(self.robot.unresolved_research());self.assertEqual(self.requests,[])

    def test_native_invalid_choice_off_or_different_day_preserves_evidence(self):
        _,native_day=self.seed_native_choice();self.on=False;before=self.frozen()
        self.assertEqual(self.recover_native_choice(native_day)['wait_reason'],'ROBOT_OFF');self.assertEqual(self.frozen(),before)
        self.on=True
        self.assertIsNone(self.recover_native_choice('2026-11-08'));self.assertEqual(self.frozen(),before)

    def test_bound_missing_symbol_is_rejected_without_accepting_partial_output(self):
        request=self.row('research_browser_requests');receipt=self.row('research_browser_responses')
        settings=self.robot.store.settings()
        result=self.recover()
        self.assertEqual(result['wait_reason'],'SCREENING_INVALID_SYMBOL_REPLACEMENT_PENDING')
        api=self.row('api_requests');job=self.row('robot_jobs');cycle=self.row('robot_cycles','id');data=json.loads(cycle['data'])
        self.assertEqual((api['state'],api['failure_code'],api['output']),('REJECTED_RESPONSE_VALIDATION','INVALID_SCREENING_SYMBOL',None))
        self.assertEqual(job['state'],'REQUEST_REJECTED')
        self.assertEqual((cycle['state'],data['screen'],data['seen'],data['replacement_due'],data['queue']),('ACTIVE',0,['MPLXUSDT'],2,[]))
        self.assertEqual(self.row('research_browser_requests'),request)
        self.assertEqual(self.row('research_browser_responses'),receipt)
        self.assertEqual(self.robot.store.settings(),settings)
        self.assertFalse(self.robot.unresolved_research())
        self.assertEqual(self.requests,[])
        self.assertEqual(self.db.execute('SELECT count(*) FROM robot_candidates').fetchone()[0],0)

    def test_normal_tick_after_restart_uses_new_replacement_operation_and_original_prompt(self):
        self.recover()
        # Recreating the Coordinator leaves all progress in SQLite, not memory.
        successor=object.__new__(coordinator_class());successor.__dict__.update(self.robot.__dict__);self.robot=successor
        pending=SimpleNamespace(review_then_advance=lambda owner,account,today:None)
        with patch.dict(sys.modules,{'agent.native_pending_review':pending}):result=self.robot._tick()
        self.assertEqual(result['wait_reason'],'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(len(self.requests),1)
        request=self.requests[0]
        self.assertEqual(request['operation'],self.cycle+':screening:1:2')
        self.assertEqual(request['prompt'],REPLACEMENT_SCREENING.format(count=2))
        self.assertIn('MPLXUSDT',request['context']['excluded_symbols'])
        self.assertEqual(request['context']['requested_count'],2)
        self.assertNotEqual(request['operation'],self.operation)
        data=json.loads(self.row('robot_cycles','id')['data'])
        self.assertNotIn('replacement_due',data)
        self.assertEqual(data['queue'],self.next_symbols)
        self.assertEqual(self.row('api_requests')['state'],'REJECTED_RESPONSE_VALIDATION')
        self.assertEqual(self.db.execute('SELECT count(*) FROM order_intents').fetchone()[0],0)

    def test_normal_replacement_uses_actual_one_slot_capacity(self):
        self.recover();self.account['available_slots']=1;self.next_symbols=['PENDLEUSDT']
        pending=SimpleNamespace(review_then_advance=lambda owner,account,today:None)
        with patch.dict(sys.modules,{'agent.native_pending_review':pending}):self.robot._tick()
        self.assertEqual(self.requests[0]['operation'],self.cycle+':screening:1:1')
        self.assertEqual(self.requests[0]['prompt'],REPLACEMENT_SCREENING.format(count=1))
        self.assertEqual(self.requests[0]['context']['requested_count'],1)

    def test_valid_coins_are_not_reclassified_or_queued_from_review(self):
        self.catalog['MPLXUSDT']={};before=self.frozen()
        self.assertIsNone(self.recover());self.assertEqual(self.frozen(),before)
        self.assertTrue(self.robot.unresolved_research())

    def test_unknown_or_other_failure_remains_blocking(self):
        for failure in('NETWORK_UNCERTAIN','INVALID_RESPONSE_ENVELOPE','NEUROBRO_CHART_EVIDENCE_INVALID'):
            with self.subTest(failure=failure):
                self.db.execute('UPDATE api_requests SET failure_code=?',(failure,));before=self.frozen()
                self.assertIsNone(self.recover());self.assertEqual(self.frozen(),before)
                self.assertTrue(self.robot.unresolved_research())
        self.assertEqual(self.requests,[])

    def test_tampered_request_response_or_marker_never_clears_review(self):
        cases=[('api_requests','body_hash','0'*64),('research_browser_requests','marker','HARUN_'+'0'*32),
            ('research_browser_responses','assistant_sha256','0'*64),('research_browser_responses','output_sha256','0'*64),
            ('research_browser_responses','response_id',''),('research_browser_responses','chart_evidence','[{}]')]
        for table,key,bad in cases:
            with self.subTest(table=table,key=key):
                old=self.row(table)[key];self.db.execute('UPDATE '+table+' SET '+key+'=?',(bad,));before=self.frozen()
                self.assertIsNone(self.recover());self.assertEqual(self.frozen(),before)
                self.db.execute('UPDATE '+table+' SET '+key+'=?',(old,))

    def test_duplicate_output_and_other_day_are_not_recovered(self):
        for table in('robot_cycles','robot_jobs','api_requests','research_browser_requests','research_browser_responses'):self.db.execute('DELETE FROM '+table)
        self.seed(symbols=['MPLXUSDT','MPLXUSDT']);before=self.frozen()
        self.assertIsNone(self.recover());self.assertEqual(self.frozen(),before)
        for table in('robot_cycles','robot_jobs','api_requests','research_browser_requests','research_browser_responses'):self.db.execute('DELETE FROM '+table)
        self.seed(day='2026-10-30');before=self.frozen()
        self.assertIsNone(self.recover());self.assertEqual(self.frozen(),before)

    def test_incomplete_request_and_off_state_do_not_mutate(self):
        self.db.execute("UPDATE api_requests SET state='PENDING'");before=self.frozen()
        self.assertIsNone(self.recover());self.assertEqual(self.frozen(),before)
        self.db.execute("UPDATE api_requests SET state='NEEDS_REVIEW'");self.on=False;before=self.frozen()
        self.assertEqual(self.recover()['wait_reason'],'ROBOT_OFF');self.assertEqual(self.frozen(),before)

    def test_stale_screen_index_does_not_reopen_an_old_operation(self):
        row=self.row('robot_cycles','id');data=json.loads(row['data']);data['screen']=0
        self.db.execute('UPDATE robot_cycles SET data=?',(json.dumps(data),));before=self.frozen()
        self.assertIsNone(self.recover());self.assertEqual(self.frozen(),before)
        self.assertTrue(self.robot.unresolved_research())

    def test_recovery_is_idempotent_and_preserves_genuine_unknown_blocker(self):
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',('other-operation',None,'b'*64,'NEEDS_REVIEW',time.time(),1,None,'NETWORK_UNCERTAIN'))
        self.recover();after=self.frozen()
        self.assertIsNone(self.recover());self.assertEqual(self.frozen(),after)
        self.assertTrue(self.robot.unresolved_research())
        pending=SimpleNamespace(review_then_advance=lambda owner,account,today:None)
        with patch.dict(sys.modules,{'agent.native_pending_review':pending}):result=self.robot._tick()
        self.assertEqual(result['failure_code'],'ROBOT_REQUEST_NEEDS_REVIEW')
        self.assertEqual(self.requests,[])

    def test_update_failure_rolls_back_entire_reclassification(self):
        self.db.execute("CREATE TRIGGER refuse_rejection BEFORE UPDATE ON api_requests BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        before=self.frozen()
        with self.assertRaises(sqlite3.IntegrityError):self.recover()
        self.assertEqual(self.frozen(),before)
        self.assertFalse(self.db.in_transaction)

    def test_due_replacements_do_not_double_count_existing_holds(self):
        self.robot.replaceable_result=lambda row:row['status']=='HOLD'
        data={'round_symbols':['BTCUSDT'],'replacement_due':2}
        self.assertEqual(self.robot.replacement_count(data,[{'symbol':'BTCUSDT','status':'HOLD'}]),2)

if __name__=='__main__':unittest.main()
