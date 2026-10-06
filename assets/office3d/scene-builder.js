import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import {
  BOUNDS, STATION_CONFIG, COLLIDERS, SOFA_CONFIG, PLANT_CONFIG,
  STORAGE_CONFIG, LOUNGE_SLOTS, SOCIAL_SLOTS,
} from './layout.js';

// Static furniture is constructed in local coordinates and then merged by material.
// Monitor textures stay separate so presentation updates never rebuild the room.
export function createOfficeScene(THREE, { scene, monitors, mobile = false }) {
  const root = new THREE.Group(); root.name = 'harun-office-architecture'; scene.add(root);
  const staticRoot = new THREE.Group(); root.add(staticRoot);
  const displays = new THREE.Group(); displays.name = 'office-displays'; root.add(displays);
  const materials = new Map(), geometries = new Map(), ownTextures = new Set();
  const stationary = {}, displayMeshes = [];
  const mat = (color, roughness = .65, metalness = 0) => {
    const key = `${color}:${roughness}:${metalness}`;
    if (!materials.has(key)) materials.set(key, new THREE.MeshStandardMaterial({ color, roughness, metalness }));
    return materials.get(key);
  };
  const M = {
    oak: mat('#c59b70'), edge: mat('#9d7852'), white: mat('#eef0eb'), wall: mat('#dce2df'),
    steel: mat('#81969b', .3, .65), dark: mat('#223740'), chair: mat('#304b57'),
    seat: mat('#55747b', .88), textile: mat('#9aada9', .9), leaf: mat('#477657', .86),
    leafLight: mat('#77a777', .86), soil: mat('#514336'), blue: mat('#173747', .5),
    carpet: mat('#a9bbb9', .98), brass: mat('#a98b5c', .33, .58),
  };
  M.glass = new THREE.MeshStandardMaterial({ color: '#bcd7dc', transparent: true, opacity: .20, roughness: .18, metalness: .05, depthWrite: false });
  M.glow = new THREE.MeshBasicMaterial({ color: '#fff0cd' });
  materials.set('glass', M.glass); materials.set('glow', M.glow);
  const geo = (key, factory) => { if (!geometries.has(key)) geometries.set(key, factory()); return geometries.get(key); };
  const add = (parent, geometry, material, position, scale) => {
    const mesh = new THREE.Mesh(geometry, material);
    if (position) mesh.position.set(...position);
    if (scale) mesh.scale.set(...scale);
    mesh.castShadow = material !== M.glass && material !== M.glow;
    mesh.receiveShadow = material !== M.glass; parent.add(mesh); return mesh;
  };
  const block = (parent, position, size, material, radius = .035) => add(parent,
    geo(`box:${size.join(',')}:${radius}`, () => radius ?
      new RoundedBoxGeometry(...size, mobile ? 1 : 2, Math.min(radius, ...size.map(n => n / 2))) : new THREE.BoxGeometry(...size)),
    material, position);
  const sphere = geo('sphere', () => new THREE.SphereGeometry(1, mobile ? 10 : 14, mobile ? 8 : 10));
  const cylinder = geo('cylinder', () => new THREE.CylinderGeometry(1, 1, 1, mobile ? 10 : 14));
  const ellipse = (parent, position, scale, material) => add(parent, sphere, material, position, scale);
  const rod = (parent, from, to, radius, material) => {
    const a = new THREE.Vector3(...from), b = new THREE.Vector3(...to), direction = b.clone().sub(a);
    const mesh = add(parent, cylinder, material, a.clone().add(b).multiplyScalar(.5).toArray(), [radius, direction.length(), radius]);
    mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.normalize()); return mesh;
  };
  const canvasTexture = (width, height, paint) => {
    const canvas = document.createElement('canvas'); canvas.width = width; canvas.height = height;
    paint(canvas.getContext('2d'), width, height);
    const texture = new THREE.CanvasTexture(canvas); texture.colorSpace = THREE.SRGBColorSpace;
    ownTextures.add(texture); return texture;
  };
  const label = (position, width, height, lines, accent = '#b8d9d4', rotation = 0, background = '#223a44') => {
    const texture = canvasTexture(1024, 256, (ctx, w, h) => {
      ctx.fillStyle = background; ctx.fillRect(0, 0, w, h);
      ctx.fillStyle = accent; ctx.fillRect(0, 0, 10, h);
      ctx.font = '600 48px Arial'; ctx.fillText(lines[0], 38, 102);
      ctx.font = '400 27px Arial'; ctx.fillStyle = '#a6bdc3';
      if (lines[1]) ctx.fillText(lines[1], 38, 167);
    });
    const material = new THREE.MeshBasicMaterial({ map: texture }); materials.set(`label:${materials.size}`, material);
    const mesh = add(displays, geo(`plane:${width}:${height}`, () => new THREE.PlaneGeometry(width, height)), material, position);
    mesh.rotation.y = rotation; mesh.castShadow = false; displayMeshes.push(mesh); return mesh;
  };

  // A floating platform and locally generated oak planks give the cutaway room depth.
  block(staticRoot, [0, -.25, 1], [22.1, .5, 20.75], M.white, .16);
  block(staticRoot, [0, -.04, 1], [21.9, .10, 20.55], M.edge, .04);
  const floorTexture = canvasTexture(512, 512, ctx => {
    ctx.fillStyle = '#cfb495'; ctx.fillRect(0, 0, 512, 512);
    for (let y = 0; y < 512; y += 64) {
      ctx.fillStyle = y % 128 ? '#d1ba9e' : '#c6aa88'; ctx.fillRect(0, y, 512, 63);
      for (let grain = 0; grain < 11; grain++) {
        ctx.strokeStyle = 'rgba(108,75,44,.055)'; ctx.beginPath();
        ctx.moveTo(0, y + grain * 5); ctx.bezierCurveTo(150, y + grain * 5 + 3, 350, y + grain * 5 - 3, 512, y + grain * 5); ctx.stroke();
      }
      ctx.fillStyle = '#b3936e'; ctx.fillRect(y % 128 ? 170 : 370, y, 1, 64);
    }
  });
  floorTexture.wrapS = floorTexture.wrapT = THREE.RepeatWrapping; floorTexture.repeat.set(5, 5);
  const floorMaterial = new THREE.MeshStandardMaterial({ map: floorTexture, roughness: .76 }); materials.set('floor', floorMaterial);
  block(staticRoot, [0, .0225, 1], [21.7, .035, 20.4], floorMaterial, 0);

  // Rear glazing with a soft skyline: no external image and no market connections.
  block(staticRoot, [0, 2.6, -9.38], [22, 5.2, .22], M.wall, .025);
  block(staticRoot, [-10.87, 1.26, 1], [.2, 2.5, 20.5], M.wall, .02);
  block(staticRoot, [10.87, .36, 1], [.2, .72, 20.5], M.white, .02);
  block(staticRoot, [0, .1, 11.32], [22, .20, .16], M.white, .02);
  block(staticRoot, [-10.72, .12, 1], [.06, .20, 20.5], M.white, .01);
  const skyline = canvasTexture(1536, 512, (ctx, w, h) => {
    const gradient = ctx.createLinearGradient(0, 0, 0, h);
    gradient.addColorStop(0, '#a9c6d7'); gradient.addColorStop(.65, '#e9eeea'); gradient.addColorStop(1, '#b9cacc');
    ctx.fillStyle = gradient; ctx.fillRect(0, 0, w, h);
    let seed = 31; const random = () => ((seed = seed * 16807 % 2147483647) - 1) / 2147483646;
    for (let layer = 0; layer < 3; layer++) for (let x = -25; x < w; x += 28 + random() * 42) {
      const height = 50 + random() * (160 + layer * 30), width = 25 + random() * 46;
      ctx.fillStyle = ['#c5d5da', '#b0c5ce', '#9bb4be'][layer]; ctx.fillRect(x, h - height, width, height);
      ctx.fillStyle = '#dce7e7';
      for (let yy = h - height + 12; yy < h - 8; yy += 17) for (let xx = x + 6; xx < x + width - 5; xx += 12) ctx.fillRect(xx, yy, 3, 6);
    }
  });
  const skylineMaterial = new THREE.MeshBasicMaterial({ map: skyline }); materials.set('skyline', skylineMaterial);
  add(displays, geo('skyline-plane', () => new THREE.PlaneGeometry(21.3, 4.6)), skylineMaterial, [0, 2.72, -9.24]);
  for (let x = -10.5; x <= 10.5; x += 2.6) block(staticRoot, [x, 2.73, -9.14], [.055, 4.7, .08], M.steel, .006);
  for (const y of [.41, 5.04]) block(staticRoot, [0, y, -9.14], [21.3, .06, .1], M.steel, .006);

  // A blue operations map anchors the back-left wall, framed in warm oak slats.
  block(staticRoot, [-5.35, 2.84, -8.94], [8.3, 3.9, .26], M.blue, .10);
  for (let x = -9.42; x < -1.2; x += .27) block(staticRoot, [x, 2.84, -8.77], [.075, 3.75, .08], M.edge, .012);
  block(staticRoot, [-5.35, 3.12, -8.66], [7.58, 2.55, .15], M.dark, .07);
  const wallTexture = monitors?.get('market', 1);
  if (wallTexture) {
    const material = new THREE.MeshBasicMaterial({ map: wallTexture }); materials.set('map-display', material);
    const mesh = add(displays, geo('wall-map-plane', () => new THREE.PlaneGeometry(7.34, 2.31)), material, [-5.35, 3.12, -8.57]);
    mesh.castShadow = false; displayMeshes.push(mesh);
  }
  label([-5.35, 1.29, -8.60], 4.2, .55, ['GLOBAL MARKET / OPERATIONS', 'OFFICE STATUS'], '#8ce0e5');
  label([4.30, 3.05, -9.07], 6.8, 1.25, ['HARUN AI TRADING OFFICE', 'RESEARCH · RISK · REVIEW'], '#d7c098', 0, '#2e4850');

  // Slender light rails hover over the work rows without enclosing the cutaway.
  for (const z of [-4.7, 1.1]) for (const x of [-6, 0, 6]) {
    block(staticRoot, [x, 4.73, z], [3.25, .1, .30], M.dark, .04);
    block(staticRoot, [x, 4.666, z], [3.05, .025, .22], M.glow, .008);
    for (const dx of [-1.18, 1.18]) rod(staticRoot, [x + dx, 4.76, z], [x + dx, 5.16, z], .012, M.steel);
  }

  function partition(station) {
    const { x, z } = station;
    // Lower white acoustic panel, translucent upper strip and slim metal rails.
    block(staticRoot, [x, .64, z - .99], [4.35, 1.18, .12], M.white, .025);
    block(staticRoot, [x, 1.42, z - .99], [4.23, .39, .07], M.glass, .01);
    block(staticRoot, [x, 1.65, z - .99], [4.38, .055, .12], M.steel, .012);
    for (const side of [-1, 1]) {
      const sx = x + side * 2.175;
      block(staticRoot, [sx, .64, z + .2], [.12, 1.18, 2.5], M.white, .025);
      block(staticRoot, [sx, 1.42, z + .2], [.07, .39, 2.37], M.glass, .01);
      block(staticRoot, [sx, 1.65, z + .2], [.12, .055, 2.52], M.steel, .012);
      block(staticRoot, [sx, .82, z + 1.43], [.12, 1.67, .1], M.steel, .014);
    }
  }

  function chair(x, z) {
    const group = new THREE.Group(); group.position.set(x, 0, z); staticRoot.add(group);
    rod(group, [0, .12, 0], [0, .69, 0], .055, M.steel);
    for (let index = 0; index < 5; index++) {
      const angle = index * Math.PI * 2 / 5, px = Math.cos(angle) * .36, pz = Math.sin(angle) * .36;
      rod(group, [0, .17, 0], [px, .12, pz], .025, M.steel);
      ellipse(group, [px, .07, pz], [.064, .045, .064], M.dark);
    }
    block(group, [0, .72, 0], [.64, .12, .60], M.seat, .07);
    block(group, [0, 1.06, .30], [.64, .61, .11], M.chair, .07).rotation.x = -.11;
    block(group, [0, 1.40, .31], [.38, .16, .115], M.seat, .055);
    for (const side of [-1, 1]) {
      rod(group, [side * .28, .64, .12], [side * .35, .95, .01], .023, M.dark);
      block(group, [side * .35, .98, .01], [.065, .05, .30], M.chair, .024);
    }
    return group;
  }

  function monitor(role, variant, center, angle = 0, width = 1.22) {
    const [x, y, z] = center;
    const group = new THREE.Group(); group.position.set(x, y, z); group.rotation.y = angle; staticRoot.add(group);
    block(group, [0, -.48, -.015], [.35, .03, .25], M.steel, .017);
    rod(group, [0, -.46, -.035], [0, -.07, -.035], .026, M.steel);
    block(group, [0, 0, -.025], [width + .09, .79, .075], M.dark, .035);
    const texture = monitors?.get(role, variant);
    if (texture) {
      const material = new THREE.MeshBasicMaterial({ map: texture }); materials.set(`monitor:${role}:${variant}`, material);
      const screen = add(displays, geo(`screen:${width}`, () => new THREE.PlaneGeometry(width, .69)), material, [x, y, z]);
      screen.rotation.y = angle; screen.position.x += Math.sin(angle) * .017; screen.position.z += Math.cos(angle) * .017;
      screen.castShadow = false; displayMeshes.push(screen);
    }
    ellipse(group, [width * .42, -.35, .024], [.009, .009, .008], M.glow);
  }

  function workstation(station) {
    const { role, x, z, accent } = station, boss = role === 'boss';
    const width = boss ? 4.1 : 3.5, depth = boss ? 1.5 : 1.45;
    const accentMaterial = mat(accent, .6);
    block(staticRoot, [x, .04, z + .46], [boss ? 5.0 : 4.17, .034, 3.5], M.carpet, .09);
    if (!boss) partition(station);
    if (boss) {
      // A curved executive desktop with its keyboard edge gently inset.
      const shape = new THREE.Shape();
      shape.moveTo(-2.02, -.60); shape.quadraticCurveTo(0, -.82, 2.02, -.60);
      shape.lineTo(2.02, .65); shape.quadraticCurveTo(1.35, .70, .68, .57);
      shape.quadraticCurveTo(0, .39, -.68, .57); shape.quadraticCurveTo(-1.35, .70, -2.02, .65); shape.closePath();
      const desktop = new THREE.ExtrudeGeometry(shape, { depth: .12, bevelEnabled: true, bevelSegments: 2, steps: 1, bevelSize: .028, bevelThickness: .015, curveSegments: 20 });
      // Shape's local Y maps onto floor Z, while extrusion is the desktop height.
      desktop.rotateX(-Math.PI / 2); desktop.translate(x, .965, z);
      geometries.set('boss-curved-desktop', desktop); add(staticRoot, desktop, M.oak);
    } else block(staticRoot, [x, 1.04, z], [width, .12, depth], M.oak, .065);
    block(staticRoot, [x, .969, z], [width - .10, .025, depth - .08], M.edge, .006);
    for (const offset of [-width / 2 + .25, width / 2 - .25]) {
      block(staticRoot, [x + offset, .50, z - .04], [.07, .88, 1.09], M.white, .022);
      block(staticRoot, [x + offset, .08, z - .04], [.39, .06, 1.15], M.steel, .018);
    }
    block(staticRoot, [x - width / 2 + .48, .53, z - .12], [.60, .76, .74], M.white, .04);
    for (let drawer = 0; drawer < 3; drawer++) {
      block(staticRoot, [x - width / 2 + .48, .31 + drawer * .22, z + .265], [.53, .19, .018], M.wall, .008);
      block(staticRoot, [x - width / 2 + .48, .35 + drawer * .22, z + .278], [.17, .018, .027], M.steel, .004);
    }
    if (boss) {
      monitor(role, 0, [x - 1.15, 1.64, z - .25], .25, 1.03);
      monitor(role, 1, [x, 1.64, z - .39], 0, 1.08);
      monitor(role, 2, [x + 1.15, 1.64, z - .25], -.25, 1.03);
    } else {
      monitor(role, 0, [x - .68, 1.60, z - .32], .085);
      monitor(role, 1, [x + .68, 1.60, z - .32], -.085);
    }
    block(staticRoot, [x - .08, 1.118, z + .38], [.73, .027, .24], M.dark, .022);
    const keyRows = mobile ? 3 : 4, keyColumns = mobile ? 9 : 11;
    for (let row = 0; row < keyRows; row++) for (let column = 0; column < keyColumns; column++)
      block(staticRoot, [x - .37 + column * .057, 1.137, z + .299 + row * .048], [.041, .006, .030], M.steel, .002);
    block(staticRoot, [x + .55, 1.115, z + .36], [.28, .012, .30], M.chair, .02);
    ellipse(staticRoot, [x + .55, 1.146, z + .36], [.065, .035, .095], M.white);
    block(staticRoot, [x - 1.29, 1.125, z + .36], [.31, .035, .39], M.dark, .013);
    block(staticRoot, [x - 1.29, 1.144, z + .36], [.27, .005, .35], accentMaterial, .005);
    const mug = geo('mug', () => new THREE.CylinderGeometry(.074, .068, .17, 12));
    add(staticRoot, mug, M.white, [x + 1.37, 1.18, z + .29]);
    add(staticRoot, geo('mug-handle', () => new THREE.TorusGeometry(.054, .012, 5, 12)), M.white, [x + 1.447, 1.18, z + .29]);
    block(staticRoot, [x, .74, z + depth / 2 + .012], [2.55, .36, .04], M.dark, .017);
    label([x, .74, z + depth / 2 + .038], 2.45, .28, [station.name, ''], accent);
    chair(x, z + 1.45);
    // Metadata stays independent of merged furniture; seats never follow avatars.
    stationary[role] = { ...station, seat: { ...station.seat }, dock: { ...station.dock }, entry: { ...station.entry }, deskWidth: width, deskDepth: depth };
  }
  STATION_CONFIG.forEach(workstation);

  function plant(config) {
    const { x, z, scale = 1 } = config;
    const group = new THREE.Group(); group.position.set(x, 0, z); group.scale.setScalar(scale); staticRoot.add(group);
    add(group, geo('plant-pot', () => new THREE.CylinderGeometry(.30, .24, .55, 14)), M.white, [0, .30, 0]);
    add(group, geo('pot-soil', () => new THREE.CylinderGeometry(.275, .275, .025, 14)), M.soil, [0, .58, 0]);
    for (let leaf = 0; leaf < (mobile ? 7 : 10); leaf++) {
      const angle = leaf * 2.4, y = .86 + (leaf % 3) * .20;
      rod(group, [0, .57, 0], [Math.cos(angle) * .21, y, Math.sin(angle) * .21], .015, M.leaf);
      const mesh = ellipse(group, [Math.cos(angle) * .27, y + .18, Math.sin(angle) * .27], [.12, .33, .048], leaf % 2 ? M.leaf : M.leafLight);
      mesh.rotation.set(Math.sin(angle) * .50, angle, Math.cos(angle) * .50);
    }
  }
  PLANT_CONFIG.forEach(plant);

  function storage(config) {
    const { x, z, width, depth } = config;
    block(staticRoot, [x, .58, z], [width, 1.10, depth], M.white, .05);
    block(staticRoot, [x, 1.17, z], [width + .05, .075, depth + .06], M.oak, .023);
    const inside = x < 0 ? 1 : -1;
    for (const offset of [-.61, .61]) {
      block(staticRoot, [x + inside * (width / 2 + .014), .59, z + offset], [.025, .94, 1.16], M.wall, .008);
      rod(staticRoot, [x + inside * (width / 2 + .035), .66, z + offset - .1], [x + inside * (width / 2 + .035), .66, z + offset + .1], .014, M.steel);
    }
    for (let book = 0; book < 5; book++) block(staticRoot, [x, 1.24 + book * .045, z + .45], [.44, .035, .34], book % 2 ? M.dark : M.edge, .006);
    block(staticRoot, [x, 1.23, z - .58], [.43, .035, .44], M.white, .015);
  }
  STORAGE_CONFIG.forEach(storage);
  block(staticRoot, [-3.15, .56, 7.05], [.70, 1.05, 1.40], M.white, .04);
  block(staticRoot, [-3.15, 1.13, 7.05], [.76, .07, 1.46], M.oak, .021);

  // Water cooler, transparent bottle and a small cup shelf.
  block(staticRoot, [-9.8, .69, 5.7], [.73, 1.28, .70], M.white, .055);
  block(staticRoot, [-9.8, .98, 6.055], [.48, .34, .026], M.dark, .018);
  for (const dx of [-.13, .13]) block(staticRoot, [-9.8 + dx, 1.02, 6.08], [.055, .09, .075], dx < 0 ? M.blue : M.brass, .008);
  add(staticRoot, geo('water-bottle', () => new THREE.CylinderGeometry(.25, .24, .55, 16)), M.glass, [-9.8, 1.62, 5.7]);
  add(staticRoot, geo('water-neck', () => new THREE.CylinderGeometry(.095, .095, .16, 12)), M.blue, [-9.8, 1.31, 5.7]);
  block(staticRoot, [-9.8, .72, 6.06], [.51, .035, .15], M.steel, .009);

  function sofa(config) {
    const { x, z, width, depth } = config;
    block(staticRoot, [x, .36, z], [width, .42, depth], M.chair, .13);
    block(staticRoot, [x, .83, z + .47], [width, .72, .23], M.seat, .105);
    for (const side of [-1, 1]) {
      block(staticRoot, [x + side * (width / 2 - .10), .65, z], [.20, .56, depth], M.seat, .085);
      block(staticRoot, [x + side * .70, .58, z - .09], [1.31, .14, .83], M.textile, .07);
      const cushion = block(staticRoot, [x + side * .92, .84, z + .23], [.50, .43, .14], M.textile, .09); cushion.rotation.z = side * .10;
      for (const dz of [-.42, .42]) block(staticRoot, [x + side * (width / 2 - .25), .09, z + dz], [.09, .13, .09], M.steel, .008);
    }
  }
  SOFA_CONFIG.forEach(sofa);
  for (const table of COLLIDERS.filter(value => value.id.startsWith('coffee-table'))) {
    block(staticRoot, [table.x, .48, table.z], [table.width, .10, table.depth], M.oak, .09);
    for (const side of [-1, 1]) rod(staticRoot, [table.x + side * .42, .07, table.z], [table.x + side * .42, .44, table.z], .037, M.dark);
    block(staticRoot, [table.x - .28, .55, table.z], [.42, .03, .31], M.blue, .012);
    block(staticRoot, [table.x - .28, .57, table.z], [.36, .005, .27], M.white, .003);
    add(staticRoot, geo('table-cup', () => new THREE.CylinderGeometry(.065, .055, .13, 12)), M.white, [table.x + .4, .60, table.z]);
  }
  label([-7.7, 1.6, -9.05], 2.4, .38, ['OPERATIONS FLOOR', ''], '#aee0da');

  // Merge once. Even individual keyboard keys and chair wheels cost one draw per material.
  staticRoot.updateMatrixWorld(true);
  const batches = new Map(); let sourceMeshes = 0;
  staticRoot.traverse(object => {
    if (!object.isMesh) return;
    sourceMeshes++;
    const geometry = object.geometry.index ? object.geometry.toNonIndexed() : object.geometry.clone();
    geometry.applyMatrix4(object.matrixWorld);
    if (!batches.has(object.material)) batches.set(object.material, []);
    batches.get(object.material).push(geometry);
  });
  staticRoot.clear();
  const mergedGeometries = [];
  for (const [material, pieces] of batches) {
    const geometry = mergeGeometries(pieces, false);
    pieces.forEach(piece => piece.dispose());
    if (!geometry) throw new Error('Office material batch could not be merged');
    mergedGeometries.push(geometry);
    const mesh = new THREE.Mesh(geometry, material); mesh.castShadow = material !== M.glass && material !== M.glow;
    mesh.receiveShadow = material !== M.glass; staticRoot.add(mesh);
  }
  let disposed = false;
  const diagnostics = {
    stationCount: STATION_CONFIG.length, colliderCount: COLLIDERS.length,
    loungeSeatCount: LOUNGE_SLOTS.length, staticSourceMeshes: sourceMeshes,
    staticDrawCalls: batches.size, estimatedDrawCalls: batches.size + displays.children.length,
    monitorCount: displayMeshes.filter(mesh => mesh.material.map && !ownTextures.has(mesh.material.map)).length,
    fixedChairs: true, floorBounds: { ...BOUNDS }, mobile,
  };
  return {
    stations: stationary, colliders: COLLIDERS.map(value => ({ ...value })), bounds: { ...BOUNDS },
    loungeSlots: LOUNGE_SLOTS.map(value => ({ ...value, position: { ...value.position }, entry: { ...value.entry } })),
    socialSlots: SOCIAL_SLOTS.map(value => ({ ...value, position: { ...value.position } })), diagnostics,
    dispose() {
      if (disposed) return; disposed = true; scene.remove(root);
      geometries.forEach(geometry => geometry.dispose()); mergedGeometries.forEach(geometry => geometry.dispose());
      materials.forEach(material => material.dispose()); ownTextures.forEach(texture => texture.dispose());
    },
  };
}
