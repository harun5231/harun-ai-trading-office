"""Regression coverage for the exact 400 CLOSED-candle dialogue contract.

All transports are in-memory public Binance schema fixtures. No account,
browser, Neurobro, model API, or order endpoint is called by these tests.
"""
import copy
import hashlib
import json
import unittest

from agent.market import BinanceFuturesMarket, MarketUnavailable
from agent.native_market_context import (
    NativeContextUnavailable, candle_digest, convert_native_market_context,
)


NOW = 1_792_164_832.0
INTERVALS = {"1h": 3_600_000, "15m": 900_000}


def kline_rows(server_ms, timeframe, count=202):
    interval = INTERVALS[timeframe]
    current = server_ms // interval * interval
    return [[current - (count - 1 - i) * interval, "64900.00", "65100.00",
             "64800.00", "65000.00", "12.500", current - (count - 1 - i) * interval + interval - 1]
            for i in range(count)]


def public_transport(path, parameters, *, now=NOW):
    if path == "/fapi/v1/time":
        return {"serverTime": int(now * 1000)}
    if path == "/fapi/v1/exchangeInfo":
        return {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING",
            "contractType": "PERPETUAL", "quoteAsset": "USDT", "marginAsset": "USDT",
            "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}
    if path == "/fapi/v1/premiumIndex":
        return {"symbol": "BTCUSDT", "markPrice": "65000.125", "time": int(now * 1000)}
    if path == "/fapi/v1/klines":
        return kline_rows(int(now * 1000), parameters["interval"], parameters["limit"])
    raise AssertionError("Unexpected endpoint")


def native_context_200(now=NOW, count=202):
    fields = ("open_time", "open", "high", "low", "close", "volume", "close_time")
    return {"source": "Binance Futures", "symbol": "BTCUSDT", "fetched_at": now,
        "server_time": int(now * 1000), "mark_price": {"price": "65000.125", "time": int(now * 1000)},
        "timeframes": {tf: {"symbol": "BTCUSDT", "timeframe": tf,
            "candles": [dict(zip(fields, row)) for row in kline_rows(int(now * 1000), tf, count)]}
            for tf in INTERVALS}}


class ClosedCandleMarketTests(unittest.TestCase):
    def market(self, mutate=None, *, limit=202):
        def transport(path, parameters):
            value = public_transport(path, parameters)
            return mutate(path, parameters, value) if mutate else value
        return BinanceFuturesMarket(transport=transport, clock=lambda: NOW, candle_limit=limit)

    def test_exact_400_historical_bars_and_running_bars_are_separate(self):
        for limit in (201, 202):
            result = self.market(limit=limit).snapshot("BTCUSDT")
            self.assertEqual(sum(map(len, result["candles"].values())), 400)
            self.assertEqual(result["provenance"]["candles_sha256"], candle_digest(result["candles"]))
            self.assertEqual(result["provenance"]["running_candles_sha256"], candle_digest(result["running_candles"]))
            for tf, interval in INTERVALS.items():
                rows = result["candles"][tf]
                active = result["running_candles"][tf]
                self.assertEqual(len(rows), 200)
                self.assertTrue(all(row["close_time"] < result["server_time_ms"] for row in rows))
                self.assertEqual(rows[-1]["close_time"] + 1, active["open_time"])
                self.assertEqual(rows[0]["open_time"], active["open_time"] - 200 * interval)
                self.assertNotIn(active, rows)
                self.assertEqual(result["candle_windows"][tf]["running_candle_state"], "OPEN_AT_CUTOFF")
            self.assertEqual(result["mark_price"], "65000.125")
            self.assertEqual(result["mark_time_ms"], int(NOW * 1000))

    def test_fixed_public_gets_and_requested_202_rows(self):
        calls = []
        def transport(path, parameters):
            calls.append((path, copy.deepcopy(parameters)))
            return public_transport(path, parameters)
        BinanceFuturesMarket(transport=transport, clock=lambda: NOW).snapshot("BTCUSDT")
        self.assertEqual(len(calls), 6)
        self.assertEqual([p["limit"] for path, p in calls if path.endswith("klines")], [202, 202])
        self.assertEqual([path for path, _ in calls].count("/fapi/v1/time"), 2)
        self.assertFalse(any(path.endswith("order") for path, _ in calls))
        for path, parameters in calls:
            self.assertNotIn("signature", parameters)
            self.assertNotIn("timestamp", parameters)

    def test_insufficient_history_and_missing_current_bar_are_rejected(self):
        for transform in (lambda rows: rows[-200:], lambda rows: rows[:-1]):
            def mutate(path, parameters, value):
                return transform(value) if path.endswith("klines") else value
            with self.assertRaises(MarketUnavailable):
                self.market(mutate).snapshot("BTCUSDT")

    def test_non_aligned_duplicate_gapped_or_reordered_bars_are_rejected(self):
        def shifted(rows):
            for row in rows:
                row[0] += 1
                row[6] += 1
            return rows
        transforms = (shifted, lambda rows: rows[:50] + [rows[49]] + rows[51:],
                      lambda rows: rows[::-1])
        for transform in transforms:
            def mutate(path, parameters, value):
                return transform(value) if path.endswith("klines") else value
            with self.assertRaises(MarketUnavailable):
                self.market(mutate).snapshot("BTCUSDT")

    def test_future_aligned_bar_cannot_be_ignored_with_unchanged_server_clock(self):
        def mutate(path, parameters, value):
            if path.endswith("klines"):
                return kline_rows(int(NOW * 1000) + INTERVALS[parameters["interval"]],
                                  parameters["interval"], parameters["limit"])
            return value
        with self.assertRaisesRegex(MarketUnavailable, "CANDLE_FUTURE_UNVERIFIED"):
            self.market(mutate).snapshot("BTCUSDT")

    def test_invalid_or_regressed_clock_is_classified(self):
        for clock in (lambda: float("nan"), lambda: float("inf"), lambda: True, lambda: 10 ** 13):
            with self.assertRaisesRegex(MarketUnavailable, "MARKET_CLOCK_UNVERIFIED"):
                BinanceFuturesMarket(transport=public_transport, clock=clock).snapshot("BTCUSDT")
        calls = []
        def transport(path, parameters):
            value = public_transport(path, parameters)
            if path.endswith("time"):
                calls.append(path)
                if len(calls) == 2:
                    value["serverTime"] -= 1
            return value
        with self.assertRaisesRegex(MarketUnavailable, "MARKET_CLOCK_REGRESSED"):
            BinanceFuturesMarket(transport=transport, clock=lambda: NOW).snapshot("BTCUSDT")

    def test_open_bar_values_do_not_change_historical_digest(self):
        first = self.market().snapshot("BTCUSDT")
        def mutate(path, parameters, value):
            if path.endswith("klines"):
                value[-1][4] = "65050.00"
                value[-1][5] = "13.0"
            return value
        second = self.market(mutate).snapshot("BTCUSDT")
        self.assertEqual(first["provenance"]["candles_sha256"], second["provenance"]["candles_sha256"])
        self.assertNotEqual(first["provenance"]["running_candles_sha256"], second["provenance"]["running_candles_sha256"])

    def test_exact_boundary_selects_just_closed_bar(self):
        boundary = (int(NOW * 1000) // 3_600_000 + 1) * 3_600_000
        for instant in (boundary - 1, boundary, boundary + 1):
            market = BinanceFuturesMarket(transport=lambda p, v: public_transport(p, v, now=instant / 1000),
                                          clock=lambda: instant / 1000)
            result = market.snapshot("BTCUSDT")
            for tf, interval in INTERVALS.items():
                self.assertEqual(result["candles"][tf][-1]["close_time"], instant // interval * interval - 1)

    def test_boundary_crossing_rereads_once_and_uses_fresh_closed_ohlcv(self):
        boundary = (int(NOW * 1000) // 900_000 + 1) * 900_000
        state = {"now": (boundary - 100) / 1000, "time_calls": 0, "klines": 0}
        def transport(path, parameters):
            if path.endswith("time"):
                state["time_calls"] += 1
                if state["time_calls"] == 2:
                    state["now"] = (boundary + 100) / 1000
            value = public_transport(path, parameters, now=state["now"])
            if path.endswith("klines"):
                state["klines"] += 1
                if state["klines"] <= 2:
                    value[-1][4] = "64950.00"  # partial price must not become closed history
            return value
        result = BinanceFuturesMarket(transport=transport, clock=lambda: state["now"]).snapshot("BTCUSDT")
        self.assertEqual(state["time_calls"], 4)
        self.assertEqual(state["klines"], 4)
        self.assertEqual(result["candles"]["15m"][-1]["close"], "65000.00")
        self.assertEqual(result["candles"]["15m"][-1]["close_time"], boundary - 1)

    def test_boundary_retries_are_finite_even_with_changing_exchange_clock(self):
        boundary = (int(NOW * 1000) // 900_000 + 1) * 900_000
        state = {"now": (boundary - 100) / 1000, "time_calls": 0}
        def transport(path, parameters):
            if path.endswith("time"):
                state["time_calls"] += 1
                if state["time_calls"] % 2 == 0:
                    state["now"] += 900
            return public_transport(path, parameters, now=state["now"])
        with self.assertRaisesRegex(MarketUnavailable, "MARKET_BOUNDARY_UNSTABLE"):
            BinanceFuturesMarket(transport=transport, clock=lambda: state["now"]).snapshot("BTCUSDT")
        self.assertEqual(state["time_calls"], 4)


class NativeClosedCandleContextTests(unittest.TestCase):
    def test_full_history_is_forwarded_without_private_context(self):
        value = native_context_200()
        value.update(risk_constraints=object(), contract_rules=object(), unknown=object())
        result = convert_native_market_context(value, now=NOW)
        self.assertEqual(sum(map(len, result["candles"].values())), 400)
        self.assertNotIn("recent_candles", result)
        self.assertNotIn("risk_constraints", result)
        self.assertNotIn("contract_rules", result)
        serialized = json.dumps(result, sort_keys=True, separators=(",", ":"))
        self.assertLess(len(serialized.encode()), 256 * 1024)
        self.assertGreater(len(serialized.encode()), 8192)
        self.assertEqual(result["provenance"]["candles_sha256"], hashlib.sha256(
            json.dumps(result["candles"], sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        for tf in INTERVALS:
            self.assertEqual(result["candles"][tf][-1], value["timeframes"][tf]["candles"][-2])
            self.assertEqual(result["running_candles"][tf], value["timeframes"][tf]["candles"][-1])
            self.assertEqual(result["candles"][tf][0]["volume"], "12.500")
        result["candles"]["1h"][0]["close"] = "1"
        self.assertEqual(value["timeframes"]["1h"]["candles"][-201]["close"], "65000.00")

    def test_native_100_row_source_cannot_claim_200_closed(self):
        for count in (20, 100, 200):
            with self.subTest(count=count), self.assertRaises(NativeContextUnavailable):
                convert_native_market_context(native_context_200(count=count), now=NOW)

    def test_201_rows_are_sufficient_but_missing_running_is_rejected(self):
        self.assertEqual(len(convert_native_market_context(native_context_200(count=201), now=NOW)["candles"]["1h"]), 200)
        value = native_context_200(count=202)
        for frame in value["timeframes"].values():
            frame["candles"].pop()
        with self.assertRaises(NativeContextUnavailable):
            convert_native_market_context(value, now=NOW)

    def test_native_malformed_gap_symbol_future_and_stale_context_rejected(self):
        mutations = (
            lambda v: v["timeframes"]["1h"]["candles"].reverse(),
            lambda v: v["timeframes"]["15m"]["candles"].pop(100),
            lambda v: v["timeframes"]["15m"].update(symbol="ETHUSDT"),
            lambda v: v["timeframes"]["1h"]["candles"][-1].update(open_time=int(NOW * 1000) + 1),
            lambda v: v.update(fetched_at=NOW - 181),
        )
        for mutate in mutations:
            value = native_context_200()
            mutate(value)
            with self.assertRaises(NativeContextUnavailable):
                convert_native_market_context(value, now=NOW)


if __name__ == "__main__":
    unittest.main()
