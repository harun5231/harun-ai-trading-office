# Monthly billing contracts

Owner-entered expense evidence plus existing cached Binance income facts. No
provider, Usage API, financial API, payment or withdrawal calls are made by
billing. The existing API-key file is read locally only to check the cache's
credential identity; no signing-secret file is read or client constructed.
Synthetic tests use local fixtures and loopback HTTP only.

## Evidence and unknowns

- Money is a nonnegative decimal **string**; `null` means unknown, never zero.
  Enter `"0"` only when absence of a charge/remaining credit is confirmed.
- `coverage_confirmed` attests that the owner listed every monthly service.
- `fx_idr_per_usdt` is an owner-evidenced **net IDR per USDT** conversion quote
  (include spread), not USD/IDR. `fx_source` records its source/date. If provider
  usage/credit is denominated in USD, convert separately to IDR and document
  that conversion in the usage/credit evidence. USDT is not assumed equal to USD.
- Each prepaid row may add `usage = {through_date, available_credit_idr,
  credit_evidence}`. `actual_idr` is official cumulative usage **from the first
  of the selected month through the end of `through_date` in WIB**. Enter only
  completed days, strictly before the current WIB date. The remaining credit
  snapshot must refer to that same date and already be converted into IDR.
- The API does not verify an owner's evidence. `openai_balance` remains `null`
  even when an owner enters a credit snapshot. There is no automatic balance or
  usage claim. A future automatic integration requires a separately authorized
  organization Admin/Usage API credential; ordinary model credentials are not
  silently repurposed or requested here.
- Legacy prepaid rows without dated usage can retain owner-entered estimates,
  but cannot establish final actual costs, net profit or a top-up requirement.
- Partial-month usage is actual usage-to-date, not final monthly actual cost.
  Final actual cost/net profit require month-end usage, complete cost coverage,
  known actual fees and verified complete selected-month income history.

## Fees and calculations

Optional document field `cashflow` has exactly these nullable decimal-string
fields (older revisions remain readable; omitted fee evidence is unknown):

| Field | Meaning |
| --- | --- |
| `payment_fee_actual_idr` | Total payment fees already incurred this month |
| `payment_fee_estimated_idr` | Additional payment fees expected for unpaid bills/top-ups |
| `withdrawal_fee_actual_usdt` | Already-incurred cash-out fees outside trading income PNL |
| `withdrawal_fee_estimated_usdt` | Fixed fee for the contemplated consolidated cash-out |
| `withdrawal_fee_percent` | Additional fee deducted from the gross contemplated cash-out, 0 <= % < 100 |

Do not re-enter Binance commission/funding/insurance already included in trading
income PNL. Do not enter historical paid top-ups as an extra usage cost: a top-up moves
cash into prepaid credit, while consumption is the expense.

For each prepaid row:

```
projected_month_usage = actual_usage_to_date / elapsed_calendar_days * days_in_month
additional_topup = max(0, projected_month_usage - actual_usage_to_date - remaining_credit)
```

The prepaid `estimated_idr` field is a legacy/manual estimate only; dated actual
usage takes precedence. This is a linear run-rate estimate, not a forecast of
future traffic. It estimates credit needed **for the remainder after the usage
snapshot**, not reimbursement for top-ups already funded. Provider minimum
purchase sizes, increments and taxes are not discovered automatically; account
for known payment charges explicitly and review the provider portal before
paying. A stale snapshot must be refreshed by the owner.

```
actual_cost_idr = sum(final actual service costs) + actual payment fees
estimated_cost_idr = sum(projected service costs) + actual payment fees + remaining payment fees
cash_needed_idr = unpaid subscription invoices + additional topups + remaining payment fees
withdrawal_usdt = (cash_needed_idr / fx + fixed_remaining_cashout_fee) / (1 - percent/100)
actual_net_profit = monthly_trading_income_net - actual_cost_idr/fx - actual cashout fees
estimated_net_profit = monthly_trading_income_net - estimated_cost_idr/fx - actual cashout fees - projected cashout fees
```

A zero funding requirement does not incur another fixed withdrawal fee. Funding
is rounded **up** to eight USDT decimals; net profit uses eight-decimal display
rounding. Decimal calculations use an isolated high-precision context. Rounding
surplus remains cash, not a second expense. Negative profit is preserved.
Unknown credit, fees, FX, payment status or incomplete coverage leaves the
corresponding result unknown. Withdrawal uses owner payment status, not exchange
balance/available-margin checks; it is not an authorization or an instruction to
withdraw.

`worker/monthly_pnl.py` reads `pnl-history-v1.json` through the existing private
`HistoryCache` loader, including schema, row-key uniqueness, file privacy and
credential identity checks. The selected month must have ended in WIB and its
entire inclusive millisecond range must be covered by exhausted, unfiltered
`income_coverage` windows. A broader cache may prove multiple historical months;
a gap anywhere in the selected month keeps results unknown. Cache contention,
missing credentials/cache, invalid schema and mismatched identity also fail closed.

The adapter emits `kind=MONTHLY_NET_TRADING_INCOME`, exact selected-month coverage
bounds, every unique daily net amount, and explicit signed component totals:
`REALIZED_PNL + COMMISSION + FUNDING_FEE + INSURANCE_CLEAR + COMMISSION_REBATE + FEE_RETURN`.
Income amounts already have their signs: negative commissions reduce net, positive
fee refunds increase it. Facts are counted once by the cache's incomeType/tranId
key; fill commissions/realized PNL are never added again. Recognition is on each
income event's WIB date, including fees on still-open positions, NOT the closing
date of a reconstructed flat-to-flat position.

Explicitly excluded non-trading types: `TRANSFER`, `INTERNAL_TRANSFER`,
`CROSS_COLLATERAL_TRANSFER`, `STRATEGY_UMFUTURES_TRANSFER`, `WELCOME_BONUS`,
`CONTEST_REWARD`, `REFERRAL_KICKBACK`, `API_REBATE` (broker/referral partner income),
and `BFUSD_REWARD` (yield). Any other selected-month income type, or any non-USDT
trading component (even zero), makes trading net unknown; no FX is invented.
These classifications define trading income, not total wallet change.

The existing `CLOSED_POSITIONS_FROM_FILLS` calendar remains intentionally partial
with `SYMBOL_DISCOVERY_NOT_EXHAUSTIVE`; its known subtotals are not used for billing.
Summary acceptance retains strict source, WIB, full-month daily coverage and
finite decimal-string checks, and additionally requires the adapter kind and
exact complete income-coverage evidence. This is neither unrealized PNL, official
balance, available margin nor a withdrawal authorization.

Income endpoint schema/sign/identity reference:
https://developers.binance.com/legacy-docs/derivatives/usds-margined-futures/account/rest-api/Get-Income-History
Broker rebate scope:
https://www.binance.com/en/support/faq/a78a065d0c4846aaa1af474d8e712ab9

## Portal links and access

Both server and UI restrict payment links to HTTPS, exact approved portal hosts,
and approved paths. Current hosts: `platform.openai.com` (organization billing
path only), `hpanel.hostinger.com`, `console.hetzner.cloud`, `accounts.hetzner.com`,
`my.vultr.com`, `cloud.digitalocean.com`. Unsupported providers can be recorded
with a null link. Ports, credentials, lookalike domains, query strings, fragments,
percent escapes, whitespace, backslashes and dot path segments are rejected.
This conservative allowlist is not invoice verification. No checkout session,
secret token or automatic payment URL is generated. Links use text rendering and
`noopener noreferrer`.

Reads require the existing read/control authorization. Writes require the
existing control authorization and exact permitted Origin, bounded JSON,
duplicate-key rejection and optimistic month-version matching. Revisions are
append-only in a dedicated SQLite DB, not the trading ledger. UI saves are
read-back verified and cannot cross a changed connection generation. No auth
secrets are persisted by the dashboard.

## Storage security

`monthly-billing.sqlite3` is a dedicated, complete snapshot, never the financial
ledger. `Store.connect(write=False)` is a context manager, not a persistent
connection: SQLite receives only `:memory:` and no connection escapes its context.
Missing reads/history return version zero/empty history without creating any file;
existing reads are query-only and never initialize schemas or publish images.

The shared `worker/secure_sqlite_snapshot.py` helper is copied unchanged from
evaluation commit `898892f2ad53a1085ae3308166fd54a4b9cff32c`. It pins the trusted
worker-owned, non-group/other-writable directory with NOFOLLOW, locks that directory
inode (shared reads/exclusive writes, five-second bounded acquisition), and reads
a regular, worker-owned, single-link main image through a NOFOLLOW descriptor.
Main symlink/hardlink aliases and WAL-format prototype images are refused. SQLite
never opens WAL, SHM, rollback journals or other filesystem sidecars: existing
sidecar aliases are **ignored**, not recovered, migrated, followed or removed.

A save checks the month version and appends one revision while holding the same
exclusive directory lock, including across processes. Successful transaction
commit is followed by exclusive creation of a random mode-0600 temporary image,
file fsync, dirfd-relative atomic replacement and directory fsync before returning.
Exceptions/version conflicts publish nothing. Preserve prior revisions; do not
migrate financial files or recover a prototype WAL image implicitly.

The directory and its ancestors must be stable/trusted; cooperating callers must
retain memory-only SQL storage (no ATTACH/filesystem pragmas). This requires local
POSIX flock/replace/fsync and SQLite serialize/deserialize support. Worker UID/root
is not an adversarial isolation boundary. Whole-snapshot memory/I/O grows with
history; lock acquisition, not all I/O/processing, is bounded.

## Local verification

```
python -m unittest tests.test_monthly_billing tests.test_monthly_billing_storage tests.test_monthly_pnl tests.test_pnl_calendar tests.test_dashboard_contract -q
node --check assets/dashboard.js
git diff --check
```

Native packaging must include `worker/monthly_billing.py`, `worker/monthly_pnl.py`
and `worker/secure_sqlite_snapshot.py` alongside the native
API service and ship the changed dashboard/index/CSS. Integration/deployment is
left to the parent reviewer; this work performs no deploy, push or production
operation.
