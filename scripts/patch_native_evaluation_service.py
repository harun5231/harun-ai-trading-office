"""Prepare one source-bound additive READ RPC in the existing native handler.

Offline only: returned source is never installed or executed by this utility.
The operator must compare the expected full-source hash with the active image;
this patch does not touch BrowserActor/Send, gateway, staging or deployment.
"""
import ast
import hashlib
import re


def patch(source, *, expected_sha256):
    if (type(source) is not bytes or type(expected_sha256) is not str
            or re.fullmatch('[a-f0-9]{64}',expected_sha256) is None
            or hashlib.sha256(source).hexdigest()!=expected_sha256):
        raise ValueError('EVALUATION_SERVICE_SOURCE_UNVERIFIED')
    tree=ast.parse(source.decode('utf-8'))
    methods=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='do_POST']
    if len(methods)!=1:raise ValueError('EVALUATION_SERVICE_HANDLER_UNVERIFIED')
    method=methods[0]
    matches=[n for n in method.body if isinstance(n,ast.Assign) and len(n.targets)==1
        and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='path'
        and ast.dump(n.value)==ast.dump(ast.parse('path.path',mode='eval').body)]
    if len(matches)!=1 or b'/v1/historical-evidence' in source:
        raise ValueError('EVALUATION_SERVICE_PATCH_SITE_UNVERIFIED')
    names={n.attr for n in ast.walk(method) if isinstance(n,ast.Attribute) and isinstance(n.value,ast.Name) and n.value.id=='self'}
    if not {'authorized','body','reply'}<=names:raise ValueError('EVALUATION_SERVICE_AUTH_UNVERIFIED')
    site=matches[0];rows=source.splitlines(keepends=True);offset=sum(map(len,rows[:site.end_lineno]))
    indent=' '*site.col_offset
    added='''if path == '/v1/historical-evidence':
    if not self.authorized():
        return self.reply(403, {'error': 'FORBIDDEN'})
    try:
        value = self.body(1000000)
        from agent.native_evaluation_rpc import handle
        result = handle(value)
        return self.reply(200, result)
    except ValueError as error:
        code = str(error)
        if not (code.startswith('EVALUATION_') and len(code) <= 160):
            code = 'EVALUATION_NATIVE_EVIDENCE_UNAVAILABLE'
        return self.reply(409, {'error': code})
    except Exception:
        return self.reply(409, {'error': 'EVALUATION_NATIVE_EVIDENCE_UNAVAILABLE'})
'''
    raw=''.join(indent+line+'\n' for line in added.rstrip().splitlines()).encode()
    result=source[:offset]+raw+source[offset:]
    ast.parse(result.decode('utf-8'))
    return result
