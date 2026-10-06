"""Static source repair fixtures: never import a gateway or run Docker/HTTP."""
import ast
from contextlib import redirect_stderr, redirect_stdout
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('existing_gateway_repair', HERE.parent / 'deploy' / 'repair_existing_gateway.py')
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)
SECRET = b'SYNTHETIC_SECRET_NEVER_PRINT_OR_IMPORT'
PREFIX = (b'# observed module\nimport hashlib, hmac\n'
          b"SYNTHETIC_SECRET = '" + SECRET + b"'\n"
          b'class _MissingOrderImplementation:\n    pass\n\n'
          b'def build_intent(plan, operation):\n    return (plan, operation)\n\n'
          b'def require_implementation(gateway):\n    return gateway\n\n')
OLD_CLASS = (b'class OrderGateway(_MissingOrderImplementation):\n'
             b"    def __init__(self, api_key='', api_secret='', base_url='https://fapi.binance.com'):\n        self.api_key = api_key\n"
             b'    def status(self):\n        return {"connected": True}\n'
             b'    def _sign(self, values):\n        return values\n'
             b'    def _get_server_time(self):\n        return 0\n'
             b'    def _request(self, method, path):\n        return method, path\n'
             b'    def submit(self, intent):\n        return intent\n'
             b'    def reconcile(self, intent):\n        return intent')
SUFFIX = (b'  # retained final comment\n\n'
          b'def another_function():\n    return "unchanged Unicode: caf\xc3\xa9"\n'
          b'raise RuntimeError("SDK_MUST_NEVER_BE_IMPORTED")')
TEMPLATE = (b'"""Published class template only."""\n'
            b'class OrderGateway(_MissingOrderImplementation):\n'
            b'    def __init__(self, api_key="", api_secret="", base_url="https://fapi.binance.com"):\n        self.api_key = api_key\n'
            b'    def status(self):\n        return {"connected": False}\n'
            b'    def submit(self, intent):\n        return {"fixture": intent}\n'
            b'    def reconcile(self, intent):\n        return {"fixture": intent}')


class GatewayRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='gateway-repair-fixture-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir(mode=0o700)
        (self.project / 'worker').mkdir(mode=0o700)
        self.gateway = self.project / 'worker' / 'order_gateway.py'
        self.original = PREFIX + OLD_CLASS + SUFFIX
        self.gateway.write_bytes(self.original)
        self.gateway.chmod(0o640)
        self.template = self.root / 'template.py'
        self.template.write_bytes(TEMPLATE)
        self.template.chmod(0o600)
        self.compose = self.project / 'compose.yaml'
        self.compose.write_bytes(b'services: {}\n')
        self.docker = self.project / 'Dockerfile'
        self.docker.write_bytes(b'# CUSTOM_DOCKER_UNTOUCHED\n')
        self.untracked = self.project / 'compose.hotfix.yaml'
        self.untracked.write_bytes(b'# USER_FILE_UNTOUCHED\n')
        subprocess.run(['git', 'init', '--quiet', '--template=', str(self.project)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.off = patch.object(tool, 'require_robot_off')
        self.guard = self.off.start()
        self.addCleanup(self.off.stop)

    def invoke(self, *options, expected=None, template=True):
        arguments = ['--project', str(self.project), '--expected-gateway-sha', expected or tool.digest(self.original)]
        if template: arguments += ['--template', str(self.template)]
        arguments += [str(value) for value in options]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = tool.main(arguments)
        output = out.getvalue() + err.getvalue()
        self.assertNotIn(SECRET.decode(), output)
        self.assertNotIn('SDK_MUST_NEVER_BE_IMPORTED', output)
        return result, output

    def backups(self):
        path = self.root / 'project-gateway-repair-backups'
        return sorted(path.iterdir()) if path.exists() else []

    def assert_other_files_preserved(self):
        self.assertEqual(self.docker.read_bytes(), b'# CUSTOM_DOCKER_UNTOUCHED\n')
        self.assertEqual(self.compose.read_bytes(), b'services: {}\n')
        self.assertEqual(self.untracked.read_bytes(), b'# USER_FILE_UNTOUCHED\n')

    def test_observed_fingerprint_and_inspection_default_do_not_mutate(self):
        self.assertEqual(tool.EXPECTED_GATEWAY_SHA, '74b1db1b51461af34d0d4f0ecc189d4876bc72a6e021a049f9dcd22c9dd3509c')
        before = self.gateway.stat().st_ino
        result, output = self.invoke()
        self.assertEqual(result, 0)
        self.assertIn('INSPECTION_ONLY', output)
        self.assertEqual(self.gateway.read_bytes(), self.original)
        self.assertEqual(self.gateway.stat().st_ino, before)
        self.assertEqual(self.guard.call_count, 1)
        self.assertEqual(self.backups(), [])
        self.assert_other_files_preserved()

    def test_apply_only_class_preserves_helpers_suffix_metadata_and_private_backup(self):
        old_info = self.gateway.stat()
        result, output = self.invoke('--apply')
        self.assertEqual(result, 0)
        self.assertIn('GATEWAY_REPAIRED', output)
        replacement = TEMPLATE[TEMPLATE.index(b'class OrderGateway'):]
        self.assertEqual(self.gateway.read_bytes(), PREFIX + replacement + SUFFIX)
        self.assertEqual(self.guard.call_count, 2)
        info = self.gateway.stat()
        self.assertEqual((info.st_mode, info.st_uid, info.st_gid), (old_info.st_mode, old_info.st_uid, old_info.st_gid))
        backup, = self.backups()
        self.assertFalse(backup.is_relative_to(self.project))
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        self.assertEqual({path.name for path in backup.iterdir()}, {'order_gateway.py', 'manifest.json'})
        for path in backup.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual((backup / 'order_gateway.py').read_bytes(), self.original)
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        self.assertEqual(manifest['target_sha256'], tool.digest(self.gateway.read_bytes()))
        self.assertEqual(manifest['template_sha256'], tool.digest(TEMPLATE))
        self.assert_other_files_preserved()

    def test_crlf_multiline_utf8_and_eof_variants_preserve_outside_class(self):
        for newline in (b'\n', b'\r\n'):
            for final in (b'', newline):
                with self.subTest(newline=newline, final=bool(final)):
                    original = (PREFIX + OLD_CLASS.replace(b'def status(self):', b'def status(\n        self,\n    ):') + SUFFIX).replace(b'\n', newline) + final
                    changed = tool.repaired_bytes(original, TEMPLATE, tool.digest(original))
                    cls = tool.class_node(original)
                    first, last = tool.source_span(original, cls)
                    replacement = TEMPLATE[TEMPLATE.index(b'class OrderGateway'):].replace(b'\n', newline)
                    self.assertEqual(changed, original[:first] + replacement + original[last:])
                    ast.parse(changed)

    def test_restore_exact_original_and_refuse_later_edits(self):
        self.assertEqual(self.invoke('--apply')[0], 0)
        backup, = self.backups()
        repaired = self.gateway.read_bytes()
        self.gateway.write_bytes(repaired + b'\n# developer later change\n')
        before = self.gateway.read_bytes()
        result, output = self.invoke('--restore', backup, template=False)
        self.assertEqual(result, 1)
        self.assertIn('RESTORE_TARGET_CHANGED', output)
        self.assertEqual(self.gateway.read_bytes(), before)
        self.gateway.write_bytes(repaired)
        self.assertEqual(self.invoke('--restore', backup, template=False)[0], 0)
        self.assertEqual(self.gateway.read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(self.gateway.stat().st_mode), 0o640)
        self.assert_other_files_preserved()

    def test_repeated_apply_refuses_changed_fingerprint_without_new_backup(self):
        self.assertEqual(self.invoke('--apply')[0], 0)
        before, backups = self.gateway.read_bytes(), self.backups()
        result, output = self.invoke('--apply')
        self.assertEqual(result, 1)
        self.assertIn('GATEWAY_FINGERPRINT_MISMATCH', output)
        self.assertEqual(self.gateway.read_bytes(), before)
        self.assertEqual(self.backups(), backups)

    def test_wrong_sha_or_method_shape_never_mutates(self):
        result, output = self.invoke('--apply', expected='0' * 64)
        self.assertEqual(result, 1)
        self.assertIn('GATEWAY_FINGERPRINT_MISMATCH', output)
        for raw in (self.original.replace(b'def _sign', b'def unexpected'),
                    self.original + b'\nclass OrderGateway:\n    pass\n',
                    self.original.replace(b'class OrderGateway', b'@unknown\nclass OrderGateway')):
            with self.subTest(raw=tool.digest(raw)):
                self.gateway.write_bytes(raw)
                result, _ = self.invoke('--apply', expected=tool.digest(raw))
                self.assertEqual(result, 1)
                self.assertEqual(self.gateway.read_bytes(), raw)
        self.assertEqual(self.backups(), [])

    def test_template_top_level_code_or_missing_methods_is_refused(self):
        for raw in (TEMPLATE + b'\nimport requests\n', TEMPLATE + b'\nraise RuntimeError("never execute")\n',
                    TEMPLATE.replace(b'def submit', b'def missing_submit')):
            with self.subTest(raw=tool.digest(raw)):
                self.template.write_bytes(raw)
                self.assertEqual(self.invoke('--apply')[0], 1)
                self.assertEqual(self.gateway.read_bytes(), self.original)
        self.assertEqual(self.backups(), [])

    def test_individually_bounded_inputs_cannot_produce_unrestorable_oversize_target(self):
        padding = b'x' * (tool.LIMIT // 2 + 1024)
        original = b'PRESERVED_METADATA = "' + padding + b'"\n' + self.original
        template = TEMPLATE + b'\n    replacement_metadata = "' + padding + b'"\n'
        self.assertLess(len(original), tool.LIMIT)
        self.assertLess(len(template), tool.LIMIT)
        self.gateway.write_bytes(original)
        self.template.write_bytes(template)
        result, output = self.invoke('--apply', expected=tool.digest(original))
        self.assertEqual(result, 1)
        self.assertIn('REPAIRED_SOURCE_TOO_LARGE', output)
        self.assertEqual(self.gateway.read_bytes(), original)
        self.assertEqual(self.backups(), [])

    def test_off_failure_at_initial_check_or_before_rename_keeps_original(self):
        self.guard.side_effect = tool.Refuse('ROBOT_OFF_CHECK_FAILED')
        self.assertEqual(self.invoke('--apply')[0], 1)
        self.assertEqual(self.backups(), [])
        self.guard.side_effect = (None, tool.Refuse('ROBOT_OFF_CHECK_FAILED'))
        self.assertEqual(self.invoke('--apply')[0], 1)
        self.assertEqual(self.gateway.read_bytes(), self.original)
        self.assertFalse(list(self.gateway.parent.glob('.gateway-repair-*')))
        self.assert_other_files_preserved()

    def test_symlink_hardlink_and_oversize_sources_or_templates_are_refused(self):
        for path in (self.gateway, self.template):
            original = path.read_bytes()
            for link in ('symlink', 'hardlink'):
                with self.subTest(path=path.name, link=link):
                    held = self.root / ('held-' + path.name)
                    path.rename(held)
                    path.symlink_to(held) if link == 'symlink' else os.link(held, path)
                    try:
                        self.assertEqual(self.invoke('--apply')[0], 1)
                        self.assertEqual(held.read_bytes(), original)
                    finally:
                        path.unlink()
                        held.rename(path)
            path.write_bytes(b' ' * (tool.LIMIT + 1))
            self.assertEqual(self.invoke('--apply')[0], 1)
            path.write_bytes(original)
        self.assertEqual(self.backups(), [])

    def test_backup_inside_repository_is_refused(self):
        self.assertEqual(self.invoke('--apply', '--backup-root', self.project / 'backup')[0], 1)
        self.assertEqual(self.gateway.read_bytes(), self.original)
        self.assertFalse((self.project / 'backup').exists())

    def test_new_backup_root_parent_is_synced_before_gateway_replacement(self):
        synced = []
        fsync, replace = tool.os.fsync, tool.replace_target
        def sync(fd):
            synced.append(os.fstat(fd).st_ino)
            return fsync(fd)
        def replace_after_backup(item, project):
            self.assertIn(self.root.stat().st_ino, synced)
            backup, = self.backups()
            self.assertTrue((backup / 'manifest.json').is_file())
            return replace(item, project)
        with patch.object(tool.os, 'fsync', side_effect=sync), patch.object(tool, 'replace_target', side_effect=replace_after_backup):
            self.assertEqual(self.invoke('--apply')[0], 0)

    def test_concurrent_target_lock_refuses_repair(self):
        with self.gateway.open('rb') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result, output = self.invoke('--apply')
        self.assertEqual(result, 1)
        self.assertIn('GATEWAY_REPAIR_BUSY', output)
        self.assertEqual(self.gateway.read_bytes(), self.original)
        self.assertEqual(self.backups(), [])

    def test_target_edit_during_second_off_check_is_preserved(self):
        changed = self.original + b'\n# newer developer change\n'
        count = [0]
        def off(project):
            count[0] += 1
            if count[0] == 2: self.gateway.write_bytes(changed)
        self.guard.side_effect = off
        result, output = self.invoke('--apply')
        self.assertEqual(result, 1)
        self.assertIn('TARGET_CHANGED', output)
        self.assertEqual(self.gateway.read_bytes(), changed)
        self.assertFalse(list(self.gateway.parent.glob('.gateway-repair-*')))

    def test_docker_off_reader_is_fixed_read_only_and_failure_is_sanitized(self):
        self.off.stop()
        result = SimpleNamespace(returncode=0, stdout=tool.OFF_TOKEN)
        with patch.object(tool.subprocess, 'run', return_value=result) as execute:
            tool.require_robot_off(self.project)
        arguments = execute.call_args
        self.assertEqual(arguments.args[0][:4], ['docker', 'compose', '-f', str(self.compose)])
        self.assertIn('--user', arguments.args[0])
        self.assertEqual(arguments.kwargs['input'], tool.OFF_READER)
        self.assertIn(b'mode=ro', tool.OFF_READER)
        self.assertIn(b'PRAGMA query_only=ON', tool.OFF_READER)
        self.assertIn(b'SELECT enabled FROM robot_settings WHERE id=1', tool.OFF_READER)
        self.assertNotIn(b'worker.', tool.OFF_READER)
        with patch.object(tool.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout=SECRET)):
            with self.assertRaisesRegex(tool.Refuse, '^ROBOT_OFF_CHECK_FAILED$'):
                tool.require_robot_off(self.project)


if __name__ == '__main__': unittest.main()
