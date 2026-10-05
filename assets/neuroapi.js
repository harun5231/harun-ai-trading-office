/* Only worker control tokens enter this panel. Provider secrets stay on the server. */
(()=>{
 'use strict';
 const panel=document.getElementById('infoPanel'),body=document.getElementById('panelContent'),title=document.getElementById('panelTitle');
 const menu=document.createElement('button');menu.id='neuroapiMenu';menu.className='menu-item';menu.textContent='WORKER OFFLINE · NEUROAPI';
 document.getElementById('menuDrawer').insertBefore(menu,document.querySelector('.drawer-note'));
 const robotMenu=document.createElement('button');robotMenu.id='robotMenu';robotMenu.className='menu-item';robotMenu.textContent='ROBOT TRADING · BELUM TERHUBUNG';
 document.getElementById('menuDrawer').insertBefore(robotMenu,document.querySelector('.drawer-note'));
 let robotData=null,robotBusy=false,cardKey='';
 let origin='',configured='',token='',timer=null,active=false,busy=false,online=false,generation=0;
 let status='NEUROAPI_UNCHECKED',note='Hubungkan worker privat. API key hanya dimasukkan di VPS.';
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
  const data=await r.json();if(data.mode!=='DRY_RUN'||data.live_enabled!==false)throw Error('Mode worker tidak valid.');return data;
 }
 async function poll(){
  if(!origin)return;const g=generation;
  try{
   const [s,h,snapshot,robot]=await Promise.all([request('/neuroapi/status'),request('/health'),request('/snapshot'),request('/robot/status')]);if(g!==generation)return;
   robotData=robot;online=h.status==='ONLINE';busy=s.busy===true;status=busy?snapshot.status:s.status;
   note=busy?'Worker memproses permintaan. Jangan ulangi siklus.':'Binance GET-only. OK hanya menyimpan approval; order live tetap nonaktif.';
   window.dispatchEvent(new CustomEvent('workerSnapshot',{detail:snapshot}));paint();
  }catch(e){if(g===generation){online=false;note=e.message;paint();}}
 }
 async function action(name){
  if(busy||!origin)return;busy=true;paint();
  try{await request('/neuroapi/'+name,'POST');note='Permintaan diterima worker.';}catch(e){note=e.message;busy=false;}
  paint();poll();
 }
 function disconnect(){generation++;origin='';token='';robotData=null;cardKey='';clearInterval(timer);online=false;busy=false;status='NEUROAPI_UNCHECKED';}
 function paintRobot(){
  const toggle=document.getElementById('robotToggle'),risk=document.getElementById('robotRisk');
  if(!toggle)return;
  toggle.disabled=!origin||robotBusy||!robotData;
  toggle.textContent='ROBOT TRADING: '+(robotData?(robotData.robot_on?'ON':'OFF'):'BELUM TERHUBUNG');
  document.getElementById('robotRiskSave').disabled=!origin||robotBusy||!robotData;
  if(robotData&&document.activeElement!==risk)risk.value=robotData.risk_target_usdt;
  document.getElementById('robotStatus').textContent=robotData?
   'RISK PER SL: '+robotData.risk_target_usdt+' USDT · RUNNING FUTURES: '+(robotData.running_positions??'?')+' / 2 · BOT ENTRIES TODAY: '+robotData.bot_entries_today+' / 2 · AVAILABLE SLOTS: '+(robotData.available_slots??'?')+' · MANUAL EXPOSURE: '+(robotData.manual_exposure.join(', ')||'—')+' · BOT STATUS: '+robotData.bot_status+(robotData.failure_code?' · '+robotData.failure_code:''):'Hubungkan worker untuk membaca status.';
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
   }cards.append(card);
  }
 }
 async function robotAction(path,value){
  if(!origin||robotBusy)return;robotBusy=true;cardKey='';paint();
  const g=generation;
  try{const result=await request(path,'POST',value);if(g!==generation)return;robotData=result;note='Tersimpan. Live submission tetap DISABLED.';}
  catch(e){if(g===generation)note=e.message;}
  finally{robotBusy=false;cardKey='';paint();}
 }
 menu.onclick=()=>{
  active=true;title.dataset.workflow='false';title.textContent='NEUROAPI · DRY RUN';panel.hidden=false;
  document.getElementById('menuDrawer').hidden=true;document.getElementById('menuToggle').setAttribute('aria-expanded','false');
  cardKey='';body.innerHTML=`<div class="wf-note"><b id="apiStatus"></b><p id="apiNote"></p><small>NeuroAPI Starter · smart · Binance Futures public data</small></div>
   <div class="wf-actions"><button id="apiCheck">CEK API</button><button id="apiRun">JALANKAN DRY RUN</button></div>
   <p>CEK API tidak mengirim prompt. JALANKAN DRY RUN hanya meminta evaluasi coordinator saat ROBOT ON. OFF menghentikan riset baru; request yang sudah terkirim dapat selesai. Tidak ada order Binance.</p>
   <div class="wf-note"><div class="wf-actions"><button id="robotToggle">ROBOT TRADING: OFF</button></div>
   <label>RISK PER SL (USDT, lebih dari 0 sampai 100)<input id="robotRisk" inputmode="decimal" type="text" value="5" maxlength="64"></label><div class="wf-actions"><button id="robotRiskSave">SIMPAN RISIKO</button></div>
   <p id="robotStatus" aria-live="polite"></p><small>ON menjalankan riset NeuroAPI berbayar, bukan izin eksekusi. Perubahan risiko hanya untuk analisis baru. OK menyimpan approval saja. TIDAK dapat memicu replacement saat ON.</small></div><div id="robotCards" aria-live="polite"></div>
   <details ${origin?'':'open'}><summary>Koneksi worker privat</summary><label>Origin HTTPS worker<input id="apiOrigin" type="url" value="${esc(origin||configured)}"></label><label>Token kontrol worker<input id="apiToken" type="password" autocomplete="off" placeholder="Bukan API key Neurobro"></label><div class="wf-actions"><button id="apiConnect">HUBUNGKAN WORKER</button><button id="apiDisconnect">PUTUSKAN</button></div><small>Token kontrol hanya di memori tab. Jangan masukkan API key Neurobro/Binance.</small></details>`;
  document.getElementById('robotToggle').onclick=()=>robotAction('/robot/settings',{robot_on:!robotData.robot_on});
  document.getElementById('robotRiskSave').onclick=()=>robotAction('/robot/settings',{risk_target_usdt:document.getElementById('robotRisk').value});
  document.getElementById('apiCheck').onclick=()=>action('check');document.getElementById('apiRun').onclick=()=>action('run');
  document.getElementById('apiConnect').onclick=()=>{
   try{const u=validOrigin(document.getElementById('apiOrigin').value),t=document.getElementById('apiToken').value;if(t.length<32)throw Error('Token kontrol minimal 32 karakter.');disconnect();origin=u;token=t;document.getElementById('apiToken').value='';poll();timer=setInterval(poll,4000);}catch(e){note=e.message;paint();}
  };
  document.getElementById('apiDisconnect').onclick=()=>{disconnect();note='Dashboard terputus; worker tetap berjalan.';paint();};paint();
 };
 robotMenu.onclick=()=>menu.onclick();
 document.querySelectorAll('[data-view],#workflowMenu,#panelClose').forEach(b=>b.addEventListener('click',()=>{active=false;}));
})();
