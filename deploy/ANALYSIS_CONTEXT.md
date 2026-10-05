# Analysis context and one-time validation

Both normal workflow and `python -m worker analysis-once BTCUSDT ETHUSDT` now fetch
public Binance contract rules before submitting each literal ANALYSIS prompt.
Context keeps the original symbol/source/mark/timestamps/1h and 15m candles and
adds base-asset quantity units, stepSize/minQty/maxQty/tickSize/minPrice/maxPrice,
applicable minNotional and percent-price multipliers/reference mark/entry bounds.
Risk constraints specify CROSS, 75x, maximum price loss at SL 5 USDT and minimum
actual reward/risk 2. These system constraints are separate from exchange rules.
Schema descriptions explain quantity and price compliance; prompts are untouched.

The same immutable setup is checked after the response against refreshed exchange
rules. No rounding, resizing, repair prompts or live orders. Failed provider output
is not exposed; numeric result fields are null if a valid Signal is unavailable.
Valid structured signals show exact returned levels, calculated price risk and RR,
even when rejected later for quantity precision or excess risk.

`analysis-once` is analysis/validation only: no screening, paper trade reservation,
cycle reset or live submission. It requires exactly two distinct active symbols.
Each requested symbol has a durable daily `analysis-v4` operation and a claimed
analysis_checks record before any paid call. Up to two checks per day for this version; repeating
or reversing the same command returns stored results, not new provider requests.
Interrupted checks fail closed. HTTP retry policy remains only 429/503, bounded.
Old cycles and API request records are untouched. Cached results are historical
validation results, not refreshed trading signals.

After deployment, run once:

```sh
docker compose exec -T worker python -m worker analysis-once BTCUSDT ETHUSDT
```

This consumes up to two new logical analysis requests. Do not reset records to
retry. No NeuroAPI key is requested, printed or stored in the repository. Tests
use fixtures only and do not establish that the real provider will produce an
acceptable setup; an invalid setup must still be rejected.

## Maximum-risk sizing contract

Target price loss at SL is 5 USDT. Context and quantity schema now explicitly ask
for the largest legal base-asset quantity within that budget. The independent
validator computes the number of complete stepSize increments allowed by both
5 / abs(entry-SL) and maxQty, using exact integer/rational floor arithmetic.
It checks minQty/minNotional and retains all price/range/percent-price checks.
Only exact equality with this maximum legal quantity passes. There is no arbitrary
percentage tolerance; a smaller risk is accepted only when the next step would
exceed the budget or maxQty. Provider prices and quantity are never modified.
Undersizing yields POSITION_SIZE_NOT_MAX_RISK; risk above 5 still yields RISK_ABOVE_5.
RR is still based on actual levels and may exceed 2. Leverage does not alter risk.

Version v4 permits one new pair of analysis-only checks without deleting old v3
records. Repeating v4 that day returns its stored result; no new paid request.
The trading ledger's maximum two positions/day remains unchanged across versions.
