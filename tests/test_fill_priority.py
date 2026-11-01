import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from worker.fill_priority import FillEventError, FillPriority, protection_state, owned_intent_lock


class FillPriorityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.at = 1800000000.0
        self.queue = FillPriority(self.db, wall_clock=lambda: self.at)
        self.intent = {'intent_id':'old-operation','client_order_id':'hao-existing-entry',
            'symbol':'BTCUSDT','position_side':'BOTH','side':'LONG',
            'entry':{'side':'BUY','quantity':'1','price':'100','order_type':'LIMIT'},
            'protection':{'stop_loss':'99','take_profit':'102','exit_side':'SELL'},
            'margin_mode':'CROSS','leverage':75}
        self.event = {'e':'ORDER_TRADE_UPDATE','E':1800000000000,
            'o':{'x':'TRADE','X':'PARTIALLY_FILLED','o':'LIMIT','R':False,
                'c':'hao-existing-entry','s':'BTCUSDT','S':'BUY','ps':'BOTH',
                'z':'0.4','i':101,'T':1799999999900}}
        self.recorded = []

    def tearDown(self):
        self.db.close()

    def result(self, quantity='0.4', sl=True, tp=True):
        value = {'source':'BINANCE_FUTURES','state':'POSITION_PROTECTED' if sl and tp else 'PROTECTION_INCOMPLETE',
            'symbol':'BTCUSDT','client_order_id':'hao-existing-entry','order_id':'101',
            'filled_quantity':quantity,'sl_confirmed':sl,'tp_confirmed':tp}
        if sl:value['sl_order_id']='201'
        if tp:value['tp_order_id']='202'
        return value

    def run_one(self, fn):
        return self.queue.run_one(fn, lambda operation,result:self.recorded.append((operation,result)))

    def test_owned_partial_fill_dispatches_frozen_old_intent_without_market_or_analysis(self):
        original=copy.deepcopy(self.intent)
        self.assertTrue(self.queue.admit(self.event,self.intent))
        calls=[]
        def reconcile(intent):
            calls.append(intent)
            self.at += 0.03
            return self.result()
        result=self.run_one(reconcile)
        self.assertEqual(calls,[original])
        self.assertEqual(self.intent,original)
        self.assertEqual(result['state'],'POSITION_OPEN_PROTECTED')
        self.assertFalse(result['retry_required'])
        self.assertEqual(self.recorded[0][0],'old-operation')
        self.assertEqual(result['exchange_fill_at'],'2027-01-15T07:59:59.900000+00:00')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM fill_priority_observations').fetchone()[0],1)

    def test_duplicate_or_out_of_order_stream_updates_do_not_repeat_callback(self):
        self.queue.admit(self.event,self.intent)
        self.run_one(lambda intent:self.result())
        self.assertFalse(self.queue.admit(self.event,self.intent))
        old=copy.deepcopy(self.event);old['o']['z']='0.2'
        self.assertFalse(self.queue.admit(old,self.intent))
        self.assertIsNone(self.run_one(lambda intent:self.fail('duplicate callback')))
        newer=copy.deepcopy(self.event);newer['o']['z']='1';newer['o']['X']='FILLED'
        self.assertTrue(self.queue.admit(newer,self.intent))
        result=self.run_one(lambda intent:self.result('1'))
        self.assertFalse(result['retry_required'])
        self.assertEqual(len(self.recorded),2)

    def test_manual_or_exit_events_cannot_claim_ownership(self):
        for key,value in [('c','manual-entry'),('s','ETHUSDT'),('S','SELL'),('ps','LONG'),('R',True),('z','2')]:
            event=copy.deepcopy(self.event);event['o'][key]=value
            with self.subTest(key=key),self.assertRaises(FillEventError):
                self.queue.admit(event,self.intent)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM fill_priority').fetchone()[0],0)

    def test_rereleased_policy_cannot_change_frozen_pending_parameters(self):
        self.queue.admit(self.event,self.intent)
        for group,key,value in [('entry','quantity','2'),('entry','price','101'),('protection','take_profit','103'),('protection','stop_loss','98')]:
            changed=copy.deepcopy(self.intent);changed[group][key]=value
            with self.subTest(key=key),self.assertRaisesRegex(FillEventError,'FILL_FROZEN_INTENT_CHANGED'):
                self.queue.enqueue(changed,'101','0.5')
        self.assertEqual(json.loads(self.db.execute('SELECT payload FROM fill_priority').fetchone()[0]),self.intent)

    def test_missing_tp_preserves_confirmed_sl_and_remains_priority_work(self):
        self.queue.admit(self.event,self.intent)
        first=self.run_one(lambda intent:self.result(tp=False))
        self.assertEqual(first['state'],'PROTECTION_INCOMPLETE')
        self.assertTrue(first['retry_required'])
        saved=json.loads(self.db.execute('SELECT last_result FROM fill_priority').fetchone()[0])
        self.assertEqual(saved['sl_order_id'],'201')
        self.assertFalse(saved['tp_confirmed'])
        second=self.run_one(lambda intent:self.result())
        self.assertEqual(second['state'],'POSITION_OPEN_PROTECTED')
        self.assertEqual(self.recorded[0][1]['sl_order_id'],self.recorded[1][1]['sl_order_id'])

    def test_uncertain_request_reconciles_without_entry_resubmit(self):
        self.queue.admit(self.event,self.intent)
        def uncertain(intent):raise TimeoutError('no response')
        result=self.run_one(uncertain)
        self.assertEqual(result['state'],'RECONCILIATION_REQUIRED')
        self.assertTrue(result['retry_required'])
        self.assertEqual(self.recorded,[])
        self.assertEqual(self.run_one(lambda intent:self.result())['state'],'POSITION_OPEN_PROTECTED')

    def test_snapshot_behind_stream_event_does_not_downgrade_position(self):
        self.queue.admit(self.event,self.intent)
        stale=self.run_one(lambda intent:self.result('0'))
        self.assertEqual(stale['failure_code'],'FILL_RECONCILE_BEHIND_EVENT')
        self.assertEqual(self.recorded,[])
        self.assertEqual(self.db.execute('SELECT state FROM fill_priority').fetchone()[0],'PENDING')

    def test_fill_increases_during_reconcile_requeues_before_analysis(self):
        self.queue.admit(self.event,self.intent)
        def reconcile(intent):
            self.queue.enqueue(self.intent,'101','1',exchange_fill_ms=1800000000100)
            return self.result('0.4')
        first=self.run_one(reconcile)
        self.assertTrue(first['retry_required'])
        self.assertEqual(self.run_one(lambda intent:self.result('1'))['state'],'POSITION_OPEN_PROTECTED')
        self.assertEqual(len(self.recorded),2)

    def test_api_recovery_can_wake_completed_pending_without_new_fill_event(self):
        self.queue.enqueue(self.intent,'101','0')
        result=self.result('0',False,False);result['state']='ENTRY_PENDING'
        self.run_one(lambda intent:result)
        self.assertTrue(self.queue.enqueue(self.intent,'101','0',recovery=True))
        self.assertEqual(self.run_one(lambda intent:self.result())['state'],'POSITION_OPEN_PROTECTED')

    def test_restart_recovers_interrupted_claim_with_same_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'ledger.sqlite3'
            first=sqlite3.connect(path,isolation_level=None)
            queue=FillPriority(first,wall_clock=lambda:self.at)
            queue.admit(self.event,self.intent)
            first.execute("UPDATE fill_priority SET state='RUNNING',claim='interrupted'")
            first.close()
            second=sqlite3.connect(path,isolation_level=None)
            queue=FillPriority(second,wall_clock=lambda:self.at)
            queue.recover_interrupted()
            calls=[]
            queue.run_one(lambda intent:(calls.append(intent) or self.result()),lambda *args:None)
            self.assertEqual(calls,[self.intent])
            second.close()

    def test_false_or_duplicate_protection_ids_cannot_report_protected(self):
        for result in [self.result(sl=False),self.result(tp=False),dict(self.result(),tp_order_id='201'),
                dict(self.result(),sl_order_id='101'),dict(self.result(),state='NEEDS_REVIEW')]:
            with self.subTest(result=result):self.assertEqual(protection_state(result),'PROTECTION_INCOMPLETE')

    def test_rejected_observation_remains_reconciliation_work(self):
        self.queue.admit(self.event,self.intent)
        result=self.queue.run_one(lambda intent:self.result(),lambda *args:{'bot_status':'NEEDS_REVIEW'})
        self.assertEqual(result['failure_code'],'FILL_OBSERVATION_NOT_PERSISTED')
        self.assertTrue(result['retry_required'])

    def test_gateway_and_observation_share_one_intent_lock(self):
        with tempfile.TemporaryDirectory()as directory:
            path=Path(directory)/'ledger.sqlite3'
            db=sqlite3.connect(path,isolation_level=None)
            priority=FillPriority(db,wall_clock=lambda:self.at)
            priority.admit(self.event,self.intent)
            attempting=threading.Event();acquired=threading.Event();threads=[];errors=[]
            def competitor():
                connection=sqlite3.connect(path,isolation_level=None)
                try:
                    attempting.set()
                    with owned_intent_lock(connection,self.intent['client_order_id']):
                        acquired.set()
                except Exception as error:errors.append(error)
                finally:connection.close()
            def persist(*args):
                thread=threading.Thread(target=competitor);threads.append(thread);thread.start()
                self.assertTrue(attempting.wait(5))
                self.assertFalse(acquired.wait(.03))
            try:
                value=priority.run_one(lambda intent:self.result(),persist)
                for thread in threads:thread.join(5)
                self.assertEqual(errors,[]);self.assertTrue(acquired.is_set())
                self.assertEqual(value['state'],'POSITION_OPEN_PROTECTED')
            finally:
                for thread in threads:thread.join(5)
                db.close()

    def test_two_priority_actors_cannot_claim_same_pending_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'ledger.sqlite3'
            first=sqlite3.connect(path,isolation_level=None,check_same_thread=False)
            second=sqlite3.connect(path,isolation_level=None)
            a=FillPriority(first,wall_clock=lambda:self.at)
            b=FillPriority(second,wall_clock=lambda:self.at)
            a.admit(self.event,self.intent)
            entered=threading.Event();release=threading.Event();results=[]
            def reconcile(intent):
                entered.set()
                self.assertTrue(release.wait(5))
                return self.result()
            thread=threading.Thread(target=lambda:results.append(a.run_one(reconcile,lambda *args:None)))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertIsNone(b.run_one(lambda intent:self.fail('double claim'),lambda *args:None))
            finally:
                release.set();thread.join(5)
                first.close();second.close()
            self.assertEqual(results[0]['state'],'POSITION_OPEN_PROTECTED')


if __name__=='__main__':unittest.main()
