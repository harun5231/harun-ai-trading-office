"""Offline source-bound verification and ordinary-notice status additions.

The patch observes a verification overlay without solving or dismissing it.
After those guards and password/OTP guards, only a source-bound native AUTO
observer may acknowledge the exact known ordinary maintenance notice.
Existing origin, iframe, login and DOM-contract decisions remain unchanged.
No installation, browser restart or status write is performed by this module.
"""
import ast
import hashlib
import re


DOM_BEFORE_SHA256 = 'c842713f886992af7289bc6503b57ef6d06fc59dcb85f3be7cc1230ef6fd1071'
STATUS_BEFORE_SHA256 = 'f816941fe724f447a322cbaa9e13f6587aefb85a4e4cc00258fd76bf2a21a0c4'
VISIBLE_BEFORE_SHA256 = 'e5e06ed9e89ea7b9893d0d83ca83294b7d2c8a0f49ec3d96a8973d4ec720b12a'
ACTOR_BEFORE_SHA256 = '5569f51b8f06028ea96e525b5cdf113f21f78bcb4f58c514f189e4bed3b1b37a'
OBSERVE_BEFORE_SHA256 = '22309b8870e281515cb67060cb1760d705dc885045fb907d96d2ec63e50fd2d6'
MODE_BEFORE_SHA256 = 'ae02fc5fb1afd19ac623da2385e262d50b227b9c3e82f8c9d01450a66cddb313'
KNOWN_CHALLENGE_SELECTOR = (
    "iframe[src*='challenges.cloudflare.com'], iframe[src*='hcaptcha.com'], "
    "iframe[src*='/recaptcha/']")


def patch(source, *, expected_sha256=DOM_BEFORE_SHA256):
    if (type(source) is not bytes or type(expected_sha256) is not str
            or re.fullmatch('[a-f0-9]{64}', expected_sha256) is None
            or hashlib.sha256(source).hexdigest() != expected_sha256):
        raise ValueError('HUMAN_GATE_PARENT_SOURCE_UNVERIFIED')
    text = source.decode('utf-8')
    tree = ast.parse(text)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DOMBridge']
    if len(classes) != 1 or 'native_human_gate_status' in text:
        raise ValueError('HUMAN_GATE_NATIVE_CONTRACT_UNVERIFIED')
    methods = {n.name:n for n in classes[0].body if isinstance(n, ast.FunctionDef)}
    for name, expected in (('status', STATUS_BEFORE_SHA256), ('_visible', VISIBLE_BEFORE_SHA256)):
        method = methods.get(name)
        if method is None or hashlib.sha256(ast.get_source_segment(text, method).encode()).hexdigest() != expected:
            raise ValueError('HUMAN_GATE_NATIVE_CONTRACT_UNVERIFIED')
    messages = [n for n in tree.body if isinstance(n, ast.Assign)
        and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id == 'MESSAGE']
    if len(messages) != 1 or not isinstance(messages[0].value, ast.Constant) or messages[0].value.value != '[data-message-id]':
        raise ValueError('HUMAN_GATE_MESSAGE_CONTRACT_UNVERIFIED')
    status = methods['status']
    frames = [n for n in status.body if isinstance(n, ast.If)
        and isinstance(n.test, ast.Call) and isinstance(n.test.func, ast.Attribute)
        and isinstance(n.test.func.value, ast.Name) and n.test.func.value.id == 'self'
        and n.test.func.attr == '_visible' and len(n.test.args) == 1
        and isinstance(n.test.args[0], ast.Constant)
        and n.test.args[0].value == KNOWN_CHALLENGE_SELECTOR]
    if len(frames) != 1:
        raise ValueError('HUMAN_GATE_PATCH_SITE_UNVERIFIED')
    frame = frames[0]
    if not any(isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
            and n.value.value == 'CHALLENGE_REQUIRED' for n in frame.body):
        raise ValueError('HUMAN_GATE_PATCH_SITE_UNVERIFIED')
    logins = [n for n in status.body if isinstance(n, ast.If)
        and isinstance(n.test, ast.Call) and isinstance(n.test.func, ast.Attribute)
        and isinstance(n.test.func.value, ast.Name) and n.test.func.value.id == 'self'
        and n.test.func.attr == '_visible' and len(n.test.args) == 1
        and isinstance(n.test.args[0], ast.Constant)
        and n.test.args[0].value == "input[type='password'], input[autocomplete='one-time-code']"]
    if len(logins) != 1 or not any(isinstance(n, ast.Return)
            and isinstance(n.value, ast.Constant) and n.value.value == 'LOGIN_REQUIRED'
            for n in logins[0].body):
        raise ValueError('HUMAN_GATE_PATCH_SITE_UNVERIFIED')
    # Origin/known iframe/human/password/OTP checks retain their priority.
    # Legacy calls receive no AUTO callback, so they cannot acknowledge notices.
    rows = source.splitlines(keepends=True)
    offset = sum(map(len, rows[:frame.end_lineno]))
    indent = ' ' * frame.col_offset
    added = (
        indent + 'from agent.native_human_gate_status import visible_human_gate\n'
        + indent + 'if visible_human_gate(self.page, message_selector=MESSAGE):\n'
        + indent + "    observation['reason'] = 'CHALLENGE_VISIBLE'\n"
        + indent + "    return 'CHALLENGE_REQUIRED'\n")
    login_offset = sum(map(len, rows[:logins[0].end_lineno]))
    ordinary = (
        indent + 'from agent.native_human_gate_status import acknowledge_known_maintenance_notice\n'
        + indent + 'acknowledge_known_maintenance_notice(self.page, message_selector=MESSAGE,\n'
        + indent + '    maintenance_allowed=maintenance_allowed)\n')
    header_start = sum(map(len, rows[:status.lineno-1]))
    header = rows[status.lineno-1]
    if header.strip() != b'def status(self):':
        raise ValueError('HUMAN_GATE_PATCH_SITE_UNVERIFIED')
    replacement = b' ' * status.col_offset + b'def status(self, *, maintenance_allowed=None):\n'
    edits = [(offset, offset, added.encode()), (login_offset, login_offset, ordinary.encode()),
        (header_start, header_start+len(header), replacement)]
    result = source
    for start, end, value in sorted(edits, reverse=True):
        result = result[:start] + value + result[end:]
    ast.parse(result.decode('utf-8'))
    return result


def patch_actor(source, *, expected_sha256=ACTOR_BEFORE_SHA256):
    """Bind the optional notice callback at the actual owner-thread poll only.

    The callback reads current native mode, manual-browser ownership, owner
    session activity and stopping state. It never changes any of those fields.
    No other Actor status/cache/diagnostic/dispatch byte is modified.
    """
    if (type(source) is not bytes or type(expected_sha256) is not str
            or re.fullmatch('[a-f0-9]{64}', expected_sha256) is None
            or hashlib.sha256(source).hexdigest() != expected_sha256):
        raise ValueError('MAINTENANCE_ACTOR_PARENT_SOURCE_UNVERIFIED')
    text=source.decode('utf-8');tree=ast.parse(text)
    classes=[n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='BrowserActor']
    if len(classes)!=1 or 'maintenance_allowed' in text:
        raise ValueError('MAINTENANCE_ACTOR_CONTRACT_UNVERIFIED')
    methods={n.name:n for n in classes[0].body if isinstance(n,ast.FunctionDef)}
    for name,expected in (('_observe_site_status',OBSERVE_BEFORE_SHA256),('_mode',MODE_BEFORE_SHA256)):
        node=methods.get(name)
        if node is None or hashlib.sha256(ast.get_source_segment(text,node).encode()).hexdigest()!=expected:
            raise ValueError('MAINTENANCE_ACTOR_CONTRACT_UNVERIFIED')
    calls=[n for n in ast.walk(methods['_observe_site_status']) if isinstance(n,ast.Call)
        and ast.get_source_segment(text,n)=='self._bridge.status()']
    if len(calls)!=1:
        raise ValueError('MAINTENANCE_ACTOR_PATCH_SITE_UNVERIFIED')
    call=calls[0];rows=source.splitlines(keepends=True)
    start=sum(map(len,rows[:call.lineno-1]))+call.col_offset
    end=sum(map(len,rows[:call.end_lineno-1]))+call.end_col_offset
    replacement=("self._bridge.status(maintenance_allowed=lambda: self._mode() == 'AUTOMATION' "
        "and self._manual is None and not self.sessions.active() and not self.stopping.is_set())").encode()
    result=source[:start]+replacement+source[end:]
    ast.parse(result.decode('utf-8'))
    return result
