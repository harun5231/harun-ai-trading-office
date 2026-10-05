# Analysis decisions and deterministic execution sizing

The normal workflow and `analysis-once` fetch public Binance contract rules before
sending the unchanged ANALYSIS literal. Context retains symbol, source, mark price,
timestamps, 1h/15m OHLCV, stepSize/minQty/maxQty/tickSize/minPrice/maxPrice,
minNotional and available percent-price limits. System constraints remain CROSS,
75x, target/max price loss 5 USDT, actual RR >=2. No live execution exists.

Neurobro chooses LONG/SHORT/HOLD and immutable Entry/TP/SL. For LONG/SHORT, the
worker computes the largest legal execution quantity within 5 / abs(entry-SL),
flooring exact rational step increments and capping at maxQty. If minQty or
minNotional cannot be met, reject. Never round upward. Price filters and actual
RR still apply; no price changes or repair requests. `neurobro_position_size` is
informational audit only (nullable); `execution_quantity` is the deterministic
quantity. The legacy plan `quantity` field equals execution_quantity for paper
monitoring compatibility. Preflight independently recomputes and rejects tampering.
Old ledger records are retained unchanged.

HOLD requires null numeric fields, creates no order and consumes no trade slot.
Normal cycles request replacement screening only for HOLD, with the exact same
SCREENING literal and exact-two-symbol schema. The first active eligible symbol
not previously analyzed in the cycle is chosen. Technical rejection does not
create another replacement. At most three replacement screenings per cycle; stop
immediately at two accepted setups. Insufficient setups after HOLD are recorded as
INSUFFICIENT_ACTIONABLE_SETUPS. Every decision is an event; dashboard accepts the
LONG/SHORT/HOLD, REPLACEMENT_SCREENING and REPLACEMENT_SELECTED states.

The durable daily cycle claim prevents restart replay, including interrupted
replacement requests. Existing cycle and API records are never reset. Provider
retry remains bounded to actual HTTP 429/503; uncertain requests require review.

`analysis-once` is an explicit analysis-only tool: exactly two supplied active
symbols, no screening/replacement, paper reservation or live submission. Version
`analysis-v5` keeps old v3/v4 records intact. It claims at most two checks per day
for this version before paid calls; repeat/reversed commands use stored results,
and interrupted checks fail closed. HOLD is reported with null execution fields.
Deployment does not run this command or any research automatically. Tests use
synthetic provider responses and do not establish real NeuroAPI acceptance.
