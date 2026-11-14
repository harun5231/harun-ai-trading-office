"""Read a current Neurobro decision without turning conversation into orders.

The caller binds the current operation/coin. Prices come only from an explicit
final summary in this reply; another coin, an old scenario, or unreadable input
cannot supply missing fields. Native JSON remains a separate compatible input.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
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
            + _COIN_LABEL + r'\$?' + PAIR + r'(?:[ \t]+Perpetual)?\s*\|([^\n]*)', block, re.I | re.M)
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


def _json_decoder(*, preserve_keys=False):
    def unique(items):
        result = {}; seen = set()
        for key, value in items:
            normalized = key.upper()
            if normalized in seen:
                raise ReplyIncomplete('NEUROBRO_JSON_SUMMARY_INVALID')
            seen.add(normalized)
            result[key if preserve_keys else normalized] = value
        return result
    return json.JSONDecoder(object_pairs_hook=unique, parse_float=Decimal,
        parse_int=Decimal, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _json_tail(reply, *, preserve_keys=False):
    """Decode a complete final JSON value without editing its authored bytes.

    Text before the value remains available for contradiction/input checks.
    Objects inside explanation or examples cannot replace the final answer.
    This decoder grants no receipt, material, market or execution authority.
    """
    if not isinstance(reply, str):
        return None
    text = reply.strip()
    fences = list(re.finditer(r'```(?:json)?[ \t]*\n?([\s\S]*?)\n?```', text, re.I))
    if fences and not text[fences[-1].end():].strip():
        body = fences[-1][1].strip()
        if not body.startswith(('{', '[')):
            return None
        try:
            value = _json_decoder(preserve_keys=preserve_keys).decode(body)
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise ReplyIncomplete('NEUROBRO_JSON_SUMMARY_INVALID') from None
        return value, text[:fences[-1].start()].strip()
    # raw_decode owns nested objects, string escapes and number lexemes. A
    # candidate is final only when nothing follows its complete JSON value.
    malformed = False
    for match in re.finditer(r'[\[{]', text):
        start = match.start()
        try:
            value, end = _json_decoder(preserve_keys=preserve_keys).raw_decode(text, start)
        except (ValueError, TypeError, AttributeError, RecursionError):
            if (re.match(r'\{\s*"', text[start:]) and re.search(
                    r'"(?:SYMBOLS|COIN|ARAH|KEPUTUSAN)"[ \t]*:', text[start:], re.I)):
                malformed = True
            continue
        if not text[end:].strip():
            if malformed:
                raise ReplyIncomplete('NEUROBRO_JSON_SUMMARY_INVALID')
            return value, text[:start].strip()
    if malformed:
        raise ReplyIncomplete('NEUROBRO_JSON_SUMMARY_INVALID')
    return None


def _explanation_scope(prefix):
    text = clean(prefix) if prefix.strip() else ''
    if _TECHNICAL.search(text):
        raise ReplyIncomplete('NEUROBRO_INPUT_UNREADABLE')
    finals = list(_FINAL.finditer(text))
    return text[finals[-1].start():] if finals else text


def _public_json_tail(reply, phase):
    tail = _json_tail(reply)
    if tail is None:
        return None
    value, prefix = tail
    if type(value) is not dict:
        return None
    if not {'COIN', 'ARAH', 'SYMBOLS', 'KEPUTUSAN'}.intersection(value):
        return None  # Existing marker-bound native protocol is a separate ABI.
    if phase == 'SCREENING':
        if 'SYMBOLS' not in value:
            raise ReplyIncomplete('NEUROBRO_JSON_PHASE_MISMATCH')
        # Lowercase `symbols` is the historical marker-bound native ABI.
        # Only the explicit public key can authorize an unmarked deterministic
        # WEB summary. Existing real Mini reviews can still read either form.
        authored = _json_tail(reply, preserve_keys=True)[0]
        if 'SYMBOLS' not in authored:
            return None
    elif phase == 'ANALYSIS':
        if not {'COIN', 'ARAH'} <= set(value):
            raise ReplyIncomplete('NEUROBRO_JSON_PHASE_MISMATCH')
    elif phase == 'PENDING_REVIEW':
        if not {'COIN', 'KEPUTUSAN'} <= set(value):
            raise ReplyIncomplete('NEUROBRO_JSON_PHASE_MISMATCH')
    _explanation_scope(prefix)  # Material complaints cannot disappear in JSON.
    return value, prefix


def _same_analysis_json(left, right):
    if set(left) != set(right):
        return False
    if any(type(left.get(key)) is not str or type(right.get(key)) is not str
            for key in ('COIN', 'ARAH')):
        return False
    if symbol(left['COIN']) != symbol(right['COIN']) or left['ARAH'].upper() != right['ARAH'].upper():
        return False
    try:
        return all(left[key] is None and right[key] is None or
            left[key] is not None and right[key] is not None and
            Decimal(str(left[key]).replace(',', '.')) == Decimal(str(right[key]).replace(',', '.'))
            for key in ('ENTRY', 'TP', 'SL'))
    except (InvalidOperation, KeyError):
        return False


def _future_reassessment_clause(text):
    return (not text.rstrip().endswith(':')
        and re.search(r'\b(?:jika|apabila|asalkan)\b', text, re.I)
        and re.search(r'\b(?:nanti|berikutnya|mendatang)\b', text, re.I)
        and re.search(r'\b(?:evaluasi|tinjau|bahas|review)\b', text, re.I)
        and not re.search(r'\b(?:bersyarat|alternatif|skenario)\b', text, re.I))


def _conditional_json_intro(scope, allowed):
    """A condition on the decision differs from a separate future caveat."""
    intro = scope.strip().split('\n')[-1]
    conditional = r'\b(?:jika|apabila|asalkan|bersyarat|alternatif|skenario)\b'
    if re.search(conditional, intro, re.I):
        if not _future_reassessment_clause(intro):
            return True
    direction = '(?:' + '|'.join(allowed) + ')'
    return re.search(r'(?:^|[;|\n])\s*(?:(?:ARAH|POSISI|KEPUTUSAN|STATUS|SETUP)'
        r'(?:\s+(?:AKHIR|FINAL|AKTIF))?\s*[:=]\s*)?' + direction
        + r'\b[^.!?;|\n]*\b(?:jika|apabila|asalkan|bersyarat)\b', scope, re.I) is not None


def _final_json_analysis(reply):
    """Project an authored final public summary into the established ABI."""
    tail = _public_json_tail(reply, 'ANALYSIS')
    if tail is None:
        return None
    value, prefix = tail
    if (type(value) is not dict or set(value) != {'COIN', 'ARAH', 'ENTRY', 'TP', 'SL'}
            or type(value['COIN']) is not str or type(value['ARAH']) is not str
            or re.fullmatch(PAIR, value['COIN'], re.I) is None
            or value['ARAH'].upper() not in {'LONG', 'SHORT', 'HOLD'}):
        raise ReplyIncomplete('NEUROBRO_JSON_SUMMARY_INVALID')
    direction = value['ARAH'].upper()
    selected = symbol(value['COIN'])
    scope = _explanation_scope(prefix)
    # Retain the existing final-heading boundary: it may supersede older
    # scenarios, but a competing decision/level in this final scope cannot be
    # silently overridden by the machine-readable version of that decision.
    if scope.strip():
        previous = _json_tail(scope)
        if (previous is not None and type(previous[0]) is dict
                and {'COIN', 'ARAH'} <= set(previous[0])
                and not _same_analysis_json(previous[0], value)):
            raise ReplyIncomplete('NEUROBRO_DECISION_CONFLICT')
        try:
            prose = _inline_summary_fields(coin_block(scope, selected))
        except ReplyIncomplete as error:
            if str(error) == 'NEUROBRO_FOREIGN_COIN':
                raise ReplyIncomplete('NEUROBRO_DECISION_CONFLICT') from None
            prose = scope
        try:
            prior_direction = _decision(prose, ('LONG', 'SHORT', 'HOLD'))
        except ReplyIncomplete as error:
            if str(error) == 'NEUROBRO_DECISION_CONFLICT':
                raise
        else:
            if prior_direction != direction:
                raise ReplyIncomplete('NEUROBRO_DECISION_CONFLICT')
        if _conditional_json_intro(scope, ('LONG', 'SHORT', 'HOLD')):
            raise ReplyIncomplete('NEUROBRO_SETUP_CONDITIONAL')
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
        if scope.strip():
            names = {'ENTRY': ('ENTRY', 'LIMIT ENTRY', 'ENTRY LIMIT', 'ENTRY_PRICE'),
                'TP': ('TP', 'TAKE PROFIT', 'TAKE_PROFIT'),
                'SL': ('SL', 'STOP LOSS', 'STOP_LOSS')}[key]
            if re.search(r'(?:^|[;|\n])\s*(?:' + '|'.join(names) + r')\s*[:=]', prose, re.I):
                if Decimal(_level(prose, names)) != Decimal(levels[key].replace(',', '.')):
                    raise ReplyIncomplete('NEUROBRO_DECISION_CONFLICT')
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


def author_market_hold(reply, requested):
    """Prove a present authored HOLD without vetoing future market scenarios.

    Parsing still owns the current coin/final-summary, input and direction
    conflict checks. A condition attached to the direction (or leading into it)
    is not a final decision. A later sentence about a possible future entry is
    distinct from the author's current no-entry decision.
    """
    if not isinstance(reply, str) or not isinstance(requested, str):
        return False
    try:
        if parse_analysis_reply(reply, requested)['side'] != 'HOLD':
            return False
        tail = _public_json_tail(reply, 'ANALYSIS')
        if tail is not None:
            # JSON is another presentation of the authored decision, not
            # proof that contrary unlabelled prose can be discarded. Keep
            # ambiguous present direction cues with the existing semantic
            # review; clear HOLD and separate future reassessment stay final.
            scope = _explanation_scope(tail[1])
            for clause in re.split(r'[;|\n]|[.!?](?=\s|$)', scope):
                if (re.search(r'\b(?:LONG|SHORT)\b', clause, re.I)
                        and not _future_reassessment_clause(clause)):
                    return False
        block = _inline_summary_fields(analysis_coin_block(reply, requested))
    except ReplyIncomplete:
        return False
    # A final coin-qualified summary is stronger than an earlier narrative
    # direction in this same reply. Full input/conflict checks above still run
    # before selecting this positive authored boundary.
    summaries = [match for match in re.finditer(r'^\s*Ringkasan(?:\s+(?:akhir|final|aktif))?\s*[:=]\s*'
        + _COIN_LABEL + r'\$?(' + PAIR + r')\b', block, re.I | re.M)
        if symbol(match.group(1)) == requested]
    if summaries:
        block = block[summaries[-1].start():]
    # These are the same direction presentations accepted by _decision. Keep
    # offsets in the authored current block; do not search chat history or
    # turn a model's HOLD token into analyst authority.
    labelled = list(re.finditer(r'(?:^|[;|\n])\s*(?:ARAH|POSISI|KEPUTUSAN|STATUS|SETUP)'
        r'(?:\s+(?:AKHIR|FINAL|AKTIF))?\s*[:=]\s*HOLD\b', block, re.I))
    plain = list(re.finditer(r'^\s*(?:' + PAIR + r'\s*(?:[-—:|]\s*|\s+))?'
        r'HOLD(?=\s*(?:[.!]|$))', block, re.I | re.M))
    pipes = []
    for match in re.finditer(r'^\s*(?:Ringkasan(?:\s+(?:akhir|final|aktif))?\s*[:=]\s*)?'
            + _COIN_LABEL + r'\$?' + PAIR + r'(?:[ \t]+Perpetual)?\s*\|([^\n]*)', block, re.I | re.M):
        for cell in re.finditer(r'(?:^|\|)\s*HOLD\s*(?=\||$)', match.group(1), re.I):
            pipes.append((match.start(1) + cell.start(), match.start(1) + cell.end()))
    directions = [(match.start(), match.end()) for match in [*labelled, *plain]] + pipes
    if not directions:
        return False
    text = block if summaries else clean(reply)
    final = list(_FINAL.finditer(text))
    if final:
        text = text[final[-1].start():]
    final_heading = r'\s*(?:(?:ringkasan|keputusan|setup)\s+(?:akhir|final|aktif)\s*:?)?\s*'
    headers = sorted(match.start() for pattern in (_COIN, _COIN_HEADING)
        for match in pattern.finditer(text))
    if headers and not re.fullmatch(final_heading, text[:headers[0]], re.I):
        return False
    first = min(start for start, _ in directions)
    summaries = list(re.finditer(r'^\s*Ringkasan(?:\s+(?:akhir|final|aktif))?\s*[:=]',
        block[:first], re.I | re.M))
    scope = summaries[-1].end() if summaries else 0
    preamble = block[scope:first]
    # A coin/final-summary heading is positive scope. Arbitrary prose leading
    # into HOLD, including an unfamiliar conditional phrase, stays with Mini.
    heading = (final_heading + r'(?:(?:Ringkasan\s*[:=]?\s*)?(?:'
        + _COIN_LABEL + r')?\$?' + PAIR + r'(?:[ \t]+Perpetual)?\s*\|?)?\s*')
    if not re.fullmatch(heading, preamble, re.I):
        return False
    for _, end in directions:
        # Only the exact present direction is authoritative here. Reasons and
        # unfamiliar inline wording are interpreted by the existing dialogue;
        # a separate future clause after punctuation/newline remains separate.
        suffix = re.split(r'[;|\n]|[.!](?=\s|$)', block[end:], maxsplit=1)[0]
        if not re.fullmatch(r'\s*(?:sekarang|(?:untuk\s+)?saat\s+ini)?\s*', suffix, re.I):
            return False
    return True


def _screening_strong_final(text):
    strong = (_FINAL.search(text) is not None or _SCREENING_PICK.search(text) is not None
        or re.search(r'^[ \t]*(?:(?:COIN|KOIN)[ \t]+YANG[ \t]+DIPILIH|PILIHAN COIN|COIN PILIHAN)'
            r'(?:[ \t]*\([12]\))?[ \t]*[:=][ \t]*$', text, re.I | re.M) is not None)
    labels = list(re.finditer(r'^[ \t]*(?:COIN|KOIN)[ \t]*[:=][ \t]*[^\n]+', text, re.I | re.M))
    if len(labels) == 1 and labels[0].start() == 0:
        tail = text[labels[0].end():]
        competing = re.search(r'\b(?:pilihan|rekomendasi|daftar)[ \t]+(?:akhir|final)\b|\bYANG[ \t]+DIPILIH\b', tail, re.I)
        strong = strong or competing is None
    return strong


def _screening_explicit_alias_conflict(scope, selected):
    """Compare explicit final fields, never ticker mentions in their reasons.

    Actual Mini/source verification already owns the interpretation. This only
    preserves the existing contradiction guard for labelled standalone
    cashtags that the strict pair parser cannot read. It completes no ticker.
    """
    identity = r'(?:\$?' + PAIR + r'|\$[A-Z0-9]{2,20})'
    choices = identity + r'(?:[ \t]*(?:,|&|\bdan\b)[ \t]*' + identity + r')?'
    label = r'(?:COIN|KOIN|PILIHAN COIN|COIN PILIHAN)(?:[ \t]*\([12]\))?[ \t]*[:=]'
    final = r'(?:ringkasan|keputusan|setup)[ \t]+(?:akhir|final|aktif)'

    def identities(body):
        clause = re.split(r';|[ \t]+(?:karena|[-—])[ \t]+', body, maxsplit=1, flags=re.I)[0].strip()
        clause = re.sub(r'[.!]$', '', clause).strip()
        return re.findall(identity, clause, re.I) if re.fullmatch(choices, clause, re.I) else None

    def compare(tokens):
        authored = []
        for token in tokens:
            plain = token.removeprefix('$')
            if re.fullmatch(PAIR, plain, re.I):
                authored.append(symbol(plain))
                continue
            base = plain.upper()
            if re.fullmatch('[0-9]+[KMBT]?', base):
                return  # A money amount is not a source coin identity.
            matches = [pair for pair in selected if pair[:-4] == base]
            if len(matches) != 1:
                raise ReplyIncomplete('NEUROBRO_SCREENING_DECISION_CONFLICT')
            authored.append(matches[0])
        if authored != selected:
            raise ReplyIncomplete('NEUROBRO_SCREENING_DECISION_CONFLICT')

    # Complete explicit clauses are independent finals. Read the choice part
    # only, without consuming any comparison/reason after its clear boundary.
    clauses = final + r'[ \t]*[:=]|pilihan[ \t]+(?:akhir|final)[ \t]+saya[ \t]+'
    for match in re.finditer(r'^[ \t]*(?:' + clauses + r')[ \t]*([^\n]+)', scope, re.I | re.M):
        tokens = identities(match[1])
        if tokens:
            compare(tokens)

    # Aggregate adjacent COIN fields/identity rows in their explicitly named
    # final selection section. A later reason or comparison section ends it.
    lines = scope.strip().splitlines()
    if lines and re.fullmatch(final + r'[ \t]*:?', lines[0].strip(), re.I):
        lines = lines[1:]
    picks = [index for index, line in enumerate(lines) if _SCREENING_PICK.fullmatch(line)
        or re.fullmatch(r'[ \t]*(?:COIN|KOIN)[ \t]+YANG[ \t]+DIPILIH[ \t]*[:=]?[ \t]*', line, re.I)]
    sections = [(lines, False)] if _screening_strong_final(scope) else []
    sections.extend((lines[index + 1:], True) for index in picks)
    for section, named in sections:
        tokens = []
        for line in section:
            if not line.strip():
                continue
            field = re.fullmatch(r'[ \t]*' + label + r'[ \t]*(.+)', line, re.I)
            row = identities(field[1] if field else line) if field or named else None
            if row is None:
                break
            tokens.extend(row)
        if tokens:
            compare(tokens)


def _screening_public_summary(value, prefix, *, semantic_symbols=None):
    """Validate an authored summary; aliases require the actual Mini reading.

    The caller first validates the Mini receipt and independent source
    attribution. This helper only compares a bare base with those already
    admitted USDT pairs, without rewriting the analyst reply or its hashes.
    The deterministic/public parser never supplies this capability.
    """
    if set(value) != {'SYMBOLS'} or type(value['SYMBOLS']) is not list:
        raise ReplyIncomplete('NEUROBRO_JSON_SUMMARY_INVALID')
    selected = []
    for choice in value['SYMBOLS']:
        if type(choice) is not str:
            raise ReplyIncomplete('NEUROBRO_JSON_SUMMARY_INVALID')
        if re.fullmatch(PAIR, choice, re.I) is not None:
            selected.append(symbol(choice))
            continue
        # A public quote separator is presentation only after the actual
        # Mini/source checks have already admitted that exact USDT pair. Do
        # not remove underscores inside a base or grant aliases to the local
        # deterministic parser.
        matches = ([pair for pair in semantic_symbols
            if choice.strip().upper() in (pair, pair[:-4], pair[:-4] + '_USDT')]
            if semantic_symbols is not None else [])
        if len(matches) != 1:
            raise ReplyIncomplete('NEUROBRO_JSON_SUMMARY_INVALID')
        selected.append(matches[0])
    if not 1 <= len(selected) <= 2 or len(selected) != len(set(selected)):
        raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
    scope = _explanation_scope(prefix)
    # Compare explicit prior decisions using the same whole-token quote
    # presentation. This validation-only view never changes source bytes or
    # supplies attribution/selection authority to the deterministic parser.
    comparison_scope = (re.sub(r'(?<![A-Z0-9_])([A-Z0-9]{2,20})_USDT(?![A-Z0-9_])',
        r'\1USDT', scope, flags=re.I) if semantic_symbols is not None else scope)
    if scope.strip():
        # A comparison table is not another final choice. Preserve the same
        # prefix/conditional checks for both full pairs and semantic aliases.
        prior_json = _json_tail(comparison_scope)
        prior_summary = (prior_json is not None and type(prior_json[0]) is dict
            and 'SYMBOLS' in prior_json[0])
        if _screening_strong_final(comparison_scope) or prior_summary:
            try:
                previous = (_screening_public_summary(*prior_json, semantic_symbols=semantic_symbols)
                    if semantic_symbols is not None and prior_summary else parse_screening_reply(comparison_scope))
            except ReplyIncomplete as error:
                if semantic_symbols is not None and prior_summary or str(error) in (
                        'NEUROBRO_SCREENING_AMBIGUOUS', 'NEUROBRO_SCREENING_DECISION_CONFLICT'):
                    raise
            else:
                if previous['symbols'] != selected:
                    raise ReplyIncomplete('NEUROBRO_SCREENING_DECISION_CONFLICT')
        if semantic_symbols is not None:
            _screening_explicit_alias_conflict(comparison_scope, selected)
        if _conditional_json_intro(comparison_scope, ('LONG', 'SHORT', 'HOLD')):
            raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
    return {'symbols': selected}


def parse_screening_reply(reply):
    structured = _public_json_tail(reply, 'SCREENING')
    if structured is not None:
        return _screening_public_summary(*structured)
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


def _screening_json_choices(reply, *, semantic_symbols=None):
    """Recognize explicit authored coin fields, never prices or comparisons."""
    # The public final object may follow an explanation without a heading.
    # Keep the complete original prefix: trimming its final heading first can
    # hide an authored same-line choice. Aliases are available only after the
    # actual Mini/source validation in screening_interpretation.
    tail = _json_tail(reply.strip(), preserve_keys=True)
    if tail is not None and type(tail[0]) is dict and 'SYMBOLS' in tail[0]:
        structured = _public_json_tail(reply, 'SCREENING')
        return _screening_public_summary(*structured, semantic_symbols=semantic_symbols)['symbols']
    text=reply.strip();final=list(_FINAL.finditer(text))
    if final:text=text[final[-1].end():].strip()
    fences=list(re.finditer(r'```(?:json)?[ \t]*\n?([\s\S]*?)\n?```',text,re.I))
    blocks=[match[1].strip()for match in fences]if fences else[text]
    found=[]
    def unique(items):
        value={}
        for key,item in items:
            key=key.upper()
            if key in value:raise ReplyIncomplete('NEUROBRO_JSON_SUMMARY_INVALID')
            value[key]=item
        return value
    for block in blocks:
        if not block.startswith(('{','[')):continue
        try:value=json.loads(block,object_pairs_hook=unique,
            parse_constant=lambda _:(_ for _ in()).throw(ValueError()))
        except(ValueError,TypeError,AttributeError):raise ReplyIncomplete('NEUROBRO_JSON_SUMMARY_INVALID')from None
        if type(value)is dict and type(value.get('SYMBOLS'))is list:choices=value['SYMBOLS']
        elif type(value)is dict and type(value.get('COIN'))is str:choices=[value['COIN']]
        elif type(value)is list and value and all(type(item)is dict and type(item.get('COIN'))is str for item in value):
            choices=[item['COIN']for item in value]
        else:continue
        selected=[]
        for choice in choices:
            match=re.fullmatch('('+PAIR+r')(?:[ \t]+Perpetual)?',choice.strip(),re.I)if type(choice)is str else None
            if match is None:raise ReplyIncomplete('NEUROBRO_JSON_SUMMARY_INVALID')
            selected.append(symbol(match[1]))
        if not 1<=len(selected)<=2 or len(set(selected))!=len(selected):
            raise ReplyIncomplete('NEUROBRO_SCREENING_AMBIGUOUS')
        found.append(selected)
    if found and any(value!=found[-1]for value in found):
        raise ReplyIncomplete('NEUROBRO_SCREENING_DECISION_CONFLICT')
    return found[-1]if found else None


def screening_interpretation(reply, payload, request, response, turns):
    """Use the actual Mini reading of a source-bound screening conversation.

    This projects only chosen coin identities. It never supplies a price,
    changes the analyst reply, or replaces a CLARIFY/HOLD with a selection.
    Native receipt ownership and the live Binance catalog remain caller guards.
    """
    def need(value,code='NEUROBRO_SCREENING_INTERPRETATION_UNVERIFIED'):
        if not value:raise ReplyIncomplete(code)
    digest=lambda value:hashlib.sha256(json.dumps(value,sort_keys=True,
        separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()
    need(type(reply)is str and type(payload)is dict and type(request)is dict
        and type(response)is dict and type(turns)is list and bool(turns))
    need(all(type(row)is dict and type(row.get('text'))is str for row in turns))
    need(type(payload.get('operation'))is str and type(payload.get('request_sha256'))is str
        and re.fullmatch('[a-f0-9]{64}',payload['request_sha256']))
    schema=payload.get('output_schema',{})
    need(type(schema)is dict and schema.get('required')==['symbols'])
    reply_sha=hashlib.sha256(reply.encode()).hexdigest()
    need(request.get('schema_version')==1 and type(request.get('schema_version'))is int
        and request.get('gate')=={'robot_on':True,'native_dispatch_verified':True}
        and all(type(flag)is bool for flag in request['gate'].values())
        and request.get('phase')=='SCREENING'and request.get('market_context')is None
        and request.get('reply')==reply and request.get('reply_sha256')==reply_sha
        and turns[-1].get('text')==reply and turns[-1].get('complete')is True)
    expected=digest(dict(operation=payload.get('operation'),request_sha256=payload.get('request_sha256'),
        reply_sha256=reply_sha,phase='SCREENING',transcript_sha256=digest(turns)))
    need(request.get('request_id')==expected)
    # Both callers already validate their actual Mini receipt. Keep this pure
    # shared projection usable by the worker without importing a model runtime.
    need(response.get('schema_version')==1 and type(response.get('schema_version'))is int
        and response.get('ok')is True and response.get('authority')=='DIALOGUE_CONTROL_ONLY'
        and response.get('trading_authority')is False
        and response.get('reason')=='DIALOGUE_DECISION'
        and all(response.get(key)==request.get(key)for key in('request_id','conversation_id','phase','reply_sha256'))
        and type(response.get('decision'))is dict)
    decision=response['decision']
    need(set(decision)=={'action','symbols','question'}and type(decision['symbols'])is list
        and 1<=len(decision['symbols'])<=2 and all(type(s)is str for s in decision['symbols'])
        and len(decision['symbols'])==len(set(decision['symbols'])))
    need(decision['action']=='SCREENING'and response['needs_followup']is False
        and decision['question']=='','NEUROBRO_SCREENING_INTERPRETATION_INCOMPLETE')
    selected=list(decision['symbols'])
    limits=schema.get('properties',{}).get('symbols',{})
    minimum,maximum=limits.get('minItems',1),limits.get('maxItems',2)
    need(type(minimum)is int and type(maximum)is int and 1<=minimum<=maximum<=2,
        'NEUROBRO_SCHEMA_UNVERIFIED')
    need(minimum<=len(selected)<=maximum,'NEUROBRO_SCREENING_COUNT_MISMATCH')
    pattern=limits.get('items',{}).get('pattern',r'^[A-Z0-9]{2,18}USDT$')
    need(type(pattern)is str and all(re.fullmatch(pattern,value)for value in selected),
        'NEUROBRO_SCREENING_INTERPRETATION_UNVERIFIED')
    mentioned=attributed_symbols(reply)
    for choice in selected:
        base=choice[:-4]
        # Match the established Mini standalone-cashtag attribution contract;
        # dollar amounts and compound foreign pairs do not name a USDT coin.
        if not re.fullmatch('[0-9]+[KMBT]?',base)and re.search(
                r'''(?:^|(?<=[\s,;:!?()\[\]{}*'"`]))\$(?ai:'''+re.escape(base)
                +r''')(?=\.?(?:$|[\s,;:!?()\[\]{}*'"`]))''',reply):
            mentioned.add(choice)
    need(set(selected)<=mentioned,'NEUROBRO_SCREENING_INTERPRETATION_UNVERIFIED')
    context=payload.get('context')or{}
    need(type(context)is dict,'NEUROBRO_SCHEMA_UNVERIFIED')
    if 'requested_count'in context:
        need(type(context['requested_count'])is int and len(selected)==context['requested_count'],
            'NEUROBRO_SCREENING_COUNT_MISMATCH')
    excluded=context.get('excluded_symbols',[])
    need(type(excluded)is list and all(type(value)is str for value in excluded),
        'NEUROBRO_SCHEMA_UNVERIFIED')
    need(not set(selected).intersection(excluded),'NEUROBRO_SCREENING_EXCLUDED_SYMBOL')
    # An already unambiguous explicit final choice must not be contradicted.
    # Failure to fit a local text layout is not a second semantic decision:
    # Mini has read the original paragraph/table/JSON and identified its choices.
    text=clean(reply)
    strong_final = _screening_strong_final(text)
    try:explicit=parse_screening_reply(reply)if strong_final else None
    except ReplyIncomplete:explicit=None
    if explicit is not None:
        need(explicit['symbols']==selected,'NEUROBRO_SCREENING_DECISION_CONFLICT')
    structured=_screening_json_choices(reply, semantic_symbols=selected)
    if structured is not None:
        need(structured==selected,'NEUROBRO_SCREENING_DECISION_CONFLICT')
    return {'symbols':selected}


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
    structured = _public_json_tail(reply, 'PENDING_REVIEW')
    if structured is not None:
        value, prefix = structured
        if (set(value) != {'COIN', 'KEPUTUSAN'} or type(value['COIN']) is not str
                or re.fullmatch(PAIR, value['COIN'], re.I) is None
                or type(value['KEPUTUSAN']) is not str
                or value['KEPUTUSAN'].upper() not in ('TAHAN', 'BATALKAN')):
            raise ReplyIncomplete('NEUROBRO_JSON_SUMMARY_INVALID')
        if symbol(value['COIN']) != requested:
            raise ReplyIncomplete('NEUROBRO_FOREIGN_COIN')
        decision = value['KEPUTUSAN'].upper()
        scope = _explanation_scope(prefix)
        if scope.strip():
            prior_json = _json_tail(scope)
            prior_json = (prior_json if prior_json is not None and type(prior_json[0]) is dict
                and {'COIN', 'KEPUTUSAN'} <= set(prior_json[0]) else None)
            try:
                block = coin_block(scope, requested)
            except ReplyIncomplete as error:
                if str(error) == 'NEUROBRO_FOREIGN_COIN':
                    raise
                block = scope
            try:
                prior = (parse_pending_reply(scope, requested) if prior_json is not None
                    else _decision(block, ('TAHAN', 'BATALKAN')))
            except ReplyIncomplete as error:
                if str(error) in ('NEUROBRO_DECISION_CONFLICT', 'NEUROBRO_REVIEW_CONDITIONAL'):
                    raise
            else:
                if prior != decision:
                    raise ReplyIncomplete('NEUROBRO_DECISION_CONFLICT')
            if _conditional_json_intro(scope, ('TAHAN', 'BATALKAN')):
                raise ReplyIncomplete('NEUROBRO_REVIEW_CONDITIONAL')
        return decision
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
        context = payload.get('context') or {}
        if type(context) is not dict:
            raise ReplyIncomplete('NEUROBRO_SCHEMA_UNVERIFIED')
        if 'requested_count' in context and (type(context['requested_count']) is not int
                or context['requested_count'] != len(parsed['symbols'])):
            raise ReplyIncomplete('NEUROBRO_SCREENING_COUNT_MISMATCH')
        excluded = context.get('excluded_symbols', [])
        if type(excluded) is not list or any(type(item) is not str for item in excluded):
            raise ReplyIncomplete('NEUROBRO_SCHEMA_UNVERIFIED')
        if set(parsed['symbols']).intersection(excluded):
            raise ReplyIncomplete('NEUROBRO_SCREENING_EXCLUDED_SYMBOL')
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
