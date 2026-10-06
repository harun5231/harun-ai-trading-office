#!/bin/bash
set -euo pipefail
umask 022
# Build first; failure leaves the existing running worker and volumes intact.
docker compose build worker
docker compose config --quiet
# Allow the worker to drain an active provider request before replacement.
docker compose stop -t 660 worker
# No volume removals, no secret cleanup. Same project/volume names as before.
docker compose up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 worker
docker compose up -d --no-deps --force-recreate --wait --wait-timeout 90 proxy
docker compose ps
# Read-only coordinator/account/lock diagnostics; no paid research or state reset.
docker compose exec --user 10001:10001 -T worker python -m worker.robot_status
