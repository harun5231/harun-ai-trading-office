"""Observation-only post-trade journal. Never calls providers or order machinery.

The append-only SQLite file is separate from the financial/order ledger. A poll's
source snapshots and derived jobs commit together; replay makes no new events.
Jobs remain blocked until strict historical evidence joins are supplied. The
binding/once-intent/completion APIs below never dispatch a provider request;
only an actual source-bound native receipt can project an analyst evaluation.
"""
import hashlib
import json
from .trade_evaluation_metrics import net_pnl, decline_windows, monthly_metrics

from . import secure_sqlite_snapshot as snapshot_storage

FILENAME = 'trade_evaluation.sqlite3'
LOCK_TIMEOUT_SECONDS = 5


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _latest(db, kind):
    return {key: json.loads(payload) for key, payload in db.execute('''
        SELECT e.entity, e.payload FROM evaluation_events e
        WHERE e.kind=? AND e.seq=(SELECT max(x.seq) FROM evaluation_events x
        WHERE x.kind=e.kind AND x.entity=e.entity) ORDER BY e.entity''', (kind,))}


def _append(db, kind, key, value):
    payload = _json(value)
    old = db.execute('SELECT payload FROM evaluation_events WHERE kind=? AND entity=? ORDER BY seq DESC LIMIT 1',
                     (kind, key)).fetchone()
    if old and old[0] == payload:
        return
    db.execute('INSERT INTO evaluation_events(kind,entity,payload,sha256) VALUES(?,?,?,?)',
               (kind, key, payload, hashlib.sha256(payload.encode()).hexdigest()))


def _snapshot(db):
    trades = _latest(db, 'trade')
    scope = _latest(db, 'scope').get('calendar', {})
    completed = {row['operation'] for row in _latest(db, 'analyst_evaluation').values()}
    unresolved = [{key:row[key] for key in ('job_id','operation','input_evidence_sha256','reserved_at')}
        for row in _latest(db, 'evaluation_intent').values() if row['operation'] not in completed]
    return dict(status='OBSERVATION_ONLY', processed_trade_ids=sorted(trades),
                consecutive_losses=scope.get('consecutive_losses', 0),
                streak_complete=scope.get('streak_complete', False),
                streak_scope='OBSERVED_CLOSED_POSITIONS',
                history_missing_reasons=scope.get('history_missing_reasons', []),
                monthly_performance=monthly_metrics(trades, _latest(db, 'binding')),
                unresolved_evaluations=unresolved,
                jobs=_project_evaluations(db, list(_latest(db, 'job').values())))


def read_status(directory):
    """Read only: no filesystem SQLite connection, recovery or sidecar writes."""
    with snapshot_storage.snapshot_database(directory, FILENAME, lock_timeout=LOCK_TIMEOUT_SECONDS) as db:
        if db is None:
            return dict(status='NOT_OBSERVED', processed_trade_ids=[], consecutive_losses=0,
                        streak_complete=False, streak_scope='OBSERVED_CLOSED_POSITIONS',
                        history_missing_reasons=['HISTORY_NOT_OBSERVED'], monthly_performance=[],
                        unresolved_evaluations=[], jobs=[])
        return _snapshot(db)


def observe_calendar(directory, calendar):
    """Ingest existing reconstructed *net* PNL, preserving the exact source row."""
    with snapshot_storage.snapshot_database(directory, FILENAME, write=True,
                                           lock_timeout=LOCK_TIMEOUT_SECONDS) as db:
        return _observe(db, calendar)


def _observe(db, calendar):
    try:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS evaluation_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                entity TEXT NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE INDEX IF NOT EXISTS evaluation_entity ON evaluation_events(kind,entity,seq);
            CREATE TRIGGER IF NOT EXISTS evaluation_no_update BEFORE UPDATE ON evaluation_events
                BEGIN SELECT RAISE(ABORT,'APPEND_ONLY_EVALUATION'); END;
            CREATE TRIGGER IF NOT EXISTS evaluation_no_delete BEFORE DELETE ON evaluation_events
                BEGIN SELECT RAISE(ABORT,'APPEND_ONLY_EVALUATION'); END;
        ''')
        db.execute('BEGIN IMMEDIATE')
        for row in calendar.get('positions', []):
            if row.get('status') == 'CLOSED':
                _append(db, 'trade', row['id'], row)
        trades = _latest(db, 'trade')
        losses = 0
        loss_ids = []
        active_streaks = set()
        ordered = sorted(trades.values(), key=lambda row: (row['closed_at'], row['id']))
        for row in ordered:
            pnl = net_pnl(row)
            losses = losses + 1 if pnl is not None and pnl < 0 else 0
            missing = ['HISTORICAL_CHART_ARCHIVE_NOT_LINKED', 'ORIGINAL_NEUROBRO_DECISION_NOT_LINKED',
                       'REQUIRES_NEUROBRO_EVALUATION']
            if pnl is None:
                missing += ['CLOSED_NET_PNL_INCOMPLETE'] + row.get('incomplete_reasons', [])
            job = dict(id='post-trade:' + row['id'], trade_ids=[row['id']],
                       status='BLOCKED_MISSING_EVIDENCE', evidence_complete=False,
                       missing_reasons=missing,
                       evidence=dict(closed_position=row, closed_position_sha256=hashlib.sha256(_json(row).encode()).hexdigest()),
                       findings=dict(net_pnl_usdt=row['pnl_usdt'] if pnl is not None else None,
                                     outcome='UNKNOWN' if pnl is None else 'LOSS' if pnl < 0 else 'WIN' if pnl > 0 else 'BREAKEVEN'),
                       experiment_outcome=dict(status='NOT_RUN', reason='REQUIRES_NEUROBRO_EVALUATION'))
            _append(db, 'job', job['id'], job)
            loss_ids = loss_ids + [row['id']] if losses else []
            # Three is an observation trigger, never a trading/risk policy.
            # Each qualifying closing trade owns exactly one stable job ID.
            if losses >= 3:
                streak = dict(job, id='losing-streak:' + row['id'], trade_ids=list(loss_ids),
                              evidence=dict(closed_positions=[trades[key] for key in loss_ids]),
                              findings=dict(consecutive_losses=losses))
                _append(db, 'job', streak['id'], streak)
                active_streaks.add(streak['id'])
        for rows, findings in decline_windows(ordered):
            key = 'performance-decline:' + rows[-1]['id']
            job = dict(id=key, trade_ids=[row['id'] for row in rows],
                status='BLOCKED_MISSING_EVIDENCE', evidence_complete=False,
                missing_reasons=['HISTORICAL_CHART_ARCHIVE_NOT_LINKED',
                    'ORIGINAL_NEUROBRO_DECISION_NOT_LINKED', 'REQUIRES_NEUROBRO_EVALUATION'],
                evidence=dict(closed_positions=rows), findings=findings,
                experiment_outcome=dict(status='NOT_RUN', reason='REQUIRES_NEUROBRO_EVALUATION'))
            _append(db, 'job', key, job)
            active_streaks.add(key)
        for key, job in _latest(db, 'job').items():
            if key.startswith(('losing-streak:', 'performance-decline:')) and key not in active_streaks:
                _append(db, 'job', key, dict(job, status='SUPERSEDED_HISTORY_REVISION'))
        history_missing = list(calendar.get('incomplete_reasons', []))
        if any(row.get('complete') is not True or row.get('pnl_usdt') is None for row in trades.values()):
            history_missing.append('CLOSED_NET_PNL_INCOMPLETE')
        _append(db, 'scope', 'calendar', dict(consecutive_losses=losses,
                streak_complete=calendar.get('complete') is True and not history_missing,
                history_missing_reasons=history_missing))
        result = _snapshot(db)
        db.execute('COMMIT')
        return result
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise


# These operations extend the SAME append-only snapshot journal. They neither
# mutate the trading ledger nor dispatch messages; the existing native caller
# must supply verified history/archive/native-receipt readers.
def _validated_binding(binding):
    from .trade_evaluation_evidence import PROTOCOL, digest, need
    import re
    need(type(binding)is dict and binding.get('version')==PROTOCOL
        and binding.get('binding_sha256')==digest({k:v for k,v in binding.items()if k!='binding_sha256'}),
        'EVALUATION_BINDING_INVALID')
    need(all(type(binding.get(k))is str and re.fullmatch('[a-f0-9]{64}',binding[k])for k in
        ('closed_position_sha256','financial_receipt_sha256','fills_sha256','original_reply_sha256'))
        and type(binding.get('original_reply'))is str
        and hashlib.sha256(binding['original_reply'].encode()).hexdigest()==binding['original_reply_sha256']
        and binding.get('source_evidence',{}).get('source_type')=='NEUROBRO_BROWSER'
        and binding.get('original_decision',{}).get('side')in('LONG','SHORT')
        and type(binding.get('materials'))is dict
        and {(r.get('kind'),r.get('timeframe'))for r in binding['materials'].get('files',[])}==
            {(kind,tf)for kind in('PNG','CSV')for tf in('15m','1h')},'EVALUATION_BINDING_INVALID')
    return binding


def _project_evaluations(db, jobs):
    from .trade_evaluation_evidence import digest
    bindings=_latest(db,'binding');results=_latest(db,'analyst_evaluation');intents=_latest(db,'evaluation_intent');trades=_latest(db,'trade')
    for job in jobs:
        if job['status']=='SUPERSEDED_HISTORY_REVISION':continue
        valid={}
        for trade_id in job['trade_ids']:
            value=bindings.get(trade_id)
            trade=trades.get(trade_id)
            if value is not None and trade is not None:
                _validated_binding(value)
                if value['closed_position_sha256']==digest(trade):valid[trade_id]=value
        if len(valid)!=len(job['trade_ids']):continue
        missing=[r for r in job['missing_reasons']if r not in
            ('HISTORICAL_CHART_ARCHIVE_NOT_LINKED','ORIGINAL_NEUROBRO_DECISION_NOT_LINKED')]
        if any(r!='REQUIRES_NEUROBRO_EVALUATION'for r in missing):continue
        evidence_sha=digest(dict(job_id=job['id'],trade_ids=job['trade_ids'],
            bindings=[valid[t]['binding_sha256']for t in job['trade_ids']]))
        job.update(status='REQUIRES_NEUROBRO_EVALUATION',evidence_complete=True,
            missing_reasons=['REQUIRES_NEUROBRO_EVALUATION'],input_evidence_sha256=evidence_sha,
            evidence_bindings={t:valid[t]['binding_sha256']for t in job['trade_ids']})
        result=results.get(job['id']);intent=intents.get(job['id'])
        if result is not None and result['input_evidence_sha256']==evidence_sha:
            job.update(status='EVALUATED'if result['assessment']!='INCONCLUSIVE'else'EVALUATED_INCONCLUSIVE',
                missing_reasons=[],analyst_evaluation=result)
        elif intent is not None and intent['operation']not in {r['operation']for r in results.values()}:
            job.update(status='EVALUATION_OUTCOME_UNKNOWN',
                missing_reasons=['NATIVE_EVALUATION_RECEIPT_NOT_CONFIRMED'])
    return jobs


def attach_evidence(directory, binding):
    """Persist the strict join produced by bind_closed_trade; no financial write."""
    from .trade_evaluation_evidence import digest, need
    _validated_binding(binding)
    with snapshot_storage.snapshot_database(directory,FILENAME,write=True,lock_timeout=LOCK_TIMEOUT_SECONDS)as db:
        trades=_latest(db,'trade');trade=trades.get(binding['trade_id'])
        need(trade is not None and binding['closed_position_sha256']==digest(trade),
            'EVALUATION_HISTORY_REVISION_CHANGED')
        db.execute('BEGIN IMMEDIATE')
        try:
            _append(db,'binding',binding['trade_id'],binding)
            result=_snapshot(db);db.execute('COMMIT');return result
        except BaseException:
            db.execute('ROLLBACK');raise


def claim_evaluation(directory, job_id, room_id, *, now=None):
    """Durably claim one same-room native request BEFORE a possible Send.

    Never call the provider again after this returns an existing unresolved
    claim. A completed result can be read from status; there is no blind replay
    or arbitrary new operation ID after a crash.
    """
    import time,math
    from .trade_evaluation_evidence import evaluation_request, digest, need
    at=time.time()if now is None else now
    need(type(at)in(int,float)and math.isfinite(at)and at>0,'EVALUATION_TIME_INVALID')
    with snapshot_storage.snapshot_database(directory,FILENAME,write=True,lock_timeout=LOCK_TIMEOUT_SECONDS)as db:
        jobs={j['id']:j for j in _snapshot(db)['jobs']};job=jobs.get(job_id)
        need(job is not None and job['status']=='REQUIRES_NEUROBRO_EVALUATION','EVALUATION_JOB_NOT_READY')
        request=evaluation_request(job,_latest(db,'binding'),room_id)
        old=_latest(db,'evaluation_intent').get(job_id)
        if old is not None:
            done=_latest(db,'analyst_evaluation').get(job_id)
            need(done is not None and done['operation']==old['operation'],'EVALUATION_OUTCOME_UNKNOWN_NO_REPLAY')
            need(old['operation']!=request['operation'],'EVALUATION_ALREADY_COMPLETED_NO_REPLAY')
        value=dict(request=request,operation=request['operation'],job_id=job_id,
            request_sha256=digest(request),input_evidence_sha256=request['input_evidence_sha256'],reserved_at=at)
        db.execute('BEGIN IMMEDIATE')
        try:
            _append(db,'evaluation_intent',job_id,value);db.execute('COMMIT');return value
        except BaseException:
            db.execute('ROLLBACK');raise


def complete_evaluation(directory, operation, *, native_receipt_reader):
    """Append a real, source-bound Neurobro receipt, not a model/owner assertion.

    The trusted internal reader revalidates browser request/completion/room
    custody and returns the normalized native envelope. Its request_sha256
    links the internal evaluation request; the adapter must also verify the
    actual native wire-body hash through source_evidence_sha256, never relabel
    that transport hash as this input digest. The envelope itself is
    never accepted from Office input or a provider reply claiming verification.
    """
    import re,math
    from .trade_evaluation_evidence import digest, need, parse_evaluation_reply, PROTOCOL
    need(callable(native_receipt_reader),'EVALUATION_NATIVE_RECEIPT_READER_REQUIRED')
    with snapshot_storage.snapshot_database(directory,FILENAME,lock_timeout=LOCK_TIMEOUT_SECONDS)as db:
        values=[]if db is None else [v for v in _latest(db,'evaluation_intent').values()if v['operation']==operation]
        need(len(values)==1,'EVALUATION_INTENT_NOT_UNIQUE');intent=values[0]
    receipt=native_receipt_reader(operation)
    request=intent['request']
    need(type(receipt)is dict and set(receipt)=={'protocol','phase','provider','operation','request_sha256',
        'input_evidence_sha256','room_id','sent_this_request','received_at','response_id','source_evidence_sha256',
        'assistant_text','assistant_sha256'}and receipt.get('protocol')==PROTOCOL
        and receipt.get('phase')=='POST_TRADE_EVALUATION'and receipt.get('provider')=='NEUROBRO_WEB'
        and receipt.get('operation')==operation and receipt.get('request_sha256')==intent['request_sha256']
        and receipt.get('input_evidence_sha256')==intent['input_evidence_sha256']
        and receipt.get('room_id')==request['room_id']and receipt.get('sent_this_request')is True
        and type(receipt.get('received_at'))in(int,float)and math.isfinite(receipt['received_at'])
        and receipt['received_at']>=intent['reserved_at']
        and type(receipt.get('response_id'))is str and re.fullmatch('[A-Za-z0-9_.:-]{1,160}',receipt['response_id'])
        and type(receipt.get('source_evidence_sha256'))is str
        and re.fullmatch('[a-f0-9]{64}',receipt['source_evidence_sha256'])
        and type(receipt.get('assistant_text'))is str
        and receipt.get('assistant_sha256')==__import__('hashlib').sha256(receipt['assistant_text'].encode()).hexdigest(),
        'EVALUATION_NATIVE_RECEIPT_MISMATCH')
    answer=parse_evaluation_reply(receipt['assistant_text'])
    record=dict(operation=operation,input_evidence_sha256=intent['input_evidence_sha256'],
        assessment=answer['assessment'],findings=answer['findings'],native_receipt=receipt,
        experiment_outcome=dict(status='NOT_RUN',reason='ANALYST_PROPOSALS_ARE_NOT_EXPERIMENT_RESULTS'))
    with snapshot_storage.snapshot_database(directory,FILENAME,write=True,lock_timeout=LOCK_TIMEOUT_SECONDS)as db:
        need(_latest(db,'evaluation_intent').get(intent['job_id'])==intent,'EVALUATION_INTENT_CHANGED')
        # Native completion belongs to the exact original once-request even if
        # backfill later superseded its trigger or changed the source rows.
        # Retain that actual receipt and clear the room's unknown outcome; the
        # projection still requires current matching evidence before displaying
        # an assessment. A stale claim can NEVER authorize another Send.
        old=_latest(db,'analyst_evaluation').get(intent['job_id'])
        if old is not None and old.get('operation')==operation:
            need(old==record,'EVALUATION_RECEIPT_REPLAY_CHANGED');return _snapshot(db)
        db.execute('BEGIN IMMEDIATE')
        try:
            _append(db,'analyst_evaluation',intent['job_id'],record)
            result=_snapshot(db);db.execute('COMMIT');return result
        except BaseException:
            db.execute('ROLLBACK');raise


def read_evaluation_claim(directory, operation):
    """Only an unresolved, current evidence claim can authorize native Send.

    The native adapter also checks its own immutable wire request and owner ON
    gate. Completed results remain readable through status, never dispatchable.
    """
    from .trade_evaluation_evidence import need, digest
    with snapshot_storage.snapshot_database(directory,FILENAME,lock_timeout=LOCK_TIMEOUT_SECONDS) as db:
        if db is None:return None
        intents=[v for v in _latest(db,'evaluation_intent').values() if v['operation']==operation]
        if not intents:return None
        need(len(intents)==1,'EVALUATION_INTENT_NOT_UNIQUE')
        intent=intents[0]
        job=next((j for j in _snapshot(db)['jobs'] if j['id']==intent['job_id']),None)
        need(job is not None and job['status']=='EVALUATION_OUTCOME_UNKNOWN'
            and job.get('input_evidence_sha256')==intent['input_evidence_sha256']
            and digest(intent['request'])==intent['request_sha256'],'EVALUATION_HISTORY_REVISION_CHANGED')
        return intent


def evaluation_operation(directory, job_id):
    """Recover the existing once operation; never allocate a replacement ID."""
    with snapshot_storage.snapshot_database(directory,FILENAME,lock_timeout=LOCK_TIMEOUT_SECONDS) as db:
        if db is None:return None
        value=_latest(db,'evaluation_intent').get(job_id)
        return value['operation'] if value else None
