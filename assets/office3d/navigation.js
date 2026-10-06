// Local visual movement only. Geometry and actor inputs are never mutated.
const EPS = 1e-7;
const DEFAULT_GAP = 0.025;
const finite = value => typeof value === 'number' && Number.isFinite(value);
const point = value => value && finite(value.x) && finite(value.z);
const copyPoint = value => ({ x: value.x, z: value.z });
const distance2 = (a, b) => (a.x - b.x) ** 2 + (a.z - b.z) ** 2;
const distance = (a, b) => Math.sqrt(distance2(a, b));

function pointSegmentDistance2(p, a, b) {
  const dx = b.x - a.x, dz = b.z - a.z;
  const length2 = dx * dx + dz * dz;
  const t = length2 > EPS * EPS
    ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.z - a.z) * dz) / length2)) : 0;
  return (p.x - a.x - t * dx) ** 2 + (p.z - a.z - t * dz) ** 2;
}

function cross(a, b, c) {
  return (b.x - a.x) * (c.z - a.z) - (b.z - a.z) * (c.x - a.x);
}

function segmentsIntersect(a, b, c, d) {
  const aa = cross(a, b, c), ab = cross(a, b, d);
  const ba = cross(c, d, a), bb = cross(c, d, b);
  if (((aa > EPS && ab < -EPS) || (aa < -EPS && ab > EPS)) &&
      ((ba > EPS && bb < -EPS) || (ba < -EPS && bb > EPS))) return true;
  const on = (p, q, r, area) => Math.abs(area) <= EPS &&
    r.x >= Math.min(p.x, q.x) - EPS && r.x <= Math.max(p.x, q.x) + EPS &&
    r.z >= Math.min(p.z, q.z) - EPS && r.z <= Math.max(p.z, q.z) + EPS;
  return on(a, b, c, aa) || on(a, b, d, ab) || on(c, d, a, ba) || on(c, d, b, bb);
}

function segmentDistance2(a, b, c, d) {
  if (segmentsIntersect(a, b, c, d)) return 0;
  return Math.min(pointSegmentDistance2(a, c, d), pointSegmentDistance2(b, c, d),
    pointSegmentDistance2(c, a, b), pointSegmentDistance2(d, a, b));
}

function local(p, collider) {
  const x = p.x - collider.x, z = p.z - collider.z;
  // Match Three.js rotation.y: positive Y rotates local +X toward world -Z.
  return { x: x * collider.cos - z * collider.sin, z: x * collider.sin + z * collider.cos };
}

function pointRectDistance2(p, c) {
  return Math.max(0, Math.abs(p.x) - c.halfWidth) ** 2 +
    Math.max(0, Math.abs(p.z) - c.halfDepth) ** 2;
}

function segmentRectDistance2(a, b, c) {
  const aa = local(a, c), bb = local(b, c);
  let closest = Math.min(pointRectDistance2(aa, c), pointRectDistance2(bb, c));
  if (closest === 0) return 0;
  const corners = [
    { x: -c.halfWidth, z: -c.halfDepth }, { x: c.halfWidth, z: -c.halfDepth },
    { x: c.halfWidth, z: c.halfDepth }, { x: -c.halfWidth, z: c.halfDepth },
  ];
  for (let i = 0; i < 4; i++) {
    closest = Math.min(closest, segmentDistance2(aa, bb, corners[i], corners[(i + 1) % 4]));
  }
  return closest;
}

function normalizeCollider(value, index) {
  if (!value || value.solid === false) return null;
  const x = value.x ?? value.center?.x, z = value.z ?? value.center?.z;
  const id = String(value.id ?? `collider-${index}`);
  if (finite(value.radius) && value.radius >= 0 && finite(x) && finite(z)) {
    return { id, type: 'circle', x, z, radius: value.radius };
  }
  const minX = value.minX ?? value.min?.x, maxX = value.maxX ?? value.max?.x;
  const minZ = value.minZ ?? value.min?.z, maxZ = value.maxZ ?? value.max?.z;
  const aabb = [minX, maxX, minZ, maxZ].every(finite);
  const width = aabb ? maxX - minX : value.width;
  const depth = aabb ? maxZ - minZ : value.depth;
  const angle = value.rotation ?? value.rotationY ?? 0;
  if (!finite(width) || width <= 0 || !finite(depth) || depth <= 0 || !finite(angle) ||
      (!aabb && (!finite(x) || !finite(z)))) throw new TypeError(`Invalid navigation collider: ${id}`);
  return { id, type: 'rect', x: aabb ? (minX + maxX) / 2 : x,
    z: aabb ? (minZ + maxZ) / 2 : z, halfWidth: width / 2, halfDepth: depth / 2,
    cos: Math.cos(angle), sin: Math.sin(angle) };
}

class MinHeap {
  constructor() { this.items = []; }
  less(a, b) { return a.f < b.f || (a.f === b.f && (a.h < b.h || (a.h === b.h && a.id < b.id))); }
  push(value) {
    const values = this.items; values.push(value); let i = values.length - 1;
    while (i > 0) {
      const parent = (i - 1) >> 1;
      if (!this.less(values[i], values[parent])) break;
      [values[i], values[parent]] = [values[parent], values[i]]; i = parent;
    }
  }
  pop() {
    const values = this.items, first = values[0], last = values.pop();
    if (values.length) {
      values[0] = last; let i = 0;
      for (;;) {
        let next = i, left = i * 2 + 1, right = left + 1;
        if (left < values.length && this.less(values[left], values[next])) next = left;
        if (right < values.length && this.less(values[right], values[next])) next = right;
        if (next === i) break;
        [values[i], values[next]] = [values[next], values[i]]; i = next;
      }
    }
    return first;
  }
  get length() { return this.items.length; }
}

/** Capsule-inflated grid A*, continuous swept collision checks, and local yielding. */
export class NavigationMesh {
  constructor({ bounds, colliders = [], cellSize = 0.32, radius = 0.46 } = {}) {
    const minX = bounds?.minX ?? bounds?.min?.x, maxX = bounds?.maxX ?? bounds?.max?.x;
    const minZ = bounds?.minZ ?? bounds?.min?.z, maxZ = bounds?.maxZ ?? bounds?.max?.z;
    if (![minX, maxX, minZ, maxZ, cellSize, radius].every(finite) ||
        cellSize <= 0 || radius <= 0 || maxX - minX <= radius * 2 || maxZ - minZ <= radius * 2) {
      throw new TypeError('Invalid navigation bounds, cell size, or capsule radius');
    }
    this.bounds = Object.freeze({ minX, maxX, minZ, maxZ });
    this.cellSize = cellSize; this.radius = radius;
    this.colliders = colliders.map(normalizeCollider).filter(Boolean);
    this.origin = { x: minX + radius + EPS * 2, z: minZ + radius + EPS * 2 };
    this.columns = Math.floor((maxX - minX - 2 * radius - EPS * 4) / cellSize) + 1;
    this.rows = Math.floor((maxZ - minZ - 2 * radius - EPS * 4) / cellSize) + 1;
    if (this.columns * this.rows > 250000) throw new RangeError('Navigation grid is too large');
    this.grids = new Map(); this.reservations = new Map(); this.yields = new Map(); this.waits = new Map(); this.frame = 0;
    this.lastSearch = { reason: 'NOT_SEARCHED', expanded: 0 };
    this.lastMotion = { reason: 'NOT_MOVED', blocked: false };
  }

  _radius(opts) { return finite(opts.radius) && opts.radius > 0 ? opts.radius : this.radius; }
  _ignored(opts) { return opts._ignoredSet || new Set((opts.ignoreIds || []).map(String)); }
  _actor(actor) {
    if (!point(actor)) return null;
    return { id: String(actor.id ?? ''), x: actor.x, z: actor.z,
      radius: finite(actor.radius) && actor.radius > 0 ? actor.radius : this.radius,
      priority: finite(actor.priority) ? actor.priority : 0 };
  }
  _actors(opts) {
    return (opts.actors || []).map(actor => this._actor(actor)).filter(actor => actor &&
      (opts.actorId === undefined || actor.id !== String(opts.actorId)));
  }
  _inside(p, radius) {
    const b = this.bounds;
    return point(p) && p.x >= b.minX + radius + EPS && p.x <= b.maxX - radius - EPS &&
      p.z >= b.minZ + radius + EPS && p.z <= b.maxZ - radius - EPS;
  }

  isWalkable(p, opts = {}) {
    const radius = this._radius(opts);
    if (!this._inside(p, radius)) return false;
    const ignored = this._ignored(opts);
    for (const c of this.colliders) {
      if (ignored.has(c.id)) continue;
      const ex = c.type === 'circle' ? c.radius : c.halfWidth * Math.abs(c.cos) + c.halfDepth * Math.abs(c.sin);
      const ez = c.type === 'circle' ? c.radius : c.halfWidth * Math.abs(c.sin) + c.halfDepth * Math.abs(c.cos);
      if (Math.abs(p.x - c.x) > ex + radius + EPS || Math.abs(p.z - c.z) > ez + radius + EPS) continue;
      if (c.type === 'circle') {
        if (distance2(p, c) <= (radius + c.radius + EPS) ** 2) return false;
      } else if (pointRectDistance2(local(p, c), c) <= (radius + EPS) ** 2) return false;
    }
    return !this._actors(opts).some(actor => distance2(p, actor) <= (radius + actor.radius + DEFAULT_GAP) ** 2);
  }

  segmentClear(a, b, opts = {}) {
    const radius = this._radius(opts);
    if (!this._inside(a, radius) || !this._inside(b, radius)) return false;
    const ignored = this._ignored(opts);
    const minX = Math.min(a.x, b.x) - radius - EPS, maxX = Math.max(a.x, b.x) + radius + EPS;
    const minZ = Math.min(a.z, b.z) - radius - EPS, maxZ = Math.max(a.z, b.z) + radius + EPS;
    for (const c of this.colliders) {
      if (ignored.has(c.id)) continue;
      const ex = c.type === 'circle' ? c.radius : c.halfWidth * Math.abs(c.cos) + c.halfDepth * Math.abs(c.sin);
      const ez = c.type === 'circle' ? c.radius : c.halfWidth * Math.abs(c.sin) + c.halfDepth * Math.abs(c.cos);
      if (maxX < c.x - ex || minX > c.x + ex || maxZ < c.z - ez || minZ > c.z + ez) continue;
      if (c.type === 'circle') {
        if (pointSegmentDistance2(c, a, b) <= (radius + c.radius + EPS) ** 2) return false;
      } else if (segmentRectDistance2(a, b, c) <= (radius + EPS) ** 2) return false;
    }
    return !this._actors(opts).some(actor => pointSegmentDistance2(actor, a, b) <=
      (radius + actor.radius + DEFAULT_GAP) ** 2);
  }

  _gridPoint(id) { return { x: this.origin.x + (id % this.columns) * this.cellSize,
    z: this.origin.z + Math.floor(id / this.columns) * this.cellSize }; }

  _grid(opts) {
    const key = JSON.stringify([this._radius(opts), [...this._ignored(opts)].sort()]);
    let grid = this.grids.get(key);
    if (!grid) {
      grid = new Uint8Array(this.columns * this.rows);
      const staticOpts = { ...opts, actors: [], _ignoredSet: this._ignored(opts) };
      for (let id = 0; id < grid.length; id++) grid[id] = Number(this.isWalkable(this._gridPoint(id), staticOpts));
      if (this.grids.size >= 8) this.grids.delete(this.grids.keys().next().value);
      this.grids.set(key, grid);
    }
    const actors = this._actors(opts);
    if (!actors.length) return grid;
    const occupied = grid.slice(), radius = this._radius(opts);
    for (let id = 0; id < occupied.length; id++) {
      if (occupied[id] && actors.some(actor => distance2(this._gridPoint(id), actor) <=
          (radius + actor.radius + DEFAULT_GAP) ** 2)) occupied[id] = 0;
    }
    return occupied;
  }

  _connectors(p, grid, opts) {
    const candidates = [];
    const ix = Math.round((p.x - this.origin.x) / this.cellSize);
    const iz = Math.round((p.z - this.origin.z) / this.cellSize);
    for (let dz = -4; dz <= 4; dz++) for (let dx = -4; dx <= 4; dx++) {
      const x = ix + dx, z = iz + dz;
      if (x < 0 || x >= this.columns || z < 0 || z >= this.rows) continue;
      const id = z * this.columns + x, position = this._gridPoint(id);
      if (grid[id]) candidates.push({ id, position, cost: distance(p, position) });
    }
    candidates.sort((a, b) => a.cost - b.cost || a.id - b.id);
    return candidates.filter(candidate => this.segmentClear(p, candidate.position, opts)).slice(0, 12);
  }

  findPath(start, goal, opts = {}) {
    opts = { ...opts, _ignoredSet: this._ignored(opts) };
    this.lastSearch = { reason: 'SEARCHING', expanded: 0 };
    if (!this.isWalkable(start, opts) || !this.isWalkable(goal, opts)) {
      this.lastSearch.reason = 'INVALID_ENDPOINT'; return [];
    }
    if (distance2(start, goal) <= EPS * EPS) {
      this.lastSearch.reason = 'ARRIVED'; return [copyPoint(goal)];
    }
    if (this.segmentClear(start, goal, opts)) {
      this.lastSearch.reason = 'DIRECT'; return [copyPoint(start), copyPoint(goal)];
    }
    const grid = this._grid(opts), starts = this._connectors(start, grid, opts);
    const goals = new Map(this._connectors(goal, grid, opts).map(value => [value.id, value.cost]));
    if (!starts.length || !goals.size) { this.lastSearch.reason = 'NO_GRID_CONNECTION'; return []; }
    const costs = new Float64Array(grid.length); costs.fill(Infinity);
    const parent = new Int32Array(grid.length); parent.fill(-2);
    const closed = new Uint8Array(grid.length), heap = new MinHeap();
    for (const candidate of starts) {
      costs[candidate.id] = candidate.cost; parent[candidate.id] = -1;
      const h = distance(candidate.position, goal);
      heap.push({ id: candidate.id, g: candidate.cost, h, f: candidate.cost + h });
    }
    const neighbors = [[0, -1], [-1, 0], [1, 0], [0, 1], [-1, -1], [1, -1], [-1, 1], [1, 1]];
    let end = -1, bestCost = Infinity;
    while (heap.length) {
      const current = heap.pop();
      if (current.f >= bestCost - EPS) break;
      if (closed[current.id] || current.g > costs[current.id] + EPS) continue;
      closed[current.id] = 1; this.lastSearch.expanded++;
      if (goals.has(current.id)) {
        const total = current.g + goals.get(current.id);
        if (total < bestCost) { end = current.id; bestCost = total; }
        continue;
      }
      const x = current.id % this.columns, z = Math.floor(current.id / this.columns);
      const a = this._gridPoint(current.id);
      for (const [dx, dz] of neighbors) {
        const nx = x + dx, nz = z + dz;
        if (nx < 0 || nx >= this.columns || nz < 0 || nz >= this.rows) continue;
        const id = nz * this.columns + nx;
        if (!grid[id] || closed[id]) continue;
        if (dx && dz && (!grid[z * this.columns + nx] || !grid[nz * this.columns + x])) continue;
        const b = this._gridPoint(id);
        if (!this.segmentClear(a, b, opts)) continue;
        const g = current.g + this.cellSize * (dx && dz ? Math.SQRT2 : 1);
        if (g >= costs[id] - EPS) continue;
        costs[id] = g; parent[id] = current.id;
        const h = distance(b, goal); heap.push({ id, g, h, f: g + h });
      }
    }
    if (end < 0) { this.lastSearch.reason = 'NO_PATH'; return []; }
    const path = [copyPoint(goal)];
    for (let id = end; id >= 0; id = parent[id]) path.push(this._gridPoint(id));
    path.push(copyPoint(start)); path.reverse();
    const compressed = [path[0]];
    for (let i = 0; i < path.length - 1;) {
      let next = path.length - 1;
      while (next > i + 1 && !this.segmentClear(path[i], path[next], opts)) next--;
      compressed.push(path[next]); i = next;
    }
    this.lastSearch.reason = 'FOUND'; this.lastSearch.length = this.inspectPath(compressed, opts).length;
    return this.validatePath(compressed, opts) ? compressed : [];
  }

  inspectPath(path, opts = {}) {
    const invalidPoints = [], blockedSegments = []; let length = 0;
    if (!Array.isArray(path) || !path.length) return { valid: false, invalidPoints, blockedSegments, length };
    path.forEach((p, i) => { if (!this.isWalkable(p, opts)) invalidPoints.push(i); });
    for (let i = 1; i < path.length; i++) {
      if (!this.segmentClear(path[i - 1], path[i], opts)) blockedSegments.push(i - 1);
      if (point(path[i - 1]) && point(path[i])) length += distance(path[i - 1], path[i]);
    }
    return { valid: !invalidPoints.length && !blockedSegments.length, invalidPoints, blockedSegments, length };
  }
  validatePath(path, opts = {}) { return this.inspectPath(path, opts).valid; }
  getWalkablePoints(opts = {}) {
    const grid = this._grid(opts), points = [];
    for (let id = 0; id < grid.length; id++) if (grid[id]) points.push(this._gridPoint(id));
    return points;
  }
  getDiagnostics() {
    return { bounds: { ...this.bounds }, cellSize: this.cellSize, radius: this.radius,
      columns: this.columns, rows: this.rows, colliderCount: this.colliders.length,
      reservationCount: this.reservations.size, frame: this.frame,
      lastSearch: { ...this.lastSearch }, lastMotion: { ...this.lastMotion } };
  }
  get diagnostics() { return this.getDiagnostics(); }

  /** Call once per animation frame before moving actors, in a stable ID order. */
  beginFrame() { this.frame++; this.reservations.clear(); }

  _dynamicBlockers(a, b, actor, others) {
    const blockers = [];
    for (const other of others) {
      const minimum = actor.radius + other.radius, padded = minimum + DEFAULT_GAP;
      const nearest = pointSegmentDistance2(other, a, b), initial = distance2(a, other);
      // An actor just inside the comfort gap may move away, but never closer or overlap.
      const separating = initial >= (minimum + EPS) ** 2 && nearest >= initial - EPS &&
        distance2(b, other) > initial + EPS;
      if (nearest <= (padded + EPS) ** 2 && !separating) { blockers.push(other); continue; }
      const reserved = this.reservations.get(other.id);
      if (reserved && !(separating && distance2(reserved.from, reserved.to) <= EPS * EPS) &&
          distance2(other, reserved.to) < EPS * EPS &&
          segmentDistance2(a, b, reserved.from, reserved.to) <= (padded + EPS) ** 2) blockers.push(other);
    }
    return blockers;
  }

  move(inputActor, desired, otherActors = [], opts = {}) {
    const actor = this._actor(inputActor);
    if (!actor) throw new TypeError('Movement requires finite actor x/z coordinates');
    if (!point(desired)) return { x: actor.x, z: actor.z, blocked: true, reason: 'INVALID_MOVEMENT' };
    const start = copyPoint(actor), id = actor.id;
    if (this.reservations.has(id)) this.beginFrame(); // Safe fallback for sequential callers.
    const others = otherActors.map(other => this._actor(other)).filter(other => other && (!id || other.id !== id))
      .sort((a, b) => a.priority - b.priority || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    const staticOpts = { ...opts, radius: actor.radius, actors: [] };
    const save = (position, blocked, reason, blocker = null) => {
      this.reservations.set(id, { from: start, to: copyPoint(position), radius: actor.radius });
      this.lastMotion = { actorId: id, blocked, reason, blockerId: blocker?.id ?? null };
      return { ...copyPoint(position), blocked, reason };
    };
    if (!this.isWalkable(start, staticOpts)) return save(start, true, 'INVALID_START');
    const dx = desired.x - actor.x, dz = desired.z - actor.z, step = Math.hypot(dx, dz);
    if (step <= EPS) return save(start, false, 'ARRIVED');
    const staticClear = this.segmentClear(start, desired, staticOpts);
    const blockers = this._dynamicBlockers(start, desired, actor, others);
    if (staticClear && !blockers.length) {
      this.yields.delete(id); this.waits.delete(id); return save(desired, false, 'MOVED');
    }
    if (!staticClear && !blockers.length) return save(start, true, 'STATIC_BLOCKED');
    const blocker = blockers[0];
    const lowerPriority = actor.priority > blocker.priority || (actor.priority === blocker.priority && id > blocker.id);
    const previousWait = this.waits.get(id);
    const wait = previousWait?.blockerId === blocker.id ? previousWait.count + 1 : 1;
    this.waits.set(id, { blockerId: blocker.id, count: wait });
    // The winner briefly holds its lane while the other yields. After twelve
    // blocked updates it also detours: an idle/seated actor may never move.
    if (!lowerPriority && wait <= 12) return save(start, true, 'YIELD_WAIT', blocker);
    let yielding = this.yields.get(id);
    if (!yielding || yielding.blockerId !== blocker.id) {
      // Both directions are tried, but retain the successful side across frames.
      const pair = [blocker.id, id].sort().join(':');
      let hash = 0; for (const char of pair) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
      yielding = { blockerId: blocker.id, side: hash % 2 ? 1 : -1 }; this.yields.set(id, yielding);
    }
    const ux = dx / step, uz = dz / step;
    for (const side of [yielding.side, -yielding.side]) {
      for (const angle of [Math.PI / 2, Math.PI / 3, Math.PI / 4]) {
        const cos = Math.cos(angle), sin = Math.sin(angle) * side;
        const candidate = { x: start.x + (ux * cos - uz * sin) * step,
          z: start.z + (ux * sin + uz * cos) * step };
        if (this.segmentClear(start, candidate, staticOpts) &&
            !this._dynamicBlockers(start, candidate, actor, others).length) {
          yielding.side = side; return save(candidate, false, 'YIELD_LATERAL', blocker);
        }
      }
    }
    return save(start, true, 'AGENT_BLOCKED', blocker);
  }
}

export default NavigationMesh;
