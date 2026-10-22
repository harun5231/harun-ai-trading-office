"""Capture verified 200-candle data charts inside an existing isolated Chromium context.

This is an additive adapter, not a browser launcher or a production installer.
The caller must authenticate the native namespace, reconcile the SOURCE label at
all native admission boundaries, and provide its reviewed deadline-bound RPC.
Only our newly created page is closed. No network navigation, trading action,
persistent profile access, or fallback to the legacy website capture occurs.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from types import ModuleType

from .charts import chart_provenance, render_chart_png
from .native_market_context import convert_native_market_context


SOURCE = 'BINANCE_FUTURES_200_CLOSED_DATA_RENDER'
PHASES = ('VALIDATE_CONTEXT', 'CREATE_PAGE', 'RENDER_15M', 'SCREENSHOT_15M',
          'RENDER_1H', 'SCREENSHOT_1H', 'ARCHIVE', 'COMPLETE', 'CLEANUP')
VIEWPORT = {'width': 1024, 'height': 640}
SIDECAR = 'closed-200-provenance.json'
CLEANUP_SECONDS = 5.0
MAX_SECONDS = 90.0
MAX_CONTEXT_BYTES = 2 * 1024 * 1024
MAX_SIDECAR_BYTES = 256 * 1024
CSP = ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
       "connect-src 'none'; base-uri 'none'; form-action 'none'; "
       "frame-src 'none'; object-src 'none'")
IMAGE_READY = '''() => {
  const images = document.querySelectorAll('img');
  const image = images[0];
  return images.length === 1 && document.body.children.length === 1 &&
    image.complete && image.naturalWidth === 1024 && image.naturalHeight === 640 &&
    image.width === 1024 && image.height === 640 && image.currentSrc === image.src &&
    image.src.startsWith('data:image/png;base64,') &&
    document.documentElement.scrollWidth === 1024 && document.documentElement.scrollHeight === 640;
}'''


class NativeCaptureConfigurationError(ValueError):
    pass


def _need(ok):
    if not ok:
        raise NativeCaptureConfigurationError('NATIVE_200_CAPTURE_CONFIGURATION_UNVERIFIED')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode('ascii')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _html(png):
    # The only dynamic input is our already verified PNG, base64 encoded. The
    # document contains no script, external stylesheet, iframe or remote URL.
    encoded = base64.b64encode(png).decode('ascii')
    return ('<!doctype html><html style="margin:0;width:1024px;height:640px;overflow:hidden">'
        '<head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' + CSP +
        '"></head><body style="margin:0;width:1024px;height:640px;overflow:hidden">'
        '<img alt="Verified Binance Futures 200 closed candle data chart" '
        'width="1024" height="640" style="display:block;width:1024px;height:640px" '
        'src="data:image/png;base64,' + encoded + '"></body></html>')


def _rgb(native, raw, maximum):
    if type(raw) is not bytes or not 1024 <= len(raw) <= maximum:
        raise ValueError('NATIVE_200_PNG_UNVERIFIED')
    decoded = native['png_pixels'](raw)
    if (type(decoded) not in (tuple, list) or len(decoded) != 4
            or tuple(decoded[:2]) != (VIEWPORT['width'], VIEWPORT['height'])
            or type(decoded[2]) is not int or decoded[2] not in (3, 4)
            or type(decoded[3]) not in (bytes, bytearray)):
        raise ValueError('NATIVE_200_PNG_UNVERIFIED')
    channels, pixels = decoded[2:]
    count = VIEWPORT['width'] * VIEWPORT['height']
    if len(pixels) != count * channels:
        raise ValueError('NATIVE_200_PNG_UNVERIFIED')
    if channels == 3:
        return bytes(pixels)
    if bytes(pixels[3::4]) != b'\xff' * count:
        raise ValueError('NATIVE_200_PNG_UNVERIFIED')
    result = bytearray(count * 3)
    result[0::3], result[1::3], result[2::3] = pixels[0::4], pixels[1::4], pixels[2::4]
    return bytes(result)


def _sidecar(root, result, images_key, value):
    """Create one supplemental manifest without modifying native image receipts."""
    root = Path(root)
    paths = [Path(path) for path in result[images_key]]
    _need(root.is_absolute() and '..' not in root.parts
        and all(path.is_absolute() and '..' not in path.parts for path in paths)
        and paths[0].parent == paths[1].parent)
    attempt = paths[0].parent
    _need(attempt != root and root in attempt.parents)
    # Walk through directory descriptors with O_NOFOLLOW. The writable subtree
    # is the native archiver's existing private archive, never a new public path.
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        seen = Path('/')
        for component in attempt.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
            seen = seen / component
            info = os.fstat(directory)
            if seen == root or root in seen.parents:
                _need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                    and stat.S_IMODE(info.st_mode) & 0o077 == 0)
        for path, evidence in zip(paths, result['evidence']):
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
            try:
                info = os.fstat(fd)
                _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                    and info.st_size == evidence['bytes'])
                raw = os.read(fd, info.st_size + 1)
                after = os.fstat(fd)
                named = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid,
                    s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                _need(len(raw) == info.st_size and identity(info) == identity(after) == identity(named)
                    and _sha(raw) == evidence['sha256'])
            finally:
                os.close(fd)
        raw = _json(value)
        _need(len(raw) <= MAX_SIDECAR_BYTES)
        fd = os.open(SIDECAR, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)


def install(native_namespace, *, expected_source, expected_capture, images_key,
            phase_map, rpc, monotonic=time.monotonic, wall_clock=time.time):
    """Install only into a caller-authenticated, explicitly reconciled namespace.

    ``expected_capture`` is the original function whose source the caller pinned.
    ``rpc`` must be a caller-reviewed, deadline-bound Playwright adapter supporting
    new_page, set_viewport_size, set_content, wait_for_function and screenshot.
    Native _startup_read alone does not support these methods; this module never
    guesses a private Playwright ABI or an allowlisted method name.
    """
    native = native_namespace.__dict__ if isinstance(native_namespace, ModuleType) else native_namespace
    _need(type(native) is dict and type(expected_source) is str
        and expected_source == SOURCE and native.get('SOURCE') == expected_source
        and callable(expected_capture) and native.get('capture') is expected_capture
        and type(images_key) is str and re.fullmatch(r'[a-z][a-z_]{0,31}', images_key) is not None
        and images_key != 'evidence' and callable(rpc) and callable(monotonic) and callable(wall_clock))
    for name in ('archive_images', 'validate_context', 'png_pixels', 'close_resource'):
        _need(callable(native.get(name)))
    _need(isinstance(native.get('ChartUnavailable'), type)
        and issubclass(native['ChartUnavailable'], Exception)
        and type(native.get('MAX_PNG')) is int and 1024 <= native['MAX_PNG'] <= 16*1024*1024
        and type(phase_map) is dict and set(phase_map) == set(PHASES)
        and all(type(value) is str for value in phase_map.values()))
    for key in ('CAPTURE_PHASES', 'PROGRESS_PHASES'):
        _need(type(native.get(key)) in (set, frozenset, tuple, list)
            and all(type(phase) is str for phase in native[key])
            and set(phase_map.values()) <= set(native[key]))
    signature = inspect.signature(expected_capture)
    _need(tuple(signature.parameters) == ('browser_context', 'context_dict', 'marker',
        'request_sha256', 'archive_root', 'deadline', 'progress', 'checkpoint'))
    default_archive = signature.parameters['archive_root'].default
    _need(isinstance(default_archive, (str, Path)) and Path(default_archive).is_absolute())
    phases = dict(phase_map)
    maximum = native['MAX_PNG']

    def capture(browser_context, context_dict, marker, request_sha256,
                archive_root=default_archive, *, deadline=None, progress=None, checkpoint=None):
        page = None
        owns_page = False
        phase = 'VALIDATE_CONTEXT'
        failure = None
        outcome = None
        started = monotonic()
        if type(started) not in (int, float) or not math.isfinite(started):
            raise native['ChartUnavailable']('NATIVE_200_CAPTURE_UNVERIFIED', phase=phases[phase])
        final_deadline = started + MAX_SECONDS
        if deadline is not None:
            if type(deadline) not in (int, float) or not math.isfinite(deadline):
                raise native['ChartUnavailable']('NATIVE_200_CAPTURE_UNVERIFIED', phase=phases[phase])
            final_deadline = min(final_deadline, deadline)
        work_deadline = final_deadline - CLEANUP_SECONDS

        def guard():
            current = monotonic()
            if (type(current) not in (int, float) or not math.isfinite(current)
                    or current < started or current >= work_deadline
                    or native.get('SOURCE') != expected_source):
                raise ValueError('NATIVE_200_CAPTURE_DEADLINE_OR_SOURCE_UNVERIFIED')

        def step(value):
            nonlocal phase
            phase = value
            if callable(progress):
                try:
                    progress(phases[value], {'chart_source': SOURCE, 'network_navigation_performed': False})
                except Exception:
                    pass

        def call(resource, method, *args, **kwargs):
            guard()
            remaining = max(1, min(10_000, int((work_deadline-monotonic())*1000)))
            if method in ('set_content', 'wait_for_function', 'screenshot'):
                kwargs['timeout'] = remaining
            result = rpc(resource, method, min(work_deadline, monotonic()+10.0), *args, **kwargs)
            guard()
            return result

        try:
            step('VALIDATE_CONTEXT')
            guard()
            _need(type(marker) is str and marker.isascii() and 1 <= len(marker) <= 160
                and not any(ord(char) <= 32 or char in '/\\' for char in marker)
                and type(request_sha256) is str and re.fullmatch(r'[a-f0-9]{64}', request_sha256) is not None)
            _need(native['validate_context'](context_dict) == context_dict.get('symbol'))
            canonical = convert_native_market_context(context_dict, now=wall_clock())
            raw_context = _json(context_dict)
            _need(len(raw_context) <= MAX_CONTEXT_BYTES)
            _need(type(getattr(browser_context, 'pages', None)) in (list, tuple)
                and len(browser_context.pages) == 0)
            chart_metadata = {tf: chart_provenance(canonical, tf) for tf in ('15m', '1h')}
            step('CREATE_PAGE')
            guard()
            # Retain a returned handle and establish ownership before the
            # post-RPC guard. A source/deadline change after new_page returns
            # must not lose a known owned page and skip its cleanup.
            page = rpc(browser_context, 'new_page', min(work_deadline, monotonic()+10.0))
            _need(getattr(page, 'context', None) is browser_context
                and type(getattr(browser_context, 'pages', None)) in (list, tuple)
                and len(browser_context.pages) == 1 and browser_context.pages[0] is page)
            owns_page = True
            guard()
            call(page, 'set_viewport_size', dict(VIEWPORT))
            pictures, renders = [], {}
            for timeframe, label in (('15m', '15M'), ('1h', '1H')):
                step('RENDER_' + label)
                guard()
                rendered = render_chart_png(canonical, timeframe)
                expected_pixels = _rgb(native, rendered, maximum)
                call(page, 'set_content', _html(rendered), wait_until='load')
                call(page, 'wait_for_function', IMAGE_READY)
                step('SCREENSHOT_' + label)
                raw = call(page, 'screenshot', type='png', full_page=False, animations='disabled', scale='css')
                _need(_rgb(native, raw, maximum) == expected_pixels)
                captured_at = dt.datetime.fromtimestamp(wall_clock(), dt.timezone.utc).isoformat()
                pictures.append((timeframe, raw, captured_at))
                renders[timeframe] = {'provenance': chart_metadata[timeframe],
                    'rendered_png_sha256': _sha(rendered), 'screenshot_png_sha256': _sha(raw),
                    'screenshot_rgb_sha256': _sha(expected_pixels),
                    'browser_screenshot_of_data_render': True, 'exchange_website_screenshot': False}
            step('ARCHIVE')
            guard()
            _need(native['validate_context'](context_dict) == canonical['symbol'])
            _need(convert_native_market_context(context_dict, now=wall_clock()) == canonical
                and _sha(_json(context_dict)) == _sha(raw_context))
            result = native['archive_images'](archive_root, marker, request_sha256, canonical['symbol'], pictures)
            _need(type(result) is dict and set(result) == {images_key, 'evidence'}
                and type(result[images_key]) is list and len(result[images_key]) == 2
                and all(type(path) is str for path in result[images_key])
                and type(result['evidence']) is list and len(result['evidence']) == 2)
            for (timeframe, raw, captured_at), evidence in zip(pictures, result['evidence']):
                _need(type(evidence) is dict and evidence == dict(source=SOURCE, symbol=canonical['symbol'],
                    timeframe=timeframe, captured_at=captured_at, sha256=_sha(raw), bytes=len(raw)))
            _need(convert_native_market_context(context_dict, now=wall_clock()) == canonical
                and _sha(_json(context_dict)) == _sha(raw_context))
            _sidecar(archive_root, result, images_key, {'schema_version': 1, 'source': SOURCE,
                'marker': marker, 'request_sha256': request_sha256,
                'native_context_canonical_sha256': _sha(raw_context),
                'native_context_hash_encoding': 'JSON_SORTED_COMPACT_ASCII_V1',
                'public_market_context': canonical, 'charts': renders,
                'legacy_evidence': result['evidence'], 'network_navigation_performed': False,
                'persistent_browser_context_used': False})
            guard()
            _need(convert_native_market_context(context_dict, now=wall_clock()) == canonical
                and _sha(_json(context_dict)) == _sha(raw_context))
            step('COMPLETE')
            outcome = 'CAPTURED'
            return result
        except Exception:
            failure = native['ChartUnavailable']('NATIVE_200_CAPTURE_UNVERIFIED', phase=phases[phase])
            outcome = 'FAILED'
            raise failure from None
        except BaseException as error:
            failure = error
            outcome = 'INTERRUPTED'
            raise
        finally:
            operation_phase = phase
            if page is not None and owns_page:
                step('CLEANUP')
                try:
                    # The native helper closes a single resource under its own
                    # reviewed five-second bound. Work reserved that allowance.
                    _need(monotonic() < final_deadline
                        and getattr(page, 'context', None) is browser_context)
                    native['close_resource'](page)
                except Exception:
                    if failure is None:
                        outcome = 'FAILED'
                        failure = native['ChartUnavailable']('NATIVE_200_CAPTURE_CLEANUP_UNVERIFIED',
                            phase=phases['CLEANUP'])
                        if callable(checkpoint):
                            try:
                                checkpoint({'status': 'NATIVE_200_CHART_CAPTURE', 'outcome': outcome,
                                    'phase': phases['CLEANUP']})
                            except Exception:
                                pass
                        raise failure from None
            if callable(checkpoint):
                try:
                    checkpoint({'status': 'NATIVE_200_CHART_CAPTURE', 'outcome': outcome,
                        'phase': phases[operation_phase]})
                except Exception:
                    pass

    capture.__name__ = 'capture'
    capture.__doc__ = 'Reviewed 200-closed-candle data-render screenshot adapter; no website navigation.'
    capture.__harun_native_chart_capture_source__ = SOURCE
    native['capture'] = capture
    return capture
