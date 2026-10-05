"""Shared pre-analysis contract context and bounded, paper-only analysis checks."""
import json
import time
from .core import Review,day,number,RISK,D,risk_check
from .neuroapi import SETUP_SCHEMA,selections,setup
from .prompts import ANALYSIS
from .diagnostics import safe_code,validation_code

SIZING_CONTRACT='Target loss at stop loss is 5 USDT. The worker Risk Manager independently computes the largest valid Binance base-asset quantity conforming to stepSize/minQty/maxQty such that quantity × abs(limit_entry - stop_loss) <= 5 USDT. The resulting risk should be as close as possible to 5 USDT without exceeding it.'

def analysis_context(market,symbol):
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
        'margin_mode_target':'CROSS','leverage_target':75,'maximum_loss_at_sl_usdt':str(RISK),
        'minimum_actual_reward_risk':2,'target_loss_at_sl_usdt':str(RISK),'position_sizing_contract':SIZING_CONTRACT}},rules

def analysis_once(ledger,client,market,symbols):
    # Validate all supplied symbols before any analysis. No screening or substitutions.
    catalog=market.catalog();selections({'symbols':symbols},catalog)
    today=day()
    ledger.db.execute('CREATE TABLE IF NOT EXISTS analysis_checks(operation TEXT PRIMARY KEY,day TEXT,state TEXT,result TEXT)')
    results=[]
    for symbol in symbols:
        operation=today+':analysis-v5:'+symbol
        ledger.db.execute('BEGIN IMMEDIATE')
        try:
            old=ledger.db.execute('SELECT state,result FROM analysis_checks WHERE operation=?',(operation,)).fetchone()
            if old:
                ledger.db.execute('COMMIT')
                # No network/replay for completed or interrupted checks.
                results.append(json.loads(old['result']) if old['state']=='COMPLETE' and old['result'] else empty_result(symbol,'ANALYSIS_CHECK_NEEDS_REVIEW'))
                continue
            if day()!=today:raise Review('CYCLE_DAY_CHANGED')
            if ledger.db.execute('SELECT COUNT(*) FROM analysis_checks WHERE day=? AND operation LIKE ?',(today,today+':analysis-v5:%')).fetchone()[0]>=2:raise Review('ANALYSIS_CHECK_DAILY_LIMIT')
            ledger.db.execute('INSERT INTO analysis_checks VALUES(?,?,?,?)',(operation,today,'PENDING',None))
            ledger.db.execute('COMMIT')
        except BaseException:ledger.db.execute('ROLLBACK');raise
        result=empty_result(symbol,None)
        try:
            context,_=analysis_context(market,symbol)
            value=client.ask(operation,ANALYSIS,SETUP_SCHEMA,lambda v:setup(v,symbol),context)
            signal=setup(value,symbol)
            if signal.side=='HOLD':
                result.update(status='HOLD',side='HOLD')
            else:
                result.update(side=signal.side,neurobro_position_size=str(signal.quantity) if signal.quantity is not None else None,
                    entry=str(signal.entry),TP=str(signal.tp),SL=str(signal.sl),
                    actual_RR=str(abs(signal.tp-signal.entry)/abs(signal.entry-signal.sl)))
                market.fresh(context,symbol)
                rules=market.rules(symbol)
                try:plan=risk_check(signal,rules)
                except Review as error:
                    client.record_validation(operation,error);raise
                result.update(status='ACCEPT',position_size=plan['quantity'],execution_quantity=plan['quantity'],calculated_risk=plan['risk'])
        except Exception as error:
            row=ledger.db.execute('SELECT failure_code FROM api_requests WHERE operation=?',(operation,)).fetchone()
            result['failure_code']=safe_code(row[0]) if row and row[0] else validation_code(error)
        ledger.db.execute("UPDATE analysis_checks SET state='COMPLETE',result=? WHERE operation=?",(json.dumps(result),operation))
        results.append(result)
    return results

def empty_result(symbol,reason):
    return dict(status='REJECT',symbol=symbol,side=None,position_size=None,execution_quantity=None,neurobro_position_size=None,entry=None,TP=None,SL=None,
        calculated_risk=None,actual_RR=None,failure_code=reason,mode='DRY_RUN')
