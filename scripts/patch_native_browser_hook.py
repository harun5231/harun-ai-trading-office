#!/usr/bin/env python3
"""Prepare an additive actor hook only for the authenticated existing browser source.

This build utility never starts a service or changes the source file. The caller
must supply a new output path; all original bytes outside the one construction
expression are retained, including private selectors, login and chart handling.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import stat


NATIVE_ACTOR_SHA256 = '0ccb1d63cc898af231e813ed8a3934c219fb9f0dff4f2345bb339c26b729e1ad'
PREFIX = (b"__import__('agent.native_browser_hook', "
          b"fromlist=('install_native_bridge',)).install_native_bridge(")
MAX_SOURCE = 262144


class PatchRefused(ValueError):
    pass


def need(ok, code):
    if not ok:
        raise PatchRefused(code)


def patch_native_actor(raw, *, _expected_sha256=NATIVE_ACTOR_SHA256):
    need(type(raw) is bytes and 0 < len(raw) <= MAX_SOURCE,
         'NATIVE_ACTOR_SOURCE_SIZE_UNVERIFIED')
    digest = hashlib.sha256(raw).hexdigest()
    need(digest == _expected_sha256, 'NATIVE_ACTOR_SOURCE_PIN_MISMATCH')
    try:
        tree = ast.parse(raw.decode('utf-8'))
    except (SyntaxError, UnicodeError, ValueError, RecursionError):
        raise PatchRefused('NATIVE_ACTOR_AST_UNVERIFIED') from None
    matches = []
    for item in tree.body:
        if not isinstance(item, ast.ClassDef) or item.name != 'BrowserActor':
            continue
        for method in item.body:
            if not isinstance(method, ast.FunctionDef) or method.name != '_open_automation':
                continue
            for node in ast.walk(method):
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Attribute)
                        and isinstance(node.targets[0].value, ast.Name)
                        and node.targets[0].value.id == 'self' and node.targets[0].attr == '_bridge'
                        and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                        and node.value.func.id == 'DOMBridge' and len(node.value.args) == 1
                        and isinstance(node.value.args[0], ast.Name) and node.value.args[0].id == 'page'
                        and not node.value.keywords):
                    matches.append(node.value)
    need(len(matches) == 1, 'NATIVE_ACTOR_CONSTRUCTION_SITE_UNVERIFIED')
    node = matches[0]
    rows = raw.splitlines(keepends=True)
    start = sum(map(len, rows[:node.lineno - 1])) + node.col_offset
    end = sum(map(len, rows[:node.end_lineno - 1])) + node.end_col_offset
    original = raw[start:end]
    replacement = PREFIX + original + b')'
    result = raw[:start] + replacement + raw[end:]
    try:
        ast.parse(result.decode('utf-8'))
    except (SyntaxError, UnicodeError, ValueError, RecursionError):
        raise PatchRefused('NATIVE_ACTOR_PATCH_AST_UNVERIFIED') from None
    need(result[:start] == raw[:start] and result[start + len(replacement):] == raw[end:]
         and result.count(PREFIX) == 1, 'NATIVE_ACTOR_BYTE_PRESERVATION_UNVERIFIED')
    return result, {'schema_version': 1, 'source_sha256': digest,
        'patched_sha256': hashlib.sha256(result).hexdigest(),
        'original_bytes': len(raw), 'patched_bytes': len(result),
        'changed_expression_count': 1, 'all_other_source_bytes_preserved': True,
        'production_deployment_performed': False}


def read_source(path):
    for parent in path.parents:
        need(stat.S_ISDIR(parent.lstat().st_mode), 'NATIVE_ACTOR_SOURCE_PARENT_UNVERIFIED')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
             and not before.st_mode & 0o022 and 0 < before.st_size <= MAX_SOURCE,
             'NATIVE_ACTOR_SOURCE_METADATA_UNVERIFIED')
        raw = os.read(fd, MAX_SOURCE + 1)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        need(len(raw) == before.st_size and identity(before) == identity(os.fstat(fd))
             == identity(path.lstat()), 'NATIVE_ACTOR_SOURCE_CHANGED')
        return raw
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        result, receipt = patch_native_actor(read_source(args.input))
        if args.output is not None:
            for parent in args.output.parents:
                need(stat.S_ISDIR(parent.lstat().st_mode), 'NATIVE_ACTOR_OUTPUT_PARENT_UNVERIFIED')
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(result)
                output.flush()
                os.fsync(output.fileno())
            receipt['new_output_written'] = True
        else:
            receipt['new_output_written'] = False
        print(json.dumps({'ok': True, **receipt}, sort_keys=True))
        return 0
    except Exception:
        print('{"ok":false,"reason":"AUTHENTICATED_NATIVE_PATCH_PREPARATION_REFUSED"}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
