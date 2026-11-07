// Visual locomotion only. Activity comes exclusively from the read-only status bridge.
const smooth = x => { x = Math.max(0, Math.min(1, x)); return x * x * (3 - 2 * x); };
const inverseSmooth = value => .5 - Math.sin(Math.asin(1 - 2 * Math.max(0, Math.min(1, value))) / 3);
const distance = (a, b) => Math.hypot(a.x - b.x, a.z - b.z);
const point = value => ({ x: value.x, z: value.z });
const seatIgnores = seat => seat?.ignoreIds || (seat?.colliderId ? [seat.colliderId] : []);

export class OfficeAnimationController {
  constructor({ actors, navigation, loungeSlots = [], socialSlots = [], idleSlots = [],
    status, reducedMotion = false, random = Math.random }) {
    this.actors = actors;
    this.nav = navigation;
    this.lounge = loungeSlots;
    this.social = socialSlots;
    this.idleSlots = idleSlots;
    this.status = status;
    this.reducedMotion = reducedMotion;
    this.random = random;
    this.time = 0;
    this.retryAt = 0;
    this.assignments = new Map();
    this.pathsRejected = 0;
    this.blockedFrames = 0; this.progressReplans = 0;
    for (const [index, actor] of actors.entries()) {
      Object.assign(actor, {
        priority: index, basePriority: index, mode: 'AT_DESK', sit: 1, phase: 0,
        route: [], routeIndex: 0, speed: 0, motionSpeed: 0, destination: null,
        handsReady: false, trayOffset: .42,
        recentActivities: [], idleUntil: 2 + index * 1.9 + this.random() * 7,
        currentSeat: { id: `desk-${actor.role}`, ...actor.station.dock, yaw: actor.station.yaw,
          entry: actor.station.entry, approach: actor.station.approach, ignoreIds: actor.station.ignoreIds, seatHeight: actor.station.seat.y,
          colliderId: `chair-${actor.role}` },
      });
      actor.avatar.root.position.set(actor.station.dock.x, 0, actor.station.dock.z);
      actor.avatar.root.rotation.y = actor.station.yaw;
    }
  }

  desk(actor) {
    return { id: `desk-${actor.role}`, kind: 'DESK', position: actor.station.dock,
      entry: actor.station.entry, approach: actor.station.approach, ignoreIds: actor.station.ignoreIds, yaw: actor.station.yaw,
      seatHeight: actor.station.seat.y, colliderId: `chair-${actor.role}` };
  }

  choose(values) {
    return values[Math.min(values.length - 1, Math.floor(this.random() * values.length))];
  }

  idleGoal(slot, kind = slot.kind) {
    const position = slot.position || slot;
    return { ...slot, kind, position: point(position), entry: point(slot.entry || position),
      yaw: slot.yaw ?? 0, colliderId: slot.colliderId ?? null };
  }

  assign(actor, goal) {
    this.assignments.set(actor.id, goal);
    actor.idleUntil = Infinity; // The dwell timer begins only after the walk has finished.
    actor.recentActivities.push(goal.kind);
    actor.recentActivities = actor.recentActivities.slice(-2);
  }

  settle(actor) {
    actor.idleUntil = this.time + (actor.role === 'boss' && actor.destination?.kind === 'DESK' ? 90 : 18) + this.random() * 32;
  }

  refreshAssignments() {
    const idle = this.actors.filter(actor => {
      const role = this.status.getRole(actor.role);
      return actor.avatar.available !== false && role.known && !role.active;
    });
    const eligible = new Set(idle.map(actor => actor.id));
    for (const [id, goal] of this.assignments) {
      const partner = goal.partnerId && this.actors.find(actor => actor.id === goal.partnerId);
      const host = goal.hostId && this.actors.find(actor => actor.id === goal.hostId);
      if (!eligible.has(id) || (partner && (!eligible.has(partner.id) ||
          this.assignments.get(partner.id)?.partnerId !== id)) ||
          (host && (!eligible.has(host.id) || host.mode !== 'AT_DESK'))) {
        this.assignments.delete(id);
        const actor = this.actors.find(candidate => candidate.id === id);
        if (actor) actor.idleUntil = Math.min(actor.idleUntil, this.time);
      }
    }
    const due = idle.filter(actor => this.time >= actor.idleUntil);
    // Shuffle each decision round. No global fixed sequence or synchronized idle loop.
    for (let i = due.length - 1; i > 0; i--) {
      const j = Math.min(i, Math.floor(this.random() * (i + 1)));
      [due[i], due[j]] = [due[j], due[i]];
    }
    for (const actor of due) {
      if (this.time < actor.idleUntil) continue;
      const previous = this.assignments.get(actor.id);
      this.assignments.delete(actor.id);
      const reserved = new Set([...this.assignments.values()].map(goal => goal.id));
      // Ownership persists while the body is still occupying or approaching a seat.
      for (const other of this.actors) if (other !== actor) {
        if (other.currentSeat?.id) reserved.add(other.currentSeat.id);
        if (other.destination?.id) reserved.add(other.destination.id);
      }
      const slots = [...this.idleSlots.filter(slot => slot.kind !== 'SOCIAL').map(slot => this.idleGoal(slot)),
        ...this.lounge.map(slot => this.idleGoal(slot, 'LOUNGE'))]
        .filter(slot => !reserved.has(slot.id) && slot.id !== previous?.id &&
          (!slot.gender || !actor.station.gender || slot.gender === actor.station.gender));
      const host = idle.find(candidate => candidate.role === 'boss' && candidate !== actor &&
        candidate.mode === 'AT_DESK' && (!this.assignments.has(candidate.id) ||
          this.assignments.get(candidate.id).kind === 'DESK'));
      const candidates = slots.filter(slot => slot.kind !== 'VISIT' || host);
      const partner = idle.find(candidate => candidate !== actor && candidate.role !== 'boss' &&
        (this.time >= candidate.idleUntil || (candidate.mode === 'AT_DESK' &&
          (!this.assignments.has(candidate.id) || this.assignments.get(candidate.id).kind === 'DESK'))));
      const socialSlots = [...new Map([...this.idleSlots.filter(slot => slot.kind === 'SOCIAL'),
        ...this.social].map(slot => [slot.id, slot])).values()];
      const pairs = [];
      if (partner) for (let i = 0; i + 1 < socialSlots.length; i += 2) {
        const pair = socialSlots.slice(i, i + 2);
        if (pair.every(slot => !reserved.has(slot.id))) pairs.push(pair);
      }
      let categories = [...new Set(candidates.map(slot => slot.kind))];
      // Natural quiet periods, especially for Harun's monitoring role.
      if (actor.role === 'boss' || actor.recentActivities.length) categories.push('DESK');
      if (pairs.length) categories.push('SOCIAL');
      if (actor.role !== 'boss' && actor.recentActivities.at(-1) === 'DESK' && categories.length > 1)
        categories = categories.filter(kind => kind !== 'DESK');
      if (!categories.length) {
        this.assignments.set(actor.id, this.desk(actor));
        actor.idleUntil = this.time + 5 + this.random() * 6;
        continue;
      }
      // Never force a fixed round-robin: avoid the immediate repeat, then weight
      // less-recent choices rather than always selecting a single "next" activity.
      const lastLeisure = actor.recentActivities.findLast(kind => kind !== 'DESK');
      if (categories.some(kind => kind !== lastLeisure && kind !== 'DESK'))
        categories = categories.filter(kind => kind === 'DESK' || kind !== lastLeisure);
      const weighted = categories.flatMap(kind => kind === 'DESK' && actor.role === 'boss'
        ? Array(18).fill(kind) : actor.recentActivities.includes(kind) ? [kind] : [kind, kind, kind]);
      const kind = this.choose(weighted);
      if (kind === 'DESK') {
        const goal = this.desk(actor);
        this.assign(actor, goal);
        if (actor.mode === 'AT_DESK') { actor.destination = goal; this.settle(actor); }
      } else if (kind === 'SOCIAL') {
        const pair = this.choose(pairs).map(slot => this.idleGoal(slot, 'SOCIAL'));
        for (let i = 0; i < 2; i++) {
          const goal = pair[i], other = pair[1 - i];
          goal.yaw = Math.atan2(other.position.x - goal.position.x, other.position.z - goal.position.z);
          goal.partnerId = (i === 0 ? partner : actor).id;
        }
        this.assign(actor, pair[0]); this.assign(partner, pair[1]);
      } else {
        const goal = this.choose(candidates.filter(slot => slot.kind === kind));
        if (kind === 'VISIT') {
          goal.hostId = host.id;
          goal.yaw = Math.atan2(host.avatar.root.position.x - goal.position.x,
            host.avatar.root.position.z - goal.position.z);
          this.assignments.set(host.id, this.desk(host));
          host.idleUntil = this.time + 60;
        }
        this.assign(actor, goal);
      }
    }
  }

  desired(actor) {
    const role = this.status.getRole(actor.role);
    return role.active || !role.known || !this.assignments.has(actor.id)
      ? this.desk(actor)
      : this.assignments.get(actor.id);
  }

  plan(actor, target, ignoreIds = []) {
    const start = actor.avatar.root.position;
    const seat = actor.currentSeat;
    const contactRoute = ignoreIds.length && seat?.approach
      ? [point(start), point(seat.approach), point(target)] : null;
    const people = ignoreIds.length ? [] : this.others(actor);
    let path = contactRoute
      ? (this.nav.validatePath(contactRoute, { ignoreIds }) ? contactRoute : [])
      : this.nav.findPath(start, target, { ignoreIds, actors: people, actorId: actor.id });
    // A colleague's leaning head can enter the comfort gap between frames.
    // A static route lets move() perform its strictly separating escape; its
    // swept body/head collision checks still apply to every actual step.
    if (!path.length && !contactRoute && this.nav.lastSearch.reason === 'INVALID_ENDPOINT' &&
        this.nav.isWalkable(start, { ignoreIds }) && this.nav.isWalkable(target, { ignoreIds, actors: people }))
      path = this.nav.findPath(start, target, { ignoreIds });
    if (!path.length) {
      actor.route = []; actor.routeIndex = 0; actor.speed = 0;
      actor.mode = 'WAITING_PATH'; actor.retryTime = this.time + 1.2;
      actor.pathWaitSince ??= this.time;
      this.pathsRejected++;
      return false;
    }
    actor.route = path; actor.routeIndex = Math.min(1, path.length - 1); actor.progressWaypoint = null;
    actor.pathWaitSince = null;
    actor.routeIgnore = ignoreIds;
    actor.mode = 'WALKING';
    return true;
  }

  canLeaveSeat(actor) {
    const seat = actor.currentSeat;
    if (!seat) return true;
    const route = [point(actor.avatar.root.position), ...(seat.approach ? [point(seat.approach)] : []), point(seat.entry)];
    return this.nav.validatePath(route, { ignoreIds: seatIgnores(seat), actors: this.others(actor), actorId: actor.id });
  }

  beginDeparture(actor, destination) {
    actor.destination = destination;
    if (actor.currentSeat && actor.sit > .01) {
      actor.mode = this.canLeaveSeat(actor)
        ? (actor.handsReady ? 'RETRACTING_TRAY' : 'STANDING_UP') : 'WAITING_EXIT';
      actor.phase = 0; actor.startSit = actor.sit; actor.trayStart = actor.trayOffset;
    } else {
      actor.currentSeat = null;
      this.plan(actor, destination.entry);
    }
  }

  others(actor) {
    return this.actors.filter(other => other !== actor && other.avatar.available !== false).flatMap(other => {
      const body = { id: other.id, x: other.avatar.root.position.x,
        z: other.avatar.root.position.z, radius: this.nav.radius, priority: other.priority };
      // A seated person can lean beyond the floor anchor. Reserve the actual
      // upper-body footprint so a passing colleague cannot intersect it.
      other.avatar.root.updateMatrixWorld?.(true);
      const head = other.avatar.headAnchor?.matrixWorld?.elements;
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
    if (actor.progressWaypoint !== waypoint) {
      actor.progressWaypoint = waypoint; actor.progressDistance = remaining; actor.progressAt = this.time;
    }
    const inContact = Boolean(actor.currentSeat);
    const maximum = inContact ? .28 : this.reducedMotion ? .62 : actor.station.gender === 'male' ? .76 : .70;
    const targetSpeed = Math.min(maximum, Math.sqrt(2 * .85 * remaining));
    actor.motionSpeed += Math.sign(targetSpeed - actor.motionSpeed) * Math.min(Math.abs(targetSpeed - actor.motionSpeed), dt * .85);
    const step = Math.min(remaining, dt * actor.motionSpeed);
    const from = point(root.position);
    const desired = { x: from.x + dx / remaining * step, z: from.z + dz / remaining * step };
    const move = this.nav.move({ id: actor.id, ...from, priority: actor.priority,
      radius: this.nav.radius }, desired, this.others(actor), { ignoreIds: actor.routeIgnore || [], allowDetour: !inContact });
    root.position.set(move.x, 0, move.z);
    const travelled = distance(from, move);
    actor.speed = dt > 0 ? travelled / dt : 0;
    if (travelled > .0001) this.turn(actor, Math.atan2(move.x - from.x, move.z - from.z), dt);
    if (move.blocked) { this.blockedFrames++; actor.waiting += dt; actor.motionSpeed = 0; }
    else actor.waiting = 0;
    const left = distance(root.position, waypoint);
    if (actor.progressDistance - left > .08) { actor.progressDistance = left; actor.progressAt = this.time; }
    // Successful lateral yields may still make no progress toward the waypoint.
    // Replan around complete body/head footprints instead of creeping forever.
    if ((actor.waiting > 1.8 || this.time - actor.progressAt > 1.5) && !inContact) {
      this.progressReplans++;
      const goal = actor.route[actor.route.length - 1];
      const path = this.nav.findPath(root.position, goal, {
        ignoreIds: actor.routeIgnore || [], actors: this.others(actor), actorId: actor.id,
      });
      if (path.length) { actor.route = path; actor.routeIndex = Math.min(1, path.length - 1); actor.progressWaypoint = null; }
      actor.waiting = 0; actor.progressAt = this.time; actor.progressDistance = left;
    }
    return false;
  }

  arrive(actor) {
    const goal = actor.destination;
    actor.speed = 0;
    if (!Number.isFinite(goal.seatHeight)) {
      actor.currentSeat = null; actor.mode = goal.kind === 'SOCIAL' ? 'SOCIAL' : 'IDLE_ACTIVITY';
      actor.sit = 0; this.settle(actor); return;
    }
    const root = actor.avatar.root;
    const contactRoute = [point(root.position), ...(goal.approach ? [point(goal.approach)] : []), point(goal.position)];
    if (!this.nav.validatePath(contactRoute, { ignoreIds: seatIgnores(goal), actors: this.others(actor), actorId: actor.id })) {
      actor.mode = 'WAITING_PATH'; actor.retryTime = this.time + 1.2; return;
    }
    actor.currentSeat = goal;
    actor.mode = 'DOCKING';
    actor.route = contactRoute; actor.routeIndex = 1;
    actor.routeIgnore = seatIgnores(goal);
  }

  update(dt, elapsed) {
    this.time = elapsed;
    if (elapsed >= this.retryAt) { this.refreshAssignments(); this.retryAt = elapsed + .5; }
    this.nav.beginFrame();
    const ordered = [...this.actors].sort((a, b) =>
      Number(this.status.getRole(b.role).active) - Number(this.status.getRole(a.role).active) ||
      a.basePriority - b.basePriority);
    for (const actor of ordered) {
      // A missing/loading licensed model is never represented by an invisible
      // wandering actor or given a synthetic activity label.
      if (actor.avatar.available === false) { actor.speed = 0; continue; }
      const goal = this.desired(actor);
      const liveRole = this.status.getRole(actor.role);
      actor.priority = actor.basePriority - (liveRole.active ? 100 : 0);
      actor.waiting ||= 0;
      if (actor.mode === 'WAITING_PATH' && goal.kind !== 'DESK' &&
          elapsed - (actor.pathWaitSince ?? elapsed) > 12) {
        this.assignments.delete(actor.id); actor.idleUntil = elapsed;
        actor.pathWaitSince = null;
      }
      const settled = ['AT_DESK', 'RESTING', 'SOCIAL', 'IDLE_ACTIVITY'].includes(actor.mode);
      if (settled && actor.destination?.id !== goal.id &&
          !(actor.mode === 'AT_DESK' && goal.kind === 'DESK')) this.beginDeparture(actor, goal);
      else if (actor.mode === 'AT_DESK' && goal.kind === 'DESK') actor.destination = goal;
      // Cancel the leisure docking/sitting transition as soon as real work is
      // observed, without teleporting through the chair collider.
      if (['DOCKING', 'ALIGNING', 'SITTING_DOWN', 'ALIGN_HANDS', 'EXTENDING_TRAY'].includes(actor.mode) && actor.destination?.id !== goal.id) {
        if (actor.sit > .01) this.beginDeparture(actor, goal);
        else {
          actor.destination = goal; actor.exiting = true;
          this.plan(actor, actor.currentSeat.entry, seatIgnores(actor.currentSeat));
        }
      }
      if (actor.mode === 'WAITING_EXIT') {
        actor.destination = goal;
        if (goal.id === actor.currentSeat?.id) {
          actor.mode = actor.sit < 1 ? 'SITTING_DOWN' : goal.kind !== 'DESK' ? 'RESTING'
            : actor.handsReady ? (actor.trayOffset > .001 ? 'EXTENDING_TRAY' : 'AT_DESK') : 'ALIGN_HANDS';
          actor.phase = actor.sit < 1 ? inverseSmooth(actor.sit) : 0; actor.trayStart = actor.trayOffset;
        } else if (this.canLeaveSeat(actor)) {
          actor.mode = actor.handsReady ? 'RETRACTING_TRAY' : 'STANDING_UP';
          actor.startSit = actor.sit; actor.phase = 0; actor.trayStart = actor.trayOffset;
        }
        else actor.sit = Math.min(1, actor.sit + dt * .8);
      }
      if (actor.mode === 'RETRACTING_TRAY') {
        actor.destination = goal;
        if (goal.id === actor.currentSeat?.id) { actor.mode = 'EXTENDING_TRAY'; actor.phase = 0; actor.trayStart = actor.trayOffset; }
      }
      if (actor.mode === 'AT_DESK' && !actor.handsReady) { actor.mode = 'ALIGN_HANDS'; actor.phase = 0; }
      if (actor.mode === 'STANDING_UP') actor.destination = goal;
      if (actor.mode === 'WALKING' && actor.destination?.id !== goal.id && !actor.exiting) {
        actor.destination = goal; this.plan(actor, goal.entry);
      }
      if (actor.mode === 'WAITING_PATH' && actor.destination?.id !== goal.id) {
        actor.destination = goal; actor.retryTime = elapsed;
      }
      if (actor.mode === 'RETRACTING_TRAY') {
        actor.phase += dt / .35;
        actor.trayOffset = actor.trayStart + (.42 - actor.trayStart) * smooth(actor.phase);
        if (actor.phase >= 1) {
          actor.trayOffset = .42; actor.phase = 0;
          actor.mode = this.canLeaveSeat(actor) ? 'STANDING_UP' : 'WAITING_EXIT';
          if (actor.mode === 'STANDING_UP') actor.handsReady = false;
        }
      } else if (actor.mode === 'ALIGN_HANDS') {
        actor.trayOffset = .42; actor.phase += dt / .4;
        if (actor.phase >= 1) { actor.handsReady = true; actor.mode = 'EXTENDING_TRAY'; actor.phase = 0; actor.trayStart = .42; }
      } else if (actor.mode === 'EXTENDING_TRAY') {
        actor.phase += dt / .35; actor.trayOffset = actor.trayStart * (1 - smooth(actor.phase));
        if (actor.phase >= 1) { actor.trayOffset = 0; actor.mode = 'AT_DESK'; actor.handsReady = true; this.settle(actor); }
      } else if (actor.mode === 'STANDING_UP') {
        actor.handsReady = false; actor.trayOffset = .42;
        actor.phase += dt / 1.42;
        actor.sit = (actor.startSit ?? 1) * (1 - smooth(actor.phase));
        if (actor.phase >= 1) {
          const seat = actor.currentSeat;
          actor.sit = 0; actor.exiting = true;
          this.plan(actor, seat.entry, seatIgnores(seat));
        }
      } else if (actor.mode === 'WALKING') {
        if (this.follow(actor, dt)) {
          if (actor.exiting) {
            actor.exiting = false; actor.currentSeat = null;
            actor.destination = goal; this.plan(actor, goal.entry);
          } else this.arrive(actor);
        }
      } else if (actor.mode === 'DOCKING') {
        if (this.follow(actor, dt)) { actor.mode = 'ALIGNING'; actor.phase = 0; actor.motionSpeed = 0; }
      } else if (actor.mode === 'ALIGNING') {
        this.turn(actor, actor.currentSeat.yaw, dt);
        const angle = actor.currentSeat.yaw - actor.avatar.root.rotation.y;
        if (Math.abs(Math.atan2(Math.sin(angle), Math.cos(angle))) < .025) { actor.mode = 'SITTING_DOWN'; actor.phase = 0; }
      } else if (actor.mode === 'SITTING_DOWN') {
        actor.phase += dt / 1.42; actor.sit = smooth(actor.phase);
        this.turn(actor, actor.currentSeat.yaw, dt);
        if (actor.phase >= 1) {
          actor.mode = actor.destination.kind === 'DESK' ? 'ALIGN_HANDS' : 'RESTING';
          actor.phase = 0; actor.handsReady = false;
          if (actor.mode === 'RESTING') this.settle(actor);
        }
      } else if (actor.mode === 'WAITING_PATH' && elapsed >= (actor.retryTime || 0)) {
        if (actor.exiting && actor.currentSeat) this.plan(actor, actor.currentSeat.entry, seatIgnores(actor.currentSeat));
        else this.plan(actor, actor.destination?.entry || goal.entry);
      } else if (['SOCIAL', 'IDLE_ACTIVITY'].includes(actor.mode)) this.turn(actor, goal.yaw, dt);
      if (actor.currentSeat?.id === `desk-${actor.role}`)
        actor.station.setChairOffset?.(.42 * (1 - actor.sit), actor.trayOffset);
      const partner = goal.partnerId && this.actors.find(other => other.id === goal.partnerId);
      const host = goal.hostId && this.actors.find(other => other.id === goal.hostId);
      const visitor = actor.role === 'boss' && this.actors.find(other => other.destination?.hostId === actor.id && other.mode === 'IDLE_ACTIVITY');
      const conversation = (partner?.mode === 'SOCIAL' ? partner : host || visitor) || null;
      conversation?.avatar.root.updateMatrixWorld?.(true);
      const face = conversation?.avatar.headAnchor?.matrixWorld?.elements;
      const gazeTarget = conversation ? (face
        ? { x: face[12], y: face[13] - .21, z: face[14] }
        : { x: conversation.avatar.root.position.x, y: 1.4, z: conversation.avatar.root.position.z }) : null;
      if (conversation && ['SOCIAL', 'IDLE_ACTIVITY'].includes(actor.mode)) this.turn(actor, Math.atan2(gazeTarget.x - actor.avatar.root.position.x, gazeTarget.z - actor.avatar.root.position.z), dt);
      const state = ['WALKING', 'DOCKING'].includes(actor.mode) ? 'WALKING'
        : actor.mode === 'STANDING_UP' ? 'STAND_UP'
        : actor.mode === 'SITTING_DOWN' || (actor.mode === 'WAITING_EXIT' && actor.sit < 1) ? 'SIT_DOWN'
        : actor.mode === 'SOCIAL' && conversation ? 'SOCIAL'
        : actor.mode === 'AT_DESK' && liveRole.active ? 'WORKING' : 'IDLE';
      actor.avatar.pose(dt, elapsed, {
        state, gazeTarget, deskHands: ['ALIGN_HANDS', 'EXTENDING_TRAY'].includes(actor.mode),
        deskRest: (actor.mode === 'AT_DESK' && !liveRole.active) || actor.mode === 'RETRACTING_TRAY' ||
          (actor.mode === 'WAITING_EXIT' && actor.handsReady), sit: actor.sit, seatHeight: actor.currentSeat?.seatHeight ?? .43,
        seatContact: Boolean(actor.currentSeat), seatYaw: actor.currentSeat?.yaw,
        seatPosition: actor.currentSeat ? point(actor.currentSeat.position ?? actor.currentSeat) : null,
        walkSpeed: actor.speed, transitionProgress: actor.mode === 'STANDING_UP'
          ? inverseSmooth(1 - actor.sit) : actor.mode === 'WAITING_EXIT' ? inverseSmooth(actor.sit) : actor.phase,
        reducedMotion: this.reducedMotion,
        activity: ['IDLE_ACTIVITY', 'RESTING'].includes(actor.mode)
          ? ({ VISIT: 'VISITING' }[goal.kind] || goal.kind) : null,
      });
    }
  }

  diagnostics() {
    return { pathsRejected: this.pathsRejected, blockedFrames: this.blockedFrames, progressReplans: this.progressReplans,
      states: this.actors.map(actor => ({ id: actor.id, name: actor.station.name,
        state: actor.avatar.available === false ? 'ASSET_UNAVAILABLE' : actor.mode,
        active: this.status.getRole(actor.role).active,
        available: actor.avatar.available !== false,
        known: this.status.getRole(actor.role).known, sit: actor.sit,
        x: actor.avatar.root.position.x, z: actor.avatar.root.position.z,
        destination: actor.destination?.id ?? null,
        activity: actor.destination?.kind ?? 'DESK',
        recentActivities: [...actor.recentActivities],
        pathRemaining: Math.max(0, actor.route.length - actor.routeIndex) })) };
  }
}
