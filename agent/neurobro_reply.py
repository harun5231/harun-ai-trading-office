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
_COIN_LABEL = r'(?:COIN|KOIN|SYMBOL|SIMBOL)(?:\s*[:=]\s*|[ \t]+)'
_COIN = re.compile(r'^\s*' + _COIN_LABEL + '(' + PAIR + r')\b', re.I | re.M)
_COIN_HEADING = re.compile(r'^\s*(' + PAIR + r')\s*(?:[-—:|]\s*|\s+)'
    r'(?:LONG|SHORT|HOLD)\s*[.!]?\s*$', re.I | re.M)
_FINAL = re.compile(r'^\s*(?:ringkasan|keputusan|setup)\s+(?:akhir|final|aktif)\b[^\n]*', re.I | re.M)
_SCREENING_PICK = re.compile(r'^[ \t]*(?:RINGKASAN|DAFTAR)[ \t]+PICKS?[ \t]*:?[ \t]*$', re.I | re.M)
# Negating proof of a visual candle count does not negate readability. The
# outer material/receipt checks still own actual CSV counts and chart coverage;
# a separate missing, unreadable, inconsistent or wrong-material complaint
# remains a technical failure even when this count caveat is also present.
_VISUAL_COUNT_PROOF = (r'(?:dapat[ \t]+)?membuktikan[ \t]+(?:bahwa[ \t]+)?'
    r'(?:(?:seluruh|semua|sebanyak|setidaknya|minimal|sekurang-kurangnya)[ \t]+)?'
    r'\d{1,4}[ \t]+(?:candle|candlestick|bar)[ \t]+'
    r'(?:tertutup[ \t]+)?(?:terlihat|tampak|kelihatan)\b')
_TECHNICAL = re.compile(
    r'(?:\b(?:gambar|screenshot|chart|csv|lampiran|data)\b[^.!?\n]{0,100}'
    r'\b(?:tidak|belum|kurang|gagal|tak)\b(?![ \t]+' + _VISUAL_COUNT_PROOF + r')'
    r'[^.!?\n]{0,70}'
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
    # Unlabelled numeric USDT values are money amounts, including a regex
    # match starting after a decimal point (2.50 USDT). Explicit COIN labels
    # retain their original authority; catalog validation still owns listings.
    values = {symbol(match.group(1)) for match in _PAIR.finditer(reply)
        if not symbol(match.group(1))[:-4].isdigit()}
    values.update(symbol(value) for value in re.findall(
        r'\b' + _COIN_LABEL + r'\$?(' + PAIR + r')\b', reply, re.I))
    return values


def coin_block(reply, requested):
    text = clean(reply)
    if not re.fullmatch(r'[A-Z0-9]{2,20}USDT', requested or ''):
        raise ReplyIncomplete('NEUROBRO_COIN_UNVERIFIED')
    headers = sorted([(match.start(), symbol(match.group(1)))
        for pattern in (_COIN, _COIN_HEADING) for match in pattern.finditer(text)])
    if headers:
        # A general input complaint before any coin header applies to the
        # material as a whole; a new whitespace header must not hide it.
        if _TECHNICAL.search(text[:headers[0][0]]):
            raise ReplyIncomplete('NEUROBRO_INPUT_UNREADABLE')
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
    # A native human summary may use a bare direction cell after its explicit
    # coin, while the full coin block still owns input/conflict/condition checks.
    summary = []
    if set(allowed) == {'LONG', 'SHORT', 'HOLD'}:
        lines = re.findall(r'^\s*(?:Ringkasan(?:\s+(?:akhir|final|aktif))?\s*[:=]\s*)?'
            + _COIN_LABEL + r'\$?' + PAIR + r'\s*\|([^\n]*)', block, re.I | re.M)
        for line in lines:
            for field in line.split('|'):
                match = re.fullmatch(expression, field.strip(), re.I)
                if match:
                    summary.append(match.group(1))
    values = {value.upper() for value in [*labelled, *standalone, *headings, *summary]}
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


def _inline_summary_fields(block):
    # Native textContent may flatten a structured COIN/ARAH/ENTRY/TP/SL
    # summary into one line. Restore field boundaries only for that explicit
    # coin-qualified form; ordinary prose keeps its original interpretation.
    direction = (r'(?:ARAH|POSISI|KEPUTUSAN|STATUS|SETUP)'
        r'(?:[ \t]+(?:AKHIR|FINAL|AKTIF))?[ \t]*[:=][ \t]*(?:LONG|SHORT|HOLD)\b')
    prefix = re.compile(r'^[ \t]*(?:Ringkasan[ \t]+)?' + _COIN_LABEL + r'\$?(' + PAIR + r')'
        r'(?:[ \t]+Perpetual)?[ \t]+' + direction, re.I)
    labels = (r'(?:ARAH|POSISI|KEPUTUSAN|STATUS|SETUP)(?:[ \t]+(?:AKHIR|FINAL|AKTIF))?'
        r'|LIMIT ENTRY|ENTRY LIMIT|ENTRY_PRICE|ENTRY|STOP LOSS|STOP_LOSS|SL'
        r'|TAKE PROFIT|TAKE_PROFIT|TP')
    pipe = re.compile(r'^[ \t]*(?:Ringkasan(?:[ \t]+(?:akhir|final|aktif))?[ \t]*[:=][ \t]*)?'
        + _COIN_LABEL + r'\$?(' + PAIR + r')(?:[ \t]+Perpetual)?[ \t]*\|', re.I)
    level_labels = r'LIMIT ENTRY|ENTRY LIMIT|ENTRY_PRICE|ENTRY|STOP LOSS|STOP_LOSS|SL|TAKE PROFIT|TAKE_PROFIT|TP'
    lines = []
    for line in block.split('\n'):
        selected = pipe.match(line)
        if selected:
            if attributed_symbols(line) != {symbol(selected.group(1))}:
                raise ReplyIncomplete('NEUROBRO_COIN_UNVERIFIED')
            cells = line.split('|')
            for index in range(1, len(cells)):
                # A labelled price cell may use a space rather than a colon.
                # Preserve its entire value so ranges, prose and conflicting
                # levels still fail the ordinary full-block checks below.
                field = re.fullmatch(r'([ \t]*(?:' + level_labels + r'))[ \t]+([^ \t:=].*)', cells[index], re.I)
                if field:
                    cells[index] = field.group(1) + ': ' + field.group(2)
            line = '|'.join(cells)
        match = prefix.match(line)
        if match:
            if attributed_symbols(line) != {symbol(match.group(1))}:
                raise ReplyIncomplete('NEUROBRO_COIN_UNVERIFIED')
            # A labelled RR-only annotation is metadata, not part of TP. Keep
            # every other suffix intact so ranges, extra prices and prose fail.
            line = re.sub(r'[ \t]+-[ \t]+(R[ \t]*:[ \t]*R(?:[ \t]+bersih)?[ \t]+'
                r'\d{1,16}(?:[.,]\d{1,16})?[ \t]*:[ \t]*\d{1,16}(?:[.,]\d{1,16})?)'
                r'(?=[ \t]+(?:' + labels + r')[ \t]*[:=]|[ \t]*$)',
                r'\n\1', line, flags=re.I)
            line = re.sub(r'[ \t]+(?=(?:' + labels + r')[ \t]*[:=])', '\n', line, flags=re.I)
        lines.append(line)
    return '\n'.join(lines)


def _final_json_analysis(reply):
    """Read only an author's short final summary, never JSON inside a scenario.

    The browser still binds the reply to the current operation and its native
    receipt. This is an alternative presentation of the same authored fields,
    not permission to supply missing prices from model output or chat history.
    """
    if not isinstance(reply, str):
        return None
    text = reply.strip()
    # An explicit final heading supersedes older cards/scenarios in this reply,
    # just as the prose parser does. Do not change price lexemes while stripping
    # Markdown. Input complaints still cannot disappear behind a JSON object.
    headings = list(re.finditer(r'(?im)^\s*(?:#{1,6}\s+)?(?:\*\*)?'
        r'(?:ringkasan|keputusan|setup)\s+(?:akhir|final|aktif)\b(?:\*\*)?'
        r'[ \t]*:?[ \t]*(?:\n|(?=\{)|(?=```))', text))
    if headings:
        prefix, text = text[:headings[-1].end()], text[headings[-1].end():].strip()
        if _TECHNICAL.search(prefix):
            raise ReplyIncomplete('NEUROBRO_INPUT_UNREADABLE')
    fenced = re.fullmatch(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text, re.I)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith('{'):
        return None
    def unique(items):
        result = {}
        for key, value in items:
            key = key.upper()
            if key in result:
                raise ReplyIncomplete('NEUROBRO_JSON_SUMMARY_INVALID')
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=unique, parse_float=Decimal,
            parse_int=Decimal, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, AttributeError):
        raise ReplyIncomplete('NEUROBRO_JSON_SUMMARY_INVALID') from None
    if type(value) is dict and not {'COIN', 'ARAH'}.intersection(value):
        return None  # Existing marker-bound native protocol is a separate ABI.
    if (type(value) is not dict or set(value) != {'COIN', 'ARAH', 'ENTRY', 'TP', 'SL'}
            or type(value['COIN']) is not str or type(value['ARAH']) is not str
            or re.fullmatch(PAIR, value['COIN'], re.I) is None
            or value['ARAH'].upper() not in {'LONG', 'SHORT', 'HOLD'}):
        raise ReplyIncomplete('NEUROBRO_JSON_SUMMARY_INVALID')
    direction = value['ARAH'].upper()
    if direction == 'HOLD':
        if any(value[key] is not None for key in ('ENTRY', 'TP', 'SL')):
            raise ReplyIncomplete('NEUROBRO_DECISION_CONFLICT')
        return 'COIN: ' + value['COIN'] + '\nARAH: HOLD'
    levels = {}
    for key in ('ENTRY', 'TP', 'SL'):
        price = value[key]
        if type(price) not in (str, Decimal):
            raise ReplyIncomplete('NEUROBRO_LEVEL_INCOMPLETE')
        levels[key] = format(price, 'f') if type(price) is Decimal else price
        if not re.fullmatch(r'\d{1,32}(?:[.,]\d{1,24})?', levels[key]):
            raise ReplyIncomplete('NEUROBRO_LEVEL_AMBIGUOUS')
    return ('COIN: ' + value['COIN'] + '\nARAH: ' + direction + '\n'
        + '\n'.join(key + ': ' + levels[key] for key in ('ENTRY', 'TP', 'SL')))


def analysis_coin_block(reply, requested):
    summary = _final_json_analysis(reply)
    return coin_block(reply if summary is None else summary, requested)


def parse_analysis_reply(reply, requested):
    block = analysis_coin_block(reply, requested)
    if _TECHNICAL.search(block):
        raise ReplyIncomplete('NEUROBRO_INPUT_UNREADABLE')
    block = _inline_summary_fields(block)
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
    named = sorted([*re.finditer(r'^[ \t]*(?:COIN|KOIN)[ \t]+YANG[ \t]+DIPILIH'
        r'[ \t]*[:=][ \t]*$', text, re.I | re.M), *_SCREENING_PICK.finditer(text)],
        key=lambda heading: heading.start())
    if named:
        if len(named) != 1:
            raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
        heading = named[0]
        selected = _screening_named_choices(text[heading.end():])
        prefix = text[:heading.start()].strip()
        # A separate explicit selection cannot silently disagree with this
        # summary. Bare tickers in an earlier market table supply no identity.
        if re.search(r'^[ \t]*(?:COIN|KOIN|PILIHAN COIN|COIN PILIHAN)'
                r'(?:[ \t]*\(([12])\))?[ \t]*[:=]', prefix, re.I | re.M):
            if parse_screening_reply(prefix)['symbols'] != selected:
                raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
        # A condition immediately introducing the list is not a final choice.
        if re.search(r'\b(?:jika|apabila|bersyarat|alternatif)\b',
                prefix.rsplit('\n\n', 1)[-1], re.I):
            raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
        return {'symbols': selected}
    labels = list(re.finditer(r'^[ \t]*(?:COIN|KOIN|PILIHAN COIN|COIN PILIHAN)'
        r'(?:[ \t]*\(([12])\))?[ \t]*[:=][ \t]*([^\n]*)$', text, re.I | re.M))
    if not labels:
        raise ReplyIncomplete('NEUROBRO_SCREENING_INCOMPLETE')
    result = []
    for index, label in enumerate(labels):
        count, content = label.groups()
        if not content:
            # The native renderer preserves table columns as tabs. Read only
            # the explicitly labelled selection table's coin column, never
            # pairs mentioned in its reasons, comparisons or earlier prose.
            end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
            section = text[label.end():end]
            if re.search(r'\b(?:jika|apabila|bersyarat|alternatif)\b',
                    text[:label.start()].strip().rsplit('\n\n', 1)[-1], re.I):
                raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
            first = section.strip().splitlines()[0] if section.strip() else ''
            table = re.match(r'^(?:COIN|KOIN|SYMBOL|SIMBOL)(?:\t|[ \t]*\||$)', first.strip('|').strip(), re.I)
            selected = _screening_table(section) if table else _screening_named_choices(section)
            if count is not None and len(selected) != int(count):
                raise ReplyIncomplete('NEUROBRO_SCREENING_COUNT_MISMATCH')
            result.extend(selected)
            continue
        # Do not extract pairs from comparison prose or a conditional choice.
        if re.search(r'\b(?:dibanding|alternatif|jika|apabila|atau)\b', content, re.I):
            raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
        pairs = [symbol(match.group(1)) for match in _PAIR.finditer(content)]
        if not pairs:
            raise ReplyIncomplete('NEUROBRO_SCREENING_INCOMPLETE')
        if count is not None and len(pairs) != int(count):
            raise ReplyIncomplete('NEUROBRO_SCREENING_COUNT_MISMATCH')
        result.extend(pairs)
    if not 1 <= len(result) <= 2 or len(result) != len(set(result)):
        raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
    return {'symbols': result}


def _screening_named_choices(block, *, details=False):
    """Read explicit pair identities from a labelled human selection list."""
    row = re.compile(r'^[ \t]*(?:\$?([A-Z0-9]{2,20})[ \t]*:[ \t]*)?'
        r'\$?(' + PAIR + r')(?:[ \t]+Perpetual)?'
        r'(?:[ \t]+[-—][ \t]+([^\n]+))?[ \t]*$', re.I)
    selected = []; reasons = []
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        match = row.fullmatch(line)
        if match is None:
            if not selected:
                raise ReplyIncomplete('NEUROBRO_SCREENING_INCOMPLETE')
            if (re.search(r'\b(?:jika|apabila|bersyarat|alternatif)\b', line, re.I)
                    or (re.match(r'^[ \t]*\$?[A-Z0-9]{2,20}[ \t]*:', line, re.I)
                        and _PAIR.search(line))
                    or _PAIR.match(line.strip().removeprefix('$'))):
                raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
            # Do not conceal another selection after an intervening paragraph.
            remainder = '\n'.join(lines[index:])
            if any(row.fullmatch(item) for item in lines[index + 1:]) or re.search(
                    r'^[ \t]*(?:COIN|KOIN|PILIHAN COIN|COIN PILIHAN)'
                    r'(?:[ \t]*\([12]\))?[ \t]*[:=]', remainder, re.I | re.M):
                raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
            break
        base, pair, reason = match.groups()
        choice = symbol(pair)
        if (base is not None and (base.upper() if base.upper().endswith('USDT')
                else base.upper() + 'USDT') != choice):
            raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
        if reason and (re.search(r'\b(?:dibanding|alternatif|jika|apabila|atau|bersyarat|skenario)\b', reason, re.I)
                or attributed_symbols(reason) - {choice}):
            raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
        selected.append(choice)
        reasons.append(reason)
    if not 1 <= len(selected) <= 2 or len(selected) != len(set(selected)):
        raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
    return (selected, reasons) if details else selected


def screening_market_hold(reply, selected):
    """Only explicit final per-coin HOLD rows make every selected coin held."""
    try:
        text = clean(reply)
        if _TECHNICAL.search(text) or parse_screening_reply(text)['symbols'] != selected:
            return False
        headings = sorted([*re.finditer(r'^[ \t]*(?:(?:COIN|KOIN)(?:[ \t]+YANG[ \t]+DIPILIH)?|PILIHAN COIN|COIN PILIHAN)'
            r'(?:[ \t]*\([12]\))?[ \t]*[:=][ \t]*$', text, re.I | re.M), *_SCREENING_PICK.finditer(text)],
            key=lambda heading: heading.start())
        if not headings:
            return False
        choices, reasons = _screening_named_choices(text[headings[-1].end():], details=True)
    except ReplyIncomplete:
        return False
    return choices == selected and all(type(reason) is str
        and re.match(r'^HOLD\b', reason, re.I)
        and not re.search(r'\b(?:LONG|SHORT|jika|apabila|bersyarat|alternatif|skenario)\b', reason, re.I)
        for reason in reasons)


def _screening_table(block):
    lines = block.strip().splitlines()
    if not lines:
        raise ReplyIncomplete('NEUROBRO_SCREENING_INCOMPLETE')

    def cells(line):
        return [cell.strip() for cell in re.split(r'\t|\|', line.strip().strip('|'))]

    if cells(lines[0])[0].upper() not in {'COIN', 'KOIN', 'SYMBOL', 'SIMBOL'}:
        raise ReplyIncomplete('NEUROBRO_SCREENING_INCOMPLETE')
    selected = []
    for line in lines[1:]:
        if not line.strip():
            break  # A later paragraph is not another selection-table row.
        row = cells(line)
        if all(re.fullmatch(r':?-{3,}:?', cell) for cell in row):
            continue  # Markdown separator; the native DOM renderer omits it.
        value = row[0]
        match = re.fullmatch(r'(?:\$([A-Z0-9]{2,20})[ \t]+\((' + PAIR
            + r')\)|(' + PAIR + r'))', value, re.I)
        if match is None:
            raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
        base, named, direct = match.groups()
        choice = symbol(named or direct)
        if base is not None and base.upper() + 'USDT' != choice:
            raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
        selected.append(choice)
    if not selected:
        raise ReplyIncomplete('NEUROBRO_SCREENING_INCOMPLETE')
    return selected


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
    from .native_evaluation_protocol import is_evaluation_schema, envelope
    if is_evaluation_schema(payload['output_schema']):
        envelope(payload)
        from worker.trade_evaluation_evidence import parse_evaluation_reply
        return parse_evaluation_reply(reply)
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
