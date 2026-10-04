"""24/7 supervisor-owned DRY RUN scheduler; existing workflow/risk rules are unchanged.
Disabled until deployment opt-in. One persisted attempt/day; no blind crash retry.
"""
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def atomic(path,data):
    path=Path(path);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data));os.replace(tmp,path)


def due(now,run_at,enabled):
    hour,minute=map(int,run_at.split(':'))
    if not (0<=hour<24 and 0<=minute<60):raise ValueError('Invalid schedule')
    return enabled and (now.hour,now.minute)>=(hour,minute)


def claim(directory,day):
    folder=Path(directory)/'runner-attempts';folder.mkdir(exist_ok=True,mode=0o700)
    try:
        with (folder/day).open('x') as f:f.write('DRY_RUN_ATTEMPT');f.flush();os.fsync(f.fileno())
        return True
    except FileExistsError:return False


def main():
    os.umask(0o077)
    data=Path(os.environ.get('OFFICE_DATA_DIR','/data'));data.mkdir(exist_ok=True)
    runtime=Path(os.environ.get('OFFICE_RUNTIME_DIR','/run/office'));runtime.mkdir(exist_ok=True)
    enabled=os.environ.get('OFFICE_AUTO_DRY_RUN','false')=='true'
    run_at=os.environ.get('OFFICE_RUN_AT','08:00');child=None
    def stop(*args):
        if child and child.poll() is None:
            child.terminate()
            try:child.wait(timeout=55)
            except subprocess.TimeoutExpired:child.kill()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop)
    directory=data/'browser';directory.mkdir(exist_ok=True)
    own=(directory/'runner.lock').open('a');fcntl.flock(own,fcntl.LOCK_EX|fcntl.LOCK_NB)
    while True:
        state='DISABLED' if not enabled else 'WAITING_SCHEDULE'
        if child:
            if child.poll() is None:state='RUNNING_DRY_RUN'
            else:child=None;state='ATTEMPT_FINISHED'
        elif due(datetime.now(ZoneInfo('Asia/Bangkok')),run_at,enabled):
            lock=(directory/'worker.lock').open('a')
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                day=datetime.now(ZoneInfo('Asia/Bangkok')).date().isoformat()
                # Persist before starting; even a crash between claim and spawn cannot resend.
                should_run=claim(directory,day)
            except BlockingIOError:should_run=False;state='WAITING_LOGIN_OR_WORKER'
            finally:lock.close()
            if should_run:
                child=subprocess.Popen([sys.executable,'-m','worker','browser-dry-run',
                    '--data-dir',str(data),'--config',os.environ['OFFICE_CONFIG']],
                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                state='RUNNING_DRY_RUN'
        atomic(runtime/'runner.json',{'at':time.time(),'state':state,'mode':'DRY_RUN'})
        time.sleep(5)

if __name__=='__main__':main()
