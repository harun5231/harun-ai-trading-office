"""USD-M authentication preflight only: fixed-origin, GET-only, no account mutations."""
import hashlib
import hmac
import json
import os
import re
import stat
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler
from .http_client import NoRedirect, unique

BASE='https://fapi.binance.com'
PRIVATE_PATHS=frozenset(('/fapi/v3/account','/fapi/v1/accountConfig'))
TIME_PATH='/fapi/v1/time'
CODES=frozenset(('BINANCE_NOT_CONFIGURED','BINANCE_AUTH_FAILED','BINANCE_IP_RESTRICTED',
    'BINANCE_PERMISSION_DENIED','BINANCE_CLOCK_ERROR','BINANCE_ACCOUNT_UNAVAILABLE','BINANCE_ENDPOINT_DENIED'))
class BinanceCheckError(Exception):pass

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
            if not re.fullmatch(r'recvWindow=5000&timestamp=[0-9]{1,16}&signature=[0-9a-f]{64}',parts.query):raise ValueError()
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
            elif isinstance(data,dict):return data
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
    def signed_get(self,path):
        if path not in PRIVATE_PATHS:raise BinanceCheckError('BINANCE_ENDPOINT_DENIED')
        elapsed=self._clock()-self._synced if self._synced is not None else None
        if elapsed is None or not 0<=elapsed<=30:raise BinanceCheckError('BINANCE_CLOCK_ERROR')
        query=urlencode((('recvWindow',5000),('timestamp',self._server+int(elapsed*1000))))
        return self._get(BASE+path+'?'+query+'&signature='+signature(self._secret,query),
            {'X-MBX-APIKEY':self._key,'Accept':'application/json','User-Agent':'harun-office/1.0'})
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
                multi_assets_margin=config['multiAssetsMargin'],live_execution=False,mode='DRY_RUN')
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
    return dict(status=reason,live_execution=False,mode='DRY_RUN')
