"""All-round received SCREENING recovery through the actual native recorder.

The fixtures are local journal/evidence records. Every outbound research and
order path raises, including recovery of a replacement from round nine.
"""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from worker.core import Review
from worker.account_state import screening_contract
from agent.native_screening_recovery import clarification
from worker import screening_response_recovery as recovery
from worker.analysis_response_recovery import wire, sha, digest
from tests.test_screening_response_recovery import native_module

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 5, 14, 8, tzinfo=timezone.utc).timestamp()
DAY = '2026-11-05'
CYCLE = DAY + ':robot-v9:0'
OP = CYCLE + ':screening:9:1'
KIND = 'NATIVE_RECEIVED_SCREENING_OBSERVATION_V1'
NATIVE_SOURCE = '30ea13cd51706b1db65620cdfb5f8db6dec1c9b3b8a20c9b7ca1d6c9db07f72e'


class GenericScreeningResponseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.old = {name: sys.modules.get(name) for name in
            ('worker.research_provider', 'worker.neurobrowser')}
        with patch('worker.owner_source_protocol.load_owner_release_cutoff',
                   return_value='2026-11-04T00:00:00Z'):
            self.source = native_module('worker.research_provider', ROOT / 'vps_native/worker/research_provider.py')
        self.native = native_module('worker.neurobrowser', ROOT / 'vps_native/worker/neurobrowser.py')
        self.addCleanup(self.restore)
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.source.ensure_journal(self.db)
        self.db.executescript('''
            CREATE TABLE robot_cycles(id TEXT PRIMARY KEY,day TEXT,entry_epoch INTEGER,state TEXT,data TEXT);
            CREATE TABLE robot_jobs(operation TEXT PRIMARY KEY,cycle TEXT,kind TEXT,symbol TEXT,state TEXT,risk_target TEXT);
            CREATE TABLE robot_settings(id INTEGER PRIMARY KEY,enabled INTEGER,risk TEXT);
            INSERT INTO robot_settings VALUES(1,1,'5');
            CREATE TABLE robot_candidates(id TEXT PRIMARY KEY,cycle TEXT,symbol TEXT,status TEXT,plan TEXT,failure_code TEXT);
            CREATE TABLE order_intents(id TEXT PRIMARY KEY,candidate_id TEXT,symbol TEXT,state TEXT,payload TEXT);
            CREATE TABLE pending_review(operation TEXT PRIMARY KEY,state TEXT,decision TEXT);
            INSERT INTO pending_review VALUES('accepted-inj-review','ORDER_TERMINAL_NOT_APPLIED','CLOSED');
        ''')
        self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
            (CYCLE + ':analysis-v9:INJUSDT', CYCLE, 'INJUSDT', 'CLOSED', 'accepted INJ plan', None))
        self.db.execute('INSERT INTO robot_candidates VALUES(?,?,?,?,?,?)',
            (CYCLE + ':analysis-v9:AVAXUSDT', CYCLE, 'AVAXUSDT', 'HOLD', None, None))
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?)',
            ('accepted-inj', CYCLE + ':analysis-v9:INJUSDT', 'INJUSDT', 'CLOSED', 'accepted immutable INJ order'))
        self.provider = object.__new__(self.native.NeuroBrowser)
        self.provider.db = self.db
        self.provider.chart_max_age = 180
        self.provider.transport = self.forbid
        self.provider.ask = self.forbid
        self.provider.require_key = self.forbid
        self.catalog = {'SUIUSDT': {}, 'AVAXUSDT': {}, 'INJUSDT': {}}
        self.context = dict(requested_count=1,
            excluded_symbols=['AVAXUSDT', 'BTCUSDT', 'DOGEUSDT', 'ETHUSDT', 'INJUSDT',
                'LINKUSDT', 'NEARUSDT', 'SOLUSDT', 'TIAUSDT', 'TONUSDT', 'TRXUSDT', 'WIFUSDT', 'XRPUSDT'],
            replacement_instruction='Choose different Binance USD-M USDT perpetual coins that are not in excluded_symbols.')
        _, schema = screening_contract(1)
        # Historical text is intentionally different from today's initial prompt.
        prompt = 'Instruksi pengganti historis — pilih satu coin baru sesuai konteks asli.'
        self.body, serialized, self.request_hash = self.source.request_body(OP, prompt, schema, self.context)
        self.payload = {**self.body, 'request_sha256': self.request_hash}
        self.created = NOW - 90
        self.data = dict(target=2, queue=[], seen=self.context['excluded_symbols'],
            screen=8, replacements=8, round_symbols=['AVAXUSDT'],
            risk_target='5', analysis_contexts={'existing': {'risk': '5'}}, unrelated={'keep': True})
        self.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',
            (CYCLE, DAY, 0, 'NEEDS_REVIEW', json.dumps(self.data)))
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
            (OP, CYCLE, 'SCREENING', None, 'NEEDS_REVIEW', None))
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            (OP, None, self.request_hash, 'NEEDS_REVIEW', self.created, 1, None, 'NEUROBRO_BROWSER_OUTCOME_UNKNOWN'))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',
            (OP, self.source.BROWSER_PROVIDER, self.source.BROWSER_CONTRACT,
             self.request_hash, self.body['marker'], serialized))
        self.envelope = self.fixture()

    def restore(self):
        for name, previous in self.old.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def forbid(self, *args, **kwargs):
        raise AssertionError('Recovery must not send provider/model/upload/order work')

    def row(self, table):
        key = 'id' if table == 'robot_cycles' else 'operation'
        value = CYCLE if table == 'robot_cycles' else OP
        row = self.db.execute('SELECT * FROM ' + table + ' WHERE ' + key + '=?', (value,)).fetchone()
        return dict(row) if row else None

    def finances(self):
        return {name: [tuple(row) for row in self.db.execute('SELECT * FROM ' + name)]
            for name in ('order_intents', 'pending_review', 'robot_candidates', 'robot_settings')}

    def seal(self, envelope):
        raw = wire(envelope['observation'])
        envelope.update(observation_sha256=sha(raw), observation_bytes=len(raw.encode()))
        return envelope

    def fixture(self, *, approval=True, reply='COIN: SUIUSDT', followup=False, operation=OP, symbols=None,
            action='SCREENING'):
        url = 'https://app.neurobro.ai/chat/' + 'a' * 32
        rendered = self.body['prompt'] + '\n\n' + self.body['marker']
        rows = [dict(role='user', text=rendered, attachments=[]),
            dict(role='assistant', text=reply, id='native-round9-first', complete=True, attachments=[])]
        if followup:
            rows += [dict(role='user', text=clarification(1), attachments=[]),
                dict(role='assistant', text=reply, id='native-round9-final', complete=True, attachments=[])]
        proof = dict(schema_version=2, kind=KIND, operation=operation,
            request_sha256=self.request_hash, payload_sha256=digest(self.payload),
            native_source_sha256=NATIVE_SOURCE, operation_completed=False,
            orders_sent=0, model_calls=0, provider_messages=0, uploads=0,
            observed_at=NOW-11, finished_at=NOW-10, status='CONNECTED',
            stage='OBSERVED_WITHOUT_COMPLETION', errors=[], rows=rows,
            room=dict(day=DAY, room_id='a'*32, url=url), page_url=url,
            room_witness=dict(native_room_matches=True, same_loaded_room_verified=True,
                initial_screening_user_rows=[0], followup_user_rows=list(range(2, len(rows), 2))),
            operation_scope=dict(verified=True, initial_user=0, latest_user=len(rows)-2,
                user_rows=list(range(0, len(rows), 2)), reply_row=len(rows)-1,
                reply_complete=True, unmatched_intents=[], reason=None),
            row_offset=18, stable_snapshot_reads=4,
            rendered_prompt_receipt=dict(operation=operation, payload_sha256=digest(self.payload),
                rendered_sha256=sha(rendered), rendered_prompt=rendered),
            receipt_files={}, file_tiles=[], prepared_materials=[], review_receipts=[])
        if approval:
            reply_sha = sha(rows[-1]['text'])
            transcript = digest(rows)
            request_id = digest(dict(operation=operation, request_sha256=self.request_hash,
                reply_sha256=reply_sha, phase='SCREENING', transcript_sha256=transcript))
            request = dict(schema_version=1, request_id=request_id,
                conversation_id='neurobro:' + DAY + ':' + 'a'*32, phase='SCREENING',
                reply=rows[-1]['text'], reply_sha256=reply_sha, market_context=None,
                gate=dict(robot_on=True, native_dispatch_verified=True), deadline_epoch=NOW+50)
            response = dict(schema_version=1, ok=True, request_id=request_id,
                conversation_id=request['conversation_id'], phase='SCREENING', reply_sha256=reply_sha,
                authority='DIALOGUE_CONTROL_ONLY', trading_authority=False,
                decision=dict(action=action, symbols=['SUIUSDT'] if symbols is None else symbols, question=''),
                needs_followup=False, reason='DIALOGUE_DECISION')
            value = dict(schema_version=1, kind='NATIVE_SCREENING_MINI_REVIEW_V1', operation=operation,
                request_sha256=self.request_hash, payload_sha256=digest(self.payload),
                request_id=request_id, reply_sha256=reply_sha, context_sha256=digest(self.context),
                conversation_id=request['conversation_id'], phase='SCREENING', request=request,
                response=response, transcript_sha256=transcript, observed_at=NOW-15,
                model_operation=dict(operation_id='delegated-model:' + request_id,
                    kind='DELEGATED_MODEL_DIALOGUE', request_hash=digest(request),
                    state='DONE', result=wire(response, ascii=True), updated_at=NOW-15))
            raw = wire(value, ascii=True)
            proof['review_receipts'] = [dict(name='screening-review-fixture.json',
                sha256=sha(raw), bytes=len(raw.encode()), value=value)]
        return self.seal(dict(kind=KIND, operation=operation, request_sha256=self.request_hash,
            operation_completed=False, observation=proof,
            capture_name='received-analysis-observation-'+'a'*16+'-'+'b'*64+'.json',
            capture_sha256='b'*64, capture_bytes=100000))

    def recover(self, envelope=None, *, now=NOW):
        return recovery.recover_received_screening(self.provider, envelope or self.envelope,
            catalog=self.catalog, max_age=180, now=now)

    def assert_market_hold_replaced(self, *, stale=False, action='SCREENING'):
        reply = 'COIN PILIHAN:\nSUIUSDT Perpetual - HOLD, tunggu struktur dan momentum yang jelas.'
        self.envelope = self.fixture(reply=reply, action=action)
        if stale:
            self.created = NOW-1000
            self.db.execute('UPDATE api_requests SET created=? WHERE operation=?', (self.created, OP))
        finances = self.finances()
        self.assertEqual(self.recover(), 'NOEXEC')
        api = self.row('api_requests')
        self.assertEqual((api['state'],api['failure_code'],api['output'],api['attempts'],api['created']),
            (recovery.RECEIVED_HOLD_TERMINAL,recovery.RECEIVED_HOLD_REASON,None,1,self.created))
        data = json.loads(self.row('robot_cycles')['data'])
        self.assertEqual(data,{**self.data,'screen':9,'replacements':9,'round_symbols':[],
            'seen':self.data['seen']+['SUIUSDT'],'replacement_due':1})
        self.assertEqual(self.row('robot_jobs')['state'],'COMPLETE')
        self.assertEqual(recovery.received_screening_replacement_due(self.db,CYCLE,data),1)
        self.assertEqual(recovery.received_screening_replacement_due(self.db,CYCLE,{**data,'seen':self.data['seen']}),0)
        self.assertEqual(self.finances(),finances)
        self.assertEqual(self.row('research_browser_responses')['assistant_text'],reply)
        snapshot=tuple(self.db.iterdump());self.assertEqual(self.recover(),'NOEXEC')
        self.assertEqual(tuple(self.db.iterdump()),snapshot)

    def test_fresh_neurobro_hold_with_immutable_mini_screening_review_never_queues_held_coin(self):
        self.assert_market_hold_replaced()

    def test_stale_neurobro_hold_keeps_true_hold_cause_and_actual_mini_hold(self):
        self.assert_market_hold_replaced(stale=True,action='HOLD')

    def test_mini_hold_cannot_invent_clear_hold_or_replace_missing_approval(self):
        finances=self.finances()
        for reply,approval,action in (
                ('COIN: SUIUSDT',True,'HOLD'),
                ('COIN PILIHAN:\nSUIUSDT Perpetual - HOLD dan LONG',True,'HOLD'),
                ('COIN PILIHAN:\nSUIUSDT Perpetual - HOLD, tunggu struktur.',False,'HOLD'),
                ('Data chart belum terbaca.\nCOIN PILIHAN:\nSUIUSDT Perpetual - HOLD',True,'HOLD')):
            with self.subTest(reply=reply,approval=approval),self.assertRaises(Review):
                self.recover(self.fixture(reply=reply,approval=approval,action=action))
            self.assertEqual(self.row('api_requests')['state'],'NEEDS_REVIEW')
            self.assertIsNone(self.row('research_browser_responses'))
            self.assertEqual(self.finances(),finances)

    def test_actual_sc3_reply_retires_both_hold_selections_without_queue_or_replay(self):
        operation=CYCLE+':screening:3:2';_,schema=screening_contract(2)
        self.context=dict(self.context,requested_count=2)
        self.body,serialized,self.request_hash=self.source.request_body(operation,
            'Pilih dua coin pengganti.',schema,self.context)
        self.payload={**self.body,'request_sha256':self.request_hash}
        data={**self.data,'screen':2,'replacements':2}
        self.db.execute('UPDATE robot_cycles SET data=? WHERE id=?',(json.dumps(data),CYCLE))
        self.db.execute('UPDATE robot_jobs SET operation=? WHERE operation=?',(operation,OP))
        self.db.execute('UPDATE api_requests SET operation=?,body_hash=? WHERE operation=?',
            (operation,self.request_hash,OP))
        self.db.execute('UPDATE research_browser_requests SET operation=?,request_sha256=?,marker=?,request_body=? WHERE operation=?',
            (operation,self.request_hash,self.body['marker'],serialized,OP))
        reply=(ROOT/'tests/fixtures/neurobro_sc3_named_hold_20261106.txt').read_text()
        self.catalog.update(ARBUSDT={},ZECUSDT={})
        envelope=self.fixture(operation=operation,reply=reply,symbols=['ARBUSDT','ZECUSDT'],action='HOLD')
        self.assertEqual(self.recover(envelope),'NOEXEC')
        cycle=dict(self.db.execute('SELECT * FROM robot_cycles WHERE id=?',(CYCLE,)).fetchone())
        current=json.loads(cycle['data'])
        self.assertEqual(current['queue'],[]);self.assertEqual(current['round_symbols'],[])
        self.assertEqual(current['seen'][-2:],['ARBUSDT','ZECUSDT'])
        self.assertEqual((current['screen'],current['replacements'],current['replacement_due']),(3,3,2))
        self.assertEqual(recovery.received_screening_replacement_due(self.db,CYCLE,current),2)
        self.assertEqual(self.db.execute('SELECT state FROM api_requests WHERE operation=?',(operation,)).fetchone()[0],
            recovery.RECEIVED_HOLD_TERMINAL)

    def native_hold_replacement_case(self):
        reply='COIN PILIHAN:\nSUIUSDT Perpetual - HOLD, tunggu struktur yang jelas.'
        self.envelope=self.fixture(reply=reply)
        result,received,_=recovery.validate_received_screening(self.provider,self.envelope,self.payload,
            self.created,catalog=self.catalog,max_age=180,now=NOW)
        from worker.neuroapi import selections
        self.provider._record_browser_response(OP,self.body,self.request_hash,result,self.body['output_schema'],
            lambda value:selections(value,self.catalog,1),self.context,catalog=self.catalog,received_at=received,recovery=True)
        self.db.execute("UPDATE robot_jobs SET state='PENDING' WHERE operation=?",(OP,))
        self.db.execute("UPDATE robot_cycles SET state='ACTIVE' WHERE id=?",(CYCLE,))
        self.assertTrue(recovery.completed_screening_market_hold(self.db,OP))
        native_robot=native_module('worker._held_screen_native',ROOT/'vps_native/worker/robot.py')
        robot=object.__new__(native_robot.Coordinator);robot.db=self.db;robot.neuro=self.provider
        robot.allowed=lambda today:True;robot.market=SimpleNamespace(catalog=lambda:self.catalog,max_age=180)
        robot.exposed_symbols=lambda account:set()
        robot.store=SimpleNamespace(report=lambda status,account=None,reason=None,**kw:dict(bot_status=status,reason=reason,**kw))
        with patch.object(native_robot.time,'time',return_value=NOW):
            waiting=robot.screen(CYCLE,copy.deepcopy(self.data),1,{},DAY,False)
        self.assertEqual(waiting['wait_reason'],'SCREENING_HOLD_RECONCILING')
        self.assertEqual(json.loads(self.row('robot_cycles')['data']),self.data)
        self.assertEqual(self.row('api_requests')['state'],'COMPLETE')
        calls=[];self.provider.require_key=lambda:None
        self.provider.headers=lambda:{}
        def transport(method,url,headers,body,*,timeout):
            calls.append(body['action']);return 200,{},self.envelope
        self.provider.transport=transport
        with patch.object(recovery.time,'time',return_value=NOW):
            recovered=recovery.resume_uncertain_screening(robot,DAY,{})
        self.assertEqual(recovered['wait_reason'],'SCREENING_HOLD_REPLACEMENT_PENDING')
        self.assertEqual(calls,['SCREENING_RECEIVED_OBSERVE'])
        self.assertEqual(self.row('api_requests')['state'],recovery.RECEIVED_HOLD_TERMINAL)
        current=json.loads(self.row('robot_cycles')['data'])
        self.assertEqual(native_robot.Coordinator.replacement_count(robot,current,[],CYCLE),1)
        self.assertEqual(current['seen'][-1],'SUIUSDT');self.assertEqual(current['queue'],[])
        # The next original coordinator tick follows the sealed replacement
        # trigger. Its existing provider boundary is stopped before any Send.
        robot.stopping=None;robot.account_factory=lambda:None
        robot.settle_previous_research=lambda today,account:None
        robot.execution_slots=lambda account,today:1
        robot.store.settings=lambda:dict(robot_on=True)
        robot.store.entries=lambda today:1
        robot.store.report_account=lambda account:None
        robot.store.results=lambda cycle:list(self.db.execute('SELECT * FROM robot_candidates WHERE cycle=?',(cycle,)))
        asks=[]
        from worker.neuroapi import ResearchPaused
        def before_send(operation,prompt,schema,validate,context,**kwargs):
            asks.append((operation,context));raise ResearchPaused('RESEARCH_PAUSED')
        self.provider.ask=before_send
        with patch.object(native_robot,'day',return_value=DAY), \
                patch.object(native_robot,'account_state',return_value={}), \
                patch('agent.native_pending_review.review_then_advance',return_value=None):
            following=robot._tick()
        self.assertEqual(following['wait_reason'],'NEUROBRO_RESEARCH_PAUSED')
        self.assertEqual(asks[0][0],CYCLE+':screening:10:1')
        self.assertEqual(asks[0][1]['requested_count'],1)
        self.assertIn('SUIUSDT',asks[0][1]['excluded_symbols'])
        self.assertEqual(self.row('api_requests')['attempts'],1)
        return robot,native_robot,asks

    def test_normal_native_screen_defers_accepted_hold_then_scheduler_recovers_without_unknown(self):
        self.native_hold_replacement_case()

    def test_native_hold_then_stale_replacement_keeps_search_running_with_sealed_proof(self):
        robot,native_robot,asks=self.native_hold_replacement_case()
        operation=CYCLE+':screening:10:1';self.context=asks[0][1]
        self.body,serialized,self.request_hash=self.source.request_body(operation,
            'Pilih satu coin pengganti lain.',self.body['output_schema'],self.context)
        self.payload={**self.body,'request_sha256':self.request_hash};self.catalog['FILUSDT']={}
        self.db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',
            (operation,CYCLE,'SCREENING',None,'NEEDS_REVIEW',None))
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',
            (operation,None,self.request_hash,'NEEDS_REVIEW',NOW-1000,1,None,'NEUROBRO_BROWSER_OUTCOME_UNKNOWN'))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',
            (operation,self.source.BROWSER_PROVIDER,self.source.BROWSER_CONTRACT,self.request_hash,self.body['marker'],serialized))
        replacement=self.fixture(operation=operation,reply='COIN: FILUSDT',symbols=['FILUSDT'])
        self.assertEqual(self.recover(replacement),'NOEXEC')
        current=json.loads(self.row('robot_cycles')['data'])
        self.assertEqual((current['screen'],current['replacements'],current['replacement_due']),(10,10,1))
        self.assertEqual(current['round_symbols'],[]);self.assertIn('SUIUSDT',current['seen'])
        self.assertNotIn('FILUSDT',current['seen'])
        self.assertEqual(recovery.received_screening_replacement_due(self.db,CYCLE,current),1)
        with patch.object(native_robot,'day',return_value=DAY), \
                patch.object(native_robot,'account_state',return_value={}), \
                patch('agent.native_pending_review.review_then_advance',return_value=None):
            following=robot._tick()
        self.assertEqual(following['wait_reason'],'NEUROBRO_RESEARCH_PAUSED')
        self.assertEqual(asks[-1][0],CYCLE+':screening:11:1')
        self.assertIn('SUIUSDT',asks[-1][1]['excluded_symbols'])
        self.assertEqual(self.db.execute('SELECT state FROM api_requests WHERE operation=?',(operation,)).fetchone()[0],
            recovery.RECEIVED_TERMINAL)

    def test_fresh_round9_uses_original_saved_contract_and_native_cached_receipt(self):
        original = self.row('robot_cycles')['data']
        before_api = self.row('api_requests')
        finances = self.finances()
        self.assertEqual(self.recover(), 'RESUME_CACHED')
        self.assertEqual(self.row('api_requests')['state'], 'COMPLETE')
        self.assertEqual(json.loads(self.row('api_requests')['output']), {'symbols': ['SUIUSDT']})
        self.assertEqual(self.row('robot_jobs')['state'], 'PENDING')
        self.assertEqual(self.row('robot_cycles')['state'], 'ACTIVE')
        self.assertEqual(self.row('robot_cycles')['data'], original)
        journal = dict(self.db.execute('SELECT * FROM screening_response_recoveries').fetchone())
        self.assertEqual(json.loads(journal['preimage'])['api_requests'], before_api)
        self.assertEqual(journal['phase'], 'RESUME_CACHED')
        self.assertEqual(self.finances(), finances)
        receipt = self.row('research_browser_responses')
        self.assertEqual(receipt['assistant_text'], 'COIN: SUIUSDT')
        self.assertEqual(receipt['received_at'], NOW-10)
        snapshot = tuple(self.db.iterdump())
        self.assertEqual(self.recover(), 'RESUME_CACHED')
        self.assertEqual(tuple(self.db.iterdump()), snapshot)

    def test_existing_selection_table_approval_and_cache_are_unchanged(self):
        from agent.neurobro_reply import screening_market_hold
        reply='COIN PILIHAN:\n| COIN | Alasan |\n| --- | --- |\n| SUIUSDT | Momentum jelas |'
        self.assertFalse(screening_market_hold(reply,['SUIUSDT']))
        self.assertEqual(self.recover(self.fixture(reply=reply)),'RESUME_CACHED')
        self.assertEqual(json.loads(self.row('api_requests')['output']),{'symbols':['SUIUSDT']})
        self.assertEqual(self.row('robot_jobs')['state'],'PENDING')

    def test_existing_coordinator_drains_fresh_round9_cache_without_provider_call(self):
        from worker import robot as robot_module
        self.assertEqual(self.recover(), 'RESUME_CACHED')
        robot = object.__new__(robot_module.Coordinator)
        robot.db = self.db
        robot.neuro = self.provider
        robot.allowed = lambda today: today == DAY
        robot.market = SimpleNamespace(catalog=lambda: self.catalog, max_age=180)
        robot.exposed_symbols = lambda account: set()
        robot.store = SimpleNamespace(report=lambda status, account=None, reason=None, **kw:
            dict(bot_status=status, reason=reason, **kw))
        finances = self.finances()
        data = json.loads(self.row('robot_cycles')['data'])
        with patch.object(robot_module.time, 'time', return_value=NOW):
            result = robot.screen(CYCLE, data, 1, {}, DAY, False)
        self.assertEqual(result['wait_reason'], 'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(self.row('robot_jobs')['state'], 'COMPLETE')
        self.assertEqual(json.loads(self.row('robot_cycles')['data']),
            {**self.data, 'screen': 9, 'replacements': 9,
             'queue': ['SUIUSDT'], 'round_symbols': ['SUIUSDT']})
        self.assertEqual(self.finances(), finances)

    def test_stale_round9_preserves_previous_hold_trigger_capacity_and_raw_native_receipt(self):
        self.created = NOW - 1000
        self.db.execute('UPDATE api_requests SET created=? WHERE operation=?', (self.created, OP))
        finances = self.finances()
        self.assertEqual(self.recover(), 'NOEXEC')
        api = self.row('api_requests')
        self.assertEqual(api['state'], 'RECEIVED_STALE_NOT_EXECUTED')
        self.assertEqual(api['failure_code'], 'SCREENING_RESPONSE_RECEIVED_STALE')
        self.assertIsNone(api['output'])
        self.assertEqual(api['created'], self.created)
        self.assertEqual(api['attempts'], 1)
        self.assertEqual(self.row('robot_jobs')['state'], 'COMPLETE')
        self.assertEqual(self.row('robot_cycles')['state'], 'ACTIVE')
        self.assertEqual(json.loads(self.row('robot_cycles')['data']),
            {**self.data, 'screen': 9, 'replacements': 9, 'replacement_due': 1})
        self.assertEqual(self.finances(), finances)
        self.assertEqual(self.row('research_browser_responses')['assistant_text'], 'COIN: SUIUSDT')
        snapshot = tuple(self.db.iterdump())
        self.assertEqual(self.recover(), 'NOEXEC')
        self.assertEqual(tuple(self.db.iterdump()), snapshot)

    def test_normal_scheduler_keeps_round9_hold_trigger_and_requests_round10_for_one_slot(self):
        from worker import robot as robot_module
        self.db.execute('UPDATE api_requests SET created=? WHERE operation=?', (NOW-1000, OP))
        self.assertEqual(self.recover(), 'NOEXEC')
        robot = object.__new__(robot_module.Coordinator)
        robot.db = self.db
        robot.neuro = self.provider
        robot.stopping = None
        robot.account_factory = lambda: None
        robot.allowed = lambda today: True
        robot.advance_execution = lambda account, today: None
        robot.settle_previous_research = lambda today, account: None
        robot.execution_slots = lambda account, today: 1
        robot.exposed_symbols = lambda account: set()
        robot.market = SimpleNamespace(catalog=lambda: self.catalog, max_age=180)
        robot.store = SimpleNamespace(settings=lambda: dict(robot_on=True),
            entries=lambda today: 1, report_account=lambda account: None,
            results=lambda cycle: list(self.db.execute(
                'SELECT * FROM robot_candidates WHERE cycle=?', (cycle,))),
            report=lambda status, account=None, reason=None, **kw:
                dict(bot_status=status, reason=reason, **kw))
        self.provider.require_key = lambda: None
        calls = []
        def screen(cycle, data, count, account, today, initial):
            call = dict(cycle=cycle, count=count, next_round=data['screen']+1,
                initial=initial, round_symbols=data['round_symbols'])
            calls.append(call)
            return call
        robot.screen = screen
        with patch.object(robot_module, 'day', return_value=DAY), \
                patch.object(robot_module, 'account_state', return_value={}):
            result = robot._tick()
        self.assertEqual(result, dict(cycle=CYCLE, count=1, next_round=10,
            initial=False, round_symbols=['AVAXUSDT']))
        self.assertEqual(calls, [result])
        self.assertEqual(self.db.execute('SELECT count(*) FROM order_intents').fetchone()[0], 1)
        self.assertEqual(self.row('api_requests')['attempts'], 1)

    def test_stale_initial_screening_replacement_requires_its_immutable_proof(self):
        from worker.robot import Coordinator
        operation = CYCLE + ':screening:0:1'
        self.context = None
        _, schema = screening_contract(1)
        self.body, serialized, self.request_hash = self.source.request_body(
            operation, 'Instruksi awal asli — satu coin.', schema, None)
        self.payload = {**self.body, 'request_sha256': self.request_hash}
        original = dict(target=1, queue=[], seen=[], screen=-1,
            replacements=0, round_symbols=[], risk_target='5')
        self.db.execute('UPDATE robot_cycles SET data=? WHERE id=?', (json.dumps(original), CYCLE))
        self.db.execute('UPDATE robot_jobs SET operation=? WHERE operation=?', (operation, OP))
        self.db.execute('UPDATE api_requests SET operation=?,body_hash=?,created=? WHERE operation=?',
            (operation, self.request_hash, NOW-1000, OP))
        self.db.execute('UPDATE research_browser_requests SET operation=?,request_sha256=?,marker=?,request_body=? WHERE operation=?',
            (operation, self.request_hash, self.body['marker'], serialized, OP))
        envelope = self.fixture(operation=operation)
        self.assertEqual(self.recover(envelope), 'NOEXEC')
        data = json.loads(self.row('robot_cycles')['data'])
        self.assertEqual(data, {**original, 'screen': 0, 'replacement_due': 1})
        robot = object.__new__(Coordinator)
        robot.db = self.db
        self.assertEqual(robot.replacement_count(data, [], CYCLE), 1)
        self.assertEqual(robot.replacement_count({**data, 'replacement_due': 2}, [], CYCLE), 0)
        self.db.execute("UPDATE screening_response_recoveries SET proof_sha256=? WHERE operation=?", ('0'*64, operation))
        self.assertEqual(robot.replacement_count(data, [], CYCLE), 0)
        self.db.execute('DELETE FROM screening_response_recoveries WHERE operation=?', (operation,))
        self.assertEqual(robot.replacement_count(data, [], CYCLE), 0)

    def test_received_named_two_coin_reply_requires_actual_done_review_before_stale_nonexecution(self):
        operation = CYCLE + ':screening:0:2'
        self.context = None
        _, schema = screening_contract(2)
        self.body, serialized, self.request_hash = self.source.request_body(
            operation, 'Instruksi screening awal asli untuk dua coin.', schema, None)
        self.payload = {**self.body, 'request_sha256': self.request_hash}
        original = dict(target=2, queue=[], seen=[], screen=-1, replacements=0, round_symbols=[], risk_target='5')
        self.catalog.update(NEARUSDT={}, ZECUSDT={})
        self.db.execute('UPDATE robot_cycles SET data=? WHERE id=?', (json.dumps(original), CYCLE))
        self.db.execute('UPDATE robot_jobs SET operation=? WHERE operation=?', (operation, OP))
        self.db.execute('UPDATE api_requests SET operation=?,body_hash=?,created=? WHERE operation=?',
            (operation, self.request_hash, NOW-1000, OP))
        self.db.execute('UPDATE research_browser_requests SET operation=?,request_sha256=?,marker=?,request_body=? WHERE operation=?',
            (operation, self.request_hash, self.body['marker'], serialized, OP))
        reply = (ROOT / 'tests/fixtures/neurobro_screening_named_choices_20261106.txt').read_text()
        envelope = self.fixture(approval=False, operation=operation, reply=reply)
        finances = self.finances()
        with self.assertRaisesRegex(Review, 'SCREENING_MINI_APPROVAL_NOT_RETAINED'):
            self.recover(envelope)
        self.assertIsNone(self.db.execute('SELECT 1 FROM research_browser_responses WHERE operation=?', (operation,)).fetchone())
        self.assertEqual(self.db.execute('SELECT state FROM api_requests WHERE operation=?', (operation,)).fetchone()[0], 'NEEDS_REVIEW')
        approved = self.fixture(operation=operation, reply=reply, symbols=['NEARUSDT', 'ZECUSDT'])
        self.assertEqual(self.recover(approved), 'NOEXEC')
        api = dict(self.db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone())
        self.assertEqual((api['state'], api['failure_code'], api['output'], api['attempts'], api['created']),
            ('RECEIVED_STALE_NOT_EXECUTED', 'SCREENING_RESPONSE_RECEIVED_STALE', None, 1, NOW-1000))
        self.assertEqual(self.db.execute('SELECT state FROM robot_jobs WHERE operation=?', (operation,)).fetchone()[0], 'COMPLETE')
        self.assertEqual(json.loads(self.row('robot_cycles')['data']), {**original, 'screen': 0, 'replacement_due': 2})
        self.assertEqual(self.finances(), finances)

    def test_root_replacement_flag_without_recovery_journal_is_not_a_trigger(self):
        from worker.robot import Coordinator
        robot = object.__new__(Coordinator)
        robot.db = self.db
        data = dict(target=1, queue=[], seen=[], screen=0, replacements=0,
            round_symbols=[], replacement_due=1)
        self.assertEqual(robot.replacement_count(data, [], CYCLE), 0)

    def test_missing_actual_mini_approval_refuses_even_stale_receipt(self):
        self.db.execute('UPDATE api_requests SET created=? WHERE operation=?', (NOW-1000, OP))
        with self.assertRaises(Review):
            self.recover(self.fixture(approval=False))
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')
        self.assertIsNone(self.row('research_browser_responses'))

    def test_pending_process_disappearance_uses_same_received_receipt(self):
        self.db.execute("UPDATE api_requests SET state='PENDING',failure_code=NULL WHERE operation=?", (OP,))
        self.db.execute("UPDATE robot_jobs SET state='PENDING' WHERE operation=?", (OP,))
        self.db.execute("UPDATE robot_cycles SET state='ACTIVE' WHERE id=?", (CYCLE,))
        self.assertEqual(self.recover(), 'RESUME_CACHED')
        self.assertEqual(self.row('api_requests')['attempts'], 1)
        self.assertEqual(self.row('api_requests')['created'], self.created)

    def test_scheduler_unknown_mini_preserves_unknown_without_initial_ask(self):
        calls = []
        envelope = self.fixture(approval=False)
        self.provider.require_key = lambda: None
        def observe(operation):
            calls.append(('observe', operation))
            return envelope
        def review(operation, observation):
            calls.append(('review', operation))
            raise Review('SCREENING_REVIEW_OUTCOME_UNKNOWN')
        self.provider.observe_received_screening = observe
        self.provider.review_received_screening = review
        coordinator = SimpleNamespace(db=self.db, neuro=self.provider,
            market=SimpleNamespace(max_age=180, catalog=lambda: self.catalog),
            allowed=lambda today: True,
            store=SimpleNamespace(report=lambda status, account=None, reason=None, **kw:
                dict(bot_status=status, reason=reason, **kw)))
        with patch.object(recovery.time, 'time', return_value=NOW):
            result = recovery.resume_uncertain_screening(coordinator, DAY, {})
        self.assertEqual(result['wait_reason'], 'SCREENING_REVIEW_OUTCOME_UNKNOWN')
        self.assertEqual(calls, [('observe', OP), ('review', OP)])
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')
        self.assertEqual(self.row('robot_jobs')['state'], 'NEEDS_REVIEW')
        self.assertIsNone(self.row('research_browser_responses'))

    def test_unknown_or_unbound_actual_mini_row_cannot_clear_received_research(self):
        for key, invalid in (('state', 'UNKNOWN'), ('state', 'STARTED'),
                ('request_hash', 'not-a-sha256'), ('operation_id', 'delegated-model:'+'e'*64),
                ('updated_at', NOW+1)):
            with self.subTest(key=key, invalid=invalid):
                envelope = copy.deepcopy(self.envelope)
                entry = envelope['observation']['review_receipts'][0]
                entry['value']['model_operation'][key] = invalid
                raw = wire(entry['value'], ascii=True)
                entry.update(sha256=sha(raw), bytes=len(raw.encode()))
                self.seal(envelope)
                with self.assertRaises(Review):
                    self.recover(envelope)
                self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')
                self.assertIsNone(self.row('research_browser_responses'))

    def test_native_helper_observe_and_review_use_exact_authenticated_fixed_rpc(self):
        calls = []
        self.provider.require_key = lambda: None
        self.provider.headers = lambda: {'Authorization': 'Bearer local-test-only'}
        def transport(method, url, headers, body, *, timeout):
            calls.append((method, url, headers, body, timeout))
            return 200, {}, self.envelope
        self.provider.transport = transport
        self.assertEqual(self.provider.observe_received_screening(OP), self.envelope)
        self.assertEqual(self.provider.review_received_screening(OP, self.envelope), self.envelope)
        self.assertEqual(calls[0], ('POST', self.native.BASE+'/v1/screening-recovery',
            {'Authorization': 'Bearer local-test-only'},
            dict(action='SCREENING_RECEIVED_OBSERVE', payload=self.payload), 45))
        self.assertEqual(calls[1], ('POST', self.native.BASE+'/v1/screening-recovery',
            {'Authorization': 'Bearer local-test-only'},
            dict(action='SCREENING_RECEIVED_REVIEW', payload=self.payload,
                source_capture_name=self.envelope['capture_name'],
                source_capture_sha256=self.envelope['capture_sha256'],
                source_capture_bytes=self.envelope['capture_bytes'],
                source_observation_sha256=self.envelope['observation_sha256']), 120))
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')

    def test_native_helper_unknown_review_never_enters_initial_ask(self):
        self.provider.require_key = lambda: None
        self.provider.headers = lambda: {}
        calls = []
        def transport(method, url, headers, body, *, timeout):
            calls.append(body['action'])
            return 409, {}, dict(reason='MODEL_OUTCOME_UNKNOWN')
        self.provider.transport = transport
        with self.assertRaisesRegex(Review, '^SCREENING_REVIEW_OUTCOME_UNKNOWN$'):
            self.provider.review_received_screening(OP, self.envelope)
        self.assertEqual(calls, ['SCREENING_RECEIVED_REVIEW'])
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')
        self.assertIsNone(self.row('research_browser_responses'))

    def test_fixed_recovery_endpoint_refuses_send_and_extra_payload_before_transport(self):
        denied = [dict(action='SEND', payload=self.payload),
            dict(action='SCREENING_RECEIVED_OBSERVE', payload=self.payload, envelope=self.envelope)]
        for body in denied:
            with self.subTest(action=body['action']), self.assertRaisesRegex(
                    Review, 'NEUROBRO_BROWSER_ENDPOINT_DENIED'):
                self.native.browser_request('POST', self.native.BASE+'/v1/screening-recovery', {}, body)
        self.provider.require_key = lambda: None
        invalid = copy.deepcopy(self.envelope)
        invalid['capture_name'] = '../private-other-operation.json'
        with self.assertRaises(Review):
            self.provider.review_received_screening(OP, invalid)

    def test_actual_mini_hash_survives_a_new_technical_lookup_deadline(self):
        entry = self.envelope['observation']['review_receipts'][0]
        historical_hash = entry['value']['model_operation']['request_hash']
        entry['value']['request']['deadline_epoch'] += 30
        raw = wire(entry['value'], ascii=True)
        entry.update(sha256=sha(raw), bytes=len(raw.encode()))
        self.seal(self.envelope)
        self.assertEqual(self.recover(), 'RESUME_CACHED')
        retained = json.loads(self.db.execute(
            'SELECT proof FROM screening_response_recoveries').fetchone()[0])
        self.assertEqual(retained['observation']['review_receipts'][0]
            ['value']['model_operation']['request_hash'], historical_hash)

    def test_posted_followups_use_full_transcript_identity(self):
        envelope = self.fixture(followup=True)
        self.assertEqual(self.recover(envelope), 'RESUME_CACHED')
        self.assertEqual(self.row('research_browser_responses')['assistant_text'], 'COIN: SUIUSDT')

    def test_wrong_transcript_review_cannot_approve_same_text(self):
        envelope = self.fixture(followup=True)
        old = self.fixture()['observation']['review_receipts']
        envelope['observation']['review_receipts'] = old
        self.seal(envelope)
        with self.assertRaises(Review):
            self.recover(envelope)
        self.assertIsNone(self.row('research_browser_responses'))

    def test_canonical_saved_request_is_required(self):
        self.db.execute('UPDATE research_browser_requests SET request_body=? WHERE operation=?',
            (json.dumps(self.body), OP))
        with self.assertRaises(Review):
            self.recover()
        self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')
        self.assertIsNone(self.row('research_browser_responses'))

    def test_actual_context_exclusions_cannot_be_discarded(self):
        envelope = self.fixture(reply='COIN: AVAXUSDT', approval=False)
        entry = copy.deepcopy(self.envelope['observation']['review_receipts'][0])
        value = entry['value']
        rows = envelope['observation']['rows']
        reply_sha = sha(rows[-1]['text'])
        transcript = digest(rows)
        request_id = digest(dict(operation=OP, request_sha256=self.request_hash,
            reply_sha256=reply_sha, phase='SCREENING', transcript_sha256=transcript))
        value.update(request_id=request_id, reply_sha256=reply_sha, transcript_sha256=transcript)
        value['request'].update(request_id=request_id, reply=rows[-1]['text'], reply_sha256=reply_sha)
        value['response'].update(request_id=request_id, reply_sha256=reply_sha)
        value['response']['decision']['symbols'] = ['AVAXUSDT']
        value['model_operation'].update(operation_id='delegated-model:' + request_id,
            request_hash=digest(value['request']), result=wire(value['response'], ascii=True))
        raw = wire(value, ascii=True)
        entry.update(sha256=sha(raw), bytes=len(raw.encode()))
        envelope['observation']['review_receipts'] = [entry]
        self.seal(envelope)
        with self.assertRaises(Review):
            self.recover(envelope)
        self.assertIsNone(self.row('research_browser_responses'))

    def test_incomplete_or_attached_transcript_cannot_clear_unknown(self):
        for change in ('incomplete', 'attachment', 'marker', 'scope'):
            with self.subTest(change=change):
                envelope = copy.deepcopy(self.envelope)
                proof = envelope['observation']
                if change == 'incomplete':
                    proof['rows'][-1]['complete'] = False
                elif change == 'attachment':
                    proof['rows'][0]['attachments'] = ['unrelated.png']
                elif change == 'marker':
                    proof['rows'][0]['text'] += '\n' + self.body['marker']
                else:
                    proof['operation_scope']['verified'] = False
                self.seal(envelope)
                with self.assertRaises(Review):
                    self.recover(envelope)
                self.assertEqual(self.row('api_requests')['state'], 'NEEDS_REVIEW')
                self.assertIsNone(self.row('research_browser_responses'))

    def test_sealed_preimage_exists_before_native_write_and_resumes_crash_gap(self):
        native_record = self.provider._record_browser_response
        calls = []
        def interrupted(*args, **kwargs):
            journal = dict(self.db.execute('SELECT * FROM screening_response_recoveries').fetchone())
            self.assertEqual(journal['phase'], 'PREPARED')
            self.assertEqual(json.loads(journal['preimage'])['api_requests']['state'], 'NEEDS_REVIEW')
            calls.append(1)
            native_record(*args, **kwargs)
            raise RuntimeError('controlled process disappearance after native commit')
        self.provider._record_browser_response = interrupted
        with self.assertRaisesRegex(RuntimeError, 'controlled process'):
            self.recover()
        self.assertEqual(self.row('api_requests')['state'], 'COMPLETE')
        self.assertEqual(self.row('robot_jobs')['state'], 'NEEDS_REVIEW')
        receipt = self.row('research_browser_responses')
        self.provider._record_browser_response = self.forbid
        self.assertEqual(self.recover(now=NOW+1), 'RESUME_CACHED')
        self.assertEqual(self.row('research_browser_responses'), receipt)
        self.assertEqual(calls, [1])

    def test_crash_gap_rechecks_current_age_before_resuming(self):
        native_record = self.provider._record_browser_response
        def interrupted(*args, **kwargs):
            native_record(*args, **kwargs)
            raise RuntimeError('controlled disappearance')
        self.provider._record_browser_response = interrupted
        with self.assertRaises(RuntimeError):
            self.recover()
        receipt = self.row('research_browser_responses')
        self.provider._record_browser_response = self.forbid
        self.assertEqual(self.recover(now=NOW+300), 'NOEXEC')
        self.assertEqual(self.row('research_browser_responses'), receipt)
        self.assertIsNone(self.row('api_requests')['output'])
        self.assertEqual(json.loads(self.row('robot_cycles')['data']),
            {**self.data, 'screen': 9, 'replacements': 9, 'replacement_due': 1})


if __name__ == '__main__':
    unittest.main()
