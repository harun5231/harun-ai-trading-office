"""Allowlisted failure codes and a strictly read-only, payload-free ledger view."""
import re
import sqlite3
from pathlib import Path

CODES=frozenset(('NETWORK_UNCERTAIN','INVALID_RESPONSE_ENVELOPE','INVALID_OUTPUT_SCHEMA',
 'INVALID_SETUP_SCHEMA','INVALID_SETUP_SYMBOL_SIDE','INVALID_NUMERIC_TYPE','INVALID_NUMERIC_VALUE',
 'INVALID_ENTRY_TP_SL','RISK_REWARD_BELOW_2','VALIDATION_REJECTED',
 'RISK_ABOVE_5','INVALID_SCREENING_SCHEMA','INVALID_SCREENING_COUNT','INVALID_SCREENING_SYMBOL',
 'REJECT_QUANTITY_PRECISION_OR_RANGE','REJECT_MIN_NOTIONAL','REJECT_PERCENT_PRICE',
 'MARKET_DATA_REJECTED','LOCAL_PROCESSING_FAILED'))
def safe_code(value):
    if isinstance(value,str) and (value in CODES or re.fullmatch(r'HTTP_[1-5][0-9]{2}',value)):return value
    return 'VALIDATION_REJECTED'
def validation_code(error):
    value=str(error)
    return {'NEEDS_REVIEW: risiko melampaui batas':'RISK_ABOVE_5',
        'NEEDS_REVIEW: susunan ENTRY/TP/SL salah':'INVALID_ENTRY_TP_SL',
        'NEEDS_REVIEW: reward/risk kurang dari 1:2':'RISK_REWARD_BELOW_2'}.get(value,safe_code(value))
def read_records(root):
    # Never call state.directory, Ledger or NeuroAPI: no migration, key read or network.
    root=Path(root).resolve()
    path=root/'trading'/'ledger.sqlite3'
    if not path.exists():path=root/'browser'/'ledger.sqlite3'
    if not path.is_file():return []
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        cols={row[1] for row in db.execute('PRAGMA table_info(api_requests)')}
        if not cols:return []
        code='failure_code' if 'failure_code' in cols else 'NULL'
        rows=db.execute('SELECT operation,state,attempts,'+code+' FROM api_requests ORDER BY operation')
        result=[]
        for operation,state,attempts,failure in rows:
            operation=operation if isinstance(operation,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}:(screening|analysis:[A-Z0-9]{2,18}USDT)',operation) else 'OPERATION_REDACTED'
            state=state if state in ('PENDING','COMPLETE','NEEDS_REVIEW') else 'STATE_REDACTED'
            reason=safe_code(failure) if failure else ('LEGACY_REASON_UNAVAILABLE' if state=='NEEDS_REVIEW' else None)
            result.append(dict(operation=operation,state=state,attempts=attempts if type(attempts) is int and 0<=attempts<=1000000 else None,failure_code=reason))
        return result
    finally:db.close()
