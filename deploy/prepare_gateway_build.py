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
WIRE_INIT_BEGIN = '# HARUN_FILE_SECRETS_V1_BEGIN sha256='
WIRE_INIT_END = '# HARUN_FILE_SECRETS_V1_END'
WIRE_STATUS_BEGIN = '# HARUN_FILE_STATUS_V1_BEGIN sha256='
WIRE_STATUS_END = '# HARUN_FILE_STATUS_V1_END'
WIRE_CREDENTIAL_FIELDS = {'apikey', 'apisecret', 'secret', 'secretkey', 'password', 'token',
                          'signature', 'authorization', 'credentials', 'accesskey',
                          'accesstoken', 'bearertoken', 'authtoken', 'privatekey', 'key'}


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


def source_span(raw, node):
    lines = raw.splitlines(keepends=True)
    start = sum(map(len, lines[:node.lineno - 1])) + node.col_offset
    end = sum(map(len, lines[:node.end_lineno - 1])) + node.end_col_offset
    return start, end


def line_start(raw, number):
    return sum(map(len, raw.splitlines(keepends=True)[:number - 1]))


def wire_methods(raw):
    try:
        tree = ast.parse(raw)
    except (SyntaxError, UnicodeError):
        raise Refuse('SOURCE_PARSE_FAILED') from None
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'OrderGateway']
    if len(classes) != 1 or classes[0].decorator_list or classes[0].keywords:
        raise Refuse('WIRE_CONSTRUCTOR_UNSUPPORTED')
    cls = classes[0]
    protected = {'__init__', 'status'}
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if ((isinstance(node, ast.ClassDef) and node.name in protected)
                or any(isinstance(value, ast.Name) and isinstance(value.ctx, (ast.Store, ast.Del))
                       and value.id in protected for value in ast.walk(node))
                or (isinstance(node, ast.Import) and any((alias.asname or alias.name.split('.')[0]) in protected for alias in node.names))
                or (isinstance(node, ast.ImportFrom) and any((alias.asname or alias.name) in protected or alias.name == '*' for alias in node.names))):
            raise Refuse('WIRE_CONSTRUCTOR_UNSUPPORTED')
    methods = {}
    for name in ('__init__', 'status'):
        found = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
        if len(found) != 1 or not isinstance(found[0], ast.FunctionDef) or found[0].decorator_list:
            raise Refuse('WIRE_' + ('CONSTRUCTOR' if name == '__init__' else 'STATUS') + '_UNSUPPORTED')
        methods[name] = found[0]
    if methods['__init__'].lineno >= methods['status'].lineno:
        raise Refuse('WIRE_CONSTRUCTOR_UNSUPPORTED')
    return tree, methods['__init__'], methods['status']


def self_attr(node, name):
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'self' and node.attr == name


def pure_connected(node):
    return (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And) and len(node.values) == 2
            and self_attr(node.values[0], 'api_key') and self_attr(node.values[1], 'api_secret'))


def validate_wire_shape(raw):
    tree, ctor, status = wire_methods(raw)
    args = ctor.args
    if (args.posonlyargs or args.vararg or args.kwarg or args.kwonlyargs
            or [arg.arg for arg in args.args] != ['self', 'api_key', 'api_secret', 'base_url']
            or len(args.defaults) != 3
            or any(not isinstance(node, ast.Constant) or node.value != value
                   for node, value in zip(args.defaults, ('', '', 'https://fapi.binance.com')))
            or any(not isinstance(arg.annotation, ast.Name) or arg.annotation.id != 'str' for arg in args.args[1:])):
        raise Refuse('WIRE_CONSTRUCTOR_UNSUPPORTED')
    body = ctor.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if (len(body) != 5 or not isinstance(body[0], ast.Import)
            or [(alias.name, alias.asname) for alias in body[0].names] != [('os', None)]):
        raise Refuse('WIRE_CONSTRUCTOR_UNSUPPORTED')
    for node, name in zip(body[1:], ('api_key', 'api_secret', 'base_url', '_connected')):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not self_attr(node.targets[0], name):
            raise Refuse('WIRE_CONSTRUCTOR_UNSUPPORTED')
        value = node.value
        if name in ('api_key', 'api_secret'):
            if not (isinstance(value, ast.IfExp) and isinstance(value.test, ast.Name) and value.test.id == name
                    and isinstance(value.body, ast.Name) and value.body.id == name
                    and isinstance(value.orelse, ast.Call) and isinstance(value.orelse.func, ast.Attribute)
                    and isinstance(value.orelse.func.value, ast.Name) and value.orelse.func.value.id == 'os'
                    and value.orelse.func.attr == 'getenv' and not value.orelse.keywords
                    and 1 <= len(value.orelse.args) <= 2
                    and all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in value.orelse.args)
                    and bool(value.orelse.args[0].value.strip())
                    and (len(value.orelse.args) == 1 or value.orelse.args[1].value == '')):
                raise Refuse('WIRE_CONSTRUCTOR_UNSUPPORTED')
        elif name == 'base_url':
            if not isinstance(value, ast.Name) or value.id != 'base_url':
                raise Refuse('WIRE_CONSTRUCTOR_UNSUPPORTED')
        elif not ((isinstance(value, ast.IfExp) and pure_connected(value.test)
                   and isinstance(value.body, ast.Constant) and value.body.value is True
                   and isinstance(value.orelse, ast.Constant) and value.orelse.value is False)
                  or (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == 'bool'
                      and not value.keywords and len(value.args) == 1 and pure_connected(value.args[0]))):
            raise Refuse('WIRE_CONSTRUCTOR_UNSUPPORTED')
    args = status.args
    if (args.posonlyargs or args.vararg or args.kwarg or args.kwonlyargs or args.defaults
            or [arg.arg for arg in args.args] != ['self'] or len(status.body) != 1
            or not isinstance(status.body[0], ast.Return) or not isinstance(status.body[0].value, ast.Dict)):
        raise Refuse('WIRE_STATUS_UNSUPPORTED')
    value = status.body[0].value
    if (len(value.keys) != 4 or any(not isinstance(key, ast.Constant) or not isinstance(key.value, str) for key in value.keys)
            or len({key.value for key in value.keys}) != 4
            or {'status', 'failure_code'} & {key.value for key in value.keys}):
        raise Refuse('WIRE_STATUS_UNSUPPORTED')
    connected = False
    clock = ast.dump(ast.parse('datetime.now(timezone.utc).isoformat()', mode='eval').body, include_attributes=False)
    for key, val in zip(value.keys, value.values):
        normalized = re.sub(r'[^a-z0-9]', '', key.value.lower())
        if normalized in WIRE_CREDENTIAL_FIELDS or normalized.removeprefix('binance') in WIRE_CREDENTIAL_FIELDS:
            raise Refuse('WIRE_STATUS_UNSUPPORTED')
        if key.value == 'connected':
            if not self_attr(val, '_connected'):
                raise Refuse('WIRE_STATUS_UNSUPPORTED')
            connected = True
        elif not (isinstance(val, ast.Constant) and type(val.value) in (str, bool, int, float, type(None))) and ast.dump(val, include_attributes=False) != clock:
            raise Refuse('WIRE_STATUS_UNSUPPORTED')
    if not connected:
        raise Refuse('WIRE_STATUS_UNSUPPORTED')
    return tree, ctor, status


def wire_init_block(indent, checksum, newline):
    lines = [WIRE_INIT_BEGIN + checksum,
        'self._office_file_secret_failure = None',
        'if api_key and api_secret:',
        '    self.api_key = api_key', '    self.api_secret = api_secret', '    self._connected = True',
        'elif api_key or api_secret:',
        "    self.api_key = ''", "    self.api_secret = ''", '    self._connected = False',
        "    self._office_file_secret_failure = 'BINANCE_NOT_CONFIGURED'", 'else:',
        '    from .binance_private import read_secret as _office_read_secret, BinanceCheckError as _office_secret_error',
        '    try:', "        self.api_key = _office_read_secret('BINANCE_API_KEY_FILE')",
        "        self.api_secret = _office_read_secret('BINANCE_API_SECRET_FILE')",
        '    except _office_secret_error:', "        self.api_key = ''", "        self.api_secret = ''",
        "        self._office_file_secret_failure = 'BINANCE_NOT_CONFIGURED'",
        '    self._connected = bool(self.api_key and self.api_secret)', WIRE_INIT_END]
    return b''.join(indent + line.encode() + newline for line in lines)


def wire_status_block(indent, checksum, original, tail, newline):
    return (indent + WIRE_STATUS_BEGIN.encode() + checksum.encode() + newline
        + indent + b'return (' + original + b') | {' + newline
        + indent + b"    'status': 'CONFIGURED' if self._connected else 'NOT_CONNECTED'," + newline
        + indent + b"    'connected': bool(self._connected)," + newline
        + indent + b"    'failure_code': self._office_file_secret_failure," + newline
        + indent + b'}' + tail + (b'' if tail.endswith(b'\n') else newline)
        + indent + WIRE_STATUS_END.encode() + newline)


def marker_range(raw, begin, end):
    start = re.compile(rb'(?m)^([ \t]*)' + re.escape(begin.encode()) + rb'([0-9a-f]{64})\r?\n')
    finish = re.compile(rb'(?m)^([ \t]*)' + re.escape(end.encode()) + rb'(?:\r?\n|$)')
    starts, finishes = list(start.finditer(raw)), list(finish.finditer(raw))
    if len(starts) != 1 or len(finishes) != 1 or starts[0].end() >= finishes[0].start() or starts[0].group(1) != finishes[0].group(1):
        raise Refuse('WIRE_MARKER_CONFLICT')
    return starts[0], finishes[0]


def wire_file_secrets(raw):
    newline = b'\r\n' if b'\r\n' in raw else b'\n'
    _, ctor, status = wire_methods(raw)
    marked = b'HARUN_FILE_SECRETS_V1_' in raw or b'HARUN_FILE_STATUS_V1_' in raw
    if marked:
        try:
            init_begin, init_end = marker_range(raw, WIRE_INIT_BEGIN, WIRE_INIT_END)
            status_begin, status_end = marker_range(raw, WIRE_STATUS_BEGIN, WIRE_STATUS_END)
            if raw.count(b'HARUN_FILE_SECRETS_V1_') != 2 or raw.count(b'HARUN_FILE_STATUS_V1_') != 2:
                raise Refuse('WIRE_MARKER_CONFLICT')
            ctor_start = line_start(raw, ctor.lineno)
            status_start = line_start(raw, status.lineno)
            if not (ctor_start < init_begin.start() <= line_start(raw, ctor.end_lineno)
                    and init_end.start() == line_start(raw, ctor.end_lineno + 1)
                    and init_end.end() <= status_start):
                raise Refuse('WIRE_MARKER_CONFLICT')
            if not (status_start < status_begin.start()
                    and status_end.start() == line_start(raw, status.end_lineno + 1)):
                raise Refuse('WIRE_MARKER_CONFLICT')
            prefix = raw[ctor_start:init_begin.start()]
            checksum = digest(prefix)
            expected_init = wire_init_block(init_begin.group(1), checksum, newline)
            if init_begin.group(2).decode() != checksum or raw[init_begin.start():init_end.end()] != expected_init:
                raise Refuse('WIRE_MARKER_CONFLICT')
            if (len(status.body) != 1 or not isinstance(status.body[0], ast.Return)
                    or not isinstance(status.body[0].value, ast.BinOp) or not isinstance(status.body[0].value.op, ast.BitOr)
                    or not isinstance(status.body[0].value.left, ast.Dict)):
                raise Refuse('WIRE_MARKER_CONFLICT')
            if status_begin.end() != line_start(raw, status.body[0].lineno):
                raise Refuse('WIRE_MARKER_CONFLICT')
            returned = status.body[0].value
            start, end = source_span(raw, returned.left)
            original = raw[start:end]
            _, outer_end = source_span(raw, returned)
            tail = raw[outer_end:status_end.start()]
            if not re.fullmatch(rb'[ \t]*(?:#[^\r\n]*)?(?:\r?\n)?', tail):
                raise Refuse('WIRE_MARKER_CONFLICT')
            status_prefix = raw[status_start:status_begin.start()]
            checksum = digest(status_prefix + original + tail)
            expected_status = wire_status_block(status_begin.group(1), checksum, original, tail, newline)
            if status_begin.group(2).decode() != checksum or raw[status_begin.start():status_end.end()] != expected_status:
                raise Refuse('WIRE_MARKER_CONFLICT')
            unwired = raw
            edits = [(init_begin.start(), init_end.end(), b''),
                     (status_begin.start(), status_end.end(), status_begin.group(1) + b'return ' + original + tail)]
            for start, end, value in sorted(edits, reverse=True):
                unwired = unwired[:start] + value + unwired[end:]
            validate_wire_shape(unwired)
            return raw
        except Refuse:
            raise Refuse('WIRE_MARKER_CONFLICT') from None
    tree, ctor, status = validate_wire_shape(raw)
    if any((isinstance(node, ast.Name) and node.id.startswith('_office_'))
           or (isinstance(node, ast.Attribute) and node.attr == '_office_file_secret_failure') for node in ast.walk(tree)):
        raise Refuse('WIRE_MARKER_CONFLICT')
    lines = raw.splitlines(keepends=True)
    ctor_start = line_start(raw, ctor.lineno)
    ctor_end = sum(map(len, lines[:ctor.end_lineno]))
    indent = lines[ctor.body[0].lineno - 1][:ctor.body[0].col_offset]
    if indent.strip() or not raw[ctor_start:ctor_end].endswith(b'\n'):
        raise Refuse('WIRE_CONSTRUCTOR_UNSUPPORTED')
    init_block = wire_init_block(indent, digest(raw[ctor_start:ctor_end]), newline)
    returned = status.body[0]
    value_start, value_end = source_span(raw, returned.value)
    return_start = line_start(raw, returned.lineno)
    return_end = sum(map(len, lines[:returned.end_lineno]))
    status_start = line_start(raw, status.lineno)
    indent = lines[returned.lineno - 1][:returned.col_offset]
    if raw[return_start:value_start] != indent + b'return ':
        raise Refuse('WIRE_STATUS_UNSUPPORTED')
    original, tail = raw[value_start:value_end], raw[value_end:return_end]
    if not tail.endswith(b'\n'):
        tail += newline
    status_block = wire_status_block(indent, digest(raw[status_start:return_start] + original + tail), original, tail, newline)
    changed = raw
    for start, end, value in sorted([(ctor_end, ctor_end, init_block), (return_start, return_end, status_block)], reverse=True):
        changed = changed[:start] + value + changed[end:]
    try:
        ast.parse(changed)
    except (SyntaxError, UnicodeError):
        raise Refuse('PREPARED_SOURCE_PARSE_FAILED') from None
    # Apply the same exact marker and shape checks to a newly produced patch.
    if wire_file_secrets(changed) != changed:
        raise Refuse('WIRE_MARKER_CONFLICT')
    return changed


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
        if args.wire_file_secrets:
            items[0]['changed'] = wire_file_secrets(items[0]['changed'])
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
    parser.add_argument('--wire-file-secrets', action='store_true',
                        help='Opt in to the verified simple constructor/status FILE-secret bridge; no exchange requests.')
    args = parser.parse_args(argv)
    if args.restore and args.wire_file_secrets:
        parser.error('--restore and --wire-file-secrets cannot be combined')
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
