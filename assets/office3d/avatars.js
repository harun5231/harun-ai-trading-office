import * as THREE from 'three';

// Local +Z is forward. Navigation owns root.position/root.rotation; the clips
// below only move articulated joints. Every rig shares its inexpensive meshes.
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

const DOWN = new THREE.Vector3(0, -1, 0);
const _axis = new THREE.Vector3(), _pole = new THREE.Vector3();
const _upper = new THREE.Vector3(), _lower = new THREE.Vector3();
const _inverse = new THREE.Quaternion();
const _target = new THREE.Vector3();
const _handWorld = new THREE.Quaternion().setFromEuler(new THREE.Euler(-Math.PI / 2, 0, 0));
const _handChain = new THREE.Quaternion();
let users = 0;
let geometries = new Map();
let materials = new Map();

function geometry(kind) {
  if (!geometries.has(kind)) {
    const value = kind === 'sphere' ? new THREE.SphereGeometry(1, 16, 12)
      : kind === 'cylinder' ? new THREE.CylinderGeometry(1, 1, 1, 10)
      : kind === 'cone' ? new THREE.ConeGeometry(1, 1, 5)
      : kind === 'hair' ? new THREE.SphereGeometry(1, 18, 12, 0, Math.PI * 2, 0, 1.57)
      : new THREE.BoxGeometry(1, 1, 1);
    geometries.set(kind, value);
  }
  return geometries.get(kind);
}
function material(color, roughness = 0.7, metalness = 0) {
  const key = `${color}|${roughness}|${metalness}`;
  if (!materials.has(key)) materials.set(key, new THREE.MeshStandardMaterial({ color, roughness, metalness }));
  return materials.get(key);
}
function mesh(parent, kind, mat, position, scale, name) {
  const result = new THREE.Mesh(geometry(kind), mat);
  result.position.set(...position);
  result.scale.set(...scale);
  result.castShadow = true;
  result.receiveShadow = true;
  if (name) result.name = name;
  parent.add(result);
  return result;
}
function group(parent, name, position = [0, 0, 0]) {
  const result = new THREE.Group();
  result.name = name;
  result.position.set(...position);
  parent.add(result);
  return result;
}
function triangle(parent, mat, vertices, owned, name) {
  const shape = new THREE.BufferGeometry();
  shape.setAttribute('position', new THREE.Float32BufferAttribute(vertices, 3));
  shape.computeVertexNormals();
  owned.push(shape);
  const result = new THREE.Mesh(shape, mat);
  result.name = name;
  result.castShadow = true;
  parent.add(result);
  return result;
}
function curve(parent, mat, points, radius, owned, name) {
  const geo = new THREE.TubeGeometry(new THREE.CatmullRomCurve3(points.map((v) => new THREE.Vector3(...v))), 10, radius, 5, false);
  owned.push(geo);
  const result = new THREE.Mesh(geo, mat);
  result.name = name;
  parent.add(result);
  return result;
}

function badge(role, accent, owned) {
  if (typeof document === 'undefined') return material(accent);
  const canvas = document.createElement('canvas');
  canvas.width = 128;
  canvas.height = 72;
  const ctx = canvas.getContext('2d');
  if (!ctx) return material(accent);
  ctx.fillStyle = '#edf5ff';
  ctx.fillRect(0, 0, 128, 72);
  ctx.fillStyle = new THREE.Color(accent).getStyle();
  ctx.fillRect(0, 0, 13, 72);
  ctx.fillStyle = '#20324b';
  ctx.font = 'bold 27px sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(String(role || 'TEAM').replace(/[^a-z0-9]/gi, '').slice(0, 4).toUpperCase(), 72, 38);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const mat = new THREE.MeshStandardMaterial({ map: texture, roughness: 0.85 });
  owned.push(texture, mat);
  return mat;
}

function solveLimb(joint, target, pole, upperLength, lowerLength) {
  _axis.copy(target);
  const distance = clamp(_axis.length(), 0.025, upperLength + lowerLength - 0.0005);
  _axis.normalize();
  _pole.copy(pole).addScaledVector(_axis, -pole.dot(_axis));
  if (_pole.lengthSq() < 0.00001) _pole.set(0, 0, 1).addScaledVector(_axis, -_axis.z);
  _pole.normalize();
  const along = (upperLength ** 2 + distance ** 2 - lowerLength ** 2) / (2 * distance);
  const bend = Math.sqrt(Math.max(0, upperLength ** 2 - along ** 2));
  _upper.copy(_axis).multiplyScalar(along).addScaledVector(_pole, bend);
  _lower.copy(_axis).multiplyScalar(distance).sub(_upper);
  joint.pivot.quaternion.setFromUnitVectors(DOWN, _upper.normalize());
  _inverse.copy(joint.pivot.quaternion).invert();
  _lower.normalize().applyQuaternion(_inverse);
  joint.middle.quaternion.setFromUnitVectors(DOWN, _lower);
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

export function createAvatar({ id, role = 'TEAM', name = '', accent = '#5eafff', index = 0 } = {}) {
  users += 1;
  const owned = [];
  const root = new THREE.Group();
  root.name = `avatar-${id || index}`;
  root.userData = { id: id || String(index), role, name, forward: '+Z' };
  // Smooth, full-body mascot rigs keep the proven limb lengths and chair/desk
  // contacts. Colour variation identifies colleagues without changing role IDs.
  const skin = material(['#9451dc', '#9b58df', '#8750cf', '#a064df'][index % 4], 0.48);
  const skinShade = material('#7135aa', 0.62);
  const suit = material(['#e47c32', '#d77538', '#de9144', '#d46d39'][index % 4], 0.78);
  const trouser = material(role === 'boss' ? '#735077' : '#bf6137', 0.83);
  const white = material('#f6eee0', 0.78);
  const black = material('#211c32', 0.45);
  const colored = material(accent, 0.55);
  const iris = material('#f1ac42', 0.37);
  const hips = group(root, 'hips', [0, 1.02, 0]);
  const spine = group(hips, 'spine', [0, 0.015, 0]);
  mesh(spine, 'sphere', suit, [0, 0.285, 0], [0.235, 0.302, 0.165], 'mascot-office-jacket');
  mesh(spine, 'sphere', suit, [0, 0.50, 0], [0.247, 0.095, 0.147], 'jacket-shoulders');
  // The underside of the seat is hipY-.09, preserving the cushion contact.
  mesh(hips, 'sphere', trouser, [0, -0.036, -0.08], [0.18, 0.054, 0.14], 'tailored-seat');
  mesh(spine, 'cylinder', white, [0, 0.56, 0], [0.105, 0.07, 0.095], 'soft-office-collar');
  mesh(spine, 'box', white, [0, 0.304, 0.163], [0.018, 0.405, 0.016], 'jacket-zip');
  mesh(spine, 'box', black, [0.015, 0.405, 0.179], [0.018, 0.038, 0.012], 'zip-pull');
  mesh(spine, 'sphere', suit, [0, 0.037, 0], [0.217, 0.046, 0.151], 'jacket-waistband');
  for (const side of [-1, 1]) {
    const pocket = mesh(spine, 'box', white, [side * 0.136, 0.19, 0.143], [0.087, 0.014, 0.012], 'jacket-pocket-trim');
    pocket.rotation.z = side * 0.25;
  }
  mesh(spine, 'box', badge(role, accent, owned), [-0.123, 0.386, 0.156], [0.083, 0.045, 0.011], 'office-badge');
  mesh(spine, 'cylinder', skin, [0, 0.602, 0], [0.065, 0.105, 0.065], 'neck');
  const head = group(spine, 'head', [0, 0.812, 0.005]);
  mesh(head, 'sphere', skin, [0, 0.012, 0], [0.329, 0.344, 0.283], 'purple-mascot-head');
  mesh(head, 'sphere', skin, [0, -0.151, 0.027], [0.247, 0.178, 0.241], 'soft-jaw');
  for (const side of [-1, 1]) {
    mesh(head, 'sphere', skin, [side * 0.319, -0.015, 0], [0.047, 0.074, 0.036], 'ear');
    mesh(head, 'sphere', skinShade, [side * 0.327, -0.014, 0.022], [0.023, 0.041, 0.014], 'ear-inner');
  }
  const eyes = [];
  for (const side of [-1, 1]) {
    const eye = group(head, side < 0 ? 'left-eye' : 'right-eye', [side * 0.119, 0.019, 0.261]);
    mesh(eye, 'sphere', black, [0, 0, 0], [0.085, 0.069, 0.025], 'soft-eye-outline');
    mesh(eye, 'sphere', white, [0, -0.002, 0.018], [0.067, 0.052, 0.014], 'eye-white');
    mesh(eye, 'sphere', iris, [-side * 0.008, -0.003, 0.031], [0.037, 0.047, 0.012], 'amber-iris');
    mesh(eye, 'sphere', black, [-side * 0.008, -0.002, 0.041], [0.019, 0.034, 0.008], 'pupil');
    mesh(eye, 'sphere', white, [-side * 0.008 - 0.01, 0.016, 0.049], [0.009, 0.012, 0.004], 'eye-glint');
    curve(head, black, [[side * 0.048, 0.119, 0.25], [side * 0.111, 0.143, 0.238], [side * 0.182, 0.116, 0.213]], 0.013, owned, 'expressive-brow');
    eyes.push(eye);
  }
  mesh(head, 'sphere', skin, [0, -0.077, 0.292], [0.037, 0.038, 0.035], 'nose');
  curve(head, skinShade, [[-0.067, -0.139, 0.271], [0, -0.158, 0.291], [0.07, -0.126, 0.266]], 0.009, owned, 'warm-smile');
  mesh(head, 'sphere', skinShade, [0.078, -0.124, 0.264], [0.012, 0.014, 0.006], 'smile-dimple');
  const headAnchor = group(head, 'label-anchor', [0, 0.43, 0]);

  const arms = [], legs = [];
  for (const side of [-1, 1]) {
    const shoulder = group(spine, side < 0 ? 'left-shoulder' : 'right-shoulder', [side * 0.255, 0.514, 0]);
    mesh(shoulder, 'sphere', white, [0, 0, 0], [0.087, 0.09, 0.09]);
    mesh(shoulder, 'cylinder', white, [0, -0.17, 0], [0.069, 0.34, 0.071], 'upper-arm');
    const elbow = group(shoulder, 'elbow', [0, -0.34, 0]);
    mesh(elbow, 'sphere', white, [0, 0, 0], [0.067, 0.065, 0.067]);
    mesh(elbow, 'cylinder', white, [0, -0.155, 0], [0.061, 0.31, 0.064], 'forearm');
    mesh(elbow, 'cylinder', suit, [0, -0.29, 0], [0.061, 0.045, 0.064], 'shirt-cuff');
    const hand = group(elbow, 'wrist', [0, -0.31, 0]);
    mesh(hand, 'sphere', skin, [0, -0.014, 0], [0.057, 0.058, 0.034], 'hand');
    const fingers = [];
    for (let f = 0; f < 4; f += 1) {
      const digit = group(hand, 'finger', [(f - 1.5) * 0.022, -0.043, 0]);
      mesh(digit, 'cylinder', skin, [0, -0.023, 0], [0.01, 0.045 - Math.abs(f - 1.5) * 0.005, 0.01]);
      fingers.push(digit);
    }
    mesh(hand, 'sphere', skin, [side * 0.05, -0.014, 0], [0.018, 0.035, 0.018], 'thumb');
    arms.push({ side, pivot: shoulder, middle: elbow, hand, fingers,
      pole: new THREE.Vector3(side * 0.45, -0.7, 0.18) });
    const thigh = group(hips, side < 0 ? 'left-hip' : 'right-hip', [side * 0.113, -0.009, 0]);
    mesh(thigh, 'cylinder', trouser, [0, -0.235, 0], [0.078, 0.47, 0.08], 'thigh');
    const knee = group(thigh, 'knee', [0, -0.47, 0]);
    mesh(knee, 'sphere', trouser, [0, 0, 0], [0.076, 0.071, 0.076]);
    mesh(knee, 'cylinder', trouser, [0, -0.235, 0], [0.067, 0.47, 0.069], 'shin');
    mesh(thigh, 'box', white, [side * 0.075, -0.235, 0.012], [0.012, 0.38, 0.027], 'trouser-side-stripe');
    mesh(knee, 'box', white, [side * 0.065, -0.235, 0.012], [0.012, 0.38, 0.025], 'trouser-shin-stripe');
    const ankle = group(knee, 'ankle', [0, -0.47, 0]);
    mesh(ankle, 'sphere', white, [0, -0.024, 0.075], [0.087, 0.066, 0.145], 'office-sneaker');
    // Sole underside is ankleY-.105: planted feet actually meet y=0.
    mesh(ankle, 'sphere', black, [0, -0.083, 0.077], [0.089, 0.022, 0.147], 'sneaker-sole');
    mesh(ankle, 'sphere', colored, [0, -0.019, -0.032], [0.085, 0.048, 0.041], 'sneaker-heel');
    for (let lace = 0; lace < 3; lace += 1)
      mesh(ankle, 'box', suit, [0, 0.033 - lace * 0.004, 0.068 + lace * 0.024], [0.081, 0.009, 0.01], 'sneaker-lace');
    legs.push({ side, pivot: thigh, middle: knee, ankle, pole: new THREE.Vector3(0, 0, 1) });
  }
  const blender = new AnimationClipBlender();
  let current = 'IDLE', stateTime = 0, gaitPhase = 0, disposed = false;
  let lastPose = { state: 'IDLE', sit: 1, weights: blender.weights };

  function pose(dt, time, input = {}) {
    if (disposed) return;
    const delta = clamp(Number.isFinite(dt) ? dt : 0, 0, 0.1);
    const clock = Number.isFinite(time) ? time : 0;
    const reduced = input.reducedMotion === true;
    const state = stateName(input.state);
    if (state !== current) { current = state; stateTime = 0; }
    stateTime += delta;
    const weights = blender.update(delta, input.weights || state, reduced);
    const progress = ease(Number.isFinite(input.transitionProgress) ? input.transitionProgress : reduced ? 1 : stateTime / 0.7);
    let sitting = weights.WORKING + weights.IDLE + weights.SIT_DOWN * progress + weights.STAND_UP * (1 - progress);
    if (Number.isFinite(input.sit)) sitting = clamp(input.sit);
    else if (input.seated === false && state === 'IDLE') sitting = 0;
    else if (input.seated === true && state === 'SOCIAL') sitting = 1;
    sitting = clamp(sitting);
    const speed = Math.max(0, Number.isFinite(input.walkSpeed) ? input.walkSpeed : 0);
    const working = weights.WORKING, social = weights.SOCIAL, walking = weights.WALKING * (1 - sitting);
    const breath = reduced ? 0 : Math.sin(clock * 1.9 + index * 0.7);
    const seatHeight = Number.isFinite(input.seatHeight) ? clamp(input.seatHeight, 0.35, 0.95) : 0.78;
    const transitionContact = state === 'SIT_DOWN' || state === 'STAND_UP';
    const seatedForward = THREE.MathUtils.lerp(0.32, 0.46, clamp((0.78 - seatHeight) / 0.13));
    const seat = input.seatPosition;
    const seatDistance = Number.isFinite(seat?.x) && Number.isFinite(seat?.z)
      ? Math.hypot(root.position.x - seat.x, root.position.z - seat.z) : 0;
    const contact = input.seatContact === true || transitionContact;
    // Hold the pelvis in front of the fixed seat while rising or sidestepping;
    // retreating into its centre would put standing thighs through the cushion.
    const contactAmount = contact ? 1 - ease((seatDistance - 0.65) / 0.30) : 0;
    const strideScale = 1 - .9 * contactAmount;
    // Match planted-foot velocity to root travel, including the short seat shuffle.
    // Essential locomotion remains enabled with reduced decorative motion.
    gaitPhase += delta * speed * .6 / (.5 * strideScale);
    const relativeSeatYaw = Number.isFinite(input.seatYaw) ? input.seatYaw - root.rotation.y : 0;
    const contactX = Math.sin(relativeSeatYaw), contactZ = Math.cos(relativeSeatYaw);
    // Short chibi shins require the pelvis near the cushion front: knees can
    // bend outside its edge rather than passing through the seat surface.
    const pelvisForward = seatedForward * Math.max(sitting, contactAmount)
      + 0.045 * contactAmount * (1 - sitting);
    hips.position.x = contactX * pelvisForward;
    hips.position.z = contactZ * pelvisForward;
    hips.position.y = THREE.MathUtils.lerp(1.02, seatHeight + 0.09, sitting)
      + breath * 0.004 * (1 - sitting);
    // A forward planted foot needs a slightly bent standing leg during egress.
    // Capping the hip by the actual limb reach avoids lifting a stationary shoe.
    if (transitionContact) hips.position.y = Math.min(hips.position.y,
      0.105 + 0.009 + Math.sqrt(0.9395 ** 2 - 0.35 ** 2));
    spine.rotation.set(0.30 * sitting + breath * 0.003, reduced ? 0 : Math.sin(clock * 0.9) * social * 0.04, 0);
    head.rotation.set(working * 0.055 + (reduced ? 0 : Math.sin(clock * 2.4) * social * 0.08),
      reduced ? 0 : Math.sin(clock * 0.65 + index) * working * 0.065 + Math.sin(clock * 1.1) * social * 0.13, 0);
    const blinkClock = ((clock + index * 0.68) % 5.3 + 5.3) % 5.3;
    const blink = !reduced && blinkClock > 5.15 ? Math.max(0.09, Math.abs(blinkClock - 5.225) / 0.075) : 1;
    for (const eye of eyes) eye.scale.y = blink;
    spine.updateMatrix();
    for (const arm of arms) {
      const side = arm.side;
      const wave = reduced ? 0 : Math.sin(clock * 2.2 + side);
      const idleX = side * THREE.MathUtils.lerp(0.29, 0.18, sitting);
      const idleY = hips.position.y + THREE.MathUtils.lerp(-0.13, 0.13, sitting);
      const idleZ = THREE.MathUtils.lerp(0.03, 0.36, sitting);
      // Each hand counter-swings its own leg, using the same planted-foot cycle.
      const stride = reduced ? 0 : -footCycle(gaitPhase + (side < 0 ? 0 : 0.5)).z / 0.25;
      const walkZ = 0.035 + stride * 0.23;
      const walkY = hips.position.y - 0.12 + Math.abs(stride) * 0.035;
      const workX = side * (0.175 + (reduced ? 0 : Math.sin(clock * 2.1 + side) * 0.01));
      const workY = 1.13 + (reduced ? 0 : Math.sin(clock * Math.PI * 24 + side) * 0.008);
      // Dock z is deskZ+1.45 and the keyboard is deskZ+.38. Wrists at .99,
      // with forward-pointing fingertips, contact the actual keys near 1.07.
      const workZ = 0.99 + (reduced ? 0 : Math.sin(clock * 1.6 + side) * 0.017);
      const socialX = side * (0.34 + wave * 0.05);
      const socialY = hips.position.y + (side > 0 ? 0.42 + wave * 0.065 : 0.08);
      const socialZ = 0.24 + (side > 0 ? wave * 0.05 : 0);
      _target.set(idleX + (workX - idleX) * working + (socialX - idleX) * social,
        idleY + (workY - idleY) * working + (socialY - idleY) * social + (walkY - idleY) * walking,
        idleZ + (workZ - idleZ) * working + (socialZ - idleZ) * social + (walkZ - idleZ) * walking);
      _target.sub(hips.position).sub(spine.position);
      _inverse.copy(spine.quaternion).invert();
      _target.applyQuaternion(_inverse).sub(arm.pivot.position);
      solveLimb(arm, _target, arm.pole, 0.34, 0.31);
      _handChain.copy(spine.quaternion).multiply(arm.pivot.quaternion).multiply(arm.middle.quaternion).invert();
      arm.hand.quaternion.copy(_handChain).multiply(_handWorld);
      for (let f = 0; f < arm.fingers.length; f += 1) {
        arm.fingers[f].rotation.x = reduced ? 0 : working * Math.sin(clock * Math.PI * 24 + f * 1.7 + side) * 0.20;
      }
    }
    for (const leg of legs) {
      const cycle = footCycle(gaitPhase + (leg.side < 0 ? 0 : 0.5));
      const plantedForward = Math.max((seatedForward + 0.03) * contactAmount, 0.40 * sitting);
      const z = contactZ * plantedForward + cycle.z * walking * strideScale;
      _target.set(leg.side * 0.119 + contactX * plantedForward,
        0.105 + cycle.lift * walking * strideScale * (reduced ? .45 : 1), z);
      _target.sub(hips.position);
      _target.sub(leg.pivot.position);
      // Knees keep facing out of the fixed cushion while the actor turns to
      // leave it; a short contact shuffle prevents a full backward stride there.
      leg.pole.set(contactX * contactAmount, 0, 1 - contactAmount + contactZ * contactAmount);
      solveLimb(leg, _target, leg.pole, 0.47, 0.47);
      // Keep the shoe flat in root space even while knee/hip bones rotate.
      leg.ankle.quaternion.copy(leg.pivot.quaternion).multiply(leg.middle.quaternion).invert();
    }
    lastPose = { state, sit: sitting, seatHeight, weights: { ...weights }, walkSpeed: speed, reducedMotion: reduced };
    root.updateMatrixWorld(true);
  }

  function diagnostics() {
    let meshes = 0, joints = 0;
    root.traverse((node) => { if (node.isMesh) meshes += 1; else if (node.isGroup) joints += 1; });
    const world = new THREE.Vector3();
    return { id: root.userData.id, role, disposed, meshes, joints,
      sharedGeometries: geometries.size, sharedMaterials: materials.size,
      ...lastPose, headHeight: head.getWorldPosition(world).y,
      footHeights: legs.map((leg) => leg.ankle.getWorldPosition(new THREE.Vector3()).y - 0.105),
      animation: 'ARTICULATED_CLIP_BLEND', style: 'PURPLE_OFFICE_MASCOT', forward: '+Z' };
  }
  function dispose() {
    if (disposed) return;
    disposed = true;
    root.removeFromParent();
    for (const item of owned) item.dispose();
    users -= 1;
    if (!users) {
      for (const item of geometries.values()) item.dispose();
      for (const item of materials.values()) item.dispose();
      geometries = new Map();
      materials = new Map();
    }
  }
  pose(0, 0, { state: 'IDLE', sit: 1 });
  return { root, headAnchor, pose, dispose, diagnostics, blender };
}
