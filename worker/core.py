"""Fail-closed USDⓈ-M linear-contract risk checks and persistent dry-run ledger."""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, getcontext, DefaultContext
from zoneinfo import ZoneInfo
from fractions import Fraction
import hashlib
import json
import re
import sqlite3
import time

DefaultContext.prec = 160
getcontext().prec = 160
D = Decimal
TZ = ZoneInfo('Asia/Bangkok')
RISK = D('5')
STATES = ('IDLE','NEUROAPI_NOT_CONFIGURED','NEUROAPI_CONNECTED','SCREENING','COINS_SELECTED','MARKET_DATA','ANALYZING_COIN_1','ANALYZING_COIN_2','VALIDATING','DRY_RUN_READY','REJECTED','ORDER_READY','POSITION_OPEN','MONITORING','CLOSED','LOCKED','ERROR')
class Review(Exception): pass
class Locked(Review): pass

def now(): return datetime.now(timezone.utc).isoformat()
def day(): return datetime.now(TZ).date().isoformat()
def number(value):
    if not isinstance(value, (str,int,Decimal)) or isinstance(value,bool):
        raise Review('NEEDS_REVIEW: nilai numerik harus eksplisit')
    text = str(value).strip()
    if len(text)>64:raise Review('INVALID_NUMBER_SIZE')
    if not re.fullmatch(r'\d+(?:\.\d+)?',text):
        raise Review('NEEDS_REVIEW: angka ambigu / rentang / pemisah ribuan')
    try: value = D(text)
    except InvalidOperation: raise Review('NEEDS_REVIEW: angka tidak valid')
    if not value.is_finite() or value <= 0: raise Review('NEEDS_REVIEW: angka harus positif')
    return value

@dataclass(frozen=True)
class Signal:
    symbol: str
    side: str
    entry: Decimal
    tp: Decimal
    sl: Decimal
    quantity: Decimal | None = None


@dataclass(frozen=True)
class Rules:
    step: Decimal
    minimum: Decimal
    maximum: Decimal
    tick: Decimal
    min_notional: Decimal
    observed_at: float
    min_price: Decimal = D('0')
    max_price: Decimal = D('1000000000')
    multiplier_up: Decimal | None = None
    multiplier_down: Decimal | None = None
    mark_price: Decimal | None = None


def maximum_risk_quantity(entry,sl,rules):
    """Independent exact comparator only; never modifies the provider's Signal."""
    distance=abs(entry-sl)
    if distance<=0:raise Review('INVALID_ENTRY_TP_SL')
    # Integer floors on exact rationals avoid rounding a near-step quotient upward.
    steps=min(Fraction(RISK)//(Fraction(distance)*Fraction(rules.step)),
              Fraction(rules.maximum)//Fraction(rules.step))
    expected=D(steps)*rules.step
    if expected<=0 or expected<rules.minimum or expected*entry<rules.min_notional:
        raise Review('NO_LEGAL_MAX_RISK_QUANTITY')
    return expected


def risk_check(signal, rules):
    if not 0 <= time.time()-rules.observed_at <= 300: raise Review('NEEDS_REVIEW: filter pasar kedaluwarsa')
    for v in (rules.step,rules.minimum,rules.maximum,rules.tick): number(v)
    if rules.min_notional < 0: raise Review('INVALID_RULES')
    if signal.side not in ('LONG','SHORT'): raise Review('INVALID_SIDE')
    for v in (signal.entry,signal.tp,signal.sl):number(v)
    if signal.side=='LONG': valid=signal.sl<signal.entry<signal.tp
    else: valid=signal.tp<signal.entry<signal.sl
    if not valid: raise Review('NEEDS_REVIEW: susunan ENTRY/TP/SL salah')
    distance=abs(signal.entry-signal.sl)
    if abs(signal.tp-signal.entry) < 2*distance:
        raise Review('NEEDS_REVIEW: reward/risk kurang dari 1:2')
    for price in (signal.entry,signal.tp,signal.sl):
        if not rules.min_price<=price<=rules.max_price or price % rules.tick:
            raise Review('NEEDS_REVIEW: harga tidak sesuai tick/rentang; level tidak diubah otomatis')
    qty=number(signal.quantity)
    if qty % rules.step or not rules.minimum<=qty<=rules.maximum:
        raise Review('REJECT_QUANTITY_PRECISION_OR_RANGE')
    if qty*signal.entry < rules.min_notional:
        raise Review('REJECT_MIN_NOTIONAL')
    if rules.multiplier_up is not None:
        if rules.mark_price is None or rules.multiplier_down is None:raise Review('INVALID_RULES')
        if not rules.mark_price*rules.multiplier_down<=signal.entry<=rules.mark_price*rules.multiplier_up:
            raise Review('REJECT_PERCENT_PRICE')
    risk=qty*distance
    if not D('0')<risk<=RISK: raise Review('NEEDS_REVIEW: risiko melampaui batas')
    if qty!=maximum_risk_quantity(signal.entry,signal.sl,rules):raise Review('POSITION_SIZE_NOT_MAX_RISK')
    return dict(symbol=signal.symbol,side=signal.side,entry=str(signal.entry),tp=str(signal.tp),sl=str(signal.sl),
                quantity=str(qty),risk=str(risk),margin_mode='CROSS',leverage=75,order_type='LIMIT',
                mode='DRY_RUN',rr=str(abs(signal.tp-signal.entry)/distance),rules_checked_at=rules.observed_at)


def preflight(plan, rules):
    if plan.get('mode')!='DRY_RUN': raise Review('LIVE_DISABLED: build ini tidak memiliki eksekutor live')
    if plan.get('margin_mode')!='CROSS' or plan.get('leverage')!=75 or plan.get('order_type')!='LIMIT':
        raise Review('NEEDS_REVIEW: konfigurasi order salah')
    sig=Signal(plan['symbol'],plan['side'],number(plan['entry']),number(plan['tp']),number(plan['sl']),number(plan['quantity']))
    if sig.side not in ('LONG','SHORT') or not re.fullmatch(r'[A-Z0-9]{2,18}USDT',sig.symbol):
        raise Review('NEEDS_REVIEW: symbol/side tidak valid')
    verified=risk_check(sig,rules)
    if D(verified['quantity'])!=D(plan['quantity']) or D(verified['risk'])!=D(plan['risk']):
        raise Review('NEEDS_REVIEW: ukuran/risk berubah sebelum submit')

class Ledger:
    def __init__(self,path):
        self.db=sqlite3.connect(path,timeout=15,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY, day TEXT NOT NULL, source TEXT NOT NULL,
          state TEXT NOT NULL, plan TEXT NOT NULL, exit_price TEXT, pnl TEXT, created TEXT NOT NULL, closed_day TEXT);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,at TEXT,state TEXT,agent TEXT,message TEXT);
        ''')
    def count(self,business_day=None):
        return self.db.execute('SELECT COUNT(*) FROM trades WHERE day=?',(business_day or day(),)).fetchone()[0]
    def event(self,state,agent,message):
        if state not in STATES: raise ValueError(state)
        self.db.execute('INSERT INTO events(at,state,agent,message) VALUES(?,?,?,?)',(now(),state,agent,message))
    def reserve(self,plan,source,rules):
        # One atomic transaction enforces the cap across restarts and concurrent workers.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            preflight(plan,rules)
            business_day=day()
            if self.count(business_day)>=2: raise Locked('LOCKED: dua slot trade hari ini sudah tercatat')
            key=hashlib.sha256((business_day+json.dumps({k:plan[k] for k in ('symbol','side','entry','tp','sl')},sort_keys=True)).encode()).hexdigest()[:24]
            self.db.execute('INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)',
                (key,business_day,source,'ORDER_READY',json.dumps(plan),None,None,now(),None))
            self.db.execute('COMMIT');return key
        except sqlite3.IntegrityError:
            self.db.execute('ROLLBACK');raise Review('NEEDS_REVIEW: signal duplikat')
        except BaseException:
            self.db.execute('ROLLBACK');raise
    def fill(self,trade_id):
        row=self.db.execute('SELECT * FROM trades WHERE id=?',(trade_id,)).fetchone()
        if not row or row['state']!='ORDER_READY': raise Review('NEEDS_REVIEW: fill tidak sesuai state')
        self.db.execute("UPDATE trades SET state='POSITION_OPEN' WHERE id=?",(trade_id,))
    def close(self,trade_id,exit_price):
        exit_price=number(exit_price)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute('SELECT * FROM trades WHERE id=?',(trade_id,)).fetchone()
            if not row or row['state']!='POSITION_OPEN': raise Review('NEEDS_REVIEW: posisi tidak terbuka')
            p=json.loads(row['plan']);pnl=(exit_price-D(p['entry']))*D(p['quantity'])*(1 if p['side']=='LONG' else -1)
            self.db.execute("UPDATE trades SET state='CLOSED',exit_price=?,pnl=?,closed_day=? WHERE id=?",(str(exit_price),str(pnl),day(),trade_id))
            self.db.execute('COMMIT');return str(pnl)
        except BaseException:
            self.db.execute('ROLLBACK');raise
    def snapshot(self,source,connected=False):
        rows=[dict(x) for x in self.db.execute('SELECT * FROM trades ORDER BY created')]
        for row in rows: row['plan']=json.loads(row['plan'])
        events=[dict(x) for x in self.db.execute('SELECT at,state,agent,message FROM events ORDER BY seq DESC LIMIT 100')][::-1]
        # Realized paper PNL is attributed to the closing day, in Asia/Bangkok.
        pnl=sum((D(r['pnl']) for r in rows if r['closed_day']==day() and r['pnl'] is not None),D('0'))
        return dict(schema_version=1,mode='DRY_RUN',source=source,generated_at=now(),timezone='Asia/Bangkok',
                    status=events[-1]['state'] if events else 'IDLE',locked=self.count()>=2,
                    api_connected=connected,live_enabled=False,balance=None,pnl_today=str(pnl),
                    trades_today=self.count(),active_positions=sum(r['state']=='POSITION_OPEN' for r in rows),
                    pending_orders=sum(r['state']=='ORDER_READY' for r in rows),trades=rows,events=events,
                    note='Semua order adalah paper trade. Balance akun tidak terhubung; fees/slippage tidak disimulasikan.')
