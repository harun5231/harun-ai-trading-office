"""Bounded UDS-only reasoning client; callers retain all dispatch authority."""
import json
import os
from pathlib import Path
import re
import socket
import stat
import time

from .dialogue_service import (DialogueError, MAX_BODY, MAX_RESPONSE, PUBLIC_ERRORS,
    canonical, decode_json, need, peer_uid, private_secret, receive,
    validate_request, validate_result)


class DialogueClient:
    def __init__(self, socket_path, token_file, *, caller_uid=10001, server_uid=10002):
        self.path,self.token_file = Path(socket_path),str(token_file)
        self.caller_uid,self.server_uid = caller_uid,server_uid

    def request(self, value):
        try:
            return self._request(value)
        except DialogueError:
            raise
        except (OSError,ValueError,UnicodeError,TypeError,RecursionError):
            raise DialogueError('DIALOGUE_UNAVAILABLE') from None

    def compose(self, value):
        """Same authenticated transport; drafting does not dispatch browser UI."""
        need(type(value) is dict and value.get('phase', '').startswith('COMPOSE_'),
             'DIALOGUE_REQUEST_INVALID')
        result = self.request(value)
        need(result['decision']['action'] == 'PROMPT' and result['needs_followup'] is False,
             'DIALOGUE_MODEL_RESULT_INVALID')
        return result

    def _request(self, value):
        value = validate_request(decode_json(canonical(value).encode()))
        need(os.geteuid() == self.caller_uid, 'DIALOGUE_AUTHENTICATION_FAILED')
        path = self.path
        need(path.is_absolute(), 'DIALOGUE_AUTHENTICATION_FAILED')
        for parent in path.parents:
            need(stat.S_ISDIR(parent.lstat().st_mode), 'DIALOGUE_AUTHENTICATION_FAILED')
        info = path.lstat()
        need(stat.S_ISSOCK(info.st_mode) and info.st_uid == self.server_uid
             and stat.S_IMODE(info.st_mode) == 0o660, 'DIALOGUE_AUTHENTICATION_FAILED')
        token = private_secret(self.token_file,uid=self.caller_uid)
        raw = canonical(value).encode()
        need(len(raw) <= MAX_BODY, 'DIALOGUE_BODY_LIMIT')
        deadline = time.monotonic()+min(50.0,value['deadline_epoch']-time.time())
        wire = ('POST /v1/dialogue HTTP/1.1\r\nHost: localhost\r\n'
            'Content-Type: application/json\r\nAuthorization: Bearer '+token+
            '\r\nContent-Length: '+str(len(raw))+'\r\nConnection: close\r\n\r\n').encode()+raw
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                connection.settimeout(max(0.001,deadline-time.monotonic()))
                connection.connect(str(path))
                need(peer_uid(connection) == self.server_uid, 'DIALOGUE_PEER_REFUSED')
                validate_request(value)
                connection.sendall(wire)
                result = b''
                while b'\r\n\r\n' not in result:
                    result += receive(connection,min(4096,8193-len(result)),deadline)
                    need(len(result)<=8192 or b'\r\n\r\n' in result,'DIALOGUE_HTTP_INVALID')
                headers,body = result.split(b'\r\n\r\n',1)
                try:rows=headers.decode('ascii').split('\r\n')
                except UnicodeError:raise DialogueError('DIALOGUE_HTTP_INVALID') from None
                need(re.fullmatch(r'HTTP/1\.[01] (200|400|403|503) [A-Za-z ]{1,32}',rows[0])
                     and len(rows)<=33,'DIALOGUE_HTTP_INVALID')
                status=int(rows[0].split()[1])
                parsed={}
                for row in rows[1:]:
                    name,sep,item=row.partition(':')
                    need(sep and re.fullmatch(r'[A-Za-z-]{1,40}',name)
                         and name.lower() not in parsed,'DIALOGUE_HTTP_INVALID')
                    parsed[name.lower()]=item.strip()
                need(parsed.get('content-type')=='application/json' and 'transfer-encoding' not in parsed
                     and re.fullmatch(r'[0-9]{1,6}',parsed.get('content-length','')),'DIALOGUE_HTTP_INVALID')
                size=int(parsed['content-length'])
                need(0<size<=MAX_RESPONSE and len(body)<=size,'DIALOGUE_RESPONSE_LIMIT')
                while len(body)<size:
                    body+=receive(connection,min(4096,size-len(body)),deadline)
                response=decode_json(body)
                if status != 200:
                    need(type(response) is dict and set(response)=={'schema_version','ok','error'}
                         and type(response['schema_version']) is int and response['schema_version']==1
                         and response['ok'] is False and response['error'] in PUBLIC_ERRORS,
                         'DIALOGUE_HTTP_INVALID')
                    raise DialogueError(response['error'])
                validate_request(value)
                return validate_result(response,value)
        except DialogueError:
            raise
        except (OSError,ValueError,UnicodeError,TypeError):
            raise DialogueError('DIALOGUE_UNAVAILABLE') from None
