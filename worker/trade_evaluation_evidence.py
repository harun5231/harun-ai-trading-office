"""Strict, observation-only joins for existing post-trade journal jobs.

No provider, account, scheduler or financial write is started here. The account
actor supplies its authenticated history cache and Coordinator. A native archive
reader must verify the original browser intent/completion custody before exposing
its projection below. Historical bytes are never replaced with current charts.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import math
import re

PROTOCOL = 'NEUROBRO_POST_TRADE_EVALUATION_V1'
MATERIAL_PROTOCOL = 'NATIVE_HISTORICAL_MATERIAL_CUSTODY_V1'
WEBSITE_SOURCE = 'BINANCE_USDM_FUTURES_WEBSITE_SCREENSHOT'
EVALUATION_SCHEMA = {
    'type':'object', 'additionalProperties':False,
    'required':['assessment','findings'],
    'properties':{'assessment':{'type':'string','enum':['CONSISTENT','INCONSISTENT','INCONCLUSIVE']},
                  'findings':{'type':'string','minLength':1,'maxLength':512000}},
    'authority':'NEUROBRO_POST_TRADE_ONLY_NO_ORDER_OR_STRATEGY_CHANGE',
    'protocol':PROTOCOL}


def need(ok, code):
    if not ok:raise ValueError(code)


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def digest(value):
    return hashlib.sha256(value if type(value)is bytes else canonical(value).encode()).hexdigest()


def utc_epoch(value):
    need(type(value)is str,'EVALUATION_TIME_INVALID')
    at=datetime.fromisoformat(value.replace('Z','+00:00'))
    need(at.tzinfo is not None and at.utcoffset()==timezone.utc.utcoffset(at),'EVALUATION_TIME_INVALID')
    delta=at-datetime(1970,1,1,tzinfo=timezone.utc)
    return Decimal(delta.days*86400+delta.seconds)+Decimal(delta.microseconds)/1000000


def utc_ms(value):
    milliseconds=utc_epoch(value)*1000
    need(milliseconds==milliseconds.to_integral_value(),'EVALUATION_TIME_INVALID')
    return int(milliseconds)


def financial_receipt(value):
    # Observation/retry timestamps cannot invalidate an already completed
    # evaluation; changes to ownership, fills or economics still do.
    fields=('source','state','symbol','client_order_id','order_id','filled_quantity',
        'first_fill_at','exit_order_id','closed_at','closure_source','closure_receipt')
    return {key:deepcopy(value[key])for key in fields if key in value}


def _source_rows(coordinator, operation):
    db=coordinator.db
    request=db.execute('SELECT * FROM research_browser_requests WHERE operation=?',(operation,)).fetchone()
    response=db.execute('SELECT * FROM research_browser_responses WHERE operation=?',(operation,)).fetchone()
    journal=db.execute('SELECT * FROM api_requests WHERE operation=?',(operation,)).fetchone()
    need(request is not None and response is not None and journal is not None
        and journal['state']=='COMPLETE','ORIGINAL_NEUROBRO_DECISION_NOT_LINKED')
    return dict(request),dict(response),dict(journal)


def _owned_fills(closed, intent, result, history):
    from . import pnl_calendar as pnl
    need(type(history)is dict and type(history.get('identity'))is str,'AUTHENTICATED_HISTORY_CACHE_REQUIRED')
    state=pnl._validate(deepcopy(history),history['identity'])
    end=max([utc_ms(closed['closed_at'])]+[r['end']for r in state['anchors'].values()])
    need(closed in pnl.calendar(state,end)['positions'],'EVALUATION_LIFECYCLE_HISTORY_MISMATCH')
    start,finish=utc_ms(closed['opened_at']),utc_ms(closed['closed_at'])
    need(pnl._covers(state['trade_coverage'].get(intent['symbol'],[]),start,finish),
        'EVALUATION_FILL_HISTORY_INCOMPLETE')
    rows=sorted((r for r in state['fills'].values()if r['symbol']==intent['symbol']
        and r['positionSide']==intent['position_side']and start<=r['time']<=finish),key=lambda r:(r['time'],r['id']))
    entry=[r for r in rows if r['side']==intent['entry']['side']]
    exits=[r for r in rows if r['side']==intent['protection']['exit_side']]
    allowed_exits={result['exit_order_id']}
    if result.get('closure_source')=='EXTERNAL_REDUCE_ONLY':
        proof=result.get('closure_receipt')
        need(type(proof)is dict,'EVALUATION_EXTERNAL_RECEIPT_REQUIRED')
        allowed_exits={str(r['orderId'])for r in proof['exit_trades']}
        # The saved native closure contains the exact signed economics, not an
        # attribution inferred from symbol/time or a single terminal order ID.
        normalize=lambda rows:sorted([pnl._fill(r)for r in rows],key=lambda r:r['id'])
        need(normalize(entry)==normalize(proof['entry_trades'])
            and normalize(exits)==normalize(proof['exit_trades']), 'EVALUATION_EXTERNAL_RECEIPT_MISMATCH')
    need(entry and exits and len(entry)+len(exits)==len(rows)
        and all(str(r['orderId'])==result['order_id']for r in entry)
        and all(str(r['orderId'])in allowed_exits for r in exits), 'EVALUATION_FOREIGN_OR_AMBIGUOUS_OWNER')
    quantity=Decimal(result['filled_quantity'])
    need(sum((Fraction(r['qty'])for r in entry),Fraction(0))==Fraction(quantity)
        ==sum((Fraction(r['qty'])for r in exits),Fraction(0))==Fraction(closed['closed_quantity']),
        'EVALUATION_FILL_QUANTITY_MISMATCH')
    last=max(exits,key=lambda r:(r['time'],r['id']))
    identity='{}:{}:{}:{}'.format(intent['symbol'],intent['position_side'],last['time'],last['id'])
    need(hashlib.sha256(identity.encode()).hexdigest()[:24]==closed['id']
        and entry[0]['time']==start and last['time']==finish
        and str(last['orderId'])==result['exit_order_id'], 'EVALUATION_LIFECYCLE_ID_MISMATCH')
    return rows


def _materials(operation, request, response, loader):
    need(callable(loader),'HISTORICAL_ARCHIVE_READER_REQUIRED')
    # The trusted native adapter must validate its complete archived custody;
    # booleans, user JSON, file paths or a current chart cannot supply authority.
    value=loader(operation,deepcopy(request),deepcopy(response))
    need(type(value)is dict and value.get('protocol')==MATERIAL_PROTOCOL,
        'HISTORICAL_CHART_ARCHIVE_NOT_LINKED')
    witness=value.get('delivery')
    need(type(witness)is dict and set(witness)=={'operation','request_sha256','response_id','assistant_sha256',
        'native_intent_sha256','native_completion_sha256','context_sha256','png_evidence_sha256','csv_sha256'}
        and witness.get('operation')==operation
        and witness.get('request_sha256')==request['request_sha256']
        and witness.get('response_id')==response['response_id']
        and witness.get('assistant_sha256')==response['assistant_sha256']
        and all(type(witness.get(k))is str and re.fullmatch('[a-f0-9]{64}',witness[k])
            for k in ('native_intent_sha256','native_completion_sha256')),
        'EVALUATION_ARCHIVE_CUSTODY_MISMATCH')
    need(type(value.get('sidecar_sha256'))is str and re.fullmatch('[a-f0-9]{64}',value['sidecar_sha256']),
        'EVALUATION_NATIVE_SIDECAR_REQUIRED')
    context=value.get('context')
    need(type(context)is dict and witness.get('context_sha256')==digest(context),
        'EVALUATION_HISTORICAL_CONTEXT_MISMATCH')
    at=context.get('fetched_at')
    need(type(at)in(int,float)and math.isfinite(at),'EVALUATION_HISTORICAL_CONTEXT_MISMATCH')
    from agent.native_data_files import deliver_market_data_files
    csvs=deliver_market_data_files(context,now=at)
    expected_csv={r['timeframe']:r for r in csvs}
    charts=json.loads(response['chart_evidence'])
    need(type(charts)is list and len(charts)==2,'EVALUATION_HISTORICAL_PNG_REQUIRED')
    expected_png={r['timeframe']:r for r in charts}
    files=value.get('files')
    need(type(files)is list and len(files)==4,'EVALUATION_HISTORICAL_FOUR_FILES_REQUIRED')
    keys={(r.get('kind'),r.get('timeframe'))for r in files if type(r)is dict}
    need(keys=={(kind,tf)for kind in('PNG','CSV')for tf in('15m','1h')},
        'EVALUATION_HISTORICAL_FOUR_FILES_REQUIRED')
    manifest=[]
    for item in files:
        kind,tf=item['kind'],item['timeframe'];raw=item.get('buffer')
        need(type(raw)is bytes and 0<len(raw)<=8_000_000 and type(item.get('name'))is str
            and 0<len(item['name'])<=240 and not any(c in item['name']for c in('/','\\','\x00')), 
            'EVALUATION_HISTORICAL_BYTES_REQUIRED')
        hashed=hashlib.sha256(raw).hexdigest()
        if kind=='CSV':
            original=expected_csv[tf]
            need(raw==original['buffer']and item['name']==original['name']
                and witness.get('csv_sha256',{}).get(tf)==hashed,
                'EVALUATION_HISTORICAL_CSV_MISMATCH')
            metadata=dict(kind=kind,timeframe=tf,name=item['name'],sha256=hashed,bytes=len(raw),closed_count=200)
        else:
            original=expected_png[tf]
            need(original.get('source')==WEBSITE_SOURCE and original.get('symbol')==context['symbol']
                and original.get('sha256')==hashed and original.get('bytes')==len(raw)
                and raw.startswith(b'\x89PNG\r\n\x1a\n'), 'EVALUATION_HISTORICAL_PNG_MISMATCH')
            captured=utc_epoch(original['captured_at'])
            need(at<=captured<=response['received_at'],'EVALUATION_HISTORICAL_CAPTURE_TIME_MISMATCH')
            metadata=dict(kind=kind,timeframe=tf,name=item['name'],sha256=hashed,bytes=len(raw),
                captured_at=original['captured_at'],source=WEBSITE_SOURCE)
        manifest.append(metadata)
    need(witness.get('png_evidence_sha256')==digest(charts),'EVALUATION_ARCHIVE_CUSTODY_MISMATCH')
    return dict(files=sorted(manifest,key=lambda r:(r['kind'],r['timeframe'])),
        observed_at=at,context_sha256=digest(context),delivery_custody=deepcopy(witness),
        archive_sidecar_sha256=value.get('sidecar_sha256'))


def bind_closed_trade(coordinator, closed, *, history, archive_reader, source_reader=None):
    """Return one verified join, with no account/provider call or financial write.

    ``history`` is the authenticated cache already obtained by the account actor.
    ``archive_reader`` is a trusted internal native receipt/file adapter, not an
    HTTP callback or a caller-supplied claim that an upload succeeded.
    """
    need(type(closed)is dict and closed.get('status')=='CLOSED'and closed.get('complete')is True
        and type(closed.get('pnl_usdt'))is str, 'CLOSED_NET_PNL_INCOMPLETE')
    if source_reader is None:
        from .research_provider import source_evidence
        source_reader=source_evidence
    need(callable(source_reader),'ORIGINAL_NEUROBRO_READER_REQUIRED')
    matches=[]
    for row in coordinator.db.execute("SELECT * FROM order_intents WHERE state='CLOSED' AND symbol=?",(closed['symbol'],)):
        result=json.loads(row['result'])if row['result']else{}
        if (result.get('source')!='BINANCE_FUTURES'or result.get('state')!='CLOSED'
                or utc_ms(result['closed_at'])!=utc_ms(closed['closed_at'])):continue
        intent=coordinator.verified_intent(row)
        need(intent['side']==closed['side']and intent['position_side']==closed['position_side']
            and result.get('symbol')==intent['symbol']and result.get('client_order_id')==intent['client_order_id']
            and 0<Decimal(result['filled_quantity'])<=Decimal(intent['entry']['quantity']),
            'EVALUATION_FOREIGN_OR_AMBIGUOUS_OWNER')
        fills=_owned_fills(closed,intent,result,history)
        operation=row['candidate_id'];request,response,journal=_source_rows(coordinator,operation)
        source=source_reader(coordinator.db,operation)
        need(type(source)is dict and source.get('source_type')=='NEUROBRO_BROWSER'
            and type(source.get('provider_evidence_sha256'))is str
            and re.fullmatch('[a-f0-9]{64}',source['provider_evidence_sha256']),
            'ORIGINAL_NEUROBRO_DECISION_NOT_LINKED')
        body=json.loads(request['request_body']);decision=json.loads(journal['output'])
        need(body.get('operation')==operation and body.get('context',{}).get('symbol')==closed['symbol']
            and decision.get('symbol')==closed['symbol']and decision.get('side')==closed['side']
            and request['request_sha256']==journal['body_hash']
            and hashlib.sha256(response['assistant_text'].encode()).hexdigest()==response['assistant_sha256']
            and response['received_at']<=utc_epoch(row['created']),
            'EVALUATION_ORIGINAL_DECISION_MISMATCH')
        materials=_materials(operation,request,response,archive_reader)
        candidate=coordinator.db.execute('SELECT * FROM robot_candidates WHERE id=?',(operation,)).fetchone()
        plan=coordinator.store.verified_plan(candidate)
        proof=dict(version=PROTOCOL,trade_id=closed['id'],closed_position_sha256=digest(closed),
            intent_id=row['id'],client_order_id=intent['client_order_id'],entry_order_id=result['order_id'],
            exit_order_ids=sorted({str(r['orderId'])for r in fills if r['side']==intent['protection']['exit_side']}),
            financial_receipt_sha256=digest(financial_receipt(result)),fills_sha256=digest(fills),research_operation=operation,
            fills=deepcopy(fills),
            source_evidence={k:source[k]for k in('source_type','contract_version','provider_evidence_sha256')if k in source},original_decision=decision,original_reply=response['assistant_text'],
            original_reply_sha256=response['assistant_sha256'],materials=materials,
            execution=dict(entry_limit=plan['entry'],entry=closed['entry_price'],sl=plan['sl'],tp=plan['tp'],
                quantity=result['filled_quantity'],planned_quantity=plan['quantity'],exit_average=closed['exit_price'],
                closure_source=result.get('closure_source','NATIVE_CLOSED_RECEIPT_TRIGGER_NOT_IDENTIFIED'),
                original_neurobro_tp=plan.get('neurobro_tp',decision.get('take_profit'))),
            outcome={key:deepcopy(closed[key]) for key in ('opened_at','closed_at','pnl_usdt',
                'realized_pnl_usdt','commission_usdt','funding_usdt','insurance_usdt','calculation_method')
                if key in closed})
        # No long financial transaction spans archive access. Recheck the
        # immutable source/economics after that external internal-reader seam.
        current=coordinator.db.execute('SELECT * FROM order_intents WHERE id=?',(row['id'],)).fetchone()
        current_candidate=coordinator.db.execute('SELECT * FROM robot_candidates WHERE id=?',(operation,)).fetchone()
        need(current is not None and current['state']=='CLOSED'and current['payload']==row['payload']
            and financial_receipt(json.loads(current['result']))==financial_receipt(result)
            and current_candidate is not None and current_candidate['plan']==candidate['plan']
            and _source_rows(coordinator,operation)==(request,response,journal), 'EVALUATION_SOURCE_CHANGED_DURING_BINDING')
        proof['binding_sha256']=digest(proof);matches.append(proof)
    need(len(matches)==1,'EVALUATION_OWNER_NOT_UNIQUELY_LINKED')
    return matches[0]


def evaluation_request(job, bindings, room_id):
    """Natural Indonesian historical evaluation, distinct from a new setup."""
    need(type(room_id)is str and 0<len(room_id)<=256,'EVALUATION_BOUND_ROOM_REQUIRED')
    need(job.get('evidence_complete')is True and all(t in bindings for t in job['trade_ids']),
        'EVALUATION_EVIDENCE_INCOMPLETE')
    records=[bindings[t]for t in job['trade_ids']]
    from .trade_evaluation_metrics import actual_reward_risk
    evidence_sha=digest(dict(job_id=job['id'],trade_ids=job['trade_ids'],bindings=[r['binding_sha256']for r in records]))
    prompt=('Mari evaluasi transaksi historis ini dari bukti aslinya, bukan membuat setup atau order baru. '
        'Neurobro tetap analis utama; Hermes menyiapkan konteks dan mencatat evaluasi. '
        'Bedakan kualitas analisis awal, pelaksanaan entry/exit, biaya dan kondisi pasar. '
        'Jangan menilai keputusan lampau memakai chart terbaru seolah tersedia saat itu.\n\n')
    if job['id'].startswith('losing-streak:'):
        prompt+=('Pemicu faktual: rentetan kerugian bersih berturut-turut pada transaksi tertutup yang teramati. '
            'Ini pemicu evaluasi, bukan perubahan batas trading atau alasan otomatis menghentikan robot.\n')
    elif job['id'].startswith('performance-decline:'):
        prompt+=('Pemicu faktual: tiga penutupan terakhir memiliki lebih sedikit kemenangan dan rata-rata PNL bersih '
            'lebih rendah daripada tiga penutupan sebelumnya. Ini sampel kecil transaksi teramati, '
            'bukan bukti sebab, perubahan strategi atau keseluruhan kinerja akun.\n')
    for item in records:
        original=item['original_decision'];executed=item['execution'];outcome=item['outcome']
        prompt+=(f"Transaksi {item['trade_id']}: {original['symbol']} {original['side']}. "
            f"Setup asli Neurobro ENTRY {original.get('limit_entry')}, SL {original.get('stop_loss')}, "
            f"TP rekomendasi {original.get('take_profit')}. Eksekusi tercatat ENTRY {executed['entry']}, "
            f"SL {executed['sl']}, TP {executed['tp']}, quantity terisi {executed['quantity']} "
            f"dari rencana {executed['planned_quantity']}; harga exit rata-rata {executed['exit_average']}. "
            + ('Penutupan terbukti melalui reduce-only eksternal; jangan menyebutnya SL atau TP robot yang terpicu. '
               if executed['closure_source']=='EXTERNAL_REDUCE_ONLY'else
               'Gateway mengonfirmasi penutupan, tetapi receipt ini tidak menetapkan apakah pemicunya SL atau TP. ')
            + 
            f"PNL bersih tercatat {outcome['pnl_usdt']} USDT; biaya trading sudah diperhitungkan sekali. "
            f"R terealisasi bersih terhadap jarak SL awal dan quantity yang benar-benar terisi: "
            f"{actual_reward_risk(item) or 'belum diketahui'}. "
            f"Realized PNL sebelum biaya {outcome.get('realized_pnl_usdt', 'belum diketahui')}; "
            f"komisi {outcome.get('commission_usdt', 'belum diketahui')}; "
            f"funding bertanda {outcome.get('funding_usdt', 'belum diketahui')}; "
            f"insurance bertanda {outcome.get('insurance_usdt', 'belum diketahui')} USDT. "
            'Jangan kurangi komisi atau tambahkan funding lagi dari PNL bersih tersebut. '
            f"Bahan historis diambil {datetime.fromtimestamp(item['materials']['observed_at'],timezone.utc).isoformat()} UTC.\n")
        if item.get('fills') is not None:
            need(digest(item['fills'])==item['fills_sha256'],'EVALUATION_FILL_HISTORY_MISMATCH')
            prompt+='Fills riil dari cache history terautentikasi: '+canonical(item['fills'])+'\n'
    prompt+=('\nTolong jelaskan temuan yang benar-benar didukung bahan, keterbatasan bukti, dan hipotesis '
        'pengujian yang layak bila ada. Pisahkan pelajaran yang didukung bukti dari hipotesis; '
        'status pengujian tetap NOT_RUN sampai ada hasil pengujian nyata. '
        'Biaya operasional kantor dan net sesudah biaya itu belum terikat bukti di pekerjaan ini; jangan anggap nol '
        'atau kurangi pembayaran/top-up prepaid sebagai biaya kedua. '
        'Jangan mengklaim eksperimen atau peningkatan win rate sudah terjadi. '
        'Jika bukti tambahan memang tidak tersedia, nyatakan keterbatasannya dan selesaikan sebagai '
        'BELUM CUKUP BUKTI; jangan mengulang diskusi tanpa pertanyaan yang bisa dijawab oleh bahan terikat. '
        'Akhiri PENILAIAN SETUP: SESUAI BUKTI, TIDAK SESUAI BUKTI, atau BELUM CUKUP BUKTI. '
        'Ini penilaian historis, bukan LONG/SHORT/HOLD untuk transaksi baru.')
    operation='post-trade-evaluation:'+evidence_sha[:32]
    return dict(protocol=PROTOCOL,operation=operation,phase='POST_TRADE_EVALUATION',room_id=room_id,
        job_id=job['id'],input_evidence_sha256=evidence_sha,prompt=prompt,output_schema=deepcopy(EVALUATION_SCHEMA),
        historical_materials=[deepcopy(r['materials'])for r in records],
        source_neurobro_replies=[dict(trade_id=r['trade_id'],text=r['original_reply'],
            sha256=r['original_reply_sha256'])for r in records],trading_authority=False)


def parse_evaluation_reply(text):
    need(type(text)is str and 0<len(text.encode())<=512000,'EVALUATION_REPLY_INVALID')
    clean=text.replace('**','').replace('`','')
    found=re.findall(r'(?im)^\s*PENILAIAN(?:\s+SETUP)?\s*:\s*'
        r'(TIDAK SESUAI BUKTI|BELUM CUKUP BUKTI|SESUAI BUKTI)\s*[.!]?\s*$',clean)
    need(len(found)==1,'EVALUATION_REPLY_NEEDS_CLARIFICATION')
    assessment={'SESUAI BUKTI':'CONSISTENT','TIDAK SESUAI BUKTI':'INCONSISTENT',
        'BELUM CUKUP BUKTI':'INCONCLUSIVE'}[found[0].upper()]
    return dict(assessment=assessment,findings=text)


def project_native_archive(charts, archive_root, request, response, *, paths, delivery_reader):
    """Read an EXISTING native website archive and verify its historical bytes.

    Run in the existing native/browser filesystem namespace. ``paths`` must
    come from the owned capture/receipt lookup. The caller's trusted
    ``delivery_reader`` revalidates the original intent/completion and actual
    four-file upload (including follow-up chains) and projects MATERIAL_PROTOCOL
    delivery fields; a reply or Office JSON is never such a reader.

    This does not search profiles, capture, navigate, send, create a receipt, or
    alter live setup freshness. The existing capture validator is evaluated at
    its recorded capture instant, specifically for historical evaluation only.
    """
    import os
    from pathlib import Path
    from agent.native_chart_followup import (_directory, _private_file, pairs,
        canonical as native_canonical, sha as native_sha, verify_website_capture, MAX_BYTES)
    from agent.native_chart_capture import WEBSITE_SIDECAR
    from agent.native_market_context import convert_native_market_context
    need(callable(delivery_reader),'NATIVE_DELIVERY_CUSTODY_READER_REQUIRED')
    need(type(paths)is list and len(paths)==2,'EVALUATION_HISTORICAL_PNG_REQUIRED')
    root=Path(archive_root);selected=[Path(p)for p in paths]
    need(root.is_absolute()and '..'not in root.parts and all(p.is_absolute()and '..'not in p.parts for p in selected)
        and selected[0]!=selected[1]and selected[0].parent==selected[1].parent
        and root in selected[0].parents,'EVALUATION_ARCHIVE_PATH_INVALID')
    directory=_directory(root)
    try:
        import stat
        for part in selected[0].parent.relative_to(root).parts:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            os.close(directory);directory=child;info=os.fstat(directory)
            need(info.st_uid==os.geteuid()and stat.S_IMODE(info.st_mode)==0o700,
                'EVALUATION_ARCHIVE_NOT_PRIVATE')
        raw_sidecar=_private_file(directory,WEBSITE_SIDECAR,MAX_BYTES)
        sidecar=json.loads(raw_sidecar,object_pairs_hook=pairs)
        payload=json.loads(request['request_body']);payload['request_sha256']=request['request_sha256']
        context=deepcopy(payload['context']);public=sidecar['public_market_context']
        if sidecar.get('native_context_canonical_sha256')!=native_sha(context):
            # ActorCharts refreshes only these facts; extras such as the owned
            # order/risk context are retained from the immutable original body.
            frames={}
            for tf in('15m','1h'):
                rows=deepcopy(public['candles'][tf])+[deepcopy(public['running_candles'][tf])]
                frames[tf]=dict(symbol=public['symbol'],timeframe=tf,candles=rows)
            context.update(source='Binance Futures',symbol=public['symbol'],fetched_at=public['observed_at'],
                server_time=public['server_time_ms'],mark_price=dict(price=public['mark_price'],time=public['mark_time_ms']),
                quantity_unit='base_asset',timeframes=frames)
        need(sidecar.get('native_context_canonical_sha256')==native_sha(context)
            and convert_native_market_context(context,now=context['fetched_at'])==public,
            'EVALUATION_HISTORICAL_CONTEXT_MISMATCH')
        original_charts=json.loads(response['chart_evidence'])
        fields=('source','symbol','timeframe','captured_at','sha256','bytes')
        receipts=[{key:r[key]for key in fields}for r in original_charts]
        need([r['timeframe']for r in receipts]==['15m','1h'],'EVALUATION_HISTORICAL_PNG_REQUIRED')
        capture_at=max(float(utc_epoch(r['captured_at']))for r in receipts)
        result=dict(paths=list(paths),evidence=receipts)
        verified=verify_website_capture(charts,result,context,payload,archive_root=root,
            now=capture_at,gate=lambda:True)
        need(verified==result and _private_file(directory,WEBSITE_SIDECAR,MAX_BYTES)==raw_sidecar,
            'EVALUATION_ARCHIVE_CHANGED_DURING_READ')
        csvs=deliver_market_data_files_for_archive(context)
        delivery=delivery_reader(payload['operation'],deepcopy(request),deepcopy(response),
            hashlib.sha256(raw_sidecar).hexdigest(),deepcopy(csvs))
        # The worker-side join below independently checks this exact native
        # reader projection against its immutable source request/response.
        files=[]
        for path,receipt in zip(selected,receipts):
            raw=_private_file(directory,path.name,charts.MAX_PNG)
            need(hashlib.sha256(raw).hexdigest()==receipt['sha256']and len(raw)==receipt['bytes'],
                'EVALUATION_HISTORICAL_PNG_MISMATCH')
            files.append(dict(kind='PNG',timeframe=receipt['timeframe'],name=path.name,buffer=raw))
        files.extend(dict(kind='CSV',timeframe=r['timeframe'],name=r['name'],buffer=r['buffer'])for r in csvs)
        return dict(protocol=MATERIAL_PROTOCOL,context=context,files=files,delivery=delivery,
            sidecar_sha256=hashlib.sha256(raw_sidecar).hexdigest())
    finally:os.close(directory)


def deliver_market_data_files_for_archive(context):
    from agent.native_data_files import deliver_market_data_files
    return deliver_market_data_files(context,now=context['fetched_at'])
