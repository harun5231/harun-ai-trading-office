"""Lossless request packing within NeuroAPI's published character limits."""
import json

from .core import Review

MAX_CHARACTERS = 32_000
MAX_MESSAGES = 50
LIMIT_ERROR = 'NEUROAPI_REQUEST_LIMIT_EXCEEDED'
ANALYSIS_FORMAT = 'ANALYSIS_CONTEXT_PARTS_V1'
FRAGMENT_FORMAT = 'JSON_CONTEXT_FRAGMENTS_V1'
ANALYSIS_INSTRUCTION = (
    'All numbered user messages form ONE complete analysis context. Merge the metadata '
    'with each timeframe; concatenate its candle chunks in candle_start order. '
    'Every original candle, price, timestamp, risk constraint and filter is included. '
    'Use both complete timeframes together; no part replaces or truncates another.'
)
FRAGMENT_INSTRUCTION = (
    'All numbered user messages form ONE complete JSON context. Concatenate '
    'context_json_fragment strings in part order, then parse that complete JSON. '
    'Fragments are consecutive text, not independent or truncated contexts.'
)


def _encode(value):
    # New partitions use actual Unicode characters: the provider limits characters,
    # not UTF-8 bytes. The historical single-message representation is kept below.
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False)


def _packet(kind, data, *, part=MAX_MESSAGES, parts=MAX_MESSAGES, **details):
    header = dict(format=ANALYSIS_FORMAT, part=part, parts=parts, kind=kind, **details)
    if kind == 'metadata':
        header['instruction'] = ANALYSIS_INSTRUCTION
    return dict(context_partition=header, data=data)


def _analysis_parts(context):
    """Return valid-JSON candle partitions, or defer unusual shapes to raw packing."""
    if not isinstance(context, dict) or not isinstance(context.get('timeframes'), dict):
        return None
    frames = context['timeframes']
    if set(frames) != {'1h', '15m'} or any(
            not isinstance(frame, dict) or not isinstance(frame.get('candles'), list)
            or not frame['candles'] for frame in frames.values()):
        return None
    packets = [_packet('metadata', {key: value for key, value in context.items() if key != 'timeframes'})]
    if len(_encode(packets[0])) > MAX_CHARACTERS:
        return None
    for timeframe, frame in frames.items():
        candles = frame['candles']
        start = 0
        while start < len(candles):
            def candidate(end):
                data = {**frame, 'candles': candles[start:end]}
                return _packet('timeframe', data, timeframe=timeframe, candle_start=start,
                               candle_end=end, candle_total=len(candles))
            low, high = start + 1, len(candles)
            end = start
            while low <= high:
                middle = (low + high) // 2
                if len(_encode(candidate(middle))) <= MAX_CHARACTERS:
                    end = middle
                    low = middle + 1
                else:
                    high = middle - 1
            if end == start or len(packets) >= MAX_MESSAGES:
                return None
            packets.append(candidate(end))
            start = end
    for index, packet in enumerate(packets, 1):
        packet['context_partition'].update(part=index, parts=len(packets))
    return [_encode(packet) for packet in packets]


def _raw_parts(serialized):
    """Fallback preserves the exact historical JSON text by concatenation."""
    fragments = []
    start = 0
    def candidate(end, *, part=MAX_MESSAGES, parts=MAX_MESSAGES):
        return dict(context_partition=dict(format=FRAGMENT_FORMAT, part=part, parts=parts,
                                           instruction=FRAGMENT_INSTRUCTION),
                    context_json_fragment=serialized[start:end])
    while start < len(serialized):
        if len(fragments) >= MAX_MESSAGES:
            raise Review(LIMIT_ERROR)
        low, high = start + 1, min(len(serialized), start + MAX_CHARACTERS)
        end = start
        while low <= high:
            middle = (low + high) // 2
            if len(_encode(candidate(middle))) <= MAX_CHARACTERS:
                end = middle
                low = middle + 1
            else:
                high = middle - 1
        if end == start:
            raise Review(LIMIT_ERROR)
        fragments.append(serialized[start:end])
        start = end
    return [_encode(dict(context_partition=dict(format=FRAGMENT_FORMAT, part=index,
                                                parts=len(fragments), instruction=FRAGMENT_INSTRUCTION),
                         context_json_fragment=fragment))
            for index, fragment in enumerate(fragments, 1)]


def build_request_body(prompt, schema, context=None):
    """Keep small requests identical; partition oversized contexts without data loss."""
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= MAX_CHARACTERS:
        raise Review(LIMIT_ERROR)
    body = {'prompt': prompt, 'mode': 'smart', 'stream': False, 'output_schema': schema}
    if context is None:
        return body
    serialized = json.dumps(context, separators=(',', ':'))
    if len(serialized) <= MAX_CHARACTERS:
        contents = [serialized]
    else:
        contents = _analysis_parts(context)
        if contents is None:
            contents = _raw_parts(serialized)
    if not 1 <= len(contents) <= MAX_MESSAGES or any(
            not 1 <= len(content) <= MAX_CHARACTERS for content in contents):
        raise Review(LIMIT_ERROR)
    body['message_history'] = [{'role': 'user', 'content': content} for content in contents]
    return body
