"""Opt-in actual Chromium/CDP test, entirely offline and with an isolated profile.

Run HARUN_BROWSER_INTEGRATION=1 python -m unittest discover -s tests
    -p test_agent_provider_browser_integration.py -v
The production adapter never launches a browser; this test fixture does.
"""
import base64
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from agent.providers import NeurobroBrowser, RobotOff


FIXTURE = """<!doctype html><meta charset=utf-8>
<span id=account>Signed in test fixture</span>
<div id=conversation data-conversation-id=offline-fixture>Offline conversation fixture</div>
<div id=login_required hidden>Login required</div><div id=busy hidden>Responding</div>
<textarea id=composer></textarea><input id=attachment type=file multiple>
<button id=submit>Send</button><div id=messages></div>
<script>
document.getElementById('submit').onclick = () => {
  const busy = document.getElementById('busy'); busy.hidden = false;
  const reply = document.createElement('div'); reply.className = 'assistant';
  reply.dataset.messageId = 'offline-reply';
  reply.textContent = 'HOLD. Fixture response with ' + document.getElementById('attachment').files.length + ' PNG attachments.';
  document.getElementById('messages').appendChild(reply);
  setTimeout(() => { reply.dataset.complete = 'true'; busy.hidden = true; }, 300);
};
</script>"""


@unittest.skipUnless(os.environ.get("HARUN_BROWSER_INTEGRATION") == "1", "Set HARUN_BROWSER_INTEGRATION=1 for isolated real Chromium fixture")
class ActualBrowserIntegrationTests(unittest.TestCase):
    def test_actual_existing_browser_attach_send_read_and_off_gate(self):
        from playwright.sync_api import sync_playwright
        # CI grants a sandbox profile to this exact installed Playwright ELF.
        # Reuse that executable instead of selecting a different PATH wrapper.
        configured = os.environ.get("HARUN_TEST_CHROMIUM_EXECUTABLE")
        executable = configured or shutil.which("chromium") or shutil.which("chromium-browser")
        if not executable:
            with sync_playwright() as setup:
                candidate = Path(setup.chromium.executable_path)
                executable = str(candidate) if candidate.is_file() else None
        self.assertIsNotNone(executable, "Opt-in Chromium integration requires an installed browser")
        self.assertTrue(Path(executable).is_file(), "The configured test browser must exist")
        with tempfile.TemporaryDirectory(prefix="harun-browser-fixture-") as profile:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            process = subprocess.Popen([
                executable, "--headless", *([] if configured else ["--no-sandbox"]), "--disable-gpu", "--no-first-run",
                "--disable-background-networking", "--disable-sync", "--disable-dev-shm-usage",
                "--host-resolver-rules=MAP * ~NOTFOUND", "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "about:blank",
            ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                endpoint = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 20
                while True:
                    try:
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                        with opener.open(endpoint + "/json/version", timeout=1) as response:
                            self.assertEqual(response.status, 200)
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            self.fail("Isolated Chromium CDP fixture failed to start")
                        time.sleep(0.1)
                # Establish a page once. The adapter must then attach to that page.
                with sync_playwright() as setup:
                    browser = setup.chromium.connect_over_cdp(endpoint)
                    context = browser.contexts[0]
                    page = context.pages[0]
                    page.route("https://chat.example.test/**", lambda route: route.fulfill(status=200, content_type="text/html", body=FIXTURE))
                    page.goto("https://chat.example.test/chat/fixture", wait_until="domcontentloaded")
                    self.assertTrue(page.locator("#composer").is_visible())
                    # Exiting the transport must keep the persistent browser alive.
                contract = {
                    "schema_version": 1, "provider": "NEUROBRO_WEB", "admitted": True,
                    "source_sha256": "b" * 64, "origin": "https://chat.example.test",
                    "selectors": {
                        "account": "#account", "composer": "#composer", "submit": "#submit",
                        "assistant_response": ".assistant", "assistant_complete": ".assistant[data-complete='true']",
                        "busy": "#busy", "login_required": "#login_required", "conversation": "#conversation", "attachment": "#attachment",
                    },
                    "timeout_seconds": 15, "max_response_chars": 8192,
                }
                enabled = [False]
                passive = NeurobroBrowser(endpoint, contract, lambda: enabled[0])
                self.assertEqual(passive.status()["status"], "CONNECTED")
                # Exercise the actual daemon composition, including the exact
                # file SHA pin and the provider deadline/evidence methods.
                from agent.__main__ import _LazyBrowser
                contract_raw = json.dumps(contract, sort_keys=True).encode()
                contract_path = Path(profile) / "browser-contract.json"
                contract_path.write_bytes(contract_raw)
                contract_path.chmod(0o600)
                adapter = _LazyBrowser({"cdp_url": endpoint, "contract_file": str(contract_path),
                                        "contract_sha256": hashlib.sha256(contract_raw).hexdigest()}, lambda: enabled[0])
                adapter.set_remaining_budget(lambda: 120.0)
                with self.assertRaises(RobotOff):
                    adapter.exchange("Do not send while OFF", request_id="fixture-off")
                enabled[0] = True  # Fixture-only boolean, never the production robot setting.
                png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
                reply = adapter.exchange("Exact fixture prompt", attachments_png=(png, png), request_id="fixture-actual-cdp")
                self.assertEqual(reply.text, "HOLD. Fixture response with 2 PNG attachments.")
                self.assertEqual(reply.response_id, "offline-reply")
                self.assertTrue(adapter.evidence()["verified"])
                self.assertIsNone(process.poll(), "Adapter disconnected without stopping the existing browser")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
