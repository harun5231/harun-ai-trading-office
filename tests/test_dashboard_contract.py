"""Dashboard transport contracts in a local DOM harness; no provider or browser access."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MenuParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.views = []; self.scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'button' and 'data-view' in attrs:
            self.views.append(attrs['data-view'])
        if tag == 'script' and 'src' in attrs:
            self.scripts.append(attrs['src'])


HARNESS = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Node {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.parent=null;this.dataset={};this.events={};this.attributes={};this._text='';this.className='';this.hidden=false;this.disabled=false;this.value='';this.id='';this.classList={toggle:(name,on)=>{const classes=new Set(this.className.split(/\s+/).filter(Boolean));if(on)classes.add(name);else classes.delete(name);this.className=[...classes].join(' ');}};}
  set textContent(text){this._text=String(text);this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  append(...nodes){for(const node of nodes){this.children.push(node);node.parent=this;}}
  replaceChildren(...nodes){this.children=[];this._text='';this.append(...nodes);}
  setAttribute(name,value){this.attributes[name]=String(value);}
  removeAttribute(name){delete this.attributes[name];}
  addEventListener(name,fn){(this.events[name]??=[]).push(fn);}
  focus(){document.activeElement=this;}
  click(event={defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}}){if(this.disabled)return event;document.activeElement=this;this.onclick?.(event);for(const fn of this.events.click??[])fn(event);return event;}
  querySelector(selector){return this.querySelectorAll(selector)[0]??null;}
  querySelectorAll(selector){const matches=node=>selector==='[data-view]'?!!node.dataset.view:selector.startsWith('#')?node.id===selector.slice(1):selector.startsWith('.')?node.className.split(/\s+/).includes(selector.slice(1)):node.tagName.toLowerCase()===selector;return walk(this).slice(1).filter(matches);}
}
const walk=node=>[node,...node.children.flatMap(walk)];
const root=new Node('body');
const make=(tag,id='',className='')=>{const node=new Node(tag);node.id=id;node.className=className;return node;};
const panel=make('div','infoPanel'),content=make('div','panelContent'),title=make('h3','panelTitle'),drawer=make('div','menuDrawer'),toggle=make('button','menuToggle'),close=make('button','panelClose');panel.hidden=true;drawer.hidden=true;panel.append(title,close,content);root.append(panel,drawer,toggle);
for(const view of ['robot','calendar','neurobro']){const button=make('button',view==='robot'?'robotMenu':view==='neurobro'?'neurobroMenu':'','menu-item');button.dataset.view=view;button.textContent=view;if(view==='neurobro')button.append(make('small','neurobroMenuStatus'));drawer.append(button);}
const brand=make('div','','brand'),brandSmall=new Node('small');brand.append(brandSmall);root.append(brand);
const stats=make('div','','stats');for(let i=0;i<4;i++){const stat=make('div','','stat');stat.append(new Node('span'),new Node('b'));stats.append(stat);}root.append(stats);
const listeners={},snapshots=[],intervals=new Map(),timeouts=new Map(),calls=[];let intervalId=0,timeoutId=0;
const document={baseURI:'https://office.test/',currentScript:{src:'https://office.test/assets/dashboard.js'},activeElement:null,hidden:false,createElement:tag=>new Node(tag),getElementById:id=>walk(root).find(node=>node.id===id)??null,querySelector:selector=>selector==='.brand small'?brandSmall:root.querySelector(selector),querySelectorAll:selector=>selector==='.stats .stat b'?stats.children.map(node=>node.children[1]):selector==='.stats .stat span'?stats.children.map(node=>node.children[0]):root.querySelectorAll(selector),addEventListener:(name,fn)=>{(listeners[name]??=[]).push(fn);}};
const window={dispatchEvent:event=>{snapshots.push(event.detail);for(const fn of listeners[event.type]??[])fn(event);},addEventListener:(name,fn)=>{(listeners[name]??=[]).push(fn);},removeEventListener:(name,fn)=>{listeners[name]=(listeners[name]??[]).filter(item=>item!==fn);},open:()=>{throw Error('Login must use an ordinary user-clicked anchor');}};
let clock=Date.parse('2026-10-07T13:40:00Z');
const ClockDate=class extends Date{constructor(...args){super(...(args.length?args:[clock]));}static now(){return clock;}};
globalThis.Date=ClockDate;
const now=()=>new ClockDate().toISOString();
const data={schema_version:2,source:'BINANCE_FUTURES',generated_at:now(),robot:{robot_on:false,bot_status:'OFF',checked_at:now(),risk_target_usdt:'5',running_positions:1,available_slots:1,manual_exposure:['HYPEUSDT'],bot_entries_today:0,execution_gateway:{connected:false,status:'NOT_CONNECTED',failure_code:'BINANCE_ORDER_GATEWAY_NOT_CONNECTED'}},account:{status:'CONNECTED',checked_at:now(),usdt_wallet_balance:'116.928',usdt_available_balance:'105.25',active_positions:1,positions:[{symbol:'HYPEUSDT',side:'LONG',position_side:'BOTH',quantity:'1.5',entry_price:'28',mark_price:'29',unrealized_pnl:'1.5'}]},reports:{status:'AVAILABLE',checked_at:now(),period_start:now(),period_end:now(),pnl_today_usdt:'3.75',realized_pnl_today_usdt:'4',commission_today_usdt:'-0.25',funding_today_usdt:'0',trades_today:2,complete:true},position_history:{status:'AVAILABLE',kind:'BINANCE_FILLS',checked_at:now(),period_start:now(),period_end:now(),items:[{id:'fill-1',symbol:'BTCUSDT',order_id:'order-1',side:'SELL',position_side:'BOTH',quantity:'0.001',price:'64000',realized_pnl:'4',commission:'0.25',commission_asset:'USDT',time:now()}],complete:true},employees:[{id:'neuro',name:'NeuroAPI Analyst',status:'WAITING'}],activity:[{at:now(),state:'SCREENING',agent:'Coordinator',message:'<img src=x onerror="window.pwned=true">'}]};
data.pnl_calendar={source:'BINANCE_FUTURES',kind:'CLOSED_POSITIONS_FROM_FILLS',status:'PARTIAL',checked_at:now(),timezone:'Asia/Jakarta',start_date:'2026-10-01',end_date:'2026-10-07',complete:false,incomplete_reasons:['SYMBOL_DISCOVERY_PARTIAL'],days:Array.from({length:7},(_,index)=>({date:'2026-10-0'+(index+1),pnl_usdt:null,known_pnl_usdt:index===6?'-6.51598280':'0',closed_positions:index===6?1:0,complete:false})),positions:[{id:'ETH-1',symbol:'ETHUSDT',side:'LONG',position_side:'BOTH',status:'CLOSED',opened_at:'2026-10-07T08:18:23.715Z',closed_at:'2026-10-07T10:07:39.052Z',close_date:'2026-10-07',entry_price:'2610',exit_price:'2575.81',closed_quantity:'0.181',realized_pnl_usdt:'-6.18839000',commission_usdt:'0.32759280',funding_usdt:'0',insurance_usdt:'0',known_pnl_usdt:'-6.51598280',pnl_usdt:'-6.51598280',complete:true}],funding_usdt:'0'};
data.research={provider:'NEUROBRO_WEB',status:'LOGIN_REQUIRED',checked_at:now(),profile_persistent:true};
let responseOverride=null;
const response=value=>({ok:true,status:200,json:async()=>structuredClone(value)});
async function fetch(url,options={}){url=String(url);calls.push({url,...options});if(url.endsWith('/worker-config.json'))return response({worker_origin:''});if(responseOverride){const handled=responseOverride(url,options);if(handled)return handled;}if(url.endsWith('/office/status'))return response(data);if(url.endsWith('/robot/settings')){const value=JSON.parse(options.body);Object.assign(data.robot,value);if(Object.hasOwn(value,'robot_on'))data.robot.bot_status=value.robot_on?'WAITING':'OFF';return response(data.robot);}throw Error('Unexpected route: '+url);}
vm.runInNewContext(fs.readFileSync('assets/dashboard.js','utf8'),{document,window,location:{protocol:'https:',origin:'https://office.test'},fetch,URL,Date:ClockDate,performance:{now:()=>clock},AbortSignal,CustomEvent:class{constructor(type,{detail}){this.type=type;this.detail=detail;}},setInterval:fn=>{intervals.set(++intervalId,fn);return intervalId;},clearInterval:id=>intervals.delete(id),setTimeout:(fn,delay)=>{timeouts.set(++timeoutId,{fn,delay});return timeoutId;},clearTimeout:id=>timeouts.delete(id)});
const flush=async()=>{for(let i=0;i<12;i++)await new Promise(resolve=>setImmediate(resolve));};
const open=view=>drawer.children.find(button=>button.dataset.view===view).click();
const connect=async()=>{open('robot');document.getElementById('apiOrigin').value='https://worker.test';document.getElementById('apiToken').value='C'.repeat(32);document.getElementById('apiConnect').click();await flush();};
const statValues=()=>stats.children.map(stat=>stat.children[1].textContent);
"""


class DashboardContract(unittest.TestCase):
    def test_robot_calendar_and_neurobro_menus_and_one_dashboard_entry(self):
        parser = MenuParser(); parser.feed((ROOT / 'index.html').read_text())
        self.assertEqual(parser.views, ['robot', 'calendar', 'neurobro'])
        scripts = [urlsplit(src).path for src in parser.scripts]
        self.assertEqual(scripts.count('assets/dashboard.js'), 1)
        self.assertIn('assets/office3d/renderer.js', scripts)
        self.assertNotIn('assets/workflow.js', scripts)
        self.assertNotIn('assets/neuroapi.js', scripts)
        self.assertFalse((ROOT / 'assets/workflow.js').exists())
        self.assertFalse((ROOT / 'assets/neuroapi.js').exists())

    def test_no_initial_account_fixtures_or_removed_actions(self):
        text = (ROOT / 'index.html').read_text() + (ROOT / 'assets/dashboard.js').read_text()
        for removed in ('12,430.85', '+82.40', 'SALIN TIKET', 'UJI SIMULASI', 'CEK API', 'JALANKAN DRY RUN', '/robot/approval', '/robot/simulation', 'localStorage', 'sessionStorage', 'document.cookie', 'innerHTML'):
            self.assertNotIn(removed, (ROOT / 'assets/dashboard.js').read_text() if removed == 'innerHTML' else text)

    def run_dom(self, assertions):
        if shutil.which('node') is None:
            self.skipTest('Node.js is required for the local dashboard DOM harness')
        script = HARNESS + '\n(async()=>{\n' + assertions + '\n})().catch(error=>{console.error(error);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script], cwd=ROOT, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_account_connection_switch_and_read_only_sections(self):
        self.run_dom(r"""
assert.deepEqual(statValues(),['—','—','—','—']);open('robot');assert.equal(document.getElementById('robotToggle').disabled,true);assert.match(content.textContent,/Status gateway belum tersedia dari worker/);assert.doesNotMatch(content.textContent,/BINANCE_ORDER_GATEWAY_NOT_CONNECTED/);
await connect();assert.equal(document.getElementById('apiToken').value,'');assert.equal(statValues()[0],'116.93');assert.equal(statValues()[3],'1');assert.match(content.textContent,/HYPEUSDT/);assert.match(content.textContent,/Transport pengiriman order Binance belum tersedia/);assert.match(content.textContent,/Worker belum dapat mengirim order ke Binance/);assert.doesNotMatch(content.textContent,/Eksekusi Binance diblokir/);
document.getElementById('robotToggle').click();await flush();assert.match(document.getElementById('robotToggle').textContent,/: ON/);
const writes=calls.filter(call=>call.method==='POST');assert.equal(writes.length,1);assert.equal(new URL(writes[0].url).pathname,'/robot/settings');assert.deepEqual(JSON.parse(writes[0].body),{robot_on:true});
open('calendar');assert.match(content.textContent,/PNL Calendar/);assert.match(content.textContent,/ETHUSDT/);assert.match(content.textContent,/−6,52/);assert.match(content.textContent,/Ditutup/);assert.match(content.textContent,/0,181/);assert.equal(walk(content).some(node=>node.tagName==='IMG'),false);
assert.equal(calls.some(call=>/\/order|\/approval|\/simulation/.test(call.url)),false);assert.equal(snapshots.at(-1).balance,'116.928');
""")

    def test_missing_or_stale_statistics_remain_unknown(self):
        self.run_dom(r"""
data.account.status='UNAVAILABLE';data.account.usdt_wallet_balance=null;data.account.active_positions=null;data.reports.status='UNAVAILABLE';data.reports.pnl_today_usdt=null;data.reports.trades_today=null;await connect();assert.deepEqual(statValues(),['—','—','—','—']);
data.account.status='CONNECTED';data.account.checked_at='2020-01-01T00:00:00Z';data.account.usdt_wallet_balance='116.928';[...intervals.values()][0]();await flush();assert.equal(statValues()[0],'—');assert.match(content.textContent,/Data terakhir sudah lama/);assert.equal(snapshots.at(-1).balance,null);
""")

    def test_late_poll_cannot_restore_disconnected_account_or_token(self):
        self.run_dom(r"""
await connect();let finish;responseOverride=url=>url.endsWith('/office/status')?new Promise(resolve=>{finish=()=>resolve(response(data));}):null;[...intervals.values()][0]();await flush();document.getElementById('apiDisconnect').click();finish();await flush();assert.deepEqual(statValues(),['—','—','—','—']);assert.equal(document.getElementById('robotToggle').disabled,true);assert.equal(document.getElementById('apiToken').value,'');assert.equal(snapshots.at(-1).status,'OFFLINE');assert.equal(intervals.size,0);
""")

    def test_partial_reports_and_calendar_are_explicit(self):
        self.run_dom(r"""
data.reports.status='PARTIAL';data.reports.complete=false;data.reports.trades_today=null;await connect();assert.equal(statValues()[2],'—');assert.equal(stats.children[1].children[0].textContent,'PNL Hari Ini *');assert.match(stats.children[1].children[1].title,/parsial/);open('calendar');assert.match(content.textContent,/Riwayat parsial/);assert.match(document.getElementById('pnl-day-2026-10-07').textContent,/−6,52\*/);assert.match(document.getElementById('pnl-day-2026-10-01').textContent,/—/);assert.doesNotMatch(document.getElementById('pnl-day-2026-10-01').textContent,/0,00/);
""")

    def test_dashboard_flat_event_connects_to_read_only_avatar_bridge(self):
        self.run_dom(r"""
globalThis.window=window;const {createStatusController}=await import('./assets/office3d/status-controller.js');const bridge=createStatusController();data.robot.last_decision={symbol:'ETHUSDT',status:'EXECUTION_BLOCKED',failure_code:'BINANCE_ORDER_GATEWAY_NOT_CONNECTED'};await connect();
const event=snapshots.at(-1);assert.equal(event.robot_checked_at,data.robot.checked_at);assert.equal(event.account_checked_at,data.account.checked_at);assert.equal(event.reports_checked_at,data.reports.checked_at);assert.equal(event.risk_target_usdt,'5');assert.equal(event.available_slots,1);assert.equal(JSON.stringify(event.manual_exposure),JSON.stringify(['HYPEUSDT']));
assert.equal(bridge.getTelemetry().balance,'116.928');assert.equal(bridge.getTelemetry().last_decision.symbol,'ETHUSDT');assert.equal(bridge.getRole('position').active,false);assert.equal(bridge.getTelemetry().active_positions,1);assert.equal(bridge.getRole('market').active,false);assert.equal(bridge.getRole('trading').active,false);
document.getElementById('apiDisconnect').click();assert.equal(bridge.getTelemetry().balance,null);assert.equal(bridge.getRole('position').active,false);bridge.dispose();
""")

    def test_post_refresh_wins_over_inflight_older_poll(self):
        self.run_dom(r"""
await connect();const previous=structuredClone(data);let finish;let intercepted=false;responseOverride=url=>url.endsWith('/office/status')&&!intercepted?(intercepted=true,new Promise(resolve=>{finish=()=>resolve(response(previous));})):null;[...intervals.values()][0]();await flush();document.getElementById('robotToggle').click();await flush();finish();await flush();assert.match(document.getElementById('robotToggle').textContent,/: ON/);assert.equal(calls.filter(call=>call.url.endsWith('/office/status')).length,3);assert.equal(snapshots.at(-1).robot_on,true);
""")

    def test_risk_settings_preserve_decimal_string_and_only_affect_new_analysis(self):
        self.run_dom(r"""
open('robot');assert.equal(document.getElementById('robotRisk').value,'5');assert.equal(document.getElementById('robotRisk').disabled,true);await connect();assert.equal(document.getElementById('robotRisk').value,'5');
const risk=document.getElementById('robotRisk');risk.value='7.50000000000000001';risk.oninput();document.getElementById('robotRiskSave').click();assert.equal(document.getElementById('robotRiskSave').disabled,true);await flush();
const writes=calls.filter(call=>call.method==='POST');assert.equal(writes.length,1);assert.equal(new URL(writes[0].url).pathname,'/robot/settings');assert.deepEqual(JSON.parse(writes[0].body),{risk_target_usdt:'7.50000000000000001'});assert.equal(document.getElementById('robotRisk').value,'7.50000000000000001');assert.equal(data.robot.robot_on,false);assert.match(content.textContent,/hanya untuk analisis baru/);assert.match(content.textContent,/intent yang sudah dibuat tetap/);
document.getElementById('apiDisconnect').click();assert.equal(document.getElementById('robotRiskSave').disabled,true);
""")

    def test_risk_draft_survives_focused_poll_and_panel_reopen(self):
        self.run_dom(r"""
await connect();const risk=document.getElementById('robotRisk');risk.value='6.125';risk.oninput();document.activeElement=risk;data.robot.risk_target_usdt='8';[...intervals.values()][0]();await flush();assert.equal(document.getElementById('robotRisk'),risk);assert.equal(risk.value,'6.125');
open('calendar');open('robot');assert.equal(document.getElementById('robotRisk').value,'6.125');document.getElementById('robotRiskSave').click();await flush();assert.equal(data.robot.risk_target_usdt,'6.125');
""")

    def test_invalid_risk_values_never_send_setting_mutations(self):
        self.run_dom(r"""
await connect();for(const value of ['0','0.0000','-1','101','100.0000000000000000001','Infinity','1e2','not-a-number','']){const risk=document.getElementById('robotRisk');risk.value=value;risk.oninput();document.getElementById('robotRiskSave').click();await flush();assert.equal(calls.filter(call=>call.method==='POST').length,0);assert.match(content.textContent,/Risiko harus/);}
const risk=document.getElementById('robotRisk');risk.value='100.000';risk.oninput();document.getElementById('robotRiskSave').click();await flush();assert.equal(data.robot.risk_target_usdt,'100.000');
""")

    def test_new_risk_draft_is_not_erased_by_post_save_refresh(self):
        self.run_dom(r"""
await connect();let finish;responseOverride=url=>url.endsWith('/office/status')?new Promise(resolve=>{finish=()=>resolve(response(data));}):null;
document.getElementById('robotRisk').value='9.25';document.getElementById('robotRisk').oninput();document.getElementById('robotRiskSave').click();await flush();const next=document.getElementById('robotRisk');next.value='10.75';next.oninput();document.activeElement=next;finish();await flush();assert.equal(next.value,'10.75');open('calendar');open('robot');assert.equal(document.getElementById('robotRisk').value,'10.75');assert.equal(data.robot.risk_target_usdt,'9.25');
""")

    def test_risk_post_refresh_wins_over_old_get_and_disconnect_generation(self):
        self.run_dom(r"""
await connect();const previous=structuredClone(data);let finish;let intercepted=false;responseOverride=url=>url.endsWith('/office/status')&&!intercepted?(intercepted=true,new Promise(resolve=>{finish=()=>resolve(response(previous));})):null;[...intervals.values()][0]();await flush();const risk=document.getElementById('robotRisk');risk.value='9.25';risk.oninput();document.getElementById('robotRiskSave').click();await flush();finish();await flush();assert.equal(document.getElementById('robotRisk').value,'9.25');
let finishSave;responseOverride=(url,options)=>url.endsWith('/robot/settings')?new Promise(resolve=>{finishSave=()=>resolve(response(data.robot));}):null;document.getElementById('robotRisk').value='10';document.getElementById('robotRiskSave').click();await flush();document.getElementById('apiDisconnect').click();finishSave();await flush();assert.equal(document.getElementById('robotRiskSave').disabled,true);assert.deepEqual(statValues(),['—','—','—','—']);assert.equal(snapshots.at(-1).status,'OFFLINE');
""")

    def test_old_office_url_is_a_relative_alias_without_duplicate_renderer(self):
        alias = (ROOT / 'harun_ai_trading_office_3d_detailed.html').read_text()
        self.assertIn('http-equiv="refresh" content="0;url=./"', alias)
        self.assertIn('href="./"', alias)
        self.assertNotIn('<script', alias)
        self.assertNotIn('SIMULATION', alias)

    def test_daily_two_entry_rule_and_off_calendar_preserve_account_reads(self):
        self.run_dom(r"""
data.robot.bot_entries_today=2;data.employees=['market','neuro','risk','trading','position','reviewer','report','boss'].map(id=>({id,name:id,status:'WORKING'}));await connect();assert.match(content.textContent,/Entry bot hari ini2 \/ 2/);assert.match(content.textContent,/Maksimal 2 entry bot per hari/);assert.match(content.textContent,/hari sebelumnya dan posisi manual tetap memakai slot/);assert.match(content.textContent,/OFF menghentikan riset, pengiriman order baru, dan rekonsiliasi robot/);assert.match(content.textContent,/OFF tidak menutup posisi atau membatalkan order/);assert.equal(statValues()[0],'116.93');assert.equal(statValues()[3],'1');
open('calendar');assert.doesNotMatch(content.textContent,/WORKING/);assert.match(content.textContent,/ETHUSDT/);assert.equal(calls.filter(call=>call.method==='POST').length,0);[...intervals.values()][0]();await flush();assert.equal(statValues()[3],'1');assert.equal(calls.some(call=>/\/order|\/cancel|\/close/.test(call.url)),false);
""")


    def test_visible_branding_and_retired_menu_renderers_are_removed(self):
        paths = ('index.html', 'harun_ai_trading_office_3d_detailed.html',
                 'assets/office3d/scene-builder.js', 'assets/office3d/avatars.js')
        for path in paths:
            self.assertNotRegex((ROOT / path).read_text(), r'(?i)\b(?:nanda|ai)\b')
        source = (ROOT / 'assets/dashboard.js').read_text()
        for retired in ('renderStaff', 'renderReports', 'renderActivity', 'renderPositions', 'KARYAWAN AI', 'RIWAYAT POSISI BINANCE'):
            self.assertNotIn(retired, source)
        self.run_dom(r"""
data.account.positions[0].symbol='Nanda AI HYPEUSDT';await connect();assert.doesNotMatch(content.textContent,/\b(?:nanda|ai)\b/i);assert.match(content.textContent,/HYPEUSDT/);
""")

    def test_calendar_month_limits_wib_dates_and_proven_position_details(self):
        self.run_dom(r"""
await connect();open('calendar');assert.match(document.getElementById('pnlMonth').textContent,/Oktober 2026/i);assert.equal(document.getElementById('pnlPreviousMonth').disabled,true);assert.equal(document.getElementById('pnlNextMonth').disabled,true);assert.equal(document.getElementById('pnl-day-2026-10-08').disabled,true);assert.equal(content.querySelectorAll('.pnl-day').length,31);assert.equal(content.querySelectorAll('.pnl-day-empty').length,4);
assert.match(content.textContent,/PNL terealisasi net/);assert.match(content.textContent,/2\.575,81/);assert.match(content.textContent,/15.18.23/);assert.match(content.textContent,/17.07.39/);assert.match(content.textContent,/1 jam 49 mnt/);assert.doesNotMatch(content.textContent,/ROI|75X|Cross/);assert.equal(document.getElementById('pnl-day-2026-10-07').className.includes('loss'),true);
clock=Date.parse('2026-11-01T03:00:00Z');data.generated_at=now();data.account.checked_at=now();data.robot.checked_at=now();data.pnl_calendar.checked_at=now();data.pnl_calendar.end_date='2026-11-01';[...intervals.values()][0]();await flush();assert.equal(document.getElementById('pnlNextMonth').disabled,false);document.getElementById('pnlNextMonth').click();assert.match(document.getElementById('pnlMonth').textContent,/November 2026/i);assert.equal(document.getElementById('pnlNextMonth').disabled,true);assert.equal(document.getElementById('pnlPreviousMonth').disabled,false);document.getElementById('pnlPreviousMonth').click();assert.equal(document.getElementById('pnlPreviousMonth').disabled,true);
""")

    def test_unknown_net_shows_only_known_subtotal_and_fee_components(self):
        self.run_dom(r"""
const position=data.pnl_calendar.positions[0];position.pnl_usdt=null;position.complete=false;position.funding_usdt=null;position.insurance_usdt=null;position.opened_at=null;position.entry_price=null;await connect();open('calendar');assert.match(content.textContent,/PNL diketahui/);assert.match(content.textContent,/−6,52 \*/);assert.match(content.textContent,/Funding belum dapat dipastikan/);assert.match(content.textContent,/Rincian posisi belum lengkap/);assert.match(content.textContent,/Belum tersedia/);assert.doesNotMatch(content.textContent,/PNL terealisasi net|Durasi/);
""")

    def test_calendar_keyboard_navigation_selects_days_and_stays_in_bounds(self):
        self.run_dom(r"""
await connect();open('calendar');let prevented=0;const arrow=(id,key)=>{for(const listener of document.getElementById(id).events.keydown??[])listener({key,preventDefault(){prevented++;}});};arrow('pnl-day-2026-10-07','ArrowLeft');assert.equal(document.getElementById('pnl-day-2026-10-06').attributes['aria-pressed'],'true');assert.equal(document.activeElement.id,'pnl-day-2026-10-06');assert.equal(content.querySelectorAll('.pnl-position').length,0);arrow('pnl-day-2026-10-06','ArrowRight');assert.equal(content.querySelectorAll('.pnl-position').length,1);arrow('pnl-day-2026-10-07','ArrowRight');assert.equal(document.getElementById('pnl-day-2026-10-07').attributes['aria-pressed'],'true');assert.equal(prevented,3);assert.equal(calls.filter(call=>call.method==='POST').length,0);
""")

    def test_changing_panels_opens_calendar_from_the_top(self):
        self.run_dom(r"""
await connect();panel.scrollTop=250;open('calendar');assert.equal(panel.scrollTop,0);panel.scrollTop=100;document.getElementById('pnl-day-2026-10-06').click();assert.equal(panel.scrollTop,100);open('robot');assert.equal(panel.scrollTop,0);
""")

    def test_monthly_known_totals_preserve_decimal_precision(self):
        self.run_dom(r"""
data.pnl_calendar.days[0].closed_positions=1;data.pnl_calendar.days[0].known_pnl_usdt='0.10000000000000001';data.pnl_calendar.days[6].known_pnl_usdt='0.10499999999999999';await connect();open('calendar');assert.match(content.querySelector('.pnl-summary').textContent,/\+0,21 \*/);data.pnl_calendar.days[0].known_pnl_usdt='0.00000001';data.pnl_calendar.days[6].known_pnl_usdt='0.00000002';[...intervals.values()][0]();await flush();assert.match(content.querySelector('.pnl-summary').textContent,/\+0,00 \*/);
""")

    def test_stale_missing_and_invalid_calendar_never_fabricate_zero(self):
        self.run_dom(r"""
await connect();data.pnl_calendar.checked_at='2020-01-01T00:00:00Z';[...intervals.values()][0]();await flush();open('calendar');assert.match(content.textContent,/Riwayat ini belum diperbarui/);assert.match(content.textContent,/−6,52/);data.pnl_calendar.positions[0].close_date='2026-10-06';[...intervals.values()][0]();await flush();assert.match(content.textContent,/Kalender PNL belum tersedia/);assert.equal(content.querySelectorAll('.pnl-position').length,0);assert.equal(statValues()[0],'116.93');delete data.pnl_calendar;[...intervals.values()][0]();await flush();assert.match(content.textContent,/Kalender PNL belum tersedia/);assert.doesNotMatch(document.getElementById('pnl-day-2026-10-07').textContent,/0,00/);
""")

    def test_connected_loading_calendar_does_not_ask_to_reconnect(self):
        self.run_dom(r"""
data.pnl_calendar.status='UNAVAILABLE';data.pnl_calendar.days=[];data.pnl_calendar.positions=[];data.pnl_calendar.checked_at=null;data.pnl_calendar.incomplete_reasons=['HISTORY_NOT_LOADED'];await connect();open('calendar');assert.match(content.textContent,/Riwayat Futures sedang dimuat/);assert.doesNotMatch(content.textContent,/Hubungkan worker/);assert.equal(statValues()[0],'116.93');assert.match(document.getElementById('pnl-day-2026-10-07').textContent,/—/);
data.pnl_calendar.incomplete_reasons=['BINANCE_ACCOUNT_UNAVAILABLE'];[...intervals.values()][0]();await flush();assert.match(content.textContent,/belum tersedia dari Binance Futures/);assert.doesNotMatch(content.textContent,/Hubungkan worker|sedang dimuat/);open('robot');document.getElementById('apiDisconnect').click();open('calendar');assert.match(content.textContent,/Hubungkan worker/);
""")

    def test_final_close_is_grouped_in_wib_not_browser_timezone(self):
        self.run_dom(r"""
const position=data.pnl_calendar.positions[0];position.closed_at='2026-10-06T18:30:00Z';position.opened_at='2026-10-06T17:30:00Z';position.close_date='2026-10-07';await connect();open('calendar');assert.match(content.textContent,/ETHUSDT/);assert.match(content.textContent,/01.30.00/);assert.equal(content.querySelectorAll('.pnl-position').length,1);document.getElementById('pnl-day-2026-10-06').click();assert.equal(content.querySelectorAll('.pnl-position').length,0);
""")

    def test_neurobro_login_prepares_one_use_anchor_during_active_grant_without_robot_writes(self):
        self.run_dom(r"""
open('neurobro');assert.equal(document.getElementById('neurobroLogin').disabled,true);await connect();open('neurobro');assert.match(content.textContent,/Login diperlukan/);assert.match(document.getElementById('neurobroMenuStatus').textContent,/Login diperlukan/);
let finish;responseOverride=url=>url.endsWith('/neurobro/login/start')?new Promise(resolve=>{finish=()=>resolve(response({ticket:'a'.repeat(64),expires_in:60,path:'/neurobro/login'}));}):null;
document.getElementById('neurobroLogin').click();assert.equal(document.getElementById('neurobroLoginOpen'),null);assert.equal(document.getElementById('neurobroLogin').disabled,true);await flush();
const post=calls.filter(call=>call.method==='POST');assert.equal(post.length,1);assert.equal(post[0].url,'https://worker.test/neurobro/login/start');assert.equal(post[0].headers.Authorization,'Bearer '+'C'.repeat(32));assert.equal(post[0].credentials,'omit');assert.equal(post[0].redirect,'error');assert.equal(post[0].cache,'no-store');assert.deepEqual(JSON.parse(post[0].body),{});
data.research.status='LOGIN_IN_PROGRESS';[...intervals.values()][0]();await flush();assert.doesNotMatch(content.textContent,/tab Neurobro yang sudah terbuka/);finish();await flush();
const link=document.getElementById('neurobroLoginOpen');assert.equal(link.tagName,'A');assert.equal(link.textContent,'BUKA BROWSER LOGIN');assert.equal(link.attributes.href,'https://worker.test/neurobro/login#'+'a'.repeat(64));assert.equal(link.attributes.target,'_blank');assert.equal(link.attributes.rel,'noopener noreferrer');assert.equal(link.attributes.href.includes('C'.repeat(32)),false);assert.equal(content.textContent.includes('a'.repeat(64)),false);assert.match(content.textContent,/Akses login siap/);assert.equal(link.click().defaultPrevented,false);assert.equal(calls.filter(call=>call.method==='POST').length,1);assert.equal(data.robot.robot_on,false);assert.equal(calls.some(call=>/\/robot\/settings|\/order|\/cancel|\/close/.test(call.url)),false);
data.research.status='CONNECTED';[...intervals.values()][0]();await flush();assert.match(document.getElementById('neurobroMenuStatus').textContent,/Terhubung/);assert.equal(document.getElementById('neurobroLogin'),null);assert.equal(document.getElementById('neurobroLoginOpen'),null);assert.equal(link.attributes.href,undefined);assert.equal(timeouts.size,0);
""")

    def test_neurobro_login_rejects_invalid_ticket_and_late_disconnect(self):
        self.run_dom(r"""
await connect();open('neurobro');
for(const invalid of [{ticket:'a'.repeat(64),expires_in:60,path:'https://bad.test/'},{ticket:'C'.repeat(64),expires_in:60,path:'/neurobro/login'},{ticket:'a'.repeat(64),expires_in:61,path:'/neurobro/login'},{ticket:'a'.repeat(64),expires_in:0,path:'/neurobro/login'}]){responseOverride=url=>url.endsWith('/neurobro/login/start')?response(invalid):null;document.getElementById('neurobroLogin').click();await flush();assert.equal(document.getElementById('neurobroLoginOpen'),null);assert.equal(timeouts.size,0);assert.match(content.textContent,/tidak sesuai kontrak/);}
let finish;responseOverride=url=>url.endsWith('/neurobro/login/start')?new Promise(resolve=>{finish=()=>resolve(response({ticket:'b'.repeat(64),expires_in:60,path:'/neurobro/login'}));}):null;document.getElementById('neurobroLogin').click();await flush();document.getElementById('apiDisconnect').click();finish();await flush();assert.equal(document.getElementById('neurobroLoginOpen'),null);assert.equal(timeouts.size,0);assert.equal(content.textContent.includes('b'.repeat(64)),false);assert.equal(document.getElementById('neurobroLogin').disabled,true);assert.equal(document.getElementById('neurobroMenu').dataset.status,'UNAVAILABLE');
""")

    def test_neurobro_ticket_deadline_starts_before_request_and_expired_click_is_blocked(self):
        self.run_dom(r"""
await connect();open('neurobro');let finish;responseOverride=url=>url.endsWith('/neurobro/login/start')?new Promise(resolve=>{finish=()=>resolve(response({ticket:'d'.repeat(64),expires_in:60,path:'/neurobro/login'}));}):null;document.getElementById('neurobroLogin').click();await flush();clock+=9000;finish();await flush();assert.equal([...timeouts.values()][0].delay,51000);const oldLink=document.getElementById('neurobroLoginOpen');clock+=51000;const event=oldLink.click();assert.equal(event.defaultPrevented,true);assert.equal(oldLink.attributes.href,undefined);assert.equal(timeouts.size,0);assert.equal(document.getElementById('neurobroLoginOpen'),null);assert.match(content.textContent,/kedaluwarsa/);assert.equal(calls.filter(call=>call.method==='POST').length,1);
""")

    def test_neurobro_expiry_timer_and_disconnect_remove_ticket_from_hidden_panel(self):
        self.run_dom(r"""
await connect();open('neurobro');responseOverride=url=>url.endsWith('/neurobro/login/start')?response({ticket:'e'.repeat(64),expires_in:60,path:'/neurobro/login'}):null;document.getElementById('neurobroLogin').click();await flush();const expiredLink=document.getElementById('neurobroLoginOpen');document.getElementById('panelClose').click();clock+=60000;[...timeouts.values()][0].fn();assert.equal(expiredLink.attributes.href,undefined);assert.equal(expiredLink.hidden,true);assert.equal(timeouts.size,0);
data.research.checked_at=now();open('neurobro');document.getElementById('neurobroLogin').click();await flush();const disconnectedLink=document.getElementById('neurobroLoginOpen');document.getElementById('apiDisconnect').click();assert.equal(disconnectedLink.attributes.href,undefined);assert.equal(timeouts.size,0);assert.equal(disconnectedLink.click().defaultPrevented,true);assert.equal(calls.filter(call=>call.method==='POST').length,2);
""")

    def test_neurobro_pending_or_expired_grant_never_automatically_requests_another(self):
        self.run_dom(r"""
await connect();open('neurobro');const oldButton=document.getElementById('neurobroLogin');data.research.status='LOGIN_IN_PROGRESS';[...intervals.values()][0]();await flush();oldButton.click();await flush();assert.equal(document.getElementById('neurobroLogin'),null);assert.equal(document.getElementById('neurobroLoginOpen'),null);assert.match(content.textContent,/tunggu sampai tombol LOGIN NEURO tersedia kembali/);assert.equal(calls.filter(call=>call.method==='POST').length,0);
data.research.status='LOGIN_REQUIRED';[...intervals.values()][0]();await flush();let finish;responseOverride=url=>url.endsWith('/neurobro/login/start')?new Promise(resolve=>{finish=()=>resolve(response({ticket:'f'.repeat(64),expires_in:60,path:'/neurobro/login'}));}):null;document.getElementById('neurobroLogin').click();await flush();clock+=60000;data.research.checked_at=now();finish();await flush();assert.equal(document.getElementById('neurobroLoginOpen'),null);assert.equal(timeouts.size,0);assert.match(content.textContent,/kedaluwarsa sebelum siap/);assert.equal(calls.filter(call=>call.method==='POST').length,1);
""")

    def test_neurobro_busy_or_transport_error_clears_prepared_ticket(self):
        self.run_dom(r"""
await connect();open('neurobro');responseOverride=url=>url.endsWith('/neurobro/login/start')?response({ticket:'9'.repeat(64),expires_in:60,path:'/neurobro/login'}):null;document.getElementById('neurobroLogin').click();await flush();const busyLink=document.getElementById('neurobroLoginOpen');data.research.status='BUSY';[...intervals.values()][0]();await flush();assert.equal(busyLink.attributes.href,undefined);assert.equal(timeouts.size,0);assert.equal(document.getElementById('neurobroLoginOpen'),null);
data.research.status='LOGIN_REQUIRED';[...intervals.values()][0]();await flush();document.getElementById('neurobroLogin').click();await flush();const failedLink=document.getElementById('neurobroLoginOpen');responseOverride=url=>url.endsWith('/office/status')?Promise.reject(Error('Offline')):null;[...intervals.values()][0]();await flush();assert.equal(failedLink.attributes.href,undefined);assert.equal(timeouts.size,0);assert.equal(document.getElementById('neurobroLoginOpen'),null);assert.equal(calls.some(call=>/\/robot\/settings|\/order|\/cancel|\/close/.test(call.url)),false);
""")

    def test_neurobro_session_staleness_logout_challenge_and_legacy_provider(self):
        self.run_dom(r"""
data.research.status='CONNECTED';await connect();open('neurobro');assert.equal(document.getElementById('neurobroStatus').dataset.status,'CONNECTED');assert.equal(document.getElementById('neurobroLogin'),null);
clock+=90000;data.generated_at=now();[...intervals.values()][0]();await flush();assert.equal(document.getElementById('neurobroStatus').dataset.status,'UNAVAILABLE');assert.doesNotMatch(document.getElementById('neurobroMenuStatus').textContent,/Terhubung/);assert.equal(document.getElementById('neurobroLogin').disabled,false);
data.research.checked_at=now();for(const state of ['LOGIN_REQUIRED','CHALLENGE_REQUIRED','UNAVAILABLE']){data.research.status=state;[...intervals.values()][0]();await flush();assert.equal(document.getElementById('neurobroStatus').dataset.status,state);assert.equal(document.getElementById('neurobroLogin').disabled,false);}
data.research.status='CONNECTED';data.research.checked_at=new Date(clock+30000).toISOString();[...intervals.values()][0]();await flush();assert.equal(document.getElementById('neurobroStatus').dataset.status,'UNAVAILABLE');assert.equal(document.getElementById('neurobroLogin').disabled,false);data.research.checked_at=now();
data.research.status='LOGIN_IN_PROGRESS';[...intervals.values()][0]();await flush();assert.equal(document.getElementById('neurobroLogin'),null);assert.equal(document.getElementById('neurobroLoginOpen'),null);assert.match(content.textContent,/tunggu sampai tombol LOGIN NEURO tersedia kembali/);
data.research.provider='NEUROAPI';data.research.status='API_ACTIVE';[...intervals.values()][0]();await flush();assert.match(content.textContent,/Browser belum diaktifkan/);assert.equal(document.getElementById('neurobroLogin'),null);assert.equal(calls.filter(call=>call.method==='POST').length,0);assert.equal(data.robot.robot_on,false);
""")

if __name__ == '__main__':
    unittest.main()
