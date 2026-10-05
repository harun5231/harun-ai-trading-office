# API migration manifest

Runtime NeuroAPI Starter/smart + Binance Futures public market data; DRY RUN only.
No private key was provided to the development environment. No real NeuroAPI
request or Binance order was sent for validation. Full local suite: 56 passing
tests with the two dev-only 3D/UI tests enabled. Obsolete provider-login tests were
removed; ledger/risk/monitor tests were retained and adjusted for ACCEPT/REJECT.
The Three.js inline module is byte-identical to the previous main; index.html
changes only its dashboard integration script reference.

The new image requires Docker Hub for the official Python base; no MCR dependency.
Actual VPS build, authenticated provider request and deployment still require
running the documented Termius commands. Existing private files, state and audit
logs are not automatically removed. See DOCKER.md for one-time ledger migration
and the optional separate archive command. No Git history rewriting.

## modified

- `.dockerignore`
- `.env.example`
- `.gitignore`
- `Dockerfile`
- `README.md`
- `assets/workflow.js`
- `compose.yaml`
- `deploy/Caddyfile.docker`
- `deploy/DOCKER.md`
- `deploy/README.md`
- `deploy/container_boot.py`
- `deploy/setup.py`
- `index.html`
- `tests/test_worker.py`
- `worker/__main__.py`
- `worker/core.py`
- `worker/health.py`
- `worker/prompts.py`
- `worker/requirements.txt`
- `worker/workflow.py`

## added

- `assets/neuroapi.js`
- `deploy/API_MIGRATION.md`
- `deploy/archive-retired.py`
- `deploy/update-api.sh`
- `tests/test_api_deployment.py`
- `tests/test_neuroapi.py`
- `tests/test_office_ui.py`
- `worker/api_service.py`
- `worker/http_client.py`
- `worker/market.py`
- `worker/neuroapi.py`
- `worker/state.py`

## deleted

- `assets/neurobro.js`
- `deploy/Caddyfile.example`
- `deploy/Dockerfile.local`
- `deploy/SELECTOR_DISCOVERY.md`
- `deploy/discover-selectors.sh`
- `deploy/install_novnc.py`
- `deploy/novnc/office-mobile.css`
- `deploy/novnc/office-mobile.js`
- `deploy/proxy-start.sh`
- `deploy/start-desktop.sh`
- `deploy/supervise.py`
- `deploy/update-portrait.sh`
- `tests/test_deployment.py`
- `tests/test_desktop.py`
- `tests/test_discovery_diagnostic.py`
- `tests/test_manual_login.py`
- `tests/test_mobile_manual.py`
- `tests/test_screening.py`
- `tests/test_screening_browser.py`
- `tests/test_selector_discovery.py`
- `tests/test_semantic_inventory.py`
- `tests/test_session_service.py`
- `tests/test_stale_singleton.py`
- `tests/test_structural_discovery.py`
- `worker/browser-config.example.json`
- `worker/browser.py`
- `worker/desktop.py`
- `worker/discovery_preflight.py`
- `worker/manual_browser.py`
- `worker/process_safety.py`
- `worker/profile_owner.py`
- `worker/runner.py`
- `worker/screening.py`
- `worker/selector_diagnostic.py`
- `worker/selector_discovery.py`
- `worker/selector_evidence.py`
- `worker/semantic_inventory.py`
- `worker/session_service.py`
- `worker/structural_discovery.py`

