"""Read-only office motion: leisure variety, real-work preemption, no ghost actors."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
import assert from 'node:assert/strict';
import {OfficeAnimationController} from './assets/office3d/animation-controller.js';
import {NavigationMesh} from './assets/office3d/navigation.js';
globalThis.fetch=()=>{throw Error('Visual scheduling must never call a provider or worker.');};
function actor(id, x=0, z=-5) {
  const position={x:0,y:0,z:0,set(x,y,z){Object.assign(this,{x,y,z});}};
  const avatar={available:true,root:{position,rotation:{y:0}},pose(dt,time,value){this.lastPose=value;}};
  return {id,role:id,avatar,station:{name:id,x,z,yaw:Math.PI,
    dock:{x,z:z+1.35},entry:{x:x+1.08,z:z+1.35},seat:{y:.78}}};
}
function office({actors=[actor('market')], idleSlots=[], loungeSlots=[], known=true, random=()=>.27}={}) {
  const activity=new Map();
  const status={getRole(id){return {known,active:activity.get(id)===true,robot_on:true};}};
  const colliders=actors.map(a=>({id:`chair-${a.role}`,x:a.station.dock.x,z:a.station.dock.z,radius:.5}));
  for(const seat of loungeSlots) colliders.push({id:seat.colliderId,x:seat.position.x,z:seat.position.z,width:2,depth:1});
  const navigation=new NavigationMesh({bounds:{minX:-15,maxX:15,minZ:-15,maxZ:15},colliders});
  const controller=new OfficeAnimationController({actors,navigation,status,idleSlots,loungeSlots,random});
  let clock=0;
  function tick(seconds=.05){clock+=seconds;controller.update(seconds,clock);}
  function until(predicate,seconds=120){for(let i=0;i<seconds/.05;i++){tick();if(predicate())return;}assert.fail('motion did not settle');}
  return {controller,activity,status,navigation,tick,until,actors};
}
const coffee={id:'coffee',kind:'COFFEE',position:{x:-8,z:3},yaw:0};
const pool={id:'pool',kind:'POOL',position:{x:8,z:3},yaw:Math.PI/2};
const sofa={id:'sofa-seat',position:{x:0,z:8},entry:{x:0,z:6.8},yaw:Math.PI,seatHeight:.65,colliderId:'sofa'};
"""


class OfficeAnimation(unittest.TestCase):
    def run_node(self, assertions):
        if shutil.which('node') is None:
            self.skipTest('Node.js is required for the office motion harness')
        result = subprocess.run(['node', '--input-type=module', '-e', HARNESS + assertions],
                                cwd=ROOT, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unknown_or_missing_models_never_walk_or_work(self):
        self.run_node(r"""
const unknown=office({known:false,idleSlots:[coffee,pool]});
for(let i=0;i<300;i++)unknown.tick(.05);
assert.equal(unknown.actors[0].mode,'AT_DESK');
assert.equal(unknown.actors[0].avatar.lastPose.state,'IDLE');
const a=actor('market');a.avatar.available=false;
const missing=office({actors:[a],idleSlots:[coffee]});missing.activity.set('market',true);
for(let i=0;i<300;i++)missing.tick(.05);
assert.equal(a.mode,'AT_DESK');assert.equal(a.avatar.lastPose,undefined);
assert.equal(missing.controller.diagnostics().states[0].available,false);
assert.equal(missing.controller.assignments.size,0);
""")

    def test_robot_on_does_not_create_work_and_idle_choices_vary(self):
        self.run_node(r"""
let seed=123456;
const random=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};
const env=office({idleSlots:[coffee,pool],loungeSlots:[sofa],random});
const a=env.actors[0],seen=[];
for(let i=0;i<15;i++){
  env.until(()=>['IDLE_ACTIVITY','RESTING'].includes(a.mode));
  seen.push(a.destination.kind);
  assert.notEqual(a.avatar.lastPose.state,'WORKING');
  if(i<14){const old=a.destination.id;env.until(()=>a.destination.id!==old);}
}
assert.equal(new Set(seen).size,3);
for(let i=1;i<seen.length;i++)assert.notEqual(seen[i],seen[i-1]);
""")

    def test_real_work_preempts_pool_and_starts_working_only_at_own_desk(self):
        self.run_node(r"""
const env=office({idleSlots:[pool]});const a=env.actors[0];
env.until(()=>a.mode==='IDLE_ACTIVITY');
assert.equal(a.destination.kind,'POOL');env.activity.set(a.role,true);env.tick();
assert.equal(a.destination.kind,'DESK');assert.equal(a.mode,'WALKING');
assert.ok(a.priority<0);assert.notEqual(a.avatar.lastPose.state,'WORKING');
env.until(()=>a.mode==='AT_DESK');
assert.equal(a.destination.id,'desk-market');assert.equal(a.avatar.lastPose.state,'WORKING');
assert.ok(Math.hypot(a.avatar.root.position.x-a.station.dock.x,a.avatar.root.position.z-a.station.dock.z)<.02);
""")

    def test_real_work_cancels_leisure_docking_and_sitting(self):
        self.run_node(r"""
for(const mode of ['DOCKING','SITTING_DOWN']){
  const env=office({loungeSlots:[sofa]});const a=env.actors[0];
  env.until(()=>a.mode===mode);env.activity.set(a.role,true);env.tick();
  assert.equal(a.destination.kind,'DESK');
  assert.notEqual(a.mode,'RESTING');
  env.until(()=>a.mode==='AT_DESK');
  assert.equal(a.avatar.lastPose.state,'WORKING');
}
""")

    def test_discussion_requires_idle_partner_and_reservations_are_distinct(self):
        self.run_node(r"""
const social=[{id:'talk-a',kind:'SOCIAL',position:{x:-2,z:3}},
 {id:'talk-b',kind:'SOCIAL',position:{x:0,z:3}}];
const solo=office({idleSlots:social});for(let i=0;i<300;i++)solo.tick();
assert.equal(solo.actors[0].mode,'AT_DESK');
const env=office({actors:[actor('market',-3),actor('neuro',3)],idleSlots:social});
env.until(()=>env.actors.every(a=>a.mode==='SOCIAL'));
const [a,b]=env.actors;assert.notEqual(a.destination.id,b.destination.id);
assert.equal(a.destination.partnerId,b.id);assert.equal(b.destination.partnerId,a.id);
env.activity.set(a.role,true);env.tick(.55);
assert.equal(a.destination.kind,'DESK');assert.notEqual(b.destination.kind,'SOCIAL');
""")

    def test_visits_do_not_interrupt_a_busy_harun(self):
        self.run_node(r"""
const visit={id:'harun-guest',kind:'VISIT',position:{x:-3,z:5},yaw:Math.PI/2};
const env=office({actors:[actor('market',-5),actor('boss',0,4)],idleSlots:[visit]});
env.activity.set('boss',true);for(let i=0;i<300;i++)env.tick();
assert.ok(env.actors.every(a=>a.destination.kind==='DESK'));
env.activity.set('boss',false);
env.until(()=>env.actors[0].destination.kind==='VISIT');
assert.equal(env.actors[1].mode,'AT_DESK');
env.activity.set('boss',true);env.tick(.55);
assert.equal(env.actors[0].destination.kind,'DESK');
assert.equal(env.actors[1].avatar.lastPose.state,'WORKING');
""")

    def test_actual_office_all_workstations_reach_every_idle_area(self):
        self.run_node(r"""
const {BOUNDS,COLLIDERS,STATION_CONFIG,IDLE_SLOTS,LOUNGE_SLOTS}=
  await import('./assets/office3d/layout.js');
const nav=new NavigationMesh({bounds:BOUNDS,colliders:COLLIDERS,radius:.46,cellSize:.32});
for(const station of STATION_CONFIG){
  const ignoreIds=[`chair-${station.role}`];
  const exit=nav.findPath(station.dock,station.entry,{ignoreIds});
  assert.ok(nav.validatePath(exit,{ignoreIds}),`blocked workstation: ${station.name}`);
  for(const slot of [...IDLE_SLOTS,...LOUNGE_SLOTS]){
    const path=nav.findPath(station.entry,slot.entry||slot.position);
    assert.ok(nav.validatePath(path),`${station.name} cannot reach ${slot.id}`);
    if(slot.colliderId){
      const seating=nav.findPath(slot.entry,slot.position,{ignoreIds:[slot.colliderId]});
      assert.ok(nav.validatePath(seating,{ignoreIds:[slot.colliderId]}),`blocked seating: ${slot.id}`);
    }
  }
}
""")


if __name__ == '__main__':
    unittest.main()
