"""Actual browser observation/review receipts enter the native recorder seam."""
import json
import time
import unittest

from tests import test_received_screening_rounds as browser_fixture
from tests import test_generic_screening_response_recovery as worker_fixture
from worker.screening_response_recovery import recover_received_screening


class ReceivedScreeningWorkerBridgeTests(unittest.TestCase):
    def test_actual_scoped_browser_and_done_review_retire_stale_round_without_web_or_order_replay(self):
        browser=browser_fixture.ReceivedScreeningRoundsTests('test_round9_reads_actual_owned_reply_in_same_bound_room_without_model_or_web_send')
        browser.setUp();self.addCleanup(browser.doCleanups)
        worker=worker_fixture.GenericScreeningResponseRecoveryTests()
        worker.setUp();self.addCleanup(worker.doCleanups)
        body={key:value for key,value in browser.payload.items()if key!='request_sha256'}
        serialized=worker.source.encode(body);wire=worker.source.sha(serialized)
        self.assertEqual(wire,browser.payload['request_sha256'])
        operation=body['operation']
        worker.db.execute('UPDATE api_requests SET body_hash=?,created=? WHERE operation=?',(wire,time.time()-1200,operation))
        worker.db.execute('UPDATE research_browser_requests SET request_sha256=?,marker=?,request_body=? WHERE operation=?',
            (wire,body['marker'],serialized,operation))
        data=json.loads(worker.row('robot_cycles')['data']);data['seen']=list(body['context']['excluded_symbols'])
        worker.db.execute('UPDATE robot_cycles SET data=?',(json.dumps(data),))
        before=worker.finances()
        client=browser.install_review();browser.review(browser.invoke())
        envelope=browser.invoke()
        phase=recover_received_screening(worker.provider,envelope,catalog={'SOLUSDT':{}},max_age=180,now=time.time())
        self.assertEqual(phase,'NOEXEC');self.assertEqual(client.calls,1)
        self.assertEqual(worker.row('api_requests')['state'],'RECEIVED_STALE_NOT_EXECUTED')
        self.assertIsNone(worker.row('api_requests')['output'])
        recovered=json.loads(worker.row('robot_cycles')['data'])
        self.assertEqual(recovered['screen'],9);self.assertEqual(recovered['round_symbols'],['AVAXUSDT'])
        self.assertEqual(recovered['seen'],data['seen']);self.assertEqual(worker.finances(),before)
        self.assertEqual(browser.case.raw.messages,browser.before)


if __name__=='__main__':unittest.main()
