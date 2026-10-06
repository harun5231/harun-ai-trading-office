"""Fresh public market context for the single research-to-order-intent pipeline."""
import time
import re
from dataclasses import replace
from datetime import datetime
from .core import D,Review,number,RISK,validated_risk_target,validated_fee_rate,FEE_SOURCE

SIZING_CONTRACT='Target planned net loss at stop loss is 5 USDT including entry and stop-loss exit fees. The worker computes the largest legal Binance base-asset quantity such that quantity × (abs(limit_entry - stop_loss) + limit_entry × taker_fee_rate + stop_loss × taker_fee_rate) <= 5 USDT. Use the supplied per-symbol account taker commission for entry, SL exit, and TP exit. Net TP reward after entry and TP exit fees must be at least twice the fee-inclusive SL loss. Slippage, funding, and price gaps are outside this calculation. The provider determines entry, TP, SL, or HOLD; the worker alone sizes quantity.'

def account_fee_rules(rules,symbol,client):
    """Attach a fresh account commission GET, without assuming a fallback rate."""
    if client is None:raise Review('FEE_EVIDENCE_UNAVAILABLE')
    try:
        quote=client.commission_rate(symbol)
        if not isinstance(quote,dict) or quote.get('source')!=FEE_SOURCE or quote.get('symbol')!=symbol:raise ValueError
        rates={}
        for name in ('maker','taker'):
            value=quote[name]
            if not isinstance(value,str) or len(value)>64 or not re.fullmatch(r'\d+(?:\.\d+)?',value):raise ValueError
            rates[name]=D(value)
            if not rates[name].is_finite() or not 0<=rates[name]<1:raise ValueError
        observed=datetime.fromisoformat(quote['checked_at'])
        milliseconds=quote['checked_at_ms']
        if observed.tzinfo is None or type(milliseconds) is not int or milliseconds<=0:raise ValueError
        if abs(observed.timestamp()-milliseconds/1000)>.001:raise ValueError
        value=replace(rules,taker_fee_rate=rates['taker'],fee_observed_at=milliseconds/1000,
            fee_symbol=symbol,fee_source=FEE_SOURCE)
        validated_fee_rate(value,symbol)
        return value
    except Review:raise
    except Exception:raise Review('FEE_EVIDENCE_UNAVAILABLE') from None

def analysis_context(market,symbol,risk_target=RISK,account_client=None):
    risk_target=validated_risk_target(risk_target)
    data=market.data(symbol)
    rules=account_fee_rules(market.rules(symbol),symbol,account_client) # Before any paid analysis call.
    market.fresh(data,symbol)
    if not 0<=time.time()-rules.observed_at<=300:raise Review('STALE_CONTRACT_CONTEXT')
    for value in (rules.step,rules.minimum,rules.maximum,rules.tick):number(value)
    if any(not v.is_finite() or v<0 for v in (rules.min_price,rules.max_price,rules.min_notional)):raise Review('INVALID_CONTRACT_CONTEXT')
    contract={'source':'Binance Futures /fapi/v1/exchangeInfo','symbol':symbol,'observed_at':rules.observed_at,
        'quantity_unit':'base_asset','stepSize':format(rules.step,'f'),'minQty':format(rules.minimum,'f'),
        'maxQty':format(rules.maximum,'f'),'tickSize':format(rules.tick,'f'),'minPrice':format(rules.min_price,'f'),
        'maxPrice':format(rules.max_price,'f'),'minNotional':format(rules.min_notional,'f') if rules.min_notional>0 else None}
    if rules.multiplier_up is not None:
        if rules.multiplier_down is None or rules.mark_price is None:raise Review('INVALID_CONTRACT_CONTEXT')
        for value in (rules.multiplier_up,rules.multiplier_down,rules.mark_price):number(value)
        contract['percent_price']={'multiplierUp':str(rules.multiplier_up),'multiplierDown':str(rules.multiplier_down),
            'reference_mark_price':str(rules.mark_price),'min_entry_price':str(rules.mark_price*rules.multiplier_down),
            'max_entry_price':str(rules.mark_price*rules.multiplier_up)}
    return {**data,'contract_rules':contract,'risk_constraints':{'quantity_unit':'base_asset',
        'margin_mode_target':'CROSS','leverage_target':75,'maximum_loss_at_sl_usdt':str(risk_target),
        'minimum_actual_reward_risk':2,'reward_risk_basis':'NET_AFTER_ENTRY_AND_EXIT_FEES',
        'entry_fee_rate':format(rules.taker_fee_rate,'f'),'sl_exit_fee_rate':format(rules.taker_fee_rate,'f'),
        'tp_exit_fee_rate':format(rules.taker_fee_rate,'f'),'fee_source':rules.fee_source,
        'fee_symbol':rules.fee_symbol,'fee_observed_at':rules.fee_observed_at,
        'excluded_costs':['SLIPPAGE','FUNDING','GAPS'],
        'target_loss_at_sl_usdt':str(risk_target),'position_sizing_contract':SIZING_CONTRACT.replace('5 USDT',str(risk_target)+' USDT')}},rules
