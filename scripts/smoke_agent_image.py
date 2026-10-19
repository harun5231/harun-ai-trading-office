"""Run INSIDE the test image with --network none; all backend data is synthetic.

This exercises the actual daemon, SQLite, bridge HTTP authentication, health and
SIGTERM. It never contacts Neurobro, OpenAI, Binance, or a production container.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone


def main():
    import playwright
    from playwright.sync_api import sync_playwright
    # Verify the native driver dependencies without opening a browser transport.
    driver = Path(playwright.__file__).parent/'driver'/'node'
    subprocess.run([str(driver),'--version'], check=True, timeout=10,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert os.getuid() == 10002
    assert not Path('/app/worker/order_gateway.py').exists()
    assert not Path('/app/worker/robot.py').exists()
    assert not Path('/app/worker/api_service.py').exists()
    source = hashlib.sha256(b'synthetic-off-backend-for-image-test').hexdigest()
    token = 'synthetic-test-app-token-no-exchange-credentials'
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.headers.get('Authorization') == 'Bearer ' + token
            requests.append(self.path)
            if self.path == '/agent/v1/capabilities':
                value = {'protocol':'harun-agent-bridge-v1','backend_revision':'fixture-v1',
                    'source_sha256':source,'gateway':'EXISTING_BINANCE_FUTURES_LIVE',
                    'provider':'NEUROBRO_WEB','atomic_robot_gate':True,
                    'idempotent_handoff':True,'idempotent_cancel_remaining':True,
                    'source_receipts_verified':True,'single_coordinator_verified':True,
                    'daily_entry_limit':2,'combined_slot_limit':2,'risk_model':'FEE_SLIPPAGE_RISK_V3'}
            elif self.path == '/agent/v1/snapshot':
                value = {'schema_version':1,'robot_on':False,'observed_at':time.time(),
                    'generation':'fixture-generation','jakarta_day':datetime.now(timezone(timedelta(hours=7))).date().isoformat(),
                    'balance_usdt':'100','risk_usdt':'5','daily_new_trades':0,
                    'positions':[],'pending':[],'supported_symbols':['BTCUSDT'],
                    'risk_evidence':{'ownership_reconciled':True,'occupied_slots':0}}
            else:
                raise AssertionError('Unexpected synthetic endpoint')
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_):
            pass

    backend = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='agent-image-fixture-', dir='/data') as directory:
            root = Path(directory)
            data = root/'data'
            data.mkdir(mode=0o700)
            key = root/'bridge.token'
            key.write_text(token)
            key.chmod(0o600)
            config = {'schema_version':1,'data_directory':str(data),
                'backend':{'url':'http://127.0.0.1:'+str(backend.server_port),
                    'token_file':str(key),'revision':'fixture-v1','source_sha256':source},
                'browser':{'cdp_url':'http://127.0.0.1:9','contract_file':str(root/'intentionally-absent-browser.json'),
                    'contract_sha256':hashlib.sha256(b'intentionally-absent-browser-fixture').hexdigest()},
                'model':{'key_file':str(root/'intentionally-absent-openai.key')},
                'loop':{'active_tick_seconds':1,'idle_tick_seconds':5}}
            raw = (json.dumps(config,sort_keys=True)+'\n').encode()
            path = root/'runtime.json'
            path.write_bytes(raw)
            path.chmod(0o600)
            with (root/'daemon.log').open('wb') as log:
                child = subprocess.Popen([sys.executable,'-m','agent','--config',str(path)],
                    stdout=log,stderr=log,stdin=subprocess.DEVNULL)
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    deadline = time.monotonic()+25
                    health = {}
                    while time.monotonic()<deadline:
                        if child.poll() is not None:
                            raise AssertionError('Daemon exited before health observation')
                        try:
                            with opener.open('http://127.0.0.1:8765/health',timeout=1) as response:
                                health = json.load(response)
                            if health.get('state') == 'PAUSED':
                                break
                        except OSError:
                            pass
                        time.sleep(0.2)
                    else:
                        raise AssertionError('Daemon did not reach OFF pause: ' + json.dumps({
                            'state':health.get('state'),'reason':health.get('reason'),
                            'paths':requests}))
                    assert health['reason']=='ROBOT_OFF'
                    assert health['runtime_config_sha256']==hashlib.sha256(raw).hexdigest()
                    assert health['runtime_implemented'] is True
                    assert health['integration_verified'] is False
                    assert health['deployment_authorized'] is False
                    assert set(requests)=={'/agent/v1/capabilities','/agent/v1/snapshot'}
                    database = sqlite3.connect(data/'agent.sqlite3')
                    assert database.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0
                    assert database.execute('SELECT COUNT(*) FROM messages').fetchone()[0]==0
                    database.close()
                    child.send_signal(signal.SIGTERM)
                    assert child.wait(timeout=5)==0
                    print(json.dumps({'status':'ISOLATED_IMAGE_DAEMON_SMOKE_VERIFIED',
                        'synthetic_backend':True,'uid':os.getuid(),'robot_fixture':'OFF',
                        'production_contact':False,'model_or_chat_calls':0,'orders':0,
                        'daemon_sigterm_clean':True,'runtime_config_sha256':health['runtime_config_sha256']}))
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
    finally:
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=3)


if __name__ == '__main__':
    main()
