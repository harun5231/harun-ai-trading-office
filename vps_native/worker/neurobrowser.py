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
    if (method, url) not in (('GET', BASE + '/v1/status'), ('POST', BASE + '/v1/ask'), ('POST', BASE + '/v1/historical-evidence'), ('POST', BASE + '/v1/screening-recovery')):
        raise Review('NEUROBRO_BROWSER_ENDPOINT_DENIED')
    if method == 'GET' and body is not None:
        raise Review('NEUROBRO_BROWSER_ENDPOINT_DENIED')
    observation = url == BASE + '/v1/screening-recovery'
    if observation:
        action_fields = {
            'ANALYSIS_RECEIVED_OBSERVE': {'action', 'payload'},
            'ANALYSIS_RECEIVED_CONTINUE': {'action', 'payload', 'grant'},
            'SCREENING_RECEIVED_OBSERVE': {'action', 'payload'},
            'SCREENING_RECEIVED_REVIEW': {'action', 'payload', 'source_capture_name',
                'source_capture_sha256', 'source_capture_bytes', 'source_observation_sha256'},
        }
        if type(body) is not dict or body.get('action') not in action_fields or set(body) != action_fields[body['action']]:
            raise Review('NEUROBRO_BROWSER_ENDPOINT_DENIED')
    try:
        raw = None if body is None else encode(body).encode('utf-8')
        if raw is not None and len(raw) > (1_000_000 if url.endswith('/v1/historical-evidence') else MAX_REQUEST_BYTES + (4096 if observation else 128)):
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
            maximum = 24*1024*1024 if url.endswith('/v1/historical-evidence') else MAX_RESPONSE_BYTES
            raw = response.read(maximum + 1)
            if len(raw) > maximum:
                raise Review('INVALID_RESPONSE_ENVELOPE')
            try:
                value = json.loads(raw, parse_float=(float if observation or url.endswith('/v1/historical-evidence') else D), object_pairs_hook=unique,
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
        # Screening selects symbols without market/chart material. Keep its
        # multi-turn conversation inside the existing native 900s budget;
        # setup and legacy material freshness retain their original checks.
        screening = screening_count(body['output_schema']) is not None
        if now - created > (NEW_OPERATION_SECONDS if owner_material or screening else max_age):
            # An observed incomplete answer may continue its original native
            # conversation under a fresh sealed followup grant. This does not
            # alter the original request clock or permit its first Send again.
            from .analysis_continuation import verify_dispatch_grant
            if not owner_material or not verify_dispatch_grant(db,operation,request_sha256,today,now):
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
        from .trade_evaluation_runtime import NativeEvaluationReads, EvaluationConsumer
        try:
            native_reads = NativeEvaluationReads(self)
            self.evaluation_consumer = EvaluationConsumer(native_reads.directory,
                archive_reader=native_reads.archive, room_reader=native_reads.room,
                native_receipt_reader=native_reads.completion)
        except ValueError:
            self.evaluation_consumer = None # No native archive authority in an in-memory fixture.
            self.evaluation_failure = 'EVALUATION_LEDGER_PATH_UNVERIFIED'

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

    def observe_received_analysis(self, operation):
        """Observe the same operation through the existing authenticated actor.

        The maintenance action cannot Send, upload, call a model, or create a
        room. A BUSY native actor retains exclusive ownership; absence of an
        observation is never permission to replay the original request.
        """
        self.require_key()
        row = self.db.execute('SELECT * FROM research_browser_requests WHERE operation=?', (operation,)).fetchone()
        current = self.db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone()
        if row is None or current is None:
            raise Review('ANALYSIS_RESPONSE_RECOVERY_UNVERIFIED')
        body = json.loads(row['request_body'], object_pairs_hook=unique)
        rebuilt, serialized, wire = request_body(operation,body['prompt'],body['output_schema'],body['context'])
        if (body != rebuilt or serialized != row['request_body'] or wire != row['request_sha256']
                or current['body_hash'] != wire or body['output_schema'] != SETUP_SCHEMA_V4
                or current['state'] not in ('PENDING','NEEDS_REVIEW') or current['attempts'] != 1
                or current['output'] is not None or current['idempotency'] is not None):
            raise Review('ANALYSIS_RESPONSE_RECOVERY_UNVERIFIED')
        code, _, result = self.transport('POST', BASE + '/v1/screening-recovery', self.headers(),
            {'action':'ANALYSIS_RECEIVED_OBSERVE','payload':{**body,'request_sha256':wire}}, timeout=45)
        if code == 409:
            reason = result.get('reason') if isinstance(result,dict) else None
            error = result.get('error') if isinstance(result,dict) else None
            if isinstance(error,str) and error.startswith('SCREENING_RECOVERY_'):
                reason = error[len('SCREENING_RECOVERY_'):]
            if reason in ('BUSY','LOGIN_REQUIRED','CHALLENGE_REQUIRED','UNAVAILABLE','LOGIN_IN_PROGRESS'):
                raise Review('NEUROBRO_' + reason)
            raise Review('ANALYSIS_RESPONSE_OBSERVATION_PENDING')
        if code != 200 or not isinstance(result,dict):
            raise Review('ANALYSIS_RESPONSE_OBSERVATION_PENDING')
        return result

    def continue_received_analysis(self, operation, grant):
        """Continue a received turn through the same actor's followup hook.

        This fixed action never enters ordinary ask(), whose UNKNOWN receipt
        still prohibits re-sending the initial prompt.
        """
        self.require_key()
        from .analysis_continuation import active_grant
        current=active_grant(self.db,operation)
        if current is None or current['grant'] != json.dumps(grant,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False):
            raise Review('ANALYSIS_CONTINUATION_UNVERIFIED')
        request=self.db.execute('SELECT request_body,request_sha256 FROM research_browser_requests WHERE operation=?',(operation,)).fetchone()
        if request is None:raise Review('ANALYSIS_CONTINUATION_UNVERIFIED')
        payload={**json.loads(request['request_body']), 'request_sha256':request['request_sha256']}
        code,_,result=self.transport('POST',BASE+'/v1/screening-recovery',self.headers(),
            dict(action='ANALYSIS_RECEIVED_CONTINUE',payload=payload,grant=grant),timeout=930)
        if code!=200 or not isinstance(result,dict):
            raise Review('ANALYSIS_CONTINUATION_OUTCOME_UNKNOWN')
        if result.get('grant_id')!=grant['grant_id'] or result.get('operation')!=operation or result.get('request_sha256')!=request['request_sha256']:
            raise Review('ANALYSIS_CONTINUATION_UNVERIFIED')
        return result

    def _received_screening_payload(self, operation):
        from .screening_response_recovery import stored_screening_request, completed_screening_market_hold
        self.require_key()
        body, request_hash, _ = stored_screening_request(self.db, operation)
        current = self.db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone()
        held = completed_screening_market_hold(self.db, operation)
        if ((current['state'] not in ('PENDING', 'NEEDS_REVIEW') and not held)
                or type(current['attempts']) is not int or current['attempts'] != 1
                or (current['output'] is not None and not held) or current['idempotency'] is not None):
            raise Review('SCREENING_RESPONSE_RECOVERY_UNVERIFIED')
        return {**body, 'request_sha256': request_hash}

    def _received_screening_rpc(self, value, *, review=False):
        code, _, result = self.transport('POST', BASE + '/v1/screening-recovery', self.headers(),
            value, timeout=120 if review else 45)
        if code == 409:
            reason = result.get('reason') if isinstance(result, dict) else None
            error = result.get('error') if isinstance(result, dict) else None
            if isinstance(error, str) and error.startswith('SCREENING_RECOVERY_'):
                reason = error[len('SCREENING_RECOVERY_'):]
            if reason in ('BUSY', 'LOGIN_REQUIRED', 'CHALLENGE_REQUIRED', 'UNAVAILABLE', 'LOGIN_IN_PROGRESS'):
                raise Review('NEUROBRO_' + reason)
        if code != 200 or not isinstance(result, dict):
            raise Review('SCREENING_REVIEW_OUTCOME_UNKNOWN' if review else 'SCREENING_RESPONSE_OBSERVATION_PENDING')
        payload = value['payload']
        if result.get('operation') != payload['operation'] or result.get('request_sha256') != payload['request_sha256']:
            raise Review('SCREENING_RESPONSE_RECOVERY_UNVERIFIED')
        return result

    def observe_received_screening(self, operation):
        """Observe an existing screening; this fixed RPC cannot enter ask()."""
        return self._received_screening_rpc(dict(action='SCREENING_RECEIVED_OBSERVE',
            payload=self._received_screening_payload(operation)))

    def review_received_screening(self, operation, envelope):
        """Review the retained native reply with no browser Send or upload."""
        from .screening_response_recovery import RECEIVED_KIND, _wire, _sha
        payload = self._received_screening_payload(operation)
        if (type(envelope) is not dict or envelope.get('kind') != RECEIVED_KIND
                or envelope.get('operation') != operation or envelope.get('request_sha256') != payload['request_sha256']
                or envelope.get('operation_completed') is not False
                or envelope.get('observation_sha256') != _sha(_wire(envelope.get('observation')))
                or type(envelope.get('capture_sha256')) is not str
                or re.fullmatch('[a-f0-9]{64}', envelope['capture_sha256']) is None
                or type(envelope.get('capture_name')) is not str
                or re.fullmatch(r'received-analysis-observation-[a-f0-9]{16}-' + envelope['capture_sha256'] + r'\.json', envelope['capture_name']) is None
                or type(envelope.get('capture_bytes')) is not int or not 0 < envelope['capture_bytes'] <= 8 * 1024 * 1024):
            raise Review('SCREENING_RESPONSE_RECOVERY_UNVERIFIED')
        return self._received_screening_rpc(dict(action='SCREENING_RECEIVED_REVIEW', payload=payload,
            source_capture_name=envelope['capture_name'], source_capture_sha256=envelope['capture_sha256'],
            source_capture_bytes=envelope['capture_bytes'], source_observation_sha256=envelope['observation_sha256']), review=True)

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
        if (schema not in (SETUP_SCHEMA, SETUP_SCHEMA_V4) and screening_count(schema) is None) and not __import__('agent.native_pending_protocol', fromlist=('is_pending_schema',)).is_pending_schema(schema) and not __import__('agent.native_evaluation_protocol', fromlist=('is_evaluation_schema',)).is_evaluation_schema(schema):
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
            try:
                return self._record_browser_response(operation,body,request_hash,result,schema,validate,context,
                    catalog=catalog)
            except Review as error:
                failure = str(error)
                raise
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

    def _record_browser_response(self, operation, body, request_hash, result, schema, validate, context,
                                 *, catalog=None, received_at=None, recovery=False):
        """The same exact-response recorder for HTTP delivery and verified recovery."""
        failure = 'INVALID_RESPONSE_ENVELOPE'
        try:
            failure = 'INVALID_RESPONSE_ENVELOPE'
            if not isinstance(result, dict):
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
            received_at = time.time() if received_at is None else received_at
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
                if (not current or current['state'] not in (('PENDING','NEEDS_REVIEW') if recovery else ('PENDING',)) or current['body_hash'] != request_hash
                        or current['attempts'] != 1 or current['output'] is not None):
                    raise Review('NEUROBRO_BROWSER_REQUEST_NEEDS_REVIEW')
                old_receipt = self.db.execute('SELECT * FROM research_browser_responses WHERE operation=?', (operation,)).fetchone()
                if old_receipt is not None:
                    expected = (result['response_id'],sha(receipt_output),text,sha(text),chart_text)
                    actual = tuple(old_receipt[key] for key in ('response_id','output_sha256','assistant_text','assistant_sha256','chart_evidence'))
                    if not recovery or actual != expected:
                        raise Review('NEUROBRO_BROWSER_REQUEST_NEEDS_REVIEW')
                    received_at = old_receipt['received_at']
                else:
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
                changed = self.db.execute("UPDATE api_requests SET state='COMPLETE',output=?,failure_code=NULL WHERE operation=? AND state=?",
                                          (cached, operation, current['state'])).rowcount
                if changed != 1:
                    raise Review('NEUROBRO_BROWSER_REQUEST_NEEDS_REVIEW')
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            return value
        except Exception:
            raise Review(failure) from None

    def recover_evaluation_response(self, operation, completion):
        """Record actual durable native completion without calling ask/transport.

        Recovery is restricted to an existing once-admitted historical request.
        Missing/incomplete/wrong native custody causes no local response/status
        write. The normal provider recorder still validates raw output/charts.
        """
        from .trade_evaluation_recovery import validate_native_completion
        from agent.native_evaluation_protocol import is_evaluation_schema
        row = self.db.execute('SELECT * FROM research_browser_requests WHERE operation=?', (operation,)).fetchone()
        current = self.db.execute('SELECT * FROM api_requests WHERE operation=?', (operation,)).fetchone()
        if row is None or current is None:
            raise Review('NEUROBRO_BROWSER_REQUEST_NEEDS_REVIEW')
        body = json.loads(row['request_body'], object_pairs_hook=unique)
        rebuilt, serialized, wire = request_body(operation,body['prompt'],body['output_schema'],body['context'])
        if (not is_evaluation_schema(body['output_schema']) or body != rebuilt
                or serialized != row['request_body'] or row['request_sha256'] != wire
                or (row['provider'],row['contract_version'],row['marker']) != (BROWSER_PROVIDER,BROWSER_CONTRACT,body['marker'])
                or current['body_hash'] != wire or current['idempotency'] is not None
                or current['attempts'] != 1 or current['output'] is not None
                or current['state'] not in ('PENDING','NEEDS_REVIEW')):
            raise Review('NEUROBRO_BROWSER_REQUEST_NEEDS_REVIEW')
        verified = validate_native_completion({**body,'request_sha256':wire},completion,now=time.time())
        if not current['created'] <= verified['received_at'] <= time.time():
            raise Review('NEUROBRO_BROWSER_REQUEST_NEEDS_REVIEW')
        return self._record_browser_response(operation,body,wire,verified['result'],body['output_schema'],
            lambda value: None,body['context'],received_at=verified['received_at'],recovery=True)

    @staticmethod
    def _proven_unsent(result, body, request_hash):
        return (isinstance(result, dict) and set(result) == {'operation', 'marker', 'request_sha256', 'submitted', 'reason'}
            and (result['operation'], result['marker'], result['request_sha256']) == (body['operation'], body['marker'], request_hash)
            and result['submitted'] is False and result['reason'] in NO_SEND_REASONS)


__import__('agent.native_pending_protocol', fromlist=('install_worker_browser_protocol',)).install_worker_browser_protocol(globals())

__import__('agent.native_evaluation_protocol', fromlist=('install_worker_protocol',)).install_worker_protocol(globals())
