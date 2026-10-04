import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import patch
import yaml
from deploy.setup import create_once,private_location,ROOT
from worker.runner import claim,due,atomic
from worker.health import fresh,persistent_probe,report

class DeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.compose=yaml.safe_load(Path('compose.yaml').read_text())
    def test_only_proxy_publishes_ports(self):
        services=self.compose['services']
        self.assertNotIn('ports',services['worker'])
        self.assertEqual(set(services['proxy']['ports']),{'80:80','443:443','443:443/udp'})
        self.assertNotIn('privileged',services['worker'])
        self.assertNotIn('/var/run/docker.sock',Path('compose.yaml').read_text())
    def test_persistence_and_restart(self):
        self.assertIn('worker_data:/data',self.compose['services']['worker']['volumes'])
        self.assertIn('caddy_data:/data',self.compose['services']['proxy']['volumes'])
        for service in self.compose['services'].values():
            self.assertEqual(service['restart'],'unless-stopped');self.assertIn('healthcheck',service)
        self.assertEqual(self.compose['services']['worker']['hostname'],'harun-worker')
    def test_secrets_are_runtime_files_not_build_environment(self):
        for secret in self.compose['secrets'].values():self.assertTrue(secret['file'].startswith('${OFFICE_PRIVATE_DIR}/secrets/'))
        docker=Path('Dockerfile').read_text()
        self.assertNotIn('COPY . ',docker);self.assertNotIn('TOKEN=',docker);self.assertNotIn('PASSWORD=',docker)
        allowed=Path('.dockerignore').read_text()
        self.assertIn('**',allowed);self.assertNotIn('!.env',allowed)
        env=Path('.env.example').read_text()
        self.assertIn('OFFICE_AUTO_DRY_RUN=false',env);self.assertNotIn('TOKEN=',env)
    def test_desktop_is_authenticated_and_api_has_no_plain_public_path(self):
        caddy=Path('deploy/Caddyfile.docker').read_text()
        self.assertIn('basic_auth',caddy);self.assertIn('respond @foreignSocket 403',caddy)
        self.assertIn('reverse_proxy worker:6080',caddy);self.assertIn('reverse_proxy worker:8787',caddy)
        self.assertNotIn('file_server',caddy)
        supervisor=Path('deploy/supervise.py').read_text()
        self.assertIn("'-localhost'",supervisor);self.assertIn("'127.0.0.1:5900'",supervisor)
    def test_setup_is_idempotent_no_rotation(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'secret'
            self.assertTrue(create_once(p,'SYNTHETIC_FIRST'))
            self.assertFalse(create_once(p,'SYNTHETIC_SECOND'))
            self.assertEqual(p.read_text(),'SYNTHETIC_FIRST');self.assertEqual(p.stat().st_mode&0o777,0o600)
        with self.assertRaises(ValueError):private_location(ROOT/'private')
    def test_persisted_schedule_claim_prevents_restart_resend(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(claim(d,'2026-10-04'));self.assertFalse(claim(d,'2026-10-04'))
            self.assertTrue(claim(d,'2026-10-05'))
    def test_schedule_disabled_and_time_gated(self):
        self.assertFalse(due(datetime(2026,10,4,12),'08:00',False))
        self.assertFalse(due(datetime(2026,10,4,7),'08:00',True))
        self.assertTrue(due(datetime(2026,10,4,8),'08:00',True))
        with self.assertRaises(ValueError):due(datetime.now(),'99:00',True)
    def test_health_probe_is_real_write_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(persistent_probe(d));self.assertEqual(list(Path(d).iterdir()),[])
            self.assertFalse(persistent_probe(Path(d)/'missing'))
    def test_stale_heartbeat_offline(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'runner.json';atomic(p,{'at':time.time(),'state':'DISABLED'})
            self.assertIsNotNone(fresh(p))
            atomic(p,{'at':time.time()-40,'state':'IDLE'});self.assertIsNone(fresh(p))
    def test_health_separates_online_from_neurobro_login(self):
        c=SimpleNamespace(thread=SimpleNamespace(is_alive=lambda:True),heartbeat=time.monotonic(),
            browser_health='IDLE',persistence_ok=True,snapshot=lambda:{'status':'LOGIN_REQUIRED'})
        with patch.dict(os.environ,{'OFFICE_MANAGED':'0'}):
            data=report(c);self.assertEqual(data['status'],'ONLINE')
            self.assertEqual(data['components']['session_status'],'LOGIN_REQUIRED')
            c.persistence_ok=False;self.assertEqual(report(c)['status'],'OFFLINE')
            c.persistence_ok=True;c.browser_health='ERROR';self.assertEqual(report(c)['status'],'OFFLINE')
    def test_dry_run_only_runtime_and_no_order_endpoints(self):
        runner=Path('worker/runner.py').read_text()
        self.assertIn("'browser-dry-run'",runner)
        self.assertNotIn("'live'",runner)
        self.assertIn("'DRY_RUN_ATTEMPT'",runner)

if __name__=='__main__':unittest.main()
