"""Actual LINK Mini DONE survives a UI-only assistant UUID rerender.

The immutable fixtures are the original captured bytes. No model, browser Send,
order, clock change, receipt rewrite, or fabricated interpretation is needed.
"""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent.native_received_analysis import _project_operation
from tests import test_generic_screening_response_recovery as native_fixture
from worker import screening_response_recovery as recovery
from worker.core import Review

ROOT=Path(__file__).resolve().parent/'fixtures'
CAPTURE='screening_link_uuid_drift_20261111.json'
PAYLOAD='screening_link_payload_20261111.json'
CAPTURE_SHA='38a18c11ec073b1b45f662cf09586f0b07062c60bcd633682ba5b15d3201569b'
BODY_SHA='68b548ceb5180c90aa34b6fe3baba7b2588a7776bd5f57c86ef9ebe8125d119c'
ORIGINAL_REVIEW_SHA='92a5dfaef2886baa8bb7df436a6cdc957ff96997dcb4e5d33cb4a475e5ae53a9'
ORIGINAL_REQUEST='611528031a107e5536fe5951069a72f262bb547797c7951c88fbba1d79c36cd5'
CREATED=1794339174.01598


class ReceivedScreeningUuidSourceTests(unittest.TestCase):
    def setUp(self):
        self.raw=(ROOT/CAPTURE).read_bytes();self.body_raw=(ROOT/PAYLOAD).read_bytes()
        self.capture=json.loads(self.raw);self.proof=_project_operation(self.capture)
        self.payload={**json.loads(self.body_raw),'request_sha256':BODY_SHA}
        self.operation=self.payload['operation'];self.cycle=self.operation.rsplit(':screening:',1)[0]
        self.original=copy.deepcopy(self.proof['review_receipts'][0])
        self.rows=self.proof['rows'];self.now=self.proof['finished_at']+1

    def envelope(self,proof=None):
        proof=self.proof if proof is None else proof
        raw=recovery._wire(proof)
        return dict(kind=recovery.RECEIVED_KIND,operation=self.operation,request_sha256=BODY_SHA,
            operation_completed=False,observation=proof,observation_sha256=recovery._sha(raw),
            observation_bytes=len(raw.encode()),capture_name='received-analysis-observation-dcb2f526c401f323-'+CAPTURE_SHA+'.json',
            capture_sha256=CAPTURE_SHA,capture_bytes=len(self.raw))

    def worker(self):
        case=native_fixture.GenericScreeningResponseRecoveryTests();case.setUp();self.addCleanup(case.doCleanups)
        db=case.db
        for table in ('api_requests','research_browser_requests','robot_jobs','robot_cycles'):
            db.execute('DELETE FROM '+table)
        data=dict(target=1,queue=[],seen=[],screen=-1,replacements=0,round_symbols=[])
        db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',(self.cycle,self.operation[:10],0,'NEEDS_REVIEW',json.dumps(data)))
        db.execute('INSERT INTO robot_jobs VALUES(?,?,?,?,?,?)',(self.operation,self.cycle,'SCREENING',None,'NEEDS_REVIEW',None))
        db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?,?)',(self.operation,None,BODY_SHA,'NEEDS_REVIEW',CREATED,1,None,'NEUROBRO_BROWSER_OUTCOME_UNKNOWN'))
        db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',(self.operation,
            case.source.BROWSER_PROVIDER,case.source.BROWSER_CONTRACT,BODY_SHA,self.payload['marker'],self.body_raw.decode()))
        db.execute("UPDATE order_intents SET state='POSITION_PROTECTED'")
        return case

    def test_exact_production_bytes_and_only_completed_assistant_id_changed(self):
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(),CAPTURE_SHA);self.assertEqual(len(self.raw),10390)
        self.assertEqual(hashlib.sha256(self.body_raw).hexdigest(),BODY_SHA)
        old=self.original['value']['request']['conversation_context']['turns']
        differences=[(i,key)for i,(left,right)in enumerate(zip(old,self.rows))
            for key in set(left)|set(right)if left.get(key)!=right.get(key)or(key in left)!=(key in right)]
        self.assertEqual(differences,[(1,'id')])
        self.assertEqual(self.original['sha256'],ORIGINAL_REVIEW_SHA)
        self.assertNotEqual(recovery._digest(old),recovery._digest(self.rows))
        self.assertEqual(self.original['value']['request_id'],ORIGINAL_REQUEST)

    def test_complete_current_observation_reuses_original_done_without_new_model_identity(self):
        request,response=recovery._screening_review(self.proof,self.payload,self.rows,with_request=True)
        self.assertEqual(request['request_id'],ORIGINAL_REQUEST)
        self.assertEqual(response['decision'],dict(action='SCREENING',symbols=['LINKUSDT'],question=''))
        output,review=recovery._reviewed_screening_output(self.proof,self.payload,self.rows,require_approval=True)
        self.assertEqual(output,{'symbols':['LINKUSDT']});self.assertEqual(review['request_id'],ORIGINAL_REQUEST)
        artifact=recovery._received_interpretation_proof(self.proof,self.payload)
        self.assertEqual(artifact['review_receipt'],self.original)
        self.assertEqual(artifact['turns'],request['conversation_context']['turns'])
        self.assertNotEqual(artifact['turns'][1]['id'],self.rows[1]['id'])
        self.assertEqual(self.proof['review_receipts'][0],self.original)

    def test_actual_native_recorder_retires_stale_once_and_restart_does_not_write(self):
        case=self.worker();before=case.finances()
        phase=recovery.recover_received_screening(case.provider,self.envelope(),catalog={'LINKUSDT':{}},max_age=180,now=self.now)
        self.assertEqual(phase,'NOEXEC');self.assertEqual(case.finances(),before)
        api=dict(case.db.execute('SELECT * FROM api_requests WHERE operation=?',(self.operation,)).fetchone())
        self.assertEqual(api['state'],'RECEIVED_STALE_NOT_EXECUTED');self.assertIsNone(api['output']);self.assertEqual(api['attempts'],1)
        row=dict(case.db.execute('SELECT * FROM screening_response_recoveries WHERE operation=?',(self.operation,)).fetchone())
        retained=json.loads(row['proof']);self.assertEqual(retained['observation']['review_receipts'][0],self.original)
        self.assertEqual(retained['observation']['rows'],self.rows)
        native=case.db.execute('SELECT assistant_text,assistant_sha256 FROM research_browser_responses WHERE operation=?',(self.operation,)).fetchone()
        self.assertEqual(native['assistant_text'],self.rows[-1]['text']);self.assertEqual(native['assistant_sha256'],self.original['value']['reply_sha256'])
        self.assertEqual(dict(case.db.execute('SELECT * FROM robot_jobs WHERE operation=?',(self.operation,)).fetchone())['state'],'COMPLETE')
        with tempfile.TemporaryDirectory()as path:
            restarted=sqlite3.connect(str(Path(path)/'ledger.sqlite3'),isolation_level=None);restarted.row_factory=sqlite3.Row
            case.db.backup(restarted);self.addCleanup(restarted.close)
            provider=object.__new__(case.native.NeuroBrowser);provider.db=restarted;provider.chart_max_age=180
            provider.transport=provider.ask=provider.require_key=case.forbid
            changes=restarted.total_changes
            self.assertEqual(recovery.recover_received_screening(provider,self.envelope(),catalog={'LINKUSDT':{}},max_age=180,now=self.now+10),'NOEXEC')
            self.assertEqual(restarted.total_changes,changes)
            self.assertEqual(restarted.execute('SELECT COUNT(*) FROM screening_response_recoveries').fetchone()[0],1)
        self.assertEqual(self.proof['review_receipts'][0],self.original)

    def test_minimal_interpretation_artifact_cannot_authorize_changed_ids(self):
        minimal={key:self.proof[key]for key in('room','rows','finished_at','review_receipts')}
        self.assertIsNone(recovery._screening_review(minimal,self.payload,self.rows))
        original_rows=self.original['value']['request']['conversation_context']['turns']
        self.assertIsNotNone(recovery._screening_review(minimal,self.payload,original_rows))

    def test_incomplete_room_payload_text_or_observation_never_grants_projection(self):
        edits=(lambda p:p['rows'][0].update(text='changed question'),lambda p:p['rows'][1].update(text='changed author reply'),
            lambda p:p['room'].update(room_id='b'*32),lambda p:p.update(payload_sha256='0'*64),
            lambda p:p.update(stable_snapshot_reads=3),lambda p:p.update(stable_snapshot_reads=4.0),
            lambda p:p.update(schema_version=2.0),lambda p:p['room_witness'].update(same_loaded_room_verified=False),
            lambda p:p['rows'][1].update(complete=False),lambda p:p['rows'][1].update(id='invalid id'),
            lambda p:p['rows'][1].update(attachments=['foreign.csv']),lambda p:p.update(errors=[{'reason':'UNAVAILABLE'}]),
            lambda p:p.update(native_source_sha256='0'*64),lambda p:p.update(model_calls=1),
            lambda p:p.update(room_initial_state='UNBOUND'),lambda p:p['room_witness'].update(journal_url_matches=False),
            lambda p:p['review_receipts'][0].update(name='unbound-review.json'),
            lambda p:p['rendered_prompt_receipt'].update(rendered_sha256='0'*64),
            lambda p:p['rows'][1].update(extra_field=True))
        for edit in edits:
            proof=copy.deepcopy(self.proof);edit(proof)
            with self.subTest(edit=edit):
                self.assertIsNone(recovery._screening_review(proof,self.payload,proof['rows']))

    def test_modified_done_or_original_context_fields_are_rejected_not_reauthored(self):
        for change in(lambda v:v['model_operation'].update(state='UNKNOWN'),
                lambda v:v['request'].update(unrecognized_field=True),
                lambda v:v['request']['conversation_context']['turns'][0].update(text='changed original'),
                lambda v:v['request']['conversation_context']['turns'][1].update(id='invalid original id')):
            proof=copy.deepcopy(self.proof);entry=proof['review_receipts'][0];change(entry['value'])
            raw=recovery._wire(entry['value'],ascii=True);entry.update(sha256=recovery._sha(raw),bytes=len(raw.encode()))
            with self.subTest(change=change):
                try:result=recovery._screening_review(proof,self.payload,proof['rows'])
                except Review:result=None
                self.assertIsNone(result)

    def test_duplicate_original_or_current_assistant_ids_never_project(self):
        for original_duplicates in (True,False):
            proof=copy.deepcopy(self.proof);value=copy.deepcopy(self.original['value'])
            source=value['request']['conversation_context']['turns'];source.extend(copy.deepcopy(source))
            proof['rows'].extend(copy.deepcopy(proof['rows']))
            if original_duplicates:proof['rows'][3]['id']='different-current-assistant'
            else:source[3]['id']='different-original-assistant'
            proof['operation_scope'].update(latest_user=2,reply_row=3,user_rows=[0,2])
            proof['room_witness']['followup_user_rows']=[2]
            self.assertIsNone(recovery._received_screening_source_rows(proof,self.payload,proof['rows'],value))


if __name__=='__main__':unittest.main()
