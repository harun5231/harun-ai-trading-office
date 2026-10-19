"""Optional authenticated status telemetry for the existing native Office flow.

No model, browser input, exchange, scheduler or financial operation is invoked.
The service hook adds one read-only route to its existing authenticated UDS;
the browser hook preserves the native status and appends closed agent telemetry.
"""
from datetime import datetime, timezone
import hmac
import importlib
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import struct
import threading
import time


SERVICE = 'harun-neurobro-agent'
MODE = 'NATIVE_DELEGATED_DIALOGUE'
SOCKET = '/run/harun-dialogue/dialogue.sock'
TOKEN = '/run/secrets/dialogue_token'
RELEASE_BINDING = '/app-config/dialogue-status.json'
IMAGE = re.compile(r'ghcr\.io/harun5231/harun-neurobro-agent@sha256:[a-f0-9]{64}', re.ASCII)
SHA = re.compile(r'[a-f0-9]{64}', re.ASCII)
COMMIT = re.compile(r'[a-f0-9]{40}', re.ASCII)
REASONS = frozenset({'NATIVE_CALLER_GATED','RUNTIME_CONFIG_UNVERIFIED',
    'MODEL_KEY_MISSING','MODEL_KEY_INVALID','DIALOGUE_TRANSPORT_UNAVAILABLE'})
HEALTH_FIELDS = frozenset({'service','runtime_implemented','mode','model','model_key_state',
    'state','dialogue_ready','reason','uid','process_generation','observed_at','source_commit',
    'image_digest','runtime_config_sha256','integration_verified','native_coordinator_preserved',
    'model_authentication_verified','dialogue_transport_verified','health_observation_only',
    'provider_contact_performed','trading_authority'})


class StatusUnavailable(ValueError):
    pass


def _need(value):
    if not value:
        raise StatusUnavailable('NATIVE_AGENT_STATUS_UNAVAILABLE')


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def _decode(raw):
    _need(type(raw) is bytes and 0<len(raw)<=16384)
    def pairs(items):
        result={}
        for key,value in items:
            _need(key not in result)
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:_need(False))


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


def _identity(info):
    return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,
            info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def _stamp(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat().replace('+00:00','Z')


def _release(value):
    _need(type(value) is dict and set(value)=={'source_commit','image','runtime_config_sha256'})
    for field,pattern,zero in (('source_commit',COMMIT,'0'*40),
                               ('runtime_config_sha256',SHA,'0'*64)):
        _need(type(value[field]) is str and pattern.fullmatch(value[field]) and value[field]!=zero)
    _need(type(value['image']) is str and IMAGE.fullmatch(value['image'])
          and not value['image'].endswith('0'*64))
    return value


def read_release_binding(path=RELEASE_BINDING):
    """Pins come from a separate root-approved file, never the health response."""
    path=Path(path)
    _need(path.is_absolute())
    for parent in path.parents:
        info=parent.lstat()
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        _need(stat.S_ISREG(before.st_mode) and before.st_uid==0 and before.st_gid==10001
              and before.st_nlink==1 and stat.S_IMODE(before.st_mode)==0o440
              and 0<before.st_size<=4096)
        raw=os.read(fd,4097)
        _need(len(raw)==before.st_size and _identity(before)==_identity(os.fstat(fd))
              ==_identity(path.lstat()))
    finally:
        os.close(fd)
    return _release(_decode(raw))


def unavailable():
    return {'schema_version':1,'service':SERVICE,'source':'LOCAL_AGENT_HEALTH',
        'status':'UNAVAILABLE','checked_at':None,'state':'UNKNOWN','phase':None,
        'reason':'AGENT_STATUS_UNAVAILABLE','integration_verified':False,
        'dependencies':{name:{'verified':False,'checked_at':None} for name in ('backend','browser','model')}}


def _public_health(value):
    """Only the released delegated health schema can cross the UDS route."""
    _need(type(value) is dict and HEALTH_FIELDS<=set(value))
    result={name:value[name] for name in HEALTH_FIELDS}
    _need(result['service']==SERVICE and result['mode']==MODE and result['model']=='gpt-5-mini'
          and type(result['uid']) is int and result['uid']==10002
          and result['state'] in {'PAUSED','BLOCKED'}
          and result['model_key_state'] in {'AVAILABLE','MISSING','INVALID'}
          and result['reason'] in REASONS
          and type(result['process_generation']) is str
          and re.fullmatch(r'[a-f0-9]{32}',result['process_generation'])
          and result['process_generation']!='0'*32 and _number(result['observed_at']))
    for name in ('runtime_implemented','native_coordinator_preserved','health_observation_only'):
        _need(result[name] is True)
    for name in ('integration_verified','provider_contact_performed','trading_authority'):
        _need(result[name] is False)
    for name in ('dialogue_ready','model_authentication_verified','dialogue_transport_verified'):
        _need(type(result[name]) is bool)
    _need(result['dialogue_ready'] is (result['state']=='PAUSED')
          and (not result['dialogue_ready'] or result['model_key_state']=='AVAILABLE'))
    for name,pattern in (('source_commit',COMMIT),('image_digest',IMAGE),('runtime_config_sha256',SHA)):
        item=result[name]
        _need(type(item) is str and (item=='UNVERIFIED' or pattern.fullmatch(item)))
    _need(len(_canonical(result))<=8192)
    return result


def sanitize_delegated_health(health, *, expected_release, browser=None, now=None):
    try:
        current=time.time() if now is None else now
        _need(_number(current) and 0<current<253402300799)
        pins=_release(expected_release)
        value=_public_health(health)
        observed=value['observed_at']
        _need(0<=current-observed<60 and value['source_commit']==pins['source_commit']
              and value['image_digest']==pins['image']
              and value['runtime_config_sha256']==pins['runtime_config_sha256'])
        dependencies=unavailable()['dependencies']
        browser=browser if type(browser) is dict else {}
        try:
            at=datetime.fromisoformat(browser.get('checked_at','').replace('Z','+00:00'))
            browser_fresh=(at.tzinfo is not None and 0<=current-at.timestamp()<120)
        except (TypeError,ValueError,AttributeError,OverflowError):
            browser_fresh=False
        if (browser.get('provider')=='NEUROBRO_WEB' and browser.get('status') in {'CONNECTED','BUSY'}
                and browser.get('profile_persistent') is True and browser_fresh):
            dependencies['browser']={'verified':True,'checked_at':browser['checked_at']}
        if value['model_authentication_verified'] is True and value['model_key_state']=='AVAILABLE':
            dependencies['model']={'verified':True,'checked_at':_stamp(observed)}
        return {'schema_version':1,'service':SERVICE,'source':'LOCAL_AGENT_HEALTH',
            'status':'AVAILABLE','checked_at':_stamp(observed),'state':value['state'],
            'phase':None,'reason':value['reason'],'integration_verified':False,
            'dependencies':dependencies}
    except Exception:
        return unavailable()


def _recv(connection,size,deadline):
    remaining=deadline-time.monotonic()
    _need(remaining>0)
    connection.settimeout(remaining)
    raw=connection.recv(size)
    _need(bool(raw))
    return raw


def _headers(connection,prefix,deadline):
    raw=prefix
    while b'\r\n\r\n' not in raw:
        raw+=_recv(connection,min(4096,8193-len(raw)),deadline)
        _need(len(raw)<=8192)
    header,body=raw.split(b'\r\n\r\n',1)
    rows=header.decode('ascii').split('\r\n')
    _need(1<len(rows)<=33)
    result={}
    for row in rows[1:]:
        key,sep,value=row.partition(':')
        _need(sep and re.fullmatch(r'[A-Za-z-]{1,40}',key) and key.lower() not in result
              and not any(ord(char)<32 or ord(char)==127 for char in value))
        result[key.lower()]=value.strip()
    _need('transfer-encoding' not in result)
    return rows[0],result,body


def _peer(connection):
    return struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,
                                                  struct.calcsize('3i')))[1]


def _token(path,uid):
    path=Path(path)
    _need(path.is_absolute())
    for parent in path.parents:
        _need(stat.S_ISDIR(parent.lstat().st_mode))
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        _need(stat.S_ISREG(before.st_mode) and before.st_uid==uid and before.st_nlink==1
              and stat.S_IMODE(before.st_mode) in (0o400,0o600) and 32<=before.st_size<=257)
        raw=os.read(fd,258)
        _need(len(raw)==before.st_size and _identity(before)==_identity(os.fstat(fd))
              ==_identity(path.lstat()) and re.fullmatch(rb'[A-Za-z0-9_-]{32,256}\n?',raw))
        return raw.rstrip(b'\n').decode('ascii')
    finally:
        os.close(fd)


class StatusClient:
    def __init__(self,socket_path=SOCKET,token_file=TOKEN,*,caller_uid=10001,server_uid=10002):
        self.path,self.token_file=Path(socket_path),str(token_file)
        self.caller_uid,self.server_uid=caller_uid,server_uid

    def observe(self, *, expected_release, browser=None, now=None):
        try:
            _release(expected_release)
            _need(os.geteuid()==self.caller_uid and self.path.is_absolute())
            for parent in self.path.parents:
                _need(stat.S_ISDIR(parent.lstat().st_mode))
            info=self.path.lstat()
            _need(stat.S_ISSOCK(info.st_mode) and info.st_uid==self.server_uid
                  and info.st_gid==self.caller_uid and stat.S_IMODE(info.st_mode)==0o660)
            token=_token(self.token_file,self.caller_uid)
            deadline=time.monotonic()+1.0
            wire=('GET /v1/status HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '+token+
                  '\r\nConnection: close\r\n\r\n').encode('ascii')
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                connection.settimeout(1.0)
                connection.connect(str(self.path))
                _need(_peer(connection)==self.server_uid)
                connection.sendall(wire)
                line,headers,body=_headers(connection,b'',deadline)
                _need(line=='HTTP/1.1 200 OK' and headers.get('content-type')=='application/json'
                      and re.fullmatch(r'[0-9]{1,5}',headers.get('content-length','')))
                size=int(headers['content-length'])
                _need(0<size<=8192 and len(body)<=size)
                while len(body)<size:
                    body+=_recv(connection,min(4096,size-len(body)),deadline)
            return sanitize_delegated_health(_decode(body),expected_release=expected_release,
                                              browser=browser,now=now)
        except Exception:
            return unavailable()


class _PrefixedConnection:
    """Return consumed method bytes to the unchanged original POST parser."""
    def __init__(self,connection,prefix):
        self.connection,self.prefix=connection,prefix
    def recv(self,size,*args):
        if self.prefix:
            raw,self.prefix=self.prefix[:size],self.prefix[size:]
            return raw
        return self.connection.recv(size,*args)
    def __getattr__(self,name):
        return getattr(self.connection,name)


def install_dialogue_status(namespace):
    """Add authenticated GET status without changing POST validation or models."""
    if namespace.get('_native_status_protocol_installed') is True:
        return
    server_class,health_class=namespace['DialogueServer'],namespace['DialogueHealth']
    original_server_init,original_health_init=server_class.__init__,health_class.__init__
    def health_init(self,dialogue,*args,**kwargs):
        original_health_init(self,dialogue,*args,**kwargs)
        dialogue._native_status_observer=self.observe
    def server_init(self,*args,**kwargs):
        original_server_init(self,*args,**kwargs)
        parent=self
        handler_class=self.server.RequestHandlerClass
        original_handle=handler_class.handle
        def handle(handler):
            connection=handler.request
            try:
                deadline=time.monotonic()+3.0
                prefix=b''
                while len(prefix)<4:
                    prefix+=_recv(connection,4-len(prefix),deadline)
                if prefix!=b'GET ':
                    handler.request=_PrefixedConnection(connection,prefix)
                    try:
                        return original_handle(handler)
                    finally:
                        handler.request=connection
                _need(_peer(connection)==parent.caller_uid)
                token=_token(parent.token_file,parent.server_uid)
                line,headers,body=_headers(connection,prefix,deadline)
                _need(line in {'GET /v1/status HTTP/1.1','GET /v1/status HTTP/1.0'}
                      and not body and headers.get('content-length','0')=='0'
                      and hmac.compare_digest(headers.get('authorization',''),'Bearer '+token))
                value=_public_health(parent._native_status_observer())
                raw=_canonical(value)
                status='200 OK'
            except Exception:
                raw=b'{"error":"NATIVE_AGENT_STATUS_UNAVAILABLE"}'
                status='503 Rejected'
            wire=('HTTP/1.1 '+status+'\r\nContent-Type: application/json\r\nContent-Length: '+
                  str(len(raw))+'\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n').encode()+raw
            try:
                connection.settimeout(1.0)
                connection.sendall(wire)
            except OSError:
                pass
        handler_class.handle=handle
    server_class.__init__=server_init
    health_class.__init__=health_init
    namespace['_native_status_protocol_installed']=True


def install_actor_status(namespace, *, binding_loader=read_release_binding, client=None):
    """Append optional telemetry after the existing native actor releases lock."""
    if namespace.get('_native_actor_agent_status_installed') is True:
        return
    actor=namespace['BrowserActor']
    original=actor.status
    client=client or StatusClient()
    def status(self):
        value=original(self)
        try:
            pins=binding_loader()
            agent=client.observe(expected_release=pins,browser=value)
        except Exception:
            agent=unavailable()
        return {**value,'agent':agent}
    actor.status=status
    namespace['_native_actor_agent_status_installed']=True


def _office_agent(value, *, now=None):
    """Validate already-sanitized telemetry received through the native helper."""
    try:
        current=time.time() if now is None else now
        _need(_number(current) and type(value) is dict and set(value)==set(unavailable()))
        _need(type(value['schema_version']) is int and value['schema_version']==1
              and value['service']==SERVICE and value['source']=='LOCAL_AGENT_HEALTH'
              and value['status']=='AVAILABLE' and value['state'] in {'PAUSED','BLOCKED'}
              and value['phase'] is None and value['reason'] in REASONS
              and value['integration_verified'] is False)
        checked=datetime.fromisoformat(value['checked_at'].replace('Z','+00:00'))
        _need(checked.tzinfo is not None and 0<=current-checked.timestamp()<60)
        supplied=value['dependencies']
        _need(type(supplied) is dict and set(supplied)=={'backend','browser','model'})
        dependencies={}
        for name,maximum in (('backend',30),('browser',120),('model',300)):
            item=supplied[name]
            _need(type(item) is dict and set(item)=={'verified','checked_at'}
                  and type(item['verified']) is bool)
            if item['verified']:
                at=datetime.fromisoformat(item['checked_at'].replace('Z','+00:00'))
                _need(name!='backend' and at.tzinfo is not None and 0<=current-at.timestamp()<maximum)
            else:
                _need(item['checked_at'] is None)
            dependencies[name]=dict(item)
        return {**value,'dependencies':dependencies}
    except Exception:
        return unavailable()


class AgentStatusCache:
    """Keep only closed optional telemetry, never a native response or token."""
    def __init__(self, *, clock=time.time):
        self.clock,self.lock,self.value=clock,threading.Lock(),unavailable()
    def record(self,value):
        selected=_office_agent(value,now=self.clock())
        with self.lock:
            self.value=selected
    def read(self):
        with self.lock:
            return _office_agent(self.value,now=self.clock())


def install_control_status(namespace, *, cache=None):
    """Observe the existing control response without issuing a second request.

    The actual native function captures helper_control as a default argument;
    rebinding the helper global would not affect it. Pass one recording wrapper
    to that original session_status call and preserve its result unchanged.
    """
    if namespace.get('_native_control_agent_status_installed') is True:
        return
    original=namespace['session_status']
    _need(callable(original) and type(original.__defaults__) is tuple
          and len(original.__defaults__)==1 and callable(original.__defaults__[0]))
    native_transport=original.__defaults__[0]
    cache=cache or AgentStatusCache()
    def session_status(transport=native_transport):
        candidate=unavailable()
        def observe(method,path,*args,**kwargs):
            nonlocal candidate
            value=transport(method,path,*args,**kwargs)
            if method=='GET' and type(value) is dict and value.get('provider')=='NEUROBRO_WEB':
                candidate=_office_agent(value.get('agent'),now=cache.clock())
            return value
        try:
            value=original(observe)
        except Exception:
            cache.record(unavailable())
            raise
        if (type(value) is dict and value.get('provider')=='NEUROBRO_WEB'
                and value.get('status') in namespace['SESSION_STATES']
                and type(value.get('profile_persistent')) is bool
                and type(value.get('checked_at')) is str):
            cache.record(candidate)
        else:
            cache.record(unavailable())
        return value
    namespace['session_status']=session_status
    namespace['native_agent_status']=cache.read
    namespace['_native_control_agent_status_installed']=True


def install_office_status(namespace, *, control=None):
    """Append the optional helper observation without another status request."""
    if namespace.get('_native_office_agent_status_installed') is True:
        return
    control=control or importlib.import_module('worker.neurobrowser_control')
    original=namespace['office_snapshot']
    def office_snapshot(controller,result):
        value=original(controller,result)
        try:
            observed=control.native_agent_status()
            agent=_office_agent(observed)
        except Exception:
            agent=unavailable()
        return {**value,'agent':agent}
    namespace['office_snapshot']=office_snapshot
    namespace['_native_office_agent_status_installed']=True


def main(argv=None):
    from . import dialogue_service
    install_dialogue_status(vars(dialogue_service))
    return dialogue_service.main(argv)


if __name__=='__main__':
    raise SystemExit(main())
