// Read-only animation bridge. Worker facts determine activity; avatars never initiate work.
const ROLES = ['market', 'neuro', 'risk', 'trading', 'position', 'reviewer', 'report', 'boss'];
const PHASE_FRESH_MS = 120000, ACCOUNT_FRESH_MS = 45000;
const PHASES = new Set(['OFF', 'IDLE', 'WAITING', 'SCREENING', 'ANALYZING', 'VALIDATING', 'HOLD',
  'REJECTED', 'EXECUTION_BLOCKED', 'EXECUTING', 'NEEDS_REVIEW', 'READY_FOR_EXECUTION',
  'ENTRY_PENDING', 'POSITION_PROTECTED', 'CLOSED', 'INSUFFICIENT_ACTIONABLE_SETUPS', 'OFFLINE']);
const EMPLOYEE_STATES = new Set(['WORKING', 'IDLE', 'WAITING', 'BLOCKED', 'OFFLINE']);
const IDLE_SPEECH = {
  market: 'Menunggu screening.', neuro: 'Menunggu analisis.', risk: 'Menunggu validasi risiko.',
  trading: 'Menunggu adapter Binance.', position: 'Tidak ada posisi aktif.',
  reviewer: 'Menunggu hasil pipeline.', report: 'Menunggu laporan Binance.', boss: 'Mengawasi status kantor.'
};
const WORK_SPEECH = {
  market: 'Screening kandidat coin.', neuro: 'Menganalisis setup.', risk: 'Memvalidasi risiko.',
  trading: 'Menjalankan adapter Binance.', reviewer: 'Memeriksa hasil pipeline.', report: 'Membaca laporan Binance.'
};
function text(value, limit = 80) {
  return typeof value === 'string' ? value.replace(/[\u0000-\u001f\u007f]/g, ' ').slice(0, limit) : null;
}
function state(value, allowed) {
  const candidate = text(value, 64)?.trim().toUpperCase();
  return allowed.has(candidate) ? candidate : null;
}
function time(value) {
  const candidate = typeof value === 'string' ? Date.parse(value) : NaN;
  return Number.isFinite(candidate) && candidate > 0 && candidate <= Date.now() + 30000 ? Math.min(candidate, Date.now()) : null;
}
function decimal(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  return typeof value === 'string' && value.length <= 64 && /^-?\d+(?:\.\d+)?$/.test(value) ? value : null;
}
function count(value) { return Number.isInteger(value) && value >= 0 && value <= 10000 ? value : null; }
function fresh(at, now, ttl) { return at !== null && now >= at && now - at < ttl; }
function iso(at) { return at === null ? null : new Date(at).toISOString(); }
function copyTelemetry(value) {
  return { ...value, events: (value.events ?? []).map(event => ({ ...event })),
    trades: [], employees: (value.employees ?? []).map(employee => ({ ...employee })),
    execution_gateway: value.execution_gateway ? { ...value.execution_gateway } : null,
    last_decision: value.last_decision ? { ...value.last_decision } : null,
    manual_exposure: [...(value.manual_exposure ?? [])] };
}

export function createStatusController({ onChange } = {}) {
  const win = typeof window === 'undefined' ? null : window;
  let snapshot = null, roles = {}, telemetry = {}, signature = '';
  let disposed = false, expiryTimer = null, expiryAt = null, received = 0, malformed = 0;

  function normalize(payload) {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload) ||
        payload.schema_version !== 2 || payload.source !== 'BINANCE_FUTURES') return null;
    const status = state(payload.status, PHASES), generatedAt = time(payload.generated_at);
    if (!status || generatedAt === null) return null;
    const receivedAt = Date.now();
    const phaseAt = payload.robot_checked_at === undefined ? generatedAt : time(payload.robot_checked_at);
    const accountAt = payload.account_checked_at === undefined ? generatedAt : time(payload.account_checked_at);
    const reportsAt = payload.reports_checked_at === undefined ? generatedAt : time(payload.reports_checked_at);
    const employees = Array.isArray(payload.employees) ? payload.employees.slice(0, 50).flatMap(employee => {
      if (!employee || typeof employee !== 'object' || !ROLES.includes(employee.id)) return [];
      const status = state(employee.status, EMPLOYEE_STATES);
      return status ? [{ id: employee.id, name: text(employee.name), status,
        at: employee.checked_at === undefined ? phaseAt : time(employee.checked_at) }] : [];
    }) : [];
    const events = Array.isArray(payload.events) ? payload.events.slice(-20).flatMap(event => {
      if (!event || typeof event !== 'object') return [];
      const at = time(event.at), message = text(event.message, 160), agent = text(event.agent, 80), status = text(event.state, 64);
      return at !== null && message !== null && agent !== null && status !== null ? [{ at: iso(at), state: status, agent, message }] : [];
    }) : [];
    const gateway = payload.execution_gateway;
    const executionGateway = gateway && typeof gateway === 'object' && !Array.isArray(gateway) ?
      { connected: gateway.connected === true, status: text(gateway.status, 64), failure_code: text(gateway.failure_code, 80) } :
      { connected: false, status: 'NOT_CONNECTED', failure_code: null };
    return { status, generatedAt, phaseAt, accountAt, reportsAt, receivedAt, employees, events,
      robotOn: typeof payload.robot_on === 'boolean' ? payload.robot_on : null,
      positions: count(payload.active_positions), pendingOrders: count(payload.pending_orders),
      executionGateway, balance: decimal(payload.usdt_wallet_balance ?? payload.balance),
      decision: payload.last_decision && typeof payload.last_decision === 'object' && !Array.isArray(payload.last_decision) ? {
        symbol: text(payload.last_decision.symbol, 32), status: text(payload.last_decision.status, 64),
        failure_code: text(payload.last_decision.failure_code, 80)
      } : null,
      pnl: decimal(payload.pnl_today), tradesToday: count(payload.trades_today),
      slots: count(payload.available_slots), risk: decimal(payload.risk_target_usdt),
      manual: Array.isArray(payload.manual_exposure) ? payload.manual_exposure.slice(0, 30).map(value => text(value, 32)).filter(Boolean) : [] };
  }
  function schedule(deadlines, now) {
    const next = deadlines.filter(value => value !== null && value > now).sort((a, b) => a - b)[0] ?? null;
    if (next === expiryAt) return;
    if (expiryTimer !== null) clearTimeout(expiryTimer);
    expiryAt = next; expiryTimer = null;
    if (next !== null && !disposed) {
      expiryTimer = setTimeout(() => { expiryTimer = null; expiryAt = null; refresh(); }, Math.max(1, next - now + 1));
      expiryTimer?.unref?.();
    }
  }
  function refresh() {
    if (disposed) return;
    const now = Date.now();
    const transportFresh = Boolean(snapshot && snapshot.status !== 'OFFLINE' && fresh(snapshot.receivedAt, now, PHASE_FRESH_MS));
    const phaseFresh = transportFresh && fresh(snapshot.phaseAt, now, PHASE_FRESH_MS);
    const accountFresh = transportFresh && fresh(snapshot.accountAt, now, ACCOUNT_FRESH_MS);
    const reportsFresh = transportFresh && fresh(snapshot.reportsAt, now, PHASE_FRESH_MS);
    const positions = accountFresh ? snapshot.positions : null;
    const status = phaseFresh ? snapshot.status : 'OFFLINE';
    const robotRunning = phaseFresh && snapshot.robotOn === true && status !== 'OFF';
    const robotOff = phaseFresh && (snapshot.robotOn === false || status === 'OFF');
    const nextRoles = Object.fromEntries(ROLES.map(role => [role, {
      state: 'IDLE', active: false, known: phaseFresh, timestamp: phaseFresh ? iso(snapshot.phaseAt) : null,
      speech: phaseFresh ? IDLE_SPEECH[role] : 'Menunggu status worker…'
    }]));
    const gateway = snapshot?.executionGateway;
    const blocked = !gateway?.connected || ['BLOCKED', 'NOT_CONNECTED', 'UNAVAILABLE'].includes(gateway.status) ||
      gateway.failure_code === 'BINANCE_ORDER_GATEWAY_NOT_CONNECTED' || status === 'EXECUTION_BLOCKED';
    function work(role, speech, at = snapshot.phaseAt) {
      nextRoles[role] = { state: 'WORKING', active: true, known: true, timestamp: iso(at), speech };
    }
    // Robot activity stops with OFF; read-only account telemetry remains independent.
    if (robotRunning) {
      const phaseRole = { SCREENING: 'market', ANALYZING: 'neuro', VALIDATING: 'risk', EXECUTING: 'trading' }[status];
      if (phaseRole && (phaseRole !== 'trading' || !blocked)) work(phaseRole, WORK_SPEECH[phaseRole]);
      for (const employee of snapshot.employees) {
        if (employee.status !== 'WORKING' || !fresh(employee.at, now, PHASE_FRESH_MS) || ['boss', 'position'].includes(employee.id)) continue;
        if (employee.id === 'trading' && blocked) continue;
        work(employee.id, WORK_SPEECH[employee.id], employee.at);
      }
      if (status === 'HOLD') nextRoles.neuro.speech = 'HOLD; menunggu kandidat berikutnya.';
      if (status === 'REJECTED') nextRoles.risk.speech = 'Setup tidak memenuhi validasi.';
      if (status === 'NEEDS_REVIEW') nextRoles.trading.speech = 'Status adapter belum pasti.';
    }
    if (phaseFresh && blocked) {
      nextRoles.trading = { state: 'IDLE', active: false, known: true, timestamp: iso(snapshot.phaseAt), speech: 'Gateway Binance belum terhubung.' };
    }
    if (accountFresh && positions !== null) {
      const monitoring = robotRunning && positions > 0;
      nextRoles.position = { state: monitoring ? 'WORKING' : 'IDLE', active: monitoring, known: true,
        timestamp: iso(snapshot.accountAt), speech: monitoring ? `Memantau ${positions} posisi Binance.` : IDLE_SPEECH.position };
    } else nextRoles.position = { state: 'IDLE', active: false, known: false, timestamp: null, speech: 'Menunggu data posisi Binance…' };
    if (robotOff) for (const role of ROLES) {
      nextRoles[role] = { state: 'IDLE', active: false, known: true, timestamp: iso(snapshot.phaseAt),
        speech: role === 'position' ? 'Robot OFF; posisi tetap di Binance.' : 'Robot OFF.' };
    }
    const active = ROLES.filter(role => role !== 'boss' && nextRoles[role].active).length;
    if (active) work('boss', `Mengawasi ${active} divisi aktif.`, phaseFresh ? snapshot.phaseAt : snapshot.accountAt);
    const nextTelemetry = {
      source: transportFresh ? 'workerSnapshot' : 'unknown', schema_version: 2,
      balance: accountFresh ? snapshot.balance : null, usdt_wallet_balance: accountFresh ? snapshot.balance : null,
      pnl_today: reportsFresh ? snapshot.pnl : null, trades_today: reportsFresh ? snapshot.tradesToday : null,
      active_positions: positions, activePositions: positions,
      pending_orders: phaseFresh ? snapshot.pendingOrders : null, pendingOrders: phaseFresh ? snapshot.pendingOrders : null,
      generated_at: snapshot ? iso(snapshot.generatedAt) : null, robot_checked_at: phaseFresh ? iso(snapshot.phaseAt) : null,
      account_checked_at: accountFresh ? iso(snapshot.accountAt) : null, reports_checked_at: reportsFresh ? iso(snapshot.reportsAt) : null,
      status, robotOn: phaseFresh ? snapshot.robotOn : null, robot_on: phaseFresh ? snapshot.robotOn : null,
      connected: transportFresh, stale: Boolean(snapshot && (!phaseFresh || !transportFresh)),
      receivedAt: snapshot ? iso(snapshot.receivedAt) : null, updatedAt: phaseFresh ? iso(snapshot.phaseAt) : null,
      events: transportFresh ? snapshot.events : [], trades: [],
      employees: phaseFresh ? snapshot.employees.map(({ id, name, status }) => ({ id, name, status: robotOff ? 'OFF' : status })) : [],
      execution_gateway: phaseFresh ? gateway : null,
      last_decision: phaseFresh ? snapshot.decision : null,
      available_slots: phaseFresh ? snapshot.slots : null, risk_target_usdt: phaseFresh ? snapshot.risk : null,
      manual_exposure: accountFresh ? snapshot.manual : [], waitReason: null
    };
    roles = nextRoles; telemetry = nextTelemetry;
    schedule([
      transportFresh ? snapshot.receivedAt + PHASE_FRESH_MS : null,
      phaseFresh ? snapshot.phaseAt + PHASE_FRESH_MS : null,
      accountFresh ? snapshot.accountAt + ACCOUNT_FRESH_MS : null,
      reportsFresh ? snapshot.reportsAt + PHASE_FRESH_MS : null,
      ...snapshot?.employees.filter(employee => fresh(employee.at, now, PHASE_FRESH_MS)).map(employee => employee.at + PHASE_FRESH_MS) ?? []
    ], now);
    const nextSignature = JSON.stringify({ roles, telemetry });
    if (signature !== nextSignature) {
      signature = nextSignature;
      if (typeof onChange === 'function') {
        try { onChange({ roles: Object.fromEntries(ROLES.map(role => [role, { ...roles[role] }])), telemetry: copyTelemetry(telemetry) }); }
        catch (_) { /* An animation consumer cannot interrupt worker updates. */ }
      }
    }
  }
  function receive(event) {
    if (disposed) return;
    received++;
    try {
      const candidate = normalize(event?.detail);
      if (!candidate) { malformed++; return; }
      snapshot = candidate; refresh();
    } catch (_) { malformed++; }
  }
  function ensureFresh() { if (expiryAt !== null && Date.now() >= expiryAt) refresh(); }
  win?.addEventListener('workerSnapshot', receive); refresh();
  return {
    getRole(role) {
      ensureFresh();
      return { ...(ROLES.includes(role) ? roles[role] : { state: 'IDLE', active: false, known: false, timestamp: null, speech: 'Menunggu status worker…' }) };
    },
    getTelemetry() { ensureFresh(); return copyTelemetry(telemetry); },
    diagnostics() {
      ensureFresh();
      return { disposed, observerActive: false, receivedSnapshots: received, malformedPayloads: malformed,
        eventCount: snapshot?.events.length ?? 0, telemetry: copyTelemetry(telemetry),
        roleStates: Object.fromEntries(ROLES.map(role => [role, roles[role].state])) };
    },
    dispose() {
      if (disposed) return;
      disposed = true; win?.removeEventListener('workerSnapshot', receive);
      if (expiryTimer !== null) clearTimeout(expiryTimer);
      expiryTimer = null; expiryAt = null;
    }
  };
}
