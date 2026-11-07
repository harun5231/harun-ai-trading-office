import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import {
  BOUNDS, STATION_CONFIG, COLLIDERS, SOFA_CONFIG, PLANT_CONFIG,
  LOUNGE_SLOTS, SOCIAL_SLOTS, IDLE_SLOTS, POOL_CONFIG, COFFEE_CONFIG,
} from './layout.js';

// Architecture, furniture and read-only displays. No trading state is owned here.
export function createOfficeScene(THREE, { scene, monitors, mobile = false }) {
  const root = new THREE.Group(); root.name = 'harun-office-architecture'; scene.add(root);
  const staticRoot = new THREE.Group(); root.add(staticRoot);
  const displays = new THREE.Group(); displays.name = 'office-displays'; root.add(displays);
  const materials = new Map(), geometries = new Map(), ownTextures = new Set();
  const stationary = {}, displayMeshes = [];
  const geo = (key, factory) => { if (!geometries.has(key)) geometries.set(key, factory()); return geometries.get(key); };
  const mat = (color, roughness = .65, metalness = 0) => {
    const key = `${color}:${roughness}:${metalness}`;
    if (!materials.has(key)) materials.set(key, new THREE.MeshStandardMaterial({ color, roughness, metalness }));
    return materials.get(key);
  };
  function texture(width, height, paint) {
    const canvas = document.createElement('canvas'); canvas.width = width; canvas.height = height;
    paint(canvas.getContext('2d'), width, height);
    const value = new THREE.CanvasTexture(canvas); value.colorSpace = THREE.SRGBColorSpace;
    value.anisotropy = mobile ? 2 : 4; ownTextures.add(value); return value;
  }
  const random = (() => { let value = 7831; return () => ((value = value * 16807 % 2147483647) - 1) / 2147483646; })();
  const wood = texture(512, 512, (ctx, w, h) => {
    ctx.fillStyle = '#8e603d'; ctx.fillRect(0, 0, w, h);
    for (let line = 0; line < 210; line++) {
      const y = random() * h, strength = random();
      ctx.strokeStyle = strength > .4 ? `rgba(49,27,15,${.025 + random() * .1})` : `rgba(242,197,137,${random() * .11})`;
      ctx.lineWidth = .3 + random() * 1.5; ctx.beginPath(); ctx.moveTo(0, y);
      ctx.bezierCurveTo(150, y + Math.sin(y) * 11, 340, y - Math.sin(y) * 7, w, y + 3); ctx.stroke();
    }
  });
  const stone = texture(512, 512, (ctx, w, h) => {
    ctx.fillStyle = '#d1bfaa'; ctx.fillRect(0, 0, w, h);
    for (let i = 0; i < 2300; i++) {
      ctx.fillStyle = `rgba(${i % 2 ? '83,66,48' : '255,248,226'},${.025 + random() * .08})`;
      ctx.fillRect(random() * w, random() * h, random() * 4 + .4, random() * 2 + .4);
    }
    for (let i = 0; i < 28; i++) {
      const y = random() * h;
      ctx.strokeStyle = `rgba(114,87,61,${.025 + random() * .06})`; ctx.lineWidth = 1 + random() * 2;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.bezierCurveTo(170, y + 12, 300, y - 14, w, y + 1); ctx.stroke();
    }
    ctx.strokeStyle = '#bca58b'; ctx.lineWidth = 1;
    for (const y of [128, 256, 384]) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
    for (let row = 0; row < 4; row++) { const x = row % 2 ? 90 : 325; ctx.beginPath(); ctx.moveTo(x, row * 128); ctx.lineTo(x, (row + 1) * 128); ctx.stroke(); }
  });
  stone.wrapS = stone.wrapT = THREE.RepeatWrapping; stone.repeat.set(2, 1);
  const floorMap = texture(1024, 1024, (ctx, w, h) => {
    ctx.fillStyle = '#c8a47a'; ctx.fillRect(0, 0, w, h);
    for (let row = 0; row < 16; row++) for (let column = -1; column < 4; column++) {
      const x = column * 320 + row % 2 * 160, y = row * 64;
      const v = Math.floor(random() * 16);
      ctx.fillStyle = `rgb(${204 + v},${169 + v},${127 + v})`; ctx.fillRect(x + 1, y + 1, 318, 62);
      for (let line = 0; line < 13; line++) {
        const yy = y + 3 + line * 4.4;
        ctx.strokeStyle = `rgba(116,74,37,${.035 + random() * .055})`; ctx.lineWidth = .6;
        ctx.beginPath(); ctx.moveTo(x + 2, yy); ctx.bezierCurveTo(x + 120, yy + 4, x + 220, yy - 3, x + 317, yy); ctx.stroke();
      }
    }
  });
  floorMap.wrapS = floorMap.wrapT = THREE.RepeatWrapping; floorMap.repeat.set(2, 2);
  const M = {
    wood: new THREE.MeshStandardMaterial({ map: wood, roughness: .43, metalness: .035 }),
    stone: new THREE.MeshStandardMaterial({ map: stone, roughness: .72 }),
    floor: new THREE.MeshStandardMaterial({ map: floorMap, roughness: .52 }),
    paleStone: mat('#eee2ce', .66), edge: mat('#5e3f2b', .48),
    brass: mat('#c8a568', .28, .72), paleBrass: mat('#dbc296', .32, .52),
    dark: mat('#17262a', .42), metal: mat('#3e4b4d', .3, .7), chrome: mat('#b2b6b2', .23, .8),
    teal: mat('#244a49', .75), cushion: mat('#3c6461', .91), linen: mat('#eee8da', .98),
    rug: mat('#ccbd9f', 1), white: mat('#f2efdf', .35), soil: mat('#403227'),
    leaf: mat('#315534', .88), leafLight: mat('#567d42', .88), coffee: mat('#32251b'),
  };
  M.glow = new THREE.MeshBasicMaterial({ color: '#ffe2ac' });
  M.glass = new THREE.MeshStandardMaterial({ color: '#bdd9d8', roughness: .08, metalness: .1, transparent: true, opacity: .13, depthWrite: false });
  Object.entries(M).forEach(([key, material]) => materials.set(key, material));
  const add = (parent, geometry, material, position, scale) => {
    const mesh = new THREE.Mesh(geometry, material);
    if (position) mesh.position.set(...position); if (scale) mesh.scale.set(...scale);
    mesh.castShadow = material !== M.glow && material !== M.glass;
    mesh.receiveShadow = material !== M.glass; parent.add(mesh); return mesh;
  };
  const block = (parent, position, size, material, radius = .035) => add(parent,
    geo(`box:${size.join(',')}:${radius}`, () => radius ? new RoundedBoxGeometry(...size, mobile ? 1 : 2, Math.min(radius, ...size.map(n => n / 2))) : new THREE.BoxGeometry(...size)), material, position);
  const cylinder = geo('cylinder', () => new THREE.CylinderGeometry(1, 1, 1, mobile ? 12 : 18));
  const sphere = geo('sphere', () => new THREE.SphereGeometry(1, mobile ? 10 : 16, mobile ? 8 : 12));
  const ellipse = (parent, position, scale, material) => add(parent, sphere, material, position, scale);
  function rod(parent, from, to, radius, material) {
    const a = new THREE.Vector3(...from), b = new THREE.Vector3(...to), direction = b.clone().sub(a);
    const mesh = add(parent, cylinder, material, a.clone().add(b).multiplyScalar(.5).toArray(), [radius, direction.length(), radius]);
    mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.normalize()); return mesh;
  }
  function label(position, width, height, name, { size = 68, color = '#211d18', background = '#d0b17d', rotation = 0 } = {}) {
    const map = texture(1536, 256, (ctx, w, h) => {
      const g = ctx.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, '#f1dbad'); g.addColorStop(.18, background); g.addColorStop(.58, '#dac299'); g.addColorStop(1, '#b39561');
      ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
      for (let line = 0; line < h; line += 3) { ctx.fillStyle = 'rgba(255,255,255,.055)'; ctx.fillRect(0, line, w, 1); }
      ctx.strokeStyle = '#f6e2bb'; ctx.lineWidth = 5; ctx.strokeRect(10, 10, w - 20, h - 20);
      ctx.fillStyle = color; ctx.font = `700 ${size}px Arial`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(name, w / 2, h / 2 + 3, w - 110);
      for (const x of [31, w - 31]) { ctx.beginPath(); ctx.arc(x, h / 2, 5, 0, Math.PI * 2); ctx.fill(); }
    });
    const material = new THREE.MeshBasicMaterial({ map }); materials.set(`label:${materials.size}`, material);
    const mesh = add(displays, geo(`plane:${width}:${height}`, () => new THREE.PlaneGeometry(width, height)), material, position);
    mesh.rotation.y = rotation; mesh.castShadow = false; return mesh;
  }
  function display(role, variant, position, width, height, angle = 0) {
    const map = monitors?.get(role, variant); if (!map) return;
    const material = new THREE.MeshBasicMaterial({ map }); materials.set(`display:${materials.size}`, material);
    const mesh = add(displays, geo(`screen:${width}:${height}`, () => new THREE.PlaneGeometry(width, height)), material, position);
    mesh.rotation.y = angle; mesh.castShadow = false; displayMeshes.push(mesh);
  }

  // Open-front cutaway with real geometry and softly reflective oak flooring.
  block(staticRoot, [0, -.25, .2], [22.2, .5, 17.7], M.paleStone, .12);
  block(staticRoot, [0, .025, .2], [21.8, .05, 17.3], M.floor, 0);
  block(staticRoot, [0, 2.67, -8.43], [22, 5.35, .22], M.stone, .02);
  block(staticRoot, [10.87, .35, .2], [.2, .7, 17.3], M.paleStone, .02);
  block(staticRoot, [-10.87, .13, .2], [.2, .25, 17.3], M.paleStone, .02);
  block(staticRoot, [0, .09, 8.88], [22, .18, .16], M.paleStone, .02);
  for (const y of [.18, 5.12]) {
    block(staticRoot, [0, y, -8.26], [21.5, .08, .13], M.brass, .01);
    if (y > 1) block(staticRoot, [0, y - .08, -8.17], [21.3, .035, .06], M.glow, .006);
  }
  // Full-height left glazing with a generated skyline, independent of data feeds.
  const skyline = texture(1024, 512, (ctx, w, h) => {
    const g = ctx.createLinearGradient(0, 0, 0, h); g.addColorStop(0, '#b1cbd5'); g.addColorStop(.7, '#e6e9db'); g.addColorStop(1, '#93a8a1'); ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
    for (let layer = 0; layer < 3; layer++) for (let x = -10; x < w; x += 24 + random() * 38) {
      const height = 45 + random() * (170 + layer * 28), width = 20 + random() * 35;
      ctx.fillStyle = ['#c1cfd0', '#a5b9c0', '#829ba8'][layer]; ctx.fillRect(x, h - height, width, height);
      for (let yy = h - height + 7; yy < h - 3; yy += 10) for (let xx = x + 4; xx < x + width - 4; xx += 8) { ctx.fillStyle = '#d2dddd'; ctx.fillRect(xx, yy, 2, 4); }
    }
  });
  const skyMaterial = new THREE.MeshBasicMaterial({ map: skyline, side: THREE.DoubleSide }); materials.set('skyline', skyMaterial);
  const windowView = add(displays, geo('window-view', () => new THREE.PlaneGeometry(17.0, 5.1)), skyMaterial, [-10.91, 2.85, .15]); windowView.rotation.y = Math.PI / 2;
  for (let z = -8.1; z < 9; z += 2.45) block(staticRoot, [-10.7, 2.72, z], [.12, 5.35, .065], M.metal, .006);
  for (const y of [.25, 2.75, 5.28]) block(staticRoot, [-10.69, y, .2], [.12, .065, 17.3], M.metal, .006);
  block(staticRoot, [-10.72, 2.74, .15], [.035, 5.05, 17.0], M.glass, 0);

  // A panoramic wall display carries only textures supplied by the status bridge.
  block(staticRoot, [-1.25, 3.01, -8.10], [13.6, 3.72, .24], M.brass, .08);
  block(staticRoot, [-1.25, 3.01, -7.95], [13.39, 3.53, .10], M.dark, .06);
  for (const [index, role] of ['market', 'neuro', 'risk', 'trading'].entries()) {
    const x = -1.25 + (index % 2 ? 3.28 : -3.28), y = 3.01 + (index < 2 ? .86 : -.86);
    display(role, 0, [x, y, -7.887], 6.43, 1.61);
  }
  label([-1.25, .82, -8.02], 4.7, .31, 'HARUN  /  TRADING OFFICE', { size: 59 });
  // Warm architectural sconces and concealed perimeter light.
  for (const x of [-9, 6.2]) {
    block(staticRoot, [x, 2.85, -8.12], [.14, 1.25, .15], M.brass, .025);
    block(staticRoot, [x, 2.85, -8.015], [.055, 1.08, .055], M.glow, .015);
  }
  for (const z of [-4.1, 3.5]) {
    block(staticRoot, [-1.0, 5.25, z], [13, .08, .16], M.brass, .025);
    block(staticRoot, [-1.0, 5.19, z], [12.8, .028, .115], M.glow, .006);
  }

  function chair(x, z) {
    const group = new THREE.Group(); group.position.set(x, 0, z); group.rotation.y = Math.PI; staticRoot.add(group);
    rod(group, [0, .14, 0], [0, .69, 0], .055, M.chrome);
    for (let index = 0; index < 5; index++) {
      const angle = index * Math.PI * 2 / 5, px = Math.cos(angle) * .37, pz = Math.sin(angle) * .37;
      rod(group, [0, .17, 0], [px, .12, pz], .026, M.chrome);
      ellipse(group, [px, .08, pz], [.065, .045, .065], M.dark);
    }
    block(group, [0, .72, 0], [.70, .12, .65], M.teal, .075);
    block(group, [0, 1.09, .31], [.70, .69, .14], M.teal, .09).rotation.x = -.10;
    block(group, [0, 1.46, .33], [.52, .17, .14], M.cushion, .065);
    for (const x of [-.22, 0, .22]) block(group, [x, 1.10, .22], [.015, .54, .014], M.cushion, .004);
    for (const side of [-1, 1]) {
      rod(group, [side * .28, .64, .12], [side * .39, .98, -.02], .022, M.chrome);
      block(group, [side * .39, .99, -.02], [.075, .05, .35], M.teal, .027);
    }
  }
  function monitor(role, variant, center, angle = 0, width = 1.47) {
    const [x, y, z] = center, group = new THREE.Group(); group.position.set(x, y, z); group.rotation.y = angle; staticRoot.add(group);
    block(group, [0, -.49, -.02], [.42, .035, .29], M.metal, .018);
    rod(group, [0, -.48, -.06], [0, -.05, -.06], .029, M.metal);
    block(group, [0, 0, -.035], [width + .065, .84, .075], M.dark, .025);
    display(role, variant, [x + Math.sin(angle) * .012, y, z + Math.cos(angle) * .012], width, .755, angle);
    // Rear-facing screen lets the seated colleague read the same existing telemetry.
    display(role, variant, [x - Math.sin(angle) * .082, y, z - Math.cos(angle) * .082], width, .755, angle + Math.PI);
    ellipse(group, [width * .44, -.395, .011], [.008, .008, .008], M.glow);
  }
  function mug(parent, x, y, z) {
    add(parent, geo('mug', () => new THREE.CylinderGeometry(.075, .066, .17, 14)), M.white, [x, y + .085, z]);
    add(parent, geo('mug-coffee', () => new THREE.CylinderGeometry(.064, .064, .006, 14)), M.coffee, [x, y + .164, z]);
    add(parent, geo('mug-handle', () => new THREE.TorusGeometry(.049, .012, 6, 14)), M.white, [x + .083, y + .09, z]);
  }
  function workstation(station) {
    const { role, x, z } = station, boss = role === 'boss', width = boss ? 4.8 : 3.65, depth = boss ? 1.8 : 1.55;
    // A rounded walnut pedestal and full-width warm light line replace cubicles.
    block(staticRoot, [x, 1.035, z], [width, .14, depth], M.wood, .14);
    block(staticRoot, [x, .46, z - .08], [width - .18, .87, depth - .17], M.wood, .18);
    block(staticRoot, [x, .095, z - .02], [width - .26, .12, depth - .24], M.edge, .05);
    block(staticRoot, [x, .90, z + depth / 2 - .075], [width - .28, .036, .045], M.glow, .008);
    block(staticRoot, [x, .125, z + depth / 2 - .08], [width - .35, .022, .035], M.glow, .005);
    // Metal physical plate plus separate high-resolution lettering surface.
    const plaqueWidth = boss ? 3.15 : 2.72;
    block(staticRoot, [x, .65, z + depth / 2 - .012], [plaqueWidth + .10, .36, .065], M.paleBrass, .026);
    label([x, .65, z + depth / 2 + .026], plaqueWidth, .285, station.name, { size: boss ? 220 : 164 });
    if (boss) {
      monitor(role, 0, [x - 1.36, 1.66, z + .15], .16, 1.18);
      monitor(role, 1, [x, 1.66, z + .29], 0, 1.42);
      monitor(role, 0, [x + 1.36, 1.66, z + .15], -.16, 1.18);
    } else {
      monitor(role, 0, [x - .78, 1.64, z + .15], .07, 1.47);
      monitor(role, 1, [x + .78, 1.64, z + .15], -.07, 1.47);
    }
    block(staticRoot, [x - .07, 1.119, z - .46], [.79, .026, .26], M.dark, .022);
    if (!mobile) for (let row = 0; row < 4; row++) for (let col = 0; col < 11; col++) block(staticRoot, [x - .40 + col * .062, 1.138, z - .55 + row * .051], [.044, .005, .032], M.metal, .002);
    block(staticRoot, [x + .58, 1.119, z - .45], [.32, .01, .32], M.teal, .02);
    ellipse(staticRoot, [x + .58, 1.15, z - .45], [.069, .034, .10], M.dark);
    block(staticRoot, [x - 1.28, 1.125, z - .45], [.34, .035, .43], M.edge, .013);
    block(staticRoot, [x - 1.28, 1.146, z - .45], [.30, .006, .39], M.linen, .006);
    rod(staticRoot, [x - 1.38, 1.155, z - .56], [x - 1.25, 1.155, z - .33], .009, M.brass);
    mug(staticRoot, x + (boss ? 2.03 : 1.51), 1.11, z - .45);
    // Compact workstation tower remains on the desktop, not another station.
    block(staticRoot, [x + (boss ? 2.08 : 1.54), 1.34, z + .37], [.23, .47, .40], M.dark, .03);
    block(staticRoot, [x + (boss ? 2.20 : 1.66), 1.34, z + .37], [.009, .34, .025], M.brass, .003);
    chair(x, station.dock.z);
    stationary[role] = { ...station, seat: { ...station.seat }, dock: { ...station.dock }, entry: { ...station.entry }, deskWidth: width, deskDepth: depth };
  }
  STATION_CONFIG.forEach(workstation);

  // Back-right walnut coffee nook with open shelving, cups and espresso machine.
  const coffee = COFFEE_CONFIG;
  block(staticRoot, [coffee.x, 2.65, -8.20], [coffee.width + .16, 5.1, .15], M.wood, .02);
  for (let x = 6.65; x < 10.2; x += .16) block(staticRoot, [x, 3.25, -8.08], [.045, 3.6, .075], M.edge, .005);
  block(staticRoot, [coffee.x, .57, coffee.z], [coffee.width, 1.08, coffee.depth], M.wood, .05);
  block(staticRoot, [coffee.x, 1.15, coffee.z], [coffee.width + .08, .10, coffee.depth + .10], M.paleStone, .04);
  for (const dx of [-1.15, 0, 1.15]) {
    block(staticRoot, [coffee.x + dx, .60, coffee.z + .61], [1.08, .91, .025], M.edge, .015);
    rod(staticRoot, [coffee.x + dx - .14, .94, coffee.z + .637], [coffee.x + dx + .14, .94, coffee.z + .637], .012, M.brass);
  }
  for (const y of [2.3, 3.16]) {
    block(staticRoot, [coffee.x, y, -7.71], [coffee.width - .18, .09, .70], M.edge, .025);
    block(staticRoot, [coffee.x, y - .06, -7.64], [coffee.width - .35, .025, .40], M.glow, .006);
    for (let i = 0; i < 5; i++) add(staticRoot, geo('coffee-tin', () => new THREE.CylinderGeometry(.10, .10, .23, 14)), i % 2 ? M.white : M.paleBrass, [7.30 + i * .49, y + .16, -7.70]);
  }
  block(staticRoot, [8.75, 1.57, -7.05], [.83, .70, .62], M.dark, .055);
  block(staticRoot, [8.75, 1.56, -6.72], [.69, .54, .018], M.chrome, .014);
  block(staticRoot, [8.75, 1.70, -6.70], [.36, .16, .021], M.dark, .006);
  rod(staticRoot, [8.75, 1.54, -6.68], [8.75, 1.42, -6.59], .022, M.chrome);
  block(staticRoot, [8.75, 1.26, -6.59], [.60, .035, .35], M.chrome, .012);
  mug(staticRoot, 8.74, 1.29, -6.58); mug(staticRoot, 7.26, 1.21, -6.78); mug(staticRoot, 7.61, 1.21, -6.78);

  // Lounge: pale linen sofa, teal cushions, woven rug and low coffee table.
  block(staticRoot, [-6.6, .06, 5.25], [5.6, .025, 4.3], M.rug, .12);
  for (const sofa of SOFA_CONFIG) {
    const { x, z, width, depth } = sofa;
    block(staticRoot, [x, .33, z], [width, .40, depth], M.linen, .14);
    block(staticRoot, [x, .86, z + .49], [width, .85, .25], M.linen, .13);
    for (const side of [-1, 1]) {
      block(staticRoot, [x + side * (width / 2 - .13), .66, z], [.26, .64, depth], M.linen, .10);
      block(staticRoot, [x + side * 1.0, .59, z - .07], [1.82, .16, 1.02], M.linen, .08);
      const cushion = block(staticRoot, [x + side * 1.35, .91, z + .28], [.66, .59, .16], M.teal, .09); cushion.rotation.z = side * .09;
      for (const dz of [-.47, .47]) rod(staticRoot, [x + side * (width / 2 - .30), .05, z + dz], [x + side * (width / 2 - .30), .19, z + dz], .035, M.brass);
    }
  }
  const table = COLLIDERS.find(value => value.id === 'coffee-table-left');
  block(staticRoot, [table.x, .43, table.z], [table.width, .10, table.depth], M.paleStone, .15);
  for (const side of [-1, 1]) rod(staticRoot, [table.x + side * .55, .05, table.z], [table.x + side * .55, .38, table.z], .035, M.brass);
  block(staticRoot, [table.x - .27, .50, table.z], [.48, .035, .33], M.teal, .012); mug(staticRoot, table.x + .49, .49, table.z);

  // Indoor pool: stone coping, tiled basin and a translucent rippled water surface.
  const pool = POOL_CONFIG;
  block(staticRoot, [pool.x, .14, pool.z], [pool.width, .22, pool.depth], M.paleStone, .06);
  const poolTiles = texture(512, 512, (ctx, w, h) => {
    ctx.fillStyle = '#237f83'; ctx.fillRect(0, 0, w, h);
    for (let y = 0; y < h; y += 32) for (let x = 0; x < w; x += 32) {
      const v = Math.floor(random() * 26); ctx.fillStyle = `rgb(${35 + v},${115 + v},${121 + v})`; ctx.fillRect(x + 1, y + 1, 30, 30);
    }
    ctx.strokeStyle = 'rgba(200,249,234,.38)'; ctx.lineWidth = 1.5;
    for (let i = 0; i < 54; i++) {
      const x = random() * w, y = random() * h; ctx.beginPath(); ctx.ellipse(x, y, 12 + random() * 55, 5 + random() * 18, random() * 3, 0, Math.PI * 2); ctx.stroke();
    }
  });
  poolTiles.wrapS = poolTiles.wrapT = THREE.RepeatWrapping; poolTiles.repeat.set(2, 3);
  const tileMaterial = new THREE.MeshStandardMaterial({ map: poolTiles, roughness: .32, metalness: .04 }); materials.set('pool-tiles', tileMaterial);
  block(staticRoot, [pool.x, .262, pool.z], [pool.width - .54, .025, pool.depth - .54], tileMaterial, .025);
  const ripples = texture(256, 256, (ctx, w, h) => {
    ctx.fillStyle = '#8080ff'; ctx.fillRect(0, 0, w, h);
    for (let y = 0; y < h; y += 5) { ctx.strokeStyle = `rgba(110,155,255,${.22 + random() * .35})`; ctx.beginPath(); for (let x = 0; x <= w; x += 4) { const yy = y + Math.sin(x / 13 + y / 23) * 2.8; if (!x) ctx.moveTo(x, yy); else ctx.lineTo(x, yy); } ctx.stroke(); }
  });
  ripples.colorSpace = THREE.NoColorSpace; ripples.wrapS = ripples.wrapT = THREE.RepeatWrapping; ripples.repeat.set(4, 6);
  const waterMaterial = new THREE.MeshPhysicalMaterial({ color: '#5cc5c4', roughness: .16, metalness: .12, transparent: true, opacity: .64, normalMap: ripples, normalScale: new THREE.Vector2(.24, .24), clearcoat: 1, clearcoatRoughness: .1, depthWrite: false });
  materials.set('pool-water', waterMaterial);
  const water = add(displays, geo('water-surface', () => new THREE.PlaneGeometry(pool.width - .54, pool.depth - .54)), waterMaterial, [pool.x, .295, pool.z]); water.rotation.x = -Math.PI / 2; water.castShadow = false;
  for (const side of [-1, 1]) {
    block(staticRoot, [pool.x + side * (pool.width / 2 - .14), .31, pool.z], [.28, .17, pool.depth], M.paleStone, .025);
    block(staticRoot, [pool.x, .31, pool.z + side * (pool.depth / 2 - .14)], [pool.width, .17, .28], M.paleStone, .025);
    rod(staticRoot, [pool.x + side * .33, .30, pool.z - 3.45], [pool.x + side * .33, .87, pool.z - 3.45], .026, M.chrome);
    rod(staticRoot, [pool.x + side * .33, .87, pool.z - 3.45], [pool.x + side * .33, .87, pool.z - 2.98], .026, M.chrome);
  }

  function plant(config) {
    const { x, z, scale = 1 } = config, group = new THREE.Group(); group.position.set(x, 0, z); group.scale.setScalar(scale); staticRoot.add(group);
    add(group, geo('plant-pot', () => new THREE.CylinderGeometry(.32, .24, .62, 18)), M.paleStone, [0, .33, 0]);
    add(group, geo('pot-rim', () => new THREE.CylinderGeometry(.33, .33, .055, 18)), M.brass, [0, .64, 0]);
    add(group, geo('pot-soil', () => new THREE.CylinderGeometry(.30, .30, .025, 18)), M.soil, [0, .66, 0]);
    for (let leaf = 0; leaf < (mobile ? 9 : 14); leaf++) {
      const angle = leaf * 2.4, y = 1.0 + leaf % 5 * .21, reach = .25 + leaf % 3 * .05;
      rod(group, [0, .66, 0], [Math.cos(angle) * reach, y, Math.sin(angle) * reach], .012, M.leaf);
      const mesh = ellipse(group, [Math.cos(angle) * (reach + .10), y + .21, Math.sin(angle) * (reach + .10)], [.14, .42, .025], leaf % 2 ? M.leaf : M.leafLight);
      mesh.rotation.set(Math.sin(angle) * .65, angle, Math.cos(angle) * .65);
    }
  }
  PLANT_CONFIG.forEach(plant);

  // Batch all fixed furniture by material. Dynamic monitor textures stay separate.
  staticRoot.updateMatrixWorld(true);
  const batches = new Map(); let sourceMeshes = 0;
  staticRoot.traverse(object => {
    if (!object.isMesh) return; sourceMeshes++;
    const geometry = object.geometry.index ? object.geometry.toNonIndexed() : object.geometry.clone(); geometry.applyMatrix4(object.matrixWorld);
    if (!batches.has(object.material)) batches.set(object.material, []); batches.get(object.material).push(geometry);
  });
  staticRoot.clear(); const mergedGeometries = [];
  for (const [material, pieces] of batches) {
    const geometry = mergeGeometries(pieces, false); pieces.forEach(piece => piece.dispose());
    if (!geometry) throw new Error('Office material batch could not be merged'); mergedGeometries.push(geometry);
    const mesh = new THREE.Mesh(geometry, material); mesh.castShadow = material !== M.glass && material !== M.glow; mesh.receiveShadow = material !== M.glass; staticRoot.add(mesh);
  }
  let disposed = false;
  const diagnostics = {
    stationCount: STATION_CONFIG.length, stationNames: STATION_CONFIG.map(station => station.name),
    colliderCount: COLLIDERS.length, loungeSeatCount: LOUNGE_SLOTS.length,
    idleActivities: [...new Set(IDLE_SLOTS.map(slot => slot.kind))],
    staticSourceMeshes: sourceMeshes, staticDrawCalls: batches.size,
    estimatedDrawCalls: batches.size + displays.children.length,
    monitorCount: STATION_CONFIG.reduce((count, station) => count + (station.role === 'boss' ? 3 : 2), 4),
    screenSurfaceCount: displayMeshes.length,
    fixedChairs: true, pool: true, coffeeNook: true, floorBounds: { ...BOUNDS }, mobile,
    textureSources: 'locally generated office materials; existing read-only monitor telemetry',
  };
  return {
    stations: stationary, colliders: COLLIDERS.map(value => ({ ...value })), bounds: { ...BOUNDS },
    loungeSlots: LOUNGE_SLOTS.map(value => ({ ...value, position: { ...value.position }, entry: { ...value.entry } })),
    socialSlots: SOCIAL_SLOTS.map(value => ({ ...value, position: { ...value.position }, entry: { ...value.entry } })),
    idleSlots: IDLE_SLOTS.map(value => ({ ...value, position: { ...value.position }, entry: { ...value.entry } })), diagnostics,
    update(time) { if (!disposed && !mobile) { ripples.offset.x = time * .003; ripples.offset.y = time * .002; } },
    dispose() {
      if (disposed) return; disposed = true; scene.remove(root);
      geometries.forEach(geometry => geometry.dispose()); mergedGeometries.forEach(geometry => geometry.dispose());
      materials.forEach(material => material.dispose()); ownTextures.forEach(value => value.dispose());
    },
  };
}
