# Phase 1: shadow preflight only

Verified 2026-10-05 against Binance's current USD-M REST reference and official
Python SDK `trade_api.py`. No SDK execution code/dependency is installed.

Sources:
- https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade
- https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account
- https://github.com/binance/binance-connector-python/blob/master/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/api/trade_api.py
- https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/change-log

## Intended contracts (inert data, never submitted)

Entry: `/fapi/v1/order`, LIMIT/GTC, BUY for LONG or SELL for SHORT, BOTH for
One-way, exact persisted Risk Manager execution quantity and provider entry.

TP/SL: `/fapi/v1/algoOrder`, algoType CONDITIONAL, TAKE_PROFIT_MARKET / STOP_MARKET,
triggerPrice equal to provider TP/SL, opposite side, BOTH, workingType MARK_PRICE.
Conditional orders migrated to Algo Service; never send them to legacy entry
order contracts. Close-All uses closePosition=true and omits both quantity and
reduceOnly (these cannot be combined with Close-All). This is not exchange-native
OCO; future code must reconcile flat exposure and clear the remaining sibling.

These contracts are inert JSON only. The signed transport cannot reach the entry
or algo-order paths, even with GET, and has no mutation method argument. There is
no live switch or submission function. Mainnet authentication is only used for GET.

## Executed reads

Existing time, account V3 and accountConfig reads remain. Added signed GET reads:
`/fapi/v1/symbolConfig`, `/fapi/v3/positionRisk`, `/fapi/v1/openOrders`,
`/fapi/v1/openAlgoOrders`, `/fapi/v1/leverageBracket`. Symbol input is strictly
validated; only the two open-order endpoints may omit symbol for account-wide
checks. Public exchangeInfo/mark-price GETs refresh contract checks. No paid
NeuroAPI request or candlestick/analysis regeneration occurs.

Reject Hedge Mode, disabled canTrade, multi-asset mode, ambiguous responses,
existing exposure/orders (including other symbols), unsupported 75x bracket,
insufficient estimated initial margin, changed provider levels, incompatible
rules/quantity/risk/RR, crossed TP/SL, stale day, duplicate symbols/identities, or
more than two daily slots. Required margin/leverage changes are listed but never
performed: such plans remain SHADOW_PLAN_READY / NEEDS_REVIEW, not preflight OK.
The margin estimate excludes fees; shadow OK is not permission to submit later.
All checks must be repeated immediately before any separately reviewed live phase.

## Persisted setup requirements

`python -m worker binance-shadow` only reads current Bangkok-business-day
ORDER_READY paper plans and COMPLETE/ACCEPT analysis_checks explicitly stamped
by the current producer. Unmarked legacy ACCEPT/REJECT/HOLD records are ignored,
without deletion or backfilling trust, even if they contain execution_quantity.
NO_PERSISTED_SETUP is expected when no current eligible ACCEPT exists.

Current provenance stored atomically inside each new result/plan:
- contract_version: `neuroapi-decision-v1`
- analysis_version: `analysis-v6`
- source_type: `NEUROAPI_STRUCTURED`
- execution_sizing_version: `deterministic-max-risk-5-v1`
- exact API operation plus SHA256 of the sanitized result/plan and of the canonical
  structured COMPLETE API output (never raw provider prose, prompts or secrets).

A current-version claim with missing/incorrect provenance, changed evidence or
missing execution quantity fails SHADOW_SOURCE_UNVERIFIED. Hashes detect changed
persisted evidence, not malicious database administrators. Shadow independently
compares side/Entry/TP/SL to the COMPLETE request and rechecks deterministic quantity,
current Binance rules, risk and RR. No provider level or quantity is repaired.
HOLD/REJECT remain persisted but never become candidates or consume trade slots.
Existing trade slots still count against the daily cap, regardless of provenance.

Optional **paid research**, never part of deployment/shadow:
`docker compose exec -T worker python -m worker analysis-once BTCUSDT ETHUSDT`
now claims `<business-day>:analysis-v6:<symbol>`, at most two distinct symbols for
that namespace/day. Previous v3/v4/v5 requests/cycles remain intact. PENDING is
persisted before any call; repeated or interrupted invocations never replay a
claimed symbol, including across restart. One normal analysis per symbol; existing
bounded retries only on explicit provider 429/503 remain unchanged. No screening,
trade reservation or live execution occurs. Normal daily workflow stamps new
plans with the same current contract, keeping its existing cycle/request locks.

A new shadow_plans table stores only sanitized plans/IDs and separate model-only
state. Existing trades, cycles, API ledger, prompts and dashboard are unchanged.
A transaction rechecks persisted candidates/daily count before storing plans.
CLI cycle locking prevents overlapping CLI research; database revalidation also
catches account-service ledger changes during preflight. A shadow check is only a
point-in-time assessment, never an order reservation or protection acknowledgment.

Client IDs derive from day + symbol + direction + immutable levels + execution
quantity, with distinct ENTRY/TP/SL suffixes, all <=36 allowed characters.
Repeat/restart preserves IDs. Different setup on the same day/symbol conflicts
rather than silently replacing its identity. No random retry identity exists.

## Future-state model, not an executor

PLAN_READY -> ENTRY_SUBMITTED -> ENTRY_CONFIRMED -> PROTECTION_SUBMITTED ->
POSITION_PROTECTED is a pure model tested with synthetic events. Runtime shadow
only saves SHADOW_PLAN_READY, SHADOW_PREFLIGHT_OK or NEEDS_REVIEW. The model starts
at PLAN_READY; the CLI never fabricates actual submission/fill/protection events.

Uncertain entry blocks subsequent setup and requires reconciliation against the
same deterministic ID; no retry/NOT_FOUND shortcut exists. Intended lookup
contracts are documented in the plan but are not called in Phase 1. Partial fills
block progression until fill and remaining entry are reconciled. Both protective
acknowledgments are required; one missing/failed leg is PROTECTION_INCOMPLETE and
blocks the next setup across restart. Persisted malformed/model-incomplete state
also blocks. Real fill reconciliation, protective submission/cancel/recovery remain
unimplemented; a future live phase requires separate review and implementation.

## Permission fix and safe deployment

Git/source operations use umask 022. Docker build explicitly normalizes only
`/app` (public source) to directories 0755/files 0644, even if checkout files were
created under umask 077. Build then imports runtime modules as USER office (UID
10001) and returns to root only for existing bootstrap privilege drop. No secret
or data directory is normalized: secrets stay 0600, private directories 0700.
The Docker build context still excludes all secrets and private data.

Work has no Docker daemon and forbids setuid, so local tests verify DAC permissions
and imports; the actual UID 10001 import gate executes during VPS Docker build.
Do not use broad recursive chmod on VPS /opt or private directories.

Update/rebuild worker only, with OFFICE_AUTO_DRY_RUN=false, preserve volumes and
secrets, then explicitly run binance-check and binance-shadow. Do not run setup.py
again, screening, analysis, or any account mutation. Preflight output can be
NEEDS_REVIEW; inspect the sanitized reason instead of forcing acceptance.
