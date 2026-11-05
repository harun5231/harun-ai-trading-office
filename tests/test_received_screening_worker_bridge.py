"""Actual browser observation/review receipts enter the native recorder seam."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from tests import test_received_screening_rounds as browser_fixture
from tests import test_generic_screening_response_recovery as worker_fixture
from worker.screening_response_recovery import recover_received_screening


class ReceivedScreeningWorkerBridgeTests(unittest.TestCase):
    def test_cold_native_worker_tree_recovers_without_browser_helper_package(self):
        worker=worker_fixture.GenericScreeningResponseRecoveryTests()
        worker.setUp();self.addCleanup(worker.doCleanups)
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory()as directory:
            path=Path(directory)
            shutil.copytree(root/'worker',path/'worker',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            for name in ('neurobrowser.py','research_provider.py'):
                shutil.copyfile(root/'vps_native/worker'/name,path/'worker'/name)
            (path/'agent').mkdir()
            for name in ('__init__.py','neurobro_reply.py','native_pending_protocol.py',
                    'native_chart_history.py','native_evaluation_protocol.py','binance_website_chart.py'):
                shutil.copyfile(root/'agent'/name,path/'agent'/name)
            (path/'fixture.json').write_text(json.dumps(dict(envelope=worker.envelope,catalog=worker.catalog,now=worker_fixture.NOW)))
            (path/'fixture.sql').write_text('\n'.join(worker.db.iterdump()))
            script='''import json,sqlite3,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);root=Path(sys.argv[1])
import worker.owner_source_protocol as owner
owner.load_owner_release_cutoff=lambda:'2026-11-04T00:00:00Z'
from worker.neurobrowser import NeuroBrowser
from worker.screening_response_recovery import recover_received_screening
data=json.loads((root/'fixture.json').read_text());db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row
db.executescript((root/'fixture.sql').read_text());provider=object.__new__(NeuroBrowser);provider.db=db;provider.chart_max_age=180
def forbidden(*args,**kwargs):raise AssertionError('No provider/order calls')
provider.transport=provider.ask=provider.require_key=forbidden
phase=recover_received_screening(provider,data['envelope'],catalog=data['catalog'],max_age=180,now=data['now'])
assert phase=='RESUME_CACHED'
assert 'agent.native_screening_recovery'not in sys.modules
assert not(root/'agent/native_screening_recovery.py').exists()
print('NATIVE_WORKER_RECOVERED_WITHOUT_BROWSER_HELPERS')
'''
            result=subprocess.run([sys.executable,'-I','-c',script,str(path)],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),'NATIVE_WORKER_RECOVERED_WITHOUT_BROWSER_HELPERS')

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
