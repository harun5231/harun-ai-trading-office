# Sanitized request diagnostics

Run `docker compose exec --user 10001:10001 -T worker python -m worker diagnostics`.
This command opens the existing SQLite database with `mode=ro` and `query_only`;
it never loads a provider key, invokes network requests, claims a cycle, migrates,
resets records or replays research. It selects only operation/state/attempts/code.
Unexpected operation/state/code strings are redacted or replaced with safe codes.
Missing failure codes on old NEEDS_REVIEW rows show LEGACY_REASON_UNAVAILABLE.
The real cause of the two 2026-10-05 failures cannot be reconstructed.

Normal worker initialization adds one nullable failure_code column transactionally.
Existing records, state, attempts, hashes, cached validated results, trades and
cycles are unchanged. Repeated migration is safe. Never reset or replay the
2026-10-05 cycle to obtain a diagnostic.

New failures record only an allowlisted category: HTTP status, NETWORK_UNCERTAIN,
INVALID_RESPONSE_ENVELOPE, INVALID_OUTPUT_SCHEMA, INVALID_SETUP_SCHEMA,
INVALID_SETUP_SYMBOL_SIDE, INVALID_NUMERIC_TYPE/VALUE, INVALID_ENTRY_TP_SL,
RISK_REWARD_BELOW_2 or VALIDATION_REJECTED.
Specific existing screening/contract-filter rejection codes are also allowlisted.
HTTP failures/uncertain outcomes remain NEEDS_REVIEW. HTTP 200 with invalid setup
also stops, but has a validation code rather than a network failure code.
A successful structured setup later rejected by Risk Manager stays COMPLETE
(provider finished), with e.g. RISK_ABOVE_5. No provider replay is enabled.
Diagnostics contain no provider body, output, prompt, market data, key or headers.
The existing successful-result cache stores only reconstructed validated fields;
raw envelopes, extra fields, prose and rejected output are never added to it.

## Numeric and RR audit (2026-10-05)

Official https://neuroapi.neurobro.ai/docs/best-practices/examples shows JSON
Schema `number` fields for support/resistance. Numbers need not be JSON strings.
Smart structured output remains best-effort, so local validation is mandatory.
We now request numeric JSON fields and decode NeuroAPI decimals directly to
Decimal (no binary float intermediate). Binance decoding is unchanged. Boolean,
string/range/prose, non-finite, non-positive or unbounded numbers are rejected.
Validated cache serialization preserves numeric values exactly. A changed schema
cannot cause a completed/uncertain old operation to be replayed: operation/body
hash and cycle protection still fail closed.

ENTRY/TP/SL must have correct LONG/SHORT ordering. Actual reward distance must be
at least twice stop distance, compared exactly without division rounding. Declared
RR must be a positive numeric ratio, but need not equal the calculated ratio.
It does not override actual level-based RR, including a rounded declaration 1.9999.
Thus declared 2 with actual 2.0001 passes; actual 1.9999 never passes. Provider prices and audit quantity are preserved; execution quantity is separately
computed by Risk Manager, retaining the <=5 USDT price-risk
limit, contract filters, CROSS/75x paper plan and persistent two-slot daily cap.

Prompts are unchanged byte-for-byte. Only public Binance GETs and DRY RUN remain.
Testing uses synthetic responses; no VPS key or paid request is used here.
