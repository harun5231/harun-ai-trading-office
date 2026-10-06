// Visual locomotion only. Activity comes exclusively from the read-only status bridge.
const smooth = x => { x = Math.max(0, Math.min(1, x)); return x * x * (3 - 2 * x); };
const distance = (a, b) => Math.hypot(a.x - b.x, a.z - b.z);
const point = value => ({ x: value.x, z: value.z });

export class OfficeAnimationController {
  constructor({ actors, navigation, loungeSlots, socialSlots, status, reducedMotion = false }) {
    this.actors = actors;
    this.nav = navigation;
    this.lounge = loungeSlots;
    this.social = socialSlots;
    this.status = status;
    this.reducedMotion = reducedMotion;
    this.time = 0;
    this.retryAt = 0;
    this.assignments = new Map();
    this.pathsRejected = 0;
    this.blockedFrames = 0;
    for (const [index, actor] of actors.entries()) {
      Object.assign(actor, {
        priority: index, mode: 'AT_DESK', sit: 1, phase: 0,
        route: [], routeIndex: 0, speed: 0, destination: null,
        currentSeat: { ...actor.station.dock, yaw: actor.station.yaw,
          entry: actor.station.entry, seatHeight: actor.station.seat.y,
          colliderId: `chair-${actor.role}` },
      });
      actor.avatar.root.position.set(actor.station.dock.x, 0, actor.station.dock.z);
      actor.avatar.root.rotation.y = actor.station.yaw;
    }
  }

  refreshAssignments() {
    const idle = this.actors.filter(actor => {
      const role = this.status.getRole(actor.role);
      return role.known && !role.active;
    });
    const next = new Map();
    // Two small conversation pairs leave the central aisle and the lounge available.
    const socialCount = idle.length >= 6 ? 4 : idle.length >= 2 ? 2 : 0;
    for (let index = 0; index < socialCount && index < this.social.length; index++) {
      const slot = this.social[index];
      const partner = this.social[index % 2 === 0 ? index + 1 : index - 1];
      const position = slot.position || slot;
      const other = partner?.position || partner || position;
      next.set(idle[index].id, {
        id: slot.id, kind: 'SOCIAL', position: point(position), entry: point(position),
        yaw: Math.atan2(other.x - position.x, other.z - position.z),
        colliderId: null,
      });
    }
    const rest = idle.slice(socialCount);
    const reserved = new Set();
    // Keep an existing lounge reservation when another division changes state.
    for (const actor of rest) {
      const old = this.assignments.get(actor.id);
      if (old?.kind === 'LOUNGE' && this.lounge.some(slot => slot.id === old.id)) {
        next.set(actor.id, old); reserved.add(old.id);
      }
    }
    for (const actor of rest) {
      if (next.has(actor.id)) continue;
      const slot = this.lounge.find(candidate => !reserved.has(candidate.id));
      if (!slot) continue; // Remain safely at the assigned cubicle when no seat is free.
      next.set(actor.id, { ...slot, kind: 'LOUNGE' }); reserved.add(slot.id);
    }
    this.assignments = next;
  }

  desired(actor) {
    const role = this.status.getRole(actor.role);
    return role.active || !role.known || !this.assignments.has(actor.id)
      ? { id: `desk-${actor.role}`, kind: 'DESK', position: actor.station.dock,
        entry: actor.station.entry, yaw: actor.station.yaw,
        seatHeight: actor.station.seat.y, colliderId: `chair-${actor.role}` }
      : this.assignments.get(actor.id);
  }

  plan(actor, target, ignoreIds = []) {
    const start = actor.avatar.root.position;
    const path = this.nav.findPath(start, target, { ignoreIds });
    if (!path.length) {
      actor.route = []; actor.routeIndex = 0; actor.speed = 0;
      actor.mode = 'WAITING_PATH'; actor.retryTime = this.time + 1.2;
      this.pathsRejected++;
      return false;
    }
    actor.route = path; actor.routeIndex = Math.min(1, path.length - 1);
    actor.routeIgnore = ignoreIds;
    actor.mode = 'WALKING';
    return true;
  }

  beginDeparture(actor, destination) {
    actor.destination = destination;
    if (actor.currentSeat && actor.sit > .01) {
      actor.mode = 'STANDING_UP'; actor.phase = 0; actor.startSit = actor.sit;
    } else {
      actor.currentSeat = null;
      this.plan(actor, destination.entry);
    }
  }

  others(actor) {
    return this.actors.filter(other => other !== actor).flatMap(other => {
      const body = { id: other.id, x: other.avatar.root.position.x,
        z: other.avatar.root.position.z, radius: this.nav.radius, priority: other.priority };
      // A seated chibi leans beyond its floor anchor. Reserve its actual head
      // footprint too, so a passing colleague cannot intersect the large head.
      const head = other.avatar.headAnchor?.parent?.matrixWorld?.elements;
      return head && Number.isFinite(head[12]) && Number.isFinite(head[14])
        ? [body, { id: `${other.id}:head`, x: head[12], z: head[14], radius: .39,
          priority: other.priority }]
        : [body];
    });
  }

  turn(actor, target, dt) {
    const root = actor.avatar.root;
    const angle = Math.atan2(Math.sin(target - root.rotation.y), Math.cos(target - root.rotation.y));
    root.rotation.y += angle * (1 - Math.exp(-dt * 8));
  }

  follow(actor, dt) {
    const root = actor.avatar.root;
    const waypoint = actor.route[actor.routeIndex];
    if (!waypoint) { actor.speed = 0; return true; }
    const dx = waypoint.x - root.position.x, dz = waypoint.z - root.position.z;
    const remaining = Math.hypot(dx, dz);
    if (remaining < .012) {
      actor.routeIndex++;
      actor.speed = 0;
      return actor.routeIndex >= actor.route.length;
    }
    const seat = actor.currentSeat;
    const seatPosition = seat?.position ?? seat;
    const contact = seat ? 1 - smooth((distance(root.position, seatPosition) - .65) / .30) : 0;
    const step = Math.min(remaining, dt * (this.reducedMotion ? .72 : .98) * (1 - .85 * contact));
    const from = point(root.position);
    const desired = { x: from.x + dx / remaining * step, z: from.z + dz / remaining * step };
    const move = this.nav.move({ id: actor.id, ...from, priority: actor.priority,
      radius: this.nav.radius }, desired, this.others(actor), { ignoreIds: actor.routeIgnore || [] });
    root.position.set(move.x, 0, move.z);
    const travelled = distance(from, move);
    actor.speed = dt > 0 ? travelled / dt : 0;
    if (travelled > .0001) this.turn(actor, Math.atan2(move.x - from.x, move.z - from.z), dt);
    if (move.blocked) { this.blockedFrames++; actor.waiting += dt; }
    else actor.waiting = 0;
    // Replan the remaining corridor when a neighbour has occupied it for a while.
    if (actor.waiting > 1.8) {
      const goal = actor.route[actor.route.length - 1];
      const path = this.nav.findPath(root.position, goal, {
        ignoreIds: actor.routeIgnore || [], actors: this.others(actor), actorId: actor.id,
      });
      if (path.length) { actor.route = path; actor.routeIndex = Math.min(1, path.length - 1); }
      actor.waiting = 0;
    }
    return false;
  }

  arrive(actor) {
    const goal = actor.destination;
    actor.speed = 0;
    if (goal.kind === 'SOCIAL') {
      actor.currentSeat = null; actor.mode = 'SOCIAL'; actor.sit = 0; return;
    }
    const root = actor.avatar.root;
    if (!this.nav.segmentClear(root.position, goal.position, { ignoreIds: [goal.colliderId] })) {
      actor.mode = 'WAITING_PATH'; actor.retryTime = this.time + 1.2; return;
    }
    actor.currentSeat = goal;
    actor.mode = 'DOCKING';
    actor.route = [point(root.position), point(goal.position)]; actor.routeIndex = 1;
    actor.routeIgnore = [goal.colliderId];
  }

  update(dt, elapsed) {
    this.time = elapsed;
    if (elapsed >= this.retryAt) { this.refreshAssignments(); this.retryAt = elapsed + .5; }
    this.nav.beginFrame();
    for (const actor of this.actors) {
      const goal = this.desired(actor);
      actor.waiting ||= 0;
      const settled = ['AT_DESK', 'RESTING', 'SOCIAL'].includes(actor.mode);
      if (settled && actor.destination?.id !== goal.id &&
          !(actor.mode === 'AT_DESK' && goal.kind === 'DESK')) this.beginDeparture(actor, goal);
      else if (actor.mode === 'AT_DESK' && goal.kind === 'DESK') actor.destination = goal;
      if (actor.mode === 'WALKING' && actor.destination?.id !== goal.id && !actor.exiting) {
        actor.destination = goal; this.plan(actor, goal.entry);
      }
      if (actor.mode === 'WAITING_PATH' && actor.destination?.id !== goal.id) {
        actor.destination = goal; actor.retryTime = elapsed;
      }
      if (actor.mode === 'STANDING_UP') {
        actor.phase += dt / 1.0;
        actor.sit = (actor.startSit ?? 1) * (1 - smooth(actor.phase));
        if (actor.phase >= 1) {
          const seat = actor.currentSeat;
          actor.sit = 0; actor.exiting = true;
          this.plan(actor, seat.entry, [seat.colliderId]);
        }
      } else if (actor.mode === 'WALKING') {
        if (this.follow(actor, dt)) {
          if (actor.exiting) {
            actor.exiting = false; actor.currentSeat = null;
            actor.destination = goal; this.plan(actor, goal.entry);
          } else this.arrive(actor);
        }
      } else if (actor.mode === 'DOCKING') {
        this.turn(actor, actor.currentSeat.yaw, dt);
        if (this.follow(actor, dt)) { actor.mode = 'SITTING_DOWN'; actor.phase = 0; }
      } else if (actor.mode === 'SITTING_DOWN') {
        actor.phase += dt / 1.2; actor.sit = smooth(actor.phase);
        this.turn(actor, actor.currentSeat.yaw, dt);
        if (actor.phase >= 1) actor.mode = actor.destination.kind === 'DESK' ? 'AT_DESK' : 'RESTING';
      } else if (actor.mode === 'WAITING_PATH' && elapsed >= (actor.retryTime || 0)) {
        if (actor.exiting && actor.currentSeat) this.plan(actor, actor.currentSeat.entry, [actor.currentSeat.colliderId]);
        else this.plan(actor, actor.destination?.entry || goal.entry);
      } else if (actor.mode === 'SOCIAL') this.turn(actor, goal.yaw, dt);
      const liveRole = this.status.getRole(actor.role);
      const state = ['WALKING', 'DOCKING'].includes(actor.mode) ? 'WALKING'
        : actor.mode === 'STANDING_UP' ? 'STAND_UP'
        : actor.mode === 'SITTING_DOWN' ? 'SIT_DOWN'
        : actor.mode === 'SOCIAL' ? 'SOCIAL'
        : actor.mode === 'AT_DESK' && liveRole.active ? 'WORKING' : 'IDLE';
      actor.avatar.pose(dt, elapsed, {
        state, sit: actor.sit, seatHeight: actor.currentSeat?.seatHeight ?? .78,
        seatContact: Boolean(actor.currentSeat), seatYaw: actor.currentSeat?.yaw,
        seatPosition: actor.currentSeat ? point(actor.currentSeat.position ?? actor.currentSeat) : null,
        walkSpeed: actor.speed, transitionProgress: actor.phase,
        reducedMotion: this.reducedMotion,
      });
    }
  }

  diagnostics() {
    return { pathsRejected: this.pathsRejected, blockedFrames: this.blockedFrames,
      states: this.actors.map(actor => ({ id: actor.id, name: actor.station.name,
        state: actor.mode, active: this.status.getRole(actor.role).active,
        known: this.status.getRole(actor.role).known, sit: actor.sit,
        x: actor.avatar.root.position.x, z: actor.avatar.root.position.z,
        destination: actor.destination?.id ?? null,
        pathRemaining: Math.max(0, actor.route.length - actor.routeIndex) })) };
  }
}
