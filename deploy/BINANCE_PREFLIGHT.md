# Binance USD-M read-only preflight

`python -m worker binance-check` performs only these GETs at the fixed HTTPS origin
`https://fapi.binance.com`:

- `/fapi/v1/time` (public, no credential header)
- `/fapi/v3/account` (signed account information)
- `/fapi/v1/accountConfig` (signed canTrade, dualSidePosition, multiAssetsMargin)

The client signs the exact URL-encoded query with HMAC SHA256, uses X-MBX-APIKEY,
recvWindow=5000, and advances Binance server time with monotonic elapsed time.
Clock samples taking over two seconds and samples older than 30 seconds fail closed.
There is no clock-error retry, automatic resubmission, account mutation, order,
leverage/margin change, transfer or withdrawal. Private transport has a strict
origin/path/query allowlist, hardcoded GET and no body/method parameter. Redirects
and environment proxies are disabled. The shared public-data transport also
rejects Binance non-GET methods and paths outside its market-data allowlist.

Output contains only status, DRY_RUN/live_execution=false, Futures availability,
can_trade, ONE_WAY/HEDGE, multi_assets_margin and optional numeric USDT balances.
Account canTrade is reported, not proof of every API-key trading permission; no
order is used to test permissions. No account identity, positions, provider error
message, signed query or credentials are printed/persisted. There is no database,
snapshot, dashboard integration, or scheduled invocation of this command.

Error mapping: -1021 -> BINANCE_CLOCK_ERROR; -1011 -> BINANCE_IP_RESTRICTED;
-1002 -> BINANCE_PERMISSION_DENIED; -1022/-2014/-2015 or HTTP 401 ->
BINANCE_AUTH_FAILED. Binance -2015 conflates key/IP/permission problems, so the
worker does not guess which caused it. HTTP 403 without a specific recognized
code may be WAF-related and yields BINANCE_ACCOUNT_UNAVAILABLE. Unknown/malformed
responses fail closed. Errors never echo provider messages or signed URLs.

## Secrets and deployment

Run `python3 deploy/setup.py` from the installation directory in Termius.
Only missing Binance secret files prompt for hidden input using getpass; a
terminal that cannot hide input is rejected. Existing NeuroAPI key contents and
nonempty Binance credentials are preserved. Credentials are never shell arguments
or .env values. Private root/secrets directories use 0700; Binance files use 0600,
atomic replace and fsync. Secret paths must be outside the checkout.

Compose mounts binance_api_key and binance_api_secret from OFFICE_PRIVATE_DIR.
The root bootstrap copies them into private tmpfs `/run/office`, mode 0600 and
owned by uid 10001, then drops privileges. Environment contains only
BINANCE_API_KEY_FILE and BINANCE_API_SECRET_FILE paths. Missing/invalid/insecure
files yield BINANCE_NOT_CONFIGURED; no credential-value environment fallback.
The normal worker filesystem remains read-only and persistent data is untouched.

Rebuild/recreate only worker after setup, keep OFFICE_AUTO_DRY_RUN=false, then run
`docker compose exec -T worker python -m worker binance-check` explicitly.
No screening/analysis is run by the preflight. Do not remove volumes or reset the
ledger. Work tests use synthetic fixtures; actual account authentication must be
verified on the trusted VPS IP. Do not paste secrets into chat or dashboard.

## Official references checked 2026-10-05

- https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account
- https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info
- https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/error-code
