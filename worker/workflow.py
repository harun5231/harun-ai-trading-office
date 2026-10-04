from dataclasses import dataclass
from pathlib import Path
from decimal import Decimal as D
import hashlib
import json
import os
import time
import uuid
from .core import Review, Locked, Rules, Ledger, coins, parse_signal, risk_check, now
from .prompts import SCREENING, ANALYSIS

@dataclass(frozen=True)
class Capture:
    symbol: str
    timeframe: str
    path: str
    sha256: str
    captured_at: float
    source: str
    def verify(self,symbol,timeframe,source):
        if self.symbol!=symbol or self.timeframe!=timeframe or self.source!=source:
            raise Review('NEEDS_REVIEW: symbol/timeframe/sumber screenshot tidak cocok')
        if not 0<=time.time()-self.captured_at<=600:
            raise Review('NEEDS_REVIEW: screenshot kedaluwarsa')
        if hashlib.sha256(Path(self.path).read_bytes()).hexdigest()!=self.sha256:
            raise Review('NEEDS_REVIEW: screenshot berubah setelah verifikasi')

AGENT={'SCREENING_NEUROBRO':'Neurobro','COINS_SELECTED':'Market Analyst','CAPTURE_1H':'Market Analyst',
'CAPTURE_15M':'Market Analyst','NEUROBRO_ANALYSIS':'Neurobro','SIGNAL_RECEIVED':'Neurobro',
'RISK_CHECK':'Risk Manager','BINANCE_SETUP':'Trading Agent','ORDER_READY':'Trading Agent',
'POSITION_OPEN':'Position Monitor','MONITORING':'Position Monitor','CLOSED':'Trade Reviewer',
'IDLE':'Coordinator','ERROR':'Risk Manager'}
ALLOWED={
 'IDLE':{'SCREENING_NEUROBRO','MONITORING'},'SCREENING_NEUROBRO':{'COINS_SELECTED'},
 'COINS_SELECTED':{'CAPTURE_1H'},'CAPTURE_1H':{'CAPTURE_15M'},
 'CAPTURE_15M':{'CAPTURE_1H','NEUROBRO_ANALYSIS'},'NEUROBRO_ANALYSIS':{'SIGNAL_RECEIVED'},
 'SIGNAL_RECEIVED':{'RISK_CHECK'},'RISK_CHECK':{'BINANCE_SETUP'},'BINANCE_SETUP':{'ORDER_READY'},
 'ORDER_READY':{'NEUROBRO_ANALYSIS','POSITION_OPEN','MONITORING'},
 'POSITION_OPEN':{'MONITORING'},'MONITORING':{'POSITION_OPEN','CLOSED'},
 'CLOSED':{'POSITION_OPEN','MONITORING'},'ERROR':set()}

class Workflow:
    def __init__(self,ledger,adapter,snapshot_path):
        self.ledger,self.adapter,self.output=ledger,adapter,Path(snapshot_path)
        self.state='IDLE'
        self.ledger.event('IDLE','Coordinator','Worker DRY RUN siap; '+adapter.source)
    def export(self):
        data=self.ledger.snapshot(self.adapter.source,getattr(self.adapter,'connected',False))
        self.output.parent.mkdir(parents=True,exist_ok=True)
        tmp=self.output.with_suffix('.tmp');tmp.write_text(json.dumps(data,indent=2));os.replace(tmp,self.output)
        return data
    def go(self,state,message):
        if state!='ERROR' and state not in ALLOWED.get(self.state,set()): raise Review('NEEDS_REVIEW: transisi state tidak valid')
        self.state=state;self.ledger.event(state,AGENT[state],message);self.export()
    def run(self):
        try:
            if self.ledger.count()>=2: raise Locked('LOCKED: batas harian tercapai')
            self.go('SCREENING_NEUROBRO','Mengirim prompt screening persis; '+self.adapter.source)
            selected=coins(self.adapter.screen(SCREENING))
            self.go('COINS_SELECTED',', '.join(selected))
            captures={}
            for symbol in selected:
                captures[symbol]=[]
                for tf,state in [('1h','CAPTURE_1H'),('15m','CAPTURE_15M')]:
                    self.go(state,f'{symbol} / {tf}')
                    cap=self.adapter.capture(symbol,tf)
                    cap.verify(symbol,tf,self.adapter.source);captures[symbol].append(cap)
            for symbol in selected:
                if self.ledger.count()>=2: raise Locked('LOCKED: slot harian habis; coin berikutnya tidak diproses')
                self.go('NEUROBRO_ANALYSIS',symbol+' / dua screenshot terverifikasi')
                for cap,tf in zip(captures[symbol],('1h','15m')): cap.verify(symbol,tf,self.adapter.source)
                answer=self.adapter.analyze(symbol,captures[symbol],ANALYSIS)
                signal=parse_signal(answer,symbol)
                self.go('SIGNAL_RECEIVED',symbol+' / field lengkap')
                self.go('RISK_CHECK',symbol+' / risiko harga maksimum 5 USDT')
                rules=self.adapter.rules(symbol)
                plan=risk_check(signal,rules)
                self.go('BINANCE_SETUP',symbol+' / rencana paper LIMIT, CROSS, 75x; tidak mengubah akun Binance')
                trade_id=self.ledger.reserve(plan,self.adapter.source,rules)
                self.go('ORDER_READY',symbol+' / paper order '+trade_id)
            if self.adapter.source=='FIXTURE':
                self.monitor_fixture()
            # Browser dry run stops at verified paper plans: no pretend fill or real submit.
            return self.export()
        except Exception as exc:
            message=str(exc) if isinstance(exc,Review) else 'ERROR: langkah worker gagal; periksa log lokal tanpa melanjutkan order'
            self.go('ERROR',message)
            return self.export()
    def monitor_fixture(self):
        for row in self.ledger.db.execute("SELECT * FROM trades WHERE state='ORDER_READY' ORDER BY created").fetchall():
            p=json.loads(row['plan'])
            from .monitor import PaperMonitor
            monitor=PaperMonitor(self.ledger)
            sign=D('1') if p['side']=='LONG' else D('-1')
            for quote in [D(p['entry'])+sign,D(p['entry']),D(p['entry'])+sign,D(p['tp'])]:
                for state in monitor.quote(row['id'],p['symbol'],str(quote),'FIXTURE'):
                    self.go(state,p['symbol']+' / quote FIXTURE; tidak ada order Binance')

class FixtureAdapter:
    source='FIXTURE'
    connected=False
    def __init__(self,directory): self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
    def screen(self,prompt):
        assert prompt==SCREENING
        return 'BTCUSDT\nETHUSDT'
    def capture(self,symbol,timeframe):
        # Test artifact, intentionally NOT a counterfeit Binance chart image.
        p=self.directory/(uuid.uuid4().hex+'.fixture.txt')
        p.write_text(f'FIXTURE ONLY: {symbol} {timeframe}; no real chart')
        return Capture(symbol,timeframe,str(p),hashlib.sha256(p.read_bytes()).hexdigest(),time.time(),self.source)
    def analyze(self,symbol,captures,prompt):
        assert prompt==ANALYSIS
        return json.dumps({'symbol':symbol,'side':'LONG','entry':'100','tp':'104','sl':'98','quantity':'10','quantity_unit':symbol[:-4]})
    def rules(self,symbol): return Rules(D('.001'),D('.001'),D('10000'),D('.01'),D('5'),time.time())
