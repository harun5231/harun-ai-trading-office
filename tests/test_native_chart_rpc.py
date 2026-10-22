"""Exercise real sandboxed local Chromium with offline public-data fixtures only."""
import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent.native_chart_capture import IMAGE_READY, SIDECAR
from agent.native_chart_rpc import NativeChartRPCUnavailable, make_rpc

if __package__:
    from .test_native_chart_capture import Harness, png_pixels
    from .test_agent_closed_candles_200 import NOW, native_context_200
else:
    from test_native_chart_capture import Harness, png_pixels
    from test_agent_closed_candles_200 import NOW, native_context_200

try:
    from playwright.sync_api import BrowserContext, JSHandle, Page, sync_playwright
    from playwright._impl._page import Page as ImplPage
    HAS_PLAYWRIGHT=True
except ImportError:
    HAS_PLAYWRIGHT=False


CHROMIUM = Path(os.environ.get('HARUN_TEST_CHROMIUM_EXECUTABLE', '/usr/bin/chromium'))


@unittest.skipUnless(HAS_PLAYWRIGHT and CHROMIUM.is_absolute() and CHROMIUM.is_file(),
    'A local Chromium executable and Playwright are required for this browser integration test')
class NativeChartRPCBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager=sync_playwright().start()
        try:
            cls.browser=cls.manager.chromium.launch(executable_path=str(CHROMIUM),
                chromium_sandbox=True,headless=True,args=['--disable-dev-shm-usage',
                    '--disable-background-networking','--disable-component-update'])
        except BaseException:
            cls.manager.stop()
            raise
        cls.sdk_version=version('playwright')
    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.manager.stop()
    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':1024,'height':640},
            device_scale_factor=1,service_workers='block')
        self.addCleanup(self.context.close)
        self.requests=[]
        self.context.on('request',lambda request:self.requests.append(request.url))
        self.context.route('**/*',lambda route:route.abort())
        self.rpc=make_rpc(expected_playwright_version=self.sdk_version)
    def new_page(self):
        return self.rpc(self.context,'new_page',time.monotonic()+3)
    def test_real_two_screenshots_match_200_closed_data_pixels_and_preserve_native_receipts(self):
        harness=Harness()
        closed=[]
        def close_resource(page):
            if type(page) is not Page or page.context is not self.context:
                raise AssertionError('not our isolated page')
            async def close():
                return await page._impl_obj.close()
            page._sync(asyncio.wait_for(close(),timeout=5))
            closed.append(page)
        harness.native['close_resource']=close_resource
        capture=harness.install(rpc=self.rpc,monotonic=time.monotonic,wall_clock=lambda:NOW)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'archive'
            result=capture(self.context,native_context_200(),'offline_chart_test','a'*64,
                str(root),deadline=time.monotonic()+30)
            self.assertEqual(set(result),{'reviewed_images_key','evidence'})
            self.assertEqual([row['timeframe'] for row in result['evidence']],['15m','1h'])
            for row in result['evidence']:
                self.assertEqual(set(row),{'source','symbol','timeframe','captured_at','sha256','bytes'})
            for path in result['reviewed_images_key']:
                decoded=png_pixels(Path(path).read_bytes())
                self.assertEqual(decoded[:2],(1024,640))
            sidecar=json.loads((Path(result['reviewed_images_key'][0]).parent/SIDECAR).read_text())
            for timeframe in ('15m','1h'):
                self.assertEqual(len(sidecar['public_market_context']['candles'][timeframe]),200)
                self.assertEqual(sidecar['charts'][timeframe]['provenance']['closed_candle_count'],200)
                self.assertTrue(sidecar['charts'][timeframe]['browser_screenshot_of_data_render'])
                self.assertFalse(sidecar['charts'][timeframe]['exchange_website_screenshot'])
                # Chromium encodes its own PNG; source metadata is in the
                # supplemental manifest, while verified decoded pixels match.
                self.assertNotEqual(sidecar['charts'][timeframe]['rendered_png_sha256'],
                                    sidecar['charts'][timeframe]['screenshot_png_sha256'])
            self.assertFalse(sidecar['network_navigation_performed'])
        self.assertEqual(len(closed),1)
        self.assertEqual(self.context.pages,[])
        self.assertTrue(self.browser.is_connected())
        self.assertFalse(any(url.startswith(('https:','http:','ws:','wss:')) for url in self.requests))
        self.assertEqual(self.rpc.sdk_evidence['playwright_version'],self.sdk_version)
        self.assertEqual(set(self.rpc.sdk_evidence['method_parameter_names']),
            {'new_page','set_viewport_size','set_content','wait_for_function','screenshot'})
    def test_new_page_and_javascript_handle_are_mapped_to_actual_sync_api_types(self):
        page=self.new_page()
        self.assertIs(type(page),Page)
        self.assertIs(page.context,self.context)
        # A valid image document comes from the actual capture renderer. Direct
        # arbitrary HTML and arbitrary JavaScript predicates remain unavailable.
        from agent.charts import render_chart_png
        from agent.native_chart_capture import _html
        from agent.native_market_context import convert_native_market_context
        png=render_chart_png(convert_native_market_context(native_context_200(),now=NOW),'15m')
        self.rpc(page,'set_viewport_size',time.monotonic()+3,{'width':1024,'height':640})
        self.rpc(page,'set_content',time.monotonic()+3,_html(png),wait_until='load',timeout=2000)
        handle=self.rpc(page,'wait_for_function',time.monotonic()+3,IMAGE_READY,timeout=2000)
        self.assertIs(type(handle),JSHandle)
        self.assertTrue(handle.json_value())
    def test_arbitrary_methods_resources_scripts_and_screenshot_paths_are_rejected(self):
        page=self.new_page()
        cases=[(page,'goto',('https://example.invalid',),{}),
            (object(),'new_page',(),{}),
            (page,'set_content',('<script>alert(1)</script>',),{'wait_until':'load','timeout':100}),
            (page,'wait_for_function',('() => fetch("https://example.invalid")',),{'timeout':100}),
            (page,'screenshot',(),{'type':'png','full_page':False,'animations':'disabled','scale':'css','timeout':100,'path':'/tmp/forbidden.png'}),
            (page,'screenshot',(),{'type':'png','full_page':False,'animations':'disabled','scale':'css','timeout':100,'mask':[]}),
            (page,'set_viewport_size',({'width':100000,'height':640},),{})]
        for resource,method,args,kwargs in cases:
            with self.subTest(method=method,keys=list(kwargs)),self.assertRaises(NativeChartRPCUnavailable):
                self.rpc(resource,method,time.monotonic()+3,*args,**kwargs)
        self.assertFalse(self.requests)
    def test_out_of_budget_or_nonfinite_deadline_performs_no_new_page(self):
        for deadline in (time.monotonic()-1,float('nan'),float('inf'),True):
            with self.subTest(deadline=str(deadline)),self.assertRaises(NativeChartRPCUnavailable):
                self.rpc(self.context,'new_page',deadline)
        self.assertEqual(self.context.pages,[])
    def test_hanging_private_rpc_is_cancelled_by_effective_timeout(self):
        page=self.new_page()
        cancelled=[]
        async def hung(*args,**kwargs):
            try:await asyncio.Event().wait()
            finally:cancelled.append(True)
        started=time.monotonic()
        with patch.object(ImplPage,'wait_for_function',hung):
            with self.assertRaises(TimeoutError):
                self.rpc(page,'wait_for_function',started+.075,IMAGE_READY,timeout=10000)
        self.assertLess(time.monotonic()-started,1.5)
        self.assertEqual(cancelled,[True])
    def test_version_mismatch_and_actual_missing_sdk_parameters_refuse_factory(self):
        with self.assertRaises(NativeChartRPCUnavailable):
            make_rpc(expected_playwright_version='0.0.0')
        async def missing_parameters(self,type=None):pass
        with patch.object(ImplPage,'screenshot',missing_parameters):
            with self.assertRaises(NativeChartRPCUnavailable):
                make_rpc(expected_playwright_version=self.sdk_version)


if __name__=='__main__':
    unittest.main()
