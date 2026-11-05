"""Durable upload-preview material, explicitly not a WEB Send receipt.

Pure metadata validation is shared with the worker. Filesystem access uses only
the actor-selected existing archive; requests cannot choose a path.
"""
import csv
import io
import hashlib
import json
import math
import os
import re
import time

KIND = 'NATIVE_ANALYSIS_MATERIAL_PREPARED_V1'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def digest(value): return hashlib.sha256(canonical(value)).hexdigest()
def need(value):
    if not value: raise ValueError('NATIVE_ANALYSIS_PREPARED_MATERIAL_UNVERIFIED')


def is_analysis(payload):
    return (type(payload) is dict and type(payload.get('output_schema')) is dict
        and set(payload['output_schema'].get('required', [])) == {
            'symbol','side','limit_entry','take_profit','stop_loss','risk_reward'})


def prefix(payload):
    return 'analysis-prepared-' + digest({key: payload[key] for key in
        ('operation','request_sha256')}) + '-'


def validate_prepared(value, payload):
    need(is_analysis(payload) and type(value) is dict and value['schema_version'] == 1
         and value['kind'] == KIND and value['state'] == 'PREPARED_NOT_SUBMITTED'
         and value['send_attempted'] is False and type(value['original_request_matches_material']) is bool
         and all(re.fullmatch('[a-f0-9]{64}',value[key])for key in('context_sha256','material_payload_sha256'))
         and value['operation'] == payload['operation']
         and value['request_sha256'] == payload['request_sha256']
         and value['marker'] == payload['marker'] and value['symbol'] == payload['context']['symbol']
         and type(value['observed_at']) in (int,float) and math.isfinite(value['observed_at']) and value['observed_at'] > 0
         and value['closed_candles_per_timeframe'] == 200 and value['closed_candles_total'] == 400
         and value['running_candles_separate'] is True and value['preview_verified'] is True
         and type(value['attachment_names']) is list and len(value['attachment_names']) == 4
         and len(set(value['attachment_names'])) == 4)
    if value['original_request_matches_material']:
        need(value['material_payload_sha256'] == digest(payload)
             and value['context_sha256'] == digest(payload['context']))
    charts, files = value['chart_evidence'], value['csv_receipts']
    need(len(charts) == len(files) == 2)
    for index, tf in enumerate(('15m','1h')):
        chart, file = charts[index], files[index]
        need(chart['symbol'] == file['symbol'] == value['symbol']
             and chart['timeframe'] == file['timeframe'] == tf
             and chart['source'] == 'BINANCE_USDM_FUTURES_WEBSITE_SCREENSHOT'
             and re.fullmatch('[a-f0-9]{64}',chart['sha256'])
             and type(chart['attachment_id']) is str and chart['attachment_id']
             and value['attachment_names'][index] == value['marker']+'-'+tf+'-'+chart['sha256'][:12]+'.png'
             and value['attachment_names'][index+2] == file['name']
             and file['mimeType'] == 'text/csv' and file['closed_count'] == 200 and file['timestamp_unit'] == 'UNIX_MILLISECONDS_UTC'
             and file['name'] == value['symbol']+'_'+tf+'_200_closed_'+file['sha256'][:8]+'.csv'
             and all(re.fullmatch('[a-f0-9]{64}',file[key]) for key in
                     ('sha256','series_sha256','source_candles_sha256'))
             and type(file['bytes']) is int and file['bytes'] > 0)
    need(re.fullmatch('[a-f0-9]{64}',value['rendered_prompt_sha256']))
    return value


def retain_prepared(root, payload, charts, names, files, rendered_prompt, *, observed_at=None):
    from .native_chart_followup import _directory, _private_file
    # CSV bytes are those actually passed to the native file chooser.
    receipts=[]
    for item in files:
        raw=item['buffer']; need(type(raw) is bytes and hashlib.sha256(raw).hexdigest() == item['sha256'])
        rows=list(csv.reader(io.StringIO(raw.decode('utf-8'))))
        need(len(rows)==201 and rows[0]==['open_time_utc_ms','open','high','low','close','volume','close_time_utc_ms']
             and all(len(row)==7 for row in rows[1:]))
        receipts.append({**{k:v for k,v in item.items() if k != 'buffer'}, 'bytes':len(raw)})
    body={k:v for k,v in payload.items() if k != 'request_sha256'}
    request_hash=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),
        ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    value=dict(schema_version=1,kind=KIND,state='PREPARED_NOT_SUBMITTED',send_attempted=False,
        operation=payload['operation'],request_sha256=payload['request_sha256'],marker=payload['marker'],
        symbol=payload['context']['symbol'],material_payload_sha256=digest(payload),
        context_sha256=digest(payload['context']),original_request_matches_material=request_hash==payload['request_sha256'],
        rendered_prompt_sha256=hashlib.sha256(rendered_prompt.encode()).hexdigest(),
        observed_at=time.time() if observed_at is None else observed_at,
        chart_evidence=charts,attachment_names=names,csv_receipts=receipts,
        closed_candles_per_timeframe=200,closed_candles_total=400,running_candles_separate=True,preview_verified=True)
    validate_prepared(value,payload);raw=canonical(value);need(len(raw)<=262144)
    name=prefix(payload)+digest(value)+'.json';directory=_directory(root)
    temporary='.analysis-prepared-'+os.urandom(16).hex()+'.tmp'
    try:
        fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600,dir_fd=directory)
        with os.fdopen(fd,'wb')as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        try:os.link(temporary,name,src_dir_fd=directory,dst_dir_fd=directory,follow_symlinks=False);os.fsync(directory)
        except FileExistsError:pass
        os.unlink(temporary,dir_fd=directory);need(_private_file(directory,name,262144)==raw)
        return dict(name=name,sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))
    finally:
        try:os.unlink(temporary,dir_fd=directory)
        except FileNotFoundError:pass
        os.close(directory)


def read_prepared(root,payload):
    from .native_chart_followup import _directory, _private_file, pairs
    directory=_directory(root);result=[];total=0
    try:
        names=sorted(name for name in os.listdir(directory)if re.fullmatch(re.escape(prefix(payload))+r'[a-f0-9]{64}\.json',name))
        for name in names:
            raw=_private_file(directory,name,262144);total+=len(raw);need(total<=4*1024*1024)
            value=json.loads(raw,object_pairs_hook=pairs);validate_prepared(value,payload)
            result.append(dict(name=name,sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw),value=value))
        return result
    finally:os.close(directory)
