#!/usr/bin/env python3
"""Prepare build compatibility without importing or executing an order adapter.

Run with ROBOT OFF. This does not validate credentials or order protection,
build/restart containers, or send requests. --restore only restores unchanged
prepared files from a private backup created by this utility.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

BASELINE_SHA = 'ba15012f49b7eafbac729f2e509590e4b03a4ba3544124d073c14c924f81642e'
HELPERS = ('build_intent', 'require_implementation')
TARGETS = ('worker/order_gateway.py', 'Dockerfile')
PIN = b'RUN python -m pip install --no-cache-dir requests==2.32.5'
LIMIT = 2 * 1024 * 1024


class Refuse(Exception):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def directory(path, create=False):
    path = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            if create and index == len(path.parts) - 2:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
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
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(map(len, chunks)) > LIMIT:
                raise Refuse('FILE_TOO_LARGE')
        return fd, info, b''.join(chunks)
    except BaseException:
        os.close(fd)
        raise


def unchanged(item):
    info = os.stat(item['name'], dir_fd=item['parent'], follow_symlinks=False)
    old = item['info']
    if (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode, info.st_nlink) != (
            old.st_dev, old.st_ino, old.st_uid, old.st_gid, old.st_mode, old.st_nlink):
        raise Refuse('TARGET_CHANGED')
    os.lseek(item['fd'], 0, os.SEEK_SET)
    if os.read(item['fd'], LIMIT + 1) != item['raw']:
        raise Refuse('TARGET_CHANGED')


def gateway_bytes(raw, original):
    if digest(original) != BASELINE_SHA:
        raise Refuse('BASELINE_MISMATCH')
    try:
        tree = ast.parse(raw)
        source = original.decode('utf-8')
        reference = ast.parse(source)
    except (SyntaxError, UnicodeError):
        raise Refuse('SOURCE_PARSE_FAILED') from None
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'OrderGateway']
    if len(classes) != 1:
        raise Refuse('ORDER_GATEWAY_CLASS_REQUIRED')
    present = set()

    class Bindings(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            if node.name in HELPERS:
                if node not in tree.body or node.name in present:
                    raise Refuse('HELPER_CONFLICT')
                present.add(node.name)
            # Decorators/default expressions execute at definition time.
            for value in [*node.decorator_list, *node.args.defaults,
                          *(v for v in node.args.kw_defaults if v is not None)]:
                self.visit(value)

        def visit_AsyncFunctionDef(self, node):
            if node.name in HELPERS:
                raise Refuse('HELPER_CONFLICT')
            self.visit_FunctionDef(node)

        def visit_ClassDef(self, node):
            if node.name in HELPERS:
                raise Refuse('HELPER_CONFLICT')
            for value in [*node.decorator_list, *node.bases, *(k.value for k in node.keywords)]:
                self.visit(value)

        def visit_Name(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)) and node.id in HELPERS:
                raise Refuse('HELPER_CONFLICT')

        def visit_Import(self, node):
            if any((n.asname or n.name.split('.')[0]) in HELPERS for n in node.names):
                raise Refuse('HELPER_CONFLICT')

        def visit_ImportFrom(self, node):
            if any((n.asname or n.name) in HELPERS or n.name == '*' for n in node.names):
                raise Refuse('HELPER_CONFLICT')

        def visit_ExceptHandler(self, node):
            if node.name in HELPERS:
                raise Refuse('HELPER_CONFLICT')
            self.generic_visit(node)

        def visit_MatchAs(self, node):
            if node.name in HELPERS:
                raise Refuse('HELPER_CONFLICT')
            self.generic_visit(node)

        def visit_MatchStar(self, node):
            if node.name in HELPERS:
                raise Refuse('HELPER_CONFLICT')

    Bindings().visit(tree)
    missing = [name for name in HELPERS if name not in present]
    if not missing:
        return raw
    exports = {n.name: n for n in reference.body if isinstance(n, ast.FunctionDef)}
    if any(name not in exports for name in missing):
        raise Refuse('BASELINE_HELPERS_MISSING')
    # These pure helpers rely on the module's established imports/classes.
    imports = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update(n.asname or n.name.split('.')[0] for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.update(n.asname or n.name for n in node.names)
    definitions = {getattr(n, 'name', '') for n in tree.body}
    assigned = {target.id for n in tree.body if isinstance(n, ast.Assign)
                for target in n.targets if isinstance(target, ast.Name)}
    if not {'hashlib', 'D', 'Review', 'number', 'validated_risk_target'} <= imports:
        raise Refuse('HELPER_DEPENDENCIES_MISSING')
    if not {'GatewayUnavailable', '_MissingOrderImplementation'} <= definitions or 'NOT_CONNECTED' not in assigned:
        raise Refuse('HELPER_DEPENDENCIES_MISSING')
    cls = classes[0]
    first = min([cls.lineno, *(n.lineno for n in cls.decorator_list)])
    offset = sum(len(line) for line in raw.splitlines(keepends=True)[:first - 1])
    newline = b'\r\n' if b'\r\n' in raw else b'\n'
    segments = [ast.get_source_segment(source, exports[name]).encode('utf-8') for name in missing]
    insertion = (newline * 2).join(s.replace(b'\n', newline) for s in segments) + newline * 2
    changed = raw[:offset] + insertion + raw[offset:]
    try:
        ast.parse(changed)
    except (SyntaxError, UnicodeError):
        raise Refuse('PREPARED_SOURCE_PARSE_FAILED') from None
    return changed


def docker_bytes(raw):
    lines = raw.splitlines(keepends=True)
    instructions = [(i, line.strip()) for i, line in enumerate(lines)
                    if line.strip() and not line.lstrip().startswith(b'#')]
    if sum(bool(re.match(rb'(?i)^FROM\s', line)) for _, line in instructions) != 1:
        raise Refuse('DOCKERFILE_STAGE_UNSUPPORTED')
    users = [(i, line) for i, line in instructions if re.match(rb'(?i)^USER\s', line)]
    office = [i for i, line in users if re.fullmatch(rb'(?i:USER)\s+office', line)]
    if not office or any(not re.fullmatch(rb'(?i:USER)\s+root', line) for i, line in users if i < office[0]):
        raise Refuse('DOCKERFILE_USER_UNSUPPORTED')
    index = office[0]
    installs = [(i, line) for i, line in instructions
                if re.match(rb'(?i)^RUN\s', line) and re.search(rb'\brequests\b', line)]
    if installs:
        if len(installs) == 1 and installs[0][1] == PIN and installs[0][0] < index:
            return raw
        raise Refuse('REQUESTS_INSTALL_CONFLICT')
    if any(i < index and re.match(rb'(?i)^RUN\s', line) and b'import worker' in line
           for i, line in instructions):
        raise Refuse('DOCKERFILE_IMPORT_PRECEDES_INSTALL')
    newline = b'\r\n' if b'\r\n' in raw else b'\n'
    return b''.join(lines[:index]) + PIN + newline + b''.join(lines[index:])


def needs_requests(raw):
    try:
        tree = ast.parse(raw)
    except (SyntaxError, UnicodeError):
        raise Refuse('SOURCE_PARSE_FAILED') from None
    return any(
        (isinstance(node, ast.Import) and any(alias.name.split('.')[0] == 'requests' for alias in node.names))
        or (isinstance(node, ast.ImportFrom) and node.level == 0
            and (node.module or '').split('.')[0] == 'requests')
        for node in ast.walk(tree))


def write_new(parent, name, raw, mode=0o600, uid=None, gid=None):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 mode, dir_fd=parent)
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


def replace_all(items):
    staged = []
    installed = []
    try:
        for item in items:
            unchanged(item)
            temporary = '.gateway-prepare-' + uuid.uuid4().hex
            staged.append((item, temporary))
            write_new(item['parent'], temporary, item['changed'],
                      stat.S_IMODE(item['info'].st_mode), item['info'].st_uid, item['info'].st_gid)
            item['prepared_info'] = os.stat(temporary, dir_fd=item['parent'], follow_symlinks=False)
        for item in items:
            unchanged(item)
        for item, temporary in staged:
            unchanged(item)
            os.replace(temporary, item['name'], src_dir_fd=item['parent'], dst_dir_fd=item['parent'])
            item['installed_info'] = item['prepared_info']
            installed.append(item)
            os.fsync(item['parent'])
    except BaseException:
        # Roll back only a replacement still exactly equal to our prepared bytes.
        for item in reversed(installed):
            try:
                fd, info, raw = read_file(item['parent'], item['name'])
                os.close(fd)
                expected = item['installed_info']
                if raw != item['changed'] or (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid) != (
                        expected.st_dev, expected.st_ino, expected.st_mode, expected.st_uid, expected.st_gid):
                    continue
                temporary = '.gateway-rollback-' + uuid.uuid4().hex
                write_new(item['parent'], temporary, item['raw'],
                          stat.S_IMODE(item['info'].st_mode), item['info'].st_uid, item['info'].st_gid)
                os.replace(temporary, item['name'], src_dir_fd=item['parent'], dst_dir_fd=item['parent'])
                os.fsync(item['parent'])
            except Exception:
                pass
        raise
    finally:
        for item, temporary in staged:
            try:
                os.unlink(temporary, dir_fd=item['parent'])
            except FileNotFoundError:
                pass


def run(args):
    opened = []
    project, parent = directory(args.project)
    opened.append(parent)
    try:
        worker = os.open('worker', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        opened.append(worker)
        worker_info = os.fstat(worker)
        if worker_info.st_uid not in (os.geteuid(), 0) or worker_info.st_mode & 0o022:
            raise Refuse('UNSAFE_DIRECTORY')
        items = []
        for relative, fd_parent, name in ((TARGETS[0], worker, 'order_gateway.py'), (TARGETS[1], parent, 'Dockerfile')):
            fd, info, raw = read_file(fd_parent, name)
            opened.append(fd)
            items.append(dict(relative=relative, parent=fd_parent, name=name, fd=fd, info=info, raw=raw))
        if args.restore:
            backup, backup_fd = directory(args.restore)
            opened.append(backup_fd)
            fd, _, manifest_raw = read_file(backup_fd, 'manifest.json', private=True)
            opened.append(fd)
            manifest = json.loads(manifest_raw)
            if manifest.get('project') != str(project) or manifest.get('baseline_sha256') != BASELINE_SHA:
                raise Refuse('BACKUP_PROJECT_MISMATCH')
            entries = manifest.get('files', {})
            for item in items:
                record = entries.get(item['relative'], {})
                fd, _, raw = read_file(backup_fd, item['name'], private=True)
                opened.append(fd)
                if digest(raw) != record.get('original_sha256') or digest(item['raw']) != record.get('prepared_sha256'):
                    raise Refuse('RESTORE_TARGET_CHANGED')
                item['changed'] = raw
            changed = [item for item in items if item['changed'] != item['raw']]
            replace_all(changed)
            for item in changed:
                print(item['relative'])
            return
        result = subprocess.run(['git', '--no-optional-locks', '-C', str(project),
                                 'show', '--no-ext-diff', '--no-textconv', 'HEAD:worker/order_gateway.py'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=15)
        if result.returncode:
            raise Refuse('BASELINE_UNAVAILABLE')
        items[0]['changed'] = gateway_bytes(items[0]['raw'], result.stdout)
        items[1]['changed'] = docker_bytes(items[1]['raw']) if needs_requests(items[0]['raw']) else items[1]['raw']
        changed = [item for item in items if item['changed'] != item['raw']]
        if not changed:
            return
        for item in items:
            unchanged(item)
        root_path = args.backup_root or project.parent / (project.name + '-gateway-build-backups')
        if os.path.commonpath((str(project), os.path.abspath(root_path))) == str(project):
            raise Refuse('BACKUP_MUST_BE_OUTSIDE_PROJECT')
        root, root_fd = directory(root_path, create=True)
        opened.append(root_fd)
        if stat.S_IMODE(os.fstat(root_fd).st_mode) != 0o700:
            raise Refuse('BACKUP_DIRECTORY_NOT_PRIVATE')
        unique = uuid.uuid4().hex
        os.mkdir(unique, mode=0o700, dir_fd=root_fd)
        backup_fd = os.open(unique, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        opened.append(backup_fd)
        metadata = {'project': str(project), 'baseline_sha256': BASELINE_SHA, 'files': {}}
        for item in items:
            write_new(backup_fd, item['name'], item['raw'])
            metadata['files'][item['relative']] = dict(original_sha256=digest(item['raw']),
                prepared_sha256=digest(item['changed']), mode=stat.S_IMODE(item['info'].st_mode),
                uid=item['info'].st_uid, gid=item['info'].st_gid)
        write_new(backup_fd, 'manifest.json', json.dumps(metadata, sort_keys=True).encode())
        os.fsync(backup_fd)
        os.fsync(root_fd)
        replace_all(changed)
        for item in changed:
            print(item['relative'])
        print('Backup: ' + str(root / unique))
    finally:
        for fd in reversed(opened):
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path('/root/harun-ai-trading-office'))
    parser.add_argument('--backup-root', type=Path)
    parser.add_argument('--restore', type=Path, metavar='BACKUP_DIRECTORY')
    args = parser.parse_args(argv)
    try:
        run(args)
    except Refuse as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        return 1
    except Exception:
        print('ERROR: PREPARATION_FAILED', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
