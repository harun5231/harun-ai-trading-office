#!/usr/bin/env bash
# Run once for the fingerprinted existing VPS; never enable ROBOT ON here.
set -euo pipefail
umask 077

HARUN_PROJECT=/root/harun-ai-trading-office
cd "$HARUN_PROJECT"

harun_assert_robot_off() {
  docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - <<'PY'
import sqlite3
try:
    db = sqlite3.connect('file:/data/trading/ledger.sqlite3?mode=ro', uri=True, timeout=5)
    try:
        db.execute('PRAGMA query_only=ON')
        row = db.execute('SELECT enabled FROM robot_settings WHERE id=1').fetchone()
    finally:
        db.close()
except Exception:
    raise SystemExit('ROBOT_OFF_CHECK_FAILED') from None
if row != (0,):
    raise SystemExit('ROBOT_OFF_REQUIRED')
print('ROBOT_OFF_CONFIRMED')
PY
}

harun_assert_robot_off
HARUN_COMPOSE_FILES="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' harun-office-worker-1)"
if [ "$HARUN_COMPOSE_FILES" != "$HARUN_PROJECT/compose.yaml" ]; then
  printf '%s\n' COMPOSE_CONFIGURATION_MISMATCH >&2
  exit 1
fi

git fetch origin main
HARUN_REVISION="$(git --no-optional-locks rev-parse origin/main)"
HARUN_STAGING="$(mktemp -d /root/harun-ai-trading-office-order-fix.XXXXXX)"
git show "$HARUN_REVISION:deploy/live_gateway_template.py" > "$HARUN_STAGING/live_gateway_template.py"
git show "$HARUN_REVISION:deploy/repair_existing_gateway.py" > "$HARUN_STAGING/repair_existing_gateway.py"
git show "$HARUN_REVISION:deploy/update_existing_worker.py" > "$HARUN_STAGING/update_existing_worker.py"
git show "$HARUN_REVISION:deploy/inspect_vps_gateway.py" > "$HARUN_STAGING/inspect_vps_gateway.py"

# Inspect both plans before either tool writes source. Each tool independently
# checks OFF again; SDK and other protected files are never imported by them.
python3 -I -B -S "$HARUN_STAGING/repair_existing_gateway.py" --project "$HARUN_PROJECT" --template "$HARUN_STAGING/live_gateway_template.py"
python3 -I -B -S "$HARUN_STAGING/update_existing_worker.py" --project "$HARUN_PROJECT"

python3 -I -B -S "$HARUN_STAGING/repair_existing_gateway.py" --project "$HARUN_PROJECT" --template "$HARUN_STAGING/live_gateway_template.py" --apply
python3 -I -B -S "$HARUN_STAGING/update_existing_worker.py" --project "$HARUN_PROJECT" --apply
docker compose -f compose.yaml config --quiet
docker compose -f compose.yaml build worker
harun_assert_robot_off
docker compose -f compose.yaml stop -t 660 worker
docker compose -f compose.yaml up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 worker
harun_assert_robot_off
docker compose -f compose.yaml ps
HARUN_WORKER_SHA="$(python3 -I -B -S -c 'import hashlib, json; from pathlib import Path; names=("order_gateway.py", "robot.py", "binance_private.py", "market.py", "neuroapi.py", "research_guard.py"); print(json.dumps({name: hashlib.sha256((Path("worker") / name).read_bytes()).hexdigest() for name in names}))')"
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - "$HARUN_WORKER_SHA" <<'PY'
import hashlib, json, sqlite3, sys
from pathlib import Path
expected = json.loads(sys.argv[1])
names = ('order_gateway.py', 'robot.py', 'binance_private.py', 'market.py', 'neuroapi.py', 'research_guard.py')
if set(expected) != set(names):
    raise SystemExit('WORKER_SOURCE_CHECK_INVALID')
for name in names:
    raw = (Path('/app/worker') / name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected[name]:
        raise SystemExit('WORKER_SOURCE_MISMATCH:' + name)
print('GATEWAY_SOURCE_MATCH')
print('WORKER_SOURCE_MATCH')
db = sqlite3.connect('file:/data/trading/ledger.sqlite3?mode=ro', uri=True, timeout=5)
try:
    db.execute('PRAGMA query_only=ON')
    rows = db.execute('SELECT state, COUNT(*) FROM order_intents GROUP BY state').fetchall()
finally:
    db.close()
allowed = {'READY_FOR_EXECUTION', 'EXECUTION_BLOCKED', 'SUBMITTING', 'ENTRY_PENDING', 'POSITION_PROTECTED', 'CLOSED', 'REJECTED', 'NEEDS_REVIEW'}
counts = {}
for state, count in rows:
    label = state if state in allowed else 'OTHER'
    counts[label] = counts.get(label, 0) + count
print(json.dumps({'order_intent_counts': counts}, sort_keys=True))
PY
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker.robot_status
printf 'Review scripts: %s\n' "$HARUN_STAGING"
