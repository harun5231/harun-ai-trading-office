#!/bin/bash
set -euo pipefail
umask 077
# Build first; failure leaves the existing running worker and volumes intact.
docker compose build worker
docker compose config --quiet
docker compose stop -t 120 worker
# No volume removals, no secret cleanup. Same project/volume names as before.
docker compose up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 worker
docker compose up -d --no-deps --force-recreate --wait --wait-timeout 90 proxy
docker compose ps
