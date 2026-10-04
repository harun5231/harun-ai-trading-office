#!/bin/sh
set -eu
export OFFICE_DESKTOP_PASSWORD_HASH="$(cat /run/secrets/desktop_hash)"
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
