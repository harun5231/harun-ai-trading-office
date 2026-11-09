"""B4 consumer on the existing account and coordinator actors, no order authority.

The archive and room readers are trusted deployment dependencies. They must read
actual native receipts in the browser namespace (or an admitted read-only mount),
never an Office body or an assertion supplied by an analyst. No fallback guesses
an archive path or substitutes current market data.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import trade_evaluation as journal
from .trade_evaluation_evidence import bind_closed_trade, digest, need, PROTOCOL
from agent.native_evaluation_protocol import PHASE


class EvaluationConsumer:
    def __init__(self, directory, *, archive_reader, room_reader, native_receipt_reader):
        need(all(callable(f) for f in (archive_reader, room_reader, native_receipt_reader)),
            'EVALUATION_TRUSTED_NATIVE_READERS_REQUIRED')
        self.directory = Path(directory)
        self.archive_reader, self.room_reader = archive_reader, room_reader
        self.receipt_reader = native_receipt_reader
        self.status = {'state': 'NOT_OBSERVED'}

    def observe(self, coordinator, calendar, history, *, observed=False):
        """Use ALREADY collected account data, after protection/account recovery.

        Every failure remains local to evaluation. Neither an unowned position
        nor a missing old attachment may change orders or stop monitoring.
        """
        if not observed: journal.observe_calendar(self.directory, calendar)
        failures = {}
        jobs={j['id']:j for j in journal.read_status(self.directory)['jobs']}
        pending=[row for row in calendar.get('positions', []) if row.get('status')=='CLOSED'
            and jobs.get('post-trade:'+row['id'],{}).get('evidence_complete') is not True]
        # One evidence join per history refresh keeps archive I/O off the next
        # account/protection recovery interval. This is not a dialogue/trading limit.
        if pending:
            cursor=getattr(self,'binding_cursor',0)%len(pending)
            pending=[pending[cursor]];self.binding_cursor=cursor+1
        for closed in pending:
            try:
                binding = bind_closed_trade(coordinator, closed, history=history,
                    archive_reader=self.archive_reader)
                journal.attach_evidence(self.directory, binding)
            except Exception as error:
                # Fixed evidence errors, not financial payloads or credentials.
                code = str(error)
                failures[closed['id']] = code if code.startswith(('EVALUATION_', 'ORIGINAL_',
                    'HISTORICAL_', 'AUTHENTICATED_')) and len(code) <= 160 else 'EVALUATION_EVIDENCE_UNAVAILABLE'
        self.status = dict(state='BLOCKED_MISSING_EVIDENCE' if failures else 'OBSERVED', failures=failures)
        return self.status

    def advance(self, coordinator, today):
        """One existing coordinator tick, after its trading/protection work.

        Paid Mini dialogue is authorized only for acquiring trading setups,
        not post-trade evaluation. Keep data/evidence observation and recover
        existing claims from original receipts, but do not claim new work.
        This does not register robot_jobs/candidates or consume trade capacity.
        """
        if coordinator.neuro is None or coordinator.allowed(today) is not True: return None
        snapshot = journal.read_status(self.directory)
        # Once-intents outlive job qualification and current history bindings.
        # A superseded streak can still have an unknown in-flight native Send.
        unknown = snapshot['unresolved_evaluations']
        if unknown:
            # An older missing receipt must not starve recovery of a different
            # completed operation. No new Send is allowed while any outcome is
            # unknown in the shared room; rotate read-only receipt attempts.
            cursor = getattr(self, 'recovery_cursor', 0) % len(unknown)
            intent = unknown[cursor]
            self.recovery_cursor = cursor + 1
            operation = intent['operation']
            try:
                completed = journal.complete_evaluation(self.directory, operation,
                    native_receipt_reader=self.receipt_reader)
                current = any(job.get('analyst_evaluation', {}).get('operation') == operation
                    for job in completed['jobs'])
                self.status = dict(state='EVALUATED' if current else 'RECEIPT_RETAINED_SUPERSEDED_EVIDENCE',
                    operation=operation, recovered=True)
            except Exception:
                self.status = dict(state='EVALUATION_OUTCOME_UNKNOWN', operation=operation, replayed=False)
            return self.status
        ready = [j for j in snapshot['jobs'] if j['status'] == 'REQUIRES_NEUROBRO_EVALUATION']
        if not ready: return None
        # Defer BEFORE require_key/room reads/claim/provider dispatch. A policy
        # refusal is not an uncertain Send and must not reserve the shared room
        # or create an UNKNOWN claim on every restart. The journal's original
        # jobs, evidence, metrics and prior receipts remain untouched.
        self.status = dict(state='DEFERRED_MINI_SETUP_ONLY',
            reason='PAID_MODEL_NOT_AUTHORIZED_FOR_EVALUATION', pending_jobs=len(ready),
            new_claims=0, provider_calls=0)
        return self.status


def after_trading_tick(coordinator, today, result):
    """Optional admitted consumer shares the existing cycle lock and scheduler.

    Missing native archive topology is reported explicitly rather than creating
    a second dispatcher or declaring an unbound evaluation complete. The normal
    trading result is retained by the caller regardless of evaluation failure.
    """
    # A completed, received market HOLD is a real transaction boundary too.
    # The existing boundary can report data-only evaluation deferral or recover
    # an original receipt without interrupting replacement screening.
    # Never interrupt an analysis, pending review or uncertain browser Send.
    if type(result) is not dict:
        return
    decision = result.get('last_decision')
    completed_hold = (result.get('bot_status') == 'HOLD' and not result.get('failure_code')
        and result.get('wait_reason') is None and type(decision) is dict
        and decision.get('status') == 'HOLD' and not decision.get('failure_code'))
    idle = (result.get('bot_status') in {'WAITING', 'INSUFFICIENT_ACTIONABLE_SETUPS', 'REJECTED'}
        and result.get('wait_reason') in {'ROBOT_CAPACITY_FULL', 'ROBOT_CYCLE_COMPLETE'})
    if (not (completed_hold or idle) or coordinator.db.in_transaction or coordinator.unresolved_research()):
        return
    consumer = getattr(coordinator, 'evaluation_consumer', None)
    if consumer is None:
        coordinator.evaluation_status = dict(state='BLOCKED_MISSING_EVIDENCE',
            reason='EVALUATION_NATIVE_ARCHIVE_TRANSPORT_NOT_BOUND')
        return
    try:
        consumer.advance(coordinator, today)
        coordinator.evaluation_status = consumer.status
    except Exception:
        coordinator.evaluation_status = dict(state='EVALUATION_UNAVAILABLE')


def native_completion_reader(db, archive_root, operation, *, chain_reader=None, response_recorder=None):
    """Recover only actual durable native results, then use provider evidence.

    A worker may die after native COMPLETE but before saving its HTTP response.
    The authenticated read is therefore allowed BEFORE the local response row
    exists. Full request/room/material/raw-result proof is checked first; only
    the existing provider's verified-response recorder may restore that row.
    No ask, upload, new operation or synthetic local completion is permitted.
    """
    import time
    from .research_provider import source_evidence
    from .trade_evaluation_recovery import validate_native_completion
    from agent.native_evaluation_protocol import envelope
    request = db.execute('SELECT * FROM research_browser_requests WHERE operation=?', (operation,)).fetchone()
    need(request is not None,'EVALUATION_NATIVE_RECEIPT_NOT_FOUND')
    payload=json.loads(request['request_body'])
    bound=envelope(payload);internal=bound['request']
    need(callable(chain_reader),'EVALUATION_AUTHENTICATED_NATIVE_CHAIN_READER_REQUIRED')
    completion=chain_reader(operation,request['request_sha256'])
    verified=validate_native_completion({**payload,'request_sha256':request['request_sha256']},completion,now=time.time())
    response=db.execute('SELECT * FROM research_browser_responses WHERE operation=?',(operation,)).fetchone()
    journal=db.execute('SELECT state FROM api_requests WHERE operation=?',(operation,)).fetchone()
    if response is None or journal is None or journal['state']!='COMPLETE':
        need(callable(response_recorder),'EVALUATION_VERIFIED_RESPONSE_RECORDER_REQUIRED')
        response_recorder(operation,completion)
        response=db.execute('SELECT * FROM research_browser_responses WHERE operation=?',(operation,)).fetchone()
    need(response is not None,'EVALUATION_NATIVE_RECEIPT_NOT_FOUND')
    result=verified['result']
    need(response['response_id']==result['response_id']
        and response['assistant_text']==result['assistant_text']
        and response['assistant_sha256']==hashlib.sha256(result['assistant_text'].encode()).hexdigest()
        and json.loads(response['chart_evidence'])==result['chart_evidence'],
        'EVALUATION_NATIVE_RECEIPT_MISMATCH')
    evidence=source_evidence(db,operation)
    need(evidence.get('source_type')=='NEUROBRO_BROWSER','EVALUATION_NATIVE_SOURCE_UNVERIFIED')
    return dict(protocol=PROTOCOL,phase=PHASE,provider='NEUROBRO_WEB',operation=operation,
        request_sha256=bound['request_sha256'],input_evidence_sha256=internal['input_evidence_sha256'],
        room_id=internal['room_id'],sent_this_request=True,received_at=response['received_at'],
        response_id=response['response_id'],source_evidence_sha256=evidence['provider_evidence_sha256'],
        assistant_text=response['assistant_text'],assistant_sha256=response['assistant_sha256'])


class NativeEvaluationReads:
    """Fixed internal authenticated browser READ RPC on the existing service."""
    def __init__(self, provider):
        self.provider = provider
        filename=next(row[2] for row in provider.db.execute('PRAGMA database_list') if row[1]=='main')
        need(type(filename) is str and filename and Path(filename).is_absolute(),
            'EVALUATION_LEDGER_PATH_UNVERIFIED')
        self.directory=Path(filename).parent

    def _read(self, value):
        from .neurobrowser import BASE
        code,_,result=self.provider.transport('POST',BASE+'/v1/historical-evidence',
            self.provider.headers(),value,timeout=30)
        if code!=200:
            error=result.get('error') if type(result) is dict else None
            raise ValueError(error if type(error) is str and error.startswith('EVALUATION_')
                else 'EVALUATION_NATIVE_EVIDENCE_UNAVAILABLE')
        need(type(result) is dict and result.get('kind')==value['kind'],
            'EVALUATION_NATIVE_RESPONSE_INVALID')
        return result

    def archive(self, operation, request, response):
        import base64
        result=self._read(dict(kind='ARCHIVE',operation=operation,request=request,response=response))
        value={k:v for k,v in result.items() if k!='kind'}
        need(type(value.get('files')) is list and len(value['files'])==4,
            'EVALUATION_HISTORICAL_FOUR_FILES_REQUIRED')
        files=[]
        for row in value['files']:
            need(type(row) is dict and type(row.get('buffer_base64')) is str,
                'EVALUATION_HISTORICAL_BYTES_REQUIRED')
            raw=base64.b64decode(row['buffer_base64'],validate=True)
            need(0<len(raw)<=8_000_000,'EVALUATION_HISTORICAL_BYTES_REQUIRED')
            files.append({**{k:v for k,v in row.items() if k!='buffer_base64'},'buffer':raw})
        return {**value,'files':files}

    def room(self):
        result=self._read(dict(kind='BOUND_ROOM'))
        need(set(result)=={'kind','room_id'},'EVALUATION_NATIVE_RESPONSE_INVALID')
        return result['room_id']

    def completion(self, operation):
        return native_completion_reader(self.provider.db,None,operation,chain_reader=lambda op,wire:
            self._read(dict(kind='COMPLETION',operation=op,request_sha256=wire)),
            response_recorder=getattr(self.provider,'recover_evaluation_response',None))


def observe_authenticated_calendar(coordinator, directory, client, calendar, consumer=None):
    """Bind from the already refreshed credential-scoped history, no GETs.

    Native account actor calls this after observing/account recovery. Missing
    source/custody stays blocked and cannot affect protection or owner settings.
    """
    from .pnl_calendar import HistoryCache
    key=getattr(client,'_key',None)
    need(type(key) is str and bool(key),'AUTHENTICATED_HISTORY_CACHE_REQUIRED')
    with HistoryCache(directory,hashlib.sha256(key.encode()).hexdigest()) as cache:
        history=cache.load()
    consumer=consumer or getattr(coordinator,'evaluation_binding_consumer',None)
    if consumer is None:
        from .neurobrowser import browser_request, helper_token
        from types import SimpleNamespace
        token=helper_token()
        need(bool(token),'EVALUATION_NATIVE_EVIDENCE_UNAVAILABLE')
        # This read-only adapter does NOT construct NeuroBrowser, initialize
        # native journals, call require_key, or make any exchange/market GET.
        provider=SimpleNamespace(db=coordinator.db,transport=browser_request,
            headers=lambda:{'Authorization':'Bearer '+token,'Accept':'application/json',
                'Content-Type':'application/json'})
        reads=NativeEvaluationReads(provider)
        consumer=EvaluationConsumer(directory,archive_reader=reads.archive,
            room_reader=reads.room,native_receipt_reader=reads.completion)
    # Keep one consumer/cursor on the existing account actor, not a daemon.
    coordinator.evaluation_binding_consumer=consumer
    return consumer.observe(coordinator,calendar,history,observed=True)
