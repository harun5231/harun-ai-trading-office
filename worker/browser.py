"""Browser-only Neurobro adapter. Selectors must be verified on the signed-in UI.
No Binance trade-form selector, account-setting change, or submit method exists here.
"""
from .desktop import browser_options
import hashlib
import json
import time
import uuid
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen
from .core import Review, Rules
from .workflow import Capture

class BrowserAdapter:
    source='BROWSER_DRY_RUN'
    connected=False
    def __init__(self,config,profile,artifacts):
        from .screening import private_profile, PendingSend
        self.config=config
        self.profile=private_profile(profile)
        self.pending=PendingSend(self.profile)
        self.handoff=None
        self.artifacts=Path(artifacts);self.artifacts.mkdir(parents=True,exist_ok=True)
        self.neuro=config.get('neurobro',{});self.binance=config.get('binance',{})
        self.progress=lambda state,message: None
    def open_screening(self):
        from .screening import ChatScreening
        self.progress('OPENING_NEUROBRO','Membuka chatbot resmi pada profil browser privat')
        ChatScreening.validate(self.neuro)
        if not self.config.get('selectors_verified_on'):
            raise Review('NEEDS_REVIEW: audit selector belum dilakukan')
        try:
            from playwright.sync_api import sync_playwright
            self.pw=sync_playwright().start()
            self.context=self.pw.chromium.launch_persistent_context(str(self.profile),headless=False,accept_downloads=False,**browser_options())
            self.context.set_default_timeout(5000)
            # Reuse the private session, not a second browser's exported cookies.
            self.chat=self.context.new_page();self.market=self.context.new_page()
            self.chat.goto(self.neuro['url'],wait_until='domcontentloaded',timeout=60000)
        except Exception:
            self.close()
            raise Review('NEEDS_REVIEW: browser/navigasi Neurobro gagal; periksa instalasi dan koneksi lokal') from None
    def close(self):
        self.connected=False
        if hasattr(self,'context'): self.context.close()
        if hasattr(self,'pw'): self.pw.stop()
    def _ask(self,prompt,attachments=None):
        c=self.chat;n=self.neuro
        count=c.locator(n['assistant_messages']).count()
        if attachments:
            if c.locator(n['attachment_ready']).count():
                raise Review('NEEDS_REVIEW: masih ada attachment percakapan sebelumnya')
            upload=c.locator(n['upload'])
            upload.set_input_files([a.path for a in attachments])
            filenames=upload.evaluate('(e)=>Array.from(e.files).map(f=>f.name)')
            if filenames!=[Path(a.path).name for a in attachments]:
                raise Review('NEEDS_REVIEW: berkas upload tidak sesuai screenshot terverifikasi')
            c.wait_for_function('(s)=>document.querySelectorAll(s).length===2',arg=n['attachment_ready'],timeout=60000)
        composer=c.locator(n['composer'])
        composer.fill(prompt)
        value=composer.input_value() if composer.evaluate('(e)=>e.tagName==="TEXTAREA"||e.tagName==="INPUT"') else composer.inner_text()
        if value.replace('\r\n','\n')!=prompt: raise Review('NEEDS_REVIEW: isi prompt di browser berubah')
        c.locator(n['send']).click()
        c.wait_for_function('([s,n])=>document.querySelectorAll(s).length>n',arg=[n['assistant_messages'],count],timeout=180000)
        messages=c.locator(n['assistant_messages'])
        if messages.count()!=count+1: raise Review('NEEDS_REVIEW: respons baru tidak tunggal')
        response=messages.nth(count)
        # A verified completion marker INSIDE this exact message; no fixed sleeps or stale replies.
        response.locator(n['completed_response']).wait_for(state='visible',timeout=180000)
        text=response.inner_text().strip()
        if not text: raise Review('NEEDS_REVIEW: respons kosong')
        return text
    def screen(self,prompt):
        from .screening import ChatScreening, futures_catalog, parse_screening
        self.pending.check()
        self.open_screening()
        answer=ChatScreening(self.chat,self.neuro,self.progress,
                             handoff=self.handoff,before_send=self.pending.mark).run(prompt)
        selected=parse_screening(answer,futures_catalog())
        self.pending.complete()
        self.connected=True
        return '\n'.join(selected)
    def capture(self,symbol,timeframe):
        import re
        if not re.fullmatch(r'[A-Z0-9]{2,18}USDT',symbol): raise Review('NEEDS_REVIEW: symbol tidak valid')
        p=self.market;b=self.binance
        p.goto('https://www.binance.com/en/futures/'+symbol,wait_until='domcontentloaded',timeout=60000)
        p.locator(b['timeframe_buttons'][timeframe]).click()
        expected=b.get('timeframe_labels',{}).get(timeframe)
        if not expected: raise Review('NEEDS_REVIEW: label timeframe belum dipetakan')
        p.locator(b['chart_ready']).wait_for(state='visible',timeout=60000)
        def verify():
            if urlparse(p.url).hostname!='www.binance.com' or urlparse(p.url).path.rstrip('/')!='/en/futures/'+symbol:
                raise Review('NEEDS_REVIEW: halaman bukan pair Binance yang diminta')
            displayed=p.locator(b['symbol']).inner_text().strip().replace('/','').replace(' ','')
            if displayed!=symbol or p.locator(b['timeframe']).inner_text().strip()!=expected:
                raise Review('NEEDS_REVIEW: chart symbol/timeframe tidak sesuai')
            # Scope must include the symbol and timeframe labels in the screenshot itself.
            chart=p.locator(b['chart'])
            if chart.locator(b['symbol']).count()!=1 or chart.locator(b['timeframe']).count()!=1:
                raise Review('NEEDS_REVIEW: area screenshot harus mencakup label symbol/timeframe')
        verify()
        filename=self.artifacts/(uuid.uuid4().hex+'.png')
        p.locator(b['chart']).screenshot(path=str(filename))
        verify()
        return Capture(symbol,timeframe,str(filename),hashlib.sha256(filename.read_bytes()).hexdigest(),time.time(),self.source)
    def analyze(self,symbol,captures,prompt):
        for cap,tf in zip(captures,('1h','15m')): cap.verify(symbol,tf,self.source)
        # A new chat is required for each coin so an old pair cannot contaminate attachment context.
        button=self.neuro.get('new_chat')
        if not button: raise Review('NEEDS_REVIEW: selector percakapan baru belum tersedia')
        self.chat.locator(button).click()
        self.chat.wait_for_function('(s)=>document.querySelectorAll(s).length===0',arg=self.neuro['assistant_messages'],timeout=30000)
        return self._ask(prompt,captures)
    def rules(self,symbol):
        # Public read-only exchange metadata; Neurobro still uses browser only.
        with urlopen('https://fapi.binance.com/fapi/v1/exchangeInfo',timeout=15) as response:
            info=json.load(response)
        rows=[s for s in info['symbols'] if s['symbol']==symbol and s['status']=='TRADING' and s['contractType']=='PERPETUAL' and s['quoteAsset']=='USDT' and s['marginAsset']=='USDT']
        if len(rows)!=1: raise Review('NEEDS_REVIEW: kontrak linear USDT perpetual tidak ditemukan')
        filters={f['filterType']:f for f in rows[0]['filters']};lot=filters['LOT_SIZE'];price=filters['PRICE_FILTER']
        return Rules(D(lot['stepSize']),D(lot['minQty']),D(lot['maxQty']),D(price['tickSize']),D(filters['MIN_NOTIONAL']['notional']),time.time(),D(price['minPrice']),D(price['maxPrice']))
