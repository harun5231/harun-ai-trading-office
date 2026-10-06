"""Fresh public market context for the single research-to-order-intent pipeline."""
import time
from .core import Review,number,RISK,validated_risk_target

SIZING_CONTRACT='Target loss at stop loss is 5 USDT. The worker Risk Manager independently computes the largest valid Binance base-asset quantity conforming to stepSize/minQty/maxQty such that quantity × abs(limit_entry - stop_loss) <= 5 USDT. The resulting risk should be as close as possible to 5 USDT without exceeding it.'

def analysis_context(market,symbol,risk_target=RISK):
    risk_target=validated_risk_target(risk_target)
    data=market.data(symbol)
    rules=market.rules(symbol) # Must complete before any paid analysis call.
    market.fresh(data,symbol)
    if not 0<=time.time()-rules.observed_at<=300:raise Review('STALE_CONTRACT_CONTEXT')
    for value in (rules.step,rules.minimum,rules.maximum,rules.tick):number(value)
    if any(not v.is_finite() or v<0 for v in (rules.min_price,rules.max_price,rules.min_notional)):raise Review('INVALID_CONTRACT_CONTEXT')
    contract={'source':'Binance Futures /fapi/v1/exchangeInfo','symbol':symbol,'observed_at':rules.observed_at,
        'quantity_unit':'base_asset','stepSize':str(rules.step),'minQty':str(rules.minimum),
        'maxQty':str(rules.maximum),'tickSize':str(rules.tick),'minPrice':str(rules.min_price),
        'maxPrice':str(rules.max_price),'minNotional':str(rules.min_notional) if rules.min_notional>0 else None}
    if rules.multiplier_up is not None:
        if rules.multiplier_down is None or rules.mark_price is None:raise Review('INVALID_CONTRACT_CONTEXT')
        for value in (rules.multiplier_up,rules.multiplier_down,rules.mark_price):number(value)
        contract['percent_price']={'multiplierUp':str(rules.multiplier_up),'multiplierDown':str(rules.multiplier_down),
            'reference_mark_price':str(rules.mark_price),'min_entry_price':str(rules.mark_price*rules.multiplier_down),
            'max_entry_price':str(rules.mark_price*rules.multiplier_up)}
    return {**data,'contract_rules':contract,'risk_constraints':{'quantity_unit':'base_asset',
        'margin_mode_target':'CROSS','leverage_target':75,'maximum_loss_at_sl_usdt':str(risk_target),
        'minimum_actual_reward_risk':2,'target_loss_at_sl_usdt':str(risk_target),'position_sizing_contract':SIZING_CONTRACT.replace('5 USDT',str(risk_target)+' USDT')}},rules
