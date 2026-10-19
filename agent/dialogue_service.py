"""Delegated reasoning only; the native coordinator retains every trading action.

Run ``python -m agent.dialogue_service --socket ... --data-directory ...
--token-file ... --key-file ...``. The native browser actor must recheck its own
ROBOT gate immediately before this call and before any actual browser dispatch.
This service neither schedules work nor accesses a browser or trading backend.
"""
import argparse
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import stat
import struct
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from .llm import OpenAIDialogue, read_api_key, validated_market_context
from .memory import Memory, canonical
from .models import AgentError, AmbiguousOperation, NotSent

MAX_BODY = 65536
MAX_RESPONSE = 16384
PHASES = frozenset({'SCREENING', 'ANALYSIS', 'PENDING_REVIEW'})
ACTIONS = frozenset({'SCREENING', 'CLARIFY', 'HOLD', 'TAHAN', 'BATALKAN', 'COMPLETE'})
SHA = re.compile(r'[a-f0-9]{64}', re.ASCII)
IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', re.ASCII)
REQUEST_FIELDS = frozenset({'schema_version', 'request_id', 'conversation_id', 'phase',
    'reply', 'reply_sha256', 'market_context', 'gate', 'deadline_epoch'})
RESULT_FIELDS = frozenset({'schema_version', 'ok', 'request_id', 'conversation_id', 'phase',
    'reply_sha256', 'authority', 'trading_authority', 'decision', 'needs_followup', 'reason'})
PUBLIC_ERRORS = frozenset({'DIALOGUE_REQUEST_INVALID', 'DIALOGUE_BODY_LIMIT', 'DIALOGUE_JSON_INVALID',
    'DIALOGUE_ROBOT_OFF', 'DIALOGUE_DISPATCH_UNVERIFIED', 'DIALOGUE_DEADLINE_EXPIRED',
    'DIALOGUE_DEADLINE_INVALID', 'DIALOGUE_MARKET_CONTEXT_INVALID', 'DIALOGUE_MODEL_KEY_MISSING',
    'DIALOGUE_MODEL_KEY_UNAVAILABLE', 'DIALOGUE_AUTHENTICATION_FAILED', 'DIALOGUE_PEER_REFUSED',
    'DIALOGUE_HTTP_INVALID', 'DIALOGUE_HTTP_TIMEOUT', 'DIALOGUE_OPERATION_UNKNOWN',
    'DIALOGUE_OPERATION_ID_MISMATCH', 'DIALOGUE_MODEL_UNAVAILABLE', 'DIALOGUE_MODEL_NOT_SENT',
    'DIALOGUE_MODEL_RESULT_INVALID', 'DIALOGUE_FOREIGN_SYMBOL', 'DIALOGUE_PRICE_PROPOSAL_REFUSED',
    'DIALOGUE_RESPONSE_LIMIT', 'DIALOGUE_UNAVAILABLE'})


class DialogueError(AgentError):
    pass


def need(value, code='DIALOGUE_REQUEST_INVALID'):
    if not value:
        raise DialogueError(code)


def pairs(items):
    value = {}
    for key, child in items:
        need(key not in value, 'DIALOGUE_JSON_INVALID')
        value[key] = child
    return value


def decode_json(raw):
    need(type(raw) is bytes and 0 < len(raw) <= MAX_BODY, 'DIALOGUE_BODY_LIMIT')
    try:
        return json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(DialogueError('DIALOGUE_JSON_INVALID')))
    except (ValueError, UnicodeError, RecursionError):
        raise DialogueError('DIALOGUE_JSON_INVALID') from None


def text_safe(value, maximum, *, empty=False):
    return (type(value) is str and (empty or bool(value)) and len(value) <= maximum
        and not any(ord(char) < 32 and char not in '\n\t' or 127 <= ord(char) < 160 for char in value))


def validate_request(value, *, now=None):
    now = time.time() if now is None else now
    need(type(value) is dict and set(value) == REQUEST_FIELDS
         and type(value['schema_version']) is int and value['schema_version'] == 1
         and type(value['request_id']) is str and SHA.fullmatch(value['request_id'])
         and type(value['conversation_id']) is str and IDENTITY.fullmatch(value['conversation_id'])
         and type(value['phase']) is str and value['phase'] in PHASES and text_safe(value['reply'], 16000)
         and type(value['reply_sha256']) is str and SHA.fullmatch(value['reply_sha256'])
         and hashlib.sha256(value['reply'].encode()).hexdigest() == value['reply_sha256'])
    gate = value['gate']
    need(type(gate) is dict and set(gate) == {'robot_on', 'native_dispatch_verified'})
    need(gate['robot_on'] is True, 'DIALOGUE_ROBOT_OFF')
    need(gate['native_dispatch_verified'] is True, 'DIALOGUE_DISPATCH_UNVERIFIED')
    deadline = value['deadline_epoch']
    need(type(deadline) in (int, float) and math.isfinite(deadline)
         and deadline <= now + 90, 'DIALOGUE_DEADLINE_INVALID')
    need(deadline > now, 'DIALOGUE_DEADLINE_EXPIRED')
    if value['phase'] == 'SCREENING':
        need(value['market_context'] is None, 'DIALOGUE_MARKET_CONTEXT_INVALID')
    else:
        try:
            validated_market_context(value['market_context'])
        except (AgentError, ValueError, TypeError, OverflowError):
            raise DialogueError('DIALOGUE_MARKET_CONTEXT_INVALID') from None
    try:
        need(len(canonical(value).encode()) <= MAX_BODY, 'DIALOGUE_BODY_LIMIT')
    except (ValueError, UnicodeError, RecursionError):
        raise DialogueError('DIALOGUE_JSON_INVALID') from None
    return value


def validate_decision(value, request):
    need(type(value) is dict and set(value) == {'action', 'symbols', 'question'},
         'DIALOGUE_MODEL_RESULT_INVALID')
    action, symbols, question = value['action'], value['symbols'], value['question']
    need(type(action) is str and action in ACTIONS and type(symbols) is list and len(symbols) <= 2
         and all(type(symbol) is str and re.fullmatch(r'[A-Z0-9]{2,20}USDT', symbol, re.ASCII)
                 for symbol in symbols) and len(symbols) == len(set(symbols))
         and text_safe(question, 400, empty=True), 'DIALOGUE_MODEL_RESULT_INVALID')
    question_symbols = re.findall(r'(?<![A-Z0-9])([A-Z0-9]{2,20}USDT)(?![A-Z0-9])', question.upper())
    for symbol in [*symbols, *question_symbols]:
        base = re.escape(symbol[:-4])
        need(re.search(r'(?<![A-Z0-9])'+base+r'(?:[/ -]?USDT)(?![A-Z0-9])',
                       request['reply'].upper()) is not None, 'DIALOGUE_FOREIGN_SYMBOL')
    if action == 'CLARIFY':
        need(bool(question) and '\n' not in question and '\t' not in question,
             'DIALOGUE_MODEL_RESULT_INVALID')
        # Questions may request Neurobro's missing levels but may not propose a
        # number, order, or numeric level. Chart/timeframe data is already supplied.
        need(not any(char.isdigit() for char in question)
             and not re.search(r'\b(?:ENTRY|TP|SL|PRICE|HARGA|TARGET|BUY|SELL|LONG|SHORT)\s*[:=]', question, re.I),
             'DIALOGUE_PRICE_PROPOSAL_REFUSED')
    else:
        need(question == '', 'DIALOGUE_MODEL_RESULT_INVALID')
    phase = request['phase']
    need(action != 'SCREENING' or phase == 'SCREENING' and bool(symbols), 'DIALOGUE_MODEL_RESULT_INVALID')
    need(action not in {'TAHAN', 'BATALKAN'} or phase == 'PENDING_REVIEW', 'DIALOGUE_MODEL_RESULT_INVALID')
    if action in {'HOLD', 'TAHAN', 'BATALKAN'}:
        need(re.search(r'\b'+action+r'\b', request['reply'], re.I) is not None,
             'DIALOGUE_MODEL_RESULT_INVALID')
    need(action != 'COMPLETE' or phase != 'PENDING_REVIEW', 'DIALOGUE_MODEL_RESULT_INVALID')
    return value


def validate_result(value, request):
    need(type(value) is dict and set(value) == RESULT_FIELDS
         and type(value['schema_version']) is int and value['schema_version'] == 1
         and value['ok'] is True and value['authority'] == 'DIALOGUE_CONTROL_ONLY'
         and value['trading_authority'] is False
         and all(value[key] == request[key] for key in ('request_id','conversation_id','phase','reply_sha256'))
         and type(value['needs_followup']) is bool
         and value['reason'] in {'DIALOGUE_DECISION', 'CLARIFICATION_ALREADY_USED'},
         'DIALOGUE_MODEL_RESULT_INVALID')
    decision = value['decision']
    if value['reason'] == 'CLARIFICATION_ALREADY_USED':
        need(decision == {'action':'CLARIFY','symbols':[],'question':''}
             and value['needs_followup'] is False, 'DIALOGUE_MODEL_RESULT_INVALID')
    else:
        validate_decision(decision, request)
        need(value['needs_followup'] is (decision['action'] == 'CLARIFY'), 'DIALOGUE_MODEL_RESULT_INVALID')
    need(len(canonical(value).encode()) <= MAX_RESPONSE, 'DIALOGUE_RESPONSE_LIMIT')
    return value


def private_secret(path, *, uid=None):
    uid = os.geteuid() if uid is None else uid
    path = Path(path)
    need(path.is_absolute(), 'DIALOGUE_AUTHENTICATION_FAILED')
    for parent in path.parents:
        need(stat.S_ISDIR(parent.lstat().st_mode), 'DIALOGUE_AUTHENTICATION_FAILED')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_uid == uid and before.st_nlink == 1
             and stat.S_IMODE(before.st_mode) in (0o400, 0o600) and 32 <= before.st_size <= 257,
             'DIALOGUE_AUTHENTICATION_FAILED')
        raw = os.read(fd, 258)
        identity = lambda item:(item.st_dev,item.st_ino,item.st_mode,item.st_uid,item.st_gid,
            item.st_nlink,item.st_size,item.st_mtime_ns,item.st_ctime_ns)
        need(identity(before) == identity(os.fstat(fd)) == identity(path.lstat())
             and len(raw) == before.st_size, 'DIALOGUE_AUTHENTICATION_FAILED')
    finally:
        os.close(fd)
    need(re.fullmatch(rb'[A-Za-z0-9_-]{32,256}\n?', raw) is not None, 'DIALOGUE_AUTHENTICATION_FAILED')
    return raw.rstrip(b'\n').decode('ascii')


def key_state(path):
    try:
        read_api_key(path)
        return 'AVAILABLE'
    except FileNotFoundError:
        return 'MISSING'
    except (OSError, AgentError, UnicodeError, ValueError):
        return 'INVALID'


def key_identity(path):
    """Private key generation binding; neither metadata nor digest is exported."""
    try:
        before = Path(path).lstat()
        secret = read_api_key(path)
        after = Path(path).lstat()
        identity = lambda item:(item.st_dev,item.st_ino,item.st_mode,item.st_uid,
            item.st_gid,item.st_nlink,item.st_size,item.st_mtime_ns,item.st_ctime_ns)
        if identity(before) != identity(after):
            return None
        return identity(after), hashlib.sha256(secret.encode()).digest()
    except (OSError,AgentError,ValueError,UnicodeError):
        return None


def runtime_config_sha256(path, references):
    """Observe exact approved app config bytes; never hash an environment claim."""
    path = Path(path)
    need(path.is_absolute(),'DIALOGUE_AUTHENTICATION_FAILED')
    for parent in path.parents:
        need(stat.S_ISDIR(parent.lstat().st_mode),'DIALOGUE_AUTHENTICATION_FAILED')
    fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        mode=stat.S_IMODE(before.st_mode)
        need(stat.S_ISREG(before.st_mode) and before.st_uid in (0,os.geteuid())
             and before.st_nlink==1 and mode in (0o400,0o600,0o440)
             and (mode!=0o440 or before.st_gid==os.getegid()) and 0<before.st_size<=8192,
             'DIALOGUE_AUTHENTICATION_FAILED')
        raw=os.read(fd,8193)
        identity=lambda item:(item.st_dev,item.st_ino,item.st_mode,item.st_uid,item.st_gid,
            item.st_nlink,item.st_size,item.st_mtime_ns,item.st_ctime_ns)
        need(len(raw)==before.st_size and identity(before)==identity(os.fstat(fd))
             ==identity(path.lstat()),'DIALOGUE_AUTHENTICATION_FAILED')
    finally:
        os.close(fd)
    value=decode_json(raw)
    need(type(references) is dict and set(references)=={'socket','data_directory','token_file','key_file'},
         'DIALOGUE_AUTHENTICATION_FAILED')
    expected={'schema_version':2,'mode':'NATIVE_DELEGATED_DIALOGUE','model':'gpt-5-mini',
        'socket_path':references['socket'],'data_directory':references['data_directory'],
        'dialogue_token_file':references['token_file'],'openai_key_file':references['key_file']}
    need(all(type(value) is str and Path(value).is_absolute() and '..' not in Path(value).parts
                 for value in references.values()) and type(value) is dict and value==expected
         and type(value.get('schema_version')) is int,'DIALOGUE_AUTHENTICATION_FAILED')
    return hashlib.sha256(raw).hexdigest()


class DialogueEngine:
    """Persist only this service's own conversations and model delivery receipts."""
    def __init__(self, memory, key_file, *, model=None, clock=time.time):
        self.memory, self.key_file, self.clock = memory, str(key_file), clock
        self.model = model or OpenAIDialogue(self.key_file, memory, timeout=45,
            daily_requests=math.inf, daily_input_tokens=math.inf, daily_output_tokens=math.inf)
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self._authenticated_key_identity = None
        self.configuration_check = lambda:True

    def configuration_ready(self):
        try:return self.configuration_check() is True
        except Exception:return False

    def shutdown(self):
        self.stopping.set()

    def authentication_verified(self):
        evidence = self.model.evidence()
        observed = evidence.get('observed_at')
        return (evidence.get('verified') is True and type(observed) in (int,float)
            and math.isfinite(observed) and 0 <= self.clock()-observed <= 300
            and self._authenticated_key_identity is not None
            and key_identity(self.key_file) == self._authenticated_key_identity)

    def process(self, request):
        # This precedes Memory.operation: OFF/expired gates reject even DONE replays.
        request = validate_request(decode_json(canonical(request).encode()), now=self.clock())
        with self.lock:
            need(not self.stopping.is_set(),'DIALOGUE_UNAVAILABLE')
            need(self.configuration_ready(),'DIALOGUE_UNAVAILABLE')
            validate_request(request, now=self.clock())
            state = key_state(self.key_file)
            need(state == 'AVAILABLE', 'DIALOGUE_MODEL_KEY_MISSING' if state == 'MISSING'
                 else 'DIALOGUE_MODEL_KEY_UNAVAILABLE')
            operation_id = 'delegated-model:' + request['request_id']
            def execute():
                validate_request(request, now=self.clock())
                remaining = min(45.0, request['deadline_epoch'] - self.clock())
                need(remaining > 0, 'DIALOGUE_DEADLINE_EXPIRED')
                until = time.monotonic() + remaining
                self.model.set_remaining_budget(lambda: max(0.0,
                    min(until-time.monotonic(), request['deadline_epoch']-self.clock())))
                conversation = request['conversation_id']
                history = self.memory.history(conversation, max_chars=8000, max_messages=12)
                day = datetime.fromtimestamp(self.clock(), ZoneInfo('Asia/Jakarta')).date().isoformat()
                # Last possible gate check immediately before provider dispatch.
                validate_request(request, now=self.clock())
                original_transport = self.model.transport
                dispatched_key = [None]
                def gated_transport(payload):
                    validate_request(request, now=self.clock())
                    need(not self.stopping.is_set(),'DIALOGUE_UNAVAILABLE')
                    need(self.configuration_ready(),'DIALOGUE_UNAVAILABLE')
                    need(time.monotonic() < until, 'DIALOGUE_DEADLINE_EXPIRED')
                    dispatched_key[0] = key_identity(self.key_file)
                    return original_transport(payload)
                self.model.transport = gated_transport
                try:
                    decision = self.model.plan(history, request['reply'], phase=request['phase'],
                        jakarta_day=day, context=request['market_context'])
                finally:
                    self.model.transport = original_transport
                need(not self.stopping.is_set(),'DIALOGUE_UNAVAILABLE')
                need(self.clock() < request['deadline_epoch'] and time.monotonic() < until,
                     'DIALOGUE_DEADLINE_EXPIRED')
                if (dispatched_key[0] is not None and key_identity(self.key_file) == dispatched_key[0]
                        and self.model.evidence().get('verified') is True):
                    self._authenticated_key_identity = dispatched_key[0]
                value = validate_decision({'action':decision.action,
                    'symbols':list(decision.symbols),'question':decision.question}, request)
                phase_key = 'dialogue-clarification:' + hashlib.sha256(
                    (conversation+'\0'+request['phase']).encode()).hexdigest()
                reason = 'DIALOGUE_DECISION'
                if value['action'] == 'CLARIFY':
                    used = self.memory.get(phase_key, False)
                    need(type(used) is bool, 'DIALOGUE_MODEL_RESULT_INVALID')
                    if used:
                        value = {'action':'CLARIFY','symbols':[],'question':''}
                        reason = 'CLARIFICATION_ALREADY_USED'
                    else:
                        self.memory.put(phase_key, True)
                result = {'schema_version':1, 'ok':True,
                    **{key:request[key] for key in ('request_id','conversation_id','phase','reply_sha256')},
                    'authority':'DIALOGUE_CONTROL_ONLY','trading_authority':False,
                    'decision':value,'needs_followup':value['action']=='CLARIFY' and bool(value['question']),
                    'reason':reason}
                validate_result(result, request)
                self.memory.append(conversation, 'user', request['reply'], self.clock())
                self.memory.append(conversation, 'assistant', canonical(value), self.clock())
                return result
            try:
                result = self.memory.operation(operation_id, 'DELEGATED_MODEL_DIALOGUE', request, execute)
            except AmbiguousOperation:
                raise DialogueError('DIALOGUE_OPERATION_UNKNOWN') from None
            except NotSent:
                raise DialogueError('DIALOGUE_MODEL_NOT_SENT') from None
            except DialogueError:
                raise
            except ValueError:
                raise DialogueError('DIALOGUE_OPERATION_ID_MISMATCH') from None
            except Exception:
                raise DialogueError('DIALOGUE_MODEL_UNAVAILABLE') from None
            validate_request(request, now=self.clock())
            return validate_result(result, request)


def peer_uid(connection):
    return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                   struct.calcsize('3i')))[1]


def safe_error(error):
    return str(error) if isinstance(error, DialogueError) and str(error) in PUBLIC_ERRORS else 'DIALOGUE_UNAVAILABLE'


def receive(connection, length, deadline):
    remaining = deadline - time.monotonic()
    need(remaining > 0, 'DIALOGUE_HTTP_TIMEOUT')
    connection.settimeout(remaining)
    try:
        raw = connection.recv(length)
    except (socket.timeout, TimeoutError):
        raise DialogueError('DIALOGUE_HTTP_TIMEOUT') from None
    need(bool(raw), 'DIALOGUE_HTTP_INVALID')
    return raw


def http_request(connection, token, *, deadline):
    raw = b''
    while b'\r\n\r\n' not in raw:
        raw += receive(connection, min(8193-len(raw),8192), deadline)
        need(len(raw) <= 8192 or b'\r\n\r\n' in raw, 'DIALOGUE_HTTP_INVALID')
    header, body = raw.split(b'\r\n\r\n',1)
    need(len(header) <= 8192, 'DIALOGUE_HTTP_INVALID')
    try:
        rows = header.decode('ascii').split('\r\n')
    except UnicodeError:
        raise DialogueError('DIALOGUE_HTTP_INVALID') from None
    need(rows[0] in {'POST /v1/dialogue HTTP/1.1','POST /v1/dialogue HTTP/1.0'}
         and 2 <= len(rows) <= 33, 'DIALOGUE_HTTP_INVALID')
    headers = {}
    for row in rows[1:]:
        name, sep, value = row.partition(':')
        need(sep and re.fullmatch(r'[A-Za-z-]{1,40}',name) and name.lower() not in headers
             and not any(ord(char)<32 or ord(char)==127 for char in value), 'DIALOGUE_HTTP_INVALID')
        headers[name.lower()] = value.strip()
    need('transfer-encoding' not in headers and headers.get('content-type') == 'application/json'
         and re.fullmatch(r'[0-9]{1,6}',headers.get('content-length','')), 'DIALOGUE_HTTP_INVALID')
    need(hmac.compare_digest(headers.get('authorization',''), 'Bearer '+token),
         'DIALOGUE_AUTHENTICATION_FAILED')
    size = int(headers['content-length'])
    need(0 < size <= MAX_BODY, 'DIALOGUE_BODY_LIMIT')
    need(len(body) <= size, 'DIALOGUE_HTTP_INVALID')
    while len(body) < size:
        body += receive(connection,min(8192,size-len(body)),deadline)
    return decode_json(body)


class DialogueServer:
    def __init__(self, socket_path, engine, token_file, *, server_uid=10002, caller_uid=10001):
        self.path, self.engine, self.token_file = Path(socket_path), engine, str(token_file)
        need(os.geteuid() == server_uid and self.path.is_absolute(), 'DIALOGUE_AUTHENTICATION_FAILED')
        self.caller_uid = caller_uid
        self.server_uid = server_uid
        self.closing = False
        private_secret(self.token_file, uid=server_uid)
        for parent in self.path.parents:
            need(stat.S_ISDIR(parent.lstat().st_mode), 'DIALOGUE_AUTHENTICATION_FAILED')
        info = self.path.parent.lstat()
        mode = stat.S_IMODE(info.st_mode)
        need(info.st_uid == server_uid and mode in (0o700,0o750)
             and (mode != 0o750 or info.st_gid == caller_uid), 'DIALOGUE_AUTHENTICATION_FAILED')
        lock = self.path.parent / 'dialogue.lock'
        self.lock_path = lock
        self.lock_fd = os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
        try:
            info = os.fstat(self.lock_fd)
            need(stat.S_ISREG(info.st_mode) and info.st_uid == server_uid and info.st_nlink == 1
                 and stat.S_IMODE(info.st_mode) == 0o600, 'DIALOGUE_AUTHENTICATION_FAILED')
            fcntl.flock(self.lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if self.path.exists() or self.path.is_symlink():
                before = self.path.lstat()
                need(stat.S_ISSOCK(before.st_mode) and before.st_uid == server_uid
                     and stat.S_IMODE(before.st_mode) == 0o660, 'DIALOGUE_AUTHENTICATION_FAILED')
                # Reuse only our dead socket after acquiring our exclusive lock.
                # A live listener, unknown inode, symlink or other object is kept.
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.5)
                    try:
                        probe.connect(str(self.path))
                    except ConnectionRefusedError:
                        after = self.path.lstat()
                        need((before.st_dev,before.st_ino) == (after.st_dev,after.st_ino),
                             'DIALOGUE_AUTHENTICATION_FAILED')
                        self.path.unlink()
                    else:
                        raise DialogueError('DIALOGUE_AUTHENTICATION_FAILED')
        except BaseException:
            os.close(self.lock_fd)
            raise
        parent = self
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    need(peer_uid(self.request) == parent.caller_uid, 'DIALOGUE_PEER_REFUSED')
                    token = private_secret(parent.token_file, uid=server_uid)
                    request = http_request(self.request,token,deadline=time.monotonic()+10)
                    value = parent.engine.process(request)
                    status = 200
                except Exception as error:
                    value = {'schema_version':1,'ok':False,'error':safe_error(error)}
                    status = 403 if value['error'] in {'DIALOGUE_PEER_REFUSED','DIALOGUE_AUTHENTICATION_FAILED'} else 400
                raw = canonical(value).encode()
                if len(raw) > MAX_RESPONSE:
                    status, raw = 503, b'{"schema_version":1,"ok":false,"error":"DIALOGUE_RESPONSE_LIMIT"}'
                wire = ('HTTP/1.1 '+str(status)+' '+('OK' if status==200 else 'Rejected')+'\r\n'
                    'Content-Type: application/json\r\nContent-Length: '+str(len(raw))+
                    '\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n').encode()+raw
                try:
                    self.request.settimeout(2)
                    self.request.sendall(wire)
                except OSError:
                    pass
        self.server = socketserver.UnixStreamServer(str(self.path),Handler,bind_and_activate=False)
        self.server.request_queue_size = 8
        try:
            self.server.server_bind()
            os.chmod(self.path,0o660)
            if os.getegid() != caller_uid:
                os.chown(self.path,-1,caller_uid)
            self.socket_identity = (self.path.lstat().st_dev,self.path.lstat().st_ino)
            self.server.server_activate()
        except BaseException:
            self.server.server_close()
            os.close(self.lock_fd)
            raise
        self.thread = threading.Thread(target=self.server.serve_forever,
            kwargs={'poll_interval':0.2},name='delegated-dialogue',daemon=True)

    def start(self):
        self.thread.start()
        return self

    def runtime_ready(self):
        try:
            if self.closing or not self.thread.is_alive() or self.engine.stopping.is_set():
                return False
            private_secret(self.token_file,uid=self.server_uid)
            info = self.path.lstat()
            lock = os.fstat(self.lock_fd)
            named = self.lock_path.lstat()
            return (stat.S_ISSOCK(info.st_mode) and info.st_uid == self.server_uid
                and info.st_gid == self.caller_uid and stat.S_IMODE(info.st_mode) == 0o660
                and (info.st_dev,info.st_ino) == self.socket_identity
                and stat.S_ISREG(lock.st_mode) and lock.st_uid == self.server_uid
                and lock.st_nlink == 1 and stat.S_IMODE(lock.st_mode) == 0o600
                and (lock.st_dev,lock.st_ino) == (named.st_dev,named.st_ino))
        except (OSError,AgentError):
            return False

    def _cleanup(self):
        self.server.server_close()
        try:
            info = self.path.lstat()
            if stat.S_ISSOCK(info.st_mode) and (info.st_dev,info.st_ino) == self.socket_identity:
                self.path.unlink()
        except FileNotFoundError:
            pass
        os.close(self.lock_fd)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.engine.shutdown()
        if self.thread.is_alive():
            # urllib's DNS/open phase can outlive an inactivity timeout. Bound
            # shutdown itself and exit the CLI process rather than wait forever.
            # A durable STARTED receipt is kept when an in-flight daemon thread
            # ends with the process; it cannot be automatically replayed.
            stopped = threading.Thread(target=self.server.shutdown,daemon=True)
            stopped.start()
            stopped.join(timeout=2)
            self.thread.join(timeout=0.1)
        if self.thread.is_alive():
            # Keep our singleton lock until that handler actually ends. In the
            # CLI all remaining threads are daemon threads and exit with main.
            def later():
                self.thread.join()
                self._cleanup()
            threading.Thread(target=later,daemon=True).start()
        else:
            self._cleanup()


class DialogueHealth:
    def __init__(self, dialogue, *, host='127.0.0.1', port=8765, config_sha256='UNVERIFIED'):
        self.dialogue, self.generation = dialogue, uuid.uuid4().hex
        self.config_sha256 = config_sha256
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in {'/health','/health/live','/health/ready'}:
                    self.send_error(404)
                    return
                value = parent.observe()
                raw = canonical(value).encode()
                self.send_response(200 if self.path != '/health/ready' or value['dialogue_ready'] else 503)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self,*_):
                pass
        self.server = ThreadingHTTPServer((host,port),Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,name='dialogue-health',daemon=True)

    def observe(self):
        state = key_state(self.dialogue.engine.key_file)
        transport = self.dialogue.runtime_ready()
        configured = self.dialogue.engine.configuration_ready()
        ready = state == 'AVAILABLE' and transport and configured
        source = os.environ.get('HARUN_SOURCE_COMMIT','')
        image = os.environ.get('HARUN_IMAGE_DIGEST','')
        source = source if re.fullmatch(r'[a-f0-9]{40}',source,re.ASCII) else 'UNVERIFIED'
        image = image if re.fullmatch(r'ghcr\.io/harun5231/harun-neurobro-agent@sha256:[a-f0-9]{64}',image,re.ASCII) else 'UNVERIFIED'
        return {'service':'harun-neurobro-agent','runtime_implemented':True,
            'mode':'NATIVE_DELEGATED_DIALOGUE','model':'gpt-5-mini','model_key_state':state,
            'state':'PAUSED' if ready else 'BLOCKED', 'dialogue_ready':ready,
            'reason':('RUNTIME_CONFIG_UNVERIFIED' if not configured else 'NATIVE_CALLER_GATED' if ready else 'MODEL_KEY_'+state
                      if state != 'AVAILABLE' else 'DIALOGUE_TRANSPORT_UNAVAILABLE'),
            'uid':os.geteuid(),'process_generation':self.generation,'observed_at':time.time(),
            'source_commit':source,'image_digest':image,
            'runtime_config_sha256':getattr(self,'config_sha256','UNVERIFIED') if configured else 'UNVERIFIED',
            'integration_verified':False,'native_coordinator_preserved':True,
            'model_authentication_verified':self.dialogue.engine.authentication_verified(),
            'dialogue_transport_verified':transport,
            'health_observation_only':True,'provider_contact_performed':False,'trading_authority':False}

    def start(self):
        self.thread.start()
        return self

    def close(self):
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=3)
        self.server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('socket','data-directory','token-file','key-file'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--runtime-config')
    args = parser.parse_args(argv)
    # Optional Office telemetry uses this exact running module/class namespace.
    # It adds authenticated UDS GET status; the POST/model path is unchanged.
    from .native_status_bridge import install_dialogue_status
    install_dialogue_status(globals())
    memory = dialogue = health = None
    stop = threading.Event()
    try:
        need(os.geteuid() == 10002, 'DIALOGUE_AUTHENTICATION_FAILED')
        references={key:getattr(args,key) for key in ('socket','data_directory','token_file','key_file')}
        config_sha=(runtime_config_sha256(args.runtime_config,references)
                    if args.runtime_config else 'UNVERIFIED')
        memory = Memory(Path(args.data_directory)/'agent.sqlite3')
        engine = DialogueEngine(memory,args.key_file)
        if args.runtime_config:
            engine.configuration_check=lambda:runtime_config_sha256(args.runtime_config,references)==config_sha
        dialogue = DialogueServer(args.socket,engine,args.token_file).start()
        health = DialogueHealth(dialogue,config_sha256=config_sha).start()
        for number in (signal.SIGTERM,signal.SIGINT):
            signal.signal(number,lambda *_:stop.set())
        stop.wait()
        return 0
    except Exception:
        print('{"event":"DIALOGUE_STARTUP_REFUSED","reason":"CONFIGURATION_UNVERIFIED"}',flush=True)
        return 1
    finally:
        if health is not None:health.close()
        if dialogue is not None:dialogue.close()
        if memory is not None:memory.close()


if __name__ == '__main__':
    raise SystemExit(main())
