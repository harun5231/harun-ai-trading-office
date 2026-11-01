"""Provider selection and immutable browser evidence beside the legacy journal.

The existing API journal and historical proof fields remain unchanged. Browser
requests add their own request/response evidence rather than relabeling API rows.
"""
import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone

from .core import D, Review
from .http_client import unique
from .owner_source_protocol import parse_bound_assistant_text, load_owner_release_cutoff

BROWSER_PROVIDER = 'NEUROBRO_WEB'
BROWSER_CONTRACT = 'neurobro-browser-decision-v2'
BROWSER_SOURCE = 'NEUROBRO_BROWSER'
CHART_SOURCE = 'BINANCE_USDM_FUTURES_WEBSITE_SCREENSHOT'
MAX_CHART_BYTES = 8_000_000
MAX_ASSISTANT_BYTES = 512_000
MAX_OUTPUT_BYTES = 64_000
MAX_REQUEST_BYTES = 393216
MAX_PROMPT_CHARACTERS = 16_000
MAX_CONTEXT_CHARACTERS = 262144


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def marker(operation):
    if not isinstance(operation, str) or not 1 <= len(operation) <= 256:
        raise Review('NEUROBRO_BROWSER_REQUEST_INVALID')
    return 'HARUN_' + sha(operation)[:32]


def ensure_journal(db):
    db.execute('CREATE TABLE IF NOT EXISTS api_requests(operation TEXT PRIMARY KEY, idempotency TEXT, body_hash TEXT, state TEXT, created REAL, attempts INTEGER, output TEXT)')
    db.execute('BEGIN IMMEDIATE')
    try:
        if 'failure_code' not in {row[1] for row in db.execute('PRAGMA table_info(api_requests)')}:
            db.execute('ALTER TABLE api_requests ADD COLUMN failure_code TEXT')
        db.execute('''CREATE TABLE IF NOT EXISTS research_browser_requests(
            operation TEXT PRIMARY KEY, provider TEXT NOT NULL,
            contract_version TEXT NOT NULL, request_sha256 TEXT NOT NULL,
            marker TEXT NOT NULL, request_body TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS research_browser_responses(
            operation TEXT PRIMARY KEY, response_id TEXT NOT NULL,
            output_sha256 TEXT NOT NULL, assistant_text TEXT NOT NULL,
            assistant_sha256 TEXT NOT NULL, chart_evidence TEXT,
            received_at REAL, chart_max_age INTEGER)''')
        columns = {row[1] for row in db.execute('PRAGMA table_info(research_browser_responses)')}
        for name, kind in (('chart_evidence', 'TEXT'), ('received_at', 'REAL'), ('chart_max_age', 'INTEGER')):
            if name not in columns:
                db.execute('ALTER TABLE research_browser_responses ADD COLUMN ' + name + ' ' + kind)
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise


def request_body(operation, prompt, schema, context):
    if (not isinstance(prompt, str) or not 1 <= len(prompt) <= MAX_PROMPT_CHARACTERS
            or not isinstance(schema, dict)):
        raise Review('NEUROBRO_BROWSER_REQUEST_INVALID')
    body = dict(operation=operation, marker=marker(operation),
                provider=BROWSER_PROVIDER, contract_version=BROWSER_CONTRACT,
                prompt=prompt, output_schema=schema, context=context)
    try:
        serialized = encode(body)
        if context is not None and len(encode(context)) > MAX_CONTEXT_CHARACTERS:
            raise Review('NEUROBRO_BROWSER_REQUEST_TOO_LARGE')
    except (ValueError, TypeError, OverflowError):
        raise Review('NEUROBRO_BROWSER_REQUEST_INVALID') from None
    if len(encode({**body, 'request_sha256': '0' * 64}).encode('utf-8')) > MAX_REQUEST_BYTES:
        raise Review('NEUROBRO_BROWSER_REQUEST_TOO_LARGE')
    return body, serialized, sha(serialized)


def parse_assistant_text(text):
    """Accept a whole JSON object or one explicit JSON fence, never pick a guess."""
    if not isinstance(text, str) or not text or len(text.encode('utf-8')) > MAX_ASSISTANT_BYTES:
        raise Review('INVALID_OUTPUT_SCHEMA')
    stripped = text.strip()
    try:
        value = json.loads(stripped, parse_float=D, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError):
        blocks = re.findall(r'```(?:json)?[ \t]*\r?\n(.*?)```', text, re.I | re.S)
        if len(blocks) != 1 or text.count('```') != 2:
            raise Review('INVALID_OUTPUT_SCHEMA') from None
        stripped = blocks[0].strip()
        try:
            value = json.loads(stripped, parse_float=D, object_pairs_hook=unique,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, TypeError):
            raise Review('INVALID_OUTPUT_SCHEMA') from None
    if len(stripped.encode('utf-8')) > MAX_OUTPUT_BYTES or not isinstance(value, dict):
        raise Review('INVALID_OUTPUT_SCHEMA')
    return value


def response_output_text(value):
    """Bound a received JSON value without losing Decimal numeric precision.

    Rejected decisions may have any JSON shape. Their digest is audit evidence,
    never a validated decision. Limit nesting and numeric size before formatting.
    """
    parts = []
    size = 0
    def append(text):
        nonlocal size
        size += len(text.encode('utf-8'))
        if size > MAX_OUTPUT_BYTES:
            raise ValueError
        parts.append(text)
    def visit(item, depth=0):
        if depth > 20:
            raise ValueError
        if item is None or type(item) is bool:
            append(json.dumps(item))
        elif type(item) is int:
            if item.bit_length() > 4096:
                raise ValueError
            append(str(item))
        elif isinstance(item, D):
            if not item.is_finite() or abs(item.adjusted()) > 63 or abs(item.as_tuple().exponent) > 64:
                raise ValueError
            append(format(item, 'f'))
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError
            append(json.dumps(item, allow_nan=False))
        elif isinstance(item, str):
            if len(item.encode('utf-8')) > MAX_OUTPUT_BYTES:
                raise ValueError
            append(json.dumps(item, ensure_ascii=False))
        elif type(item) is dict:
            if len(item) > 1024 or any(not isinstance(key, str) for key in item):
                raise ValueError
            append('{')
            for index, key in enumerate(sorted(item)):
                if index: append(',')
                visit(key, depth + 1)
                append(':')
                visit(item[key], depth + 1)
            append('}')
        elif type(item) is list:
            if len(item) > 1024:
                raise ValueError
            append('[')
            for index, child in enumerate(item):
                if index: append(',')
                visit(child, depth + 1)
            append(']')
        else:
            raise ValueError
    try:
        visit(value)
        return ''.join(parts)
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise Review('INVALID_RESPONSE_ENVELOPE') from None


def validate_chart_evidence(value, schema, context, created, received_at, max_age):
    """Verify selected-symbol screenshots when received, retaining historical age.

    Metadata binds the helper's private archive and uploaded files. These checks
    cannot independently inspect PNG pixels over this metadata-only RPC.
    """
    from .neuroapi import SETUP_SCHEMA, SETUP_SCHEMA_V4, screening_count
    try:
        if (type(created) not in (int, float) or not math.isfinite(created) or created <= 0
                or type(received_at) not in (int, float) or not math.isfinite(received_at)
                or received_at < created or type(max_age) is not int or not 30 <= max_age <= 300):
            raise ValueError
        if screening_count(schema) is not None:
            if type(value) is not list or value:
                raise ValueError
            return value
        if schema not in (SETUP_SCHEMA, SETUP_SCHEMA_V4) or type(value) is not list or len(value) != 2:
            raise ValueError
        if (not isinstance(context, dict) or context.get('source') != 'Binance Futures'
                or not isinstance(context.get('symbol'), str)
                or not re.fullmatch(r'[A-Z0-9]{2,18}USDT', context['symbol'])
                or not isinstance(context.get('timeframes'), dict)
                or set(context['timeframes']) != {'15m', '1h'}):
            raise ValueError
        if any(not isinstance(context['timeframes'][tf], dict)
                or context['timeframes'][tf].get('symbol') != context['symbol']
                or context['timeframes'][tf].get('timeframe') != tf for tf in ('15m', '1h')):
            raise ValueError
        timeframes, attachments, digests = set(), set(), set()
        fields = {'source', 'symbol', 'timeframe', 'captured_at', 'sha256', 'bytes', 'attachment_id'}
        for row in value:
            if (not isinstance(row, dict) or set(row) != fields or row['source'] != CHART_SOURCE
                    or row['symbol'] != context['symbol'] or row['timeframe'] not in ('15m', '1h')
                    or row['timeframe'] in timeframes or type(row['bytes']) is not int
                    or not 1 <= row['bytes'] <= MAX_CHART_BYTES
                    or not isinstance(row['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', row['sha256'])
                    or row['sha256'] in digests or not isinstance(row['attachment_id'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', row['attachment_id'])
                    or row['attachment_id'] in attachments or not isinstance(row['captured_at'], str)
                    or len(row['captured_at']) > 40):
                raise ValueError
            captured = datetime.fromisoformat(row['captured_at'].replace('Z', '+00:00'))
            if captured.tzinfo is None or captured.utcoffset() != timezone.utc.utcoffset(captured):
                raise ValueError
            captured_at = captured.timestamp()
            if not created - 5 <= captured_at <= received_at or not 0 <= received_at - captured_at <= max_age:
                raise ValueError
            timeframes.add(row['timeframe'])
            attachments.add(row['attachment_id'])
            digests.add(row['sha256'])
        if timeframes != {'15m', '1h'}:
            raise ValueError
        return value
    except Exception:
        raise Review('NEUROBRO_CHART_EVIDENCE_INVALID') from None


def source_evidence(db, operation):
    """Return legacy fields exactly, or validate the complete browser witness."""
    table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_browser_requests'").fetchone()
    row = db.execute('SELECT * FROM research_browser_requests WHERE operation=?', (operation,)).fetchone() if table else None
    if row is None:
        return dict(source_type='NEUROAPI_STRUCTURED', contract_version='neuroapi-decision-v2')
    from .neuroapi import canonical_output, screening_count
    try:
        if (row['provider'], row['contract_version'], row['marker']) != (BROWSER_PROVIDER, BROWSER_CONTRACT, marker(operation)):
            raise ValueError
        body = json.loads(row['request_body'], object_pairs_hook=unique)
        if set(body) != {'operation', 'marker', 'provider', 'contract_version', 'prompt', 'output_schema', 'context'}:
            raise ValueError
        rebuilt, serialized, request_hash = request_body(operation, body['prompt'], body['output_schema'], body['context'])
        if body != rebuilt or row['request_body'] != serialized or row['request_sha256'] != request_hash:
            raise ValueError
        journal = db.execute("SELECT body_hash,output,created FROM api_requests WHERE operation=? AND state='COMPLETE'", (operation,)).fetchone()
        response = db.execute('SELECT * FROM research_browser_responses WHERE operation=?', (operation,)).fetchone()
        if not journal or not response or journal['body_hash'] != request_hash:
            raise ValueError
        if sha(journal['output']) != response['output_sha256'] or sha(response['assistant_text']) != response['assistant_sha256']:
            raise ValueError
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', response['response_id']):
            raise ValueError
        value = parse_bound_assistant_text(response['assistant_text'], body, parse_assistant_text,
            max_assistant_bytes=MAX_ASSISTANT_BYTES)
        schema = body['output_schema']
        catalog = {symbol: {} for symbol in value.get('symbols', []) if isinstance(symbol, str)} if screening_count(schema) is not None else None
        if canonical_output(value, schema, catalog) != journal['output']:
            raise ValueError
        charts = json.loads(response['chart_evidence'], object_pairs_hook=unique)
        if response_output_text(charts) != response['chart_evidence']:
            raise ValueError
        validate_chart_evidence(charts, schema, body['context'], journal['created'],
                                response['received_at'], response['chart_max_age'])
        return dict(source_type=BROWSER_SOURCE, contract_version=BROWSER_CONTRACT,
                    provider_evidence_sha256=sha(encode(dict(
                        request_sha256=request_hash, marker=row['marker'],
                        response_id=response['response_id'], output_sha256=response['output_sha256'],
                        assistant_sha256=response['assistant_sha256'],
                        chart_evidence_sha256=sha(response['chart_evidence']),
                        received_at=response['received_at'], chart_max_age=response['chart_max_age']))))
    except Exception:
        raise Review('ROBOT_SETUP_UNVERIFIED') from None


def selected_provider():
    value = os.getenv('OFFICE_RESEARCH_PROVIDER', 'BROWSER').upper()
    if value != 'BROWSER':
        raise Review('RESEARCH_PROVIDER_INVALID')
    return value


def create_provider(ledger, *, kind=None, api_factory=None, **kwargs):
    kind = selected_provider() if kind is None else kind
    if kind == 'BROWSER':
        from .neurobrowser import NeuroBrowser
        return NeuroBrowser(ledger, **kwargs)
    # Explicit fixture injection preserves legacy API test coverage; production
    # selection is browser-only and never imports or initializes a paid client.
    if kind == 'API' and api_factory is not None:
        return api_factory(ledger, **kwargs)
    raise Review('RESEARCH_PROVIDER_INVALID')


__import__('agent.native_pending_protocol', fromlist=('install_source_protocol',)).install_source_protocol(globals())


__import__('agent.native_chart_history', fromlist=('install_website_chart_history',)).install_website_chart_history(globals(), legacy_source='BINANCE_USDM_FUTURES_WEB', release_cutoff_utc='2026-10-22T16:46:25Z', data_render_release_cutoff_utc=load_owner_release_cutoff(), source_evidence_sha256='855fd7e9ec580c9b0ecf62c62f0691d7cce1a69837341d03597237b8593d6bed', validator_sha256='a07f40e9fa9176baa3cb0d70ff90664cc90715d786089d2969457f1e25ce1fe2')
