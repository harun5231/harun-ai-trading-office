// Office presentation coordinates only. Backend roles and account state are unchanged.
export const BOUNDS = Object.freeze({ minX: -10.6, maxX: 10.6, minZ: -8.2, maxZ: 8.6 });
export const STATION_CONFIG = Object.freeze([
  { role: 'market', name: 'HERMES AGENT', x: -6.3, z: -3.0, accent: '#79b7ac', gender: 'male' },
  { role: 'neuro', name: 'NEUROBRO', x: -2.1, z: -3.55, accent: '#a2c4ad', gender: 'female' },
  { role: 'risk', name: 'RISK MANAGER', x: 2.1, z: -3.55, accent: '#d7b77e', gender: 'female' },
  { role: 'trading', name: 'BINANCE', x: 6.3, z: -3.0, accent: '#e8cf8a', gender: 'female' },
  { role: 'boss', name: 'HARUN', x: 0, z: 3.5, accent: '#d8bb83', gender: 'male' },
].map(station => {
  // Anchors measured from the supplied rig: feet stay on the floor while the
  // pelvis moves backward onto the seat. Furniture adapts to the human, not vice versa.
  const male = station.gender === 'male', depth = station.role === 'boss' ? 1.4 : 1.25;
  const rootZ = station.z - depth / 2 - .16;
  const seatOffset = male ? .36 : .31;
  const seatY = { market: .432, boss: .421, neuro: .373, risk: .366, trading: .373 }[station.role];
  const keyTop = { market: .752, boss: .752, neuro: .688, risk: .677, trading: .692 }[station.role];
  return Object.freeze({ ...station, z: station.z + .135, yaw: 0, deskWidth: station.role === 'boss' ? 4.8 : 3.65,
    deskDepth: depth - .27, deskTop: keyTop - .032, keyTop: keyTop - .009,
    seat: Object.freeze({ x: station.x, z: rootZ - seatOffset, y: seatY, depth: male ? .64 : .46, backZ: rootZ - .58 }),
    dock: Object.freeze({ x: station.x, z: rootZ }),
    approach: Object.freeze({ x: station.x + 1.04, z: rootZ }),
    entry: Object.freeze({ x: station.x + 1.04, z: rootZ - .60 }),
    ignoreIds: Object.freeze([`chair-${station.role}`, `desk-${station.role}`]),
  });
}));
export const SOFA_CONFIG = Object.freeze([
  Object.freeze({ id: 'sofa-left', x: -6.6, z: 6.1, width: 4.2, depth: .9 }),
]);
export const POOL_CONFIG = Object.freeze({ id: 'pool', x: 7.6, z: 4.0, width: 4.7, depth: 7.1 });
export const COFFEE_CONFIG = Object.freeze({ id: 'coffee-bar', x: 8.45, z: -7.05, width: 3.5, depth: 1.2 });
export const PLANT_CONFIG = Object.freeze([
  { id: 'plant-rear-left', x: -9.5, z: -7.2, radius: .57, scale: 1.38 },
  { id: 'plant-rear-right', x: 10, z: -5.75, radius: .42, scale: .95 },
  { id: 'plant-front-left', x: -9.45, z: 7.65, radius: .57, scale: 1.2 },
  { id: 'plant-front-right', x: 9.75, z: 8.05, radius: .48, scale: 1.0 },
  { id: 'plant-lounge', x: -9.45, z: 3.8, radius: .5, scale: 1.1 },
].map(Object.freeze));
export const STORAGE_CONFIG = Object.freeze([]);
export const COLLIDERS = Object.freeze([
  { id: 'wall-rear', x: 0, z: -8.43, width: 22, depth: .22 },
  { id: 'wall-left', x: -10.87, z: .1, width: .2, depth: 17.3 },
  { id: 'wall-right', x: 10.87, z: .1, width: .2, depth: 17.3 },
  { id: 'wall-front', x: 0, z: 8.88, width: 22, depth: .16 },
  ...STATION_CONFIG.flatMap(({ role, x, z, deskWidth, deskDepth, seat }) => [
    { id: `desk-${role}`, x, z, width: deskWidth, depth: deskDepth },
    // Keep the back and real support legs solid even during the owner's docking.
    ...[-1, 1].flatMap(side => [-1, 1].map(end => ({
      id: `desk-leg-${role}-${side}-${end}`, x: x + side * (deskWidth / 2 - .22),
      z: z + end * (deskDepth / 2 - .20), width: .10, depth: .10,
    }))),
    // Reserve the entire .42 m rolling travel even when the chair is tucked in.
    { id: `chair-${role}`, x, z: seat.z - .21, width: .76, depth: 1.18 },
    { id: `chair-back-${role}`, x, z: seat.backZ - .21, width: .70, depth: .56 },
  ]),
  ...SOFA_CONFIG.flatMap(value => [{ ...value },
    { id: `${value.id}-back`, x: value.x, z: value.z + .35, width: value.width, depth: .20 },
    ...[-1, 1].map(side => ({ id: `${value.id}-arm-${side}`, x: value.x + side * (value.width / 2 - .13), z: value.z, width: .26, depth: value.depth })),
  ]),
  ...PLANT_CONFIG.map(({ id, x, z, radius }) => ({ id, x, z, radius })),
  { ...POOL_CONFIG }, { ...COFFEE_CONFIG },
  ...[1.55, 5.8].map((z, index) => ({ id: `pool-seat-${index}`, x: 4.92, z, width: .50, depth: 1.25 })),
  { id: 'coffee-table-left', x: -6.6, z: 3.7, width: 1.7, depth: .8 },
].map(Object.freeze));
export const LOUNGE_SLOTS = Object.freeze(SOFA_CONFIG.flatMap(sofa => [-1.05, 1.05].map((offset, index) => Object.freeze({
  id: `${sofa.id}-seat-${index + 1}`,
  position: Object.freeze({ x: sofa.x + offset, z: sofa.z - .62 }),
  entry: Object.freeze({ x: sofa.x + offset, z: sofa.z - 1.3 }),
  yaw: Math.PI, seatHeight: index === 0 ? .421 : .366, gender: index === 0 ? 'male' : 'female', colliderId: sofa.id, ignoreIds: Object.freeze([sofa.id]),
}))));
export const IDLE_SLOTS = Object.freeze([
  { id: 'coffee-a', kind: 'COFFEE', x: 8.7, z: -5.4, yaw: Math.PI },
  { id: 'coffee-b', kind: 'COFFEE', x: 7.3, z: -5.4, yaw: Math.PI },
  { id: 'pool-a', kind: 'POOL', x: 4.55, z: 1.55, yaw: -Math.PI / 2, seatHeight: .421, gender: 'male', colliderId: 'pool-seat-0', ignoreIds: ['pool-seat-0'], entry: { x: 3.95, z: 1.55 } },
  { id: 'pool-b', kind: 'POOL', x: 4.55, z: 5.8, yaw: -Math.PI / 2, seatHeight: .366, gender: 'female', colliderId: 'pool-seat-1', ignoreIds: ['pool-seat-1'], entry: { x: 3.95, z: 5.8 } },
  { id: 'visit-harun-a', kind: 'VISIT', x: -.95, z: 4.9, yaw: Math.PI },
  { id: 'visit-harun-b', kind: 'VISIT', x: .95, z: 4.9, yaw: Math.PI },
  { id: 'discussion-a', kind: 'SOCIAL', x: -4, z: 1.2, yaw: Math.PI / 2, partnerId: 'discussion-b' },
  { id: 'discussion-b', kind: 'SOCIAL', x: -2.6, z: 1.2, yaw: -Math.PI / 2, partnerId: 'discussion-a' },
  { id: 'discussion-c', kind: 'SOCIAL', x: -6.7, z: 1.7, yaw: Math.PI / 2, partnerId: 'discussion-d' },
  { id: 'discussion-d', kind: 'SOCIAL', x: -5.3, z: 1.7, yaw: -Math.PI / 2, partnerId: 'discussion-c' },
].map(value => Object.freeze({ ...value,
  position: Object.freeze({ x: value.x, z: value.z }),
  entry: Object.freeze(value.entry || { x: value.x, z: value.z }),
})));
export const SOCIAL_SLOTS = Object.freeze(IDLE_SLOTS.filter(slot => slot.kind === 'SOCIAL'));
export const bounds = BOUNDS;
export const stations = STATION_CONFIG;
export const colliders = COLLIDERS;
export const loungeSlots = LOUNGE_SLOTS;
export const socialSlots = SOCIAL_SLOTS;
export const idleSlots = IDLE_SLOTS;
