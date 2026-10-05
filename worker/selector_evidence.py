"""Private, bounded evidence persistence and a read-only sanitized viewer."""
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from .semantic_inventory import WORDS
from .structural_discovery import CAPTCHA, VERSION

KEYS = set('version state selectors status evidence inventory truncated session_check_ready screening_ready candidates element selector count qualified id tag type metadata redacted_attributes contenteditable disabled visible editable in_form form shell fingerprint candidate challenge container parent sibling_controls structure chat_semantics send_controls file_controls semantic_scope application_shell definition_verified match_count'.split())
KEYS.update('composer send authenticated login_required captcha loading assistant_messages completed_response upload attachment_ready new_chat response_text user_messages streaming'.split())
ATTRS = set('aria-label placeholder name data-testid role data-test data-cy data-role data-state data-component data-slot data-ui data-element data-part data-hook data-message-author-role data-authenticated data-session-state'.split())
KEYS.update(ATTRS)
CODES = set('UNVERIFIED VERIFIED CANDIDATE AMBIGUOUS AUTHENTICATED LOGIN_REQUIRED CLOUDFLARE_REQUIRED LOADING UNIQUE_STRUCTURAL_PROOF KNOWN_PROVIDER_DEFINITION COMPOSER_SEND_APPLICATION_COMPOSITE INVENTORY_TRUNCATED DOM_NOT_STABLE INSUFFICIENT_STRUCTURAL_EVIDENCE VISIBLE_EDITABLE_UNIQUE CHAT_SEMANTIC_ATTRIBUTE SAME_FORM_AS_COMPOSER SEND_CONTROL AUTHENTICATED_APPLICATION_SHELL COMPOSER_AND_SEND LOGIN_CONTROL_SEMANTICS CHAT_LOADING_SEMANTICS NEW_CHAT_CONTROL FILE_INPUT_SAME_FORM KNOWN_CHALLENGE_IFRAME'.split())
TAGS = set('TEXTAREA INPUT BUTTON FORM IFRAME DIV SPAN MAIN NAV SECTION ARTICLE ASIDE HEADER FOOTER LABEL UL LI A P SVG OTHER'.split())
SAFE_WORDS = set(WORDS) | {'submit','hidden','checkbox','radio','number','tel','url','file','search','iframe','div','span','section','article','aside','label','ul','li','p','svg','other'}

def safe_label(value):
    if len(value) > 80 or not re.fullmatch(r'[A-Za-z _\-.!?]+', value): return False
    tokens = re.sub(r'([a-z])([A-Z])', r'\1 \2', value).lower()
    return all(w in SAFE_WORDS for w in re.findall('[a-z]+', tokens))

def safe_string(value):
    scoped=re.fullmatch(r'form:has\(([^()]+)\) ([^()]+)',value)
    if scoped: return safe_string(scoped[1]) and safe_string(scoped[2])
    if value in CODES or value in TAGS or value in (VERSION, CAPTCHA): return True
    if re.fullmatch(r'e[0-9]{1,3}', value): return True
    if ' > ' in value: return all(s in TAGS for s in value.split(' > '))
    # Candidate selectors consist solely of a known tag and observed safe attrs.
    m = re.fullmatch(r'([a-z]+)((?:\[[a-z-]+="[A-Za-z _\-.!?]+"\])*)', value)
    if m and m[1].upper() in TAGS:
        return all(a in ATTRS | {'type'} and safe_label(v) for a,v in re.findall(r'\[([a-z-]+)="([^"]+)"\]',m[2]))
    return safe_label(value)

def sanitize(data, depth=0):
    """Closed schema rejects unknown/sensitive keys; no permissive raw-data path."""
    if depth > 12: raise ValueError('EVIDENCE_DEPTH')
    if data is None or isinstance(data, bool): return data
    if isinstance(data, int) and 0 <= data <= 100000: return data
    if isinstance(data, str) and safe_string(data): return data
    if isinstance(data, list) and len(data) <= 200: return [sanitize(v,depth+1) for v in data]
    if isinstance(data, dict) and all(k in KEYS for k in data):
        return {k:sanitize(v,depth+1) for k,v in data.items()}
    raise ValueError('UNSAFE_EVIDENCE')


def save(config_path, result):
    data = sanitize(result)
    return write_private(config_path, 'selector-discovery.json', data)


def write_private(config_path, filename, data):
    # Callers must validate their closed schema before this shared atomic writer.
    if filename not in ('selector-discovery.json','selector-discovery-diagnostic.json'): raise ValueError('PRIVATE_PATH_REQUIRED')
    config = Path(config_path); path = config.with_name(filename)
    repo = Path(__file__).resolve().parent.parent
    if config.is_symlink() or path.is_symlink() or repo in path.resolve().parents: raise ValueError('PRIVATE_PATH_REQUIRED')
    owner = config.stat()
    if not stat.S_ISREG(owner.st_mode) or owner.st_mode & 0o077: raise ValueError('PRIVATE_PERMISSIONS_REQUIRED')
    old = path.lstat() if path.exists() else None
    if old and (not stat.S_ISREG(old.st_mode) or old.st_uid != owner.st_uid): raise ValueError('PRIVATE_OWNER_REQUIRED')
    temp = None
    try:
        with tempfile.NamedTemporaryFile('w',dir=path.parent,prefix='.selector-',delete=False) as f:
            temp=f.name; os.fchmod(f.fileno(),0o600)
            own=os.fstat(f.fileno())
            if (own.st_uid,own.st_gid)!=(owner.st_uid,owner.st_gid): os.fchown(f.fileno(),owner.st_uid,owner.st_gid)
            json.dump(data,f,indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
        now=path.lstat() if path.exists() or path.is_symlink() else None
        if (now is None)!=(old is None) or (now and (now.st_ino,now.st_mtime_ns)!=(old.st_ino,old.st_mtime_ns)): raise ValueError('EVIDENCE_CHANGED')
        os.replace(temp,path); temp=None
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if temp: Path(temp).unlink(missing_ok=True)
    return str(path)


def main():
    import argparse
    parser=argparse.ArgumentParser(description='Read saved sanitized evidence; no browser access')
    parser.add_argument('--path',default='/private/selector-discovery.json')
    args=parser.parse_args()
    try:
        fd=os.open(args.path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            s=os.fstat(f.fileno())
            if not stat.S_ISREG(s.st_mode) or s.st_mode & 0o077 or s.st_size>1024*1024: raise ValueError('UNSAFE_FILE')
            result=sanitize(json.load(f))
        print(json.dumps(result,indent=2,sort_keys=True)); return 0
    except Exception:
        print('{"state":"ERROR","reason":"PRIVATE_EVIDENCE_UNAVAILABLE_OR_UNSAFE"}'); return 1

if __name__=='__main__': raise SystemExit(main())
