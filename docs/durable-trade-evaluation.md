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
`existing_risk_monitor` modules **and** the new shared
`worker/secure_sqlite_snapshot.py` helper; the repository's generic Dockerfile does **not**
select the native API service by itself. This change does not switch runtimes,
deploy, restart a service or modify any deployment allowlist/hash manifest.

`<existing worker trading directory>/trade_evaluation.sqlite3` is independent of
`ledger.sqlite3` / financialDB. It is a complete, owner-only SQLite **snapshot**,
not a filesystem-opened SQLite database. SQLite runs only in memory (including
temporary tables), with UPDATE/DELETE rejection triggers. Each poll atomically
appends changed evidence, jobs and streak projection in memory, then publishes
one complete image: exclusive-create a random mode-0600 temporary file, write and
fsync it, dirfd-relative atomic replace, then fsync the directory. Success is
reported only after publication. A process crash leaves the prior or new complete
image; an orphaned pre-publication temporary file is ignored on replay. Identical
polls append no events (the image may still be republished).

All ingestion and status operations lock the same pinned actor-directory inode
(exclusive writers/shared readers; five-second bounded acquisition), including
across processes. The main image is opened once read-only with NOFOLLOW and
single-link/owner/regular-file checks. It is never reopened by SQLite or opened
for writing. Status only deserializes that image; it does not initialize storage,
recover journals, checkpoint, or publish anything. Neither path opens SQLite WAL,
SHM, rollback journals or super-journals, even if these names already alias
financial files. Publication replaces a directory entry rather than following
its current target, so main-file swaps cannot turn it into an inode write.
Back up the single published image; no WAL collection is needed. Prior prototype
WAL-format images are refused rather than implicitly recovered/migrated (this
journal has not been deployed). Financial databases are never migrated.

### Reusable snapshot API (for separate new journals)

```python
from worker.secure_sqlite_snapshot import snapshot_database

# directory is the actor's trusted stable state directory; name is a fixed
# dedicated journal basename, NEVER ledger.sqlite3 or another financial DB.
with snapshot_database(directory, 'new-evidence.sqlite3', write=True, lock_timeout=5) as db:
    db.execute('CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY)')
    db.execute('BEGIN IMMEDIATE')
    db.execute('INSERT OR IGNORE INTO evidence VALUES (?)', ('stable-id',))
    db.execute('COMMIT')
# File fsync + atomic replacement + directory fsync completed on normal exit.
with snapshot_database(directory, 'new-evidence.sqlite3') as db:
    rows = [] if db is None else db.execute('SELECT id FROM evidence').fetchall()
```

The helper is schema-agnostic; it neither knows nor imports trade/account/provider
code. Names must be single basenames (traversal/absolute/NUL names are refused).
Missing readers yield `None` without creating files. Connections are closed on
exit; reads enable query-only, writes are autocommit unless the caller starts an
explicit transaction, which must be committed before normal context exit.
Exceptions or uncommitted transactions do not publish. Use only trusted SQL and
keep SQLite storage in memory: callers must not ATTACH filesystem databases,
change temp/journal storage pragmas or otherwise perform disk operations through
the connection. The same directory lock deliberately serializes different names
in that directory too. Billing integration is left to its own workspace; no
billing code or financial storage has been changed here.

### Storage threat boundary and limits

- Linux/POSIX local storage with working `flock`, atomic same-directory replace,
  and file/directory `fsync` is required, as are Python SQLite serialize/deserialize
  APIs. Network filesystems and filesystems without these guarantees are outside
  this durability claim. Process-crash tests do not simulate hardware power loss.
- The actor supplies its own state directory, not a request-controlled path. The
  directory must be owned by the worker and not group/other writable; its ancestor
  path and directory inode must be trusted. The final directory component cannot
  be a symlink. Once opened, all image I/O/publication is relative to its dirfd,
  so a later pathname swap cannot redirect writes into a financial directory.
- Symlink/hardlink aliases and deterministic main/directory replacement races
  cannot make this implementation write through a financial inode. The worker
  UID/root is **not** an adversarial isolation boundary: a process with that
  authority can directly modify financial files, tamper with snapshot contents,
  rename lock directories to bypass cooperation, replace publication temp files,
  or deny service. Locking assumes cooperating journal users and a stable trusted
  actor-directory inode for cross-call identity. No claim of preserving journal
  authenticity/availability against those attacks is made.
- This is append-only application storage, not an externally anchored tamper-proof
  audit ledger. Complete snapshot reads/writes and all-history recomputation are
  not bounded in history size; growing streak evidence and memory/I/O/latency
  remain separate follow-up work, not resolved by this security-only fix. Lock
  acquisition is bounded, but processing and filesystem I/O are not time-bounded.

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
restart/replay and thread/process idempotency, backfill, incomplete PNL, private
file handling, both operations against main/WAL/SHM/rollback/super-journal
symlink/hardlink aliases, main/directory swaps, temporary-file alias collisions,
bounded busy-lock failure, publication fsync ordering/failures, and subprocess
crashes before/after atomic replacement. The exact 8192-byte valid financial DB
hardlinked as SHM regression fails on the prior implementation for **both**
ingestion and status, and now remains byte-identical and queryable. No real
orders or Neurobro/provider/financial requests are needed.

## Handover continuation: evidence binding

`worker.trade_evaluation_evidence` now supplies strict lifecycle/order/research
binding and a reader for existing native historical PNG/CSV sidecars. The same
journal adds durable once-intents and source-bound evaluation receipts; a history
revision invalidates the result without deleting its evidence. These are offline
interfaces, not a deployed evaluation consumer. Native evaluation phase/schema
admission, owned archive lookup and delivery/response custody adapters still need
to be connected after the retained b844 clarification and df80 worker activation.
No autonomous evaluation or win-rate experiment is claimed from these helpers.

## Historical Neurobro consumer (source; not a VPS completion claim)

`trade_evaluation_runtime` joins the already authenticated account-history cache
and owned CLOSED intent to original research and historical material custody.
The existing native account actor binds evidence; the existing coordinator only
starts `POST_TRADE_EVALUATION` after normal work returns idle/capacity-full, a
completed cycle (including a final technical rejection), or a verified completed
market HOLD. Every opportunity requires no unresolved research or financial
transaction. The HOLD boundary requires a matching final HOLD candidate and no
failure/wait; mid-analysis and uncertain provider outcomes never qualify. This
prevents unlimited HOLD replacement rounds from starving an evidenced evaluation
while keeping the same coordinator and DailyRoom. Owner ON/readiness
controls remain in force. Protection and account actors stay independent. The
one DailyRoom serializes historical discussion with trading; no daemon is added.

The existing browser service needs the source-hash-bound additive patch in
`scripts/patch_native_evaluation_service.py`. Its authenticated
`POST /v1/historical-evidence` only reads a BOUND room, original private PNG/CSV
archive and native receipt chains. It cannot navigate, Send, choose paths or
create an order. New website captures produce an owned lookup index; a missing
legacy index/four-file receipt stays blocked. Historical validation uses the
original capture instant without changing any live clock/freshness rule.

Evaluation reuses actual historical four-file turns when readable in the current
BOUND room. Otherwise it reads the original owned archive and delivers two PNGs
and two CSVs per historical material turn through the existing native upload,
labelled with the recorded capture times. A streak can continue through multiple
material turns under one operation and DailyRoom; no fresh chart substitutes for
old evidence. An immutable planned intent precedes each upload, followed by a
chained actual-upload/Send intent with the new attachment IDs. Each observed
reply completes its own turn receipts with the actual native UI message ID and
raw reply hash. A separate final-assessment receipt binds that ordered chain to
the actual final native response ID; intermediate replies are not relabelled as
the last response. Final custody requires
all expected material groups and actual user-turn witnesses, not merely the last
PNG pair. Unknown outcomes are recovered only from native completion custody and
are never replayed. Results cannot become setups, orders, strategy changes or
claimed completed experiments. This B4 phase cannot select the B1 clarification
task or re-send its attachments; it only accepts the journal's owned CLOSED-trade
bindings.

Native release assembly must package the new `agent/native_evaluation_{protocol,
browser,rpc}.py`, `worker/trade_evaluation_{evidence,archive,runtime}.py`, the existing
snapshot helper, and the changed provider/dialogue/actor hooks together. Do not
apply this source to the already staged df80 worker-only artifact or relabel that
receipt. Runtime archive access, new-phase dialogue and analyst response still
require deployment evidence; offline tests/fixtures do not establish them.

## Performance observations and evaluation scheduling

The same journal also derives a `performance-decline:<closing-trade-id>` job when
the last three observed complete closes have **both** fewer net winners and lower
average net PNL than the preceding three closes. This six-close comparison is an
evaluation observation, not a causal finding, risk limit, pause or strategy
change. An unknown close prevents that comparison. Stable IDs, evidence gating,
replay and backfill supersession are identical to losing-streak jobs; all six
original decisions and historical material groups must bind before discussion.
Ready aggregate jobs precede older singleton jobs, and the newest aggregate
precedes older ones. Unknown native outcomes still prevent a new shared-room
Send. Read-only recovery rotates among unknown operations so one missing receipt
cannot indefinitely hide another operation's actual completion; no blind replay
is introduced. `unresolved_evaluations` derives directly from durable once-intents
and actual result operations, independently of whether their jobs remain current.
Backfill/supersession cannot hide a possibly in-flight Send. An actual receipt for
the original input can be retained to resolve that unknown outcome, while its
superseded or mismatched findings remain ineligible for current projection and
its stale claim remains ineligible for dispatch.

`monthly_performance` groups exact recorded closing instants in Asia/Jakarta.
Its scope is explicitly `OBSERVED_CLOSED_POSITIONS`, never a full account-month
claim. `complete` means all **those observed positions** have complete net PNL;
the journal's separate history completeness/missing reasons remain authoritative
for history coverage. Win rate includes breakeven in the denominator. Drawdown
is peak-to-trough cumulative **closed net PNL** for the observed month, starting
from zero; it is not account equity or mark-to-market drawdown. Consistency is
the proportion of observed closing days whose net PNL is positive. Any unknown
close makes the observed net total, win rate, drawdown and day consistency null;
the complete-row subtotal is separately labelled. Replayed IDs are counted once.

Actual realized net R uses the bound initial SL, actual filled entry average and
actually filled quantity, including partial fills. Its monthly mean reports the
number of positions with that evidence and missing-denominator limitations;
history revisions invalidate stale bindings. This is distinct from the initial
recommended TP or requested strategy RR. No SL, TP, sizing or trading policy is
changed by any metric. Account equity drawdown percentage, operating costs and
net after operating costs remain null because this journal does not establish
them. Existing net position PNL already includes trading costs once; it is never
charged again for commission/funding, and prepaid payments/top-ups are never
invented as operating expenses.

New bindings retain the authenticated signed fills and the calendar's realized
PNL, commission, funding and insurance components. Historical evaluation sends
those facts and actual net R to Neurobro with the original decision and archived
1H/15M PNG/CSV evidence. Older valid bindings continue to work with unavailable
details labelled unknown. Analyst findings, lessons and hypotheses remain the
actual source-bound native reply; proposed tests remain `NOT_RUN` until real
test evidence is recorded. A terminal `BELUM CUKUP BUKTI` is a valid inconclusive
evaluation when required historical facts are unavailable, not a reason for an
unending clarification loop.

Historical reply excerpts use `agent.llm.web_display_text` for WEB display only.
Old private Office budget/advisory-sizing clauses are omitted from that excerpt,
which identifies the original source SHA-256. The original analyst text and hash
remain unchanged in the request, archive witness and journal; actual signed PNL,
fees and filled quantity remain facts even when their value happens to equal a
private budget. The complete projected material text is checked by the existing
outbound WEB guard before an initial or follow-up attachment upload.

Native packaging must also include `worker/trade_evaluation_metrics.py` alongside
the existing journal/evidence/runtime modules. These source and offline tests do
not prove a production archive join, analyst discussion, experiment, complete
account-month accounting or improved performance.
