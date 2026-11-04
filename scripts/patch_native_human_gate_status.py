"""Offline source-bound addition to the retained native DOM status check.

The patch observes a normal verification overlay; it never solves or dismisses
it. Existing origin, iframe, login and DOM-contract behavior stays unchanged.
No installation, browser restart or status write is performed by this module.
"""
import ast
import hashlib
import re


DOM_BEFORE_SHA256 = 'c842713f886992af7289bc6503b57ef6d06fc59dcb85f3be7cc1230ef6fd1071'
STATUS_BEFORE_SHA256 = 'f816941fe724f447a322cbaa9e13f6587aefb85a4e4cc00258fd76bf2a21a0c4'
VISIBLE_BEFORE_SHA256 = 'e5e06ed9e89ea7b9893d0d83ca83294b7d2c8a0f49ec3d96a8973d4ec720b12a'
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
    # Insert AFTER the original iframe branch: existing checks keep priority,
    # origin validation already ran, and login detection still follows unchanged.
    rows = source.splitlines(keepends=True)
    offset = sum(map(len, rows[:frame.end_lineno]))
    indent = ' ' * frame.col_offset
    added = (
        indent + 'from agent.native_human_gate_status import visible_human_gate\n'
        + indent + 'if visible_human_gate(self.page, message_selector=MESSAGE):\n'
        + indent + "    observation['reason'] = 'CHALLENGE_VISIBLE'\n"
        + indent + "    return 'CHALLENGE_REQUIRED'\n")
    result = source[:offset] + added.encode() + source[offset:]
    ast.parse(result.decode('utf-8'))
    return result
