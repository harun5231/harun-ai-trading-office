"""Prepare exact-source, additive pending-review patches without executing them.

The six original byte strings must match the authenticated release pins. All
edits use AST byte offsets and preserve every byte outside their reviewed spans.
This utility does not import production modules, install files, or start services.
"""
from __future__ import annotations

import ast
import hashlib

from scripts.patch_native_browser_hook import PREFIX as BROWSER_HOOK_PREFIX


SOURCE_PINS = {
    'worker/robot.py': 'ed2147a610d9919bd53ca1ff8181a003e3194e5f5e5b7939be836629f27562ad',
    'worker/neurobrowser.py': '0941bce152f3616b0d725518c6c008f5a89961e487de85d95db8e8fd22ecc70e',
    'worker/research_provider.py': 'e91e8aa40f31a6b40ace4282ee6d09dca9ea806ee05b3f1fff4de56e6428ca51',
    'browser_runtime/actor.py': '0ccb1d63cc898af231e813ed8a3934c219fb9f0dff4f2345bb339c26b729e1ad',
    'browser_runtime/dom.py': 'b5d9591e426b77d0ecc1e74ad09926ab53e181b9083869af49a00cf5303beba0',
    'agent/native_browser_hook.py': '6d398a62cc87b182e84c532511178d42592b89c608c6a525b763dac6dd2878dd',
}
# This is the reviewed successor's existing pending route. It is verified as
# already installed, never passed through the historical six-file migration.
CURRENT_HOOK_SOURCE_SHA256 = '62030618cb17e66cd7689387b030b4eafa4ab7bf5b22e99f08e7c72b714f3fe9'
MAX_SOURCE = 262144
PROTOCOL = b"__import__('agent.native_pending_protocol', fromlist=('%s',)).%s"


class PendingPatchRefused(ValueError):
    pass


def need(ok, reason):
    if not ok:
        raise PendingPatchRefused(reason)


def protocol(name):
    encoded = name.encode('ascii')
    return PROTOCOL % (encoded, encoded)


def syntax(raw):
    try:
        return ast.parse(raw.decode('utf-8'))
    except (SyntaxError, UnicodeError, ValueError, RecursionError):
        raise PendingPatchRefused('NATIVE_PENDING_AST_UNVERIFIED') from None


def matching_method(tree, class_name, method_name):
    found = [method for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
        for method in node.body
        if isinstance(method, ast.FunctionDef) and method.name == method_name]
    need(len(found) == 1, 'NATIVE_PENDING_METHOD_UNVERIFIED')
    return found[0]


def one(found):
    need(len(found) == 1, 'NATIVE_PENDING_PATCH_SITE_UNVERIFIED')
    return found[0]


def same(node, expression):
    return ast.dump(node, include_attributes=False) == ast.dump(
        ast.parse(expression, mode='eval').body, include_attributes=False)


def span(raw, node):
    rows = raw.splitlines(keepends=True)
    start = sum(map(len, rows[:node.lineno - 1])) + node.col_offset
    end = sum(map(len, rows[:node.end_lineno - 1])) + node.end_col_offset
    need(0 <= start < end <= len(raw), 'NATIVE_PENDING_PATCH_SPAN_UNVERIFIED')
    return start, end


def module_functions(tree, expected):
    observed = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
    need(all(observed.count(name) == 1 for name in expected),
         'NATIVE_PENDING_MODULE_UNVERIFIED')


def verify_current_pending_hook(raw):
    """Admit the frozen current artifact without changing any source bytes.

    The original SOURCE_PINS describe a past release and remain immutable.
    Checking this successor separately prevents accidentally applying that old
    migration again merely to exercise the current pending discussion route.
    """
    need(type(raw) is bytes and 0 < len(raw) <= MAX_SOURCE,
         'NATIVE_PENDING_SOURCE_SIZE_UNVERIFIED')
    observed = hashlib.sha256(raw).hexdigest()
    need(observed == CURRENT_HOOK_SOURCE_SHA256,
         'NATIVE_PENDING_CURRENT_HOOK_PIN_MISMATCH')
    tree = syntax(raw)
    phase = one([node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == '_phase'])
    need(len(phase.body) == 2 and isinstance(phase.body[0], ast.ImportFrom)
        and phase.body[0].level == 1 and phase.body[0].module == 'native_pending_protocol'
        and [(item.name, item.asname) for item in phase.body[0].names]
            == [('dialogue_phase_for_payload', None)]
        and isinstance(phase.body[1], ast.Return)
        and same(phase.body[1].value, 'dialogue_phase_for_payload(payload, required)'),
        'NATIVE_PENDING_CURRENT_PHASE_UNVERIFIED')
    ask = matching_method(tree, 'DelegatedDOMBridge', '_ask')
    review = one([node for node in ask.body
        if isinstance(node, ast.FunctionDef) and node.name == 'review'])
    one([node for node in ast.walk(review) if isinstance(node, ast.Assign)
        and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == 'phase' and same(node.value, '_phase(payload, required)')])
    return raw, {'schema_version': 1, 'source_sha256': observed,
        'verified_sha256': observed, 'source_changed': False,
        'production_deployment_performed': False}


def edits_for(path, raw, tree):
    """Return original-source spans; no changes are applied until all verify."""
    edits = []
    if path == 'worker/robot.py':
        method = matching_method(tree, 'Coordinator', '_tick')
        call = one([node for node in ast.walk(method) if isinstance(node, ast.Call)
                    and same(node, 'self.advance_execution(account, today)')])
        start, end = span(raw, call)
        replacement = (b"__import__('agent.native_pending_review', "
            b"fromlist=('review_then_advance',)).review_then_advance(self, account, today)")
        edits.append((start, end, replacement))
    elif path == 'worker/neurobrowser.py':
        method = matching_method(tree, 'NeuroBrowser', 'ask')
        condition = one([node.test for node in ast.walk(method) if isinstance(node, ast.If)
            and same(node.test, 'schema != SETUP_SCHEMA and screening_count(schema) is None')])
        start, end = span(raw, condition)
        edits.append((start, end, b'(' + raw[start:end] + b') and not '
                      + protocol('is_pending_schema') + b'(schema)'))
        edits.append((len(raw), len(raw), b'\n\n' + protocol('install_worker_browser_protocol')
                      + b'(globals())\n'))
    elif path == 'worker/research_provider.py':
        module_functions(tree, ('request_body', 'validate_chart_evidence', 'source_evidence'))
        edits.append((len(raw), len(raw), b'\n\n' + protocol('install_source_protocol')
                      + b'(globals())\n'))
    elif path == 'browser_runtime/actor.py':
        research = matching_method(tree, 'BrowserActor', '_research')
        candidates = []
        for node in ast.walk(research):
            if not isinstance(node, ast.If):
                continue
            capture_import = any(isinstance(child, ast.ImportFrom)
                and child.level == 1 and child.module == 'charts'
                and {name.name for name in child.names} == {'capture', 'close_resource'}
                for child in node.body)
            test = node.test
            if (capture_import and isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And)
                    and len(test.values) == 2 and same(test.values[0], 'isinstance(required, list)')
                    and isinstance(test.values[1], ast.Compare)):
                comparison = test.values[1]
                if (same(comparison.left, 'set(required)') and len(comparison.ops) == 1
                        and isinstance(comparison.ops[0], ast.Eq) and len(comparison.comparators) == 1
                        and isinstance(comparison.comparators[0], ast.Set)):
                    elements = comparison.comparators[0].elts
                    if (len(elements) == 6 and all(isinstance(item, ast.Constant)
                            and type(item.value) is str for item in elements)
                            and {'symbol', 'side'}.issubset({item.value for item in elements})):
                        candidates.append(node)
        condition = one(candidates)
        gates = [node for node in research.body if isinstance(node, ast.If)
                 and same(node.test, 'not self.dispatch_check(value)')]
        gate = one(gates)
        need(gate.lineno < condition.lineno, 'NATIVE_PENDING_DISPATCH_GATE_UNVERIFIED')
        start, end = span(raw, condition.test)
        edits.append((start, end, b'(' + raw[start:end] + b') or '
                      + protocol('is_pending_browser_request') + b'(value)'))
        construction = matching_method(tree, 'BrowserActor', '_open_automation')
        site = one([node.value for node in ast.walk(construction)
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute) and node.targets[0].attr == '_bridge'
            and isinstance(node.targets[0].value, ast.Name) and node.targets[0].value.id == 'self'
            and same(node.value, 'DOMBridge(page)')])
        start, end = span(raw, site)
        edits.append((start, end, BROWSER_HOOK_PREFIX + raw[start:end] + b')'))
    elif path == 'browser_runtime/dom.py':
        module_functions(tree, ('request_kind', 'user_prompt', 'exact_json'))
        matching_method(tree, 'DOMBridge', 'ask')
        edits.append((len(raw), len(raw), b'\n\n' + protocol('install_dom_protocol')
                      + b'(globals())\n'))
    elif path == 'agent/native_browser_hook.py':
        ask = matching_method(tree, 'DelegatedDOMBridge', 'ask')
        review = one([node for node in ask.body
                      if isinstance(node, ast.FunctionDef) and node.name == 'review'])
        target = one([node.value for node in ast.walk(review) if isinstance(node, ast.Assign)
            and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == 'phase'
            and same(node.value, "'SCREENING' if set(required) == {'symbols'} else 'ANALYSIS'")])
        start, end = span(raw, target)
        edits.append((start, end, protocol('dialogue_phase_for_payload') + b'(payload, required)'))
    else:
        raise PendingPatchRefused('NATIVE_PENDING_SOURCE_PATH_UNVERIFIED')
    return edits


def patch_native_pending_sources(sources, *, _expected_sha256=None):
    """Return six new source byte strings plus a hash-only preparation receipt.

    The private expected-pin override is solely for sanitized fixture tests.
    Production callers must omit it and use the fixed authenticated source pins.
    """
    need(type(sources) is dict and set(sources) == set(SOURCE_PINS),
         'NATIVE_PENDING_SOURCE_SET_UNVERIFIED')
    pins = SOURCE_PINS if _expected_sha256 is None else _expected_sha256
    need(type(pins) is dict and set(pins) == set(SOURCE_PINS),
         'NATIVE_PENDING_SOURCE_PIN_SET_UNVERIFIED')
    planned = {}
    for path, raw in sources.items():
        need(type(raw) is bytes and 0 < len(raw) <= MAX_SOURCE,
             'NATIVE_PENDING_SOURCE_SIZE_UNVERIFIED')
        need(hashlib.sha256(raw).hexdigest() == pins[path],
             'NATIVE_PENDING_SOURCE_PIN_MISMATCH')
        tree = syntax(raw)
        need(b'agent.native_pending_protocol' not in raw
             and b'agent.native_pending_review' not in raw
             and BROWSER_HOOK_PREFIX not in raw,
             'NATIVE_PENDING_ALREADY_PATCHED')
        planned[path] = edits_for(path, raw, tree)
    patched, records = {}, {}
    for path, edits in planned.items():
        raw, position, result = sources[path], 0, []
        for start, end, replacement in sorted(edits):
            need(position <= start <= end <= len(raw), 'NATIVE_PENDING_PATCH_OVERLAP')
            result.extend((raw[position:start], replacement))
            position = end
        result.append(raw[position:])
        output = b''.join(result)
        syntax(output)
        patched[path] = output
        records[path] = {'source_sha256': hashlib.sha256(raw).hexdigest(),
            'patched_sha256': hashlib.sha256(output).hexdigest(),
            'source_bytes': len(raw), 'patched_bytes': len(output),
            'reviewed_span_count': len(edits), 'all_other_source_bytes_preserved': True}
    return patched, {'schema_version': 1, 'ok': True, 'sources': records,
                     'production_deployment_performed': False}
