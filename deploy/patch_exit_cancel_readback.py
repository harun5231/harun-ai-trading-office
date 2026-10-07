"""Offline source-only patch for bounded readback after an owned algo cancel.

The caller must back up the SDK and manage worker deployment. This utility
never imports the SDK, reads secrets or sends exchange requests.
"""
import ast
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

SOURCE_SHA = 'df6705bbdda9f2a34374fde186244d400e2fc84ecb8f27ce05ad15f35a9bbfc4'
OLD_METHOD = "    def _cancel_algo(self, intent, algo):\n        kind = 'sl' if algo['clientAlgoId'].startswith('hao-sl-') else 'tp'\n        quantity = self._decimal(algo['quantity'], positive=True)\n        current = self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': algo['clientAlgoId']})\n        self._algo_proof(intent, kind, quantity, current)\n        if current['algoStatus'] != 'NEW':\n            if current['algoStatus'] not in ('CANCELED', 'EXPIRED', 'REJECTED') or current.get('actualOrderId') not in ('', '0', 0, None):\n                raise Review('BINANCE_ORDER_EXIT_RACE')\n            return\n        self._request('DELETE', '/fapi/v1/algoOrder', {'algoId': self._id(current['algoId'])})\n        after = self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': algo['clientAlgoId']})\n        self._algo_proof(intent, kind, quantity, after)\n        if after['algoStatus'] != 'CANCELED' or after.get('actualOrderId') not in ('', '0', 0, None):\n            raise Review('BINANCE_ORDER_EXIT_RACE')\n"
NEW_METHOD = "    def _cancel_algo(self, intent, algo):\n        kind = 'sl' if algo['clientAlgoId'].startswith('hao-sl-') else 'tp'\n        quantity = self._decimal(algo['quantity'], positive=True)\n        current = self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': algo['clientAlgoId']})\n        self._algo_proof(intent, kind, quantity, current)\n        if current['algoStatus'] != 'NEW':\n            if current['algoStatus'] not in ('CANCELED', 'EXPIRED', 'REJECTED') or current.get('actualOrderId') not in ('', '0', 0, None):\n                raise Review('BINANCE_ORDER_EXIT_RACE')\n            return\n        if current.get('actualOrderId') not in ('', '0', 0, None):\n            raise Review('BINANCE_ORDER_EXIT_RACE')\n        self._request('DELETE', '/fapi/v1/algoOrder', {'algoId': self._id(current['algoId'])})\n        import time\n        for attempt in range(3):\n            after = self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': algo['clientAlgoId']})\n            self._algo_proof(intent, kind, quantity, after)\n            if after['algoStatus'] == 'CANCELED' and after.get('actualOrderId') in ('', '0', 0, None):\n                return\n            if after['algoStatus'] != 'NEW' or after.get('actualOrderId') not in ('', '0', 0, None) or attempt == 2:\n                raise Review('BINANCE_ORDER_EXIT_RACE')\n            delay = (0.5, 1.0)[attempt]\n            self._time_left(delay)\n            time.sleep(delay)\n"


def method_node(tree):
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'OrderGateway']
    if len(classes) != 1:
        raise ValueError('GATEWAY_CLASS_CHANGED')
    methods = [node for node in classes[0].body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == '_cancel_algo']
    if len(methods) != 1 or type(methods[0]) is not ast.FunctionDef or methods[0].decorator_list:
        raise ValueError('GATEWAY_METHOD_CHANGED')
    return methods[0]


def shape(text):
    return ast.dump(method_node(ast.parse('class OrderGateway:\n' + text)), include_attributes=False)


def transform(raw, expected_sha=SOURCE_SHA):
    if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError('GATEWAY_SOURCE_CHANGED')
    tree = ast.parse(raw)
    method = method_node(tree)
    if ast.dump(method, include_attributes=False) != shape(OLD_METHOD):
        raise ValueError('GATEWAY_METHOD_CHANGED')
    lines = raw.splitlines(keepends=True)
    first = sum(map(len, lines[:method.lineno-1]))
    last = sum(map(len, lines[:method.end_lineno]))
    original = raw[first:last]
    newline = b'\r\n' if b'\r\n' in original else b'\n'
    if original.replace(b'\r\n', b'\n') != OLD_METHOD.encode():
        raise ValueError('GATEWAY_METHOD_BYTES_CHANGED')
    replacement = NEW_METHOD.encode().replace(b'\n', newline)
    changed = raw[:first] + replacement + raw[last:]
    compile(changed, 'order_gateway.py', 'exec')
    patched = ast.parse(changed)
    if ast.dump(method_node(patched), include_attributes=False) != shape(NEW_METHOD):
        raise ValueError('GATEWAY_PATCH_INVALID')
    # The AST assertion also protects other methods and private module contents.
    before_cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'OrderGateway')
    after_cls = next(node for node in patched.body if isinstance(node, ast.ClassDef) and node.name == 'OrderGateway')
    before_cls.body.remove(method)
    after_cls.body.remove(method_node(patched))
    if ast.dump(tree, include_attributes=False) != ast.dump(patched, include_attributes=False):
        raise ValueError('GATEWAY_OTHER_SOURCE_CHANGED')
    return changed


def apply(path, write=False):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError('GATEWAY_SOURCE_UNAVAILABLE')
    raw = path.read_bytes()
    changed = transform(raw)
    result = dict(status='PATCH_READY', before_sha256=hashlib.sha256(raw).hexdigest(),
                  after_sha256=hashlib.sha256(changed).hexdigest())
    if not write:
        return result
    if os.geteuid() != 0:
        raise ValueError('ROOT_REQUIRED')
    fd, name = tempfile.mkstemp(prefix='.gateway-exit-readback-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
            os.fchown(output.fileno(), info.st_uid, info.st_gid)
            output.write(changed)
            output.flush()
            os.fsync(output.fileno())
        current = path.lstat()
        if (current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_gid) != (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid) or path.read_bytes() != raw:
            raise ValueError('GATEWAY_SOURCE_CHANGED')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    result['status'] = 'PATCHED'
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('path')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        report = apply(args.path, args.apply)
    except Exception as error:
        reason = str(error) if isinstance(error, ValueError) and str(error).isupper() else 'GATEWAY_PATCH_FAILED'
        report = dict(error=reason)
    print(json.dumps(report, sort_keys=True))
    raise SystemExit(1 if 'error' in report else 0)
