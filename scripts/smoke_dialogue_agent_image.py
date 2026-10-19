"""Run inside the derived image with --network none and --group-add 10001.

This uses the actual production CLI and no model key. Only a nonsensitive local
UDS authentication fixture is created. It observes honest BLOCKED health, clean
SIGTERM, process restart, crash recovery, and zero model operations/history.
"""
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def observe(child, path, *, timeout=15):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    limit=time.monotonic()+timeout
    while time.monotonic()<limit:
        assert child.poll() is None, 'DIALOGUE_EXITED_BEFORE_HEALTH'
        try:
            with opener.open('http://127.0.0.1:8765/health/live',timeout=1) as response:
                raw=response.read(16385)
            assert len(raw)<=16384
            value=json.loads(raw)
            if value.get('mode')=='NATIVE_DELEGATED_DIALOGUE':
                break
        except (OSError,ValueError):
            pass
        time.sleep(0.05)
    else:
        raise AssertionError('DIALOGUE_LIVENESS_UNVERIFIED')
    assert value['runtime_implemented'] is True
    assert value['uid']==10002
    assert value['state']=='BLOCKED' and value['model_key_state']=='MISSING'
    assert value['dialogue_ready'] is False and value['integration_verified'] is False
    assert value['dialogue_transport_verified'] is True
    assert value['native_coordinator_preserved'] is True
    assert value['trading_authority'] is False and value['model_authentication_verified'] is False
    assert value['source_commit']==os.environ['HARUN_SOURCE_COMMIT']
    assert value['provider_contact_performed'] is False
    info=path.lstat()
    assert stat.S_ISSOCK(info.st_mode) and info.st_uid==10002 and info.st_gid==10001
    assert stat.S_IMODE(info.st_mode)==0o660
    try:
        with opener.open('http://127.0.0.1:8765/health/ready',timeout=1):
            raise AssertionError('MISSING_KEY_MUST_NOT_CLAIM_READY')
    except urllib.error.HTTPError as error:
        assert error.code==503
        error.read(16385)
        error.close()
    return value


def empty_memory(data):
    with sqlite3.connect(str(data/'agent.sqlite3')) as database:
        for table in ('operations','messages','budgets'):
            assert database.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0


def main():
    assert os.geteuid()==10002
    assert 10001 in os.getgroups(), 'RUNTIME_SUPPLEMENTARY_SOCKET_GROUP_REQUIRED'
    assert not Path('/app/worker/order_gateway.py').exists()
    assert not Path('/app/worker/robot.py').exists()
    assert not Path('/app/worker/api_service.py').exists()
    with tempfile.TemporaryDirectory(prefix='dialogue-image-',dir='/data') as temporary:
        root=Path(temporary)
        data=root/'data';data.mkdir(mode=0o700)
        shared=root/'socket';shared.mkdir(mode=0o750);os.chown(shared,-1,10001)
        shared.chmod(0o750)
        path=shared/'dialogue.sock'
        token=root/'dialogue.token'
        token.write_text('synthetic-local-dialogue-authentication-fixture-only')
        token.chmod(0o600)
        missing_key=root/'intentionally-absent-openai.key'
        assert not missing_key.exists()
        command=[sys.executable,'-m','agent.dialogue_service','--socket',str(path),
            '--data-directory',str(data),'--token-file',str(token),'--key-file',str(missing_key)]
        generations=[]
        child=None
        with (root/'daemon.log').open('wb') as log:
            try:
                # A normal shutdown/restart reuses its own durable memory.
                child=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
                value=observe(child,path);generations.append(value['process_generation'])
                empty_memory(data)
                child.send_signal(signal.SIGTERM)
                assert child.wait(timeout=5)==0
                assert not path.exists()
                child=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
                value=observe(child,path);generations.append(value['process_generation'])
                empty_memory(data)
                child.kill();assert child.wait(timeout=5)==-signal.SIGKILL
                assert path.exists(), 'CRASH_FIXTURE_MUST_RETAIN_STALE_SOCKET'
                # No manual deletion, chmod, replacement receipt or fake API key.
                child=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
                value=observe(child,path);generations.append(value['process_generation'])
                empty_memory(data)
                child.send_signal(signal.SIGTERM);assert child.wait(timeout=5)==0
                assert len(set(generations))==3
                print(json.dumps({'status':'ISOLATED_DELEGATED_DIALOGUE_IMAGE_SMOKE_VERIFIED',
                    'uid':os.geteuid(),'socket_gid':10001,'socket_mode':'0660',
                    'runtime_mode':'NATIVE_DELEGATED_DIALOGUE','model_key_state':'MISSING',
                    'health_live_verified':True,'health_ready':False,'model_operations':0,
                    'model_or_chat_calls':0,'orders':0,'production_contact':False,
                    'daemon_sigterm_clean':True,'daemon_restart_verified':True,
                    'stale_socket_crash_recovery_verified':True,'process_generations':len(set(generations))},
                    sort_keys=True))
            finally:
                if child is not None and child.poll() is None:
                    child.kill();child.wait(timeout=5)


if __name__=='__main__':main()
