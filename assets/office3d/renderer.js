import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { createOfficeScene } from './scene-builder.js';
import { createMonitorTextures } from './monitor-textures.js';
import { createAvatar } from './avatars.js';
import { NavigationMesh } from './navigation.js';
import { createStatusController } from './status-controller.js';
import { OfficeAnimationController } from './animation-controller.js';
import { createAvatarBatch } from './avatar-batch.js';

// Everything below belongs to the visual canvas. The dashboard and transport own their state.
const host = document.getElementById('scene');
const mobile = matchMedia('(max-width:700px)').matches;
const reducedMotion = matchMedia('(prefers-reduced-motion:reduce)').matches;
const scene = new THREE.Scene();
scene.background = new THREE.Color('#dfe8e9');
const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: mobile ? 'low-power' : 'high-performance' });
renderer.setPixelRatio(Math.min(devicePixelRatio || 1, mobile ? 1.4 : 1.8));
renderer.setSize(innerWidth, innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.03;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.domElement.className = 'office-webgl';
renderer.domElement.setAttribute('aria-label', 'Kantor trading 3D: delapan avatar chibi dan workstation divisi');
host.append(renderer.domElement);

const camera = new THREE.PerspectiveCamera(39, innerWidth / innerHeight, .1, 220);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = .08;
controls.minDistance = 12;
controls.maxDistance = 130;
controls.minPolarAngle = .34;
controls.maxPolarAngle = Math.PI * .47;
controls.target.set(0, 1.25, 1.25);
scene.add(new THREE.HemisphereLight('#e9f7ff', '#a7937e', 2.0));
const key = new THREE.DirectionalLight('#fff2de', 2.4);
key.position.set(-12, 20, 12);
key.castShadow = true;
key.shadow.mapSize.set(mobile ? 1024 : 2048, mobile ? 1024 : 2048);
Object.assign(key.shadow.camera, { left: -18, right: 18, top: 19, bottom: -19, near: 1, far: 65 });
key.shadow.camera.updateProjectionMatrix();
key.shadow.bias = -.00025;
key.shadow.normalBias = .025;
scene.add(key);
const fill = new THREE.DirectionalLight('#afdcf2', 1.0);
fill.position.set(13, 11, -8);
scene.add(fill);

const monitors = createMonitorTextures(THREE, { mobile, reducedMotion });
const office = createOfficeScene(THREE, { scene, monitors, mobile });
const status = createStatusController();
const navigation = new NavigationMesh({ bounds: office.bounds, colliders: office.colliders, cellSize: .32, radius: .46 });
const stations = Array.isArray(office.stations) ? office.stations : Object.values(office.stations);
const actors = stations.map((station, index) => {
  const avatar = createAvatar({ id: station.role, role: station.role, name: station.name, accent: station.accent, index });
  scene.add(avatar.root);
  return { id: station.role, role: station.role, station, avatar };
});
const animation = new OfficeAnimationController({ actors, navigation,
  loungeSlots: office.loungeSlots, socialSlots: office.socialSlots, status, reducedMotion });
const avatarBatch = createAvatarBatch(THREE, { scene, actors });

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
  controls.target.set(0, 1.25, 1.25);
  const top = document.querySelector('.top')?.getBoundingClientRect().bottom || 0;
  const maxTop = Math.max(.45, 1 - 2 * (top + 12) / innerHeight);
  const points = [];
  for (const x of [-11.2, 11.2]) for (const z of [-9.8, 11.7]) points.push(new THREE.Vector3(x, 0, z));
  points.push(new THREE.Vector3(-10, 5.15, -9.4), new THREE.Vector3(10, 5.15, -9.4));
  let distance = 27;
  for (let attempt = 0; attempt < 22; attempt++) {
    camera.position.set(distance * .66, distance * .81, distance * .86 + 1.25);
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

const projectedHead = new THREE.Vector3();
function projectSpeech(dt) {
  const viewport = renderer.domElement.getBoundingClientRect();
  for (const actor of actors) {
    const role = status.getRole(actor.role);
    const text = actor.mode === 'SOCIAL' ? 'Diskusi tim…'
      : actor.mode === 'RESTING' ? 'Istirahat sebentar…' : role.speech;
    if (actor.bubble.textContent !== text) actor.bubble.textContent = text;
    actor.bubble.dataset.active = String(role.active && actor.mode === 'AT_DESK');
    actor.avatar.headAnchor.getWorldPosition(projectedHead);
    projectedHead.y += .13;
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
    actor.bubble.style.transform = `translate(${actor.screen.x.toFixed(2)}px,${actor.screen.y.toFixed(2)}px) translate(-50%,-100%)`;
  }
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
  avatarBatch.update();
  projectSpeech(dt);
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
  // Framing affects the canvas camera only, never the dashboard layout.
  frameOffice();
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
  document.getElementById('resetCamera')?.removeEventListener('click', frameOffice);
  renderer.domElement.removeEventListener('webglcontextlost', contextLost);
  controls.dispose(); status.dispose();
  avatarBatch.dispose();
  for (const actor of actors) actor.avatar.dispose();
  office.dispose(); monitors.dispose(); renderer.dispose();
  speechLayer.remove(); renderer.domElement.remove();
}
addEventListener('resize', resize);
document.addEventListener('visibilitychange', visibility);
document.getElementById('resetCamera')?.addEventListener('click', frameOffice);
renderer.domElement.addEventListener('webglcontextlost', contextLost);
addEventListener('pagehide', event => { if (event.persisted) pause(); else dispose(); });
addEventListener('pageshow', event => { if (event.persisted) resume(); });

// Read-only diagnostics; no trading state or mutating controller is published.
window.officeDiagnostics = () => {
  const motion = animation.diagnostics();
  const violations = [];
  const heads = actors.map(actor => actor.avatar.headAnchor.parent.getWorldPosition(new THREE.Vector3()));
  for (let index = 0; index < actors.length; index++) {
    const actor = actors[index];
    const ignoreIds = actor.currentSeat && (actor.sit > 0 || actor.exiting ||
      ['STANDING_UP', 'SITTING_DOWN', 'DOCKING'].includes(actor.mode))
      ? [actor.currentSeat.colliderId] : [];
    if (!navigation.isWalkable(actor.avatar.root.position, { ignoreIds })) violations.push(`obstacle:${actor.id}`);
    for (let next = index + 1; next < actors.length; next++) {
      const other = actors[next];
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
    avatarBatch: avatarBatch.diagnostics(),
    pathsRejected: motion.pathsRejected, blockedFrames: motion.blockedFrames,
    renderer: { mobile, reducedMotion, pixelRatio: renderer.getPixelRatio() } };
};
animation.update(0, 0);
monitors.update(0, status.getTelemetry());
scene.updateMatrixWorld(true); avatarBatch.update(); projectSpeech(0); renderer.render(scene, camera);
document.getElementById('loading').style.display = 'none';
resume();
