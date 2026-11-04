"""Historical evaluation joins only: fake native readers and isolated journals.

No browser, provider, account or service is contacted. PNG bytes below are
format-only fixtures, never evidence that a real market screenshot was read.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from agent.native_data_files import deliver_market_data_files
from tests.test_agent_closed_candles_200 import native_context_200
from worker import pnl_calendar as pnl
from worker import trade_evaluation as journal
from worker import trade_evaluation_evidence as evidence

NOW=1_793_491_200

def stamp(seconds):return datetime.fromtimestamp(seconds,timezone.utc).isoformat()

def fixtures():
    context=native_context_200(NOW-10)
    opened=(NOW+20)*1000;closed=(NOW+80)*1000;end=closed+10000
    rows=[dict(symbol='BTCUSDT',id=1,orderId=101,side='BUY',positionSide='BOTH',qty='1',
        price='100',realizedPnl='0',commission='0.05',commissionAsset='USDT',time=opened),
        dict(symbol='BTCUSDT',id=2,orderId=202,side='SELL',positionSide='BOTH',qty='1',
        price='98',realizedPnl='-2',commission='0.049',commissionAsset='USDT',time=closed)]
    history=pnl._empty('a'*64);history['fills']={pnl._key(r):r for r in rows}
    history['trade_coverage']={'BTCUSDT':[[opened-1000,end]]}
    history['income_coverage']=[[opened-1000,end]]
    history['anchors']={'BTCUSDT':dict(end=end,quantities={})}
    position=pnl.calendar(history,end)['positions'][0]
    operation='2026-11-01:robot-v9:0:analysis-v9:BTCUSDT'
    intent=dict(symbol='BTCUSDT',side='LONG',position_side='BOTH',client_order_id='hao-existing',
        entry=dict(side='BUY',quantity='1',price='100'),protection=dict(exit_side='SELL',stop_loss='99',take_profit='102'))
    result=dict(source='BINANCE_FUTURES',state='CLOSED',symbol='BTCUSDT',client_order_id='hao-existing',order_id='101',exit_order_id='202',
        closed_at=stamp(NOW+80),first_fill_at=stamp(NOW+20),filled_quantity='1',
        closure_source='EXTERNAL_REDUCE_ONLY',closure_receipt=dict(entry_trades=[rows[0]],exit_trades=[rows[1]],exit_orders=[]))
    reply='COIN: BTCUSDT\nARAH: LONG\nENTRY: 100\nSL: 99\nTP: 102'
    decision=dict(symbol='BTCUSDT',side='LONG',limit_entry=100,entry_price=100,stop_loss=99,take_profit=102,risk_reward=2)
    request=dict(operation=operation,context=context)
    encoded=evidence.canonical(request);request_sha=hashlib.sha256(encoded.encode()).hexdigest()
    pngs={tf:b'\x89PNG\r\n\x1a\nFORMAT_ONLY_'+tf.encode()for tf in('15m','1h')}
    charts=[dict(source=evidence.WEBSITE_SOURCE,symbol='BTCUSDT',timeframe=tf,
        captured_at=stamp(NOW-8),sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))for tf,raw in pngs.items()]
    response=dict(operation=operation,response_id='original-native',assistant_text=reply,
        assistant_sha256=hashlib.sha256(reply.encode()).hexdigest(),received_at=NOW+5,
        chart_evidence=json.dumps(charts))
    csvs=deliver_market_data_files(context,now=context['fetched_at'])
    files=[dict(kind='PNG',timeframe=tf,name=tf+'.png',buffer=raw)for tf,raw in pngs.items()]
    files.extend(dict(kind='CSV',timeframe=r['timeframe'],name=r['name'],buffer=r['buffer'])for r in csvs)
    archive=dict(protocol=evidence.MATERIAL_PROTOCOL,context=context,files=files,sidecar_sha256='9'*64,
        delivery=dict(operation=operation,request_sha256=request_sha,response_id=response['response_id'],
            assistant_sha256=response['assistant_sha256'],native_intent_sha256='b'*64,
            native_completion_sha256='c'*64,context_sha256=evidence.digest(context),
            png_evidence_sha256=evidence.digest(charts),csv_sha256={r['timeframe']:r['sha256']for r in csvs}))
    db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row
    db.executescript('CREATE TABLE order_intents(id TEXT,candidate_id TEXT,state TEXT,symbol TEXT,payload TEXT,result TEXT,created TEXT);'
        'CREATE TABLE robot_candidates(id TEXT,plan TEXT);'
        'CREATE TABLE research_browser_requests(operation TEXT,request_body TEXT,request_sha256 TEXT);'
        'CREATE TABLE research_browser_responses(operation TEXT,response_id TEXT,assistant_text TEXT,assistant_sha256 TEXT,received_at REAL,chart_evidence TEXT);'
        'CREATE TABLE api_requests(operation TEXT,state TEXT,body_hash TEXT,output TEXT);')
    plan=dict(entry='100',sl='99',tp='102',quantity='1',neurobro_tp='102')
    db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?)',(operation,operation,'CLOSED','BTCUSDT',json.dumps(intent),json.dumps(result),stamp(NOW+10.123456)))
    db.execute('INSERT INTO robot_candidates VALUES(?,?)',(operation,json.dumps(plan)))
    db.execute('INSERT INTO research_browser_requests VALUES(?,?,?)',(operation,encoded,request_sha))
    db.execute('INSERT INTO research_browser_responses VALUES(?,?,?,?,?,?)',tuple(response[k]for k in('operation','response_id','assistant_text','assistant_sha256','received_at','chart_evidence')))
    db.execute('INSERT INTO api_requests VALUES(?,?,?,?)',(operation,'COMPLETE',request_sha,json.dumps(decision)))
    coordinator=SimpleNamespace(db=db,verified_intent=lambda row:json.loads(row['payload']),
        store=SimpleNamespace(verified_plan=lambda row:json.loads(row['plan'])))
    return SimpleNamespace(coordinator=coordinator,history=history,position=position,archive=archive,
        operation=operation,source=lambda *_:dict(source_type='NEUROBRO_BROWSER',provider_evidence_sha256='d'*64))


class EvaluationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures();self.addCleanup(self.fixture.coordinator.db.close)
    def bind(self):
        f=self.fixture
        return evidence.bind_closed_trade(f.coordinator,f.position,history=f.history,
            archive_reader=lambda *_:copy.deepcopy(f.archive),source_reader=f.source)
    def test_exact_lifecycle_order_research_and_original_four_materials_bind_without_financial_write(self):
        f=self.fixture;before=f.coordinator.db.serialize();binding=self.bind()
        self.assertEqual(binding['trade_id'],f.position['id']);self.assertEqual(binding['entry_order_id'],'101')
        self.assertEqual(binding['exit_order_ids'],['202']);self.assertEqual(len(binding['materials']['files']),4)
        self.assertEqual(binding['outcome']['pnl_usdt'],'-2.099')
        self.assertEqual(binding['original_decision']['take_profit'],102)
        self.assertEqual(binding['binding_sha256'],evidence.digest({k:v for k,v in binding.items()if k!='binding_sha256'}))
        self.assertEqual(f.coordinator.db.serialize(),before)
    def test_lifecycle_id_or_signed_economics_tamper_and_ambiguous_owner_are_refused(self):
        f=self.fixture;original=copy.deepcopy(f.position)
        f.position['id']='wrong'
        with self.assertRaises(ValueError):self.bind()
        f.position=original
        f.history['fills']['BTCUSDT:2']['commission']='0.001'
        with self.assertRaisesRegex(ValueError,'HISTORY_MISMATCH'):self.bind()
        f.history['fills']['BTCUSDT:2']['commission']='0.049'
        row=f.coordinator.db.execute('SELECT * FROM order_intents').fetchone()
        f.coordinator.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?)',('another',row['candidate_id'],row['state'],row['symbol'],row['payload'],row['result'],row['created']))
        with self.assertRaisesRegex(ValueError,'NOT_UNIQUELY_LINKED'):self.bind()
    def test_current_or_mutated_materials_cannot_replace_original_archive(self):
        f=self.fixture;original=copy.deepcopy(f.archive)
        for kind in('PNG','CSV'):
            f.archive=copy.deepcopy(original)
            next(r for r in f.archive['files']if r['kind']==kind)['buffer']+=b'tampered'
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.bind()
        f.archive=copy.deepcopy(original);f.archive['context']['fetched_at']+=3600
        with self.assertRaisesRegex(ValueError,'CONTEXT_MISMATCH'):self.bind()
        f.archive=copy.deepcopy(original);f.archive['delivery']['request_sha256']='e'*64
        with self.assertRaisesRegex(ValueError,'CUSTODY_MISMATCH'):self.bind()
    def test_observation_only_timestamp_does_not_change_binding_but_financial_race_refuses(self):
        f=self.fixture;original=self.bind()
        row=f.coordinator.db.execute('SELECT result FROM order_intents').fetchone()
        changed=json.loads(row[0]);changed['observed_at']=stamp(NOW+100)
        f.coordinator.db.execute('UPDATE order_intents SET result=?',(json.dumps(changed),))
        self.assertEqual(self.bind(),original)
        def mutate(*args):
            changed['filled_quantity']='2'
            f.coordinator.db.execute('UPDATE order_intents SET result=?',(json.dumps(changed),))
            return copy.deepcopy(f.archive)
        with self.assertRaisesRegex(ValueError,'SOURCE_CHANGED_DURING_BINDING'):
            evidence.bind_closed_trade(f.coordinator,f.position,history=f.history,
                archive_reader=mutate,source_reader=f.source)

    def test_partial_entry_and_external_close_remain_actual_facts_not_full_plan_or_sl_label(self):
        f=self.fixture
        for row in f.history['fills'].values():row['qty']='0.5'
        f.history['fills']['BTCUSDT:2']['realizedPnl']='-1'
        f.position=pnl.calendar(f.history,max(v['end']for v in f.history['anchors'].values()))['positions'][0]
        row=f.coordinator.db.execute('SELECT result FROM order_intents').fetchone();value=json.loads(row[0])
        value['filled_quantity']='0.5'
        value['closure_receipt']['entry_trades']=[copy.deepcopy(f.history['fills']['BTCUSDT:1'])]
        value['closure_receipt']['exit_trades']=[copy.deepcopy(f.history['fills']['BTCUSDT:2'])]
        f.coordinator.db.execute('UPDATE order_intents SET result=?',(json.dumps(value),))
        binding=self.bind()
        self.assertEqual(binding['execution']['quantity'],'0.5')
        self.assertEqual(binding['execution']['planned_quantity'],'1')
        self.assertEqual(binding['execution']['closure_source'],'EXTERNAL_REDUCE_ONLY')
        self.assertEqual(binding['outcome']['pnl_usdt'],'-1.099')

    def test_missing_reader_or_wrong_provider_and_incomplete_pnl_remain_blockers(self):
        f=self.fixture
        with self.assertRaisesRegex(ValueError,'ARCHIVE_READER_REQUIRED'):
            evidence.bind_closed_trade(f.coordinator,f.position,history=f.history,archive_reader=None,source_reader=f.source)
        f.source=lambda *_:dict(source_type='NEUROAPI_STRUCTURED')
        with self.assertRaisesRegex(ValueError,'DECISION_NOT_LINKED'):self.bind()
        f.position['complete']=False
        with self.assertRaisesRegex(ValueError,'CLOSED_NET_PNL_INCOMPLETE'):self.bind()


class EvaluationJournalTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures();self.addCleanup(self.fixture.coordinator.db.close)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        f=self.fixture;self.binding=evidence.bind_closed_trade(f.coordinator,f.position,history=f.history,
            archive_reader=lambda *_:copy.deepcopy(f.archive),source_reader=f.source)
        journal.observe_calendar(self.directory,dict(positions=[self.fixture.position],complete=False))
        journal.attach_evidence(self.directory,self.binding)
    def job(self):return journal.read_status(self.directory)['jobs'][0]
    def receipt(self,intent,text='Analisis awal sesuai bahan yang tersedia; hasil rugi saja tidak membuktikan setup salah.\nPENILAIAN SETUP: SESUAI BUKTI'):
        request=intent['request']
        return dict(protocol=evidence.PROTOCOL,phase='POST_TRADE_EVALUATION',provider='NEUROBRO_WEB',
            operation=intent['operation'],request_sha256=intent['request_sha256'],
            input_evidence_sha256=intent['input_evidence_sha256'],room_id=request['room_id'],sent_this_request=True,
            response_id='actual-native-evaluation-fixture',source_evidence_sha256='f'*64,received_at=intent['reserved_at']+1,
            assistant_text=text,assistant_sha256=hashlib.sha256(text.encode()).hexdigest())
    def test_once_intent_is_not_replayed_after_restart_and_result_survives_observation_refresh(self):
        self.assertEqual(self.job()['status'],'REQUIRES_NEUROBRO_EVALUATION')
        intent=journal.claim_evaluation(self.directory,self.job()['id'],'bound-existing-room',now=NOW+100)
        self.assertIn('bukan membuat setup atau order baru',intent['request']['prompt'])
        self.assertNotIn('JSON',intent['request']['prompt']);self.assertFalse(intent['request']['trading_authority'])
        self.assertEqual(intent['request']['source_neurobro_replies'][0]['text'],self.binding['original_reply'])
        self.assertEqual(self.job()['status'],'EVALUATION_OUTCOME_UNKNOWN')
        with self.assertRaisesRegex(ValueError,'JOB_NOT_READY|NO_REPLAY'):
            journal.claim_evaluation(self.directory,self.job()['id'],'bound-existing-room',now=NOW+101)
        receipt=self.receipt(intent)
        result=journal.complete_evaluation(self.directory,intent['operation'],native_receipt_reader=lambda _:receipt)
        self.assertEqual(result['jobs'][0]['status'],'EVALUATED')
        before=(self.directory/journal.FILENAME).read_bytes()
        again=journal.complete_evaluation(self.directory,intent['operation'],native_receipt_reader=lambda _:receipt)
        self.assertEqual(again,result)
        self.assertEqual((self.directory/journal.FILENAME).read_bytes(),before)
        state=journal.observe_calendar(self.directory,dict(positions=[self.fixture.position],complete=False))
        self.assertEqual(state['jobs'][0]['status'],'EVALUATED')
        self.assertEqual(state['jobs'][0]['experiment_outcome']['status'],'NOT_RUN')
    def test_wrong_room_changed_reply_or_missing_native_reader_cannot_complete(self):
        intent=journal.claim_evaluation(self.directory,self.job()['id'],'bound-existing-room',now=NOW+100)
        with self.assertRaisesRegex(ValueError,'READER_REQUIRED'):
            journal.complete_evaluation(self.directory,intent['operation'],native_receipt_reader=None)
        value=self.receipt(intent);value['room_id']='different'
        with self.assertRaisesRegex(ValueError,'NATIVE_RECEIPT_MISMATCH'):
            journal.complete_evaluation(self.directory,intent['operation'],native_receipt_reader=lambda _:value)
        value=self.receipt(intent);value['assistant_text']+='changed'
        with self.assertRaisesRegex(ValueError,'NATIVE_RECEIPT_MISMATCH'):
            journal.complete_evaluation(self.directory,intent['operation'],native_receipt_reader=lambda _:value)
        self.assertEqual(self.job()['status'],'EVALUATION_OUTCOME_UNKNOWN')
    def test_history_revision_invalidates_old_binding_and_evaluation_without_deleting_evidence(self):
        intent=journal.claim_evaluation(self.directory,self.job()['id'],'bound-existing-room',now=NOW+100)
        journal.complete_evaluation(self.directory,intent['operation'],native_receipt_reader=lambda _:self.receipt(intent))
        revised=copy.deepcopy(self.fixture.position);revised['pnl_usdt']='-2.10'
        state=journal.observe_calendar(self.directory,dict(positions=[revised],complete=False))
        self.assertEqual(state['jobs'][0]['status'],'BLOCKED_MISSING_EVIDENCE')
        with journal.snapshot_storage.snapshot_database(self.directory,journal.FILENAME)as db:
            self.assertEqual(db.execute("SELECT count(*) FROM evaluation_events WHERE kind='analyst_evaluation'").fetchone()[0],1)
    def test_inconclusive_neurobro_evaluation_never_claims_an_experiment_was_run(self):
        intent=journal.claim_evaluation(self.directory,self.job()['id'],'bound-existing-room',now=NOW+100)
        text='Sebagian alasan tidak dapat dipastikan dari bukti yang ada.\nPENILAIAN SETUP: BELUM CUKUP BUKTI'
        result=journal.complete_evaluation(self.directory,intent['operation'],native_receipt_reader=lambda _:self.receipt(intent,text))
        self.assertEqual(result['jobs'][0]['status'],'EVALUATED_INCONCLUSIVE')
        self.assertEqual(result['jobs'][0]['analyst_evaluation']['experiment_outcome']['status'],'NOT_RUN')


class NativeArchiveAdapterTests(unittest.TestCase):
    def setUp(self):
        from tests.test_binance_website_chart import WebsiteCaptureFixtureTests
        self.native=WebsiteCaptureFixtureTests();self.native.setUp();self.addCleanup(self.native.tearDown)
        self.result=self.native.capture(self.native.context,self.native.native_context,'HARUN_TEST','a'*64,
            archive_root=self.native.root)
        self.context=self.native.native_context
        self.request=dict(request_body=json.dumps(dict(operation='owned-history',marker='HARUN_TEST',context=self.context)),
            request_sha256='a'*64)
        self.response=dict(response_id='owned-reply',assistant_sha256='b'*64,
            chart_evidence=json.dumps(self.result['evidence']))
    def custody(self, operation, request, response, sidecar_sha, csvs):
        self.assertEqual(operation,'owned-history');self.assertRegex(sidecar_sha,'^[a-f0-9]{64}$')
        return dict(operation=operation,request_sha256=request['request_sha256'],response_id=response['response_id'],
            assistant_sha256=response['assistant_sha256'],native_intent_sha256='c'*64,native_completion_sha256='d'*64,
            context_sha256=evidence.digest(self.context),png_evidence_sha256=evidence.digest(json.loads(response['chart_evidence'])),
            csv_sha256={r['timeframe']:r['sha256']for r in csvs})
    def read(self, reader=None):
        return evidence.project_native_archive(SimpleNamespace(**self.native.namespace),self.native.root,
            self.request,self.response,paths=self.result['paths'],delivery_reader=reader or self.custody)
    def test_original_private_sidecar_png_and_csv_use_capture_time_without_fresh_market(self):
        from unittest.mock import patch
        with patch('time.time',return_value=4_000_000_000):
            value=self.read()
        self.assertEqual(value['context'],self.context);self.assertEqual(len(value['files']),4)
        self.assertEqual(value['delivery']['csv_sha256'],{r['timeframe']:r['sha256']for r in
            deliver_market_data_files(self.context,now=self.context['fetched_at'])})
        for item in value['files']:
            if item['kind']=='PNG':self.assertEqual(item['buffer'],Path(self.result['paths'][0]).read_bytes())
    def test_mutated_png_or_sidecar_or_missing_native_custody_reader_refuses(self):
        with self.assertRaisesRegex(ValueError,'CUSTODY_READER_REQUIRED'):
            evidence.project_native_archive(SimpleNamespace(**self.native.namespace),self.native.root,
                self.request,self.response,paths=self.result['paths'],delivery_reader=None)
        from agent.native_chart_capture import WEBSITE_SIDECAR
        sidecar=Path(self.result['paths'][0]).parent/WEBSITE_SIDECAR
        original=sidecar.read_bytes();value=json.loads(original);value['network_navigation_performed']=False
        sidecar.write_text(json.dumps(value))
        with self.assertRaises(ValueError):self.read()
        sidecar.write_bytes(original)
        path=Path(self.result['paths'][0]);path.write_bytes(path.read_bytes()+b'wrong')
        with self.assertRaises(ValueError):self.read()


if __name__=='__main__':unittest.main()
