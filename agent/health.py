"""Local bounded health observations; these handlers never probe trading services."""
import hashlib
import json
import os
import math
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MAX_RESPONSE_BYTES = 16384


class HealthServer:
    def __init__(self, controller, *, config_bytes=b"", host="0.0.0.0", port=8765,
                 clock=time.time):
        self.controller = controller
        self.clock = clock
        self.generation = uuid.uuid4().hex
        self.config_sha256 = hashlib.sha256(config_bytes).hexdigest()
        self.started_at = clock()
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in ("/health", "/health/live", "/health/ready"):
                    self.send_error(404)
                    return
                try:
                    value = parent.observe()
                    # Running in OFF/PAUSED is a valid liveness observation, but it
                    # does not prove browser, model, or the production bridge.
                    ready = value.get("state") in ("RUNNING", "IDLE", "PAUSED") and value.get("integration_verified") is True
                    status = 200 if self.path != "/health/ready" or ready else 503
                    raw = json.dumps(value, sort_keys=True, allow_nan=False).encode()
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ValueError()
                except Exception:
                    status, raw = 503, b'{"service":"harun-neurobro-agent","status":"HEALTH_UNAVAILABLE"}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="agent-health", daemon=True)

    def observe(self):
        value = self.controller.health_status()
        if type(value) is not dict:
            raise ValueError("HEALTH_STATE_INVALID")
        # Do not serialize arbitrary runtime metadata, errors, chat, or settings.
        safe = {key: value[key] for key in ("state", "phase", "reason", "last_tick_at") if key in value}
        dependencies = {}
        observed = value.get("dependencies", {})
        for name in ("backend", "browser", "model"):
            item = observed.get(name, {}) if type(observed) is dict else {}
            item = item if type(item) is dict else {}
            clean = {key: item.get(key) is True for key in ("verified", "fresh")}
            checked = item.get("observed_at")
            if type(checked) in (int, float) and math.isfinite(checked):
                clean["observed_at"] = checked
            for key in ("source_sha256", "revision", "model", "transport"):
                text = item.get(key)
                if type(text) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", text):
                    clean[key] = text
            dependencies[name] = clean
        observed_at = self.clock()
        safe.update(service="harun-neurobro-agent", compose_project="harun-neurobro-agent",
                    uid=os.geteuid(), gid=os.getegid(), mode="ROBOT_GATED_COORDINATION", runtime_implemented=True,
                    source_commit=os.environ.get("HARUN_SOURCE_COMMIT", "UNVERIFIED"),
                    image_digest=os.environ.get("HARUN_IMAGE_DIGEST", "UNVERIFIED"),
                    runtime_config_sha256=self.config_sha256,
                    process_generation=self.generation, observed_at=observed_at, expires_at=observed_at+60,
                    started_at=self.started_at, dependencies=dependencies,
                    integration_verified=all(item["verified"] and item["fresh"] for item in dependencies.values()),
                    deployment_authorized=False)
        return safe

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
