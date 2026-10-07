/* Private worker dashboard. Credentials stay in tab memory; exchange work belongs to the server. */
(() => {
  'use strict';
  const panel = document.getElementById('infoPanel');
  const content = document.getElementById('panelContent');
  const title = document.getElementById('panelTitle');
  const drawer = document.getElementById('menuDrawer');
  const menuToggle = document.getElementById('menuToggle');
  const views = { robot: 'ROBOT TRADING ON / OFF', calendar: 'KALENDER PNL' };
  const CALENDAR_START = '2026-10-01';
  const FRESH_MS = 120000;
  let origin = '', configuredOrigin = '', token = '', timer = null, snapshot = null;
  let view = 'robot', generation = 0, revision = 0, polling = false, saving = false;
  let riskDraft = null, resetRiskOnRefresh = null;
  let online = false, refreshRequested = false, message = 'Hubungkan worker untuk membaca akun dan status robot.';
  let calendarMonth = null, selectedDay = null;

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text ?? '—').replace(/\b(?:nanda|ai)\b/gi, '').replace(/\s+/g, ' ').trim();
    if (className) node.className = className;
    return node;
  }
  function number(value, digits = 2) {
    if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value))) return '—';
    return Number(value).toLocaleString('en-US', { maximumFractionDigits: digits });
  }
  function timestamp(value) {
    const time = typeof value === 'string' ? Date.parse(value) : NaN;
    return Number.isFinite(time) && time <= Date.now() + 60000 ? time : null;
  }
  function fresh(value) { const time = timestamp(value); return time !== null && Date.now() - time >= -60000 && Date.now() - time < FRESH_MS; }
  function date(value) {
    const time = timestamp(value);
    return time === null ? 'Belum tersedia' : new Date(time).toLocaleString('id-ID', { dateStyle: 'short', timeStyle: 'medium', timeZone: 'Asia/Jakarta' });
  }
  function validOrigin(value) {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw Error('Gunakan origin HTTPS worker tanpa path atau kredensial.');
    return url.origin;
  }
  function validRisk(value) {
    const draft = value.trim(), match = /^(\d+)(?:\.(\d+))?$/.exec(draft);
    if (!match || draft.length > 64) throw Error('Risiko harus angka desimal lebih dari 0 sampai 100 USDT.');
    const whole = match[1].replace(/^0+/, '') || '0', fraction = match[2] || '';
    if (!/[1-9]/.test(whole+fraction) || whole.length > 3 ||
        (whole.length === 3 && (whole > '100' || (whole === '100' && /[1-9]/.test(fraction))))) {
      throw Error('Risiko harus lebih dari 0 dan maksimal 100 USDT.');
    }
    return draft;
  }
  const configUrl = new URL('worker-config.json', document.currentScript?.src || document.baseURI);
  fetch(configUrl, { credentials: 'omit', cache: 'no-store' }).then(response => response.ok ? response.json() : null).then(config => {
    if (config?.worker_origin) configuredOrigin = validOrigin(config.worker_origin);
    else if (location.protocol === 'https:') configuredOrigin = location.origin;
    const field = document.getElementById('apiOrigin');
    if (field && !field.value && document.activeElement !== field) field.value = configuredOrigin;
  }).catch(() => {});

  function validate(data) {
    if (!data || data.schema_version !== 2 || data.source !== 'BINANCE_FUTURES' || timestamp(data.generated_at) === null ||
        !data.robot || typeof data.robot.robot_on !== 'boolean' || !data.account || !data.reports || !data.position_history ||
        !Array.isArray(data.employees) || !Array.isArray(data.activity) || !Array.isArray(data.account.positions) || !Array.isArray(data.position_history.items)) {
      throw Error('Respons dashboard worker tidak sesuai kontrak.');
    }
    if (data.employees.length > 50 || data.activity.length > 1000 || data.account.positions.length > 1000 || data.position_history.items.length > 10000) throw Error('Respons dashboard terlalu besar.');
    return data;
  }
  function accountCurrent() { return online && snapshot?.account.status === 'CONNECTED' && fresh(snapshot.account.checked_at); }
  function reportsCurrent() { return online && ['AVAILABLE', 'PARTIAL'].includes(snapshot?.reports.status) && fresh(snapshot.reports.checked_at); }
  function publish() {
    const account = accountCurrent() ? snapshot.account : null;
    const reports = reportsCurrent() ? snapshot.reports : null;
    const detail = {
      source: 'BINANCE_FUTURES', schema_version: 2, status: online ? snapshot?.robot.bot_status ?? 'WAITING' : 'OFFLINE',
      robot_on: online ? snapshot?.robot.robot_on ?? null : null,
      robot_checked_at: online ? snapshot?.robot.checked_at ?? null : null,
      account_checked_at: account?.checked_at ?? null,
      reports_checked_at: reports?.checked_at ?? null,
      risk_target_usdt: online ? snapshot?.robot.risk_target_usdt ?? null : null,
      available_slots: online ? snapshot?.robot.available_slots ?? null : null,
      manual_exposure: account && Array.isArray(snapshot?.robot.manual_exposure) ? [...snapshot.robot.manual_exposure] : [],
      last_decision: online && snapshot?.robot.last_decision ? {
        symbol: snapshot.robot.last_decision.symbol ?? null,
        status: snapshot.robot.last_decision.status ?? null,
        failure_code: snapshot.robot.last_decision.failure_code ?? null
      } : null,
      balance: account?.usdt_wallet_balance ?? null, usdt_wallet_balance: account?.usdt_wallet_balance ?? null,
      active_positions: account?.active_positions ?? null, pnl_today: reports?.pnl_today_usdt ?? null,
      trades_today: reports?.trades_today ?? null, generated_at: online ? snapshot?.generated_at : new Date().toISOString(),
      events: online ? snapshot?.activity ?? [] : [], trades: [],
      employees: online ? snapshot?.employees ?? [] : [], execution_gateway: online ? snapshot?.robot.execution_gateway ?? null : null
    };
    window.dispatchEvent(new CustomEvent('workerSnapshot', { detail }));
  }
  function paintStats() {
    const account = accountCurrent() ? snapshot.account : null;
    const reports = reportsCurrent() ? snapshot.reports : null;
    const values = [number(account?.usdt_wallet_balance), number(reports?.pnl_today_usdt), number(reports?.trades_today, 0), number(account?.active_positions, 0)];
    document.querySelectorAll('.stats .stat b').forEach((node, index) => { node.textContent = values[index]; });
    document.querySelectorAll('.stats .stat b')[1]?.classList.toggle('positive', reports?.pnl_today_usdt !== null && reports?.pnl_today_usdt !== undefined && Number(reports.pnl_today_usdt) >= 0);
    const pnlLabel = document.querySelectorAll('.stats .stat span')[1];
    if (pnlLabel) pnlLabel.textContent = reports?.complete === false ? 'PNL Hari Ini *' : 'PNL Hari Ini';
    const pnlValue = document.querySelectorAll('.stats .stat b')[1];
    if (pnlValue) pnlValue.title = reports?.complete === false ? 'Data Binance parsial; rincian posisi ditutup tersedia pada Kalender PNL.' : '';
    document.querySelector('.brand small').textContent = !origin ? '● WORKER BELUM TERHUBUNG' : !online ? '● WORKER TIDAK TERJANGKAU' : !account ? '● AKUN BINANCE BELUM TERSEDIA' : '● BINANCE FUTURES · '+(snapshot.robot.robot_on ? 'ROBOT ON' : 'ROBOT OFF');
    const robotMenu = document.getElementById('robotMenu');
    robotMenu.dataset.robotOn = snapshot ? String(snapshot.robot.robot_on) : '';
    robotMenu.dataset.status = online ? snapshot?.robot.bot_status ?? 'WAITING' : 'OFFLINE';
  }
  function notice(text, tone = 'normal') { return element('p', text, 'dashboard-notice '+tone); }
  function section(label) {
    const node = element('section', undefined, 'dashboard-section'); node.append(element('h4', label)); return node;
  }
  function field(container, label, value) {
    const row = element('div', undefined, 'dashboard-field'); row.append(element('span', label), element('b', value)); container.append(row);
  }
  function source(container, data, unavailable = 'Data belum tersedia dari worker.') {
    const good = data && ['AVAILABLE', 'PARTIAL', 'CONNECTED'].includes(data.status);
    container.append(notice(good ? `Binance Futures · Diperiksa ${date(data.checked_at)}${data.complete === false ? ' · Data parsial' : ''}` : unavailable, good && fresh(data.checked_at) && online ? 'normal' : 'warning'));
    if (good && (!fresh(data.checked_at) || !online)) container.append(notice('Data terakhir sudah lama atau worker tidak terjangkau. Angka ini bukan pembaruan terkini.', 'warning'));
  }
  function gatewayNote(robot) {
    const gateway = robot?.execution_gateway;
    if (!online || !gateway) return notice('Status gateway belum tersedia dari worker.', 'warning');
    if (!gateway?.connected || ['NOT_CONNECTED', 'BLOCKED', 'UNAVAILABLE'].includes(gateway.status)) {
      return notice('Transport pengiriman order Binance belum tersedia · '+(gateway?.failure_code || 'BINANCE_ORDER_GATEWAY_NOT_CONNECTED')+'. Worker belum dapat mengirim order ke Binance.', 'warning');
    }
    return notice('Gateway eksekusi: '+(gateway.status ?? 'BELUM DIKETAHUI')+'. Riwayat Binance menjadi bukti transaksi yang terjadi.');
  }
  function renderRobot() {
    const robot = snapshot?.robot, account = snapshot?.account;
    const main = section('COORDINATOR 24/7');
    const actions = element('div', undefined, 'dashboard-actions');
    const toggle = element('button', 'ROBOT TRADING: '+(robot ? robot.robot_on ? 'ON' : 'OFF' : 'BELUM TERHUBUNG'));
    toggle.id = 'robotToggle'; toggle.disabled = !origin || !online || saving || !robot;
    toggle.setAttribute('aria-pressed', String(robot?.robot_on === true));
    toggle.onclick = () => saveSettings({ robot_on: !snapshot.robot.robot_on }); actions.append(toggle); main.append(actions);
    main.append(element('p', message, 'dashboard-message'));
    const riskControls = element('div', undefined, 'dashboard-connection dashboard-risk');
    const riskLabel = element('label', 'RISIKO PER SL TERMASUK FEE DAN CADANGAN SLIPPAGE (USDT, lebih dari 0 sampai 100)');
    const risk = element('input'); risk.id = 'robotRisk'; risk.type = 'number'; risk.inputMode = 'decimal';
    risk.min = '0'; risk.max = '100'; risk.step = 'any'; risk.value = riskDraft ?? robot?.risk_target_usdt ?? '5';
    risk.disabled = !origin || !online || saving || !robot; risk.oninput = () => { riskDraft = risk.value; };
    riskLabel.append(risk);
    const riskActions = element('div', undefined, 'dashboard-actions'), riskSave = element('button', 'SIMPAN RISIKO');
    riskSave.id = 'robotRiskSave'; riskSave.disabled = risk.disabled;
    riskSave.onclick = () => {
      try { riskDraft = risk.value; saveSettings({ risk_target_usdt: validRisk(risk.value) }); }
      catch (error) { message = error.message; paint(); }
    };
    riskActions.append(riskSave); riskControls.append(riskLabel, riskActions,
      notice('Ukuran posisi dihitung mendekati batas risiko, termasuk fee entry, fee exit, dan cadangan slippage exit 0,5%. Gap atau slippage yang melampaui cadangan serta funding dapat membuat kerugian aktual melebihi batas. Perubahan risiko berlaku hanya untuk analisis baru; setup dan intent yang sudah dibuat tetap memakai risiko sebelumnya.'));
    main.append(riskControls);
    const status = element('p', robot ? 'BOT STATUS: '+robot.bot_status+(robot.wait_reason ? ' · WAIT: '+robot.wait_reason : '')+(robot.failure_code ? ' · '+robot.failure_code : '') : 'Menunggu status worker.', 'dashboard-status');
    status.id = 'robotStatus'; status.setAttribute('role', 'status'); main.append(status);
    const metrics = element('div', undefined, 'dashboard-metrics');
    field(metrics, 'Risk per SL', number(robot?.risk_target_usdt)+' USDT');
    field(metrics, 'Posisi / kapasitas', number(account?.active_positions, 0)+' / 2');
    field(metrics, 'Slot tersedia', number(robot?.available_slots, 0));
    field(metrics, 'Entry bot hari ini', number(robot?.bot_entries_today, 0)+' / 2');
    main.append(metrics, gatewayNote(robot));
    main.append(notice('Maksimal 2 entry bot per hari WIB. Posisi bawaan dari hari sebelumnya dan posisi manual tetap memakai slot dari kapasitas 2 coin. Order entry yang belum terisi juga memakai slot.'));
    main.append(notice('ON menjalankan coordinator ketika slot tersedia. OFF menghentikan riset, pengiriman order baru, dan rekonsiliasi robot. Pemasangan TP/SL serta pembatalan order oleh robot juga berhenti. Posisi serta order yang sudah ada tetap di Binance dan dapat terisi; OFF tidak menutup posisi atau membatalkan order. Dashboard tetap dapat membaca saldo dan posisi.'));
    if (robot?.last_decision) main.append(notice('Keputusan terakhir: '+robot.last_decision.symbol+' · '+robot.last_decision.status+(robot.last_decision.failure_code ? ' · '+robot.last_decision.failure_code : '')));
    content.append(main);
    const current = section('AKUN FUTURES'); source(current, account);
    const details = element('div', undefined, 'dashboard-metrics');
    field(details, 'Wallet USDT', number(account?.usdt_wallet_balance)); field(details, 'USDT tersedia', number(account?.usdt_available_balance));
    field(details, 'Posisi manual', Array.isArray(robot?.manual_exposure) && robot.manual_exposure.length ? robot.manual_exposure.join(', ') : '—');
    current.append(details);
    if (account?.positions?.length) for (const row of account.positions) {
      const position = element('article', undefined, 'dashboard-row');
      position.append(element('strong', row.symbol+' · '+row.side), element('p', `Quantity ${number(row.quantity, 8)} · Entry ${number(row.entry_price, 8)} · Mark ${number(row.mark_price, 8)} · Unrealized PnL ${number(row.unrealized_pnl)} USDT`)); current.append(position);
    }
    else current.append(notice(account?.status === 'CONNECTED' ? 'Tidak ada posisi aktif yang dilaporkan Binance.' : 'Posisi aktif belum tersedia.'));
    content.append(current, renderConnection());
  }
  function renderConnection() {
    const details = element('details', undefined, 'dashboard-connection'); details.open = !origin;
    details.append(element('summary', 'Koneksi worker privat'));
    const originLabel = element('label', 'Origin HTTPS worker');
    const originField = element('input'); originField.id = 'apiOrigin'; originField.type = 'url'; originField.value = origin || configuredOrigin; originField.placeholder = 'https://worker-anda'; originField.autocomplete = 'url';
    originLabel.append(originField);
    const tokenLabel = element('label', 'Token kontrol worker');
    const tokenField = element('input'); tokenField.id = 'apiToken'; tokenField.type = 'password'; tokenField.autocomplete = 'off'; tokenField.placeholder = 'Token worker · bukan API key';
    tokenLabel.append(tokenField);
    const actions = element('div', undefined, 'dashboard-actions');
    const connect = element('button', 'HUBUNGKAN WORKER'); connect.id = 'apiConnect';
    connect.onclick = () => {
      try {
        const nextOrigin = validOrigin(originField.value.trim()), nextToken = tokenField.value.trim();
        if (nextToken.length < 32) throw Error('Token kontrol minimal 32 karakter.');
        disconnect(); origin = nextOrigin; token = nextToken; tokenField.value = ''; message = 'Menghubungkan worker…';
        paint(); poll(); timer = setInterval(poll, 5000);
      } catch (error) { message = error.message; const node = content.querySelector('.dashboard-message'); if (node) node.textContent = message; }
    };
    const stop = element('button', 'PUTUSKAN'); stop.id = 'apiDisconnect'; stop.disabled = !origin;
    stop.onclick = () => { disconnect(); message = 'Dashboard terputus. Worker tetap menjalankan status terakhirnya di VPS.'; paint(); };
    actions.append(connect, stop); details.append(originLabel, tokenLabel, actions, notice('Token kontrol hanya berada di memori tab. Kunci Binance dan NeuroAPI tetap di VPS.'));
    return details;
  }
  function wibDay(value = Date.now()) {
    return new Date(value + 7 * 60 * 60 * 1000).toISOString().slice(0, 10);
  }
  function calendarDate(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const at = Date.parse(value+'T00:00:00Z');
    return Number.isFinite(at) && new Date(at).toISOString().slice(0, 10) === value;
  }
  function decimal(value) {
    return typeof value === 'string' && value.length <= 64 && /^-?\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value));
  }
  function calendarData() {
    const data = snapshot?.pnl_calendar;
    if (!data || data.source !== 'BINANCE_FUTURES' || data.kind !== 'CLOSED_POSITIONS_FROM_FILLS' ||
        data.timezone !== 'Asia/Jakarta' || data.start_date !== CALENDAR_START ||
        !calendarDate(data.end_date) || data.end_date < CALENDAR_START || data.end_date > wibDay() ||
        !['READY', 'PARTIAL', 'UNAVAILABLE'].includes(data.status) || typeof data.complete !== 'boolean' ||
        !Array.isArray(data.days) || data.days.length > 10000 || !Array.isArray(data.positions) || data.positions.length > 20000) return null;
    const dates = new Set();
    for (const day of data.days) {
      if (!calendarDate(day.date) || day.date < CALENDAR_START || day.date > data.end_date || dates.has(day.date) ||
          typeof day.complete !== 'boolean' || !Number.isSafeInteger(day.closed_positions) || day.closed_positions < 0 ||
          !decimal(day.known_pnl_usdt) || day.pnl_usdt !== null && !decimal(day.pnl_usdt)) return null;
      dates.add(day.date);
    }
    const ids = new Set();
    for (const position of data.positions) {
      const closed = timestamp(position.closed_at), opened = position.opened_at === null ? null : timestamp(position.opened_at);
      if (typeof position.id !== 'string' || position.id.length > 128 || ids.has(position.id) ||
          !/^[A-Z0-9]{1,24}USDT$/.test(position.symbol) || !['LONG', 'SHORT'].includes(position.side) ||
          position.status !== 'CLOSED' || typeof position.complete !== 'boolean' || closed === null ||
          position.opened_at !== null && (opened === null || opened > closed) ||
          !calendarDate(position.close_date) || position.close_date < CALENDAR_START || position.close_date > data.end_date || wibDay(closed) !== position.close_date ||
          position.entry_price !== null && !decimal(position.entry_price) || !decimal(position.exit_price) ||
          !decimal(position.closed_quantity) || Number(position.closed_quantity) <= 0 || !decimal(position.realized_pnl_usdt) ||
          position.commission_usdt !== null && !decimal(position.commission_usdt) || position.pnl_usdt !== null && !decimal(position.pnl_usdt) ||
          !decimal(position.known_pnl_usdt) || position.funding_usdt !== null && !decimal(position.funding_usdt) ||
          position.insurance_usdt !== null && !decimal(position.insurance_usdt)) return null;
      ids.add(position.id);
    }
    return data;
  }
  function signedMoney(value) {
    if (!decimal(value)) return '—';
    const amount = Number(value);
    const formatted = Math.abs(amount).toLocaleString('id-ID', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (amount > 0 ? '+' : amount < 0 ? '−' : '')+formatted;
  }
  function calendarNumber(value, minimumFractionDigits = 0) {
    return decimal(value) ? Number(value).toLocaleString('id-ID', { minimumFractionDigits, maximumFractionDigits: 8 }) : '—';
  }
  function decimalTotal(values) {
    const scale = Math.max(0, ...values.map(value => (value.split('.')[1] ?? '').length));
    const total = values.reduce((sum, value) => {
      const negative = value.startsWith('-'), [whole, fraction = ''] = value.replace(/^-/, '').split('.');
      const units = BigInt(whole+fraction.padEnd(scale, '0'));
      return sum+(negative ? -units : units);
    }, 0n);
    const digits = (total < 0n ? -total : total).toString().padStart(scale+1, '0');
    return (total < 0n ? '-' : '')+(scale ? digits.slice(0, -scale)+'.'+digits.slice(-scale) : digits);
  }
  function dayLabel(value, options = { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }) {
    return new Date(value+'T00:00:00Z').toLocaleDateString('id-ID', { ...options, timeZone: 'UTC' });
  }
  function shiftMonth(month, offset) {
    const [year, index] = month.split('-').map(Number);
    return new Date(Date.UTC(year, index - 1 + offset, 1)).toISOString().slice(0, 7);
  }
  function chooseDay(key, focus = false) {
    if (!calendarDate(key) || key < CALENDAR_START || key > wibDay()) return;
    selectedDay = key; calendarMonth = key.slice(0, 7); paint();
    if (focus) document.getElementById('pnl-day-'+key)?.focus?.({ preventScroll: true });
  }
  function moveMonth(offset) {
    const next = shiftMonth(calendarMonth, offset);
    if (next < CALENDAR_START.slice(0, 7) || next > wibDay().slice(0, 7)) return;
    const day = next === wibDay().slice(0, 7) ? wibDay() : next+'-01';
    chooseDay(day);
  }
  function positionCard(position) {
    const card = element('article', undefined, 'pnl-position');
    const heading = element('div', undefined, 'pnl-position-head'), identity = element('div', undefined, 'pnl-position-identity');
    identity.append(element('span', position.side === 'LONG' ? 'B' : 'S', 'pnl-side '+position.side.toLowerCase()),
      element('strong', position.symbol), element('span', 'Perp', 'pnl-badge'), element('span', position.side, 'pnl-badge'));
    heading.append(identity, element('span', 'Ditutup', 'pnl-closed')); card.append(heading);
    const metrics = element('div', undefined, 'pnl-position-metrics');
    const metric = (label, value, className = '') => {
      const item = element('div', undefined, 'pnl-position-metric');
      item.append(element('span', label), element('b', value, className)); metrics.append(item);
    };
    const net = position.pnl_usdt, amount = net ?? position.known_pnl_usdt;
    metric(net === null ? 'PNL diketahui · USDT' : 'PNL terealisasi net · USDT', signedMoney(amount)+(net === null ? ' *' : ''),
      decimal(amount) ? Number(amount) < 0 ? 'pnl-loss-text' : Number(amount) > 0 ? 'pnl-profit-text' : '' : '');
    metric('Vol. tertutup', calendarNumber(position.closed_quantity));
    metric('Harga masuk rata-rata', calendarNumber(position.entry_price, 2));
    metric('Harga penutupan rata-rata', calendarNumber(position.exit_price, 2));
    card.append(metrics);
    const times = element('div', undefined, 'pnl-position-times');
    field(times, 'Dibuka · WIB', date(position.opened_at));
    field(times, 'Ditutup · WIB', date(position.closed_at));
    if (position.opened_at !== null) {
      const minutes = Math.floor((timestamp(position.closed_at) - timestamp(position.opened_at)) / 60000);
      const days = Math.floor(minutes / 1440), hours = Math.floor(minutes % 1440 / 60), remaining = minutes % 60;
      field(times, 'Durasi', [days ? days+' hari' : '', hours ? hours+' jam' : '', remaining+' mnt'].filter(Boolean).join(' '));
    }
    card.append(times);
    card.append(element('p', 'PNL sebelum fee '+signedMoney(position.realized_pnl_usdt)+' USDT · Komisi '+calendarNumber(position.commission_usdt)+' USDT', 'pnl-fees'));
    card.append(element('p', 'Funding '+(position.funding_usdt === null ? 'belum dapat dipastikan' : signedMoney(position.funding_usdt)+' USDT')+
      ' · Biaya asuransi '+(position.insurance_usdt === null ? 'belum dapat dipastikan' : signedMoney(position.insurance_usdt)+' USDT'), 'pnl-fees'));
    if (!position.complete || net === null) card.append(notice('Rincian posisi belum lengkap. Total net belum dapat dipastikan.', 'warning'));
    return card;
  }
  function renderCalendar() {
    const data = calendarData(), today = wibDay(), startMonth = CALENDAR_START.slice(0, 7), currentMonth = today.slice(0, 7);
    if (!calendarMonth || calendarMonth < startMonth || calendarMonth > currentMonth) calendarMonth = currentMonth;
    if (!selectedDay || selectedDay.slice(0, 7) !== calendarMonth || selectedDay > today) selectedDay = calendarMonth === currentMonth ? today : calendarMonth+'-01';
    const main = element('section', undefined, 'pnl-calendar');
    const header = element('div', undefined, 'pnl-calendar-head'), heading = element('div', undefined, 'pnl-calendar-heading');
    heading.append(element('span', '▦', 'pnl-calendar-icon'), element('h4', 'PNL Calendar'));
    const navigation = element('div', undefined, 'pnl-month-navigation');
    const previous = element('button', '‹'), next = element('button', '›');
    previous.id = 'pnlPreviousMonth'; next.id = 'pnlNextMonth';
    previous.setAttribute('aria-label', 'Bulan sebelumnya'); next.setAttribute('aria-label', 'Bulan berikutnya');
    previous.disabled = calendarMonth <= startMonth; next.disabled = calendarMonth >= currentMonth;
    previous.onclick = () => moveMonth(-1); next.onclick = () => moveMonth(1);
    const label = element('strong', dayLabel(calendarMonth+'-01', { month: 'long', year: 'numeric' })); label.id = 'pnlMonth'; label.setAttribute('aria-live', 'polite');
    navigation.append(previous, label, next); header.append(heading, navigation); main.append(header);
    const summaries = element('div', undefined, 'pnl-summary');
    const days = data?.days.filter(day => day.date.startsWith(calendarMonth)) ?? [];
    const count = days.reduce((total, day) => total+day.closed_positions, 0);
    const monthComplete = data?.complete === true && days.length > 0 && days.every(day => day.complete && day.pnl_usdt !== null);
    const known = decimalTotal(days.filter(day => day.closed_positions > 0).map(day => day.known_pnl_usdt));
    const hasAmounts = days.some(day => day.closed_positions > 0);
    const pnl = element('div'); pnl.append(element('span', monthComplete ? 'PNL bulan ini · USDT' : 'PNL diketahui · USDT'), element('strong', monthComplete || hasAmounts ? signedMoney(known)+(monthComplete ? '' : ' *') : '—'));
    const positions = element('div'); positions.append(element('span', 'Posisi ditutup'), element('strong', data && (data.complete || count > 0) ? String(count)+(data.complete ? '' : ' *') : '—'));
    summaries.append(pnl, positions); main.append(summaries);
    const good = data && ['READY', 'PARTIAL'].includes(data.status);
    if (!good) {
      const reasons = snapshot?.pnl_calendar?.incomplete_reasons;
      const loading = Array.isArray(reasons) && reasons.some(reason => ['HISTORY_NOT_LOADED', 'HISTORY_LOADING'].includes(reason));
      const unavailable = !online || !origin
        ? 'Kalender PNL belum tersedia. Hubungkan worker untuk membaca riwayat Futures Binance sejak 1 Oktober 2026.'
        : loading ? 'Riwayat Futures sedang dimuat sejak 1 Oktober 2026.'
        : 'Kalender PNL belum tersedia dari Binance Futures sejak 1 Oktober 2026.';
      main.append(notice(unavailable, 'warning'));
    }
    else {
      main.append(element('p', 'Binance Futures · '+date(data.checked_at)+' WIB', 'pnl-source'));
      if (!online || !fresh(data.checked_at)) main.append(notice('Data terakhir sudah lama atau worker tidak terjangkau. Riwayat ini belum diperbarui.', 'warning'));
      if (!data.complete) main.append(notice('Riwayat parsial. Tanda * menunjukkan PNL dan jumlah posisi yang berhasil ditemukan; total akun belum dapat dipastikan. Hari tanpa data ditampilkan —.', 'warning'));
    }
    const weekdays = element('div', undefined, 'pnl-weekdays'); weekdays.setAttribute('aria-hidden', 'true');
    for (const name of ['Sen', 'Sel', 'Rab', 'Kam', 'Jum', 'Sab', 'Min']) weekdays.append(element('span', name));
    const grid = element('div', undefined, 'pnl-grid'); grid.setAttribute('role', 'group'); grid.setAttribute('aria-label', 'Kalender PNL '+label.textContent);
    const [year, month] = calendarMonth.split('-').map(Number), first = new Date(Date.UTC(year, month-1, 1));
    const offset = (first.getUTCDay()+6)%7, length = new Date(Date.UTC(year, month, 0)).getUTCDate();
    const byDay = new Map((data?.days ?? []).map(day => [day.date, day]));
    for (let index = 0; index < Math.ceil((offset+length)/7)*7; index++) {
      const dayNumber = index-offset+1;
      if (dayNumber < 1 || dayNumber > length) { const empty = element('div', undefined, 'pnl-day-empty'); empty.setAttribute('aria-hidden', 'true'); grid.append(empty); continue; }
      const key = calendarMonth+'-'+String(dayNumber).padStart(2, '0'), detail = byDay.get(key);
      const knownDay = detail && (detail.pnl_usdt !== null || detail.closed_positions > 0);
      const amount = detail?.pnl_usdt ?? (detail?.closed_positions > 0 ? detail.known_pnl_usdt : null);
      const sign = knownDay && decimal(amount) ? Number(amount) : null;
      const cell = element('button', undefined, 'pnl-day'+(sign > 0 ? ' profit' : sign < 0 ? ' loss' : ''));
      cell.id = 'pnl-day-'+key; cell.dataset.date = key; cell.disabled = key < CALENDAR_START || key > today;
      cell.tabIndex = key === selectedDay ? 0 : -1; cell.setAttribute('aria-pressed', String(key === selectedDay));
      if (key === today) cell.setAttribute('aria-current', 'date');
      const value = knownDay ? signedMoney(amount)+(detail.complete && data?.complete ? '' : '*') : '—';
      cell.setAttribute('aria-label', dayLabel(key)+': '+value+' USDT'+(detail ? ', '+detail.closed_positions+' posisi ditutup'+(!detail.complete || !data?.complete ? ', data parsial' : '') : ', belum tersedia'));
      cell.append(element('span', String(dayNumber), 'pnl-day-number'), element('b', key > today ? '' : value, 'pnl-day-value'));
      const caption = key > today ? '' : detail?.closed_positions > 0 ? detail.closed_positions+' posisi' : detail?.complete && data?.complete ? '0 posisi' : '—';
      cell.append(element('small', caption, 'pnl-day-count')); cell.onclick = () => chooseDay(key, true);
      cell.addEventListener('keydown', event => {
        const delta = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[event.key];
        if (delta !== undefined) { event.preventDefault(); chooseDay(new Date(Date.parse(key+'T00:00:00Z')+delta*86400000).toISOString().slice(0, 10), true); }
        else if (event.key === 'PageUp' || event.key === 'PageDown') { event.preventDefault(); moveMonth(event.key === 'PageUp' ? -1 : 1); document.getElementById('pnl-day-'+selectedDay)?.focus?.({ preventScroll: true }); }
      });
      grid.append(cell);
    }
    main.append(weekdays, grid, element('p', 'Hijau = profit · Merah = rugi · WIB · PNL posisi setelah fee, funding dan biaya asuransi yang dapat dipastikan.', 'pnl-legend'));
    main.append(element('p', 'Riwayat direkonstruksi dari transaksi Futures Binance. PNL posisi dicatat pada tanggal posisi selesai ditutup.', 'pnl-method'));
    content.append(main);
    const selected = element('section', undefined, 'pnl-day-detail');
    selected.append(element('h4', dayLabel(selectedDay)));
    const rows = (data?.positions ?? []).filter(position => position.close_date === selectedDay).sort((a, b) => timestamp(b.closed_at)-timestamp(a.closed_at));
    for (const position of rows) selected.append(positionCard(position));
    if (!rows.length) selected.append(notice(data?.complete && byDay.get(selectedDay)?.complete ? 'Tidak ada posisi yang ditutup pada tanggal ini.' : 'Belum ada posisi ditutup yang dapat dipastikan pada tanggal ini. Riwayat belum lengkap.', 'warning'));
    content.append(selected);
    if (!origin) content.append(renderConnection());
  }
  function paint() {
    paintStats();
    if (panel.hidden) return;
    const focused = document.activeElement;
    // Keep unfinished credential/risk input intact while worker facts refresh.
    if (view === 'robot' && ['apiOrigin', 'apiToken', 'robotRisk'].includes(focused?.id)) {
      const toggle = document.getElementById('robotToggle'); if (toggle) toggle.disabled = !origin || !online || saving || !snapshot;
      for (const id of ['robotRisk', 'robotRiskSave']) {
        const control = document.getElementById(id); if (control) control.disabled = !origin || !online || saving || !snapshot;
      }
      const note = content.querySelector('.dashboard-message'); if (note) note.textContent = message;
      return;
    }
    const calendarFocus = view === 'calendar' && focused?.id?.startsWith('pnl-day-') ? focused.id : null;
    panel.classList.toggle('calendar-panel', view === 'calendar');
    title.textContent = views[view]; content.replaceChildren();
    ({ robot: renderRobot, calendar: renderCalendar })[view]();
    if (calendarFocus) document.getElementById(calendarFocus)?.focus?.({ preventScroll: true });
  }
  async function request(path, method = 'GET', value) {
    const headers = { Authorization: 'Bearer '+token };
    if (value !== undefined) headers['Content-Type'] = 'application/json';
    const response = await fetch(origin+path, { method, headers, body: value === undefined ? undefined : JSON.stringify(value),
      credentials: 'omit', redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw Error('Worker HTTP '+response.status);
    return response.json();
  }
  async function poll() {
    if (!origin || polling || saving) return;
    const currentGeneration = generation, currentRevision = revision; polling = true;
    try {
      const data = validate(await request('/office/status'));
      if (currentGeneration !== generation || currentRevision !== revision || saving) return;
      snapshot = data; online = true;
      if (resetRiskOnRefresh !== null) {
        if (riskDraft === resetRiskOnRefresh) riskDraft = null;
        resetRiskOnRefresh = null;
      }
      message = 'Worker terhubung · Diperiksa '+date(data.generated_at); publish(); paint();
    } catch (error) {
      if (currentGeneration !== generation || currentRevision !== revision) return;
      online = false; message = error.message+'. Data terakhir bukan pembaruan terkini.'; publish(); paint();
    } finally {
      if (currentGeneration === generation) {
        polling = false;
        if (refreshRequested && !saving) { refreshRequested = false; poll(); }
      }
    }
  }
  async function saveSettings(value) {
    if (!origin || !online || saving || !snapshot) return;
    const riskChange = typeof value.risk_target_usdt === 'string';
    const currentGeneration = generation; revision++; saving = true; message = riskChange ? 'Menyimpan risiko untuk analisis baru…' : 'Menyimpan status robot…'; paint();
    try {
      await request('/robot/settings', 'POST', value);
      if (currentGeneration !== generation) return;
      if (riskChange) resetRiskOnRefresh = value.risk_target_usdt;
      message = riskChange ? 'Risiko tersimpan untuk analisis baru. Membaca pembaruan worker…' : 'Status tersimpan. Membaca pembaruan worker…';
    } catch (error) { if (currentGeneration === generation) message = error.message+'. Pengaturan belum dapat dipastikan.'; }
    finally {
      if (currentGeneration === generation) {
        saving = false; refreshRequested = true; paint();
        if (!polling) { refreshRequested = false; poll(); }
      }
    }
  }
  function disconnect() {
    generation++; revision++; clearInterval(timer); timer = null; token = ''; origin = ''; snapshot = null;
    polling = false; saving = false; refreshRequested = false; online = false;
    riskDraft = null; resetRiskOnRefresh = null; calendarMonth = null; selectedDay = null;
    const field = document.getElementById('apiToken'); if (field) field.value = ''; publish();
  }
  menuToggle.addEventListener('click', () => { drawer.hidden = !drawer.hidden; menuToggle.setAttribute('aria-expanded', String(!drawer.hidden)); });
  drawer.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {
    view = button.dataset.view; drawer.querySelectorAll('[data-view]').forEach(item => item.classList.toggle('active', item === button));
    panel.hidden = false; panel.scrollTop = 0; drawer.hidden = true; menuToggle.setAttribute('aria-expanded', 'false'); paint();
  }));
  document.getElementById('panelClose').addEventListener('click', () => { panel.hidden = true; });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { publish(); paint(); poll(); } });
  window.addEventListener('pagehide', () => { disconnect(); });
  paintStats();
})();
