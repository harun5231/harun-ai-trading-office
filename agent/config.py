"""Closed runtime configuration. Credentials are file references, never values."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat


def read_json_file(path, limit=32768, *, include_sha=False):
    p = Path(path)
    if not p.is_absolute():
        raise ValueError("CONFIG_PATH_NOT_ABSOLUTE")
    for parent in p.parents:
        s = parent.lstat()
        if not stat.S_ISDIR(s.st_mode):
            raise ValueError("CONFIG_PARENT_UNSAFE")
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        if (not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_uid not in (0, os.geteuid()) or
                s.st_mode & 0o022 or not 1 <= s.st_size <= limit):
            raise ValueError("CONFIG_FILE_UNSAFE")
        raw = os.read(fd, limit + 1)
        a = os.fstat(fd)
        if len(raw) != s.st_size or (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns) != (
                a.st_dev, a.st_ino, a.st_size, a.st_mtime_ns, a.st_ctime_ns):
            raise ValueError("CONFIG_CHANGED")
    finally:
        os.close(fd)
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("CONFIG_DUPLICATE_FIELD")
            out[key] = value
        return out
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("CONFIG_NUMBER_INVALID")))
    return (value, hashlib.sha256(raw).hexdigest()) if include_sha else value


def _path(value):
    if not isinstance(value, str) or not value.startswith("/") or ".." in Path(value).parts:
        raise ValueError("CONFIG_PATH_INVALID")
    return value


def _keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("CONFIG_FIELDS_INVALID")


@dataclass(frozen=True)
class AgentConfig:
    data_directory: str
    backend: dict
    browser: dict
    model: dict
    loop: dict
    sha256: str

    @classmethod
    def load(cls, path):
        value, digest = read_json_file(path, include_sha=True)
        _keys(value, ("schema_version", "data_directory", "backend", "browser", "model", "loop"))
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("CONFIG_SCHEMA_INVALID")
        _path(value["data_directory"])
        _keys(value["backend"], ("url", "token_file", "revision", "source_sha256"))
        browser = value["browser"]
        rpc = type(browser) is dict and "rpc_url" in browser
        _keys(browser, ("rpc_url", "token_file", "source_sha256", "contract_file", "contract_sha256")
              if rpc else ("cdp_url", "contract_file", "contract_sha256"))
        if rpc:
            _path(browser["token_file"])
            if (not isinstance(browser["source_sha256"], str) or
                    not re.fullmatch(r"[0-9a-f]{64}", browser["source_sha256"]) or
                    browser["source_sha256"] == "0"*64):
                raise ValueError("BROWSER_SOURCE_PIN_REQUIRED")
        _keys(value["model"], ("key_file",))
        _keys(value["loop"], ("active_tick_seconds", "idle_tick_seconds"))
        for section, name in (("backend", "token_file"), ("browser", "contract_file"), ("model", "key_file")):
            _path(value[section][name])
        if not re.fullmatch(r"[0-9a-f]{64}", str(value["backend"]["source_sha256"])) or set(value["backend"]["source_sha256"]) == {"0"}:
            raise ValueError("BACKEND_SOURCE_PIN_REQUIRED")
        if (not isinstance(value["browser"]["contract_sha256"], str) or
                not re.fullmatch(r"[0-9a-f]{64}", value["browser"]["contract_sha256"]) or
                value["browser"]["contract_sha256"] == "0" * 64):
            raise ValueError("BROWSER_CONTRACT_PIN_REQUIRED")
        if not isinstance(value["backend"]["revision"], str) or not 1 <= len(value["backend"]["revision"]) <= 128:
            raise ValueError("BACKEND_REVISION_REQUIRED")
        active, idle = value["loop"]["active_tick_seconds"], value["loop"]["idle_tick_seconds"]
        if (type(active) is not int or not 1 <= active <= 5 or
                type(idle) is not int or not 5 <= idle <= 120):
            raise ValueError("LOOP_BOUND_INVALID")
        return cls(value["data_directory"], value["backend"], value["browser"], value["model"], value["loop"], digest)
