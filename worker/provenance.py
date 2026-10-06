"""Stable audit digests for completed provider evidence."""
import hashlib
import json

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def request_digest(db,operation):
    row=db.execute("SELECT output FROM api_requests WHERE operation=? AND state='COMPLETE'",(operation,)).fetchone()
    return digest(json.loads(row[0])) if row and row[0] else None
