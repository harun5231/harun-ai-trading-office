"""Offline one-slot maintenance: proof-bound SQLite, no live gateway or replay."""
import fcntl
import importlib.util
import json
import os
import sqlite3
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

import test_order_pipeline as pipeline
from worker.core import Ledger, now, day
from worker.account_state import account_state

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('seed_one_slot', ROOT / 'deploy/seed_one_slot.py')
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)

class SeedOneSlotTests(unittest.TestCase):
    def fixture(self):
        fixture=pipeline.OrderPipelineTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.ledger.db.close();root=Path(fixture.temporary.name);trading=root/'trading';trading.mkdir(mode=0o700)
        target=trading/'ledger.sqlite3';fixture.path.rename(target);fixture.path=target;fixture.ledger=Ledger(target);fixture.make()
        fixture.on();self.assertEqual(fixture.robot.tick()['bot_status'],'WAITING');self.assertEqual(fixture.robot.tick()['bot_status'],'READY_FOR_EXECUTION')
        gateway=fixture.connected()
        def unknown(intent):raise RuntimeError('private previous transport exception')
        gateway.on_submit=unknown;self.assertEqual(fixture.robot.tick()['bot_status'],'NEEDS_REVIEW');fixture.store.configure({'robot_on':False})
        old=dict(fixture.intent());candidate=dict(fixture.ledger.db.execute('SELECT * FROM robot_candidates WHERE id=?',(old['candidate_id'],)).fetchone())
        settings=[dict(r) for r in fixture.ledger.db.execute('SELECT enabled,risk FROM robot_settings WHERE id=1')]
        folder=trading/tool.RELATIVE;folder.mkdir(mode=0o700,parents=True);backup=folder/'ledger.before.sqlite3'
        with sqlite3.connect(backup) as saved:fixture.ledger.db.backup(saved)
        backup.chmod(0o600);cid=json.loads(old['payload'])['client_order_id'];stamp=int(time.time()*1000)
        proof=dict(status='ABSENCE_VERIFIED',client_order_id=cid,intent_sha256=tool.digest(old['payload'].encode()),snapshot_sha256=tool.digest(tool.packed({'order':old,'candidate':candidate,'settings':settings})),backup=str(backup),proofs=[dict(server_ms=stamp+n,start_ms=stamp+n-72*3600*1000,btc_order_rows=0,btc_trade_rows=0,btc_open_orders=0,btc_open_algos=0,btc_active_positions=0) for n in (0,1)])
        proof['proof_sha256']=tool.digest(tool.packed(proof));proof_file=folder/'proof.json';proof_file.write_text(json.dumps(proof));proof_file.chmod(0o600)
        pins=dict(client=cid,payload=proof['intent_sha256'],snapshot=proof['snapshot_sha256'],proof=proof['proof_sha256'])
        fixture.ledger.db.execute("UPDATE order_intents SET state='REJECTED',failure_code=?,updated=? WHERE id=?",(tool.REASON,now(),old['id']))
        fixture.ledger.db.execute("UPDATE robot_candidates SET status='REJECTED',failure_code=? WHERE id=?",(tool.REASON,candidate['id']))
        fixture.on();gateway.on_submit=lambda intent:gateway.observation(intent)
        self.assertEqual(fixture.robot.tick()['bot_status'],'READY_FOR_EXECUTION');self.assertEqual(fixture.robot.tick()['bot_status'],'ENTRY_PENDING')
        self.assertEqual(fixture.robot.tick()['wait_reason'],'ROBOT_CYCLE_COMPLETE')
        return fixture,root,pins,proof_file

    def test_one_new_normal_cycle_fresh_btc_analysis_bounded_and_old_entry_never_replayed(self):
        f,root,pins,proof_file=self.fixture();db=f.ledger.db
        old_cycle=dict(db.execute('SELECT * FROM robot_cycles').fetchone());before={name:[tuple(r) for r in db.execute('SELECT * FROM '+name)] for name in ('order_intents','robot_candidates','robot_jobs','api_requests','robot_settings','robot_entry_receipts')}
        files=set(root.rglob('*'));self.assertEqual(tool.seed(root,pins=pins)['status'],'REPAIR_READY');self.assertEqual(set(root.rglob('*')),files)
        report=tool.seed(root,True,pins);self.assertEqual(report['status'],'ONE_REPAIR_CYCLE_SEEDED')
        self.assertEqual(dict(db.execute('SELECT * FROM robot_cycles WHERE id=?',(old_cycle['id'],)).fetchone()),old_cycle)
        self.assertEqual(before,{name:[tuple(r) for r in db.execute('SELECT * FROM '+name)] for name in before})
        row=db.execute('SELECT * FROM robot_cycles WHERE entry_epoch=1').fetchone();data=json.loads(row['data'])
        self.assertEqual((data['target'],data['queue'],data['seen'],data['screen'],data['replacements']),(1,[],[],-1,0))
        self.assertEqual(data['repair_origin'],pins['proof']);self.assertEqual(tool.seed(root,True,pins)['status'],'ALREADY_SCHEDULED')
        f.screens=[['BTCUSDT']];self.assertEqual(f.robot.tick()['wait_reason'],'SCREENING_COMPLETE_ANALYSIS_PENDING');self.assertEqual(f.robot.tick()['bot_status'],'READY_FOR_EXECUTION');self.assertEqual(f.robot.tick()['bot_status'],'ENTRY_PENDING')
        ids=[intent['client_order_id'] for intent in f.gateway.submissions]
        self.assertEqual(ids.count(pins['client']),1);self.assertNotEqual(ids[0],ids[-1]);self.assertEqual(len(f.calls),5)
        f.ticks(3);self.assertEqual(len(f.calls),5);self.assertEqual(f.receipt_count(),0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM robot_cycles').fetchone()[0],2)
        self.assertEqual(tool.seed(root,True,pins)['status'],'ALREADY_SCHEDULED')

    def test_ambiguity_proof_mismatch_foreign_cycle_and_busy_refuse_without_cycle_insert(self):
        for mutation in ('proof','payload','off','unknown','provider','foreign','busy'):
            with self.subTest(mutation=mutation):
                f,root,pins,proof_file=self.fixture();db=f.ledger.db;lock=None
                if mutation=='proof':proof_file.write_text('{}')
                if mutation=='payload':db.execute("UPDATE order_intents SET payload=payload||' ' WHERE symbol='BTCUSDT'")
                if mutation=='off':f.store.configure({'robot_on':False})
                if mutation=='unknown':db.execute("UPDATE order_intents SET state='NEEDS_REVIEW' WHERE symbol='ETHUSDT'")
                if mutation=='provider':db.execute("UPDATE api_requests SET state='NEEDS_REVIEW' WHERE operation=(SELECT operation FROM api_requests LIMIT 1)")
                if mutation=='foreign':
                    old=db.execute('SELECT * FROM robot_cycles').fetchone();db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',(old['day']+':robot-v9:1',old['day'],1,'ACTIVE','{"target":1}'))
                if mutation=='busy':lock=os.open(root/'trading/cycle.lock',os.O_RDONLY);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                before=[tuple(r) for r in db.execute('SELECT * FROM robot_cycles')];files=set(root.rglob('*'))
                try:
                    with self.assertRaises(Exception):tool.seed(root,True,pins,lock_timeout=0)
                finally:
                    if lock is not None:os.close(lock)
                self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM robot_cycles')],before)
                self.assertEqual(set(root.rglob('*')),files)

    def test_eth_filled_or_closed_is_noop_without_new_backup_or_cycle(self):
        for state in ('POSITION_PROTECTED','CLOSED'):
            with self.subTest(state=state):
                f,root,pins,proof_file=self.fixture();eth=f.ledger.db.execute("SELECT * FROM order_intents WHERE symbol='ETHUSDT'").fetchone();intent=json.loads(eth['payload'])
                value=f.gateway.observation(intent,'POSITION_PROTECTED','2.5')
                if state=='CLOSED':value.update(state='CLOSED',closed_at=value['observed_at'],exit_order_id='13')
                self.assertEqual(f.robot.record_gateway_observation(eth['id'],eth['candidate_id'],value,account_state(f.account,f.store,day()))['bot_status'],state)
                before=set(root.rglob('*'));self.assertEqual(tool.seed(root,True,pins)['status'],'NO_CHANGE_ENTRY_ALREADY_FILLED')
                self.assertEqual(set(root.rglob('*')),before);self.assertEqual(f.ledger.db.execute('SELECT COUNT(*) FROM robot_cycles').fetchone()[0],1)

    def test_historical_cycle_metadata_does_not_block_current_repair(self):
        for state in ('ACTIVE','NEEDS_REVIEW'):
            with self.subTest(state=state):
                f,root,pins,proof_file=self.fixture()
                previous=(date.fromisoformat(day())-timedelta(days=1)).isoformat()
                old_id=previous+':robot-v9:0'
                payload=json.dumps(dict(target=1,queue=[],seen=[],screen=-1,replacements=0,round_symbols=[]))
                f.ledger.db.execute('INSERT INTO robot_cycles VALUES(?,?,?,?,?)',(old_id,previous,0,state,payload))
                before=dict(f.ledger.db.execute('SELECT * FROM robot_cycles WHERE id=?',(old_id,)).fetchone())
                self.assertEqual(tool.seed(root,True,pins)['status'],'ONE_REPAIR_CYCLE_SEEDED')
                self.assertEqual(dict(f.ledger.db.execute('SELECT * FROM robot_cycles WHERE id=?',(old_id,)).fetchone()),before)

if __name__=='__main__':unittest.main()
