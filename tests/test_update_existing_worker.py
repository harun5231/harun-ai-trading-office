"""Pinned source updates with offline Git material and mocked Docker OFF checks."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('existing_worker_update', ROOT / 'deploy/update_existing_worker.py')
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)
SECRET = b'SYNTHETIC_PRIVATE_SOURCE_DO_NOT_PRINT'


class ExistingWorkerUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = json.loads((ROOT / 'tests/fixtures/vps_worker_baseline.json').read_text(encoding='utf-8'))
        assert set(fixture) == {name for name, pair in tool.HASHES.items() if pair[0] is not None}
        cls.old = {name: raw.encode('utf-8') for name, raw in fixture.items()}
        cls.new = {name: (ROOT / 'worker' / name).read_bytes() for name in tool.HASHES}
        cls.anchors = {name: (ROOT / 'worker' / name).read_bytes() for name in tool.ANCHORS}
        for name, pair in tool.HASHES.items():
            if pair[0] is not None: assert tool.digest(cls.old[name]) == pair[0]
            assert tool.digest(cls.new[name]) == pair[1]
        for name, raw in cls.anchors.items(): assert tool.digest(raw) == tool.ANCHORS[name]

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir(mode=0o700)
        self.worker = self.project / 'worker'
        self.worker.mkdir(mode=0o700)
        for name, raw in self.old.items():
            path = self.worker / name
            path.write_bytes(raw)
            path.chmod(0o640)
        for name, raw in self.anchors.items(): (self.worker / name).write_bytes(raw)
        self.gateway = self.worker / 'order_gateway.py'
        self.gateway.write_bytes(b"raise RuntimeError('ADAPTER_MUST_NEVER_BE_IMPORTED')\n# " + SECRET)
        self.gateway.chmod(0o600)
        self.docker = self.project / 'Dockerfile'
        self.docker.write_bytes(b'FROM private-sdk\n# custom immutable build\n')
        self.docker.chmod(0o640)
        self.untracked = self.project / 'custom-untracked.txt'
        self.untracked.write_bytes(SECRET)
        self.calls = []
        self.off_checks = 0
        self.off = True
        self.new_material = dict(self.new)

    def external(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[0] == 'docker':
            self.off_checks += 1
            self.assertEqual(command[command.index('--project-directory') + 1], str(self.project))
            self.assertEqual(command[command.index('-f') + 1], str(self.project / 'compose.yaml'))
            self.assertEqual(kwargs['input'], tool.OFF_SCRIPT.encode())
            self.assertIn('-I', command)
            self.assertIn('-S', command)
            self.assertNotIn('order_gateway', kwargs['input'].decode())
            return subprocess.CompletedProcess(command, 0 if self.off else 1, b'ROBOT_OFF\n' if self.off else b'')
        if '--show-toplevel' in command:
            return subprocess.CompletedProcess(command, 0, os.fsencode(self.project) + b'\n')
        if 'show' in command:
            reference = command[-1]
            self.assertTrue(reference.startswith(tool.TARGET_COMMIT + ':worker/'))
            name = reference.split('/')[-1]
            return subprocess.CompletedProcess(command, 0, self.new_material[name])
        self.fail('Unexpected external operation')

    def invoke(self, *arguments):
        output = io.StringIO()
        with patch.object(tool.subprocess, 'run', side_effect=self.external), redirect_stdout(output):
            status = tool.main(['--project', str(self.project), *map(str, arguments)])
        self.assertNotIn(SECRET.decode(), output.getvalue())
        self.assertNotIn('ADAPTER_MUST_NEVER_BE_IMPORTED', output.getvalue())
        return status, json.loads(output.getvalue())

    def snapshot(self):
        return {str(path.relative_to(self.project)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_uid, path.stat().st_gid)
                for path in self.project.rglob('*') if path.is_file()}

    def backups(self):
        path = self.root / 'project-worker-update-backups'
        return list(path.iterdir()) if path.exists() else []

    def test_inspect_default_changes_nothing_and_runs_only_read_operations(self):
        before = self.snapshot()
        status, result = self.invoke()
        self.assertEqual(status, 0)
        self.assertEqual(result['status'], 'UPDATE_AVAILABLE')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.off_checks, 1)
        self.assertEqual(len([call for call in self.calls if 'show' in call[0]]), 5)
        self.assertFalse(any(set(command) & {'fetch','pull','switch','reset','build','restart'} for command, _ in self.calls))

    def test_apply_updates_exactly_five_files_preserves_custom_files_and_metadata(self):
        before = self.snapshot()
        status, result = self.invoke('--apply')
        self.assertEqual((status, result['status']), (0, 'APPLIED'))
        self.assertEqual(self.off_checks, 2)
        after = self.snapshot()
        for name in self.new:
            self.assertEqual((self.worker / name).read_bytes(), self.new[name])
            self.assertEqual(stat.S_IMODE((self.worker / name).stat().st_mode), 0o644 if name == 'research_guard.py' else 0o640)
        for name, value in before.items():
            if name not in {'worker/' + name for name in self.new}: self.assertEqual(after[name], value)
        backup = Path(result['backup'])
        self.assertFalse(backup.is_relative_to(self.project))
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        for path in backup.iterdir(): self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        metadata = json.loads((backup / 'manifest.json').read_bytes())
        self.assertEqual(set(metadata['files']), set(tool.HASHES))
        self.assertEqual(metadata['target_commit'], tool.TARGET_COMMIT)
        self.assertFalse((backup / 'research_guard.py.old').exists())

    def test_all_target_bytes_are_idempotent_even_with_new_custom_sdk_hash(self):
        self.invoke('--apply')
        self.gateway.write_bytes(b'# separately repaired SDK\n' + SECRET)
        before = self.snapshot()
        status, result = self.invoke('--apply')
        self.assertEqual((status, result['status']), (0, 'UNCHANGED'))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.backups()), 1)

    def test_restore_returns_only_five_original_files_and_removes_added_guard(self):
        before = self.snapshot()
        _, result = self.invoke('--apply')
        self.gateway.write_bytes(b'# repaired after research update\n' + SECRET)
        repaired = self.gateway.read_bytes()
        status, restored = self.invoke('--restore-backup', result['backup'])
        self.assertEqual((status, restored['status']), (0, 'RESTORED'))
        before['worker/order_gateway.py'] = (repaired, 0o600, self.gateway.stat().st_uid, self.gateway.stat().st_gid)
        self.assertEqual(self.snapshot(), before)

    def test_modified_target_blocks_restore_without_overwrite(self):
        _, result = self.invoke('--apply')
        (self.worker / 'market.py').write_bytes(b'# concurrent operator change\n' + SECRET)
        before = self.snapshot()
        status, error = self.invoke('--restore-backup', result['backup'])
        self.assertEqual((status, error['error']), (1, 'RESTORE_TARGET_CHANGED'))
        self.assertEqual(self.snapshot(), before)

    def test_baseline_mismatch_refuses_with_no_backup(self):
        (self.worker / 'robot.py').write_bytes(b'# custom core\n' + SECRET)
        before = self.snapshot()
        status, error = self.invoke('--apply')
        self.assertEqual((status, error['error']), (1, 'SOURCE_BASELINE_MISMATCH'))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backups(), [])

    def test_incompatible_untargeted_core_is_rejected(self):
        (self.worker / 'core.py').write_bytes(b'# custom unrelated core\n')
        before = self.snapshot()
        status, error = self.invoke('--apply')
        self.assertEqual((status, error['error']), (1, 'CORE_BASELINE_MISMATCH'))
        self.assertEqual(self.snapshot(), before)

    def test_mixed_old_new_targets_are_refused(self):
        (self.worker / 'market.py').write_bytes(self.new['market.py'])
        before = self.snapshot()
        status, error = self.invoke('--apply')
        self.assertEqual((status, error['error']), (1, 'SOURCE_BASELINE_MISMATCH'))
        self.assertEqual(self.snapshot(), before)

    def test_wrong_pinned_git_material_is_rejected_without_writes(self):
        self.new_material['market.py'] = b'# forged Git bytes\n' + SECRET
        before = self.snapshot()
        status, error = self.invoke('--apply')
        self.assertEqual((status, error['error']), (1, 'PINNED_SOURCE_MISMATCH'))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backups(), [])

    def test_existing_unknown_guard_is_not_replaced(self):
        guard = self.worker / 'research_guard.py'
        guard.write_bytes(b'# independent operator file\n' + SECRET)
        before = self.snapshot()
        status, error = self.invoke('--apply')
        self.assertEqual((status, error['error']), (1, 'SOURCE_BASELINE_MISMATCH'))
        self.assertEqual(self.snapshot(), before)

    def test_sdk_changed_while_reading_git_material_is_preserved_and_refused(self):
        check = self.external
        changed = b'# concurrent SDK repair\n' + SECRET

        def concurrent(command, **kwargs):
            if 'show' in command and command[-1].endswith('market.py'):
                self.gateway.write_bytes(changed)
            return check(command, **kwargs)

        with patch.object(self, 'external', side_effect=concurrent):
            status, error = self.invoke('--apply')
        self.assertEqual((status, error['error']), (1, 'TARGET_CHANGED'))
        self.assertEqual(self.gateway.read_bytes(), changed)
        self.assertEqual((self.worker / 'robot.py').read_bytes(), self.old['robot.py'])
        self.assertEqual(self.backups(), [])

    def test_robot_on_prevents_even_inspection(self):
        self.off = False
        before = self.snapshot()
        status, error = self.invoke('--apply')
        self.assertEqual((status, error['error']), (1, 'ROBOT_OFF_REQUIRED'))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backups(), [])

    def test_second_off_check_turning_on_leaves_all_sources_untouched(self):
        before = self.snapshot()
        check = self.external

        def turning_on(command, **kwargs):
            if command[0] == 'docker' and self.off_checks == 1: self.off = False
            return check(command, **kwargs)

        with patch.object(self, 'external', side_effect=turning_on):
            status, error = self.invoke('--apply')
        self.assertEqual((status, error['error']), (1, 'ROBOT_OFF_REQUIRED'))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(path.name.startswith('.worker-update-') for path in self.worker.iterdir()))

    def test_mid_batch_failure_rolls_back_every_installed_source(self):
        before = self.snapshot()
        replace = os.replace
        count = 0

        def failing(source, destination, **kwargs):
            nonlocal count
            if source.startswith('.worker-update-'):
                count += 1
                if count == 3: raise OSError('private exception must be redacted')
            return replace(source, destination, **kwargs)

        with patch.object(tool.os, 'replace', side_effect=failing):
            status, error = self.invoke('--apply')
        self.assertEqual((status, error['error']), (1, 'WORKER_UPDATE_FAILED'))
        self.assertEqual(self.snapshot(), before)

    def test_concurrent_edit_is_not_overwritten_during_rollback(self):
        replace = os.replace
        count = 0
        concurrent = b'# new operator revision\n' + SECRET

        def failing(source, destination, **kwargs):
            nonlocal count
            if source.startswith('.worker-update-'):
                count += 1
                if count == 3:
                    (self.worker / 'robot.py').write_bytes(concurrent)
                    raise OSError('redact')
            return replace(source, destination, **kwargs)

        with patch.object(tool.os, 'replace', side_effect=failing):
            status, error = self.invoke('--apply')
        self.assertEqual((status, error['error']), (1, 'ROLLBACK_NEEDS_REVIEW'))
        self.assertEqual((self.worker / 'robot.py').read_bytes(), concurrent)
        self.assertEqual((self.worker / 'binance_private.py').read_bytes(), self.old['binance_private.py'])
        self.assertEqual(self.gateway.read_bytes(), b"raise RuntimeError('ADAPTER_MUST_NEVER_BE_IMPORTED')\n# " + SECRET)

    def test_concurrent_new_guard_creation_is_never_overwritten(self):
        link = os.link
        changed = b'# operator owns this new guard\n' + SECRET

        def concurrent(source, destination, **kwargs):
            if destination == 'research_guard.py':
                (self.worker / destination).write_bytes(changed)
            return link(source, destination, **kwargs)

        with patch.object(tool.os, 'link', side_effect=concurrent):
            status, error = self.invoke('--apply')
        self.assertEqual((status, error['error']), (1, 'ROLLBACK_NEEDS_REVIEW'))
        self.assertEqual((self.worker / 'research_guard.py').read_bytes(), changed)
        for name in self.old: self.assertEqual((self.worker / name).read_bytes(), self.old[name])

    def test_links_and_unknown_guard_do_not_authorize_updates(self):
        target = self.worker / 'market.py'
        original = target.read_bytes()
        target.unlink()
        outside = self.root / 'outside'
        outside.write_bytes(original)
        target.symlink_to(outside)
        status, error = self.invoke('--apply')
        self.assertEqual((status, error['error']), (1, 'UNSAFE_FILE'))
        self.assertEqual(outside.read_bytes(), original)
        self.assertEqual(self.backups(), [])

    def test_backup_inside_project_is_refused(self):
        before = self.snapshot()
        status, error = self.invoke('--apply', '--backup-root', self.project / 'private')
        self.assertEqual((status, error['error']), (1, 'BACKUP_MUST_BE_OUTSIDE_PROJECT'))
        self.assertEqual(self.snapshot(), before)


class IndependentOffGuardTests(unittest.TestCase):
    def test_guard_reads_only_boolean_setting_without_loading_sdk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'trading').mkdir()
            path = root / 'trading/ledger.sqlite3'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE robot_settings(id INTEGER PRIMARY KEY,enabled INTEGER,risk TEXT)')
            db.execute("INSERT INTO robot_settings VALUES(1,0,'private setting')")
            db.commit()
            environment = dict(os.environ, OFFICE_DATA_DIR=str(root))
            before = path.read_bytes()
            result = subprocess.run([sys.executable, '-I', '-B', '-S', '-'], input=tool.OFF_SCRIPT.encode(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, check=False)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'ROBOT_OFF\n', b''))
            self.assertEqual(path.read_bytes(), before)
            db.execute('UPDATE robot_settings SET enabled=1')
            db.commit()
            result = subprocess.run([sys.executable, '-I', '-B', '-S', '-'], input=tool.OFF_SCRIPT.encode(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, check=False)
            self.assertEqual((result.returncode, result.stdout), (1, b''))
            db.close()


if __name__ == '__main__':
    unittest.main()
