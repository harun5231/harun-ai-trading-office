"""USD-M account/history reads: fixed-origin, GET-only, no account mutations."""
import hashlib
import hmac
import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, parse_qsl
from urllib.request import Request, build_opener, ProxyHandler
from .http_client import NoRedirect, unique

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

def read_only_get(url,headers):
    """Transport validates origin/path/query itself; method and body are not arguments."""
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
    reason='BINANCE_ACCOUNT_UNAVAILABLE'
    try:
        req=Request(url,headers=headers,method='GET')
        opener=build_opener(ProxyHandler({}),NoRedirect())
        try:response=opener.open(req,timeout=15)
        except HTTPError as error:response=error
        with response:
            status=response.code
            raw=response.read(2_000_001)
            if len(raw)>2_000_000:raise ValueError()
            try:data=json.loads(raw,parse_float=str,object_pairs_hook=unique)
            except (ValueError,TypeError):data=None
            if status!=200 or (isinstance(data,dict) and type(data.get('code')) is int and data['code']<0):
                reason=failure(status,data)
            elif isinstance(data,(dict,list)):return data
    except Exception:pass
    # Never chain a URL-bearing urllib exception, provider message or raw response.
    raise BinanceCheckError(reason) from None

class BinanceReadOnly:
    def __init__(self,transport=read_only_get,clock=time.monotonic):
        self._key=read_secret('BINANCE_API_KEY_FILE')
        self._secret=read_secret('BINANCE_API_SECRET_FILE')
        self._transport=transport;self._clock=clock;self._server=None;self._synced=None
    def _get(self,url,headers):
        try:return self._transport(url,headers)
        except BinanceCheckError as error:
            reason=str(error) if str(error) in CODES else 'BINANCE_ACCOUNT_UNAVAILABLE'
        except Exception:reason='BINANCE_ACCOUNT_UNAVAILABLE'
        raise BinanceCheckError(reason) from None
    def sync_time(self):
        start=self._clock();data=self._get(BASE+TIME_PATH,{})
        end=self._clock()
        if not isinstance(data,dict) or type(data.get('serverTime')) is not int or not 0<data['serverTime']<10**16 or not 0<=end-start<=2:
            raise BinanceCheckError('BINANCE_CLOCK_ERROR')
        # Advance trusted server time using monotonic elapsed time, not VPS wall clock.
        # Anchoring at receipt is conservative (at most measured RTT behind server).
        self._server=data['serverTime'];self._synced=end
    def server_time_ms(self):
        """Trusted server time advanced only with monotonic elapsed time."""
        elapsed=self._clock()-self._synced if self._synced is not None else None
        if elapsed is None or not 0<=elapsed<=30:raise BinanceCheckError('BINANCE_CLOCK_ERROR')
        return self._server+int(elapsed*1000)
    def signed_get(self,path,symbol=None,*,start_time=None,end_time=None,limit=None,page=None,from_id=None):
        # Validate shape before touching the clock, transport, or credentials.
        options={k:v for k,v in (('startTime',start_time),('endTime',end_time),('limit',limit),('page',page),('fromId',from_id)) if v is not None}
        try:_query_options(path,symbol,options,10**16-1)
        except Exception:raise BinanceCheckError('BINANCE_ENDPOINT_DENIED') from None
        timestamp=self.server_time_ms()
        try:scoped=_query_options(path,symbol,options,timestamp)
        except Exception:raise BinanceCheckError('BINANCE_ENDPOINT_DENIED') from None
        params=[('recvWindow',5000),('timestamp',timestamp)]+scoped
        query=urlencode(params)
        return self._get(BASE+path+'?'+query+'&signature='+signature(self._secret,query),
            {'X-MBX-APIKEY':self._key,'Accept':'application/json','User-Agent':'harun-office/1.0'})
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
    except BinanceCheckError as error:
        reason=str(error) if str(error) in CODES else 'BINANCE_ACCOUNT_UNAVAILABLE'
    except Exception:reason='BINANCE_ACCOUNT_UNAVAILABLE'
    return dict(status=reason,live_execution=False,mode='BINANCE_READ_ONLY')
