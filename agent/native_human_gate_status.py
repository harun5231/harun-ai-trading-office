"""Observe verification, and acknowledge one known ordinary AUTO-only notice.

The ordinary native origin/iframe/login checks remain authoritative. This adds
the site's public session-expired overlay and Cloudflare iframe title which can
remain visible above an otherwise authenticated composer. Message descendants
are excluded so a past discussion about verification is not a current gate.
The verification observer never interacts with a security widget. A separate,
fail-closed helper below can acknowledge only the proved maintenance notice.
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


KNOWN_MAINTENANCE_BODY = (
    'Pemeliharaan terjadwal 5 Nov, 03:00–04:00 UTC. '
    'Gangguan singkat mungkin terjadi.')

# This is intentionally not a general modal closer. Recheck the exact public
# notice and frontmost native button synchronously, then dispatch ONE click.
# There is no challenge interaction, application-state access or private API.
KNOWN_MAINTENANCE_UI_JS = r'''(root, contract) => {
 if(contract.automation_allowed!==true)return false;
 const doc=root.ownerDocument,view=doc.defaultView;
 const msg=contract.message_selector;
 const norm=s=>String(s||'').replace(/\s+/g,' ').trim();
 const compact=s=>norm(s).replace(/\s/g,'').replace('03:00-04:00','03:00–04:00');
 const inMessage=e=>!!e.closest(msg);
 const visible=e=>{
  if(!e||!e.isConnected)return false;
  const r=e.getBoundingClientRect();
  if(r.width<=0||r.height<=0||r.right<=0||r.bottom<=0||r.left>=view.innerWidth||r.top>=view.innerHeight)return false;
  for(let p=e,depth=0;p&&depth<8;p=p.parentElement,depth++){
   const s=getComputedStyle(p);
   if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)===0)return false;
  }
  return true;
 };
 const modals=Array.from(root.querySelectorAll('[role="dialog"],[role="alertdialog"]')).filter(e=>!inMessage(e)&&visible(e));
 if(modals.length!==1)return false;
 const dialog=modals[0];
 if(dialog.querySelector(msg)||dialog.closest('[inert]'))return false;
 if(dialog.querySelector('iframe,input,select,textarea,a,code,pre,script,style,object,embed,form,[contenteditable],[href],[src],[formaction],[onclick]'))return false;
 const security=Array.from(root.querySelectorAll('iframe,input[type="password"],input[autocomplete="one-time-code"]')).filter(e=>!inMessage(e)&&visible(e));
 if(security.length)return false;
 const buttons=Array.from(dialog.querySelectorAll('button,[role="button"]')).filter(visible);
 if(buttons.length!==1)return false;
 const button=buttons[0];
 if(button.tagName!=='BUTTON'||norm(button.textContent)!=='Mengerti'||button.disabled||button.getAttribute('aria-disabled')==='true'
   ||button.closest('[inert]')||button.form||button.getAttribute('form')!==null||getComputedStyle(button).pointerEvents==='none')return false;
 const walker=doc.createTreeWalker(dialog,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT,{
  acceptNode:n=>n.nodeType===1?(n.matches(msg)?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_SKIP):NodeFilter.FILTER_ACCEPT
 });
 let node,text=[],count=0;
 while((node=walker.nextNode())){
  const p=node.parentElement;if(!p||inMessage(p)||!visible(p))continue;
  if(++count>32)return false;
  const value=norm(node.nodeValue);if(value)text.push(value);
  if(text.join(' ').length>500)return false;
 }
 const expected='Pemeliharaan terjadwal '+contract.expected_body+' Mengerti';
 if(compact(text.join(' '))!==compact(expected))return false;
 const r=button.getBoundingClientRect(),hit=doc.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
 if(!hit||(hit!==button&&!button.contains(hit)))return false;
 button.click();
 return true;
}'''


def acknowledge_known_maintenance_notice(page, *, message_selector, maintenance_allowed=None):
    """Attempt the one ordinary button only under a current native AUTO gate.

    The source-bound Actor observation supplies the callback. Legacy callers,
    MANUAL/session owners and uncertain mode observations cannot click. Return
    means only whether this call dispatched its single DOM click; the original
    native DOM detection MUST decide readiness afterwards, even on exceptions.
    """
    if not callable(maintenance_allowed):
        return False
    if (type(message_selector) is not str or not 0 < len(message_selector) <= 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in message_selector)):
        return False
    try:
        if maintenance_allowed() is not True:
            return False
        target = page.locator('html')
        if maintenance_allowed() is not True:
            return False
        value = target.evaluate(KNOWN_MAINTENANCE_UI_JS, {
            'message_selector': message_selector, 'automation_allowed': True,
            'expected_body': KNOWN_MAINTENANCE_BODY}, timeout=UI_OBSERVATION_TIMEOUT_MS)
        return value is True
    except Exception:
        # The original native status still observes the actual current page.
        # In particular, an uncertain click is never a CONNECTED assertion.
        return False
