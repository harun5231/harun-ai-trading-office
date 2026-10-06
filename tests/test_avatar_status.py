"""Actual worker phases drive the visual bridge; tests use no browser or network."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
import assert from 'node:assert/strict';
const RealDate=Date;let clock=RealDate.parse('2026-10-06T12:00:00Z');
globalThis.Date=class extends RealDate{constructor(...args){super(...(args.length?args:[clock]));}static now(){return clock;}};
const handlers=new Map(),timers=new Map();let timerId=0;
globalThis.window={addEventListener:(name,fn)=>handlers.set(name,fn),removeEventListener:(name,fn)=>{if(handlers.get(name)===fn)handlers.delete(name);}};
Object.defineProperty(globalThis,'document',{get(){throw Error('The bridge must not inspect dashboard DOM.');}});
globalThis.fetch=()=>{throw Error('The bridge must not make network requests.');};
globalThis.setTimeout=(fn,delay)=>{const handle={id:++timerId,unref(){}};timers.set(handle,{fn,delay});return handle;};
globalThis.clearTimeout=handle=>timers.delete(handle);
const {createStatusController}=await import('./assets/office3d/status-controller.js');
const roles=['market','neuro','risk','trading','position','reviewer','report','boss'];
const iso=(at=clock)=>new Date(at).toISOString();
const data=()=>({schema_version:2,source:'BINANCE_FUTURES',generated_at:iso(),robot_checked_at:iso(),account_checked_at:iso(),reports_checked_at:iso(),status:'WAITING',robot_on:true,active_positions:1,balance:'116.928',pnl_today:'3.75',trades_today:null,employees:[],events:[],execution_gateway:{connected:false,status:'NOT_CONNECTED',failure_code:'BINANCE_ORDER_GATEWAY_NOT_CONNECTED'}});
const emit=payload=>handlers.get('workerSnapshot')?.({detail:payload});
const bridge=createStatusController();
"""


class AvatarStatus(unittest.TestCase):
    def run_node(self, assertions):
        if shutil.which('node') is None:
            self.skipTest('Node.js is required for the visual status harness')
        result = subprocess.run(['node', '--input-type=module', '-e', HARNESS + assertions], cwd=ROOT,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unknown_state_has_no_fabricated_activity_or_account(self):
        self.run_node(r"""
for(const role of roles){assert.equal(bridge.getRole(role).state,'IDLE');assert.equal(bridge.getRole(role).known,false);assert.equal(bridge.getRole(role).active,false);}
assert.equal(bridge.getTelemetry().balance,null);assert.equal(bridge.getTelemetry().active_positions,null);assert.equal(bridge.getTelemetry().connected,false);
emit({...data(),schema_version:1});emit({...data(),status:'FORGED_ORDER_SENT'});assert.equal(bridge.diagnostics().malformedPayloads,2);assert.equal(bridge.getRole('trading').active,false);assert.equal(bridge.getRole('toString').known,false);
bridge.dispose();assert.equal(handlers.size,0);assert.equal(timers.size,0);
""")

    def test_real_phases_and_employee_work_are_read_only(self):
        self.run_node(r"""
emit({...data(),status:'SCREENING'});assert.equal(bridge.getRole('market').state,'WORKING');assert.equal(bridge.getRole('neuro').state,'IDLE');assert.equal(bridge.getRole('position').state,'WORKING');assert.equal(bridge.getRole('boss').state,'WORKING');
emit({...data(),status:'ANALYZING'});assert.equal(bridge.getRole('market').state,'IDLE');assert.equal(bridge.getRole('neuro').state,'WORKING');
emit({...data(),status:'VALIDATING'});assert.equal(bridge.getRole('risk').state,'WORKING');
emit({...data(),employees:[{id:'report',name:'Report Manager',status:'WORKING'}]});assert.equal(bridge.getRole('report').state,'WORKING');
const copy=bridge.getTelemetry();copy.execution_gateway.connected=true;copy.employees[0].status='IDLE';assert.equal(bridge.getTelemetry().execution_gateway.connected,false);assert.equal(bridge.getTelemetry().employees[0].status,'WORKING');
bridge.dispose();emit({...data(),status:'SCREENING'});assert.equal(bridge.diagnostics().disposed,true);assert.equal(handlers.size,0);
""")

    def test_blocked_gateway_never_animates_execution(self):
        self.run_node(r"""
emit({...data(),status:'EXECUTING',employees:[{id:'trading',name:'Trading Agent',status:'WORKING'}]});assert.equal(bridge.getRole('trading').state,'IDLE');assert.equal(bridge.getRole('trading').active,false);assert.match(bridge.getRole('trading').speech,/belum terhubung/);
emit({...data(),status:'EXECUTION_BLOCKED',execution_gateway:{connected:true,status:'CONNECTED'},employees:[{id:'trading',status:'WORKING'}]});assert.equal(bridge.getRole('trading').active,false);
emit({...data(),status:'EXECUTING',execution_gateway:{connected:true,status:'CONNECTED'}});assert.equal(bridge.getRole('trading').state,'WORKING');assert.match(bridge.getRole('trading').speech,/adapter/);assert.doesNotMatch(bridge.getRole('trading').speech,/dikirim|terkirim|sent/i);
bridge.dispose();
""")

    def test_robot_off_stops_all_avatars_but_keeps_read_only_account_values(self):
        self.run_node(r"""
emit({...data(),status:'OFF',robot_on:false,employees:roles.map(id=>({id,status:'WORKING'}))});for(const role of roles){assert.equal(bridge.getRole(role).state,'IDLE');assert.equal(bridge.getRole(role).active,false);assert.equal(bridge.getRole(role).known,true);}assert.match(bridge.getRole('position').speech,/Robot OFF/);assert.equal(bridge.getTelemetry().balance,'116.928');assert.equal(bridge.getTelemetry().active_positions,1);assert.ok(bridge.getTelemetry().employees.every(employee=>employee.status==='OFF'));
emit({...data(),status:'OFF',robot_on:false,active_positions:0});assert.equal(bridge.getRole('position').active,false);assert.equal(bridge.getRole('position').known,true);assert.equal(bridge.getRole('boss').active,false);
emit({...data(),status:'OFF',robot_on:false,active_positions:null});assert.equal(bridge.getRole('position').known,true);assert.equal(bridge.getRole('position').active,false);assert.equal(bridge.getTelemetry().active_positions,null);
emit({...data(),status:'ANALYZING',robot_on:false,employees:roles.map(id=>({id,status:'WORKING'}))});assert.ok(roles.every(role=>!bridge.getRole(role).active));
emit({...data(),status:'OFFLINE',active_positions:2});assert.equal(bridge.getRole('position').active,false);assert.equal(bridge.getTelemetry().balance,null);bridge.dispose();
""")

    def test_all_coordinator_terminal_and_gateway_states_keep_actual_account(self):
        self.run_node(r"""
for(const status of ['READY_FOR_EXECUTION','ENTRY_PENDING','POSITION_PROTECTED','CLOSED','INSUFFICIENT_ACTIONABLE_SETUPS','IDLE']){emit({...data(),status});assert.equal(bridge.getTelemetry().status,status);assert.equal(bridge.getTelemetry().balance,'116.928');assert.equal(bridge.getRole('position').state,'WORKING');assert.equal(bridge.getRole('position').known,true);assert.equal(bridge.getRole('trading').state,'IDLE');assert.equal(bridge.getRole('trading').active,false);for(const role of ['market','neuro','risk','reviewer','report'])assert.equal(bridge.getRole(role).active,false);}
assert.equal(bridge.diagnostics().malformedPayloads,0);emit({...data(),status:'UNKNOWN_COORDINATOR_STATE'});assert.equal(bridge.diagnostics().malformedPayloads,1);assert.equal(bridge.getTelemetry().status,'IDLE');bridge.dispose();
""")

    def test_receipt_does_not_refresh_old_account_or_phase_timestamps(self):
        self.run_node(r"""
const original=clock,payload={...data(),status:'SCREENING',account_checked_at:iso(clock-40000)};emit(payload);assert.equal(bridge.getRole('position').active,true);clock+=6000;assert.equal(bridge.getRole('position').active,false);assert.equal(bridge.getTelemetry().balance,null);assert.equal(bridge.getRole('market').active,true);
emit({...payload,generated_at:iso()});assert.equal(bridge.getRole('position').active,false);assert.equal(bridge.getTelemetry().receivedAt,iso());assert.equal(bridge.getTelemetry().robot_checked_at,iso(original));
clock=original+121000;emit({...payload,generated_at:iso()});assert.equal(bridge.getRole('market').active,false);assert.equal(bridge.getRole('market').known,false);assert.equal(bridge.getTelemetry().stale,true);bridge.dispose();
""")

    def test_stale_employee_status_and_historical_events_do_not_imply_work(self):
        self.run_node(r"""
emit({...data(),active_positions:0,employees:[{id:'report',status:'WORKING',checked_at:iso(clock-121000)}],events:[{at:iso(clock-1000),agent:'Trading Agent',state:'EXECUTING',message:'Historical adapter event'}]});assert.equal(bridge.getRole('report').active,false);assert.equal(bridge.getRole('trading').active,false);assert.equal(bridge.getRole('boss').active,false);assert.equal(bridge.getTelemetry().events.length,1);
const copied=bridge.getTelemetry();copied.events[0].message='changed';assert.equal(bridge.getTelemetry().events[0].message,'Historical adapter event');clock+=121000;assert.equal(bridge.getTelemetry().events.length,0);bridge.dispose();
""")

    def test_legacy_dom_and_order_speech_are_absent(self):
        source = (ROOT / 'assets/office3d/status-controller.js').read_text()
        for legacy in ('DRY_RUN', 'APPROVED', 'ticket', 'tiket', 'kirim manual', 'dikirim manual', 'MutationObserver', 'getElementById', 'querySelector', 'fetch('):
            self.assertNotIn(legacy, source)


if __name__ == '__main__':
    unittest.main()
