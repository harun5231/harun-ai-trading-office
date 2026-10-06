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
            route.fulfill(content_type=kind,body=body)
        self.page.route('**/*',local);self.page.goto('https://office.test/');self.page.wait_for_function('typeof officeDiagnostics === "function"')
    def tearDown(self):self.page.close()
    def test_eight_avatars_all_menus_portrait_and_read_only_activity(self):
        p=self.page;d=p.evaluate('officeDiagnostics()');self.assertEqual(d['actors'],8);self.assertTrue(d['finite'])
        self.assertEqual(d['collisionViolations'],[])
        for view in ('office','market','trading','reports','staff','settings'):
            p.click('#menuToggle');p.click('[data-view='+view+']');self.assertTrue(p.locator('#infoPanel').is_visible());p.click('#panelClose')
        self.assertFalse(p.evaluate('document.body.scrollWidth>innerWidth'))
        p.evaluate("document.getElementById('neuroapiMenu').textContent='WORKER ONLINE · NEUROAPI_UNCHECKED';document.getElementById('robotMenu').textContent='ROBOT ON · SCREENING'")
        p.wait_for_function("officeDiagnostics().states.find(a=>a.id==='market').active")
        d=p.evaluate('officeDiagnostics()');self.assertTrue(d['finite']);self.assertEqual(d['collisionViolations'],[])
        self.assertTrue(next(a for a in d['states'] if a['id']=='neuro')['active'])
        self.assertFalse(next(a for a in d['states'] if a['id']=='trading')['active'])
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
    def _ticket_robot(self):
        ticket=dict(setup_id='test',symbol='BTCUSDT',side='LONG',status='SETUP_READY',execution_mode='MANUAL_ONLY',submission_enabled=False,review_required=True,margin_mode='ISOLATED',leverage=2,risk_target_usdt='5',risk='5',rr='2',estimated_loss_usdt='5',estimated_profit_usdt='10',entry=dict(order_type='LIMIT',side='BUY',price='100',quantity='2.5',time_in_force='GTC',position_side='BOTH'),take_profit=dict(order_type='TAKE_PROFIT_MARKET',side='SELL',trigger_price='104',working_type='MARK_PRICE',close_position=True),stop_loss=dict(order_type='STOP_MARKET',side='SELL',trigger_price='98',working_type='MARK_PRICE',close_position=True))
        row=dict(id='test',symbol='BTCUSDT',status='SETUP_READY',side='LONG',entry='100',tp='104',sl='98',execution_quantity='2.5',risk_target_usdt='5',risk='5',rr='2',ticket=ticket)
        return dict(mode='DRY_RUN',live_enabled=False,live_execution=False,robot_on=True,risk_target_usdt='5',usdt_wallet_balance='117.25',usdt_available_balance='109.50',running_positions=1,bot_entries_today=0,available_slots=1,manual_exposure=['HYPEUSDT'],bot_status='SETUP_READY',setups=[row])
    def _connect_ticket_worker(self,robot,on_post=None):
        calls=[]
        def worker(route):
            request=route.request;path=request.url.removeprefix('https://worker.test');calls.append((request.method,path,request.post_data,request.headers))
            data=dict(mode='DRY_RUN',live_enabled=False)
            if path.startswith('/robot/'):
                if request.method=='POST' and on_post is not None:
                    error=on_post(path,json.loads(request.post_data),robot)
                    if error:
                        return route.fulfill(status=error,body='rejected',headers={'Access-Control-Allow-Origin':'https://office.test'})
                data=robot
            elif path=='/health':data.update(status='ONLINE')
            elif path=='/neuroapi/status':data.update(status='IDLE',busy=False)
            elif path=='/snapshot':data.update(schema_version=1,source='NEUROAPI_DRY_RUN',status='IDLE',balance=None,trades=[],events=[],pnl_today='0',trades_today=0,active_positions=0,generated_at='2026-01-01T00:00:00Z')
            route.fulfill(content_type='application/json',body=json.dumps(data),headers={'Access-Control-Allow-Origin':'https://office.test','Access-Control-Allow-Headers':'Authorization, Content-Type'})
        p=self.page;p.route('https://worker.test/**',worker)
        p.click('#menuToggle');p.click('#robotMenu');p.fill('#apiOrigin','https://worker.test');p.fill('#apiToken','C'*32);p.click('#apiConnect')
        p.wait_for_function('!document.querySelector("#robotToggle").disabled')
        return calls
    def test_manual_ticket_copy_synthetic_trace_and_no_live_requests(self):
        p=self.page;robot=self._ticket_robot()
        robot['setups'] += [dict(id='hold',symbol='ETHUSDT',status='HOLD',ticket=robot['setups'][0]['ticket']),dict(id='reject',symbol='ADAUSDT',status='USER_REJECTED',ticket=robot['setups'][0]['ticket'])]
        def posted(path,value,robot):
            row=robot['setups'][0]
            if path=='/robot/approval':row['status']=value['decision']
            if path=='/robot/simulation':
                row['simulation']=dict(mode='SIMULATION',real_order_submitted=False,live_execution=False,scenario=value['scenario'],status='CLOSED',filled_quantity='2.5',remaining_quantity='0',canceled_quantity='0',pnl_usdt='10',blocks_next=False,steps=[dict(sequence=1,event='ENTRY_FILLED',state='POSITION_OPEN',model_only=True,fill_confirmed=True,sl_confirmed=False,tp_confirmed=False,detail='<img src=x onerror="window.pwned=true">'),dict(sequence=2,event='TP_FILLED',state='CLOSED',model_only=True,fill_confirmed=True,sl_confirmed=True,tp_confirmed=True,detail='TP filled synthetically')])
        calls=self._connect_ticket_worker(robot,posted)
        p.evaluate('Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:async(text)=>{window.copiedTicket=text;}}})')
        self.assertEqual(p.locator('.robot-ticket-copy').count(),1);self.assertEqual(p.locator('.robot-simulation-run').count(),1)
        p.click('.robot-ticket-copy');p.wait_for_function('document.querySelector(".robot-ticket-feedback").textContent.includes("Tiket disalin")')
        text=p.evaluate('window.copiedTicket')
        for fragment in ('BTCUSDT','LIMIT BUY','Quantity (base asset, bukan USDT): 2.5','TAKE_PROFIT_MARKET SELL','STOP_MARKET SELL','Trigger: 98','MARK_PRICE','Close position: true','Position side: BOTH','Margin: ISOLATED','tidak termasuk biaya'):
            self.assertIn(fragment,text)
        p.locator('#robotCards button').filter(has_text='OK').click();p.wait_for_function('document.querySelector("#robotCards").textContent.includes("APPROVED")')
        self.assertEqual(p.locator('.robot-ticket-copy').count(),1)
        p.select_option('.robot-simulation-scenario','PARTIAL_TP');p.click('.robot-simulation-run')
        p.wait_for_function('document.querySelector(".robot-simulation-trace")?.textContent.includes("TP filled synthetically")')
        trace=p.locator('.robot-simulation-trace').inner_text();self.assertIn('PARTIAL_TP',trace);self.assertIn('PnL model: 10',trace)
        self.assertFalse(p.evaluate('!!window.pwned||!!document.querySelector("#robotCards img")'))
        self.assertFalse(p.evaluate('document.body.scrollWidth>innerWidth'))
        self.assertEqual([(path,json.loads(data)) for method,path,data,_ in calls if method=='POST' and path=='/robot/simulation'],[('/robot/simulation',dict(setup_id='test',scenario='PARTIAL_TP'))])
        self.assertFalse(any('order' in path or 'execute' in path for _,path,_,_ in calls))
        self.assertEqual(p.evaluate('Object.keys(localStorage).length+Object.keys(sessionStorage).length'),0)
        self.assertEqual(p.locator('#apiToken').input_value(),'');self.assertEqual(self.errors,[])
    def test_ticket_copy_and_simulation_errors_are_visible(self):
        p=self.page;robot=self._ticket_robot()
        calls=self._connect_ticket_worker(robot,lambda path,value,robot:503 if path=='/robot/simulation' else None)
        p.evaluate('Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:async()=>{throw Error("clipboard denied")}}})')
        p.click('.robot-ticket-copy');p.wait_for_function('document.querySelector(".robot-ticket-feedback").textContent.includes("clipboard denied")')
        self.assertFalse(p.locator('.robot-ticket-copy').is_disabled())
        self.assertEqual(p.locator('.robot-simulation-scenario option').count(),5)
        p.select_option('.robot-simulation-scenario','PROTECTION_FAILURE');p.click('.robot-simulation-run')
        p.wait_for_function('document.querySelector("#apiNote").textContent.includes("Worker HTTP 503")')
        self.assertFalse(p.locator('.robot-simulation-run').is_disabled())
        self.assertFalse(any('order' in path or 'execute' in path for _,path,_,_ in calls));self.assertEqual(self.errors,[])
    def test_late_simulation_response_after_disconnect_cannot_restore_data(self):
        p=self.page;robot=self._ticket_robot();calls=self._connect_ticket_worker(robot)
        p.evaluate('''(()=>{const original=window.fetch;window.fetch=(url,options)=>String(url).endsWith("/robot/simulation")?new Promise(resolve=>{window.finishSimulation=()=>resolve(new Response(JSON.stringify(window.lateRobot),{status:200,headers:{"Content-Type":"application/json"}}));}):original(url,options);})();''')
        p.evaluate('(robot)=>window.lateRobot=robot',robot)
        p.click('.robot-simulation-run');p.wait_for_function('typeof window.finishSimulation==="function"')
        p.click('#apiDisconnect');self.assertEqual(p.locator('#robotCards').inner_text(),'')
        p.evaluate('window.finishSimulation()');p.wait_for_function('document.querySelector("#robotToggle").disabled')
        self.assertEqual(p.locator('#robotCards').inner_text(),'');self.assertIn('Dashboard terputus',p.locator('#apiNote').inner_text())
        self.assertEqual(p.locator('#apiToken').input_value(),'');self.assertEqual(p.evaluate('Object.keys(localStorage).length+Object.keys(sessionStorage).length'),0)
        self.assertEqual(self.errors,[])
    def test_selected_scenario_survives_account_refresh_and_unknown_exposure(self):
        p=self.page;robot=self._ticket_robot()
        robot['setups'][0]['account_review']=dict(checked_at='2026-10-06T00:00:00Z',available_slots=None,symbol_exposed=None,setup_from_current_day=True)
        self._connect_ticket_worker(robot)
        self.assertIn('BELUM DIKETAHUI',p.locator('.robot-account-review').inner_text());self.assertIn('Slot: ?',p.locator('.robot-account-review').inner_text())
        p.select_option('.robot-simulation-scenario','UNCERTAIN_ENTRY')
        robot['setups'][0]['account_review'].update(checked_at='2026-10-06T00:00:15Z',available_slots=1,symbol_exposed=False)
        p.wait_for_function('document.querySelector(".robot-account-review").textContent.includes("00:00:15Z")')
        self.assertEqual(p.locator('.robot-simulation-scenario').input_value(),'UNCERTAIN_ENTRY');self.assertEqual(self.errors,[])
