"""Run the real deployment wrapper with private PATH stubs, never Docker/network."""
import ast
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


WRAPPER = Path(__file__).resolve().parent.parent / 'deploy' / 'apply_vps_order_fix.sh'
FILES = ('order_gateway.py', 'robot.py', 'binance_private.py', 'market.py', 'neuroapi.py', 'research_guard.py')
REVISION = '9' * 40
STUB = r'''import hashlib, json, os, sys
from pathlib import Path
command = Path(sys.argv[0]).name
args = sys.argv[1:]
body = sys.stdin.read()
log = Path(os.environ['FIXTURE_LOG'])
old = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
kind = command
if command == 'git':
    if args[:1] == ['fetch']: kind = 'fetch'
    elif 'rev-parse' in args: kind = 'revision'
    elif args[:1] == ['show']: kind = 'download'
elif command == 'python3':
    if '-c' in args: kind = 'host_hashes'
    elif any(value.endswith('/repair_existing_gateway.py') for value in args):
        kind = 'gateway_apply' if '--apply' in args else 'gateway_inspect'
    elif any(value.endswith('/update_existing_worker.py') for value in args):
        kind = 'worker_apply' if '--apply' in args else 'worker_inspect'
elif command == 'docker':
    if args[:1] == ['inspect']: kind = 'compose_label'
    elif args[:3] == ['compose', '-f', 'compose.yaml']:
        kind = args[3]
        if kind == 'exec':
            if 'SELECT enabled FROM robot_settings' in body: kind = 'off_guard'
            elif 'WORKER_SOURCE_MATCH' in body: kind = 'source_verify'
            elif 'worker.robot_status' in args: kind = 'status'
event = dict(command=command, args=args, stdin=body, kind=kind)
with log.open('a') as stream:
    stream.write(json.dumps(event) + '\n')
if kind == os.environ.get('FIXTURE_FAIL'):
    print('FIXTURE_' + kind.upper() + '_FAILED', file=sys.stderr)
    raise SystemExit(42)
if kind == 'off_guard':
    ordinal = 1 + sum(row['kind'] == 'off_guard' for row in old)
    if str(ordinal) == os.environ.get('FIXTURE_FAIL_OFF_AT'):
        print('ROBOT_OFF_REQUIRED', file=sys.stderr)
        raise SystemExit(43)
    print('ROBOT_OFF_CONFIRMED')
elif kind == 'compose_label':
    project = os.environ['FIXTURE_PROJECT']
    print(project + ('/wrong.yaml' if os.environ.get('FIXTURE_BAD_COMPOSE') else '/compose.yaml'))
elif kind == 'revision': print('9' * 40)
elif kind == 'download': print('# Downloaded fixture only; never execute SDK code')
elif kind == 'host_hashes':
    names = ('order_gateway.py', 'robot.py', 'binance_private.py', 'market.py', 'neuroapi.py', 'research_guard.py')
    print(json.dumps({name: hashlib.sha256((Path('worker') / name).read_bytes()).hexdigest() for name in names}))
elif kind == 'source_verify':
    expected = json.loads(args[-1])
    names = ('order_gateway.py', 'robot.py', 'binance_private.py', 'market.py', 'neuroapi.py', 'research_guard.py')
    actual = {name: hashlib.sha256((Path('worker') / name).read_bytes()).hexdigest() for name in names}
    if expected != actual: raise SystemExit(44)
    print('GATEWAY_SOURCE_MATCH\nWORKER_SOURCE_MATCH')
    print(json.dumps({'order_intent_counts': {'ENTRY_PENDING': 1, 'NEEDS_REVIEW': 1}}))
elif kind == 'status': print('ROBOT_OFF_FIXTURE_STATUS')
'''


class ApplyVpsOrderFixTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='vps-wrapper-fixture-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir(mode=0o700)
        (self.project / 'worker').mkdir(mode=0o700)
        for name in FILES:
            (self.project / 'worker' / name).write_bytes(('FIXTURE_ONLY_' + name + '\n').encode())
        (self.project / 'compose.yaml').write_bytes(b'FIXTURE_COMPOSE_UNTOUCHED\n')
        (self.project / 'Dockerfile').write_bytes(b'FIXTURE_CUSTOM_DOCKER_UNTOUCHED\n')
        (self.project / 'compose.hotfix.yaml').write_bytes(b'FIXTURE_UNTRACKED_UNTOUCHED\n')
        self.files_before = {path.relative_to(self.project): path.read_bytes() for path in self.project.rglob('*') if path.is_file()}
        self.bin = self.root / 'bin'
        self.bin.mkdir(mode=0o700)
        for command in ('docker', 'git', 'python3'):
            path = self.bin / command
            path.write_text('#!' + sys.executable + '\n' + STUB)
            path.chmod(0o700)
        self.log = self.root / 'events.jsonl'
        self.script = self.root / 'apply_private_fixture.sh'
        self.source = WRAPPER.read_text()
        project = 'HARUN_PROJECT=/root/harun-ai-trading-office'
        staging = 'mktemp -d /root/harun-ai-trading-office-order-fix.XXXXXX'
        self.assertEqual(self.source.count(project), 1)
        self.assertEqual(self.source.count(staging), 1)
        # Only these two literal paths differ from the actual checked-in script.
        private = self.source.replace(project, 'HARUN_PROJECT=' + shlex.quote(str(self.project)))
        private = private.replace(staging, 'mktemp -d ' + shlex.quote(str(self.root / 'staging.XXXXXX')))
        self.script.write_text(private)

    def run_wrapper(self, **controls):
        environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''),
                           FIXTURE_LOG=str(self.log), FIXTURE_PROJECT=str(self.project), **controls)
        result = subprocess.run(['bash', str(self.script)], input=b'', stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=environment, cwd=self.root, timeout=10)
        events = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        self.assertEqual(self.files_before, {path.relative_to(self.project): path.read_bytes() for path in self.project.rglob('*') if path.is_file()})
        return result, events

    def assert_no_restart(self, events):
        self.assertFalse({'stop', 'up'} & {event['kind'] for event in events})

    def test_success_inspects_both_before_apply_builds_before_restart_and_verifies_six_files(self):
        result, events = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        kinds = [event['kind'] for event in events]
        self.assertLess(max(kinds.index('gateway_inspect'), kinds.index('worker_inspect')),
                        min(kinds.index('gateway_apply'), kinds.index('worker_apply')))
        self.assertLess(max(i for i, kind in enumerate(kinds) if kind == 'build'), kinds.index('stop'))
        self.assertLess(kinds.index('stop'), kinds.index('up'))
        guards = [i for i, kind in enumerate(kinds) if kind == 'off_guard']
        self.assertEqual(len(guards), 3)
        self.assertLess(guards[0], kinds.index('fetch'))
        self.assertLess(kinds.index('build'), guards[1])
        self.assertLess(guards[1], kinds.index('stop'))
        self.assertLess(kinds.index('up'), guards[2])
        self.assertLess(guards[2], kinds.index('source_verify'))
        self.assertLess(kinds.index('source_verify'), kinds.index('status'))
        verification = next(event for event in events if event['kind'] == 'source_verify')
        expected = json.loads(verification['args'][-1])
        self.assertEqual(expected, {name: hashlib.sha256((self.project / 'worker' / name).read_bytes()).hexdigest() for name in FILES})
        self.assertIn(b'GATEWAY_SOURCE_MATCH', result.stdout)
        self.assertIn(b'WORKER_SOURCE_MATCH', result.stdout)
        self.assertIn(b'order_intent_counts', result.stdout)

    def test_preflight_fingerprint_failures_prevent_either_apply_and_restart(self):
        for failure in ('gateway_inspect', 'worker_inspect'):
            with self.subTest(failure=failure):
                self.log.unlink(missing_ok=True)
                result, events = self.run_wrapper(FIXTURE_FAIL=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse({'gateway_apply', 'worker_apply', 'build'} & {event['kind'] for event in events})
                self.assert_no_restart(events)

    def test_build_or_configuration_failure_prevents_stop_and_recreate(self):
        for failure in ('config', 'build'):
            with self.subTest(failure=failure):
                self.log.unlink(missing_ok=True)
                result, events = self.run_wrapper(FIXTURE_FAIL=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_restart(events)

    def test_off_guard_failure_before_work_or_after_build_prevents_restart(self):
        for ordinal in ('1', '2'):
            with self.subTest(ordinal=ordinal):
                self.log.unlink(missing_ok=True)
                result, events = self.run_wrapper(FIXTURE_FAIL_OFF_AT=ordinal)
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_restart(events)
                if ordinal == '1':
                    self.assertEqual([event['kind'] for event in events], ['off_guard'])
                else:
                    self.assertIn('build', {event['kind'] for event in events})

    def test_apply_failure_prevents_build_and_restart(self):
        for failure in ('gateway_apply', 'worker_apply'):
            with self.subTest(failure=failure):
                self.log.unlink(missing_ok=True)
                result, events = self.run_wrapper(FIXTURE_FAIL=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('build', {event['kind'] for event in events})
                self.assert_no_restart(events)

    def test_wrong_compose_label_stops_before_fetch_or_source_changes(self):
        result, events = self.run_wrapper(FIXTURE_BAD_COMPOSE='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'COMPOSE_CONFIGURATION_MISMATCH', result.stderr)
        self.assertEqual([event['kind'] for event in events], ['off_guard', 'compose_label'])
        self.assert_no_restart(events)

    def test_every_compose_call_is_explicit_and_git_downloads_share_one_revision(self):
        result, events = self.run_wrapper()
        self.assertEqual(result.returncode, 0)
        for event in events:
            if event['command'] == 'docker' and event['args'][0] == 'compose':
                self.assertEqual(event['args'][:3], ['compose', '-f', 'compose.yaml'])
        downloads = [event for event in events if event['kind'] == 'download']
        self.assertEqual(len(downloads), 4)
        self.assertTrue(all(event['args'][1].startswith(REVISION + ':deploy/') for event in downloads))
        recreate = next(event for event in events if event['kind'] == 'up')
        self.assertTrue({'--no-build', '--no-deps', '--force-recreate', '--wait'} <= set(recreate['args']))
        stop = next(event for event in events if event['kind'] == 'stop')
        self.assertEqual(stop['args'][-3:], ['-t', '660', 'worker'])

    def test_final_verification_failure_reports_failure_and_skips_success_status(self):
        result, events = self.run_wrapper(FIXTURE_FAIL='source_verify')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('status', {event['kind'] for event in events})
        self.assertNotIn(b'Review scripts:', result.stdout)

    def test_final_ledger_counts_are_read_only_and_keep_known_persisted_states(self):
        result, events = self.run_wrapper()
        self.assertEqual(result.returncode, 0)
        body = next(event['stdin'] for event in events if event['kind'] == 'source_verify')
        self.assertIn('mode=ro', body)
        self.assertIn('PRAGMA query_only=ON', body)
        self.assertIn('SELECT state, COUNT(*) FROM order_intents GROUP BY state', body)
        tree = ast.parse(body)
        allowed = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == 'allowed' for target in node.targets))
        self.assertEqual(ast.literal_eval(allowed), {'READY_FOR_EXECUTION', 'EXECUTION_BLOCKED', 'SUBMITTING', 'ENTRY_PENDING',
                                                   'POSITION_PROTECTED', 'CLOSED', 'REJECTED', 'NEEDS_REVIEW'})
        self.assertNotIn('import worker', body)

    def test_wrapper_never_enables_robot_resets_journal_or_runs_git_pull_reset(self):
        result, events = self.run_wrapper()
        self.assertEqual(result.returncode, 0)
        for event in events:
            if event['command'] == 'git':
                self.assertFalse({'pull', 'reset', 'checkout', 'restore', 'clean'} & set(event['args']))
        self.assertNotIn('robot_on=True', self.source)
        self.assertNotIn('robot_on=true', self.source)
        self.assertNotIn('SET enabled=1', self.source)
        self.assertNotIn('DELETE FROM', self.source)
        self.assertNotIn('DROP TABLE', self.source)
        self.assertNotIn('UPDATE order_intents', self.source)
        self.assertNotIn('worker simulation', self.source)


if __name__ == '__main__': unittest.main()
