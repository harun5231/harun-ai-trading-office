"""Public Binance USD-M data only. This module supports GET, never order endpoints."""
import time
from decimal import Decimal as D
from .core import Review,Rules,number
from .http_client import request
from .research_guard import ResearchReadPaused,check_research_read

BASE='https://fapi.binance.com'
INTERVALS={'1h':3600000,'15m':900000}
class Market:
    def __init__(self,lookback=100,max_age=180,transport=request,clock=time.time):
        if not 20<=lookback<=500 or not 30<=max_age<=300:raise Review('INVALID_MARKET_CONFIG')
        self.lookback=lookback;self.max_age=max_age;self.transport=transport;self.clock=clock
    def get(self,path,params=''):
        if path not in ('exchangeInfo','klines','premiumIndex','time'):raise Review('MARKET_ENDPOINT_DENIED')
        check_research_read()
        try:
            code,_,data=self.transport('GET',BASE+'/fapi/v1/'+path+('?' + params if params else ''),timeout=15)
            if code!=200:raise ValueError()
            return data
        except ResearchReadPaused:raise
        except Exception:raise Review('BINANCE_MARKET_DATA_FAILED') from None
    def catalog(self):
        try:
            data=self.get('exchangeInfo');rows={}
            for r in data['symbols']:
                s=r['symbol']
                if r['status']=='TRADING' and r['contractType']=='PERPETUAL' and r['quoteAsset']=='USDT' and r['marginAsset']=='USDT' and 'LIMIT' in r['orderTypes']:
                    rows[s]=r
            if not rows:raise ValueError()
            return rows
        except ResearchReadPaused:raise
        except Exception:raise Review('BINANCE_CONTRACT_RULES_FAILED') from None
    def mark(self,symbol):
        from re import fullmatch
        if not fullmatch(r'[A-Z0-9]{2,18}USDT',symbol):raise Review('INVALID_SYMBOL')
        try:
            row=self.get('premiumIndex','symbol='+symbol)
            if row['symbol']!=symbol or not 0<=self.clock()-int(row['time'])/1000<=60:raise ValueError()
            return {'price':str(number(row['markPrice'])),'time':int(row['time'])}
        except ResearchReadPaused:raise
        except Exception:raise Review('STALE_OR_INVALID_MARK_PRICE') from None
    def data(self,symbol):
        mark=self.mark(symbol)
        try:
            server=int(self.get('time')['serverTime'])
            if abs(server/1000-self.clock())>30:raise ValueError()
            frames={}
            for tf,interval in INTERVALS.items():
                raw=self.get('klines',f'symbol={symbol}&interval={tf}&limit={self.lookback}')
                if not isinstance(raw,list) or len(raw)!=self.lookback:raise ValueError()
                candles=[]
                for r in raw:
                    if len(r)!=12 or type(r[0]) is not int or type(r[6]) is not int:raise ValueError()
                    start,end=r[0],r[6]
                    if start%interval or end!=start+interval-1 or start>server:raise ValueError()
                    if candles and start!=candles[-1]['open_time']+interval:raise ValueError()
                    o,h,l,c=[number(v) for v in r[1:5]];volume=D(r[5])
                    if not volume.is_finite() or volume<0 or not l<=min(o,c)<=max(o,c)<=h:raise ValueError()
                    candles.append(dict(open_time=start,open=r[1],high=r[2],low=r[3],close=r[4],volume=r[5],close_time=end))
                if not candles[-1]['open_time']<=server<=candles[-1]['close_time']:raise ValueError()
                frames[tf]={'symbol':symbol,'timeframe':tf,'candles':candles}
            return {'source':'Binance Futures','symbol':symbol,'fetched_at':self.clock(),'server_time':server,
                    'mark_price':mark,'timeframes':frames,'quantity_unit':'base_asset'}
        except ResearchReadPaused:raise
        except Exception:raise Review('STALE_OR_INVALID_CANDLES') from None
    def fresh(self,data,symbol):
        if data['source']!='Binance Futures' or data['symbol']!=symbol or set(data['timeframes'])!=set(INTERVALS) or not 0<=self.clock()-data['fetched_at']<=self.max_age:
            raise Review('STALE_MARKET_CONTEXT')
        for tf,interval in INTERVALS.items():
            frame=data['timeframes'][tf]
            if frame['symbol']!=symbol or frame['timeframe']!=tf:raise Review('MARKET_CONTEXT_MISMATCH')
            if self.clock()*1000>frame['candles'][-1]['close_time']+self.max_age*1000:raise Review('STALE_MARKET_CONTEXT')
    def rules(self,symbol):
        try:
            row=self.catalog()[symbol];f={x['filterType']:x for x in row['filters']}
            lot=f['LOT_SIZE'];price=f['PRICE_FILTER'];percent=f.get('PERCENT_PRICE')
            mark=number(self.mark(symbol)['price'])
            return Rules(number(lot['stepSize']),number(lot['minQty']),number(lot['maxQty']),number(price['tickSize']),
                D(f.get('MIN_NOTIONAL',{}).get('notional','0')),self.clock(),D(price['minPrice']),D(price['maxPrice']),
                number(percent['multiplierUp']) if percent else None,number(percent['multiplierDown']) if percent else None,mark)
        except ResearchReadPaused:raise
        except Exception:raise Review('BINANCE_CONTRACT_RULES_FAILED') from None
