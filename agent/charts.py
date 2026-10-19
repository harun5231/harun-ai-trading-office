"""Render actual public futures OHLC observations into bounded PNG chart images.

These are data charts, not screenshots of the Binance website. Provenance is
visible on the image and embedded in PNG text metadata; prices are never invented.
"""
from __future__ import annotations

import binascii
import hashlib
import json
import math
import struct
import zlib
from decimal import Decimal


class ChartUnavailable(RuntimeError):
    pass


# Five by seven bitmap labels keep image generation independent of native fonts.
_GLYPHS = {
    "0": "01110/10001/10011/10101/11001/10001/01110", "1": "00100/01100/00100/00100/00100/00100/01110",
    "2": "01110/10001/00001/00010/00100/01000/11111", "3": "11110/00001/00001/01110/00001/00001/11110",
    "4": "00010/00110/01010/10010/11111/00010/00010", "5": "11111/10000/10000/11110/00001/00001/11110",
    "6": "01110/10000/10000/11110/10001/10001/01110", "7": "11111/00001/00010/00100/01000/01000/01000",
    "8": "01110/10001/10001/01110/10001/10001/01110", "9": "01110/10001/10001/01111/00001/00001/01110",
    "A": "01110/10001/10001/11111/10001/10001/10001", "B": "11110/10001/10001/11110/10001/10001/11110",
    "C": "01111/10000/10000/10000/10000/10000/01111", "D": "11110/10001/10001/10001/10001/10001/11110",
    "E": "11111/10000/10000/11110/10000/10000/11111", "F": "11111/10000/10000/11110/10000/10000/10000",
    "G": "01111/10000/10000/10111/10001/10001/01111", "H": "10001/10001/10001/11111/10001/10001/10001",
    "I": "01110/00100/00100/00100/00100/00100/01110", "J": "00111/00010/00010/00010/10010/10010/01100",
    "K": "10001/10010/10100/11000/10100/10010/10001", "L": "10000/10000/10000/10000/10000/10000/11111",
    "M": "10001/11011/10101/10101/10001/10001/10001", "N": "10001/11001/10101/10011/10001/10001/10001",
    "O": "01110/10001/10001/10001/10001/10001/01110", "P": "11110/10001/10001/11110/10000/10000/10000",
    "Q": "01110/10001/10001/10001/10101/10010/01101", "R": "11110/10001/10001/11110/10100/10010/10001",
    "S": "01111/10000/10000/01110/00001/00001/11110", "T": "11111/00100/00100/00100/00100/00100/00100",
    "U": "10001/10001/10001/10001/10001/10001/01110", "V": "10001/10001/10001/10001/10001/01010/00100",
    "W": "10001/10001/10001/10101/10101/10101/01010", "X": "10001/10001/01010/00100/01010/10001/10001",
    "Y": "10001/10001/01010/00100/00100/00100/00100", "Z": "11111/00001/00010/00100/01000/10000/11111",
    ".": "00000/00000/00000/00000/00000/00110/00110", "-": "00000/00000/00000/11111/00000/00000/00000",
    ":": "00000/00110/00110/00000/00110/00110/00000", "/": "00001/00010/00010/00100/01000/01000/10000",
    " ": "00000/00000/00000/00000/00000/00000/00000", "+": "00000/00100/00100/11111/00100/00100/00000",
}


def render_chart_png(snapshot: dict, timeframe: str, *, width: int = 1024, height: int = 640) -> bytes:
    if width != 1024 or height != 640 or timeframe not in ("1h", "15m"):
        raise ChartUnavailable("CHART_DIMENSIONS_OR_TIMEFRAME_INVALID")
    try:
        provenance = snapshot["provenance"]
        if provenance["provider"] != "BINANCE_FUTURES_PUBLIC" or provenance["market"] != "USDT_PERPETUAL":
            raise ValueError()
        symbol, observed = snapshot["symbol"], snapshot["observed_at_utc"]
        if not isinstance(symbol, str) or not symbol.isascii() or not symbol.isalnum() or len(symbol) > 28:
            raise ValueError()
        if not isinstance(observed, str) or not observed.isascii() or not 20 <= len(observed) <= 40:
            raise ValueError()
        rows = snapshot["candles"][timeframe]
        if not isinstance(rows, list) or not 30 <= len(rows) <= 200:
            raise ValueError()
        plotted = rows[-100:]
        values = []
        previous = None
        for row in plotted:
            opened = row["open_time"]
            if type(opened) is not int or (previous is not None and opened <= previous):
                raise ValueError()
            previous = opened
            o, h, l, c, volume = (Decimal(str(row[key])) for key in ("open", "high", "low", "close", "volume"))
            if not all(v.is_finite() for v in (o, h, l, c, volume)) or l <= 0 or volume < 0 or not l <= min(o, c) <= max(o, c) <= h:
                raise ValueError()
            values.append((float(o), float(h), float(l), float(c), float(volume)))
        if not all(math.isfinite(v) for value in values for v in value):
            raise ValueError()
        low, high = min(v[2] for v in values), max(v[1] for v in values)
        if high <= low:
            raise ValueError()
        metadata = json.dumps({"chart_kind": "CANDLESTICK_RENDER_FROM_PUBLIC_KLINES", "exchange_website_screenshot": False, "symbol": symbol, "timeframe": timeframe, "observed_at_utc": observed, "source_origin": provenance["origin"], "plotted_candles_sha256": hashlib.sha256(json.dumps(plotted, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}, sort_keys=True, separators=(",", ":"))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise ChartUnavailable("CHART_SOURCE_UNVERIFIED") from None
    pixels = bytearray(bytes((14, 20, 31)) * width * height)

    def rect(x1, y1, x2, y2, color):
        x1, x2 = max(0, int(x1)), min(width - 1, int(x2))
        y1, y2 = max(0, int(y1)), min(height - 1, int(y2))
        if x2 < x1 or y2 < y1:
            return
        line = bytes(color) * (x2 - x1 + 1)
        for y in range(y1, y2 + 1):
            offset = (y * width + x1) * 3
            pixels[offset:offset + len(line)] = line

    def text(x, y, value, color=(206, 216, 231), scale=1):
        for char in value.upper():
            glyph = _GLYPHS.get(char, _GLYPHS[" "]).split("/")
            for gy, line in enumerate(glyph):
                for gx, bit in enumerate(line):
                    if bit == "1":
                        rect(x + gx * scale, y + gy * scale, x + (gx + 1) * scale - 1, y + (gy + 1) * scale - 1, color)
            x += 6 * scale

    text(24, 22, f"BINANCE FUTURES USDT PERPETUAL  {symbol}  {timeframe}", scale=2)
    text(24, 50, f"OBSERVED UTC {str(observed)[:26]}")
    text(24, 65, "PUBLIC KLINES DATA CHART - NOT EXCHANGE WEBSITE SCREENSHOT", color=(242, 193, 94))
    left, right, top, bottom = 24, width - 140, 100, height - 140
    padding = (high - low) * 0.05
    low, high = low - padding, high + padding
    price_y = lambda v: top + (high - v) / (high - low) * (bottom - top)
    for n in range(6):
        y = top + n * (bottom - top) / 5
        rect(left, y, right, y, (41, 52, 69))
        price = high - n * (high - low) / 5
        text(right + 12, int(y) - 3, format(price, ".8g"))
    spacing = (right - left) / len(values)
    maximum_volume = max(v[4] for v in values) or 1
    for i, (o, h, l, c, volume) in enumerate(values):
        x = left + (i + 0.5) * spacing
        color = (41, 190, 145) if c >= o else (236, 85, 103)
        rect(x, price_y(h), x, price_y(l), color)
        body_top, body_bottom = sorted((price_y(o), price_y(c)))
        half = max(1, spacing * 0.28)
        rect(x - half, body_top, x + half, max(body_top + 1, body_bottom), color)
        rect(x - half, height - 80 - volume / maximum_volume * 40, x + half, height - 80, color)
    text(24, height - 60, f"LAST CLOSE {plotted[-1]['close']}  MARK {snapshot.get('mark_price', '')}")
    text(24, height - 42, "LATEST CANDLE MAY BE OPEN. TIMES AND PRICES COME FROM BINANCE PUBLIC DATA.")
    scanlines = b"".join(b"\0" + bytes(pixels[y * width * 3:(y + 1) * width * 3]) for y in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"tEXt", b"HarunProvenance\0" + metadata.encode("ascii")) +
            chunk(b"IDAT", zlib.compress(scanlines, level=6)) + chunk(b"IEND", b""))
