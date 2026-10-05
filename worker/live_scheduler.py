"""Scheduler is physically disconnected in this shadow/preflight release."""
def enabled():
    return False

def run_scheduler(root):
    return {'status':'SCHEDULER_OFF','live_execution':False,'scheduler_enabled':False}
