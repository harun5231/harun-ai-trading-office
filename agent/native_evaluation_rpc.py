"""Authenticated READ-only projections inside the existing browser HTTP service.

The caller's path and filesystem authority are fixed by native release assembly.
This module never touches a page, dispatches the actor, sends a message, creates
an archive/index, or calls an exchange. Payloads cannot select filesystem paths.
"""
from __future__ import annotations

import base64
from datetime import datetime
import importlib
import json
import os
import re
from zoneinfo import ZoneInfo

from worker.trade_evaluation_evidence import need
from worker.trade_evaluation_archive import NativeHistoricalArchiveReader, _subdirectory
from .native_chart_followup import _directory, _private_file, pairs, sha, MAX_BYTES

PATH = '/v1/historical-evidence'
MAX_WIRE_BYTES = 24 * 1024 * 1024


def evaluation_chain(root, operation, request_sha256):
    """Actual private ReceiptStore files, including every unknown leaf."""
    need(type(operation) is str and re.fullmatch(r'post-trade-evaluation:[a-f0-9]{32}', operation)
        and type(request_sha256) is str and re.fullmatch('[a-f0-9]{64}', request_sha256),
        'EVALUATION_NATIVE_REQUEST_INVALID')
    key = sha(dict(operation=operation, request_sha256=request_sha256))
    parent = _directory(root);directory = None
    try:
        directory = _subdirectory(parent, 'dialogue-200-followup')
        pattern = re.compile(re.escape(key)+r'(?:\.([2-9][0-9]*|1[0-9]+))?\.(intent|complete)\.json')
        names={}
        for name in os.listdir(directory):
            if name.startswith(key):
                found=pattern.fullmatch(name)
                need(found is not None, 'EVALUATION_NATIVE_RECEIPT_CHAIN_INVALID')
                names[int(found.group(1) or 1),found.group(2)]=name
        need(names, 'EVALUATION_NATIVE_RECEIPT_NOT_FOUND')
        count=max(n for n,_ in names)
        need(set(names)=={(n,s) for n in range(1,count+1) for s in ('intent','complete')},
            'EVALUATION_NATIVE_RECEIPT_CHAIN_INCOMPLETE')
        retained=[];result=[]
        for n in range(1,count+1):
            row={}
            for stage in ('intent','complete'):
                raw=_private_file(directory,names[n,stage],MAX_BYTES)
                row[stage]=json.loads(raw,object_pairs_hook=pairs)
                retained.append((names[n,stage],raw))
            result.append(row)
        need({n for n in os.listdir(directory) if n.startswith(key)}==set(names.values())
            and all(_private_file(directory,n,MAX_BYTES)==raw for n,raw in retained),
            'EVALUATION_NATIVE_RECEIPT_CHANGED_DURING_READ')
        return result
    finally:
        if directory is not None:os.close(directory)
        os.close(parent)


def handle(value, *, archive_root='/profile/research', native_package='browser_runtime', now=None):
    """Internal token-authenticated handler; no browser/account mutation."""
    need(type(value) is dict and type(value.get('kind')) is str,
        'EVALUATION_NATIVE_REQUEST_INVALID')
    kind=value['kind']
    if kind=='BOUND_ROOM':
        need(set(value)=={'kind'}, 'EVALUATION_NATIVE_REQUEST_INVALID')
        from .native_daily_room import DailyRoomStore
        import time
        day=datetime.fromtimestamp(time.time() if now is None else now,ZoneInfo('Asia/Jakarta')).date().isoformat()
        # Ordinary SQLite read locks coordinate with the existing DailyRoom
        # writer. Opening the journal RO cannot initialize/recover/migrate it;
        # a hot journal or unavailable lock is an evidence-read failure.
        from .native_daily_room import JOURNAL_NAME
        import sqlite3, stat
        parent=_directory(archive_root);fd=None;db=None
        try:
            fd=os.open(JOURNAL_NAME,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            info=os.fstat(fd)
            need(stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid()
                and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==0o600,
                'EVALUATION_BOUND_ROOM_NOT_AVAILABLE')
            db=sqlite3.connect('file:/proc/self/fd/'+str(parent)+'/'+JOURNAL_NAME+'?mode=ro',
                uri=True,timeout=1,isolation_level=None)
            db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            row=db.execute('SELECT room_id,state FROM daily_rooms WHERE day=?',(day,)).fetchone()
            named=os.stat(JOURNAL_NAME,dir_fd=parent,follow_symlinks=False)
            need((info.st_dev,info.st_ino)==(named.st_dev,named.st_ino)
                and row is not None and row[1]=='BOUND','EVALUATION_BOUND_ROOM_NOT_AVAILABLE')
            return dict(kind=kind,room_id=row[0])
        finally:
            if db is not None:db.close()
            if fd is not None:os.close(fd)
            os.close(parent)
    if kind=='COMPLETION':
        need(set(value)=={'kind','operation','request_sha256'},'EVALUATION_NATIVE_REQUEST_INVALID')
        return dict(kind=kind,operation=value['operation'],request_sha256=value['request_sha256'],
            chain=evaluation_chain(archive_root,value['operation'],value['request_sha256']))
    need(kind=='ARCHIVE' and set(value)=={'kind','operation','request','response'},
        'EVALUATION_NATIVE_REQUEST_INVALID')
    request,response=value['request'],value['response']
    need(type(request) is dict and type(response) is dict and type(value['operation']) is str
        and request.get('operation')==response.get('operation')==value['operation'],
        'EVALUATION_NATIVE_REQUEST_INVALID')
    charts=importlib.import_module(native_package+'.charts')
    archive=NativeHistoricalArchiveReader(charts,archive_root)(
        value['operation'],request,response)
    files=[]
    for item in archive['files']:
        copied={k:v for k,v in item.items() if k!='buffer'}
        copied['buffer_base64']=base64.b64encode(item['buffer']).decode('ascii');files.append(copied)
    result={**archive,'files':files,'kind':kind}
    need(len(json.dumps(result,ensure_ascii=True).encode())<=MAX_WIRE_BYTES,
        'EVALUATION_NATIVE_RESPONSE_TOO_LARGE')
    return result
