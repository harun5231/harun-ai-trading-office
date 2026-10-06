/* Private worker dashboard. Credentials stay in tab memory; exchange work belongs to the server. */
(() => {
  'use strict';
  const panel = document.getElementById('infoPanel');
  const content = document.getElementById('panelContent');
  const title = document.getElementById('panelTitle');
  const drawer = document.getElementById('menuDrawer');
  const menuToggle = document.getElementById('menuToggle');
  const views = { robot: 'ROBOT TRADING ON / OFF', staff: 'KARYAWAN AI', reports: 'LAPORAN', activity: 'AKTIVITAS', positions: 'RIWAYAT POSISI BINANCE' };
  const FRESH_MS = 120000;
  let origin = '', configuredOrigin = '', token = '', timer = null, snapshot = null;
  let view = 'robot', generation = 0, revision = 0, polling = false, saving = false;
  let riskDraft = null, resetRiskOnRefresh = null;
  let online = false, refreshRequested = false, message = 'Hubungkan worker untuk membaca akun dan status robot.';

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text ?? '—');
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
    return time === null ? 'Belum tersedia' : new Date(time).toLocaleString('id-ID', { dateStyle: 'short', timeStyle: 'medium' });
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
    if (pnlValue) pnlValue.title = reports?.complete === false ? 'Laporan Binance parsial; baca rincian pada Laporan.' : '';
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
    const riskLabel = element('label', 'RISIKO PER SL TERMASUK FEE (USDT, lebih dari 0 sampai 100)');
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
      notice('Ukuran posisi dihitung mendekati batas risiko, termasuk fee entry dan exit SL pada harga setup. Slippage, gap harga, dan funding dapat mengubah kerugian aktual. Perubahan risiko berlaku hanya untuk analisis baru. Setup dan intent yang sudah dibuat tetap memakai risiko sebelumnya.'));
    main.append(riskControls);
    const status = element('p', robot ? 'BOT STATUS: '+robot.bot_status+(robot.wait_reason ? ' · WAIT: '+robot.wait_reason : '')+(robot.failure_code ? ' · '+robot.failure_code : '') : 'Menunggu status worker.', 'dashboard-status');
    status.id = 'robotStatus'; status.setAttribute('role', 'status'); main.append(status);
    const metrics = element('div', undefined, 'dashboard-metrics');
    field(metrics, 'Risk per SL', number(robot?.risk_target_usdt)+' USDT');
    field(metrics, 'Posisi / kapasitas', number(account?.active_positions, 0)+' / 2');
    field(metrics, 'Slot tersedia', number(robot?.available_slots, 0));
    field(metrics, 'Entry bot hari ini', number(robot?.bot_entries_today, 0));
    main.append(metrics, gatewayNote(robot));
    main.append(notice('ON menjalankan coordinator ketika slot tersedia. OFF menghentikan pekerjaan baru; proses yang sudah berjalan dapat selesai. Posisi manual tetap dipantau tanpa diubah.'));
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
  function renderStaff() {
    const employees = snapshot?.employees ?? [];
    content.append(notice(online ? 'Status karyawan mengikuti pekerjaan yang dilaporkan coordinator.' : 'Status karyawan belum tersedia dari worker.', online ? 'normal' : 'warning'));
    for (const employee of employees) {
      const row = element('article', undefined, 'dashboard-row'); row.append(element('strong', employee.name), element('p', employee.status)); content.append(row);
    }
    if (!employees.length) content.append(notice('Belum ada status karyawan AI.'));
  }
  function renderReports() {
    const reports = snapshot?.reports, main = section('HASIL BINANCE FUTURES'); source(main, reports);
    field(main, 'PnL hari ini · USDT', number(reports?.pnl_today_usdt));
    field(main, 'Realized PnL · USDT', number(reports?.realized_pnl_today_usdt));
    field(main, 'Komisi · USDT', number(reports?.commission_today_usdt));
    field(main, 'Funding · USDT', number(reports?.funding_today_usdt));
    field(main, 'Fill / transaksi hari ini', number(reports?.trades_today, 0));
    main.append(notice('Periode: '+date(reports?.period_start)+' — '+date(reports?.period_end)));
    if (reports?.complete === false) main.append(notice('Laporan parsial. Jangan membaca hasil ini sebagai total akun yang sudah lengkap.', 'warning'));
    content.append(main);
  }
  function renderActivity() {
    const activity = snapshot?.activity ?? [];
    content.append(notice(online ? 'Aktivitas terbaru dari worker.' : 'Worker belum terjangkau; aktivitas yang tersimpan bukan pembaruan terkini.', online ? 'normal' : 'warning'));
    for (const event of activity.slice(-80).reverse()) {
      const row = element('article', undefined, 'dashboard-row');
      row.append(element('small', date(event.at)), element('strong', event.agent+' · '+event.state), element('p', event.message)); content.append(row);
    }
    if (!activity.length) content.append(notice('Belum ada aktivitas worker.'));
  }
  function renderPositions() {
    const history = snapshot?.position_history, main = section('RIWAYAT TRANSAKSI DARI BINANCE'); source(main, history);
    main.append(notice('Daftar ini berisi fill Binance. Satu posisi bisa memiliki beberapa fill; data ini tidak menyimpulkan otomatis bahwa sebuah posisi sudah ditutup.'));
    main.append(notice('Periode: '+date(history?.period_start)+' — '+date(history?.period_end)));
    for (const fill of [...(history?.items ?? [])].sort((a, b) => (timestamp(b.time) ?? 0) - (timestamp(a.time) ?? 0)).slice(0, 100)) {
      const row = element('article', undefined, 'dashboard-row');
      row.append(element('strong', fill.symbol+' · '+fill.side+' · '+fill.position_side), element('small', date(fill.time)));
      row.append(element('p', `Quantity ${number(fill.quantity, 8)} · Harga ${number(fill.price, 8)} · Realized PnL ${number(fill.realized_pnl)} USDT`));
      row.append(element('p', `Komisi ${number(fill.commission, 8)} ${fill.commission_asset ?? '—'} · Order ${fill.order_id ?? '—'} · Fill ${fill.id ?? '—'}`)); main.append(row);
    }
    if (!history?.items?.length) main.append(notice(history?.status === 'AVAILABLE' ? 'Tidak ada fill Binance pada periode ini.' : history?.status === 'PARTIAL' ? 'Tidak ada fill dalam data yang berhasil dibaca; riwayat masih parsial.' : 'Riwayat Binance belum tersedia.'));
    content.append(main);
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
    title.textContent = views[view]; content.replaceChildren();
    ({ robot: renderRobot, staff: renderStaff, reports: renderReports, activity: renderActivity, positions: renderPositions })[view]();
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
    riskDraft = null; resetRiskOnRefresh = null;
    const field = document.getElementById('apiToken'); if (field) field.value = ''; publish();
  }
  menuToggle.addEventListener('click', () => { drawer.hidden = !drawer.hidden; menuToggle.setAttribute('aria-expanded', String(!drawer.hidden)); });
  drawer.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {
    view = button.dataset.view; drawer.querySelectorAll('[data-view]').forEach(item => item.classList.toggle('active', item === button));
    panel.hidden = false; drawer.hidden = true; menuToggle.setAttribute('aria-expanded', 'false'); paint();
  }));
  document.getElementById('panelClose').addEventListener('click', () => { panel.hidden = true; });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { publish(); paint(); poll(); } });
  window.addEventListener('pagehide', () => { disconnect(); });
  paintStats();
})();
