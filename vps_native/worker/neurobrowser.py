"""Durable browser chatbot research. This adapter never falls back to paid API."""
import json
import math
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from zoneinfo import ZoneInfo

from .core import D, Review, FEE_SOURCE, validated_risk_target
from .diagnostics import validation_code
from .http_client import NoRedirect, unique
from .neuroapi import ResearchPaused, SETUP_SCHEMA, SETUP_SCHEMA_V4, canonical_output, screening_count
from .owner_source_protocol import (NEW_OPERATION_SECONDS, owner_material_request,
    parse_bound_assistant_text)
from .research_provider import (BROWSER_PROVIDER, BROWSER_CONTRACT, BROWSER_SOURCE, MAX_REQUEST_BYTES,
    MAX_ASSISTANT_BYTES, ensure_journal, encode, parse_assistant_text, request_body,
    response_output_text, sha, source_evidence, validate_chart_evidence)

BASE = 'http://neurobro-browser:8790'
MAX_RESPONSE_BYTES = 700_000
SESSION_STATES = frozenset(('CONNECTED', 'LOGIN_REQUIRED', 'LOGIN_IN_PROGRESS',
    'UNAVAILABLE', 'CHALLENGE_REQUIRED', 'BUSY'))
NO_SEND_REASONS = frozenset(('RESEARCH_PAUSED', 'LOGIN_REQUIRED', 'CHALLENGE_REQUIRED', 'BUSY', 'UNAVAILABLE'))


def helper_token():
    try:
        path = Path(os.environ['NEUROBRO_BROWSER_TOKEN_FILE'])
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            return ''
        value = path.read_text().strip()
        if not 32 <= len(value) <= 512 or not value.isascii() or any(c.isspace() for c in value):
            return ''
        return value
    except (KeyError, OSError, UnicodeError):
        return ''


def browser_request(method, url, headers=None, body=None, timeout=15):
    """Fixed internal service; no proxies, redirects, secrets in URL or logs."""
    if (method, url) not in (('GET', BASE + '/v1/status'), ('POST', BASE + '/v1/ask')):
        raise Review('NEUROBRO_BROWSER_ENDPOINT_DENIED')
    if method == 'GET' and body is not None:
        raise Review('NEUROBRO_BROWSER_ENDPOINT_DENIED')
    try:
        raw = None if body is None else encode(body).encode('utf-8')
        if raw is not None and len(raw) > MAX_REQUEST_BYTES + 128:
            raise Review('NEUROBRO_BROWSER_REQUEST_TOO_LARGE')
        request = Request(url, data=raw, method=method, headers=headers or {})
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            status = response.code
            if status not in (200, 409):
                return status, {}, None
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise Review('INVALID_RESPONSE_ENVELOPE')
            try:
                value = json.loads(raw, parse_float=D, object_pairs_hook=unique,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, TypeError):
                raise Review('INVALID_RESPONSE_ENVELOPE') from None
            return status, {}, value
    except Review:
        raise
    except Exception:
        raise Review('NEUROBRO_BROWSER_OUTCOME_UNKNOWN') from None


def unavailable_status():
    return dict(provider=BROWSER_PROVIDER, status='UNAVAILABLE', checked_at=None,
                profile_persistent=False)


def verify_dispatch(db, operation, request_marker, request_sha256, today, now, *, max_age=180):
    """Pure journal/freshness query for the helper's immediate pre-Send callback.

The authenticated handler separately checks robot ON and process stopping. This
function never grants a replay, initializes tables, changes state, or reads keys.
"""
    try:
        if (type(now) not in (int, float) or not math.isfinite(now) or now <= 0
                or type(max_age) is not int or not 30 <= max_age <= 300):
            return False
        if today != datetime.fromtimestamp(now, ZoneInfo('Asia/Jakarta')).date().isoformat():
            return False
        if not isinstance(operation, str) or request_marker != marker_for_operation(operation):
            return False
        if not isinstance(request_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', request_sha256):
            return False
        journal = db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone()
        evidence = db.execute('SELECT * FROM research_browser_requests WHERE operation=?', (operation,)).fetchone()
        job = db.execute('SELECT * FROM robot_jobs WHERE operation=?', (operation,)).fetchone()
        if (not journal or not evidence or not job or journal['state'] != 'PENDING'
                or journal['attempts'] != 1 or journal['output'] is not None
                or journal['idempotency'] is not None or job['state'] != 'PENDING'):
            return False
        if db.execute('SELECT 1 FROM research_browser_responses WHERE operation=?', (operation,)).fetchone():
            return False
        created = journal['created']
        if type(created) not in (int, float) or not math.isfinite(created) or not 0 <= now - created:
            return False
        if (evidence['provider'], evidence['contract_version'], evidence['marker'], evidence['request_sha256'], journal['body_hash']) != (
                BROWSER_PROVIDER, BROWSER_CONTRACT, request_marker, request_sha256, request_sha256):
            return False
        body = json.loads(evidence['request_body'], object_pairs_hook=unique)
        rebuilt, serialized, request_hash = request_body(operation, body['prompt'], body['output_schema'], body['context'])
        if body != rebuilt or serialized != evidence['request_body'] or request_hash != request_sha256:
            return False
        owner_material = owner_material_request(body)
        if now - created > (NEW_OPERATION_SECONDS if owner_material else max_age):
            return False
        validation_at = created if owner_material else now
        cycle = db.execute('SELECT * FROM robot_cycles WHERE id=?', (job['cycle'],)).fetchone()
        if (not cycle or cycle['state'] != 'ACTIVE' or cycle['day'] != today
                or not re.fullmatch(re.escape(today) + r':robot-v9:(?:0|[1-9]\d{0,11})', cycle['id'])
                or cycle['entry_epoch'] != int(cycle['id'].rsplit(':', 1)[1])):
            return False
        data = json.loads(cycle['data'], object_pairs_hook=unique)
        if job['kind'] == 'SCREENING':
            count = screening_count(body['output_schema'])
            return bool(count in (1, 2) and job['symbol'] is None and job['risk_target'] is None
                and not data['queue'] and type(data['screen']) is int
                and operation == cycle['id'] + ':screening:' + str(data['screen'] + 1) + ':' + str(count))
        if job['kind'] != 'ANALYSIS' or body['output_schema'] not in (SETUP_SCHEMA, SETUP_SCHEMA_V4):
            return False
        symbol = job['symbol']
        if (not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9]{2,18}USDT', symbol)
                or operation != cycle['id'] + ':analysis-v9:' + symbol
                or not data['queue'] or data['queue'][0] != symbol):
            return False
        context = body['context']
        if (not isinstance(context, dict) or context['source'] != 'Binance Futures'
                or context['symbol'] != symbol or set(context['timeframes']) != {'1h', '15m'}):
            return False
        fetched = context['fetched_at']
        if (type(fetched) not in (int, float) or not math.isfinite(fetched)
                or not 0 <= validation_at - fetched <= max_age or fetched > created):
            return False
        server = context['server_time']
        if type(server) is not int or server <= 0 or abs(server / 1000 - fetched) > 30:
            return False
        for timeframe, interval in (('1h', 3_600_000), ('15m', 900_000)):
            frame = context['timeframes'][timeframe]
            candles = frame['candles']
            if (frame['symbol'] != symbol or frame['timeframe'] != timeframe
                    or not isinstance(candles, list) or not 20 <= len(candles) <= 500):
                return False
            last = candles[-1]
            if (type(last['open_time']) is not int or type(last['close_time']) is not int
                    or last['open_time'] % interval or last['close_time'] != last['open_time'] + interval - 1
                    or not last['open_time'] <= server <= last['close_time']
                    or validation_at * 1000 > last['close_time'] + max_age * 1000):
                return False
        contract = context['contract_rules']
        risk = context['risk_constraints']
        if (contract['symbol'] != symbol or contract['source'] != 'Binance Futures /fapi/v1/exchangeInfo'
                or risk['fee_symbol'] != symbol or risk['fee_source'] != FEE_SOURCE):
            return False
        for observed in (contract['observed_at'], risk['fee_observed_at']):
            if type(observed) not in (int, float) or not math.isfinite(observed) or not 0 <= validation_at - observed <= 300:
                return False
        target = validated_risk_target(job['risk_target'])
        if (validated_risk_target(risk['maximum_loss_at_sl_usdt']) != target
                or validated_risk_target(risk['target_loss_at_sl_usdt']) != target):
            return False
        witness = data['analysis_contexts'][operation]
        return bool(witness['version'] == 'ANALYSIS_CONTEXT_FRESHNESS_V1'
            and witness['symbol'] == symbol and witness['source'] == context['source']
            and witness['fetched_at'] == fetched and witness['server_time'] == server
            and type(witness['claimed_at']) in (int, float)
            and fetched <= witness['claimed_at'] <= created
            and set(witness['timeframes']) == {'1h', '15m'}
            and all(witness['timeframes'][tf] == dict(symbol=symbol, timeframe=tf,
                last_close_time=context['timeframes'][tf]['candles'][-1]['close_time']) for tf in ('1h', '15m')))
    except Exception:
        return False


def marker_for_operation(operation):
    from .research_provider import marker
    return marker(operation)


class NeuroBrowser:
    def __init__(self, ledger, token=None, transport=browser_request, *, max_age=None):
        self.db = ledger.db
        try:
            self.chart_max_age = int(os.getenv('OFFICE_MARKET_MAX_AGE', '180')) if max_age is None else max_age
            if type(self.chart_max_age) is not int or not 30 <= self.chart_max_age <= 300:
                raise ValueError
        except (ValueError, TypeError):
            raise Review('INVALID_MARKET_CONFIG') from None
        self._token = helper_token() if token is None else token
        if (not isinstance(self._token, str) or not 32 <= len(self._token) <= 512
                or not self._token.isascii() or any(c.isspace() for c in self._token)):
            self._token = ''
        self.transport = transport
        ensure_journal(self.db)

    def headers(self):
        if not self._token:
            raise Review('NEUROBRO_BROWSER_NOT_CONFIGURED')
        return {'Authorization': 'Bearer ' + self._token, 'Accept': 'application/json',
                'Content-Type': 'application/json'}

    def session_status(self):
        """Read only the bounded public session shape, never helper internals."""
        try:
            code, _, value = self.transport('GET', BASE + '/v1/status', self.headers(), timeout=15)
            if code != 200 or not isinstance(value, dict) or value.get('provider') != BROWSER_PROVIDER:
                raise ValueError
            if value.get('status') not in SESSION_STATES or type(value.get('profile_persistent')) is not bool:
                raise ValueError
            checked = value.get('checked_at')
            if not isinstance(checked, str) or len(checked) > 40:
                raise ValueError
            parsed = datetime.fromisoformat(checked.replace('Z', '+00:00'))
            if parsed.tzinfo is None or not -60 <= (datetime.now(timezone.utc) - parsed).total_seconds() <= 120:
                raise ValueError
            return dict(provider=BROWSER_PROVIDER, status=value['status'], checked_at=checked,
                        profile_persistent=value['profile_persistent'])
        except Exception:
            return unavailable_status()

    def require_key(self):
        """Compatibility readiness hook; no paid key is read or required."""
        if not self._token:
            raise Review('NEUROBRO_BROWSER_NOT_CONFIGURED')
        state = self.session_status()['status']
        if state != 'CONNECTED':
            raise Review('NEUROBRO_' + state)

    def health(self):
        self.require_key()
        return 'NEUROBRO_BROWSER_CONNECTED'

    def record_validation(self, operation, error):
        self.db.execute("UPDATE api_requests SET failure_code=? WHERE operation=? AND state='COMPLETE'",
                        (validation_code(error), operation))

    def ask(self, operation, prompt, schema, validate, context=None, *, catalog=None, continue_if=None):
        if continue_if is not None and not callable(continue_if):
            raise Review('INVALID_CONTINUATION_GUARD')
        def permitted():
            try:
                return continue_if is None or continue_if() is True
            except Exception:
                return False
        if not permitted():
            raise ResearchPaused('RESEARCH_PAUSED')
        if (schema not in (SETUP_SCHEMA, SETUP_SCHEMA_V4) and screening_count(schema) is None) and not __import__('agent.native_pending_protocol', fromlist=('is_pending_schema',)).is_pending_schema(schema):
            raise Review('INVALID_OUTPUT_SCHEMA')
        if screening_count(schema) is not None and (not isinstance(catalog, dict) or not catalog):
            raise Review('SCREENING_CATALOG_REQUIRED')
        body, serialized, request_hash = request_body(operation, prompt, schema, context)
        # Cached completed browser responses never require another chat submission.
        old = self.db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone()
        if old:
            if old['body_hash'] != request_hash or old['state'] != 'COMPLETE':
                raise Review('NEUROBRO_BROWSER_REQUEST_NEEDS_REVIEW')
            if source_evidence(self.db, operation).get('source_type') != BROWSER_SOURCE:
                raise Review('NEUROBRO_BROWSER_REQUEST_NEEDS_REVIEW')
            value = json.loads(old['output'], parse_float=D, object_pairs_hook=unique)
            validate(value)
            canonical_output(value, schema, catalog)
            return value
        try:
            self.require_key()
        except Review as error:
            raise ResearchPaused(str(error)) from None
        if not permitted():
            raise ResearchPaused('RESEARCH_PAUSED')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute('SELECT 1 FROM api_requests WHERE operation=?', (operation,)).fetchone():
                raise Review('NEUROBRO_BROWSER_REQUEST_NEEDS_REVIEW')
            self.db.execute('INSERT INTO api_requests(operation,idempotency,body_hash,state,created,attempts,output) VALUES(?,?,?,?,?,?,?)',
                (operation, None, request_hash, 'PENDING', time.time(), 0, None))
            self.db.execute('INSERT INTO research_browser_requests VALUES(?,?,?,?,?,?)',
                (operation, BROWSER_PROVIDER, BROWSER_CONTRACT, request_hash, body['marker'], serialized))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        sent = False
        failure = 'LOCAL_PROCESSING_FAILED'
        try:
            if not permitted():
                raise ResearchPaused('RESEARCH_PAUSED')
            self.db.execute('UPDATE api_requests SET attempts=1 WHERE operation=?', (operation,))
            if not permitted():
                raise ResearchPaused('RESEARCH_PAUSED')
            # The helper separately calls worker dispatch-check immediately before
            # the browser Send click. The HTTP call itself is not a Send grant.
            failure = 'NEUROBRO_BROWSER_OUTCOME_UNKNOWN'
            sent = True
            code, _, result = self.transport('POST', BASE + '/v1/ask', self.headers(),
                {**body, 'request_sha256': request_hash}, timeout=930)
            if code == 409 and self._proven_unsent(result, body, request_hash):
                sent = False
                raise ResearchPaused(result['reason'])
            if code != 200:
                raise Review('NEUROBRO_BROWSER_OUTCOME_UNKNOWN')
            failure = 'INVALID_RESPONSE_ENVELOPE'
            if code != 200 or not isinstance(result, dict):
                raise Review(failure)
            required = {'operation', 'marker', 'request_sha256', 'output', 'assistant_text', 'response_id'}
            if set(result) not in (required, required | {'chart_evidence'}):
                raise Review(failure)
            if (result['operation'], result['marker'], result['request_sha256']) != (operation, body['marker'], request_hash):
                raise Review(failure)
            if not isinstance(result['response_id'], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', result['response_id']):
                raise Review(failure)
            text = result['assistant_text']
            try:
                if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_ASSISTANT_BYTES:
                    raise ValueError
            except (ValueError, UnicodeError):
                raise Review(failure) from None
            value = result['output']
            raw_output = response_output_text(value)
            raw_charts = result.get('chart_evidence')
            chart_text = response_output_text(raw_charts)
            received_at = time.time()
            try:
                receipt_output = canonical_output(value, schema, catalog)
            except Exception:
                receipt_output = raw_output
            # Save the exact bound assistant response before parsing or local
            # validation. An unusable answer must still be inspectable in review.
            # This receipt alone is never COMPLETE research or an order proof.
            failure = 'LOCAL_PROCESSING_FAILED'
            self.db.execute('BEGIN IMMEDIATE')
            try:
                current = self.db.execute('SELECT state,body_hash,attempts,output,created FROM api_requests WHERE operation=?', (operation,)).fetchone()
                if (not current or current['state'] != 'PENDING' or current['body_hash'] != request_hash
                        or current['attempts'] != 1 or current['output'] is not None):
                    raise Review('NEUROBRO_BROWSER_REQUEST_NEEDS_REVIEW')
                self.db.execute('''INSERT INTO research_browser_responses(
                    operation,response_id,output_sha256,assistant_text,assistant_sha256,
                    chart_evidence,received_at,chart_max_age) VALUES(?,?,?,?,?,?,?,?)''',
                    (operation, result['response_id'], sha(receipt_output), text, sha(text),
                     chart_text, received_at, self.chart_max_age))
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            failure = 'NEUROBRO_CHART_EVIDENCE_INVALID'
            validate_chart_evidence(raw_charts, schema, context, current['created'], received_at, self.chart_max_age)
            failure = 'INVALID_OUTPUT_SCHEMA'
            actual = parse_bound_assistant_text(text, body, parse_assistant_text,
                max_assistant_bytes=MAX_ASSISTANT_BYTES)
            validate(value)
            cached = canonical_output(value, schema, catalog)
            if canonical_output(actual, schema, catalog) != cached or sha(cached) != sha(receipt_output):
                raise Review(failure)
            failure = 'LOCAL_PROCESSING_FAILED'
            self.db.execute('BEGIN IMMEDIATE')
            try:
                changed = self.db.execute("UPDATE api_requests SET state='COMPLETE',output=?,failure_code=NULL WHERE operation=? AND state='PENDING'",
                                          (cached, operation)).rowcount
                if changed != 1:
                    raise Review('NEUROBRO_BROWSER_REQUEST_NEEDS_REVIEW')
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            return value
        except Exception as error:
            if isinstance(error, ResearchPaused) and not sent:
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    self.db.execute("DELETE FROM research_browser_requests WHERE operation=? AND NOT EXISTS(SELECT 1 FROM research_browser_responses WHERE operation=?)", (operation, operation))
                    self.db.execute("DELETE FROM api_requests WHERE operation=? AND state='PENDING' AND output IS NULL AND attempts<=1", (operation,))
                    self.db.execute('COMMIT')
                except BaseException:
                    self.db.execute('ROLLBACK')
                    raise
                raise ResearchPaused(str(error)) from None
            self.db.execute("UPDATE api_requests SET state='NEEDS_REVIEW',failure_code=? WHERE operation=? AND state='PENDING'",
                            (failure, operation))
            raise Review(failure) from None

    @staticmethod
    def _proven_unsent(result, body, request_hash):
        return (isinstance(result, dict) and set(result) == {'operation', 'marker', 'request_sha256', 'submitted', 'reason'}
            and (result['operation'], result['marker'], result['request_sha256']) == (body['operation'], body['marker'], request_hash)
            and result['submitted'] is False and result['reason'] in NO_SEND_REASONS)


__import__('agent.native_pending_protocol', fromlist=('install_worker_browser_protocol',)).install_worker_browser_protocol(globals())
