import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
import yaml
from worker.api_service import handler
from worker.core import Ledger,day
from worker.state import directory
from worker.neuroapi import api_key

class DeploymentTests(unittest.TestCase):
    def test_python_image_minimal_and_private_secret(self):
        docker=Path('Dockerfile').read_text();self.assertIn('FROM python:3.12-slim-bookworm',docker)
        self.assertNotIn('pip install',docker);self.assertNotIn('mcr.microsoft.com',docker)
        c=yaml.safe_load(Path('compose.yaml').read_text());w=c['services']['worker']
        self.assertNotIn('ports',w);self.assertTrue(w['read_only']);self.assertEqual(w['restart'],'unless-stopped')
        self.assertIn('neuroapi_key',w['secrets']);self.assertNotIn('NEUROBRO_API_KEY',w['environment'])
        self.assertEqual(w['volumes'],['worker_data:/data']);self.assertEqual(c['name'],'harun-office')
    def test_no_retired_runtime_modules_or_dependencies(self):
        runtime='\n'.join(p.read_text() for p in Path('worker').glob('*.py'))+Path('Dockerfile').read_text()+Path('compose.yaml').read_text()
        for forbidden in ('playwright','chromium','noVNC','Xvfb','websockify','selector_discovery','browser-profile'):
            self.assertNotIn(forbidden.lower(),runtime.lower())
        for path in ('worker/browser.py','worker/session_service.py','assets/neurobro.js'):self.assertFalse(Path(path).exists())
    def test_no_obsolete_ui_routes(self):
        ui=Path('assets/neuroapi.js').read_text();proxy=Path('deploy/Caddyfile.docker').read_text()
        for text in ('LOGIN NEUROBRO','CEK SESI','BUKA BROWSER SERVER','CLOUDFLARE','/neurobro/','/desktop/'):
            self.assertNotIn(text,ui+proxy)
        self.assertIn('/neuroapi/check',proxy);self.assertNotIn('localStorage',ui)
    def test_secret_file_missing_configured_and_not_echoed(self):
        with patch.dict(os.environ,{'NEUROBRO_API_KEY_FILE':'/nonexistent','NEUROBRO_API_KEY':'DO_NOT_ECHO'}):self.assertEqual(api_key(),'')
        with patch.dict(os.environ,{'NEUROBRO_API_KEY_FILE':'','NEUROBRO_API_KEY':'test-private-key'}):self.assertEqual(api_key(),'test-private-key')
    def test_update_preserves_volumes_and_old_secrets(self):
        script=Path('deploy/update-api.sh').read_text()
        self.assertNotIn('down',script);self.assertNotIn('rm ',script)
        self.assertLess(script.index('compose build'),script.index('compose stop'))
        self.assertNotIn('archive-retired',script)
    def test_ignore_and_image_context(self):
        ignore=Path('.dockerignore').read_text();self.assertTrue(ignore.startswith('**'))
        self.assertNotIn('!.env',ignore);self.assertNotIn('!secrets',ignore)
        self.assertIn('secrets/',Path('.gitignore').read_text())
    def test_migration_keeps_daily_counter_events_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            old=Path(tmp)/'browser';old.mkdir();db=Ledger(old/'ledger.sqlite3')
            db.event('IDLE','Coordinator','audit-preserved')
            db.db.execute('INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)',('old',day(),'LEGACY','ORDER_READY','{}',None,None,'time',None));db.db.close()
            attempts=old/'runner-attempts';attempts.mkdir();(attempts/day()).write_text('attempt')
            target=directory(tmp);new=Ledger(target/'ledger.sqlite3')
            self.assertEqual(new.count(),1);self.assertEqual(new.db.execute('SELECT message FROM events').fetchone()[0],'audit-preserved')
            self.assertEqual(new.db.execute('SELECT state FROM cycles WHERE day=?',(day(),)).fetchone()[0],'LEGACY_ATTEMPT')
            new.db.close();directory(tmp);self.assertTrue((old/'ledger.sqlite3').exists())
    def test_setup_uses_silent_entry_and_external_path(self):
        script=Path('deploy/setup.py').read_text();self.assertIn('getpass.getpass',script);self.assertNotIn('print(value)',script)
        self.assertNotIn('neuroapi_key',Path('.env.example').read_text())

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.control='C'*32;self.read='R'*32;self.origin='https://office.example'
        self.c=Mock();self.c.directory=Path(self.temp.name);self.c.snapshot.return_value={'status':'NEUROAPI_NOT_CONFIGURED','mode':'DRY_RUN','live_enabled':False,'busy':False};self.c.thread.is_alive.return_value=True
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.c,self.read,self.control,self.origin));self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):self.server.shutdown();self.server.server_close()
    def call(self,path,token=None,method='GET',origin=None,data=None):
        headers={}
        if token:headers['Authorization']='Bearer '+token
        if origin:headers['Origin']=origin
        req=Request('http://127.0.0.1:'+str(self.server.server_port)+path,method=method,headers=headers,data=data)
        try:r=urlopen(req,timeout=2)
        except HTTPError as e:r=e
        with r:return r.status,json.load(r)
    def test_no_unauthenticated_health(self):self.assertEqual(self.call('/health')[0],403)
    def test_online_read_token(self):
        code,data=self.call('/health',self.read);self.assertEqual(code,200);self.assertEqual(data['status'],'ONLINE');self.assertFalse(data['live_enabled'])
    def test_mutation_requires_control_and_origin(self):
        for token,origin in ((self.read,self.origin),(self.control,'https://evil.example'),(self.control,None)):
            self.assertEqual(self.call('/neuroapi/run',token,'POST',origin)[0],403)
        self.c.submit.assert_not_called()
    def test_no_credential_body_or_old_routes(self):
        self.assertEqual(self.call('/neuroapi/check',self.control,'POST',self.origin,b'private-key')[0],400)
        self.assertEqual(self.call('/neurobro/login',self.control,'POST',self.origin)[0],404)
        self.c.submit.assert_not_called()
    def test_check_is_queued_not_prompt(self):
        code,data=self.call('/neuroapi/check',self.control,'POST',self.origin);self.assertEqual(code,202);self.c.submit.assert_called_once_with('check');self.assertNotIn(self.control,json.dumps(data))

    def test_http_cannot_arm_execute_or_enable_scheduler(self):
        for path in ('/binance/arm','/binance/execute','/binance/scheduler','/binance-arm','/neuroapi/run?live=true'):
            self.assertEqual(self.call(path,self.control,'POST',self.origin)[0],404)
        self.c.submit.assert_not_called()
