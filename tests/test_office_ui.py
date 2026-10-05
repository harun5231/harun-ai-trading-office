"""Optional local Three.js UI regression only; never connects to a provider."""
import json
import os
from pathlib import Path
import unittest

@unittest.skipUnless(os.getenv('OFFICE_UI_TESTS')=='1','Dev-only UI opt-in')
class OfficeUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage','--use-gl=angle','--use-angle=swiftshader','--no-zygote','--single-process'])
        cls.context=cls.browser.new_context(viewport={'width':390,'height':844})
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.page=self.context.new_page();self.errors=[];self.page.on('pageerror',lambda e:self.errors.append(str(e)))
        def local(route):
            url=route.request.url
            if url.startswith('https://cdn.jsdelivr.net/'):
                path=Path('test-assets')/url.rsplit('/',1)[-1];kind='application/javascript'
                if not path.exists():return route.continue_()
            else:
                path=Path(url.replace('https://office.test/','') or 'index.html');kind='application/javascript' if path.suffix=='.js' else 'text/css' if path.suffix=='.css' else 'application/json' if path.suffix=='.json' else 'text/html'
            if not path.is_file():return route.fulfill(status=404,body='')
            body=path.read_text()
            if path.name=='index.html':body=body.replace('// Read-only diagnostics',"cancelAnimationFrame(raf);window.testAdvance=(seconds)=>{for(let t=0;t<seconds;t+=1/60){elapsed+=1/60;actors.forEach(a=>updateActor(a,1/60,elapsed));}}; // Read-only diagnostics")
            route.fulfill(content_type=kind,body=body)
        self.page.route('**/*',local);self.page.goto('https://office.test/');self.page.wait_for_function('typeof officeDiagnostics === "function"')
    def tearDown(self):self.page.close()
    def test_seven_avatars_all_menus_portrait_and_natural_animation(self):
        p=self.page;d=p.evaluate('officeDiagnostics()');self.assertEqual(d['actors'],7);self.assertTrue(d['finite'])
        for view in ('office','market','trading','reports','staff','settings'):
            p.click('#menuToggle');p.click('[data-view='+view+']');self.assertTrue(p.locator('#infoPanel').is_visible());p.click('#panelClose')
        self.assertFalse(p.evaluate('document.body.scrollWidth>innerWidth'))
        p.evaluate('testAdvance(90)');d=p.evaluate('officeDiagnostics()');self.assertTrue(d['finite']);self.assertTrue(all(a['visits']>=1 for a in d['states'][:6]))
        self.assertEqual(self.errors,[])
    def test_api_panel_and_report_xss_protection(self):
        p=self.page;p.click('#menuToggle');p.click('#neuroapiMenu');self.assertTrue(p.locator('#apiCheck').is_disabled());self.assertTrue(p.locator('#apiRun').is_disabled())
        self.assertNotIn('LOGIN NEUROBRO',p.locator('#panelContent').inner_text())
        p.click('#panelClose');p.click('#menuToggle');p.click('#workflowMenu')
        data={'schema_version':1,'mode':'DRY_RUN','live_enabled':False,'source':'NEUROAPI_DRY_RUN','status':'IDLE','balance':None,'pnl_today':'0','trades_today':0,'active_positions':0,'trades':[],'events':[{'state':'IDLE','at':'2026-01-01T00:00:00Z','agent':'Coordinator','message':'<img src=x onerror="window.pwned=true">'}],'generated_at':'2026-01-01T00:00:00Z'}
        p.locator('#wfFile').set_input_files({'name':'snapshot.json','mimeType':'application/json','buffer':json.dumps(data).encode()})
        self.assertFalse(p.evaluate('!!window.pwned||!!document.querySelector("#panelContent img")'))
        data['live_enabled']=True;p.locator('#wfFile').set_input_files({'name':'bad.json','mimeType':'application/json','buffer':json.dumps(data).encode()})
        p.wait_for_function('document.querySelector("#panelContent").textContent.includes("tidak sesuai skema")')
        self.assertEqual(self.errors,[])
