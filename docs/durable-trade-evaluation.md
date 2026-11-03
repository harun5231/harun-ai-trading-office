# Durable post-trade observation (native account actor)

This is **not a completed Neurobro evaluation or strategy-learning system**.
It provides an operationally integrated, durable evidence/job journal and factual
closed-net-PNL findings. It never changes strategies, orders, risk or settings.

## Hook and storage

`vps_native/worker/api_service.py::Controller.account_loop` ingests the already
collected `pnl_calendar` after the existing account recovery and office snapshot,
on the existing throttled history pass, also while ROBOT is OFF. No extra actor,
provider request, exchange request or order path is introduced. Journal failure
is isolated from account/protection processing and retried on the next history
pass. Native `/office/status` adds read-only `trade_evaluation` status; a journal
failure returns a fixed failure code, not internal exception details.

The shared worker module is `worker/trade_evaluation.py`. Native runtime assembly
must include that file alongside its existing shared `pnl_calendar` and
`existing_risk_monitor` modules; the repository's generic Dockerfile does **not**
select the native API service by itself. This change does not switch runtimes,
deploy, restart a service or modify any deployment allowlist/hash manifest.

`<existing worker trading directory>/trade_evaluation.sqlite3` is independent of
`ledger.sqlite3` / financialDB. It is owner-only, refuses linked files, uses WAL
and synchronous FULL, and rejects UPDATE/DELETE via database triggers. Each poll
atomically appends changed trade evidence, derived jobs and streak projection.
Identical polls append nothing. Read status never initializes storage. Backups
must use SQLite's backup API or a stopped/checkpointed database, not copy only the
main file while a WAL writer is active. This is append-only application storage,
not an externally anchored tamper-proof audit ledger.

## Meaning of recorded results

- Stable IDs and `pnl_usdt` come from the existing closed-position reconstruction
  (`REALIZED_MINUS_FEES_PLUS_FUNDING_AND_INSURANCE`). No gross/known subtotal is
  substituted for incomplete net PNL. Exact source rows and SHA-256 digests are
  retained as immutable journal events.
- Post-trade findings are only LOSS / WIN / BREAKEVEN / UNKNOWN and net PNL.
- Consecutive losses are chronological **observed closed positions**, not a claim
  of exhaustive account history. A win, breakeven or unknown breaks the observed
  run. The account calendar's completeness limitations are retained explicitly.
- Three observed consecutive losses trigger a losing-streak job; each subsequent
  qualifying close has its own stable ID. Three is only an evaluation trigger,
  not a trading policy. History backfill/revisions recompute the projection;
  invalidated streak jobs become `SUPERSEDED_HISTORY_REVISION`, never deleted.
- Processed IDs are observations, **not completed analyst evaluations**. Jobs
  remain `BLOCKED_MISSING_EVIDENCE`, with explicit missing historical chart and
  original Neurobro decision linkage plus `REQUIRES_NEUROBRO_EVALUATION`.
- `experiment_outcome.status=NOT_RUN` is durable truthful state. No experiment,
  market explanation, hypothesis or analyst conclusion is fabricated.

## Remaining seam (deliberately not implemented)

The account calendar's closed lifecycle ID does not safely identify a bot order,
original research operation or historical browser attachment archive. Those
private receipts/200-candle materials exist only when captured, in another
existing seam; this actor cannot assume they exist or substitute current charts.
A later extension must verify lifecycle-to-order/research attribution, bind
original decision and historical chart/candle bytes by digest and capture time,
and expose a bounded Neurobro post-trade/evaluation protocol with durable result
receipts. Existing setup/screening requests are not such a protocol. Only then
can jobs move to `REQUIRES_NEUROBRO_EVALUATION` without missing-evidence blockers,
and analyst findings / actual experiment outcomes be appended. No generic model
or GPT market analysis has been added as a replacement.

## Isolated verification

```
python -W error::ResourceWarning -m unittest tests.test_trade_evaluation tests.test_existing_risk_monitor tests.test_pnl_calendar -q
```

Tests use temporary databases and fixtures, execute the actual native account
method without starting services, kill a subprocess mid-transaction, verify
restart/replay and concurrent idempotency, backfill, incomplete PNL, private file
handling, financial-file link refusal and read-only status. No real orders or
Neurobro/provider/financial requests are needed.
