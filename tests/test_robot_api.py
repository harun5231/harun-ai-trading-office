import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import Mock
from worker.api_service import handler,Controller
from worker.core import Ledger

class RobotAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.c=Mock();self.c.directory=Path(self.tmp.name)
        self.c.robot=lambda value=None,approval=False:Controller.robot(self.c,value,approval)
        self.c.thread.is_alive.return_value=True
        self.origin='https://office.example';self.read='R'*32;self.control='C'*32
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.c,self.read,self.control,self.origin))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):self.server.shutdown();self.server.server_close()
    def call(self,path,method='GET',token=None,value=None,origin=None,raw=None):
        headers={'Authorization':'Bearer '+(token or self.control),'Origin':origin or self.origin}
        if value is not None or raw is not None:headers['Content-Type']='application/json'
        data=raw if raw is not None else json.dumps(value).encode() if value is not None else None
        req=Request('http://127.0.0.1:'+str(self.server.server_port)+path,headers=headers,method=method,data=data)
        try:r=urlopen(req,timeout=3)
        except HTTPError as e:r=e
        with r:return r.status,json.load(r)
    def test_read_status_authenticated_no_external_calls(self):
        code,data=self.call('/robot/status',token=self.read);self.assertEqual(code,200);self.assertFalse(data['robot_on']);self.assertFalse(data['live_execution'])
        self.assertEqual(self.call('/robot/status',token='invalid')[0],403)
    def test_settings_control_and_origin_required(self):
        for token,origin in [(self.read,self.origin),(self.control,'https://evil.example')]:
            self.assertEqual(self.call('/robot/settings','POST',token,{'robot_on':True},origin)[0],403)
        self.assertFalse(self.call('/robot/status')[1]['robot_on'])
    def test_persistent_settings_through_real_handler(self):
        code,data=self.call('/robot/settings','POST',value={'robot_on':True,'risk_target_usdt':'10'})
        self.assertEqual(code,200);self.assertTrue(data['robot_on']);self.assertEqual(self.call('/robot/status')[1]['risk_target_usdt'],'10')
        self.assertFalse(data['live_execution']);self.assertFalse(data['scheduler_enabled'])
    def test_bad_bodies_no_partial_update_or_secret_echo(self):
        for raw in [b'{"robot_on":true,"risk_target_usdt":"0"}',b'{"robot_on":true,"robot_on":false}',b'{"secret":"DO_NOT_ECHO"}',b'x'*1025]:
            code,data=self.call('/robot/settings','POST',raw=raw);self.assertEqual(code,400);self.assertNotIn('DO_NOT_ECHO',json.dumps(data))
        self.assertFalse(self.call('/robot/status')[1]['robot_on'])
    def test_approval_auth_and_unknown_id_fail_closed(self):
        value={'setup_id':'unknown','decision':'APPROVED'}
        self.assertEqual(self.call('/robot/approval','POST',token=self.read,value=value)[0],403)
        self.assertEqual(self.call('/robot/approval','POST',value=value)[0],400)
    def test_cors_json_header_and_no_live_route(self):
        code,data=self.call('/binance/execute','POST');self.assertEqual(code,404)
        for path in ('/robot/settings','/robot/approval'):self.assertEqual(self.call(path)[0],404)
    def test_proxy_exact_routes(self):
        proxy=Path('deploy/Caddyfile.docker').read_text()
        for path in ('/robot/status','/robot/settings','/robot/approval'):self.assertIn(path,proxy)
        self.assertNotIn('/robot/*',proxy)
