/** Office screens are a read-only projection of existing worker telemetry. */
export function createMonitorTextures(THREE, { mobile = false, reducedMotion = false } = {}) {
  const WIDTH = mobile ? 384 : 768, HEIGHT = mobile ? 192 : 384;
  const FPS = mobile ? 8 : 12;
  const palette = { bg: '#111b22', panel: '#192731', border: '#3b4545', gold: '#d2b67f',
    text: '#f0ede4', muted: '#9da9ac', green: '#8bc5ab', red: '#e69b90' };
  // Backend roles are deliberately retained, independent of the five visible desks.
  const roles = ['market', 'neuro', 'risk', 'trading', 'position', 'reviewer', 'report', 'boss'];
  const titles = { market: 'HERMES AGENT', neuro: 'NEUROBRO', risk: 'RISK MANAGER',
    trading: 'BINANCE', position: 'POSITION MONITOR', reviewer: 'TRADE REVIEWER',
    report: 'REPORT MANAGER', boss: 'HARUN' };
  const screens = new Map();
  let disposed = false, lastUpdate = -Infinity, updates = 0, firstUpdate = true, lastTelemetry = '';
  if (!THREE || typeof THREE.CanvasTexture !== 'function') throw new TypeError('Three.js CanvasTexture is required.');

  const clean = value => String(value ?? '—').replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim();
  const short = (value, length = 36) => clean(value).slice(0, length);
  const finite = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const number = (value, digits = 2) => finite(value) ? Number(value).toFixed(digits) : '—';
  const fresh = (value, ttl) => {
    const at = typeof value === 'string' ? Date.parse(value) : NaN;
    return Number.isFinite(at) && at <= Date.now() + 30000 && Date.now() - at < ttl;
  };
  const phaseCurrent = data => data.connected === true && fresh(data.robot_checked_at, 120000);
  const accountCurrent = data => data.connected === true && fresh(data.account_checked_at, 45000);
  const reportsCurrent = data => data.connected === true && fresh(data.reports_checked_at, 120000);
  const balance = data => accountCurrent(data) ? data.usdt_wallet_balance ?? data.balance : null;
  const positions = data => accountCurrent(data) ? data.active_positions : null;
  const pnl = data => reportsCurrent(data) ? data.pnl_today : null;
  const status = data => phaseCurrent(data) && typeof data.status === 'string' ? short(data.status, 36) : 'MENUNGGU DATA WORKER';
  const gateway = data => phaseCurrent(data) && data.execution_gateway && typeof data.execution_gateway === 'object' ? data.execution_gateway : null;
  const decision = data => phaseCurrent(data) && data.last_decision && typeof data.last_decision === 'object' ? data.last_decision : null;
  const events = data => {
    if (data.connected !== true || !fresh(data.receivedAt ?? data.generated_at ?? data.robot_checked_at, 120000)) return [];
    return (Array.isArray(data.events) ? data.events : []).filter(event => event &&
      typeof event.message === 'string' && typeof event.agent === 'string' && fresh(event.at, 120000)).slice(-4).reverse();
  };
  const timestamp = value => fresh(value, 120000) ? value.slice(11, 19) + ' UTC' : '—';
  const text = (ctx, value, x, y, color = palette.text, size = 11, weight = '400') => {
    ctx.fillStyle = color; ctx.font = `${weight} ${size}px Inter, Arial, sans-serif`;
    ctx.fillText(short(value, 96), x, y);
  };
  function line(ctx, x1, y1, x2, y2, color = palette.border) {
    ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
  }
  function box(ctx, x, y, w, h, fill = palette.panel, radius = 6) {
    ctx.fillStyle = fill; ctx.beginPath(); ctx.moveTo(x + radius, y);
    ctx.arcTo(x + w, y, x + w, y + h, radius); ctx.arcTo(x + w, y + h, x, y + h, radius);
    ctx.arcTo(x, y + h, x, y, radius); ctx.arcTo(x, y, x + w, y, radius); ctx.closePath(); ctx.fill();
  }
  function stat(ctx, label, value, x, y, width = 155, color = palette.gold) {
    box(ctx, x, y, width, 50); text(ctx, label, x + 10, y + 16, palette.muted, 9);
    text(ctx, value, x + 10, y + 39, color, 19, '600');
  }
  function row(ctx, label, value, y, color = palette.text) {
    text(ctx, label, 25, y, palette.muted, 10); text(ctx, value, 184, y, color, 12, '600');
  }
  function frame(ctx, role, variant, data) {
    ctx.fillStyle = palette.bg; ctx.fillRect(0, 0, 512, 256);
    const gradient = ctx.createLinearGradient(0, 0, 512, 0);
    gradient.addColorStop(0, '#28322f'); gradient.addColorStop(1, '#131e27');
    ctx.fillStyle = gradient; ctx.fillRect(0, 0, 512, 34);
    text(ctx, titles[role], 15, 23, palette.gold, 15, '600');
    text(ctx, variant ? 'AKTIVITAS' : 'OVERVIEW', 420, 22, palette.muted, 9);
    line(ctx, 0, 34, 512, 34, palette.gold);
    text(ctx, 'DATA WORKER · READ ONLY', 14, 247, palette.muted, 9);
    text(ctx, timestamp(data.robot_checked_at), 402, 247, palette.muted, 9);
  }
  function activity(ctx, data, { y = 119, count = 3 } = {}) {
    const recent = events(data).slice(0, count);
    text(ctx, 'AKTIVITAS WORKER', 25, y, palette.gold, 10, '600');
    if (!recent.length) {
      text(ctx, 'Belum ada aktivitas terbaru.', 25, y + 23, palette.muted, 11);
      return;
    }
    for (let index = 0; index < recent.length; index++) {
      const event = recent[index], top = y + 21 + index * 31;
      text(ctx, short(event.agent, 24), 25, top, palette.text, 10, '600');
      text(ctx, timestamp(event.at), 402, top, palette.muted, 9);
      // These are actual worker messages, not scripted character dialogue.
      text(ctx, short(event.message, 79), 25, top + 13, palette.muted, 10);
    }
  }
  function analysis(ctx, data) {
    box(ctx, 13, 46, 486, 62);
    row(ctx, 'FASE WORKER', status(data), 68, palette.gold);
    row(ctx, 'SIMBOL TERAKHIR', decision(data)?.symbol ?? '—', 94);
    activity(ctx, data, { y: 126, count: 3 });
  }
  function risk(ctx, data) {
    stat(ctx, 'RISIKO / SL · USDT', number(phaseCurrent(data) ? data.risk_target_usdt : null), 13, 46);
    stat(ctx, 'SLOT TERSEDIA', number(phaseCurrent(data) ? data.available_slots : null, 0), 178, 46);
    stat(ctx, 'POSISI AKTIF', number(positions(data), 0), 343, 46, 156);
    box(ctx, 13, 107, 486, 43);
    row(ctx, 'KEPUTUSAN TERAKHIR', decision(data)?.status ?? '—', 134);
    activity(ctx, data, { y: 169, count: 1 });
  }
  function trading(ctx, data) {
    box(ctx, 13, 46, 486, 98);
    const adapter = gateway(data);
    row(ctx, 'GATEWAY', adapter?.status ?? 'BELUM ADA DATA GATEWAY', 70, palette.gold);
    row(ctx, 'FASE WORKER', status(data), 99);
    row(ctx, 'KODE KENDALA', adapter?.failure_code ?? '—', 127, palette.muted);
    stat(ctx, 'POSISI AKTIF', number(positions(data), 0), 13, 157, 238);
    stat(ctx, 'ORDER TERTUNDA', number(phaseCurrent(data) ? data.pending_orders : null, 0), 261, 157, 238);
    text(ctx, 'STATUS BERASAL DARI WORKER', 25, 226, palette.muted, 9);
  }
  function position(ctx, data) {
    stat(ctx, 'WALLET FUTURES · USDT', number(balance(data)), 13, 46, 189);
    stat(ctx, 'POSISI AKTIF', number(positions(data), 0), 212, 46, 134);
    stat(ctx, 'PNL HARI INI', number(pnl(data)), 356, 46, 143);
    box(ctx, 13, 108, 486, 103);
    const manual = accountCurrent(data) && Array.isArray(data.manual_exposure) ? data.manual_exposure : [];
    text(ctx, 'EKSPOSUR MANUAL', 25, 130, palette.gold, 10, '600');
    text(ctx, manual.length ? short(manual.join(' / '), 78) : '—', 25, 153, palette.text, 12);
    text(ctx, 'FASE WORKER', 25, 180, palette.muted, 10); text(ctx, status(data), 184, 180, palette.text, 12);
    text(ctx, 'ACCOUNT TELEMETRY · READ ONLY', 25, 226, palette.muted, 9);
  }
  function reviewer(ctx, data) {
    box(ctx, 13, 46, 486, 98);
    const item = decision(data);
    row(ctx, 'SIMBOL', item?.symbol ?? '—', 70);
    row(ctx, 'KEPUTUSAN', item?.status ?? '—', 99, palette.gold);
    row(ctx, 'KODE KENDALA', item?.failure_code ?? '—', 127, palette.muted);
    activity(ctx, data, { y: 165, count: 1 });
  }
  function report(ctx, data) {
    stat(ctx, 'PNL HARI INI · USDT', number(pnl(data)), 13, 46);
    stat(ctx, 'FILL HARI INI', number(reportsCurrent(data) ? data.trades_today : null, 0), 178, 46);
    stat(ctx, 'WALLET · USDT', number(balance(data)), 343, 46, 156);
    activity(ctx, data, { y: 121, count: 3 });
  }
  function boss(ctx, data, variant) {
    if (variant) { report(ctx, data); return; }
    box(ctx, 13, 46, 486, 44);
    row(ctx, 'FASE WORKER', status(data), 73, palette.gold);
    stat(ctx, 'WALLET · USDT', number(balance(data)), 13, 101);
    stat(ctx, 'POSISI AKTIF', number(positions(data), 0), 178, 101);
    stat(ctx, 'PNL HARI INI', number(pnl(data)), 343, 101, 156);
    const robot = phaseCurrent(data) ? data.robot_on ?? data.robotOn : null;
    text(ctx, 'ROBOT', 25, 174, palette.muted, 10);
    text(ctx, robot === true ? 'ON' : robot === false ? 'OFF' : '—', 91, 174, palette.gold, 11, '600');
    text(ctx, 'KEPUTUSAN', 178, 174, palette.muted, 10);
    text(ctx, decision(data)?.status ?? '—', 265, 174, palette.text, 11, '600');
    activity(ctx, data, { y: 196, count: 1 });
  }
  const painters = { market: analysis, neuro: analysis, risk, trading, position, reviewer, report, boss };
  for (const role of roles) {
    const variants = [];
    for (let variant = 0; variant < 2; variant++) {
      const canvas = document.createElement('canvas'); canvas.width = WIDTH; canvas.height = HEIGHT;
      const context = canvas.getContext('2d', { alpha: false });
      if (!context) throw new Error('Canvas 2D monitor rendering is unavailable.');
      context.setTransform(WIDTH / 512, 0, 0, HEIGHT / 256, 0, 0);
      const texture = new THREE.CanvasTexture(canvas);
      if (THREE.SRGBColorSpace) texture.colorSpace = THREE.SRGBColorSpace;
      if (THREE.LinearFilter) { texture.minFilter = THREE.LinearFilter; texture.magFilter = THREE.LinearFilter; }
      texture.generateMipmaps = false; texture.name = `office-monitor-${role}-${variant}`;
      variants.push({ canvas, context, texture, variant });
    }
    screens.set(role, variants);
  }
  function render(telemetry) {
    for (const role of roles) for (const screen of screens.get(role)) {
      frame(screen.context, role, screen.variant, telemetry);
      painters[role](screen.context, telemetry, screen.variant); screen.texture.needsUpdate = true;
    }
  }
  function signature(data) {
    return JSON.stringify([balance(data), pnl(data), positions(data), status(data),
      phaseCurrent(data) ? [data.risk_target_usdt, data.available_slots, data.pending_orders, data.robot_on ?? data.robotOn, data.robot_checked_at] : null,
      reportsCurrent(data) ? data.trades_today : null, gateway(data), decision(data), events(data),
      accountCurrent(data) ? data.manual_exposure : null]);
  }
  render({});
  return {
    get(role, variant = 0) {
      if (disposed) throw new Error('Monitor textures have been disposed.');
      if (!screens.has(role)) throw new RangeError(`Unknown monitor role: ${role}`);
      const index = Number.isFinite(Number(variant)) ? Math.abs(Math.trunc(Number(variant))) % 2 : 0;
      return screens.get(role)[index].texture;
    },
    update(time, telemetry = {}) {
      if (disposed) return false;
      const t = Number.isFinite(time) ? time : 0;
      if (!firstUpdate && t >= lastUpdate && t - lastUpdate < 1 / FPS) return false;
      const data = telemetry && typeof telemetry === 'object' ? telemetry : {};
      const next = signature(data);
      // No decorative motion: repaint only for changed facts or expired freshness.
      if (!firstUpdate && next === lastTelemetry) { lastUpdate = t; return false; }
      render(data); lastTelemetry = next; lastUpdate = t; firstUpdate = false; updates++; return true;
    },
    dispose() {
      if (disposed) return;
      for (const variants of screens.values()) for (const screen of variants) {
        screen.texture.dispose(); screen.canvas.width = 1; screen.canvas.height = 1;
      }
      disposed = true;
    },
    diagnostics() {
      return { roles: [...roles], textures: roles.length * 2, width: WIDTH, height: HEIGHT,
        fps: FPS, reducedMotion: Boolean(reducedMotion), updates, disposed, decorativeCharts: false, realEventsOnly: true };
    }
  };
}
