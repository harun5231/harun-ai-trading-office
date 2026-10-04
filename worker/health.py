"""Authenticated liveness report; login validity is separate from infrastructure health."""
import json
import os
import socket
import time
import uuid
from pathlib import Path
from urllib.request import Request,urlopen


def fresh(path,seconds=30):
    try:
        data=json.loads(Path(path).read_text())
        return data if 0<=time.time()-data['at']<=seconds else None
    except (OSError,ValueError,KeyError,TypeError):return None


def persistent_probe(directory):
    path=Path(directory)/('.health-'+uuid.uuid4().hex)
    try:
        with path.open('x') as f:
            f.write('probe');f.flush();os.fsync(f.fileno())
        return path.read_text()=='probe'
    except OSError:return False
    finally:
        try:path.unlink(missing_ok=True)
        except OSError:pass


def report(controller):
    managed=os.environ.get('OFFICE_MANAGED')=='1'
    runtime=Path(os.environ.get('OFFICE_RUNTIME_DIR','/run/office'))
    runner=fresh(runtime/'runner.json') if managed else {'state':'SESSION_ONLY'}
    supervisor=fresh(runtime/'supervisor.json') if managed else {'ok':True}
    actor=controller.thread.is_alive() and time.monotonic()-controller.heartbeat<120
    browser=controller.browser_health
    persistence=controller.persistence_ok
    okay=bool(actor and runner and supervisor and supervisor.get('ok') and persistence and browser!='ERROR')
    return {'status':'ONLINE' if okay else 'OFFLINE','mode':'DRY_RUN','live_enabled':False,
            'components':{'api':'ONLINE','worker':runner.get('state') if runner else 'OFFLINE',
                          'browser':browser,'persistent_session':'PERSISTENT_OK' if persistence else 'ERROR',
                          'session_status':controller.snapshot()['status']}}


def probe_api():
    token=Path('/run/office/read_token').read_text().strip()
    request=Request('http://127.0.0.1:8787/health',headers={'Authorization':'Bearer '+token})
    with urlopen(request,timeout=5) as response:return json.load(response)['status']=='ONLINE'

if __name__=='__main__':
    try:okay=probe_api()
    except Exception:okay=False
    raise SystemExit(0 if okay else 1)
