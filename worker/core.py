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
REWARD_RISK_POLICY = 'NET_1_TO_2_NEAREST_TICK'
RISK_MODEL = 'FEE_SLIPPAGE_RISK_V3'
NORMALIZED_REWARD_RISK_POLICY = 'NET_1_TO_2_NORMALIZED_WITH_EXIT_RESERVE'
NORMALIZED_SIZING_METHOD = 'MAX_LOT_ENTRY_SL_TAKER_FEES_SLIPPAGE_V3'
EXIT_SLIPPAGE_RATE = D('0.005')
NORMALIZATION_VERSION = 'TP_NET_RR_NORMALIZATION_V1'
RESERVE_EXCLUDED_COSTS = ['FUNDING','GAPS_BEYOND_RESERVE','FEE_CHANGES_AFTER_OBSERVATION']
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

def validated_protection_working_type(value):
    if not isinstance(value,str) or value not in ('MARK_PRICE','CONTRACT_PRICE'):
        raise Review('INVALID_ORDER_CONTRACT')
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


def maximum_risk_quantity(entry,sl,rules,risk_target=RISK,*,check_fresh=True,risk_model=None,side=None):
    """Largest legal lot within the explicitly selected planned SL cost model."""
    target=Fraction(validated_risk_target(risk_target))
    entry,sl=(Fraction(number(value)) for value in (entry,sl))
    for value in (rules.step,rules.minimum,rules.maximum):number(value)
    if not isinstance(rules.min_notional,D) or not rules.min_notional.is_finite() or rules.min_notional<0:
        raise Review('INVALID_RULES')
    rate=Fraction(validated_fee_rate(rules,check_fresh=check_fresh))
    distance=abs(entry-sl)
    if distance<=0:raise Review('INVALID_ENTRY_TP_SL')
    if risk_model is None:loss=distance+(entry+sl)*rate
    else:
        _require_risk_model(risk_model)
        loss=_reserve_unit_loss(entry,sl,side,rate)
    step=Fraction(rules.step)
    # Fee products and lot floors stay exact even under a caller's low precision.
    steps=min(target//(loss*step),Fraction(rules.maximum)//step)
    expected=steps*step
    if expected<=0 or expected<Fraction(rules.minimum) or expected*entry<Fraction(rules.min_notional):
        raise Review('NO_LEGAL_MAX_RISK_QUANTITY')
    return _exact_decimal(expected)


def risk_costs(entry,tp,sl,quantity,rules,*,symbol=None,check_fresh=True,risk_model=None,side=None):
    """Exact planned costs; only the versioned model includes exit reserves.

    The absent-marker archival model retains its original fee-only arithmetic.
    Neither model treats a planned trigger as proof of an actual exit fill.
    """
    if risk_model is not None:
        _require_risk_model(risk_model)
        return _reserve_risk_costs(entry,tp,sl,quantity,rules,side,symbol=symbol,check_fresh=check_fresh)
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


def _require_risk_model(value):
    if not isinstance(value,str) or value!=RISK_MODEL:raise Review('INVALID_COST_MODEL')
    return value


def validated_exit_slippage_rate(value=EXIT_SLIPPAGE_RATE):
    value=number(value)
    if value!=EXIT_SLIPPAGE_RATE:raise Review('INVALID_COST_MODEL')
    return value


def _adverse_exit_price(trigger,side):
    if side not in ('LONG','SHORT'):raise Review('INVALID_SIDE')
    factor=1-Fraction(EXIT_SLIPPAGE_RATE) if side=='LONG' else 1+Fraction(EXIT_SLIPPAGE_RATE)
    return Fraction(trigger)*factor


def _reserve_unit_loss(entry,sl,side,fee):
    if side not in ('LONG','SHORT'):raise Review('INVALID_SIDE')
    if not (sl<entry if side=='LONG' else sl>entry):raise Review('INVALID_ENTRY_TP_SL')
    adverse=_adverse_exit_price(sl,side)
    return abs(entry-sl)+sl*Fraction(EXIT_SLIPPAGE_RATE)+fee*(entry+adverse)


def normalized_reward_risk_tp(entry,stop_loss,side,rules,*,symbol=None,check_fresh=False):
    """First legal trigger whose reserved adverse TP fill yields planned net 1:2."""
    e,sl,tick=(Fraction(number(value)) for value in (entry,stop_loss,rules.tick))
    fee=Fraction(validated_fee_rate(rules,symbol,check_fresh=check_fresh))
    reserve=Fraction(EXIT_SLIPPAGE_RATE)
    loss=_reserve_unit_loss(e,sl,side,fee)
    target=(e*(1+fee)+2*loss)/((1-reserve)*(1-fee)) if side=='LONG' else (e*(1-fee)-2*loss)/((1+reserve)*(1+fee))
    steps=target/tick
    count=-(-steps.numerator//steps.denominator) if side=='LONG' else steps.numerator//steps.denominator
    price=count*tick
    if price<=0 or not (price>e if side=='LONG' else price<e):raise Review('INVALID_ENTRY_TP_SL')
    return _exact_decimal(price)


def _reserve_risk_costs(entry,tp,sl,quantity,rules,side,*,symbol=None,check_fresh=True):
    e,t,s,q=(Fraction(number(value)) for value in (entry,tp,sl,quantity))
    fee=Fraction(validated_fee_rate(rules,symbol,check_fresh=check_fresh))
    loss=_reserve_unit_loss(e,s,side,fee)
    if not (s<e<t if side=='LONG' else t<e<s):raise Review('INVALID_ENTRY_TP_SL')
    adverse_sl=_adverse_exit_price(s,side);adverse_tp=_adverse_exit_price(t,side)
    reserve=Fraction(EXIT_SLIPPAGE_RATE)
    distance=abs(e-s);reward=abs(t-e)
    gross_risk=q*distance;gross_reward=q*reward
    entry_fee=q*e*fee;sl_fee=q*adverse_sl*fee;tp_fee=q*adverse_tp*fee
    sl_slippage=q*s*reserve;tp_slippage=q*t*reserve
    total=q*loss;net_reward=gross_reward-tp_slippage-entry_fee-tp_fee
    if net_reward<2*total:raise Review('NET_RISK_REWARD_BELOW_2')
    costs=dict(risk=_exact_text(total),total_risk=_exact_text(total),
        gross_risk=_exact_text(gross_risk),entry_fee_usdt=_exact_text(entry_fee),
        sl_exit_fee_usdt=_exact_text(sl_fee),tp_exit_fee_usdt=_exact_text(tp_fee),
        gross_reward=_exact_text(gross_reward),net_reward=_exact_text(net_reward),
        rr=str(_ratio_decimal(reward/distance)),net_rr=str(_ratio_decimal(net_reward/total)),
        entry_fee_rate=format(rules.taker_fee_rate,'f'),exit_fee_rate=format(rules.taker_fee_rate,'f'),
        tp_fee_rate=format(rules.taker_fee_rate,'f'),fee_symbol=rules.fee_symbol,fee_source=rules.fee_source,
        fee_observed_at=rules.fee_observed_at,sizing_method=NORMALIZED_SIZING_METHOD,
        excluded_costs=list(RESERVE_EXCLUDED_COSTS),risk_model=RISK_MODEL,
        exit_slippage_rate=format(EXIT_SLIPPAGE_RATE,'f'),entry_slippage_rate='0',
        sl_slippage_usdt=_exact_text(sl_slippage),tp_slippage_usdt=_exact_text(tp_slippage),
        sl_execution_price=_exact_text(adverse_sl),tp_execution_price=_exact_text(adverse_tp))
    costs['cost_evidence']=dict(version=RISK_MODEL,fee_source=rules.fee_source,fee_symbol=rules.fee_symbol,
        fee_observed_at=rules.fee_observed_at,taker_fee_rate=format(rules.taker_fee_rate,'f'),
        exit_slippage_rate=format(EXIT_SLIPPAGE_RATE,'f'),entry_slippage_rate='0',
        reserve_source='CONFIGURED_ADVERSE_EXIT_RATE',excluded_costs=list(RESERVE_EXCLUDED_COSTS))
    return costs


def _tp_normalization(signal,tp,rules):
    distance=abs(Fraction(signal.entry)-Fraction(signal.sl))
    if distance<=0:raise Review('INVALID_ENTRY_TP_SL')
    model_rr=abs(Fraction(signal.tp)-Fraction(signal.entry))/distance
    return dict(version=NORMALIZATION_VERSION,source='WORKER_DERIVED',
        model_entry=format(signal.entry,'f'),model_tp=format(signal.tp,'f'),model_sl=format(signal.sl,'f'),
        model_gross_rr=str(_ratio_decimal(model_rr)),execution_tp=format(tp,'f'),
        tick_size=format(rules.tick,'f'),risk_model=RISK_MODEL,
        exit_slippage_rate=format(EXIT_SLIPPAGE_RATE,'f'),taker_fee_rate=format(rules.taker_fee_rate,'f'))


def _validate_normalized_signal(signal,rules,*,check_fresh=True):
    if type(rules.observed_at) not in (int,float) or not math.isfinite(rules.observed_at) or rules.observed_at<0:
        raise Review('INVALID_RULES')
    if check_fresh and not 0<=time.time()-rules.observed_at<=300:raise Review('NEEDS_REVIEW: filter pasar kedaluwarsa')
    for value in (rules.step,rules.minimum,rules.maximum,rules.tick):number(value)
    if not isinstance(rules.min_notional,D) or not rules.min_notional.is_finite() or rules.min_notional<0:raise Review('INVALID_RULES')
    if any(not isinstance(value,D) or not value.is_finite() or value<0 for value in (rules.min_price,rules.max_price)) or rules.min_price>rules.max_price:
        raise Review('INVALID_RULES')
    if signal.side not in ('LONG','SHORT') or not re.fullmatch(r'[A-Z0-9]{2,18}USDT',signal.symbol):raise Review('INVALID_SIDE')
    for value in (signal.entry,signal.tp,signal.sl):number(value)
    if not (signal.sl<signal.entry<signal.tp if signal.side=='LONG' else signal.tp<signal.entry<signal.sl):raise Review('INVALID_ENTRY_TP_SL')
    distance=abs(Fraction(signal.entry)-Fraction(signal.sl))
    if abs(Fraction(signal.tp)-Fraction(signal.entry))<2*distance:raise Review('RISK_REWARD_BELOW_2')
    # The original model TP is retained for audit; only entry/SL execute directly.
    for price in (signal.entry,signal.sl):
        if not rules.min_price<=price<=rules.max_price or (Fraction(price)/Fraction(rules.tick)).denominator!=1:
            raise Review('NEEDS_REVIEW: harga tidak sesuai tick/rentang')
    validated_fee_rate(rules,signal.symbol,check_fresh=check_fresh)
    if rules.multiplier_up is not None:
        if rules.mark_price is None or rules.multiplier_down is None:raise Review('INVALID_RULES')
        for value in (rules.multiplier_up,rules.multiplier_down,rules.mark_price):number(value)
        if not Fraction(rules.mark_price)*Fraction(rules.multiplier_down)<=Fraction(signal.entry)<=Fraction(rules.mark_price)*Fraction(rules.multiplier_up):
            raise Review('REJECT_PERCENT_PRICE')


def _normalized_plan(signal,rules,risk_target,*,check_fresh=True):
    _validate_normalized_signal(signal,rules,check_fresh=check_fresh)
    target=validated_risk_target(risk_target)
    tp=normalized_reward_risk_tp(signal.entry,signal.sl,signal.side,rules,symbol=signal.symbol,check_fresh=check_fresh)
    if not rules.min_price<=tp<=rules.max_price:raise Review('NO_LEGAL_NET_RR_TP')
    qty=maximum_risk_quantity(signal.entry,signal.sl,rules,target,check_fresh=check_fresh,risk_model=RISK_MODEL,side=signal.side)
    costs=risk_costs(signal.entry,tp,signal.sl,qty,rules,symbol=signal.symbol,check_fresh=check_fresh,risk_model=RISK_MODEL,side=signal.side)
    if not Fraction(0)<Fraction(D(costs['risk']))<=Fraction(target):raise Review('NEEDS_REVIEW: risiko melampaui batas')
    return dict(symbol=signal.symbol,side=signal.side,entry=format(signal.entry,'f'),tp=format(tp,'f'),sl=format(signal.sl,'f'),
        quantity=format(qty,'f'),execution_quantity=format(qty,'f'),margin_mode='CROSS',leverage=75,order_type='LIMIT',
        mode='ORDER_INTENT',risk_target_usdt=format(target,'f'),rules_checked_at=rules.observed_at,
        reward_risk_policy=NORMALIZED_REWARD_RISK_POLICY,tp_normalization=_tp_normalization(signal,tp,rules),**costs)


def validate_normalized_plan(plan,rules,*,check_fresh=False):
    """Recompute a versioned execution plan from preserved original model levels."""
    try:
        _require_risk_model(plan['risk_model'])
        if plan['reward_risk_policy']!=NORMALIZED_REWARD_RISK_POLICY:raise Review('INVALID_RISK_REWARD')
        validated_exit_slippage_rate(plan['exit_slippage_rate'])
        if plan['entry_slippage_rate']!='0':raise Review('INVALID_COST_MODEL')
        original=plan['tp_normalization']
        signal=Signal(plan['symbol'],plan['side'],number(original['model_entry']),number(original['model_tp']),number(original['model_sl']))
        expected=_normalized_plan(signal,rules,plan['risk_target_usdt'],check_fresh=check_fresh)
        for key,value in expected.items():
            if key=='rules_checked_at' and check_fresh:continue
            if key in ('fee_observed_at','cost_evidence') and check_fresh:
                # Fresh quotes must keep the actual rate; original evidence remains bound below.
                if key=='fee_observed_at':continue
                value={**value,'fee_observed_at':plan['fee_observed_at']}
            if plan.get(key)!=value:raise Review('ORDER_COSTS_CHANGED')
        return True
    except (KeyError,TypeError,ValueError):raise Review('INVALID_COST_MODEL') from None


def target_reward_risk_tp(entry,stop_loss,side,rules,*,symbol=None):
    """Exact first legal tick reaching net 1:2; never change an analyzed level."""
    e,sl,tick=(Fraction(number(value)) for value in (entry,stop_loss,rules.tick))
    fee=Fraction(validated_fee_rate(rules,symbol,check_fresh=False))
    if side not in ('LONG','SHORT'):raise Review('INVALID_SIDE')
    if not (sl<e if side=='LONG' else sl>e):raise Review('INVALID_ENTRY_TP_SL')
    loss=abs(e-sl)+fee*(e+sl)
    target=(e*(1+fee)+2*loss)/(1-fee) if side=='LONG' else (e*(1-fee)-2*loss)/(1+fee)
    steps=target/tick
    count=-(-steps.numerator//steps.denominator) if side=='LONG' else steps.numerator//steps.denominator
    if count*tick<=0:raise Review('INVALID_ENTRY_TP_SL')
    return _exact_decimal(count*tick)

def validate_reward_risk_policy(plan,rules):
    if 'risk_model' in plan:return validate_normalized_plan(plan,rules)
    if 'reward_risk_policy' not in plan:return True
    if not isinstance(plan['reward_risk_policy'],str) or plan['reward_risk_policy']!=REWARD_RISK_POLICY:
        raise Review('INVALID_RISK_REWARD')
    e,tp,sl=(Fraction(number(plan[key])) for key in ('entry','tp','sl'))
    fee=Fraction(validated_fee_rate(rules,plan['symbol'],check_fresh=False))
    target=target_reward_risk_tp(plan['entry'],plan['sl'],plan['side'],rules,symbol=plan['symbol'])
    reward=(tp-e if plan['side']=='LONG' else e-tp)-fee*(e+tp)
    loss=abs(e-sl)+fee*(e+sl)
    if reward<2*loss:raise Review('NET_RISK_REWARD_BELOW_2')
    if tp!=Fraction(target):raise Review('NET_RISK_REWARD_NOT_TARGET_2')
    return True

def risk_check(signal, rules, risk_target=RISK,*,reward_risk_policy=None,risk_model=None):
    if risk_model is not None:
        _require_risk_model(risk_model)
        if reward_risk_policy not in (None,NORMALIZED_REWARD_RISK_POLICY):raise Review('INVALID_RISK_REWARD')
        return _normalized_plan(signal,rules,risk_target)
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
    plan=dict(symbol=signal.symbol,side=signal.side,entry=format(signal.entry,'f'),tp=format(signal.tp,'f'),sl=format(signal.sl,'f'),
                quantity=format(qty,'f'),execution_quantity=format(qty,'f'),margin_mode='CROSS',leverage=75,order_type='LIMIT',
                mode='ORDER_INTENT',risk_target_usdt=format(risk_target,'f'),rules_checked_at=rules.observed_at,**costs)
    if reward_risk_policy is not None:
        plan['reward_risk_policy']=reward_risk_policy
        validate_reward_risk_policy(plan,rules)
    return plan


def preflight(plan, rules, risk_target=RISK):
    if not isinstance(plan,dict) or plan.get('mode')!='ORDER_INTENT': raise Review('INVALID_ORDER_CONTRACT')
    validated_protection_working_type(plan.get('protection_working_type','MARK_PRICE'))
    if 'risk_model' in plan:
        if validated_risk_target(plan['risk_target_usdt'])!=validated_risk_target(risk_target):raise Review('ORDER_COSTS_CHANGED')
        validate_normalized_plan(plan,rules,check_fresh=True)
        observed=plan.get('fee_observed_at')
        if type(observed) not in (int,float) or not math.isfinite(observed) or not 0<=time.time()-observed<=300:raise Review('STALE_FEE_EVIDENCE')
        return
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
    validate_reward_risk_policy(plan,rules)

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
