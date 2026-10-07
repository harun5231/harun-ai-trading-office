"""Real Binance account and transaction reads; no orders, simulation or ownership inference.

Financial reports use Binance income in its recorded asset. Filled transactions
are not closed positions. Symbol discovery is bounded and cannot establish a
complete account-wide fill list, which is stated explicitly in the result.
"""
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from .binance_private import BinanceCheckError, CODES
from .account_state import account_order, open_entry_symbols

DAY_MS=86_400_000
PAGE_SIZE=1000
MAX_INCOME_PAGES=4
MAX_TRADE_PAGES=3
MAX_HISTORY_SYMBOLS=12
HISTORY_BUDGET_SECONDS=60
SYMBOL=re.compile(r'[A-Z0-9_]{3,30}')
NUMBER=re.compile(r'-?\d+(?:\.\d+)?')
BANGKOK=timezone(timedelta(hours=7))


def _iso(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')


def _number(value,*,positive=False,nonnegative=False):
    if not isinstance(value,str) or len(value)>64 or not NUMBER.fullmatch(value):raise ValueError()
    number=Decimal(value)
    if positive and number<=0 or nonnegative and number<0:raise ValueError()
    return number


def _symbol(value):
    if not isinstance(value,str) or not SYMBOL.fullmatch(value):raise ValueError()
    return value


def _integer(value):
    if type(value) is not int or not 0<=value<2**63:raise ValueError()
    return value


def _reason(error):
    return str(error) if isinstance(error,BinanceCheckError) and str(error) in CODES else 'BINANCE_ACCOUNT_UNAVAILABLE'


def _read(client,path,**options):
    try:return client.signed_get(path,**options)
    except BinanceCheckError as error:
        if str(error)!='BINANCE_CLOCK_ERROR':raise
    # A slow previous GET can exhaust the signing clock. Resync once, never retry
    # a mutation (there is no mutation interface), and retain the captured window.
    client.sync_time()
    return client.signed_get(path,**options)


def _account(account,config,risks,checked_at):
    if not isinstance(account,dict) or not isinstance(config,dict) or not isinstance(risks,list):raise ValueError()
    if any(type(config.get(k)) is not bool for k in ('canTrade','dualSidePosition','multiAssetsMargin')):raise ValueError()
    assets=account['assets']
    if not isinstance(assets,list) or any(not isinstance(a,dict) for a in assets):raise ValueError()
    usdt=[a for a in assets if a.get('asset')=='USDT']
    if len(usdt)!=1:raise ValueError()
    _number(usdt[0]['walletBalance']);_number(usdt[0]['availableBalance'])
    raw_positions=account['positions']
    if not isinstance(raw_positions,list):raise ValueError()
    account_positions={}
    for p in raw_positions:
        if not isinstance(p,dict):raise ValueError()
        symbol=_symbol(p['symbol']);side=p['positionSide'];amount=_number(p['positionAmt'])
        if side not in ('BOTH','LONG','SHORT'):raise ValueError()
        key=(symbol,side)
        if key in account_positions:raise ValueError()
        account_positions[key]=amount
    seen=set();positions=[];risk_positions={}
    for p in risks:
        if not isinstance(p,dict):raise ValueError()
        symbol=_symbol(p['symbol']);position_side=p['positionSide'];quantity=_number(p['positionAmt'])
        if position_side not in ('BOTH','LONG','SHORT'):raise ValueError()
        key=(symbol,position_side)
        if key in seen:raise ValueError()
        seen.add(key)
        if quantity==0:continue
        if position_side=='LONG' and quantity<0 or position_side=='SHORT' and quantity>0:raise ValueError()
        risk_positions[key]=quantity
        _number(p['entryPrice'],nonnegative=True);_number(p['unRealizedProfit'])
        row=dict(symbol=symbol,side='LONG' if quantity>0 else 'SHORT',position_side=position_side,
                 quantity=format(quantity.copy_abs(),'f'),signed_quantity=p['positionAmt'],entry_price=p['entryPrice'],
                 unrealized_pnl=p['unRealizedProfit'])
        if 'markPrice' in p:
            _number(p['markPrice'],positive=True);row['mark_price']=p['markPrice']
        positions.append(row)
    # Snapshot races are retried on the next refresh; never silently hide an exposure.
    if {k:v for k,v in account_positions.items() if v!=0}!=risk_positions:raise ValueError()
    positions.sort(key=lambda p:(p['symbol'],p['position_side']))
    return dict(status='CONNECTED',checked_at=checked_at,usdt_wallet_balance=usdt[0]['walletBalance'],
                usdt_available_balance=usdt[0]['availableBalance'],positions=positions,active_positions=len(positions),
                position_mode='HEDGE' if config['dualSidePosition'] else 'ONE_WAY',can_trade=config['canTrade'],
                multi_assets_margin=config['multiAssetsMargin'])


def _income(row):
    if not isinstance(row,dict):raise ValueError()
    at=_integer(row['time']);transaction=_integer(row['tranId'])
    kind=row['incomeType'];asset=row['asset'];symbol=row.get('symbol','')
    if not isinstance(kind,str) or not re.fullmatch(r'[A-Z_]{1,40}',kind):raise ValueError()
    if not isinstance(asset,str) or not re.fullmatch(r'[A-Z0-9]{1,16}',asset):raise ValueError()
    if symbol:_symbol(symbol)
    amount=_number(row['income'])
    return (kind,transaction),dict(time=at,symbol=symbol,income_type=kind,asset=asset,amount=amount)


def _fill(row,symbol):
    if not isinstance(row,dict) or _symbol(row['symbol'])!=symbol:raise ValueError()
    identity=_integer(row['id']);order=_integer(row['orderId']);at=_integer(row['time'])
    side=row['side'];position_side=row['positionSide'];asset=row['commissionAsset']
    if side not in ('BUY','SELL') or position_side not in ('BOTH','LONG','SHORT'):raise ValueError()
    if not isinstance(asset,str) or not re.fullmatch(r'[A-Z0-9]{1,16}',asset):raise ValueError()
    _number(row['qty'],positive=True);_number(row['price'],positive=True)
    _number(row['realizedPnl']);_number(row['commission'])
    return identity,at,dict(id=str(identity),symbol=symbol,order_id=str(order),side=side,position_side=position_side,
                            quantity=row['qty'],price=row['price'],realized_pnl=row['realizedPnl'],
                            commission=row['commission'],commission_asset=asset,time=_iso(at))


def _history(client,account,end_ms,checked_at):
    start_ms=max(0,end_ms-7*DAY_MS+1)
    today=datetime.fromtimestamp(end_ms/1000,BANGKOK).replace(hour=0,minute=0,second=0,microsecond=0)
    today_ms=int(today.timestamp())*1000
    deadline=time.monotonic()+HISTORY_BUDGET_SECONDS
    incomes={};income_complete=False;income_responded=False;reasons=[]
    for page in range(1,MAX_INCOME_PAGES+1):
        if time.monotonic()>=deadline:reasons.append('HISTORY_TIME_BUDGET');break
        try:
            rows=_read(client,'/fapi/v1/income',start_time=start_ms,end_time=end_ms,limit=PAGE_SIZE,page=page)
            if not isinstance(rows,list) or len(rows)>PAGE_SIZE:raise ValueError()
            parsed=[_income(row) for row in rows]
            if any(not start_ms<=row['time']<=end_ms for _,row in parsed):raise ValueError()
            page_rows={}
            for key,row in parsed:
                if key in page_rows and page_rows[key]!=row:raise ValueError()
                if key in incomes and incomes[key]!=row:raise ValueError()
                page_rows[key]=row
            # Repeating a full page is not evidence of forward pagination.
            new={key:row for key,row in page_rows.items() if key not in incomes}
            if rows and not new:raise ValueError()
            incomes.update(new);income_responded=True
            if len(rows)<PAGE_SIZE:income_complete=True;break
        except Exception as error:reasons.append(_reason(error));break
    if income_responded and not income_complete and not reasons:reasons.append('INCOME_PAGE_LIMIT')
    symbols={p['symbol'] for p in account['positions']}
    symbols.update(row['symbol'] for row in incomes.values() if row['symbol'])
    sorted_symbols=sorted(symbols)
    if len(sorted_symbols)>MAX_HISTORY_SYMBOLS:reasons.append('HISTORY_SYMBOL_LIMIT')
    selected=sorted_symbols[:MAX_HISTORY_SYMBOLS]
    fills={};fills_responded=False;fills_paginated=True;full_trade_window=False;symbols_checked=set()
    for symbol in selected:
        window_end=end_ms;symbol_complete=False
        for page in range(MAX_TRADE_PAGES):
            if time.monotonic()>=deadline:reasons.append('HISTORY_TIME_BUDGET');break
            try:
                rows=_read(client,'/fapi/v1/userTrades',symbol=symbol,limit=PAGE_SIZE,
                           start_time=start_ms,end_time=window_end)
                if not isinstance(rows,list) or len(rows)>PAGE_SIZE:raise ValueError()
                parsed=[_fill(row,symbol) for row in rows]
                ids=[identity for identity,_,_ in parsed]
                if len(set(ids))!=len(ids):raise ValueError()
                if any(not start_ms<=at<=window_end for _,at,_ in parsed):raise ValueError()
                for identity,at,row in parsed:
                    key=(symbol,identity)
                    if key in fills and fills[key]!=row:raise ValueError()
                    fills[key]=row
                fills_responded=True
                symbols_checked.add(symbol)
                if len(rows)<PAGE_SIZE:
                    symbol_complete=True;break
                # The first window returns the most recent fills. A forward ID
                # cursor would miss older ones. Move the upper time bound back,
                # while explicitly declaring the same-ms boundary unproven.
                full_trade_window=True
                reasons.append('TRADE_WINDOW_FULL_SAME_MS_UNPROVEN')
                window_end=min(at for _,at,_ in parsed)-1
                if window_end<start_ms:
                    symbol_complete=True;break
            except Exception as error:reasons.append(_reason(error));break
        if not symbol_complete:
            fills_paginated=False
            if not reasons or reasons[-1] not in ('HISTORY_TIME_BUDGET','BINANCE_ACCOUNT_UNAVAILABLE'):reasons.append('TRADE_PAGE_LIMIT')
    # Income/active-position symbols are evidence, not an exhaustive instrument
    # inventory. A zero-commission, zero-realized fill can be absent from income.
    reasons.append('SYMBOL_DISCOVERY_NOT_EXHAUSTIVE')
    items=sorted(fills.values(),key=lambda row:(row['time'],row['symbol'],int(row['id'])),reverse=True)
    history_status='PARTIAL' if fills_responded or income_responded else 'UNAVAILABLE'
    history=dict(status=history_status,checked_at=checked_at,period_start=_iso(start_ms),period_end=_iso(end_ms),
                 items=items,complete=False,kind='BINANCE_FILLS',symbol_scope='INCOME_AND_ACTIVE_POSITIONS',
                 symbols_checked=sorted(symbols_checked),symbols_selected=selected,
                 truncated=full_trade_window or not income_complete or not fills_paginated or len(sorted_symbols)>MAX_HISTORY_SYMBOLS,
                 incomplete_reasons=list(dict.fromkeys(reasons)))
    sums={'REALIZED_PNL':Decimal(0),'COMMISSION':Decimal(0),'FUNDING_FEE':Decimal(0)}
    other_assets=set()
    with localcontext() as precision:
        # Up to 4000 fixed-point 64-character values can span more than 128
        # significant digits when large integer amounts mix with tiny rebates.
        precision.prec=256
        if income_complete:
            for row in incomes.values():
                if today_ms<=row['time']<=end_ms and row['income_type'] in sums:
                    if row['asset']=='USDT':sums[row['income_type']]+=row['amount']
                    else:other_assets.add(row['asset'])
        pnl=sum(sums.values())
    reports=dict(status='PARTIAL' if income_responded else 'UNAVAILABLE',checked_at=checked_at,
                 period_start=_iso(today_ms),period_end=_iso(end_ms),pnl_today_usdt=None,realized_pnl_today_usdt=None,
                 commission_today_usdt=None,funding_today_usdt=None,trades_today=None,complete=False,
                 income_complete=income_complete,trade_count_complete=False,incomplete_reasons=history['incomplete_reasons'])
    if income_complete:
        reports.update(pnl_today_usdt=format(pnl,'f'),realized_pnl_today_usdt=format(sums['REALIZED_PNL'],'f'),
                       commission_today_usdt=format(sums['COMMISSION'],'f'),funding_today_usdt=format(sums['FUNDING_FEE'],'f'))
    if other_assets:reports['excluded_income_assets']=sorted(other_assets)
    return reports,history


def collect(client,include_history=True):
    """Read exchange facts once; callers cache this result independently of ROBOT ON.

    Account errors are sanitized exceptions so a failed read cannot overwrite the
    last usable balance/position snapshot with a fictional empty account.
    """
    try:
        if type(include_history) is not bool:raise ValueError()
        client.sync_time()
        # Potential entries precede the position reads so a concurrent fill is
        # still represented by their symbol union in capacity calculations.
        pending=set(open_entry_symbols(_read(client,'/fapi/v1/openOrders'),allow_hedge=True))
        pending.update(open_entry_symbols(_read(client,'/fapi/v1/openAlgoOrders'),algo=True,allow_hedge=True))
        raw_account=_read(client,'/fapi/v3/account')
        config=_read(client,'/fapi/v1/accountConfig')
        risks=_read(client,'/fapi/v3/positionRisk')
        completion_order=account_order()
        try:end_ms=client.server_time_ms()
        except BinanceCheckError as error:
            if str(error)!='BINANCE_CLOCK_ERROR':raise
            client.sync_time();end_ms=client.server_time_ms()
        checked_at=_iso(end_ms)
        account=_account(raw_account,config,risks,checked_at)
        account['open_entry_symbols']=sorted(pending)
        account.update(completion_order)
    except Exception as error:
        reason=_reason(error)
        raise BinanceCheckError(reason) from None
    result=dict(source='BINANCE_FUTURES',generated_at=checked_at,account=account)
    if include_history:
        reports,history=_history(client,account,end_ms,checked_at)
        result.update(reports=reports,position_history=history)
    return result
