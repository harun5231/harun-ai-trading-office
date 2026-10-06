"""Offline preparation checks; custom adapter source is never imported."""
import ast
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch


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

    def assert_refused_unchanged(self, expected=None):
        before = (self.gateway.read_bytes(), self.docker.read_bytes())
        result, output = self.invoke()
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


if __name__ == "__main__":
    unittest.main()
