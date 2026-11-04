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
from agent.native_evaluation_protocol import native_arguments, PHASE


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

        Claims precede provider dispatch. After a crash or unknown result we
        only look for an actual native receipt; the request is NEVER re-sent.
        This does not register robot_jobs/candidates or consume trade capacity.
        """
        if coordinator.neuro is None or coordinator.allowed(today) is not True: return None
        snapshot = journal.read_status(self.directory)
        unknown = [j for j in snapshot['jobs'] if j['status'] == 'EVALUATION_OUTCOME_UNKNOWN']
        for job in unknown:
            operation = job.get('evaluation_operation')
            if not operation:
                operation = journal.evaluation_operation(self.directory, job['id'])
            try:
                journal.complete_evaluation(self.directory, operation, native_receipt_reader=self.receipt_reader)
                self.status = dict(state='EVALUATED', operation=operation, recovered=True)
            except Exception:
                self.status = dict(state='EVALUATION_OUTCOME_UNKNOWN', operation=operation, replayed=False)
            return self.status
        ready = [j for j in snapshot['jobs'] if j['status'] == 'REQUIRES_NEUROBRO_EVALUATION']
        if not ready: return None
        # Existing native browser readiness is checked BEFORE a durable claim.
        # Failure does not change owner ON/OFF or hold the trading pipeline.
        coordinator.neuro.require_key()
        room_id = self.room_reader()
        need(type(room_id) is str and __import__('re').fullmatch('[a-f0-9]{32}', room_id),
            'EVALUATION_BOUND_ROOM_NOT_AVAILABLE')
        claim = journal.claim_evaluation(self.directory, ready[0]['id'], room_id)
        args = native_arguments(claim)
        def permitted(): return coordinator.allowed(today) is True
        try:
            coordinator.neuro.ask(args['operation'], args['prompt'], args['output_schema'],
                lambda value: __import__('worker.trade_evaluation_evidence',
                    fromlist=('parse_evaluation_reply',)).parse_evaluation_reply(value['findings']),
                args['context'], continue_if=permitted)
            journal.complete_evaluation(self.directory, args['operation'], native_receipt_reader=self.receipt_reader)
            self.status = dict(state='EVALUATED', operation=args['operation'], recovered=False)
        except Exception:
            self.status = dict(state='EVALUATION_OUTCOME_UNKNOWN', operation=args['operation'], replayed=False)
        return self.status


def after_trading_tick(coordinator, today, result):
    """Optional admitted consumer shares the existing cycle lock and scheduler.

    Missing native archive topology is reported explicitly rather than creating
    a second dispatcher or declaring an unbound evaluation complete. The normal
    trading result is retained by the caller regardless of evaluation failure.
    """
    # Analysis, pending review, fills and protection always go first. Historical
    # dialogue only uses an existing idle/saturated/completed opportunity. The
    # one shared room necessarily serializes an already-sent discussion.
    if (type(result) is not dict or result.get('bot_status') not in
            {'WAITING', 'INSUFFICIENT_ACTIONABLE_SETUPS'}
            or result.get('wait_reason') not in {'ROBOT_CAPACITY_FULL', 'ROBOT_CYCLE_COMPLETE'}
            or coordinator.db.in_transaction or coordinator.unresolved_research()):
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


def native_completion_reader(db, archive_root, operation, *, chain_reader=None):
    """Revalidate native wire/source AND actual browser once/completion custody."""
    from .research_provider import source_evidence
    from agent.native_evaluation_protocol import envelope
    from agent.native_evaluation_protocol import native_sha as sha
    request = db.execute('SELECT * FROM research_browser_requests WHERE operation=?', (operation,)).fetchone()
    response = db.execute('SELECT * FROM research_browser_responses WHERE operation=?', (operation,)).fetchone()
    need(request is not None and response is not None, 'EVALUATION_NATIVE_RECEIPT_NOT_FOUND')
    evidence = source_evidence(db, operation)
    need(evidence.get('source_type') == 'NEUROBRO_BROWSER', 'EVALUATION_NATIVE_SOURCE_UNVERIFIED')
    payload = json.loads(request['request_body']);bound = envelope(payload);internal = bound['request']
    need(callable(chain_reader),'EVALUATION_AUTHENTICATED_NATIVE_CHAIN_READER_REQUIRED')
    chain=chain_reader(operation,request['request_sha256'])
    need(type(chain) is list and chain, 'EVALUATION_NATIVE_RECEIPT_NOT_FOUND')
    previous=None
    for n,row in enumerate(chain,1):
        need(type(row) is dict and set(row)=={'intent','complete'},'EVALUATION_NATIVE_RECEIPT_INVALID')
        intent,complete=row['intent'],row['complete']
        need(type(complete) is dict
            and complete == dict(intent,intent_sha256=sha(intent),response_id=complete.get('response_id'),
                final_reply_sha256=complete.get('final_reply_sha256'))
            and intent['kind']=='NATIVE_POST_TRADE_EVALUATION' and intent['phase']==PHASE
            and intent['operation']==operation and intent['request_sha256']==request['request_sha256']
            and intent['marker']==request['marker'] and intent['input_request_sha256']==bound['request_sha256']
            and intent['input_evidence_sha256']==internal['input_evidence_sha256']
            and intent['room_id']==internal['room_id']
            and intent['followup_sequence']==n and intent['previous_followup_intent_sha256']==previous
            and intent['historical_access_sha256']==digest(internal['historical_materials']),
            'EVALUATION_NATIVE_RECEIPT_MISMATCH')
        previous=sha(intent)
    from agent.native_evaluation_protocol import validate_material_access_chain
    validate_material_access_chain(internal,chain,json.loads(response['chart_evidence']),
        final_response=dict(response_id=response['response_id'],assistant_sha256=response['assistant_sha256']))
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
            self._read(dict(kind='COMPLETION',operation=op,request_sha256=wire))['chain'])


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
