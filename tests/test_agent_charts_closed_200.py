"""Offline contract fixtures; these rows are not claimed as live market data."""
import binascii
import copy
import datetime as dt
import hashlib
import json
import struct
import unittest
import zlib

from agent.charts import ChartUnavailable, chart_provenance, render_chart_png


SERVER_MS = 1792164832000
INTERVALS = {"1h": 3_600_000, "15m": 900_000}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


def snapshot_fixture():
    observed = SERVER_MS / 1000 + 1
    result = {"symbol": "BTCUSDT", "observed_at": observed,
        "observed_at_utc": dt.datetime.fromtimestamp(observed, dt.timezone.utc).isoformat(),
        "server_time_ms": SERVER_MS, "mark_price": "65000.125", "mark_time_ms": SERVER_MS,
        "candles": {}, "running_candles": {}, "candle_windows": {},
        "provenance": {"provider": "BINANCE_FUTURES_PUBLIC", "market": "USDT_PERPETUAL",
            "origin": "https://fapi.binance.com"}}
    for tf, interval in INTERVALS.items():
        running_open = SERVER_MS // interval * interval
        def row(opened):
            return {"open_time": opened, "close_time": opened+interval-1,
                "open": "64900", "high": "65100", "low": "64800", "close": "65000", "volume": "12.5"}
        result["candles"][tf] = [row(running_open-(200-i)*interval) for i in range(200)]
        result["running_candles"][tf] = row(running_open)
        result["candle_windows"][tf] = {"closed_count": 200, "closed_as_of_server_time_ms": SERVER_MS,
            "first_open_time_ms": result["candles"][tf][0]["open_time"],
            "last_close_time_ms": result["candles"][tf][-1]["close_time"],
            "running_candle_state": "OPEN_AT_CUTOFF"}
    rehash(result)
    return result


def rehash(snapshot):
    snapshot["provenance"]["candles_sha256"] = digest(snapshot["candles"])
    snapshot["provenance"]["running_candles_sha256"] = digest(snapshot["running_candles"])


def chunks(png):
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("not PNG")
    cursor, result = 8, {}
    while cursor < len(png):
        length = struct.unpack(">I", png[cursor:cursor+4])[0]
        kind = png[cursor+4:cursor+8]
        data = png[cursor+8:cursor+8+length]
        crc = struct.unpack(">I", png[cursor+8+length:cursor+12+length])[0]
        if crc != binascii.crc32(kind + data) & 0xffffffff:
            raise AssertionError("invalid CRC")
        result[kind] = data
        cursor += 12 + length
    if cursor != len(png):
        raise AssertionError("incomplete PNG")
    return result


class ClosedChartTests(unittest.TestCase):
    def test_exact_200_plotted_and_full_400_hash_bound_in_each_frame(self):
        snapshot = snapshot_fixture()
        for tf in INTERVALS:
            with self.subTest(timeframe=tf):
                png = render_chart_png(snapshot, tf)
                parts = chunks(png)
                metadata = json.loads(parts[b"tEXt"].split(b"\0", 1)[1])
                self.assertEqual(struct.unpack(">II", parts[b"IHDR"][:8]), (1024, 640))
                self.assertEqual(len(zlib.decompress(parts[b"IDAT"])), 640 * (1+1024*3))
                self.assertEqual(metadata["chart_metadata_version"], 2)
                self.assertEqual(metadata["closed_candle_count"], 200)
                self.assertEqual(metadata["observation_closed_candle_count"], 400)
                self.assertEqual(metadata["plotted_candles_sha256"], digest(snapshot["candles"][tf]))
                self.assertEqual(metadata["observation_candles_sha256"], digest(snapshot["candles"]))
                self.assertEqual(metadata["first_open_time_ms"], snapshot["candles"][tf][0]["open_time"])
                self.assertEqual(metadata["last_close_time_ms"], snapshot["candles"][tf][-1]["close_time"])
                self.assertEqual(metadata["symbol"], "BTCUSDT")
                self.assertEqual(metadata["mark_price"], snapshot["mark_price"])
                self.assertEqual(metadata["mark_time_ms"], snapshot["mark_time_ms"])
                self.assertFalse(metadata["running_candle_rendered"])
                self.assertFalse(metadata["exchange_website_screenshot"])
                self.assertLess(len(png), 2*1024*1024)

    def test_all_200_candles_have_distinct_visible_wicks(self):
        parts = chunks(render_chart_png(snapshot_fixture(), "1h"))
        raw = zlib.decompress(parts[b"IDAT"])
        # On this contract fixture the entire high/low wick crosses y=200.
        # Count distinct green runs: silent truncation or overcrowding fails.
        row = raw[200*(1+1024*3)+1:201*(1+1024*3)]
        green = bytes((41, 190, 145))
        values = [row[i:i+3] == green for i in range(24*3, 884*3, 3)]
        self.assertEqual(sum(value and (i == 0 or not values[i-1]) for i, value in enumerate(values)), 200)

    def test_first_candle_changes_pixels_not_only_hash_label(self):
        snapshot = snapshot_fixture()
        first = chunks(render_chart_png(snapshot, "15m"))
        snapshot["candles"]["15m"][0]["close"] = "64950"
        rehash(snapshot)
        modified = chunks(render_chart_png(snapshot, "15m"))
        stride = 1+1024*3
        first_plot = zlib.decompress(first[b"IDAT"])[100*stride:416*stride]
        modified_plot = zlib.decompress(modified[b"IDAT"])[100*stride:416*stride]
        self.assertNotEqual(first_plot, modified_plot)

    def test_running_candle_never_enters_historical_chart_pixels(self):
        snapshot = snapshot_fixture()
        before = chunks(render_chart_png(snapshot, "1h"))
        snapshot["running_candles"]["1h"]["high"] = "999999"
        rehash(snapshot)
        after = chunks(render_chart_png(snapshot, "1h"))
        self.assertEqual(before[b"IDAT"], after[b"IDAT"])
        self.assertNotEqual(before[b"tEXt"], after[b"tEXt"])

    def test_running_candle_can_be_absent_but_is_labeled(self):
        snapshot = snapshot_fixture()
        snapshot["running_candles"]["1h"] = None
        snapshot["candle_windows"]["1h"]["running_candle_state"] = "NOT_OBSERVED"
        rehash(snapshot)
        metadata = chart_provenance(snapshot, "1h")
        self.assertEqual(metadata["running_candle_state"], "NOT_OBSERVED")
        self.assertFalse(metadata["running_candle_rendered"])

    def test_refuses_incomplete_unclosed_gapped_or_tampered_observations(self):
        mutations = {
            "199": lambda s: s["candles"]["1h"].pop(0),
            "201": lambda s: s["candles"]["1h"].append(s["running_candles"]["1h"]),
            "running_as_closed": lambda s: s["candles"]["1h"].__setitem__(-1,s["running_candles"]["1h"]),
            "gap": lambda s: s["candles"]["1h"].__setitem__(0,s["candles"]["1h"][1]),
            "first_unhashed": lambda s: s["candles"]["1h"][0].__setitem__("close","64950"),
            "other_frame_unhashed": lambda s: s["candles"]["15m"][0].__setitem__("close","64950"),
            "wrong_origin": lambda s: s["provenance"].__setitem__("origin","https://api.binance.com"),
            "wrong_symbol": lambda s: s.__setitem__("symbol","BTCBUSD"),
            "missing_window": lambda s: s["candle_windows"].pop("15m"),
            "false_count": lambda s: s["candle_windows"]["1h"].__setitem__("closed_count",100),
            "wrong_mark_time": lambda s: s.__setitem__("mark_time_ms",SERVER_MS-181000),
            "false_observed_time": lambda s: s.__setitem__("observed_at",SERVER_MS/1000-60),
            "nan": lambda s: s["candles"]["1h"][0].__setitem__("close","NaN"),
            "bad_ohlc": lambda s: s["candles"]["1h"][0].__setitem__("low","999999"),
        }
        for label, mutate in mutations.items():
            snapshot = snapshot_fixture()
            mutate(snapshot)
            with self.subTest(mutation=label), self.assertRaises(ChartUnavailable):
                render_chart_png(snapshot, "1h")

    def test_stale_or_wrong_cutoff_rejected_even_with_updated_hash(self):
        snapshot = snapshot_fixture()
        snapshot["server_time_ms"] += 3_600_000
        rehash(snapshot)
        with self.assertRaises(ChartUnavailable):
            render_chart_png(snapshot, "1h")

    def test_mark_time_uses_binance_collection_clock_not_assumed_host_clock(self):
        snapshot = snapshot_fixture()
        snapshot["collection_finished_server_time_ms"] = SERVER_MS + 10_000
        snapshot["mark_time_ms"] = SERVER_MS + 10_000
        self.assertEqual(chart_provenance(snapshot, "1h")["mark_time_ms"], SERVER_MS + 10_000)
        snapshot["mark_time_ms"] += 1
        with self.assertRaises(ChartUnavailable):
            render_chart_png(snapshot, "1h")

    def test_public_and_native_canonical_observations_render_without_contract_drift(self):
        from agent.market import BinanceFuturesMarket
        from agent.native_market_context import convert_native_market_context
        if __package__:
            from .test_agent_closed_candles_200 import NOW, native_context_200, public_transport
        else:
            from test_agent_closed_candles_200 import NOW, native_context_200, public_transport
        public = BinanceFuturesMarket(transport=public_transport, clock=lambda: NOW).snapshot("BTCUSDT")
        native = convert_native_market_context(native_context_200(), now=NOW)
        for snapshot in (public, native):
            for timeframe in INTERVALS:
                with self.subTest(source=snapshot["provenance"]["chart_kind"], timeframe=timeframe):
                    metadata = chart_provenance(snapshot, timeframe)
                    self.assertEqual(metadata["observation_candles_sha256"], snapshot["provenance"]["candles_sha256"])
                    self.assertTrue(render_chart_png(snapshot, timeframe).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_flat_valid_market_and_determinism(self):
        snapshot = snapshot_fixture()
        for rows in snapshot["candles"].values():
            for row in rows:
                row.update(open="1",high="1",low="1",close="1",volume="0")
        rehash(snapshot)
        self.assertEqual(render_chart_png(snapshot, "1h"), render_chart_png(snapshot, "1h"))

    def test_unbounded_dimensions_refused(self):
        for dimensions in ({"width": 50000}, {"height": 900}, {"width": True}):
            with self.subTest(dimensions=dimensions), self.assertRaises(ChartUnavailable):
                render_chart_png(snapshot_fixture(), "1h", **dimensions)


if __name__ == "__main__":
    unittest.main()
