"""Actual provider/ReceiptStore recovery with isolated journals, never a Send.

Simulate process loss after native COMPLETE and before any worker response row.
The only transport operation allowed by these fixtures is authenticated evidence
READ; no browser, model, exchange, service, upload or production DB is contacted.
"""
import copy
from datetime import datetime,timezone
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.native_chart_followup import ReceiptStore
from agent.native_evaluation_protocol import native_sha
from agent import native_evaluation_rpc as rpc
from tests.test_native_evaluation_protocol import prepared
from tests.test_trade_evaluation_evidence import NOW
from tests.test_trade_evaluation_recovery_proof import proof
from worker import neuroapi,owner_source_protocol as owner
from worker import trade_evaluation as journal
from worker.trade_evaluation_evidence import digest

ROOT=Path(__file__).resolve().parents[1]


class NativeCompletedResponseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.modules=patch.dict(sys.modules);self.modules.start();self.addCleanup(self.modules.stop)
        self.canonical=patch.object(neuroapi,'canonical_output',neuroapi.canonical_output)
        self.canonical.start();self.addCleanup(self.canonical.stop)
        self.cutoff=patch.object(owner,'load_owner_release_cutoff',return_value=
            datetime.fromtimestamp(NOW,timezone.utc).isoformat())
        self.cutoff.start();self.addCleanup(self.cutoff.stop)
        def load(name,path):
            spec=importlib.util.spec_from_file_location(name,ROOT/path)
            module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
            return module
        self.provider=load('worker.research_provider','vps_native/worker/research_provider.py')
        self.browser=load('worker.neurobrowser','vps_native/worker/neurobrowser.py')
        self.fixture,self.claim,self.args=prepared(self.directory)
        self.addCleanup(self.fixture.coordinator.db.close)
        self.archive=self.directory/'native-archive';self.archive.mkdir(mode=0o700)
        self.calls=[]

    def connect(self):
        self.db=sqlite3.connect(self.directory/'ledger.sqlite3',isolation_level=None)
        self.db.row_factory=sqlite3.Row;self.addCleanup(self.db.close)
        def transport(method,url,headers=None,body=None,timeout=15):
            self.calls.append((method,url,copy.deepcopy(body)))
            self.assertEqual((method,url),('POST','http://neurobro-browser:8790/v1/historical-evidence'))
            self.assertEqual(body['kind'],'COMPLETION')
            try:return 200,{},rpc.handle(body,archive_root=self.archive)
            except ValueError as error:return 409,{},dict(error=str(error))
        self.actor=self.browser.NeuroBrowser(SimpleNamespace(db=self.db),token='x'*32,transport=transport)
        return self.actor

    def pending_and_native_complete(self, *, state='NEEDS_REVIEW', mutate=None, unfinished=False):
        self.connect()
        body,serialized,wire=self.provider.request_body(self.args['operation'],self.args['prompt'],
            self.args['output_schema'],self.args['context'])
        self.db.execute('INSERT INTO api_requests(operation,idempotency,body_hash,state,created,attempts,output,failure_code) VALUES(?,?,?,?,?,?,?,?)',
            (body['operation'],None,wire,state,NOW+101,1,None,'NEUROBRO_BROWSER_OUTCOME_UNKNOWN'))
        self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',
            (body['operation'],body['provider'],body['contract_version'],wire,body['marker'],serialized))
        _,completion=proof();completion.update(operation=body['operation'],request_sha256=wire)
        material=self.claim['request']['historical_materials'][0]
        previous=None
        for index,record in enumerate(completion['chain']):
            value=record['intent']
            value.update(operation=body['operation'],request_sha256=wire,marker=body['marker'],
                input_request_sha256=self.claim['request_sha256'],input_evidence_sha256=self.claim['input_evidence_sha256'],
                room_id=self.claim['request']['room_id'],historical_access_sha256=digest([material]),
                previous_followup_intent_sha256=previous)
            if index==0:
                value['reused_materials']=[dict(material_sha256=digest(material),
                    native_completion_sha256=material['delivery_custody']['native_completion_sha256'],
                    assistant_sha256=material['delivery_custody']['assistant_sha256'],
                    attachment_names=[r['name'] for r in material['files']])]
            else:
                value['source_chain_sha256']=digest(completion['chain'][:index])
                value['native_received_at']=NOW+120
                value['native_result'].update(operation=body['operation'],marker=body['marker'],request_sha256=wire)
                if mutate:mutate(value)
            record['complete']=dict(value,intent_sha256=native_sha(value),
                response_id=record['complete']['response_id'],final_reply_sha256=record['complete']['final_reply_sha256'])
            previous=native_sha(value)
        store=ReceiptStore(self.archive,monotonic=lambda:1)
        for index,record in enumerate(completion['chain']):
            store(stage='INTENT',receipt=record['intent'],deadline=2)
            if not(unfinished and index==len(completion['chain'])-1):
                store(stage='COMPLETE',receipt=record['complete'],deadline=2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM research_browser_responses').fetchone()[0],0)
        # A REAL new SQLite connection/provider object simulates worker restart.
        self.db.close();self.connect()
        return body,wire

    def test_worker_restart_recovers_native_complete_without_local_response_or_send(self):
        self.pending_and_native_complete()
        with patch.object(self.browser.time,'time',return_value=NOW+500):
            result=self.actor.evaluation_consumer.advance(SimpleNamespace(neuro=self.actor,allowed=lambda _:True),'2026-11-01')
        self.assertEqual(result['state'],'EVALUATED');self.assertTrue(result['recovered'])
        row=self.db.execute('SELECT * FROM research_browser_responses').fetchone()
        self.assertEqual(row['response_id'],'native-final-response');self.assertEqual(row['received_at'],NOW+120)
        self.assertEqual(row['assistant_text'],proof()[1]['chain'][-1]['intent']['native_result']['assistant_text'])
        self.assertEqual(self.db.execute('SELECT state FROM api_requests').fetchone()[0],'COMPLETE')
        self.assertEqual(self.provider.source_evidence(self.db,self.args['operation'])['source_type'],'NEUROBRO_BROWSER')
        self.assertEqual(journal.read_status(self.directory)['jobs'][0]['status'],'EVALUATED_INCONCLUSIVE')
        self.assertEqual(len(self.calls),1)
        # Reading the same actual completion is idempotent, without another row
        # or any ask/session/Send transport operation.
        with patch.object(self.browser.time,'time',return_value=NOW+501):
            self.actor.evaluation_consumer.receipt_reader(self.args['operation'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM research_browser_responses').fetchone()[0],1)
        self.assertEqual(len(self.calls),2)

    def test_hard_kill_pending_state_is_also_recovered_from_same_native_completion(self):
        self.pending_and_native_complete(state='PENDING')
        with patch.object(self.browser.time,'time',return_value=NOW+500):
            receipt=self.actor.evaluation_consumer.receipt_reader(self.args['operation'])
        self.assertEqual(receipt['response_id'],'native-final-response')
        self.assertEqual(self.db.execute('SELECT attempts,state FROM api_requests').fetchone()[:],(1,'COMPLETE'))
        self.assertEqual(len(self.calls),1)

    def test_wrong_request_room_or_raw_reply_cannot_create_a_local_response(self):
        mutations=(lambda final:final['native_result'].update(request_sha256='f'*64),
            lambda final:final.update(room_id='f'*32),
            lambda final:final['native_result'].update(assistant_text='invented replacement'))
        # Independent native receipt directories preserve exact immutable files.
        for index,mutation in enumerate(mutations):
            with self.subTest(case=index):
                if index:
                    self.db.close();(self.directory/'ledger.sqlite3').unlink()
                    self.archive=self.directory/('native-archive-'+str(index));self.archive.mkdir(mode=0o700)
                self.pending_and_native_complete(mutate=mutation)
                with patch.object(self.browser.time,'time',return_value=NOW+500):
                    result=self.actor.evaluation_consumer.advance(SimpleNamespace(neuro=self.actor,allowed=lambda _:True),'2026-11-01')
                self.assertEqual(result['state'],'EVALUATION_OUTCOME_UNKNOWN');self.assertFalse(result['replayed'])
                self.assertEqual(self.db.execute('SELECT count(*) FROM research_browser_responses').fetchone()[0],0)
                self.assertEqual(self.db.execute('SELECT state,output FROM api_requests').fetchone()[:],('NEEDS_REVIEW',None))
        self.assertEqual(len(self.calls),3)

    def test_unfinished_native_final_remains_unknown_and_never_replays(self):
        self.pending_and_native_complete(unfinished=True)
        for _ in range(2):
            with patch.object(self.browser.time,'time',return_value=NOW+500):
                result=self.actor.evaluation_consumer.advance(SimpleNamespace(neuro=self.actor,allowed=lambda _:True),'2026-11-01')
            self.assertEqual(result['state'],'EVALUATION_OUTCOME_UNKNOWN');self.assertFalse(result['replayed'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM research_browser_responses').fetchone()[0],0)
        self.assertEqual(journal.read_status(self.directory)['jobs'][0]['status'],'EVALUATION_OUTCOME_UNKNOWN')
        self.assertEqual(len(self.calls),2)
