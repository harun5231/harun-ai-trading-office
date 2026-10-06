"""Diagnostics must remain read-only and never echo provider/auth payloads."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from worker.robot_status import collect, main


class RobotStatusTests(unittest.TestCase):
    def test_only_authenticated_gets_allowlisted_output_and_no_state_creation(self):
        with tempfile.TemporaryDirectory() as root:
            token = Path(root) / 'token'
            token.write_text('private-read-token-placeholder-123456789')
            requests = []
            def opener(request, timeout):
                requests.append(request)
                self.assertEqual(request.get_method(), 'GET')
                self.assertEqual(request.get_header('Authorization'), 'Bearer ' + token.read_text())
                self.assertEqual(timeout, 10)
                return io.StringIO(json.dumps(dict(status='ONLINE', mode='DRY_RUN',
                    robot_on=True, manual_exposure=['HYPEUSDT'], setups=[],
                    api_key='NEVER_ECHO_PROVIDER_KEY', headers='NEVER_ECHO_HEADERS')))
            value = collect(root, token, opener)
            self.assertEqual([r.full_url for r in requests], [
                'http://127.0.0.1:8787/health', 'http://127.0.0.1:8787/neuroapi/status',
                'http://127.0.0.1:8787/robot/status'])
            output = json.dumps(value)
            for secret in ('private-read-token', 'NEVER_ECHO_PROVIDER_KEY', 'NEVER_ECHO_HEADERS'):
                self.assertNotIn(secret, output)
            self.assertFalse((Path(root) / 'trading').exists())
            self.assertEqual(value['locks']['cycle.lock']['status'], 'MISSING')
            self.assertEqual(value['robot']['manual_exposure'], ['HYPEUSDT'])

    def test_existing_lock_inode_and_contents_stay_unchanged_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            token = Path(root) / 'token'; token.write_text('r' * 32)
            folder = Path(root) / 'trading'; folder.mkdir()
            lock = folder / 'cycle.lock'; lock.write_bytes(b'existing-lock')
            inode = lock.stat().st_ino
            (folder / 'migration.lock').symlink_to(lock)
            value = collect(root, token, lambda *a, **k: io.StringIO('{"status":"ONLINE"}'))
            self.assertEqual(value['locks']['migration.lock']['status'], 'INVALID')
            self.assertFalse(value['locks']['migration.lock']['writable'])
            self.assertEqual(lock.stat().st_ino, inode)
            self.assertEqual(lock.read_bytes(), b'existing-lock')

    def test_failure_output_never_contains_exception_or_token(self):
        output = io.StringIO()
        with patch('worker.robot_status.collect', side_effect=OSError('SECRET_IN_EXCEPTION')), contextlib.redirect_stdout(output):
            self.assertEqual(main(), 1)
        self.assertEqual(json.loads(output.getvalue()), {'status': 'WORKER_STATUS_UNAVAILABLE'})
        self.assertNotIn('SECRET_IN_EXCEPTION', output.getvalue())
