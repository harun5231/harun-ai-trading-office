"""Read-only semantic discovery. Candidate vocabulary is NOT verified Neurobro config.
Only fixed selector strings and boolean evidence leave the page; never DOM text.
"""
import json
import os
import stat
import tempfile
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit

VERSION='semantic-readonly-v1'
REQUIRED=('composer','send','authenticated','login_required','captcha','loading')
OPTIONAL=('assistant_messages','completed_response','upload','attachment_ready','new_chat','response_text','user_messages','streaming')

PROBE=r'''() => {
 const found={};
 const all=s=>Array.from(document.querySelectorAll(s));
 const visible=e=>!!(e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');
 const editable=e=>visible(e)&&!e.disabled&&!e.readOnly&&(e.tagName==='TEXTAREA'||e.isContentEditable);
 const control=e=>['BUTTON','INPUT','A'].includes(e.tagName)||e.getAttribute('role')==='button';
 function pick(key,candidates,valid) {
   const accepted=candidates.filter(s=>{const es=all(s);return es.length&&valid(es);});
   // Different valid selectors must refer to the same element set, else ambiguous.
   if(accepted.length&&accepted.every(s=>all(s).length===all(accepted[0]).length&&all(s).every(e=>all(accepted[0]).includes(e)))) found[key]=accepted[0];
 }
 pick('composer',['textarea[aria-label="Message"]','textarea[placeholder="Type a message..."]','textarea[data-testid="chat-input"]','[role="textbox"][contenteditable="true"][aria-label="Message"]'],es=>es.length===1&&editable(es[0]));
 const composer=found.composer?all(found.composer)[0]:null;
 const form=composer?.closest('form');
 if(form) {
   pick('send',['form button[aria-label="Send message"]','form button[aria-label="Send"]','form button[data-testid="send-message"]','form button[type="submit"]'],es=>es.length===1&&visible(es[0])&&control(es[0])&&es[0].closest('form')===form);
 }
 // Chat input alone may be a guest UI: require an explicit sign-out control.
 if(found.composer&&found.send) pick('authenticated',['button[aria-label="Log out"]','button[aria-label="Sign out"]','button[data-testid="logout"]'],es=>es.length===1&&visible(es[0])&&control(es[0]));
 pick('login_required',['form[aria-label="Sign in"]','form[aria-label="Log in"]','button[aria-label="Sign in"]','a[aria-label="Sign in"]'],es=>es.length===1&&(control(es[0])||(es[0].tagName==='FORM'&&es[0].querySelector('input[type="password"]'))));
 pick('captcha',['iframe[src*="challenges.cloudflare.com/"]','iframe[src*="www.google.com/recaptcha/"]','iframe[src*="hcaptcha.com/"]'],es=>es.every(e=>e.tagName==='IFRAME'));
 pick('loading',['[role="progressbar"][aria-label="Loading chat"]','[role="status"][data-testid="chat-loading"]'],es=>es.length===1);
 pick('new_chat',['button[aria-label="New chat"]','button[data-testid="new-chat"]'],es=>es.length===1&&visible(es[0])&&control(es[0]));
 pick('upload',['form input[type="file"]'],es=>es.length===1&&form&&es[0].closest('form')===form&&!es[0].disabled);
 // Explicit author metadata within a conversation/log prevents matching arbitrary UI.
 for(const author of ['assistant','user']) pick(author+'_messages',[
   '[role="log"] [data-message-author-role="'+author+'"]',
   '[data-testid="conversation"] [data-message-author-role="'+author+'"]'
 ],es=>es.every(e=>!e.querySelector('textarea,input,[contenteditable="true"]')));
 if(found.assistant_messages) {
   const messages=all(found.assistant_messages);
   for(const [key,selector] of [['response_text','[data-testid="message-content"]'],['completed_response','[data-status="complete"]']]) {
     if(messages.every(e=>e.querySelectorAll(selector).length===1)) found[key]=selector; // relative to each assistant message
   }
   pick('streaming',[found.assistant_messages+' [data-status="streaming"]'],es=>es.length===1);
 }
 if(form) pick('attachment_ready',['form [data-testid="attachment"][data-status="ready"]'],es=>es.every(e=>e.closest('form')===form));
 const challenge=found.captcha&&all(found.captcha).some(visible);
 const login=found.login_required&&all(found.login_required).some(visible);
 const loading=found.loading&&all(found.loading).some(visible);
 return {found,state:challenge?'CLOUDFLARE_REQUIRED':login?'LOGIN_REQUIRED':loading?'LOADING':found.authenticated?'AUTHENTICATED':'UNVERIFIED'};
}'''


def discover(page):
    url=urlsplit(page.url)
    if url.scheme!='https' or url.hostname!='app.neurobro.ai':
        return {'version':VERSION,'state':'UNVERIFIED','selectors':{},'status':{k:'UNVERIFIED' for k in REQUIRED+OPTIONAL}}
    data=page.evaluate(PROBE)
    found=data['found']
    # Re-observe to reject transient/unstable matches without any page action.
    again=page.evaluate(PROBE)
    stable={k:v for k,v in found.items() if again['found'].get(k)==v}
    return {'version':VERSION,'state':again['state'],'selectors':stable,
            'status':{k:'VERIFIED' if k in stable else 'UNVERIFIED' for k in REQUIRED+OPTIONAL}}


def atomic_private(path,data):
    path=Path(path)
    repo=Path(__file__).resolve().parent.parent
    if path.is_symlink() or repo==path.resolve() or repo in path.resolve().parents:raise ValueError('PRIVATE_PATH_REQUIRED')
    old=path.stat();mode=stat.S_IMODE(old.st_mode)
    if not stat.S_ISREG(old.st_mode) or mode & 0o077:raise ValueError('PRIVATE_PERMISSIONS_REQUIRED')
    temp=None
    try:
        with tempfile.NamedTemporaryFile('w',dir=path.parent,prefix='.selector-',delete=False) as f:
            temp=f.name;os.fchmod(f.fileno(),mode)
            if os.fstat(f.fileno()).st_uid!=old.st_uid or os.fstat(f.fileno()).st_gid!=old.st_gid:os.fchown(f.fileno(),old.st_uid,old.st_gid)
            json.dump(data,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
        now=path.stat()
        if (now.st_ino,now.st_mtime_ns)!=(old.st_ino,old.st_mtime_ns):raise ValueError('CONFIG_CHANGED')
        os.replace(temp,path);temp=None
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if temp:Path(temp).unlink(missing_ok=True)


def commit_verified(path,result,original):
    if result['state']!='AUTHENTICATED' or any(result['status'][k]!='VERIFIED' for k in REQUIRED):return False
    current=json.loads(Path(path).read_text())
    if current!=original:raise ValueError('CONFIG_CHANGED')
    updated=json.loads(json.dumps(original));updated.setdefault('neurobro',{})
    # Clear unverified workflow selectors so the existing screening validator blocks.
    for key in REQUIRED+OPTIONAL:updated['neurobro'][key]=result['selectors'].get(key,'')
    updated['selectors_verified_on']=datetime.now(timezone.utc).isoformat()+'/'+VERSION
    updated['selector_verification']={'version':VERSION,'status':result['status']}
    atomic_private(path,updated);return True


def main():
    import argparse,fcntl
    from .session_service import SessionBrowser
    from .screening import private_profile
    os.umask(0o077)
    parser=argparse.ArgumentParser(description='Read-only DOM discovery; no messages, uploads or trading')
    parser.add_argument('--config',required=True);parser.add_argument('--data-dir',required=True)
    args=parser.parse_args();browser=None;service=None
    try:
        config_path=Path(args.config).resolve();original=json.loads(config_path.read_text())
        directory=private_profile(Path(args.data_dir)/'browser')
        service=(directory/'session-service.lock').open('a');fcntl.flock(service,fcntl.LOCK_EX|fcntl.LOCK_NB)
        browser=SessionBrowser(directory,original)
        browser.prepare_check();browser.open()
        # Bounded readiness observation only; never refresh, click or type.
        import time
        deadline=time.monotonic()+20
        while True:
            result=discover(browser.page)
            if result['state'] in ('AUTHENTICATED','LOGIN_REQUIRED','CLOUDFLARE_REQUIRED') or time.monotonic()>=deadline:break
            time.sleep(.5)
        browser.close();browser=None
        written=commit_verified(config_path,result,original)
        print(json.dumps({'state':result['state'],'selectors':result['status'],'config_updated':written,
                          'screening_ready':written and all(result['status'][k]=='VERIFIED' for k in OPTIONAL),
                          'mode':'DRY_RUN'},sort_keys=True))
        return 0 if written else 2
    except Exception:
        print('{"state":"ERROR","reason":"DISCOVERY_OR_OWNERSHIP_CHECK_FAILED","config_updated":false}')
        return 1
    finally:
        if browser:
            try:browser.close()
            except Exception:pass
        if service:service.close()

if __name__=='__main__':raise SystemExit(main())
