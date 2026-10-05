import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from deploy.runtime_permissions import normalize

class SourcePermissionTests(unittest.TestCase):
    def test_uid10001_posix_readability_from_restrictive_checkout_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);root.chmod(0o755);app=root/'app';app.mkdir(mode=0o700)
            shutil.copytree('worker',app/'worker',ignore=shutil.ignore_patterns('__pycache__'))
            for path in app.rglob('*'):path.chmod(0o700 if path.is_dir() else 0o600)
            private=root/'private';private.mkdir(mode=0o700);secret=private/'secret';secret.write_text('synthetic-secret');secret.chmod(0o600)
            normalize(app)
            # Work forbids setuid and has no Docker daemon. Verify other-user DAC
            # bits here; Dockerfile additionally imports under real USER office.
            for path in [app,*app.rglob('*')]:
                required=0o005 if path.is_dir() else 0o004
                self.assertEqual(stat.S_IMODE(path.stat().st_mode)&required,required)
            result=subprocess.run([sys.executable,'-B','-c','import worker.http_client,worker.binance_shadow,worker.api_service'],cwd=app,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(stat.S_IMODE(secret.stat().st_mode),0o600);self.assertEqual(stat.S_IMODE(private.stat().st_mode),0o700)
            for path in app.rglob('*.py'):self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o644)
    def test_docker_guarantees_modes_after_copy(self):
        source=Path('Dockerfile').read_text()
        self.assertIn('RUN python /app/deploy/runtime_permissions.py',source)
        self.assertLess(source.index('COPY worker'),source.index('RUN python /app/deploy/runtime_permissions.py'))
        self.assertLess(source.index('COPY deploy/container_boot.py'),source.index('RUN python /app/deploy/runtime_permissions.py'))
        self.assertIn('USER office\nRUN python -B -c',source)
        self.assertIn('USER root\nENTRYPOINT',source)
        self.assertIn('!deploy/runtime_permissions.py',Path('.dockerignore').read_text())
    def test_source_normalizer_cannot_follow_secret_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);secret=p/'secret';secret.write_text('synthetic');secret.chmod(0o600)
            app=p/'app';app.mkdir();(app/'link').symlink_to(secret)
            with self.assertRaises(SystemExit):normalize(app)
            self.assertEqual(stat.S_IMODE(secret.stat().st_mode),0o600)
    def test_deployment_uses_readable_source_umask_scheduler_off_no_key_prompt(self):
        script=Path('deploy/update-api.sh').read_text()
        self.assertIn('umask 022',script);self.assertNotIn('umask 077',script)
        self.assertIn('OFFICE_AUTO_DRY_RUN=false',script);self.assertNotIn('down -v',script)
        self.assertNotIn('setup.py',script)
