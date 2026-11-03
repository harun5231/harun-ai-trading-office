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
