"""Retained public Binance labels and isolated DOM fixtures; no live requests."""
import copy
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import time
import unittest

from agent.binance_website_chart import (OBSERVE, VOLUME_ONLY, VOLUME_CONTROL,
    WebsiteChartUnavailable, canonical, observation, volume_only)
from agent.native_chart_rpc import make_website_rpc

FIXTURE=Path(__file__).parent/'fixtures/binance_volume_public_header_20261126.json'
CHROMIUM=Path(os.environ.get('HARUN_TEST_CHROMIUM_EXECUTABLE','/usr/bin/chromium'))
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT=True
except ImportError:
    HAS_PLAYWRIGHT=False


class RetainedVolumeReadinessTests(unittest.TestCase):
    def test_exact_retained_ui_has_volume_but_legacy_observer_never_reaches_configuration(self):
        saved=json.loads(FIXTURE.read_text());ui=saved['ui_observation']
        self.assertEqual(hashlib.sha256(canonical(ui)).hexdigest(),saved['ui_observation_sha256'])
        self.assertIs(observation(ui,'WIFUSDT','15m'),ui)
        self.assertEqual(ui['indicators'],[{'id':'@MA','visible':True}])
        self.assertEqual(ui['header_rows'][2].splitlines(),
            ['Vol(WIF)','721.535K','Vol(USDT)','96.042K','704.836K','983.181K'])
        self.assertFalse(any(row['id']=='@VOL' for row in ui['indicators']))
        with self.assertRaises(WebsiteChartUnavailable):volume_only(ui)


# The DOM models only public labels/controls from the retained observation and
# the already supported volume settings contract. It is never a chart source.
HTML=r'''<!doctype html><html><head><meta charset="utf-8"><title>0.1337 | WIFUSDT USDⓈ-Margined Perpetual Chart | Binance Futures</title>
<style>
body{margin:0;background:#112;color:white}.chart-widget-root{width:1754px;height:1050px}
.chart-title-row{position:relative;width:1700px;white-space:pre-line;min-height:30px}
.indicator-title-toolbox{display:none;position:absolute;right:10px;top:0}
.chart-title-row:hover .indicator-title-toolbox{display:block}
canvas.scene{display:block;width:1754px;height:697px}button{width:60px;height:24px}
[role=dialog]{position:fixed;left:700px;top:300px;width:300px;background:#555;z-index:10}
[role=checkbox]{padding:10px;cursor:pointer}
</style></head><body><span>0.1337</span><div class="chart-widget-root">
<span>Original</span><div id="15m" class="text-PrimaryText">15m</div><div id="1h">1H</div>
<div class="chart-title-row">2026/11/26 14:00<br>Open<br>0.1331<br>Close<br>0.1337</div>
<div class="chart-title-row" id="ma-row"><div class="chart-title-indicator-container" key="@MA">
<span class="default-label-box">MA(7) 0.1325 MA(25) 0.1326 MA(99) 0.1273</span>
<div class="indicator-title-toolbox"><button tooltip="Close" onclick="document.querySelector('#ma-row').remove()">Close</button></div>
</div></div>
<div class="chart-title-row" id="volume-row"><div class="chart-title-indicator-container--sub" id="volume-content"></div></div>
<canvas class="scene" width="1754" height="697"></canvas></div>
<script>
window.averages=[true,true];window.unknownExtra=[];
window.renderVolume=()=>{
 const lines=['Vol(WIF)','721.535K','Vol(USDT)','96.042K'];
 if(averages[0])lines.push('704.836K');if(averages[1])lines.push('983.181K');lines.push(...unknownExtra);
 document.querySelector('#volume-content').innerHTML=lines.map(x=>'<div>'+x+'</div>').join('')+
 '<div class="indicator-title-toolbox"><button tooltip="Settings" onclick="openVolumeSettings()">Settings</button></div>';
};
window.openVolumeSettings=()=>{
 const d=document.createElement('div');d.setAttribute('role','dialog');d.setAttribute('aria-label','modal');
 d.innerHTML='<span>Vol - Volume</span>'+['MAVOL1','MAVOL2'].map((x,i)=>'<div role="checkbox" aria-checked="'+averages[i]+'" onclick="this.setAttribute(\'aria-checked\',this.getAttribute(\'aria-checked\')===\'true\'?\'false\':\'true\')">'+x+'</div>').join('')+
 '<button onclick="averages=Array.from(this.parentNode.querySelectorAll(\'[role=checkbox]\')).map(e=>e.getAttribute(\'aria-checked\')===\'true\');this.parentNode.remove();renderVolume()">Save</button>';
 document.body.append(d);
};
renderVolume();
const c=document.querySelector('canvas').getContext('2d');
for(let x=0;x<1754;x+=10){c.fillStyle='rgb('+(x%251)+',100,'+(x%241)+')';c.fillRect(x,0,10,697)}
</script></body></html>'''


@unittest.skipUnless(HAS_PLAYWRIGHT and CHROMIUM.is_absolute() and CHROMIUM.is_file(),
    'A local Chromium executable and Playwright are required for this isolated DOM test')
class PublicVolumeDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager=sync_playwright().start()
        try:
            cls.browser=cls.manager.chromium.launch(executable_path=str(CHROMIUM),chromium_sandbox=True,
                headless=True,args=['--disable-dev-shm-usage','--disable-background-networking','--disable-component-update'])
        except BaseException:
            cls.manager.stop();raise
        cls.sdk_version=version('playwright')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.manager.stop()

    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':2560,'height':1440},timezone_id='UTC',
            device_scale_factor=1,service_workers='block')
        self.addCleanup(self.context.close);self.requests=[]
        def route(request):
            self.requests.append(request.request.url)
            if request.request.url=='https://www.binance.com/en/futures/WIFUSDT':
                request.fulfill(status=200,content_type='text/html',body=HTML)
            else:request.abort()
        self.context.route('**/*',route);self.page=self.context.new_page()
        self.page.goto('https://www.binance.com/en/futures/WIFUSDT')
        self.rpc=make_website_rpc(expected_playwright_version=self.sdk_version)

    def observed(self):return self.page.locator('html').evaluate_all(OBSERVE)

    def test_real_observer_recognizes_unkeyed_public_volume_and_exposes_displayed_averages(self):
        value=self.observed();observation(value,'WIFUSDT','15m')
        self.assertEqual(value['indicators'],[{'id':'@MA','visible':True},{'id':'@VOL','visible':True}])
        self.assertEqual(value['volume_average_series'],['PUBLIC_VOLUME_EXTRA_VALUE_1','PUBLIC_VOLUME_EXTRA_VALUE_2'])
        self.assertEqual(self.page.locator('html').evaluate_all(VOLUME_ONLY),{'ma_rows':1,'volume_rows':1})
        with self.assertRaises(WebsiteChartUnavailable):volume_only(value)

    def test_actual_configure_action_removes_ma_and_disables_both_public_mavol_controls(self):
        self.assertTrue(self.rpc(self.page,'configure_volume_only',time.monotonic()+10))
        value=self.observed();self.assertEqual(volume_only(value)['policy'],'CANDLES_AND_VOLUME_ONLY')
        self.assertEqual(self.page.evaluate('averages'),[False,False])
        self.assertEqual(self.page.locator('#ma-row').count(),0)
        self.assertEqual(self.requests,['https://www.binance.com/en/futures/WIFUSDT'])

    def test_public_volume_identity_does_not_depend_on_an_internal_indicator_key(self):
        for key in ('@VOL','fixture-unrecognized-key'):
            with self.subTest(key=key):
                self.page.reload()
                self.page.evaluate("key=>{const e=document.querySelector('#volume-content');e.classList.add('chart-title-indicator-container');e.setAttribute('key',key)}",key)
                value=self.observed()
                self.assertEqual(value['indicators'],[{'id':'@MA','visible':True},{'id':'@VOL','visible':True}])
                self.rpc(self.page,'configure_volume_only',time.monotonic()+10)
                volume_only(self.observed())

    def test_hidden_foreign_or_duplicate_volume_never_proves_unique_owned_volume(self):
        scripts=["document.querySelector('#volume-row').style.display='none'",
            "document.querySelector('#volume-content').innerHTML=document.querySelector('#volume-content').innerHTML.replace('Vol(WIF)','Vol(ETH)')",
            "document.querySelector('#volume-row').after(document.querySelector('#volume-row').cloneNode(true))"]
        for script in scripts:
            with self.subTest(script=script):
                self.page.reload();self.page.evaluate(script)
                value=self.observed()
                self.assertFalse(any(row['id']=='@VOL' and row['visible'] for row in value['indicators']))
                with self.assertRaises(WebsiteChartUnavailable):volume_only(value)

    def test_unknown_unkeyed_indicator_or_extra_volume_value_cannot_be_hidden_by_fallback(self):
        self.rpc(self.page,'configure_volume_only',time.monotonic()+10)
        self.page.evaluate("document.querySelector('#volume-row').insertAdjacentHTML('afterend','<div class=\"chart-title-row\">RSI(14)<br>50</div>')")
        self.assertIn({'id':None,'visible':True},self.observed()['indicators'])
        with self.assertRaises(WebsiteChartUnavailable):volume_only(self.observed())
        self.page.reload();self.page.evaluate("averages=[false,false];unknownExtra=['123.456K'];renderVolume()")
        self.rpc(self.page,'configure_volume_only',time.monotonic()+10)
        with self.assertRaises(WebsiteChartUnavailable):volume_only(self.observed())


if __name__=='__main__':unittest.main()
