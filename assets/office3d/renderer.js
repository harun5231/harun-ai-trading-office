import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { createOfficeScene } from './scene-builder.js';
import { createMonitorTextures } from './monitor-textures.js';
import { createAvatar } from './avatars.js';
import { NavigationMesh } from './navigation.js';
import { createStatusController } from './status-controller.js';
import { OfficeAnimationController } from './animation-controller.js';

// Everything below belongs to the visual canvas. The dashboard and transport own their state.
const host = document.getElementById('scene');
const mobile = matchMedia('(max-width:700px)').matches;
const reducedMotion = matchMedia('(prefers-reduced-motion:reduce)').matches;
const scene = new THREE.Scene();
scene.background = new THREE.Color('#e8dfd2');
const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: mobile ? 'low-power' : 'high-performance' });
renderer.setPixelRatio(Math.min(devicePixelRatio || 1, mobile ? 1.4 : 1.8));
renderer.setSize(innerWidth, innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.12;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.domElement.className = 'office-webgl';
renderer.domElement.setAttribute('aria-label', 'Trading Office 3D: Kantor A dengan lima workstation, area kopi, sofa, dan kolam');
host.append(renderer.domElement);

const camera = new THREE.PerspectiveCamera(39, innerWidth / innerHeight, .1, 220);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.enablePan = false;
controls.dampingFactor = .08;
controls.minDistance = 3;
controls.maxDistance = 130;
controls.minPolarAngle = .34;
controls.maxPolarAngle = Math.PI * .47;
controls.target.set(0, 1.65, -.2);
scene.add(new THREE.HemisphereLight('#fff7e8', '#88715b', 1.8));
const key = new THREE.DirectionalLight('#fff1d9', 3.0);
key.position.set(-12, 20, 12);
key.castShadow = true;
key.shadow.mapSize.set(mobile ? 1024 : 2048, mobile ? 1024 : 2048);
Object.assign(key.shadow.camera, { left: -18, right: 18, top: 19, bottom: -19, near: 1, far: 65 });
key.shadow.camera.updateProjectionMatrix();
key.shadow.bias = -.00025;
key.shadow.normalBias = .025;
scene.add(key);
const fill = new THREE.DirectionalLight('#d9e8ef', .85);
fill.position.set(13, 11, -8);
scene.add(fill);

const monitors = createMonitorTextures(THREE, { mobile, reducedMotion });
const office = createOfficeScene(THREE, { scene, monitors, mobile });
const status = createStatusController();
const navigation = new NavigationMesh({ bounds: office.bounds, colliders: office.colliders, cellSize: .32, radius: .46 });
const stations = Array.isArray(office.stations) ? office.stations : Object.values(office.stations);
// Five visible colleagues aggregate all eight existing backend roles. This
// mapping reads the bridge only; backend employees and their contracts are intact.
const visualRoleAliases = Object.freeze({
  market: ['market', 'reviewer'], neuro: ['neuro'], risk: ['risk'],
  trading: ['trading', 'position'], boss: ['boss', 'report'],
});
const displayStatus = {
  getRole(role) {
    const candidates = (visualRoleAliases[role] || [role]).map(id => ({ ...status.getRole(id), displayRole: id }));
    return candidates.find(candidate => candidate.active) ||
      candidates.find(candidate => !candidate.known) || candidates[0];
  },
};
const actors = stations.map((station, index) => {
  const avatar = createAvatar({ id: station.role, role: station.role, name: station.name, accent: station.accent, index });
  scene.add(avatar.root);
  return { id: station.role, role: station.role, station, avatar };
});
const animation = new OfficeAnimationController({ actors, navigation,
  loungeSlots: office.loungeSlots, socialSlots: office.socialSlots, idleSlots: office.idleSlots,
  status: displayStatus, reducedMotion });

// A missing licensed human model stays absent, with an honest visible notice.
// Never substitute the former procedural mascots or show invisible work/chat.
const assetNotice = document.createElement('div');
assetNotice.className = 'office-asset-notice';
assetNotice.setAttribute('role', 'status');
host.append(assetNotice);
function refreshAssetNotice() {
  const missing = actors.filter(actor => !actor.avatar.available);
  assetNotice.hidden = missing.length === 0;
  assetNotice.textContent = missing.length
    ? `Karakter belum tersedia · ${missing.length} model manusia berizin belum terpasang`
    : '';
}
refreshAssetNotice();
for (const actor of actors) actor.avatar.ready?.then(refreshAssetNotice, refreshAssetNotice);

const speechLayer = document.createElement('div');
speechLayer.className = 'office-speech-layer';
speechLayer.setAttribute('aria-hidden', 'true');
host.append(speechLayer);
for (const actor of actors) {
  const bubble = document.createElement('div');
  bubble.className = 'office-speech'; bubble.hidden = true;
  bubble.style.setProperty('--speech-accent', actor.station.accent);
  speechLayer.append(bubble);
  actor.bubble = bubble; actor.screen = null;
}

function frameOffice() {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  const { minX, maxX, minZ, maxZ } = office.bounds;
  const centerX = (minX + maxX) / 2, centerZ = (minZ + maxZ) / 2;
  controls.target.set(centerX, 1.65, centerZ);
  const top = document.querySelector('.top')?.getBoundingClientRect().bottom || 0;
  const maxTop = Math.max(.45, 1 - 2 * (top + 12) / innerHeight);
  const points = [];
  for (const x of [minX - .5, maxX + .5]) for (const z of [minZ - .5, maxZ + .5])
    points.push(new THREE.Vector3(x, 0, z));
  points.push(new THREE.Vector3(minX - .35, 5.35, minZ - .3),
    new THREE.Vector3(maxX + .35, 5.35, minZ - .3),
    new THREE.Vector3(minX - .35, 5.1, maxZ + .3));
  let distance = Math.max(maxX - minX, maxZ - minZ);
  for (let attempt = 0; attempt < 22; attempt++) {
    camera.position.set(centerX + distance * (mobile ? .12 : .32), distance * .62, centerZ + distance * 1.02);
    camera.lookAt(controls.target); camera.updateMatrixWorld(true);
    if (points.every(point => {
      const projected = point.clone().project(camera);
      return Math.abs(projected.x) < .94 && projected.y < maxTop && projected.y > -.87;
    })) break;
    distance *= 1.075;
  }
  controls.update();
  camera.updateMatrixWorld(true);
}
frameOffice();
// Preserve manual orbit and zoom when browser chrome or orientation resizes.
let cameraMoved = false;
function manualCameraStart() { cameraMoved = true; }
controls.addEventListener('start', manualCameraStart);

const projectedHead = new THREE.Vector3();
let labelDiagnostics = { visible: 0, hiddenForSpace: 0, overlaps: 0 };
function activityLabel(actor, role) {
  if (role.active) return actor.mode === 'AT_DESK' ? role.speech : 'Kembali ke workstation';
  if (!role.known) return 'Status tugas belum terkonfirmasi';
  const activity = ({ COFFEE: 'Kopi', SOCIAL: 'Diskusi', LOUNGE: 'Sofa', POOL: 'Tepi kolam', VISIT: 'Kunjungi Harun' })[actor.destination?.kind];
  if (activity) return `Rehat visual · ${activity}`;
  return role.speech;
}
function projectSpeech(dt) {
  const viewport = renderer.domElement.getBoundingClientRect();
  const candidates = [];
  for (const actor of actors) {
    if (!actor.avatar.available) { actor.bubble.hidden = true; actor.screen = null; continue; }
    if (mobile) {
      actor.bubble.hidden = true; actor.screen = null; continue;
    }
    const role = displayStatus.getRole(actor.role);
    const text = activityLabel(actor, role);
    if (actor.bubble.textContent !== text) {
      actor.bubble.textContent = text;
      actor.labelWidth = null;
    }
    actor.bubble.dataset.active = String(role.active && actor.mode === 'AT_DESK');
    actor.avatar.headAnchor.getWorldPosition(projectedHead);
    projectedHead.y += .09;
    projectedHead.project(camera);
    const visible = projectedHead.z >= -1 && projectedHead.z <= 1 &&
      Math.abs(projectedHead.x) < 1.08 && Math.abs(projectedHead.y) < 1.08;
    actor.bubble.hidden = !visible;
    if (!visible) { actor.screen = null; continue; }
    const next = { x: (projectedHead.x * .5 + .5) * viewport.width,
      y: (-projectedHead.y * .5 + .5) * viewport.height };
    if (!actor.screen) actor.screen = next;
    else {
      const blend = reducedMotion ? 1 : 1 - Math.exp(-dt * 28);
      actor.screen.x += (next.x - actor.screen.x) * blend;
      actor.screen.y += (next.y - actor.screen.y) * blend;
    }
    actor.labelWidth ??= actor.bubble.offsetWidth;
    actor.labelHeight ??= actor.bubble.offsetHeight;
    candidates.push({ actor, x: actor.screen.x, y: actor.screen.y,
      width: actor.labelWidth, height: actor.labelHeight });
  }
  // Greedy rows near each projected head avoid the old pile of large bubbles.
  // Stable back-to-front ordering keeps labels from swapping while a rig types.
  candidates.sort((a, b) => a.y - b.y || a.actor.id.localeCompare(b.actor.id));
  const occupied = [];
  let hiddenForSpace = 0;
  for (const item of candidates) {
    const { actor, width, height } = item;
    let placed = null;
    for (let row = 0; row < 4 && !placed; row++) {
      for (const shift of [0, -width * .55, width * .55]) {
        const x = Math.min(viewport.width - width / 2 - 5, Math.max(width / 2 + 5, item.x + shift));
        const bottom = item.y - row * (height + 4);
        const box = { left: x - width / 2, right: x + width / 2, top: bottom - height, bottom };
        if (box.top < 8 || occupied.some(other => box.left < other.right + 4 &&
          box.right + 4 > other.left && box.top < other.bottom + 3 && box.bottom + 3 > other.top)) continue;
        placed = { x, bottom, box }; break;
      }
    }
    actor.bubble.hidden = !placed;
    if (!placed) { hiddenForSpace++; continue; }
    occupied.push(placed.box);
    actor.bubble.style.transform = `translate(${placed.x.toFixed(2)}px,${placed.bottom.toFixed(2)}px) translate(-50%,-100%)`;
  }
  const overlaps = occupied.reduce((count, box, index) => count + occupied.slice(index + 1).filter(other =>
    box.left < other.right && box.right > other.left && box.top < other.bottom && box.bottom > other.top).length, 0);
  labelDiagnostics = { visible: occupied.length, hiddenForSpace, overlaps };
}

let disposed = false, raf = 0, previous = performance.now(), lastPaint = previous, elapsed = 0;
function render(now) {
  if (disposed || document.hidden) return;
  raf = requestAnimationFrame(render);
  if (mobile && now - lastPaint < 1000 / 30) return;
  const dt = Math.min(Math.max((now - previous) / 1000, 0), .06);
  previous = lastPaint = now; elapsed += dt;
  animation.update(dt, elapsed);
  monitors.update(elapsed, status.getTelemetry());
  controls.update();
  camera.updateMatrixWorld(true);
  scene.updateMatrixWorld(true);
  projectSpeech(dt);
  office.update?.(reducedMotion ? 0 : elapsed);
  renderer.render(scene, camera);
}
function pause() { cancelAnimationFrame(raf); raf = 0; }
function resume() {
  if (disposed || document.hidden || raf) return;
  previous = lastPaint = performance.now(); raf = requestAnimationFrame(render);
}
function resize() {
  camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
  for (const actor of actors) { actor.labelWidth = null; actor.labelHeight = null; }
  if (!cameraMoved) frameOffice();
}
function visibility() { if (document.hidden) pause(); else resume(); }
function contextLost(event) {
  event.preventDefault(); pause();
  const loading = document.getElementById('loading');
  loading.style.display = 'grid'; loading.textContent = 'Tampilan 3D terhenti. Muat ulang untuk melanjutkan.';
}
function dispose() {
  if (disposed) return; disposed = true; pause();
  removeEventListener('resize', resize);
  document.removeEventListener('visibilitychange', visibility);
  controls.removeEventListener('start', manualCameraStart);
  renderer.domElement.removeEventListener('webglcontextlost', contextLost);
  controls.dispose(); status.dispose();
  for (const actor of actors) actor.avatar.dispose();
  office.dispose(); monitors.dispose(); renderer.dispose();
  speechLayer.remove(); assetNotice.remove(); renderer.domElement.remove();
}
addEventListener('resize', resize);
document.addEventListener('visibilitychange', visibility);
renderer.domElement.addEventListener('webglcontextlost', contextLost);
addEventListener('pagehide', event => { if (event.persisted) pause(); else dispose(); });
addEventListener('pageshow', event => { if (event.persisted) resume(); });

// Read-only diagnostics; no trading state or mutating controller is published.
window.officeDiagnostics = () => {
  const motion = animation.diagnostics();
  const violations = [];
  const heads = actors.map(actor => actor.avatar.headAnchor.getWorldPosition(new THREE.Vector3()));
  for (let index = 0; index < actors.length; index++) {
    const actor = actors[index];
    if (!actor.avatar.available) continue;
    const ignoreIds = actor.currentSeat && (actor.sit > 0 || actor.exiting ||
      ['STANDING_UP', 'SITTING_DOWN', 'DOCKING', 'ALIGNING'].includes(actor.mode))
      ? (actor.currentSeat.ignoreIds || [actor.currentSeat.colliderId]) : [];
    if (!navigation.isWalkable(actor.avatar.root.position, { ignoreIds })) violations.push(`obstacle:${actor.id}`);
    for (let next = index + 1; next < actors.length; next++) {
      const other = actors[next];
      if (!other.avatar.available) continue;
      if (actor.avatar.root.position.distanceTo(other.avatar.root.position) < navigation.radius * 2 - .001)
        violations.push(`avatar:${actor.id}:${other.id}`);
      if (heads[index].distanceTo(heads[next]) < .75)
        violations.push(`avatar-head:${actor.id}:${other.id}`);
    }
  }
  return { actors: actors.length, meshes: renderer.info.render.calls, triangles: renderer.info.render.triangles,
    states: motion.states, finite: actors.every(actor => {
      const position = actor.avatar.root.position;
      return [position.x, position.y, position.z, actor.sit].every(Number.isFinite);
    }), collisionViolations: violations, navigation: navigation.getDiagnostics(),
    monitorTextures: monitors.diagnostics(), office: { ...office.diagnostics }, status: status.diagnostics(),
    avatars: actors.map(actor => ({ id: actor.id, ...actor.avatar.diagnostics() })),
    modelsReady: actors.every(actor => actor.avatar.available), labels: { ...labelDiagnostics },
    visualRoleAliases,
    pathsRejected: motion.pathsRejected, blockedFrames: motion.blockedFrames,
    renderer: { mobile, reducedMotion, pixelRatio: renderer.getPixelRatio() } };
};
animation.update(0, 0);
monitors.update(0, status.getTelemetry());
scene.updateMatrixWorld(true); projectSpeech(0); renderer.render(scene, camera);
document.getElementById('loading').style.display = 'none';
resume();
