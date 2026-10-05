"""Disabled supervisor interface retained for offline lifecycle regression tests."""
import threading
from .live_arm import execution_requested
from .binance_execution_transport import BinanceExecution

class LiveSupervisor:
    def __init__(self,root,stopping):
        self.root=root;self.stopping=stopping
        self.thread=threading.Thread(target=self.loop,daemon=True)
    def tick(self):
        return {'status':'LIVE_EXECUTION_DISARMED','live_execution':False,'scheduler_enabled':False}
    def loop(self):
        return # No background research, reconciliation or mutation job.
