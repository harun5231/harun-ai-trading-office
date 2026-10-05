"""Official NeuroAPI, smart/non-streaming structured output. Never broker execution."""
import hashlib
import json
import os
import re
import time
from pathlib import Path
from .core import Review,number,Signal
from .http_client import request

BASE='https://api.neurobro.ai/api/v1'
SCREEN_SCHEMA={'type':'object','properties':{'symbols':{'type':'array','items':{'type':'string'},'minItems':2,'maxItems':2,'uniqueItems':True}},'required':['symbols'],'additionalProperties':False}
NUM={'type':'string','pattern':r'^\d+(\.\d+)?$'}
SETUP_SCHEMA={'type':'object','properties':{'symbol':{'type':'string'},'side':{'type':'string','enum':['LONG','SHORT']},
 'position_size':{**NUM,'description':'Quantity in base-asset units, not USDT notional or margin'},
 'limit_entry':NUM,'take_profit':NUM,'stop_loss':NUM,
 'risk_reward':{**NUM,'description':'Reward divided by risk; 2 means risk:reward 1:2'}},
 'required':['symbol','side','position_size','limit_entry','take_profit','stop_loss','risk_reward'],'additionalProperties':False}

def api_key():
    try:
        file=os.environ.get('NEUROBRO_API_KEY_FILE')
        key=Path(file).read_text().strip() if file else os.environ.get('NEUROBRO_API_KEY','').strip()
        if not key or len(key)>512 or not key.isascii() or any(c.isspace() for c in key):return ''
        return key
    except OSError:return ''

def selections(output,catalog):
    if not isinstance(output,dict) or set(output)!={'symbols'}:raise Review('INVALID_SCREENING_SCHEMA')
    values=output['symbols']
    if not isinstance(values,list) or len(values)!=2 or any(not isinstance(v,str) for v in values) or len(set(values))!=2:raise Review('INVALID_SCREENING_COUNT')
    if any(not re.fullmatch(r'[A-Z0-9]{2,18}USDT',v) or v not in catalog for v in values):raise Review('INVALID_SCREENING_SYMBOL')
    return values

def setup(output,expected):
    if not isinstance(output,dict) or set(output)!=set(SETUP_SCHEMA['required']):raise Review('INVALID_SETUP_SCHEMA')
    if output['symbol']!=expected or output['side'] not in ('LONG','SHORT'):raise Review('INVALID_SETUP_SYMBOL_SIDE')
    for k in ('position_size','limit_entry','take_profit','stop_loss','risk_reward'):
        if not isinstance(output[k],str):raise Review('INVALID_SETUP_SCHEMA')
    q,e,tp,sl,rr=[number(output[k]) for k in ('position_size','limit_entry','take_profit','stop_loss','risk_reward')]
    if e==sl or rr!=abs(tp-e)/abs(e-sl) or rr<2:raise Review('REJECT_RISK_REWARD')
    return Signal(expected,output['side'],e,tp,sl,q)

class NeuroAPI:
    def __init__(self,ledger,key=None,transport=request,sleep=time.sleep):
        self.db=ledger.db;self._key=api_key() if key is None else key;self.transport=transport;self.sleep=sleep
        self.db.execute('CREATE TABLE IF NOT EXISTS api_requests(operation TEXT PRIMARY KEY, idempotency TEXT, body_hash TEXT, state TEXT, created REAL, attempts INTEGER, output TEXT)')
    def require_key(self):
        if not self._key:raise Review('NEUROAPI_NOT_CONFIGURED')
    def _headers(self,json_body=False):
        headers={'X-API-Key':self._key,'Accept':'application/json','User-Agent':'harun-office/1.0'}
        if json_body:headers['Content-Type']='application/json'
        return headers
    def health(self):
        self.require_key()
        try:
            code,_,data=self.transport('GET',BASE+'/health',self._headers(),timeout=15)
            if code!=200 or not isinstance(data,dict) or data.get('authenticated') is not True or data.get('status')!='healthy':raise Review('NEUROAPI_UNAVAILABLE')
            return 'NEUROAPI_CONNECTED'
        except Exception:raise Review('NEUROAPI_UNAVAILABLE') from None
    def ask(self,operation,prompt,schema,validate,context=None):
        self.require_key()
        body={'prompt':prompt,'mode':'smart','stream':False,'output_schema':schema}
        if context is not None:body['message_history']=[{'role':'user','content':json.dumps(context,separators=(',',':'))}]
        digest=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old=self.db.execute('SELECT * FROM api_requests WHERE operation=?',(operation,)).fetchone()
            if old:
                if old['body_hash']!=digest or old['state']!='COMPLETE':raise Review('NEUROAPI_REQUEST_NEEDS_REVIEW')
                value=json.loads(old['output']);validate(value);self.db.execute('COMMIT');return value
            # Retain the legacy nullable column without relying on provider replay.
            idem=None
            self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?,?,?,?)',(operation,idem,digest,'PENDING',time.time(),0,None))
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
        try:
            for attempt in range(3):
                self.db.execute('UPDATE api_requests SET attempts=attempts+1 WHERE operation=?',(operation,))
                code,headers,data=self.transport('POST',BASE+'/agent/ask',self._headers(json_body=True),body,timeout=90)
                if code==200:
                    if not isinstance(data,dict) or data.get('mode')!='smart' or data.get('answer') is not None or not isinstance(data.get('output'),dict):raise Review('INVALID_NEUROAPI_RESPONSE')
                    value=data['output'];validate(value) # Never persist raw prose, errors or key prefixes.
                    self.db.execute("UPDATE api_requests SET state='COMPLETE',output=? WHERE operation=?",(json.dumps(value),operation))
                    return value
                if code not in (429,503) or attempt==2:raise Review('NEUROAPI_REQUEST_FAILED')
                hinted=headers.get('retry-after','').strip()
                # Do not retry early when a present header cannot be interpreted safely.
                if hinted and not re.fullmatch(r'\d{1,6}',hinted):raise Review('NEUROAPI_RETRY_DEFERRED')
                delay=int(hinted) if hinted else 2**attempt
                if delay>30:raise Review('NEUROAPI_RETRY_DEFERRED')
                self.sleep(max(1,delay))
        except Exception:
            self.db.execute("UPDATE api_requests SET state='NEEDS_REVIEW' WHERE operation=?",(operation,))
            raise Review('NEUROAPI_REQUEST_NEEDS_REVIEW') from None
