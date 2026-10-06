#!/usr/bin/env python3
"""Inspect/apply five pinned research files, preserving the custom VPS adapter.

Run with host Python -I -B -S. This never imports the adapter, builds/restarts
containers, or fetches Git. The pinned object must already exist in the checkout.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import uuid

BASE_COMMIT = 'adca15a660841e5048f9b0d79755bbf72604d0a4'
TARGET_COMMIT = '79d3c51a6bdc7c80b98ac16092286c0f3e8b8f0f'
HASHES = {
    'robot.py': ('1f49ece44bb10a27a97d7f12fd4be0c6d9ffdfed2d3987f1d8b498e612008767', 'a82963eeb6945b7d4583a45d2078739d00136c623922c7f1c351f8cac4397777'),
    'binance_private.py': ('dc24c254d6fab090337dfcd1409e7bb4c68986fc0ff0e1519e58600342d60b6a', 'b9db1d0161b23f30312571ba6d810ea950dbb2bbab8237b7ab4107883e24689c'),
    'market.py': ('8b980490b15b5213333f25312c45938cd9375a343b671039b3a320510a4f452d', '645240f560ad4c12982b4aea348e38e0617eb086a7f7d093e0d9e54f43f6eb15'),
    'neuroapi.py': ('6d6872d2570849c270eb94f59a69baed9175803b0192d3a57dacc99ba3329ed0', 'cf3445af2972b8227e9fc129c60fcf03de49253d28149948ae0a21f03ca7704c'),
    'research_guard.py': (None, '0296065b17f2cef48e17483301293d9bc0908d470e4d4e1f291ca28b0cd9a1b0'),
}
ANCHORS = {
    'robot_store.py': '6bd33d25bcea68783ce25eb2a41eb9a4b7e5fccd3588a5aa02623f283c1e2dc1',
    'account_state.py': '56361e40a360ddccc2128bb2036dc0133cc8a9415b0f70f6f854e20cf1b3eba5',
    'core.py': '11df540717e6e89a7da2777fa373723562f4ca05b87e3540c36c1902dd7cf9df',
    'analysis.py': 'f9712ce490f99061a45e32962d6489dd372a74aa18213001cc5d167208543a6d',
    'binance_office.py': '6bbd83ba7235a72d4dd2e3a910a06b584616f88786db8db3ad6912c357455457',
    'prompts.py': 'c3119f7e00d228bcd9980a6e9104948947ed819b90b173d615ae6872944b90b5',
    'provenance.py': '894d27ac87d670767bf289add2708438ac4d1ed12d3c4e2a9dc44c737ea38481',
    'robot_provenance.py': 'f539eecb35ca57f8a8f91e9cbbcae2900f31e9dcfcd72246070d3d967582cb15',
    'api_service.py': '9970187caffa5a2166e6c068566613850796a5c7ba872d506ae910427893b6bc',
}
LIMIT = 2 * 1024 * 1024
OFF_SCRIPT = r'''
import os, sqlite3, stat
from pathlib import Path
def authorize(action, first, second, database, trigger):
    if action == sqlite3.SQLITE_SELECT: return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and first == 'robot_settings' and second in {'id','enabled'}: return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and first == 'query_only' and second == 'ON': return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY
try:
    path = Path(os.environ.get('OFFICE_DATA_DIR','/data')) / 'trading' / 'ledger.sqlite3'
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: raise ValueError()
    db = sqlite3.connect(path.absolute().as_uri()+'?mode=ro',uri=True,timeout=5)
    try:
        db.set_authorizer(authorize)
        db.execute('PRAGMA query_only=ON')
        row = db.execute('SELECT enabled FROM robot_settings WHERE id=1').fetchone()
    finally: db.close()
    if row != (0,): raise ValueError()
except Exception:
    raise SystemExit(1)
print('ROBOT_OFF')
'''


class Refuse(Exception):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def identity(info):
    return [info.st_dev, info.st_ino, info.st_uid, info.st_gid,
            stat.S_IMODE(info.st_mode), info.st_mtime_ns, info.st_size]


def directory(path, create=False):
    path = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            if create and index == len(path.parts) - 2:
                try: os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError: pass
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        info = os.fstat(fd)
        if info.st_uid not in (os.geteuid(), 0) or info.st_mode & 0o022:
            raise Refuse('UNSAFE_DIRECTORY')
        return path, fd
    except BaseException:
        os.close(fd)
        raise


def read_file(parent, name, missing=False, private=False):
    try: fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        if missing: return None, None
        raise Refuse('REQUIRED_FILE_MISSING') from None
    except OSError: raise Refuse('UNSAFE_FILE') from None
    try:
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > LIMIT
                or info.st_uid not in (os.geteuid(), 0) or mode & 0o7022
                or private and mode != 0o600):
            raise Refuse('UNSAFE_FILE')
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk: break
            total += len(chunk)
            if total > LIMIT: raise Refuse('FILE_TOO_LARGE')
            chunks.append(chunk)
        return info, b''.join(chunks)
    finally:
        os.close(fd)


def unchanged(item):
    info, raw = read_file(item['parent'], item['name'], missing=item['info'] is None)
    if raw != item['raw'] or (identity(info) if info else None) != (identity(item['info']) if item['info'] else None):
        raise Refuse('TARGET_CHANGED')


def require_off(project):
    try:
        result = subprocess.run(['docker', 'compose', '--project-directory', str(project),
            '-f', str(project / 'compose.yaml'),
            'exec', '--user', '10001:10001', '-T', 'worker', 'python', '-I', '-B', '-S', '-'],
            input=OFF_SCRIPT.encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=False, timeout=15)
    except (OSError, subprocess.SubprocessError): raise Refuse('ROBOT_OFF_CHECK_FAILED') from None
    if result.returncode or result.stdout != b'ROBOT_OFF\n':
        raise Refuse('ROBOT_OFF_REQUIRED')


def git_file(project, relative):
    try:
        result = subprocess.run(['git', '--no-optional-locks', '-C', str(project), 'show',
            '--no-ext-diff', '--no-textconv', TARGET_COMMIT + ':' + relative],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError): raise Refuse('PINNED_OBJECT_UNAVAILABLE') from None
    if result.returncode or len(result.stdout) > LIMIT:
        raise Refuse('PINNED_OBJECT_UNAVAILABLE')
    return result.stdout


def write_new(parent, name, raw, mode=0o600, uid=None, gid=None):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
    try:
        if uid is not None: os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
        return os.fstat(fd)
    finally:
        os.close(fd)


def cleanup_stages(items):
    for item in items:
        name = item.get('staged_name')
        if name:
            try: os.unlink(name, dir_fd=item['parent'])
            except FileNotFoundError: pass


def stage(items):
    try:
        for item in items:
            unchanged(item)
            if item['changed'] is None: continue
            info = item['info']
            item['staged_name'] = '.worker-update-' + uuid.uuid4().hex
            item['staged_info'] = write_new(item['parent'], item['staged_name'], item['changed'],
                item.get('mode', stat.S_IMODE(info.st_mode) if info else 0o644),
                item.get('uid', info.st_uid if info else os.geteuid()),
                item.get('gid', info.st_gid if info else os.getegid()))
    except BaseException:
        cleanup_stages(items)
        raise


def install(items, protected, project):
    installed = []
    try:
        require_off(project)
        for item in items + protected: unchanged(item)
        for item in items:
            unchanged(item)
            # Record before mutation so an interrupt immediately after rename
            # is still rolled back only when the new inode belongs to this tool.
            installed.append(item)
            if item['changed'] is None:
                os.unlink(item['name'], dir_fd=item['parent'])
            elif item['info'] is None:
                os.link(item['staged_name'], item['name'], src_dir_fd=item['parent'], dst_dir_fd=item['parent'], follow_symlinks=False)
                os.unlink(item['staged_name'], dir_fd=item['parent'])
            else:
                os.replace(item['staged_name'], item['name'], src_dir_fd=item['parent'], dst_dir_fd=item['parent'])
            os.fsync(item['parent'])
        for item in protected: unchanged(item)
    except BaseException:
        cleanup_stages(items)
        incomplete = False
        for item in reversed(installed):
            try:
                info, raw = read_file(item['parent'], item['name'], missing=True)
                expected = item.get('staged_info')
                if item['changed'] is None:
                    safe = info is None
                else:
                    safe = raw == item['changed'] and info is not None and identity(info) == identity(expected)
                if not safe:
                    # A concurrent edit or a failed replacement belongs to
                    # someone else; never overwrite it during rollback.
                    if raw != item['raw']: incomplete = True
                    continue
                if item['raw'] is None:
                    os.unlink(item['name'], dir_fd=item['parent'])
                else:
                    name = '.worker-rollback-' + uuid.uuid4().hex
                    original = item['info']
                    write_new(item['parent'], name, item['raw'], stat.S_IMODE(original.st_mode), original.st_uid, original.st_gid)
                    os.replace(name, item['name'], src_dir_fd=item['parent'], dst_dir_fd=item['parent'])
                os.fsync(item['parent'])
            except Exception:
                incomplete = True
        if incomplete: raise Refuse('ROLLBACK_NEEDS_REVIEW') from None
        raise
    finally:
        cleanup_stages(items)


def backup(project, root_path, items, protected):
    root_path = Path(os.path.abspath(root_path))
    if root_path == project or project in root_path.parents or root_path in project.parents:
        raise Refuse('BACKUP_MUST_BE_OUTSIDE_PROJECT')
    root, parent = directory(root_path, create=True)
    try:
        if stat.S_IMODE(os.fstat(parent).st_mode) != 0o700:
            raise Refuse('BACKUP_DIRECTORY_NOT_PRIVATE')
        token = uuid.uuid4().hex
        os.mkdir(token, 0o700, dir_fd=parent)
        destination = os.open(token, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            metadata = dict(version=1, project=str(project), base_commit=BASE_COMMIT,
                target_commit=TARGET_COMMIT, files={}, protected={})
            for item in items:
                old = item['info']
                if item['raw'] is not None: write_new(destination, item['name'] + '.old', item['raw'])
                metadata['files'][item['name']] = dict(original_sha256=digest(item['raw']) if item['raw'] is not None else None,
                    target_sha256=digest(item['changed']), original_identity=identity(old) if old else None,
                    target_identity=identity(item['staged_info']))
            for item in protected:
                metadata['protected'][item['name']] = digest(item['raw'])
            write_new(destination, 'manifest.json', json.dumps(metadata, sort_keys=True).encode())
            os.fsync(destination)
            os.fsync(parent)
        finally: os.close(destination)
        return root / token
    finally: os.close(parent)


def load_restore(project, path, items):
    path = Path(os.path.abspath(path))
    if path == project or project in path.parents or path in project.parents:
        raise Refuse('BACKUP_MUST_BE_OUTSIDE_PROJECT')
    _, parent = directory(path)
    try:
        if stat.S_IMODE(os.fstat(parent).st_mode) != 0o700: raise Refuse('UNSAFE_BACKUP')
        _, raw = read_file(parent, 'manifest.json', private=True)
        try: metadata = json.loads(raw)
        except (ValueError, UnicodeError): raise Refuse('UNSAFE_BACKUP') from None
        if (metadata.get('version') != 1 or metadata.get('project') != str(project)
                or metadata.get('base_commit') != BASE_COMMIT or metadata.get('target_commit') != TARGET_COMMIT
                or set(metadata.get('files', {})) != set(HASHES)):
            raise Refuse('BACKUP_PROJECT_MISMATCH')
        for item in items:
            record = metadata['files'][item['name']]
            old_hash, new_hash = HASHES[item['name']]
            if (record.get('original_sha256') != old_hash or record.get('target_sha256') != new_hash
                    or item['raw'] is None or digest(item['raw']) != new_hash
                    or identity(item['info']) != record.get('target_identity')):
                raise Refuse('RESTORE_TARGET_CHANGED')
            old = record.get('original_identity')
            if old_hash is None:
                if old is not None: raise Refuse('UNSAFE_BACKUP')
                item['changed'] = None
            else:
                _, source = read_file(parent, item['name'] + '.old', private=True)
                if digest(source) != old_hash or not isinstance(old, list) or len(old) != 7:
                    raise Refuse('UNSAFE_BACKUP')
                item.update(changed=source, uid=old[2], gid=old[3], mode=old[4])
        return metadata
    finally: os.close(parent)


def run(args):
    project, parent = directory(args.project)
    worker = None
    try:
        # Confirm this is the repository root, without executing hooks/filters.
        result = subprocess.run(['git', '--no-optional-locks', '-C', str(project), 'rev-parse', '--show-toplevel'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=15)
        if result.returncode or result.stdout.strip() != os.fsencode(project): raise Refuse('PROJECT_ROOT_MISMATCH')
        require_off(project)
        worker = os.open('worker', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        info = os.fstat(worker)
        if info.st_uid not in (os.geteuid(), 0) or info.st_mode & 0o022: raise Refuse('UNSAFE_DIRECTORY')
        items = []
        for name in HASHES:
            info, raw = read_file(worker, name, missing=name == 'research_guard.py')
            items.append(dict(name=name, parent=worker, info=info, raw=raw))
        protected = []
        for fd_parent, name in ((worker, 'order_gateway.py'), (parent, 'Dockerfile')):
            info, raw = read_file(fd_parent, name)
            protected.append(dict(name=name, parent=fd_parent, info=info, raw=raw))
        for name, expected in ANCHORS.items():
            info, raw = read_file(worker, name)
            if digest(raw) != expected: raise Refuse('CORE_BASELINE_MISMATCH')
            protected.append(dict(name=name, parent=worker, info=info, raw=raw))
        if args.restore_backup:
            load_restore(project, args.restore_backup, items)
            stage(items)
            install(items, protected, project)
            return dict(status='RESTORED', files=5, gateway_sha256=digest(protected[0]['raw']), dockerfile_sha256=digest(protected[1]['raw']))
        old = all((digest(item['raw']) if item['raw'] is not None else None) == HASHES[item['name']][0] for item in items)
        new = all(item['raw'] is not None and digest(item['raw']) == HASHES[item['name']][1] for item in items)
        if not old and not new: raise Refuse('SOURCE_BASELINE_MISMATCH')
        for item in items:
            raw = git_file(project, 'worker/' + item['name'])
            if digest(raw) != HASHES[item['name']][1]: raise Refuse('PINNED_SOURCE_MISMATCH')
            try: compile(raw, item['name'], 'exec')
            except (SyntaxError, UnicodeError): raise Refuse('PINNED_SOURCE_PARSE_FAILED') from None
            item['changed'] = raw
        for item in items + protected: unchanged(item)
        if new:
            return dict(status='UNCHANGED', files=0, target_commit=TARGET_COMMIT)
        report = dict(status='UPDATE_AVAILABLE', files=5, target_commit=TARGET_COMMIT,
            gateway_sha256=digest(protected[0]['raw']), dockerfile_sha256=digest(protected[1]['raw']))
        if not args.apply: return report
        stage(items)
        try:
            for item in items + protected: unchanged(item)
            root_path = args.backup_root or project.parent / (project.name + '-worker-update-backups')
            saved = backup(project, root_path, items, protected)
            install(items, protected, project)
        finally: cleanup_stages(items)
        report.update(status='APPLIED', backup=str(saved))
        return report
    finally:
        if worker is not None: os.close(worker)
        os.close(parent)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path('/root/harun-ai-trading-office'))
    parser.add_argument('--backup-root', type=Path)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--apply', action='store_true')
    selection.add_argument('--restore-backup', type=Path)
    args = parser.parse_args(argv)
    try: result = run(args)
    except Refuse as error:
        print(json.dumps({'error': str(error)}, sort_keys=True))
        return 1
    except Exception:
        print('{"error":"WORKER_UPDATE_FAILED"}')
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
