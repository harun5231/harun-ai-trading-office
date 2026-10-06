"""Shared offline provider/account fixtures for the automatic pipeline tests."""
import copy
import time
from urllib.parse import parse_qs, urlsplit

from worker.core import D, Rules
from worker.market import INTERVALS, Market
from worker.neuroapi import NUMERIC_FIELDS


GOOD = dict(symbol='BTCUSDT', side='LONG', position_size=D('2.5'),
            limit_entry=100, take_profit=104, stop_loss=98, risk_reward=2)
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT']


def RULES():
    return Rules(D('.001'), D('.001'), D('1000'), D('.01'), D('5'), time.time())


def row(symbol):
    return dict(symbol=symbol, status='TRADING', contractType='PERPETUAL',
                quoteAsset='USDT', marginAsset='USDT', orderTypes=['LIMIT'], filters=[
                    dict(filterType='LOT_SIZE', stepSize='0.001', minQty='0.001', maxQty='1000'),
                    dict(filterType='PRICE_FILTER', tickSize='0.01', minPrice='0.01', maxPrice='1000000'),
                    dict(filterType='MIN_NOTIONAL', notional='5')])


def hold(symbol):
    return dict(symbol=symbol, side='HOLD', **{field: None for field in NUMERIC_FIELDS})


class FakeMarket(Market):
    symbols = ('BTCUSDT', 'ETHUSDT')

    def __init__(self):
        self.calls = []
        super().__init__(20, transport=self.respond)

    def respond(self, method, url, headers=None, body=None, timeout=None):
        """Implement the public GET contract in memory; reject every other route."""
        parsed = urlsplit(url)
        if method != 'GET' or body is not None or parsed.netloc != 'fapi.binance.com':
            raise AssertionError('Fixture accepts public Binance GETs only')
        self.calls.append((method, url))
        now = int(time.time() * 1000)
        if parsed.path == '/fapi/v1/exchangeInfo':
            return 200, {}, dict(symbols=[row(symbol) for symbol in self.symbols])
        if parsed.path == '/fapi/v1/time':
            return 200, {}, dict(serverTime=now)
        query = parse_qs(parsed.query)
        if parsed.path == '/fapi/v1/premiumIndex':
            return 200, {}, dict(symbol=query['symbol'][0], markPrice='101', time=now)
        if parsed.path != '/fapi/v1/klines':
            raise AssertionError('Unexpected fixture endpoint')
        interval = INTERVALS[query['interval'][0]]
        count = int(query['limit'][0])
        start = now // interval * interval
        return 200, {}, [[start - (count - 1 - i) * interval, '100', '102', '99', '101', '10',
                          start - (count - 1 - i) * interval + interval - 1, '0', 0, '0', '0', '0']
                         for i in range(count)]


class ExpandedMarket(FakeMarket):
    symbols = tuple(SYMBOLS)


class RobotMarket(ExpandedMarket):
    symbols = (*ExpandedMarket.symbols, 'HYPEUSDT')


class Account:
    def __init__(self):
        self.positions = []
        self.calls = []

    def check(self):
        return dict(status='BINANCE_CONNECTED', position_mode='ONE_WAY',
                    multi_assets_margin=False, can_trade=True,
                    usdt_wallet_balance='117.25', usdt_available_balance='109.50')

    def sync_time(self):
        pass

    def signed_get(self, path):
        if path != '/fapi/v3/account':
            raise AssertionError('Unexpected fixture account endpoint')
        self.calls.append(('GET', path))
        return dict(positions=copy.deepcopy(self.positions))

    def position(self, symbol, amount):
        self.positions.append(dict(symbol=symbol, positionSide='BOTH', positionAmt=str(amount)))
