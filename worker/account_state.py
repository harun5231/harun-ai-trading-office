"""Read-only account observations and completion ordering in one worker process."""
from datetime import datetime,timezone
from itertools import count
from uuid import uuid4
import re
import json
from .core import D,Review,now
from .neuroapi import SCREEN_SCHEMA,SCREEN_ONE_SCHEMA
from .prompts import SCREENING

SCREENING_ONE='pilihkan 1 coin yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance'
MANUAL_ONLY_SYMBOLS=frozenset(('HYPEUSDT',))
MAX_REPLACEMENTS=3
ACCOUNT_FIELDS=('running_positions','running_symbols','open_entry_symbols','available_slots','manual_exposure',
    'usdt_wallet_balance','usdt_available_balance','bot_entries_today')
ACCOUNT_GENERATION=uuid4().hex
ACCOUNT_REVISION=count(1)
ACCOUNT_ORDER_FIELDS=('_account_generation','_account_revision')

def empty_account():
    return dict(running_positions=None,running_symbols=None,open_entry_symbols=None,available_slots=None,manual_exposure=[],
        usdt_wallet_balance=None,usdt_available_balance=None,account_checked_at=None,account_failure_code=None,
        _account_generation=None,_account_revision=None)

def account_order():
    # Observation completion order is shared by the daemon's account/actor
    # threads. UTC remains a displayed time, not the ordering mechanism; a new
    # process generation also supersedes persisted observations after restart.
    return dict(_account_generation=ACCOUNT_GENERATION,_account_revision=next(ACCOUNT_REVISION))

def current_account_revision(value):
    revision=value.get('_account_revision')
    return revision if value.get('_account_generation')==ACCOUNT_GENERATION and type(revision) is int and revision>0 else None

def account_timestamp(value):
    try:
        value=datetime.fromisoformat(value)
        if value.tzinfo is None:return None
        return value.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (TypeError,ValueError):return None

def account_observation(account,reason=None,stamp=False):
    value=empty_account()
    if account:value.update({k:account[k] for k in ACCOUNT_FIELDS if k in account})
    checked=account_timestamp(account.get('account_checked_at')) if account else None
    if account:value.update({k:account[k] for k in ACCOUNT_ORDER_FIELDS if k in account})
    if stamp and checked is None:value.update(account_order())
    value.update(account_checked_at=checked or (account_timestamp(now()) if stamp else None),
        account_failure_code=reason if reason is not None else (account.get('account_failure_code') if account else None))
    return value

def newer_account(observation,previous):
    revision=current_account_revision(observation);older=current_account_revision(previous)
    if revision is not None:return older is None or revision>=older
    if older is not None:return False
    # Legacy observations lack a source ordering token. Use their actual GET
    # timestamps only until this daemon publishes its first ordered observation.
    # Office accounts use checked_at; robot summaries use account_checked_at.
    # An explicit missing account_checked_at must never borrow bot checked_at.
    checked=account_timestamp(observation.get('account_checked_at' if 'account_checked_at' in observation else 'checked_at'))
    prior=account_timestamp(previous.get('account_checked_at' if 'account_checked_at' in previous else 'checked_at'))
    return prior is None or checked is not None and checked>=prior

def newer_account_json(observation,previous):
    return newer_account(json.loads(observation),json.loads(previous))

def screening_contract(count):
    if count==1:return SCREENING_ONE,SCREEN_ONE_SCHEMA
    if count==2:return SCREENING,SCREEN_SCHEMA
    raise Review('INVALID_SCREENING_COUNT')

def slots(running,entries=0,*,pending=0,unrepresented=0):
    # Carryover/manual positions consume concurrency; first fills consume that
    # calendar day's two-entry allowance even after their positions are closed.
    return max(0,min(2-running-unrepresented,2-entries-pending))

def open_entry_symbols(rows,*,algo=False,allow_hedge=False):
    """Validate global regular open orders; reducing exits reserve no new slot."""
    if not isinstance(rows,list):raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
    symbols=set()
    for row in rows:
        if not isinstance(row,dict) or type(row.get('reduceOnly')) is not bool:
            raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
        if row['reduceOnly']:continue
        if type(row.get('closePosition')) is not bool:raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
        if row['closePosition']:continue
        symbol=row.get('symbol')
        if (not isinstance(symbol,str) or not re.fullmatch(r'[A-Z0-9_]{2,30}',symbol)
                or row.get('positionSide') not in (('BOTH','LONG','SHORT') if allow_hedge else ('BOTH',)) or row.get('side') not in ('BUY','SELL')
                or row.get('algoStatus' if algo else 'status') not in (
                    ('NEW','TRIGGERING','TRIGGERED') if algo else ('NEW','PARTIALLY_FILLED','PENDING_CANCEL'))):
            raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
        symbols.add(symbol)
    return sorted(symbols)

def account_state(client,store,today):
    config=client.check()
    if config.get('status')!='BINANCE_CONNECTED' or config.get('position_mode')!='ONE_WAY' or config.get('multi_assets_margin') is not False or config.get('can_trade') is not True:
        raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
    client.sync_time()
    # Read potential entries first: a fill between these GETs appears in the
    # final positions snapshot and the union still reserves that symbol once.
    pending_symbols=sorted(set(open_entry_symbols(client.signed_get('/fapi/v1/openOrders')))|
        set(open_entry_symbols(client.signed_get('/fapi/v1/openAlgoOrders'),algo=True)))
    value=client.signed_get('/fapi/v3/account')
    rows=value.get('positions') if isinstance(value,dict) else None
    if not isinstance(rows,list):raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
    running=set()
    for row in rows:
        if not isinstance(row,dict):raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
        amount=row.get('positionAmt');symbol=row.get('symbol')
        if not isinstance(amount,str) or len(amount)>64 or not re.fullmatch(r'-?\d+(?:\.\d+)?',amount) or not isinstance(symbol,str) or not re.fullmatch(r'[A-Z0-9_]{2,30}',symbol):raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
        # Zero-amount rows are not active Futures exposure. Validate BOTH only
        # for non-zero positions that actually consume one of the two slots.
        if D(amount)==0:continue
        if row.get('positionSide')!='BOTH':raise Review('ROBOT_ACCOUNT_UNAVAILABLE')
        running.add(symbol)
    entries=store.entries(today)
    # A historical entry receipt does not identify a currently open position.
    # Until the gateway supplies current ownership evidence, every existing
    # exposure is externally managed and must remain protected from robot use.
    return dict(running_positions=len(running),running_symbols=sorted(running),manual_exposure=sorted(running),
        open_entry_symbols=pending_symbols,bot_entries_today=entries,
        available_slots=store.available_slots(len(running),sorted(running),today,pending_symbols=pending_symbols),
        usdt_wallet_balance=config.get('usdt_wallet_balance'),
        usdt_available_balance=config.get('usdt_available_balance'),account_checked_at=now(),account_failure_code=None,
        **account_order())
