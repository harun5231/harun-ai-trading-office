"""Deadline-bound chart RPCs against the reviewed installed Playwright SDK.

The legacy five-method adapter remains available for historical render tests.
The website adapter admits only fixed public Binance navigation, observed chart
UI actions and screenshots. Neither exposes a general JavaScript executor,
account controls or arbitrary network endpoints.
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


def make_website_rpc(*, expected_playwright_version, monotonic=time.monotonic):
    """Bound the observed public Binance UI actions, without a general executor.

    Reuses the installed SDK and caller-owned isolated context. Its action set
    has no typing, financial endpoint, account login, storage or order control.
    """
    from .binance_website_chart import (OBSERVE, SELECT, DISMISS_TOUR,
        VIEWPORT as website_viewport, url_for)
    _need(type(expected_playwright_version) is str
          and version('playwright') == expected_playwright_version)
    from playwright.sync_api import BrowserContext, Page
    from playwright._impl._browser_context import BrowserContext as ImplContext
    from playwright._impl._page import Page as ImplPage
    from playwright.sync_api._generated import mapping
    methods = {'new_page', 'set_viewport_size', 'goto', 'observe', 'select_timeframe',
               'dismiss_tour', 'hover', 'zoom_out', 'screenshot'}

    def rpc(resource, method, deadline, *args, **kwargs):
        now = monotonic()
        _need(type(now) in (int, float) and math.isfinite(now)
              and type(deadline) in (int, float) and math.isfinite(deadline) and deadline > now
              and method in methods and not kwargs)
        kind, impl_kind = (BrowserContext, ImplContext) if method == 'new_page' else (Page, ImplPage)
        _need(type(resource) is kind and type(resource._impl_obj) is impl_kind)
        remaining = min(10.0, deadline-now)
        impl = resource._impl_obj

        async def action():
            if method == 'new_page':
                _need(not args)
                return await impl.new_page()
            if method == 'set_viewport_size':
                _need(not args)
                return await impl.set_viewport_size(viewportSize=dict(website_viewport))
            if method == 'goto':
                _need(len(args) == 1 and type(args[0]) is str
                      and re.fullmatch(r'[A-Z0-9]{2,24}USDT', args[0]) is not None)
                response = await impl.goto(url_for(args[0]), timeout=max(1, int(remaining*1000)),
                                           waitUntil='domcontentloaded')
                _need(response is not None and response.status == 200)
                return {'status': response.status, 'url': impl.url}
            _need(re.fullmatch(r'https://www\.binance\.com/en/futures/[A-Z0-9]{2,24}USDT', impl.url) is not None)
            if method in ('observe', 'dismiss_tour'):
                _need(not args)
                return await impl.eval_on_selector_all('html', OBSERVE if method == 'observe' else DISMISS_TOUR)
            if method == 'select_timeframe':
                _need(len(args) == 1 and args[0] in ('15m', '1h'))
                return await impl.eval_on_selector_all('html', SELECT, args[0])
            if method in ('hover', 'zoom_out'):
                _need(len(args) == 2 and all(type(v) in (int, float) and math.isfinite(v) for v in args)
                      and 0 <= args[0] < website_viewport['width']
                      and 0 <= args[1] < website_viewport['height'])
                await impl.mouse.move(args[0], args[1])
                if method == 'zoom_out':
                    await impl.mouse.wheel(0, 400)
                await impl.eval_on_selector_all('html',
                    'async roots => { await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))); return true; }')
                return None
            _need(method == 'screenshot' and len(args) == 1 and type(args[0]) is dict)
            observed = await impl.eval_on_selector_all('html', OBSERVE)
            _need(args[0] == observed.get('capture_clip'))
            return await impl.screenshot(type='png', timeout=max(1, int(remaining*1000)),
                                         fullPage=False, clip=args[0], animations='disabled', scale='css')

        result = resource._sync(asyncio.wait_for(action(), timeout=remaining))
        _need(now <= monotonic() < deadline)
        return mapping.from_maybe_impl(result)

    rpc.sdk_evidence = {'playwright_version': expected_playwright_version,
                        'scope': 'OBSERVED_BINANCE_FUTURES_PUBLIC_UI_ONLY'}
    return rpc
