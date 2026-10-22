"""Render actual public futures OHLC observations into bounded PNG chart images.

These are data charts, not screenshots of the Binance website. Provenance is
visible on the image and embedded in PNG text metadata; prices are never invented.
"""
from __future__ import annotations

import binascii
import datetime as dt
import hashlib
import json
import math
import re
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


_INTERVALS = {"1h": 3_600_000, "15m": 900_000}
_CANDLE_FIELDS = {"open_time", "open", "high", "low", "close", "volume", "close_time"}
_CLOSED_COUNT = 200
_SOURCE_ORIGIN = "https://fapi.binance.com"
_SYMBOL = re.compile(r"[A-Z0-9]{2,24}USDT\Z", re.ASCII)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


def _number(value, *, zero=False):
    if type(value) is not str or not 1 <= len(value) <= 96:
        raise ValueError()
    parsed = Decimal(value)
    if (not parsed.is_finite() or (parsed < 0 if zero else parsed <= 0)
            or abs(parsed.adjusted()) > 30):
        raise ValueError()
    return parsed


def _candle(row, interval):
    if type(row) is not dict or set(row) != _CANDLE_FIELDS:
        raise ValueError()
    opened, closed = row["open_time"], row["close_time"]
    if (type(opened) is not int or type(closed) is not int or opened < 0
            or opened % interval or closed != opened + interval - 1):
        raise ValueError()
    o, h, low, c = (_number(row[key]) for key in ("open", "high", "low", "close"))
    volume = _number(row["volume"], zero=True)
    if not low <= min(o, c) <= max(o, c) <= h:
        raise ValueError()
    values = tuple(float(value) for value in (o, h, low, c, volume))
    if not all(math.isfinite(value) for value in values):
        raise ValueError()
    return values


def chart_provenance(snapshot: dict, timeframe: str) -> dict:
    """Verify and bind the full two-frame observation before drawing a chart.

    No historic row is guessed, synthesized, replaced or silently truncated.
    ``server_time_ms`` is the pre-collection Binance cutoff: a candle is historical
    only when its close is strictly before that cutoff. Running observations are
    independently hashed and never included in the 200 historical candles.
    """
    try:
        if type(timeframe) is not str or timeframe not in _INTERVALS or type(snapshot) is not dict:
            raise ValueError()
        provenance = snapshot["provenance"]
        if (type(provenance) is not dict or provenance["provider"] != "BINANCE_FUTURES_PUBLIC"
                or provenance["market"] != "USDT_PERPETUAL" or provenance["origin"] != _SOURCE_ORIGIN):
            raise ValueError()
        symbol, observed, server = snapshot["symbol"], snapshot["observed_at_utc"], snapshot["server_time_ms"]
        if (type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None
                or type(observed) is not str or not 20 <= len(observed) <= 40
                or not observed.isascii() or type(server) is not int or not 0 < server <= 10**16):
            raise ValueError()
        instant = dt.datetime.fromisoformat(observed.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() != dt.timedelta(0):
            raise ValueError()
        observed_seconds = snapshot["observed_at"]
        if (type(observed_seconds) not in (int, float) or not math.isfinite(observed_seconds)
                or abs(instant.timestamp() - observed_seconds) > 0.002
                or abs(observed_seconds - server / 1000) > 180):
            raise ValueError()
        _number(snapshot["mark_price"])
        mark_time = snapshot["mark_time_ms"]
        collection_server = snapshot.get("collection_finished_server_time_ms", server)
        if (type(collection_server) is not int or not server <= collection_server <= server + 90_000
                or type(mark_time) is not int or mark_time < server - 180_000
                or mark_time > collection_server):
            raise ValueError()
        frames, running, windows = snapshot["candles"], snapshot["running_candles"], snapshot["candle_windows"]
        if any(type(value) is not dict or set(value) != set(_INTERVALS)
               for value in (frames, running, windows)):
            raise ValueError()
        for tf, interval in _INTERVALS.items():
            rows = frames[tf]
            if type(rows) is not list or len(rows) != _CLOSED_COUNT:
                raise ValueError()
            previous = None
            for row in rows:
                _candle(row, interval)
                opened = row["open_time"]
                if (row["close_time"] >= server
                        or previous is not None and opened - previous != interval):
                    raise ValueError()
                previous = opened
            if rows[-1]["close_time"] != server // interval * interval - 1:
                raise ValueError()
            current = running[tf]
            if current is not None:
                _candle(current, interval)
                if not current["open_time"] <= server <= current["close_time"]:
                    raise ValueError()
            expected = {"closed_count": _CLOSED_COUNT, "closed_as_of_server_time_ms": server,
                "first_open_time_ms": rows[0]["open_time"], "last_close_time_ms": rows[-1]["close_time"],
                "running_candle_state": "OPEN_AT_CUTOFF" if current is not None else "NOT_OBSERVED"}
            if windows[tf] != expected:
                raise ValueError()
        if provenance["candles_sha256"] != _digest(frames) or provenance["running_candles_sha256"] != _digest(running):
            raise ValueError()
        rows = frames[timeframe]
        return {"chart_metadata_version": 2, "chart_kind": "CANDLESTICK_RENDER_FROM_PUBLIC_KLINES",
            "exchange_website_screenshot": False, "symbol": symbol, "timeframe": timeframe,
            "observed_at_utc": observed, "observed_at": observed_seconds,
            "closed_as_of_server_time_ms": server, "collection_finished_server_time_ms": collection_server,
            "source_origin": _SOURCE_ORIGIN,
            "closed_candle_count": _CLOSED_COUNT, "observation_closed_candle_count": 2 * _CLOSED_COUNT,
            "first_open_time_ms": rows[0]["open_time"], "last_close_time_ms": rows[-1]["close_time"],
            "observation_candles_sha256": provenance["candles_sha256"],
            "plotted_candles_sha256": _digest(rows), "running_candles_sha256": provenance["running_candles_sha256"],
            "running_candle_rendered": False, "running_candle_state": windows[timeframe]["running_candle_state"],
            "mark_price": snapshot["mark_price"], "mark_time_ms": mark_time}
    except (KeyError, TypeError, ValueError, ArithmeticError, OverflowError):
        raise ChartUnavailable("CHART_SOURCE_UNVERIFIED") from None


def render_chart_png(snapshot: dict, timeframe: str, *, width: int = 1024, height: int = 640) -> bytes:
    if type(width) is not int or type(height) is not int or width != 1024 or height != 640 or type(timeframe) is not str or timeframe not in _INTERVALS:
        raise ChartUnavailable("CHART_DIMENSIONS_OR_TIMEFRAME_INVALID")
    metadata = chart_provenance(snapshot, timeframe)
    plotted = snapshot["candles"][timeframe]
    values = [_candle(row, _INTERVALS[timeframe]) for row in plotted]
    low, high = min(v[2] for v in values), max(v[1] for v in values)
    # Flat observed markets still have a valid chart: padding alters only the
    # axis range, never the candle's observed OHLC prices.
    padding = max((high - low) * 0.05, abs(high) * 0.000001)
    low, high = low - padding, high + padding
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

    def utc(milliseconds, pattern="%Y-%m-%d %H:%M:%S"):
        return dt.datetime.fromtimestamp(milliseconds / 1000, dt.timezone.utc).strftime(pattern)

    text(24, 18, f"BINANCE FUTURES USDT PERPETUAL  {metadata['symbol']}  {timeframe}", scale=2)
    text(24, 45, f"COLLECTED UTC {metadata['observed_at_utc'][:26]}  MARK {snapshot['mark_price']}")
    text(24, 59, f"200 CLOSED CANDLES - CLOSED BEFORE UTC {utc(metadata['closed_as_of_server_time_ms'])}", color=(242, 193, 94))
    text(24, 73, "PUBLIC KLINES DATA CHART - NOT EXCHANGE WEBSITE SCREENSHOT")
    left, right, top, bottom = 24, width - 140, 100, 415
    volume_top, volume_bottom = 458, 523
    price_y = lambda v: top + (high - v) / (high - low) * (bottom - top)
    for n in range(6):
        y = top + n * (bottom - top) / 5
        rect(left, y, right, y, (41, 52, 69))
        price = high - n * (high - low) / 5
        text(right + 12, int(y) - 3, format(price, ".8g"))
    spacing = (right - left) / _CLOSED_COUNT
    maximum_volume = max(v[4] for v in values) or 1
    for i, (o, h, l, c, volume) in enumerate(values):
        x = left + (i + 0.5) * spacing
        color = (41, 190, 145) if c >= o else (236, 85, 103)
        rect(x, price_y(h), x, price_y(l), color)
        body_top, body_bottom = sorted((price_y(o), price_y(c)))
        half = max(0.5, spacing * 0.28)
        rect(x - half, body_top, x + half, max(body_top + 1, body_bottom), color)
        rect(x - half, volume_bottom - volume / maximum_volume * (volume_bottom-volume_top), x + half, volume_bottom, color)
    # UTC tick labels span all 200 observations; exact millisecond endpoints are
    # embedded with hashes in the image's PNG provenance record.
    for i in (0, 40, 80, 120, 160, 199):
        x = left + (i + 0.5) * spacing
        rect(x, bottom + 4, x, bottom + 9, (100, 114, 133))
        label = utc(plotted[i]["open_time"], "%m-%d %H:%M")
        text(max(left, min(right - len(label) * 6, int(x) - len(label) * 3)), bottom + 14, label)
    text(right + 12, volume_top + 6, "BASE VOL")
    text(right + 12, volume_top + 20, format(maximum_volume, ".8g"))
    text(24, 546, f"FIRST OPEN UTC {utc(plotted[0]['open_time'])}  LAST CLOSE UTC {utc(plotted[-1]['close_time'])}")
    text(24, 561, f"LAST CLOSED PRICE {plotted[-1]['close']}  CURRENT MARK {snapshot['mark_price']}  UTC {utc(snapshot['mark_time_ms'])}")
    text(24, 576, "RUNNING CANDLE EXCLUDED FROM HISTORICAL CHART AND 200-CANDLE COUNT.", color=(242, 193, 94))
    text(24, 591, f"CLOSED DATA SHA256 {metadata['plotted_candles_sha256']}")
    text(24, 606, f"400-CANDLE OBSERVATION SHA256 {metadata['observation_candles_sha256']}")
    scanlines = b"".join(b"\0" + bytes(pixels[y * width * 3:(y + 1) * width * 3]) for y in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"tEXt", b"HarunProvenance\0" + json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")) +
            chunk(b"IDAT", zlib.compress(scanlines, level=6)) + chunk(b"IEND", b""))
