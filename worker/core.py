"""Fail-closed USDⓈ-M order-intent risk checks and a durable audit connection."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, getcontext, DefaultContext, Context, ROUND_HALF_EVEN
from zoneinfo import ZoneInfo
from fractions import Fraction
import re
import sqlite3
import time

DefaultContext.prec = 160
getcontext().prec = 160
D = Decimal
TZ = ZoneInfo('Asia/Bangkok')
RISK = D('5')
class Review(Exception): pass

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


def validated_risk_target(value):
    if isinstance(value,D):
        if not value.is_finite() or abs(value.as_tuple().exponent)>64:raise Review('INVALID_RISK_TARGET')
        value=format(value,'f')
    value=number(value)
    if value>D("100"):raise Review("INVALID_RISK_TARGET")
    return value

def maximum_risk_quantity(entry,sl,rules,risk_target=RISK):
    """Deterministic execution sizing; never modifies provider levels or audit quantity."""
    risk_target=validated_risk_target(risk_target)
    distance=abs(entry-sl)
    if distance<=0:raise Review('INVALID_ENTRY_TP_SL')
    # Integer floors on exact rationals avoid rounding a near-step quotient upward.
    steps=min(Fraction(risk_target)//(Fraction(distance)*Fraction(rules.step)),
              Fraction(rules.maximum)//Fraction(rules.step))
    expected=D(steps)*rules.step
    if expected<=0 or expected<rules.minimum or expected*entry<rules.min_notional:
        raise Review('NO_LEGAL_MAX_RISK_QUANTITY')
    return expected


def risk_check(signal, rules, risk_target=RISK):
    risk_target=validated_risk_target(risk_target)
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
    qty=maximum_risk_quantity(signal.entry,signal.sl,rules,risk_target)
    if rules.multiplier_up is not None:
        if rules.mark_price is None or rules.multiplier_down is None:raise Review('INVALID_RULES')
        if not rules.mark_price*rules.multiplier_down<=signal.entry<=rules.mark_price*rules.multiplier_up:
            raise Review('REJECT_PERCENT_PRICE')
    risk=qty*distance
    if not D('0')<risk<=risk_target: raise Review('NEEDS_REVIEW: risiko melampaui batas')
    return dict(symbol=signal.symbol,side=signal.side,entry=str(signal.entry),tp=str(signal.tp),sl=str(signal.sl),
                quantity=str(qty),execution_quantity=str(qty),neurobro_position_size=str(signal.quantity) if signal.quantity is not None else None,risk=str(risk),margin_mode='CROSS',leverage=75,order_type='LIMIT',
                mode='ORDER_INTENT',rr=str(abs(signal.tp-signal.entry)/distance),rules_checked_at=rules.observed_at)


def preflight(plan, rules, risk_target=RISK):
    if not isinstance(plan,dict) or plan.get('mode')!='ORDER_INTENT': raise Review('INVALID_ORDER_CONTRACT')
    if plan.get('margin_mode')!='CROSS' or plan.get('leverage')!=75 or plan.get('order_type')!='LIMIT':
        raise Review('NEEDS_REVIEW: konfigurasi order salah')
    sig=Signal(plan['symbol'],plan['side'],number(plan['entry']),number(plan['tp']),number(plan['sl']),number(plan['quantity']))
    if sig.side not in ('LONG','SHORT') or not re.fullmatch(r'[A-Z0-9]{2,18}USDT',sig.symbol):
        raise Review('NEEDS_REVIEW: symbol/side tidak valid')
    if 'execution_quantity' in plan and D(plan['execution_quantity'])!=D(plan['quantity']):raise Review('INVALID_EXECUTION_QUANTITY')
    verified=risk_check(sig,rules,risk_target)
    if D(verified['quantity'])!=D(plan['quantity']) or D(verified['risk'])!=D(plan['risk']):
        raise Review('NEEDS_REVIEW: ukuran/risk berubah sebelum submit')

def verified_rr(plan):
    """Verify derived RR against exact levels without repricing or resizing.

    The risk producer uses Decimal precision 160. Accept its precisely rounded
    ratio or the exact rational value, independently of the caller's context.
    """
    try:
        entry,tp,sl=(Fraction(number(plan[k])) for k in ('entry','tp','sl'))
        distance=abs(entry-sl)
        if not distance:raise ValueError
        ratio=abs(tp-entry)/distance
        if ratio<2:raise ValueError
        value=plan['rr']
        if isinstance(value,bool) or not isinstance(value,(str,int,D)):raise ValueError
        text=str(value)
        if len(text)>256 or not re.fullmatch(r'\d+(?:\.\d+)?(?:E[+-]?\d{1,3})?',text):raise ValueError
        saved=D(text)
        if not saved.is_finite() or saved<=0 or abs(saved.as_tuple().exponent)>256:raise ValueError
        rounded=Context(prec=160,rounding=ROUND_HALF_EVEN).divide(D(ratio.numerator),D(ratio.denominator))
        if Fraction(saved)!=ratio and saved!=rounded:raise ValueError
        return ratio
    except Exception:raise Review('INVALID_RISK_REWARD') from None


class Ledger:
    """Thread-owned SQLite connection; producers create their own audit tables."""
    def __init__(self,path):
        self.db=sqlite3.connect(path,timeout=15,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        try:self.db.execute('PRAGMA journal_mode=WAL')
        except BaseException:
            self.db.close();raise
