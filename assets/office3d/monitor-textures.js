/** Read-only, procedural screen artwork. No market data is invented as account telemetry. */
export function createMonitorTextures(THREE, { mobile = false, reducedMotion = false } = {}) {
  const WIDTH = mobile ? 384 : 512, HEIGHT = mobile ? 192 : 256;
  const FPS = mobile ? 8 : 12;
  const palette = {
    bg: '#06131e', panel: '#0a1e2c', border: '#173c4f', cyan: '#57e1fa',
    blue: '#298af1', green: '#5ee6a2', amber: '#ffd078', red: '#ff7488',
    text: '#d7f3fa', muted: '#7198aa', grid: '#123143'
  };
  const roles = ['market', 'neuro', 'risk', 'trading', 'position', 'reviewer', 'report', 'boss'];
  const titles = {
    market: 'MARKET INTELLIGENCE', neuro: 'NEURO // ANALYSIS', risk: 'RISK CONTROL',
    trading: 'FUTURES // EXECUTION DESK', position: 'POSITION MONITOR',
    reviewer: 'SETUP REVIEW', report: 'OFFICE REPORTS', boss: 'HARUN // COMMAND CENTER'
  };
  const screens = new Map();
  let disposed = false, lastUpdate = -Infinity, updates = 0, firstUpdate = true, lastTelemetry = '';
  if (!THREE || typeof THREE.CanvasTexture !== 'function') throw new TypeError('Three.js CanvasTexture is required.');

  const finite = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const number = (value, digits = 2) => finite(value) ? Number(value).toFixed(digits) : '—';
  const short = (value, length = 30) => String(value ?? '—').replace(/[\r\n\t]/g, ' ').slice(0, length);
  const phase = (t, index, speed = 1) => Math.sin(t * speed + index * 1.83);
  const actual = data => ['balance', 'usdt_wallet_balance', 'pnl_today', 'running_positions', 'active_positions']
    .some(key => finite(data[key]));
  const balance = data => data.usdt_wallet_balance ?? data.balance;
  const positions = data => data.running_positions ?? data.active_positions;
  const pnl = data => data.pnl_today ?? data.realized_pnl;
  const text = (ctx, value, x, y, color = palette.text, size = 11, weight = '400') => {
    ctx.fillStyle = color; ctx.font = `${weight} ${size}px ui-monospace, SFMono-Regular, Consolas, monospace`;
    ctx.fillText(short(value, 80), x, y);
  };
  function line(ctx, x1, y1, x2, y2, color = palette.grid, width = 1) {
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
  }
  function box(ctx, x, y, w, h, fill = palette.panel, radius = 5) {
    ctx.fillStyle = fill; ctx.beginPath();
    ctx.moveTo(x + radius, y); ctx.arcTo(x + w, y, x + w, y + h, radius);
    ctx.arcTo(x + w, y + h, x, y + h, radius); ctx.arcTo(x, y + h, x, y, radius);
    ctx.arcTo(x, y, x + w, y, radius); ctx.closePath(); ctx.fill();
  }
  function dot(ctx, x, y, radius, color) {
    ctx.fillStyle = color; ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2); ctx.fill();
  }
  function grid(ctx, x, y, w, h, rows = 4, columns = 6) {
    for (let i = 0; i <= rows; i++) line(ctx, x, y + h * i / rows, x + w, y + h * i / rows);
    for (let i = 0; i <= columns; i++) line(ctx, x + w * i / columns, y, x + w * i / columns, y + h);
  }
  function series(ctx, x, y, w, h, t, color, seed = 0) {
    grid(ctx, x, y, w, h);
    ctx.beginPath();
    for (let i = 0; i <= 38; i++) {
      const value = .49 + phase(t * .18, i * .16 + seed) * .19 + phase(t * .11, i * .05 + seed * 2) * .11;
      const sx = x + i / 38 * w, sy = y + h * (1 - value);
      if (i === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
    }
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
  }
  function stat(ctx, label, value, x, y, width = 145, color = palette.cyan) {
    box(ctx, x, y, width, 45); text(ctx, label, x + 9, y + 14, palette.muted, 9);
    text(ctx, value, x + 9, y + 34, color, 18, '600');
  }
  function frame(ctx, role, variant, data) {
    ctx.fillStyle = palette.bg; ctx.fillRect(0, 0, 512, 256);
    const gradient = ctx.createLinearGradient(0, 0, 512, 0);
    gradient.addColorStop(0, '#102b3f'); gradient.addColorStop(1, '#071720');
    ctx.fillStyle = gradient; ctx.fillRect(0, 0, 512, 30);
    dot(ctx, 14, 15, 3, palette.cyan); text(ctx, titles[role], 25, 19, palette.text, 12, '600');
    text(ctx, variant ? 'AUX / 02' : 'MAIN / 01', 422, 19, palette.muted, 10);
    line(ctx, 0, 30, 512, 30, palette.border);
    ctx.fillStyle = '#081923'; ctx.fillRect(0, 234, 512, 22);
    text(ctx, actual(data) ? 'READ ONLY TELEMETRY · DEMO CHARTS' : 'SYNTHETIC VISUAL TELEMETRY · NO ORDERS', 12, 248, palette.muted, 9);
    text(ctx, 'HARUN AI', 442, 248, palette.cyan, 9);
  }
  function candles(ctx, t, variant) {
    box(ctx, 12, 42, 327, 154); text(ctx, variant ? 'DEMO / MOMENTUM' : 'DEMO / CANDLE FEED', 23, 58, palette.muted, 9);
    grid(ctx, 23, 70, 304, 113, 4, 8);
    for (let i = 0; i < 30; i++) {
      const base = 124 + phase(t * .12, i * .09 + variant) * 23 + phase(0, i * .37) * 8;
      const move = phase(t * .16, i + variant) * 13;
      const x = 28 + i * 10;
      line(ctx, x, base - 12 - Math.abs(move), x, base + 13 + Math.abs(move), move > 0 ? palette.green : palette.red);
      ctx.fillStyle = move > 0 ? palette.green : palette.red;
      ctx.fillRect(x - 3, Math.min(base, base + move), 6, Math.max(2, Math.abs(move)));
      ctx.globalAlpha = .35; ctx.fillRect(x - 3, 184 - Math.abs(move) * .7, 6, Math.abs(move) * .7 + 2); ctx.globalAlpha = 1;
    }
  }
  function globe(ctx, t) {
    const cx = 423, cy = 118, r = 58, spin = t * .2;
    ctx.save(); ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.clip();
    const glow = ctx.createRadialGradient(cx, cy, 2, cx, cy, r);
    glow.addColorStop(0, '#0e385c'); glow.addColorStop(1, '#071b2b');
    ctx.fillStyle = glow; ctx.fillRect(cx - r, cy - r, r * 2, r * 2);
    for (let i = -2; i <= 2; i++) {
      ctx.beginPath(); ctx.ellipse(cx, cy, r * Math.sqrt(1 - i * i / 12), 9 + Math.abs(i) * 4, 0, 0, Math.PI * 2);
      ctx.strokeStyle = '#145181'; ctx.lineWidth = 1; ctx.stroke();
    }
    for (let i = 0; i < 6; i++) {
      ctx.beginPath(); ctx.ellipse(cx, cy, Math.abs(Math.cos(spin + i * Math.PI / 6)) * r, r, 0, 0, Math.PI * 2);
      ctx.strokeStyle = '#1b64a0'; ctx.stroke();
    }
    // Stylized rotating continent/network points, all procedural.
    for (let i = 0; i < 57; i++) {
      const latitude = phase(0, i * .071) * .9, longitude = i * .39 + spin;
      const depth = Math.cos(longitude) * Math.cos(latitude);
      if (depth < 0) continue;
      const x = cx + Math.sin(longitude) * Math.cos(latitude) * r * .95;
      const y = cy + Math.sin(latitude) * r * .86;
      dot(ctx, x, y, i % 5 === 0 ? 2 : 1, i % 5 === 0 ? palette.cyan : palette.blue);
    }
    ctx.restore(); ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.strokeStyle = '#2c82bf'; ctx.stroke();
    text(ctx, 'GLOBAL SIGNAL GRID', 363, 192, palette.cyan, 9);
  }
  function market(ctx, t, data, variant) {
    candles(ctx, t, variant); globe(ctx, t + variant * 5);
    const tickers = ['BTC', 'ETH', 'SOL', 'BNB', 'HYPE'];
    const demoPrices = [67000, 3500, 150, 580, 28];
    for (let i = 0; i < tickers.length; i++) {
      const x = 13 + i * 99, value = phase(t * .35, i + variant) * 2.1;
      text(ctx, tickers[i], x, 208, palette.muted, 10);
      text(ctx, `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`, x + 33, 208, value >= 0 ? palette.green : palette.red, 9);
      text(ctx, '$'+(demoPrices[i] * (1 + value / 100)).toFixed(i < 2 ? 0 : 2), x, 224, palette.text, 11);
    }
  }
  function neuro(ctx, t, data, variant) {
    box(ctx, 12, 42, 285, 181); box(ctx, 306, 42, 194, 181);
    text(ctx, 'NEURAL SCREENING / VISUAL LOG', 22, 60, palette.cyan, 10);
    const code = ['> screen.universe(futures)', '  features := momentum + volume', '  tokens → encode.signal()', '  model.score(candidate)', '  setup.validate(entry,tp,sl)', '  exposure.read_only()', '  confidence → review_queue', '  return structured_analysis'];
    const offset = Math.floor(t * .8) % code.length;
    for (let i = 0; i < 8; i++) {
      text(ctx, String(i + 1).padStart(2, '0'), 22, 81 + i * 16, '#3c6173', 9);
      text(ctx, code[(i + offset + variant) % code.length], 45, 81 + i * 16, i === 5 ? palette.amber : i < 3 ? palette.green : palette.muted, 10);
    }
    // A breathing brain silhouette under the propagating neural graph.
    ctx.save(); ctx.translate(403, 129);
    const pulse = .94 + Math.sin(t * 1.5 + variant) * .025;
    ctx.scale(pulse, pulse); ctx.globalAlpha = .55;
    ctx.beginPath(); ctx.moveTo(0, -49);
    ctx.bezierCurveTo(-16, -66, -46, -49, -43, -27);
    ctx.bezierCurveTo(-63, -25, -64, 9, -44, 25);
    ctx.bezierCurveTo(-49, 45, -19, 59, 0, 40);
    ctx.bezierCurveTo(19, 59, 49, 45, 44, 25);
    ctx.bezierCurveTo(64, 9, 63, -25, 43, -27);
    ctx.bezierCurveTo(46, -49, 16, -66, 0, -49);
    ctx.strokeStyle = palette.blue; ctx.lineWidth = 2; ctx.stroke();
    line(ctx, 0, -45, 0, 39, palette.blue); ctx.restore();
    const nodes = [];
    for (let layer = 0; layer < 4; layer++) for (let n = 0; n < 4; n++) nodes.push({x: 325 + layer * 50, y: 77 + n * 31, layer});
    for (const a of nodes) for (const b of nodes) if (b.layer === a.layer + 1) {
      ctx.globalAlpha = .12 + (phase(t * 1.4, a.y + b.y) + 1) * .12;
      line(ctx, a.x, a.y, b.x, b.y, palette.cyan);
    }
    ctx.globalAlpha = 1;
    nodes.forEach((node, i) => dot(ctx, node.x, node.y, 3 + (phase(t * 2, i) + 1), i % 4 === variant ? palette.green : palette.cyan));
    text(ctx, 'TOKEN FLOW → SETUP', 319, 211, palette.cyan, 10);
    const marker = (t * 38) % 154; box(ctx, 322 + marker, 192, 12, 3, palette.green, 1);
  }
  function risk(ctx, t, data, variant) {
    stat(ctx, 'RISK / SL · USDT', number(data.risk_target_usdt), 12, 42, 145, palette.amber);
    stat(ctx, 'AVAILABLE SLOTS', number(data.available_slots, 0), 168, 42, 144);
    stat(ctx, 'EXPOSURE COUNT', number(positions(data), 0), 323, 42, 177, palette.green);
    box(ctx, 12, 99, 190, 123); text(ctx, 'SCENARIO MATRIX / DEMO', 22, 116, palette.muted, 9);
    for (let row = 0; row < 4; row++) for (let col = 0; col < 7; col++) {
      const score = (phase(t * .3, row * 2 + col + variant) + 1) / 2;
      ctx.globalAlpha = .24 + score * .55; box(ctx, 23 + col * 24, 128 + row * 19, 19, 14, score > .75 ? palette.amber : score < .25 ? palette.red : palette.green, 2); ctx.globalAlpha = 1;
    }
    box(ctx, 214, 99, 286, 123); text(ctx, 'DRAWDOWN PROFILE / DEMO', 225, 116, palette.muted, 9);
    series(ctx, 225, 130, 169, 73, t, palette.amber, variant);
    ctx.beginPath(); ctx.arc(452, 166, 33, Math.PI * .75, Math.PI * 2.25); ctx.strokeStyle = '#18394b'; ctx.lineWidth = 7; ctx.stroke();
    ctx.beginPath(); ctx.arc(452, 166, 33, Math.PI * .75, Math.PI * (.95 + (phase(t * .25, variant) + 1) * .45)); ctx.strokeStyle = palette.amber; ctx.stroke();
    text(ctx, 'CHECK', 436, 172, palette.amber, 10);
  }
  function trading(ctx, t, data, variant) {
    box(ctx, 12, 42, 224, 180); box(ctx, 246, 42, 254, 180);
    text(ctx, 'ORDER BOOK / DEMO', 22, 59, palette.amber, 10);
    text(ctx, 'PRICE', 23, 77, palette.muted, 9); text(ctx, 'QTY', 113, 77, palette.muted, 9); text(ctx, 'DEPTH', 167, 77, palette.muted, 9);
    for (let i = 0; i < 10; i++) {
      const ask = i < 5, amount = (phase(t * .6, i + variant) + 1) * 5 + .2, y = 94 + i * 11;
      ctx.globalAlpha = .15; ctx.fillStyle = ask ? palette.red : palette.green; ctx.fillRect(226 - amount * 13, y - 8, amount * 13, 9); ctx.globalAlpha = 1;
      text(ctx, (100 + (4.5 - i) * .12).toFixed(2), 23, y, ask ? palette.red : palette.green, 10);
      text(ctx, amount.toFixed(3), 113, y, palette.muted, 10);
      text(ctx, (amount * 100).toFixed(0), 176, y, palette.muted, 10);
    }
    text(ctx, 'BUY / SELL WALLS', 257, 59, palette.cyan, 10);
    grid(ctx, 258, 76, 228, 88, 3, 7);
    for (let i = 0; i < 22; i++) {
      const height = 17 + (phase(t * .4, i * .25 + variant) + 1) * 29;
      ctx.fillStyle = i < 11 ? palette.green : palette.red; ctx.globalAlpha = .7;
      ctx.fillRect(260 + i * 10, 164 - height, 7, height); ctx.globalAlpha = 1;
    }
    text(ctx, 'LEVERAGE / REVIEW ONLY', 259, 184, palette.muted, 10);
    text(ctx, finite(data.leverage) ? `${number(data.leverage, 0)}x` : '—', 259, 207, palette.amber, 18, '600');
    box(ctx, 352, 180, 60, 28, '#143b35'); text(ctx, 'BUY', 368, 198, palette.green, 11, '600');
    box(ctx, 420, 180, 66, 28, '#402631'); text(ctx, 'SELL', 437, 198, palette.red, 11, '600');
  }
  function position(ctx, t, data, variant) {
    stat(ctx, 'FUTURES BALANCE · USDT', number(balance(data)), 12, 42, 183);
    stat(ctx, 'OPEN POSITIONS', number(positions(data), 0), 206, 42, 136, palette.amber);
    stat(ctx, 'TODAY PNL · USDT', number(pnl(data)), 353, 42, 147, finite(pnl(data)) && Number(pnl(data)) < 0 ? palette.red : palette.green);
    box(ctx, 12, 99, 296, 123); text(ctx, 'EXPOSURE HISTORY / DEMO', 23, 116, palette.muted, 9);
    series(ctx, 23, 131, 273, 77, t, variant ? palette.blue : palette.green, variant + 3);
    box(ctx, 319, 99, 181, 123); text(ctx, 'MANUAL EXPOSURE', 329, 116, palette.amber, 10);
    const manual = Array.isArray(data.manual_exposure) ? data.manual_exposure : [];
    text(ctx, manual.length ? manual.slice(0, 3).map(v => short(v, 10)).join(' / ') : '—', 329, 140, palette.text, 11);
    text(ctx, 'STATUS', 329, 164, palette.muted, 9);
    text(ctx, data.bot_status ?? data.status ?? 'VISUAL DEMO', 329, 183, palette.cyan, 10);
    text(ctx, 'READ ONLY · NO ORDERS', 329, 208, palette.muted, 9);
  }
  function reviewer(ctx, t, data, variant) {
    box(ctx, 12, 42, 488, 180); text(ctx, 'SETUP QUEUE // HUMAN REVIEW', 24, 61, palette.cyan, 11);
    const rows = Array.isArray(data.setups) ? data.setups : Array.isArray(data.trades)
      ? data.trades.filter(trade => trade?.plan).slice(-4).reverse().map(trade =>
        ({ ...trade.plan, status: trade.state })) : [];
    text(ctx, 'SYMBOL', 24, 84, palette.muted, 9); text(ctx, 'STATUS', 125, 84, palette.muted, 9);
    text(ctx, 'ENTRY / SL', 279, 84, palette.muted, 9); text(ctx, 'RR', 445, 84, palette.muted, 9);
    for (let i = 0; i < 4; i++) {
      const row = rows[i], y = 109 + i * 28;
      line(ctx, 23, y + 10, 487, y + 10);
      text(ctx, row ? short(row.symbol, 11) : '—', 24, y, palette.text, 10);
      text(ctx, row ? short(row.status, 20) : 'AWAITING TELEMETRY', 125, y, row ? palette.green : palette.muted, 9);
      text(ctx, row ? `${short(row.entry, 9)} / ${short(row.sl, 9)}` : '—', 279, y, palette.muted, 10);
      text(ctx, row ? number(row.rr, 1) : '—', 445, y, palette.amber, 10);
      if (!row) { ctx.globalAlpha = .35; box(ctx, 24 + (t * 22 + i * 31 + variant * 15) % 440, y + 14, 15, 2, palette.cyan, 1); ctx.globalAlpha = 1; }
    }
  }
  function report(ctx, t, data, variant) {
    stat(ctx, 'DAY PNL · USDT', number(pnl(data)), 12, 42, 151, palette.green);
    stat(ctx, 'TRADES TODAY', number(data.trades_today ?? data.bot_entries_today, 0), 174, 42, 152);
    stat(ctx, 'ACCOUNT BALANCE · USDT', number(balance(data)), 337, 42, 163, palette.amber);
    box(ctx, 12, 100, 270, 122); text(ctx, 'ACTIVITY PROFILE / DEMO', 23, 117, palette.muted, 9);
    for (let i = 0; i < 17; i++) {
      const h = 15 + (phase(t * .2, i * .13 + variant * 2) + 1) * 30;
      box(ctx, 23 + i * 15, 208 - h, 9, h, i % 4 === 0 ? palette.cyan : '#2564a0', 2);
    }
    box(ctx, 293, 100, 207, 122); text(ctx, 'LATEST OFFICE EVENTS', 304, 117, palette.cyan, 9);
    const events = Array.isArray(data.events) ? data.events.slice(-4) : [];
    for (let i = 0; i < 4; i++) {
      dot(ctx, 308, 137 + i * 21, 2, i === 0 ? palette.green : palette.blue);
      text(ctx, events[i] ? short(events[i].state ?? events[i].message, 24) : 'WAITING FOR TELEMETRY', 317, 141 + i * 21, palette.muted, 9);
    }
  }
  function boss(ctx, t, data, variant) {
    // Source canvases are updated before boss screens, so the overview remains coherent.
    const tiles = variant ? ['trading', 'position', 'report', 'reviewer'] : ['market', 'neuro', 'risk', 'position'];
    tiles.forEach((role, index) => {
      const source = screens.get(role)[variant].canvas;
      const x = 12 + index % 2 * 251, y = 41 + Math.floor(index / 2) * 92;
      box(ctx, x, y, 237, 86, palette.panel, 6);
      ctx.save(); ctx.beginPath(); ctx.rect(x + 2, y + 2, 233, 82); ctx.clip();
      ctx.drawImage(source, 0, 0, WIDTH, HEIGHT, x + 2, y + 2, 233, 82); ctx.restore();
    });
    text(ctx, 'READ ONLY · '+short(data.bot_status ?? data.status ?? 'VISUAL DEMO', 26), 14, 229, palette.cyan, 9);
    text(ctx, 'FUTURES '+number(balance(data))+' USDT', 311, 229, palette.amber, 9);
  }
  const painters = { market, neuro, risk, trading, position, reviewer, report, boss };
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
      texture.generateMipmaps = false;
      texture.name = `office-monitor-${role}-${variant}`;
      variants.push({ canvas, context, texture, variant });
    }
    screens.set(role, variants);
  }
  function render(time, telemetry) {
    for (const role of roles) for (const screen of screens.get(role)) {
      frame(screen.context, role, screen.variant, telemetry);
      painters[role](screen.context, time, telemetry, screen.variant);
      screen.texture.needsUpdate = true;
    }
  }
  render(0, {});
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
      if (reducedMotion) {
        const signature = JSON.stringify([
          balance(data), pnl(data), positions(data), data.status, data.bot_status,
          data.risk_target_usdt, data.available_slots, data.leverage,
          data.trades_today, data.bot_entries_today,
          Array.isArray(data.manual_exposure) ? data.manual_exposure.slice(0, 3) : [],
          Array.isArray(data.setups) ? data.setups.slice(0, 4).map(row =>
            [row?.symbol, row?.status, row?.entry, row?.sl, row?.rr]) : [],
          Array.isArray(data.trades) ? data.trades.slice(-4).map(trade =>
            [trade?.state, trade?.plan?.symbol, trade?.plan?.entry, trade?.plan?.sl, trade?.plan?.rr]) : [],
          Array.isArray(data.events) ? data.events.slice(-4).map(event => event?.state ?? event?.message) : [],
        ]);
        if (!firstUpdate && signature === lastTelemetry) { lastUpdate = t; return false; }
        lastTelemetry = signature;
      }
      // Freeze decorative charts, while live numbers and status still refresh.
      render(reducedMotion ? 0 : t, data);
      lastUpdate = t; firstUpdate = false; updates++;
      return true;
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
        fps: FPS, reducedMotion: Boolean(reducedMotion), updates, disposed, syntheticCharts: true };
    }
  };
}
