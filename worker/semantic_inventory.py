"""Phase 2: bounded, sanitized semantic metadata. No raw DOM or user content."""
import re
from urllib.parse import urlsplit
from .selector_discovery import REQUIRED,OPTIONAL
VERSION='semantic-inventory-v2'

# Closed vocabulary: arbitrary names, identifiers and phrases are redacted even
# when they don't resemble a token. Unknown UI labels require a privacy review.
WORDS='''a an the and or to of for in on at with your you here new start ask anything
neurobro ai assistant user message messages chat conversation compose composer input
send submit stop cancel upload attach attachment attachments file files image images
ready complete completed loading streaming idle error success busy pending true false
sign log login logout out authenticated unauthenticated guest session application app
shell main content response text button textbox form search type enter write prompt
me something about please select choose optional required email password account profile menu navigation
nav dialog status progressbar region log toolbar settings help close open expand
collapse clear delete remove copy edit retry submit footer header wrapper container
list item root panel screen field area icon action actions test testid test-id test-id
chatbot textarea rich editor tiptap editable multiline auto off none polite assertive
kirim pesan masuk keluar unggah lampiran baru ketik tulis tanya percakapan memuat
selesai siap batal semua pertanyaan jawaban silakan disini di ini untuk saya anda'''.split()

PROBE=r'''(words) => {
 const vocabulary=new Set(words),limit=200,attrs=['aria-label','placeholder','name','data-testid','role','data-test','data-cy','data-role','data-state','data-component','data-slot','data-ui','data-element','data-part','data-hook','data-message-author-role','data-authenticated','data-session-state'];
 const safe=s=>{
   if(typeof s!=='string'||!s||s.length>80||/[\r\n\t]/.test(s))return null;
   if(!/^[A-Za-z _\-.!?]+$/.test(s)||/bearer|secret|token|credential|cookie/i.test(s))return null;
   const tokens=s.replace(/([a-z])([A-Z])/g,'$1 $2').toLowerCase().split(/[^a-z]+/).filter(Boolean);
   return tokens.length&&tokens.every(w=>vocabulary.has(w))?s:null;
 };
 const visible=e=>!!(e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');
 const allowedTags=new Set('TEXTAREA INPUT BUTTON FORM IFRAME DIV SPAN MAIN NAV SECTION ARTICLE ASIDE HEADER FOOTER LABEL UL LI A P SVG'.split(' '));
 const tag=e=>allowedTags.has(e.tagName)?e.tagName:'OTHER';
 const all=Array.from(document.querySelectorAll('*'));
 const relevant=all.filter(e=>e.matches('textarea,input,button,[role],[contenteditable],form,iframe,[data-testid]')||Array.from(e.attributes).some(a=>a.name.startsWith('aria-')||a.name.startsWith('data-')));
 const elements=relevant.slice(0,limit),ids=new Map(elements.map((e,i)=>[e,'e'+i]));
 const rows=elements.map(e=>{
   const metadata={};let redacted=0;
   for(const attr of attrs){if(!e.hasAttribute(attr))continue;const raw=e.getAttribute(attr);const privateContext=['aria-label','placeholder','name'].includes(attr)&&(e.closest('[data-message-author-role],[role="log"]')||/account|profile|user/i.test(raw));const v=privateContext?null:safe(raw);if(v!==null)metadata[attr]=v;else redacted++;}
   const types=new Set(['text','search','email','password','submit','button','file','hidden','checkbox','radio','number','tel','url']);
   const rawType=e.getAttribute('type');
   const type=types.has(rawType)?rawType:(e.tagName==='INPUT'&&!rawType?'text':null);
   const chain=[];for(let p=e.parentElement;p&&chain.length<3;p=p.parentElement)chain.unshift(tag(p));chain.push(tag(e));
   const form=e.closest('form'),shell=e.closest('[role="application"],main');
   const editable=visible(e)&&!e.disabled&&!e.readOnly&&(e.tagName==='TEXTAREA'||e.isContentEditable||(e.tagName==='INPUT'&&['text','search'].includes(type)));
   const choices=[];
   for(const [a,v] of Object.entries(tag(e)==='OTHER'?{}:metadata)){const s=e.tagName.toLowerCase()+'['+a+'='+JSON.stringify(v)+']';choices.push({selector:s,count:document.querySelectorAll(s).length});}
   if(tag(e)!=='OTHER'&&rawType&&type){const s=e.tagName.toLowerCase()+'[type='+JSON.stringify(type)+']';choices.push({selector:s,count:document.querySelectorAll(s).length});}
   if(tag(e)!=='OTHER')choices.push({selector:e.tagName.toLowerCase(),count:document.querySelectorAll(e.tagName.toLowerCase()).length});
   const chosen=choices.find(c=>c.count===1)||choices[0]||null;
   return {id:ids.get(e),tag:tag(e),type,metadata,redacted_attributes:redacted,
    contenteditable:!!e.isContentEditable,disabled:!!e.disabled,visible:visible(e),editable,
    in_form:!!form,form:ids.get(form)||null,shell:ids.get(shell)||null,
    fingerprint:chain.join(' > '),candidate:chosen,
    challenge:e.matches('iframe[src*="challenges.cloudflare.com/"],iframe[src*="www.google.com/recaptcha/"],iframe[src*="hcaptcha.com/"]')};
 });
 return {inventory:rows,truncated:relevant.length>limit};
}'''


def observe(page):
    return page.evaluate(PROBE,WORDS)


def classify(data):
    rows=data['inventory'];by_id={r['id']:r for r in rows}
    out={k:{'status':'UNVERIFIED','evidence':[],'candidates':[]} for k in REQUIRED+OPTIONAL}
    selectors={}
    def semantic(r):return re.sub(r'([a-z])([A-Z])',r'\1 \2',' '.join(r['metadata'].values())).lower().replace('-',' ').replace('_',' ')
    def has(r,*words):return any(w in semantic(r).split() for w in words)
    def select(key,candidates,proof):
        out[key]['candidates']=[{'element':r['id'],**r['candidate']} for r in candidates if r['candidate']]
        if len(candidates)>1:out[key]['status']='AMBIGUOUS';return None
        if not candidates:return None
        r=candidates[0];c=r['candidate']
        if not c:return None
        if c['count']!=1:out[key]['status']='AMBIGUOUS';return None
        evidence=proof(r)
        out[key]['status']='VERIFIED' if evidence else 'CANDIDATE'
        out[key]['evidence']=evidence or ['INSUFFICIENT_STRUCTURAL_EVIDENCE']
        if evidence:selectors[key]=c['selector'];return r
        return None
    composer=select('composer',[r for r in rows if r['editable'] and r['tag'] in ('TEXTAREA','INPUT','DIV','SPAN')],
        lambda r:['VISIBLE_EDITABLE_UNIQUE','CHAT_SEMANTIC_ATTRIBUTE'] if (has(r,'chat','message','messages','composer','compose','prompt','pesan','tanya') or (has(r,'ask') and has(r,'neurobro'))) and r['type'] not in ('email','password','search') else [])
    send=None
    if composer:
        send=select('send',[r for r in rows if r['visible'] and (r['tag']=='BUTTON' or r['type']=='submit' or r['metadata'].get('role')=='button') and (has(r,'send','kirim') or r['type']=='submit')],
          lambda r:['SAME_FORM_AS_COMPOSER','SEND_CONTROL'] if r['form'] and r['form']==composer['form'] else [])
    if composer and send:
        shells=[r for r in rows if (r['metadata'].get('role')=='application' or r['tag']=='MAIN') and r['visible']]
        shell=select('authenticated',shells,lambda r:['COMPOSER_AND_SEND','AUTHENTICATED_APPLICATION_SHELL'] if
               composer['shell']==r['id'] and send['shell']==r['id'] and (r['metadata'].get('data-authenticated')=='true' or r['metadata'].get('data-session-state')=='authenticated') else [])
        if shell:
            marker='[data-authenticated="true"]' if shell['metadata'].get('data-authenticated')=='true' else '[data-session-state="authenticated"]'
            selectors['authenticated']+=marker
    login=select('login_required',[r for r in rows if has(r,'login','masuk') or (has(r,'sign','log') and has(r,'in'))],
        lambda r:['LOGIN_CONTROL_SEMANTICS'] if r['tag'] in ('BUTTON','FORM','A') else [])
    # Provider classification is a boolean only; iframe URL is never returned/read.
    challenge=[r for r in rows if r['challenge']]
    select('captcha',challenge,lambda r:['KNOWN_CHALLENGE_IFRAME'] if r['tag']=='IFRAME' else [])
    if out['captcha']['status']=='VERIFIED':
        # Never use generic iframe selector as a challenge detector.
        selectors['captcha']='iframe[src*="challenges.cloudflare.com/"],iframe[src*="www.google.com/recaptcha/"],iframe[src*="hcaptcha.com/"]'
    select('loading',[r for r in rows if has(r,'loading','memuat')],lambda r:['CHAT_LOADING_SEMANTICS'] if has(r,'chat','conversation') and r['metadata'].get('role') in ('status','progressbar') else [])
    select('new_chat',[r for r in rows if has(r,'new','baru') and has(r,'chat','percakapan')],lambda r:['NEW_CHAT_CONTROL'] if r['tag']=='BUTTON' else [])
    select('upload',[r for r in rows if r['tag']=='INPUT' and r['type']=='file'],lambda r:['FILE_INPUT_SAME_FORM'] if composer and r['form'] and r['form']==composer['form'] and not r['disabled'] else [])
    # Message collections/lifecycle require separately reviewed scoping evidence.
    # Report their observed structural candidates, never infer completion from text.
    for key,terms in {'assistant_messages':('assistant',),'user_messages':('user',),'response_text':('response',),
                      'completed_response':('complete','completed'),'attachment_ready':('attachment','ready'),
                      'streaming':('streaming',)}.items():
        select(key,[r for r in rows if has(r,*terms)],lambda r:[])
    state='CLOUDFLARE_REQUIRED' if any(r['visible'] for r in challenge) else (
        'LOGIN_REQUIRED' if login and login['visible'] else
        'LOADING' if out['loading']['status']=='VERIFIED' and any(r['visible'] and has(r,'loading','memuat') for r in rows) else
        'AUTHENTICATED' if out['authenticated']['status']=='VERIFIED' else 'UNVERIFIED')
    if data['truncated']:
        # A partial DOM cannot prove global uniqueness/absence of conflicting UI.
        for v in out.values():
            if v['status']=='VERIFIED':v['status']='CANDIDATE';v['evidence']=['INVENTORY_TRUNCATED']
        selectors={};state='UNVERIFIED' if state=='AUTHENTICATED' else state
    return {'version':VERSION,'state':state,'selectors':selectors,'status':{k:v['status'] for k,v in out.items()},
            'evidence':out,'inventory':rows,'truncated':data['truncated']}


def discover_phase2(page):
    u=urlsplit(page.url)
    if u.scheme!='https' or u.hostname!='app.neurobro.ai':return classify({'inventory':[],'truncated':False})
    first=observe(page);second=observe(page)
    result=classify(second)
    if first!=second:
        for key in result['status']:
            if result['status'][key]=='VERIFIED':
                result['status'][key]='CANDIDATE';result['evidence'][key]['status']='CANDIDATE'
                result['evidence'][key]['evidence']=['DOM_NOT_STABLE']
        result['selectors']={}
        if result['state']=='AUTHENTICATED':result['state']='UNVERIFIED'
    return result
