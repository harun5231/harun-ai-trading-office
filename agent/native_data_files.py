"""Deliver the already validated public candle series as compact CSV files.

Only the WEB representation changes. Native request context, screenshots,
provenance, schema, prices and risk calculations remain with their owners.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json

from .llm import validated_market_context
from .native_market_context import convert_native_market_context


TIMEFRAMES = ('15m', '1h')
CSV_COLUMNS = ('open_time_utc_ms', 'open', 'high', 'low', 'close', 'volume', 'close_time_utc_ms')
CONTEXT_SEPARATOR = '\n\nCONTEXT_JSON:\n'
SCHEMA_SEPARATOR = '\n\nOUTPUT_JSON_SCHEMA:\n'
MAX_PROMPT_BYTES = 384 * 1024


def _need(value):
    if not value:
        raise ValueError('NATIVE_DATA_FILES_UNVERIFIED')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def prepare_data_files(projected_context, *, now=None):
    """Return two buffers and receipts from the existing 400-candle validator.

    ``buffer`` is ready for Playwright. Callers pass only name/mimeType/buffer
    into its file API; the remaining fields attest those same bytes.
    """
    context = validated_market_context(projected_context, now=now)
    files = []
    for timeframe in TIMEFRAMES:
        rows = context['candles'][timeframe]
        # The shared validator above checks counts, cutoff, sequence, values,
        # running-bar separation, and both source candle digests.
        _need(len(rows) == 200)
        stream = io.StringIO(newline='')
        writer = csv.writer(stream, lineterminator='\n')
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow((row['open_time'], row['open'], row['high'], row['low'],
                             row['close'], row['volume'], row['close_time']))
        raw = stream.getvalue().encode('utf-8')
        digest = _sha(raw)
        symbol = context['symbol']
        files.append({'name': f'{symbol}_{timeframe}_200_closed_{digest[:8]}.csv',
            'mimeType': 'text/csv', 'buffer': raw, 'sha256': digest,
            'symbol': symbol, 'timeframe': timeframe, 'closed_count': 200,
            'source_candles_sha256': context['provenance']['candles_sha256'],
            'series_sha256': _sha(_canonical(rows)), 'timestamp_unit': 'UNIX_MILLISECONDS_UTC'})
    return files


def deliver_market_data_files(native_context, *, now=None):
    """Validate the native market context and make exact CLOSED-series files."""
    projected = convert_native_market_context(native_context, now=now)
    return prepare_data_files(projected, now=now)


def render_short_prompt(payload, formatted_original, files, *, now=None):
    """Replace only the native formatter's large inline public-context section.

    The source-pinned original formatter remains responsible for the owner
    prompt, output schema and marker reminder. Its exact suffix is preserved.
    Internal payload and data provenance are never rewritten.
    """
    _need(type(payload) is dict and type(payload.get('prompt')) is str
          and type(formatted_original) is str and type(files) is list)
    expected = deliver_market_data_files(payload.get('context'), now=now)
    _need(files == expected)
    prefix = payload['prompt'] + CONTEXT_SEPARATOR
    _need(formatted_original.startswith(prefix))
    inline_and_suffix = formatted_original[len(prefix):]
    _need(inline_and_suffix.count(SCHEMA_SEPARATOR) == 1)
    _, suffix = inline_and_suffix.split(SCHEMA_SEPARATOR, 1)
    _need(type(payload.get('marker')) is str and payload['marker']
          and suffix.count(payload['marker']) == 1)
    symbol = expected[0]['symbol']
    instructions = (
        f'Untuk {symbol}, gunakan dua file CSV terlampir: masing-masing 200 candle '
        'tertutup 15M dan 1H, bersama dua gambar chart Binance Futures USDT Perpetual. '
        'Waktu pada CSV adalah Unix millisecond UTC; candle berjalan tidak disertakan.\n'
        f"15M: {expected[0]['name']}\n1H: {expected[1]['name']}")
    rendered = payload['prompt'] + '\n\n' + instructions + SCHEMA_SEPARATOR + suffix
    _need(len(rendered.encode('utf-8')) <= MAX_PROMPT_BYTES)
    return rendered
