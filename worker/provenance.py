"""Explicit current producer contract; never backfill or promote legacy records.

Hashes detect changed persisted evidence, not malicious database administrators.
Shadow additionally revalidates provider levels and live public contract rules.
"""
import hashlib
import json
from .core import Review

ANALYSIS_VERSION='analysis-v6'
CONTRACT_VERSION='neuroapi-decision-v1'
SIZING_VERSION='deterministic-max-risk-5-v1'
SOURCE_TYPE='NEUROAPI_STRUCTURED'

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def request_digest(db,operation):
    row=db.execute("SELECT output FROM api_requests WHERE operation=? AND state='COMPLETE'",(operation,)).fetchone()
    return digest(json.loads(row[0])) if row and row[0] else None

def stamp(db,record,operation):
    """Called only by current producers, after validation, before atomic persistence."""
    payload={k:v for k,v in record.items() if k!='provenance'}
    record['provenance']=dict(contract_version=CONTRACT_VERSION,analysis_version=ANALYSIS_VERSION,
        source_type=SOURCE_TYPE,execution_sizing_version=SIZING_VERSION,operation=operation,
        payload_sha256=digest(payload),request_output_sha256=request_digest(db,operation))
    return record

def current(db,record,operation):
    """Unknown/legacy unmarked records are ineligible; current claims fail closed."""
    claimed=isinstance(record,dict) and 'provenance' in record
    if not claimed and (':'+ANALYSIS_VERSION+':') not in operation:return False
    try:
        proof=record['provenance']
        expected=dict(contract_version=CONTRACT_VERSION,analysis_version=ANALYSIS_VERSION,
            source_type=SOURCE_TYPE,execution_sizing_version=SIZING_VERSION,operation=operation,
            payload_sha256=digest({k:v for k,v in record.items() if k!='provenance'}),
            request_output_sha256=request_digest(db,operation))
        if proof!=expected:raise ValueError
        if record.get('status','ACCEPT') in ('ACCEPT','HOLD') and not expected['request_output_sha256']:raise ValueError
    except Exception:raise Review('SHADOW_SOURCE_UNVERIFIED') from None
    return True
