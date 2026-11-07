import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { AVATAR_MODELS, REQUIRED_MODEL_CLIPS } from './models/manifest.js';

// Local +Z is forward. Navigation owns root.position/root.rotation. Only actual
// licensed, textured, skinned assets may supply a visible human in this office.
export const CLIP_STATES = Object.freeze([
  'WORKING', 'IDLE', 'WALKING', 'SOCIAL', 'SIT_DOWN', 'STAND_UP',
]);
export const clamp = (v, lo = 0, hi = 1) => Math.min(hi, Math.max(lo, v));
export const ease = (v) => { const t = clamp(v); return t * t * (3 - 2 * t); };

const ALIASES = { WORK: 'WORKING', TYPING: 'WORKING', AT_DESK: 'WORKING',
  RESTING: 'IDLE', SEATED_IDLE: 'IDLE', WALK: 'WALKING', TALKING: 'SOCIAL' };
const stateName = (state) => {
  const value = typeof state === 'string' ? state.toUpperCase() : 'IDLE';
  return ALIASES[value] || (CLIP_STATES.includes(value) ? value : 'IDLE');
};

export class AnimationClipBlender {
  constructor({ response = 9, initial = 'IDLE' } = {}) {
    this.response = response;
    this.weights = Object.fromEntries(CLIP_STATES.map((clip) => [clip, clip === stateName(initial) ? 1 : 0]));
  }
  update(dt, target = 'IDLE', immediate = false) {
    const desired = Object.fromEntries(CLIP_STATES.map((clip) => [clip, 0]));
    if (typeof target === 'string') desired[stateName(target)] = 1;
    else if (target && typeof target === 'object') {
      for (const [clip, weight] of Object.entries(target)) {
        if (Number.isFinite(weight) && weight > 0) desired[stateName(clip)] += weight;
      }
      const sum = Object.values(desired).reduce((a, b) => a + b, 0);
      if (!sum) desired.IDLE = 1;
      else for (const clip of CLIP_STATES) desired[clip] /= sum;
    } else desired.IDLE = 1;
    const alpha = immediate ? 1 : 1 - Math.exp(-Math.max(0, this.response) * clamp(dt || 0, 0, 0.1));
    for (const clip of CLIP_STATES) this.weights[clip] += (desired[clip] - this.weights[clip]) * alpha;
    const total = Object.values(this.weights).reduce((a, b) => a + b, 0) || 1;
    for (const clip of CLIP_STATES) this.weights[clip] /= total;
    return { ...this.weights };
  }
  diagnostics() { return { weights: { ...this.weights }, response: this.response }; }
}

export function footCycle(phase) {
  // The planted section moves backward relative to the travelling root.
  // speed * stance duration == stride distance, so contact does not slide.
  const stride = 0.25, stance = 0.6;
  const cycle = ((phase % 1) + 1) % 1;
  if (cycle < stance) return { z: stride - cycle / stance * 2 * stride, lift: 0 };
  const swing = (cycle - stance) / (1 - stance);
  return { z: -stride + ease(swing) * 2 * stride, lift: Math.sin(swing * Math.PI) * 0.13 };
}

function assetError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function disposeModel(model) {
  const geometries = new Set(), materials = new Set(), textures = new Set();
  model?.traverse(node => {
    if (node.geometry) geometries.add(node.geometry);
    for (const mat of Array.isArray(node.material) ? node.material : [node.material]) {
      if (!mat) continue;
      materials.add(mat);
      for (const value of Object.values(mat)) if (value?.isTexture) textures.add(value);
    }
    if (node.isSkinnedMesh) node.skeleton?.dispose();
  });
  for (const texture of textures) texture.dispose();
  for (const mat of materials) mat.dispose();
  for (const geo of geometries) geo.dispose();
}

function validateDescriptor(spec) {
  if (!spec?.url) throw assetError('MISSING_ASSET', 'Model manusia 3D bertekstur dan berkerangka belum tersedia.');
  if (spec.referenceReviewed !== true) throw assetError('REFERENCE_REVIEW_REQUIRED', 'Wajah, proporsi, dan pakaian model belum diperiksa terhadap referensi.');
  const license = spec.license;
  if (!license?.name || !license.sourceUrl || !license.file ||
      license.allowsWebUse !== true || license.allowsPublicRedistribution !== true) {
    throw assetError('LICENSE_REQUIRED', 'Bukti izin penggunaan web dan distribusi aset dalam repository publik belum lengkap.');
  }
  for (const state of REQUIRED_MODEL_CLIPS) {
    if (!spec.clips?.[state]) throw assetError('CLIP_MAPPING_REQUIRED', `Klip animasi ${state} belum ditentukan.`);
  }
  if (typeof spec.headBone !== 'string' || !spec.headBone.trim()) {
    throw assetError('HEAD_BONE_REQUIRED', 'Nama bone kepala wajib ditentukan agar label mengikuti pose karakter.');
  }
  const url = new URL(spec.url, import.meta.url);
  const directory = new URL('./models/', import.meta.url);
  if (url.origin !== directory.origin || !url.pathname.startsWith(directory.pathname) ||
      !/\.(glb|gltf)$/i.test(url.pathname)) {
    throw assetError('LOCAL_ASSET_REQUIRED', 'Model harus berupa GLB/glTF lokal dalam assets/office3d/models/.');
  }
  return url.href;
}

function validateModel(gltf, spec) {
  const model = gltf.scene;
  if (!model) throw assetError('INVALID_MODEL', 'Berkas tidak memiliki scene glTF.');
  let skinnedMeshes = 0, texturedMeshes = 0;
  const skeletonBones = new Set();
  model.traverse(node => {
    if (!node.isMesh) return;
    if (node.isSkinnedMesh && node.skeleton?.bones.length &&
        node.geometry?.attributes.skinIndex && node.geometry?.attributes.skinWeight) {
      skinnedMeshes++;
      for (const bone of node.skeleton.bones) skeletonBones.add(bone);
    }
    const mats = Array.isArray(node.material) ? node.material : [node.material];
    if (mats.some(mat => mat?.map?.isTexture)) texturedMeshes++;
  });
  if (!skinnedMeshes) throw assetError('RIG_REQUIRED', 'Model tidak memiliki mesh berkerangka dengan skin weights.');
  if (!texturedMeshes) throw assetError('TEXTURES_REQUIRED', 'Model tidak memiliki tekstur warna yang sudah dimuat.');
  const clips = new Map((gltf.animations || []).map(clip => [clip.name, clip]));
  for (const state of REQUIRED_MODEL_CLIPS) {
    const clip = clips.get(spec.clips[state]);
    if (!clip?.tracks.length || !(clip.duration > 0)) {
      throw assetError('ANIMATION_REQUIRED', `Model tidak memiliki klip ${state}: ${spec.clips[state]}.`);
    }
  }
  const headBone = model.getObjectByName(spec.headBone);
  if (!headBone) {
    throw assetError('HEAD_BONE_MISSING', `Bone kepala ${spec.headBone} tidak ditemukan.`);
  }
  if (!headBone.isBone || !skeletonBones.has(headBone)) {
    throw assetError('HEAD_BONE_INVALID', `Node ${spec.headBone} bukan bone dalam skeleton mesh karakter.`);
  }
  return { skinnedMeshes, texturedMeshes, clips, headBone };
}

export function createAvatar({ id, role = 'TEAM', name = '', index = 0, model: descriptor } = {}) {
  const root = new THREE.Group();
  root.name = `avatar-${id || index}`;
  root.userData = { id: id || String(index), role, name, forward: '+Z' };
  root.visible = false;
  const headAnchor = new THREE.Group();
  headAnchor.name = 'label-anchor';
  root.add(headAnchor);
  const spec = descriptor || AVATAR_MODELS[role];
  const blender = new AnimationClipBlender();
  let disposed = false, available = false, model = null, mixer = null, current = null;
  let assetState = 'LOADING', failure = null, validation = null, headBone = null;
  let lastPose = { state: 'IDLE', sit: 1, walkSpeed: 0, reducedMotion: false };
  const actions = new Map();
  const headPosition = new THREE.Vector3();

  function pose(dt, time, input = {}) {
    if (disposed || !available) return;
    const delta = clamp(Number.isFinite(dt) ? dt : 0, 0, .1);
    const reduced = input.reducedMotion === true;
    let target = stateName(input.state);
    const activity = String(input.activity || '').toUpperCase();
    if ((target === 'IDLE' || target === 'SOCIAL') && ['COFFEE', 'POOL', 'VISITING'].includes(activity)) target = activity;
    else if (target === 'IDLE' && input.sit > .5) target = 'SEATED_IDLE';
    const action = actions.get(target);
    if (!action) return; // Every supported action was checked at load time.
    if (target !== current) {
      const previous = actions.get(current);
      action.reset();
      const transition = target === 'SIT_DOWN' || target === 'STAND_UP';
      action.setLoop(transition ? THREE.LoopOnce : THREE.LoopRepeat, transition ? 1 : Infinity);
      action.clampWhenFinished = transition;
      action.enabled = true;
      action.setEffectiveWeight(1);
      action.play();
      if (previous && previous !== action) action.crossFadeFrom(previous, reduced ? 0 : .28, false);
      current = target;
    }
    action.paused = reduced && !['WALKING', 'SIT_DOWN', 'STAND_UP'].includes(target);
    if (target === 'WALKING') {
      action.timeScale = clamp((input.walkSpeed || .9) / (spec.walkSpeed || .9), .15, 2);
    } else action.timeScale = 1;
    if ((target === 'SIT_DOWN' || target === 'STAND_UP') && Number.isFinite(input.transitionProgress)) {
      action.paused = true;
      action.time = clamp(input.transitionProgress) * action.getClip().duration;
    }
    mixer.update(delta);
    if (headBone) {
      root.updateMatrixWorld(true);
      headBone.getWorldPosition(headPosition);
      root.worldToLocal(headPosition);
      headAnchor.position.copy(headPosition);
      headAnchor.position.y += .21;
    }
    blender.update(delta, stateName(input.state), reduced);
    lastPose = { state: target, sit: clamp(input.sit ?? 0),
      seatHeight: input.seatHeight ?? .78, walkSpeed: input.walkSpeed ?? 0, reducedMotion: reduced };
  }

  function diagnostics() {
    return { id: root.userData.id, role, disposed, available, assetState,
      failureCode: failure?.code || null, failureMessage: failure?.message || null,
      modelName: spec?.name || name, modelUrl: spec?.url || null,
      license: spec?.license?.name || null,
      meshes: validation?.skinnedMeshes ?? 0, texturedMeshes: validation?.texturedMeshes ?? 0,
      animation: available ? 'GLTF_ANIMATION_MIXER' : 'UNAVAILABLE',
      style: 'REALISTIC_LICENSED_HUMAN', forward: '+Z', clips: [...actions.keys()], ...lastPose };
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    available = false;
    mixer?.stopAllAction();
    if (model) mixer?.uncacheRoot(model);
    actions.clear();
    disposeModel(model);
    model = null; mixer = null; headBone = null; validation = null;
    root.removeFromParent();
    root.clear();
    assetState = 'DISPOSED';
  }

  const avatar = { root, headAnchor, pose, update: pose, dispose, diagnostics, blender,
    get available() { return available; },
    get headBone() { return headBone; } };
  avatar.ready = (async () => {
    let loaded = null;
    try {
      const url = validateDescriptor(spec);
      const loader = new GLTFLoader(new THREE.LoadingManager());
      // Embedded GLB textures are preferred. Reject external model resources
      // outside the local model directory; no provider or backend calls.
      const directory = new URL('./models/', import.meta.url);
      loader.manager.setURLModifier(resource => {
        if (/^(data:|blob:)/.test(resource)) return resource;
        const assetUrl = new URL(resource, url);
        if (assetUrl.origin !== directory.origin || !assetUrl.pathname.startsWith(directory.pathname)) {
          throw assetError('LOCAL_RESOURCE_REQUIRED', 'Tekstur dan buffer model harus disimpan bersama aset lokal.');
        }
        return assetUrl.href;
      });
      loaded = await loader.loadAsync(url);
      if (disposed) { disposeModel(loaded.scene); return avatar; }
      validation = validateModel(loaded, spec);
      model = loaded.scene;
      // Assets use metres, feet at zero, +Z forward. A reviewed orientation
      // adjustment is allowed without touching navigation or trading state.
      model.rotation.y = Number.isFinite(spec.forwardRotation) ? spec.forwardRotation : 0;
      const bounds = new THREE.Box3().setFromObject(model);
      const height = bounds.max.y - bounds.min.y;
      if (!(height > .1) || !Number.isFinite(height)) throw assetError('INVALID_SCALE', 'Ukuran model tidak valid.');
      const scale = (spec.height || 1.75) / height;
      model.scale.multiplyScalar(scale);
      model.position.y -= bounds.min.y * scale;
      model.traverse(node => {
        if (node.isMesh) { node.castShadow = true; node.receiveShadow = true; }
      });
      root.add(model);
      headBone = validation.headBone;
      mixer = new THREE.AnimationMixer(model);
      for (const state of REQUIRED_MODEL_CLIPS) actions.set(state, mixer.clipAction(validation.clips.get(spec.clips[state])));
      available = true;
      root.visible = true;
      assetState = 'READY';
      pose(0, 0, { state: 'IDLE', sit: 1 });
    } catch (error) {
      if (loaded?.scene && loaded.scene !== model) disposeModel(loaded.scene);
      else if (model) { model.removeFromParent(); disposeModel(model); model = null; }
      if (!disposed) {
        failure = { code: error.code || 'MODEL_LOAD_FAILED', message: String(error.message || error) };
        assetState = failure.code;
        available = false;
        root.visible = false;
      }
    }
    return avatar;
  })();
  return avatar;
}
