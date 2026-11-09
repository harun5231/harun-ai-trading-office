"""Reproduce retained Mini with the reviewed UTC fix and HTTP diagnostics.

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
PROFILE = 'RESTORED_MINI_TRADING_BUDGET_V4'
BASE_IMAGE = 'ghcr.io/harun5231/harun-neurobro-agent@sha256:83e5d17b04bfe2b577d9e401859a5e1221eeb564f7e5d776391b757909fa4626'
BASE_COMMIT = '4a0e65910ac4c36a73c7dcf99b566edbdd02f16a'
BASE_SHA256 = '4700ad77d5e7d05857d3eb1bc8f14c5d3cb3731e36fbbec80025a378b9c42ca7'
UTC_PATCH = 'b06fdf08abee9feeedc686f5ef5b25aa717ea03f'
UTC_SOURCE_SHA256 = '8ff6f3d25a4dc72c644b101205620196b4f6c202b0e32af947fbd1cc948fbcde'
SOURCE_SHA256 = '033e5b8c4758c3841350132dd58344460273310baf6d30bccad9e762ac748c37'
GENERATED = Path('deploy/.mini-dialogue-runtime/agent/dialogue_service.py')
GENERATED_LLM = Path('deploy/.mini-dialogue-runtime/agent/llm.py')
MODEL_SOURCE_SHA256 = 'f74911e9999c1f918f4651a5ae0555004a7401ab64ec1c67d40201ad2328110a'
UNCHANGED_SOURCES = {
    'agent/memory.py': '0fc28cf527a27e653cd9992946afbbba14bb2ed276833c06d1f9e88634dfb272',
    'agent/dialogue_client.py': '87b873b40ebff100fef08481783a85d3b027e21d2f2be7ed3f06688fe8f04e92',
    'agent/native_status_bridge.py': 'f6ad632038414188572d5de9a53c5d71ac2cd6d5cf7f08c559e6300787bede9c',
}


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def build_utc_source(root=ROOT):
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
    if sha256(result) != UTC_SOURCE_SHA256:
        raise ValueError('MINI_DERIVED_SOURCE_CHANGED')
    tree = ast.parse(source)
    if any(isinstance(node, ast.ImportFrom) and 'hermes' in (node.module or '')
           for node in tree.body):
        raise ValueError('MINI_HERMES_IMPORT_REFUSED')
    compile(source, 'retained-mini-dialogue-service.py', 'exec')
    return result


PURPOSE_RECEIPT = '''    def _existing_purpose_receipt(self, request):
        """Read exact historical receipts without funding a forbidden purpose."""
        operation = 'delegated-model:' + request['request_id']
        memo = ({key:value for key,value in request.items() if key != 'deadline_epoch'}
            if request['phase'].startswith('COMPOSE_') else request)
        with self.memory.lock:
            row = self.memory.db.execute('SELECT kind,request_hash,state,result FROM operations '
                'WHERE operation_id=?',(operation,)).fetchone()
        if row is not None:
            if row[2] in ('STARTED','UNKNOWN'):
                raise DialogueError('DIALOGUE_OPERATION_UNKNOWN')
            if (row[0] == 'DELEGATED_MODEL_DIALOGUE' and row[1] == hashlib.sha256(canonical(memo).encode()).hexdigest()
                    and row[2] == 'DONE' and type(row[3]) is str):
                result = decode_json(row[3].encode())
                if type(result) is dict and '_agent_not_sent' not in result:
                    return validate_result(result,request)
        try:
            denial = {'schema_version':1,'authority':'MODEL_PURPOSE_POLICY','operation_id':operation,
                'phase':request['phase'],'reason':'DIALOGUE_MODEL_PURPOSE_NOT_ALLOWED',
                'request_sent':False,'provider_response_received':False}
            key = 'dialogue-model-purpose-denied:'+hashlib.sha256(canonical(denial).encode()).hexdigest()
            if self.memory.get(key) is None:
                self.memory.put(key,{**denial,'observed_at':self.clock()})
        except Exception:
            pass
        raise NotSent('DIALOGUE_MODEL_PURPOSE_NOT_ALLOWED')

'''


def apply_purpose_policy(raw,root=ROOT):
    source=raw.decode()
    current=(Path(root)/'agent/dialogue_service.py').read_text()
    helpers=[node for node in ast.parse(current).body if isinstance(node,ast.FunctionDef)
        and node.name in {'_archive_compose_request','_without_discussion_structure'}]
    if len(helpers)!=2:
        raise ValueError('MINI_DISCUSSION_STRUCTURE_SOURCE_UNVERIFIED')
    helper='\n\n\n'.join(ast.get_source_segment(current,node) for node in helpers)
    source=source.replace('def validate_question_sources(',helper+'\n\n\ndef validate_question_sources(',1)
    source=source.replace('_without_sourced_utc_times(question,request))',
        '_without_discussion_structure(_without_sourced_utc_times(question,request)))',1)
    source=source.replace('from .memory import Memory, canonical',
        'from .llm import PAID_DIALOGUE_PHASES\nfrom .memory import Memory, canonical',1)
    source=source.replace('    def process(self, request):',PURPOSE_RECEIPT+'    def process(self, request):',1)
    needle='        request = validate_request(request, now=self.clock())\n        with self.lock:'
    if source.count(needle)!=1:
        raise ValueError('MINI_PURPOSE_PATCH_TARGET_CHANGED')
    source=source.replace(needle,'        request = validate_request(request, now=self.clock())\n'
        '        if request[\'phase\'] not in PAID_DIALOGUE_PHASES:\n'
        '            with self.lock:\n'
        '                return self._existing_purpose_receipt(request)\n        with self.lock:',1)
    source=source.replace('                    return original_transport(payload)',
        "                    _archive_compose_request(self.memory,current_id,request,self.clock())\n"
        "                    validate_request(request,now=self.clock())\n"
        "                    need(not self.stopping.is_set() and self.configuration_ready(),'DIALOGUE_UNAVAILABLE')\n"
        "                    need(time.monotonic()<until,'DIALOGUE_DEADLINE_EXPIRED')\n"
        "                    self.model.bind_operation(current_id,request['phase'])\n"
        '                    return original_transport(payload)',1)
    source=source.replace("                    decision = self.model.plan(history, request['reply'], **arguments)",
        "                    self.model.bind_operation(current_id,request['phase'])\n"
        "                    decision = self.model.plan(history, request['reply'], **arguments)",1)
    source=source.replace('def safe_error(error):\n',
        "def safe_error(error):\n    if isinstance(error,NotSent) and str(error)=='DIALOGUE_MODEL_PURPOSE_NOT_ALLOWED':\n"
        "        return 'DIALOGUE_MODEL_NOT_SENT'\n",1)
    budget_needle=('        self.model = model or OpenAIDialogue(self.key_file, memory, timeout=45,\n'
        '            daily_requests=math.inf, daily_input_tokens=math.inf, daily_output_tokens=math.inf)')
    if source.count(budget_needle)!=1:
        raise ValueError('MINI_BUDGET_PATCH_TARGET_CHANGED')
    source=source.replace(budget_needle,
        '        self.model = model or OpenAIDialogue(self.key_file, memory, timeout=45)',1)
    return source.encode()


def build_source(root=ROOT):
    raw=apply_purpose_policy(build_utc_source(root),root)
    if sha256(raw)!=SOURCE_SHA256:
        raise ValueError('MINI_PURPOSE_SOURCE_CHANGED')
    compile(raw,'retained-mini-dialogue-service.py','exec')
    return raw


def prepare(root=ROOT):
    root = Path(root)
    raw = build_source(root)
    model = (root/'agent/llm.py').read_bytes()
    if sha256(model) != MODEL_SOURCE_SHA256:
        raise ValueError('MINI_MODEL_SOURCE_CHANGED')
    payload = {GENERATED:raw,GENERATED_LLM:model}
    # Validate every existing input before writing either generated file.
    for name,value in payload.items():
        target=root/name
        if (target.exists() or target.is_symlink()) and (target.is_symlink() or target.read_bytes()!=value):
            raise ValueError('MINI_GENERATED_SOURCE_CONFLICT')
    for name,value in payload.items():
        target=root/name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not target.exists():
            with target.open('xb') as stream:
                stream.write(value)
            target.chmod(0o444)
    return {'profile': PROFILE, 'base_image': BASE_IMAGE,
        'base_source_commit': BASE_COMMIT, 'base_source_sha256': BASE_SHA256,
        'shared_utc_patch': UTC_PATCH, 'source_sha256': sha256(raw),
        'source_path': str(GENERATED), 'model': 'gpt-5-mini',
        'source_files':{'agent/dialogue_service.py':SOURCE_SHA256,'agent/llm.py':MODEL_SOURCE_SHA256},
        'hermes_runtime_inherited': False, 'deployment_authorized': False}


if __name__ == '__main__':
    print(json.dumps(prepare(), sort_keys=True))
