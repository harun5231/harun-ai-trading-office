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


SCREENING_INTERPRETATIONS_SCHEMA = '''CREATE TABLE IF NOT EXISTS screening_interpretations(
 operation TEXT PRIMARY KEY,request_sha256 TEXT NOT NULL,assistant_sha256 TEXT NOT NULL,
 proof_sha256 TEXT NOT NULL,proof TEXT NOT NULL,retained_at REAL NOT NULL);'''


def _screening_interpretation_source(db, text, body):
    from .neuroapi import screening_count
    from .screening_response_recovery import stored_screening_request, _row, _sha
    if type(body) is not dict or screening_count(body.get('output_schema')) not in (1, 2):
        raise Review('INVALID_OUTPUT_SCHEMA')
    stored, request_hash, _ = stored_screening_request(db, body.get('operation'))
    api = _row(db, 'api_requests', 'operation', body['operation'])
    receipt = _row(db, 'research_browser_responses', 'operation', body['operation'])
    if (stored != body or api is None or receipt is None or api['body_hash'] != request_hash
            or type(api['attempts']) is not int or api['attempts'] != 1 or api['idempotency'] is not None
            or api['state'] not in ('PENDING', 'NEEDS_REVIEW', 'COMPLETE')
            or receipt['assistant_text'] != text or receipt['assistant_sha256'] != _sha(text)):
        raise Review('INVALID_OUTPUT_SCHEMA')
    return {**stored, 'request_sha256': request_hash}, api['created'], receipt


def _validated_screening_interpretation(db, text, body, proof, *, retained):
    from .screening_response_recovery import screening_interpretation_proof_output, _sha
    payload, created, receipt = _screening_interpretation_source(db, text, body)
    output = screening_interpretation_proof_output(proof, payload, text,
        created=created, received=receipt['received_at'], retained=retained)
    if receipt['response_id'] != 'dom-' + _sha(proof['room']['url'] + '\n' + payload['marker'] + '\n' + text):
        raise Review('INVALID_OUTPUT_SCHEMA')
    return payload, output


def retain_screening_interpretation(db, body, request_hash, text, proof):
    """Append genuine Mini provenance after the unchanged raw native receipt."""
    from .screening_response_recovery import _wire, _sha
    retained = time.time()
    payload, output = _validated_screening_interpretation(db, text, body, proof, retained=retained)
    if payload['request_sha256'] != request_hash or db.in_transaction:
        raise Review('INVALID_OUTPUT_SCHEMA')
    raw = _wire(proof)
    if len(raw.encode()) > 2 * 1024 * 1024:
        raise Review('INVALID_OUTPUT_SCHEMA')
    expected = (payload['operation'], request_hash, _sha(text), _sha(raw), raw)
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute(SCREENING_INTERPRETATIONS_SCHEMA)
        existing = db.execute('SELECT operation,request_sha256,assistant_sha256,proof_sha256,proof '
            'FROM screening_interpretations WHERE operation=?', (payload['operation'],)).fetchone()
        if existing is None:
            db.execute('INSERT INTO screening_interpretations VALUES(?,?,?,?,?,?)', (*expected, retained))
        elif tuple(existing) != expected:
            raise Review('INVALID_OUTPUT_SCHEMA')
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    return output


def read_screening_interpretation(db, text, body):
    """Revalidate saved provenance on every native source read, without RPCs."""
    from .neuroapi import screening_count
    from .screening_response_recovery import _wire, _sha
    if type(body) is not dict or screening_count(body.get('output_schema')) not in (1, 2):
        return None
    table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='screening_interpretations'").fetchone()
    if not table:
        return None
    saved = db.execute('SELECT * FROM screening_interpretations WHERE operation=?',
        (body.get('operation'),)).fetchone()
    if saved is None:
        return None
    try:
        raw = saved['proof']
        if (type(raw) is not str or len(raw.encode()) > 2 * 1024 * 1024
                or saved['proof_sha256'] != _sha(raw) or saved['assistant_sha256'] != _sha(text)):
            raise ValueError
        proof = json.loads(raw)
        if _wire(proof) != raw:
            raise ValueError
        payload, output = _validated_screening_interpretation(db, text, body, proof, retained=saved['retained_at'])
        if saved['request_sha256'] != payload['request_sha256']:
            raise ValueError
        return output
    except Exception:
        raise Review('INVALID_OUTPUT_SCHEMA') from None


def parse_bound_assistant_text(text, body, legacy_parser, *, max_assistant_bytes, db=None):
    """Read the same raw reply at live validation and historical re-validation.

    Native JSON keeps its original parser and decimal lexemes. Natural language
    is projected by the same per-coin parser as the browser, using the immutable
    operation payload rather than a fabricated JSON message attributed to it.
    """
    if (not isinstance(text, str) or not text or type(max_assistant_bytes) is not int
            or max_assistant_bytes <= 0 or len(text.encode('utf-8')) > max_assistant_bytes):
        raise Review('INVALID_OUTPUT_SCHEMA')
    if db is not None:
        interpreted = read_screening_interpretation(db, text, body)
        if interpreted is not None:
            return interpreted
    try:
        value=legacy_parser(text)
        from .neuroapi import SETUP_SCHEMA_V4
        human_setup_json=(type(body)is dict and body.get('output_schema')==SETUP_SCHEMA_V4
            and type(value)is dict and any(type(key)is str and key.upper()in{'COIN','ARAH'}for key in value))
        if not human_setup_json:return value
        # A human final-summary JSON is a presentation of authored levels, not
        # the native symbol/side wire schema. Use the SAME per-coin parser as
        # the browser instead of handing untranslated keys to the recorder.
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


def chart_windows_current(charts, reference_ms):
    """Pure latest closed-series watermark check, with no seconds-to-live.

    Callers retain their own symbol/hash/receipt authority. Runtime handoff
    passes Binance's actual server clock; read-only recovery may use its
    observation clock and still needs that final actual market validation.
    """
    try:
        if (type(reference_ms) is not int or reference_ms <= 0
                or type(charts) is not list or len(charts) != 2
                or {row.get('timeframe') for row in charts if type(row) is dict} != {'1h','15m'}):
            return False
        for chart in charts:
            if chart.get('source') != WEBSITE_SOURCE:
                return False
            captured = _instant(chart.get('captured_at'))
            interval = 3_600_000 if chart['timeframe'] == '1h' else 900_000
            if not 0 <= captured*1000 <= reference_ms or int(captured*1000)//interval != reference_ms//interval:
                return False
        return True
    except (TypeError,ValueError,OverflowError,Review):
        return False


def completed_decision_fresh(db, operation, market, symbol=None, *, now=None,
                             max_age=180, refresh=False, source_reader=None, browser_source=None, current_context=None):
    """Verify final accepted material, then optionally perform public GETs.

    Initial market facts are immutable request witnesses. V4 discussions may
    refresh their screenshots during the same operation; the final *native*
    receipt must retain exact source and chronological bindings. For V4 there is
    no fixed seconds-to-live: material must cover the latest closed Binance
    candle for each timeframe. An allowed refresh verifies that watermark
    against an actual public GET observation, not a prolonged cache deadline.
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
    received = receipt['received_at']
    if (type(received) not in (int, float) or not math.isfinite(received)
            or not 0 <= current - received
            or type(receipt['chart_max_age']) is not int
            or not 30 <= receipt['chart_max_age'] <= 300):
        raise Review('STALE_MARKET_CONTEXT')
    charts = json.loads(receipt['chart_evidence'])
    if (type(charts) is not list or len(charts) != 2
            or {row.get('timeframe') for row in charts if type(row) is dict} != {'1h', '15m'}):
        raise Review('NEUROBRO_CHART_EVIDENCE_INVALID')
    fresh = None
    if refresh:
        cached_current = (type(current_context) is dict
            and type(current_context.get('fetched_at')) in (int, float)
            and received <= current_context['fetched_at'] <= current)
        fresh = current_context if cached_current else market.data(selected)
        if now is None:
            current = time.time()
        market.fresh(fresh, selected)
        if (type(fresh) is not dict or fresh.get('source') != 'Binance Futures'
                or fresh.get('symbol') != selected or type(fresh.get('server_time')) is not int
                or type(fresh.get('fetched_at')) not in (int, float)
                or not received <= fresh['fetched_at'] <= current
                or abs(fresh['server_time']/1000-fresh['fetched_at']) > 30):
            raise Review('STALE_MARKET_CONTEXT')
    reference_ms = fresh['server_time'] if fresh is not None else int(current*1000)
    for chart in charts:
        if chart.get('source') != WEBSITE_SOURCE or chart.get('symbol') != selected:
            raise Review('NEUROBRO_CHART_EVIDENCE_INVALID')
        captured = _instant(chart.get('captured_at'))
        interval = 3_600_000 if chart['timeframe'] == '1h' else 900_000
        if not (captured <= received <= current
                and int(captured*1000)//interval == reference_ms//interval):
            raise Review('STALE_MARKET_CONTEXT')
    if not refresh:
        return True
    result = deepcopy(context)
    for key in ('source', 'symbol', 'fetched_at', 'server_time', 'mark_price', 'timeframes', 'quantity_unit'):
        if key in fresh:
            result[key] = deepcopy(fresh[key])
    return result
