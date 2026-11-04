import json
from pathlib import Path
import subprocess
import unittest

from agent.native_human_gate_status import (HUMAN_GATE_UI_JS,
    UI_OBSERVATION_TIMEOUT_MS, visible_human_gate)


NODE_DOM = r'''
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const scene=input.scene;
const byId=new Map();
class Element {
 constructor(row){Object.assign(this,row);this.attrs=row.attrs||{};this.style=row.style||{};this.children=[];this.isConnected=row.connected!==false;this.nodeType=1;}
 getAttribute(key){return this.attrs[key]??null;}
 getBoundingClientRect(){const r={x:0,y:0,width:100,height:30,...this.rect};return {...r,left:r.x,top:r.y,right:r.x+r.width,bottom:r.y+r.height};}
 closest(selector){if(selector!=='[data-message-id]')throw new Error('unexpected message contract');for(let p=this;p;p=p.parentElement){if(Object.hasOwn(p.attrs,'data-message-id'))return p;}return null;}
 matches(selector){if(selector!=='[data-message-id]')throw new Error('unexpected message contract');return Object.hasOwn(this.attrs,'data-message-id');}
 get textContent(){return (this.text||'')+' '+this.children.map(c=>c.textContent).join(' ');}
 querySelectorAll(selector){
  const rows=[];const walk=e=>{for(const c of e.children){rows.push(c);walk(c);}};walk(this);
  if(selector==='iframe[title]')return rows.filter(e=>e.tag==='iframe'&&Object.hasOwn(e.attrs,'title'));
  if(selector.startsWith('[role="dialog"]'))return rows.filter(e=>['dialog','alertdialog'].includes(e.attrs.role)||e.attrs['aria-modal']==='true'||(e.tag==='div'&&(e.classes||'').split(' ').includes('fixed')&&(e.classes||'').split(' ').includes('inset-0')));
  if(selector==='h1,h2,h3,h4,[role="heading"]')return rows.filter(e=>['h1','h2','h3','h4'].includes(e.tag)||e.attrs.role==='heading');
  throw new Error('unexpected public UI query');
 }
}
for(const row of [{id:'root',tag:'html'},...scene.elements])byId.set(row.id,new Element(row));
for(const row of scene.elements){const e=byId.get(row.id);e.parentElement=byId.get(row.parent||'root');e.parentElement.children.push(e);}
const root=byId.get('root');
const doc={
 defaultView:{innerWidth:1280,innerHeight:900},
 elementFromPoint:()=>byId.get(scene.center||'root'),
 createTreeWalker:(e,mask,filter)=>{const rows=[];const walk=n=>{if(filter.acceptNode(n)===2)return;if(n.text){const t={parentElement:n,nodeValue:n.text,nodeType:3};if(filter.acceptNode(t)===1)rows.push(t);}for(const c of n.children)walk(c);};walk(e);let i=0;return {nextNode:()=>rows[i++]||null};}
};
for(const e of byId.values())e.ownerDocument=doc;
global.NodeFilter={SHOW_ELEMENT:1,SHOW_TEXT:4,FILTER_ACCEPT:1,FILTER_REJECT:2,FILTER_SKIP:3};
global.getComputedStyle=e=>({display:'block',visibility:'visible',opacity:'1',...e.style});
try {const result=eval('('+input.script+')')(root,{message_selector:'[data-message-id]'});process.stdout.write(JSON.stringify({result}));}
catch(error){process.stdout.write(JSON.stringify({error:error.message}));}
'''


def ui(elements, *, center='root'):
    payload={'script':HUMAN_GATE_UI_JS,'scene':{'elements':elements,'center':center}}
    result=subprocess.run(['node','-e',NODE_DOM],input=json.dumps(payload),
        text=True,capture_output=True,check=True,timeout=5)
    return json.loads(result.stdout)


class PublicHumanGateTests(unittest.TestCase):
    def test_visible_cloudflare_title_is_a_gate_even_without_exposed_src(self):
        self.assertEqual(ui([dict(id='frame',tag='iframe',attrs={
            'title':'Widget containing a Cloudflare security challenge'})]),{'result':True})

    def test_visible_session_expired_overlay_above_composer_is_a_gate(self):
        self.assertEqual(ui([dict(id='composer',tag='textarea'),
            dict(id='overlay',tag='div',classes='fixed inset-0'),
            dict(id='title',parent='overlay',tag='h2',text='Session Expired'),
            dict(id='copy',parent='overlay',tag='p',
                 text='Your session has expired. Please verify to continue.')]),{'result':True})

    def test_unlabelled_center_overlay_uses_public_ui_text_not_widget_contents(self):
        self.assertEqual(ui([dict(id='overlay',tag='div'),
            dict(id='title',parent='overlay',tag='span',text='Session Expired'),
            dict(id='copy',parent='overlay',tag='span',
                text='Your session has expired. Please verify to continue.')],center='copy'),{'result':True})

    def test_old_assistant_discussion_and_quoted_widget_are_not_current_gate(self):
        self.assertEqual(ui([dict(id='message',tag='article',attrs={'data-message-id':'old'}),
            dict(id='quote',parent='message',tag='div',classes='fixed inset-0'),
            dict(id='title',parent='quote',tag='h2',text='Session Expired'),
            dict(id='copy',parent='quote',tag='p',text='Please verify to continue.'),
            dict(id='frame',parent='quote',tag='iframe',attrs={
                'title':'Widget containing a Cloudflare security challenge'})],center='copy'),{'result':False})

    def test_message_text_is_excluded_even_inside_a_large_page_overlay(self):
        self.assertEqual(ui([dict(id='container',tag='div',classes='fixed inset-0'),
            dict(id='message',parent='container',tag='article',attrs={'data-message-id':'old'},
                text='Session Expired. Please verify to continue.'),
            dict(id='copy',parent='container',tag='p',text='Chat is available.')],center='copy'),{'result':False})

    def test_hidden_disconnected_or_zero_size_widget_does_not_require_human(self):
        for change in (dict(style={'display':'none'}),dict(style={'visibility':'hidden'}),
                dict(style={'opacity':'0'}),dict(connected=False),dict(rect={'width':0}),
                dict(rect={'x':-200}),dict(rect={'x':1300}),dict(rect={'y':950})):
            with self.subTest(change=change):
                frame=dict(id='frame',tag='iframe',attrs={
                    'title':'Widget containing a Cloudflare security challenge'},**change)
                self.assertEqual(ui([frame]),{'result':False})

    def test_hidden_parent_does_not_make_old_gate_visible(self):
        self.assertEqual(ui([dict(id='old',tag='div',style={'opacity':'0'}),
            dict(id='frame',parent='old',tag='iframe',attrs={
                'title':'Widget containing a Cloudflare security challenge'})]),{'result':False})

    def test_login_and_scheduled_maintenance_are_not_relabelled_as_verification(self):
        for title,copy in [('Session Expired','Please sign in again.'),
                ('Pemeliharaan terjadwal','5 Nov, 03:00–04:00 UTC. Gangguan singkat mungkin terjadi.')]:
            with self.subTest(title=title):
                self.assertEqual(ui([dict(id='overlay',tag='div',attrs={'role':'dialog'}),
                    dict(id='title',parent='overlay',tag='h2',text=title),
                    dict(id='copy',parent='overlay',tag='p',text=copy)],center='copy'),{'result':False})

    def test_regular_page_center_does_not_scan_oversized_public_sidebar_history(self):
        self.assertEqual(ui([dict(id='main',tag='main',rect={'width':1280,'height':900},
            text='Normal public sidebar title. '*200)],center='main'),{'result':False})

    def test_historical_message_counts_do_not_become_a_public_ui_limit(self):
        rows=[dict(id='message',tag='article',attrs={'data-message-id':'old'})]
        rows += [dict(id='frame'+str(n),parent='message',tag='iframe',attrs={
            'title':'Widget containing a Cloudflare security challenge'})for n in range(80)]
        rows += [dict(id='heading'+str(n),parent='message',tag='h2',text='Session Expired')for n in range(150)]
        rows += [dict(id='overlay'+str(n),parent='message',tag='div',attrs={'role':'dialog'},
            text='Session Expired. Please verify to continue.')for n in range(80)]
        self.assertEqual(ui(rows,center='heading0'),{'result':False})
        rows.append(dict(id='actual',tag='iframe',attrs={
            'title':'Widget containing a Cloudflare security challenge'}))
        self.assertEqual(ui(rows,center='heading0'),{'result':True})

    def test_message_subtrees_are_not_scanned_towards_public_text_budget(self):
        rows=[dict(id='overlay',tag='div',classes='fixed inset-0'),
            dict(id='message',parent='overlay',tag='article',attrs={'data-message-id':'old'}),
            dict(id='copy',parent='overlay',tag='p',text='Ordinary available chat.')]
        rows += [dict(id='text'+str(n),parent='message',tag='span',
            text='Session Expired. Please verify to continue.')for n in range(2100)]
        self.assertEqual(ui(rows,center='copy'),{'result':False})

    def test_observation_budget_failure_is_explicit_not_false_connected(self):
        frames=[dict(id='frame'+str(n),tag='iframe',attrs={'title':'ordinary frame'})for n in range(65)]
        self.assertEqual(ui(frames),{'error':'HUMAN_GATE_UI_OBSERVATION_UNVERIFIED'})

    def test_script_never_reads_challenge_internals_or_invokes_page_actions(self):
        for forbidden in ('shadowRoot','contentDocument','contentWindow','fetch(',
                'XMLHttpRequest','.click(','.fill(','.press(','.goto(','.scroll'):
            self.assertNotIn(forbidden,HUMAN_GATE_UI_JS)


class PageContractTests(unittest.TestCase):
    def test_native_page_observation_has_bounded_timeout_and_boolean_contract(self):
        class Locator:
            def evaluate(self,script,arg,*,timeout):
                self.values=script,arg,timeout
                return True
        class Page:
            def __init__(self):self.target=Locator()
            def locator(self,selector):
                self.selector=selector
                return self.target
        p=Page()
        self.assertTrue(visible_human_gate(p,message_selector='[data-message-id]'))
        self.assertEqual(p.selector,'html')
        self.assertEqual(p.target.values,(HUMAN_GATE_UI_JS,
            {'message_selector':'[data-message-id]'},UI_OBSERVATION_TIMEOUT_MS))
        for value in ('true',1,None,{}):
            p.target.evaluate=lambda *a,**k:value
            with self.assertRaisesRegex(ValueError,'HUMAN_GATE_UI_OBSERVATION_UNVERIFIED'):
                visible_human_gate(p,message_selector='[data-message-id]')

    def test_message_selector_is_not_an_unverified_request_value(self):
        for value in ('',None,'x\nbody','x'*1025):
            with self.assertRaisesRegex(ValueError,'HUMAN_GATE_MESSAGE_CONTRACT_UNVERIFIED'):
                visible_human_gate(None,message_selector=value)


if __name__=='__main__':unittest.main()
