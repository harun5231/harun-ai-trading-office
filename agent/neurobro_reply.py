"""Read a current Neurobro decision without turning conversation into orders.

The caller binds the current operation/coin. Prices come only from an explicit
final summary in this reply; another coin, an old scenario, or unreadable input
cannot supply missing fields. Native JSON remains a separate compatible input.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import math
import re


class ReplyIncomplete(ValueError):
    pass


PAIR = r'[A-Z0-9]{2,20}(?:[/ -]?USDT)'
_PAIR = re.compile(r'(?<![A-Z0-9])(' + PAIR + r')(?![A-Z0-9])', re.I)
_COIN = re.compile(r'^\s*(?:COIN|KOIN|SYMBOL|SIMBOL)\s*[:=]\s*(' + PAIR + r')\b', re.I | re.M)
_COIN_HEADING = re.compile(r'^\s*(' + PAIR + r')\s*(?:[-—:|]\s*|\s+)'
    r'(?:LONG|SHORT|HOLD)\s*[.!]?\s*$', re.I | re.M)
_FINAL = re.compile(r'^\s*(?:ringkasan|keputusan|setup)\s+(?:akhir|final|aktif)\b[^\n]*', re.I | re.M)
_TECHNICAL = re.compile(
    r'(?:\b(?:gambar|screenshot|chart|csv|lampiran|data)\b[^.!?\n]{0,100}'
    r'\b(?:tidak|belum|kurang|gagal|tak)\b[^.!?\n]{0,70}'
    r'\b(?:terbaca|terlihat|lengkap|konsisten|diunggah|diterima|tersedia)\b|'
    r'\b(?:tidak|belum|gagal)\b[^.!?\n]{0,70}'
    r'\b(?:membaca|melihat|menerima|mengakses)\b[^.!?\n]{0,70}'
    r'\b(?:gambar|screenshot|chart|csv|lampiran|data)\b|'
    r'\b(?:belum|tidak)\s+ada\s+(?:gambar|screenshot|chart|csv|lampiran|data)\b|'
    r'\b(?:screenshot|gambar|csv|lampiran|data)\b[^.!?\n]{0,60}'
    r'\b(?:kurang|belum|tidak)\s+(?:cukup|jelas|tajam)\b|'
    r'\b(?:screenshot|gambar|csv|lampiran)\b[^.!?\n]{0,60}'
    r'\b(?:salah|berbeda|bukan)\b)', re.I)


def symbol(value):
    return re.sub(r'[/ -]', '', value).upper()


def clean(reply):
    if not isinstance(reply, str) or not reply.strip():
        raise ReplyIncomplete('NEUROBRO_REPLY_INCOMPLETE')
    # Formatting is not content: preserve all numeric and ordinary text values.
    text = reply.replace('\r\n', '\n').replace('**', '').replace('`', '').strip()
    return re.sub(r'(?m)^\s*(?:#{1,6}\s+|[-*]\s+)', '', text)


def attributed_symbols(reply):
    return {symbol(match.group(1)) for match in _PAIR.finditer(reply)}


def coin_block(reply, requested):
    text = clean(reply)
    if not re.fullmatch(r'[A-Z0-9]{2,20}USDT', requested or ''):
        raise ReplyIncomplete('NEUROBRO_COIN_UNVERIFIED')
    headers = sorted([(match.start(), symbol(match.group(1)))
        for pattern in (_COIN, _COIN_HEADING) for match in pattern.finditer(text)])
    if headers:
        choices = [(start, text[start:headers[index + 1][0]
            if index + 1 < len(headers) else len(text)])
            for index, (start, selected) in enumerate(headers) if selected == requested]
        if not choices:
            raise ReplyIncomplete('NEUROBRO_FOREIGN_COIN')
        # An explicit final summary supersedes an earlier card for this coin.
        finals = [match.start() for match in _FINAL.finditer(text)]
        final = finals[-1] if finals else None
        current = [block for start, block in choices if final is not None and start > final]
        if current:
            return current[-1]
        if len(choices) != 1:
            raise ReplyIncomplete('NEUROBRO_DECISION_CONFLICT')
        if final is not None and final > choices[0][0] and len(headers) == 1:
            return text[final:]  # Same sole coin; an explicit final answer supersedes its card.
        return choices[0][1]
    if attributed_symbols(text) != {requested}:
        raise ReplyIncomplete('NEUROBRO_COIN_UNVERIFIED')
    finals = list(_FINAL.finditer(text))
    return text[finals[-1].start():].strip() if finals else text


def _decision(block, allowed):
    expression = r'\b(' + '|'.join(allowed) + r')\b'
    labelled = re.findall(r'(?:^|[;|\n])\s*(?:ARAH|POSISI|KEPUTUSAN|STATUS|SETUP)'
        r'(?:\s+(?:AKHIR|FINAL|AKTIF))?\s*[:=]\s*' + expression, block, re.I)
    standalone = re.findall(r'^\s*' + expression + r'\s*(?:[.!]|$)', block, re.I | re.M)
    headings = re.findall(r'^\s*' + PAIR + r'\s*(?:[-—:|]\s*|\s+)'
        + expression + r'\s*[.!]?\s*$', block, re.I | re.M)
    values = {value.upper() for value in [*labelled, *standalone, *headings]}
    if len(values) != 1:
        raise ReplyIncomplete('NEUROBRO_DECISION_CONFLICT' if values else 'NEUROBRO_REPLY_INCOMPLETE')
    return values.pop()


def _level(block, names):
    labels = '|'.join(names)
    values = re.findall(r'(?:^|[;|\n])\s*(?:' + labels + r')\s*[:=]\s*([^;|\n]+)', block, re.I)
    if len(values) != 1:
        raise ReplyIncomplete('NEUROBRO_LEVEL_INCOMPLETE')
    text = values[0].strip().removeprefix('$').strip()
    text = re.sub(r'\s*USDT$', '', text, flags=re.I).strip()
    # A range, multiple targets, thousands separator, or scenario annotation
    # needs clarification rather than guessing which price was intended.
    if not re.fullmatch(r'\d{1,32}(?:[.,]\d{1,24})?', text):
        raise ReplyIncomplete('NEUROBRO_LEVEL_AMBIGUOUS')
    if ',' in text:
        if len(text.rsplit(',', 1)[1]) == 3:
            raise ReplyIncomplete('NEUROBRO_LEVEL_AMBIGUOUS')
        text = text.replace(',', '.')
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ReplyIncomplete('NEUROBRO_LEVEL_AMBIGUOUS') from None
    if not value.is_finite() or value <= 0:
        raise ReplyIncomplete('NEUROBRO_LEVEL_AMBIGUOUS')
    return format(value, 'f')


def parse_analysis_reply(reply, requested):
    block = coin_block(reply, requested)
    if _TECHNICAL.search(block):
        raise ReplyIncomplete('NEUROBRO_INPUT_UNREADABLE')
    decision = _decision(block, ('LONG', 'SHORT', 'HOLD'))
    fields = {'symbol': requested, 'side': decision, 'entry_price': None,
        'limit_entry': None, 'stop_loss': None, 'take_profit': None, 'risk_reward': None}
    if decision == 'HOLD':
        return fields
    # Conditional alternatives are not an active order. Ask for a final summary.
    if re.search(r'\b(?:jika|apabila|bersyarat|alternatif|skenario)\b', block, re.I):
        raise ReplyIncomplete('NEUROBRO_SETUP_CONDITIONAL')
    entry = _level(block, ('ENTRY', 'LIMIT ENTRY', 'ENTRY LIMIT', 'ENTRY_PRICE'))
    stop = _level(block, ('SL', 'STOP LOSS', 'STOP_LOSS'))
    target = _level(block, ('TP', 'TAKE PROFIT', 'TAKE_PROFIT'))
    e, sl, tp = map(Decimal, (entry, stop, target))
    if not (sl < e < tp if decision == 'LONG' else tp < e < sl):
        raise ReplyIncomplete('NEUROBRO_LEVEL_ORDER_INVALID')
    # Descriptive ratio only. Quantity, costs and execution TP belong to Risk
    # Manager, which may normalize TP without asking Neurobro to approve it.
    ratio = abs(tp - e) / abs(e - sl)
    fields.update(entry_price=entry, limit_entry=entry, stop_loss=stop,
        take_profit=target, risk_reward=float(ratio))
    return fields


def parse_screening_reply(reply):
    text = clean(reply)
    final = list(_FINAL.finditer(text))
    if final:
        text = text[final[-1].end():].strip()
    labels = re.findall(r'^\s*(?:COIN|KOIN|PILIHAN COIN|COIN PILIHAN)\s*[:=]\s*([^\n]+)',
        text, re.I | re.M)
    if not labels:
        raise ReplyIncomplete('NEUROBRO_SCREENING_INCOMPLETE')
    result = []
    for label in labels:
        # Do not extract pairs from comparison prose or a conditional choice.
        if re.search(r'\b(?:dibanding|alternatif|jika|apabila|atau)\b', label, re.I):
            raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
        pairs = [symbol(match.group(1)) for match in _PAIR.finditer(label)]
        if not pairs:
            raise ReplyIncomplete('NEUROBRO_SCREENING_INCOMPLETE')
        result.extend(pairs)
    if not 1 <= len(result) <= 2 or len(result) != len(set(result)):
        raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
    return {'symbols': result}


def parse_pending_reply(reply, requested):
    block = coin_block(reply, requested)
    if _TECHNICAL.search(block):
        raise ReplyIncomplete('NEUROBRO_INPUT_UNREADABLE')
    if re.search(r'\b(?:jika|apabila|bersyarat|alternatif)\b', block, re.I):
        raise ReplyIncomplete('NEUROBRO_REVIEW_CONDITIONAL')
    return _decision(block, ('TAHAN', 'BATALKAN'))


def parse_reply_for_payload(reply, payload):
    """One shared natural projection for browser and worker receipt validators.

    The outer authenticated receipt still verifies operation, marker, raw reply
    hash and chart witnesses. An order reference is internal caller metadata;
    it is never fabricated as if Neurobro had printed a hash in its answer.
    """
    if type(payload) is not dict or type(payload.get('output_schema')) is not dict:
        raise ReplyIncomplete('NEUROBRO_SCHEMA_UNVERIFIED')
    required = payload['output_schema'].get('required')
    if type(required) is not list or not all(type(key) is str for key in required):
        raise ReplyIncomplete('NEUROBRO_SCHEMA_UNVERIFIED')
    fields = set(required)
    if fields == {'symbols'}:
        parsed = parse_screening_reply(reply)
        limits = payload['output_schema'].get('properties', {}).get('symbols', {})
        minimum, maximum = limits.get('minItems', 1), limits.get('maxItems', 2)
        if (type(minimum) is not int or type(maximum) is not int
                or not 1 <= minimum <= maximum <= 2):
            raise ReplyIncomplete('NEUROBRO_SCHEMA_UNVERIFIED')
        if not minimum <= len(parsed['symbols']) <= maximum:
            raise ReplyIncomplete('NEUROBRO_SCREENING_COUNT_MISMATCH')
    elif fields == {'symbol', 'order_ref', 'decision'}:
        context = payload.get('context') or {}
        pending = context.get('pending_review') or {}
        reference = pending.get('intent_ref')
        requested = pending.get('symbol')
        if (type(reference) is not str or re.fullmatch(r'[a-f0-9]{64}', reference) is None
                or context.get('symbol') != requested
                or re.fullmatch(r'pending-review-v1:' + reference[:32] + r':[1-9][0-9]*',
                    payload.get('operation', '')) is None):
            raise ReplyIncomplete('NEUROBRO_ORDER_UNVERIFIED')
        parsed = {'symbol': requested, 'order_ref': reference,
            'decision': parse_pending_reply(reply, requested)}
    else:
        allowed = {'symbol', 'side', 'entry_price', 'limit_entry', 'stop_loss', 'take_profit', 'risk_reward'}
        if not {'symbol', 'side', 'stop_loss', 'take_profit', 'risk_reward'} <= fields <= allowed:
            raise ReplyIncomplete('NEUROBRO_SCHEMA_UNVERIFIED')
        parsed = parse_analysis_reply(reply, (payload.get('context') or {}).get('symbol'))
    output = {key: parsed[key] for key in required}
    # The real native HTTP encoder emits JSON numbers, and the worker restores
    # decimal values from their JSON lexemes. Never let a float silently shift
    # Neurobro's ENTRY/SL/TP to a nearby representable price.
    for key in ('entry_price', 'limit_entry', 'stop_loss', 'take_profit', 'risk_reward'):
        value = output.get(key)
        if value is None:
            continue
        number = Decimal(str(value))
        if number == number.to_integral_value():
            output[key] = int(number)
        else:
            candidate = float(number)
            if not math.isfinite(candidate) or Decimal(json.dumps(candidate, allow_nan=False)) != number:
                raise ReplyIncomplete('NEUROBRO_LEVEL_PRECISION_UNSUPPORTED')
            output[key] = candidate
    return output
