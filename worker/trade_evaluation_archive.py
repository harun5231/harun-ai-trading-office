"""Read historical material custody from the existing native private archive.

This module never captures charts, contacts a provider, or creates receipts.
It accepts only the actor's owned capture lookup and verifies historical
bytes with the existing website-capture validator. There is intentionally no scan of Chromium/profile roots
and no fallback from a missing four-file receipt to a claim of upload success.
Initial turns which lack the existing follow-up custody remain unbound.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .trade_evaluation_evidence import digest, need, project_native_archive

CAPTURE_INDEX = 'historical-capture-index'
CAPTURE_INDEX_PROTOCOL = 'NATIVE_OWNED_HISTORICAL_CAPTURE_LOOKUP_V1'
_PNG_FIELDS = ('source', 'symbol', 'timeframe', 'captured_at', 'sha256', 'bytes')


def _index_identity(payload, evidence):
    from agent.native_chart_followup import sha
    core = [{k: r[k] for k in _PNG_FIELDS} for r in evidence]
    need([r['timeframe'] for r in core] == ['15m', '1h'],
        'EVALUATION_HISTORICAL_PNG_REQUIRED')
    identity = dict(marker=payload['marker'], request_sha256=payload['request_sha256'], evidence=core)
    need(type(identity['marker']) is str and identity['marker'] and
        type(identity['request_sha256']) is str and
        re.fullmatch(r'[a-f0-9]{64}', identity['request_sha256']) is not None,
        'EVALUATION_ARCHIVE_REQUEST_MISMATCH')
    return identity, sha(identity) + '.json'


def _subdirectory(parent, name, *, create=False):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    info = os.fstat(directory)
    if not (stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and
            stat.S_IMODE(info.st_mode) == 0o700):
        os.close(directory)
        raise ValueError('EVALUATION_ARCHIVE_NOT_PRIVATE')
    return directory


def record_capture_lookup(archive_root, result, payload):
    """Index an existing verified capture; this is not a delivery receipt.

    Called only by the admitted native capture path after its website sidecar
    is durably written. Capture has marker/hash/context, not an operation ABI.
    The reader later binds those facts to its immutable native request row.
    Identical repeated records are harmless; changed records are not replaced.
    """
    from agent.native_chart_followup import _directory, _private_file, pairs, canonical, sha, MAX_BYTES
    from agent.native_chart_capture import WEBSITE_SIDECAR
    from agent.binance_website_chart import SOURCE
    need(type(result) is dict and set(result) == {'paths', 'evidence'} and
        type(result['paths']) is list and len(result['paths']) == 2 and
        type(result['evidence']) is list and len(result['evidence']) == 2,
        'EVALUATION_OWNED_CAPTURE_NOT_FOUND')
    root = Path(archive_root)
    paths = [Path(p) for p in result['paths']]
    need(root.is_absolute() and '..' not in root.parts and
        all(p.is_absolute() and '..' not in p.parts and root in p.parents for p in paths) and
        paths[0] != paths[1] and paths[0].parent == paths[1].parent,
        'EVALUATION_ARCHIVE_PATH_INVALID')
    identity, name = _index_identity(payload, result['evidence'])
    parent = _directory(root)
    directory = os.dup(parent)
    index = None
    try:
        for component in paths[0].parent.relative_to(root).parts:
            child = _subdirectory(directory, component)
            os.close(directory)
            directory = child
        raw_sidecar = _private_file(directory, WEBSITE_SIDECAR, MAX_BYTES)
        sidecar = json.loads(raw_sidecar, object_pairs_hook=pairs)
        need(sidecar.get('source') == SOURCE and sidecar.get('schema_version') == 2 and
            sidecar.get('marker') == identity['marker'] and
            sidecar.get('request_sha256') == identity['request_sha256'] and
            sidecar.get('legacy_evidence') == identity['evidence'] and
            sidecar.get('native_context_canonical_sha256') == sha(payload['context']),
            'EVALUATION_ARCHIVE_SIDECAR_MISMATCH')
        for path, witness in zip(paths, identity['evidence']):
            raw = _private_file(directory, path.name, 16 * 1024 * 1024)
            need(len(raw) == witness['bytes'] and hashlib.sha256(raw).hexdigest() == witness['sha256'],
                'EVALUATION_HISTORICAL_PNG_MISMATCH')
        value = dict(protocol=CAPTURE_INDEX_PROTOCOL, identity=identity,
            paths=[str(p.relative_to(root)) for p in paths],
            native_context_sha256=sidecar['native_context_canonical_sha256'],
            sidecar_sha256=hashlib.sha256(raw_sidecar).hexdigest())
        raw = canonical(value)
        need(0 < len(raw) <= MAX_BYTES, 'EVALUATION_ARCHIVE_INDEX_INVALID')
        index = _subdirectory(parent, CAPTURE_INDEX, create=True)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=index)
        except FileExistsError:
            need(_private_file(index, name, MAX_BYTES) == raw,
                'EVALUATION_ARCHIVE_INDEX_CHANGED')
        else:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(index)
        return value
    finally:
        if index is not None:
            os.close(index)
        os.close(directory)
        os.close(parent)


def lookup_capture_paths(archive_root, operation, request, response):
    """Read only the deterministic index produced by record_capture_lookup."""
    from agent.native_chart_followup import _directory, _private_file, pairs, canonical, MAX_BYTES
    from agent.native_chart_capture import WEBSITE_SIDECAR
    root = Path(archive_root)
    payload = json.loads(request['request_body'], object_pairs_hook=pairs)
    need(payload.get('operation') == operation, 'EVALUATION_ARCHIVE_REQUEST_MISMATCH')
    payload['request_sha256'] = request['request_sha256']
    charts = json.loads(response['chart_evidence'], object_pairs_hook=pairs)
    identity, name = _index_identity(payload, charts)
    parent = _directory(root)
    index = directory = None
    try:
        try:
            index = _subdirectory(parent, CAPTURE_INDEX)
            raw = _private_file(index, name, MAX_BYTES)
        except FileNotFoundError:
            raise ValueError('EVALUATION_OWNED_CAPTURE_NOT_INDEXED') from None
        value = json.loads(raw, object_pairs_hook=pairs)
        need(type(value) is dict and set(value) == {'protocol', 'identity', 'paths',
            'native_context_sha256', 'sidecar_sha256'} and raw == canonical(value) and
            value['protocol'] == CAPTURE_INDEX_PROTOCOL and value['identity'] == identity and
            type(value['paths']) is list and len(value['paths']) == 2,
            'EVALUATION_ARCHIVE_INDEX_INVALID')
        relative = [Path(p) for p in value['paths']]
        need(all(not p.is_absolute() and '..' not in p.parts and p.parts for p in relative)
            and relative[0] != relative[1] and relative[0].parent == relative[1].parent,
            'EVALUATION_ARCHIVE_PATH_INVALID')
        directory = os.dup(parent)
        for component in relative[0].parent.parts:
            child = _subdirectory(directory, component)
            os.close(directory)
            directory = child
        raw_sidecar = _private_file(directory, WEBSITE_SIDECAR, MAX_BYTES)
        sidecar = json.loads(raw_sidecar, object_pairs_hook=pairs)
        need(hashlib.sha256(raw_sidecar).hexdigest() == value['sidecar_sha256'] and
            sidecar.get('native_context_canonical_sha256') == value['native_context_sha256'] and
            sidecar.get('legacy_evidence') == identity['evidence'] and
            sidecar.get('request_sha256') == identity['request_sha256'] and
            sidecar.get('marker') == identity['marker'], 'EVALUATION_ARCHIVE_INDEX_CHANGED')
        return [str(root / p) for p in relative]
    finally:
        for fd in (directory, index, parent):
            if fd is not None:
                os.close(fd)


def _context(sidecar, payload):
    """Reconstruct only facts the existing ActorCharts may have refreshed."""
    from agent.native_chart_followup import sha
    context = deepcopy(payload['context'])
    if sidecar.get('native_context_canonical_sha256') != sha(context):
        public = sidecar['public_market_context']
        context.update(source='Binance Futures', symbol=public['symbol'],
            fetched_at=public['observed_at'], server_time=public['server_time_ms'],
            mark_price=dict(price=public['mark_price'], time=public['mark_time_ms']),
            quantity_unit='base_asset', timeframes={tf: dict(symbol=public['symbol'],
                timeframe=tf, candles=deepcopy(public['candles'][tf]) +
                    [deepcopy(public['running_candles'][tf])]) for tf in ('15m', '1h')})
    need(sidecar.get('native_context_canonical_sha256') == sha(context),
        'EVALUATION_HISTORICAL_CONTEXT_MISMATCH')
    return context


def read_followup_custody(archive_root, operation, request, response, *, context,
                         sidecar, sidecar_sha256, csv_files, png_names):
    """Verify an actual ReceiptStore chain against the final native response.

    All turns in this native implementation complete against the same final
    response. A later incomplete intent therefore keeps the chain uncertain;
    an older completed turn cannot be substituted. Native receipt hashes use
    its ASCII canonical form; the projected evidence uses its own documented
    digest and does not relabel either hash as the other.
    """
    from agent.native_chart_followup import (_directory, _private_file, pairs,
        canonical, sha, MAX_BYTES, validate_material_send_check)
    from agent.native_market_context import convert_native_market_context
    payload = json.loads(request['request_body'], object_pairs_hook=pairs)
    payload['request_sha256'] = request['request_sha256']
    need(payload.get('operation') == operation and
        re.fullmatch(r'[a-f0-9]{64}', request['request_sha256']) is not None,
        'EVALUATION_ARCHIVE_REQUEST_MISMATCH')
    charts = json.loads(response['chart_evidence'], object_pairs_hook=pairs)
    need(type(charts) is list and len(charts) == 2 and
        [r.get('timeframe') for r in charts] == ['15m', '1h'],
        'EVALUATION_HISTORICAL_PNG_REQUIRED')
    need(type(png_names) is list and len(png_names) == 2 and
        len(set(png_names)) == 2 and type(csv_files) is list and len(csv_files) == 2,
        'EVALUATION_HISTORICAL_FOUR_FILES_REQUIRED')
    public = convert_native_market_context(context, now=context['fetched_at'])
    need(sidecar.get('native_context_canonical_sha256') == sha(context) and
        sidecar.get('public_market_context') == public and
        sidecar.get('request_sha256') == request['request_sha256'] and
        sidecar.get('marker') == payload['marker'] and
        re.fullmatch(r'[a-f0-9]{64}', sidecar_sha256) is not None,
        'EVALUATION_ARCHIVE_SIDECAR_MISMATCH')
    expected_names = png_names + [r['name'] for r in csv_files]
    need(len(set(expected_names)) == 4 and
        all(type(n) is str and n not in ('', '.', '..') and
            not any(c in n for c in ('/', '\\', '\x00')) for n in expected_names),
        'EVALUATION_HISTORICAL_FOUR_FILES_REQUIRED')
    key = sha(dict(operation=operation, request_sha256=request['request_sha256']))
    parent = _directory(archive_root)
    directory = None
    try:
        try:
            directory = os.open('dialogue-200-followup',
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            raise ValueError('EVALUATION_NATIVE_FOUR_FILE_RECEIPT_MISSING') from None
        info = os.fstat(directory)
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and
            stat.S_IMODE(info.st_mode) == 0o700, 'EVALUATION_ARCHIVE_NOT_PRIVATE')
        pattern = re.compile(re.escape(key) + r'(?:\.([2-9][0-9]*|1[0-9]+))?\.(intent|complete)\.json')
        names = {}
        for name in os.listdir(directory):
            if not name.startswith(key):
                continue
            match = pattern.fullmatch(name)
            need(match is not None, 'EVALUATION_NATIVE_RECEIPT_CHAIN_INVALID')
            names[(int(match.group(1) or 1), match.group(2))] = name
        need(names, 'EVALUATION_NATIVE_FOUR_FILE_RECEIPT_MISSING')
        count = max(n for n, _ in names)
        need(set(names) == {(n, stage) for n in range(1, count + 1)
            for stage in ('intent', 'complete')}, 'EVALUATION_NATIVE_RECEIPT_CHAIN_INCOMPLETE')
        previous = None
        final_intent = final_complete = None
        retained = []
        required = {'schema_version', 'kind', 'operation', 'request_sha256', 'marker',
            'original_payload_sha256', 'initial_context_sha256', 'followup_context_sha256',
            'projected_context_sha256', 'initial_prompt_sha256', 'followup_prompt_sha256',
            'initial_reply_sha256', 'initial_turns_sha256', 'conversation_url_sha256',
            'initial_chart_evidence', 'initial_attachment_names', 'followup_chart_evidence',
            'followup_attachment_names', 'observed_at', 'closed_candles_per_timeframe',
            'closed_candles_total', 'followup_sequence', 'previous_followup_intent_sha256'}
        for n in range(1, count + 1):
            iraw = _private_file(directory, names[n, 'intent'], MAX_BYTES)
            craw = _private_file(directory, names[n, 'complete'], MAX_BYTES)
            intent = json.loads(iraw, object_pairs_hook=pairs)
            complete = json.loads(craw, object_pairs_hook=pairs)
            need(type(intent) is dict and set(intent) in (required, required | {'material_send_check'})
                and iraw == canonical(intent) and craw == canonical(complete)
                and complete == dict(intent, intent_sha256=sha(intent),
                    response_id=response['response_id'], final_reply_sha256=response['assistant_sha256'])
                and intent['schema_version'] == 1 and intent['kind'] == 'NATIVE_200_CHART_FOLLOWUP'
                and intent['operation'] == operation and intent['request_sha256'] == request['request_sha256']
                and intent['marker'] == payload['marker'] and intent['original_payload_sha256'] == sha(payload)
                and intent['initial_context_sha256'] == sha(payload['context'])
                and intent['followup_sequence'] == n and intent['previous_followup_intent_sha256'] == previous
                and intent['closed_candles_per_timeframe'] == 200 and intent['closed_candles_total'] == 400,
                'EVALUATION_NATIVE_RECEIPT_CHAIN_INVALID')
            if 'material_send_check' in intent:
                validate_material_send_check(intent, now=intent['material_send_check']['send_checked_at'],
                    check_freshness=False)
            retained.extend(((names[n, 'intent'], iraw), (names[n, 'complete'], craw)))
            previous = sha(intent)
            final_intent, final_complete = intent, complete
        need(final_intent['followup_context_sha256'] == sha(context)
            and final_intent['projected_context_sha256'] == sha(public)
            and final_intent['observed_at'] == context['fetched_at']
            and final_intent['followup_chart_evidence'] == charts
            and final_intent['followup_attachment_names'] == expected_names,
            'EVALUATION_NATIVE_FOUR_FILE_RECEIPT_MISMATCH')
        # Detect a changed chain while it was read. Unknown or changed bytes
        # are retained as a blocker, never repaired or overwritten here.
        need(set(os.listdir(directory)) >= {name for name, _ in retained}
            and all(_private_file(directory, name, MAX_BYTES) == raw for name, raw in retained)
            and {name for name in os.listdir(directory) if name.startswith(key)} == set(names.values()),
            'EVALUATION_NATIVE_RECEIPT_CHANGED_DURING_READ')
        return dict(operation=operation, request_sha256=request['request_sha256'],
            response_id=response['response_id'], assistant_sha256=response['assistant_sha256'],
            native_intent_sha256=sha(final_intent), native_completion_sha256=sha(final_complete),
            context_sha256=digest(context), png_evidence_sha256=digest(charts),
            csv_sha256={r['timeframe']: hashlib.sha256(r['buffer']).hexdigest() for r in csv_files})
    finally:
        if directory is not None:
            os.close(directory)
        os.close(parent)


def read_bound_material_turn(archive_root, material_manifest):
    """Recover the authentic historical user-turn witness for the same room.

    The browser must still verify this exact rendered user prompt, decoded
    images/CSV tiles and final assistant reply against actual loaded room UI.
    Returning the private receipt does not claim that the turn is in that UI.
    The material manifest must originate from the strict closed-trade binder.
    """
    from agent.native_chart_followup import _directory, _private_file, pairs, canonical, sha, MAX_BYTES
    manifest = material_manifest
    need(type(manifest) is dict and type(manifest.get('delivery_custody')) is dict,
        'EVALUATION_BOUND_MATERIAL_REQUIRED')
    custody = manifest['delivery_custody']
    operation, request_sha = custody.get('operation'), custody.get('request_sha256')
    need(type(operation) is str and type(request_sha) is str and
        re.fullmatch(r'[a-f0-9]{64}', request_sha) is not None and
        custody.get('context_sha256') == manifest.get('context_sha256'),
        'EVALUATION_BOUND_MATERIAL_MISMATCH')
    key = sha(dict(operation=operation, request_sha256=request_sha))
    parent = _directory(archive_root)
    directory = index = None
    try:
        try:
            directory = _subdirectory(parent, 'dialogue-200-followup')
        except FileNotFoundError:
            raise ValueError('EVALUATION_NATIVE_FOUR_FILE_RECEIPT_MISSING') from None
        pattern = re.compile(re.escape(key) + r'(?:\.([2-9][0-9]*|1[0-9]+))?\.(intent|complete)\.json')
        names = {}
        for name in os.listdir(directory):
            if not name.startswith(key):
                continue
            found = pattern.fullmatch(name)
            need(found is not None, 'EVALUATION_NATIVE_RECEIPT_CHAIN_INVALID')
            names[int(found.group(1) or 1), found.group(2)] = name
        need(names, 'EVALUATION_NATIVE_FOUR_FILE_RECEIPT_MISSING')
        count = max(n for n, _ in names)
        need(set(names) == {(n, stage) for n in range(1, count + 1)
            for stage in ('intent', 'complete')}, 'EVALUATION_NATIVE_RECEIPT_CHAIN_INCOMPLETE')
        previous = None
        retained = []
        for n in range(1, count + 1):
            iraw = _private_file(directory, names[n, 'intent'], MAX_BYTES)
            craw = _private_file(directory, names[n, 'complete'], MAX_BYTES)
            intent = json.loads(iraw, object_pairs_hook=pairs)
            complete = json.loads(craw, object_pairs_hook=pairs)
            need(iraw == canonical(intent) and craw == canonical(complete) and
                complete == dict(intent, intent_sha256=sha(intent),
                    response_id=custody['response_id'], final_reply_sha256=custody['assistant_sha256']) and
                intent.get('schema_version') == 1 and intent.get('kind') == 'NATIVE_200_CHART_FOLLOWUP' and
                intent.get('operation') == operation and intent.get('request_sha256') == request_sha and
                intent.get('followup_sequence') == n and
                intent.get('previous_followup_intent_sha256') == previous,
                'EVALUATION_NATIVE_RECEIPT_CHAIN_INVALID')
            retained.extend(((names[n, 'intent'], iraw), (names[n, 'complete'], craw)))
            previous = sha(intent)
        need(sha(intent) == custody.get('native_intent_sha256') and
            sha(complete) == custody.get('native_completion_sha256') and
            digest(intent.get('followup_chart_evidence')) == custody.get('png_evidence_sha256') and
            intent.get('observed_at') == manifest.get('observed_at'),
            'EVALUATION_BOUND_MATERIAL_MISMATCH')
        files = manifest.get('files')
        need(type(files) is list and len(files) == 4, 'EVALUATION_HISTORICAL_FOUR_FILES_REQUIRED')
        csvs = sorted((r for r in files if r.get('kind') == 'CSV'), key=lambda r: r['timeframe'])
        pngs = {r['timeframe']: r for r in files if r.get('kind') == 'PNG'}
        names_sent = intent.get('followup_attachment_names')
        need(type(names_sent) is list and len(names_sent) == len(set(names_sent)) == 4 and
            len(csvs) == 2 and {r['timeframe'] for r in csvs} == {'15m', '1h'} and
            names_sent[2:] == [r['name'] for r in csvs] and
            custody.get('csv_sha256') == {r['timeframe']: r['sha256'] for r in csvs} and
            set(pngs) == {'15m', '1h'} and all(
                all(row[k] == pngs[row['timeframe']][k] for k in ('source', 'captured_at', 'sha256', 'bytes'))
                for row in intent['followup_chart_evidence']), 'EVALUATION_BOUND_MATERIAL_MISMATCH')
        identity, filename = _index_identity(intent, intent['followup_chart_evidence'])
        try:
            index = _subdirectory(parent, CAPTURE_INDEX)
            raw_index = _private_file(index, filename, MAX_BYTES)
        except FileNotFoundError:
            raise ValueError('EVALUATION_OWNED_CAPTURE_NOT_INDEXED') from None
        capture = json.loads(raw_index, object_pairs_hook=pairs)
        need(raw_index == canonical(capture) and capture.get('protocol') == CAPTURE_INDEX_PROTOCOL and
            capture.get('identity') == identity and
            capture.get('native_context_sha256') == intent.get('followup_context_sha256') and
            capture.get('sidecar_sha256') == manifest.get('archive_sidecar_sha256'),
            'EVALUATION_ARCHIVE_INDEX_CHANGED')
        need(all(_private_file(directory, name, MAX_BYTES) == raw for name, raw in retained) and
            {name for name in os.listdir(directory) if name.startswith(key)} == set(names.values()),
            'EVALUATION_NATIVE_RECEIPT_CHANGED_DURING_READ')
        return dict(intent=deepcopy(intent), completion=deepcopy(complete))
    finally:
        for fd in (index, directory, parent):
            if fd is not None:
                os.close(fd)


class NativeHistoricalArchiveReader:
    """Native namespace adapter; no guessed path or filesystem-wide lookup.

    ``capture_lookup`` is an internal actor-owned receipt/index lookup, not
    Office input. The default uses only the index produced by the native
    capture wrapper. Legacy captures without that index remain unbound.
    """
    def __init__(self, charts, archive_root, *, image_artifacts=None, capture_lookup=None):
        need(capture_lookup is None or callable(capture_lookup), 'EVALUATION_OWNED_CAPTURE_LOOKUP_REQUIRED')
        need(image_artifacts is None or callable(image_artifacts), 'EVALUATION_NATIVE_ARTIFACT_READER_REQUIRED')
        self.charts, self.root = charts, Path(archive_root)
        self.lookup = capture_lookup or (lambda op, req, res: lookup_capture_paths(self.root, op, req, res))
        self.image_artifacts = image_artifacts

    def __call__(self, operation, request, response):
        from agent.native_chart_followup import _directory, _private_file, pairs, MAX_BYTES
        from agent.native_chart_capture import WEBSITE_SIDECAR
        paths = self.lookup(operation, deepcopy(request), deepcopy(response))
        need(type(paths) is list and len(paths) == 2, 'EVALUATION_OWNED_CAPTURE_NOT_FOUND')
        selected = [Path(p) for p in paths]
        need(all(p.is_absolute() and '..' not in p.parts and self.root in p.parents for p in selected)
            and selected[0] != selected[1] and selected[0].parent == selected[1].parent,
            'EVALUATION_ARCHIVE_PATH_INVALID')
        directory = _directory(self.root)
        try:
            for component in selected[0].parent.relative_to(self.root).parts:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
                info = os.fstat(directory)
                need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and
                    stat.S_IMODE(info.st_mode) == 0o700, 'EVALUATION_ARCHIVE_NOT_PRIVATE')
            raw_sidecar = _private_file(directory, WEBSITE_SIDECAR, MAX_BYTES)
            sidecar = json.loads(raw_sidecar, object_pairs_hook=pairs)
        finally:
            os.close(directory)
        payload = json.loads(request['request_body'], object_pairs_hook=pairs)
        context = _context(sidecar, payload)
        charts = json.loads(response['chart_evidence'], object_pairs_hook=pairs)
        core_charts = [{k: row[k] for k in _PNG_FIELDS} for row in charts]
        # Native DOM.ask/_upload names the submitted PNGs from immutable
        # marker/timeframe/hash, independently of random archive basenames.
        # Historical readers never call the LIVE image_artifacts freshness
        # gate or change its clock. project_native_archive below revalidates
        # actual bytes/pixels/private sidecar at the ORIGINAL capture instant.
        png_names=[payload['marker']+'-'+row['timeframe']+'-'+row['sha256'][:12]+'.png'
            for row in core_charts]
        if self.image_artifacts is not None:
            artifacts=self.image_artifacts(paths,core_charts)
            need(type(artifacts) is list and len(artifacts)==2 and
                all(type(a) in (tuple,list) and len(a)==3 and type(a[1]) is bytes
                    and a[2]==c and hashlib.sha256(a[1]).hexdigest()==c['sha256']
                    and len(a[1])==c['bytes'] for a,c in zip(artifacts,core_charts)),
                'EVALUATION_NATIVE_ARTIFACT_MISMATCH')
        def delivery(op, req, res, sidecar_sha, csvs):
            need(sidecar_sha == hashlib.sha256(raw_sidecar).hexdigest(),
                'EVALUATION_ARCHIVE_CHANGED_DURING_READ')
            return read_followup_custody(self.root, op, req, res, context=context, sidecar=sidecar,
                sidecar_sha256=sidecar_sha, csv_files=csvs, png_names=png_names)
        return project_native_archive(self.charts, self.root, request, response,
            paths=paths, delivery_reader=delivery)
