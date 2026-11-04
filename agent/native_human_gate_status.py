"""Observe a visible verification gate, without interacting with its widget.

The ordinary native origin/iframe/login checks remain authoritative. This adds
the site's public session-expired overlay and Cloudflare iframe title which can
remain visible above an otherwise authenticated composer. Message descendants
are excluded so a past discussion about verification is not a current gate.
"""
from __future__ import annotations


UI_OBSERVATION_TIMEOUT_MS = 1500

# Read only light-DOM/public UI: no challenge body, shadow-root access, network,
# application state, clicks, typing, navigation or verification attempt.
HUMAN_GATE_UI_JS = r'''(root, contract) => {
 let stop=performance.now()+40;
 const doc=root.ownerDocument;
 const messageSelector=contract.message_selector;
 const norm=s=>String(s||'').replace(/\s+/g,' ').trim();
 const budget=()=>{if(performance.now()>stop)throw new Error('HUMAN_GATE_UI_OBSERVATION_UNVERIFIED');};
 const inMessage=e=>!!e.closest(messageSelector);
 const publicRows=(selector,limit)=>{
  const start=performance.now();
  const rows=Array.from(root.querySelectorAll(selector)).filter(e=>!inMessage(e));
  // Historical discussion is excluded before public-UI counts and budgets.
  stop+=performance.now()-start;
  if(rows.length>limit)throw new Error('HUMAN_GATE_UI_OBSERVATION_UNVERIFIED');
  return rows;
 };
 const visible=e=>{
  if(!e||!e.isConnected)return false;
  const r=e.getBoundingClientRect();
  const view=doc.defaultView;
  if(r.width<=0||r.height<=0||r.right<=0||r.bottom<=0||r.left>=view.innerWidth||r.top>=view.innerHeight)return false;
  for(let p=e,depth=0;p&&depth<8;p=p.parentElement,depth++){
   const s=getComputedStyle(p);
   if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)===0)return false;
  }
  return true;
 };
 const uiText=e=>{
  const walker=doc.createTreeWalker(e,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT,{
   acceptNode:n=>n.nodeType===1?(n.matches(messageSelector)?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_SKIP):NodeFilter.FILTER_ACCEPT
  });
  let n,seen=0,text=[];
  while((n=walker.nextNode())){
   const p=n.parentElement;
   if(!p||inMessage(p))continue;
   budget();if(++seen>2048)throw new Error('HUMAN_GATE_UI_OBSERVATION_UNVERIFIED');
   if(!visible(p))continue;
   const t=norm(n.nodeValue);if(t)text.push(t);
   if(text.join(' ').length>3000)throw new Error('HUMAN_GATE_UI_OBSERVATION_UNVERIFIED');
  }
  return text.join(' ');
 };
 const sessionGate=e=>{
  if(!visible(e)||inMessage(e))return false;
  const text=uiText(e);
  return /\bSession Expired\b/i.test(text)&&/\bPlease verify to continue\b/i.test(text);
 };
 const frames=publicRows('iframe[title]',64);
 for(const e of frames){
  budget();
  if(!inMessage(e)&&visible(e)&&/^Widget containing a Cloudflare security challenge$/i.test(norm(e.getAttribute('title'))))return true;
 }
 const overlays=publicRows('[role="dialog"],[role="alertdialog"],[aria-modal="true"],div.fixed.inset-0',64);
 for(const e of overlays){budget();if(sessionGate(e))return true;}
 const headings=publicRows('h1,h2,h3,h4,[role="heading"]',128);
 for(const e of headings){
  budget();if(inMessage(e)||!visible(e)||!/^Session Expired$/i.test(norm(e.textContent)))continue;
  for(let p=e.parentElement,depth=0;p&&p!==root&&depth<5;p=p.parentElement,depth++){
   budget();if(sessionGate(p))return true;
  }
 }
 // The actual gate is centered. This observes only normal hit-test ancestors,
 // including an unlabelled overlay; it does not enter an embedded challenge.
 const view=doc.defaultView;
 let center=doc.elementFromPoint(view.innerWidth/2,view.innerHeight/2);
 for(let e=center,depth=0;e&&e!==root&&depth<6;e=e.parentElement,depth++){
  budget();if(inMessage(e))break;
  const r=e.getBoundingClientRect();
  // A center hit is useful for the actual small verification card, not a
  // reason to scan the entire ordinary page/sidebar on every status poll.
  if(r.width>view.innerWidth*.8||r.height>view.innerHeight*.8)continue;
  if(sessionGate(e))return true;
 }
 return false;
}'''


def visible_human_gate(page, *, message_selector):
    """Return only a boolean from the current UI on the native page thread.

    ``message_selector`` is supplied by the source-bound native patch, from its
    actual message contract, never from a request or Office setting. Observation
    errors propagate to the native UNAVAILABLE path; they do not invent login,
    verification, or a CONNECTED observation.
    """
    if (type(message_selector) is not str or not 0 < len(message_selector) <= 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in message_selector)):
        raise ValueError('HUMAN_GATE_MESSAGE_CONTRACT_UNVERIFIED')
    value = page.locator('html').evaluate(HUMAN_GATE_UI_JS,
        {'message_selector': message_selector}, timeout=UI_OBSERVATION_TIMEOUT_MS)
    if type(value) is not bool:
        raise ValueError('HUMAN_GATE_UI_OBSERVATION_UNVERIFIED')
    return value
