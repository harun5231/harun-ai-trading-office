# Native Neurobro delegated dialogue runtime

The production browser speaks a structured research protocol and owns its persistent Chromium page, chart attachments, ROBOT dispatch checks and source receipts. The delegated agent adds GPT-5 Mini reasoning over that conversation. It does not start a second trading coordinator or replace the existing Risk Manager or LIVE gateway.

`agent.dialogue_service` runs as UID 10002 in a separate container. Its private Unix socket authenticates the native browser UID 10001 and an application token. The model can request one brief clarification or stop; it cannot supply executable prices, quantities or orders. Conversation and ambiguous-operation state are durable in the agent’s own SQLite database. Unknown provider outcomes are not automatically replayed.

`agent.native_browser_hook` wraps the original DOM bridge. It preserves the initial user prompt and original chart checks, sends at most one additional question in the same conversation, then returns the final Neurobro answer through the original parser, response identifier and receipt path. An unset hook configuration leaves the existing bridge unchanged. `scripts/patch_native_browser_hook.py` authenticates the exact deployed actor SHA and modifies one construction expression; every other byte is preserved. The original browser/profile is retained.

`agent.native_market_context` copies the verified native public Binance Futures mark and recent 1H/15M candles. It excludes private account and risk settings. Its provenance describes public data only; screenshot evidence remains with the native browser.

The derived Docker image pins the previously published agent base by digest. It does not rebuild the production worker. The manual owner-only image workflow publishes a source-pinned digest after actual offline daemon, shutdown, restart and crash-recovery checks. It performs no deployment.

The version 2 runtime configuration is an immutable file with exactly these fields: `schema_version`, `mode`, `data_directory`, `socket_path`, `dialogue_token_file`, `openai_key_file`, and `model`. Health binds its digest to the actual file and reports missing keys, changed configuration or broken local transport as BLOCKED. Local health never claims a verified OpenAI or Neurobro connection merely from file presence.

Deployment requires a dedicated OpenAI key stored outside source control and approval of a specific immutable release. Socket and token mounts are application-only; no Binance credentials, production financial database, Docker socket or Chromium profile are mounted in the agent. The agent requires supplementary group 10001 for the shared socket. Agent and browser token files contain the same random application token under their separate UIDs.

## Current verification boundary

Unit/regression tests and isolated native-AST fixtures exercise the code without production chats or orders. A published image is not evidence that the native hook is deployed. The production worker still requires an approved exact-source update for unlimited HOLD replacement, and its pending-LIMIT review must be reconciled with the native provider/gateway before claiming the complete 24/7 workflow. ROBOT remains OFF during installation and verification. No real provider call or LIVE order is used as an installation test.
