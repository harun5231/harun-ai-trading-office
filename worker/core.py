"""Fail-closed USDⓈ-M order-intent risk checks and a durable audit connection."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, getcontext, DefaultContext, Context, ROUND_HALF_EVEN
from zoneinfo import ZoneInfo
from fractions import Fraction
import re
import sqlite3
import time
import math

DefaultContext.prec = 160
getcontext().prec = 160
D = Decimal
TZ = ZoneInfo('Asia/Bangkok')
RISK = D('5')
FEE_SOURCE = 'BINANCE_FUTURES_COMMISSION_RATE'
SIZING_METHOD = 'MAX_LOT_ENTRY_SL_TAKER_FEES_V2'
class Review(Exception): pass

def now(): return datetime.now(timezone.utc).isoformat()
def day(): return datetime.now(TZ).date().isoformat()
def number(value):
    if not isinstance(value, (str,int,Decimal)) or isinstance(value,bool):
        raise Review('NEEDS_REVIEW: nilai numerik harus eksplisit')
    if isinstance(value,D):
        if not value.is_finite():raise Review('NEEDS_REVIEW: angka harus positif')
        if abs(value.as_tuple().exponent)>64:raise Review('INVALID_NUMBER_SIZE')
        text=format(value,'f')
    else:text=str(value).strip()
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
    taker_fee_rate: Decimal | None = None
    fee_observed_at: float | None = None
    fee_symbol: str | None = None
    fee_source: str | None = None


def validated_risk_target(value):
    if isinstance(value,D):
        if not value.is_finite() or abs(value.as_tuple().exponent)>64:raise Review('INVALID_RISK_TARGET')
        value=format(value,'f')
    value=number(value)
    if value>D("100"):raise Review("INVALID_RISK_TARGET")
    return value

def _exact_decimal(value):
    """Convert finite decimal fractions exactly, independently of Decimal context."""
    value=Fraction(value);denominator=value.denominator;twos=fives=0
    while denominator%2==0:denominator//=2;twos+=1
    while denominator%5==0:denominator//=5;fives+=1
    if denominator!=1:raise Review('INVALID_COST_MODEL')
    places=max(twos,fives)
    coefficient=value.numerator*2**(places-twos)*5**(places-fives)
    digits=str(abs(coefficient)).rjust(places+1,'0')
    text=digits if not places else digits[:-places]+'.'+digits[-places:]
    return D(('-' if coefficient<0 else '')+text)


def _ratio_decimal(value):
    value=Fraction(value)
    return Context(prec=160,rounding=ROUND_HALF_EVEN).divide(D(value.numerator),D(value.denominator))


def _exact_text(value):return format(_exact_decimal(value),'f')


def validated_fee_rate(rules,symbol=None,*,check_fresh=True):
    """Require per-symbol account evidence; an absent fee is never assumed zero."""
    if any(getattr(rules,key,None) is None for key in
           ('taker_fee_rate','fee_observed_at','fee_symbol','fee_source')):
        raise Review('FEE_EVIDENCE_UNAVAILABLE')
    rate=rules.taker_fee_rate;observed=rules.fee_observed_at
    if (rules.fee_source!=FEE_SOURCE or not isinstance(rules.fee_symbol,str)
            or not re.fullmatch(r'[A-Z0-9]{2,18}USDT',rules.fee_symbol)
            or symbol is not None and rules.fee_symbol!=symbol
            or not isinstance(rate,D) or not rate.is_finite() or not 0<=rate<1
            or abs(rate.as_tuple().exponent)>64 or len(format(rate,'f'))>64
            or type(observed) not in (int,float) or not math.isfinite(observed) or observed<0):
        raise Review('INVALID_FEE_EVIDENCE')
    if check_fresh and not 0<=time.time()-observed<=300:raise Review('STALE_FEE_EVIDENCE')
    return rate


def maximum_risk_quantity(entry,sl,rules,risk_target=RISK,*,check_fresh=True):
    """Largest legal lot within planned SL loss plus entry and SL taker fees."""
    target=Fraction(validated_risk_target(risk_target))
    entry,sl=(Fraction(number(value)) for value in (entry,sl))
    for value in (rules.step,rules.minimum,rules.maximum):number(value)
    if not isinstance(rules.min_notional,D) or not rules.min_notional.is_finite() or rules.min_notional<0:
        raise Review('INVALID_RULES')
    rate=Fraction(validated_fee_rate(rules,check_fresh=check_fresh))
    distance=abs(entry-sl)
    if distance<=0:raise Review('INVALID_ENTRY_TP_SL')
    loss=distance+(entry+sl)*rate
    step=Fraction(rules.step)
    # Fee products and lot floors stay exact even under a caller's low precision.
    steps=min(target//(loss*step),Fraction(rules.maximum)//step)
    expected=steps*step
    if expected<=0 or expected<Fraction(rules.minimum) or expected*entry<Fraction(rules.min_notional):
        raise Review('NO_LEGAL_MAX_RISK_QUANTITY')
    return _exact_decimal(expected)


def risk_costs(entry,tp,sl,quantity,rules,*,symbol=None,check_fresh=True):
    """Exact planned price/fee model, with archival arithmetic available explicitly.

    This model includes entry and SL exit fees, and deducts entry and TP exit
    fees from reward. Slippage, funding and price gaps are outside the model.
    """
    entry,tp,sl,quantity=(Fraction(number(value)) for value in (entry,tp,sl,quantity))
    rate=Fraction(validated_fee_rate(rules,symbol,check_fresh=check_fresh))
    distance=abs(entry-sl);reward=abs(tp-entry)
    if distance<=0 or reward<2*distance:raise Review('INVALID_RISK_REWARD')
    gross_risk=quantity*distance;gross_reward=quantity*reward
    entry_fee=quantity*entry*rate;sl_fee=quantity*sl*rate;tp_fee=quantity*tp*rate
    total=gross_risk+entry_fee+sl_fee;net_reward=gross_reward-entry_fee-tp_fee
    if net_reward<2*total:raise Review('NET_RISK_REWARD_BELOW_2')
    return dict(risk=_exact_text(total),total_risk=_exact_text(total),
        gross_risk=_exact_text(gross_risk),entry_fee_usdt=_exact_text(entry_fee),
        sl_exit_fee_usdt=_exact_text(sl_fee),tp_exit_fee_usdt=_exact_text(tp_fee),
        gross_reward=_exact_text(gross_reward),net_reward=_exact_text(net_reward),
        rr=str(_ratio_decimal(reward/distance)),net_rr=str(_ratio_decimal(net_reward/total)),
        entry_fee_rate=format(rules.taker_fee_rate,'f'),exit_fee_rate=format(rules.taker_fee_rate,'f'),
        tp_fee_rate=format(rules.taker_fee_rate,'f'),fee_symbol=rules.fee_symbol,fee_source=rules.fee_source,
        fee_observed_at=rules.fee_observed_at,sizing_method=SIZING_METHOD,
        excluded_costs=['SLIPPAGE','FUNDING','GAPS'])


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
    distance=abs(Fraction(signal.entry)-Fraction(signal.sl))
    if abs(Fraction(signal.tp)-Fraction(signal.entry)) < 2*distance:
        raise Review('NEEDS_REVIEW: reward/risk kurang dari 1:2')
    for price in (signal.entry,signal.tp,signal.sl):
        if not rules.min_price<=price<=rules.max_price or (Fraction(price)/Fraction(rules.tick)).denominator!=1:
            raise Review('NEEDS_REVIEW: harga tidak sesuai tick/rentang; level tidak diubah otomatis')
    validated_fee_rate(rules,signal.symbol)
    qty=maximum_risk_quantity(signal.entry,signal.sl,rules,risk_target)
    if rules.multiplier_up is not None:
        if rules.mark_price is None or rules.multiplier_down is None:raise Review('INVALID_RULES')
        if not Fraction(rules.mark_price)*Fraction(rules.multiplier_down)<=Fraction(signal.entry)<=Fraction(rules.mark_price)*Fraction(rules.multiplier_up):
            raise Review('REJECT_PERCENT_PRICE')
    costs=risk_costs(signal.entry,signal.tp,signal.sl,qty,rules,symbol=signal.symbol)
    if not Fraction(0)<Fraction(D(costs['risk']))<=Fraction(risk_target):raise Review('NEEDS_REVIEW: risiko melampaui batas')
    return dict(symbol=signal.symbol,side=signal.side,entry=format(signal.entry,'f'),tp=format(signal.tp,'f'),sl=format(signal.sl,'f'),
                quantity=format(qty,'f'),execution_quantity=format(qty,'f'),margin_mode='CROSS',leverage=75,order_type='LIMIT',
                mode='ORDER_INTENT',risk_target_usdt=format(risk_target,'f'),rules_checked_at=rules.observed_at,**costs)


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
    # The current fee GET may have a newer observation time with unchanged rates.
    # The intent's own evidence must also still be fresh, never silently refreshed.
    intent_observed=plan.get('fee_observed_at')
    if type(intent_observed) not in (int,float) or not math.isfinite(intent_observed):raise Review('INVALID_FEE_EVIDENCE')
    if not 0<=time.time()-intent_observed<=300:raise Review('STALE_FEE_EVIDENCE')
    for key in (*risk_costs(sig.entry,sig.tp,sig.sl,D(plan['quantity']),rules,symbol=sig.symbol), 'risk_target_usdt'):
        if key!='fee_observed_at' and plan.get(key)!=verified[key]:raise Review('ORDER_COSTS_CHANGED')

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
