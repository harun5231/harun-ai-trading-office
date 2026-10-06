"""No actual adapter imports; synthetic classes run in offline isolation."""
import ast
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch


HERE = Path(__file__).resolve().parent
UTILITY = next(path for path in (
    HERE / "prepare_gateway_build.py",
    HERE.parent / "deploy" / "prepare_gateway_build.py",
) if path.is_file())
SPEC = importlib.util.spec_from_file_location("gateway_build_preparation", UTILITY)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)
REPO = next(path for path in (
    HERE.parent,
    HERE / "harun-office-robot24",
) if (path / ".git").exists() and (path / "worker" / "order_gateway.py").is_file())
KNOWN_SHA = "ba15012f49b7eafbac729f2e509590e4b03a4ba3544124d073c14c924f81642e"
SECRET = b"SYNTHETIC_SECRET_MUST_NEVER_APPEAR_IN_OUTPUT"
DOCKER = (b"FROM python:3.12-slim-bookworm\n"
          b"RUN useradd --uid 10001 --create-home office\n"
          b"WORKDIR /app\nCOPY worker /app/worker\n"
          b"USER office\n"
          b'RUN python -B -c "import worker.robot"\n'
          b"USER root\n")


def git(project, *arguments):
    environment = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(project), *arguments],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=True,
    ).stdout


class PreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = git(REPO, "show", "--no-ext-diff", "--no-textconv",
                           "HEAD:worker/order_gateway.py")
        if hashlib.sha256(cls.baseline).hexdigest() != KNOWN_SHA:
            raise AssertionError("The fixture must use the exact known Git baseline")
        tree = ast.parse(cls.baseline)
        helpers = [node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name in tool.HELPERS]
        cls.scaffolding = b"".join(cls.baseline.splitlines(keepends=True)
                                   [:min(node.lineno for node in helpers) - 1])
        cls.helper_source = {
            node.name: ast.get_source_segment(cls.baseline.decode(), node).encode()
            for node in helpers
        }

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gateway-build-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir(mode=0o700)
        (self.project / "worker").mkdir(mode=0o700)
        self.gateway = self.project / "worker" / "order_gateway.py"
        self.docker = self.project / "Dockerfile"
        self.gateway.write_bytes(self.baseline)
        self.docker.write_bytes(DOCKER)
        git(self.project, "init", "--quiet", "--template=")
        git(self.project, "add", "worker/order_gateway.py", "Dockerfile")
        git(self.project, "-c", "user.name=Offline Fixture", "-c",
            "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "baseline")
        self.original = self.custom_source()
        self.gateway.write_bytes(self.original)
        self.gateway.chmod(0o640)
        self.docker.chmod(0o600)

    def custom_source(self, import_line=b"import requests\n", newline=b"\n", existing=b""):
        raw = (self.scaffolding + import_line + existing +
               b"\ndef custom_decorator(cls):\n    return cls\n\n"
               b"@custom_decorator\n"
               b"class OrderGateway(_MissingOrderImplementation):\n"
               b"    secret = '" + SECRET + b"'\n"
               b"    def __init__(self):\n        import os\n        self.ready = True\n"
               b"    def status(self):\n        return dict(connected=True)\n"
               b"    def submit(self, intent):\n        return {'custom': intent}\n"
               b"    def reconcile(self, intent):\n        return {'custom': intent}\n"
               b"raise RuntimeError('CUSTOM_ADAPTER_MUST_NOT_BE_IMPORTED')")
        return raw.replace(b"\n", newline)

    def invoke(self, *options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = tool.main(["--project", str(self.project), *map(str, options)])
        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(SECRET.decode(), output)
        self.assertNotIn("CUSTOM_ADAPTER_MUST_NOT_BE_IMPORTED", output)
        return result, output

    def backups(self):
        root = self.root / "project-gateway-build-backups"
        return sorted(root.iterdir()) if root.exists() else []

    def assert_refused_unchanged(self, expected=None, *options):
        before = (self.gateway.read_bytes(), self.docker.read_bytes())
        result, output = self.invoke(*options)
        self.assertEqual(result, 1)
        if expected:
            self.assertIn(expected, output)
        self.assertEqual((self.gateway.read_bytes(), self.docker.read_bytes()), before)
        self.assertEqual(self.backups(), [])

    def test_exact_baseline_and_vanilla_are_untouched_without_backup(self):
        self.assertEqual(tool.BASELINE_SHA, KNOWN_SHA)
        self.gateway.write_bytes(self.baseline)
        result, output = self.invoke()
        self.assertEqual((result, output), (0, ""))
        self.assertEqual(self.gateway.read_bytes(), self.baseline)
        self.assertEqual(self.docker.read_bytes(), DOCKER)
        self.assertEqual(self.backups(), [])

    def test_decorators_crlf_secret_bytes_modes_and_private_external_backup(self):
        original = self.custom_source(newline=b"\r\n")
        self.gateway.write_bytes(original)
        self.docker.write_bytes(DOCKER.replace(b"\n", b"\r\n"))
        original_docker = self.docker.read_bytes()
        metadata = [(path.stat().st_uid, path.stat().st_gid,
                     stat.S_IMODE(path.stat().st_mode)) for path in (self.gateway, self.docker)]
        result, _ = self.invoke()
        self.assertEqual(result, 0)
        insertion = b"\r\n\r\n".join(
            self.helper_source[name].replace(b"\n", b"\r\n") for name in tool.HELPERS
        ) + b"\r\n\r\n"
        offset = original.index(b"@custom_decorator")
        self.assertEqual(self.gateway.read_bytes(), original[:offset] + insertion + original[offset:])
        self.assertFalse(self.gateway.read_bytes().endswith(b"\n"))
        self.assertEqual(self.docker.read_bytes(), original_docker.replace(
            b"USER office\r\n", tool.PIN + b"\r\nUSER office\r\n", 1))
        self.assertEqual(metadata, [(path.stat().st_uid, path.stat().st_gid,
                                    stat.S_IMODE(path.stat().st_mode))
                                   for path in (self.gateway, self.docker)])
        backup, = self.backups()
        self.assertFalse(backup.is_relative_to(self.project))
        for directory in (backup.parent, backup):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual({path.name for path in backup.iterdir()},
                         {"order_gateway.py", "Dockerfile", "manifest.json"})
        for path in backup.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual((backup / "order_gateway.py").read_bytes(), original)
        self.assertEqual((backup / "Dockerfile").read_bytes(), original_docker)
        manifest = json.loads((backup / "manifest.json").read_bytes())
        self.assertEqual(manifest["project"], str(self.project))
        self.assertEqual(manifest["baseline_sha256"], KNOWN_SHA)
        for name, path in (("worker/order_gateway.py", self.gateway), ("Dockerfile", self.docker)):
            self.assertEqual(manifest["files"][name]["prepared_sha256"],
                             hashlib.sha256(path.read_bytes()).hexdigest())

    def test_second_prepare_is_noop_with_no_extra_backup_or_replacement(self):
        self.assertEqual(self.invoke()[0], 0)
        snapshots = [(path.read_bytes(), path.stat().st_ino) for path in (self.gateway, self.docker)]
        backups = self.backups()
        self.assertEqual(self.invoke(), (0, ""))
        self.assertEqual(self.backups(), backups)
        self.assertEqual(snapshots, [(path.read_bytes(), path.stat().st_ino)
                                    for path in (self.gateway, self.docker)])

    def test_restore_original_bytes_and_modes(self):
        self.assertEqual(self.invoke()[0], 0)
        backup, = self.backups()
        self.assertEqual(self.invoke("--restore", backup)[0], 0)
        self.assertEqual(self.gateway.read_bytes(), self.original)
        self.assertEqual(self.docker.read_bytes(), DOCKER)
        self.assertEqual(stat.S_IMODE(self.gateway.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.docker.stat().st_mode), 0o600)

    def test_restore_rejects_newer_edits_before_changing_either_file(self):
        self.assertEqual(self.invoke()[0], 0)
        backup, = self.backups()
        self.docker.write_bytes(self.docker.read_bytes() + b"# newer user edit\n")
        before = (self.gateway.read_bytes(), self.docker.read_bytes())
        result, output = self.invoke("--restore", backup)
        self.assertEqual(result, 1)
        self.assertIn("RESTORE_TARGET_CHANGED", output)
        self.assertEqual((self.gateway.read_bytes(), self.docker.read_bytes()), before)

    def test_missing_one_helper_preserves_custom_existing_function(self):
        existing = b"def build_intent(plan, operation):\n    return {'custom_secret': '" + SECRET + b"'}\n\n"
        original = self.custom_source(existing=existing)
        self.gateway.write_bytes(original)
        self.assertEqual(self.invoke()[0], 0)
        offset = original.index(b"@custom_decorator")
        self.assertEqual(self.gateway.read_bytes(), original[:offset] +
                         self.helper_source["require_implementation"] + b"\n\n" + original[offset:])
        tree = ast.parse(self.gateway.read_bytes())
        functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
        self.assertEqual(functions.count("build_intent"), 1)
        self.assertEqual(functions.count("require_implementation"), 1)

    def test_conflicting_helper_assignment_and_import_leave_files_unchanged(self):
        for conflict in (b"build_intent = None\n", b"import math as require_implementation\n",
                         b"from math import sin as build_intent\n",
                         b"async def build_intent():\n    pass\n",
                         b"class require_implementation:\n    pass\n"):
            with self.subTest(conflict=conflict.splitlines()[0]):
                self.gateway.write_bytes(self.custom_source(existing=conflict))
                self.assert_refused_unchanged("HELPER_CONFLICT")

    def test_missing_dependency_is_refused_without_any_changes(self):
        self.gateway.write_bytes(self.original.replace(b"import hashlib\n", b""))
        self.assert_refused_unchanged("HELPER_DEPENDENCIES_MISSING")

    def test_symlink_and_hardlink_targets_are_refused(self):
        for target in (self.gateway, self.docker):
            original = target.read_bytes()
            for link in ("symlink", "hardlink"):
                with self.subTest(target=target.name, link=link):
                    held = self.root / (target.name + ".held")
                    target.rename(held)
                    if link == "symlink":
                        target.symlink_to(held)
                    else:
                        os.link(held, target)
                    try:
                        self.assert_refused_unchanged()
                        self.assertEqual(held.read_bytes(), original)
                        self.assertEqual(target.is_symlink(), link == "symlink")
                    finally:
                        target.unlink()
                        held.rename(target)

    def test_wrong_git_head_hash_is_refused_without_changes(self):
        self.gateway.write_bytes(self.baseline + b"\n# wrong baseline\n")
        git(self.project, "add", "worker/order_gateway.py")
        git(self.project, "-c", "user.name=Offline Fixture", "-c",
            "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "different baseline")
        self.gateway.write_bytes(self.original)
        self.assert_refused_unchanged("BASELINE_MISMATCH")

    def test_docker_conflicting_pin_missing_user_and_multiple_stages_refuse(self):
        cases = (
            (DOCKER.replace(b"USER office\n", b"RUN pip install requests==2.31.0\nUSER office\n"),
             "REQUESTS_INSTALL_CONFLICT"),
            (DOCKER.replace(b"USER office\n", b"# USER office\n"), "DOCKERFILE_USER_UNSUPPORTED"),
            (DOCKER + b"FROM python:3.12-slim-bookworm\n", "DOCKERFILE_STAGE_UNSUPPORTED"),
        )
        for raw, error in cases:
            with self.subTest(error=error):
                self.docker.write_bytes(raw)
                self.assert_refused_unchanged(error)

    def test_absolute_from_import_adds_dependency_and_relative_import_does_not(self):
        for import_line, expected in ((b"from requests import Session\n", True),
                                      (b"from .requests import Session\n", False)):
            with self.subTest(import_line=import_line):
                self.gateway.write_bytes(self.custom_source(import_line=import_line))
                self.docker.write_bytes(DOCKER)
                self.assertEqual(self.invoke()[0], 0)
                self.assertEqual(tool.PIN in self.docker.read_bytes(), expected)
                if not expected:
                    self.assertEqual(self.docker.read_bytes(), DOCKER)

    def test_failed_second_replace_rolls_back_first_and_removes_staged_files(self):
        real_replace = tool.os.replace

        def fail_docker(source, destination, **options):
            if destination == "Dockerfile":
                raise OSError(SECRET.decode())
            return real_replace(source, destination, **options)

        with patch.object(tool.os, "replace", side_effect=fail_docker):
            result, output = self.invoke()
        self.assertEqual(result, 1)
        self.assertIn("PREPARATION_FAILED", output)
        self.assertEqual(self.gateway.read_bytes(), self.original)
        self.assertEqual(self.docker.read_bytes(), DOCKER)
        self.assertEqual(list(self.project.glob(".gateway-*")), [])
        self.assertEqual(list(self.gateway.parent.glob(".gateway-*")), [])

    def test_rollback_preserves_concurrent_newer_gateway_edit(self):
        real_replace = tool.os.replace
        newer = []

        def fail_after_edit(source, destination, **options):
            if destination == "Dockerfile":
                newer.append(self.gateway.read_bytes() + b"\n# newer user edit\n")
                self.gateway.write_bytes(newer[-1])
                raise OSError("injected second replacement failure")
            return real_replace(source, destination, **options)

        with patch.object(tool.os, "replace", side_effect=fail_after_edit):
            self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.gateway.read_bytes(), newer[0])
        self.assertEqual(self.docker.read_bytes(), DOCKER)

    def wire_source(self, secret_attribute="api_secret", newline=b"\n", boolean_call=False):
        connection = (f"bool(self.api_key and self.{secret_attribute})" if boolean_call else
                      f"True if self.api_key and self.{secret_attribute} else False")
        methods = f'''class OrderGateway(_MissingOrderImplementation):
    fixture_marker = '{SECRET.decode()}'
    def __init__(self, api_key: str = '', api_secret: str = '', base_url: str = 'https://fapi.binance.com'):
        """Synthetic constructor; no real credentials or adapter code."""
        import os
        self.api_key = api_key if api_key else os.getenv('BINANCE_API_KEY', '')
        self.{secret_attribute} = api_secret if api_secret else os.getenv('BINANCE_API_SECRET', '')
        self.base_url = base_url
        self._connected = {connection}

    def status(self):
        return {{'connected': self._connected, 'exchange': 'BINANCE', 'market': 'FUTURES_USDT', 'observed_at': datetime.now(timezone.utc).isoformat()}}

    def _request(self, method, path, parameters=None):
        return {{'fixture_method': method, 'fixture_path': path}}

    def submit(self, intent):
        return {{'fixture_submit': intent}}

    def reconcile(self, intent):
        return {{'fixture_reconcile': intent}}
raise RuntimeError('CUSTOM_ADAPTER_MUST_NOT_BE_IMPORTED')'''.encode()
        raw = (self.scaffolding + b"import requests\nfrom datetime import datetime, timezone\n\n" + methods)
        return raw.replace(b"\n", newline)

    @staticmethod
    def fixture_methods(raw):
        cls = next(node for node in ast.parse(raw).body
                   if isinstance(node, ast.ClassDef) and node.name == "OrderGateway")
        return {node.name: ast.get_source_segment(raw.decode(), node)
                for node in cls.body if isinstance(node, ast.FunctionDef)}

    @contextmanager
    def synthetic_gateway(self, *, fail_at=None, environment=None):
        """Execute only our generated synthetic class, with a fake private reader."""
        tree = ast.parse(self.gateway.read_bytes())
        cls = copy.deepcopy(next(node for node in tree.body
                                 if isinstance(node, ast.ClassDef) and node.name == "OrderGateway"))
        cls.bases = []
        cls.decorator_list = []
        module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
        package = ModuleType("offline_gateway_fixture")
        package.__path__ = []
        private = ModuleType("offline_gateway_fixture.binance_private")

        class FakeBinanceCheckError(Exception):
            pass

        values = {"BINANCE_API_KEY_FILE": "SYNTHETIC_FILE_KEY",
                  "BINANCE_API_SECRET_FILE": "SYNTHETIC_FILE_SECRET"}

        def read_secret(name):
            if name == fail_at:
                raise FakeBinanceCheckError(SECRET.decode())
            return values[name]

        private.read_secret = Mock(side_effect=read_secret)
        private.BinanceCheckError = FakeBinanceCheckError
        namespace = {"__name__": "offline_gateway_fixture.order_gateway",
                     "__package__": "offline_gateway_fixture",
                     "datetime": datetime, "timezone": timezone}
        output = io.StringIO()
        with patch.dict(sys.modules, {package.__name__: package, private.__name__: private}), \
                patch.dict(os.environ, environment or {}, clear=True), \
                redirect_stdout(output), redirect_stderr(output):
            exec(compile(module, "<synthetic-offline-gateway>", "exec"), namespace)
            yield namespace["OrderGateway"], private.read_secret
        self.assertEqual(output.getvalue(), "")

    def assert_safe_status(self, gateway, *, connected, failure):
        result = gateway.status()
        self.assertEqual(result["connected"], connected)
        self.assertIs(type(result["connected"]), bool)
        self.assertEqual(result["status"], "CONFIGURED" if connected else "NOT_CONNECTED")
        self.assertEqual(result["failure_code"], failure)
        self.assertEqual(result["exchange"], "BINANCE")
        self.assertEqual(result["market"], "FUTURES_USDT")
        self.assertIsNotNone(datetime.fromisoformat(result["observed_at"]).tzinfo)
        encoded = json.dumps(result)
        for secret in (SECRET.decode(), "SYNTHETIC_FILE_KEY", "SYNTHETIC_FILE_SECRET",
                       "EXPLICIT_KEY", "EXPLICIT_SECRET", "LEGACY_ENV_KEY", "LEGACY_ENV_SECRET"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(gateway._office_file_secret_failure, failure)

    def test_wire_is_opt_in_and_default_preparation_preserves_constructor_and_status(self):
        original = self.wire_source()
        self.gateway.write_bytes(original)
        methods = self.fixture_methods(original)
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.fixture_methods(self.gateway.read_bytes()), methods)
        self.assertNotIn(b"_office_file_secret_failure", self.gateway.read_bytes())

    def test_wire_preserves_order_methods_crlf_and_repeat_is_noop(self):
        original = self.wire_source(newline=b"\r\n")
        self.gateway.write_bytes(original)
        methods = self.fixture_methods(original)
        self.assertEqual(self.invoke("--wire-file-secrets")[0], 0)
        prepared = self.gateway.read_bytes()
        changed_methods = self.fixture_methods(prepared)
        for name in ("_request", "submit", "reconcile"):
            self.assertEqual(changed_methods[name], methods[name])
        self.assertTrue(changed_methods["__init__"].startswith(methods["__init__"]))
        self.assertIn(SECRET, prepared)
        self.assertIn(b"class OrderGateway(_MissingOrderImplementation):\r\n", prepared)
        self.assertNotIn(b"\n", prepared.replace(b"\r\n", b""))
        self.assertFalse(prepared.endswith(b"\n"))
        snapshot = [(path.read_bytes(), path.stat().st_ino) for path in (self.gateway, self.docker)]
        backups = self.backups()
        self.assertEqual(self.invoke("--wire-file-secrets"), (0, ""))
        self.assertEqual(self.backups(), backups)
        self.assertEqual(snapshot, [(path.read_bytes(), path.stat().st_ino)
                                    for path in (self.gateway, self.docker)])

    def test_wire_empty_arguments_use_file_pair_for_both_connected_expression_forms(self):
        for boolean_call in (False, True):
            with self.subTest(boolean_call=boolean_call):
                self.gateway.write_bytes(self.wire_source(boolean_call=boolean_call))
                self.assertEqual(self.invoke("--wire-file-secrets")[0], 0)
                legacy = {"BINANCE_API_KEY": "LEGACY_ENV_KEY",
                          "BINANCE_API_SECRET": "LEGACY_ENV_SECRET"}
                with self.synthetic_gateway(environment=legacy) as (Gateway, reader):
                    gateway = Gateway()
                    self.assertEqual(gateway.api_key, "SYNTHETIC_FILE_KEY")
                    self.assertEqual(gateway.api_secret, "SYNTHETIC_FILE_SECRET")
                    self.assertEqual(gateway.base_url, "https://fapi.binance.com")
                    self.assertEqual([call.args for call in reader.call_args_list],
                                     [("BINANCE_API_KEY_FILE",), ("BINANCE_API_SECRET_FILE",)])
                    self.assert_safe_status(gateway, connected=True, failure=None)

    def test_wire_reader_failure_clears_both_keys_and_returns_safe_metadata(self):
        self.gateway.write_bytes(self.wire_source())
        self.assertEqual(self.invoke("--wire-file-secrets")[0], 0)
        for failure in ("BINANCE_API_KEY_FILE", "BINANCE_API_SECRET_FILE"):
            with self.subTest(failure=failure), self.synthetic_gateway(fail_at=failure) as (Gateway, reader):
                gateway = Gateway()
                self.assertEqual((gateway.api_key, gateway.api_secret), ("", ""))
                self.assertEqual(reader.call_count, 1 if failure.endswith("KEY_FILE") else 2)
                self.assert_safe_status(gateway, connected=False, failure="BINANCE_NOT_CONFIGURED")

    def test_wire_partial_explicit_pair_never_mixes_or_reads_file_credentials(self):
        self.gateway.write_bytes(self.wire_source())
        self.assertEqual(self.invoke("--wire-file-secrets")[0], 0)
        for pair in (("EXPLICIT_KEY", ""), ("", "EXPLICIT_SECRET")):
            with self.subTest(pair=pair), self.synthetic_gateway(environment={
                "BINANCE_API_KEY": "LEGACY_ENV_KEY", "BINANCE_API_SECRET": "LEGACY_ENV_SECRET",
            }) as (Gateway, reader):
                gateway = Gateway(*pair)
                self.assertEqual((gateway.api_key, gateway.api_secret), ("", ""))
                reader.assert_not_called()
                self.assert_safe_status(gateway, connected=False, failure="BINANCE_NOT_CONFIGURED")

    def test_wire_complete_explicit_pair_is_preserved_without_reader(self):
        self.gateway.write_bytes(self.wire_source())
        self.assertEqual(self.invoke("--wire-file-secrets")[0], 0)
        with self.synthetic_gateway(fail_at="BINANCE_API_KEY_FILE") as (Gateway, reader):
            gateway = Gateway("EXPLICIT_KEY", "EXPLICIT_SECRET", "https://fixture.invalid")
            self.assertEqual((gateway.api_key, gateway.api_secret), ("EXPLICIT_KEY", "EXPLICIT_SECRET"))
            self.assertEqual(gateway.base_url, "https://fixture.invalid")
            reader.assert_not_called()
            self.assert_safe_status(gateway, connected=True, failure=None)

    def test_wire_complex_constructor_and_status_refuse_before_file_changes(self):
        original = self.wire_source()
        cases = (
            (original.replace(b"        self.base_url = base_url\n",
                              b"        self.base_url = base_url\n        self._request('POST', '/synthetic')\n"),
             "WIRE_CONSTRUCTOR_UNSUPPORTED"),
            (original.replace(b"    def status(self):\n", b"    def status(self):\n        self._request('GET', '/synthetic')\n"),
             "WIRE_STATUS_UNSUPPORTED"),
            (original.replace(b"base_url: str = 'https://fapi.binance.com'", b"base_url: str = 'https://fixture.invalid'"),
             "WIRE_CONSTRUCTOR_UNSUPPORTED"),
            (original.replace(b"self.api_secret", b"self.secret"), "WIRE_CONSTRUCTOR_UNSUPPORTED"),
            (original.replace(b"class OrderGateway(", b"@custom_decorator\nclass OrderGateway("),
             "WIRE_CONSTRUCTOR_UNSUPPORTED"),
            (original.replace(b"    def __init__(", b"    @staticmethod\n    def __init__("),
             "WIRE_CONSTRUCTOR_UNSUPPORTED"),
            (original.replace(b"    def status(self):", b"    @property\n    def status(self):"),
             "WIRE_STATUS_UNSUPPORTED"),
        )
        for raw, expected in cases:
            with self.subTest(expected=expected):
                self.gateway.write_bytes(raw)
                self.assert_refused_unchanged(expected, "--wire-file-secrets")

    def test_wire_unpaired_and_duplicate_marker_comments_refuse_without_changes(self):
        for marker in (tool.WIRE_INIT_BEGIN, tool.WIRE_INIT_END,
                       tool.WIRE_STATUS_BEGIN, tool.WIRE_STATUS_END):
            with self.subTest(marker=marker):
                raw_marker = marker.encode() if isinstance(marker, str) else marker
                self.gateway.write_bytes(self.wire_source() + b"\n" + raw_marker + b"\n")
                self.assert_refused_unchanged("WIRE_MARKER_CONFLICT", "--wire-file-secrets")

    def test_wire_marker_hash_and_generated_block_tampering_refuse_rerun(self):
        self.gateway.write_bytes(self.wire_source())
        self.assertEqual(self.invoke("--wire-file-secrets")[0], 0)
        prepared = self.gateway.read_bytes()
        marker = tool.WIRE_INIT_BEGIN
        marker = marker.encode() if isinstance(marker, str) else marker
        offset = prepared.index(marker) + len(marker)
        changed_digit = b"1" if prepared[offset:offset + 1] == b"0" else b"0"
        changes = (
            prepared[:offset] + changed_digit + prepared[offset + 1:],
            prepared.replace(b"BINANCE_NOT_CONFIGURED", b"SYNTHETIC_CHANGED_FAILURE"),
            prepared.replace(b"'exchange': 'BINANCE'", b"'exchange': 'SYNTHETIC_CHANGED_EXCHANGE'"),
            prepared.replace(b"base_url: str = 'https://fapi.binance.com'",
                             b"base_url: str = 'https://fixture.invalid'"),
            prepared + b"\n" + marker + b"0" * 64 + b"\n",
        )
        for changed in changes:
            with self.subTest(change_digest=hashlib.sha256(changed).hexdigest()):
                self.assertNotEqual(changed, prepared)
                self.gateway.write_bytes(changed)
                before = (self.gateway.read_bytes(), self.docker.read_bytes())
                result, output = self.invoke("--wire-file-secrets")
                self.assertEqual(result, 1)
                self.assertIn("WIRE_MARKER_CONFLICT", output)
                self.assertEqual((self.gateway.read_bytes(), self.docker.read_bytes()), before)
                self.assertEqual(len(self.backups()), 1)

    def test_wire_relocated_markers_outside_their_methods_refuse_rerun(self):
        self.gateway.write_bytes(self.wire_source())
        self.assertEqual(self.invoke("--wire-file-secrets")[0], 0)
        prepared = self.gateway.read_bytes()
        cases = (
            (tool.WIRE_INIT_BEGIN, b"class OrderGateway("),
            (tool.WIRE_STATUS_BEGIN, b"class OrderGateway("),
            (tool.WIRE_INIT_END, b"        return {'fixture_method':"),
            (tool.WIRE_STATUS_END, b"        return {'fixture_reconcile':"),
        )
        for marker, destination in cases:
            with self.subTest(marker=marker, destination=destination):
                marker = marker.encode() if isinstance(marker, str) else marker
                lines = prepared.splitlines(keepends=True)
                origin = next(index for index, line in enumerate(lines)
                              if line.lstrip().startswith(marker))
                moved = lines.pop(origin)
                target = next(index for index, line in enumerate(lines)
                              if line.startswith(destination))
                lines.insert(target, moved)
                changed = b"".join(lines)
                ast.parse(changed)
                for family in (b"HARUN_FILE_SECRETS_V1_", b"HARUN_FILE_STATUS_V1_"):
                    self.assertEqual(changed.count(family), prepared.count(family))
                self.gateway.write_bytes(changed)
                before = (changed, self.docker.read_bytes())
                result, output = self.invoke("--wire-file-secrets")
                self.assertEqual(result, 1)
                self.assertIn("WIRE_MARKER_CONFLICT", output)
                self.assertEqual((self.gateway.read_bytes(), self.docker.read_bytes()), before)
                self.assertEqual(len(self.backups()), 1)

    def test_wire_status_last_at_eof_without_newline_and_optional_comment_is_idempotent(self):
        source = self.wire_source()
        cls = next(node for node in ast.parse(source).body
                   if isinstance(node, ast.ClassDef) and node.name == "OrderGateway")
        status = next(node for node in cls.body
                      if isinstance(node, ast.FunctionDef) and node.name == "status")
        lines = source.splitlines(keepends=True)
        status_bytes = b"".join(lines[status.lineno - 1:status.end_lineno])
        remaining = b"".join(lines[:status.lineno - 1] + lines[status.end_lineno:])
        remaining = remaining.split(b"raise RuntimeError(", 1)[0].rstrip(b"\n")
        for newline in (b"\n", b"\r\n"):
            for inline_comment in (False, True):
                with self.subTest(newline=newline, inline_comment=inline_comment):
                    original = remaining + b"\n\n" + status_bytes.rstrip(b"\n")
                    if inline_comment:
                        original += b" # synthetic metadata comment"
                    original = original.replace(b"\n", newline)
                    self.assertFalse(original.endswith(b"\n"))
                    self.gateway.write_bytes(original)
                    order_methods = self.fixture_methods(original)
                    previous_backups = set(self.backups())
                    self.assertEqual(self.invoke("--wire-file-secrets")[0], 0)
                    prepared = self.gateway.read_bytes()
                    for name in ("_request", "submit", "reconcile"):
                        self.assertEqual(self.fixture_methods(prepared)[name], order_methods[name])
                    backup, = set(self.backups()) - previous_backups
                    self.assertEqual((backup / "order_gateway.py").read_bytes(), original)
                    snapshot = [(path.read_bytes(), path.stat().st_ino)
                                for path in (self.gateway, self.docker)]
                    self.assertEqual(self.invoke("--wire-file-secrets"), (0, ""))
                    self.assertEqual(set(self.backups()), previous_backups | {backup})
                    self.assertEqual(snapshot, [(path.read_bytes(), path.stat().st_ino)
                                                for path in (self.gateway, self.docker)])

    def test_wire_class_body_constructor_and_status_aliases_are_refused(self):
        for alias in (b"    __init__ = submit\n", b"    status = reconcile\n"):
            with self.subTest(alias=alias):
                source = self.wire_source().replace(b"raise RuntimeError(", alias + b"raise RuntimeError(", 1)
                ast.parse(source)
                self.gateway.write_bytes(source)
                self.assert_refused_unchanged("WIRE_CONSTRUCTOR_UNSUPPORTED", "--wire-file-secrets")

    def test_wire_credential_literals_and_metadata_keys_refuse_but_unknown_safe_names_pass(self):
        original = self.wire_source()
        cases = (
            (original.replace(b"os.getenv('BINANCE_API_KEY', '')",
                              b"os.getenv('BINANCE_API_KEY', 'SYNTHETIC_HARDCODED_KEY')"),
             "WIRE_CONSTRUCTOR_UNSUPPORTED"),
            (original.replace(b"os.getenv('BINANCE_API_KEY', '')", b"os.getenv('', '')"),
             "WIRE_CONSTRUCTOR_UNSUPPORTED"),
            (original.replace(b"'exchange': 'BINANCE'", b"'Binance_API_KEY': 'BINANCE'"),
             "WIRE_STATUS_UNSUPPORTED"),
        )
        for source, failure in cases:
            with self.subTest(failure=failure):
                self.gateway.write_bytes(source)
                self.assert_refused_unchanged(failure, "--wire-file-secrets")
        accepted = (original.replace(b"'BINANCE_API_KEY'", b"'UNKNOWN_SAFE_KEY_ENV'")
                    .replace(b"'BINANCE_API_SECRET'", b"'UNKNOWN_SAFE_SECRET_ENV'")
                    .replace(b"'exchange': 'BINANCE'", b"'custom_venue_metadata': 'BINANCE'"))
        self.gateway.write_bytes(accepted)
        self.assertEqual(self.invoke("--wire-file-secrets")[0], 0)
        with self.synthetic_gateway(environment={
            "UNKNOWN_SAFE_KEY_ENV": "LEGACY_ENV_KEY", "UNKNOWN_SAFE_SECRET_ENV": "LEGACY_ENV_SECRET",
        }) as (Gateway, reader):
            gateway = Gateway()
            self.assertEqual((gateway.api_key, gateway.api_secret),
                             ("SYNTHETIC_FILE_KEY", "SYNTHETIC_FILE_SECRET"))
            self.assertEqual(reader.call_count, 2)
            metadata = gateway.status()
            self.assertEqual(metadata["custom_venue_metadata"], "BINANCE")
            self.assertEqual((metadata["status"], metadata["connected"], metadata["failure_code"]),
                             ("CONFIGURED", True, None))


if __name__ == "__main__":
    unittest.main()
