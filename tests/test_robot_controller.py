"""Exercise the real actor threads with synthetic account/provider transports."""
import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from worker.api_service import Controller,ACTOR_STALL_SECONDS,ACCOUNT_STALL_SECONDS
from worker.core import Ledger
from worker.neuroapi import NeuroAPI,SCREEN_ONE_SCHEMA,SETUP_SCHEMA
from worker.robot import Coordinator,RobotStore,SCREENING_ONE
from test_decisions import ExpandedMarket
from test_neuroapi import GOOD
from test_robot import Account


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.account=Account();self.account.position('HYPEUSDT','4.16')
        self.calls=[];self.off_tick=threading.Event();self.screened=threading.Event()
        self.ready=threading.Event();self.tick_complete=threading.Event()
        self.request_started=threading.Event();self.release=threading.Event()
        self.block_request=False
        def transport(method,url,headers,body,timeout):
            self.calls.append(body)
            if self.block_request:
                self.request_started.set()
                if not self.release.wait(5):raise TimeoutError('synthetic timeout')
            value={'symbols':['BTCUSDT']} if body['output_schema']==SCREEN_ONE_SCHEMA else GOOD
            return 200,{},dict(mode='smart',answer=None,output=value)
        factory=lambda ledger:NeuroAPI(ledger,key='synthetic-controller-key',transport=transport)
        owner=self
        class ObservedCoordinator(Coordinator):
            def tick(self):
                result=super().tick()
                if not result['robot_on']:owner.off_tick.set()
                if owner.calls:owner.screened.set()
                if any(row['status']=='SETUP_READY' for row in result['setups']):owner.ready.set()
                owner.tick_complete.set()
                return result
        for target,value in (
            ('worker.api_service.NeuroAPI',factory),
            ('worker.api_service.Market',lambda *args:ExpandedMarket()),
            ('worker.api_service.BinanceReadOnly',lambda:self.account),
            ('worker.api_service.Coordinator',ObservedCoordinator),
        ):
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        self.controllers=[]
        self.addCleanup(self.cleanup_controllers)
    def cleanup_controllers(self):
        self.release.set()
        for controller in self.controllers:controller.drain(timeout=5)
    def start(self):
        controller=Controller(self.tmp.name);self.controllers.append(controller)
        return controller
    def test_on_wakes_actor_before_next_45_second_poll(self):
        c=self.start()
        self.assertTrue(self.off_tick.wait(3));self.assertEqual(self.calls,[])
        c.robot({'robot_on':True})
        self.assertTrue(self.screened.wait(3))
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0]['prompt'],SCREENING_ONE)
        status=c.robot()
        self.assertEqual(status['available_slots'],1)
        self.assertEqual(status['manual_exposure'],['HYPEUSDT'])
        self.assertFalse(status['live_execution'])
        # A repeated save of ON does not force another paid tick.
        c.robot_wakeup.clear();c.robot({'robot_on':True})
        self.assertFalse(c.robot_wakeup.is_set())
    def test_one_slot_automatic_analysis_and_restart_no_paid_replay(self):
        with patch('worker.api_service.ROBOT_POLL_SECONDS',0.05):
            c=self.start();c.robot({'robot_on':True})
            self.assertTrue(self.ready.wait(5));c.drain(timeout=5)
            self.assertEqual([body['output_schema'] for body in self.calls],[SCREEN_ONE_SCHEMA,SETUP_SCHEMA])
            status=c.robot();self.assertEqual(status['risk_target_usdt'],'5')
            self.assertEqual(status['setups'][0]['symbol'],'BTCUSDT')
            self.assertEqual(status['running_positions'],1)
            self.assertTrue(all(method=='GET' for method,_ in self.account.calls))
            self.tick_complete.clear();restarted=self.start()
            self.assertTrue(self.tick_complete.wait(3));restarted.drain(timeout=5)
            self.assertEqual(len(self.calls),2)
            self.assertTrue(restarted.robot()['robot_on'])
    def test_shutdown_drains_sent_request_and_persists_completed_claim(self):
        self.block_request=True;c=self.start();c.robot({'robot_on':True})
        self.assertTrue(self.request_started.wait(3))
        finished=threading.Event()
        def stop():c.drain(timeout=5);finished.set()
        stopper=threading.Thread(target=stop);stopper.start()
        self.assertTrue(c.stopping.wait(1));self.assertFalse(finished.is_set())
        self.release.set();self.assertTrue(finished.wait(3));stopper.join(timeout=1)
        ledger=Ledger(c.directory/'ledger.sqlite3')
        try:
            self.assertEqual(ledger.db.execute('SELECT state FROM robot_jobs').fetchone()[0],'COMPLETE')
            self.assertEqual(ledger.db.execute('SELECT state FROM api_requests').fetchone()[0],'COMPLETE')
            cycle=json.loads(ledger.db.execute('SELECT data FROM robot_cycles').fetchone()[0])
            self.assertEqual(cycle['queue'],['BTCUSDT'])
        finally:ledger.db.close()
        self.assertEqual(len(self.calls),1)
    def test_failed_paper_monitor_does_not_kill_research_actor(self):
        with patch('worker.api_service.Workflow') as workflow:
            workflow.return_value.monitor.side_effect=OSError('synthetic unavailable snapshot')
            c=self.start();self.assertTrue(self.off_tick.wait(3))
            deadline=time.monotonic()+3
            while not workflow.return_value.monitor.called and time.monotonic()<deadline:
                c.stopping.wait(0.01)
            self.assertTrue(workflow.return_value.monitor.called)
            c.robot({'robot_on':True});self.assertTrue(self.screened.wait(3))
            self.assertTrue(c.thread.is_alive())


class ControllerHealthTests(unittest.TestCase):
    def make(self):
        c=Controller.__new__(Controller);c.lock=threading.Lock();c.stopping=threading.Event()
        c.worker_progress=c.account_progress=time.monotonic()
        c.thread=Mock();c.account_thread=Mock()
        c.thread.is_alive.return_value=c.account_thread.is_alive.return_value=True
        c.jobs=queue.Queue(maxsize=1);c.checked=0;c.busy=False
        return c
    def test_health_detects_stalled_actor_and_account_reader(self):
        c=self.make();self.assertTrue(c.healthy())
        c.worker_progress-=ACTOR_STALL_SECONDS+1;self.assertFalse(c.healthy())
        c.progress();c.account_progress-=ACCOUNT_STALL_SECONDS+1;self.assertFalse(c.healthy())
        c.progress(account=True);self.assertTrue(c.healthy())
        c.thread.is_alive.return_value=False;self.assertFalse(c.healthy())
    def test_normal_provider_request_is_not_a_stall(self):
        c=self.make();c.worker_progress-=400
        self.assertTrue(c.healthy());c.stopping.set();self.assertFalse(c.healthy())
    def test_full_control_queue_does_not_leave_busy_stuck(self):
        c=self.make();c.jobs.put_nowait('check')
        self.assertFalse(c.submit('run'));self.assertFalse(c.busy)
        c.jobs.get_nowait();self.assertTrue(c.submit('check'));self.assertTrue(c.busy)
    def test_no_new_control_jobs_after_shutdown(self):
        c=self.make();c.stopping.set()
        self.assertFalse(c.submit('run'));self.assertTrue(c.jobs.empty())
