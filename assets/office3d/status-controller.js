// Read-only bridge from the existing worker UI to office activity and speech.
// A visual role never starts work, approves a setup, or submits an order.
const ROLES = ['market', 'neuro', 'risk', 'trading', 'position', 'reviewer', 'report', 'boss'];
const FRESH_MS = 120000;
const STATES = new Set([
  'IDLE', 'WORKING', 'BREAK', 'WAITING', 'SCREENING', 'REPLACEMENT_SCREENING',
  'COINS_SELECTED', 'REPLACEMENT_SELECTED', 'MARKET_DATA', 'ANALYSIS', 'ANALYZING',
  'ANALYZING_COIN_1', 'ANALYZING_COIN_2', 'VALIDATING', 'LONG', 'SHORT', 'HOLD',
  'SETUP_READY', 'APPROVED', 'USER_REJECTED', 'REJECTED', 'REVIEWING',
  'REVIEW_REQUIRED', 'APPROVAL_PENDING', 'AWAITING_APPROVAL', 'DRY_RUN_READY',
  'ORDER_READY', 'ORDER_PREPARATION', 'PREPARING_ORDER', 'ENTRY_PREPARING',
  'ENTRY_PENDING', 'ENTRY_SUBMITTED', 'POSITION_OPEN', 'MONITORING', 'CLOSED',
  'REPORTING', 'GENERATING_REPORT', 'REPORT_GENERATION', 'ERROR', 'LOCKED',
  'INSUFFICIENT_ACTIONABLE_SETUPS', 'NEUROAPI_NOT_CONFIGURED', 'NEUROAPI_UNCHECKED',
  'NEUROAPI_CONNECTED', 'NEUROAPI_UNAVAILABLE', 'NEEDS_REVIEW', 'OFFLINE', 'ONLINE'
]);
const IDLE_SPEECH = {
  market: 'Menunggu kandidat market.', neuro: 'Menunggu analisis baru.',
  risk: 'Menunggu setup untuk divalidasi.', trading: 'Menunggu tiket untuk ditinjau.',
  position: 'Belum ada posisi untuk dipantau.', reviewer: 'Menunggu setup untuk ditinjau.',
  report: 'Menunggu hasil untuk dirangkum.', boss: 'Belum ada divisi aktif.'
};

function statusValue(value) {
  if (typeof value !== 'string' || value.length > 64) return null;
  const state = value.trim().toUpperCase();
  return STATES.has(state) ? state : null;
}
function timestampValue(value) {
  const at = typeof value === 'string' ? Date.parse(value) :
    typeof value === 'number' && Number.isFinite(value) ? (value < 1e12 ? value * 1000 : value) : NaN;
  return Number.isFinite(at) && at > 0 && at <= Date.now() + 30000 ? Math.min(at, Date.now()) : null;
}
function countValue(value) {
  return Number.isInteger(value) && value >= 0 && value <= 10000 ? value : null;
}
function roleValue(agent) {
  if (typeof agent !== 'string' || agent.length > 80) return null;
  const name = agent.toLowerCase().replace(/[^a-z]/g, '');
  if (/^(market|marketanalyst|marketanalysis)$/.test(name)) return 'market';
  if (/^(neuro|neurobro|neuroapi|analysis|analyst)$/.test(name)) return 'neuro';
  if (/^(risk|riskmanager)$/.test(name)) return 'risk';
  if (/^(trading|trader|tradingdesk|orderdesk)$/.test(name)) return 'trading';
  if (/^(position|positionmanager|positionmonitor)$/.test(name)) return 'position';
  if (/^(reviewer|review|approval)$/.test(name)) return 'reviewer';
  if (/^(report|reporter|reporting|reports)$/.test(name)) return 'report';
  if (/^(boss|harun|coordinator)$/.test(name)) return 'boss';
  return null;
}
function fresh(at, now) { return at !== null && now >= at && now - at < FRESH_MS; }
function iso(at) { return at === null ? null : new Date(at).toISOString(); }
function copyRole(value) { return { ...value }; }
function textValue(value, limit = 80) {
  return typeof value === 'string' ? value.replace(/[\u0000-\u001f\u007f]/g, ' ').slice(0, limit) : null;
}
function decimalValue(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  return typeof value === 'string' && value.length <= 64 && /^-?\d+(?:\.\d+)?$/.test(value) ? value : null;
}
function tradeSummary(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  let plan = null;
  if (value.plan && typeof value.plan === 'object' && !Array.isArray(value.plan)) {
    plan = { symbol: textValue(value.plan.symbol, 32), side: ['LONG', 'SHORT', 'HOLD'].includes(value.plan.side) ? value.plan.side : null };
    for (const key of ['entry', 'tp', 'sl', 'quantity', 'execution_quantity', 'risk', 'risk_target_usdt', 'rr']) plan[key] = decimalValue(value.plan[key]);
  }
  return { id: textValue(value.id, 160), day: textValue(value.day, 10), source: textValue(value.source, 64),
    state: statusValue(value.state), created: iso(timestampValue(value.created)),
    closed_day: textValue(value.closed_day, 10), pnl: decimalValue(value.pnl), plan };
}
function eventSummaries(events) {
  const summaries = events.slice(-20).map(event => ({ at: iso(event.at), state: event.state, agent: event.agent, message: event.message }));
  const encoder = new TextEncoder();
  while (summaries.length && encoder.encode(JSON.stringify(summaries)).byteLength > 8192) summaries.shift();
  return summaries;
}
function copyTelemetry(value) {
  return { ...value, events: (value.events ?? []).map(event => ({ ...event })),
    trades: (value.trades ?? []).map(trade => ({ ...trade, plan: trade.plan ? { ...trade.plan } : null })) };
}

export function createStatusController({ onChange } = {}) {
  const win = typeof window === 'undefined' ? null : window;
  const doc = typeof document === 'undefined' ? null : document;
  let disposed = false, observer = null, expiryTimer = null, expiryAt = null;
  let snapshot = null, ui = null, roles = {}, telemetry = {}, signature = '';
  let received = 0, malformed = 0;

  function normalize(payload) {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
    const status = statusValue(payload.status);
    const activePositions = countValue(payload.active_positions);
    const pendingOrders = countValue(payload.pending_orders);
    if (!status && activePositions === null && pendingOrders === null) return null;
    const receivedAt = Date.now();
    const suppliedTime = payload.generated_at ?? payload.checked_at ?? payload.timestamp;
    const timestamp = suppliedTime === undefined ? receivedAt : timestampValue(suppliedTime);
    if (timestamp === null) return null;
    const events = Array.isArray(payload.events) ? payload.events.slice(-100).flatMap(event => {
      if (!event || typeof event !== 'object' || Array.isArray(event)) return [];
      const state = statusValue(event.state ?? event.status), at = timestampValue(event.at ?? event.timestamp);
      if (!state || at === null) return [];
      return [{ state, at, role: roleValue(event.agent), agent: textValue(event.agent), message: textValue(event.message, 160) }];
    }) : [];
    const balance = decimalValue(payload.balance);
    const wallet = decimalValue(payload.usdt_wallet_balance) ?? balance;
    const trades = Array.isArray(payload.trades) ? payload.trades.slice(-20).map(tradeSummary).filter(Boolean) : [];
    return { status, activePositions, pendingOrders, timestamp, receivedAt, events,
      display: { balance, usdt_wallet_balance: wallet, pnl_today: decimalValue(payload.pnl_today),
        trades_today: countValue(payload.trades_today), generated_at: iso(timestamp),
        events: eventSummaries(events), trades } };
  }

  function sampleUI() {
    if (!doc || disposed) return;
    const menu = doc.getElementById('robotMenu');
    const panel = doc.getElementById('robotStatus');
    const workerMenu = doc.getElementById('neuroapiMenu');
    const menuMatch = menu?.textContent?.match(/^ROBOT\s+(ON|OFF)\s*[·|]\s*([A-Z0-9_]+)\s*$/);
    const panelText = panel?.textContent ?? '';
    const panelStatus = statusValue(panelText.match(/BOT STATUS:\s*([A-Z0-9_]+)/)?.[1]);
    const menuStatus = statusValue(menuMatch?.[2]);
    // Failed polls repaint retained robotData; those DOM writes are not a new worker heartbeat.
    // NEUROAPI_UNCHECKED describes provider health and does not imply the worker is offline.
    if (/^WORKER\s+OFFLINE\b/.test(workerMenu?.textContent ?? '') ||
        (menu && /BELUM TERHUBUNG/.test(menu.textContent ?? ''))) {
      ui = { status: 'OFFLINE', robotOn: null, waitReason: null, activePositions: null, timestamp: Date.now() };
      refresh(); return;
    }
    if (menuMatch && !menuStatus) {
      ui = { status: 'OFFLINE', robotOn: null, waitReason: null, activePositions: null, timestamp: Date.now() };
      refresh(); return;
    }
    if (!menuStatus && !panelStatus) { ui = null; refresh(); return; }
    const status = menuStatus ?? panelStatus;
    const wait = panelStatus === status ? panelText.match(/\bWAIT:\s*([A-Z0-9_]{1,80})/)?.[1] : null;
    const positions = panelText.match(/RUNNING FUTURES:\s*(\d+)\s*\//)?.[1];
    ui = {
      status, robotOn: menuMatch ? menuMatch[1] === 'ON' : null,
      waitReason: wait ?? null, activePositions: positions === undefined ? null : countValue(Number(positions)),
      timestamp: Date.now()
    };
    refresh();
  }

  function nextExpiry(deadlines, now) {
    const next = deadlines.filter(at => at !== null && at > now).sort((a, b) => a - b)[0] ?? null;
    if (next === expiryAt) return;
    if (expiryTimer !== null) clearTimeout(expiryTimer);
    expiryAt = next; expiryTimer = null;
    if (next !== null && !disposed) {
      expiryTimer = setTimeout(() => { expiryAt = null; expiryTimer = null; refresh(); }, Math.max(1, next - now + 1));
      expiryTimer?.unref?.();
    }
  }

  function refresh() {
    if (disposed) return;
    const now = Date.now();
    const uiFresh = ui && fresh(ui.timestamp, now);
    const snapshotFresh = snapshot && fresh(snapshot.timestamp, now);
    const recentEvents = snapshot?.events.filter(event => fresh(event.at, now)) ?? [];
    const latestEvent = recentEvents.reduce((latest, event) => !latest || event.at >= latest.at ? event : latest, null);
    // Coordinator UI reflects v7 work; a paper snapshot can still say REJECTED from an older cycle.
    const source = uiFresh ? 'robot-ui' : snapshotFresh ? 'workerSnapshot' : latestEvent ? 'worker-event' : 'unknown';
    const status = uiFresh ? ui.status : snapshotFresh ? snapshot.status ?? latestEvent?.state : latestEvent?.state;
    const at = uiFresh ? ui.timestamp : snapshotFresh ? snapshot.timestamp : latestEvent?.at ?? null;
    const known = Boolean(status && status !== 'OFFLINE');
    const robotOn = uiFresh ? ui.robotOn : null;
    // The existing UI overlays a current account count onto its paper snapshot before dispatch.
    const countFresh = status !== 'OFFLINE' && snapshot && fresh(snapshot.receivedAt, now) && (snapshotFresh || uiFresh);
    const positions = countFresh && snapshot.activePositions !== null ? snapshot.activePositions :
      uiFresh ? ui.activePositions : null;
    const pendingOrders = status !== 'OFFLINE' && snapshotFresh ? snapshot.pendingOrders : null;
    const nextRoles = Object.fromEntries(ROLES.map(role => [role, {
      state: 'IDLE', active: false, known, timestamp: known ? iso(at) : null,
      speech: known ? IDLE_SPEECH[role] : 'Menunggu status…'
    }]));
    function work(role, speech, timestamp = at) {
      nextRoles[role] = { state: 'WORKING', active: true, known: true, speech, timestamp: iso(timestamp) };
    }
    if (known && robotOn !== false) {
      switch (status) {
        case 'SCREENING': case 'REPLACEMENT_SCREENING':
          work('market', 'Memindai kandidat market.');
          work('neuro', 'Menyeleksi kandidat coin.'); break;
        case 'MARKET_DATA': work('market', 'Membaca data market.'); break;
        case 'ANALYSIS': case 'ANALYZING': case 'ANALYZING_COIN_1': case 'ANALYZING_COIN_2':
          work('neuro', 'Menganalisis setup.'); break;
        case 'VALIDATING': work('risk', 'Memeriksa quantity dan batas risiko.'); break;
        case 'SETUP_READY': case 'REVIEWING': case 'REVIEW_REQUIRED':
        case 'APPROVAL_PENDING': case 'AWAITING_APPROVAL':
          work('reviewer', 'Setup menunggu tinjauan.');
          nextRoles.risk.speech = 'Validasi setup selesai.'; break;
        case 'DRY_RUN_READY': case 'ORDER_READY': case 'ORDER_PREPARATION':
        case 'PREPARING_ORDER': case 'ENTRY_PREPARING':
          work('trading', 'Meninjau rencana order yang dilaporkan.'); break;
        case 'ENTRY_PENDING': case 'ENTRY_SUBMITTED':
          work('trading', 'Mengamati status order yang dilaporkan.'); break;
        case 'REPORTING': case 'GENERATING_REPORT': case 'REPORT_GENERATION':
          work('report', 'Merangkum hasil yang dilaporkan.'); break;
        case 'WORKING': {
          const latestByRole = new Map();
          for (const event of recentEvents) {
            if (event.role && event.role !== 'boss' && (!latestByRole.has(event.role) || latestByRole.get(event.role).at <= event.at)) latestByRole.set(event.role, event);
          }
          for (const [role, event] of latestByRole) {
            if (event.state === 'WORKING') work(role, 'Mengerjakan tugas yang dilaporkan.', event.at);
            else if (event.state === 'BREAK') nextRoles[role].speech = 'Sedang istirahat.';
          }
          break;
        }
        case 'BREAK': for (const role of ROLES) nextRoles[role].speech = 'Sedang istirahat.'; break;
        case 'REJECTED': case 'USER_REJECTED':
          nextRoles.risk.speech = 'Setup ditolak; menunggu tugas baru.';
          nextRoles.reviewer.speech = 'Belum ada setup untuk disetujui.'; break;
        case 'HOLD': nextRoles.neuro.speech = 'HOLD; menunggu kandidat berikutnya.'; break;
        case 'APPROVED': nextRoles.reviewer.speech = 'Review tersimpan; tiket perlu dikirim manual.'; break;
        case 'CLOSED': nextRoles.report.speech = 'Hasil posisi selesai tersedia.'; break;
      }
    }
    if (uiFresh && status === 'WAITING') {
      if (ui.waitReason === 'SCREENING_COMPLETE_ANALYSIS_PENDING') nextRoles.neuro.speech = 'Menunggu giliran analisis.';
      if (ui.waitReason === 'SCREENING_REPLACEMENT_REQUIRED') nextRoles.market.speech = 'Menunggu screening pengganti.';
      if (ui.waitReason === 'ROBOT_LIFECYCLE_NEEDS_REVIEW') nextRoles.reviewer.speech = 'Lifecycle lama perlu ditinjau.';
    }
    if (positions === 0) nextRoles.position = { state: 'IDLE', active: false, known: true, timestamp: iso(at), speech: IDLE_SPEECH.position };
    if (positions > 0) work('position', `Memantau ${positions} posisi yang dilaporkan.`, countFresh ? snapshot.receivedAt : ui.timestamp);
    else if (positions === null && known && ['POSITION_OPEN', 'MONITORING'].includes(status)) work('position', 'Memantau posisi yang dilaporkan.');
    const activeCount = ROLES.filter(role => role !== 'boss' && nextRoles[role].active).length;
    if (activeCount) work('boss', `Mengawasi ${activeCount} divisi aktif.`);
    const nextTelemetry = {
      balance: countFresh ? snapshot.display.balance : null,
      usdt_wallet_balance: countFresh ? snapshot.display.usdt_wallet_balance : null,
      pnl_today: snapshot?.display.pnl_today ?? null,
      active_positions: positions, pending_orders: pendingOrders,
      trades_today: snapshot?.display.trades_today ?? null,
      generated_at: snapshot?.display.generated_at ?? null,
      events: snapshot?.display.events ?? [], trades: snapshot?.display.trades ?? [],
      status: status ?? 'UNKNOWN', robotOn, waitReason: uiFresh ? ui.waitReason : null,
      activePositions: positions, pendingOrders, source, updatedAt: iso(at),
      stale: Boolean((snapshot && status === 'OFFLINE') || ((snapshot || ui) && !uiFresh && !snapshotFresh && !latestEvent)),
      connected: source !== 'unknown' && status !== 'OFFLINE'
    };
    const nextSignature = JSON.stringify({ roles: nextRoles, telemetry: nextTelemetry });
    roles = nextRoles; telemetry = nextTelemetry;
    nextExpiry([
      uiFresh ? ui.timestamp + FRESH_MS : null,
      snapshotFresh ? snapshot.timestamp + FRESH_MS : null,
      countFresh ? snapshot.receivedAt + FRESH_MS : null,
      ...recentEvents.map(event => event.at + FRESH_MS)
    ], now);
    if (signature !== nextSignature) {
      signature = nextSignature;
      if (typeof onChange === 'function') {
        try { onChange({ roles: Object.fromEntries(ROLES.map(role => [role, copyRole(roles[role])])), telemetry: copyTelemetry(telemetry) }); }
        catch (_) { /* Visual consumers cannot interrupt the worker UI. */ }
      }
    }
  }

  function onSnapshot(event) {
    if (disposed) return;
    received += 1;
    try {
      const next = normalize(event?.detail);
      if (!next) { malformed += 1; return; }
      snapshot = next; refresh();
    } catch (_) { malformed += 1; }
  }
  function relevantNode(node, includeChildren = false) {
    if (!node) return false;
    const element = node.nodeType === 1 ? node : node.parentElement;
    return Boolean(element?.matches?.('#robotMenu, #robotStatus, #neuroapiMenu') || element?.closest?.('#robotMenu, #robotStatus, #neuroapiMenu') ||
      (includeChildren && element?.querySelector?.('#robotMenu, #robotStatus, #neuroapiMenu')));
  }
  function ensureFresh() { if (expiryAt !== null && Date.now() >= expiryAt) refresh(); }

  win?.addEventListener('workerSnapshot', onSnapshot);
  if (doc && typeof MutationObserver !== 'undefined') {
    observer = new MutationObserver(records => {
      if (records.some(record => relevantNode(record.target) || [...record.addedNodes, ...record.removedNodes].some(node => relevantNode(node, true)))) sampleUI();
    });
    for (const id of ['menuDrawer', 'infoPanel']) {
      const root = doc.getElementById(id);
      if (root) observer.observe(root, { childList: true, subtree: true, characterData: true });
    }
  }
  sampleUI(); refresh();
  return {
    getRole(role) {
      ensureFresh();
      return ROLES.includes(role) ? copyRole(roles[role]) : { state: 'IDLE', speech: 'Menunggu status…', active: false, timestamp: null, known: false };
    },
    getTelemetry() { ensureFresh(); return copyTelemetry(telemetry); },
    diagnostics() {
      ensureFresh();
      return { disposed, observerActive: Boolean(observer && !disposed), receivedSnapshots: received, malformedPayloads: malformed,
        eventCount: snapshot?.events.length ?? 0, telemetry: copyTelemetry(telemetry),
        roleStates: Object.fromEntries(ROLES.map(role => [role, roles[role].state])) };
    },
    dispose() {
      disposed = true; win?.removeEventListener('workerSnapshot', onSnapshot); observer?.disconnect();
      if (expiryTimer !== null) clearTimeout(expiryTimer);
      expiryTimer = null; expiryAt = null;
    }
  };
}
