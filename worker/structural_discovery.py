"""Phase 3: read-only structural proof; no page interaction or message reads."""
import re
from urllib.parse import urlsplit
from . import semantic_inventory as inventory
from .selector_discovery import REQUIRED, OPTIONAL

VERSION = 'structural-readonly-v3'
CAPTCHA = 'iframe[src*="challenges.cloudflare.com/"],iframe[src*="www.google.com/recaptcha/"],iframe[src*="hcaptcha.com/"]'
# Extend the audited metadata probe, without adding any content/property reads.
PROBE = inventory.PROBE
PROBE = PROBE.replace("const form=e.closest('form'),shell=", "const container=e.closest('form,[data-testid=\"chat-composer\"],[data-role=\"composer\"],[role=\"group\"]');const form=e.closest('form'),shell=")
PROBE = PROBE.replace("in_form:!!form,", "container:ids.get(container)||null, parent:ids.get(e.parentElement)||null, sibling_controls:Array.from(e.parentElement?.children||[]).filter(x=>x.matches('button,input,textarea,[role=\"button\"]')).length,in_form:!!form,")

# Resolve duplicate control selectors only through an observed unique composer
# in the same FORM, and validate the resulting CSS against the actual DOM.
PROBE = PROBE.replace("const chosen=choices.find", r'''if(form && !choices.some(c=>c.count===1)) {
  const inputs=Array.from(form.querySelectorAll('textarea,[contenteditable="true"]')).filter(x=>x.closest('form')===form);
  for(const input of inputs) for(const a of ['data-testid','aria-label','placeholder']) {
    const v=safe(input.getAttribute(a));if(!v)continue;
    const inputSelector=input.tagName.toLowerCase()+'['+a+'='+JSON.stringify(v)+']';
    if(document.querySelectorAll(inputSelector).length!==1)continue;
    for(const choice of choices.slice()) {
      const selector='form:has('+inputSelector+') '+choice.selector;
      const matches=Array.from(document.querySelectorAll(selector));
      if(matches.length===1&&matches[0]===e)choices.unshift({selector,count:1});
    }
  }
 }
 const chosen=choices.find''')


def classify(data):
    result = inventory.classify(data)
    rows = data['inventory']; by_id = {r['id']: r for r in rows}
    evidence = result['evidence']; selectors = result['selectors']
    def words(r):
        return re.sub(r'([a-z])([A-Z])', r'\1 \2', ' '.join(r['metadata'].values())).lower().replace('-', ' ').replace('_', ' ').split()
    def has(r, *terms): return bool(set(words(r)) & set(terms))
    def scope(r): return r.get('form') or r.get('container')
    def related(a, b): return bool(scope(a) and scope(a) == scope(b))
    def send_control(r):
        return r['visible'] and (r['tag'] == 'BUTTON' or r['type'] == 'submit' or r['metadata'].get('role') == 'button') and (has(r, 'send', 'kirim') or r['type'] == 'submit')
    def choose(key, candidates, qualified):
        selectors.pop(key, None)
        evidence[key] = {'status': 'UNVERIFIED', 'evidence': [], 'candidates': [
            {'element': r['id'], **r['candidate'], 'qualified': r in qualified} for r in candidates if r['candidate']]}
        e = evidence[key]
        if len(qualified) > 1: e['status'] = 'AMBIGUOUS'
        elif len(qualified) == 1:
            r = qualified[0]
            if not r['candidate'] or r['candidate']['count'] != 1: e['status'] = 'AMBIGUOUS'
            else:
                e['status'] = 'VERIFIED'; e['evidence'] = ['UNIQUE_STRUCTURAL_PROOF']
                selectors[key] = r['candidate']['selector']; return r
        elif candidates: e['status'] = 'AMBIGUOUS' if len(candidates) > 1 else 'CANDIDATE'
        return None
    candidates = [r for r in rows if r['editable'] and r['visible'] and not r['disabled'] and
                  (r['tag'] == 'TEXTAREA' or r['contenteditable'] or (r['tag'] == 'INPUT' and r['type'] == 'text'))]
    qualified = []
    for r in candidates:
        chat = has(r, 'chat', 'message', 'messages', 'composer', 'compose', 'prompt', 'pesan', 'tanya') or (has(r, 'ask') and has(r, 'neurobro'))
        controls = [b for b in rows if related(r, b) and send_control(b)]
        # All invariants are required, not a highest-score/DOM-order heuristic.
        r['structure'] = {'chat_semantics': chat, 'send_controls': len(controls),
                          'file_controls': sum(related(r, b) and b['type'] == 'file' for b in rows),
                          'semantic_scope': bool(scope(r)), 'application_shell': bool(r['shell'])}
        if chat and scope(r) and r['shell'] and len(controls) >= 1 and not has(r, 'search', 'email', 'password'):
            qualified.append(r)
    composer = choose('composer', candidates, qualified)
    sends = [r for r in rows if composer and related(composer, r) and send_control(r)]
    send = choose('send', sends, sends)
    uploads = [r for r in rows if r['type'] == 'file' and r['tag'] == 'INPUT']
    related_uploads = [r for r in uploads if composer and related(composer, r) and not r['disabled']]
    choose('upload', related_uploads if composer else uploads, related_uploads)
    # Known provider definition remains valid with zero currently matched frames.
    evidence['captcha'] = {'status': 'VERIFIED', 'evidence': ['KNOWN_PROVIDER_DEFINITION'], 'candidates': [],
                           'definition_verified': True, 'match_count': sum(r['challenge'] for r in rows),
                           'visible': any(r['challenge'] and r['visible'] for r in rows)}
    selectors['captcha'] = CAPTCHA
    for key in ('login_required', 'loading'):
        e = evidence[key]; e['definition_verified'] = e['status'] == 'VERIFIED'
        ids = {c['element'] for c in e['candidates']}
        e['match_count'] = len(ids); e['visible'] = any(r['id'] in ids and r['visible'] for r in rows)
    blocked = any(evidence[k].get('visible') for k in ('captcha', 'login_required', 'loading'))
    shell = by_id.get(composer['shell']) if composer and send and composer['shell'] == send['shell'] else None
    # A generic anonymous chat shell is insufficient. Require explicit auth marker
    # OR a semantic application shell + independently scoped conversation log +
    # new-chat control within that same shell. Never inspect message/account text.
    positive = bool(shell and (shell['metadata'].get('data-authenticated') == 'true' or
        shell['metadata'].get('data-session-state') == 'authenticated' or
        (shell['metadata'].get('role') == 'application' and has(shell, 'chat', 'neurobro', 'conversation') and
         any(r['metadata'].get('role') == 'log' and r['shell'] == shell['id'] for r in rows) and
         any(r['tag'] == 'BUTTON' and r['visible'] and r['shell'] == shell['id'] and has(r, 'new', 'baru') and has(r, 'chat', 'percakapan') for r in rows))))
    auth = choose('authenticated', [shell] if shell else [], [shell] if positive and not blocked else [])
    if auth: evidence['authenticated']['evidence'] = ['COMPOSER_SEND_APPLICATION_COMPOSITE']
    result.update(version=VERSION, status={k: evidence[k]['status'] for k in REQUIRED + OPTIONAL})
    result['state'] = ('CLOUDFLARE_REQUIRED' if evidence['captcha']['visible'] else
        'LOGIN_REQUIRED' if evidence['login_required']['visible'] else
        'LOADING' if evidence['loading']['visible'] else 'AUTHENTICATED' if auth else 'UNVERIFIED')
    if data['truncated']:
        invalidate(result, 'INVENTORY_TRUNCATED')
    readiness(result)
    return result


def invalidate(result, reason):
    for key in result['status']:
        if result['status'][key] == 'VERIFIED':
            result['status'][key] = result['evidence'][key]['status'] = 'CANDIDATE'
            result['evidence'][key]['evidence'] = [reason]
    result['selectors'] = {}
    if result['state'] == 'AUTHENTICATED': result['state'] = 'UNVERIFIED'


def readiness(result):
    result['session_check_ready'] = result['state'] == 'AUTHENTICATED' and all(result['status'][k] == 'VERIFIED' for k in REQUIRED)
    result['screening_ready'] = result['session_check_ready'] and all(result['status'][k] == 'VERIFIED' for k in OPTIONAL)


def discover_phase3(page):
    u = urlsplit(page.url)
    if u.scheme != 'https' or u.netloc != 'app.neurobro.ai':
        result = classify({'inventory': [], 'truncated': False})
        return result
    first = page.evaluate(PROBE, inventory.WORDS)
    second = page.evaluate(PROBE, inventory.WORDS)
    stable = first == second
    result = classify(second)
    if not stable: invalidate(result, 'DOM_NOT_STABLE')
    readiness(result)
    return result
