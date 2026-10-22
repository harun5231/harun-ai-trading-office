import binascii
import copy
import json
import hashlib
import struct
import unittest
import zlib

from agent.charts import ChartUnavailable, render_chart_png
from agent.market import BinanceFuturesMarket
from test_agent_market import NOW, fixture_transport


class ChartTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = BinanceFuturesMarket(transport=fixture_transport, clock=lambda: NOW).snapshot("BTCUSDT")

    def chunks(self, png):
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        cursor = 8
        result = {}
        while cursor < len(png):
            length = struct.unpack(">I", png[cursor:cursor+4])[0]
            kind = png[cursor+4:cursor+8]
            data = png[cursor+8:cursor+8+length]
            crc = struct.unpack(">I", png[cursor+8+length:cursor+12+length])[0]
            self.assertEqual(crc, binascii.crc32(kind + data) & 0xffffffff)
            result[kind] = data
            cursor += 12 + length
        self.assertEqual(cursor, len(png))
        return result

    def test_real_png_pixels_and_truthful_provenance(self):
        result = render_chart_png(self.snapshot, "1h")
        chunks = self.chunks(result)
        self.assertEqual(struct.unpack(">II", chunks[b"IHDR"][:8]), (1024, 640))
        self.assertEqual(len(zlib.decompress(chunks[b"IDAT"])), 640 * (1 + 1024 * 3))
        metadata = json.loads(chunks[b"tEXt"].split(b"\0", 1)[1])
        self.assertEqual(metadata["symbol"], "BTCUSDT")
        self.assertFalse(metadata["exchange_website_screenshot"])
        self.assertEqual(metadata["source_origin"], "https://fapi.binance.com")
        self.assertLess(len(result), 2 * 1024 * 1024)

    def test_deterministic_but_changes_with_actual_candle_data(self):
        a = render_chart_png(self.snapshot, "15m")
        self.assertEqual(a, render_chart_png(self.snapshot, "15m"))
        modified = copy.deepcopy(self.snapshot)
        modified["candles"]["15m"][-1]["close"] = "64950"
        modified["provenance"]["candles_sha256"] = hashlib.sha256(json.dumps(
            modified["candles"], sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()
        self.assertNotEqual(a, render_chart_png(modified, "15m"))

    def test_rejects_missing_provenance_and_invalid_prices(self):
        for kind in ("provenance", "nan", "ohlc", "missing"):
            bad = copy.deepcopy(self.snapshot)
            if kind == "provenance":
                bad["provenance"]["provider"] = "INVENTED"
            elif kind == "nan":
                bad["candles"]["1h"][-1]["close"] = "NaN"
            elif kind == "ohlc":
                bad["candles"]["1h"][-1]["low"] = "999999"
            else:
                bad["candles"]["1h"] = []
            with self.subTest(kind=kind), self.assertRaises(ChartUnavailable):
                render_chart_png(bad, "1h")

    def test_bounds_dimensions_and_intervals(self):
        with self.assertRaises(ChartUnavailable):
            render_chart_png(self.snapshot, "1m")
        with self.assertRaises(ChartUnavailable):
            render_chart_png(self.snapshot, "1h", width=50000)


if __name__ == "__main__":
    unittest.main()
