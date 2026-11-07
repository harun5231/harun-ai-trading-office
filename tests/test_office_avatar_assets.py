"""Offline human-asset contracts; test doubles never enter production assets."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

let loadCalls = 0;
let loadModel = async () => { throw Error('Unexpected asset request'); };
const cleaned = { geometry: 0, material: 0, texture: 0, skeleton: 0, stopped: 0, uncached: 0 };
globalThis.fetch = () => { throw Error('Network is forbidden in asset contract tests'); };
class Vector3 {
  constructor(x=0,y=0,z=0) { this.set(x,y,z); }
  set(x,y,z) { Object.assign(this,{x,y,z}); return this; }
  copy(value) { return this.set(value.x,value.y,value.z); }
}
class Group {
  constructor() {
    this.children=[]; this.position=new Vector3(); this.rotation={y:0};
    this.scale={multiplyScalar(){}}; this.visible=true; this.userData={};
  }
  add(child) { child.removeFromParent?.(); this.children.push(child); child.parent=this; }
  removeFromParent() {
    if (this.parent) this.parent.children=this.parent.children.filter(child=>child!==this);
    this.parent=null;
  }
  clear() { for (const child of this.children) child.parent=null; this.children=[]; }
  traverse(fn) { fn(this); for (const child of this.children) child.traverse(fn); }
  getObjectByName(name) {
    if(this.name===name) return this;
    for(const child of this.children) { const found=child.getObjectByName(name); if(found)return found; }
  }
  getWorldPosition(out) {
    out.copy(this.position);
    for(let parent=this.parent;parent;parent=parent.parent) {
      out.x+=parent.position.x; out.y+=parent.position.y; out.z+=parent.position.z;
    }
    return out;
  }
  worldToLocal(out) {
    const offset=this.getWorldPosition(new Vector3());
    out.x-=offset.x; out.y-=offset.y; out.z-=offset.z; return out;
  }
  updateMatrixWorld() {}
}
class Bone extends Group { constructor(){super();this.isBone=true;} }
class LoadingManager { setURLModifier(fn) { this.modify=fn; } }
class Box3 { setFromObject(){this.min={y:0};this.max={y:1.82};return this;} }
class AnimationMixer {
  constructor(root){this.root=root;this.actions=new Map();globalThis.__lastOfficeMixer=this;}
  clipAction(clip) {
    if(this.actions.has(clip.name)) return this.actions.get(clip.name);
    const action={reset(){return this;},setLoop(){return this;},setEffectiveWeight(){return this;},
      play(){return this;},crossFadeFrom(){return this;},getClip(){return clip;}};
    this.actions.set(clip.name,action); return action;
  }
  update() {}
  stopAllAction(){cleaned.stopped++;}
  uncacheRoot(){cleaned.uncached++;}
}
class GLTFLoader {
  constructor(manager){this.manager=manager;}
  loadAsync(url){loadCalls++;return loadModel(url,this.manager);}
}
globalThis.__officeTestThree={Group,Vector3,LoadingManager,Box3,AnimationMixer,LoopOnce:1,LoopRepeat:2};
globalThis.__officeTestLoader={GLTFLoader};
const manifestPath=path.resolve('assets/office3d/models/manifest.js');
const manifest=await import(pathToFileURL(manifestPath));
globalThis.__officeTestManifest=manifest;
const avatarPath=path.resolve('assets/office3d/avatars.js');
const source=fs.readFileSync(avatarPath,'utf8')
  .replace("import * as THREE from 'three';",'const THREE=globalThis.__officeTestThree;')
  .replace("import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';",'const {GLTFLoader}=globalThis.__officeTestLoader;')
  .replace("import { AVATAR_MODELS, REQUIRED_MODEL_CLIPS } from './models/manifest.js';",'const {AVATAR_MODELS,REQUIRED_MODEL_CLIPS}=globalThis.__officeTestManifest;')
  .replaceAll('import.meta.url',JSON.stringify(pathToFileURL(avatarPath).href));
const {createAvatar}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const descriptor=(overrides={})=>({url:'./models/test-only.glb',referenceReviewed:true,height:1.82,headBone:'Head',
  license:{name:'TEST_ONLY',sourceUrl:'https://example.invalid/test-license',file:'test-only.txt',allowsWebUse:true,allowsPublicRedistribution:true},
  clips:Object.fromEntries(manifest.REQUIRED_MODEL_CLIPS.map(state=>[state,state])),...overrides});
function fixture({head='bone'}={}) {
  const scene=new Group();
  const actualHead=new Bone(); actualHead.name='Head'; actualHead.position.y=1.45;
  const namedHead=head==='node'?new Group():actualHead;
  namedHead.name=head==='missing'?'Other':head==='detached'?'Other':'Head';
  scene.add(namedHead);
  if(head==='detached') { const detached=new Bone();detached.name='Head';scene.add(detached); }
  const mesh=new Group(); mesh.isMesh=true; mesh.isSkinnedMesh=true;
  mesh.skeleton={bones:[actualHead],dispose(){cleaned.skeleton++;}};
  mesh.geometry={attributes:{skinIndex:{},skinWeight:{}},dispose(){cleaned.geometry++;}};
  mesh.material={map:{isTexture:true,dispose(){cleaned.texture++;}},dispose(){cleaned.material++;}};
  scene.add(mesh);
  return {scene,head:actualHead,animations:manifest.REQUIRED_MODEL_CLIPS.map(name=>({name,duration:1,tracks:[{}]}))};
}
"""


class OfficeAvatarAssets(unittest.TestCase):
    def run_node(self, assertions):
        if shutil.which('node') is None:
            self.skipTest('Node.js is required for the offline asset harness')
        result = subprocess.run(['node', '--input-type=module', '-e', HARNESS + assertions],
                                cwd=ROOT, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_models_stay_invisible_and_ignore_poses_without_requests(self):
        self.run_node(r"""
for(const role of ['market','neuro','risk','trading','boss']) {
  const avatar=createAvatar({id:role,role,model:descriptor({url:''})}); await avatar.ready;
  assert.equal(avatar.available,false); assert.equal(avatar.root.visible,false);
  assert.equal(avatar.diagnostics().assetState,'MISSING_ASSET');
  assert.equal(avatar.diagnostics().meshes,0);
  assert.equal(avatar.root.children.filter(node=>node.isMesh).length,0);
  const before=avatar.diagnostics();
  avatar.pose(1/60,100,{state:'WORKING',sit:1});
  assert.deepEqual(avatar.diagnostics(),before);
  const parent=new Group();parent.add(avatar.root);avatar.dispose();avatar.dispose();
  assert.equal(avatar.diagnostics().assetState,'DISPOSED');
  assert.equal(avatar.root.children.length,0);assert.equal(parent.children.length,0);
}
assert.equal(loadCalls,0);
""")

    def test_installed_manifest_has_local_rigged_assets_with_all_ten_clips(self):
        self.run_node(r"""
assert.equal(manifest.avatarAssetReadiness().ready,true);
assert.deepEqual(Object.keys(manifest.AVATAR_MODELS).sort(),['boss','market','neuro','risk','trading']);
for(const spec of Object.values(manifest.AVATAR_MODELS)) {
  const data=fs.readFileSync(path.resolve('assets/office3d',spec.url));
  assert.equal(data.toString('utf8',0,4),'glTF');
  const gltf=JSON.parse(data.toString('utf8',20,20+data.readUInt32LE(12)));
  assert.equal(gltf.animations.length,10);
  assert.deepEqual(gltf.animations.map(value=>value.name).sort(),[...manifest.REQUIRED_MODEL_CLIPS].sort());
  assert.ok(gltf.nodes.some(node=>node.name===spec.headBone));
  assert.ok(gltf.skins.some(skin=>skin.joints.length>=54));
  assert.ok(gltf.images.length>0 && gltf.images.every(image=>image.bufferView!==undefined));
  assert.ok(gltf.meshes.some(mesh=>mesh.extras?.targetNames?.includes('officeBlink')));
  assert.ok(fs.existsSync(spec.license.file));
}
""")

    def test_head_bone_name_is_required_before_loading(self):
        self.run_node(r"""
for(const headBone of [undefined,null,'','   ']) {
  const avatar=createAvatar({role:'market',model:descriptor({headBone})});await avatar.ready;
  assert.equal(avatar.diagnostics().failureCode,'HEAD_BONE_REQUIRED');
  assert.equal(avatar.available,false);assert.equal(avatar.root.visible,false);avatar.dispose();
}
assert.equal(loadCalls,0);
""")

    def test_head_anchor_must_be_a_bone_of_the_loaded_skin(self):
        self.run_node(r"""
for(const [head,code] of [['missing','HEAD_BONE_MISSING'],['node','HEAD_BONE_INVALID'],['detached','HEAD_BONE_INVALID']]) {
  loadModel=async()=>fixture({head});
  const avatar=createAvatar({role:'market',model:descriptor()});await avatar.ready;
  assert.equal(avatar.diagnostics().failureCode,code);
  assert.equal(avatar.available,false);assert.equal(avatar.root.visible,false);avatar.dispose();
}
assert.equal(loadCalls,3);
assert.equal(cleaned.geometry,3);assert.equal(cleaned.material,3);
assert.equal(cleaned.texture,3);assert.equal(cleaned.skeleton,3);
""")

    def test_loaded_rig_tracks_animated_head_and_releases_resources(self):
        self.run_node(r"""
const gltf=fixture();loadModel=async()=>gltf;
const avatar=createAvatar({role:'market',model:descriptor()});await avatar.ready;
assert.equal(avatar.available,true);assert.equal(avatar.root.visible,true);
assert.equal(avatar.headBone,gltf.head);assert.equal(avatar.diagnostics().assetState,'READY');
avatar.root.position.set(5,0,8);
gltf.head.position.set(.05,.95,.18);
avatar.pose(.016,2,{state:'WORKING',sit:1});
assert.ok(Math.abs(avatar.headAnchor.position.y-1.16)<1e-8);
assert.ok(Math.abs(avatar.headAnchor.position.x-.05)<1e-8);
gltf.head.position.y=1.55;
avatar.pose(.016,3,{state:'WALKING',sit:0,walkSpeed:.9});
assert.ok(Math.abs(avatar.headAnchor.position.y-1.76)<1e-8);
assert.equal(avatar.diagnostics().state,'WALKING');
avatar.dispose();avatar.dispose();
assert.equal(avatar.available,false);assert.equal(avatar.headBone,null);
assert.equal(cleaned.geometry,1);assert.equal(cleaned.material,1);assert.equal(cleaned.texture,1);
assert.equal(cleaned.skeleton,1);assert.equal(cleaned.stopped,1);assert.equal(cleaned.uncached,1);
""")

    def test_unlicensed_and_external_assets_are_rejected_without_requests(self):
        self.run_node(r"""
for(const [overrides,code] of [[{license:null},'LICENSE_REQUIRED'],[{url:'https://example.invalid/person.glb'},'LOCAL_ASSET_REQUIRED']]) {
  const avatar=createAvatar({role:'market',model:descriptor(overrides)});await avatar.ready;
  assert.equal(avatar.diagnostics().failureCode,code);assert.equal(avatar.available,false);avatar.dispose();
}
assert.equal(loadCalls,0);
""")

    def test_desk_rest_holds_keyboard_pose_without_claiming_work_or_resetting_hands(self):
        self.run_node(r"""
loadModel=async()=>fixture();
const avatar=createAvatar({role:'market',model:descriptor()});await avatar.ready;
const actions=globalThis.__lastOfficeMixer.actions;
avatar.pose(.016,1,{state:'WORKING',sit:1});
const working=actions.get('WORKING');working.time=.72;
avatar.pose(.016,2,{state:'IDLE',sit:1,deskRest:true});
assert.equal(working.time,.72);assert.equal(working.paused,true);
assert.equal(avatar.diagnostics().state,'IDLE');
assert.equal(avatar.diagnostics().clipState,'WORKING');
avatar.pose(.016,3,{state:'WORKING',sit:1});
assert.equal(working.paused,false);assert.equal(working.time,.72);
avatar.pose(.016,4,{state:'IDLE',sit:1});
assert.equal(avatar.diagnostics().clipState,'SEATED_IDLE');
avatar.pose(.016,5,{state:'IDLE',sit:1,deskHands:true});
assert.equal(working.time,.4);assert.equal(working.paused,true);
assert.equal(avatar.diagnostics().state,'IDLE');
avatar.pose(.016,6,{state:'STAND_UP',sit:.5,deskRest:true,transitionProgress:.3});
assert.equal(avatar.diagnostics().clipState,'STAND_UP');
avatar.dispose();
""")

    def test_disposal_during_load_never_restores_a_character(self):
        self.run_node(r"""
let resolveLoad;loadModel=()=>new Promise(resolve=>{resolveLoad=resolve;});
const avatar=createAvatar({role:'market',model:descriptor()});
avatar.dispose();const gltf=fixture();resolveLoad(gltf);await avatar.ready;
assert.equal(avatar.diagnostics().assetState,'DISPOSED');assert.equal(avatar.available,false);
assert.equal(avatar.root.visible,false);assert.equal(avatar.root.children.length,0);
assert.equal(cleaned.geometry,1);assert.equal(cleaned.material,1);assert.equal(cleaned.texture,1);
""")


if __name__ == '__main__':
    unittest.main()
