#!/usr/bin/env python3
"""Replace only the fingerprinted VPS gateway class while ROBOT is OFF.

Inspection is the default; --apply makes one atomic, backed-up source change.
No adapter is imported, no credential is read, and no image/order is started.
"""
import argparse
import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid


EXPECTED_GATEWAY_SHA = '74b1db1b51461af34d0d4f0ecc189d4876bc72a6e021a049f9dcd22c9dd3509c'
OLD_METHODS = frozenset(('__init__', 'status', '_sign', '_get_server_time', '_request', 'submit', 'reconcile'))
LIMIT = 2 * 1024 * 1024
OFF_TOKEN = b'HARUN_ROBOT_OFF_CONFIRMED\n'
OFF_READER = b'''import sqlite3
try:
    db = sqlite3.connect('file:/data/trading/ledger.sqlite3?mode=ro', uri=True, timeout=5)
    try:
        db.execute('PRAGMA query_only=ON')
        row = db.execute('SELECT enabled FROM robot_settings WHERE id=1').fetchone()
    finally:
        db.close()
except Exception:
    raise SystemExit('ROBOT_OFF_CHECK_FAILED') from None
if row != (0,):
    raise SystemExit('ROBOT_OFF_REQUIRED')
print('HARUN_ROBOT_OFF_CONFIRMED')
'''


class Refuse(Exception):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def directory(path):
    path = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if info.st_uid not in (os.geteuid(), 0) or info.st_mode & 0o022:
            raise Refuse('UNSAFE_DIRECTORY')
        return path, fd
    except BaseException:
        os.close(fd)
        raise


def read_file(parent, name, private=False):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > LIMIT:
            raise Refuse('UNSAFE_FILE')
        if info.st_uid not in (os.geteuid(), 0) or mode & 0o7022:
            raise Refuse('UNSAFE_FILE')
        if private and mode != 0o600:
            raise Refuse('UNSAFE_BACKUP')
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > LIMIT:
                raise Refuse('FILE_TOO_LARGE')
            chunks.append(chunk)
        return fd, info, b''.join(chunks)
    except BaseException:
        os.close(fd)
        raise


def parse_source(raw):
    try:
        text = raw.decode('utf-8')
        return ast.parse(text)
    except (SyntaxError, UnicodeError):
        raise Refuse('SOURCE_PARSE_FAILED') from None


def class_node(raw, template=False):
    tree = parse_source(raw)
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == 'OrderGateway']
    if len(classes) != 1 or classes[0] not in tree.body:
        raise Refuse('ORDER_GATEWAY_CLASS_REQUIRED')
    cls = classes[0]
    if cls.col_offset or cls.decorator_list or cls.keywords or len(cls.bases) != 1:
        raise Refuse('ORDER_GATEWAY_SHAPE_UNSUPPORTED')
    if not isinstance(cls.bases[0], ast.Name) or cls.bases[0].id != '_MissingOrderImplementation':
        raise Refuse('ORDER_GATEWAY_SHAPE_UNSUPPORTED')
    methods = [node for node in cls.body if isinstance(node, ast.FunctionDef)]
    names = [node.name for node in methods]
    if len(names) != len(set(names)) or any(isinstance(node, ast.AsyncFunctionDef) for node in cls.body):
        raise Refuse('ORDER_GATEWAY_METHODS_UNSUPPORTED')
    if template:
        if not {'__init__', 'status', 'submit', 'reconcile'} <= set(names):
            raise Refuse('TEMPLATE_METHODS_MISSING')
        for node in tree.body:
            if node is not cls and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                raise Refuse('TEMPLATE_CLASS_ONLY_REQUIRED')
    elif set(names) != OLD_METHODS:
        raise Refuse('OBSERVED_GATEWAY_METHODS_MISMATCH')
    return cls


def source_span(raw, node):
    # AST column offsets are UTF-8 byte offsets; keep CRLF bytes in line lengths.
    lines = raw.split(b'\n')
    offsets = [0]
    for line in lines[:-1]:
        offsets.append(offsets[-1] + len(line) + 1)
    return offsets[node.lineno - 1] + node.col_offset, offsets[node.end_lineno - 1] + node.end_col_offset


def repaired_bytes(original, template, expected):
    if digest(original) != expected:
        raise Refuse('GATEWAY_FINGERPRINT_MISMATCH')
    old, replacement = class_node(original), class_node(template, template=True)
    start, end = source_span(original, old)
    first, last = source_span(template, replacement)
    newline = b'\r\n' if b'\r\n' in original else b'\n'
    block = template[first:last].replace(b'\r\n', b'\n').replace(b'\n', newline)
    changed = original[:start] + block + original[end:]
    if len(changed) > LIMIT:
        raise Refuse('REPAIRED_SOURCE_TOO_LARGE')
    parse_source(changed)
    return changed


def require_robot_off(project):
    result = subprocess.run(
        ['docker', 'compose', '-f', str(project / 'compose.yaml'), 'exec', '-T',
         '--user', '10001:10001', 'worker', 'python', '-I', '-B', '-S', '-'],
        cwd=project, input=OFF_READER, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=20, check=False,
    )
    if result.returncode or result.stdout != OFF_TOKEN:
        raise Refuse('ROBOT_OFF_CHECK_FAILED')


def repository_root(project):
    result = subprocess.run(['git', '--no-optional-locks', '-C', str(project),
                             'rev-parse', '--show-toplevel'], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=10, check=False)
    if result.returncode:
        raise Refuse('PROJECT_REPOSITORY_REQUIRED')
    try:
        value = Path(os.fsdecode(result.stdout.strip()))
        if not value.is_absolute():
            raise ValueError()
        return value
    except Exception:
        raise Refuse('PROJECT_REPOSITORY_REQUIRED') from None


def unchanged(item):
    info = os.stat(item['name'], dir_fd=item['parent'], follow_symlinks=False)
    old = item['info']
    fields = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink')
    if tuple(getattr(info, key) for key in fields) != tuple(getattr(old, key) for key in fields):
        raise Refuse('TARGET_CHANGED')
    os.lseek(item['fd'], 0, os.SEEK_SET)
    if os.read(item['fd'], LIMIT + 1) != item['raw']:
        raise Refuse('TARGET_CHANGED')


def write_new(parent, name, raw, mode=0o600, uid=None, gid=None):
    if len(raw) > LIMIT:
        raise Refuse('FILE_TOO_LARGE')
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
    complete = False
    try:
        if uid is not None:
            os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
        complete = True
    finally:
        os.close(fd)
        if not complete:
            os.unlink(name, dir_fd=parent)


def replace_target(item, project):
    temporary = '.gateway-repair-' + uuid.uuid4().hex
    try:
        unchanged(item)
        write_new(item['parent'], temporary, item['changed'], stat.S_IMODE(item['info'].st_mode),
                  item['info'].st_uid, item['info'].st_gid)
        require_robot_off(project)
        unchanged(item)
        os.replace(temporary, item['name'], src_dir_fd=item['parent'], dst_dir_fd=item['parent'])
        os.fsync(item['parent'])
    finally:
        try:
            os.unlink(temporary, dir_fd=item['parent'])
        except FileNotFoundError:
            pass


def backup(item, project, git_root, root_path, expected, template_sha):
    root = Path(os.path.abspath(root_path))
    if os.path.commonpath((str(git_root), str(root))) == str(git_root):
        raise Refuse('BACKUP_MUST_BE_OUTSIDE_REPOSITORY')
    parent_path, parent = directory(root.parent)
    try:
        try:
            os.mkdir(root.name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
        os.fsync(parent)  # Persist the new backup-root link before replacing source.
        root, root_fd = directory(parent_path / root.name)
    finally:
        os.close(parent)
    try:
        if stat.S_IMODE(os.fstat(root_fd).st_mode) != 0o700:
            raise Refuse('BACKUP_DIRECTORY_NOT_PRIVATE')
        name = uuid.uuid4().hex
        os.mkdir(name, mode=0o700, dir_fd=root_fd)
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            write_new(child, 'order_gateway.py', item['raw'])
            manifest = dict(version=1, project=str(project), expected_gateway_sha256=expected,
                            original_sha256=digest(item['raw']), target_sha256=digest(item['changed']),
                            template_sha256=template_sha, mode=stat.S_IMODE(item['info'].st_mode),
                            uid=item['info'].st_uid, gid=item['info'].st_gid)
            write_new(child, 'manifest.json', json.dumps(manifest, sort_keys=True).encode())
            os.fsync(child)
            os.fsync(root_fd)
            return root / name
        finally:
            os.close(child)
    finally:
        os.close(root_fd)


def run(args):
    opened = []
    project, parent = directory(args.project)
    opened.append(parent)
    try:
        compose_fd, _, _ = read_file(parent, 'compose.yaml')
        opened.append(compose_fd)
        require_robot_off(project)
        git_root = repository_root(project)
        _, worker = directory(project / 'worker')
        opened.append(worker)
        fd, info, raw = read_file(worker, 'order_gateway.py')
        opened.append(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refuse('GATEWAY_REPAIR_BUSY') from None
        item = dict(parent=worker, name='order_gateway.py', fd=fd, info=info, raw=raw)
        if args.restore:
            path, backup_fd = directory(args.restore)
            opened.append(backup_fd)
            if path.is_relative_to(git_root) or stat.S_IMODE(os.fstat(backup_fd).st_mode) != 0o700:
                raise Refuse('UNSAFE_BACKUP')
            manifest_fd, _, manifest_raw = read_file(backup_fd, 'manifest.json', private=True)
            opened.append(manifest_fd)
            original_fd, _, original = read_file(backup_fd, 'order_gateway.py', private=True)
            opened.append(original_fd)
            manifest = json.loads(manifest_raw)
            if (manifest.get('version') != 1 or manifest.get('project') != str(project)
                    or manifest.get('expected_gateway_sha256') != args.expected_gateway_sha):
                raise Refuse('BACKUP_PROJECT_MISMATCH')
            if (digest(original) != manifest.get('original_sha256') or digest(original) != args.expected_gateway_sha
                    or digest(raw) != manifest.get('target_sha256')
                    or (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid) !=
                       (manifest.get('mode'), manifest.get('uid'), manifest.get('gid'))):
                raise Refuse('RESTORE_TARGET_CHANGED')
            parse_source(original)
            item['changed'] = original
            replace_target(item, project)
            print('GATEWAY_RESTORED')
            return
        template_path = Path(os.path.abspath(args.template))
        _, template_parent = directory(template_path.parent)
        opened.append(template_parent)
        template_fd, _, template = read_file(template_parent, template_path.name)
        opened.append(template_fd)
        item['changed'] = repaired_bytes(raw, template, args.expected_gateway_sha)
        unchanged(item)
        print('GATEWAY_ORIGINAL_SHA256=' + digest(raw))
        print('GATEWAY_TARGET_SHA256=' + digest(item['changed']))
        if not args.apply:
            print('INSPECTION_ONLY')
            return
        root = args.backup_root or git_root.parent / (git_root.name + '-gateway-repair-backups')
        path = backup(item, project, git_root, root, args.expected_gateway_sha, digest(template))
        print('Backup: ' + str(path))
        replace_target(item, project)
        print('GATEWAY_REPAIRED')
    finally:
        for fd in reversed(opened):
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path('/root/harun-ai-trading-office'))
    parser.add_argument('--template', type=Path)
    parser.add_argument('--expected-gateway-sha', default=EXPECTED_GATEWAY_SHA)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-root', type=Path)
    parser.add_argument('--restore', type=Path, metavar='BACKUP_DIRECTORY')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[0-9a-f]{64}', args.expected_gateway_sha):
        parser.error('--expected-gateway-sha must be a lowercase SHA-256 fingerprint')
    if args.restore and (args.template or args.apply or args.backup_root):
        parser.error('--restore cannot be combined with --template, --apply or --backup-root')
    if not args.restore and not args.template:
        parser.error('--template is required')
    try:
        run(args)
    except Refuse as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        return 1
    except Exception:
        print('ERROR: REPAIR_FAILED', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
