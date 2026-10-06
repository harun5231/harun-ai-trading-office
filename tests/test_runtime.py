"""Deployment boundary and bounded state repair checks, without Docker or keys."""
from pathlib import Path
import re
import stat
import tempfile
import unittest
from unittest.mock import patch

from deploy.container_boot import STATE_FILES, repair_state_locks


ROOT = Path(__file__).resolve().parents[1]


class RuntimeBoundaryTests(unittest.TestCase):
    def test_proxy_exposes_only_current_status_and_control_routes(self):
        source = (ROOT / 'deploy/Caddyfile.docker').read_text()
        match = re.search(r'^\s*@api path (.+)$', source, re.M)
        self.assertIsNotNone(match)
        self.assertEqual(set(match.group(1).split()), {
            '/health', '/robot/status', '/office/status', '/robot/settings',
        })
        self.assertIn('respond 404', source)

    def test_deployment_has_no_alternate_startup_trigger(self):
        for path in ('.env.example', 'compose.yaml', 'deploy/update-api.sh'):
            source = (ROOT / path).read_text()
            with self.subTest(path=path):
                self.assertNotIn('OFFICE_AUTO_DRY_RUN', source)
                self.assertNotIn('OFFICE_RUN_AT', source)
        script = (ROOT / 'deploy/update-api.sh').read_text()
        self.assertLess(script.index('docker compose build worker'),
                        script.index('docker compose stop -t 660 worker'))
        self.assertNotIn('down -v', script)
        self.assertIn('--user 10001:10001', script)

    def test_image_checks_the_single_pipeline_modules(self):
        source = (ROOT / 'Dockerfile').read_text()
        self.assertIn('import worker.api_service, worker.robot, worker.order_gateway, worker.binance_office', source)
        for retired in ('worker.binance_shadow', 'worker.binance_live', 'worker.live_supervisor'):
            self.assertNotIn(retired, source)


class StateRepairTests(unittest.TestCase):
    def test_known_database_and_audit_backup_keep_bytes_and_inode(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'trading'
            directory.mkdir()
            before = {}
            for name in STATE_FILES:
                path = directory / name
                payload = ('audit:' + name).encode()
                path.write_bytes(payload)
                before[name] = (path.stat().st_ino, payload)
            with patch('deploy.container_boot.os.fchown'):
                repair_state_locks(directory)
            for name, (inode, payload) in before.items():
                path = directory / name
                self.assertEqual(path.stat().st_ino, inode)
                self.assertEqual(path.read_bytes(), payload)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn('ledger-pre-order.sqlite3', STATE_FILES)

    def test_audit_backup_link_rejected_before_permissions_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / 'trading'
            directory.mkdir()
            (directory / 'cycle.lock').write_bytes(b'claim')
            target = root / 'private'
            target.write_bytes(b'untouched')
            (directory / 'ledger-pre-order.sqlite3').symlink_to(target)
            with patch('deploy.container_boot.os.fchown') as change:
                with self.assertRaisesRegex(SystemExit, '^WORKER_STATE_LOCK_INVALID$'):
                    repair_state_locks(directory)
                change.assert_not_called()
            self.assertEqual(target.read_bytes(), b'untouched')

    def test_unknown_file_is_never_changed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            unknown = directory / 'operator-audit.txt'
            unknown.write_bytes(b'private operator record')
            unknown.chmod(0o640)
            before = unknown.stat()
            with patch('deploy.container_boot.os.fchown'):
                repair_state_locks(directory)
            self.assertEqual(unknown.stat().st_ino, before.st_ino)
            self.assertEqual(stat.S_IMODE(unknown.stat().st_mode), 0o640)
            self.assertEqual(unknown.read_bytes(), b'private operator record')


if __name__ == '__main__':
    unittest.main()
