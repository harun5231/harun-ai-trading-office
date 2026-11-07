#!/usr/bin/env python3
"""Linux OS boundary for the installed Hermes SDK, not a tool-policy sandbox.

This module is stdlib-only. The worker starts with -I -S, installs the irreversible
exec filter, then imports Hermes. `stage-proof` runs a NEW transient service only;
it never edits/restarts the root WhatsApp gateway or installs packages.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import socket
import stat
import subprocess
import sys
import time
import uuid

SCHEMA = "hermes-trading-os-boundary/v1"
INNER_SOURCE = "/opt/hermes/source"
INNER_VENV = "/opt/hermes/venv"  # installed dependency root; may be bundled, not a venv
INNER_PYTHON = "/opt/hermes/python"
INNER_ADAPTER = "/opt/office-hermes"
INNER_HOME = "/var/lib/hermes-trading"
INNER_BROKER = "/run/hermes-model"
INNER_CONFIG = "/etc/office-hermes/runtime.json"
FORBIDDEN_PATHS = (
    "/root/.ssh/id_rsa", "/root/.config/gh/hosts.yml", "/root/.hermes/.env",
    "/root/.hermes/config.yaml", "/root/.hermes/hermes-agent/.env",
    INNER_SOURCE + "/.env", "/etc/shadow", "/etc/sudoers", "/run/docker.sock",
    "/var/run/docker.sock", "/run/systemd/private", "/root-tasks", "/srv/trading",
)
_HEX = re.compile(r"^[0-9a-f]{64}$")
_EXEC_FILTER_INSTALLED = False


def install_exec_deny_filter() -> None:
    """Deny execve/execveat for this process and all future threads/children.

    Run before imports from the installed SDK/venv. No libseccomp dependency.
    Architecture mismatch and x86 x32 ABI terminate the process; exec returns
    EPERM. Requires a single-thread bootstrap, checked before installation.
    """
    global _EXEC_FILTER_INSTALLED
    if _EXEC_FILTER_INSTALLED:
        return
    if sys.platform != "linux" or len(list(Path("/proc/self/task").iterdir())) != 1:
        raise RuntimeError("single-threaded Linux bootstrap required")
    machine = platform.machine()
    definitions = {"x86_64": (0xC000003E, (59, 322), (57, 58), 56),
                   "aarch64": (0xC00000B7, (221, 281), (), 220)}
    if machine not in definitions:
        raise RuntimeError("unreviewed seccomp architecture")
    audit_arch, exec_numbers, fork_numbers, clone_number = definitions[machine]

    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                    ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]

    class Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]

    # LD ABS arch; JEQ expected else KILL; LD syscall number.
    instructions = [(0x20, 0, 0, 4), (0x15, 1, 0, audit_arch),
                    (0x06, 0, 0, 0x80000000), (0x20, 0, 0, 0)]
    if machine == "x86_64":
        instructions += [(0x45, 0, 1, 0x40000000), (0x06, 0, 0, 0x80000000)]
    for number in exec_numbers:
        instructions += [(0x15, 0, 1, number), (0x06, 0, 0, 0x00050000 | errno.EPERM)]
    for number in fork_numbers:
        instructions += [(0x15, 0, 1, number), (0x06, 0, 0, 0x00050000 | errno.EPERM)]
    # clone3 has an opaque struct: ENOSYS makes libc fall back to checked clone.
    instructions += [(0x15, 0, 1, 435), (0x06, 0, 0, 0x00050000 | errno.ENOSYS)]
    # Permit clone only for threads (CLONE_THREAD); no independent child process.
    instructions += [(0x15, 0, 3, clone_number), (0x20, 0, 0, 16),
                     (0x45, 1, 0, 0x00010000), (0x06, 0, 0, 0x00050000 | errno.EPERM),
                     (0x06, 0, 0, 0x7FFF0000)]
    array = (Filter * len(instructions))(*(Filter(*row) for row in instructions))
    program = Program(len(instructions), array)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS")
    if libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:  # PR_SET_SECCOMP/FILTER
        raise OSError(ctypes.get_errno(), "PR_SET_SECCOMP")
    _EXEC_FILTER_INSTALLED = True


def _status(path: Path) -> dict[str, str]:
    return {line.split(":", 1)[0]: line.split(":", 1)[1].strip()
            for line in path.read_text().splitlines() if ":" in line}


def _start_ticks(proc: Path) -> int:
    # comm can contain spaces and parentheses; field 22 follows the final ')'.
    return int((proc / "stat").read_text().rsplit(")", 1)[1].split()[19])


def boundary_attestation() -> dict:
    """Diagnostic only. Host verification must not trust worker self-report."""
    s = _status(Path("/proc/self/status"))
    return {"schema": SCHEMA, "pid": os.getpid(), "uid": os.getuid(), "gid": os.getgid(),
            "no_new_privs": s.get("NoNewPrivs") == "1", "seccomp": s.get("Seccomp"),
            "capabilities_zero": all(int(s.get(k, "1"), 16) == 0
                                     for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")),
            "exec_filter_installed": _EXEC_FILTER_INSTALLED}


def _sealed_json(path: str, expected_hash: str | None = None) -> tuple[dict, str]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("policy/attestation must be root-owned and sealed")
        raw = os.read(fd, 131073)
        if len(raw) > 131072:
            raise ValueError("oversized policy/attestation")
    finally:
        os.close(fd)
    digest = hashlib.sha256(raw).hexdigest()
    if expected_hash is not None and (not _HEX.fullmatch(expected_hash) or digest != expected_hash):
        raise ValueError("policy hash mismatch")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("unreviewed isolation policy schema")
    return value, digest


def _kernel_identity(pid: int, uid: int, policy: dict, proc_root: str = "/proc") -> dict:
    if pid <= 1 or uid <= 0 or uid != policy["uid"]:
        raise ValueError("nonroot visible kernel peer pid/uid required")
    proc = Path(proc_root) / str(pid)
    s = _status(proc / "status")
    if [int(v) for v in s["Uid"].split()] != [uid] * 4:
        raise ValueError("runtime uid mismatch")
    if [int(v) for v in s["Gid"].split()] != [policy["gid"]] * 4:
        raise ValueError("runtime gid mismatch")
    if s.get("NoNewPrivs") != "1" or s.get("Seccomp") != "2":
        raise ValueError("runtime kernel restrictions missing")
    if any(int(s.get(k, "1"), 16) != 0 for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")):
        raise ValueError("runtime has capabilities")
    cgroup = (proc / "cgroup").read_text()
    if not any(line.endswith("/" + policy["cgroup_unit"]) for line in cgroup.splitlines()):
        raise ValueError("unexpected runtime cgroup")
    return {"pid": pid, "uid": uid, "gid": policy["gid"], "start_ticks": _start_ticks(proc),
            "no_new_privs": True, "seccomp": 2, "capabilities_zero": True}


def _full_host_verification(pid: int, uid: int, policy: dict) -> dict:
    """Root host verifier; every required mount and namespace is kernel observed."""
    evidence = _kernel_identity(pid, uid, policy)
    proc = Path("/proc") / str(pid)
    if os.stat(proc / "root") != os.stat(policy["root_dir"]):
        # stat_result includes times etc, compare filesystem identity only below.
        a, b = os.stat(proc / "root"), os.stat(policy["root_dir"])
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise ValueError("runtime root directory mismatch")
    for name in ("net", "mnt"):
        if os.stat(proc / "ns" / name).st_ino == os.stat("/proc/1/ns/" + name).st_ino:
            raise ValueError("runtime shares host " + name + " namespace")
    mounts = {}
    for line in (proc / "mountinfo").read_text().splitlines():
        left = line.split(" - ", 1)[0].split()
        mounts[left[4]] = set(left[5].split(","))
    required = {INNER_SOURCE, INNER_VENV, INNER_ADAPTER, INNER_CONFIG, INNER_BROKER}
    if not required.issubset(set(policy["readonly_mounts"])):
        raise ValueError("required runtime mount missing from policy")
    bindings = policy["bind_sources"]
    if not (required | {INNER_HOME}).issubset(bindings):
        raise ValueError("expected host bind identities missing from policy")
    for target, source in bindings.items():
        _safe_absolute(target)
        _safe_absolute(source)
        actual, expected = os.stat(proc / "root" / target.lstrip("/")), os.stat(source)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError("unexpected host bind source: " + target)
    for target in policy["readonly_mounts"]:
        if "ro" not in mounts.get(target, set()):
            raise ValueError("missing read-only runtime mount: " + target)
    if "rw" not in mounts.get(INNER_HOME, set()):
        raise ValueError("safe memory mount missing")
    cmdline = (proc / "cmdline").read_bytes().split(b"\0")
    if b"-I" not in cmdline or b"-S" not in cmdline:
        raise ValueError("isolated Python startup missing")
    for inner, expected in ((INNER_ADAPTER + "/hermes_runtime.py", "adapter_source_sha256"),
                            (policy["source_manifest_file"], "runtime_source_sha256"),
                            (INNER_ADAPTER + "/hermes_trading_isolation.py", "helper_sha256"),
                            (INNER_CONFIG, "runtime_config_sha256")):
        raw = (proc / "root" / inner.lstrip("/")).read_bytes()
        if hashlib.sha256(raw).hexdigest() != policy[expected]:
            raise ValueError("pinned runtime artifact mismatch")
    evidence.update({"isolation_verified": True, "kernel_peer_verified": True,
                     "root_directory_verified": True, "private_network_verified": True,
                     "readonly_mounts_verified": True})
    return evidence


def verify_runtime_peer(pid: int, uid: int, policy_file: str, policy_sha256: str,
                        server_nonce: str | None = None, socket_path: str | None = None) -> dict:
    """Verify kernel peer UID and a sealed host guard receipt plus boot nonce.

    Docker clients can see pid=0 for a host peer. They must supply the worker's
    ready/health nonce and a read-only mount of root-owned policy/attestation.
    The one-shot root guard, never the worker, establishes the OS evidence.
    """
    try:
        policy, digest = _sealed_json(policy_file, policy_sha256)
        if pid < 0 or uid <= 0 or uid != policy["uid"]:
            raise ValueError("nonroot expected kernel peer uid required")
        receipt, _ = _sealed_json(policy["attestation_file"])
        if (receipt.get("policy_sha256") != digest or receipt.get("uid") != uid
                or receipt.get("isolation_verified") is not True
                or not isinstance(server_nonce, str) or len(server_nonce) < 32
                or receipt.get("server_nonce") != server_nonce):
            raise ValueError("host attestation/boot nonce mismatch")
        if pid and (receipt.get("pid") != pid or receipt.get("start_ticks") != _start_ticks(Path("/proc") / str(pid))):
            raise ValueError("host attestation does not identify kernel peer")
        info = os.stat(socket_path or policy["socket_path"], follow_symlinks=False)
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid
                or [info.st_dev, info.st_ino] != receipt["socket_identity"]):
            raise ValueError("runtime socket changed since host attestation")
        if os.geteuid() == 0 and pid:
            evidence = _full_host_verification(pid, uid, policy)
        else:
            evidence = {"pid": receipt["pid"], "uid": uid, "start_ticks": receipt["start_ticks"],
                        "isolation_verified": True, "kernel_peer_verified": True,
                        "root_attestation_verified": True}
        return {**evidence, "verified": True, "policy_sha256": digest, "server_nonce": server_nonce}
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
        return {"verified": False, "kernel_peer_verified": False, "isolation_verified": False,
                "reason": type(exc).__name__ + ": " + str(exc)}


def _read_worker_nonce(path: Path, expected_uid: int) -> str:
    # This is the only root read of worker-writable data. Never follow a symlink
    # through /proc/PID/root or block on a FIFO/device supplied by the worker.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != expected_uid
                or info.st_nlink != 1 or info.st_size > 129 or info.st_mode & 0o077):
            raise ValueError("unsafe runtime nonce file")
        raw = os.read(fd, 130)
    finally:
        os.close(fd)
    nonce = raw.decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{32,128}", nonce):
        raise ValueError("runtime boot nonce missing")
    return nonce


def write_host_attestation(policy_file: str, policy_sha256: str, pid: int) -> dict:
    if os.geteuid() != 0:
        raise PermissionError("host attestation requires root")
    policy, digest = _sealed_json(policy_file, policy_sha256)
    evidence = _full_host_verification(pid, policy["uid"], policy)
    nonce_path = Path("/proc") / str(pid) / "root" / INNER_HOME.lstrip("/") / "server-nonce"
    deadline = time.monotonic() + 60
    while True:
        try:
            info = os.stat(policy["socket_path"], follow_symlinks=False)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != policy["uid"]:
                raise ValueError("runtime socket owner mismatch")
            # connect without sending a request: kernel peer credentials bind the
            # socket inode to this exact MAINPID even during a rolling restart.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(1)
                channel.connect(policy["socket_path"])
                import struct
                peer_pid, peer_uid, _ = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if peer_pid != pid or peer_uid != policy["uid"]:
                    raise ValueError("socket is not owned by the attested process")
            nonce = _read_worker_nonce(nonce_path, policy["uid"])
            # Recheck kernel identity after startup; fail if MAINPID changed.
            if _kernel_identity(pid, policy["uid"], policy)["start_ticks"] != evidence["start_ticks"]:
                raise ValueError("runtime restarted during attestation")
            break
        except (FileNotFoundError, ConnectionRefusedError, TimeoutError):
            if time.monotonic() >= deadline:
                raise TimeoutError("runtime socket/nonce readiness timeout")
            time.sleep(0.1)
    receipt = {"schema": SCHEMA, **evidence, "policy_sha256": digest,
               "socket_identity": [info.st_dev, info.st_ino], "server_nonce": nonce}
    target = Path(policy["attestation_file"])
    parent = target.parent.stat()
    if parent.st_uid != 0 or parent.st_mode & 0o022:
        raise ValueError("attestation directory must be root-owned and sealed")
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, (json.dumps(receipt, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, target)
    return receipt


def _safe_absolute(value: str) -> str:
    if (not isinstance(value, str) or not value.startswith("/") or any(c in value for c in "\n\r\x00 :%")
            or ".." in Path(value).parts):
        raise ValueError("unsafe absolute path")
    return value


def validate_spec(value: dict) -> dict:
    """Explicit facts only; no automatic mounts of host root/home/etc/run."""
    spec = dict(value)
    for key in ("source", "venv", "adapter", "config", "root_dir", "state_dir", "broker_dir", "python"):
        _safe_absolute(spec[key])
    if spec["source"] != "/root/.hermes/hermes-agent":
        raise ValueError("candidate must reuse the observed installed Hermes source")
    if spec["uid"] <= 0 or spec["gid"] <= 0:
        raise ValueError("dedicated nonroot uid/gid required")
    for key in ("root_dir", "state_dir"):
        if not spec[key].startswith("/var/lib/hermes-trading-isolation/"):
            raise ValueError("candidate storage must stay in its dedicated tree")
    if (spec["root_dir"] == spec["state_dir"]
            or Path(spec["root_dir"]).is_relative_to(spec["state_dir"])
            or Path(spec["state_dir"]).is_relative_to(spec["root_dir"])):
        raise ValueError("root and safe memory must be independent siblings")
    if spec.get("python_bundle"):
        bundle = _safe_absolute(spec["python_bundle"])
        if not re.fullmatch(r"/root/\.hermes/tools/python-[A-Za-z0-9.+_-]+", bundle):
            raise ValueError("unexpected installed Python bundle")
    runtime = spec["runtime_ro"]
    if not isinstance(runtime, list) or not runtime:
        raise ValueError("explicit interpreter/library mounts required")
    for path in runtime:
        _safe_absolute(path)
        if not (path.startswith("/usr/") or path.startswith("/lib/") or path.startswith("/lib64/")):
            raise ValueError("runtime mounts may only be individual interpreter/library trees")
        if path in ("/usr", "/usr/bin", "/usr/sbin", "/lib", "/lib64"):
            raise ValueError("broad runtime directory mount rejected")
    return spec


def unit_properties(spec: dict) -> list[tuple[str, str]]:
    spec = validate_spec(spec)
    mounts = [(spec["source"], INNER_SOURCE), (spec["venv"], INNER_VENV),
              (spec["adapter"], INNER_ADAPTER), (spec["config"], INNER_CONFIG)]
    if spec.get("policy"):
        mounts.append((_safe_absolute(spec["policy"]), "/etc/office-hermes/policy.json"))
    if spec.get("source_manifest"):
        mounts.append((_safe_absolute(spec["source_manifest"]), "/etc/office-hermes/source-manifest.json"))
    if spec.get("python_bundle"):
        mounts.append((spec["python_bundle"], INNER_PYTHON))
    if spec.get("rpc_token"):
        mounts.append((_safe_absolute(spec["rpc_token"]), "/etc/office-hermes/rpc-token"))
    mounts += [(path, path) for path in spec["runtime_ro"]]
    mounts.append((spec["broker_dir"], INNER_BROKER))
    writable = [(spec["state_dir"], INNER_HOME)]
    if spec.get("socket_dir"):
        path = _safe_absolute(spec["socket_dir"])
        if not path.startswith("/run/hermes-trading"):
            raise ValueError("runtime socket directory must be a dedicated run directory")
        writable.append((path, path))
    properties = [
        ("User", str(spec["uid"])), ("Group", str(spec["gid"])),
        ("RootDirectory", spec["root_dir"]), ("MountAPIVFS", "yes"), ("WorkingDirectory", "/"),
        ("BindReadOnlyPaths", " ".join(a + ":" + b + ":norbind" for a, b in mounts)),
        ("BindPaths", " ".join(a + ":" + b + ":norbind" for a, b in writable)),
        ("InaccessiblePaths", "-" + INNER_SOURCE + "/.env -" + INNER_SOURCE + "/.git"),
        ("TemporaryFileSystem", "/tmp:rw,nosuid,nodev,noexec,size=64M /run:rw,nosuid,nodev,noexec,size=8M"),
        ("PrivateNetwork", "yes"), ("RestrictAddressFamilies", "AF_UNIX"),
        ("PrivateDevices", "yes"), ("PrivateTmp", "yes"), ("ProtectSystem", "strict"),
        ("ProtectHome", "yes"), ("ProtectProc", "invisible"), ("ProcSubset", "pid"),
        ("NoNewPrivileges", "yes"), ("CapabilityBoundingSet", ""), ("AmbientCapabilities", ""),
        ("RestrictNamespaces", "yes"), ("RestrictSUIDSGID", "yes"),
        ("SupplementaryGroups", "10002"),
        ("LockPersonality", "yes"), ("MemoryDenyWriteExecute", "yes"),
        ("ProtectKernelTunables", "yes"), ("ProtectKernelModules", "yes"),
        ("ProtectKernelLogs", "yes"), ("ProtectControlGroups", "yes"),
        ("SystemCallArchitectures", "native"),
        ("SystemCallFilter", "@system-service"),
        ("SystemCallErrorNumber", "EPERM"), ("UMask", "0077"),
        ("TasksMax", "32"), ("MemoryMax", "1536M"), ("CPUQuota", "100%"),
        ("LimitNOFILE", "128"), ("LimitCORE", "0"),
        ("Environment", "HOME=" + INNER_HOME + " HERMES_HOME=" + INNER_HOME + " PATH=/nonexistent LANG=C.UTF-8"),
        ("UnsetEnvironment", "LD_PRELOAD LD_LIBRARY_PATH PYTHONPATH PYTHONHOME OPENAI_API_KEY ANTHROPIC_API_KEY BINANCE_API_KEY BINANCE_API_SECRET GH_TOKEN GITHUB_TOKEN SSH_AUTH_SOCK DOCKER_HOST"),
        # Do not bind host journal/syslog sockets into RootDirectory.
        ("StandardOutput", "journal"), ("StandardError", "journal"),
    ]
    return properties


def _sealed_ancestors(path: Path) -> None:
    for current in (path, *path.parents):
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("candidate ancestry must be root-owned sealed directories")


def prepare_layout(spec: dict) -> None:
    """Prepare only the dedicated candidate tree; no package/user/service edits."""
    spec = validate_spec(spec)
    if os.geteuid() != 0:
        raise PermissionError("staged proof requires host root")
    account = pwd.getpwuid(spec["uid"])
    if account.pw_name in ("root", "docker") or spec["uid"] < 100:
        raise ValueError("dedicated service account required")
    root = Path(spec["root_dir"])
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    _sealed_ancestors(root)
    # The root-task runner uses umask077. Explicit read/traverse permissions on
    # our sealed candidate directories are required before dropping to UID10003.
    base = Path("/var/lib/hermes-trading-isolation")
    for selected in (root, Path(spec["adapter"]), Path(spec["broker_dir"])):
        if selected.is_relative_to(base):
            _sealed_ancestors(selected)
            for directory in (selected, *selected.parents):
                if directory == base or directory.is_relative_to(base):
                    os.chmod(directory, 0o755)
    for existing in root.rglob("*"):
        info = existing.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
            raise ValueError("unsafe preexisting candidate filesystem")
        if stat.S_ISDIR(info.st_mode):
            os.chmod(existing, 0o755)
    for path in ("opt/hermes/source", "opt/hermes/venv", "opt/hermes/python", "opt/office-hermes", "etc/office-hermes",
                 "var/lib/hermes-trading", "run/hermes-model", "tmp", "proc", "dev"):
        (root / path).mkdir(mode=0o755, parents=True, exist_ok=True)
    for path in spec["runtime_ro"]:
        target = root / path.lstrip("/")
        if Path(path).is_dir():
            target.mkdir(mode=0o755, parents=True, exist_ok=True)
        else:
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            target.touch(exist_ok=True)
    (root / INNER_CONFIG.lstrip("/")).touch(exist_ok=True)
    (root / "etc/office-hermes/policy.json").touch(exist_ok=True)
    (root / "etc/office-hermes/source-manifest.json").touch(exist_ok=True)
    (root / "etc/office-hermes/rpc-token").touch(exist_ok=True)
    if spec.get("socket_dir"):
        (root / _safe_absolute(spec["socket_dir"]).lstrip("/")).mkdir(mode=0o755, parents=True, exist_ok=True)
    state = Path(spec["state_dir"])
    _sealed_ancestors(state.parent)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.is_symlink():
        raise ValueError("unsafe candidate state")
    os.chown(state, spec["uid"], spec["gid"])
    os.chmod(state, 0o700)
    # chmod after all mkdir calls, including their umask-created parents.
    os.chmod(root, 0o755)
    for directory in root.rglob("*"):
        if directory.is_dir() and not directory.is_symlink():
            os.chmod(directory, 0o755)


def proof() -> dict:
    """Real denial probes inside the OS boundary; never output file contents."""
    install_exec_deny_filter()
    checks = boundary_attestation()
    checks["forbidden_reads_denied"] = {}
    for path in FORBIDDEN_PATHS:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            checks["forbidden_reads_denied"][path] = exc.errno in (errno.ENOENT, errno.EACCES, errno.EPERM, errno.ENOTDIR)
        else:
            os.close(fd)
            checks["forbidden_reads_denied"][path] = False
    checks["root_escalation_denied"] = {}
    for name, operation in (("setuid", os.setuid), ("setgid", os.setgid)):
        try:
            operation(0)
        except PermissionError:
            checks["root_escalation_denied"][name] = True
        else:
            checks["root_escalation_denied"][name] = False
    checks["network_denied"] = {}
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            channel = socket.socket(family, socket.SOCK_STREAM)
        except OSError as exc:
            checks["network_denied"][str(family)] = exc.errno in (errno.EAFNOSUPPORT, errno.EPERM, errno.EACCES)
        else:
            channel.close()
            checks["network_denied"][str(family)] = False
    # Probe raw execve with nonexistent argv: filtered EPERM proves the syscall
    # was intercepted before pathname lookup (ENOENT would mean no exec filter).
    libc = ctypes.CDLL(None, use_errno=True)
    argv = (ctypes.c_char_p * 2)(b"/definitely-no-executable", None)
    envp = (ctypes.c_char_p * 1)(None)
    libc.execve(b"/definitely-no-executable", argv, envp)
    checks["exec_denied"] = ctypes.get_errno() == errno.EPERM
    execveat_number = 322 if platform.machine() == "x86_64" else 281
    libc.syscall(execveat_number, -100, b"/definitely-no-executable", argv, envp, 0)
    checks["execveat_denied"] = ctypes.get_errno() == errno.EPERM
    try:
        os.fork()
    except PermissionError:
        checks["fork_denied"] = True
    else:
        os._exit(97)  # never continue if a future edit accidentally permits fork
    checks["readonly_mounts"] = {}
    for path in (INNER_SOURCE, INNER_VENV, INNER_ADAPTER, INNER_CONFIG):
        checks["readonly_mounts"][path] = bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    checks["readonly_writes_denied"] = {}
    for path in (INNER_SOURCE + "/run_agent.py", INNER_VENV + "/bin/hermes",
                 INNER_ADAPTER + "/hermes_trading_isolation.py", INNER_CONFIG):
        try:
            fd = os.open(path, os.O_WRONLY)  # no truncation, no bytes written
        except OSError as exc:
            checks["readonly_writes_denied"][path] = exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM)
        else:
            os.close(fd)
            checks["readonly_writes_denied"][path] = False
    memory = Path(INNER_HOME) / (".proof-" + uuid.uuid4().hex)
    try:
        with memory.open("x") as handle:
            handle.write("safe-memory-proof\n")
            handle.flush()
            os.fsync(handle.fileno())
        checks["safe_memory_writable"] = True
    finally:
        memory.unlink(missing_ok=True)
    checks["passed"] = (checks["uid"] != 0 and checks["no_new_privs"] and checks["seccomp"] == "2"
                        and checks["capabilities_zero"] and checks["exec_denied"] and checks["execveat_denied"] and checks["fork_denied"]
                        and all(checks["forbidden_reads_denied"].values())
                        and all(checks["network_denied"].values()) and all(checks["root_escalation_denied"].values())
                        and all(checks["readonly_mounts"].values()) and all(checks["readonly_writes_denied"].values())
                        and checks["safe_memory_writable"])
    return checks


def stage_proof(spec: dict) -> int:
    spec = validate_spec(spec)
    prepare_layout(spec)
    name = "hermes-trading-proof-" + uuid.uuid4().hex[:12]
    argv = ["/usr/bin/systemd-run", "--system", "--wait", "--pipe", "--collect", "--unit=" + name]
    for key, value in unit_properties(spec):
        if key in ("StandardOutput", "StandardError"):
            continue  # --pipe owns proof output; do not redirect its JSON to journal
        argv += ["--property=" + key + "=" + value]
    argv += [spec["python"], "-I", "-S", INNER_ADAPTER + "/hermes_trading_isolation.py", "proof"]
    return subprocess.run(argv, timeout=45, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("proof", "render", "stage-proof", "attest"))
    parser.add_argument("--spec")
    parser.add_argument("--policy-file")
    parser.add_argument("--policy-sha256")
    parser.add_argument("--pid", type=int)
    args = parser.parse_args()
    if args.action == "proof":
        result = proof()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["passed"] else 1
    if args.action == "attest":
        write_host_attestation(args.policy_file, args.policy_sha256, args.pid)
        return 0
    spec = validate_spec(json.loads(Path(args.spec).read_text()))
    if args.action == "stage-proof":
        return stage_proof(spec)
    print("[Service]")
    for key, value in unit_properties(spec):
        print(key + "=" + value)
    print("ExecStart=" + spec["python"] + " -I -S " + INNER_ADAPTER + "/hermes_runtime.py --config " + INNER_CONFIG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
