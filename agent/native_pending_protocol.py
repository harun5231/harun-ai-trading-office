"""Add an exact, separate pending-LIMIT review protocol to native browser code.

This module supplies validation only. It cannot fetch charts, send a browser
message, change ROBOT state, create an order, or cancel an order. The native
browser still supplies its original assistant-text/receipt equality checks.
Legacy screening and analysis always delegate to their original validators.

Only standard-library modules are imported here so the same file can be copied
into both the existing browser and the existing worker package. Worker imports
are resolved lazily against the native module's actual package name.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import functools
import hashlib
import importlib
import json
import math
import re
import time


SYMBOL_PATTERN = r'^[A-Z0-9]{2,18}USDT$'
SHA_PATTERN = r'^[a-f0-9]{64}$'
OPERATION_PATTERN = r'pending-review-v1:([a-f0-9]{32}):([1-9][0-9]*)\Z'
REVIEW_SCHEMA = {
    'type': 'object',
    'properties': {
        'symbol': {'type': 'string', 'pattern': SYMBOL_PATTERN},
        'order_ref': {'type': 'string', 'pattern': SHA_PATTERN},
        'decision': {'type': 'string', 'enum': ['TAHAN', 'BATALKAN']},
    },
    'required': ['symbol', 'order_ref', 'decision'],
    'additionalProperties': False,
}
_INTERVALS = {'1h': 3_600_000, '15m': 900_000}
_CANDLE_FIELDS = {'open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time'}
_CHART_FIELDS = {'source', 'symbol', 'timeframe', 'captured_at', 'sha256',
                 'bytes', 'attachment_id'}
_NUMBER = re.compile(r'\d{1,32}(?:\.\d{1,24})?\Z', re.ASCII)
_UTC_ISO = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z', re.ASCII)
_INSTALLED = '_harun_pending_review_protocol'


def is_pending_schema(schema):
    """Equality with one closed schema; a matching required-list is insufficient."""
    return type(schema) is dict and schema == REVIEW_SCHEMA


def _positive_instant(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= 10**13


def _utc_instant(value):
    if type(value) is not str or _UTC_ISO.fullmatch(value) is None:
        raise ValueError('PENDING_REVIEW_TIME_UNVERIFIED')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError('PENDING_REVIEW_TIME_UNVERIFIED')
    result = parsed.timestamp()
    if not _positive_instant(result):
        raise ValueError('PENDING_REVIEW_TIME_UNVERIFIED')
    return result


def _number(value, *, zero=False):
    if type(value) is not str or _NUMBER.fullmatch(value) is None:
        raise ValueError('PENDING_REVIEW_MARKET_UNVERIFIED')
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError('PENDING_REVIEW_MARKET_UNVERIFIED') from None
    if not number.is_finite() or (number < 0 if zero else number <= 0):
        raise ValueError('PENDING_REVIEW_MARKET_UNVERIFIED')
    return number


def _context_valid(context, *, operation=None, now=None, max_age=180, created=None):
    """Validate copied public native facts, not a trading/cancellation authority.

    ``now`` is an explicit historical receive time for receipt validation; an old
    completed receipt is not silently reinterpreted against today's wall clock.
    The worker's independent dispatch verifier enforces the due five-hour review,
    native intent ownership, current ROBOT ON and durable claim identity.
    """
    try:
        now = time.time() if now is None else now
        if (not _positive_instant(now) or type(max_age) is not int
                or not 30 <= max_age <= 300 or type(context) is not dict
                or context.get('source') != 'Binance Futures'):
            return False
        symbol = context.get('symbol')
        if type(symbol) is not str or re.fullmatch(SYMBOL_PATTERN, symbol, re.ASCII) is None:
            return False
        pending = context.get('pending_review')
        if (type(pending) is not dict
                or set(pending) != {'intent_ref', 'symbol', 'entry_created_at'}
                or pending['symbol'] != symbol or type(pending['intent_ref']) is not str
                or re.fullmatch(SHA_PATTERN, pending['intent_ref'], re.ASCII) is None
                or _utc_instant(pending['entry_created_at']) > now):
            return False
        if operation is not None:
            match = re.fullmatch(OPERATION_PATTERN, operation, re.ASCII) if type(operation) is str else None
            if match is None or match.group(1) != pending['intent_ref'][:32]:
                return False
        observed, server = context.get('fetched_at'), context.get('server_time')
        if (not _positive_instant(observed) or not 0 <= now - observed <= max_age
                or type(server) is not int or not 0 < server <= 10**16
                or abs(server / 1000 - observed) > 30
                or not -30 <= now - server / 1000 <= max_age
                or (created is not None and observed > created)):
            return False
        frames = context.get('timeframes')
        if type(frames) is not dict or set(frames) != set(_INTERVALS):
            return False
        for timeframe, interval in _INTERVALS.items():
            frame = frames[timeframe]
            if (type(frame) is not dict or set(frame) != {'symbol', 'timeframe', 'candles'}
                    or frame['symbol'] != symbol or frame['timeframe'] != timeframe
                    or type(frame['candles']) is not list
                    or not 20 <= len(frame['candles']) <= 500):
                return False
            previous = None
            for candle in frame['candles']:
                if type(candle) is not dict or set(candle) != _CANDLE_FIELDS:
                    return False
                opened, closed = candle['open_time'], candle['close_time']
                if (type(opened) is not int or type(closed) is not int or opened < 0
                        or opened % interval or closed != opened + interval - 1
                        or opened > server or (previous is not None and opened - previous != interval)):
                    return False
                opened_price, high, low, close = (_number(candle[key])
                    for key in ('open', 'high', 'low', 'close'))
                _number(candle['volume'], zero=True)
                if not low <= min(opened_price, close) <= max(opened_price, close) <= high:
                    return False
                previous = opened
            last = frame['candles'][-1]
            if (not last['open_time'] <= server <= last['close_time']
                    or now * 1000 > last['close_time'] + max_age * 1000):
                return False
        return True
    except (KeyError, ValueError, TypeError, OverflowError, AttributeError):
        return False


def closed_review_materials(context):
    """Current reviews need 200 closed bars; historical v1 receipts stay valid.

    The native ABI keeps its running candle. Screenshot/CSV conversion selects
    exactly the latest 200 closed rows and exposes the running row separately.
    This checks available material, not whether an image actually shows it.
    """
    try:
        server = context['server_time']
        return all(sum(candle['close_time'] < server for candle in
            context['timeframes'][timeframe]['candles']) >= 200 for timeframe in _INTERVALS)
    except (KeyError, TypeError):
        return False


def is_pending_browser_request(value):
    """Recognize only the new exact review schema/namespace/native context."""
    return (type(value) is dict and is_pending_schema(value.get('output_schema'))
            and type(value.get('operation')) is str
            and re.fullmatch(OPERATION_PATTERN, value['operation'], re.ASCII) is not None
            and _context_valid(value.get('context'), operation=value.get('operation')))


def dialogue_phase_for_payload(payload, required):
    """Classify an admitted operation without resetting its original clock.

    A long discussion becoming stale must not turn into an ANALYSIS phase.
    Request admission and every send retain their independent freshness checks;
    this structural binding alone cannot dispatch or cancel anything.
    """
    try:
        if (type(required) is list and len(required) == 3
                and set(required) == {'symbol', 'order_ref', 'decision'}
                and type(payload) is dict and is_pending_schema(payload.get('output_schema'))):
            operation = payload.get('operation')
            match = re.fullmatch(OPERATION_PATTERN, operation, re.ASCII) if type(operation) is str else None
            context = payload.get('context')
            pending = context.get('pending_review') if type(context) is dict else None
            if (match is not None and type(pending) is dict
                    and set(pending) == {'intent_ref', 'symbol', 'entry_created_at'}
                    and type(pending['intent_ref']) is str
                    and re.fullmatch(SHA_PATTERN, pending['intent_ref'], re.ASCII)
                    and match.group(1) == pending['intent_ref'][:32]
                    and pending['symbol'] == context.get('symbol')
                    and re.fullmatch(SYMBOL_PATTERN, pending['symbol'], re.ASCII)
                    and _utc_instant(pending['entry_created_at']) > 0):
                return 'PENDING_REVIEW'
    except (KeyError, TypeError, ValueError):
        pass
    return 'SCREENING' if type(required) is list and set(required) == {'symbols'} else 'ANALYSIS'


def _canonical_pending(value, exception_type):
    if (type(value) is not dict or set(value) != {'symbol', 'order_ref', 'decision'}
            or type(value['symbol']) is not str
            or re.fullmatch(SYMBOL_PATTERN, value['symbol'], re.ASCII) is None
            or type(value['order_ref']) is not str
            or re.fullmatch(SHA_PATTERN, value['order_ref'], re.ASCII) is None
            or type(value['decision']) is not str or value['decision'] not in ('TAHAN', 'BATALKAN')):
        raise exception_type('INVALID_PENDING_REVIEW_OUTPUT')
    # Never derive a decision, levels or replacement symbol from model prose.
    return json.dumps({key: value[key] for key in ('symbol', 'order_ref', 'decision')},
                      sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _native_module(namespace, name):
    module_name = namespace.get('__name__')
    if type(module_name) is not str or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+', module_name):
        raise ValueError('NATIVE_PENDING_MODULE_UNVERIFIED')
    return importlib.import_module(module_name.split('.')[0] + '.' + name)


def _install_canonical(namespace):
    api = _native_module(namespace, 'neuroapi')
    original = api.canonical_output
    if getattr(original, _INSTALLED, False) is not True:
        exception_type = namespace.get('Review', getattr(api, 'Review', ValueError))

        @functools.wraps(original)
        def canonical(value, schema, catalog=None):
            if is_pending_schema(schema):
                return _canonical_pending(value, exception_type)
            return original(value, schema, catalog)

        setattr(canonical, _INSTALLED, True)
        api.canonical_output = canonical
    # neurobrowser imports a direct alias before its additive installer runs.
    if 'canonical_output' in namespace:
        namespace['canonical_output'] = api.canonical_output


def install_dom_protocol(namespace):
    """Extend native DOM request-kind admission; native image parsing stays first."""
    original = namespace.get('request_kind')
    not_submitted = namespace.get('NotSubmitted')
    if not callable(original) or not callable(not_submitted):
        raise ValueError('NATIVE_PENDING_DOM_UNVERIFIED')
    if getattr(original, _INSTALLED, False) is True:
        return

    @functools.wraps(original)
    def request_kind(payload, artifacts):
        if not (type(payload) is dict and is_pending_schema(payload.get('output_schema'))):
            return original(payload, artifacts)
        try:
            if not is_pending_browser_request(payload) or type(artifacts) is not list or len(artifacts) != 2:
                raise ValueError
            frames, hashes = set(), set()
            for artifact in artifacts:
                if type(artifact) not in (tuple, list) or len(artifact) != 3:
                    raise ValueError
                _, raw, witness = artifact
                if (type(raw) is not bytes or type(witness) is not dict
                        or set(witness) not in (_CHART_FIELDS, _CHART_FIELDS - {'attachment_id'})
                        or witness['symbol'] != payload['context']['symbol']
                        or witness['timeframe'] not in _INTERVALS
                        or witness['timeframe'] in frames or type(witness['bytes']) is not int
                        or witness['bytes'] != len(raw) or not raw
                        or type(witness['sha256']) is not str
                        or re.fullmatch(SHA_PATTERN, witness['sha256'], re.ASCII) is None
                        or hashlib.sha256(raw).hexdigest() != witness['sha256']
                        or witness['sha256'] in hashes):
                    raise ValueError
                frames.add(witness['timeframe'])
                hashes.add(witness['sha256'])
            if frames != set(_INTERVALS):
                raise ValueError
            return 'PENDING_REVIEW'
        except Exception:
            raise not_submitted('UNAVAILABLE') from None

    setattr(request_kind, _INSTALLED, True)
    namespace['request_kind'] = request_kind


def install_source_protocol(namespace):
    """Extend native receipt/chart validation for one closed decision schema."""
    original = namespace.get('validate_chart_evidence')
    exception_type = namespace.get('Review')
    if (not callable(original) or not callable(exception_type)
            or type(namespace.get('CHART_SOURCE')) is not str
            or type(namespace.get('MAX_CHART_BYTES')) is not int
            or namespace['MAX_CHART_BYTES'] <= 0):
        raise ValueError('NATIVE_PENDING_SOURCE_UNVERIFIED')
    _install_canonical(namespace)
    if getattr(original, _INSTALLED, False) is True:
        return

    @functools.wraps(original)
    def validate_chart_evidence(value, schema, context, created, received_at, max_age):
        if not is_pending_schema(schema):
            return original(value, schema, context, created, received_at, max_age)
        try:
            # New conversations refresh chart material while keeping the exact
            # original request/hash immutable. That initial context is a fact
            # at creation; final chart freshness is still checked at receipt.
            validation_at = (created if type(context) is dict
                and context.get('material_contract') == 'OWNER_MATERIALS_V4' else received_at)
            if (not _positive_instant(created) or not _positive_instant(received_at)
                    or received_at < created or type(max_age) is not int or not 30 <= max_age <= 300
                    or not _context_valid(context, now=validation_at, max_age=max_age, created=created)
                    or type(value) is not list or len(value) != 2):
                raise ValueError
            timeframes, attachments, digests = set(), set(), set()
            for row in value:
                if (type(row) is not dict or set(row) != _CHART_FIELDS
                        or row['source'] != namespace['CHART_SOURCE']
                        or row['symbol'] != context['symbol'] or row['timeframe'] not in _INTERVALS
                        or row['timeframe'] in timeframes or type(row['bytes']) is not int
                        or not 1 <= row['bytes'] <= namespace['MAX_CHART_BYTES']
                        or type(row['sha256']) is not str
                        or re.fullmatch(SHA_PATTERN, row['sha256'], re.ASCII) is None
                        or row['sha256'] in digests or type(row['attachment_id']) is not str
                        or re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', row['attachment_id'], re.ASCII) is None
                        or row['attachment_id'] in attachments):
                    raise ValueError
                captured = _utc_instant(row['captured_at'])
                if not created - 5 <= captured <= received_at or not 0 <= received_at - captured <= max_age:
                    raise ValueError
                timeframes.add(row['timeframe'])
                attachments.add(row['attachment_id'])
                digests.add(row['sha256'])
            if timeframes != set(_INTERVALS):
                raise ValueError
            return value
        except Exception:
            raise exception_type('INVALID_PENDING_REVIEW_CHART_EVIDENCE') from None

    setattr(validate_chart_evidence, _INSTALLED, True)
    namespace['validate_chart_evidence'] = validate_chart_evidence


def install_worker_browser_protocol(namespace):
    """Add a separately authenticated pending namespace, never fake ANALYSIS."""
    original = namespace.get('verify_dispatch')
    if not callable(original):
        raise ValueError('NATIVE_PENDING_WORKER_UNVERIFIED')
    _install_canonical(namespace)
    if getattr(original, _INSTALLED, False) is True:
        return

    @functools.wraps(original)
    def verify_dispatch(db, operation, request_marker, request_sha256, today, now, *, max_age=180):
        # Preserve the real dispatch checker and its legacy result exactly.
        legacy = original(db, operation, request_marker, request_sha256, today, now, max_age=max_age)
        if type(operation) is not str or not operation.startswith('pending-review-v1:'):
            return legacy
        if re.fullmatch(OPERATION_PATTERN, operation, re.ASCII) is None:
            return False
        try:
            # Importing this offline validation module is not an application
            # startup, provider call, browser dispatch, or gateway execution.
            pending = importlib.import_module('agent.native_pending_review')
            return pending.verify_pending_dispatch(db, operation, request_marker,
                request_sha256, today, now, max_age=max_age) is True
        except Exception:
            return False

    setattr(verify_dispatch, _INSTALLED, True)
    namespace['verify_dispatch'] = verify_dispatch
