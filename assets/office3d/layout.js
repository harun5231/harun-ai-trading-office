// World coordinates shared by furniture and pedestrian movement. No account data.
export const BOUNDS = Object.freeze({ minX: -10.7, maxX: 10.7, minZ: -9.1, maxZ: 11.0 });

export const STATION_CONFIG = Object.freeze([
  { role: 'market', name: 'MARKET ANALYST', x: -6, z: -5.2, accent: '#73cde1' },
  { role: 'neuro', name: 'NEUROBRO', x: 0, z: -5.2, accent: '#81d2a9' },
  { role: 'risk', name: 'RISK MANAGER', x: 6, z: -5.2, accent: '#e5b87b' },
  { role: 'trading', name: 'TRADING AGENT', x: -6, z: .6, accent: '#e7cc87' },
  { role: 'position', name: 'POSITION MONITOR', x: 0, z: .6, accent: '#98b8ed' },
  { role: 'reviewer', name: 'TRADE REVIEWER', x: 6, z: .6, accent: '#87d5c7' },
  { role: 'report', name: 'REPORT MANAGER', x: 6, z: 5, accent: '#c4b2e8' },
  { role: 'boss', name: 'BOSS · KAMU', x: 0, z: 7.1, accent: '#edcf92' },
].map(station => Object.freeze({
  ...station, yaw: Math.PI,
  seat: Object.freeze({ x: station.x, z: station.z + 1.45, y: .78 }),
  dock: Object.freeze({ x: station.x, z: station.z + 1.45 }),
  entry: Object.freeze({ x: station.x + 1.08, z: station.z + 1.45 }),
})));

export const SOFA_CONFIG = Object.freeze([
  Object.freeze({ id: 'sofa-left', x: -6.6, z: 8.9, width: 3.2, depth: 1.2 }),
  Object.freeze({ id: 'sofa-right', x: 7.0, z: 9.5, width: 3.2, depth: 1.2 }),
]);

export const PLANT_CONFIG = Object.freeze([
  { id: 'plant-rear-left', x: -9.65, z: -7.6, radius: .60, scale: 1.2 },
  { id: 'plant-rear-right', x: 9.65, z: -7.6, radius: .60, scale: 1.2 },
  { id: 'plant-mid-left', x: -9.65, z: 3.5, radius: .54, scale: 1.05 },
  { id: 'plant-mid-right', x: 9.7, z: 4.2, radius: .54, scale: 1.05 },
  { id: 'plant-front-left', x: -9.7, z: 10.15, radius: .58, scale: 1.15 },
  { id: 'plant-front-right', x: 9.7, z: 10.2, radius: .58, scale: 1.15 },
].map(Object.freeze));

export const STORAGE_CONFIG = Object.freeze([
  { id: 'storage-left-rear', x: -10.03, z: -4.5, width: .96, depth: 2.5 },
  { id: 'storage-left-mid', x: -10.03, z: .3, width: .96, depth: 2.5 },
  { id: 'storage-right', x: 10.03, z: -3.7, width: .96, depth: 2.5 },
].map(Object.freeze));

const stationColliders = STATION_CONFIG.flatMap(station => {
  const { role, x, z } = station;
  const items = [
    { id: `desk-${role}`, x, z, width: role === 'boss' ? 4.1 : 3.5, depth: role === 'boss' ? 1.5 : 1.45 },
    { id: `chair-${role}`, x, z: z + 1.45, radius: .50 },
  ];
  if (role !== 'boss') items.push(
    { id: `partition-back-${role}`, x, z: z - .99, width: 4.35, depth: .12 },
    { id: `partition-left-${role}`, x: x - 2.175, z: z + .2, width: .12, depth: 2.5 },
    { id: `partition-right-${role}`, x: x + 2.175, z: z + .2, width: .12, depth: 2.5 },
  );
  return items;
});

export const COLLIDERS = Object.freeze([
  { id: 'wall-rear', x: 0, z: -9.38, width: 22, depth: .22 },
  { id: 'wall-left', x: -10.87, z: 1, width: .2, depth: 20.5 },
  { id: 'wall-right', x: 10.87, z: 1, width: .2, depth: 20.5 },
  { id: 'wall-front', x: 0, z: 11.32, width: 22, depth: .16 },
  ...stationColliders,
  ...SOFA_CONFIG.map(sofa => ({ ...sofa })),
  ...PLANT_CONFIG.map(({ id, x, z, radius }) => ({ id, x, z, radius })),
  ...STORAGE_CONFIG.map(storage => ({ ...storage })),
  { id: 'water-cooler', x: -9.8, z: 5.7, width: .76, depth: .74 },
  { id: 'coffee-table-left', x: -9.0, z: 8.4, width: 1.2, depth: .65 },
  { id: 'coffee-table-right', x: 9.45, z: 8.7, width: 1.2, depth: .65 },
  { id: 'boss-side-storage', x: -3.15, z: 7.05, width: .7, depth: 1.4 },
].map(Object.freeze));

export const LOUNGE_SLOTS = Object.freeze(SOFA_CONFIG.flatMap(sofa => [-.68, .68].map((offset, index) => Object.freeze({
  id: `${sofa.id}-seat-${index + 1}`,
  position: Object.freeze({ x: sofa.x + offset, z: sofa.z - .05 }),
  entry: Object.freeze({ x: sofa.x + offset, z: sofa.z - 1.18 }),
  yaw: Math.PI, seatHeight: .65, colliderId: sofa.id,
}))));

export const SOCIAL_SLOTS = Object.freeze([
  { id: 'social-row-left-a', x: -6.75, z: 4.0 },
  { id: 'social-row-left-b', x: -5.40, z: 4.0 },
  { id: 'social-row-center-a', x: .40, z: 4.0 },
  { id: 'social-row-center-b', x: 1.75, z: 4.0 },
  { id: 'social-front-center', x: -2.8, z: 9.9 },
  { id: 'social-front-left', x: -4.1, z: 9.9 },
  { id: 'social-front-right', x: 3.45, z: 9.9 },
].map(value => Object.freeze({ ...value, position: Object.freeze({ x: value.x, z: value.z }) })));

// Lowercase aliases keep callers independent of the module's naming convention.
export const bounds = BOUNDS;
export const stations = STATION_CONFIG;
export const colliders = COLLIDERS;
export const loungeSlots = LOUNGE_SLOTS;
export const socialSlots = SOCIAL_SLOTS;
