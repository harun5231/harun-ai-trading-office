# Final pre-deployment audit — 2026-10-05

Scope: small corrections on 2aedc141; no office, trading rules, deployment,
monitoring, or ledger schema rewrite. No actual provider key or paid request was
used. VPS deployment and authenticated integration remain unverified.

## Official documentation checked

- https://neuroapi.neurobro.ai/docs/quickstart
- https://neuroapi.neurobro.ai/docs/best-practices/examples
- https://neuroapi.neurobro.ai/docs/guides/idempotency
- https://neuroapi.neurobro.ai/docs/pricing

The production `/docs/api/agent-ask` reference page was not retrievable by the
audit browser. The official production Quickstart and Examples pages provide the
request/response fields used here; no speculative fields were introduced.

| Contract | Implementation |
| --- | --- |
| Base URL | `https://api.neurobro.ai/api/v1` |
| Ask | `POST /agent/ask`, `X-API-Key`, JSON body |
| Health | `GET /health`, authenticated true and status healthy required; prefix discarded |
| Starter | Explicit `mode: smart`; no max fallback |
| Structured output | Object-root `output_schema`, `stream: false`; consume object `output`, require answer null and mode smart |
| Market context | Separate user `message_history.content` JSON string; literal prompt untouched |
| Smart conformance | Provider best-effort; strict local schema/setup validation; invalid response stops, never repaired |
| Automatic retries | Only received HTTP 429/503; three total attempts, identical body |
| Retry-After | Documented integer seconds honored up to 30s; longer or unrecognized values stop for review, never retry early |
| Unknown outcome | Persist PENDING before call; timeout/network exception becomes NEEDS_REVIEW; no replay on restart |

## Idempotency discrepancy and conservative policy

Examples calls `/agent/ask` non-idempotent and recommends retries only for 429/503.
The separate official Idempotency guide explicitly documents Idempotency-Key,
24-hour replay, and 409 in-flight retries. Thus the header is documented, contrary
to the concern in the request. Nevertheless this implementation deliberately
omits it and never retries 409, following the user's stricter pre-deployment
policy. Duplicate protection depends on local operation/body hashes, persisted
PENDING/COMPLETE/NEEDS_REVIEW, and the daily cycle claim, not provider replay.
The existing nullable idempotency database column is retained for compatibility;
new rows leave it null. Existing records/audit history are not modified.

## Prompt bytes

The latest requested analysis literal is visibly one paragraph: there are no
literal line-feed characters between its words in the received message. Its
UTF-8 bytes are preserved exactly, with no leading/trailing LF. This audit does
not invent where line breaks were intended. Both prompts have independent exact
byte assertions. A separate transport regression verifies embedded LF would be
preserved verbatim; it is not presented as a multiline version of the user prompt.

## Safety and verification

All original 56 active tests retained. Additional regressions cover exact bytes,
newlines in transport, 429/503 bounded retry, 409 and other non-retry statuses,
committed PENDING before network, timeout after a retryable response, restart
protection, sensitive-error sanitization, Retry-After, smart structured output,
and Binance public GET-only calls without credentials.

Existing tests continue to cover risk <=5 USDT, RR >=2, unchanged quantities,
CROSS/75x simulation, persistent daily cap of two, race protection, secret
isolation, paper monitoring and live-mode rejection. Binance uses only public
exchangeInfo/time/klines/premiumIndex. No order endpoint, signer, Binance key,
or live execution switch was added.

Run all including the two development-only Three.js UI checks:

```sh
OFFICE_UI_TESTS=1 PLAYWRIGHT_CHROMIUM_EXECUTABLE=/tmp/chromium python -m unittest discover -s tests -v
```

Deployment remains `sudo python3 deploy/setup.py` (silent getpass to private VPS
secret file), followed by `bash deploy/update-api.sh` and
`docker compose exec -T worker python -m worker api-check`.
The health check sends no prompt. The build uses Docker Hub's Python base, not MCR.
Persistent volumes and audit logs remain intact; no `down -v` or secret cleanup.
