/* Private worker dashboard. Credentials stay in tab memory; exchange work belongs to the server. */
(() => {
  'use strict';
  const panel = document.getElementById('infoPanel');
  const content = document.getElementById('panelContent');
  const title = document.getElementById('panelTitle');
  const drawer = document.getElementById('menuDrawer');
  const menuToggle = document.getElementById('menuToggle');
  const views = { robot: 'ROBOT TRADING ON / OFF', calendar: 'KALENDER PNL', neurobro: 'LOGIN NEURO', billing: 'TAGIHAN BULANAN' };
  const CALENDAR_START = '2026-10-01';
  const FRESH_MS = 120000;
  const RESEARCH_FRESH_MS = 90000;
  let origin = '', configuredOrigin = '', token = '', timer = null, snapshot = null;
  let view = 'robot', generation = 0, revision = 0, polling = false, saving = false;
  let riskDraft = null, resetRiskOnRefresh = null;
  let online = false, refreshRequested = false, message = 'Hubungkan worker untuk membaca akun dan status robot.';
  let calendarMonth = null, selectedDay = null;
  let loginBusy = false, loginMessage = '', loginAccess = null, loginExpiryTimer = null;
  let researchOwnerNotice = null;
  let billingEditing = null, billingScheduleDrafts = {};
  let billingMonth = null, billingRecord = null, billingDraft = null, billingBusy = false, billingMessage = '', billingHistory = null;

  function billingField(parent, label, id, value, change, type = 'text') {
    const wrapper = element('label', label), input = element('input');
    input.id = id; input.type = type; input.value = value ?? ''; input.disabled = billingBusy;
    if (type === 'number') { input.min = '0'; input.step = 'any'; input.inputMode = 'decimal'; }
    input.oninput = () => change(input.value === '' ? null : input.value);
    wrapper.append(input); parent.append(wrapper); return input;
  }
  function billingLink(parent, url) {
    try {
      const parsed = new URL(url), host = parsed.hostname.toLowerCase().replace(/\.$/, '');
      if (parsed.protocol !== 'https:' || parsed.username || parsed.password || /[\s\\]/.test(url) || !host || /(?:^|\.)(?:localhost|local|internal|lan|home)$/.test(host)) return;
      const ipv4 = host.split('.').map(Number), numeric = /^\d+\.\d+\.\d+\.\d+$/.test(host);
      if (numeric && (ipv4.some(part => part > 255) || ipv4[0] === 0 || ipv4[0] === 10 || ipv4[0] === 127 || ipv4[0] >= 224 || ipv4[0] === 169 && ipv4[1] === 254 || ipv4[0] === 172 && ipv4[1] >= 16 && ipv4[1] <= 31 || ipv4[0] === 192 && ipv4[1] === 168 || ipv4[0] === 100 && ipv4[1] >= 64 && ipv4[1] <= 127)) return;
      if (!numeric && !/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/.test(host)) return;
      const link = element('a', 'Buka pembayaran'); link.href = parsed.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; parent.append(link);
    } catch (_) { /* Missing links remain editable; never invent a payment portal. */ }
  }
  function simpleBillingDraft() {
    const doc = billingDraft;
    doc.simple ??= { version: 1, usd_per_usdt: null, usd_rate_source: null, installment_idr: null, installment_due_date: null, debt_principal_idr: null };
    doc.simple.recurrence_updates ??= {};
    const knownPrincipal = billingRecord?.summary?.debt;
    if (doc.simple.debt_principal_idr == null && knownPrincipal?.principal_status === 'AVAILABLE') doc.simple.debt_principal_idr = knownPrincipal.principal_idr;
    doc.cashflow ??= { payment_fee_actual_idr: null, payment_fee_estimated_idr: null, withdrawal_fee_actual_usdt: null, withdrawal_fee_estimated_usdt: null, withdrawal_fee_percent: null };
    doc.bills.forEach((row, index) => {
      if (row.kind === 'prepaid_usage') return;
      row.id ??= 'legacy-'+index; row.currency ??= 'IDR';
      row.amount ??= row.actual_idr ?? row.estimated_idr;
      row.actual_idr = row.currency === 'IDR' ? row.amount : null; row.estimated_idr = null;
    });
    return doc;
  }
  function billAmount(row, value = row.amount) {
    return value == null ? 'Belum diisi' : (row.currency === 'USD' ? '$' : 'Rp ')+Number(value).toLocaleString('id-ID', { maximumFractionDigits: 8 });
  }
  function billingRow(ident, name = '', amount = null, currency = 'IDR', due = null) {
    return { id: ident, name, amount, currency, kind: 'subscription', actual_idr: currency === 'IDR' ? amount : null,
      estimated_idr: null, due_date: due, payment_url: null, paid: false, evidence: 'Catatan pemilik', recurring: false };
  }
  const recurrenceFields = ['name', 'kind', 'amount', 'currency', 'actual_idr', 'estimated_idr', 'payment_url', 'evidence'];
  function scheduleFor(row, doc = billingDraft) {
    const ident = row.recurrence_id || row.id, updates = doc?.simple?.recurrence_updates || {};
    return Object.hasOwn(updates, ident) ? updates[ident] : billingRecord?.recurrence_state?.[ident];
  }
  function scheduleDue(day) {
    const [year, month] = billingMonth.split('-').map(Number), last = new Date(Date.UTC(year, month, 0)).getUTCDate();
    return billingMonth+'-'+String(Math.min(day, last)).padStart(2, '0');
  }
  function billingEditor(row) {
    if (!billingScheduleDrafts[row.id]) {
      const values = JSON.parse(JSON.stringify(row)), spec = scheduleFor(row);
      if (row.paid === true && row.recurrence_id && spec) Object.assign(values, spec.bill, { due_date: scheduleDue(spec.day) });
      if (row.recurrence_id && spec === null) values.recurring = false;
      billingScheduleDrafts[row.id] = { values, dirty: false, dueChanged: false };
    }
    return billingScheduleDrafts[row.id];
  }
  function commitBillingEdits(doc) {
    for (const row of doc.bills) {
      const edit = billingScheduleDrafts[row.id]; if (!edit?.dirty) continue;
      const values = edit.values, ident = row.recurrence_id || (values.recurring ? row.id : null), previous = scheduleFor(row, doc);
      // Editing a paid subscription changes its following schedule, preserving
      // the settled occurrence's name, amount, date, currency and payment.
      if (!(row.paid === true && values.paid === true && ident)) Object.assign(row, values);
      if (ident) {
        row.recurrence_id = ident; row.recurring = values.recurring === true;
        if (row.recurring) {
          const day = edit.dueChanged || !previous ? Number(values.due_date?.slice(-2)) : previous.day;
          const bill = {}; for (const key of recurrenceFields) bill[key] = values[key];
          doc.simple.recurrence_updates[ident] = { day, bill };
        } else doc.simple.recurrence_updates[ident] = null;
      }
      edit.dirty = false;
    }
  }
  function billingFailure(error) {
    if (error.message === 'Worker HTTP 401' || error.message === 'Worker HTTP 403') return 'Akses worker ditolak. Hubungkan ulang melalui menu Robot.';
    if (error.message === 'Worker HTTP 400') return 'Periksa nama, nominal, tanggal, atau link tagihan.';
    if (error.message === 'Worker HTTP 404') return 'Layanan tagihan belum tersedia pada alamat worker.';
    if (error.message === 'Worker HTTP 409') return 'Tagihan berubah. Muat ulang sebelum menyimpan.';
    if (error instanceof TypeError || /Load failed|Failed to fetch|NetworkError/i.test(error.message || '')) return 'Koneksi layanan tagihan gagal.';
    return error.message || 'Tagihan belum tersedia.';
  }
  async function loadBilling(history = false) {
    if (!origin || billingBusy) return;
    const current = generation, month = billingMonth; billingBusy = true; billingMessage = 'Memuat…'; paint();
    try {
      const data = await request('/billing/'+(history ? 'history' : 'month')+'?month='+encodeURIComponent(month));
      if (current !== generation || month !== billingMonth) return;
      if (history) billingHistory = data.revisions;
      else {
        if (data.document?.month !== month || !Number.isInteger(data.version) || !Array.isArray(data.document.bills)) throw Error('Data tagihan tidak valid');
        billingRecord = data; billingDraft = JSON.parse(JSON.stringify(data.display_document || data.document)); billingHistory = null; billingEditing = null; billingScheduleDrafts = {};
        simpleBillingDraft();
      }
      billingMessage = '';
    } catch (error) { if (current === generation) billingMessage = billingFailure(error); }
    finally { if (current === generation) { billingBusy = false; paint(true); } }
  }
  async function saveBilling() {
    if (!origin || !billingRecord || billingBusy) return;
    const current = generation, month = billingMonth, draft = simpleBillingDraft(), version = billingRecord.version;
    commitBillingEdits(draft);
    const document = JSON.parse(JSON.stringify(draft));
    billingBusy = true; billingMessage = 'Menyimpan…'; paint(true);
    try {
      const saved = await request('/billing/month', 'POST', { document, version });
      if (current !== generation || month !== billingMonth) return;
      const verified = await request('/billing/month?month='+encodeURIComponent(month));
      if (current !== generation || month !== billingMonth) return;
      if (verified.version !== saved.version || JSON.stringify(verified.document) !== JSON.stringify(document)) throw Error('Simpanan belum terverifikasi');
      billingRecord = verified; billingDraft = JSON.parse(JSON.stringify(verified.display_document || verified.document)); billingHistory = null; billingEditing = null; billingScheduleDrafts = {};
      billingMessage = 'Tersimpan';
    } catch (error) { if (current === generation) billingMessage = billingFailure(error)+' · Muat ulang sebelum mencoba lagi.'; }
    finally { if (current === generation) { billingBusy = false; paint(true); } }
  }
  function renderBilling() {
    if (!billingMonth) billingMonth = wibDay().slice(0, 7);
    const total = element('section', undefined, 'billing-total');
    total.append(element('span', 'TOTAL USDT PERLU WD DARI BINANCE'));
    const value = billingRecord?.summary?.withdrawal_estimate_usdt;
    const amount = element('strong', value == null ? 'Belum tersedia' : Number(value).toLocaleString('id-ID', { maximumFractionDigits: 8 })+' USDT'); amount.id = 'billingTotal'; total.append(amount);
    if (value == null) total.append(element('small', 'Lengkapi nominal, tanggal, atau kurs.'));
    else if (billingRecord?.summary?.withdrawal_estimate_complete === false) total.append(element('small', 'Estimasi · biaya belum lengkap.'));
    content.append(total);
    const navigation = element('div', undefined, 'billing-navigation');
    billingField(navigation, 'Bulan', 'billingMonth', billingMonth, value => {
      if (!/^20\d{2}-(0[1-9]|1[0-2])$/.test(value || '')) return;
      billingMonth = value; billingRecord = billingDraft = billingHistory = null; billingEditing = null; loadBilling();
    }, 'month');
    const reload = element('button', 'Muat ulang'); reload.id = 'billingReload'; reload.disabled = !origin || billingBusy; reload.onclick = () => loadBilling(); navigation.append(reload); content.append(navigation);
    if (billingMessage) content.append(element('p', billingMessage, 'billing-message'));
    if (!billingDraft) { content.append(element('p', 'Hubungkan worker untuk memuat tagihan.')); if (!origin) content.append(renderConnection()); return; }
    const doc = simpleBillingDraft();
    if (!doc.bills.length) content.append(element('p', 'Belum ada tagihan bulan ini.', 'billing-empty'));
    doc.bills.forEach((row, index) => {
      if (row.kind === 'prepaid_usage') return;
      const card = element('section', undefined, 'billing-card'); card.dataset.billId = row.id;
      const heading = element('div', undefined, 'billing-card-head'); heading.append(element('strong', row.name || 'Tagihan baru'));
      heading.append(element('span', row.paid === true ? 'Lunas' : 'Belum lunas', row.paid === true ? 'billing-paid' : 'billing-unpaid')); card.append(heading);
      const facts = element('div', undefined, 'billing-card-facts'); facts.append(element('b', billAmount(row)), element('span', row.due_date ? 'Jatuh tempo '+row.due_date.split('-').reverse().join('/') : 'Tanggal belum diisi')); card.append(facts);
      if (row.recurring === true) card.append(element('small', 'Bulanan', 'billing-recurring'));
      if (billingEditing === index) {
        const edit = billingEditor(row), values = edit.values;
        const change = (key, value) => {
          values[key] = value; edit.dirty = true;
          if (key === 'amount' || key === 'currency') values.actual_idr = values.currency === 'IDR' ? values.amount : null;
          if (key === 'due_date') edit.dueChanged = true;
        };
        if (row.paid === true && row.recurrence_id) card.append(element('small', 'Perubahan jadwal berlaku untuk bulan berikutnya.'));
        billingField(card, 'Nama', 'billName'+index, values.name, value => change('name', value || ''));
        billingField(card, 'Nominal', 'billAmount'+index, values.amount, value => change('amount', value), 'number');
        const currencyLabel = element('label', 'Mata uang'), currency = element('select'); currency.id = 'billCurrency'+index; currency.disabled = billingBusy || row.kind === 'debt_installment';
        for (const code of ['IDR', 'USD']) { const option = element('option', code); option.value = code; currency.append(option); }
        currency.value = values.currency; currency.onchange = () => change('currency', currency.value); currencyLabel.append(currency); card.append(currencyLabel);
        billingField(card, 'Jatuh tempo', 'billDue'+index, values.due_date, value => change('due_date', value), 'date');
        billingField(card, 'Link pembayaran', 'billUrl'+index, values.payment_url, value => change('payment_url', value), 'url');
        const monthlyLabel = element('label', 'Bulanan'), monthly = element('input'); monthly.type = 'checkbox'; monthly.id = 'billRecurring'+index;
        monthly.checked = values.recurring === true; monthly.disabled = billingBusy || row.kind !== 'subscription'; monthly.onchange = () => change('recurring', monthly.checked); monthlyLabel.prepend ? monthlyLabel.prepend(monthly) : monthlyLabel.append(monthly); card.append(monthlyLabel);
        const paidLabel = element('label', 'Pembayaran'), paid = element('select'); paid.id = 'billPaid'+index; paid.disabled = billingBusy;
        for (const [value, text] of [['false', 'Belum lunas'], ['true', 'Lunas']]) { const option = element('option', text); option.value = value; paid.append(option); }
        paid.value = String(values.paid === true); paid.onchange = () => change('paid', paid.value === 'true'); paidLabel.append(paid); card.append(paidLabel);
      }
      const actions = element('div', undefined, 'billing-card-actions'); billingLink(actions, row.payment_url);
      const edit = element('button', billingEditing === index ? 'Tutup edit' : 'Edit'); edit.id = 'billEdit'+index; edit.disabled = billingBusy; edit.onclick = () => { billingEditing = billingEditing === index ? null : index; paint(true); };
      const paid = element('button', row.paid === true ? 'Tandai belum lunas' : 'Tandai lunas'); paid.id = 'billMarkPaid'+index; paid.disabled = billingBusy; paid.onclick = () => { commitBillingEdits(doc); row.paid = row.paid !== true; saveBilling(); };
      const remove = element('button', 'Hapus'); remove.id = 'billDelete'+index; remove.disabled = billingBusy; remove.onclick = () => {
        commitBillingEdits(doc);
        if (row.recurrence_id) doc.simple.recurrence_updates[row.recurrence_id] = null;
        if (row.paid === true && row.recurrence_id) row.recurring = false; else doc.bills.splice(index, 1);
        delete billingScheduleDrafts[row.id]; billingEditing = null; saveBilling();
      };
      actions.append(edit, paid, remove); card.append(actions); content.append(card);
    });
    const add = element('button', '+ Tambah tagihan'); add.id = 'billingAdd'; add.disabled = billingBusy || doc.bills.length >= 30;
    add.onclick = () => { doc.bills.push(billingRow('bill-'+Date.now()+'-'+doc.bills.length)); billingEditing = doc.bills.length-1; paint(true); };
    const save = element('button', 'Simpan perubahan'); save.id = 'billingSave'; save.disabled = billingBusy; save.onclick = saveBilling; content.append(add, save);
    const remaining = billingRecord?.summary?.debt?.remaining_idr;
    const principal = billingRecord?.summary?.debt?.principal_idr;
    const debtBalance = element('div', undefined, 'billing-debt');
    debtBalance.append(element('span', 'Utang awal '+(principal == null ? billingRecord?.summary?.debt?.principal_status === 'CONFLICT' ? 'perlu diperiksa' : 'belum tersedia' : 'Rp '+Number(principal).toLocaleString('id-ID'))), element('strong', 'Sisa '+(remaining == null ? 'belum tersedia' : 'Rp '+Number(remaining).toLocaleString('id-ID')))); content.append(debtBalance);
    const debt = element('details', undefined, 'billing-settings'); debt.append(element('summary', 'Cicilan utang kantor'));
    billingField(debt, 'Cicilan bulan ini (IDR)', 'billingInstallment', doc.simple.installment_idr, value => { doc.simple.installment_idr = value; }, 'number');
    billingField(debt, 'Jatuh tempo cicilan', 'billingInstallmentDue', doc.simple.installment_due_date, value => { doc.simple.installment_due_date = value; }, 'date');
    const installment = element('button', 'Masukkan cicilan ke tagihan'); installment.id = 'billingInstallmentAdd'; installment.disabled = billingBusy;
    installment.onclick = () => {
      if (!(Number(doc.simple.installment_idr) > 0) || !doc.simple.installment_due_date?.startsWith(billingMonth+'-')) { billingMessage = 'Isi nominal dan tanggal cicilan bulan ini.'; paint(true); return; }
      const ident = 'office-debt-'+billingMonth, previous = doc.bills.find(row => row.id === ident);
      if (previous) { previous.amount = previous.actual_idr = doc.simple.installment_idr; previous.due_date = doc.simple.installment_due_date; }
      else { const row = billingRow(ident, 'Cicilan utang kantor', doc.simple.installment_idr, 'IDR', doc.simple.installment_due_date); row.kind = 'debt_installment'; row.debt_id = 'office-initial'; doc.bills.push(row); }
      saveBilling();
    }; debt.append(installment); content.append(debt);
    const settings = element('details', undefined, 'billing-settings'); settings.append(element('summary', 'Kurs & biaya'));
    billingField(settings, '1 USDT = berapa IDR?', 'billingFx', doc.fx_idr_per_usdt, value => { doc.fx_idr_per_usdt = value; doc.fx_source = value == null ? null : 'Input pemilik '+wibDay(); }, 'number');
    billingField(settings, '1 USDT = berapa USD?', 'billingUsdFx', doc.simple.usd_per_usdt, value => { doc.simple.usd_per_usdt = value; doc.simple.usd_rate_source = value == null ? null : 'Input pemilik '+wibDay(); }, 'number');
    billingField(settings, 'Fee pembayaran (IDR)', 'billingPaymentEstimate', doc.cashflow.payment_fee_estimated_idr, value => { doc.cashflow.payment_fee_estimated_idr = value; }, 'number');
    billingField(settings, 'Fee WD (USDT)', 'billingWithdrawalEstimate', doc.cashflow.withdrawal_fee_estimated_usdt, value => { doc.cashflow.withdrawal_fee_estimated_usdt = value; }, 'number');
    billingField(settings, 'Fee WD (%)', 'billingWithdrawalPercent', doc.cashflow.withdrawal_fee_percent, value => { doc.cashflow.withdrawal_fee_percent = value; }, 'number');
    settings.append(element('small', 'Isi 0 hanya jika memang tidak ada biaya.')); content.append(settings);
  }

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
  function researchState() {
    const research = snapshot?.research;
    if (!online || !research) return { status: 'UNAVAILABLE', label: 'Status belum tersedia', login: online };
    if (research.provider === 'NEUROAPI') return { status: 'API_ACTIVE', label: 'Browser belum diaktifkan', login: false };
    const at = timestamp(research.checked_at);
    if (research.provider !== 'NEUROBRO_WEB' || at === null || at > Date.now() + 5000 || Date.now() - at >= RESEARCH_FRESH_MS ||
        !['CONNECTED', 'LOGIN_REQUIRED', 'LOGIN_IN_PROGRESS', 'UNAVAILABLE', 'CHALLENGE_REQUIRED', 'BUSY'].includes(research.status)) {
      return { status: 'UNAVAILABLE', label: 'Status perlu diperbarui', login: true };
    }
    const labels = { CONNECTED: 'Terhubung', LOGIN_REQUIRED: 'Login diperlukan', LOGIN_IN_PROGRESS: 'Akses login sedang aktif',
      UNAVAILABLE: 'Browser belum tersedia', CHALLENGE_REQUIRED: 'Verifikasi manusia diperlukan', BUSY: 'Analisis sedang berjalan' };
    return { status: research.status, label: labels[research.status], login: ['CONNECTED', 'LOGIN_REQUIRED', 'UNAVAILABLE', 'CHALLENGE_REQUIRED'].includes(research.status) };
  }
  function researchOwnerAction() {
    if (!online) return researchOwnerNotice;
    const research = snapshot?.research;
    if (!research || research.provider !== 'NEUROBRO_WEB') return null;
    const at = timestamp(research.checked_at), previous = timestamp(researchOwnerNotice?.checkedAt);
    const labels = { LOGIN_REQUIRED: 'MENUNGGU LOGIN NEUROBRO', CHALLENGE_REQUIRED: 'MENUNGGU VERIFIKASI MANUSIA' };
    if (at !== null && at <= Date.now() + 5000 && (previous === null || at >= previous)) {
      if (Object.hasOwn(labels, research.status)) {
        researchOwnerNotice = { label: labels[research.status], checkedAt: research.checked_at };
      } else if (['CONNECTED', 'BUSY'].includes(research.status) && Date.now() - at < RESEARCH_FRESH_MS) {
        researchOwnerNotice = null;
      }
    }
    return researchOwnerNotice;
  }
  function protectionObservations(robot) {
    const supplied = robot?.protection_observations;
    if (!supplied || typeof supplied !== 'object' || Array.isArray(supplied)) return [];
    return Object.values(supplied).filter(row => row && row.source === 'BINANCE_FUTURES' &&
      ['POSITION_PROTECTED', 'PROTECTION_INCOMPLETE'].includes(row.state) &&
      typeof row.symbol === 'string' && /^[A-Z0-9]{2,18}USDT$/.test(row.symbol) &&
      typeof row.sl_confirmed === 'boolean' && typeof row.tp_confirmed === 'boolean' &&
      timestamp(row.observed_at) !== null);
  }
  function protectionLabel(row) {
    const orderId = value => typeof value === 'string' && /^[1-9]\d*$/.test(value);
    const sl = row.sl_confirmed && orderId(row.sl_order_id) && row.sl_order_id !== row.order_id;
    const tp = row.tp_confirmed && orderId(row.tp_order_id) && row.tp_order_id !== row.order_id &&
      (!sl || row.tp_order_id !== row.sl_order_id);
    if (row.state === 'POSITION_PROTECTED' && sl && tp && row.sl_order_id !== row.tp_order_id) {
      return fresh(row.observed_at) ? 'POSISI TERLINDUNGI: SL DAN TP TERKONFIRMASI' :
        'PROTEKSI TERAKHIR TERKONFIRMASI (PERLU PEMBARUAN)';
    }
    return 'PROTEKSI BELUM LENGKAP: '+(sl ? 'SL TERPASANG' : 'SL BELUM TERKONFIRMASI')+
      ', '+(tp ? 'TP TERPASANG' : 'TP BELUM TERKONFIRMASI');
  }
  function agentState() {
    const agent = snapshot?.agent;
    const unknown = { status: 'UNAVAILABLE', label: 'Status agent belum tersedia', verified: false, dependencies: {} };
    if (!online || !agent || agent.schema_version !== 1 || agent.service !== 'harun-neurobro-agent' ||
        agent.source !== 'LOCAL_AGENT_HEALTH' || agent.status !== 'AVAILABLE') return unknown;
    const at = timestamp(agent.checked_at);
    if (at === null || at > Date.now() + 5000 || Date.now() - at >= 60000) {
      return { ...unknown, label: 'Status agent perlu diperbarui' };
    }
    const labels = { STARTING: 'Agent sedang dimulai', RUNNING: 'Agent sedang bekerja', IDLE: 'Agent menunggu tugas',
      PAUSED: 'Agent dijeda', WAITING: 'Agent menunggu kesiapan layanan', BUSY: 'Agent sedang menyelesaikan tugas',
      BLOCKED: 'Agent terhambat', REQUIRES_REVIEW: 'Agent memerlukan pemeriksaan' };
    if (typeof agent.state !== 'string' || !Object.hasOwn(labels, agent.state)) return unknown;
    const dependencies = {};
    for (const [name, maximum] of [['backend', 30000], ['browser', 1200000], ['model', 1200000]]) {
      const item = agent.dependencies?.[name], checked = timestamp(item?.checked_at);
      dependencies[name] = item?.verified === true && checked !== null && checked <= Date.now() + 5000 && Date.now() - checked < maximum;
    }
    return { status: agent.state, label: labels[agent.state], checkedAt: agent.checked_at,
      verified: agent.integration_verified === true && Object.values(dependencies).every(Boolean) &&
        ['CONNECTED', 'BUSY'].includes(researchState().status), dependencies };
  }
  function publish() {
    const account = accountCurrent() ? snapshot.account : null;
    const reports = reportsCurrent() ? snapshot.reports : null;
    const fills = online && snapshot?.position_history?.kind === 'BINANCE_FILLS' &&
      ['AVAILABLE', 'PARTIAL'].includes(snapshot.position_history.status) && fresh(snapshot.position_history.checked_at)
      ? snapshot.position_history : null;
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
      employees: online ? snapshot?.employees ?? [] : [], execution_gateway: online ? snapshot?.robot.execution_gateway ?? null : null,
      // Office screens consume the existing snapshot only. Symbol counts are not order counts.
      fills_checked_at: fills?.checked_at ?? null,
      recent_fills: fills ? fills.items.filter(row => row && typeof row === 'object' && !Array.isArray(row)).slice(0, 20).map(row => ({ symbol: row.symbol, side: row.side,
        quantity: row.quantity, price: row.price, time: row.time })) : [],
      pending_entry_symbols: account && Array.isArray(account.open_entry_symbols) ? [...account.open_entry_symbols] : null,
      protection_observations: online ? protectionObservations(snapshot?.robot).slice(0, 30).map(row => ({
        symbol: row.symbol, state: row.state, observed_at: row.observed_at, label: protectionLabel(row)
      })) : [],
      risk_observations: online && snapshot?.robot?.risk_estimates && typeof snapshot.robot.risk_estimates === 'object'
        ? Object.values(snapshot.robot.risk_estimates).filter(row => row?.source === 'RISK_MANAGER' &&
          row.kind === 'MONITORING_ESTIMATE_ONLY').slice(0, 30).map(row => ({
            source: row.source, symbol: row.symbol, status: row.status,
            observed_at: row.observed_at, failure_code: row.failure_code ?? null
          })) : []
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
    const research = researchState(), researchMenu = document.getElementById('neurobroMenu');
    if (researchMenu) researchMenu.dataset.status = research.status;
    const researchLabel = document.getElementById('neurobroMenuStatus');
    if (researchLabel) researchLabel.textContent = research.label;
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
    const main = element('section', undefined, 'dashboard-section');
    const actions = element('div', undefined, 'dashboard-actions');
    const toggle = element('button', 'ROBOT TRADING: '+(robot ? robot.robot_on ? 'ON' : 'OFF' : 'BELUM TERHUBUNG'));
    toggle.id = 'robotToggle'; toggle.disabled = !origin || !online || saving || !robot;
    toggle.setAttribute('aria-pressed', String(robot?.robot_on === true));
    toggle.onclick = () => saveSettings({ robot_on: !snapshot.robot.robot_on }); actions.append(toggle); main.append(actions);
    main.append(element('p', 'Worker: '+(online ? 'TERHUBUNG' : origin ? 'TIDAK TERJANGKAU' : 'BELUM TERHUBUNG')+
      ' · Diperiksa '+date(snapshot?.robot?.checked_at ?? snapshot?.generated_at), 'dashboard-status'));
    main.append(element('p', message, 'dashboard-message'));
    const riskControls = element('div', undefined, 'dashboard-connection dashboard-risk');
    const riskLabel = element('label', 'RISIKO PER SL TERMASUK FEE DAN CADANGAN SLIPPAGE (USDT)');
    const risk = element('input'); risk.id = 'robotRisk'; risk.type = 'number'; risk.inputMode = 'decimal';
    risk.min = '0'; risk.max = '100'; risk.step = 'any'; risk.value = riskDraft ?? robot?.risk_target_usdt ?? '';
    risk.disabled = !origin || !online || saving || !robot; risk.oninput = () => { riskDraft = risk.value; };
    riskLabel.append(risk);
    const riskActions = element('div', undefined, 'dashboard-actions'), riskSave = element('button', 'SIMPAN RISIKO');
    riskSave.id = 'robotRiskSave'; riskSave.disabled = risk.disabled;
    riskSave.onclick = () => {
      try { riskDraft = risk.value; saveSettings({ risk_target_usdt: validRisk(risk.value) }); }
      catch (error) { message = error.message; paint(); }
    };
    riskActions.append(riskSave); riskControls.append(riskLabel, riskActions); main.append(riskControls);
    const ownerAction = researchOwnerAction();
    const incomplete = protectionObservations(robot).find(row => protectionLabel(row).startsWith('PROTEKSI BELUM LENGKAP'));
    const status = element('p', 'BOT STATUS: '+(ownerAction ? ownerAction.label : incomplete ? protectionLabel(incomplete) : robot?.bot_status ?? 'BELUM TERSEDIA')+
      (robot?.failure_code ? ' · '+robot.failure_code : ''), 'dashboard-status');
    status.id = 'robotStatus'; status.setAttribute('role', 'status'); main.append(status);
    main.append(element('p', 'WAIT: '+(robot?.wait_reason ?? '—'), 'dashboard-status'));
    const metrics = element('div', undefined, 'dashboard-metrics');
    field(metrics, 'Risk per SL', number(robot?.risk_target_usdt)+' USDT');
    field(metrics, 'Posisi / kapasitas', number(robot?.running_positions ?? account?.active_positions, 0)+' / '+number(robot?.max_positions, 0));
    field(metrics, 'Slot tersedia', number(robot?.available_slots, 0));
    field(metrics, 'Entry bot hari ini', number(robot?.bot_entries_today, 0)+' / '+number(robot?.daily_entry_limit, 0));
    main.append(metrics); content.append(main);
    const current = section('AKUN FUTURES');
    current.append(element('p', 'Binance Futures · Diperiksa '+date(account?.checked_at), 'dashboard-status'));
    const details = element('div', undefined, 'dashboard-metrics'), visibleAccount = accountCurrent() ? account : null;
    field(details, 'Wallet USDT', number(visibleAccount?.usdt_wallet_balance));
    field(details, 'USDT tersedia', number(visibleAccount?.usdt_available_balance));
    field(details, 'Posisi manual', visibleAccount && Array.isArray(robot?.manual_exposure) ? robot.manual_exposure.length ? robot.manual_exposure.join(', ') : 'Tidak ada' : 'Belum tersedia');
    field(details, 'Status akun', account?.status ?? 'BELUM TERSEDIA');
    field(details, 'Posisi aktif Binance', number(visibleAccount?.active_positions, 0)); current.append(details);
    if (visibleAccount?.positions?.length) for (const row of visibleAccount.positions) {
      const position = element('article', undefined, 'dashboard-row');
      position.append(element('strong', row.symbol+' · '+row.side), element('p', `Quantity ${number(row.quantity, 8)} · Entry ${number(row.entry_price, 8)} · Mark ${number(row.mark_price, 8)} · Unrealized PnL ${number(row.unrealized_pnl)} USDT`)); current.append(position);
    }
    else current.append(notice(visibleAccount ? 'Tidak ada posisi aktif.' : 'Posisi aktif belum tersedia.'));
    content.append(current);
    const last = section('KEPUTUSAN TERAKHIR');
    field(last, 'Coin', robot?.last_decision?.symbol ?? '—');
    field(last, 'Status', robot?.last_decision?.status ?? '—');
    // Candidate status is not an exchange order receipt. This field is not exposed by the current DTO.
    field(last, 'Hasil order', robot?.last_decision?.failure_code ?? 'Belum tersedia'); content.append(last, renderConnection());
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
    actions.append(connect, stop); details.append(originLabel, tokenLabel, actions, notice('Token kontrol hanya berada di memori tab. Kunci Binance dan sesi browser Neurobro tetap di VPS.'));
    return details;
  }
  function renderNeurobro() {
    const state = researchState(), research = snapshot?.research, access = currentLoginAccess(state);
    const browserAccess = state.status === 'CONNECTED' || access?.browserAccess;
    const main = section('SESI NEUROBRO');
    const status = element('p', access ? (browserAccess ? 'Akses browser siap' : 'Akses login siap') : state.label, 'dashboard-status neurobro-status');
    status.id = 'neurobroStatus'; status.dataset.status = state.status; status.setAttribute('role', 'status'); main.append(status);
    if (online && research?.provider === 'NEUROBRO_WEB' && timestamp(research.checked_at) !== null) {
      main.append(notice('Diperiksa '+date(research.checked_at), state.status === 'UNAVAILABLE' ? 'warning' : 'normal'));
    }
    if (state.status === 'API_ACTIVE') main.append(notice('Worker masih memakai koneksi sebelumnya. Login browser tersedia setelah pembaruan worker.', 'warning'));
    else if (state.status === 'CONNECTED' || state.status === 'BUSY') {
      main.append(notice('Robot memakai chatbot Neurobro di browser VPS. Sesi login disimpan agar tetap dapat digunakan setelah worker dimulai ulang, sampai sesi berakhir di situs Neurobro.'));
      if (state.status === 'CONNECTED') main.append(notice('Jika halaman chat meminta verifikasi keamanan, tekan BUKA BROWSER NEUROBRO untuk menyelesaikannya pada browser VPS yang sama. Sesi dan percakapan yang ada tetap digunakan.'));
    } else if (access) {
      main.append(notice(browserAccess ? 'Tekan BUKA BROWSER NEUROBRO untuk membuka browser VPS yang sama dan menyelesaikan verifikasi keamanan. Tautan ini hanya dapat digunakan sekali dan berlaku paling lama 60 detik.' : 'Tekan BUKA BROWSER LOGIN untuk membuka tab browser VPS, lalu selesaikan login Neurobro di sana. Tautan ini hanya dapat digunakan sekali dan berlaku paling lama 60 detik.'));
    } else if (state.status === 'LOGIN_IN_PROGRESS') {
      main.append(notice('Akses login masih aktif di VPS. Jika tab login tidak terbuka, tunggu sampai tombol LOGIN NEURO tersedia kembali. Tautan awal berlaku 60 detik; akses browser yang sudah dibuka berlaku 10 menit.'));
    } else {
      main.append(notice(origin && online ? 'Buka browser VPS untuk login Neurobro. Jika ada verifikasi akun, selesaikan pada tab login tersebut.' : 'Hubungkan worker privat untuk membaca sesi dan membuka login Neurobro.', 'warning'));
    }
    if (access) {
      const actions = element('div', undefined, 'dashboard-actions');
      const link = element('a', browserAccess ? 'BUKA BROWSER NEUROBRO' : 'BUKA BROWSER LOGIN'); link.id = 'neurobroLoginOpen';
      link.setAttribute('href', access.url); link.setAttribute('target', '_blank'); link.setAttribute('rel', 'noopener noreferrer');
      link.onclick = event => {
        if (currentLoginAccess() !== access) { event.preventDefault(); paint(); }
      };
      actions.append(link); main.append(actions);
    } else if (state.login || !origin || !online) {
      const actions = element('div', undefined, 'dashboard-actions');
      const button = element('button', loginBusy ? (browserAccess ? 'MEMBUKA BROWSER…' : 'MEMBUKA LOGIN…') : browserAccess ? 'BUKA BROWSER NEUROBRO' : 'LOGIN NEURO');
      button.id = 'neurobroLogin'; button.disabled = !origin || !online || loginBusy;
      button.onclick = startNeurobroLogin; actions.append(button); main.append(actions);
    }
    if (loginMessage) main.append(element('p', loginMessage, 'dashboard-message'));
    main.append(notice('Untuk coin yang dipilih, robot mengirim dua gambar chart realtime Binance Futures: 15 menit dan 1 jam. Login dan CAPTCHA diselesaikan langsung oleh pengguna pada browser VPS.'));
    main.append(notice('Saat sesi logout atau membutuhkan verifikasi, screening dan analisis baru menunggu login. Login tidak mengaktifkan robot atau mengubah posisi Binance.'));
    content.append(main);
  }
  function clearLoginAccess() {
    clearTimeout(loginExpiryTimer); loginExpiryTimer = null;
    if (loginAccess) loginAccess.url = '';
    loginAccess = null;
    const link = document.getElementById('neurobroLoginOpen');
    if (link) { link.removeAttribute('href'); link.hidden = true; }
  }
  function currentLoginAccess(state = researchState()) {
    if (!loginAccess) return null;
    if (!origin || !online || loginAccess.generation !== generation || loginAccess.origin !== origin ||
        ['BUSY', 'API_ACTIVE'].includes(state.status)) {
      clearLoginAccess(); loginMessage = ''; return null;
    }
    if (performance.now() >= loginAccess.expiresAt) {
      const browserAccess = loginAccess.browserAccess;
      clearLoginAccess(); loginMessage = browserAccess ? 'Tautan akses browser kedaluwarsa. Tekan BUKA BROWSER NEUROBRO untuk membuat akses baru.' : 'Tautan login kedaluwarsa. Tunggu tombol LOGIN NEURO tersedia untuk membuat akses baru.'; return null;
    }
    return loginAccess;
  }
  async function startNeurobroLogin() {
    const state = researchState();
    if (!origin || !online || loginBusy || currentLoginAccess(state) || !state.login) return;
    const browserAccess = state.status === 'CONNECTED';
    const currentGeneration = generation, loginOrigin = origin, startedAt = performance.now();
    clearLoginAccess(); loginBusy = true; loginMessage = browserAccess ? 'Menyiapkan akses browser Neurobro…' : 'Menyiapkan akses login Neurobro…'; paint();
    try {
      const result = await request('/neurobro/login/start', 'POST', {});
      if (currentGeneration !== generation || origin !== loginOrigin) return;
      if (!result || typeof result.ticket !== 'string' || !/^[0-9a-f]{64}$/.test(result.ticket) || result.path !== '/neurobro/login' ||
          !Number.isInteger(result.expires_in) || result.expires_in < 1 || result.expires_in > 60) throw Error('Respons login worker tidak sesuai kontrak.');
      const expiresAt = startedAt + result.expires_in * 1000;
      if (performance.now() >= expiresAt) throw Error('Akses login kedaluwarsa sebelum siap.');
      loginAccess = { url: loginOrigin+'/neurobro/login#'+result.ticket, origin: loginOrigin, generation: currentGeneration, expiresAt, browserAccess };
      loginExpiryTimer = setTimeout(() => {
        if (currentGeneration !== generation) return;
        clearLoginAccess(); loginMessage = browserAccess ? 'Tautan akses browser kedaluwarsa. Tekan BUKA BROWSER NEUROBRO untuk membuat akses baru.' : 'Tautan login kedaluwarsa. Tunggu tombol LOGIN NEURO tersedia untuk membuat akses baru.'; paint();
      }, expiresAt - performance.now());
      loginMessage = '';
    } catch (error) {
      if (currentGeneration === generation) { clearLoginAccess(); loginMessage = error.message+(browserAccess ? ' Akses browser belum tersedia.' : ' Login belum terhubung.'); }
    } finally {
      if (currentGeneration === generation) { loginBusy = false; paint(); poll(); }
    }
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
  function paint(force = false) {
    currentLoginAccess();
    paintStats();
    if (panel.hidden) return;
    const focused = document.activeElement;
    if (!force && view === 'billing' && !billingBusy && ['INPUT', 'SELECT'].includes(focused?.tagName) && content.querySelector('#'+focused.id) === focused) return;
    // Keep unfinished credential/risk input intact while worker facts refresh.
    if (['robot', 'neurobro'].includes(view) && ['apiOrigin', 'apiToken', 'robotRisk'].includes(focused?.id)) {
      const toggle = document.getElementById('robotToggle'); if (toggle) toggle.disabled = !origin || !online || saving || !snapshot;
      for (const id of ['robotRisk', 'robotRiskSave']) {
        const control = document.getElementById(id); if (control) control.disabled = !origin || !online || saving || !snapshot;
      }
      const note = content.querySelector('.dashboard-message'); if (note) note.textContent = message;
      return;
    }
    const calendarFocus = view === 'calendar' && focused?.id?.startsWith('pnl-day-') ? focused.id : null;
    panel.classList.toggle('calendar-panel', view === 'calendar');
    panel.classList.toggle('billing-panel', view === 'billing');
    title.textContent = views[view]; content.replaceChildren();
    ({ robot: renderRobot, calendar: renderCalendar, neurobro: renderNeurobro, billing: renderBilling })[view]();
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
    clearLoginAccess();
    generation++; revision++; clearInterval(timer); timer = null; token = ''; origin = ''; snapshot = null;
    researchOwnerNotice = null;
    billingMonth = billingRecord = billingDraft = billingHistory = null; billingEditing = null; billingScheduleDrafts = {}; billingBusy = false; billingMessage = '';
    polling = false; saving = false; refreshRequested = false; online = false;
    riskDraft = null; resetRiskOnRefresh = null; calendarMonth = null; selectedDay = null;
    loginBusy = false; loginMessage = '';
    const field = document.getElementById('apiToken'); if (field) field.value = ''; publish();
  }
  menuToggle.addEventListener('click', () => { drawer.hidden = !drawer.hidden; menuToggle.setAttribute('aria-expanded', String(!drawer.hidden)); });
  drawer.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {
    view = button.dataset.view; drawer.querySelectorAll('[data-view]').forEach(item => item.classList.toggle('active', item === button));
    panel.hidden = false; panel.scrollTop = 0; drawer.hidden = true; menuToggle.setAttribute('aria-expanded', 'false'); paint();
    if (view === 'billing' && !billingRecord) loadBilling();
  }));
  document.getElementById('panelClose').addEventListener('click', () => { panel.hidden = true; });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { publish(); paint(); poll(); } });
  window.addEventListener('pagehide', () => { disconnect(); });
  paintStats();
})();
