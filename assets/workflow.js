/* Read-only dashboard: never controls or submits exchange orders. */
(() => {
 'use strict';
 let snapshot=null, endpoint='', token='', timer=null, busy=false, connected=false, message='Worker belum terhubung. Angka akun belum tersedia.';
 const allowedStates=new Set(['IDLE','NEUROAPI_NOT_CONFIGURED','NEUROAPI_CONNECTED','SCREENING','COINS_SELECTED','MARKET_DATA','ANALYZING_COIN_1','ANALYZING_COIN_2','VALIDATING','DRY_RUN_READY','REJECTED','ORDER_READY','POSITION_OPEN','MONITORING','CLOSED','LOCKED','ERROR','LONG','SHORT','HOLD','REPLACEMENT_SCREENING','REPLACEMENT_SELECTED','INSUFFICIENT_ACTIONABLE_SETUPS']);
 const panel=document.getElementById('infoPanel'), content=document.getElementById('panelContent'),title=document.getElementById('panelTitle');
 const button=document.createElement('button');button.id='workflowMenu';button.className='menu-item';button.textContent='◇ Workflow DRY RUN';
 document.getElementById('menuDrawer').insertBefore(button,document.querySelector('.drawer-note'));
 const esc=value=>String(value??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const fmt=value=>value===null||value===undefined?'—':Number(value).toLocaleString('en-US',{maximumFractionDigits:4});
 function validate(data){
  if(!data||data.schema_version!==1||data.mode!=='DRY_RUN'||data.live_enabled!==false||!['NEUROAPI_DRY_RUN'].includes(data.source)||!allowedStates.has(data.status))throw Error('Snapshot tidak sesuai skema DRY RUN.');
  if(!Number.isInteger(data.trades_today)||data.trades_today<0||data.trades_today>2||!Number.isInteger(data.active_positions)||data.active_positions<0)throw Error('Jumlah trade/posisi tidak valid.');
  if(!Array.isArray(data.trades)||!Array.isArray(data.events)||data.events.length>100||data.trades.length>10000)throw Error('Laporan tidak valid/terlalu besar.');
  if(!Number.isFinite(Date.parse(data.generated_at))||Date.parse(data.generated_at)>Date.now()+60000)throw Error('Timestamp tidak valid.');
  for(const field of ['balance','pnl_today'])if(data[field]!==null&&!Number.isFinite(Number(data[field])))throw Error('Nilai statistik tidak valid.');
  if(data.events.some(e=>typeof e.state!=='string'||typeof e.message!=='string'||typeof e.agent!=='string'))throw Error('Activity log tidak valid.');
  for(const t of data.trades)if(!t.plan||t.plan.mode!=='DRY_RUN'||!['ORDER_READY','POSITION_OPEN','CLOSED'].includes(t.state)||typeof t.plan.symbol!=='string')throw Error('Trade tidak valid.');
  return data;
 }
 function updateStats(){
  const values=document.querySelectorAll('.stats .stat b');
  [snapshot?fmt(snapshot.balance):'—',snapshot?fmt(snapshot.pnl_today):'—',`${snapshot?.trades_today??0} / 2`,snapshot?String(snapshot.active_positions):'—'].forEach((v,i)=>{values[i].textContent=v;});
  values[1].classList.toggle('positive',!!snapshot&&Number(snapshot.pnl_today)>=0);
  document.querySelector('.brand small').textContent=snapshot?`● DRY RUN · ${snapshot.status}${snapshot.locked?' · LOCKED':''}`:'● VISUAL SIMULASI · WORKER BELUM TERHUBUNG';
 }
 function render(){
  if(title.dataset.workflow!=='true')return;
  const s=snapshot,age=s?Math.max(0,Math.floor((Date.now()-Date.parse(s.generated_at))/1000)):null;
  const rows=s?.trades.slice(-20).reverse().map(t=>`<div class="coin"><div><b>${esc(t.plan.symbol)} · ${esc(t.plan.side)} · ${esc(t.state)}</b><small>ENTRY ${esc(t.plan.entry)} · TP ${esc(t.plan.tp)} · SL ${esc(t.plan.sl)}<br>Execution qty ${esc(t.plan.execution_quantity??t.plan.quantity)} · Neurobro qty (audit) ${esc(t.plan.neurobro_position_size)} · Risiko ${esc(t.plan.risk)} USDT · CROSS / 75x (rencana paper)<br>PNL ${esc(t.pnl??'—')} · ${esc(t.source)}</small></div></div>`).join('')||'<p>Belum ada order paper.</p>';
  content.innerHTML=`<div class="wf-note"><b>DRY RUN ONLY${s?.locked?' · LOCKED':''}</b><br>${esc(message)}${s?`<br>Status: ${esc(s.status)} · Sumber: ${esc(s.source)}<br>Snapshot ${esc(s.generated_at)} (${age}s lalu)${age>120?' · DATA LAMA':''}`:''}<br>Saldo nyata belum terhubung. Tidak ada tombol atau eksekusi order live.</div>
  <div class="wf-actions"><label class="wf-file">Buka snapshot JSON<input id="wfFile" type="file" accept=".json,application/json"></label><button id="wfExport" ${s?'':'disabled'}>Unduh laporan</button></div>
  <details><summary>Koneksi worker privat (baca saja)</summary><label>URL HTTPS /snapshot<input id="wfUrl" type="url" placeholder="https://worker-anda/snapshot"></label><label>Token baca sementara<input id="wfToken" type="password" autocomplete="off" placeholder="Tidak disimpan"></label><div class="wf-actions"><button id="wfConnect">Hubungkan</button><button id="wfDisconnect">Putuskan</button></div><small>Token hanya di memori tab. Jangan masukkan password Binance/Neurobro atau API key di sini. Worker harus sudah berjalan di server privat.</small></details>
  <h3 style="margin-top:18px">ORDER & LAPORAN PAPER</h3>${rows}<h3 style="margin-top:18px">STATUS AGENT & AKTIVITAS</h3>${s?.events.slice(-30).reverse().map(e=>`<div class="event"><time>${esc(e.at.slice(11,19))} UTC</time><div class="role">${esc(e.agent)}<br>${esc(e.state)}</div><div class="msg">${esc(e.message)}</div></div>`).join('')||'<p>Menunggu worker.</p>'}`;
  document.getElementById('wfFile').onchange=async e=>{
   try{const file=e.target.files[0];if(!file)return;if(file.size>2000000)throw Error('Snapshot terlalu besar.');stop();snapshot=validate(JSON.parse(await file.text()));message='Snapshot diimpor; bukan koneksi real-time.';updateStats();render();}catch(err){message=err.message;render();}
  };
  document.getElementById('wfExport').onclick=()=>{if(!snapshot)return;const url=URL.createObjectURL(new Blob([JSON.stringify(snapshot,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='harun-dry-run-report.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  document.getElementById('wfConnect').onclick=()=>{
   try{
    const url=new URL(document.getElementById('wfUrl').value);
    if(url.username||url.password||url.search||url.hash||url.pathname!=='/snapshot')throw Error('Gunakan URL /snapshot tanpa kredensial/query.');
    if(url.protocol!=='https:')throw Error('Koneksi dashboard memerlukan HTTPS.');
    const value=document.getElementById('wfToken').value;if(value.length<32)throw Error('Token baca minimal 32 karakter.');
    stop();endpoint=url.href;token=value;connected=true;poll();timer=setInterval(poll,5000);
   }catch(err){message=err.message;render();}
  };
  document.getElementById('wfDisconnect').onclick=()=>{stop();message='Koneksi diputus. Snapshot terakhir tetap terlihat sebagai arsip.';render();};
 }
 function stop(){connected=false;clearInterval(timer);timer=null;token='';endpoint='';}
 async function poll(){
  if(busy||!connected)return;busy=true;const requestedEndpoint=endpoint;
  try{const r=await fetch(endpoint,{headers:{Authorization:`Bearer ${token}`},cache:'no-store',credentials:'omit',redirect:'error',signal:AbortSignal.timeout(8000)});if(!r.ok)throw Error(`Worker HTTP ${r.status}`);const data=validate(await r.json());if(!connected||endpoint!==requestedEndpoint)return;snapshot=data;message=Date.now()-Date.parse(data.generated_at)>120000?'Worker terjangkau, tetapi snapshot sudah lama.':'Terhubung ke snapshot worker privat (baca saja).';updateStats();render();}
  catch(e){if(connected){message='ERROR koneksi worker; snapshot terakhir bukan data terkini.';render();}}
  finally{busy=false;}
 }
 button.onclick=()=>{title.dataset.workflow='true';title.textContent='WORKFLOW TRADING · DRY RUN';panel.hidden=false;document.getElementById('menuDrawer').hidden=true;document.getElementById('menuToggle').setAttribute('aria-expanded','false');render();};
 document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>{
  title.dataset.workflow='false';
  // Keep the original panels and controls, add a link to actual worker records.
  if(['trading','reports','staff','settings'].includes(b.dataset.view)){
   const note=document.createElement('div');note.className='wf-note';note.textContent='Panel visual simulasi. Status dan laporan worker tersedia di Workflow DRY RUN.';
   const link=document.createElement('button');link.textContent='Buka workflow';link.onclick=()=>button.click();note.appendChild(link);content.prepend(note);
  }
 }));
 document.addEventListener('visibilitychange',()=>{if(!document.hidden&&connected)poll();});
 window.addEventListener('workerSnapshot',e=>{try{snapshot=validate(e.detail);message='Terhubung ke worker API privat.';updateStats();render();}catch(_){message='Snapshot ditolak.';}});
 updateStats();
})();
