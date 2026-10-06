# Deployment source: replace only the audited VPS OrderGateway class.
# This file is never imported by the worker or selected as another executor.
class OrderGateway(_MissingOrderImplementation):
    """Fixed-origin USD-M entry and quantity-limited protection adapter.

    Construction and status are local. An exchange result is reported only after
    strict GET proofs. Unknown outcomes stop at the coordinator's NEEDS_REVIEW;
    neither transport nor lifecycle code retries an entry POST.
    """
    class _ExchangeError(Review):
        def __init__(self, code=None):
            super().__init__('BINANCE_ORDER_REQUEST_FAILED')
            self.code = code

    class _Resample(Review):
        pass

    def __init__(self, api_key: str = '', api_secret: str = '',
                 base_url: str = 'https://fapi.binance.com'):
        import os
        from .binance_private import read_secret, valid_secret
        if base_url != 'https://fapi.binance.com':
            raise Review('BINANCE_ORDER_ORIGIN_DENIED')
        self.base_url = base_url
        if os.getenv('BINANCE_API_KEY_FILE') or os.getenv('BINANCE_API_SECRET_FILE'):
            try:
                key = read_secret('BINANCE_API_KEY_FILE')
                secret = read_secret('BINANCE_API_SECRET_FILE')
            except Exception:
                raise Review('BINANCE_ORDER_NOT_CONFIGURED') from None
        elif api_key or api_secret:
            key, secret = api_key, api_secret
        else:
            key = os.getenv('BINANCE_API_KEY', '')
            secret = os.getenv('BINANCE_API_SECRET', '')
        if not (key or secret):
            self.api_key = self.api_secret = ''
            self._connected = False
        elif not valid_secret(key) or not valid_secret(secret):
            raise Review('BINANCE_ORDER_NOT_CONFIGURED')
        else:
            self.api_key, self.api_secret = key, secret
            self._connected = True

    def _operation(self):
        import time
        from contextlib import contextmanager
        @contextmanager
        def scope():
            outer = getattr(self, '_deadline', None)
            if outer is None:
                self._deadline = time.monotonic()+120
                self._request_count = 0
                self._cleanup = False
                self._cancel_attempted = set()
            try:
                yield
            finally:
                if outer is None:
                    self._deadline = None
                    self._request_count = 0
                    self._cleanup = False
        return scope()

    def _time_left(self, reserve=0):
        import time
        deadline = getattr(self, '_deadline', None)
        if deadline is None:
            return 15
        remaining = deadline-time.monotonic()-(0 if getattr(self, '_cleanup', False) else 30)-reserve
        if remaining <= 0 or getattr(self, '_request_count', 0) >= (256 if getattr(self, '_cleanup', False) else 252):
            raise Review('BINANCE_ORDER_CALLBACK_DEADLINE')
        return remaining

    def _cleanup_entry(self, intent, entry):
        previous = getattr(self, '_cleanup', False)
        self._cleanup = True
        try:
            return self._cancel_entry_remainder(intent, entry)
        finally:
            self._cleanup = previous

    def status(self) -> dict:
        return dict(status='CONFIGURED' if self._connected else 'NOT_CONFIGURED',
                    connected=self._connected,
                    failure_code=None if self._connected else 'BINANCE_ORDER_NOT_CONFIGURED',
                    observed_at=datetime.now(timezone.utc).isoformat())

    def _sign(self, query_string: str) -> str:
        return hmac.new(self.api_secret.encode('ascii'),
                        query_string.encode('ascii'), hashlib.sha256).hexdigest()

    def _wire(self, method, path, query='', signed=False):
        """No redirects, proxy credentials, automatic retry, or raw-error output."""
        import json
        from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
        from urllib.error import HTTPError
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        if self.base_url != 'https://fapi.binance.com':
            raise Review('BINANCE_ORDER_ORIGIN_DENIED')
        timeout = min(15, self._time_left())
        self._request_count = getattr(self, '_request_count', 0)+1
        url = self.base_url + path
        headers = {'Accept': 'application/json', 'User-Agent': 'harun-office/1.0'}
        if signed:
            headers['X-MBX-APIKEY'] = self.api_key
        body = None
        if method == 'POST':
            body = query.encode('ascii')
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif query:
            url += '?' + query
        try:
            request = Request(url, data=body, headers=headers, method=method)
            opener = build_opener(ProxyHandler({}), NoRedirect())
            try:
                response = opener.open(request, timeout=timeout)
            except HTTPError as error:
                response = error
            with response:
                chunks = []
                total = 0
                read_chunk = getattr(response, 'read1', response.read)
                while True:
                    self._time_left()
                    chunk = read_chunk(min(65536, 2_000_001-total))
                    if not chunk:
                        break
                    chunks.append(chunk); total += len(chunk)
                    if total > 2_000_000:
                        raise ValueError()
                self._time_left()
                raw = b''.join(chunks)
                data = json.loads(raw, parse_float=str, object_pairs_hook=unique)
                code = data.get('code') if isinstance(data, dict) else None
                if response.code != 200 or (type(code) is int and code < 0):
                    raise self._ExchangeError(code if type(code) is int else None)
                if not isinstance(data, (dict, list)):
                    raise ValueError()
                return data
        except self._ExchangeError:
            raise
        except Exception:
            raise Review('BINANCE_ORDER_OUTCOME_UNKNOWN') from None

    def _get_server_time(self) -> int:
        data = self._wire('GET', '/fapi/v1/time')
        value = data.get('serverTime') if isinstance(data, dict) else None
        if type(value) is not int or not 0 < value < 10**16:
            raise Review('BINANCE_ORDER_CLOCK_INVALID')
        return value

    def _request(self, method: str, path: str, params: dict):
        from urllib.parse import urlencode
        self._time_left()
        allowed = {
            'GET': {'/fapi/v1/accountConfig', '/fapi/v3/account', '/fapi/v3/positionRisk',
                    '/fapi/v1/openOrders', '/fapi/v1/openAlgoOrders', '/fapi/v1/symbolConfig',
                    '/fapi/v1/leverageBracket', '/fapi/v1/commissionRate', '/fapi/v1/order',
                    '/fapi/v1/algoOrder', '/fapi/v1/allAlgoOrders', '/fapi/v1/userTrades'},
            'POST': {'/fapi/v1/marginType', '/fapi/v1/leverage', '/fapi/v1/order', '/fapi/v1/algoOrder'},
            'DELETE': {'/fapi/v1/order', '/fapi/v1/algoOrder'},
        }
        if not self._connected:
            raise Review('BINANCE_ORDER_NOT_CONFIGURED')
        if method not in allowed or path not in allowed[method] or not isinstance(params, dict):
            raise Review('BINANCE_ORDER_ENDPOINT_DENIED')
        if any(not isinstance(k, str) or not isinstance(v, (str, int)) or isinstance(v, bool)
               for k, v in params.items()) or {'timestamp', 'signature', 'recvWindow'} & set(params):
            raise Review('BINANCE_ORDER_PARAMETERS_INVALID')
        values = dict(params, timestamp=self._get_server_time(), recvWindow=5000)
        query = urlencode(sorted(values.items()))
        return self._wire(method, path, query + '&signature=' + self._sign(query), True)

    def _decimal(self, value, *, positive=False):
        import re
        if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?', value):
            raise Review('BINANCE_ORDER_PROOF_INVALID')
        result = Decimal(value)
        if not result.is_finite() or (positive and result <= 0):
            raise Review('BINANCE_ORDER_PROOF_INVALID')
        return result

    def _id(self, value):
        import re
        if isinstance(value, bool):
            raise Review('BINANCE_ORDER_PROOF_INVALID')
        text = str(value)
        if not re.fullmatch(r'[1-9][0-9]{0,29}', text):
            raise Review('BINANCE_ORDER_PROOF_INVALID')
        return text

    def _quantity_text(self, quantity):
        return format(quantity, 'f').rstrip('0').rstrip('.') if '.' in format(quantity, 'f') else format(quantity, 'f')

    def _validate_intent(self, intent):
        import re
        from fractions import Fraction
        try:
            if not isinstance(intent, dict):
                raise ValueError()
            symbol, client = intent['symbol'], intent['client_order_id']
            if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9]{2,18}USDT', symbol) or symbol == 'HYPEUSDT':
                raise ValueError()
            if not isinstance(client, str) or not re.fullmatch(r'hao-[a-f0-9]{28}', client):
                raise ValueError()
            operation = intent['intent_id']
            if not isinstance(operation, str) or client != 'hao-' + hashlib.sha256(operation.encode()).hexdigest()[:28]:
                raise ValueError()
            if intent['position_side'] != 'BOTH' or intent['margin_mode'] != 'CROSS' or type(intent['leverage']) is not int or intent['leverage'] != 75:
                raise ValueError()
            if intent['side'] not in ('LONG', 'SHORT'):
                raise ValueError()
            entry, protection = intent['entry'], intent['protection']
            side = 'BUY' if intent['side'] == 'LONG' else 'SELL'
            if entry['side'] != side or entry['order_type'] != 'LIMIT' or entry['time_in_force'] != 'GTC':
                raise ValueError()
            if protection['exit_side'] != ('SELL' if side == 'BUY' else 'BUY') or protection['working_type'] != 'MARK_PRICE':
                raise ValueError()
            e, q, sl, tp = (Fraction(self._decimal(v, positive=True)) for v in
                             (entry['price'], entry['quantity'], protection['stop_loss'], protection['take_profit']))
            if not (sl < e < tp if side == 'BUY' else tp < e < sl):
                raise ValueError()
            fees = intent['fee_evidence']
            rate = Fraction(self._decimal(fees['taker_rate']))
            if fees['source'] != 'BINANCE_FUTURES_COMMISSION_RATE' or fees['symbol'] != symbol or not 0 <= rate < 1:
                raise ValueError()
            target = Fraction(validated_risk_target(intent['risk_target_usdt']))
            gross = q * abs(e - sl)
            entry_fee, sl_fee, tp_fee = q*e*rate, q*sl*rate, q*tp*rate
            risk, reward = gross + entry_fee + sl_fee, q*abs(tp-e) - entry_fee - tp_fee
            expected = {'risk_usdt': risk, 'gross_risk_usdt': gross,
                        'entry_fee_usdt': entry_fee, 'sl_exit_fee_usdt': sl_fee,
                        'tp_exit_fee_usdt': tp_fee, 'net_reward_usdt': reward}
            if not 0 < risk <= target or reward < 2*risk:
                raise ValueError()
            if any(Fraction(self._decimal(intent[key])) != val for key, val in expected.items()):
                raise ValueError()
            ratio = intent['net_reward_risk']
            if not isinstance(ratio, str) or len(ratio) > 256 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:E[+-]?[0-9]{1,3})?', ratio):
                raise ValueError()
            if not Decimal(ratio).is_finite() or Fraction(Decimal(ratio)) < 2 or intent['excluded_costs'] != ['SLIPPAGE', 'FUNDING', 'GAPS']:
                raise ValueError()
            if not re.fullmatch(r'[a-f0-9]{64}', intent['evidence_sha256']):
                raise ValueError()
        except Exception:
            raise Review('BINANCE_ORDER_INTENT_INVALID') from None

    def _list(self, data):
        if not isinstance(data, list) or len(data) > 10_000 or any(not isinstance(row, dict) for row in data):
            raise Review('BINANCE_ORDER_PROOF_INVALID')
        return data

    def _account_config(self):
        data = self._request('GET', '/fapi/v1/accountConfig', {})
        if not isinstance(data, dict) or data.get('canTrade') is not True or data.get('dualSidePosition') is not False or data.get('multiAssetsMargin') is not False:
            raise Review('BINANCE_ORDER_ACCOUNT_MODE_INVALID')

    def _preflight(self, intent):
        from fractions import Fraction
        self._account_config()
        symbol = intent['symbol']
        positions = self._list(self._request('GET', '/fapi/v3/positionRisk', {}))
        symbols = set()
        for row in positions:
            if row.get('positionSide') != 'BOTH' or not isinstance(row.get('symbol'), str):
                raise Review('BINANCE_ORDER_PROOF_INVALID')
            if self._decimal(row.get('positionAmt')):
                symbols.add(row['symbol'])
        regular = self._list(self._request('GET', '/fapi/v1/openOrders', {}))
        algos = self._list(self._request('GET', '/fapi/v1/openAlgoOrders', {}))
        for row in regular + algos:
            if not isinstance(row.get('symbol'), str) or row.get('positionSide') != 'BOTH' or type(row.get('reduceOnly')) is not bool:
                raise Review('BINANCE_ORDER_PROOF_INVALID')
            if row['symbol'] == symbol:
                raise Review('BINANCE_ORDER_SYMBOL_OCCUPIED')
            if not row['reduceOnly'] and row.get('closePosition') is not True:
                symbols.add(row['symbol'])
        if symbol in symbols or len(symbols) >= 2:
            raise Review('BINANCE_ORDER_CAPACITY_FULL')
        # A known client ID is never POSTed again, even after a restart.
        try:
            self._request('GET', '/fapi/v1/order', {'symbol': symbol, 'origClientOrderId': intent['client_order_id']})
        except self._ExchangeError as error:
            if error.code != -2013:
                raise
        else:
            raise Review('BINANCE_ORDER_ENTRY_ALREADY_EXISTS')
        fee = self._request('GET', '/fapi/v1/commissionRate', {'symbol': symbol})
        if not isinstance(fee, dict) or fee.get('symbol') != symbol or not 0 <= self._decimal(fee.get('takerCommissionRate')) <= self._decimal(intent['fee_evidence']['taker_rate']):
            raise Review('BINANCE_ORDER_FEE_CHANGED')
        account = self._request('GET', '/fapi/v3/account', {})
        if not isinstance(account, dict):
            raise Review('BINANCE_ORDER_PROOF_INVALID')
        available = Fraction(self._decimal(account.get('availableBalance')))
        notional = Fraction(self._decimal(intent['entry']['quantity'])) * Fraction(self._decimal(intent['entry']['price']))
        if available < notional / 75 + Fraction(self._decimal(intent['entry_fee_usdt'])):
            raise Review('BINANCE_ORDER_MARGIN_INSUFFICIENT')
        self._bracket(intent)

    def _bracket(self, intent):
        from fractions import Fraction
        payload = self._request('GET', '/fapi/v1/leverageBracket', {'symbol': intent['symbol']})
        rows = self._list([payload] if isinstance(payload, dict) else payload)
        matching = [r for r in rows if r.get('symbol') == intent['symbol']]
        if len(matching) != 1:
            raise Review('BINANCE_ORDER_LEVERAGE_UNSUPPORTED')
        data = matching[0]
        # Use returned account-specific caps; also confirm the configured75x
        # maxNotionalValue after the exchange changes leverage.
        notional = Fraction(self._decimal(intent['entry']['price'])) * Fraction(self._decimal(intent['entry']['quantity']))
        brackets = self._list(data.get('brackets'))
        selected = []
        for row in brackets:
            floor = row.get('notionalFloor')
            cap = row.get('notionalCap')
            if isinstance(floor, bool) or isinstance(cap, bool) or not isinstance(floor, (str, int)) or not isinstance(cap, (str, int)):
                raise Review('BINANCE_ORDER_PROOF_INVALID')
            low, high = self._decimal(str(floor)), self._decimal(str(cap), positive=True)
            if low < 0 or high <= low:
                raise Review('BINANCE_ORDER_PROOF_INVALID')
            if Fraction(low) <= notional < Fraction(high):
                selected.append(row)
        if len(selected) != 1 or type(selected[0].get('initialLeverage')) is not int or selected[0]['initialLeverage'] < 75:
            raise Review('BINANCE_ORDER_LEVERAGE_UNSUPPORTED')

    def _configure(self, intent):
        from fractions import Fraction
        symbol = intent['symbol']
        notional = Fraction(self._decimal(intent['entry']['price'])) * Fraction(self._decimal(intent['entry']['quantity']))
        try:
            result = self._request('POST', '/fapi/v1/marginType', {'symbol': symbol, 'marginType': 'CROSSED'})
        except self._ExchangeError as error:
            if error.code != -4046:  # Explicit exchange response: no change needed.
                raise
        else:
            if not isinstance(result, dict) or result.get('code') != 200:
                raise Review('BINANCE_ORDER_MARGIN_CONFIG_INVALID')
        result = self._request('POST', '/fapi/v1/leverage', {'symbol': symbol, 'leverage': 75})
        if not isinstance(result, dict) or result.get('symbol') != symbol or type(result.get('leverage')) is not int or result['leverage'] != 75:
            raise Review('BINANCE_ORDER_LEVERAGE_CONFIG_INVALID')
        if notional > Fraction(self._decimal(result.get('maxNotionalValue'), positive=True)):
            raise Review('BINANCE_ORDER_LEVERAGE_UNSUPPORTED')
        rows = self._list(self._request('GET', '/fapi/v1/symbolConfig', {'symbol': symbol}))
        selected = [r for r in rows if r.get('symbol') == symbol]
        if len(selected) != 1 or selected[0].get('marginType') != 'CROSSED' or type(selected[0].get('leverage')) is not int or selected[0]['leverage'] != 75:
            raise Review('BINANCE_ORDER_CONFIG_NOT_CONFIRMED')
        if notional > Fraction(self._decimal(selected[0].get('maxNotionalValue'), positive=True)):
            raise Review('BINANCE_ORDER_LEVERAGE_UNSUPPORTED')
        self._bracket(intent)
        # Check again immediately before entry: user may have opened a position
        # or orders while configuration requests were in flight.
        self._preflight(intent)

    def submit(self, intent: dict) -> dict:
        with self._operation():
            return self._submit_once(intent)

    def _submit_once(self, intent):
        self._validate_intent(intent)
        self._preflight(intent)
        self._configure(intent)
        entry = intent['entry']
        self._time_left(reserve=30)
        self._request('POST', '/fapi/v1/order', {
            'symbol': intent['symbol'], 'side': entry['side'], 'positionSide': 'BOTH',
            'type': 'LIMIT', 'timeInForce': 'GTC', 'price': entry['price'],
            'quantity': entry['quantity'], 'reduceOnly': 'false',
            'newClientOrderId': intent['client_order_id'], 'newOrderRespType': 'RESULT'})
        return self.reconcile(intent)

    def _entry(self, intent):
        row = self._request('GET', '/fapi/v1/order',
                            {'symbol': intent['symbol'], 'origClientOrderId': intent['client_order_id']})
        cfg = intent['entry']
        if not isinstance(row, dict) or any(row.get(k) != v for k, v in
            {'symbol': intent['symbol'], 'clientOrderId': intent['client_order_id'],
             'side': cfg['side'], 'positionSide': 'BOTH', 'type': 'LIMIT',
             'timeInForce': 'GTC', 'reduceOnly': False, 'closePosition': False}.items()):
            raise Review('BINANCE_ORDER_ENTRY_PROOF_INVALID')
        self._id(row.get('orderId'))
        if self._decimal(row.get('price'), positive=True) != self._decimal(cfg['price']) or self._decimal(row.get('origQty'), positive=True) != self._decimal(cfg['quantity']):
            raise Review('BINANCE_ORDER_ENTRY_PROOF_INVALID')
        filled = self._decimal(row.get('executedQty'))
        if not 0 <= filled <= self._decimal(cfg['quantity']) or row.get('status') not in ('NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'):
            raise Review('BINANCE_ORDER_ENTRY_PROOF_INVALID')
        if row['status'] == 'FILLED' and filled != self._decimal(cfg['quantity']):
            raise Review('BINANCE_ORDER_ENTRY_PROOF_INVALID')
        return row

    def _trades(self, symbol, order_id, expected, side, since_ms):
        """Explicit seven-day windows from order creation; earliest own trade wins.

        order.time bounds history retrieval; it is never used as first_fill_at.
        Full pages and histories outside the exchange retention fail closed.
        """
        from fractions import Fraction
        server_ms = self._get_server_time()
        if type(since_ms) is not int or not 0 < since_ms <= server_ms or server_ms-since_ms >= 90*24*60*60*1000:
            raise Review('BINANCE_ORDER_HISTORY_INCOMPLETE')
        start = since_ms
        collected = []
        for _ in range(14):
            end = min(server_ms, start+7*24*60*60*1000-1)
            page = self._list(self._request('GET', '/fapi/v1/userTrades',
                             {'symbol': symbol, 'orderId': order_id, 'startTime': start,
                              'endTime': end, 'limit': 1000}))
            if len(page) >= 1000:
                raise Review('BINANCE_ORDER_HISTORY_INCOMPLETE')
            if any(type(row.get('time')) is not int or not start <= row['time'] <= end for row in page):
                raise Review('BINANCE_ORDER_FILL_PROOF_INVALID')
            collected.extend(page)
            if end == server_ms:
                break
            start = end+1
        else:
            raise Review('BINANCE_ORDER_HISTORY_INCOMPLETE')
        seen = set()
        total = Fraction(0)
        for row in collected:
            trade_id = self._id(row.get('id'))
            if trade_id in seen or self._id(row.get('orderId')) != str(order_id) or row.get('symbol') != symbol or row.get('side') != side or row.get('positionSide') != 'BOTH':
                raise Review('BINANCE_ORDER_FILL_PROOF_INVALID')
            seen.add(trade_id)
            total += Fraction(self._decimal(row.get('qty'), positive=True))
        if total != Fraction(expected) or (expected and not collected):
            raise Review('BINANCE_ORDER_HISTORY_INCOMPLETE')
        return collected

    def _algo_id(self, intent, kind, quantity):
        seed = intent['client_order_id'] + ':' + self._quantity_text(quantity)
        return 'hao-' + kind + '-' + hashlib.sha256(seed.encode('ascii')).hexdigest()[:24]

    def _algo_proof(self, intent, kind, quantity, row, *, active=False):
        expected = dict(symbol=intent['symbol'], algoType='CONDITIONAL',
                        orderType='STOP_MARKET' if kind == 'sl' else 'TAKE_PROFIT_MARKET',
                        side=intent['protection']['exit_side'], positionSide='BOTH',
                        workingType='MARK_PRICE', reduceOnly=True, closePosition=False,
                        clientAlgoId=self._algo_id(intent, kind, quantity))
        if not isinstance(row, dict) or any(row.get(k) != v for k, v in expected.items()):
            raise Review('BINANCE_ORDER_PROTECTION_PROOF_INVALID')
        self._id(row.get('algoId'))
        trigger = intent['protection']['stop_loss' if kind == 'sl' else 'take_profit']
        if self._decimal(row.get('quantity'), positive=True) != quantity or self._decimal(row.get('triggerPrice'), positive=True) != self._decimal(trigger):
            raise Review('BINANCE_ORDER_PROTECTION_PROOF_INVALID')
        if row.get('algoStatus') not in ('NEW', 'CANCELED', 'EXPIRED', 'REJECTED', 'TRIGGERED', 'FINISHED') or (active and row['algoStatus'] != 'NEW'):
            raise Review('BINANCE_ORDER_PROTECTION_PROOF_INVALID')
        return row

    def _owned_algos(self, intent, first_ms):
        """Complete seven-day windows; truncated/aged history requires review."""
        now = self._get_server_time()
        if not 0 <= now-first_ms < 90*24*60*60*1000:
            raise Review('BINANCE_ORDER_HISTORY_INCOMPLETE')
        start = max(0, first_ms-1000)
        found = {}
        for _ in range(14):
            end = min(now, start+7*24*60*60*1000-1)
            rows = self._list(self._request('GET', '/fapi/v1/allAlgoOrders',
                            {'symbol': intent['symbol'], 'startTime': start, 'endTime': end, 'limit': 1000}))
            if len(rows) >= 1000:
                raise Review('BINANCE_ORDER_HISTORY_INCOMPLETE')
            for row in rows:
                client = row.get('clientAlgoId', '')
                if not isinstance(client, str):
                    raise Review('BINANCE_ORDER_PROOF_INVALID')
                if client.startswith(('hao-sl-', 'hao-tp-')):
                    kind = 'sl' if client.startswith('hao-sl-') else 'tp'
                    quantity = self._decimal(row.get('quantity'), positive=True)
                    if client == self._algo_id(intent, kind, quantity):
                        proof = self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': client})
                        self._algo_proof(intent, kind, quantity, proof)
                        if client in found and found[client] != proof:
                            raise Review('BINANCE_ORDER_HISTORY_INCOMPLETE')
                        found[client] = proof
            if end == now:
                return list(found.values())
            start = end+1
        raise Review('BINANCE_ORDER_HISTORY_INCOMPLETE')

    def _exits(self, intent, algos):
        from fractions import Fraction
        quantity = Fraction(0)
        rows = []
        active = []
        seen = set()
        for algo in algos:
            actual = algo.get('actualOrderId', '')
            if actual in ('', '0', 0, None):
                continue
            oid = self._id(actual)
            if oid in seen:
                raise Review('BINANCE_ORDER_EXIT_PROOF_INVALID')
            seen.add(oid)
            row = self._request('GET', '/fapi/v1/order', {'symbol': intent['symbol'], 'orderId': oid})
            if not isinstance(row, dict) or self._id(row.get('orderId')) != oid or row.get('symbol') != intent['symbol'] or row.get('side') != intent['protection']['exit_side'] or row.get('positionSide') != 'BOTH' or row.get('reduceOnly') is not True or row.get('closePosition') is not False or row.get('type') not in ('MARKET', algo['orderType']):
                raise Review('BINANCE_ORDER_EXIT_PROOF_INVALID')
            if self._decimal(row.get('origQty'), positive=True) != self._decimal(algo['quantity']):
                raise Review('BINANCE_ORDER_EXIT_PROOF_INVALID')
            filled = self._decimal(row.get('executedQty'))
            if not 0 <= filled <= self._decimal(algo['quantity']) or row.get('status') not in ('NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'):
                raise Review('BINANCE_ORDER_EXIT_PROOF_INVALID')
            if row['status'] == 'FILLED' and filled != self._decimal(algo['quantity']):
                raise Review('BINANCE_ORDER_EXIT_PROOF_INVALID')
            if row['status'] in ('NEW', 'PARTIALLY_FILLED'):
                active.append(row)
            if filled:
                trades = self._trades(intent['symbol'], oid, filled, intent['protection']['exit_side'], row.get('time'))
                rows.extend(trades)
                quantity += Fraction(filled)
        return quantity, rows, active

    def _inventory(self, intent, entry, algos):
        own_exit_ids = {self._id(a['actualOrderId']) for a in algos if a.get('actualOrderId') not in ('', '0', 0, None)}
        regular = self._list(self._request('GET', '/fapi/v1/openOrders', {'symbol': intent['symbol']}))
        for row in regular:
            if row.get('symbol') != intent['symbol']:
                raise Review('BINANCE_ORDER_PROOF_INVALID')
            oid = self._id(row.get('orderId'))
            if oid == self._id(entry['orderId']) and row.get('clientOrderId') == intent['client_order_id']:
                continue
            if oid in own_exit_ids and row.get('side') == intent['protection']['exit_side'] and row.get('positionSide') == 'BOTH' and row.get('reduceOnly') is True and row.get('closePosition') is False:
                continue
            raise Review('BINANCE_ORDER_FOREIGN_PENDING_ORDER')
        known = {a['clientAlgoId']: a for a in algos}
        opened = self._list(self._request('GET', '/fapi/v1/openAlgoOrders', {'symbol': intent['symbol']}))
        for row in opened:
            client = row.get('clientAlgoId')
            if client not in known:
                raise Review('BINANCE_ORDER_FOREIGN_PENDING_ORDER')
            kind = 'sl' if client.startswith('hao-sl-') else 'tp'
            self._algo_proof(intent, kind, self._decimal(known[client]['quantity'], positive=True), row, active=True)

    def _cancel_exit_remainder(self, intent, row):
        oid = self._id(row['orderId'])
        self._request('DELETE', '/fapi/v1/order', {'symbol': intent['symbol'], 'orderId': oid})
        after = self._request('GET', '/fapi/v1/order', {'symbol': intent['symbol'], 'orderId': oid})
        if not isinstance(after, dict) or self._id(after.get('orderId')) != oid or after.get('symbol') != intent['symbol'] or after.get('side') != intent['protection']['exit_side'] or after.get('positionSide') != 'BOTH' or after.get('reduceOnly') is not True or after.get('closePosition') is not False or after.get('status') not in ('CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'FILLED'):
            raise Review('BINANCE_ORDER_EXIT_CANCEL_OUTCOME_UNKNOWN')
        if self._decimal(after.get('executedQty')) != self._decimal(row['executedQty']):
            raise self._Resample('BINANCE_ORDER_EXIT_RESAMPLE')

    def _ownership(self, intent, entry_trades, exit_trades, remaining):
        """Reject every intervening foreign trade, including manual reopenings."""
        from fractions import Fraction
        own = {str(r['id']) for r in entry_trades+exit_trades}
        seen = set()
        now = self._get_server_time()
        start = min(r['time'] for r in entry_trades)
        if not 0 <= now-start < 90*24*60*60*1000:
            raise Review('BINANCE_ORDER_HISTORY_INCOMPLETE')
        for _ in range(14):
            end = min(now, start+7*24*60*60*1000-1)
            rows = self._list(self._request('GET', '/fapi/v1/userTrades',
                            {'symbol': intent['symbol'], 'startTime': start,
                             'endTime': end, 'limit': 1000}))
            if len(rows) >= 1000:
                raise Review('BINANCE_ORDER_HISTORY_INCOMPLETE')
            for row in rows:
                tid = self._id(row.get('id'))
                if row.get('symbol') != intent['symbol'] or type(row.get('time')) is not int or not start <= row['time'] <= end or tid in seen or tid not in own:
                    raise Review('BINANCE_ORDER_FOREIGN_EXPOSURE')
                seen.add(tid)
            if end == now:
                break
            start = end+1
        else:
            raise Review('BINANCE_ORDER_HISTORY_INCOMPLETE')
        if seen != own:
            raise Review('BINANCE_ORDER_HISTORY_INCOMPLETE')
        positions = self._list(self._request('GET', '/fapi/v3/positionRisk', {'symbol': intent['symbol']}))
        rows = [r for r in positions if r.get('symbol') == intent['symbol']]
        # V3 omits contracts with no position and no open order.
        if not rows and not remaining:
            return
        if len(rows) != 1 or rows[0].get('positionSide') != 'BOTH':
            raise Review('BINANCE_ORDER_FOREIGN_EXPOSURE')
        amount = Fraction(self._decimal(rows[0].get('positionAmt')))
        expected = remaining if intent['side'] == 'LONG' else -remaining
        if amount != expected:
            raise Review('BINANCE_ORDER_FOREIGN_EXPOSURE')
        liq = self._decimal(rows[0].get('liquidationPrice'))
        sl = self._decimal(intent['protection']['stop_loss'])
        if remaining and liq and not (liq < sl if intent['side'] == 'LONG' else liq > sl):
            raise Review('BINANCE_ORDER_LIQUIDATION_BEFORE_SL')

    def _ensure_algo(self, intent, kind, quantity):
        client = self._algo_id(intent, kind, quantity)
        try:
            proof = self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': client})
        except self._ExchangeError as error:
            if error.code != -2013:
                raise
        else:
            return self._algo_proof(intent, kind, quantity, proof, active=True)
        self._request('POST', '/fapi/v1/algoOrder', {
            'algoType': 'CONDITIONAL', 'symbol': intent['symbol'],
            'side': intent['protection']['exit_side'], 'positionSide': 'BOTH',
            'type': 'STOP_MARKET' if kind == 'sl' else 'TAKE_PROFIT_MARKET',
            'quantity': self._quantity_text(quantity), 'reduceOnly': 'true',
            'triggerPrice': intent['protection']['stop_loss' if kind == 'sl' else 'take_profit'],
            'workingType': 'MARK_PRICE', 'clientAlgoId': client,
            'newOrderRespType': 'ACK'})
        proof = self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': client})
        return self._algo_proof(intent, kind, quantity, proof, active=True)

    def _cancel_algo(self, intent, algo):
        kind = 'sl' if algo['clientAlgoId'].startswith('hao-sl-') else 'tp'
        quantity = self._decimal(algo['quantity'], positive=True)
        current = self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': algo['clientAlgoId']})
        self._algo_proof(intent, kind, quantity, current)
        if current['algoStatus'] != 'NEW':
            if current['algoStatus'] not in ('CANCELED', 'EXPIRED', 'REJECTED') or current.get('actualOrderId') not in ('', '0', 0, None):
                raise Review('BINANCE_ORDER_EXIT_RACE')
            return
        self._request('DELETE', '/fapi/v1/algoOrder', {'algoId': self._id(current['algoId'])})
        after = self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': algo['clientAlgoId']})
        self._algo_proof(intent, kind, quantity, after)
        if after['algoStatus'] != 'CANCELED' or after.get('actualOrderId') not in ('', '0', 0, None):
            raise Review('BINANCE_ORDER_EXIT_RACE')

    def _cancel_entry_remainder(self, intent, entry):
        if entry['status'] not in ('NEW', 'PARTIALLY_FILLED'):
            return entry
        attempted = getattr(self, '_cancel_attempted', set())
        client = intent['client_order_id']
        if client not in attempted:
            attempted.add(client)
            self._cancel_attempted = attempted
            self._request('DELETE', '/fapi/v1/order',
                          {'symbol': intent['symbol'], 'origClientOrderId': client})
        after = self._entry(intent)
        if after['status'] not in ('CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'FILLED') or self._decimal(after['executedQty']) < self._decimal(entry['executedQty']):
            raise Review('BINANCE_ORDER_ENTRY_CANCEL_RACE')
        return after

    def reconcile(self, intent: dict) -> dict:
        with self._operation():
            self._validate_intent(intent)
            self._account_config()
            for _ in range(4):
                self._time_left()
                try:
                    return self._reconcile_once(intent)
                except self._Resample:
                    continue
            raise Review('BINANCE_ORDER_LIFECYCLE_UNSTABLE')

    def _reconcile_once(self, intent):
        from fractions import Fraction
        entry = self._entry(intent)
        filled = self._decimal(entry['executedQty'])
        result = dict(source='BINANCE_FUTURES', symbol=intent['symbol'],
                      client_order_id=intent['client_order_id'], order_id=self._id(entry['orderId']),
                      filled_quantity=entry['executedQty'], sl_confirmed=False, tp_confirmed=False)
        try:
            if not filled:
                self._inventory(intent, entry, [])
                positions = self._list(self._request('GET', '/fapi/v3/positionRisk', {'symbol': intent['symbol']}))
                fresh = self._entry(intent)
                if self._decimal(fresh['executedQty']) > 0:
                    self._cleanup_entry(intent, fresh)
                    raise self._Resample('BINANCE_ORDER_FILL_RESAMPLE')
                if any(self._decimal(row.get('positionAmt')) for row in positions):
                    raise Review('BINANCE_ORDER_FOREIGN_EXPOSURE')
                result['state'] = 'ENTRY_PENDING' if fresh['status'] in ('NEW', 'PARTIALLY_FILLED') else 'REJECTED'
                result['observed_at'] = datetime.now(timezone.utc).isoformat()
                return result
            return self._reconcile_filled(intent, entry, result)
        except self._Resample:
            raise
        except Exception:
            # Even an early history/read failure must not leave a verified own
            # LIMIT remainder able to fill after this callback stops. This also
            # covers a first fill racing a previously zero-filled snapshot.
            after = self._cleanup_entry(intent, entry)
            if self._decimal(after['executedQty']) > filled:
                raise self._Resample('BINANCE_ORDER_FILL_RESAMPLE')
            raise

    def _reconcile_filled(self, intent, entry, result):
        from fractions import Fraction
        filled = self._decimal(entry['executedQty'])
        trades = self._trades(intent['symbol'], self._id(entry['orderId']), filled, intent['entry']['side'], entry.get('time'))
        first_ms = min(r['time'] for r in trades)
        algos = self._owned_algos(intent, first_ms)
        self._inventory(intent, entry, algos)
        triggered = [a for a in algos if a['algoStatus'] in ('TRIGGERED', 'FINISHED') or a.get('actualOrderId') not in ('', '0', 0, None)]
        if triggered:
            after = self._cancel_entry_remainder(intent, entry)
            if self._decimal(after['executedQty']) != filled:
                raise self._Resample('BINANCE_ORDER_FILL_RESAMPLE')
            entry = after
            if any(a.get('actualOrderId') in ('', '0', 0, None) for a in triggered):
                raise Review('BINANCE_ORDER_EXIT_OUTCOME_UNKNOWN')
        exited, exit_trades, active_exits = self._exits(intent, algos)
        if not 0 <= exited <= Fraction(filled):
            raise Review('BINANCE_ORDER_EXIT_PROOF_INVALID')
        if exited:
            after = self._cancel_entry_remainder(intent, entry)
            if self._decimal(after['executedQty']) != filled:
                raise self._Resample('BINANCE_ORDER_FILL_RESAMPLE')
            entry = after
        remaining = Fraction(filled)-exited
        self._ownership(intent, trades, exit_trades, remaining)
        for actual in active_exits:
            self._cancel_exit_remainder(intent, actual)
        result['first_fill_at'] = datetime.fromtimestamp(first_ms/1000, timezone.utc).isoformat()
        if not remaining:
            if not exit_trades:
                raise Review('BINANCE_ORDER_EXIT_PROOF_INVALID')
            for algo in algos:
                if algo['algoStatus'] == 'NEW':
                    self._cancel_algo(intent, algo)
            last = max(exit_trades, key=lambda r: (r['time'], int(r['id'])))
            result.update(state='CLOSED', exit_order_id=self._id(last['orderId']),
                          closed_at=datetime.fromtimestamp(last['time']/1000, timezone.utc).isoformat())
        else:
            # Fraction from finite decimal arithmetic has a finite exact Decimal.
            from decimal import localcontext
            with localcontext() as context:
                context.prec = 256
                quantity = Decimal(remaining.numerator)/Decimal(remaining.denominator)
            try:
                sl = self._ensure_algo(intent, 'sl', quantity)
                tp = self._ensure_algo(intent, 'tp', quantity)
                fresh_algos = self._owned_algos(intent, first_ms)
                old_triggered = {a['clientAlgoId'] for a in triggered}
                fresh_triggered = {a['clientAlgoId'] for a in fresh_algos if a['algoStatus'] in ('TRIGGERED', 'FINISHED') or a.get('actualOrderId') not in ('', '0', 0, None)}
                fresh_exited, _, fresh_active = self._exits(intent, fresh_algos)
                final_entry = self._entry(intent)
                if fresh_triggered-old_triggered or fresh_exited != exited or fresh_active or self._decimal(final_entry['executedQty']) != filled:
                    self._cancel_entry_remainder(intent, final_entry)
                    raise self._Resample('BINANCE_ORDER_FILL_RESAMPLE')
                # Both legs must still be NEW on this fresh read, before older
                # quantity versions are removed. Cancellation targets only IDs
                # derived from this entry and exact verified quantity.
                proof_by_id = {a['clientAlgoId']: a for a in fresh_algos}
                sl = self._algo_proof(intent, 'sl', quantity, proof_by_id.get(sl['clientAlgoId']), active=True)
                tp = self._algo_proof(intent, 'tp', quantity, proof_by_id.get(tp['clientAlgoId']), active=True)
                current = {sl['clientAlgoId'], tp['clientAlgoId']}
                for algo in fresh_algos:
                    if algo['algoStatus'] == 'NEW' and algo['clientAlgoId'] not in current:
                        self._cancel_algo(intent, algo)
                self._ownership(intent, trades, exit_trades, remaining)
            except self._Resample:
                raise
            except Exception:
                # Never leave a proven own LIMIT remainder capable of adding
                # unprotected exposure after a protection callback fails.
                raise Review('BINANCE_ORDER_PROTECTION_OUTCOME_UNKNOWN') from None
            result.update(state='POSITION_PROTECTED', sl_confirmed=True, tp_confirmed=True,
                          sl_order_id=self._id(sl['algoId']), tp_order_id=self._id(tp['algoId']))
        result['observed_at'] = datetime.now(timezone.utc).isoformat()
        return result
