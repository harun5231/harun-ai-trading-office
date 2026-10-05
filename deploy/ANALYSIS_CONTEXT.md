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
Each requested symbol has a durable daily `analysis-v3` operation and a claimed
analysis_checks record before any paid call. Up to two checks per day; repeating
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
