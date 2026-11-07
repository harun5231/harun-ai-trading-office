"""Office monitor contracts: only fresh worker facts, never invented market/chat data."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
import assert from 'node:assert/strict';
const RealDate=Date;let clock=RealDate.parse('2026-10-06T12:00:00Z');globalThis.Date=class extends RealDate{constructor(...args){super(...(args.length?args:[clock]));}static now(){return clock;}};
const canvases=[];
globalThis.document={createElement:tag=>{assert.equal(tag,'canvas');const canvas={width:0,height:0};const ctx=new Proxy({drawn:[],fillText(value){this.drawn.push(String(value));},fillRect(x,y,w,h){if(x===0&&y===0&&w===512&&h===256)this.drawn=[];},createLinearGradient(){return {addColorStop(){}};}},{get(target,key){return key in target?target[key]:()=>{};}});canvas.ctx=ctx;canvas.getContext=()=>ctx;canvases.push(canvas);return canvas;}};
globalThis.fetch=()=>{throw Error('Office monitors must not make requests.');};
class CanvasTexture{constructor(image){this.image=image;}dispose(){this.disposed=true;}}
const THREE={CanvasTexture,SRGBColorSpace:'srgb',LinearFilter:'linear'};
const {createMonitorTextures}=await import('./assets/office3d/monitor-textures.js');
const monitors=createMonitorTextures(THREE);
const words=role=>monitors.get(role).image.ctx.drawn;
const text=role=>words(role).join(' | ');
const now=()=>new Date(clock).toISOString();
const data={schema_version:2,connected:true,status:'EXECUTION_BLOCKED',robot_checked_at:now(),account_checked_at:now(),reports_checked_at:now(),balance:'116.928',usdt_wallet_balance:'116.928',active_positions:1,pnl_today:'3.75',trades_today:null,bot_entries_today:7,risk_target_usdt:'5',available_slots:1,manual_exposure:['HYPEUSDT'],last_decision:{symbol:'ETHUSDT',status:'EXECUTION_BLOCKED'},execution_gateway:{connected:false,status:'NOT_CONNECTED'},events:[{at:now(),agent:'Coordinator',state:'SCREENING',message:'Actual worker event'}]};
"""


class MonitorTextures(unittest.TestCase):
    def run_node(self, assertions):
        if shutil.which('node') is None:
            self.skipTest('Node.js is required for local canvas contracts')
        result = subprocess.run(['node', '--input-type=module', '-e', HARNESS + assertions], cwd=ROOT,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_fabricated_prices_counts_gateway_or_chat_without_data(self):
        self.run_node(r"""
assert.equal(canvases.length,16);assert.equal(monitors.diagnostics().textures,16);
assert.equal(monitors.diagnostics().decorativeCharts,false);
for(const role of monitors.diagnostics().roles){assert.match(text(role),/DATA WORKER · READ ONLY/);assert.equal(words(role).some(value=>/^\$?-?\d+(?:\.\d+)?%?$/.test(value)),false);assert.doesNotMatch(text(role),/BUY|SELL|ONLINE|WORKING|CHAT|SIGNAL PATTERN|CODE FLOW/);}
assert.match(text('trading'),/BELUM ADA DATA GATEWAY/);assert.doesNotMatch(text('trading'),/NOT_CONNECTED/);assert.match(text('market'),/Belum ada aktivitas terbaru/);
assert.equal(monitors.update(0,{}),true);assert.equal(monitors.update(.02,{}),false);monitors.dispose();assert.equal(monitors.update(1,data),false);assert.throws(()=>monitors.get('market'));
""")

    def test_only_fresh_fact_fields_render_as_numbers_and_decisions(self):
        self.run_node(r"""
monitors.update(1,data);assert.ok(words('position').includes('116.93'));assert.ok(words('report').includes('3.75'));assert.ok(words('risk').includes('5.00'));assert.ok(words('report').includes('—'));assert.ok(!words('report').includes('7'));assert.match(text('reviewer'),/ETHUSDT/);assert.match(text('reviewer'),/EXECUTION_BLOCKED/);assert.match(text('trading'),/NOT_CONNECTED/);
clock+=50000;monitors.update(2,data);assert.ok(!words('position').includes('116.93'));assert.ok(!words('position').includes('HYPEUSDT'));assert.ok(words('risk').includes('5.00'));
clock+=71000;monitors.update(3,data);assert.ok(!words('risk').includes('5.00'));assert.ok(!words('report').includes('3.75'));assert.doesNotMatch(text('reviewer'),/ETHUSDT/);assert.doesNotMatch(text('trading'),/NOT_CONNECTED/);monitors.dispose();
""")

    def test_static_screens_preserve_reduced_motion_and_refresh_real_status(self):
        self.run_node(r"""
const staticMonitors=createMonitorTextures(THREE,{mobile:true,reducedMotion:true});assert.equal(staticMonitors.diagnostics().width,384);assert.equal(staticMonitors.diagnostics().fps,8);assert.equal(staticMonitors.update(1,data),true);assert.equal(staticMonitors.update(2,data),false);assert.equal(staticMonitors.update(3,{...data,last_decision:{symbol:'SOLUSDT',status:'HOLD'}}),true);assert.match(staticMonitors.get('reviewer').image.ctx.drawn.join(' | '),/SOLUSDT/);staticMonitors.dispose();monitors.dispose();
""")

    def test_events_are_dated_real_messages_not_character_dialogue(self):
        self.run_node(r"""
data.events=[{at:now(),agent:'Nanda AI Coordinator',state:'SCREENING',message:'Analysis has not started.'}];
monitors.update(1,data);assert.match(text('market'),/Nanda AI Coordinator/);assert.match(text('market'),/Analysis has not started\./);assert.match(text('market'),/12:00:00 UTC/);assert.match(text('market'),/AKTIVITAS WORKER/);assert.doesNotMatch(text('market'),/CHAT/);
const stale=new Date(clock-120001).toISOString();
monitors.update(2,{...data,events:[{at:stale,agent:'Coordinator',message:'Do not display old message'}]});assert.doesNotMatch(text('market'),/Do not display/);
monitors.update(3,{...data,events:[{agent:'Coordinator',message:'Do not display undated message'}]});assert.doesNotMatch(text('market'),/Do not display/);
monitors.update(4,{...data,connected:false});assert.doesNotMatch(text('market'),/Analysis has not started/);monitors.dispose();
""")

    def test_five_visible_names_do_not_remove_backend_texture_roles(self):
        self.run_node(r"""
for(const [role,name] of Object.entries({market:'HERMES AGENT',neuro:'NEUROBRO',risk:'RISK MANAGER',trading:'BINANCE',boss:'HARUN'}))assert.ok(words(role).includes(name));
for(const role of ['position','reviewer','report'])assert.ok(monitors.get(role));
monitors.update(1,{...data,robot_on:true,status:'WAITING'});assert.ok(words('boss').includes('ON'));for(const role of monitors.diagnostics().roles)assert.doesNotMatch(text(role),/WORKING|ONLINE|Bekerja/);
monitors.dispose();
""")


if __name__ == '__main__':
    unittest.main()
