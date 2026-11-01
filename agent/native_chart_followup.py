"""Actor-owned fresh public charts and durable receipts for clarification turns.

Installed only by the source-pinned native image recipe. The persistent Neurobro
page remains owned by its original actor. This adapter uses an isolated chart
browser, public GET-only market data, and the existing chart archiver. It cannot
change account state, an order, the native request body, or Risk Manager inputs.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
from importlib.metadata import version
import inspect
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone

from .native_market_context import convert_native_market_context
from .native_chart_capture import SOURCE, SIDECAR, _rgb
from .charts import chart_provenance, render_chart_png


MAX_BYTES = 256 * 1024
MAX_SECONDS = 70.0
CLEANUP_SECONDS = 10.0
MARKET_CHILD = '''import json,sys,time
sys.path.insert(0,"/app")
from agent.market import BinanceFuturesMarket
request=json.loads(sys.stdin.buffer.read(1025))
assert type(request) is dict and set(request)=={"symbol","seconds"}
assert type(request["seconds"]) in (int,float) and 0<request["seconds"]<=30
deadline=time.monotonic()+request["seconds"]
market=BinanceFuturesMarket(timeout_seconds=5,candle_limit=202)
market.set_remaining_budget(lambda:max(0,deadline-time.monotonic()))
value=market.snapshot(request["symbol"])
raw=json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
assert 0<len(raw)<=262144
sys.stdout.buffer.write(raw)
'''


class FollowupUnavailable(ValueError):
    pass


def need(value):
    if not value:
        raise FollowupUnavailable('NATIVE_200_FOLLOWUP_UNVERIFIED')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def pairs(items):
    result = {}
    for key, value in items:
        need(key not in result)
        result[key] = value
    return result


def native_snapshot(value, *, now):
    """Project verified public facts into the unchanged native Market ABI.

    The final Binance clock is in the same intervals as the collection cutoff.
    Using it admits the quote collected after the first clock. No OHLCV value is
    synthesized; every closed and running row must survive a native round trip.
    """
    from .llm import market_context_from_snapshot
    market_context_from_snapshot(value, now=now)
    final = value['collection_finished_server_time_ms']
    need(type(final) is int and final >= value['server_time_ms']
         and all(final // period == value['server_time_ms'] // period
                 for period in (900000, 3600000)))
    context = {'source': 'Binance Futures', 'symbol': value['symbol'],
        'fetched_at': value['observed_at'], 'server_time': final,
        'mark_price': {'price': value['mark_price'], 'time': value['mark_time_ms']},
        'quantity_unit': 'base_asset', 'timeframes': {}}
    for timeframe in ('15m', '1h'):
        rows = copy.deepcopy(value['candles'][timeframe])
        rows.append(copy.deepcopy(value['running_candles'][timeframe]))
        context['timeframes'][timeframe] = {'symbol': value['symbol'],
            'timeframe': timeframe, 'candles': rows}
    projected = convert_native_market_context(context, now=now)
    need(projected['candles'] == value['candles']
         and projected['running_candles'] == value['running_candles']
         and projected['mark_price'] == value['mark_price']
         and len(canonical(context)) <= MAX_BYTES)
    return context


def stop_child(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def public_snapshot(symbol, *, deadline, gate, monotonic=time.monotonic):
    """Bound DNS/HTTP parsing in a child and recheck current dispatch authority.

    The child has no credential environment or account client. It inherits the
    actor's filesystem namespace, but its fixed source/import closure reads no
    credential or profile files. There is one invocation; timeouts and
    interrupted observations are not retried.
    """
    need(type(symbol) is str and re.fullmatch(r'[A-Z0-9]{2,18}USDT', symbol)
         and callable(gate) and finite(deadline) and finite(monotonic())
         and gate() is True)
    seconds = min(30.0, deadline - monotonic())
    need(seconds > 0)
    raw = canonical({'symbol': symbol, 'seconds': seconds})
    process = subprocess.Popen([sys.executable, '-I', '-B', '-S', '-c', MARKET_CHILD],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'},
        close_fds=True)
    until = min(deadline, monotonic() + seconds + 1)
    first = True
    try:
        while True:
            need(gate() is True and monotonic() < until)
            try:
                output, _ = process.communicate(input=raw if first else None,
                    timeout=min(0.5, max(0.001, until-monotonic())))
                break
            except subprocess.TimeoutExpired:
                first = False
        need(process.returncode == 0 and 0 < len(output) <= MAX_BYTES
             and gate() is True and monotonic() < until)
        value = json.loads(output)
        need(type(value) is dict)
        return value
    finally:
        stop_child(process)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def _directory(root):
    """Open the caller-pinned private archive without following a symlink."""
    root = Path(root)
    need(root.is_absolute() and '..' not in root.parts and len(root.parts) > 2)
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in root.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        info = os.fstat(directory)
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
             and stat.S_IMODE(info.st_mode) == 0o700)
        return directory
    except BaseException:
        os.close(directory)
        raise


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
        value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _private_file(directory, name, maximum):
    need(type(name) is str and name not in ('', '.', '..') and '/' not in name)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
             and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1
             and 0 < before.st_size <= maximum)
        raw = stream.read(maximum+1)
        after = os.fstat(stream.fileno())
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        need(len(raw) == before.st_size and _identity(before) == _identity(after) == _identity(named))
        return raw


def verify_capture(charts, result, context, payload, *, archive_root, now, gate):
    """Bind the actual private PNGs and complete 400-candle capture manifest.

    Native six-field receipts stay unchanged. The supplemental capture sidecar
    must name this original request and this exact fresh context, not merely a
    symbol and timeframe. RGB verification is repeated against the full public
    observation so a replaced sidecar or a PNG from another capture is refused.
    """
    from .binance_website_chart import SOURCE as website_source
    if getattr(charts, 'SOURCE', None) == website_source:
        return verify_website_capture(charts, result, context, payload, archive_root=archive_root, now=now, gate=gate)
    need(callable(gate) and gate() is True and type(result) is dict
         and set(result) == {'paths', 'evidence'}
         and type(result['paths']) is list and len(result['paths']) == 2
         and all(type(path) is str for path in result['paths'])
         and type(result['evidence']) is list and len(result['evidence']) == 2
         and callable(charts.png_pixels) and type(charts.MAX_PNG) is int
         and 1024 <= charts.MAX_PNG <= 16*1024*1024)
    root = Path(archive_root)
    paths = [Path(path) for path in result['paths']]
    need(all(path.is_absolute() and '..' not in path.parts for path in paths)
         and paths[0] != paths[1] and paths[0].parent == paths[1].parent)
    attempt = paths[0].parent
    need(attempt != root and root in attempt.parents)
    parent = _directory(root)
    directory = os.dup(parent)
    try:
        for component in attempt.relative_to(root).parts:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                 and stat.S_IMODE(info.st_mode) == 0o700)
        raw = _private_file(directory, SIDECAR, MAX_BYTES)
        sidecar = json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(FollowupUnavailable('NATIVE_200_FOLLOWUP_UNVERIFIED')))
        projected = convert_native_market_context(context, now=now)
        need(type(sidecar) is dict and set(sidecar) == {'schema_version', 'source', 'marker',
            'request_sha256', 'native_context_canonical_sha256', 'native_context_hash_encoding',
            'public_market_context', 'charts', 'legacy_evidence', 'network_navigation_performed',
            'persistent_browser_context_used'}
            and type(sidecar['schema_version']) is int and sidecar['schema_version'] == 1
            and sidecar['source'] == SOURCE and sidecar['marker'] == payload['marker']
            and sidecar['request_sha256'] == payload['request_sha256']
            and sidecar['native_context_canonical_sha256'] == sha(context)
            and sidecar['native_context_hash_encoding'] == 'JSON_SORTED_COMPACT_ASCII_V1'
            and sidecar['public_market_context'] == projected
            and sidecar['legacy_evidence'] == result['evidence']
            and sidecar['network_navigation_performed'] is False
            and sidecar['persistent_browser_context_used'] is False
            and type(sidecar['charts']) is dict and set(sidecar['charts']) == {'15m', '1h'})
        native = {'png_pixels': charts.png_pixels}
        for timeframe, path, receipt in zip(('15m', '1h'), paths, result['evidence']):
            need(gate() is True and type(receipt) is dict and set(receipt) == {
                'source', 'symbol', 'timeframe', 'captured_at', 'sha256', 'bytes'}
                and receipt['source'] == SOURCE and receipt['symbol'] == projected['symbol']
                and receipt['timeframe'] == timeframe
                and type(receipt['bytes']) is int and 1024 <= receipt['bytes'] <= charts.MAX_PNG)
            captured = datetime.fromisoformat(receipt['captured_at'].replace('Z', '+00:00'))
            need(captured.tzinfo is not None and captured.utcoffset() == timezone.utc.utcoffset(captured)
                 and projected['observed_at'] <= captured.timestamp() <= now
                 and 0 <= now-captured.timestamp() <= 180)
            png = _private_file(directory, path.name, charts.MAX_PNG)
            need(len(png) == receipt['bytes'] and hashlib.sha256(png).hexdigest() == receipt['sha256'])
            rendered = render_chart_png(projected, timeframe)
            pixels = _rgb(native, rendered, charts.MAX_PNG)
            need(_rgb(native, png, charts.MAX_PNG) == pixels)
            expected = {'provenance': chart_provenance(projected, timeframe),
                'rendered_png_sha256': hashlib.sha256(rendered).hexdigest(),
                'screenshot_png_sha256': receipt['sha256'],
                'screenshot_rgb_sha256': hashlib.sha256(pixels).hexdigest(),
                'browser_screenshot_of_data_render': True, 'exchange_website_screenshot': False}
            need(sidecar['charts'][timeframe] == expected and gate() is True)
        # Refuse directory replacement while the descriptor-based reads ran.
        need(_identity(os.fstat(parent)) == _identity(os.stat(root, follow_symlinks=False))
             and _identity(os.fstat(directory)) == _identity(os.stat(attempt, follow_symlinks=False))
             and gate() is True)
        return copy.deepcopy(result)
    finally:
        os.close(directory)
        os.close(parent)


class ReceiptStore:
    """Exclusive intent/completion files; an incomplete intent is retained."""
    def __init__(self, archive_root, *, monotonic=time.monotonic, wall_clock=time.time):
        self.root, self.monotonic, self.wall_clock = Path(archive_root), monotonic, wall_clock

    def __call__(self, *, stage, receipt, deadline):
        need(stage in ('INTENT', 'COMPLETE') and type(receipt) is dict
             and finite(deadline) and finite(self.monotonic()) and self.monotonic() < deadline
             and type(receipt.get('operation')) is str
             and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', receipt['operation'])
             and type(receipt.get('request_sha256')) is str
             and re.fullmatch(r'[a-f0-9]{64}', receipt['request_sha256']))
        if 'material_send_check' in receipt:
            validate_material_send_check(receipt, now=self.wall_clock(), check_freshness=stage == 'INTENT')
        key = sha({'operation': receipt['operation'], 'request_sha256': receipt['request_sha256']})
        # Keep the first-turn filename so historical unknown intents continue
        # blocking a replay. Later turns extend that exclusive intent chain;
        # they never overwrite a prior turn or start another native operation.
        sequence = receipt.get('followup_sequence', 1)
        previous = receipt.get('previous_followup_intent_sha256')
        need(type(sequence) is int and sequence >= 1
             and (previous is None if sequence == 1 else
                  type(previous) is str and re.fullmatch(r'[a-f0-9]{64}', previous)))
        base_key = key
        if sequence > 1:
            key += '.' + str(sequence)
        parent = _directory(self.root)
        directory = None
        try:
            try:
                os.mkdir('dialogue-200-followup', 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
            directory = os.open('dialogue-200-followup', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=parent)
            info = os.fstat(directory)
            need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                 and stat.S_IMODE(info.st_mode) == 0o700)
            if sequence > 1:
                prior_key = base_key if sequence == 2 else base_key + '.' + str(sequence-1)
                prior_raw = _private_file(directory, prior_key+'.intent.json', MAX_BYTES)
                prior = json.loads(prior_raw, object_pairs_hook=pairs)
                need(sha(prior) == previous
                     and prior.get('operation') == receipt['operation']
                     and prior.get('request_sha256') == receipt['request_sha256']
                     and prior.get('followup_sequence', 1) == sequence-1)
            if stage == 'COMPLETE':
                intent = {k: v for k, v in receipt.items()
                          if k not in ('intent_sha256', 'response_id', 'final_reply_sha256')}
                need(receipt.get('intent_sha256') == sha(intent))
                fd = os.open(key+'.intent.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
                with os.fdopen(fd, 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    need(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
                         and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1
                         and 0 < before.st_size <= MAX_BYTES)
                    raw = stream.read(MAX_BYTES+1)
                    after = os.fstat(stream.fileno())
                    named = os.stat(key+'.intent.json', dir_fd=directory, follow_symlinks=False)
                    identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid,
                        s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                    need(identity(before) == identity(after) == identity(named)
                         and raw == canonical(intent))
            raw = canonical(receipt)
            need(0 < len(raw) <= MAX_BYTES and self.monotonic() < deadline)
            fd = os.open(key+'.'+stage.lower()+'.json',
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
            need(self.monotonic() < deadline)
            return True
        finally:
            if directory is not None:
                os.close(directory)
            os.close(parent)


class ActorCharts:
    def __init__(self, actor, charts, *, archive_root, expected_playwright_version,
                 snapshot=public_snapshot, monotonic=time.monotonic, wall_clock=time.time):
        need(type(expected_playwright_version) is str and version('playwright') == expected_playwright_version
             and callable(charts.capture) and callable(charts.close_resource)
             and callable(charts.png_pixels) and type(charts.MAX_PNG) is int
             and getattr(charts, 'SOURCE', None) in ('BINANCE_FUTURES_200_CLOSED_DATA_RENDER',
                 'BINANCE_USDM_FUTURES_WEBSITE_SCREENSHOT')
             and callable(snapshot) and callable(monotonic) and callable(wall_clock))
        self.actor, self.charts, self.root = actor, charts, str(archive_root)
        self.expected_source = charts.SOURCE
        self.snapshot, self.clock, self.wall_clock = snapshot, monotonic, wall_clock
        self.sdk = expected_playwright_version

    def __call__(self, *, payload, deadline, dispatch_check):
        need(type(payload) is dict and finite(deadline) and finite(self.clock())
             and self.clock() < deadline and callable(dispatch_check))
        original = canonical(payload)
        # The admitted original request is historical conversation input after
        # a long discussion. Read only its immutable symbol/time reference;
        # every value used as current market data is fetched/validated below.
        original_context = payload.get('context')
        need(type(original_context) is dict and original_context.get('source') == 'Binance Futures'
             and type(original_context.get('symbol')) is str
             and re.fullmatch(r'[A-Z0-9]{2,18}USDT', original_context['symbol'])
             and finite(original_context.get('fetched_at'))
             and 0 < original_context['fetched_at'] <= self.wall_clock())
        reference = {'symbol': original_context['symbol'], 'observed_at': original_context['fetched_at']}
        final_deadline = min(deadline, self.clock()+MAX_SECONDS)
        work_deadline = final_deadline-CLEANUP_SECONDS
        def gate():
            need(self.clock() < work_deadline and canonical(payload) == original
                 and dispatch_check() is True)
            return True
        gate()
        public = self.snapshot(reference['symbol'], deadline=min(work_deadline, self.clock()+31), gate=gate)
        gate()
        context = native_snapshot(public, now=self.wall_clock())
        need(context['symbol'] == reference['symbol']
             and context['fetched_at'] >= reference['observed_at'])
        for key in ('pending_review', 'pending_order_context', 'material_contract'):
            if key in original_context:
                context[key] = copy.deepcopy(original_context[key])
        browser = isolated = None
        failed = False
        try:
            from playwright.sync_api import Browser, Playwright
            from playwright._impl._browser import Browser as ImplBrowser
            from playwright.sync_api._generated import mapping
            playwright = self.actor._playwright
            need(type(playwright) is Playwright and type(self.actor.executable) is str
                 and Path(self.actor.executable).is_absolute() and version('playwright') == self.sdk)
            gate()
            browser = playwright.chromium.launch(executable_path=self.actor.executable,
                headless=True, chromium_sandbox=True,
                timeout=max(1, min(10000, int((work_deadline-self.clock())*1000))))
            need(type(browser) is Browser and type(browser._impl_obj) is ImplBrowser)
            gate()
            kwargs = {'ignoreHTTPSErrors': False, 'viewport': {'width': 1024, 'height': 640},
                      'deviceScaleFactor': 1, 'serviceWorkers': 'block'}
            if self.expected_source == 'BINANCE_USDM_FUTURES_WEBSITE_SCREENSHOT':
                kwargs.update(timezoneId='UTC', locale='en-US')
            method = browser._impl_obj.new_context
            need(inspect.iscoroutinefunction(method))
            inspect.signature(method).bind(**kwargs)
            async def create():
                return await method(**kwargs)
            isolated = mapping.from_maybe_impl(browser._sync(asyncio.wait_for(create(),
                timeout=min(10.0, work_deadline-self.clock()))))
            gate()
            result = self.charts.capture(isolated, context, payload['marker'],
                payload['request_sha256'], archive_root=self.root, deadline=work_deadline)
            gate()
            result = verify_capture(self.charts, result, context, payload,
                archive_root=self.root, now=self.wall_clock(), gate=gate)
            gate()
            return {'context': context, 'chart_images': result['paths'],
                    'chart_evidence': result['evidence']}
        except BaseException:
            failed = True
            raise
        finally:
            cleanup_failed = False
            for resource in (isolated, browser):
                if resource is not None:
                    try:
                        self.charts.close_resource(resource)
                    except BaseException:
                        cleanup_failed = True
            if cleanup_failed or failed and browser is None:
                # Reuse the native actor's existing indeterminate-chart guard.
                self.actor._chart_cleanup_failed = True
            if cleanup_failed or self.clock() >= final_deadline:
                raise FollowupUnavailable('NATIVE_200_FOLLOWUP_CLEANUP_UNVERIFIED')


def install_actor_bridge(bridge, *, actor, archive_root, expected_playwright_version):
    """Bind callbacks in the existing actor; never launch another coordinator."""
    from .native_browser_hook import install_native_bridge
    charts = importlib.import_module(type(bridge).__module__.split('.')[0]+'.charts')
    callback = ActorCharts(actor, charts, archive_root=archive_root,
                           expected_playwright_version=expected_playwright_version)
    return install_native_bridge(bridge, fresh_charts=callback,
        followup_receipt=ReceiptStore(archive_root), archive_root=archive_root)


def verify_website_capture(charts, result, context, payload, *, archive_root, now, gate):
    """Verify real website receipts without accepting generated candle PNGs."""
    from .binance_website_chart import SOURCE as website_source, CONTRACT, VIEWPORT, PERIODS, url_for
    from .native_chart_capture import WEBSITE_SIDECAR, website_rgb
    need(callable(gate) and gate() is True and finite(now)
         and type(result) is dict and set(result) == {'paths', 'evidence'}
         and type(result['paths']) is list and len(result['paths']) == 2
         and type(result['evidence']) is list and len(result['evidence']) == 2)
    projected = convert_native_market_context(context, now=now)
    root = Path(archive_root);paths = [Path(path) for path in result['paths']]
    need(all(path.is_absolute() and '..' not in path.parts for path in paths)
         and paths[0] != paths[1] and paths[0].parent == paths[1].parent)
    attempt = paths[0].parent;need(attempt != root and root in attempt.parents)
    parent = _directory(root);directory = os.dup(parent)
    try:
        for component in attempt.relative_to(root).parts:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory);directory = child
            info = os.fstat(directory)
            need(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)
        sidecar = json.loads(_private_file(directory, WEBSITE_SIDECAR, MAX_BYTES), object_pairs_hook=pairs)
        need(sidecar.get('schema_version') == 2 and sidecar.get('source') == website_source
             and sidecar.get('ui_contract') == CONTRACT and sidecar.get('marker') == payload['marker']
             and sidecar.get('request_sha256') == payload['request_sha256']
             and sidecar.get('native_context_canonical_sha256') == sha(context)
             and sidecar.get('native_context_hash_encoding') == 'JSON_SORTED_COMPACT_ASCII_V1'
             and sidecar.get('public_market_context') == projected
             and sidecar.get('legacy_evidence') == result['evidence']
             and sidecar.get('network_navigation_performed') is True
             and sidecar.get('persistent_browser_context_used') is False
             and type(sidecar.get('charts')) is dict and set(sidecar['charts']) == {'15m','1h'})
        for tf, path, receipt in zip(('15m','1h'), paths, result['evidence']):
            need(gate() is True and type(receipt) is dict and set(receipt) == {
                'source','symbol','timeframe','captured_at','sha256','bytes'}
                 and receipt['source'] == website_source and receipt['symbol'] == projected['symbol']
                 and receipt['timeframe'] == tf)
            captured = datetime.fromisoformat(receipt['captured_at'].replace('Z','+00:00'))
            need(captured.utcoffset() == timezone.utc.utcoffset(captured)
                 and projected['observed_at'] <= captured.timestamp() <= now
                 and now-captured.timestamp() <= 180)
            png = _private_file(directory, path.name, charts.MAX_PNG)
            need(len(png) == receipt['bytes'] and hashlib.sha256(png).hexdigest() == receipt['sha256'])
            row = sidecar['charts'][tf]
            clip = row.get('capture_clip')
            need(type(clip) is dict and set(clip) == {'x','y','width','height'}
                 and clip['x'] == clip['y'] == 0
                 and all(type(clip[key]) is int for key in clip)
                 and 700 <= clip['width'] <= VIEWPORT['width']
                 and 400 <= clip['height'] <= VIEWPORT['height'])
            pixels = website_rgb({'png_pixels': charts.png_pixels}, png, charts.MAX_PNG,
                                 dimensions=(clip['width'],clip['height']))
            need(row.get('source') == website_source and row.get('ui_contract') == CONTRACT
                 and row.get('page_url') == url_for(projected['symbol'])
                 and row.get('symbol') == projected['symbol'] and row.get('market') == 'USDT_PERPETUAL'
                 and row.get('timeframe') == tf and row.get('captured_at') == receipt['captured_at']
                 and row.get('capture_observed_at') == captured.timestamp()
                 and row.get('viewport') == VIEWPORT
                 and row.get('observed_bar_interval_ms') == PERIODS[tf]
                 and row.get('screenshot_png_sha256') == receipt['sha256']
                 and row.get('screenshot_rgb_sha256') == hashlib.sha256(pixels).hexdigest()
                 and row.get('exchange_website_screenshot') is True
                 and row.get('browser_screenshot_of_data_render') is False)
            visible = row.get('visible_range')
            need(type(visible) is dict and visible.get('verification') == 'VISIBLE_CANDLE_HOVER_ENDPOINTS_UTC_V1')
            first, last = visible.get('first_visible_open_time_ms'), visible.get('last_visible_open_time_ms')
            interval = PERIODS[tf];closed = projected['candles'][tf]
            need(type(first) is int and type(last) is int and first%interval == last%interval == 0
                 and first <= closed[0]['open_time']-2*interval and last >= closed[-1]['open_time']
                 and last <= projected.get('collection_finished_server_time_ms', projected['server_time_ms'])//interval*interval
                 and visible.get('required_closed_count') == 200
                 and visible.get('required_first_open_time_ms') == closed[0]['open_time']
                 and visible.get('required_last_close_time_ms') == closed[-1]['close_time']
                 and visible.get('running_candle_in_closed_count') is False
                 and visible.get('covered_closed_count') ==
                     (min(last,projected['server_time_ms']//interval*interval-interval)-first)//interval+1)
        need(_identity(os.fstat(parent)) == _identity(os.stat(root,follow_symlinks=False))
             and _identity(os.fstat(directory)) == _identity(os.stat(attempt,follow_symlinks=False))
             and gate() is True)
        return copy.deepcopy(result)
    finally:
        os.close(directory);os.close(parent)


def material_dispatch_provenance(context, chart_evidence, *, now, allowed_sources=None):
    """Check both screenshots and the exact public observation at dispatch time.

    Returns facts for the existing durable intent/receipt; it does not send,
    change an operation, refresh prices or write a journal by itself.
    """
    from .binance_website_chart import SOURCE as website_source
    sources = {website_source} if allowed_sources is None else set(allowed_sources)
    need(sources and finite(now) and type(chart_evidence) in (list, tuple)
         and len(chart_evidence) == 2)
    projected = convert_native_market_context(context, now=now)
    need(all(type(row) is dict and row.get('timeframe') in ('15m','1h') for row in chart_evidence))
    indexed = {row['timeframe']: row for row in chart_evidence}
    need(len(indexed) == 2 and set(indexed) == {'15m','1h'})
    captures = []
    for timeframe in ('15m','1h'):
        row = indexed[timeframe]
        need(type(row) is dict and row.get('source') in sources
             and row.get('symbol') == projected['symbol'] and row.get('timeframe') == timeframe
             and type(row.get('sha256')) is str and re.fullmatch(r'[a-f0-9]{64}', row['sha256']))
        captured = datetime.fromisoformat(row['captured_at'].replace('Z','+00:00'))
        need(captured.tzinfo is not None and captured.utcoffset() == timezone.utc.utcoffset(captured)
             and projected['observed_at'] <= captured.timestamp() <= now
             and now-captured.timestamp() <= 180)
        captures.append({'timeframe': timeframe,'captured_at': row['captured_at'],
                         'sha256': row['sha256'], 'age_at_send_check_seconds': now-captured.timestamp()})
    return {'symbol': projected['symbol'], 'data_observed_at': projected['observed_at'],
        'data_observed_at_utc': projected['observed_at_utc'], 'send_checked_at': now,
        'data_age_at_send_check_seconds': now-projected['observed_at'],
        'closed_candles_per_timeframe': 200, 'closed_candles_total': 400,
        'screenshots': captures, 'running_candles_separate': True}


def validate_material_send_check(receipt, *, now, check_freshness):
    """Bind optional new send-time facts; historical intents remain readable."""
    facts = receipt.get('material_send_check')
    need(type(facts) is dict and set(facts) == {'symbol','data_observed_at','data_observed_at_utc',
        'send_checked_at','data_age_at_send_check_seconds','closed_candles_per_timeframe',
        'closed_candles_total','screenshots','running_candles_separate'}
         and facts['closed_candles_per_timeframe'] == 200 and facts['closed_candles_total'] == 400
         and facts['running_candles_separate'] is True
         and finite(facts['data_observed_at']) and finite(facts['send_checked_at'])
         and facts['data_observed_at'] == receipt.get('observed_at')
         and 0 <= facts['send_checked_at']-facts['data_observed_at'] <= 180
         and facts['data_age_at_send_check_seconds'] == facts['send_checked_at']-facts['data_observed_at'])
    if check_freshness:
        need(finite(now) and 0 <= now-facts['send_checked_at'] <= 5)
    observed = datetime.fromisoformat(facts['data_observed_at_utc'].replace('Z','+00:00'))
    need(observed.utcoffset() == timezone.utc.utcoffset(observed)
         and abs(observed.timestamp()-facts['data_observed_at']) <= .002)
    evidence = receipt.get('followup_chart_evidence')
    need(type(evidence) is list and len(evidence) == 2
         and type(facts['screenshots']) is list and len(facts['screenshots']) == 2)
    need(all(type(row) is dict and row.get('timeframe') in ('15m','1h') for row in facts['screenshots'])
         and all(type(row) is dict and row.get('timeframe') in ('15m','1h') for row in evidence))
    fact_map = {row['timeframe']: row for row in facts['screenshots']}
    image_map = {row['timeframe']: row for row in evidence}
    need(len(fact_map) == len(image_map) == 2 and set(fact_map) == set(image_map) == {'15m','1h'})
    for tf in ('15m','1h'):
        row, image = fact_map[tf], image_map[tf]
        need(type(row) is dict and set(row) == {'timeframe','captured_at','sha256','age_at_send_check_seconds'}
             and row['timeframe'] == image.get('timeframe') == tf
             and image.get('symbol') == facts['symbol']
             and row['captured_at'] == image.get('captured_at') and row['sha256'] == image.get('sha256'))
        stamp = datetime.fromisoformat(row['captured_at'].replace('Z','+00:00'))
        need(stamp.utcoffset() == timezone.utc.utcoffset(stamp)
             and facts['data_observed_at'] <= stamp.timestamp() <= facts['send_checked_at']
             and 0 <= facts['send_checked_at']-stamp.timestamp() <= 180
             and row['age_at_send_check_seconds'] == facts['send_checked_at']-stamp.timestamp())
    return facts
