"""Allowlisted failure codes and a strictly read-only, payload-free ledger view."""
import re
import sqlite3
from pathlib import Path

CODES=frozenset(('NETWORK_UNCERTAIN','INVALID_RESPONSE_ENVELOPE','INVALID_OUTPUT_SCHEMA',
 'SCREENING_CATALOG_REQUIRED','INVALID_SETUP_SCHEMA','INVALID_SETUP_SYMBOL_SIDE','INVALID_NUMERIC_TYPE','INVALID_NUMERIC_VALUE',
 'INVALID_ENTRY_TP_SL','RISK_REWARD_BELOW_2','VALIDATION_REJECTED',
 'INVALID_PRICE_FILTER','RISK_ABOVE_5','POSITION_SIZE_NOT_MAX_RISK','NO_LEGAL_MAX_RISK_QUANTITY','INVALID_SCREENING_SCHEMA','INVALID_SCREENING_COUNT','INVALID_SCREENING_SYMBOL',
 'REJECT_QUANTITY_PRECISION_OR_RANGE','REJECT_MIN_NOTIONAL','REJECT_PERCENT_PRICE',
 'MARKET_DATA_REJECTED','LOCAL_PROCESSING_FAILED','STALE_CONTRACT_CONTEXT','INVALID_CONTRACT_CONTEXT'))
def safe_code(value):
    if isinstance(value,str) and (value in CODES or re.fullmatch(r'HTTP_[1-5][0-9]{2}',value)):return value
    return 'VALIDATION_REJECTED'
def validation_code(error):
    value=str(error)
    return {'NEEDS_REVIEW: harga tidak sesuai tick/rentang; level tidak diubah otomatis':'INVALID_PRICE_FILTER',
        'NEEDS_REVIEW: risiko melampaui batas':'RISK_ABOVE_5',
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
            operation=operation if isinstance(operation,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}:(robot-v7:[0-2]:(?:screening:[0-3]:[12]|analysis-v7:[A-Z0-9]{2,18}USDT)|screening|replacement-screening:[1-3]|manual-screening:v2|(?:analysis|analysis-v3|analysis-v4|analysis-v5|analysis-v6):[A-Z0-9]{2,18}USDT)',operation) else 'OPERATION_REDACTED'
            state=state if state in ('PENDING','COMPLETE','NEEDS_REVIEW') else 'STATE_REDACTED'
            reason=safe_code(failure) if failure else ('LEGACY_REASON_UNAVAILABLE' if state=='NEEDS_REVIEW' else None)
            result.append(dict(operation=operation,state=state,attempts=attempts if type(attempts) is int and 0<=attempts<=1000000 else None,failure_code=reason))
        return result
    finally:db.close()
