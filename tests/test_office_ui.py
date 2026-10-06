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
        for state in ('LONG','SHORT','HOLD','REPLACEMENT_SCREENING','REPLACEMENT_SELECTED','INSUFFICIENT_ACTIONABLE_SETUPS'):
            data['status']=state
            p.locator('#wfFile').set_input_files({'name':'state.json','mimeType':'application/json','buffer':json.dumps(data).encode()})
            p.wait_for_function('(state)=>document.querySelector("#panelContent").textContent.includes(state)',arg=state)
        data['live_enabled']=True;p.locator('#wfFile').set_input_files({'name':'bad.json','mimeType':'application/json','buffer':json.dumps(data).encode()})
        p.wait_for_function('document.querySelector("#panelContent").textContent.includes("tidak sesuai skema")')
        self.assertEqual(self.errors,[])
    def test_robot_mobile_settings_and_approval_cards(self):
        p=self.page;calls=[]
        robot=dict(mode='DRY_RUN',live_enabled=False,live_execution=False,robot_on=False,risk_target_usdt='5',usdt_wallet_balance='117.25',usdt_available_balance='109.50',running_positions=1,bot_entries_today=0,available_slots=1,manual_exposure=['HYPEUSDT'],bot_status='SETUP_READY',setups=[dict(id='test',symbol='BTCUSDT',status='SETUP_READY',side='LONG',entry='100',tp='104',sl='98',execution_quantity='2.5',risk_target_usdt='5',risk='5',rr='2')])
        def worker(route):
            request=route.request;path=request.url.removeprefix('https://worker.test');calls.append((request.method,path,request.post_data))
            data=dict(mode='DRY_RUN',live_enabled=False)
            if path.startswith('/robot/'):
                if request.method=='POST':
                    body=json.loads(request.post_data)
                    if path=='/robot/settings':robot.update(body)
                    if path=='/robot/approval':robot['setups'][0]['status']=body['decision']
                data=robot
            elif path=='/health':data.update(status='ONLINE')
            elif path=='/neuroapi/status':data.update(status='IDLE',busy=False)
            elif path=='/snapshot':data.update(schema_version=1,source='NEUROAPI_DRY_RUN',status='IDLE',balance=None,trades=[],events=[],pnl_today='0',trades_today=0,active_positions=0,generated_at='2026-01-01T00:00:00Z')
            route.fulfill(content_type='application/json',body=json.dumps(data),headers={'Access-Control-Allow-Origin':'https://office.test','Access-Control-Allow-Headers':'Authorization, Content-Type'})
        p.route('https://worker.test/**',worker)
        p.click('#menuToggle');p.click('#robotMenu');self.assertTrue(p.locator('#robotToggle').is_disabled())
        p.fill('#apiOrigin','https://worker.test');p.fill('#apiToken','C'*32);p.click('#apiConnect')
        p.wait_for_function('!document.querySelector("#robotToggle").disabled')
        self.assertIn('HYPEUSDT',p.locator('#robotStatus').inner_text())
        self.assertIn('117.25',p.locator('#robotStatus').inner_text())
        p.wait_for_function('document.querySelectorAll(".stats .stat b")[0].textContent.includes("117.25")')
        self.assertEqual(p.locator('.stats .stat b').nth(3).inner_text(),'1')
        p.click('#robotToggle');p.wait_for_function('document.querySelector("#robotToggle").textContent.includes(": ON")')
        p.fill('#robotRisk','10');p.click('#robotRiskSave');p.wait_for_function('document.querySelector("#robotStatus").textContent.includes("RISK PER SL: 10")')
        p.locator('#robotCards button').filter(has_text='OK').click();p.wait_for_function('document.querySelector("#robotCards").textContent.includes("APPROVED")')
        self.assertFalse(p.evaluate('document.body.scrollWidth>innerWidth'));self.assertEqual(self.errors,[])
        self.assertEqual(sum(m=='POST' and path=='/robot/approval' for m,path,_ in calls),1)
        self.assertFalse(any('order' in path or 'execute' in path for _,path,_ in calls))
