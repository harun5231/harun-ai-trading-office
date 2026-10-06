/* Only worker control tokens enter this panel. Provider secrets stay on the server. */
(()=>{
 'use strict';
 const panel=document.getElementById('infoPanel'),body=document.getElementById('panelContent'),title=document.getElementById('panelTitle');
 const menu=document.createElement('button');menu.id='neuroapiMenu';menu.className='menu-item';menu.textContent='WORKER OFFLINE · NEUROAPI';
 document.getElementById('menuDrawer').insertBefore(menu,document.querySelector('.drawer-note'));
 const robotMenu=document.createElement('button');robotMenu.id='robotMenu';robotMenu.className='menu-item';robotMenu.textContent='ROBOT TRADING · BELUM TERHUBUNG';
 document.getElementById('menuDrawer').insertBefore(robotMenu,document.querySelector('.drawer-note'));
 let robotData=null,robotBusy=false,cardKey='',robotRevision=0;
 const selectedScenarios=new Map();
 let origin='',configured='',token='',timer=null,active=false,busy=false,online=false,generation=0;
 let status='NEUROAPI_UNCHECKED',note='Hubungkan worker privat. API key hanya dimasukkan di VPS.';
 const scenarios=[['FULL_TP','Entry penuh → TP'],['FULL_SL','Entry penuh → SL'],['PARTIAL_TP','Entry sebagian → TP'],['PROTECTION_FAILURE','Proteksi gagal'],['UNCERTAIN_ENTRY','Status entry tidak pasti']];
 const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const configUrl=new URL('worker-config.json',document.currentScript?.src||document.baseURI);
 const validOrigin=s=>{const u=new URL(s);if(u.protocol!=='https:'||u.username||u.password||u.search||u.hash||u.pathname!=='/')throw Error('Gunakan origin HTTPS worker.');return u.origin;};
 fetch(configUrl,{credentials:'omit',cache:'no-store'}).then(r=>r.ok?r.json():null).then(c=>{if(c?.worker_origin)configured=validOrigin(c.worker_origin);}).catch(()=>{});
 function paint(){
  menu.textContent='WORKER '+(online?'ONLINE':'OFFLINE')+' · '+status.replaceAll('_',' ');
  robotMenu.textContent=robotData?'ROBOT '+(robotData.robot_on?'ON':'OFF')+' · '+robotData.bot_status:'ROBOT TRADING · BELUM TERHUBUNG';
  if(!active)return;
  document.getElementById('apiStatus').textContent=menu.textContent;document.getElementById('apiNote').textContent=note;
  for(const id of ['apiCheck','apiRun'])document.getElementById(id).disabled=!origin||busy;
  paintRobot();
 }
 async function request(path,method='GET',value){
  const headers={Authorization:'Bearer '+token};if(value!==undefined)headers['Content-Type']='application/json';
  const r=await fetch(origin+path,{method,headers,body:value===undefined?undefined:JSON.stringify(value),credentials:'omit',redirect:'error',cache:'no-store',signal:AbortSignal.timeout(10000)});
  if(!r.ok)throw Error('Worker HTTP '+r.status);
  const data=await r.json();if(data.mode!=='DRY_RUN'||data.live_enabled!==false||data.live_execution===true)throw Error('Mode worker tidak valid.');return data;
 }
 async function poll(){
  if(!origin)return;const g=generation,revision=robotRevision;
  try{
   const [s,h,snapshot,robot]=await Promise.all([request('/neuroapi/status'),request('/health'),request('/snapshot'),request('/robot/status')]);if(g!==generation||robotBusy||revision!==robotRevision)return;
   robotData=robot;online=h.status==='ONLINE';busy=s.busy===true;status=busy?snapshot.status:s.status;
   note=busy?'Worker memproses permintaan. Jangan ulangi siklus.':'Binance GET-only. OK hanya menyimpan approval; order live tetap nonaktif.';
   const dashboard={...snapshot,balance:robot.usdt_wallet_balance??snapshot.balance,active_positions:robot.running_positions??snapshot.active_positions};
   window.dispatchEvent(new CustomEvent('workerSnapshot',{detail:dashboard}));paint();
  }catch(e){if(g===generation){online=false;note=e.message;paint();}}
 }
 async function action(name){
  if(busy||!origin)return;const g=generation;busy=true;paint();
  try{await request('/neuroapi/'+name,'POST');if(g!==generation)return;note='Permintaan diterima worker.';}catch(e){if(g!==generation)return;note=e.message;busy=false;}
  paint();poll();
 }
 function disconnect(){generation++;robotRevision++;origin='';token='';robotData=null;robotBusy=false;cardKey='';selectedScenarios.clear();clearInterval(timer);online=false;busy=false;status='NEUROAPI_UNCHECKED';const field=document.getElementById('apiToken');if(field)field.value='';}
 function manualTicket(row){
  const t=row.ticket;
  return ['SETUP_READY','APPROVED'].includes(row.status)&&t?.execution_mode==='MANUAL_ONLY'&&t.submission_enabled===false&&t.review_required===true&&t.entry&&t.take_profit&&t.stop_loss?t:null;
 }
 function ticketText(t){
  const entry=t.entry,tp=t.take_profit,sl=t.stop_loss;
  return ['TIKET BINANCE FUTURES · KIRIM MANUAL SETELAH DITINJAU',
   'Setup: '+t.setup_id+' · '+t.symbol+' · '+t.side,
   'Sumber setup: '+(t.source_created_at||'?')+' · Business day: '+(t.business_day||'?'),
   'Margin: '+t.margin_mode+' · Leverage: '+t.leverage+'x · Position side: '+entry.position_side,
   'ENTRY: '+entry.order_type+' '+entry.side+' · Harga: '+entry.price+' · Quantity (base asset, bukan USDT): '+entry.quantity+' · TIF: '+entry.time_in_force,
   'TP: '+tp.order_type+' '+tp.side+' · Trigger: '+tp.trigger_price+' · Working type: '+tp.working_type+' · Close position: '+tp.close_position,
   'SL: '+sl.order_type+' '+sl.side+' · Trigger: '+sl.trigger_price+' · Working type: '+sl.working_type+' · Close position: '+sl.close_position,
   'Risk target: '+t.risk_target_usdt+' USDT · Risk setup: '+t.risk+' USDT · RR: '+t.rr,
   'Perkiraan rugi: '+t.estimated_loss_usdt+' USDT · Perkiraan profit: '+t.estimated_profit_usdt+' USDT',
   'Perkiraan tidak termasuk biaya, funding, dan slippage.',
   t.manual_review_note||'Wajib meninjau tiket ini sebelum pengiriman manual.',
   'Periksa ulang saldo, posisi manual, harga, quantity, dan proteksi di Binance. Office tidak mengirim order.'].join('\n');
 }
 function appendTicket(card,row,t){
  const heading=document.createElement('strong');heading.textContent='TIKET MANUAL BINANCE';card.append(heading);
  const parameters=document.createElement('p');parameters.className='robot-manual-ticket';parameters.style.whiteSpace='pre-wrap';parameters.textContent=ticketText(t);card.append(parameters);
  if(row.account_review){
   const review=row.account_review,check=document.createElement('p');check.className='robot-account-review';
   check.textContent='Pemeriksaan akun: '+(review.checked_at||'belum tersedia')+' · Slot: '+(review.available_slots??'?')+' · Simbol sudah memiliki exposure: '+(review.symbol_exposed===true?'YA':review.symbol_exposed===false?'TIDAK':'BELUM DIKETAHUI')+' · Setup hari ini: '+(review.setup_from_current_day?'YA':'TIDAK')+'. Periksa ulang di Binance sebelum pengiriman manual.';card.append(check);
  }
  if(/^[A-Z0-9]{1,24}$/.test(t.symbol)){
   const binance=document.createElement('a');binance.textContent='BUKA BINANCE · TINJAU MANUAL';binance.href='https://www.binance.com/en/futures/'+t.symbol;binance.target='_blank';binance.rel='noopener noreferrer';card.append(binance);
  }
  const feedback=document.createElement('p');feedback.className='robot-ticket-feedback';feedback.setAttribute('role','status');
  const actions=document.createElement('div');actions.className='wf-actions';
  const copy=document.createElement('button');copy.className='robot-ticket-copy';copy.textContent='SALIN TIKET';copy.disabled=!origin||robotBusy;
  copy.onclick=async()=>{
   const g=generation;copy.disabled=true;
   try{if(!navigator.clipboard?.writeText)throw Error('Clipboard tidak tersedia. Pilih teks tiket lalu salin manual.');await navigator.clipboard.writeText(ticketText(t));if(g!==generation)return;feedback.textContent='Tiket disalin. Tinjau dan kirim sendiri melalui Binance.';}
   catch(e){if(g===generation)feedback.textContent='Gagal menyalin: '+e.message;}
   finally{if(g===generation)copy.disabled=!origin||robotBusy;}
  };
  actions.append(copy);card.append(actions,feedback);
  const label=document.createElement('label');label.textContent='Skenario simulasi lokal';label.style.display='block';
  const scenario=document.createElement('select');scenario.className='robot-simulation-scenario';scenario.setAttribute('aria-label','Skenario simulasi '+row.symbol);scenario.disabled=robotBusy;
  Object.assign(scenario.style,{display:'block',width:'100%',maxWidth:'100%',minHeight:'44px',marginTop:'4px',padding:'9px',fontSize:'16px',background:'#122934',color:'#fff',border:'1px solid var(--line)',borderRadius:'7px'});
  for(const [value,text] of scenarios){const option=document.createElement('option');option.value=value;option.textContent=text;scenario.append(option);}
  const selected=selectedScenarios.get(row.id)||row.simulation?.scenario;
  if(scenarios.some(([value])=>value===selected))scenario.value=selected;
  scenario.onchange=()=>selectedScenarios.set(row.id,scenario.value);label.append(scenario);card.append(label);
  const test=document.createElement('button');test.className='robot-simulation-run';test.textContent='UJI SIMULASI';test.disabled=!origin||robotBusy;
  test.onclick=()=>robotAction('/robot/simulation',{setup_id:row.id,scenario:scenario.value});card.append(test);
  const note=document.createElement('p');note.textContent='Simulasi lokal sintetis tanpa API key baru dan tanpa order nyata. Tidak memakai harga/fill aktual; biaya, funding, dan slippage tidak dihitung.';card.append(note);
 }
 function appendSimulation(card,simulation){
  if(simulation?.mode!=='SIMULATION'||simulation.real_order_submitted!==false||simulation.live_execution!==false)return;
  const trace=document.createElement('p');trace.className='robot-simulation-trace';trace.style.whiteSpace='pre-wrap';
  const lines=['SIMULASI SINTETIS · '+simulation.scenario+' · '+simulation.status,
   'Quantity terisi model: '+simulation.filled_quantity+' · Sisa: '+simulation.remaining_quantity+' · Dibatalkan: '+simulation.canceled_quantity];
  if(simulation.pnl_usdt!==null&&simulation.pnl_usdt!==undefined)lines.push('PnL model: '+simulation.pnl_usdt+' USDT (tanpa biaya, funding, slippage)');
  for(const step of simulation.steps||[])lines.push(step.sequence+'. '+step.event+' · '+step.state+' · '+step.detail);
  if(simulation.blocks_next)lines.push('Simulasi belum aman/selesai; periksa jejak sebelum mengulang.');
  trace.textContent=lines.join('\n');card.append(trace);
 }
 function paintRobot(){
  const toggle=document.getElementById('robotToggle'),risk=document.getElementById('robotRisk');
  if(!toggle)return;
  toggle.disabled=!origin||robotBusy||!robotData;
  toggle.textContent='ROBOT TRADING: '+(robotData?(robotData.robot_on?'ON':'OFF'):'BELUM TERHUBUNG');
  document.getElementById('robotRiskSave').disabled=!origin||robotBusy||!robotData;
  if(robotData&&document.activeElement!==risk)risk.value=robotData.risk_target_usdt;
  document.getElementById('robotStatus').textContent=robotData?
   'RISK PER SL: '+robotData.risk_target_usdt+' USDT · FUTURES BALANCE: '+(robotData.usdt_wallet_balance??'?')+' USDT · RUNNING FUTURES: '+(robotData.running_positions??'?')+' / 2 · BOT ENTRIES TODAY: '+robotData.bot_entries_today+' / 2 · AVAILABLE SLOTS: '+(robotData.available_slots??'?')+' · MANUAL EXPOSURE: '+(robotData.manual_exposure.join(', ')||'—')+' · BOT STATUS: '+robotData.bot_status+(robotData.wait_reason?' · WAIT: '+robotData.wait_reason:'')+(robotData.failure_code?' · '+robotData.failure_code:'')+(robotData.account_failure_code?' · ACCOUNT: '+robotData.account_failure_code:''):'Hubungkan worker untuk membaca status.';
  const rows=robotData?.setups||[],key=JSON.stringify(rows);
  if(key===cardKey)return;cardKey=key;
  const cards=document.getElementById('robotCards');cards.replaceChildren();
  for(const row of rows){
   const card=document.createElement('div');card.className='wf-note';
   const label=document.createElement('strong');label.textContent=row.symbol+' · '+row.status;card.append(label);
   const details=document.createElement('p');details.textContent=row.entry?
    row.side+' · LIMIT '+row.entry+' · TP '+row.tp+' · SL '+row.sl+' · Qty '+row.execution_quantity+' · Risk target '+row.risk_target_usdt+' USDT · Actual risk '+row.risk+' USDT · RR '+row.rr:
    (row.failure_code||row.status);details.style.overflowWrap='anywhere';card.append(details);
   if(row.status==='SETUP_READY'){
    const actions=document.createElement('div');actions.className='wf-actions';
    for(const [text,decision] of [['OK','APPROVED'],['TIDAK','USER_REJECTED']]){
     const button=document.createElement('button');button.textContent=text;button.disabled=robotBusy;
     button.onclick=()=>robotAction('/robot/approval',{setup_id:row.id,decision});actions.append(button);
    }card.append(actions);
   }
   const ticket=manualTicket(row);if(ticket)appendTicket(card,row,ticket);
   appendSimulation(card,row.simulation);cards.append(card);
  }
 }
 async function robotAction(path,value){
  if(!origin||robotBusy)return;robotRevision++;robotBusy=true;cardKey='';paint();
  const g=generation;
  try{const result=await request(path,'POST',value);if(g!==generation)return;robotData=result;note=path==='/robot/simulation'?'Simulasi lokal tersimpan. Tidak ada order Binance.':'Tersimpan. Live submission tetap DISABLED.';}
  catch(e){if(g===generation)note=e.message;}
  finally{if(g===generation){robotBusy=false;cardKey='';paint();}}
 }
 menu.onclick=()=>{
  active=true;title.dataset.workflow='false';title.textContent='NEUROAPI · DRY RUN';panel.hidden=false;
  document.getElementById('menuDrawer').hidden=true;document.getElementById('menuToggle').setAttribute('aria-expanded','false');
  cardKey='';body.innerHTML=`<div class="wf-note"><b id="apiStatus"></b><p id="apiNote"></p><small>NeuroAPI Starter · smart · Binance Futures public data</small></div>
   <div class="wf-actions"><button id="apiCheck">CEK API</button><button id="apiRun">JALANKAN DRY RUN</button></div>
   <p>CEK API tidak mengirim prompt. JALANKAN DRY RUN hanya meminta evaluasi coordinator saat ROBOT ON. OFF menghentikan riset baru; request yang sudah terkirim dapat selesai. Tidak ada order Binance.</p>
   <div class="wf-note"><div class="wf-actions"><button id="robotToggle">ROBOT TRADING: OFF</button></div>
   <label>RISK PER SL (USDT, lebih dari 0 sampai 100)<input id="robotRisk" inputmode="decimal" type="text" value="5" maxlength="64"></label><div class="wf-actions"><button id="robotRiskSave">SIMPAN RISIKO</button></div>
   <p id="robotStatus" aria-live="polite"></p><small>ON menjalankan riset NeuroAPI berbayar, bukan izin eksekusi. Perubahan risiko hanya untuk analisis baru. OK menyimpan approval saja dan tidak mengirim ke Binance. Tiket entry/TP/SL harus kamu tinjau dan kirim sendiri. UJI SIMULASI hanya membuat jejak sintetis lokal. TIDAK dapat memicu replacement saat ON.</small></div><div id="robotCards" aria-live="polite"></div>
   <details ${origin?'':'open'}><summary>Koneksi worker privat</summary><label>Origin HTTPS worker<input id="apiOrigin" type="url" value="${esc(origin||configured)}"></label><label>Token kontrol worker<input id="apiToken" type="password" autocomplete="off" placeholder="Bukan API key Neurobro"></label><div class="wf-actions"><button id="apiConnect">HUBUNGKAN WORKER</button><button id="apiDisconnect">PUTUSKAN</button></div><small>Token kontrol hanya di memori tab. Jangan masukkan API key Neurobro/Binance.</small></details>`;
  document.getElementById('robotToggle').onclick=()=>robotAction('/robot/settings',{robot_on:!robotData.robot_on});
  document.getElementById('robotRiskSave').onclick=()=>robotAction('/robot/settings',{risk_target_usdt:document.getElementById('robotRisk').value});
  document.getElementById('apiCheck').onclick=()=>action('check');document.getElementById('apiRun').onclick=()=>action('run');
  document.getElementById('apiConnect').onclick=()=>{
   try{const u=validOrigin(document.getElementById('apiOrigin').value),t=document.getElementById('apiToken').value.trim();if(t.length<32)throw Error('Token kontrol minimal 32 karakter.');disconnect();origin=u;token=t;document.getElementById('apiToken').value='';poll();timer=setInterval(poll,4000);}catch(e){note=e.message;paint();}
  };
  document.getElementById('apiDisconnect').onclick=()=>{disconnect();note='Dashboard terputus; worker tetap berjalan.';paint();};paint();
 };
 robotMenu.onclick=()=>menu.onclick();
 document.querySelectorAll('[data-view],#workflowMenu,#panelClose').forEach(b=>b.addEventListener('click',()=>{active=false;}));
})();
