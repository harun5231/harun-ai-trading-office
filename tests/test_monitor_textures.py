"""Monitor canvas contracts: decoration never substitutes for exchange facts."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
import assert from 'node:assert/strict';
const RealDate=Date;let clock=RealDate.parse('2026-10-06T12:00:00Z');globalThis.Date=class extends RealDate{constructor(...args){super(...(args.length?args:[clock]));}static now(){return clock;}};
const canvases=[];
globalThis.document={createElement:tag=>{assert.equal(tag,'canvas');const canvas={width:0,height:0};const ctx=new Proxy({drawn:[],fillText(value){this.drawn.push(String(value));},fillRect(x,y,w,h){if(x===0&&y===0&&w===512&&h===256)this.drawn=[];},createLinearGradient(){return {addColorStop(){}};},createRadialGradient(){return {addColorStop(){}};}},{get(target,key){return key in target?target[key]:()=>{};}});canvas.ctx=ctx;canvas.getContext=()=>ctx;canvases.push(canvas);return canvas;}};
globalThis.fetch=()=>{throw Error('Monitor graphics must not make requests.');};
class CanvasTexture{constructor(image){this.image=image;}dispose(){this.disposed=true;}}
const THREE={CanvasTexture,SRGBColorSpace:'srgb',LinearFilter:'linear'};
const {createMonitorTextures}=await import('./assets/office3d/monitor-textures.js');
const monitors=createMonitorTextures(THREE);
const words=role=>monitors.get(role).image.ctx.drawn;
const text=role=>words(role).join(' | ');
const now=()=>new Date(clock).toISOString();
const data={schema_version:2,connected:true,status:'EXECUTION_BLOCKED',robot_checked_at:now(),account_checked_at:now(),reports_checked_at:now(),balance:'116.928',usdt_wallet_balance:'116.928',active_positions:1,pnl_today:'3.75',trades_today:null,bot_entries_today:7,risk_target_usdt:'5',available_slots:1,manual_exposure:['HYPEUSDT'],last_decision:{symbol:'ETHUSDT',status:'EXECUTION_BLOCKED'},execution_gateway:{connected:false,status:'NOT_CONNECTED'},events:[{agent:'Coordinator',state:'SCREENING',message:'Actual worker event'}]};
"""


class MonitorTextures(unittest.TestCase):
    def run_node(self, assertions):
        if shutil.which('node') is None:
            self.skipTest('Node.js is required for local canvas contracts')
        result = subprocess.run(['node', '--input-type=module', '-e', HARNESS + assertions], cwd=ROOT,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unconnected_screens_have_no_fabricated_prices_or_counts(self):
        self.run_node(r"""
assert.equal(canvases.length,16);assert.equal(monitors.diagnostics().textures,16);
for(const role of monitors.diagnostics().roles){assert.match(text(role),/READ ONLY · OFFICE GRAPHICS/);assert.doesNotMatch(text(role),/DEMO|SIMULATION|SYNTHETIC|HUMAN REVIEW/);assert.equal(words(role).some(value=>/^\$?-?\d+(?:\.\d+)?%?$/.test(value)),false);}
assert.match(text('trading'),/NOT_CONNECTED/);assert.doesNotMatch(text('trading'),/BUY|SELL|ORDER BOOK|PRICE|QTY/);assert.equal(monitors.update(0,{}),true);assert.equal(monitors.update(.02,{}),false);monitors.dispose();assert.equal(monitors.update(1,data),false);assert.throws(()=>monitors.get('market'));
""")

    def test_only_fresh_fact_fields_render_as_numbers_and_decisions(self):
        self.run_node(r"""
monitors.update(1,data);assert.ok(words('position').includes('116.93'));assert.ok(words('report').includes('3.75'));assert.ok(words('risk').includes('5.00'));assert.ok(words('report').includes('—'));assert.ok(!words('report').includes('7'));assert.match(text('reviewer'),/ETHUSDT/);assert.match(text('reviewer'),/EXECUTION_BLOCKED/);assert.match(text('trading'),/NOT_CONNECTED/);
clock+=50000;monitors.update(2,data);assert.ok(!words('position').includes('116.93'));assert.ok(!words('position').includes('HYPEUSDT'));assert.ok(words('risk').includes('5.00'));
clock+=71000;monitors.update(3,data);assert.ok(!words('risk').includes('5.00'));assert.ok(!words('report').includes('3.75'));assert.doesNotMatch(text('reviewer'),/ETHUSDT/);monitors.dispose();
""")

    def test_reduced_motion_preserves_static_graphics_and_refreshes_real_status(self):
        self.run_node(r"""
const staticMonitors=createMonitorTextures(THREE,{mobile:true,reducedMotion:true});assert.equal(staticMonitors.diagnostics().width,384);assert.equal(staticMonitors.diagnostics().fps,8);assert.equal(staticMonitors.update(1,data),true);assert.equal(staticMonitors.update(2,data),false);assert.equal(staticMonitors.update(3,{...data,last_decision:{symbol:'SOLUSDT',status:'HOLD'}}),true);assert.match(staticMonitors.get('reviewer').image.ctx.drawn.join(' | '),/SOLUSDT/);staticMonitors.dispose();monitors.dispose();
""")

    def test_old_screen_labels_and_trade_fallbacks_are_removed(self):
        source = (ROOT / 'assets/office3d/monitor-textures.js').read_text()
        for retired in ('DEMO', 'SYNTHETIC', 'HUMAN REVIEW', 'demoPrices', 'data.setups', 'bot_entries_today'):
            self.assertNotIn(retired, source)
        self.assertNotRegex(source, r'data\.trades(?!_today)')
        self.assertNotIn('LOCAL VISUAL SIMULATION', (ROOT / 'assets/office3d/scene-builder.js').read_text())
        self.assertIn('OFFICE STATUS', (ROOT / 'assets/office3d/scene-builder.js').read_text())


if __name__ == '__main__':
    unittest.main()
