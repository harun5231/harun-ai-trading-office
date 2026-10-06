"""Authenticated process liveness only; no account, provider or order requests."""
import json
from pathlib import Path
from urllib.request import Request,urlopen

def probe_api():
    token=Path('/run/office/read_token').read_text().strip()
    req=Request('http://127.0.0.1:8787/health',headers={'Authorization':'Bearer '+token})
    with urlopen(req,timeout=5) as r:
        value=json.load(r)
    return isinstance(value,dict) and value.get('status')=='ONLINE' and value.get('mode')=='ORDER_PIPELINE' and type(value.get('gateway_connected')) is bool
if __name__=='__main__':
    try:okay=probe_api()
    except Exception:okay=False
    raise SystemExit(0 if okay else 1)
