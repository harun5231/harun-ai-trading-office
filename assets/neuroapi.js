/* Only worker control tokens enter this panel. Provider secrets stay on the server. */
(()=>{
 'use strict';
 const panel=document.getElementById('infoPanel'),body=document.getElementById('panelContent'),title=document.getElementById('panelTitle');
 const menu=document.createElement('button');menu.id='neuroapiMenu';menu.className='menu-item';menu.textContent='WORKER OFFLINE · NEUROAPI';
 document.getElementById('menuDrawer').insertBefore(menu,document.querySelector('.drawer-note'));
 let origin='',configured='',token='',timer=null,active=false,busy=false,online=false,generation=0;
 let status='NEUROAPI_UNCHECKED',note='Hubungkan worker privat. API key hanya dimasukkan di VPS.';
 const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const configUrl=new URL('worker-config.json',document.currentScript?.src||document.baseURI);
 const validOrigin=s=>{const u=new URL(s);if(u.protocol!=='https:'||u.username||u.password||u.search||u.hash||u.pathname!=='/')throw Error('Gunakan origin HTTPS worker.');return u.origin;};
 fetch(configUrl,{credentials:'omit',cache:'no-store'}).then(r=>r.ok?r.json():null).then(c=>{if(c?.worker_origin)configured=validOrigin(c.worker_origin);}).catch(()=>{});
 function paint(){
  menu.textContent='WORKER '+(online?'ONLINE':'OFFLINE')+' · '+status.replaceAll('_',' ');
  if(!active)return;
  document.getElementById('apiStatus').textContent=menu.textContent;document.getElementById('apiNote').textContent=note;
  for(const id of ['apiCheck','apiRun'])document.getElementById(id).disabled=!origin||busy;
 }
 async function request(path,method='GET'){
  const r=await fetch(origin+path,{method,headers:{Authorization:'Bearer '+token},credentials:'omit',redirect:'error',cache:'no-store',signal:AbortSignal.timeout(10000)});
  if(!r.ok)throw Error('Worker HTTP '+r.status);
  const data=await r.json();if(data.mode!=='DRY_RUN'||data.live_enabled!==false)throw Error('Mode worker tidak valid.');return data;
 }
 async function poll(){
  if(!origin)return;const g=generation;
  try{
   const [s,h,snapshot]=await Promise.all([request('/neuroapi/status'),request('/health'),request('/snapshot')]);if(g!==generation)return;
   online=h.status==='ONLINE';busy=s.busy===true;status=busy?snapshot.status:s.status;
   note=busy?'Worker memproses permintaan. Jangan ulangi siklus.':'Binance DRY RUN. Maksimal dua paper trade per hari; setup tidak diubah.';
   window.dispatchEvent(new CustomEvent('workerSnapshot',{detail:snapshot}));paint();
  }catch(e){if(g===generation){online=false;note=e.message;paint();}}
 }
 async function action(name){
  if(busy||!origin)return;busy=true;paint();
  try{await request('/neuroapi/'+name,'POST');note='Permintaan diterima worker.';}catch(e){note=e.message;busy=false;}
  paint();poll();
 }
 function disconnect(){generation++;origin='';token='';clearInterval(timer);online=false;busy=false;status='NEUROAPI_UNCHECKED';}
 menu.onclick=()=>{
  active=true;title.dataset.workflow='false';title.textContent='NEUROAPI · DRY RUN';panel.hidden=false;
  document.getElementById('menuDrawer').hidden=true;document.getElementById('menuToggle').setAttribute('aria-expanded','false');
  body.innerHTML=`<div class="wf-note"><b id="apiStatus"></b><p id="apiNote"></p><small>NeuroAPI Starter · smart · Binance Futures public data</small></div>
   <div class="wf-actions"><button id="apiCheck">CEK API</button><button id="apiRun">JALANKAN DRY RUN</button></div>
   <p>CEK API memeriksa koneksi tanpa mengirim prompt. DRY RUN menjalankan satu siklus riset berbayar NeuroAPI, maksimal sekali sehari. Tidak mengirim order Binance.</p>
   <details ${origin?'':'open'}><summary>Koneksi worker privat</summary><label>Origin HTTPS worker<input id="apiOrigin" type="url" value="${esc(origin||configured)}"></label><label>Token kontrol worker<input id="apiToken" type="password" autocomplete="off" placeholder="Bukan API key Neurobro"></label><div class="wf-actions"><button id="apiConnect">HUBUNGKAN WORKER</button><button id="apiDisconnect">PUTUSKAN</button></div><small>Token kontrol hanya di memori tab. Jangan masukkan API key Neurobro/Binance.</small></details>`;
  document.getElementById('apiCheck').onclick=()=>action('check');document.getElementById('apiRun').onclick=()=>action('run');
  document.getElementById('apiConnect').onclick=()=>{
   try{const u=validOrigin(document.getElementById('apiOrigin').value),t=document.getElementById('apiToken').value;if(t.length<32)throw Error('Token kontrol minimal 32 karakter.');disconnect();origin=u;token=t;document.getElementById('apiToken').value='';poll();timer=setInterval(poll,4000);}catch(e){note=e.message;paint();}
  };
  document.getElementById('apiDisconnect').onclick=()=>{disconnect();note='Dashboard terputus; worker tetap berjalan.';paint();};paint();
 };
 document.querySelectorAll('[data-view],#workflowMenu,#panelClose').forEach(b=>b.addEventListener('click',()=>{active=false;}));
})();
