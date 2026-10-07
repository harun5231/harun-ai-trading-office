"""Allowlisted failure codes and a strictly read-only, payload-free ledger view."""
import re
import sqlite3
import stat
from pathlib import Path

CODES=frozenset(('NETWORK_UNCERTAIN','INVALID_RESPONSE_ENVELOPE','INVALID_OUTPUT_SCHEMA',
 'SCREENING_CATALOG_REQUIRED','INVALID_SETUP_SCHEMA','INVALID_SETUP_SYMBOL_SIDE','INVALID_NUMERIC_TYPE','INVALID_NUMERIC_VALUE',
 'INVALID_ENTRY_TP_SL','RISK_REWARD_BELOW_2','VALIDATION_REJECTED',
 'INVALID_PRICE_FILTER','RISK_ABOVE_5','POSITION_SIZE_NOT_MAX_RISK','NO_LEGAL_MAX_RISK_QUANTITY','INVALID_SCREENING_SCHEMA','INVALID_SCREENING_COUNT','INVALID_SCREENING_SYMBOL',
 'REJECT_QUANTITY_PRECISION_OR_RANGE','REJECT_MIN_NOTIONAL','REJECT_PERCENT_PRICE',
 'MARKET_DATA_REJECTED','LOCAL_PROCESSING_FAILED','STALE_CONTRACT_CONTEXT','INVALID_CONTRACT_CONTEXT',
 'INVALID_ORDER_CONTRACT','INVALID_RISK_REWARD','INVALID_RISK_TARGET','INVALID_EXECUTION_QUANTITY',
 'INVALID_SIDE','INVALID_RULES','RISK_ABOVE_TARGET','FEE_EVIDENCE_UNAVAILABLE','INVALID_FEE_EVIDENCE',
 'STALE_FEE_EVIDENCE','NET_RISK_REWARD_BELOW_2','NET_RISK_REWARD_NOT_TARGET_2','ORDER_COSTS_CHANGED','INVALID_COST_MODEL','RESEARCH_PAUSED','NEUROAPI_REQUEST_LIMIT_EXCEEDED'))
# Old operation IDs remain readable audit metadata, never executable requests.
OPERATION_PATTERN=(r'\d{4}-\d{2}-\d{2}:(?:robot-v8:[0-2]:(?:screening:[0-3]:[12]|analysis-v8:[A-Z0-9]{2,18}USDT)'
    r'|robot-v9:(?:0|[1-9]\d{0,11}):(?:screening:[0-3]:[12]|analysis-v9:[A-Z0-9]{2,18}USDT))')
def safe_code(value):
    if isinstance(value,str) and (value in CODES or re.fullmatch(r'HTTP_[1-5][0-9]{2}',value)):return value
    return 'VALIDATION_REJECTED'
def validation_code(error):
    value=str(error)
    return {'NEEDS_REVIEW: harga tidak sesuai tick/rentang; level tidak diubah otomatis':'INVALID_PRICE_FILTER',
        'NEEDS_REVIEW: risiko melampaui batas':'RISK_ABOVE_TARGET',
        'NEEDS_REVIEW: susunan ENTRY/TP/SL salah':'INVALID_ENTRY_TP_SL',
        'NEEDS_REVIEW: reward/risk kurang dari 1:2':'RISK_REWARD_BELOW_2'}.get(value,safe_code(value))
def read_records(root):
    # Never call state.directory, Ledger or NeuroAPI: no migration, key read or network.
    root=Path(root).resolve()
    path=root/'trading'/'ledger.sqlite3'
    try:value=path.lstat()
    except FileNotFoundError:return []
    if not stat.S_ISREG(value.st_mode) or value.st_nlink!=1:raise ValueError('WORKER_STATE_INVALID')
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)
    try:
        db.execute('PRAGMA query_only=ON')
        cols={row[1] for row in db.execute('PRAGMA table_info(api_requests)')}
        if not cols:return []
        code='failure_code' if 'failure_code' in cols else 'NULL'
        rows=db.execute('SELECT operation,state,attempts,'+code+' FROM api_requests ORDER BY operation DESC LIMIT 1000')
        result=[]
        for operation,state,attempts,failure in rows:
            operation=operation if isinstance(operation,str) and re.fullmatch(OPERATION_PATTERN,operation) else 'OPERATION_REDACTED'
            state=state if state in ('PENDING','COMPLETE','NEEDS_REVIEW','REJECTED_REQUEST_VALIDATION') else 'STATE_REDACTED'
            reason=safe_code(failure) if failure else ('FAILURE_CODE_UNAVAILABLE' if state=='NEEDS_REVIEW' else None)
            result.append(dict(operation=operation,state=state,attempts=attempts if type(attempts) is int and 0<=attempts<=1000000 else None,failure_code=reason))
        return result
    finally:db.close()
