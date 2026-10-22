import copy
import unittest

from agent.market import BinanceFuturesMarket, MarketUnavailable


NOW = 1792164832.0


def fixture_transport(path, parameters):
    if path == "/fapi/v1/time":
        return {"serverTime": int(NOW * 1000)}
    if path == "/fapi/v1/exchangeInfo":
        return {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", "marginAsset": "USDT", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ]}]}
    if path == "/fapi/v1/premiumIndex":
        return {"symbol": "BTCUSDT", "markPrice": "65000.125", "time": int(NOW * 1000)}
    interval = {"1h": 3600000, "15m": 900000}[parameters["interval"]]
    last = int(NOW * 1000) // interval * interval
    return [[last - (parameters["limit"]-1-i) * interval, "64900", "65100", "64800", "65000", "12.5", last - (parameters["limit"]-1-i) * interval + interval - 1] for i in range(parameters["limit"])]


class MarketTests(unittest.TestCase):
    def market(self, mutate=None):
        def transport(path, parameters):
            result = copy.deepcopy(fixture_transport(path, parameters))
            return mutate(path, parameters, result) if mutate else result
        return BinanceFuturesMarket(transport=transport, clock=lambda: NOW)

    def test_real_schema_and_all_required_public_gets(self):
        calls = []
        def transport(path, params):
            calls.append((path, params))
            return fixture_transport(path, params)
        result = BinanceFuturesMarket(transport=transport, clock=lambda: NOW).snapshot("BTCUSDT")
        self.assertEqual(set(result["candles"]), {"1h", "15m"})
        self.assertEqual(result["observed_at"], NOW)
        self.assertEqual(result["symbol_rules"]["tick_size"], "0.10")
        self.assertNotIn("max_leverage", result["symbol_rules"])
        self.assertEqual(len(result["provenance"]["candles_sha256"]), 64)
        self.assertEqual(len(calls), 6)
        self.assertTrue(all(not any(key.lower() in ("signature", "apikey", "secret") for key in params) for _, params in calls))

    def test_disallows_non_usdt_and_url_injection(self):
        for symbol in ("BTCBUSD", "btcusdt", "BTCUSDT&signature=x", "https://evil", "BTC/USDT", ""):
            with self.subTest(symbol=symbol), self.assertRaises(MarketUnavailable):
                self.market().snapshot(symbol)

    def test_requires_both_timeframes(self):
        for value in (("1h",), ("15m", "15m"), ("1m", "1h"), ["1h", "15m"]):
            with self.subTest(value=value), self.assertRaises(MarketUnavailable):
                self.market().snapshot("BTCUSDT", value)

    def test_rejects_private_endpoint_and_unreviewed_parameters(self):
        client = self.market()
        with self.assertRaises(MarketUnavailable):
            client._get("/fapi/v1/order", {})
        with self.assertRaises(MarketUnavailable):
            client._get("/fapi/v1/time", {"signature": "never"})

    def test_rejects_stale_server_and_quote(self):
        for endpoint, key in (("/fapi/v1/time", "serverTime"), ("/fapi/v1/premiumIndex", "time")):
            def mutate(path, params, raw):
                if path == endpoint:
                    raw[key] -= 200000
                return raw
            with self.subTest(endpoint=endpoint), self.assertRaises(MarketUnavailable):
                self.market(mutate).snapshot("BTCUSDT")

    def test_rejects_nonfinite_negative_or_bool_prices(self):
        for value in ("NaN", "Infinity", "-2", True, "0"):
            def mutate(path, params, raw):
                if path == "/fapi/v1/premiumIndex":
                    raw["markPrice"] = value
                return raw
            with self.subTest(value=value), self.assertRaises(MarketUnavailable):
                self.market(mutate).snapshot("BTCUSDT")

    def test_rejects_missing_or_reordered_candles(self):
        for mutation in (lambda rows: rows[:-1], lambda rows: rows[::-1], lambda rows: rows[:15] + rows[16:]):
            def mutate(path, params, raw):
                return mutation(raw) if path == "/fapi/v1/klines" else raw
            with self.assertRaises(MarketUnavailable):
                self.market(mutate).snapshot("BTCUSDT")

    def test_rejects_inconsistent_ohlc(self):
        def mutate(path, params, raw):
            if path == "/fapi/v1/klines":
                raw[-1][2] = "64000"
            return raw
        with self.assertRaises(MarketUnavailable):
            self.market(mutate).snapshot("BTCUSDT")

    def test_rejects_unknown_contract_or_duplicate_filters(self):
        for kind in ("contract", "duplicate"):
            def mutate(path, params, raw):
                if path == "/fapi/v1/exchangeInfo":
                    row = raw["symbols"][0]
                    if kind == "contract":
                        row["contractType"] = "CURRENT_QUARTER"
                    else:
                        row["filters"].append(row["filters"][0])
                return raw
            with self.subTest(kind=kind), self.assertRaises(MarketUnavailable):
                self.market(mutate).snapshot("BTCUSDT")


if __name__ == "__main__":
    unittest.main()
