"""Owner default limits honor existing reservations, with no provider calls."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from agent import dialogue_service as canonical_service
from agent.memory import Memory
from tests.test_mini_dialogue_profile import retained_module
from tests.test_mini_http_cooldown import CompletedBody


class MiniDefaultBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.memory=Memory(self.root/'agent.sqlite3');self.key=self.root/'model.key'
        self.key.write_text('unit-model-key-value-01234567890123456789');self.key.chmod(0o600)
        self.module=retained_module()
        self.clock=[datetime(2026,11,9,16,59,30,tzinfo=timezone.utc).timestamp()]
        self.engine=self.module.DialogueEngine(self.memory,self.key,clock=lambda:self.clock[0])

    def tearDown(self):self.memory.close();self.temporary.cleanup()

    def request(self,index,*,phase='SCREENING',reply='BTCUSDT'):
        return {'schema_version':1,'request_id':hashlib.sha256(str(index).encode()).hexdigest(),
            'conversation_id':'offline-budget:'+str(index),'phase':phase,'reply':reply,
            'reply_sha256':hashlib.sha256(reply.encode()).hexdigest(),'market_context':None,
            'gate':{'robot_on':True,'native_dispatch_verified':True},'deadline_epoch':self.clock[0]+80}

    def test_both_real_factory_paths_use_unchanged_default_limits(self):
        expected={'max_requests':48,'max_input_tokens':200000,'max_output_tokens':24000}
        canonical=canonical_service.DialogueEngine(self.memory,self.key)
        self.assertEqual(canonical.model.limits,expected)
        self.assertEqual(self.engine.model.limits,expected)
        self.assertEqual((self.engine.model.timeout,self.engine.model.max_output_tokens),(45,1024))

    def test_historical_377_reservations_are_unchanged_and_block_before_any_api(self):
        original=('2026-11-09',377,193000,23000)
        self.memory.db.execute('INSERT INTO budgets VALUES(?,?,?,?)',original)
        opener=SimpleNamespace(open=Mock(side_effect=AssertionError('historical limit must prevent API')))
        with patch('agent.llm.urllib.request.build_opener',return_value=opener):
            for index in range(2):
                with self.assertRaisesRegex(self.module.DialogueError,'DIALOGUE_MODEL_NOT_SENT'):
                    self.engine.process(self.request(index))
        opener.open.assert_not_called()
        self.assertEqual(tuple(self.memory.db.execute('SELECT * FROM budgets').fetchone()),original)
        rows=self.memory.db.execute('SELECT result FROM operations').fetchall()
        self.assertEqual([json.loads(row[0]) for row in rows],
            [{'_agent_not_sent':'DAILY_MODEL_BUDGET_EXHAUSTED'}]*2)
        self.assertEqual(self.memory.db.execute("SELECT COUNT(*) FROM metadata WHERE key LIKE 'dialogue-model-api-%'").fetchone()[0],0)

    def test_next_wib_day_uses_new_row_without_resetting_or_deleting_historical_count(self):
        original=('2026-11-09',377,193000,23000)
        self.memory.db.execute('INSERT INTO budgets VALUES(?,?,?,?)',original)
        self.clock[0]+=60 # 00:00:30 WIB, not a production clock change.
        result={'status':'completed','model':'gpt-5-mini','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps({'action':'SCREENING','symbols':['BTCUSDT'],'question':''})}]}]}
        opener=SimpleNamespace(open=Mock(return_value=CompletedBody(json.dumps(result).encode())))
        with patch('agent.llm.urllib.request.build_opener',return_value=opener):
            self.assertEqual(self.engine.process(self.request('next-day'))['decision']['action'],'SCREENING')
        self.assertEqual(opener.open.call_count,1) # Injected local fake, never a provider.
        self.assertEqual(tuple(self.memory.db.execute('SELECT * FROM budgets WHERE day=?',('2026-11-09',)).fetchone()),original)
        new=tuple(self.memory.db.execute('SELECT * FROM budgets WHERE day=?',('2026-11-10',)).fetchone())
        self.assertEqual((new[0],new[1],new[3]),('2026-11-10',1,1024))
        self.assertLess(new[2],200000)

    def test_literal_screening_and_readonly_health_need_no_budget_even_if_exhausted(self):
        from worker.prompts import SCREENING
        original=('2026-11-09',377,193000,23000)
        self.memory.db.execute('INSERT INTO budgets VALUES(?,?,?,?)',original)
        opener=SimpleNamespace(open=Mock(side_effect=AssertionError('literal/health have no paid calls')))
        with patch('agent.llm.urllib.request.build_opener',return_value=opener):
            value=self.engine.process(self.request('literal',phase='COMPOSE_SCREENING',reply=SCREENING))
            self.assertEqual(value['decision']['question'],SCREENING)
            self.assertFalse(self.engine.model.evidence()['verified'])
            health=object.__new__(self.module.DialogueHealth)
            health.dialogue=SimpleNamespace(engine=self.engine,runtime_ready=lambda:True)
            health.generation='a'*32
            self.assertFalse(health.observe()['provider_contact_performed'])
        opener.open.assert_not_called()
        self.assertEqual(tuple(self.memory.db.execute('SELECT * FROM budgets').fetchone()),original)
        self.assertEqual(self.memory.db.execute("SELECT COUNT(*) FROM metadata WHERE key LIKE 'dialogue-model-api-%'").fetchone()[0],0)


if __name__=='__main__':unittest.main()
