"""Offline once-claim closure fixtures; no live proof, browser, or provider I/O."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tests.test_native_evaluation_protocol import prepared, ROOM, TEXT
from tests.test_trade_evaluation_evidence import NOW
from worker import trade_evaluation as journal
from worker import trade_evaluation_evidence as evidence
from worker.trade_evaluation_runtime import EvaluationConsumer


def not_sent_proof(claim):
    """Synthetic equivalents of retained pre-compose failure and room parity."""
    wire='6'*64
    failure=dict(schema_version=1,operation=claim['operation'],request_sha256=wire,
        phase='INITIAL',at=claim['reserved_at']+2,accepted_reply_sha256=None,
        followup_intent_sha256=[])
    failure['chain']=[{'class':'NotSubmitted','reason':'UNAVAILABLE','frames':[
        dict(file='/app/agent/native_browser_hook.py',function='_ask',line=825)]},
        {'class':'DialogueError','reason':'DIALOGUE_PRICE_PROPOSAL_REFUSED','frames':[
            dict(file='/app/agent/dialogue_client.py',function='compose',line=35)]}]
    parity=dict(schema_version=1,operation=claim['operation'],request_sha256=wire,
        phase='PARITY',at=claim['reserved_at']+1,parity_verified=True,row_count=34,
        native_source_sha256='3'*64)
    after=dict(parity,phase='FAILURE',at=claim['reserved_at']+3)
    return dict(protocol=journal.NOT_SENT_PROTOCOL,operation=claim['operation'],job_id=claim['job_id'],
        request_sha256=claim['request_sha256'],input_evidence_sha256=claim['input_evidence_sha256'],
        room_id=claim['request']['room_id'],sent_this_request=False,wire_request_sha256=wire,
        evidence_sha256='e'*64,source_sha256={'agent/native_browser_hook.py':'a'*64},
        observed_at=claim['reserved_at']+4,native_failure=failure,
        room_parity=dict(before=parity,after=after),
        worker=dict(api=None,request_present=False,response_present=False),
        browser=dict(scan_complete=True,send_intents=[],marker_seen=False))


def sent_receipt(claim):
    return dict(protocol=evidence.PROTOCOL,phase='POST_TRADE_EVALUATION',provider='NEUROBRO_WEB',
        operation=claim['operation'],request_sha256=claim['request_sha256'],
        input_evidence_sha256=claim['input_evidence_sha256'],room_id=claim['request']['room_id'],
        sent_this_request=True,received_at=claim['reserved_at']+1,response_id='actual-native-fixture',
        source_evidence_sha256='f'*64,assistant_text=TEXT,assistant_sha256=hashlib.sha256(TEXT.encode()).hexdigest())


class EvaluationNotSentClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.fixture,self.claim,_=prepared(self.directory)
        self.addCleanup(self.fixture.coordinator.db.close)
        self.proof=not_sent_proof(self.claim)

    def events(self):
        with journal.snapshot_storage.snapshot_database(self.directory,journal.FILENAME)as db:
            return db.execute('SELECT * FROM evaluation_events ORDER BY seq').fetchall()

    def job(self,job_id=None):
        return next(row for row in journal.read_status(self.directory)['jobs']
            if row['id']==(job_id or self.claim['job_id']))

    def add_job(self,key):
        row=copy.deepcopy(self.fixture.position)
        row.update(id=key,closed_at='2026-11-02T00:00:00Z')
        journal.observe_calendar(self.directory,dict(positions=[row]))
        with journal.snapshot_storage.snapshot_database(self.directory,journal.FILENAME)as db:
            binding=copy.deepcopy(journal._latest(db,'binding')[self.fixture.position['id']])
        binding.update(trade_id=key,closed_position_sha256=evidence.digest(row))
        binding['binding_sha256']=evidence.digest({k:v for k,v in binding.items()if k!='binding_sha256'})
        journal.attach_evidence(self.directory,binding)
        return 'post-trade:'+key

    def test_positive_proof_appends_terminal_outcome_preserving_claim_and_accepted_history(self):
        accepted_job=self.add_job('accepted')
        accepted=journal.claim_evaluation(self.directory,accepted_job,ROOM,now=NOW+101)
        journal.complete_evaluation(self.directory,accepted['operation'],
            native_receipt_reader=lambda _:sent_receipt(accepted))
        before=self.events();monthly=journal.read_status(self.directory)['monthly_performance']
        reader=Mock(return_value=self.proof)
        result=journal.close_evaluation_not_sent(self.directory,self.claim['operation'],native_proof_reader=reader)
        reader.assert_called_once_with(self.claim['operation'])
        after=self.events();self.assertEqual(after[:-1],before)
        self.assertEqual(after[-1][1:3],('evaluation_not_sent',self.claim['operation']))
        self.assertEqual(result['unresolved_evaluations'],[])
        self.assertEqual(result['monthly_performance'],monthly)
        closed=self.job();self.assertEqual(closed['status'],'EVALUATION_NOT_SENT')
        self.assertNotIn('analyst_evaluation',closed)
        self.assertEqual(closed['experiment_outcome']['status'],'NOT_RUN')
        self.assertEqual(closed['evaluation_not_sent']['proof'],self.proof)
        self.assertEqual(self.job(accepted_job)['analyst_evaluation']['native_receipt'],sent_receipt(accepted))
        with journal.snapshot_storage.snapshot_database(self.directory,journal.FILENAME)as db:
            self.assertEqual(journal._latest(db,'evaluation_intent')[self.claim['job_id']],self.claim)
        self.assertIsNone(journal.read_evaluation_claim(self.directory,self.claim['operation']))
        with self.assertRaisesRegex(ValueError,'JOB_NOT_READY|NO_REPLAY'):
            journal.claim_evaluation(self.directory,self.claim['job_id'],ROOM,now=NOW+102)
        receipt_reader=Mock(return_value=sent_receipt(self.claim))
        with self.assertRaisesRegex(ValueError,'ALREADY_CLOSED_NOT_SENT'):
            journal.complete_evaluation(self.directory,self.claim['operation'],native_receipt_reader=receipt_reader)
        receipt_reader.assert_not_called()
        refreshed=journal.observe_calendar(self.directory,dict(positions=[self.fixture.position]))
        self.assertEqual(next(j for j in refreshed['jobs']if j['id']==self.claim['job_id'])['status'],'EVALUATION_NOT_SENT')
        # A historical revision cannot project the old closure as a current evaluation.
        revised=dict(self.fixture.position,pnl_usdt='-3')
        journal.observe_calendar(self.directory,dict(positions=[revised]))
        self.assertEqual(self.job()['status'],'BLOCKED_MISSING_EVIDENCE')
        self.assertEqual(journal.read_status(self.directory)['unresolved_evaluations'],[])

    def test_known_sent_receipt_uses_existing_recovery_and_cannot_be_closed_not_sent(self):
        neuro=SimpleNamespace(require_key=Mock(),ask=Mock())
        reader=Mock(return_value=sent_receipt(self.claim))
        consumer=EvaluationConsumer(self.directory,archive_reader=Mock(),room_reader=Mock(),native_receipt_reader=reader)
        result=consumer.advance(SimpleNamespace(neuro=neuro,allowed=lambda _:True),'2026-11-01')
        self.assertEqual(result['state'],'EVALUATED');self.assertTrue(result['recovered'])
        self.assertEqual(self.job()['status'],'EVALUATED_INCONCLUSIVE')
        reader.assert_called_once_with(self.claim['operation']);neuro.ask.assert_not_called()
        before=self.events();proof_reader=Mock(return_value=self.proof)
        with self.assertRaisesRegex(ValueError,'ALREADY_COMPLETED'):
            journal.close_evaluation_not_sent(self.directory,self.claim['operation'],native_proof_reader=proof_reader)
        proof_reader.assert_not_called();self.assertEqual(self.events(),before)
        self.assertFalse(any(row[1]=='evaluation_not_sent'for row in before))

    def test_insufficient_or_conflicting_evidence_stays_unknown_without_any_send(self):
        changes=[lambda p:p.update(operation='post-trade-evaluation:'+'0'*32),
            lambda p:p.update(job_id='post-trade:foreign'),
            lambda p:p.update(request_sha256='0'*64),
            lambda p:p.update(input_evidence_sha256='0'*64),
            lambda p:p.update(room_id='b'*32),lambda p:p.update(sent_this_request=True),
            lambda p:p.update(source_sha256={}),lambda p:p.update(evidence_sha256='invalid'),
            lambda p:p['worker'].update(request_present=True),
            lambda p:p['worker'].update(response_present=True),
            lambda p:p['browser'].update(scan_complete=False),
            lambda p:p['browser'].update(marker_seen=True),
            lambda p:p['browser'].update(send_intents=['actual-send-intent']),
            lambda p:p['native_failure'].update(accepted_reply_sha256='1'*64),
            lambda p:p['native_failure'].update(followup_intent_sha256=['1'*64]),
            lambda p:p['native_failure']['chain'][1].update(frames=[]),
            lambda p:p['room_parity']['after'].update(row_count=35),
            lambda p:p['room_parity']['after'].update(native_source_sha256='4'*64),
            lambda p:p['room_parity']['after'].update(parity_verified=False),
            lambda p:p['room_parity']['before'].update(request_sha256='5'*64),
            lambda p:p['room_parity']['before'].update(at=self.claim['reserved_at']-1),
            lambda p:p.update(observed_at=self.claim['reserved_at']+1)]
        before=(self.directory/journal.FILENAME).read_bytes()
        for index,change in enumerate(changes):
            with self.subTest(case=index):
                value=copy.deepcopy(self.proof);change(value)
                with self.assertRaisesRegex(ValueError,'NOT_SENT_PROOF'):
                    journal.close_evaluation_not_sent(self.directory,self.claim['operation'],
                        native_proof_reader=lambda _:value)
                self.assertEqual((self.directory/journal.FILENAME).read_bytes(),before)
        for reader in (None,Mock(return_value=None),Mock(side_effect=ValueError('EVIDENCE_UNAVAILABLE'))):
            with self.subTest(reader=reader),self.assertRaises(ValueError):
                journal.close_evaluation_not_sent(self.directory,self.claim['operation'],native_proof_reader=reader)
        neuro=SimpleNamespace(require_key=Mock(),ask=Mock())
        consumer=EvaluationConsumer(self.directory,archive_reader=Mock(),room_reader=Mock(),
            native_receipt_reader=Mock(side_effect=ValueError('RECEIPT_NOT_FOUND')))
        result=consumer.advance(SimpleNamespace(neuro=neuro,allowed=lambda _:True),'2026-11-01')
        self.assertEqual(result['state'],'EVALUATION_OUTCOME_UNKNOWN');neuro.ask.assert_not_called()
        self.assertEqual(self.job()['status'],'EVALUATION_OUTCOME_UNKNOWN')
        self.assertEqual((self.directory/journal.FILENAME).read_bytes(),before)

    def test_restart_reuses_cached_closure_and_existing_consumer_advances_only_next_job(self):
        journal.close_evaluation_not_sent(self.directory,self.claim['operation'],native_proof_reader=lambda _:self.proof)
        before=(self.directory/journal.FILENAME).read_bytes()
        # A separate interpreter reopens the durable journal without any proof reader.
        script='''import json,sys
from worker import trade_evaluation as journal
state=journal.close_evaluation_not_sent(sys.argv[1],sys.argv[2],native_proof_reader=None)
print(json.dumps([state['unresolved_evaluations'],state['jobs'][0]['status'],
    journal.read_evaluation_claim(sys.argv[1],sys.argv[2])]))
'''
        restarted=subprocess.run([sys.executable,'-c',script,str(self.directory),self.claim['operation']],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(restarted.stdout),[[],'EVALUATION_NOT_SENT',None])
        self.assertEqual((self.directory/journal.FILENAME).read_bytes(),before)
        next_job=self.add_job('next')
        neuro=SimpleNamespace(require_key=Mock(),ask=Mock(side_effect=ValueError('SIMULATED_UNCERTAIN_SEND')))
        receipt=Mock(side_effect=ValueError('RECEIPT_NOT_FOUND'))
        consumer=EvaluationConsumer(self.directory,archive_reader=Mock(),room_reader=lambda:ROOM,native_receipt_reader=receipt)
        actor=SimpleNamespace(neuro=neuro,allowed=lambda _:True)
        result=consumer.advance(actor,'2026-11-02')
        next_operation=journal.evaluation_operation(self.directory,next_job)
        self.assertNotEqual(next_operation,self.claim['operation'])
        self.assertEqual(result['operation'],next_operation)
        neuro.ask.assert_called_once();self.assertEqual(neuro.ask.call_args.args[0],next_operation)
        consumer.advance(actor,'2026-11-02')
        neuro.ask.assert_called_once();receipt.assert_called_once_with(next_operation)
        self.assertIsNone(journal.read_evaluation_claim(self.directory,self.claim['operation']))
        self.assertEqual(self.job()['status'],'EVALUATION_NOT_SENT')


if __name__=='__main__':unittest.main()
