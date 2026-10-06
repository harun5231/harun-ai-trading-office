"""Dashboard transport contracts in a local DOM harness; no provider or browser access."""
from html.parser import HTMLParser
from pathlib import Path
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
  addEventListener(name,fn){(this.events[name]??=[]).push(fn);}
  click(){if(this.disabled)return;document.activeElement=this;this.onclick?.();for(const fn of this.events.click??[])fn();}
  querySelector(selector){return this.querySelectorAll(selector)[0]??null;}
  querySelectorAll(selector){const matches=node=>selector==='[data-view]'?!!node.dataset.view:selector.startsWith('#')?node.id===selector.slice(1):selector.startsWith('.')?node.className.split(/\s+/).includes(selector.slice(1)):node.tagName.toLowerCase()===selector;return walk(this).slice(1).filter(matches);}
}
const walk=node=>[node,...node.children.flatMap(walk)];
const root=new Node('body');
const make=(tag,id='',className='')=>{const node=new Node(tag);node.id=id;node.className=className;return node;};
const panel=make('div','infoPanel'),content=make('div','panelContent'),title=make('h3','panelTitle'),drawer=make('div','menuDrawer'),toggle=make('button','menuToggle'),close=make('button','panelClose');panel.hidden=true;drawer.hidden=true;panel.append(title,close,content);root.append(panel,drawer,toggle);
for(const view of ['robot','staff','reports','activity','positions']){const button=make('button',view==='robot'?'robotMenu':'','menu-item');button.dataset.view=view;button.textContent=view;drawer.append(button);}
const brand=make('div','','brand'),brandSmall=new Node('small');brand.append(brandSmall);root.append(brand);
const stats=make('div','','stats');for(let i=0;i<4;i++){const stat=make('div','','stat');stat.append(new Node('span'),new Node('b'));stats.append(stat);}root.append(stats);
const listeners={},snapshots=[],intervals=new Map(),calls=[];let intervalId=0;
const document={baseURI:'https://office.test/',currentScript:{src:'https://office.test/assets/dashboard.js'},activeElement:null,hidden:false,createElement:tag=>new Node(tag),getElementById:id=>walk(root).find(node=>node.id===id)??null,querySelector:selector=>selector==='.brand small'?brandSmall:root.querySelector(selector),querySelectorAll:selector=>selector==='.stats .stat b'?stats.children.map(node=>node.children[1]):selector==='.stats .stat span'?stats.children.map(node=>node.children[0]):root.querySelectorAll(selector),addEventListener:(name,fn)=>{(listeners[name]??=[]).push(fn);}};
const window={dispatchEvent:event=>{snapshots.push(event.detail);for(const fn of listeners[event.type]??[])fn(event);},addEventListener:(name,fn)=>{(listeners[name]??=[]).push(fn);},removeEventListener:(name,fn)=>{listeners[name]=(listeners[name]??[]).filter(item=>item!==fn);}};
const now=()=>new Date().toISOString();
const data={schema_version:2,source:'BINANCE_FUTURES',generated_at:now(),robot:{robot_on:false,bot_status:'OFF',checked_at:now(),risk_target_usdt:'5',running_positions:1,available_slots:1,manual_exposure:['HYPEUSDT'],bot_entries_today:0,execution_gateway:{connected:false,status:'NOT_CONNECTED',failure_code:'BINANCE_ORDER_GATEWAY_NOT_CONNECTED'}},account:{status:'CONNECTED',checked_at:now(),usdt_wallet_balance:'116.928',usdt_available_balance:'105.25',active_positions:1,positions:[{symbol:'HYPEUSDT',side:'LONG',position_side:'BOTH',quantity:'1.5',entry_price:'28',mark_price:'29',unrealized_pnl:'1.5'}]},reports:{status:'AVAILABLE',checked_at:now(),period_start:now(),period_end:now(),pnl_today_usdt:'3.75',realized_pnl_today_usdt:'4',commission_today_usdt:'-0.25',funding_today_usdt:'0',trades_today:2,complete:true},position_history:{status:'AVAILABLE',kind:'BINANCE_FILLS',checked_at:now(),period_start:now(),period_end:now(),items:[{id:'fill-1',symbol:'BTCUSDT',order_id:'order-1',side:'SELL',position_side:'BOTH',quantity:'0.001',price:'64000',realized_pnl:'4',commission:'0.25',commission_asset:'USDT',time:now()}],complete:true},employees:[{id:'neuro',name:'NeuroAPI Analyst',status:'WAITING'}],activity:[{at:now(),state:'SCREENING',agent:'Coordinator',message:'<img src=x onerror="window.pwned=true">'}]};
let responseOverride=null;
const response=value=>({ok:true,status:200,json:async()=>structuredClone(value)});
async function fetch(url,options={}){url=String(url);calls.push({url,...options});if(url.endsWith('/worker-config.json'))return response({worker_origin:''});if(responseOverride){const handled=responseOverride(url,options);if(handled)return handled;}if(url.endsWith('/office/status'))return response(data);if(url.endsWith('/robot/settings')){const value=JSON.parse(options.body);Object.assign(data.robot,value);if(Object.hasOwn(value,'robot_on'))data.robot.bot_status=value.robot_on?'WAITING':'OFF';return response(data.robot);}throw Error('Unexpected route: '+url);}
vm.runInNewContext(fs.readFileSync('assets/dashboard.js','utf8'),{document,window,location:{protocol:'https:',origin:'https://office.test'},fetch,URL,Date,AbortSignal,CustomEvent:class{constructor(type,{detail}){this.type=type;this.detail=detail;}},setInterval:fn=>{intervals.set(++intervalId,fn);return intervalId;},clearInterval:id=>intervals.delete(id)});
const flush=async()=>{for(let i=0;i<12;i++)await new Promise(resolve=>setImmediate(resolve));};
const open=view=>drawer.children.find(button=>button.dataset.view===view).click();
const connect=async()=>{open('robot');document.getElementById('apiOrigin').value='https://worker.test';document.getElementById('apiToken').value='C'.repeat(32);document.getElementById('apiConnect').click();await flush();};
const statValues=()=>stats.children.map(stat=>stat.children[1].textContent);
"""


class DashboardContract(unittest.TestCase):
    def test_only_five_requested_menus_and_one_dashboard_entry(self):
        parser = MenuParser(); parser.feed((ROOT / 'index.html').read_text())
        self.assertEqual(parser.views, ['robot', 'staff', 'reports', 'activity', 'positions'])
        self.assertIn('assets/dashboard.js', parser.scripts)
        self.assertIn('assets/office3d/renderer.js', parser.scripts)
        self.assertNotIn('assets/workflow.js', parser.scripts)
        self.assertNotIn('assets/neuroapi.js', parser.scripts)
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
assert.deepEqual(statValues(),['—','—','—','—']);open('robot');assert.equal(document.getElementById('robotToggle').disabled,true);
await connect();assert.equal(document.getElementById('apiToken').value,'');assert.equal(statValues()[0],'116.93');assert.equal(statValues()[3],'1');assert.match(content.textContent,/HYPEUSDT/);assert.match(content.textContent,/Transport pengiriman order Binance belum tersedia/);assert.match(content.textContent,/Worker belum dapat mengirim order ke Binance/);assert.doesNotMatch(content.textContent,/Eksekusi Binance diblokir/);
document.getElementById('robotToggle').click();await flush();assert.match(document.getElementById('robotToggle').textContent,/: ON/);
const writes=calls.filter(call=>call.method==='POST');assert.equal(writes.length,1);assert.equal(new URL(writes[0].url).pathname,'/robot/settings');assert.deepEqual(JSON.parse(writes[0].body),{robot_on:true});
open('staff');assert.match(content.textContent,/NeuroAPI Analyst/);open('reports');assert.match(content.textContent,/3.75/);open('activity');assert.match(content.textContent,/<img src=x/);assert.equal(walk(content).some(node=>node.tagName==='IMG'),false);
open('positions');assert.match(content.textContent,/beberapa fill/);assert.match(content.textContent,/fill-1/);assert.match(content.textContent,/0.001/);
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

    def test_partial_reports_and_latest_fill_order_are_explicit(self):
        self.run_dom(r"""
data.reports.status='PARTIAL';data.reports.complete=false;data.reports.trades_today=null;data.position_history.status='PARTIAL';data.position_history.complete=false;data.position_history.items.unshift({...data.position_history.items[0],id:'new-fill',time:new Date(Date.now()+1000).toISOString()});await connect();assert.equal(statValues()[2],'—');assert.equal(stats.children[1].children[0].textContent,'PNL Hari Ini *');assert.match(stats.children[1].children[1].title,/parsial/);open('reports');assert.match(content.textContent,/Laporan parsial/);open('positions');assert.ok(content.textContent.indexOf('new-fill')<content.textContent.indexOf('fill-1'));assert.match(content.textContent,/Data parsial/);
""")

    def test_dashboard_flat_event_connects_to_read_only_avatar_bridge(self):
        self.run_dom(r"""
globalThis.window=window;const {createStatusController}=await import('./assets/office3d/status-controller.js');const bridge=createStatusController();data.robot.last_decision={symbol:'ETHUSDT',status:'EXECUTION_BLOCKED',failure_code:'BINANCE_ORDER_GATEWAY_NOT_CONNECTED'};await connect();
const event=snapshots.at(-1);assert.equal(event.robot_checked_at,data.robot.checked_at);assert.equal(event.account_checked_at,data.account.checked_at);assert.equal(event.reports_checked_at,data.reports.checked_at);assert.equal(event.risk_target_usdt,'5');assert.equal(event.available_slots,1);assert.equal(JSON.stringify(event.manual_exposure),JSON.stringify(['HYPEUSDT']));
assert.equal(bridge.getTelemetry().balance,'116.928');assert.equal(bridge.getTelemetry().last_decision.symbol,'ETHUSDT');assert.equal(bridge.getRole('position').active,true);assert.equal(bridge.getRole('market').active,false);assert.equal(bridge.getRole('trading').active,false);
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
open('reports');open('robot');assert.equal(document.getElementById('robotRisk').value,'6.125');document.getElementById('robotRiskSave').click();await flush();assert.equal(data.robot.risk_target_usdt,'6.125');
""")

    def test_invalid_risk_values_never_send_setting_mutations(self):
        self.run_dom(r"""
await connect();for(const value of ['0','0.0000','-1','101','100.0000000000000000001','Infinity','1e2','not-a-number','']){const risk=document.getElementById('robotRisk');risk.value=value;risk.oninput();document.getElementById('robotRiskSave').click();await flush();assert.equal(calls.filter(call=>call.method==='POST').length,0);assert.match(content.textContent,/Risiko harus/);}
const risk=document.getElementById('robotRisk');risk.value='100.000';risk.oninput();document.getElementById('robotRiskSave').click();await flush();assert.equal(data.robot.risk_target_usdt,'100.000');
""")

    def test_new_risk_draft_is_not_erased_by_post_save_refresh(self):
        self.run_dom(r"""
await connect();let finish;responseOverride=url=>url.endsWith('/office/status')?new Promise(resolve=>{finish=()=>resolve(response(data));}):null;
document.getElementById('robotRisk').value='9.25';document.getElementById('robotRisk').oninput();document.getElementById('robotRiskSave').click();await flush();const next=document.getElementById('robotRisk');next.value='10.75';next.oninput();document.activeElement=next;finish();await flush();assert.equal(next.value,'10.75');open('reports');open('robot');assert.equal(document.getElementById('robotRisk').value,'10.75');assert.equal(data.robot.risk_target_usdt,'9.25');
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


if __name__ == '__main__':
    unittest.main()
