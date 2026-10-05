"""Read-only reconciliation transport. Mutation contracts are inert test data only.

This release cannot submit orders: both public entry points reject every non-GET
before signing, authorization callbacks, clock synchronization or network I/O.
"""
import json
import re
from urllib.parse import urlencode
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError
from .binance_private import BinanceReadOnly,BASE,signature
from .http_client import NoRedirect,unique
from .core import number

MANUAL_ONLY_SYMBOLS=frozenset(('HYPEUSDT',))
class LiveError(Exception):pass
class NotFound(LiveError):pass

def valid_contract(method,path,p):
    allowed={
      ('GET','/fapi/v1/order'):{'symbol','origClientOrderId'},
      ('GET','/fapi/v1/algoOrder'):{'clientAlgoId'},
      ('DELETE','/fapi/v1/order'):{'symbol','origClientOrderId'},
      ('DELETE','/fapi/v1/algoOrder'):{'clientAlgoId'},
      ('POST','/fapi/v1/marginType'):{'symbol','marginType'},
      ('POST','/fapi/v1/leverage'):{'symbol','leverage'},
      ('POST','/fapi/v1/order'):{'symbol','side','positionSide','type','timeInForce','quantity','price','newClientOrderId'},
      ('POST','/fapi/v1/algoOrder'):{'algoType','symbol','side','positionSide','type','triggerPrice','workingType','closePosition','clientAlgoId'}}
    if set(p)!=allowed.get((method,path)):raise LiveError('LIVE_ENDPOINT_DENIED')
    if 'symbol' in p and (not isinstance(p['symbol'],str) or not re.fullmatch(r'[A-Z0-9]{2,18}USDT',p['symbol']) or p['symbol'] in MANUAL_ONLY_SYMBOLS):raise LiveError('LIVE_SYMBOL_DENIED')
    for k in ('origClientOrderId','newClientOrderId','clientAlgoId'):
        if k in p and (not isinstance(p[k],str) or not re.fullmatch(r'ho-[0-9a-f]{28}-[ets]',p[k])):raise LiveError('LIVE_OWNERSHIP_UNVERIFIED')
    if path.endswith('/marginType') and p['marginType']!='CROSSED':raise LiveError('LIVE_ENDPOINT_DENIED')
    if path.endswith('/leverage') and p['leverage']!=75:raise LiveError('LIVE_ENDPOINT_DENIED')
    if method=='POST' and path.endswith(('/order','/algoOrder')):
        if p['side'] not in ('BUY','SELL') or p['positionSide']!='BOTH':raise LiveError('LIVE_ENDPOINT_DENIED')
        if path.endswith('/order'):
            if p['type']!='LIMIT' or p['timeInForce']!='GTC':raise LiveError('LIVE_ENDPOINT_DENIED')
            number(p['price']);number(p['quantity'])
        else:
            if p['type'] not in ('TAKE_PROFIT_MARKET','STOP_MARKET') or p['algoType']!='CONDITIONAL' or p['workingType']!='MARK_PRICE' or p['closePosition']!='true':raise LiveError('LIVE_ENDPOINT_DENIED')
            number(p['triggerPrice'])

def signed_read_request(path,p,key,secret,timestamp):
    """Pure GET request construction; no mutation request builder in this release."""
    valid_contract('GET',path,p)
    query=urlencode([('recvWindow',5000),('timestamp',timestamp)]+sorted(p.items()))
    signed=query+'&signature='+signature(secret,query)
    return Request(BASE+path+'?'+signed,headers={'X-MBX-APIKEY':key,'Accept':'application/json','User-Agent':'harun-office/1.0'},method='GET')

def wire(method,path,p,key,secret,timestamp):
    if method!='GET':raise LiveError('LIVE_SUBMISSION_DISABLED')
    valid_contract('GET',path,p)
    reason='LIVE_REQUEST_UNCERTAIN'
    try:
        req=signed_read_request(path,p,key,secret,timestamp)
        try:response=build_opener(ProxyHandler({}),NoRedirect()).open(req,timeout=15)
        except HTTPError as error:response=error
        with response:
            raw=response.read(2_000_001)
            if len(raw)>2_000_000:raise ValueError()
            value=json.loads(raw,parse_float=str,object_pairs_hook=unique)
            code=value.get('code') if isinstance(value,dict) else None
            if response.code==200 and isinstance(value,dict) and (not isinstance(code,int) or code>=0):return value
            if method=='GET' and code==-2013:reason='LIVE_ORDER_NOT_FOUND'
            elif code==-4046:reason='LIVE_MARGIN_ALREADY_SET'
            elif response.code in (400,401,403) and code in (-1021,-1022,-1100,-1101,-1102,-1111,-1115,-1116,-1117,-1121,-1130,-2014,-2015,-2018,-2019):reason='LIVE_PROVIDER_REJECTED'
    except Exception:pass
    if reason=='LIVE_ORDER_NOT_FOUND':raise NotFound(reason) from None
    raise LiveError(reason) from None

class BinanceExecution(BinanceReadOnly):
    def __init__(self,*args,authorize=None,live_transport=wire,**kwargs):
        super().__init__(*args,**kwargs)
        self.authorize=authorize or (lambda *args:False);self.live_transport=live_transport
    def scoped(self,method,path,params):
        if method!='GET':raise LiveError('LIVE_SUBMISSION_DISABLED')
        valid_contract(method,path,params)
        self.sync_time()
        try:return self.live_transport(method,path,params,self._key,self._secret,self._server)
        except NotFound:raise NotFound('LIVE_ORDER_NOT_FOUND') from None
        except LiveError as e:
            reason=str(e) if str(e) in ('LIVE_MARGIN_ALREADY_SET','LIVE_PROVIDER_REJECTED','LIVE_REQUEST_UNCERTAIN') else 'LIVE_REQUEST_UNCERTAIN'
        except Exception:reason='LIVE_REQUEST_UNCERTAIN'
        raise LiveError(reason) from None
