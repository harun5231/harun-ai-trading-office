"""Bound natural replies and material freshness for the owner V4 conversation.

No service is started and no order, setting, provider or database is changed by
this module. The existing native source-evidence reader remains authoritative.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import os
import re
import stat
import time

from .core import Review


MATERIAL_CONTRACT = 'OWNER_MATERIALS_V4'
NEW_OPERATION_SECONDS = 900
WEBSITE_SOURCE = 'BINANCE_USDM_FUTURES_WEBSITE_SCREENSHOT'
RELEASE_ARTIFACT = '/data/trading/owner-policy-release.json'
RELEASE_VERSION = 'OWNER_POLICY_RELEASE_V4'
RELEASE_POLICY = 'DYNAMIC_NET_2R_V4'



def load_owner_release_cutoff(path=RELEASE_ARTIFACT):
    """Read only the operator's private, actual-cutover artifact at startup.

    Builds never choose this timestamp. The existing installer writes it
    atomically immediately before activation, keeping historical DATA_RENDER
    receipts valid until that cutover without admitting old live attachments.
    """
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_uid not in (0, 10001) or stat.S_IMODE(metadata.st_mode) != 0o600
                or not 0 < metadata.st_size <= 4096):
            raise ValueError
        raw = os.read(descriptor, 4097)
        if len(raw) != metadata.st_size or len(raw) > 4096:
            raise ValueError
        def distinct(pairs):
            value = dict(pairs)
            if len(value) != len(pairs):
                raise ValueError
            return value
        value = json.loads(raw, object_pairs_hook=distinct)
        if (type(value) is not dict or set(value) != {'version', 'released_at', 'office_commit', 'policy'}
                or value['version'] != RELEASE_VERSION or value['policy'] != RELEASE_POLICY
                or type(value['office_commit']) is not str
                or re.fullmatch(r'[a-f0-9]{40}', value['office_commit']) is None
                or type(value['released_at']) is not str
                or re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)',
                    value['released_at']) is None):
            raise ValueError
        released = _instant(value['released_at'])
        if not _instant('2026-10-22T16:46:25Z') <= released <= time.time() + 5:
            raise ValueError
        return value['released_at']
    except Exception:
        raise Review('OWNER_RELEASE_ARTIFACT_UNVERIFIED') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def owner_material_request(body):
    """Only the new exact schema or explicitly versioned pending material ABI."""
    from .neuroapi import SETUP_SCHEMA_V4
    from agent.native_pending_protocol import is_pending_schema
    if type(body) is not dict:
        return False
    if body.get('output_schema') == SETUP_SCHEMA_V4:
        return True
    return (is_pending_schema(body.get('output_schema'))
        and type(body.get('context')) is dict
        and body['context'].get('material_contract') == MATERIAL_CONTRACT)


def parse_bound_assistant_text(text, body, legacy_parser, *, max_assistant_bytes):
    """Read the same raw reply at live validation and historical re-validation.

    Native JSON keeps its original parser and decimal lexemes. Natural language
    is projected by the same per-coin parser as the browser, using the immutable
    operation payload rather than a fabricated JSON message attributed to it.
    """
    if (not isinstance(text, str) or not text or type(max_assistant_bytes) is not int
            or max_assistant_bytes <= 0 or len(text.encode('utf-8')) > max_assistant_bytes):
        raise Review('INVALID_OUTPUT_SCHEMA')
    try:
        return legacy_parser(text)
    except Exception:
        pass
    try:
        from agent.neurobro_reply import parse_reply_for_payload
        parsed = parse_reply_for_payload(text, body)
        # Browser JSON floats are restored as Decimal by the native HTTP reader.
        # Repeat only that wire conversion; never round ENTRY/SL/TP here.
        return json.loads(json.dumps(parsed, ensure_ascii=False, allow_nan=False),
            parse_float=Decimal, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except Exception:
        raise Review('INVALID_OUTPUT_SCHEMA') from None


def _instant(value):
    if type(value) is not str:
        raise Review('STALE_MARKET_CONTEXT')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        raise Review('STALE_MARKET_CONTEXT') from None


def completed_decision_fresh(db, operation, market, symbol=None, *, now=None,
                             max_age=180, refresh=False, source_reader=None, browser_source=None):
    """Verify final accepted material, then optionally perform public GETs.

    Initial market facts are immutable request witnesses. V4 discussions may
    refresh their screenshots during the same operation; the final *native*
    receipt must still be recent now, and each website witness must be fresh
    both at receipt and now. Old receipts retain their old initial-context age.
    ``refresh=True`` is for an already permitted caller; it cannot bypass that
    caller's research-read guard or authorize a financial operation.
    """
    current = time.time() if now is None else now
    if (type(current) not in (int, float) or not math.isfinite(current) or current <= 0
            or type(max_age) is not int or not 30 <= max_age <= 300):
        raise Review('STALE_MARKET_CONTEXT')
    request = db.execute('SELECT request_body,request_sha256 FROM research_browser_requests '
        'WHERE operation=?', (operation,)).fetchone()
    journal = db.execute('SELECT state,body_hash FROM api_requests WHERE operation=?', (operation,)).fetchone()
    receipt = db.execute('SELECT received_at,chart_evidence,chart_max_age '
        'FROM research_browser_responses WHERE operation=?', (operation,)).fetchone()
    if (not request or not journal or not receipt or journal['state'] != 'COMPLETE'
            or journal['body_hash'] != request['request_sha256']):
        raise Review('ROBOT_REQUEST_NEEDS_REVIEW')
    body = json.loads(request['request_body'])
    context = body.get('context')
    selected = symbol if symbol is not None else (context or {}).get('symbol')
    if (type(context) is not dict or context.get('symbol') != selected
            or type(selected) is not str or re.fullmatch(r'[A-Z0-9]{2,18}USDT', selected) is None):
        raise Review('MARKET_CONTEXT_MISMATCH')
    # Reconstruct and recheck every original native marker, response/output
    # hash and chart binding. This helper does not replace that authority.
    if source_reader is None:
        from .research_provider import source_evidence, BROWSER_SOURCE
        source_reader, browser_source = source_evidence, BROWSER_SOURCE
    if not callable(source_reader) or type(browser_source) is not str:
        raise Review('ORDER_EVIDENCE_UNVERIFIED')
    evidence = source_reader(db, operation)
    if (type(evidence) is not dict or evidence.get('source_type') != browser_source
            or type(evidence.get('provider_evidence_sha256')) is not str
            or re.fullmatch(r'[a-f0-9]{64}', evidence['provider_evidence_sha256']) is None):
        raise Review('ORDER_EVIDENCE_UNVERIFIED')
    if not owner_material_request(body):
        market.fresh(context, selected)
        return deepcopy(context) if refresh else True
    final_max_age = min(max_age, 180)
    received = receipt['received_at']
    if (type(received) not in (int, float) or not math.isfinite(received)
            or not 0 <= current - received <= final_max_age
            or type(receipt['chart_max_age']) is not int
            or not 30 <= receipt['chart_max_age'] <= 300):
        raise Review('STALE_MARKET_CONTEXT')
    charts = json.loads(receipt['chart_evidence'])
    if (type(charts) is not list or len(charts) != 2
            or {row.get('timeframe') for row in charts if type(row) is dict} != {'1h', '15m'}):
        raise Review('NEUROBRO_CHART_EVIDENCE_INVALID')
    for chart in charts:
        if chart.get('source') != WEBSITE_SOURCE or chart.get('symbol') != selected:
            raise Review('NEUROBRO_CHART_EVIDENCE_INVALID')
        captured = _instant(chart.get('captured_at'))
        if not (0 <= received - captured <= min(final_max_age, receipt['chart_max_age'])
                and 0 <= current - captured <= final_max_age):
            raise Review('STALE_MARKET_CONTEXT')
    if not refresh:
        return True
    fresh = market.data(selected)
    market.fresh(fresh, selected)
    result = deepcopy(context)
    for key in ('source', 'symbol', 'fetched_at', 'server_time', 'mark_price', 'timeframes', 'quantity_unit'):
        if key in fresh:
            result[key] = deepcopy(fresh[key])
    return result
