"""Bounded, public-only Binance USDT perpetual market observations.

This client never accepts an API key, arbitrary URL, HTTP verb or signed request.
Account state, commissions and leverage brackets belong to the existing backend.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Callable


class MarketUnavailable(RuntimeError):
    """No complete, current and internally consistent market observation exists."""


SYMBOL = re.compile(r"[A-Z0-9]{2,24}USDT\Z", re.ASCII)
INTERVAL_MS = {"1h": 3_600_000, "15m": 900_000}
PATHS = frozenset({"/fapi/v1/time", "/fapi/v1/exchangeInfo", "/fapi/v1/klines", "/fapi/v1/premiumIndex"})


def decimal_string(value: Any, *, positive: bool = True) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise MarketUnavailable("MARKET_NUMBER_INVALID")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise MarketUnavailable("MARKET_NUMBER_INVALID") from None
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise MarketUnavailable("MARKET_NUMBER_INVALID")
    if abs(result.adjusted()) > 30:
        raise MarketUnavailable("MARKET_NUMBER_OUT_OF_RANGE")
    return format(result, "f")


def require_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise MarketUnavailable("SYMBOL_NOT_USDT_PERPETUAL")
    return symbol


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MarketUnavailable("MARKET_REDIRECT_REFUSED")


class BinanceFuturesMarket:
    def __init__(self, *, timeout_seconds: float = 15, candle_limit: int = 120,
                 transport: Callable[[str, dict[str, Any]], Any] | None = None,
                 clock: Callable[[], float] = time.time):
        if not 1 <= timeout_seconds <= 15 or not 30 <= candle_limit <= 200:
            raise ValueError("MARKET_LIMIT_INVALID")
        self.timeout_seconds = timeout_seconds
        self.candle_limit = candle_limit
        self._transport = transport
        self._clock = clock
        self._opener = urllib.request.build_opener(_NoRedirect())
        self._remaining_budget = lambda: 120.0

    def set_remaining_budget(self, callback):
        if not callable(callback):
            raise MarketUnavailable("MARKET_BUDGET_CALLBACK_INVALID")
        self._remaining_budget = callback

    def _timeout(self):
        remaining = self._remaining_budget()
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not 0 < remaining <= 120:
            raise MarketUnavailable("MARKET_TICK_BUDGET_EXHAUSTED")
        return min(self.timeout_seconds, remaining)

    def _get(self, path: str, parameters: dict[str, Any]) -> Any:
        if path not in PATHS:
            raise MarketUnavailable("MARKET_ENDPOINT_REFUSED")
        expected = {"/fapi/v1/time": set(), "/fapi/v1/exchangeInfo": set(),
                    "/fapi/v1/premiumIndex": {"symbol"}, "/fapi/v1/klines": {"symbol", "interval", "limit"}}[path]
        if not isinstance(parameters, dict) or set(parameters) != expected:
            raise MarketUnavailable("MARKET_PARAMETERS_REFUSED")
        if "symbol" in parameters:
            require_symbol(parameters["symbol"])
        if "interval" in parameters and (parameters["interval"] not in INTERVAL_MS or type(parameters["limit"]) is not int or not 30 <= parameters["limit"] <= 200):
            raise MarketUnavailable("MARKET_PARAMETERS_REFUSED")
        timeout = self._timeout()
        if self._transport is not None:
            result = self._transport(path, dict(parameters))
            self._timeout()
            return result
        query = urllib.parse.urlencode(parameters)
        url = "https://fapi.binance.com" + path + ("?" + query if query else "")
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "harun-neurobro-agent/1"}, method="GET")
        try:
            deadline = time.monotonic() + timeout
            with self._opener.open(request, timeout=timeout) as response:
                if response.status != 200:
                    raise MarketUnavailable("MARKET_HTTP_ERROR")
                chunks, size = [], 0
                while size <= 8 * 1024 * 1024:
                    remaining = min(self._timeout(), deadline - time.monotonic())
                    if remaining <= 0:
                        raise MarketUnavailable("MARKET_HTTP_DEADLINE_EXHAUSTED")
                    # urllib's HTTPResponse exposes the actual socket here. read1
                    # bounds each blocking receive instead of accepting a trickle forever.
                    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if sock is not None:
                        sock.settimeout(remaining)
                    block = response.read1(min(16384, 8 * 1024 * 1024 + 1 - size))
                    if not block:
                        break
                    chunks.append(block)
                    size += len(block)
                raw = b"".join(chunks)
            if len(raw) > 8 * 1024 * 1024:
                raise MarketUnavailable("MARKET_RESPONSE_TOO_LARGE")
            return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (OSError, ValueError, urllib.error.URLError):
            raise MarketUnavailable("MARKET_TRANSPORT_UNAVAILABLE") from None

    def _server_time(self) -> int:
        raw = self._get("/fapi/v1/time", {})
        server = raw.get("serverTime") if isinstance(raw, dict) else None
        if type(server) is not int or abs(server / 1000 - self._clock()) > 15:
            raise MarketUnavailable("MARKET_CLOCK_UNVERIFIED")
        return server

    def exchange_info(self, symbol: str) -> dict[str, str]:
        symbol = require_symbol(symbol)
        result = self._get("/fapi/v1/exchangeInfo", {})
        rows = result.get("symbols") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise MarketUnavailable("SYMBOL_RULES_UNAVAILABLE")
        matches = [r for r in rows if isinstance(r, dict) and r.get("symbol") == symbol]
        if len(matches) != 1:
            raise MarketUnavailable("SYMBOL_RULES_AMBIGUOUS")
        item = matches[0]
        if (item.get("contractType"), item.get("status"), item.get("quoteAsset"), item.get("marginAsset")) != ("PERPETUAL", "TRADING", "USDT", "USDT"):
            raise MarketUnavailable("SYMBOL_NOT_TRADABLE_USDT_PERPETUAL")
        filters = item.get("filters")
        if not isinstance(filters, list) or any(not isinstance(f, dict) for f in filters):
            raise MarketUnavailable("SYMBOL_FILTERS_INVALID")
        indexed = {f.get("filterType"): f for f in filters}
        if len(indexed) != len(filters):
            raise MarketUnavailable("SYMBOL_FILTERS_AMBIGUOUS")
        try:
            price, lot = indexed["PRICE_FILTER"], indexed["LOT_SIZE"]
            notional = indexed.get("MIN_NOTIONAL", indexed.get("NOTIONAL"))
            if not isinstance(notional, dict):
                raise KeyError()
            output = {
                "tick_size": decimal_string(price["tickSize"]),
                "step_size": decimal_string(lot["stepSize"]),
                "min_qty": decimal_string(lot["minQty"]),
                "max_qty": decimal_string(lot["maxQty"]),
                "min_notional": decimal_string(notional.get("notional", notional.get("minNotional"))),
            }
            if Decimal(output["max_qty"]) < Decimal(output["min_qty"]):
                raise ValueError()
            return output
        except (KeyError, TypeError, ValueError):
            raise MarketUnavailable("SYMBOL_FILTERS_INVALID") from None

    def _candles(self, symbol: str, interval: str, server_ms: int) -> list[dict[str, Any]]:
        rows = self._get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": self.candle_limit})
        if not isinstance(rows, list) or not 30 <= len(rows) <= self.candle_limit:
            raise MarketUnavailable("CANDLE_COUNT_INVALID")
        result = []
        previous = None
        for row in rows:
            if not isinstance(row, list) or len(row) < 7 or type(row[0]) is not int or type(row[6]) is not int:
                raise MarketUnavailable("CANDLE_SCHEMA_INVALID")
            opened, closed = row[0], row[6]
            if closed != opened + INTERVAL_MS[interval] - 1 or opened < 0:
                raise MarketUnavailable("CANDLE_INTERVAL_INVALID")
            if previous is not None and opened - previous != INTERVAL_MS[interval]:
                raise MarketUnavailable("CANDLE_SEQUENCE_INVALID")
            values = [decimal_string(x) for x in row[1:5]]
            o, h, l, c = map(Decimal, values)
            if not (l <= min(o, c) <= max(o, c) <= h):
                raise MarketUnavailable("CANDLE_OHLC_INVALID")
            result.append(dict(open_time=opened, open=values[0], high=values[1], low=values[2], close=values[3], volume=decimal_string(row[5], positive=False), close_time=closed))
            previous = opened
        if not result[-1]["open_time"] <= server_ms <= result[-1]["close_time"] + 2000:
            raise MarketUnavailable("LATEST_CANDLE_NOT_CURRENT")
        return result

    def snapshot(self, symbol: str, timeframes: tuple[str, ...] = ("1h", "15m")) -> dict[str, Any]:
        symbol = require_symbol(symbol)
        if not isinstance(timeframes, tuple) or len(set(timeframes)) != len(timeframes) or set(timeframes) != set(INTERVAL_MS):
            raise MarketUnavailable("REQUIRED_TIMEFRAMES_INVALID")
        started = self._clock()
        server_ms = self._server_time()
        rules = self.exchange_info(symbol)
        candles = {interval: self._candles(symbol, interval, server_ms) for interval in timeframes}
        quote = self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        if not isinstance(quote, dict) or quote.get("symbol") != symbol or type(quote.get("time")) is not int:
            raise MarketUnavailable("MARK_QUOTE_INVALID")
        observed = self._clock()
        if observed - started > 90 or observed < started or abs(quote["time"] / 1000 - observed) > 180:
            raise MarketUnavailable("MARKET_OBSERVATION_STALE")
        mark = decimal_string(quote.get("markPrice"))
        raw_candles = json.dumps(candles, sort_keys=True, separators=(",", ":")).encode()
        return {
            "symbol": symbol, "observed_at": observed,
            "observed_at_utc": dt.datetime.fromtimestamp(observed, dt.timezone.utc).isoformat(),
            "server_time_ms": server_ms, "mark_price": mark, "mark_time_ms": quote["time"],
            "symbol_rules": rules, "candles": candles,
            "provenance": {"provider": "BINANCE_FUTURES_PUBLIC", "market": "USDT_PERPETUAL", "origin": "https://fapi.binance.com", "candles_sha256": hashlib.sha256(raw_candles).hexdigest(), "chart_kind": "CANDLESTICK_RENDER_FROM_PUBLIC_KLINES"},
        }
