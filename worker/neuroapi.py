"""Official NeuroAPI, smart/non-streaming structured output. Never broker execution."""
from dataclasses import dataclass
import hashlib
import json
import os
import re
import time
from pathlib import Path
from .core import Review,number,Signal,D
from .diagnostics import safe_code,validation_code
from .http_client import request

BASE='https://api.neurobro.ai/api/v1'
SYMBOL_PATTERN=r'^[A-Z0-9]{2,18}USDT$'
SCREEN_SCHEMA={'type':'object','properties':{'symbols':{'type':'array','description':'Exactly two distinct active Binance USD-M Futures trading symbols.','items':{'type':'string','pattern':SYMBOL_PATTERN,'description':'Exact uppercase Binance USD-M Futures trading symbol ending in USDT. BTCUSDT is a format example only, not a coin recommendation.'},'minItems':2,'maxItems':2,'uniqueItems':True}},'required':['symbols'],'additionalProperties':False}
# One-slot research uses a separate exact contract; legacy two-slot stays unchanged.
import copy
SCREEN_ONE_SCHEMA=copy.deepcopy(SCREEN_SCHEMA)
SCREEN_ONE_SCHEMA['properties']['symbols'].update(minItems=1,maxItems=1,description='Exactly one active Binance USD-M Futures trading symbol.')

def screening_count(schema):
    if schema==SCREEN_SCHEMA:return 2
    if schema==SCREEN_ONE_SCHEMA:return 1
    return None

NUM={'type':'number','exclusiveMinimum':0}
PRICE={**NUM,'type':['number','null'],'description':'Price must conform exactly to Binance tickSize and applicable price limits supplied in context; do not round after generation.'}
SETUP_SCHEMA={'type':'object','description':'LONG/SHORT require positive entry, TP, SL and declared reward/risk. HOLD requires all numeric fields null and creates no order.','properties':{'symbol':{'type':'string'},'side':{'type':'string','enum':['LONG','SHORT','HOLD']},
 'position_size':{**NUM,'type':['number','null'],'description':'Informational base-asset quantity only; worker Risk Manager independently computes execution quantity. Null for HOLD.'},
 'limit_entry':PRICE,'take_profit':PRICE,'stop_loss':PRICE,
 'risk_reward':{**NUM,'type':['number','null'],'description':'Reward divided by risk; 2 means risk:reward 1:2'}},
 'required':['symbol','side','position_size','limit_entry','take_profit','stop_loss','risk_reward'],'additionalProperties':False}

def api_key():
    try:
        file=os.environ.get('NEUROBRO_API_KEY_FILE')
        key=Path(file).read_text().strip() if file else os.environ.get('NEUROBRO_API_KEY','').strip()
        if not key or len(key)>512 or not key.isascii() or any(c.isspace() for c in key):return ''
        return key
    except OSError:return ''

def selections(output,catalog,count=2):
    if type(count) is not int or count not in (1,2):raise Review("INVALID_SCREENING_COUNT")
    if not isinstance(output,dict) or set(output)!={'symbols'}:raise Review('INVALID_SCREENING_SCHEMA')
    values=output['symbols']
    if not isinstance(values,list) or len(values)!=count or any(not isinstance(v,str) for v in values) or len(set(values))!=count:raise Review('INVALID_SCREENING_COUNT')
    if any(not re.fullmatch(SYMBOL_PATTERN,v) or v not in catalog for v in values):raise Review('INVALID_SCREENING_SYMBOL')
    return values

NUMERIC_FIELDS=('position_size','limit_entry','take_profit','stop_loss','risk_reward')
def setup_number(value):
    # Real transport parses JSON decimals directly into Decimal, never binary float.
    if isinstance(value,bool) or not isinstance(value,(int,D)):raise Review('INVALID_NUMERIC_TYPE')
    value=D(value)
    if not value.is_finite() or value<=0 or abs(value.adjusted())>63:raise Review('INVALID_NUMERIC_VALUE')
    try:return number(format(value,'f'))
    except Review:raise Review('INVALID_NUMERIC_VALUE') from None

@dataclass(frozen=True)
class Hold:
    symbol: str
    side: str = 'HOLD'

def setup(output,expected):
    if not isinstance(output,dict) or set(output)!=set(SETUP_SCHEMA['required']):raise Review('INVALID_SETUP_SCHEMA')
    if output['symbol']!=expected or not isinstance(expected,str) or not re.fullmatch(r'[A-Z0-9]{2,18}USDT',expected) or output['side'] not in ('LONG','SHORT','HOLD'):raise Review('INVALID_SETUP_SYMBOL_SIDE')
    if output['side']=='HOLD':
        if any(output[k] is not None for k in NUMERIC_FIELDS):raise Review('INVALID_SETUP_SCHEMA')
        return Hold(expected)
    q=setup_number(output['position_size']) if output['position_size'] is not None else None
    e,tp,sl,rr=[setup_number(output[k]) for k in NUMERIC_FIELDS[1:]]
    if not (sl<e<tp if output['side']=='LONG' else tp<e<sl):raise Review('INVALID_ENTRY_TP_SL')
    # Compare distances exactly; no rounding/equality assumption for declared RR.
    if abs(tp-e)<2*abs(e-sl):raise Review('RISK_REWARD_BELOW_2')
    return Signal(expected,output['side'],e,tp,sl,q)

def canonical_output(value,schema,catalog=None):
    # Cache only reconstructed validated fields, never provider envelope/prose.
    if screening_count(schema) is not None:
        if not isinstance(catalog,dict) or not catalog:raise Review('SCREENING_CATALOG_REQUIRED')
        symbols=selections(value,catalog,screening_count(schema))
        return json.dumps({'symbols':list(symbols)})
    if schema==SETUP_SCHEMA:
        signal=setup(value,value.get('symbol'))
        fields={'symbol':signal.symbol,'side':signal.side,**{k:setup_number(value[k]) if value[k] is not None else None for k in NUMERIC_FIELDS}}
        return '{'+','.join(json.dumps(k)+':'+(format(v,'f') if isinstance(v,D) else json.dumps(v)) for k,v in fields.items())+'}'
    raise Review('INVALID_OUTPUT_SCHEMA')

class NeuroAPI:
    def __init__(self,ledger,key=None,transport=request,sleep=time.sleep):
        self.db=ledger.db;self._key=api_key() if key is None else key;self.transport=transport;self.sleep=sleep
        self.db.execute('CREATE TABLE IF NOT EXISTS api_requests(operation TEXT PRIMARY KEY, idempotency TEXT, body_hash TEXT, state TEXT, created REAL, attempts INTEGER, output TEXT)')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if 'failure_code' not in {r[1] for r in self.db.execute('PRAGMA table_info(api_requests)')}:
                self.db.execute('ALTER TABLE api_requests ADD COLUMN failure_code TEXT')
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
    def record_validation(self,operation,error):
        self.db.execute("UPDATE api_requests SET failure_code=? WHERE operation=? AND state='COMPLETE'",(validation_code(error),operation))

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
    def ask(self,operation,prompt,schema,validate,context=None,*,catalog=None):
        self.require_key()
        if screening_count(schema) is not None and (not isinstance(catalog,dict) or not catalog):raise Review('SCREENING_CATALOG_REQUIRED')
        body={'prompt':prompt,'mode':'smart','stream':False,'output_schema':schema}
        if context is not None:body['message_history']=[{'role':'user','content':json.dumps(context,separators=(',',':'))}]
        digest=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old=self.db.execute('SELECT * FROM api_requests WHERE operation=?',(operation,)).fetchone()
            if old:
                if old['body_hash']!=digest or old['state']!='COMPLETE':raise Review('NEUROAPI_REQUEST_NEEDS_REVIEW')
                value=json.loads(old['output'],parse_float=D);validate(value);canonical_output(value,schema,catalog);self.db.execute('COMMIT');return value
            # Retain the legacy nullable column without relying on provider replay.
            idem=None
            self.db.execute('INSERT INTO api_requests(operation,idempotency,body_hash,state,created,attempts,output) VALUES(?,?,?,?,?,?,?)',(operation,idem,digest,'PENDING',time.time(),0,None))
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
        failure='LOCAL_PROCESSING_FAILED'
        try:
            for attempt in range(3):
                self.db.execute('UPDATE api_requests SET attempts=attempts+1 WHERE operation=?',(operation,))
                failure='NETWORK_UNCERTAIN'
                code,headers,data=self.transport('POST',BASE+'/agent/ask',self._headers(json_body=True),body,timeout=90)
                failure='HTTP_'+str(code) if type(code) is int and 100<=code<=599 else 'INVALID_RESPONSE_ENVELOPE'
                if code==200:
                    failure='INVALID_RESPONSE_ENVELOPE'
                    if not isinstance(data,dict) or data.get('mode')!='smart' or 'answer' not in data or data['answer'] is not None:raise Review(failure)
                    failure='INVALID_OUTPUT_SCHEMA'
                    if not isinstance(data.get('output'),dict):raise Review(failure)
                    value=data['output'];failure='VALIDATION_REJECTED'
                    validate(value)
                    cached=canonical_output(value,schema,catalog)
                    failure='LOCAL_PROCESSING_FAILED'
                    self.db.execute("UPDATE api_requests SET state='COMPLETE',output=?,failure_code=NULL WHERE operation=?",(cached,operation))
                    return value
                if code not in (429,503) or attempt==2:raise Review('NEUROAPI_REQUEST_FAILED')
                hinted=headers.get('retry-after','').strip()
                # Do not retry early when a present header cannot be interpreted safely.
                if hinted and not re.fullmatch(r'\d{1,6}',hinted):raise Review('NEUROAPI_RETRY_DEFERRED')
                delay=int(hinted) if hinted else 2**attempt
                if delay>30:raise Review('NEUROAPI_RETRY_DEFERRED')
                self.sleep(max(1,delay))
        except Exception as exc:
            if failure=='VALIDATION_REJECTED':failure=validation_code(exc)
            elif failure=='NETWORK_UNCERTAIN' and isinstance(exc,Review) and str(exc)=='INVALID_RESPONSE_ENVELOPE':failure='INVALID_RESPONSE_ENVELOPE'
            failure=safe_code(failure)
            self.db.execute("UPDATE api_requests SET state='NEEDS_REVIEW',failure_code=? WHERE operation=?",(failure,operation))
            raise Review('NEUROAPI_REQUEST_NEEDS_REVIEW' if failure=='NETWORK_UNCERTAIN' else failure) from None
