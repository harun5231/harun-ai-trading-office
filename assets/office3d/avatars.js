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
  const skin = material(index % 4 === 3 ? '#b98162' : index % 4 === 2 ? '#d5a07e' : '#f0c6a5');
  const suit = material(index % 3 === 1 ? '#1b2844' : '#142238');
  const trouser = material('#111d30');
  const white = material('#f6f8ff');
  const black = material('#10131e', 0.45);
  const hair = material(index % 4 === 2 ? '#292122' : '#141923', 0.48);
  const colored = material(accent, 0.45);
  const lapel = material('#2a3b57', 0.58);
  const iris = material(index % 2 ? '#559db3' : '#716ac4', 0.35);
  const cheek = material('#d89c97');
  const hips = group(root, 'hips', [0, 1.02, 0]);
  const spine = group(hips, 'spine', [0, 0.015, 0]);
  mesh(spine, 'box', suit, [0, 0.285, 0], [0.43, 0.51, 0.29], 'tailored-jacket');
  mesh(spine, 'sphere', suit, [0, 0.53, 0], [0.228, 0.09, 0.142]);
  // The trouser seat stays on the cushion while the jacket leans forward.
  // Its underside is hipY-.09, exactly matching the supplied seat height.
  mesh(hips, 'sphere', trouser, [0, -0.036, -0.08], [0.18, 0.054, 0.14], 'tailored-seat');
  triangle(spine, white, [-0.095, 0.565, 0.155, 0, 0.255, 0.16, 0.095, 0.565, 0.155], owned, 'white-shirt');
  triangle(spine, lapel, [-0.205, 0.54, 0.157, -0.065, 0.34, 0.168, -0.018, 0.54, 0.17], owned, 'left-lapel');
  triangle(spine, lapel, [0.205, 0.54, 0.157, 0.018, 0.54, 0.17, 0.065, 0.34, 0.168], owned, 'right-lapel');
  const tie = mesh(spine, 'box', colored, [0, 0.437, 0.177], [0.042, 0.175, 0.014], 'tie');
  tie.rotation.z = 0.035;
  triangle(spine, colored, [-0.021, 0.35, 0.187, 0, 0.319, 0.187, 0.021, 0.35, 0.187], owned, 'tie-tip');
  mesh(spine, 'box', badge(role, accent, owned), [-0.128, 0.37, 0.177], [0.093, 0.052, 0.011], 'office-badge');
  mesh(spine, 'cylinder', skin, [0, 0.602, 0], [0.065, 0.105, 0.065], 'neck');
  const head = group(spine, 'head', [0, 0.812, 0.005]);
  mesh(head, 'sphere', skin, [0, 0, 0], [0.333, 0.337, 0.284], 'anime-head');
  mesh(head, 'sphere', skin, [0, -0.154, 0.023], [0.251, 0.18, 0.243], 'soft-jaw');
  for (const side of [-1, 1]) {
    mesh(head, 'sphere', skin, [side * 0.319, -0.023, 0], [0.049, 0.075, 0.035], 'ear');
    mesh(head, 'sphere', cheek, [side * 0.202, -0.076, 0.239], [0.054, 0.015, 0.007]);
  }
  const cap = mesh(head, 'hair', hair, [0, 0.027, -0.012], [0.344, 0.348, 0.298], 'slick-hair-cap');
  cap.rotation.z = -0.065;
  for (let i = 0; i < 5; i += 1) {
    const lock = mesh(head, 'cone', hair, [-0.233 + i * 0.099, 0.178 + i * 0.009, 0.205],
      [0.094, 0.23 - i * 0.012, 0.078], `swept-hair-${i}`);
    lock.rotation.z = 2.6;
    lock.rotation.x = -0.42;
  }
  const eyes = [];
  for (const side of [-1, 1]) {
    const eye = group(head, side < 0 ? 'left-eye' : 'right-eye', [side * 0.113, 0.022, 0.265]);
    mesh(eye, 'sphere', white, [0, 0, 0], [0.071, 0.085, 0.015], 'eye-white');
    mesh(eye, 'sphere', iris, [0, -0.004, 0.015], [0.038, 0.061, 0.012], 'anime-iris');
    mesh(eye, 'sphere', black, [0, -0.003, 0.026], [0.019, 0.044, 0.007], 'pupil');
    mesh(eye, 'sphere', white, [-0.014, 0.023, 0.033], [0.011, 0.016, 0.005], 'eye-glint');
    mesh(eye, 'sphere', white, [0.016, -0.028, 0.032], [0.006, 0.008, 0.004], 'eye-glint-small');
    curve(head, black, [[side * 0.047, 0.127, 0.23], [side * 0.107, 0.155, 0.227], [side * 0.171, 0.135, 0.208]], 0.009, owned, 'expressive-brow');
    const lash = mesh(eye, 'box', black, [side * 0.063, 0.048, 0.016], [0.027, 0.013, 0.009], 'upper-lash');
    lash.rotation.z = side * 0.6;
    eyes.push(eye);
  }
  mesh(head, 'sphere', skin, [0, -0.087, 0.29], [0.023, 0.03, 0.025], 'nose');
  curve(head, material('#9a565c'), [[-0.046, -0.139, 0.266], [0, -0.157, 0.284], [0.046, -0.139, 0.266]], 0.007, owned, 'smile');
  const headAnchor = group(head, 'label-anchor', [0, 0.43, 0]);

  const arms = [], legs = [];
  for (const side of [-1, 1]) {
    const shoulder = group(spine, side < 0 ? 'left-shoulder' : 'right-shoulder', [side * 0.255, 0.514, 0]);
    mesh(shoulder, 'sphere', suit, [0, 0, 0], [0.087, 0.09, 0.09]);
    mesh(shoulder, 'cylinder', suit, [0, -0.17, 0], [0.069, 0.34, 0.071], 'upper-arm');
    const elbow = group(shoulder, 'elbow', [0, -0.34, 0]);
    mesh(elbow, 'sphere', suit, [0, 0, 0], [0.067, 0.065, 0.067]);
    mesh(elbow, 'cylinder', suit, [0, -0.155, 0], [0.061, 0.31, 0.064], 'forearm');
    mesh(elbow, 'cylinder', white, [0, -0.29, 0], [0.061, 0.045, 0.064], 'shirt-cuff');
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
    const ankle = group(knee, 'ankle', [0, -0.47, 0]);
    mesh(ankle, 'sphere', black, [0, -0.015, 0.084], [0.088, 0.05, 0.145], 'polished-shoe');
    mesh(ankle, 'sphere', material('#263344', 0.22), [0, 0.018, 0.163], [0.066, 0.012, 0.035], 'shoe-highlight');
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
      const workX = side * (0.175 + (reduced ? 0 : Math.sin(clock * 2.1 + side) * 0.01));
      const workY = 1.13 + (reduced ? 0 : Math.sin(clock * Math.PI * 24 + side) * 0.008);
      // Dock z is deskZ+1.45 and the keyboard is deskZ+.38. Wrists at .99,
      // with forward-pointing fingertips, contact the actual keys near 1.07.
      const workZ = 0.99 + (reduced ? 0 : Math.sin(clock * 1.6 + side) * 0.017);
      const socialX = side * (0.34 + wave * 0.05);
      const socialY = hips.position.y + (side > 0 ? 0.42 + wave * 0.065 : 0.08);
      const socialZ = 0.24 + (side > 0 ? wave * 0.05 : 0);
      _target.set(idleX + (workX - idleX) * working + (socialX - idleX) * social,
        idleY + (workY - idleY) * working + (socialY - idleY) * social,
        idleZ + (workZ - idleZ) * working + (socialZ - idleZ) * social);
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
      footHeights: legs.map((leg) => leg.ankle.getWorldPosition(new THREE.Vector3()).y - 0.065),
      animation: 'ARTICULATED_CLIP_BLEND', forward: '+Z' };
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
