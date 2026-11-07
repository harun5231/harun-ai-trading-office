// Office presentation coordinates only. Backend roles and account state are unchanged.
export const BOUNDS = Object.freeze({ minX: -10.6, maxX: 10.6, minZ: -8.2, maxZ: 8.6 });
export const STATION_CONFIG = Object.freeze([
  { role: 'market', name: 'HERMES AGENT', x: -6.3, z: -3.4, accent: '#79b7ac', gender: 'male' },
  { role: 'neuro', name: 'NEUROBRO', x: -2.1, z: -3.4, accent: '#a2c4ad', gender: 'female' },
  { role: 'risk', name: 'RISK MANAGER', x: 2.1, z: -3.4, accent: '#d7b77e', gender: 'female' },
  { role: 'trading', name: 'BINANCE', x: 6.3, z: -3.4, accent: '#e8cf8a', gender: 'female' },
  { role: 'boss', name: 'HARUN', x: 0, z: 3.5, accent: '#d8bb83', gender: 'male' },
].map(station => Object.freeze({
  ...station, yaw: 0,
  seat: Object.freeze({ x: station.x, z: station.z - (station.role === 'boss' ? 1.5 : 1.35), y: .78 }),
  dock: Object.freeze({ x: station.x, z: station.z - (station.role === 'boss' ? 1.5 : 1.35) }),
  entry: Object.freeze({ x: station.x + 1.08, z: station.z - (station.role === 'boss' ? 1.5 : 1.35) }),
})));
export const SOFA_CONFIG = Object.freeze([
  Object.freeze({ id: 'sofa-left', x: -6.6, z: 6.1, width: 4.2, depth: 1.3 }),
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
  ...STATION_CONFIG.flatMap(({ role, x, z }) => [
    { id: `desk-${role}`, x, z, width: role === 'boss' ? 4.8 : 3.65, depth: role === 'boss' ? 1.8 : 1.55 },
    { id: `chair-${role}`, x, z: z - (role === 'boss' ? 1.5 : 1.35), radius: .50 },
  ]),
  ...SOFA_CONFIG.map(value => ({ ...value })),
  ...PLANT_CONFIG.map(({ id, x, z, radius }) => ({ id, x, z, radius })),
  { ...POOL_CONFIG }, { ...COFFEE_CONFIG },
  { id: 'coffee-table-left', x: -6.6, z: 3.7, width: 1.7, depth: .8 },
].map(Object.freeze));
export const LOUNGE_SLOTS = Object.freeze(SOFA_CONFIG.flatMap(sofa => [-1.05, 1.05].map((offset, index) => Object.freeze({
  id: `${sofa.id}-seat-${index + 1}`,
  position: Object.freeze({ x: sofa.x + offset, z: sofa.z - .05 }),
  entry: Object.freeze({ x: sofa.x + offset, z: sofa.z - 1.3 }),
  yaw: Math.PI, seatHeight: .65, colliderId: sofa.id,
}))));
export const IDLE_SLOTS = Object.freeze([
  { id: 'coffee-a', kind: 'COFFEE', x: 8.7, z: -5.4, yaw: Math.PI },
  { id: 'coffee-b', kind: 'COFFEE', x: 7.3, z: -5.4, yaw: Math.PI },
  { id: 'pool-a', kind: 'POOL', x: 4.5, z: 2.1, yaw: Math.PI / 2 },
  { id: 'pool-b', kind: 'POOL', x: 4.5, z: 4.3, yaw: Math.PI / 2 },
  { id: 'visit-harun-a', kind: 'VISIT', x: -3.2, z: 3.8, yaw: Math.PI / 2 },
  { id: 'visit-harun-b', kind: 'VISIT', x: 3.2, z: 3.8, yaw: -Math.PI / 2 },
  { id: 'discussion-a', kind: 'SOCIAL', x: -4, z: 1.2, yaw: Math.PI / 2, partnerId: 'discussion-b' },
  { id: 'discussion-b', kind: 'SOCIAL', x: -2.6, z: 1.2, yaw: -Math.PI / 2, partnerId: 'discussion-a' },
  { id: 'discussion-c', kind: 'SOCIAL', x: -6.7, z: 1.7, yaw: Math.PI / 2, partnerId: 'discussion-d' },
  { id: 'discussion-d', kind: 'SOCIAL', x: -5.3, z: 1.7, yaw: -Math.PI / 2, partnerId: 'discussion-c' },
].map(value => Object.freeze({ ...value,
  position: Object.freeze({ x: value.x, z: value.z }),
  entry: Object.freeze({ x: value.x, z: value.z }),
})));
export const SOCIAL_SLOTS = Object.freeze(IDLE_SLOTS.filter(slot => slot.kind === 'SOCIAL'));
export const bounds = BOUNDS;
export const stations = STATION_CONFIG;
export const colliders = COLLIDERS;
export const loungeSlots = LOUNGE_SLOTS;
export const socialSlots = SOCIAL_SLOTS;
export const idleSlots = IDLE_SLOTS;
