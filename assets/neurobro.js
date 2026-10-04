/* Session control only. Neurobro credentials are never entered into this dashboard. */
(() => {
 'use strict';
 const states=new Set(['DISCONNECTED','LOGIN_REQUIRED','LOGIN_IN_PROGRESS','CONNECTED','SESSION_EXPIRED','CLOUDFLARE_REQUIRED']);
 const labels={DISCONNECTED:'DISCONNECTED',LOGIN_REQUIRED:'LOGIN REQUIRED',LOGIN_IN_PROGRESS:'LOGIN IN PROGRESS',CONNECTED:'NEUROBRO CONNECTED',SESSION_EXPIRED:'LOGIN REQUIRED · SESSION EXPIRED',CLOUDFLARE_REQUIRED:'CLOUDFLARE REQUIRED'};
 const panel=document.getElementById('infoPanel'),body=document.getElementById('panelContent'),title=document.getElementById('panelTitle');
 let origin='',token='',timer=null,generation=0,active=false,polling=false,sending=false;
 let session={status:'DISCONNECTED',busy:false,takeover_url:null},note='Worker belum terhubung. Login server memerlukan hosting privat.';
 const menu=document.createElement('button');menu.className='menu-item';menu.id='neurobroMenu';menu.textContent='NEUROBRO · DISCONNECTED';
 document.getElementById('menuDrawer').insertBefore(menu,document.querySelector('.drawer-note'));
 const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const takeover=()=>origin+'/desktop/vnc.html#autoconnect=1&resize=remote&path=desktop/websockify';
 function validate(d){
  if(!d||!states.has(d.status)||d.mode!=='DRY_RUN'||d.live_enabled!==false||typeof d.busy!=='boolean')throw Error('Status worker tidak valid.');
  if(d.takeover_url!==null&&d.takeover_url!==takeover())throw Error('Alamat pengambilalihan tidak sesuai worker.');
  if(d.status==='CONNECTED'&&(!Number.isFinite(Date.parse(d.checked_at))||Math.abs(Date.now()-Date.parse(d.checked_at))>60000))throw Error('Status sesi kedaluwarsa; tekan CEK SESI.');
  return d;
 }
 function paint(){
  menu.textContent='NEUROBRO · '+labels[session.status];
  if(!active)return;
  document.getElementById('nbStatus').textContent=labels[session.status];
  document.getElementById('nbNote').textContent=note;
  document.getElementById('nbLogin').disabled=!origin||sending||session.busy;
  document.getElementById('nbCheck').disabled=!origin||sending||session.busy;
  const link=document.getElementById('nbTakeover');link.hidden=!session.takeover_url;link.style.display=session.takeover_url?'flex':'none';
  if(session.takeover_url)link.href=session.takeover_url;else link.removeAttribute('href');
 }
 function disconnect(){generation++;origin='';token='';clearInterval(timer);timer=null;session={status:'DISCONNECTED',busy:false,takeover_url:null};}
 async function request(path,method='GET'){
  const response=await fetch(origin+path,{method,headers:{Authorization:'Bearer '+token},credentials:'omit',redirect:'error',cache:'no-store',signal:AbortSignal.timeout(10000)});
  if(!response.ok)throw Error(response.status===403?'Token kontrol worker ditolak.':response.status===409?'Browser worker sedang digunakan.':`Worker HTTP ${response.status}`);
  return validate(await response.json());
 }
 async function poll(){
  if(!origin||polling)return;polling=true;const version=generation;
  try{const data=await request('/neurobro/status');if(version!==generation)return;session=data;note=data.error==='WORKER_BUSY'?'Screening sedang memakai profil. Tunggu selesai.':data.error?'Browser/config server perlu diperiksa.':data.stale?'Status lama. Tekan CEK SESI.':data.status==='CONNECTED'?'Sesi diverifikasi worker. Profil tetap tersimpan privat di server.':data.status==='DISCONNECTED'?'Sesi belum terverifikasi. Periksa konfigurasi selector worker.':'Selesaikan login/verifikasi pada browser server, lalu tekan CEK SESI.';paint();}
  catch(e){if(version===generation){session={status:'DISCONNECTED',busy:false,takeover_url:null};note=e.message;paint();}}
  finally{polling=false;}
 }
 async function action(name){
  if(sending||!origin)return;sending=true;const version=generation;paint();
  try{const data=await request('/neurobro/'+name,'POST');if(version!==generation)return;session=data;note='Permintaan diterima worker; menunggu pemeriksaan browser sebenarnya.';}
  catch(e){if(version===generation){session={status:'DISCONNECTED',busy:false,takeover_url:null};note=e.message;}}
  finally{sending=false;if(version===generation){paint();poll();}}
 }
 menu.onclick=()=>{
  active=true;title.dataset.workflow='false';title.textContent='NEUROBRO · SESI PRIVAT';panel.hidden=false;
  document.getElementById('menuDrawer').hidden=true;document.getElementById('menuToggle').setAttribute('aria-expanded','false');
  body.innerHTML=`<div class="wf-note"><b id="nbStatus"></b><p id="nbNote"></p><small>Binance tetap DRY RUN. Login di sini tidak menjalankan trading.</small></div>
   <div class="wf-actions"><button id="nbLogin">LOGIN NEUROBRO</button><button id="nbCheck">CEK SESI</button></div>
   <a id="nbTakeover" class="wf-file" target="_blank" rel="noopener noreferrer" hidden>BUKA BROWSER SERVER</a>
   <p style="font-size:12px;line-height:1.6">Login Neurobro dan verifikasi Cloudflare hanya pada halaman Neurobro di browser server. Setelah selesai, kembali ke kantor dan tekan CEK SESI. Jangan kirim prompt sendiri.</p>
   <details ${origin?'':'open'}><summary>Koneksi worker privat</summary><label>Origin HTTPS worker<input id="nbOrigin" type="url" placeholder="https://worker-anda" value="${escape(origin)}"></label><label>Token kontrol worker (bukan token Neurobro)<input id="nbToken" type="password" autocomplete="off" placeholder="Hanya di memori tab"></label><div class="wf-actions"><button id="nbConnect">HUBUNGKAN WORKER</button><button id="nbDisconnect">PUTUSKAN DASHBOARD</button></div><small>Jangan masukkan username, password, OTP, cookie, atau token Neurobro di dashboard. Putuskan dashboard tidak menghapus sesi di server.</small></details>`;
  document.getElementById('nbLogin').onclick=()=>action('login');document.getElementById('nbCheck').onclick=()=>action('check');
  document.getElementById('nbConnect').onclick=()=>{
   try{const u=new URL(document.getElementById('nbOrigin').value);const t=document.getElementById('nbToken').value;
    if(u.protocol!=='https:'||u.username||u.password||u.search||u.hash||u.pathname!=='/')throw Error('Gunakan origin HTTPS worker tanpa path/kredensial.');
    if(t.length<32)throw Error('Token kontrol worker minimal 32 karakter.');
    disconnect();origin=u.origin;token=t;document.getElementById('nbToken').value='';note='Memeriksa worker...';paint();poll();timer=setInterval(poll,3000);
   }catch(e){note=e.message;paint();}
  };
  document.getElementById('nbDisconnect').onclick=()=>{disconnect();note='Dashboard terputus. Sesi browser server tetap tersimpan.';paint();};paint();
 };
 document.querySelectorAll('[data-view],#workflowMenu').forEach(b=>b.addEventListener('click',()=>{active=false;}));
 document.getElementById('panelClose').addEventListener('click',()=>{active=false;});
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
})();
