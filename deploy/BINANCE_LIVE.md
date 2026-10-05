# Shadow/preflight release — live submission physically DISABLED

The latest user authorization permits publication only with **no activatable live
submission path**. This overrides the earlier proposed live activation design.

## Hard release boundary

- Both `BinanceExecution.scoped` and its underlying `wire` reject every non-GET
  before signing, authorization callbacks, clock sync or network I/O.
- The transport constructs GET requests only. No POST/PUT/DELETE request sender
  exists in this release. Mutation-shaped contracts are inert planning/test data.
- `binance-arm` and `binance-execute` return LIVE_EXECUTION_DISARMED. Even correct
  terminal confirmation, forged private arm/run files or environment flags cannot
  enable them. No command can produce an active authorization.
- `binance-scheduler` always returns SCHEDULER_OFF. It cannot call NeuroAPI or
  execute a cycle. The API service does not start an execution supervisor.
- Dashboard/public HTTP has no activation route. Authenticated `/binance/status`
  explicitly reports DISARMED, live_execution=false, scheduler_enabled=false.
- Existing `binance-check`, public market data and `binance-shadow` remain GET-only.
  `binance-live-preflight` is a compatibility command name for read-only preflight;
  its name/status does not authorize submission. would_submit and live_execution
  remain false, including when preflight succeeds.

Enabling submission would require a separately reviewed source-code change and
new authorization; no local configuration or command unlocks this build.

## Preserved planning and offline lifecycle tests

The deterministic entry/TP/SL contract and lifecycle model are retained for offline
exchange simulators. Simulator tests cover durable intents, uncertainty without
blind replay, partial fills, protection proof, immutable levels, sibling identity,
CROSS/75x targets and persistent daily slots. These tests never send real orders.
Model results explicitly identify SHADOW mode and live_execution=false.

The model can describe LIMIT/GTC/BOTH entry and CONDITIONAL STOP_MARKET /
TAKE_PROFIT_MARKET protection via Algo Service, MARK_PRICE, closePosition=true,
without quantity/reduceOnly on protection. It can describe candidate-only margin/
leverage configuration and owned-ID cancellation. **None can reach Binance as a
mutation.** There is no bulk cleanup, account-mode change, transfer or withdrawal.

Official contract reference audited on 2026-10-05:
https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade

Error/reconciliation reference:
https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/error-code

## Manual coexistence and existing evidence

Unrelated manual symbols such as HYPEUSDT are informational/account-margin input,
not a global rejection for BTC/ETH. Candidate-symbol manual positions, normal orders
or algo orders still reject. Real available USDT balance, current contract rules
and leverage brackets remain required. HYPEUSDT is additionally excluded from the
new scoped transport targets; nothing closes, cancels or modifies it.

Existing analysis-v6 data, hashes, provider levels, execution quantity, prompts,
Risk Manager, daily limit and previous ledger records are not reset or restamped.
The derived repeating-RR fix is retained. Preflight uses the existing validated
BTC/ETH records without new screening/analysis or NeuroAPI calls.

New lifecycle tables are reserved for simulated/reconciliation evidence; existing
trade/audit records are preserved. API keys remain in existing private files and
are never written to logs, database, repository or dashboard.

## Deployment

Build first, recreate worker with OFFICE_AUTO_DRY_RUN=false, recreate proxy to load
the read-only status route, then run **only** binance-check and binance-live-preflight.
Keep all existing volumes and secrets. Do not run arm, execute, scheduler, screening,
analysis-once or NeuroAPI during deployment. No activation command is supplied.
