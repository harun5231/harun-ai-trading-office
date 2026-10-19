# Neurobro agent: concrete preparation contract

The four files in this change define and validate a future service. They do not
implement an AI runtime, verify a browser provider, authorize deployment, start
research, or change the existing worker or frontend. All readiness flags
remain false. A successful validator result means the preparation policy is
intact; it is not a successful health check or permission to deploy.

## Confirmed source baseline

GitHub `main` was read at the full commit
`d99a3dd921d48e6347211a63fff765fecaf5c85d`. Its recursive tree was complete and
contained no `AGENTS.md`. That is an investigation reference, not proof that the
current VPS runs the same source. A fresh status snapshot reporting OFF and
healthy containers does not establish source equality, browser login, or release
authorization.

| Existing source at that commit | Concrete integration implication |
| --- | --- |
| `worker/market.py:9` | Reuse the public GET-only market parser and its four endpoint allowlist; it already supplies numeric 1h/15m candles. Additional screening endpoints need their own review. |
| `worker/neuroapi.py:105` and `:158` | Current provider is an API-key client posting to NeuroAPI. It is not Neurobro web automation and must not become a paid fallback. |
| `worker/analysis.py:13` and `:36` | Existing analysis context requires a fresh account commission quote. A credential-free service cannot call this function unchanged or invent a fee rate. |
| `worker/core.py:345` and `:382` | Reuse the deterministic risk calculations and preflight policy; the agent cannot grant live authority or override risk or model levels. |
| `worker/robot.py:202`, `:295`, `:401` | `Coordinator.tick()` reaches the order gateway before research, and OFF stops both. It is not a research-only entry point. |
| `worker/api_service.py:174` | The current actor constructs the combined coordinator and NeuroAPI provider. Do not reuse this actor for the new research service. |
| `worker/order_gateway.py:1` and `Dockerfile:5` | Public source contains a placeholder gateway copied into the worker image. Rebuilding the live worker from this baseline could replace its private gateway. |
| `compose.yaml` | Only worker and proxy exist; the worker has private exchange/provider secrets and a financial data volume. None belong in the new service. |

No `worker/ai_agent.py`, `worker/neurobrowser.py`, `browser_runtime/`, or browser
Compose overlay exists in that investigated main tree. Previously prepared
browser/provider code must be located, pinned by full commit and artifact SHA,
and reconciled against the actual browser interface before reuse. Null artifact
pins in this contract are deliberate blockers, not placeholders accepted for a
release.

## Service and authority contract

`harun-neurobro-agent` gets its own Compose project, UID/GID 10002, research
database and bounded artifact storage. It starts PAUSED. It gets no Binance or
paid-LLM credentials, NeuroAPI key, production financial database, production
browser profile, order gateway, Docker socket, host filesystem mount, public
port, or production service control.

This initial contract covers a first installation only. Actual predeployment
absence of this service must be verified; a PRESENT or unknown inventory cannot
be treated as compatible. An operations planner may describe a blocked future
upgrade, but restoring a previous owned image/configuration is outside this
version's rollback authority and requires a separately reviewed contract.

The public Binance Futures adapter supplies market evidence. Neurobro WEB
determines the setup, with authentic response and matching numeric/chart
evidence. The agent coordinates research and preserves the original ENTRY, TP
and SL; it cannot invent or override those values or risk. A rejection stays a
rejection. Proposal records remain `UNVERIFIED_RESEARCH`, never
`READY_FOR_EXECUTION` or `order_intents`.

Only the existing RiskManager may grant live execution after an authentic setup
and fresh risk preflight while ROBOT is ON. Execution continues through the one
existing private gateway. This contract creates no gateway, live executor,
financial write route, order cancellation, wallet, funding or transfer authority.
The existing worker, ROBOT state and execution workflow remain separate.

Account exposure and commission data require a root-owned, sanitized read-only
bridge. Evidence must bind source, symbol, observed time, expiry and digest, be
no older than 120 seconds, and contain no credentials. Missing, stale or
ambiguous evidence pauses work as unverified; there is no assumed fallback fee.
The bridge has not been implemented or authenticated by this change.

## Next code work, before a release can be proposed

1. Add `worker/research_agent_service.py` as a separate entry point. It must not
   import or call `Coordinator.tick()`, `OrderGateway`, or financial store
   mutation paths. Persist operations, rate/backoff, unknown outcomes, progress,
   and proposal evidence in its own database. Start paused even after restart.
2. Add a pure proposal/evidence contract, for example
   `worker/research_agent_contract.py`, to bind the actual Neurobro operation and
   response digest to the same symbol, original levels, fresh market data and
   matching closed 1h/15m screenshot evidence. Permit at most one critical
   clarification; never alias an old response as a fresh reply or replay an
   unknown browser operation.
3. Reconcile the prepared browser/provider artifacts and implement a narrow
   authenticated RPC client against the verified browser service. Pin source
   commit and artifact SHA. Do not reuse the existing NeuroAPI client, pass
   credentials/profile volumes to the agent, or assume that a healthy browser is
   logged in. Keep chat disabled until separately authorized.
4. Implement and separately review the sanitized evidence bridge in the existing
   backend only where necessary. The bridge reads confirmed account/fee facts;
   it grants no write capability and never exposes the backend's keys or ledger.
   Map fresh evidence into the existing pure risk calculations, without copying
   exchange signing or execution into the agent.
5. Add a dedicated agent Dockerfile and Compose overlay, such as
   `deploy/Dockerfile.neurobro-agent` and `compose.neurobro-agent.yaml`. They must
   implement UID/GID 10002, own project/storage, read-only root filesystem,
   dropped capabilities, no-new-privileges, default PAUSED state, no production
   mounts/secrets, no published port, and the exact bounded resource policy.
   The declared limits here are requirements, not verified runtime enforcement.
6. Implement local health/status from actual process and task state with the
   required identity, process generation and short expiry. Health must not send
   market, screening, chat, Neurobro, Binance or credential-loading requests.
   Do not report a working/healthy runtime merely because this JSON validates.
7. Test the complete runtime with mocked providers and denied network access,
   including malformed/stale evidence, unknown outcomes, resource yield,
   restart/idempotence, one clarification, prohibited imports/financial writes,
   and paused health. Review the concrete source commit and image digest, actual
   isolation and compatibility before requesting deployment authorization.

The declared per-tick work and concurrency limits bound resource consumption;
they do not impose a global candidate-count cap. Unknown browser outcomes pause
without replay. The future browser/RPC boundary still needs independently
verified resource and timeout enforcement.

## Health and rollback boundaries

Health reports identity and actual observations only, including the full source
commit, image digest, runtime-configuration digest and process generation. Those
reported bindings must match independently inspected release/runtime metadata;
reporting a digest is not provider verification or host attestation. It has at most 60-second
freshness and 16 KiB output. It does not activate research or authorize a release.
This change contains no service executable or health endpoint.

Rollback is limited to stopping/removing the new service in its own project,
while preserving its research evidence, after proving the service was absent
before installation and the new instance belongs to this reviewed release.
Missing ownership or absence proof blocks rollback automation. It must not touch existing containers,
ROBOT state, financial databases, browser profiles or credentials. Rollback
automation is not implemented or authorized here.

## Local validation

Run `python3 scripts/validate_neurobro_agent_contract.py` and
`python3 -m unittest discover -s tests -p test_neurobro_agent_contract.py -v`.
The validator uses only the standard library, validates bounded JSON with closed
fields and strict scalar types, and prints a sanitized preparation result. It
does not import application modules, start a container, contact a service, read
credentials or use privileged host interfaces. All release blockers remain in
its output. No UI or worker source change, build, pull, deployment, live chat or
Binance request is part of this preparation.
