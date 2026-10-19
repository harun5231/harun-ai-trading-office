"""Sanitize a cached, release-pinned agent observation for Office's existing API.

The reviewed production adapter may call ``append_agent_status`` when composing
``/office/status``. This module performs no IO, provider request, model request,
financial operation or robot setting change. It never treats a healthy process
as proof that its browser, model or execution integration is ready.
"""
from datetime import datetime, timezone
import math
import re
import time


SERVICE = "harun-neurobro-agent"
STATES = frozenset({"STARTING", "RUNNING", "IDLE", "PAUSED", "WAITING", "BUSY",
                    "BLOCKED", "REQUIRES_REVIEW"})
DEPENDENCY_AGES = {"backend": 30, "browser": 1200, "model": 1200}
PHASES = frozenset({"IDLE", "BLOCKED", "COMPLETE", "SCREEN_REQUEST", "SCREEN_DECIDE", "SCREEN_FOLLOWUP",
                   "ANALYSIS_REQUEST", "ANALYSIS_DECIDE", "ANALYSIS_FOLLOWUP",
                   "PENDING_REQUEST", "PENDING_DECIDE", "PENDING_FOLLOWUP"})
REASONS = frozenset({"", "ROBOT_OFF", "LOGIN_REQUIRED", "NEUROBRO_LOGIN_REQUIRED",
                    "NO_CAPACITY_OR_REVIEW_DUE", "DAILY_MODEL_BUDGET_EXHAUSTED",
                    "UNCONFIRMED_OPERATION_NOT_REPLAYED", "ADAPTER_OPERATION_UNVERIFIED",
                    "CONFIRMED_NOT_SENT", "SETUP_FORWARDED", "PENDING_TAHAN", "PENDING_BATALKAN"})
IMAGE = re.compile(r"ghcr\.io/harun5231/harun-neurobro-agent@sha256:[a-f0-9]{64}")


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def unavailable(reason="AGENT_STATUS_UNAVAILABLE"):
    return {"schema_version": 1, "service": SERVICE, "source": "LOCAL_AGENT_HEALTH",
            "status": "UNAVAILABLE", "checked_at": None, "state": "UNKNOWN", "phase": None,
            "reason": reason, "integration_verified": False,
            "dependencies": {name: {"verified": False, "checked_at": None}
                             for name in DEPENDENCY_AGES}}


def sanitize_agent_health(health, *, expected_release, now=None):
    """Return a small closed snapshot; caller supplies separately approved pins.

    ``expected_release`` comes from the trusted release catalog, not the health
    response. The cached body must be at most 60 seconds old and its controller
    must have made progress within 180 seconds. Missing or bad optional telemetry
    never makes the existing account, risk controls or login flow unavailable.
    """
    current = time.time() if now is None else now
    if (not _number(current) or not 0 <= current < 253402300799
            or type(health) is not dict or type(expected_release) is not dict
            or set(expected_release) != {"source_commit", "image", "runtime_config_sha256"}):
        return unavailable()
    commit, image, config = (expected_release[key] for key in
                              ("source_commit", "image", "runtime_config_sha256"))
    if (type(commit) is not str or not re.fullmatch(r"[a-f0-9]{40}", commit) or commit == "0"*40
            or type(image) is not str or not IMAGE.fullmatch(image) or image.endswith("0"*64)
            or type(config) is not str or not re.fullmatch(r"[a-f0-9]{64}", config) or config == "0"*64):
        return unavailable()
    if (health.get("service") != SERVICE or health.get("compose_project") != SERVICE
            or health.get("source_commit") != commit or health.get("image_digest") != image
            or health.get("runtime_config_sha256") != config
            or type(health.get("uid")) is not int or health["uid"] != 10002
            or type(health.get("gid")) is not int or health["gid"] != 10002
            or health.get("runtime_implemented") is not True
            or health.get("mode") != "ROBOT_GATED_COORDINATION"
            or type(health.get("state")) is not str or health["state"] not in STATES
            or type(health.get("process_generation")) is not str
            or not re.fullmatch(r"[a-f0-9]{32}", health["process_generation"])):
        return unavailable()
    observed, expires, tick = (health.get(name) for name in
                                ("observed_at", "expires_at", "last_tick_at"))
    if (not all(_number(value) for value in (observed, expires, tick))
            or not 0 <= current-observed < 60 or not observed < expires <= observed+60
            or current >= expires or not 0 <= observed-tick <= 180):
        return unavailable("AGENT_STATUS_STALE")
    phase, reason = health.get("phase"), health.get("reason", "")
    if (type(phase) is not str or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", phase)
            or type(reason) is not str or not re.fullmatch(r"[A-Z0-9_]{0,100}", reason)):
        return unavailable()
    dependencies = {}
    supplied = health.get("dependencies")
    supplied = supplied if type(supplied) is dict else {}
    for name, maximum in DEPENDENCY_AGES.items():
        item = supplied.get(name)
        item = item if type(item) is dict else {}
        checked = item.get("observed_at")
        verified = (item.get("verified") is True and item.get("fresh") is True
                    and _number(checked) and 0 <= current-checked <= maximum)
        dependencies[name] = {"verified": bool(verified),
                              "checked_at": _stamp(checked) if verified else None}
    return {"schema_version": 1, "service": SERVICE, "source": "LOCAL_AGENT_HEALTH",
            "status": "AVAILABLE", "checked_at": _stamp(observed), "state": health["state"],
            "phase": phase if phase in PHASES else None,
            "reason": reason if reason in REASONS else "AGENT_STATE_REPORTED",
            "integration_verified": (health.get("integration_verified") is True
                                     and all(item["verified"] for item in dependencies.values())),
            "dependencies": dependencies}


def append_agent_status(office_snapshot, cached_health, *, expected_release, now=None):
    """Add telemetry without changing the existing account, robot or research data."""
    if type(office_snapshot) is not dict:
        raise ValueError("OFFICE_SNAPSHOT_REQUIRED")
    return {**office_snapshot,
            "agent": sanitize_agent_health(cached_health, expected_release=expected_release, now=now)}
