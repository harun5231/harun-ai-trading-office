"""Frontend-only swept-body navigation and five-colleague traffic regressions."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
import assert from 'node:assert/strict';
import {NavigationMesh} from './assets/office3d/navigation.js';
import {OfficeAnimationController} from './assets/office3d/animation-controller.js';
import {BOUNDS,COLLIDERS,STATION_CONFIG,IDLE_SLOTS,LOUNGE_SLOTS} from './assets/office3d/layout.js';
globalThis.fetch=()=>{throw Error('Visual navigation must not call a trading service.');};
const distance=(a,b)=>Math.hypot(a.x-b.x,a.z-b.z);
const point=p=>({x:p.x,z:p.z});
// Minimum separation throughout a frame, not merely at its endpoints.
function sweptSeparation(a0,a1,b0,b1){
  const x=a0.x-b0.x,z=a0.z-b0.z;
  const dx=(a1.x-a0.x)-(b1.x-b0.x),dz=(a1.z-a0.z)-(b1.z-b0.z);
  const d=dx*dx+dz*dz,t=d?Math.max(0,Math.min(1,-(x*dx+z*dz)/d)):0;
  return Math.hypot(x+t*dx,z+t*dz);
}
const room={minX:-10,maxX:10,minZ:-10,maxZ:10};
function actor(station){
  const position={x:0,y:0,z:0,set(x,y,z){Object.assign(this,{x,y,z});}};
  const avatar={available:true,root:{position,rotation:{y:0}},headAnchor:{matrixWorld:{elements:Array(16).fill(0)}},
    pose(dt,time,value){this.lastPose=value;this.root.updateMatrixWorld();}};
  avatar.root.updateMatrixWorld=()=>{
    const back=.35*(avatar.lastPose?.sit??1),m=avatar.headAnchor.matrixWorld.elements;
    m[12]=position.x-Math.sin(avatar.root.rotation.y)*back;
    m[13]=1.5;m[14]=position.z-Math.cos(avatar.root.rotation.y)*back;
  };
  return {id:station.role,role:station.role,station,avatar};
}
"""


class OfficeNavigation(unittest.TestCase):
    def run_node(self, assertions):
        if shutil.which("node") is None:
            self.skipTest("Node.js is required for the office navigation harness")
        result = subprocess.run(
            ["node", "--input-type=module", "-e", HARNESS + assertions],
            cwd=ROOT, text=True, capture_output=True, timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_body_volume_and_full_sweep_prevent_large_step_tunnelling(self):
        self.run_node(r"""
for(const collider of [
  {id:'thin-wall',x:0,z:0,width:.02,depth:5},
  {id:'table-leg',x:0,z:0,radius:.055},
  {id:'rotated-chair-back',x:0,z:0,width:2,depth:.08,rotation:Math.PI/4},
]){
  const nav=new NavigationMesh({bounds:room,colliders:[collider],radius:.46});
  const a={id:'colleague',x:-3,z:0,radius:.46},goal={x:3,z:0};
  assert.ok(nav.isWalkable(a)&&nav.isWalkable(goal));
  assert.equal(nav.segmentClear(a,goal),false,collider.id);
  nav.beginFrame();const next=nav.move(a,goal);
  assert.equal(next.blocked,true,collider.id);
  assert.ok(next.x<0,`${collider.id}: body jumped across an obstacle`);
}
const nav=new NavigationMesh({bounds:room,colliders:[{id:'desk',x:0,z:0,width:2,depth:2}],radius:.46});
assert.equal(nav.isWalkable({x:1.2,z:0}),false,'a clear centre is insufficient for the shoulders');
assert.equal(nav.segmentClear({x:-3,z:1.2},{x:3,z:1.2}),false,'body must clear tabletop corners');
assert.equal(nav.segmentClear({x:-3,z:1.6},{x:3,z:1.6}),true);
assert.equal(nav.segmentClear({x:0,z:0},{x:20,z:0}),false,'room boundaries constrain the complete sweep');
""")

    def test_crossing_reservation_checks_earlier_actor_swept_volume(self):
        self.run_node(r"""
const nav=new NavigationMesh({bounds:room,radius:.46});
const a={id:'a',x:-2,z:0,radius:.46,priority:0};
const b={id:'b',x:0,z:-2,radius:.46,priority:1};
nav.beginFrame();
const a0=point(a),b0=point(b);
Object.assign(a,nav.move(a,{x:2,z:0},[b]));
Object.assign(b,nav.move(b,{x:0,z:2},[a]));
assert.ok(distance(a,b)>.92);
assert.ok(sweptSeparation(a0,a,b0,b)>.92,'crossing paths overlapped between frames');
assert.equal(nav.reservations.size,2,'both swept paths remain reserved until the next frame');
nav.beginFrame();assert.equal(nav.reservations.size,0);
""")

    def test_colleagues_pass_without_overlap_or_permanent_yield_deadlock(self):
        self.run_node(r"""
const nav=new NavigationMesh({bounds:room,radius:.46});
const actors=[{id:'market',x:-4,z:0,radius:.46,priority:0},{id:'risk',x:4,z:0,radius:.46,priority:1}];
const goals=[{x:4,z:0},{x:-4,z:0}];
let yielded=false,finished=false;
for(let frame=0;frame<1800;frame++){
  const previous=actors.map(point);nav.beginFrame();
  for(let i=0;i<2;i++){
    const a=actors[i],goal=goals[i],remaining=distance(a,goal);
    if(remaining<.01)continue;
    const step=Math.min(remaining,frame%53===0?.3:.05);
    const next=nav.move(a,{x:a.x+(goal.x-a.x)/remaining*step,z:a.z+(goal.z-a.z)/remaining*step},actors);
    yielded ||= /YIELD/.test(next.reason);Object.assign(a,next);
  }
  assert.ok(sweptSeparation(previous[0],actors[0],previous[1],actors[1])>=.92-1e-6,`overlap at frame ${frame}`);
  if(actors.every((a,i)=>distance(a,goals[i])<.01)){finished=true;break;}
}
assert.ok(yielded,'opposing traffic never exercised yielding');
assert.ok(finished,`head-on traffic did not clear: ${JSON.stringify(actors)}`);
""")

    def test_occupied_exit_holds_seated_until_clear_and_work_can_cancel_departure(self):
        self.run_node(r"""
const a=actor(STATION_CONFIG[0]),nav=new NavigationMesh({bounds:BOUNDS,colliders:COLLIDERS,radius:.46});
let active=false,blocked=true;
const controller=new OfficeAnimationController({actors:[a],navigation:nav,
  status:{getRole(){return {known:true,active};}},random:()=>.3});
a.handsReady=true;a.trayOffset=0; // Already settled, hands resting at this PC.
const visitor={id:'visitor',...a.station.entry,radius:.46,priority:1};
controller.others=()=>blocked?[visitor]:[];
const goal=controller.idleGoal(IDLE_SLOTS.find(s=>s.id==='coffee-a'));
controller.assign(a,goal);
const start=point(a.avatar.root.position);
for(let frame=1;frame<=30;frame++){
  controller.update(.05,frame*.05);
  assert.equal(a.mode,'WAITING_EXIT','must check free exit space before rising');
  assert.equal(a.sit,1);
  assert.equal(distance(start,a.avatar.root.position),0);
  assert.equal(a.avatar.lastPose.state,'IDLE');
  assert.equal(a.avatar.lastPose.activity,null,'future coffee goal must not play before departure');
}
// A task arriving while still seated should resume this PC directly.
active=true;controller.update(.05,1.55);
assert.equal(a.mode,'AT_DESK');assert.equal(a.avatar.lastPose.state,'WORKING');
assert.equal(a.sit,1);
active=false;controller.assign(a,goal);controller.update(.05,1.6);
assert.equal(a.mode,'WAITING_EXIT');
blocked=false;controller.update(.05,1.65);
assert.equal(a.mode,'RETRACTING_TRAY');assert.equal(a.sit,1);
for(let frame=0;frame<10;frame++)controller.update(.05,1.7+frame*.05);
assert.equal(a.mode,'STANDING_UP');assert.ok(a.sit<1);
// A newly blocked exit during an interrupted sit must finish sitting gently,
// rather than switching to a standing idle clip in the middle of the chair.
blocked=true;a.handsReady=false;a.trayOffset=.42;a.sit=.35;a.mode='SITTING_DOWN';a.phase=.4;a.destination=controller.desk(a);
for(let frame=0;frame<20;frame++){
  const previous=a.sit;controller.update(.05,2+frame*.05);
  assert.equal(a.mode,'WAITING_EXIT');assert.ok(a.sit>=previous&&a.sit-previous<=.041);
  if(a.sit<1){
    assert.equal(a.avatar.lastPose.state,'SIT_DOWN');
    const t=a.avatar.lastPose.transitionProgress;
    assert.ok(Math.abs(t*t*(3-2*t)-a.sit)<1e-8);
  }
}
assert.equal(a.sit,1);
""")

    def test_docking_waits_inside_contact_corridor_then_aligns_before_sitting(self):
        self.run_node(r"""
const a=actor(STATION_CONFIG[0]),nav=new NavigationMesh({bounds:BOUNDS,colliders:COLLIDERS,radius:.46});
const controller=new OfficeAnimationController({actors:[a],navigation:nav,
  status:{getRole(){return {known:true,active:true};}},random:()=>.3});
const goal=controller.desk(a);
a.currentSeat=goal;a.destination=goal;a.mode='DOCKING';a.sit=0;
a.route=[point(a.station.approach),point(a.station.dock)];a.routeIndex=1;
a.routeIgnore=goal.ignoreIds;
a.avatar.root.position.set(a.station.approach.x,0,a.station.approach.z);
let blocked=true;
controller.others=()=>blocked?[{id:'crossing-colleague',...a.station.dock,radius:.46,priority:-1}]:[];
let time=0;
for(let frame=0;frame<100;frame++){
  time+=.05;controller.update(.05,time);
  assert.equal(a.mode,'DOCKING');assert.equal(a.sit,0);
  assert.equal(a.avatar.root.position.z,a.station.dock.z,'docking may not yield sideways into furniture');
  assert.ok(a.avatar.root.position.x>a.station.dock.x+.92);
}
assert.equal(nav.lastMotion.reason,'CONTACT_WAIT');
blocked=false;
let aligned=false,seated=false;
for(let frame=0;frame<300;frame++){
  time+=.05;controller.update(.05,time);
  if(a.mode==='ALIGNING')aligned=true;
  if(a.mode==='SITTING_DOWN'){
    assert.ok(aligned,'contact must finish before orientation alignment');
    const angle=a.station.yaw-a.avatar.root.rotation.y;
    assert.ok(Math.abs(Math.atan2(Math.sin(angle),Math.cos(angle)))<.025);
  }
  if(a.mode==='AT_DESK'){seated=true;break;}
}
assert.ok(seated,'blocked docking must resume once the colleague clears');
assert.equal(a.avatar.lastPose.state,'WORKING');
""")

    def test_keyboard_retracts_before_hands_move_and_extends_after_alignment(self):
        self.run_node(r"""
const a=actor(STATION_CONFIG[0]),nav=new NavigationMesh({bounds:BOUNDS,colliders:COLLIDERS,radius:.46});
let active=true,clock=0;
const controller=new OfficeAnimationController({actors:[a],navigation:nav,
  status:{getRole(){return {known:true,active};}},random:()=>.3});
const tick=()=>{clock+=.05;controller.update(.05,clock);};
tick();assert.equal(a.mode,'ALIGN_HANDS');
while(a.mode==='ALIGN_HANDS'){
  assert.equal(a.trayOffset,.42,'keep tray clear throughout the lap-to-keyboard arm blend');
  assert.equal(a.avatar.lastPose.deskHands,true);assert.equal(a.avatar.lastPose.state,'IDLE');tick();
}
assert.ok(clock>=.35);assert.equal(a.mode,'EXTENDING_TRAY');
while(a.mode==='EXTENDING_TRAY'){
  assert.equal(a.avatar.lastPose.deskHands,true,'hands stay raised while tray approaches');
  assert.equal(a.sit,1);tick();
}
assert.equal(a.mode,'AT_DESK');assert.equal(a.trayOffset,0);assert.equal(a.avatar.lastPose.state,'WORKING');
active=false;controller.assign(a,controller.idleGoal(IDLE_SLOTS.find(s=>s.id==='coffee-a')));
tick();assert.equal(a.mode,'RETRACTING_TRAY');
let before=0;
while(a.mode==='RETRACTING_TRAY'){
  assert.equal(a.sit,1);assert.equal(a.avatar.lastPose.deskRest,true);
  assert.ok(a.trayOffset>=before);before=a.trayOffset;tick();
}
assert.equal(a.mode,'STANDING_UP');assert.equal(a.trayOffset,.42);
assert.equal(a.avatar.lastPose.state,'STAND_UP');
""")

    def test_lateral_yield_replans_around_seated_head_without_creeping(self):
        self.run_node(r"""
const station={role:'market',name:'market',gender:'male',dock:{x:-3,z:0},entry:{x:-3,z:-1},yaw:0,seat:{y:.43}};
const a=actor(station),nav=new NavigationMesh({bounds:room});
const controller=new OfficeAnimationController({actors:[a],navigation:nav,
  status:{getRole(){return {known:true,active:false};}},random:()=>.3});
const goal={id:'coffee',kind:'COFFEE',entry:{x:3,z:0},position:{x:3,z:0},yaw:0};
controller.assign(a,goal);a.currentSeat=null;a.sit=0;a.mode='WALKING';a.destination=goal;
a.route=[{x:-3,z:0},{x:3,z:0}];a.routeIndex=1;
// The seat body is off the route, but its leaning head obstructs the corridor.
const people=[{id:'seated',x:0,z:1.1,radius:.46,priority:0},
  {id:'seated:head',x:0,z:.35,radius:.39,priority:0}];
controller.others=()=>people;
let arrived=false;
for(let time=0;time<16;time+=.05){
  const previous=point(a.avatar.root.position);controller.update(.05,time);
  for(const person of people)assert.ok(nav.segmentClear(previous,a.avatar.root.position,{actors:[person]}));
  if(a.mode==='IDLE_ACTIVITY'){arrived=true;break;}
}
assert.ok(arrived,'lateral avoidance must not creep indefinitely around a stationary head');
assert.ok(controller.progressReplans>0,'lack of forward progress must trigger a route around complete occupied volumes');
// Start inside comfort padding but outside the actual body: safe separating
// movement must remain available after a head leans into that padding.
a.avatar.root.position.set(0,0,0);a.currentSeat=null;
controller.others=()=>[{id:'nearby',x:.935,z:0,radius:.46,priority:0}];
assert.ok(controller.plan(a,{x:-2,z:0}),'comfort-gap entry must not cause permanent WAITING_PATH');
const prior=point(a.avatar.root.position);controller.follow(a,.05);
assert.ok(a.avatar.root.position.x<prior.x,'the escape must move away from the occupied body');
""")

    def test_five_avatars_travel_concurrently_then_return_to_their_own_pc(self):
        self.run_node(r"""
for(const reducedMotion of [false,true]){
  const actors=STATION_CONFIG.map(actor),active=new Set();
  const status={getRole(role){return {known:true,active:active.has(role),robot_on:true};}};
  const nav=new NavigationMesh({bounds:BOUNDS,colliders:COLLIDERS,radius:.46});
  const controller=new OfficeAnimationController({actors,navigation:nav,status,reducedMotion,
    idleSlots:IDLE_SLOTS,loungeSlots:LOUNGE_SLOTS,random:()=>.37});
  const requested=['pool-a','coffee-a','discussion-c',LOUNGE_SLOTS[1].id,'coffee-b'];
  const slots=[...IDLE_SLOTS,...LOUNGE_SLOTS];
  for(let i=0;i<actors.length;i++){
    const slot=slots.find(slot=>slot.id===requested[i]);assert.ok(slot,requested[i]);
    controller.assign(actors[i],controller.idleGoal(slot,slot.kind||'LOUNGE'));
  }
  let clock=0,concurrent=0;
  function tick(frame){
    // Include dropped frames to prove the entire motion is checked.
    const dt=frame%47===0?.65:frame%13===0?.16:.05;
    const before=actors.map(a=>({position:point(a.avatar.root.position),ignore:[...(a.routeIgnore||[])],seat:a.currentSeat}));
    clock+=dt;controller.update(dt,clock);
    concurrent=Math.max(concurrent,actors.filter(a=>a.speed>.01).length);
    for(let i=0;i<actors.length;i++){
      const a=actors[i],old=before[i],next=a.avatar.root.position;
      if(!['IDLE_ACTIVITY','RESTING'].includes(a.mode))
        assert.equal(a.avatar.lastPose.activity,null,`${a.id} played future goal ${a.destination?.kind} while ${a.mode}`);
      if(a.mode==='ALIGNING') { assert.equal(a.avatar.lastPose.state,'IDLE'); assert.equal(a.sit,0); }
      assert.ok(distance(old.position,next)<=dt*1.1+.02,`${a.id} teleported`);
      const ignored=new Set([...old.ignore,...(a.routeIgnore||[])]);
      for(const seat of [old.seat,a.currentSeat])
        for(const id of seat?.ignoreIds||(seat?.colliderId?[seat.colliderId]:[]))ignored.add(id);
      if(distance(old.position,next)>.00001)
        assert.ok(nav.segmentClear(old.position,next,{ignoreIds:[...ignored]}),`${a.id} crossed static furniture`);
      for(let j=i+1;j<actors.length;j++)
        assert.ok(sweptSeparation(old.position,next,before[j].position,actors[j].avatar.root.position)>=.92-1e-5,
          `${a.id}/${actors[j].id} swept overlap at ${clock}`);
      // Keep reached fixture goals reserved until all five have arrived.
      if(['IDLE_ACTIVITY','SOCIAL','RESTING'].includes(a.mode))a.idleUntil=Infinity;
    }
    const reservations=[...controller.assignments.values()].map(g=>g.id);
    assert.equal(new Set(reservations).size,reservations.length,'two actors reserved one interaction point');
  }
  const arrived=()=>actors.every((a,i)=>a.destination?.id===requested[i]&&['IDLE_ACTIVITY','SOCIAL','RESTING'].includes(a.mode));
  for(let frame=0;frame<4000&&!arrived();frame++)tick(frame);
  assert.ok(arrived(),`leisure deadlock: ${JSON.stringify(controller.diagnostics())}`);
  assert.equal(concurrent,5,'the fixture must exercise all five moving together');
  for(const a of actors)active.add(a.role);
  const deadline=clock+100;
  for(let frame=0;frame<4000&&clock<deadline;frame++){
    tick(frame);
    if(actors.every(a=>a.mode==='AT_DESK'))break;
  }
  for(const a of actors){
    assert.equal(a.mode,'AT_DESK',`${a.id} could not return to work`);
    assert.equal(a.destination.id,`desk-${a.role}`,'visiting cannot transfer PC ownership');
    assert.equal(a.avatar.lastPose.state,'WORKING');
    assert.ok(distance(a.avatar.root.position,a.station.dock)<.02);
  }
}
""")


if __name__ == "__main__":
    unittest.main()
