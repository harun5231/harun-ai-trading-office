"""Deliver the already validated public candle series as compact CSV files.

Only the WEB representation changes. Native request context, screenshots,
provenance, schema, prices and risk calculations remain with their owners.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re

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

    The source-pinned formatter still establishes payload framing and its one
    operation marker. The wire schema stays internal; only a compact readable
    final-summary request is sent to WEB. Payload/data provenance is unchanged.
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
    projected = convert_native_market_context(payload['context'], now=now)
    instructions = (
        f"Bahan Binance Futures USDT Perpetual untuk {symbol}, diambil {projected['observed_at_utc']}. "
        'Dua CSV terlampir masing-masing berisi 200 candle tertutup 15M dan 1H dengan OHLCV asli. '
        'Waktu CSV adalah Unix millisecond UTC; candle berjalan dipisahkan dari 200 candle itu.\n'
        f"15M: {expected[0]['name']}\n1H: {expected[1]['name']}\n"
        f"Harga mark saat pengambilan data: {projected['mark_price']} USDT. "
        'Periksa juga dua gambar chart terlampir: simbol, timeframe, rentang waktu, dan harga harus terbaca. '
        'Candle paling kanan dapat masih berjalan; jangan menghitungnya sebagai candle tertutup. '
        'Jika gambar/CSV tidak terbaca atau tidak konsisten, jelaskan bahan yang perlu diperbaiki; '
        'itu bukan keputusan HOLD berdasarkan pasar.')
    rendered = payload['prompt'] + '\n\n' + instructions + '\n\n' + compact_summary(payload)
    _need(len(rendered.encode('utf-8')) <= MAX_PROMPT_BYTES)
    return rendered


def compact_summary(payload):
    """Keep the wire schema internal while asking for a readable final answer."""
    marker = payload.get('marker')
    _need(type(marker) is str and marker and '\n' not in marker)
    required = payload.get('output_schema', {}).get('required', [])
    if set(required) == {'symbols'}:
        summary = 'Akhiri dengan daftar COIN yang dipilih sesuai jumlah permintaan, beserta alasan singkat.'
    elif set(required) == {'symbol', 'order_ref', 'decision'}:
        summary = ('Akhiri dengan COIN dan keputusan TAHAN atau BATALKAN untuk order yang sedang dibahas, '
                   'beserta alasan. Jangan menafsirkan perubahan harga sebagai izin membuat order baru.')
    else:
        summary = ('Jika keputusan sudah jelas, akhiri dengan ringkasan COIN, LONG/SHORT/HOLD, ENTRY, TP, dan SL. '
                   'Jika HOLD adalah penilaian pasar, jelaskan alasannya; jangan mengarang level yang belum ditetapkan. '
                   'Quantity dihitung Risk Manager, jadi tidak perlu menentukan ukuran posisi.')
    return summary + '\nAcuan percakapan: ' + marker


def render_screening_prompt(payload, formatted_original):
    """Present capacity/exclusions naturally, without repeating an output schema."""
    _need(type(payload) is dict and type(payload.get('prompt')) is str
          and type(formatted_original) is str and payload.get('output_schema', {}).get('required') == ['symbols'])
    prefix = payload['prompt'] + CONTEXT_SEPARATOR
    _need(formatted_original.startswith(prefix) and formatted_original[len(prefix):].count(SCHEMA_SEPARATOR) == 1)
    _, suffix = formatted_original[len(prefix):].split(SCHEMA_SEPARATOR, 1)
    _need(type(payload.get('marker')) is str and suffix.count(payload['marker']) == 1)
    context = payload.get('context')
    details = ''
    if context is not None:
        _need(type(context) is dict and set(context) <= {
            'requested_count', 'excluded_symbols', 'replacement_instruction'})
        count, excluded = context.get('requested_count'), context.get('excluded_symbols')
        _need(type(count) is int and count in (1, 2) and type(excluded) is list
              and all(type(symbol) is str and re.fullmatch(r'[A-Z0-9]{2,24}USDT', symbol) for symbol in excluded))
        details = f'\n\nKapasitas saat ini {count} coin. '
        if excluded:
            details += 'Pilih coin pengganti; jangan mengulang coin berikut dalam siklus ini: ' + ', '.join(excluded) + '.'
    result = payload['prompt'] + details + '\n\n' + compact_summary(payload)
    _need(len(result.encode('utf-8')) <= MAX_PROMPT_BYTES)
    return result
