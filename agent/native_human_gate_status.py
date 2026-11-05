"""Observe verification, and acknowledge one known ordinary AUTO-only notice.

The ordinary native origin/iframe/login checks remain authoritative. This adds
the site's public session-expired overlay and Cloudflare iframe title which can
remain visible above an otherwise authenticated composer. Message descendants
are excluded so a past discussion about verification is not a current gate.
The verification observer never interacts with a security widget. A separate,
fail-closed helper below can acknowledge only the proved maintenance notice.
"""
from __future__ import annotations

import hashlib
import time
from urllib.parse import urlsplit


UI_OBSERVATION_TIMEOUT_MS = 1500
UI_OBSERVATION_ATTEMPTS = 3


class HumanGateObservationUnavailable(ValueError):
    def __init__(self, reason, observation):
        super().__init__(reason)
        self.observation = observation


def _read_error(error):
    """Classify only known transient read failures, never security outcomes."""
    text = str(error)
    browser_error = type(error).__module__.startswith('playwright.')
    if browser_error and 'HUMAN_GATE_UI_OBSERVATION_UNVERIFIED' in text:
        code, retry = 'HUMAN_GATE_UI_OBSERVATION_UNVERIFIED', True
    elif browser_error and any(marker in text for marker in ('Execution context was destroyed',
            'Cannot find context with specified id', 'Element is not attached to the DOM',
            'Frame was detached', 'JSHandle is disposed')):
        code, retry = 'HUMAN_GATE_UI_CONTEXT_UNAVAILABLE', True
    else:
        code, retry = 'HUMAN_GATE_UI_EVALUATION_FAILED', False
    return dict(reason=code, retryable=retry, exception_class=type(error).__name__,
        message_sha256=hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest(),
        message_characters=len(text))


def _page_url(page, expected=None):
    try:
        current = page.url
        if page.is_closed() is not False:
            raise ValueError
        parsed = urlsplit(current)
        if parsed.scheme != 'https' or parsed.netloc != 'app.neurobro.ai':
            raise ValueError
    except Exception:
        raise ValueError('HUMAN_GATE_UI_PAGE_UNAVAILABLE') from None
    if expected is not None and current != expected:
        raise ValueError('HUMAN_GATE_UI_PAGE_CHANGED')
    return current

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
    Known transient read errors may resample the same approved open page within
    one 1500ms total budget. Only a fresh successful boolean is returned. Failed
    observations still reach native UNAVAILABLE, never a guessed login or
    CONNECTED state. No page action, model call or WEB Send is retried.
    """
    if (type(message_selector) is not str or not 0 < len(message_selector) <= 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in message_selector)):
        raise ValueError('HUMAN_GATE_MESSAGE_CONTRACT_UNVERIFIED')
    started = time.monotonic()
    deadline = started + UI_OBSERVATION_TIMEOUT_MS / 1000
    errors = []
    attempts = 0

    def unavailable(reason, cause=None):
        observation = dict(reason=reason, attempts=attempts,
            elapsed_ms=max(0, int((time.monotonic() - started) * 1000)), errors=list(errors))
        raise HumanGateObservationUnavailable(reason, observation) from cause

    try:
        initial_url = _page_url(page)
    except ValueError as error:
        unavailable(str(error), error)
    while attempts < UI_OBSERVATION_ATTEMPTS:
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            unavailable('HUMAN_GATE_UI_OBSERVATION_DEADLINE')
        try:
            _page_url(page, initial_url)
        except ValueError as error:
            unavailable(str(error), error)
        attempts += 1
        try:
            value = page.locator('html').evaluate(HUMAN_GATE_UI_JS,
                {'message_selector': message_selector}, timeout=remaining)
        except Exception as error:
            detail = _read_error(error)
            errors.append(detail)
            try:
                _page_url(page, initial_url)
            except ValueError as changed:
                unavailable(str(changed), changed)
            if not detail['retryable'] or attempts >= UI_OBSERVATION_ATTEMPTS:
                unavailable(detail['reason'], error)
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                unavailable('HUMAN_GATE_UI_OBSERVATION_DEADLINE', error)
            time.sleep(min(.025, remaining_seconds))
            continue
        try:
            _page_url(page, initial_url)
        except ValueError as error:
            unavailable(str(error), error)
        if time.monotonic() > deadline:
            unavailable('HUMAN_GATE_UI_OBSERVATION_DEADLINE')
        if type(value) is not bool:
            unavailable('HUMAN_GATE_UI_OBSERVATION_UNVERIFIED')
        return value
    unavailable('HUMAN_GATE_UI_OBSERVATION_UNVERIFIED')


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
