# Runtime Neurobro agent and production integration

PR #24 now contains an executable Python daemon (`python -m agent`), not only the
initial preparation contract. The original 117 repository files remain unchanged,
including the complete 3D office, worker, Risk Manager and order gateway. The new
agent image copies only `worker/core.py`, `worker/prompts.py` and `worker/__init__.py`
from that baseline. It does not contain the public placeholder gateway,
`Coordinator`, the worker service, exchange credentials or a financial database.

The agent uses GPT-5 Mini through the OpenAI Responses API for bounded dialogue
control. Model output has only an action, explicitly quoted screening symbols,
and one short question; no price or order fields. ENTRY/TP/SL are parsed from the
attributed Neurobro message. The unchanged deterministic Risk Manager validates
and sizes the setup. Only an independently authenticated existing-backend adapter
can admit it to the existing LIVE gateway while ROBOT is ON. No new Binance
executor is installed, and LIVE capability is not disabled.

## Implemented behavior

- Own SQLite conversation, budgets and operation journal, with a single controller
  lock. A delivery that may have happened is not blindly replayed after restart;
  it needs an authoritative receipt. Confirmed not-sent failures can yield safely.
- Fresh sanitized account snapshots, Jakarta daily-entry count, two combined
  position/pending slots, two new entries per day, and ownership checks. Manual
  positions and pending orders consume capacity; only owned pending orders are
  eligible for automatic review. Overnight pending orders retain their slots.
- Screening uses available capacity. HOLD excludes the old symbol and permits
  at most three replacement rounds. The original analysis prompt in
  `worker/prompts.py` is preserved byte-for-byte; the agent never edits it.
- Public Binance USD-M 1H/15M candles, mark price and exchange rules use bounded
  GET-only requests. PNG charts are rendered from those numeric candles with
  source, timeframe and time evidence. **They are data-derived chart images,
  not screenshots of Binance's website UI.** No invented market prices are used.
- Bounded clarifications and fresh market facts, followed immediately by risk
  handoff when the Neurobro setup is complete. Quantity uses actual commission
  evidence and the existing fee/slippage risk model, normally 5 USDT from the
  backend setting. Cross 75x, lot/tick, margin and net 2R checks stay deterministic.
- Owned LIMIT entries are reviewed after five hours using fresh market evidence.
  TAHAN schedules another five-hour review. BATALKAN requires an independent, completed and fresh browser receipt bound
  to the exact pending-order reference and explicit decision, plus a fresh
  revision and partial-fill check at the existing backend before cancelling
  only the remainder; it cannot close an already filled or manual position.
- OFF pauses screening, dialogue, market work, handoff and cancellation. There
  is no code that turns ROBOT ON. Cached local health observations never start
  trading, provider requests or order probes.

## Browser and backend boundary

`agent/providers.py` implements attachment to an existing Chromium/CDP session,
strict origin/page/account/composer checks, PNG upload, bounded response
completion and login-expiry handling. It does not launch Chromium or read profile
or cookie files. A real temporary-Chromium fixture exercises the adapter without
using the VPS or Neurobro account. ProductionDOM selectors must be reconciled
against the existing private browser source; test selectors are not a production
contract. A container's private loopback is not the host/browser loopback, and raw
CDP access is broad. A protected origin-scoped adapter or explicitly reconciled
browser transport must be provisioned, rather than exposing a production CDP port
publicly or mounting the browser profile into this agent.

`agent/browser_rpc.py` supplies a real origin-scoped HTTP adapter and durable
request receipts; the application can use this instead of receiving any raw CDP
capability. `agent/backend_adapter.py` supplies the complementary real HTTP
server/factory for the existing backend, with shared execution locks, independent
browser-source receipts and durable admission checkpoints. Its production
callbacks still require reconciliation with the actual worker and sole research
coordinator.

`agent/bridge.py` implements the authenticated finite backend protocol. Its
revision, source hash, atomic robot gate, capacity policy and LIVE-gateway
capability must match the approved configuration. The backend retains exclusive
financial authority and must verify the Neurobro receipt against the browser's
own record before execution; the agent's self-hash alone is not origin proof.
The implemented adapter and offline fixture do not establish that the current VPS
implements those endpoints. The production adapter must be installed beside the
verified existing backend without replacing its gateway or running two trading
coordinators. Missing or stale fees, ownership, capabilities or source bindings
pause work instead of assuming financial facts.

## Image, checks and deployment

`deploy/agent.Dockerfile` builds the real non-root UID 10002 image. The base image
and Python wheels are hash-pinned. The image has its own data directory, no
production mount/socket, and no browser download. Its source/policy labels are
verified by the operations broker. A build secret can supply the environment CA
without copying that CA into image layers or weakening TLS.

The GitHub-hosted `agent-image.yml` runs unit/regression tests, builds the isolated
image, and runs the actual daemon against a synthetic authenticated OFF backend
with `--network none`. An owner manual run on main can publish the tested source
to GHCR and retain `agent-image.json` with the real registry digest. That artifact
is build evidence, not deployment approval. No registry digest is invented and
no release catalog is self-approved by the implementation.

The companion [VPS Ops PR #2](https://github.com/harun5231/harun-vps-ops/pull/2)
implements the application-only root broker and manual `agent-ops.yml` path for
deploy, status, categorized logs, restart and rollback. The runner keeps its
existing isolation and receives no Docker socket or general root shell. The old
general deployment workflows remain locked; the new path verifies the owner
manual run and exact approved release independently through GitHub. It keeps
receipts and previous own-agent images/configuration and never restores trading
ledger, positions, credentials or browser profiles.

## Current deployment status and remaining prerequisites

Final local validation ran 912 tests successfully (one unrelated optional test
skipped), with both isolated Chromium and local HTTP integration fixtures enabled.
The full-stack fixture exercises screening, chart attachments, Neurobro reply
provenance, deterministic total-risk sizing, owned partial-fill cancellation and
the OFF fence. Its gateway is a test fixture, so it sends no Binance orders.
Graphify's combined code map covers the agent and restricted operations path:
430 nodes, 1,335 extracted edges and 12 communities. These are source and local
integration checks, not VPS deployment evidence.

The daemon/image tests are local and use synthetic backend/browser fixtures.
They are not evidence of a running agent or a verified browser/backend binding
on the VPS. `provider_verified`, deployment authorization and production-source
reconciliation are deliberately not asserted by the policy validator.

The existing successful VPS audit need not be repeated. The installed runner is
ONLINE but has only a fixed read-only status publisher and `NoNewPrivileges`;
there is no installed application broker that can perform the first privileged
provisioning. A legitimate administrative management channel must install that
one application boundary. GitHub approval alone cannot manufacture root access.
The owner currently reports that administration is available only through
Termius. The companion preparation workflow stages authenticated installer bytes
using the existing runner. Its GitHub-hosted job provides one initial
administrator command; the runner itself never executes that command or gains
root. This one-time delegation is separate from routine application operations,
which use GitHub Actions. No SFTP or manual source-file upload is needed.

After initial broker provisioning, reviewed runtime/browser artifacts and
application secrets can be delivered by the approved workflow; configuration
updates do not require routine Termius work. Also required are the dedicated
OpenAI key in protected application storage,
verified production browser/backend source and endpoints, application-only
adapter credentials, and a real GHCR digest plus reviewed runtime configuration
in the approved release catalog. Never place keys in a PR, workflow input or
chat. GPT-5 Mini availability must be verified on the owner's API account; no
silent model/provider fallback is configured.

After those prerequisites and one concrete release approval, the broker can
complete that release's finite deployment and health/rollback steps without
requesting approval for each internal Docker command. The connector can read
Actions results but currently has no new workflow-dispatch tool, so an owner can
use GitHub on iPhone: **Actions → Run workflow**. No installation of the runner
is repeated. ROBOT stays OFF until the owner activates it after integration tests.
