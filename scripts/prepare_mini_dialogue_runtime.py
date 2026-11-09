"""Reproduce the retained Mini service with only the reviewed shared UTC fix.

This prepares an image input, never a provider request or production deployment.
The canonical service retains historical Hermes compatibility for audit/tests;
the restored Mini image contains no Hermes runtime or bridge imports.
"""
import ast
import base64
import hashlib
import json
from pathlib import Path
import zlib


ROOT = Path(__file__).resolve().parents[1]
PROFILE = 'RESTORED_MINI_UTC_PROVENANCE_V1'
BASE_IMAGE = 'ghcr.io/harun5231/harun-neurobro-agent@sha256:83e5d17b04bfe2b577d9e401859a5e1221eeb564f7e5d776391b757909fa4626'
BASE_COMMIT = '4a0e65910ac4c36a73c7dcf99b566edbdd02f16a'
BASE_SHA256 = '4700ad77d5e7d05857d3eb1bc8f14c5d3cb3731e36fbbec80025a378b9c42ca7'
UTC_PATCH = 'b06fdf08abee9feeedc686f5ef5b25aa717ea03f'
SOURCE_SHA256 = '8ff6f3d25a4dc72c644b101205620196b4f6c202b0e32af947fbd1cc948fbcde'
GENERATED = Path('deploy/.mini-dialogue-runtime/agent/dialogue_service.py')
UNCHANGED_SOURCES = {
    'agent/llm.py': 'c25ff71062d9bc25a5be341d3ee4b8828e5331e3b3cf88147e1bcedc11ee89b7',
    'agent/memory.py': '0fc28cf527a27e653cd9992946afbbba14bb2ed276833c06d1f9e88634dfb272',
    'agent/dialogue_client.py': '87b873b40ebff100fef08481783a85d3b027e21d2f2be7ed3f06688fe8f04e92',
    'agent/native_status_bridge.py': 'f6ad632038414188572d5de9a53c5d71ac2cd6d5cf7f08c559e6300787bede9c',
}


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def build_source(root=ROOT):
    root = Path(root)
    encoded = b''.join((root / 'deploy/mini-dialogue-service-4a0e659.zlib.b85').read_bytes().splitlines())
    raw = zlib.decompress(base64.b85decode(encoded))
    if sha256(raw) != BASE_SHA256:
        raise ValueError('MINI_RETAINED_SOURCE_CHANGED')
    for name, expected in UNCHANGED_SOURCES.items():
        if sha256((root / name).read_bytes()) != expected:
            raise ValueError('MINI_RETAINED_DEPENDENCY_CHANGED')
    source = raw.decode('utf-8')
    current = (root / 'agent/dialogue_service.py').read_text()
    nodes = [node for node in ast.parse(current).body
        if isinstance(node, ast.FunctionDef) and node.name == '_without_sourced_utc_times']
    if len(nodes) != 1:
        raise ValueError('MINI_UTC_SOURCE_UNVERIFIED')
    helper = ast.get_source_segment(current, nodes[0])
    needle = r"    without_timeframes = re.sub(r'(?i)\b(?:15\s*M|1\s*H|2\s*R|1\s*:\s*2)\b', '', question)"
    if source.count(needle) != 1 or source.count('def validate_question_sources(') != 1:
        raise ValueError('MINI_UTC_PATCH_TARGET_CHANGED')
    source = source.replace('def validate_question_sources(',
        helper + '\n\n\ndef validate_question_sources(', 1).replace(needle,
        r"    without_timeframes = re.sub(r'(?i)\b(?:15\s*M|1\s*H|2\s*R|1\s*:\s*2)\b', ''," +
        '\n        _without_sourced_utc_times(question,request))', 1)
    result = source.encode('utf-8')
    if sha256(result) != SOURCE_SHA256:
        raise ValueError('MINI_DERIVED_SOURCE_CHANGED')
    tree = ast.parse(source)
    if any(isinstance(node, ast.ImportFrom) and 'hermes' in (node.module or '')
           for node in tree.body):
        raise ValueError('MINI_HERMES_IMPORT_REFUSED')
    compile(source, 'retained-mini-dialogue-service.py', 'exec')
    return result


def prepare(root=ROOT):
    root = Path(root)
    raw = build_source(root)
    target = root / GENERATED
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists():
        if target.is_symlink() or target.read_bytes() != raw:
            raise ValueError('MINI_GENERATED_SOURCE_CONFLICT')
    else:
        with target.open('xb') as stream:
            stream.write(raw)
        target.chmod(0o444)
    return {'profile': PROFILE, 'base_image': BASE_IMAGE,
        'base_source_commit': BASE_COMMIT, 'base_source_sha256': BASE_SHA256,
        'shared_utc_patch': UTC_PATCH, 'source_sha256': sha256(raw),
        'source_path': str(GENERATED), 'model': 'gpt-5-mini',
        'hermes_runtime_inherited': False, 'deployment_authorized': False}


if __name__ == '__main__':
    print(json.dumps(prepare(), sort_keys=True))
