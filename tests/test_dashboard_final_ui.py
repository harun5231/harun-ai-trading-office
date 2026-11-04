"""Focused final menu and schedule UI bindings; synthetic HTTP only."""
from pathlib import Path
import shutil,subprocess,unittest
from tests.test_dashboard_contract import HARNESS,ROOT

class FinalUI(unittest.TestCase):
    def dom(self,body):
        self.assertIsNotNone(shutil.which('node'))
        result=subprocess.run(['node','-e',HARNESS+'\n(async()=>{'+body+'\n})().catch(e=>{console.error(e);process.exitCode=1;});'],cwd=ROOT,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_visible_branding_only_changes_title_header(self):
        html=(ROOT/'index.html').read_text()
        self.assertIn('<title>TRADING OFFICE — 3D</title>',html)
        self.assertIn('<strong>TRADING OFFICE</strong>',html)
        self.assertNotIn('HARUN TRADING OFFICE',html)
        self.assertIn('assets/office3d/renderer.js',html)

    def test_robot_order_policy_dto_and_real_unknown_order_status(self):
        self.dom(r"""
open('robot');assert.equal(document.getElementById('robotRisk').value,'');
data.robot.max_positions=2;data.robot.daily_entry_limit=2;data.robot.last_decision={symbol:'ETHUSDT',status:'NEEDS_REVIEW',failure_code:'ORDER_OUTCOME_UNKNOWN'};
await connect();const text=content.textContent;
const labels=['ROBOT TRADING:','Worker:','RISIKO PER SL TERMASUK FEE DAN CADANGAN SLIPPAGE','SIMPAN RISIKO','BOT STATUS:','WAIT:','Risk per SL','Posisi / kapasitas','Slot tersedia','Entry bot hari ini','AKUN FUTURES','Wallet USDT','USDT tersedia','Posisi manual','Posisi aktif Binance','KEPUTUSAN TERAKHIR','Hasil order','Koneksi worker privat'];
for(let i=1;i<labels.length;i++)assert.ok(text.indexOf(labels[i-1])<text.indexOf(labels[i]),labels[i]);
assert.match(text,/Posisi \/ kapasitas1 \/ 2/);assert.match(text,/Hasil orderORDER_OUTCOME_UNKNOWN/);
assert.doesNotMatch(text,/COORDINATOR|Maksimal 2 entry|ON menjalankan|ESTIMASI RISIKO|Transport pengiriman|Ukuran posisi dihitung/);
assert.equal(document.getElementById('robotRisk').value,'5');
""")

    def test_neurobro_retains_login_without_agent_panel_or_private_connection(self):
        self.dom(r"""
await connect();open('neurobro');assert.ok(document.getElementById('neurobroStatus'));assert.ok(document.getElementById('neurobroLogin'));
assert.equal(document.getElementById('apiOrigin'),null);assert.equal(document.getElementById('apiToken'),null);assert.equal(document.getElementById('agentStatus'),null);
assert.doesNotMatch(content.textContent,/AGENT GPT-5 MINI|Model GPT-5 Mini|Koneksi worker privat/);
""")

    def test_monthly_editor_paid_change_and_delete_preserve_paid_occurrence(self):
        self.dom(r"""
let stored={version:0,document:{month:'2026-10',coverage_confirmed:false,fx_idr_per_usdt:null,fx_source:null,bills:[]},summary:{}};
responseOverride=(url,options)=>url.includes('/billing/month?')?response(stored):url.endsWith('/billing/month')?(stored={...stored,version:stored.version+1,document:JSON.parse(options.body).document},response(stored)):null;
await connect();open('billing');await flush();document.getElementById('billingAdd').click();
const edit=(id,value)=>{const field=document.getElementById(id);field.value=value;field.oninput?.();field.onchange?.();};
edit('billName0','Sample monthly');edit('billAmount0','160000');edit('billDue0','2026-10-31');const monthly=document.getElementById('billRecurring0');monthly.checked=true;monthly.onchange();
document.getElementById('billingSave').click();await flush();let row=stored.document.bills[0],id=row.id;assert.equal(row.recurring,true);assert.equal(row.recurrence_id,id);assert.equal(stored.document.simple.recurrence_updates[id].day,31);
document.getElementById('billMarkPaid0').click();await flush();assert.equal(stored.document.bills[0].paid,true);
document.getElementById('billEdit0').click();edit('billAmount0','180000');document.getElementById('billingSave').click();await flush();
assert.equal(stored.document.bills[0].amount,'160000');assert.equal(stored.document.bills[0].paid,true);assert.equal(stored.document.simple.recurrence_updates[id].bill.amount,'180000');
document.getElementById('billDelete0').click();await flush();assert.equal(stored.document.bills.length,1);assert.equal(stored.document.bills[0].paid,true);assert.equal(stored.document.bills[0].amount,'160000');assert.equal(stored.document.simple.recurrence_updates[id],null);
""")

    def test_price_edit_of_clamped_paid_occurrence_keeps_original_month_day(self):
        self.dom(r"""
const bill={id:'sample-occurrence',recurrence_id:'sample-plan',name:'Sample monthly',kind:'subscription',amount:'160000',currency:'IDR',actual_idr:'160000',estimated_idr:null,due_date:'2026-10-28',payment_url:null,paid:true,evidence:'Controlled sample',recurring:true};
const fields=['name','kind','amount','currency','actual_idr','estimated_idr','payment_url','evidence'],spec={day:31,bill:Object.fromEntries(fields.map(k=>[k,bill[k]]))};
let stored={version:1,document:{month:'2026-10',coverage_confirmed:false,fx_idr_per_usdt:null,fx_source:null,bills:[bill]},recurrence_state:{'sample-plan':spec},summary:{}};
responseOverride=(url,options)=>url.includes('/billing/month?')?response(stored):url.endsWith('/billing/month')?(stored={...stored,version:2,document:JSON.parse(options.body).document},response(stored)):null;
await connect();open('billing');await flush();document.getElementById('billEdit0').click();const input=document.getElementById('billAmount0');input.value='180000';input.oninput();document.getElementById('billingSave').click();await flush();
assert.equal(stored.document.bills[0].amount,'160000');assert.equal(stored.document.bills[0].due_date,'2026-10-28');assert.equal(stored.document.simple.recurrence_updates['sample-plan'].day,31);
""")

    def test_billing_network_failure_and_auth_denial_remain_distinct(self):
        self.dom(r"""
await connect();responseOverride=url=>{if(url.includes('/billing/month?'))throw new Error('Load failed');return null;};
open('billing');await flush();assert.match(content.textContent,/Koneksi layanan tagihan gagal/);assert.equal(data.robot.robot_on,false);
responseOverride=url=>url.includes('/billing/month?')?{ok:false,status:403,json:async()=>({error:'FORBIDDEN'})}:null;
document.getElementById('billingReload').click();await flush();assert.match(content.textContent,/Akses worker ditolak/);assert.doesNotMatch(content.textContent,/login Neurobro|verifikasi manusia/i);
assert.equal(calls.filter(row=>row.method==='POST').length,0);
""")
