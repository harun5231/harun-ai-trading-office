import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from agent import native_human_gate_status as h
from scripts import patch_native_human_gate_status as p
from tests.test_patch_native_human_gate_status import native_source


NODE_DOM=r'''
const input=JSON.parse(require('fs').readFileSync(0,'utf8')),scene=input.scene,byId=new Map(),clicks=[];
const matches=(e,selector)=>selector.split(',').some(part=>{
 const m=part.trim().match(/^(?:([a-z]+))?(?:\[([a-z-]+)(?:=["']?([^"'\]]+)["']?)?\])?$/i);
 if(!m)throw new Error('unexpected selector '+part);
 return (!m[1]||e.tag===m[1].toLowerCase())&&(!m[2]||(Object.hasOwn(e.attrs,m[2])&&(m[3]===undefined||e.attrs[m[2]]===m[3])));
});
class Element {
 constructor(row){Object.assign(this,row);this.attrs=row.attrs||{};this.style=row.style||{};this.children=[];this.isConnected=row.connected!==false;this.nodeType=1;this.tagName=row.tag.toUpperCase();this.disabled=!!row.disabled;this.form=row.form||null;}
 getAttribute(k){return this.attrs[k]??null;}
 getBoundingClientRect(){const r={x:100,y:100,width:300,height:50,...this.rect};return {...r,left:r.x,top:r.y,right:r.x+r.width,bottom:r.y+r.height};}
 matches(s){return matches(this,s);}
 closest(s){for(let e=this;e;e=e.parentElement)if(matches(e,s))return e;return null;}
 get textContent(){return (this.text||'')+' '+this.children.map(e=>e.textContent).join(' ');}
 querySelectorAll(s){const out=[];const walk=e=>{for(const c of e.children){if(matches(c,s))out.push(c);walk(c);}};walk(this);return out;}
 querySelector(s){return this.querySelectorAll(s)[0]||null;}
 contains(e){for(;e;e=e.parentElement)if(e===this)return true;return false;}
 click(){clicks.push(this.id);if(scene.throwClick)throw new Error('UNCERTAIN_CLICK');}
}
for(const row of[{id:'root',tag:'html'},...scene.elements])byId.set(row.id,new Element(row));
for(const row of scene.elements){const e=byId.get(row.id);e.parentElement=byId.get(row.parent||'root');e.parentElement.children.push(e);}
const doc={defaultView:{innerWidth:1280,innerHeight:900},elementFromPoint:()=>byId.get(scene.hit||'button'),
 createTreeWalker:(e,mask,filter)=>{const out=[];const walk=n=>{if(filter.acceptNode(n)===2)return;if(n.text){const t={nodeType:3,parentElement:n,nodeValue:n.text};if(filter.acceptNode(t)===1)out.push(t);}for(const c of n.children)walk(c);};walk(e);let at=0;return {nextNode:()=>out[at++]||null};}};
for(const e of byId.values())e.ownerDocument=doc;
global.NodeFilter={SHOW_ELEMENT:1,SHOW_TEXT:4,FILTER_ACCEPT:1,FILTER_REJECT:2,FILTER_SKIP:3};
global.getComputedStyle=e=>({display:'block',visibility:'visible',opacity:'1',pointerEvents:'auto',...e.style});
try{const result=eval('('+input.script+')')(byId.get('root'),input.contract);process.stdout.write(JSON.stringify({result,clicks}));}
catch(error){process.stdout.write(JSON.stringify({error:error.message,clicks}));}
'''


def notice():
    return [dict(id='dialog',tag='div',attrs={'role':'alertdialog'}),
        dict(id='title',parent='dialog',tag='h2',text='Pemeliharaan terjadwal'),
        dict(id='body',parent='dialog',tag='p',text=h.KNOWN_MAINTENANCE_BODY),
        dict(id='button',parent='dialog',tag='button',text='Mengerti')]


def ui(rows,**scene):
    payload={'script':h.KNOWN_MAINTENANCE_UI_JS,'scene':{'elements':rows,**scene},
        'contract':{'message_selector':'[data-message-id]','automation_allowed':True,
            'expected_body':h.KNOWN_MAINTENANCE_BODY}}
    result=subprocess.run(['node','-e',NODE_DOM],input=json.dumps(payload),text=True,
        capture_output=True,check=True,timeout=5)
    return json.loads(result.stdout)


class NoticeUI(unittest.TestCase):
    def test_only_exact_known_frontmost_notice_clicks_once(self):
        self.assertEqual(ui(notice()),{'result':True,'clicks':['button']})
        rows=notice();rows[2]['text']=rows[2]['text'].replace('03:00–04:00','03:00-04:00').replace(' ','\n')
        self.assertEqual(ui(rows),{'result':True,'clicks':['button']})

    def test_unknown_title_body_date_time_button_or_extra_public_text_never_clicks(self):
        for index,text in ((1,'Session Expired'),(2,'Please verify to continue.'),
                (2,h.KNOWN_MAINTENANCE_BODY.replace('5 Nov','6 Nov')),
                (2,h.KNOWN_MAINTENANCE_BODY.replace('04:00','05:00')),
                (3,'Continue')):
            rows=notice();rows[index]['text']=text
            with self.subTest(index=index,text=text):self.assertEqual(ui(rows),{'result':False,'clicks':[]})
        self.assertEqual(ui(notice()+[dict(id='extra',parent='dialog',tag='p',text='https://example.invalid')]),{'result':False,'clicks':[]})

    def test_hidden_offscreen_disconnected_or_quoted_notice_never_clicks(self):
        for change in (dict(style={'display':'none'}),dict(style={'opacity':'0'}),
                dict(rect={'x':1400}),dict(connected=False)):
            rows=notice();rows[0].update(change)
            with self.subTest(change=change):self.assertEqual(ui(rows),{'result':False,'clicks':[]})
        rows=[dict(id='old',tag='article',attrs={'data-message-id':'old'})]+notice();rows[1]['parent']='old'
        self.assertEqual(ui(rows),{'result':False,'clicks':[]})

    def test_multiple_visible_public_modals_or_buttons_never_click(self):
        for extra in (dict(id='other',tag='div',attrs={'role':'dialog'}),
                dict(id='other',parent='dialog',tag='button',text='Mengerti')):
            with self.subTest(extra=extra):self.assertEqual(ui(notice()+[extra]),{'result':False,'clicks':[]})

    def test_disabled_inert_non_button_or_non_frontmost_target_never_clicks(self):
        for change in (dict(disabled=True),dict(attrs={'aria-disabled':'true'}),
                dict(attrs={'inert':''}),dict(style={'pointerEvents':'none'}),
                dict(tag='div',attrs={'role':'button'}),dict(form='some-form')):
            rows=notice();rows[3].update(change)
            with self.subTest(change=change):self.assertEqual(ui(rows),{'result':False,'clicks':[]})
        rows=notice()+[dict(id='blocking',tag='div')]
        self.assertEqual(ui(rows,hit='blocking'),{'result':False,'clicks':[]})

    def test_no_link_code_iframe_form_or_input_even_if_hidden(self):
        for tag in ('a','code','pre','script','style','object','embed','iframe','form','input','textarea','select'):
            with self.subTest(tag=tag):self.assertEqual(ui(notice()+[dict(id='unsafe',parent='dialog',tag=tag,style={'display':'none'})]),{'result':False,'clicks':[]})
        for attr in ('href','src','formaction','onclick','contenteditable'):
            with self.subTest(attr=attr):self.assertEqual(ui(notice()+[dict(id='unsafe',parent='dialog',tag='span',attrs={attr:'value'})]),{'result':False,'clicks':[]})

    def test_racing_public_iframe_password_or_otp_prevents_click(self):
        for tag,attrs in (('iframe',{}),('input',{'type':'password'}),('input',{'autocomplete':'one-time-code'})):
            with self.subTest(tag=tag,attrs=attrs):self.assertEqual(ui(notice()+[dict(id='security',tag=tag,attrs=attrs)]),{'result':False,'clicks':[]})

    def test_uncertain_dispatch_is_not_retried_inside_one_observation(self):
        self.assertEqual(ui(notice(),throwClick=True),{'error':'UNCERTAIN_CLICK','clicks':['button']})


class AutoContract(unittest.TestCase):
    def test_callback_missing_manual_owner_session_or_exception_cannot_touch_page(self):
        for gate in (None,lambda:False,lambda:'AUTOMATION',Mock(side_effect=RuntimeError('MODE_UNKNOWN'))):
            page=Mock()
            self.assertFalse(h.acknowledge_known_maintenance_notice(page,message_selector='[data-message-id]',maintenance_allowed=gate))
            page.locator.assert_not_called()

    def test_current_gate_is_rechecked_after_locator_before_action(self):
        gate=Mock(side_effect=[True,False]);page=Mock()
        self.assertFalse(h.acknowledge_known_maintenance_notice(page,message_selector='[data-message-id]',maintenance_allowed=gate))
        self.assertEqual(gate.call_count,2);page.locator.return_value.evaluate.assert_not_called()

    def test_uncertain_evaluate_never_claims_acknowledgement_or_retries(self):
        page=Mock();page.locator.return_value.evaluate.side_effect=RuntimeError('UNCERTAIN_CLICK')
        self.assertFalse(h.acknowledge_known_maintenance_notice(page,message_selector='[data-message-id]',maintenance_allowed=lambda:True))
        self.assertEqual(page.locator.return_value.evaluate.call_count,1)


ACTOR_FIXTURE=json.loads((Path(__file__).parent/'fixtures/native_maintenance_actor_abi.json').read_text())


def actor_source():
    return ('class BrowserActor:\n    '+ACTOR_FIXTURE['_mode']['source']+'\n    '+
        ACTOR_FIXTURE['_observe_site_status']['source']+'\n').encode()


def actor_and_bridge(mode='AUTOMATION',manual=None,session=False,stopping=False):
    raw=native_source();namespace={};exec(p.patch(raw,expected_sha256=hashlib.sha256(raw).hexdigest()),namespace)
    bridge=namespace['DOMBridge']();bridge._origin_valid=lambda:True;bridge._operation_contract=None;bridge._dom_contract=None
    state={'dialog':True,'clicks':0}
    class Locator:
        def evaluate(self,script,args,*,timeout):
            if script==h.HUMAN_GATE_UI_JS:return False
            state['clicks']+=1;state['dialog']=False;return True
    page=SimpleNamespace(url='https://app.neurobro.ai',is_closed=lambda:False,locator=lambda _:Locator())
    bridge.page=page;bridge._visible=lambda selector:[object()]if selector.startswith('[role=')and state['dialog']else[]
    bridge._current_secure_loader_blocking=lambda:False;bridge.session_observation=lambda:bridge._session_observation
    raw=actor_source();scope={'time':time,'urlsplit':urlsplit,'SESSION_REASONS':{'CHALLENGE_VISIBLE','CONNECTED','VISIBLE_BLOCKING_DIALOG'},'SESSION_COUNTS':set()}
    exec(p.patch_actor(raw,expected_sha256=hashlib.sha256(raw).hexdigest()),scope)
    actor=scope['BrowserActor']();actor.lock=threading.Lock();actor.mode=mode;actor._manual=manual
    actor.sessions=SimpleNamespace(active=lambda:session);actor.stopping=SimpleNamespace(is_set=lambda:stopping)
    actor._bridge=bridge;actor._context=SimpleNamespace(pages=[page]);actor.checked_at='retained-test-clock'
    def status(value):actor.current_status=value
    actor._status=status
    return actor,bridge,state


class NativePatchContract(unittest.TestCase):
    def test_actor_patch_changes_only_the_exact_owner_thread_status_call(self):
        raw=actor_source();result=p.patch_actor(raw,expected_sha256=hashlib.sha256(raw).hexdigest())
        replacement=("self._bridge.status(maintenance_allowed=lambda: self._mode() == 'AUTOMATION' "
            "and self._manual is None and not self.sessions.active() and not self.stopping.is_set())").encode()
        self.assertEqual(result.count(replacement),1);self.assertEqual(result.replace(replacement,b'self._bridge.status()'),raw)
        self.assertEqual(ACTOR_FIXTURE['actual_module_sha256'],p.ACTOR_BEFORE_SHA256)
        with self.assertRaisesRegex(ValueError,'PARENT_SOURCE_UNVERIFIED'):p.patch_actor(raw)
        changed=raw.replace(b'return self.mode',b"return 'AUTOMATION'")
        with self.assertRaisesRegex(ValueError,'CONTRACT_UNVERIFIED'):p.patch_actor(changed,expected_sha256=hashlib.sha256(changed).hexdigest())

    def test_actual_owner_thread_only_auto_and_unowned_browser_can_acknowledge(self):
        for mode,manual,session,stopping in (('MANUAL',None,False,False),('FAILED',None,False,False),
                ('AUTOMATION',object(),False,False),('AUTOMATION',None,True,False),('AUTOMATION',None,False,True)):
            actor,bridge,state=actor_and_bridge(mode,manual,session,stopping);actor._observe_site_status()
            with self.subTest(mode=mode,manual=manual,session=session,stopping=stopping):
                self.assertEqual(state['clicks'],0);self.assertEqual(actor.mode,mode);self.assertEqual(actor.current_status,'UNAVAILABLE')
        actor,bridge,state=actor_and_bridge();actor._observe_site_status()
        self.assertEqual(state['clicks'],1);self.assertEqual(actor.mode,'AUTOMATION');self.assertEqual(actor.current_status,'CONNECTED')
        self.assertEqual(actor._session_diagnostic['status'],'CONNECTED')

    def test_legacy_status_call_never_acknowledges_and_success_flag_does_not_force_ready(self):
        actor,bridge,state=actor_and_bridge()
        self.assertEqual(bridge.status(),'UNAVAILABLE');self.assertEqual(state['clicks'],0)
        with patch.object(h,'acknowledge_known_maintenance_notice',return_value=True):
            self.assertEqual(bridge.status(maintenance_allowed=lambda:True),'UNAVAILABLE')
        self.assertIsNone(bridge._dom_contract)

    def test_origin_iframe_human_password_and_otp_keep_priority_before_ordinary_click(self):
        actor,bridge,state=actor_and_bridge()
        with patch.object(h,'acknowledge_known_maintenance_notice',side_effect=AssertionError('ordinary must not run')):
            bridge._origin_valid=lambda:False;self.assertEqual(bridge.status(maintenance_allowed=lambda:True),'UNAVAILABLE')
            bridge._origin_valid=lambda:True
            bridge._visible=lambda selector:[object()]if selector==p.KNOWN_CHALLENGE_SELECTOR else[]
            self.assertEqual(bridge.status(maintenance_allowed=lambda:True),'CHALLENGE_REQUIRED')
            bridge._visible=lambda selector:[]
            with patch.object(h,'visible_human_gate',return_value=True):
                self.assertEqual(bridge.status(maintenance_allowed=lambda:True),'CHALLENGE_REQUIRED')
            bridge._visible=lambda selector:[object()]if selector.startswith('input[')else[]
            self.assertEqual(bridge.status(maintenance_allowed=lambda:True),'LOGIN_REQUIRED')
        self.assertEqual(state['clicks'],0)


if __name__=='__main__':unittest.main()
