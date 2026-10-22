"""Five deadline-bound Playwright RPCs for the reviewed 200-candle capture page.

This is not a browser launcher or a general JavaScript executor. Compatibility
is checked against the explicitly reviewed installed SDK version and its actual
private method signatures before use. A timed-out new_page may finish remotely
without a handle; the owning native actor must still close its isolated context.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
from importlib.metadata import version
import math
import re
import struct
import time

from .native_chart_capture import IMAGE_READY, VIEWPORT, _html


class NativeChartRPCUnavailable(ValueError):
    pass


def _need(value):
    if not value:
        raise NativeChartRPCUnavailable('NATIVE_CHART_RPC_UNVERIFIED')


def make_rpc(*, expected_playwright_version, monotonic=time.monotonic):
    """Return a capture-only RPC after inspecting the actual installed SDK."""
    _need(type(expected_playwright_version) is str
        and re.fullmatch(r'\d{1,3}\.\d{1,3}\.\d{1,3}', expected_playwright_version) is not None
        and callable(monotonic))
    try:
        actual_version = version('playwright')
        _need(actual_version == expected_playwright_version)
        from playwright.sync_api import BrowserContext, Page
        from playwright._impl._browser_context import BrowserContext as ImplContext
        from playwright._impl._page import Page as ImplPage
        from playwright.sync_api._generated import mapping
        required = {
            'new_page': (ImplContext, {}),
            'set_viewport_size': (ImplPage, {'viewportSize': VIEWPORT}),
            'set_content': (ImplPage, {'html': '', 'timeout': 1, 'waitUntil': 'load'}),
            'wait_for_function': (ImplPage, {'expression': IMAGE_READY, 'timeout': 1}),
            'screenshot': (ImplPage, {'type': 'png', 'timeout': 1, 'fullPage': False,
                'animations': 'disabled', 'scale': 'css'}),
        }
        shapes = {}
        for name, (kind, kwargs) in required.items():
            method = getattr(kind, name)
            signature = inspect.signature(method)
            _need(inspect.iscoroutinefunction(method)
                and set(kwargs) <= set(signature.parameters)
                and all(signature.parameters[key].kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY) for key in kwargs))
            signature.bind(None, **kwargs)
            shapes[name] = list(signature.parameters)
        _need(callable(mapping.from_maybe_impl))
        inspect.signature(mapping.from_maybe_impl).bind(None)
    except (ImportError, AttributeError, TypeError, ValueError):
        raise NativeChartRPCUnavailable('NATIVE_CHART_RPC_SDK_UNVERIFIED') from None

    def rpc(resource, method, deadline, *args, **kwargs):
        now = monotonic()
        _need(type(now) in (int, float) and math.isfinite(now)
            and type(deadline) in (int, float) and math.isfinite(deadline) and deadline > now
            and type(method) is str and method in required)
        expected_type, expected_impl = (BrowserContext, ImplContext) if method == 'new_page' else (Page, ImplPage)
        _need(type(resource) is expected_type and type(resource._impl_obj) is expected_impl
            and callable(resource._sync))
        remaining = min(10.0, deadline-now)
        timeout_ms = max(1, min(10_000, int(remaining*1000)))
        native = {}
        if method == 'new_page':
            _need(not args and not kwargs)
        elif method == 'set_viewport_size':
            _need(len(args) == 1 and type(args[0]) is dict and args[0] == VIEWPORT
                and all(type(value) is int for value in args[0].values()) and not kwargs)
            native = {'viewportSize': dict(VIEWPORT)}
        else:
            _need(type(kwargs.get('timeout')) is int and 0 < kwargs['timeout'] <= 10_000)
            effective_timeout = min(kwargs['timeout'], timeout_ms)
            if method == 'set_content':
                _need(len(args) == 1 and type(args[0]) is str and len(args[0]) <= 3*1024*1024
                    and set(kwargs) == {'wait_until', 'timeout'} and kwargs['wait_until'] == 'load')
                match = re.search(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"', args[0])
                _need(match is not None)
                try:
                    png = base64.b64decode(match[1], validate=True)
                except (ValueError, TypeError):
                    raise NativeChartRPCUnavailable('NATIVE_CHART_RPC_DOCUMENT_UNVERIFIED') from None
                _need(1024 <= len(png) <= 2*1024*1024 and png[:8] == b'\x89PNG\r\n\x1a\n'
                    and struct.unpack('>II', png[16:24]) == (1024, 640) and args[0] == _html(png))
                native = {'html': args[0], 'waitUntil': 'load', 'timeout': effective_timeout}
            elif method == 'wait_for_function':
                _need(len(args) == 1 and args[0] == IMAGE_READY and set(kwargs) == {'timeout'})
                native = {'expression': IMAGE_READY, 'timeout': effective_timeout}
            else:
                _need(not args and set(kwargs) == {'type', 'full_page', 'animations', 'scale', 'timeout'}
                    and kwargs['type'] == 'png' and kwargs['full_page'] is False
                    and kwargs['animations'] == 'disabled' and kwargs['scale'] == 'css')
                native = {'type': 'png', 'fullPage': False, 'animations': 'disabled',
                    'scale': 'css', 'timeout': effective_timeout}
        # Both dispatch and cancellation are bounded, including SDK calls whose
        # public sync method has no timeout parameter (new_page / viewport).
        async def operation():
            return await getattr(resource._impl_obj, method)(**native)
        result = resource._sync(asyncio.wait_for(operation(), timeout=remaining))
        finished = monotonic()
        _need(type(finished) in (int, float) and math.isfinite(finished)
            and now <= finished < deadline)
        return mapping.from_maybe_impl(result)

    rpc.sdk_evidence = {'playwright_version': actual_version,
        'method_parameter_names': shapes, 'scope': 'ISOLATED_200_CANDLE_DATA_RENDER_ONLY'}
    return rpc
