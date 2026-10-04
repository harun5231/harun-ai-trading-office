#!/bin/sh
# Run as a dedicated unprivileged user on a PRIVATE Linux server.
set -eu
umask 077
: "${OFFICE_VNC_PASSWORD_FILE:?Set private VNC password file outside repository}"
: "${OFFICE_DESKTOP_STATE:?Set private desktop runtime directory outside repository}"
: "${OFFICE_NOVNC_WEB:=/usr/share/novnc}"
python - "$OFFICE_DESKTOP_STATE" "$OFFICE_VNC_PASSWORD_FILE" <<'CHECK'
from pathlib import Path
import sys
repo=Path.cwd().resolve()
for value in sys.argv[1:]:
    path=Path(value).expanduser().resolve()
    if path==repo or repo in path.parents:
        raise SystemExit('Desktop state/password file must be outside the repository')
CHECK
[ -f "$OFFICE_VNC_PASSWORD_FILE" ] || exit 1
chmod 600 "$OFFICE_VNC_PASSWORD_FILE"
mkdir -p "$OFFICE_DESKTOP_STATE"
chmod 700 "$OFFICE_DESKTOP_STATE"
export DISPLAY=:99
export XAUTHORITY="$OFFICE_DESKTOP_STATE/Xauthority"
xauth -f "$XAUTHORITY" add "$DISPLAY" . "$(openssl rand -hex 16)"
children=''
cleanup() { [ -z "$children" ] || kill $children 2>/dev/null || true; }
trap cleanup EXIT INT TERM
Xvfb "$DISPLAY" -screen 0 1024x768x24 -nolisten tcp -auth "$XAUTHORITY" &
children="$children $!"
# Bounded readiness, not a public unauthenticated desktop.
count=0
until xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; do
    count=$((count+1)); [ "$count" -lt 20 ] || exit 1; sleep 1
done
x11vnc -display "$DISPLAY" -auth "$XAUTHORITY" -localhost -rfbport 5900 -rfbauth "$OFFICE_VNC_PASSWORD_FILE" -forever -shared -noxdamage >/dev/null 2>&1 &
children="$children $!"
websockify --web "$OFFICE_NOVNC_WEB" 127.0.0.1:6080 127.0.0.1:5900 >/dev/null 2>&1 &
children="$children $!"
# API/server inherits the exact same desktop and displays its Chromium here.
python -m worker.session_service "$@" &
service_pid=$!;children="$children $service_pid"
# Stop the stack if any required desktop component dies.
while :; do
    for child in $children; do kill -0 "$child" 2>/dev/null || exit 1; done
    sleep 5
done
