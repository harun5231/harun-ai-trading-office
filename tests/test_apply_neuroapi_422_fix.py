"""Offline installer proof and failure rollback; no Docker or network is used."""
import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'deploy/apply_neuroapi_422_fix.sh').read_text()
PRIVATE_SDK_SHA = 'df6705bbdda9f2a34374fde186244d400e2fc84ecb8f27ce05ad15f35a9bbfc4'
OLD = {
    'worker/neuroapi.py': 'cf3445af2972b8227e9fc129c60fcf03de49253d28149948ae0a21f03ca7704c',
    'worker/robot.py': 'cc0e1c6b68581fa23402dd61c9de023941a1f13489137282c83ae32ccf0dfa8d',
    'worker/diagnostics.py': '2b28a42b0f03c76c97dc5111a33b2be167d346f8aba1103a7c0be10f08382f41',
}
NEW = {
    'worker/neuroapi.py': '23f78304f76b85af4c98bf9e8eff96a4e04572b0e3aa61a8b1874f008b00c3c5',
    'worker/robot.py': '1f0ca77f82d4252bdd74fb4af83f32cff1f3da0cdc72d2faab86b5788a5c4423',
    'worker/diagnostics.py': '7d2754c5ce0f3badb8b826db6a14705e1d204af75ac22e64f11c0a677f85ba19',
    'worker/neuroapi_request.py': 'a98f0dc33d5ddf69fed1b64e04df30ce8ed03b0624e113ab1627b56f8d791bbb',
}


def block(label):
    return SCRIPT.split("<<'" + label + "'", 1)[1].split('\n', 1)[1].split('\n' + label, 1)[0] + '\n'


def source_map(root):
    paths = list((root / 'worker').rglob('*.py')) + [root / 'deploy/container_boot.py', root / 'deploy/runtime_permissions.py']
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


class Neuroapi422InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        (self.root / 'worker').mkdir(parents=True)
        (self.root / 'deploy').mkdir()
        # Undo this new patch from the frozen current modules to reconstruct
        # exactly the VPS base. SDK bytes are intentionally private/unpublished.
        for name in NEW:
            (self.root / name).write_bytes((ROOT / name).read_bytes())
        subprocess.run(['git', 'apply', '--reverse', '-'], cwd=self.root,
                       input=block('HARUN_RUNTIME_PATCH').encode(), check=True, capture_output=True)
        for name, sha in OLD.items():
            self.assertEqual(hashlib.sha256((self.root / name).read_bytes()).hexdigest(), sha)
        namespace = {'__name__': 'offline_exit_cancel_patcher'}
        exec(compile((ROOT / 'deploy/patch_exit_cancel_readback.py').read_text(), 'sdk_patch.py', 'exec'), namespace)
        self.patcher = namespace
        template = (ROOT / 'deploy/live_gateway_template.py').read_bytes()
        cls = next(n for n in ast.parse(template).body if isinstance(n, ast.ClassDef))
        lines = template.splitlines(keepends=True)
        source = b''.join(lines[cls.lineno-1:cls.end_lineno])
        old = namespace['OLD_METHOD'].encode()
        new = namespace['NEW_METHOD'].encode()
        if new in source:
            source = source.replace(new, old, 1)
        self.assertIn(old, source)
        self.sdk = (b"PRIVATE_KEY = 'unpublished-sdk-secret'\nclass _MissingOrderImplementation:\n    pass\n\n"
                    + source + b"\n\ndef custom_status():\n    return 'private-adapter'\n")
        self.sdk_sha = hashlib.sha256(self.sdk).hexdigest()
        (self.root / 'worker/order_gateway.py').write_bytes(self.sdk)
        for name in ('container_boot', 'runtime_permissions'):
            (self.root / ('deploy/' + name + '.py')).write_text('pass\n')
        (self.root / 'compose.yaml').write_text('services: {}\n')

    def apply_patch(self):
        subprocess.run(['git', 'apply', '--check', '-'], cwd=self.root,
                       input=block('HARUN_RUNTIME_PATCH').encode(), check=True, capture_output=True)
        subprocess.run(['git', 'apply', '-'], cwd=self.root,
                       input=block('HARUN_RUNTIME_PATCH').encode(), check=True, capture_output=True)

    def python_block(self, label, *args):
        return subprocess.run([sys.executable, '-I', '-B', '-S', '-', *map(str, args)],
                              cwd=self.root, input=block(label).replace(PRIVATE_SDK_SHA, self.sdk_sha), text=True, capture_output=True)

    def test_patch_targets_four_expected_sources_and_preserves_private_sdk(self):
        before = source_map(self.root)
        self.apply_patch()
        after = source_map(self.root)
        for name, sha in NEW.items():
            self.assertEqual(after[name], sha)
        self.assertEqual((self.root / 'worker/order_gateway.py').read_bytes(), self.sdk)
        self.assertEqual({name: sha for name, sha in after.items() if name not in NEW},
                         {name: sha for name, sha in before.items() if name not in OLD})

    def test_inventory_guard_detects_extra_files_and_changed_source(self):
        expected = source_map(self.root)
        result = self.python_block('HARUN_SOURCE_CHECK', json.dumps(expected), self.root, 'absent')
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.root / 'worker/neuroapi_request.py').write_text('foreign=1\n')
        result = self.python_block('HARUN_SOURCE_CHECK', json.dumps(expected), self.root, 'absent')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SOURCE_INVENTORY_CHANGED', result.stderr)
        (self.root / 'worker/neuroapi_request.py').unlink()
        (self.root / 'worker/order_gateway.py').write_text('different_sdk=1\n')
        result = self.python_block('HARUN_SOURCE_CHECK', json.dumps(expected), self.root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SOURCE_MISMATCH:worker/order_gateway.py', result.stderr)

    def test_embedded_helper_is_exact_frozen_case_helper(self):
        embedded = block('HARUN_SETTLE_422')
        self.assertEqual(embedded, (ROOT / 'deploy/settle_sol_request_422.py').read_text())
        self.assertEqual(hashlib.sha256(embedded.encode()).hexdigest(),
                         '447e07d7901e489bdb74cb7f8a302b082e7eeddb9c398b19f37bcf7c0bcacab3')
        self.assertIn("'POSITION_PROTECTED'", embedded)
        self.assertIn("'UNRESOLVED_ORDER'", embedded)
        self.assertIn("'CLOSED'", embedded)
        self.assertIn("'8389766291712624741'", embedded)

    def test_embedded_sdk_patcher_is_exact_fixed_pin_source_only_helper(self):
        embedded = block('HARUN_SDK_READBACK_PATCH')
        self.assertEqual(embedded, (ROOT / 'deploy/patch_exit_cancel_readback.py').read_text())
        self.assertEqual(hashlib.sha256(embedded.encode()).hexdigest(),
                         '6d61b15721d3a510e3b3e64e464d768bc1e5cde8800686aa804d5af0c524f855')
        self.assertIn("SOURCE_SHA = '" + PRIVATE_SDK_SHA + "'", embedded)
        self.assertNotIn('import worker.order_gateway', embedded)
        changed = self.patcher['transform'](self.sdk, self.sdk_sha)
        self.assertIn(b"PRIVATE_KEY = 'unpublished-sdk-secret'", changed)
        self.assertTrue(changed.endswith(b"\n\ndef custom_status():\n    return 'private-adapter'\n"))
        self.assertNotEqual(changed, self.sdk)

    def test_restore_reinstates_old_three_sources_and_removes_only_created_module(self):
        backup = self.root / 'backup'
        backup.mkdir()
        for name in OLD:
            (backup / (Path(name).stem + '.before.py')).write_bytes((self.root / name).read_bytes())
        (backup / 'order_gateway.before.py').write_bytes(self.sdk)
        self.apply_patch()
        result = self.python_block('HARUN_RESTORE_SOURCE', backup)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, sha in OLD.items():
            self.assertEqual(hashlib.sha256((self.root / name).read_bytes()).hexdigest(), sha)
        self.assertFalse((self.root / 'worker/neuroapi_request.py').exists())
        self.assertEqual((self.root / 'worker/order_gateway.py').read_bytes(), self.sdk)

    def test_restore_refuses_foreign_new_module_before_overwriting_sources(self):
        backup = self.root / 'backup'
        backup.mkdir()
        for name in OLD:
            (backup / (Path(name).stem + '.before.py')).write_bytes((self.root / name).read_bytes())
        (backup / 'order_gateway.before.py').write_bytes(self.sdk)
        self.apply_patch()
        (self.root / 'worker/neuroapi_request.py').write_text('foreign_changes=1\n')
        before = source_map(self.root)
        result = self.python_block('HARUN_RESTORE_SOURCE', backup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('NEW_SOURCE_CHANGED_DURING_RECOVERY', result.stderr)
        self.assertEqual(source_map(self.root), before)

    def fake_shell(self, mode):
        fakebin = self.root / 'fakebin'
        fakebin.mkdir()
        docker = fakebin / 'docker'
        docker.write_text('''#!/usr/bin/env python3
import json,os,subprocess,sys
from pathlib import Path
root=Path(os.environ['HARUN_FAKE_PROJECT']);mode=os.environ['HARUN_FAKE_MODE']
a=sys.argv[1:]
with (root/'docker.calls').open('a') as log:log.write(json.dumps(a)+'\\n')
if a[0]=='inspect':
    print(str(root/'compose.yaml') if 'config_files' in a[2] else 'fake-old-image')
    raise SystemExit(0)
if a[:2]==['image','tag']:raise SystemExit(0)
if a[0]=='compose' and 'exec' in a:
    source=sys.stdin.read()
    if '--source-hashes-json' in a:
        if mode=='inspection_failure':
            print(json.dumps(dict(status='SOL_HTTP_422_NOT_SETTLED',reason='ETH_PROTECTION_CHANGED')))
            raise SystemExit(1)
        print(json.dumps(dict(status='INSPECTION_ONLY',can_apply=True,replacements=2,maximum_replacements=3)))
        raise SystemExit(0)
    index=a.index('python');args=a[index+1:];index=args.index('-');params=args[index+1:]
    params=[str(root) if value=='/app' else value for value in params]
    raise SystemExit(subprocess.run([sys.executable,'-I','-B','-S','-',*params],input=source,text=True).returncode)
if a[0]=='compose' and 'config' in a:raise SystemExit(0)
if a[0]=='compose' and 'build' in a:
    if mode=='restore_failure':(root/'worker/neuroapi_request.py').write_text('foreign_changes=1\\n')
    raise SystemExit(7)
raise SystemExit('UNEXPECTED_FAKE_DOCKER_COMMAND')
''')
        docker.chmod(0o755)
        script = SCRIPT.replace(PRIVATE_SDK_SHA, self.sdk_sha)
        original_patcher = block('HARUN_SDK_READBACK_PATCH')
        test_patcher = original_patcher.replace(PRIVATE_SDK_SHA, self.sdk_sha)
        test_patcher = test_patcher.replace('if os.geteuid() != 0:', 'if False:')
        script = script.replace(original_patcher.replace(PRIVATE_SDK_SHA, self.sdk_sha), test_patcher, 1)
        script = script.replace(hashlib.sha256(original_patcher.encode()).hexdigest(), hashlib.sha256(test_patcher.encode()).hexdigest())
        script = script.replace('/root/harun-ai-trading-office', str(self.root))
        script = script.replace('/root/harun-neuroapi-422.XXXXXX', str(self.root / 'backup.XXXXXX'))
        if mode == 'source_patch_failure':
            fakegit = fakebin / 'git'
            fakegit.write_text('#!/bin/sh\nexit 9\n')
            fakegit.chmod(0o755)
        env = dict(os.environ, PATH=str(fakebin) + os.pathsep + os.environ['PATH'],
                   HARUN_FAKE_PROJECT=str(self.root), HARUN_FAKE_MODE=mode)
        return subprocess.run(['bash'], cwd=self.root, input=script, env=env,
                              text=True, capture_output=True, timeout=20)

    def test_build_failure_rolls_back_sources_image_without_stopping_old_worker(self):
        ledger = self.root / 'closed-journal.sqlite3'
        import sqlite3
        with sqlite3.connect(ledger) as db:
            db.execute('CREATE TABLE order_intents(state TEXT,exit_order_id TEXT)')
            db.execute('INSERT INTO order_intents VALUES(?,?)', ('CLOSED', '8389766291712624741'))
            db.execute('CREATE TABLE robot_entry_receipts(id TEXT)')
            db.execute('INSERT INTO robot_entry_receipts VALUES(?)', ('hao-ce5286d37dcdca54a2f1174a2c48',))
        finance_before = ledger.read_bytes()
        result = self.fake_shell('build_failure')
        self.assertEqual(ledger.read_bytes(), finance_before)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SOURCE_RESTORED', result.stdout)
        self.assertIn('stage=BUILD', result.stdout)
        for name, sha in OLD.items():
            self.assertEqual(hashlib.sha256((self.root / name).read_bytes()).hexdigest(), sha)
        self.assertFalse((self.root / 'worker/neuroapi_request.py').exists())
        calls = [json.loads(line) for line in (self.root / 'docker.calls').read_text().splitlines()]
        self.assertIn(['image', 'tag', 'fake-old-image', 'harun-office-worker:latest'], calls)
        self.assertFalse(any('stop' in call or 'up' in call for call in calls))
        self.assertEqual((self.root / 'worker/order_gateway.py').read_bytes(), self.sdk)
        self.assertNotIn('unpublished-sdk-secret', result.stdout + result.stderr)

    def test_source_patch_failure_after_sdk_mutation_restores_original_private_sdk(self):
        result = self.fake_shell('source_patch_failure')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('"status": "PATCHED"', result.stdout)
        self.assertIn('SOURCE_RESTORED', result.stdout)
        self.assertIn('stage=SOURCE_PATCH', result.stdout)
        for name, sha in OLD.items():
            self.assertEqual(hashlib.sha256((self.root / name).read_bytes()).hexdigest(), sha)
        self.assertEqual((self.root / 'worker/order_gateway.py').read_bytes(), self.sdk)
        self.assertFalse((self.root / 'worker/neuroapi_request.py').exists())
        calls = [json.loads(line) for line in (self.root / 'docker.calls').read_text().splitlines()]
        self.assertFalse(any('build' in call or 'stop' in call or 'up' in call for call in calls))
        self.assertNotIn('unpublished-sdk-secret', result.stdout + result.stderr)

    def test_failed_restore_guard_propagates_and_does_not_tag_false_old_image(self):
        result = self.fake_shell('restore_failure')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('RESTORE_FAILED', result.stdout)
        self.assertNotIn('SOURCE_RESTORED', result.stdout)
        calls = [json.loads(line) for line in (self.root / 'docker.calls').read_text().splitlines()]
        self.assertNotIn(['image', 'tag', 'fake-old-image', 'harun-office-worker:latest'], calls)
        self.assertFalse(any('stop' in call or 'up' in call for call in calls))

    def test_initial_helper_refusal_is_shown_and_stops_before_source_patch(self):
        before = source_map(self.root)
        result = self.fake_shell('inspection_failure')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ETH_PROTECTION_CHANGED', result.stdout)
        self.assertEqual(source_map(self.root), before)
        calls = [json.loads(line) for line in (self.root / 'docker.calls').read_text().splitlines()]
        self.assertFalse(any('build' in call or 'stop' in call or 'up' in call for call in calls))

    def test_shell_syntax_and_no_financial_or_provider_write_commands(self):
        subprocess.run(['bash', '-n', str(ROOT / 'deploy/apply_neuroapi_422_fix.sh')],
                       check=True, capture_output=True)
        patch = block('HARUN_RUNTIME_PATCH')
        self.assertNotIn('worker/order_gateway.py', patch)
        self.assertNotIn('worker/core.py', patch)
        self.assertNotIn('git reset', SCRIPT)
        self.assertNotIn('worker.robot tick', SCRIPT)
        self.assertIn("<<'HARUN_RESTORE_SOURCE' || return 1", SCRIPT)
        self.assertIn('cat "$HARUN_BACKUP/settlement.json" || true', SCRIPT)
        self.assertIn('NEUROAPI_422_RECOVERY_VERIFIED', SCRIPT)


if __name__ == '__main__':
    unittest.main()
