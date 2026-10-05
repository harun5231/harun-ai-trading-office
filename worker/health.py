"""Private authenticated worker health; never prints tokens."""
import json
from pathlib import Path
from urllib.request import Request,urlopen

def probe_api():
    token=Path('/run/office/read_token').read_text().strip()
    req=Request('http://127.0.0.1:8787/health',headers={'Authorization':'Bearer '+token})
    with urlopen(req,timeout=5) as r:return json.load(r)['status']=='ONLINE'
if __name__=='__main__':
    try:okay=probe_api()
    except Exception:okay=False
    raise SystemExit(0 if okay else 1)
