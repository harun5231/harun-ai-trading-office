"""USD-M account/history reads: fixed-origin, GET-only, no account mutations."""
import hashlib
import hmac
import json
import logging
import os
import errno
import re
import socket
import ssl
import stat
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal
from http.client import HTTPSConnection
from threading import RLock
from urllib.error import HTTPError,URLError
from urllib.parse import urlencode, urlsplit, parse_qsl
from urllib.request import Request, build_opener, ProxyHandler, HTTPSHandler
from .http_client import NoRedirect, unique
from .research_guard import ResearchReadPaused,check_research_read

BASE='https://fapi.binance.com'
COMMISSION_PATH='/fapi/v1/commissionRate'
COMMISSION_SOURCE='BINANCE_FUTURES_COMMISSION_RATE'
SYMBOL_PATHS=frozenset(('/fapi/v1/symbolConfig','/fapi/v3/positionRisk','/fapi/v1/openOrders','/fapi/v1/openAlgoOrders','/fapi/v1/leverageBracket',COMMISSION_PATH))
OPTIONAL_SYMBOL_PATHS=frozenset(('/fapi/v1/openOrders','/fapi/v1/openAlgoOrders','/fapi/v3/positionRisk'))
HISTORY_PATHS=frozenset(('/fapi/v1/income','/fapi/v1/userTrades'))
PRIVATE_PATHS=frozenset(('/fapi/v3/account','/fapi/v1/accountConfig')) | SYMBOL_PATHS | HISTORY_PATHS
TIME_PATH='/fapi/v1/time'
MAX_HISTORY_WINDOW_MS=7*24*60*60*1000
QUERY_ORDER=('symbol','startTime','endTime','limit','page','fromId')
CODES=frozenset(('BINANCE_NOT_CONFIGURED','BINANCE_AUTH_FAILED','BINANCE_IP_RESTRICTED',
    'BINANCE_PERMISSION_DENIED','BINANCE_CLOCK_ERROR','BINANCE_ACCOUNT_UNAVAILABLE','BINANCE_ENDPOINT_DENIED'))
class BinanceCheckError(Exception):pass

_time_sample=ContextVar('binance_time_sample',default=None)
_signed_request=ContextVar('binance_signed_request',default=None)
_read_diagnostic=ContextVar('binance_read_diagnostic',default=None)
_TRANSPORT_PHASES=frozenset(('VALIDATE','CONNECT','SIGN','WRITE','WRITE_ENTERED','READ',
    'HEADERS','BODY','DECODE','COMPLETE','DEADLINE','UNKNOWN'))
_DELIVERIES=frozenset(('NOT_SENT','DELIVERY_UNCONFIRMED','RESPONSE_RECEIVED'))
_ERROR_CLASSES=frozenset(('gaierror','herror','TimeoutError','ConnectionError','ConnectionRefusedError',
    'ConnectionResetError','ConnectionAbortedError','BrokenPipeError','PermissionError','OSError',
    'URLError','HTTPError','SSLError','SSLCertVerificationError','SSLEOFError','SSLSyscallError',
    'SSLZeroReturnError','SSLWantReadError','SSLWantWriteError','RemoteDisconnected','BadStatusLine',
    'IncompleteRead','LineTooLong','HTTPException','ValueError','RuntimeError','TypeError','JSONDecodeError',
    'BinanceCheckError','_ExchangeError','Review'))
_ERROR_NUMBERS={v:k for k,v in vars(errno).items()if k.startswith('E')and type(v)is int}
_ERROR_NUMBERS.update({v:k for k,v in vars(socket).items()if k.startswith('EAI_')and type(v)is int})

def safe_transport_error(error):
    """Finite diagnostics only; never stringify a provider/socket exception."""
    chain=[];seen=set()
    while isinstance(error,BaseException)and id(error)not in seen and len(chain)<4:
        seen.add(id(error));chain.append(error)
        error=error.reason if isinstance(error,URLError)and isinstance(error.reason,BaseException)else None
    if not chain:return dict(exception_class=None,exception_chain=[],errno=None,errno_name=None)
    root=chain[-1];number=getattr(root,'errno',None)
    value=dict(exception_class=type(root).__name__ if type(root).__name__ in _ERROR_CLASSES else'Exception',
        exception_chain=[type(e).__name__ if type(e).__name__ in _ERROR_CLASSES else'Exception'for e in chain])
    if isinstance(root,ssl.SSLError):
        value.update(errno=None,errno_name=None,
            ssl_error_code=int(number)if isinstance(number,int)and not isinstance(number,bool)and 0<=number<=255 else None)
        if isinstance(root,ssl.SSLCertVerificationError):
            verify=getattr(root,'verify_code',None)
            value['ssl_verify_code']=verify if type(verify)is int and 0<=verify<=1000000 else None
    else:
        value['errno']=number if type(number)is int and number in _ERROR_NUMBERS else None
        value['errno_name']=_ERROR_NUMBERS.get(value['errno'])
    return value

def transport_error_metadata(error,phase,delivery):
    return dict(safe_transport_error(error),
        phase=phase if phase in _TRANSPORT_PHASES else'UNKNOWN',
        delivery=delivery if delivery in _DELIVERIES else'DELIVERY_UNCONFIRMED')

def dns_not_sent_retryable(value):
    return (type(value)is dict and value.get('phase')=='CONNECT'and value.get('delivery')=='NOT_SENT'
        and value.get('exception_class')=='gaierror'
        and value.get('errno')in(socket.EAI_NONAME,socket.EAI_AGAIN))

def _phase(value,delivery=None):
    diagnostic=_read_diagnostic.get()
    if diagnostic is not None:
        diagnostic['phase']=value
        if delivery is not None:diagnostic['delivery']=delivery

def _read_budget():
    diagnostic=_read_diagnostic.get()
    if diagnostic is not None:
        remaining=diagnostic['deadline']-time.monotonic()
        if remaining<=0:raise BinanceCheckError('BINANCE_ACCOUNT_UNAVAILABLE')
        return min(15,remaining)
    return 15

def _socket_budget(connection):
    remaining=_read_budget();sock=connection.sock
    if sock is not None and callable(getattr(sock,'settimeout',None)):
        current=sock.gettimeout()
        if type(current)in(int,float)and current>0:remaining=min(remaining,current)
        sock.settimeout(remaining)

class _ClockSampleConnection(HTTPSConnection):
    def request(self,method,url,body=None,headers=None,*,encode_chunked=False):
        sample=_time_sample.get()
        signed=_signed_request.get()
        path=urlsplit(url).path
        if path==TIME_PATH:
            # DNS/TCP/TLS precede the unsigned time request. They cannot age a
            # serverTime value that Binance has not produced yet.
            _phase('CONNECT')
            if self.sock is None:self.connect()
            _read_budget()
            if sample is not None:sample['wire_started']=sample['clock']()
        elif signed is not None and path==signed['path']:
            if method!='GET' or body is not None:raise BinanceCheckError('BINANCE_ENDPOINT_DENIED')
            # A signed query cannot wait through DNS/TCP/TLS with its timestamp
            # already fixed. Recreate only the selector at the wire boundary.
            _phase('CONNECT')
            if self.sock is None:self.connect()
            _phase('SIGN')
            check_research_read()
            fresh_url,fresh_headers=signed['prepare']()
            try:
                parts=urlsplit(fresh_url)
                if (parts.scheme!='https' or parts.netloc!='fapi.binance.com' or parts.fragment
                        or parts.path!=path or fresh_headers!=signed['headers']):raise ValueError()
                _validate_signed_query(parts.path,parts.query)
            except Exception:raise BinanceCheckError('BINANCE_ENDPOINT_DENIED') from None
            url=parts.path+'?'+parts.query
            signed['wire_timestamp_ms']=int(dict(parse_qsl(parts.query))['timestamp'])
            signed['wire_started']=signed['clock']()
            _read_budget()
        _socket_budget(self)
        _phase('WRITE_ENTERED','DELIVERY_UNCONFIRMED')
        return super().request(method,url,body,headers or {},encode_chunked=encode_chunked)

    def getresponse(self):
        _socket_budget(self)
        _phase('HEADERS')
        value=super().getresponse()
        _phase('HEADERS','RESPONSE_RECEIVED')
        return value

class _ClockSampleHTTPSHandler(HTTPSHandler):
    def https_open(self,request):
        return self.do_open(_ClockSampleConnection,request,context=self._context)

def _query_options(path,symbol,options,timestamp):
    """One endpoint-specific allowlist, also enforced by the HTTP transport."""
    if path not in PRIVATE_PATHS:raise ValueError()
    if path in SYMBOL_PATHS:
        if symbol is None and path not in OPTIONAL_SYMBOL_PATHS:raise ValueError()
        if symbol is not None and (not isinstance(symbol,str) or not re.fullmatch(r'[A-Z0-9]{2,18}USDT',symbol)):raise ValueError()
    elif path=='/fapi/v1/userTrades':
        if not isinstance(symbol,str) or not re.fullmatch(r'[A-Z0-9_]{3,30}',symbol):raise ValueError()
    elif symbol is not None:raise ValueError()
    allowed={'startTime','endTime','limit','page'} if path=='/fapi/v1/income' else {'startTime','endTime','limit','fromId'} if path=='/fapi/v1/userTrades' else set()
    if set(options)-allowed or any(type(v) is not int for v in options.values()):raise ValueError()
    if 'limit' in options and not 1<=options['limit']<=1000:raise ValueError()
    if 'page' in options and not 1<=options['page']<=1000:raise ValueError()
    if 'fromId' in options and not 0<=options['fromId']<2**63:raise ValueError()
    if 'fromId' in options and ('startTime' in options or 'endTime' in options):raise ValueError()
    if ('startTime' in options)!=('endTime' in options):raise ValueError()
    if 'startTime' in options and not 0<=options['startTime']<=options['endTime']<=timestamp:raise ValueError()
    if 'startTime' in options and options['endTime']-options['startTime']>MAX_HISTORY_WINDOW_MS:raise ValueError()
    # History reads always have an explicit bounded window or an ID cursor.
    if path in HISTORY_PATHS and 'startTime' not in options and 'fromId' not in options:raise ValueError()
    values=dict(options)
    if symbol is not None:values['symbol']=symbol
    return [(k,values[k]) for k in QUERY_ORDER if k in values]

def _validate_signed_query(path,query):
    pairs=parse_qsl(query,keep_blank_values=True,strict_parsing=True)
    if len(dict(pairs))!=len(pairs) or len(pairs)<3:raise ValueError()
    values=dict(pairs)
    if pairs[0]!=('recvWindow','5000') or pairs[1][0]!='timestamp' or pairs[-1][0]!='signature':raise ValueError()
    if not re.fullmatch(r'[0-9]{1,16}',values['timestamp']) or not re.fullmatch(r'[0-9a-f]{64}',values['signature']):raise ValueError()
    timestamp=int(values['timestamp'])
    if not 0<timestamp<10**16:raise ValueError()
    opts={k:v for k,v in values.items() if k not in ('recvWindow','timestamp','signature','symbol')}
    if any(not re.fullmatch(r'[0-9]{1,19}',v) for v in opts.values()):raise ValueError()
    scoped=_query_options(path,values.get('symbol'),{k:int(v) for k,v in opts.items()},timestamp)
    canonical=urlencode([('recvWindow',5000),('timestamp',timestamp)]+scoped+[('signature',values['signature'])])
    if query!=canonical:raise ValueError()

def valid_secret(value):
    return isinstance(value,str) and 16<=len(value)<=512 and all(33<=ord(c)<=126 for c in value)

def read_secret(env):
    # Only file paths; never accept credential values from environment or CLI.
    try:
        path=os.environ.get(env,'')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as f:
            info=os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600:raise ValueError()
            raw=f.read(514)
        value=raw.decode('ascii').removesuffix('\n')
        if not valid_secret(value):raise ValueError()
        return value
    except Exception:raise BinanceCheckError('BINANCE_NOT_CONFIGURED') from None

def signature(secret,query):
    return hmac.new(secret.encode('ascii'),query.encode('ascii'),hashlib.sha256).hexdigest()

def failure(status,data):
    code=data.get('code') if isinstance(data,dict) else None
    if type(code) is not int:code=None
    if code==-1021:return 'BINANCE_CLOCK_ERROR'
    if code==-1011:return 'BINANCE_IP_RESTRICTED'
    if code==-1002:return 'BINANCE_PERMISSION_DENIED'
    # -2015 explicitly conflates invalid key, IP or permissions; do not guess.
    if code in (-1022,-2014,-2015) or status==401:return 'BINANCE_AUTH_FAILED'
    # 403 can be a WAF restriction, not necessarily the key permission.
    return 'BINANCE_ACCOUNT_UNAVAILABLE'

def _diagnostic_copy(value):
    if type(value)is not dict:return None
    phase=value.get('phase');delivery=value.get('delivery');number=value.get('errno');name=value.get('exception_class')
    result=dict(phase=phase if type(phase)is str and phase in _TRANSPORT_PHASES else'UNKNOWN',
        delivery=delivery if type(delivery)is str and delivery in _DELIVERIES else'DELIVERY_UNCONFIRMED',
        exception_class=name if name is None or type(name)is str and name in _ERROR_CLASSES|{'Exception'}else'Exception',
        errno=number if type(number)is int and number in _ERROR_NUMBERS else None)
    result['errno_name']=_ERROR_NUMBERS.get(result['errno'])
    chain=value.get('exception_chain')
    if isinstance(chain,list):result['exception_chain']=[name if type(name)is str and name in _ERROR_CLASSES else'Exception'for name in chain[:4]]
    for key,low,high in(('attempts',1,2),('retry_count',0,1),('http_status',100,599),
                         ('binance_code',-1000000,1000000),('ssl_error_code',0,255),('ssl_verify_code',0,1000000)):
        number=value.get(key)
        if type(number)is int and low<=number<=high:result[key]=number
    elapsed=value.get('duration_s')
    if type(elapsed)in(int,float)and 0<=elapsed<=1000000:result['duration_s']=elapsed
    return result


def read_only_get(url,headers):
    """Fixed-origin GET. Only a proven pre-write DNS failure gets one retry."""
    try:
        parts=urlsplit(url)
        if parts.scheme!='https' or parts.netloc!='fapi.binance.com' or parts.fragment:raise ValueError()
        if parts.path==TIME_PATH:
            if parts.query or headers:raise ValueError()
        elif parts.path in PRIVATE_PATHS:
            _validate_signed_query(parts.path,parts.query)
            if set(headers)!={'X-MBX-APIKEY','Accept','User-Agent'}:raise ValueError()
        else:raise ValueError()
    except Exception:raise BinanceCheckError('BINANCE_ENDPOINT_DENIED') from None
    # This bounds retry/write admission and socket waits; libc DNS resolution
    # retains its OS timeout and cannot be preempted by urllib's socket timeout.
    endpoint=parts.path;expected_headers=dict(headers);deadline=time.monotonic()+30
    for attempt in range(2):
        diagnostic=dict(phase='CONNECT',delivery='NOT_SENT',deadline=deadline,attempts=attempt+1,retry_count=attempt)
        token=_read_diagnostic.set(diagnostic);started=time.monotonic();reason='BINANCE_ACCOUNT_UNAVAILABLE';signed=_signed_request.get()
        try:
            check_research_read();_read_budget()
            if attempt and signed is not None and endpoint==signed['path']:
                _phase('SIGN')
                url,headers=signed['prepare']()
                fresh=urlsplit(url)
                if (fresh.scheme!='https' or fresh.netloc!='fapi.binance.com' or fresh.fragment
                        or fresh.path!=endpoint or headers!=expected_headers):
                    raise BinanceCheckError('BINANCE_ENDPOINT_DENIED')
                _validate_signed_query(fresh.path,fresh.query)
            _phase('CONNECT')
            req=Request(url,headers=headers,method='GET')
            handlers=[ProxyHandler({}),NoRedirect()]
            if endpoint==TIME_PATH or (signed is not None and endpoint==signed['path']):
                handlers.append(_ClockSampleHTTPSHandler())
            opener=build_opener(*handlers)
            try:response=opener.open(req,timeout=_read_budget())
            except HTTPError as error:response=error
            _phase('HEADERS','RESPONSE_RECEIVED')
            with response:
                status=response.code;diagnostic['http_status']=status
                _phase('BODY');raw=response.read(2_000_001)
                if len(raw)>2_000_000:raise ValueError()
                _phase('DECODE')
                try:data=json.loads(raw,parse_float=str,object_pairs_hook=unique)
                except (ValueError,TypeError):data=None
                code=data.get('code')if isinstance(data,dict)and type(data.get('code'))is int else None
                if code is not None:diagnostic['binance_code']=code
                if status!=200 or code is not None and code<0:reason=failure(status,data)
                elif isinstance(data,(dict,list)):
                    _phase('COMPLETE')
                    if attempt:
                        logging.getLogger(__name__).warning('BINANCE_READ_RECOVERED endpoint=%s attempts=2 reason=DNS_BEFORE_WRITE',endpoint)
                    return data
        except ResearchReadPaused:raise
        except BinanceCheckError as error:
            reason=str(error)if str(error)in CODES else'BINANCE_ACCOUNT_UNAVAILABLE'
            previous=_diagnostic_copy(getattr(error,'transport_diagnostics',None))
            diagnostic.update(previous if previous is not None else
                transport_error_metadata(error,diagnostic['phase'],diagnostic['delivery']))
        except Exception as error:
            diagnostic.update(transport_error_metadata(error,diagnostic['phase'],diagnostic['delivery']))
        finally:
            diagnostic['duration_s']=time.monotonic()-started
            _read_diagnostic.reset(token)
        metadata=_diagnostic_copy(diagnostic)
        can_refresh=(endpoint==TIME_PATH or type(signed)is dict and signed.get('path')==endpoint
            and callable(signed.get('prepare')))
        if attempt==0 and can_refresh and dns_not_sent_retryable(metadata)and time.monotonic()<deadline:
            logging.getLogger(__name__).warning('BINANCE_READ_DNS_RETRY endpoint=%s attempt=1 phase=%s errno=%s',
                endpoint,metadata['phase'],metadata['errno'])
            continue
        logging.getLogger(__name__).warning('BINANCE_READ_FAILED endpoint=%s phase=%s delivery=%s exception_class=%s errno=%s attempts=%s http_status=%s binance_code=%s reason=%s duration_s=%.6f',
            endpoint,metadata['phase'],metadata['delivery'],metadata.get('exception_class'),metadata['errno'],attempt+1,
            metadata.get('http_status'),metadata.get('binance_code'),reason,diagnostic['duration_s'])
        # No raw exception/URL chain; optional diagnostics contain finite fields only.
        error=BinanceCheckError(reason);error.transport_diagnostics=metadata
        raise error from None

class BinanceReadOnly:
    def __init__(self,transport=read_only_get,clock=time.monotonic):
        self._key=read_secret('BINANCE_API_KEY_FILE')
        self._secret=read_secret('BINANCE_API_SECRET_FILE')
        self._transport=transport;self._clock=clock;self._server=None;self._synced=None
        self._time_lock=RLock();self._clock_diagnostics=None
    def _get(self,url,headers,*,prepare=None):
        check_research_read()
        try:
            if prepare is not None:url,headers=prepare()
            return self._transport(url,headers)
        except ResearchReadPaused:raise
        except BinanceCheckError as error:
            reason=str(error) if str(error) in CODES else 'BINANCE_ACCOUNT_UNAVAILABLE'
            metadata=_diagnostic_copy(getattr(error,'transport_diagnostics',None))
        except Exception:
            reason='BINANCE_ACCOUNT_UNAVAILABLE';metadata=None
        error=BinanceCheckError(reason)
        if metadata is not None:error.transport_diagnostics=metadata
        raise error from None
    def sync_time(self):
        with self._time_lock:
            start=self._clock();sample={'clock':self._clock,'wire_started':None}
            token=_time_sample.set(sample)
            try:data=self._get(BASE+TIME_PATH,{})
            finally:
                end=self._clock();_time_sample.reset(token)
            wire=sample['wire_started']
            if wire is None:wire=start  # Custom transports retain the strict total-RTT contract.
            elapsed=end-wire
            self._clock_diagnostics=dict(source='SERVER_MONOTONIC_ANCHOR',total_s=end-start,
                wire_s=elapsed,setup_s=wire-start,recv_window_ms=5000)
            reason=('INVALID_TIME_RESPONSE' if not isinstance(data,dict) or type(data.get('serverTime')) is not int
                    or not 0<data['serverTime']<10**16 else 'SAMPLE_ROUNDTRIP')
            if not start<=wire<=end or not 0<=elapsed<=2 or reason=='INVALID_TIME_RESPONSE':
                logging.getLogger(__name__).warning('BINANCE_CLOCK_SYNC_REJECTED endpoint=%s reason=%s total_s=%.6f wire_s=%.6f setup_s=%.6f',TIME_PATH,reason,end-start,elapsed,wire-start)
                raise BinanceCheckError('BINANCE_CLOCK_ERROR')
            # Receipt anchoring is conservative: no midpoint correction can put
            # a timestamp ahead of Binance after asymmetric network latency.
            self._server=data['serverTime'];self._synced=end
            logging.getLogger(__name__).debug('BINANCE_CLOCK_SYNC_VALID endpoint=%s total_s=%.6f wire_s=%.6f setup_s=%.6f',TIME_PATH,end-start,elapsed,wire-start)
    def server_time_ms(self):
        """Trusted server time advanced only with monotonic elapsed time."""
        with self._time_lock:
            elapsed=self._clock()-self._synced if self._synced is not None else None
            if elapsed is None or not 0<=elapsed<=30:
                logging.getLogger(__name__).warning('BINANCE_CLOCK_CACHE_REJECTED source=SERVER_MONOTONIC_ANCHOR age_s=%s',elapsed)
                raise BinanceCheckError('BINANCE_CLOCK_ERROR')
            return self._server+int(elapsed*1000)
    def signed_get(self,path,symbol=None,*,start_time=None,end_time=None,limit=None,page=None,from_id=None):
        # Validate shape before touching the clock, transport, or credentials.
        options={k:v for k,v in (('startTime',start_time),('endTime',end_time),('limit',limit),('page',page),('fromId',from_id)) if v is not None}
        try:_query_options(path,symbol,options,10**16-1)
        except Exception:raise BinanceCheckError('BINANCE_ENDPOINT_DENIED') from None
        with self._time_lock:
            # Wait for a concurrent synchronization/request and run the local
            # admission guard before creating this request's timestamp/HMAC.
            headers={'X-MBX-APIKEY':self._key,'Accept':'application/json','User-Agent':'harun-office/1.0'}
            def prepare():
                timestamp=self.server_time_ms()
                try:scoped=_query_options(path,symbol,options,timestamp)
                except Exception:raise BinanceCheckError('BINANCE_ENDPOINT_DENIED') from None
                params=[('recvWindow',5000),('timestamp',timestamp)]+scoped
                query=urlencode(params)
                return BASE+path+'?'+query+'&signature='+signature(self._secret,query),headers
            context=dict(path=path,prepare=prepare,headers=headers,clock=self._clock,
                wire_timestamp_ms=None,wire_started=None)
            token=_signed_request.set(context)
            try:return self._get(None,None,prepare=prepare)
            finally:_signed_request.reset(token)
    def commission_rate(self,symbol):
        """Fresh account/symbol commission evidence, never an assumed fee tier."""
        try:_query_options(COMMISSION_PATH,symbol,{},10**16-1)
        except Exception:raise BinanceCheckError('BINANCE_ENDPOINT_DENIED') from None
        self.sync_time()
        data=self.signed_get(COMMISSION_PATH,symbol)
        checked_at_ms=self.server_time_ms()
        try:
            if not isinstance(data,dict) or data.get('symbol')!=symbol:raise ValueError()
            rates={}
            for source,target in (('makerCommissionRate','maker'),('takerCommissionRate','taker')):
                value=data[source]
                if not isinstance(value,str) or len(value)>64 or not re.fullmatch(r'\d+(?:\.\d+)?',value):raise ValueError()
                rate=Decimal(value)
                if not rate.is_finite() or not Decimal(0)<=rate<Decimal(1):raise ValueError()
                rates[target]=value
            checked_at=datetime.fromtimestamp(checked_at_ms/1000,timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
            return dict(symbol=symbol,**rates,source=COMMISSION_SOURCE,checked_at=checked_at,checked_at_ms=checked_at_ms)
        except Exception:raise BinanceCheckError('BINANCE_ACCOUNT_UNAVAILABLE') from None
    commission_rates=commission_rate
    def check(self):
        self.sync_time()
        account=self.signed_get('/fapi/v3/account')
        config=self.signed_get('/fapi/v1/accountConfig')
        try:
            if not isinstance(account,dict) or not isinstance(config,dict):raise ValueError()
            if any(type(config.get(k)) is not bool for k in ('canTrade','dualSidePosition','multiAssetsMargin')):raise ValueError()
            assets=account['assets']
            if not isinstance(assets,list) or any(not isinstance(a,dict) for a in assets):raise ValueError()
            usdt=[a for a in assets if a.get('asset')=='USDT']
            if len(usdt)>1:raise ValueError()
            result=dict(status='BINANCE_CONNECTED',futures=True,can_trade=config['canTrade'],
                position_mode='HEDGE' if config['dualSidePosition'] else 'ONE_WAY',
                multi_assets_margin=config['multiAssetsMargin'],live_execution=False,mode='BINANCE_READ_ONLY')
            if usdt:
                for source,target in (('walletBalance','usdt_wallet_balance'),('availableBalance','usdt_available_balance')):
                    value=usdt[0][source]
                    if not isinstance(value,str) or len(value)>64 or not re.fullmatch(r'-?\d+(?:\.\d+)?',value):raise ValueError()
                    result[target]=value
            return result
        except Exception:raise BinanceCheckError('BINANCE_ACCOUNT_UNAVAILABLE') from None

def check():
    """CLI boundary: no ledger, snapshot, dashboard, logging or provider response storage."""
    try:return BinanceReadOnly().check()
    except ResearchReadPaused:raise
    except BinanceCheckError as error:
        reason=str(error) if str(error) in CODES else 'BINANCE_ACCOUNT_UNAVAILABLE'
    except Exception:reason='BINANCE_ACCOUNT_UNAVAILABLE'
    return dict(status=reason,live_execution=False,mode='BINANCE_READ_ONLY')
