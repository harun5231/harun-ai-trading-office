import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from agent.browser_rpc import BrowserRPCService, NeurobroRPC
from agent.models import NotSent
from agent.providers import BrowserReply, DeliveryUncertain, LoginRequired, ProviderUnavailable, RobotOff


SOURCE = "a" * 64
TOKEN = "fixture_app_scoped_token_0123456789"
NOW = 1792164832.0


class Browser:
    contract = {"source_sha256": SOURCE}
    def __init__(self):
        self.calls = 0
        self.error = None
        self.origin_verified = False
        self.reply_text = "HOLD. Wait for Neurobro confirmation."
        self.reply_id = "fixture-reply"
    def status(self):
        return {"status": "CONNECTED"}
    def exchange(self, message, attachments_png=(), conversation_id=None, request_id=None):
        self.calls += 1
        if self.error:
            raise self.error
        return BrowserReply(self.reply_text, "b" * 64, self.reply_id)
    def evidence(self):
        return {"verified": self.origin_verified, "provider": "NEUROBRO_WEB", "source_sha256": SOURCE,
                "observed_at": NOW, "response_sha256": hashlib.sha256(self.reply_text.encode()).hexdigest()}


class BrowserRPCTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="harun-browser-rpc-")
        self.browser = Browser()
        self.database = str(Path(self.directory.name) / "receipts.sqlite3")
        self.service = BrowserRPCService(self.browser, "unused-injected-fixture-token", self.database,
                                        clock=lambda: NOW, token_reader=lambda: TOKEN)

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def payload(self, request="request-1"):
        return {"schema_version": 1, "source_sha256": SOURCE, "message": "Original exact prompt", "attachments_png": [], "conversation_id": None, "request_id": request}

    def dispatch(self, payload):
        return self.service.dispatch("POST", "/neurobro/v1/exchange", "Bearer " + TOKEN, json.dumps(payload).encode())

    def client(self, mutate=None):
        def transport(method, path, payload):
            status, result = self.service.dispatch(method, path, "Bearer " + TOKEN, b"" if payload is None else json.dumps(payload).encode())
            if status != 200:
                raise DeliveryUncertain("fixture server rejected request")
            return mutate(result) if mutate else result
        return NeurobroRPC("https://browser-bridge.example.test", "unused", SOURCE, lambda: True, transport=transport, clock=lambda: NOW)

    def test_real_service_dispatch_and_client_contract(self):
        client = self.client()
        self.assertEqual(client.status()["status"], "CONNECTED")
        reply = client.exchange("Exact prompt", request_id="request-1")
        self.assertEqual(reply.text, "HOLD. Wait for Neurobro confirmation.")
        self.assertEqual(reply.provider, "NEUROBRO_WEB")
        self.assertEqual(self.browser.calls, 1)
        self.assertFalse(client.evidence()["verified"], "Injected transport is never live network proof")

    def test_off_client_does_not_even_contact_bridge(self):
        client = self.client()
        client.robot_enabled = lambda: False
        with self.assertRaises(RobotOff):
            client.exchange("Do not submit", request_id="request-off")
        self.assertEqual(self.browser.calls, 0)

    def test_token_and_endpoint_scope(self):
        status, _ = self.service.dispatch("GET", "/neurobro/v1/status", "Bearer wrong")
        self.assertEqual(status, 401)
        for path in ("/json", "/json/version", "/neurobro/v1/evaluate", "/neurobro/v1/status?url=elsewhere", "/binance/v1/order"):
            self.assertEqual(self.service.dispatch("GET", path, "Bearer " + TOKEN)[0], 404)
        self.assertEqual(self.browser.calls, 0)

    def test_identical_request_is_cached_without_sending_again(self):
        payload = self.payload()
        status, reply = self.dispatch(payload)
        self.assertEqual(status, 200)
        self.assertEqual(self.dispatch(payload), (status, reply))
        self.assertEqual(self.browser.calls, 1)

    def test_unknown_delivery_survives_service_restart_without_resubmission(self):
        self.browser.error = DeliveryUncertain("Interrupted after submit")
        payload = self.payload()
        self.assertEqual(self.dispatch(payload)[0], 409)
        self.service.close()
        self.service = BrowserRPCService(self.browser, "unused", self.database, clock=lambda: NOW, token_reader=lambda: TOKEN)
        self.browser.error = None
        self.assertEqual(self.dispatch(payload)[0], 409)
        self.assertEqual(self.browser.calls, 1)

    def test_request_id_body_mismatch_never_sends(self):
        payload = self.payload()
        self.assertEqual(self.dispatch(payload)[0], 200)
        payload["message"] = "Changed prompt"
        self.assertEqual(self.dispatch(payload)[0], 409)
        self.assertEqual(self.browser.calls, 1)

    def test_known_login_failure_is_not_sent(self):
        self.browser.error = LoginRequired("Login first")
        status, result = self.dispatch(self.payload())
        self.assertEqual(status, 409)
        self.assertEqual(result, {"not_sent": True, "reason": "LOGIN_REQUIRED"})
        self.assertEqual(self.dispatch(self.payload())[0], 409)
        self.assertEqual(self.browser.calls, 1)

    def test_source_pin_mismatch_or_stale_response_is_unknown_after_send(self):
        for mutate in (lambda value: {**value, "source_sha256": "c" * 64}, lambda value: {**value, "observed_at": NOW - 121}):
            with self.assertRaises(DeliveryUncertain):
                self.client(mutate).exchange("Question", request_id="pin-test")

    def test_rejects_unknown_fields_source_and_invalid_png_before_browser(self):
        for kind in ("extra", "source", "image"):
            payload = self.payload(kind)
            if kind == "extra":
                payload["url"] = "https://unrelated.example.test"
            elif kind == "source":
                payload["source_sha256"] = "c" * 64
            else:
                payload["attachments_png"] = ["not-base64"]
            self.assertEqual(self.dispatch(payload)[0], 400)
        self.assertEqual(self.browser.calls, 0)

    def test_rejects_plaintext_remote_service_and_url_credentials(self):
        for url in ("http://remote.example.test", "https://token@remote.example.test", "https://remote.example.test?token=secret"):
            with self.assertRaises(ProviderUnavailable):
                NeurobroRPC(url, "unused", SOURCE, lambda: True)

    def test_stale_cached_reply_not_refreshed_into_new_connectivity_evidence(self):
        payload = self.payload()
        original = self.dispatch(payload)[1]
        self.service.clock = lambda: NOW + 500
        cached = self.dispatch(payload)[1]
        self.assertEqual(cached["observed_at"], original["observed_at"])
        self.assertEqual(self.browser.calls, 1)

    def test_source_receipt_binds_actual_durable_prompt_and_neurobro_reply(self):
        self.browser.origin_verified = True  # Trusted browser-origin fixture, never an agent request field.
        self.browser.reply_text = "BATALKAN"
        order_ref = hashlib.sha256(b"owned-order-1").hexdigest()
        prompt = (f"PENDING_REVIEW symbol=BTCUSDT\norder_ref: {order_ref}\n"
                  "Evaluasi LIMIT BTCUSDT yang belum terisi penuh selama 5 jam. "
                  "Berdasarkan chart dan data terbaru, Neurobro pilih TAHAN atau BATALKAN sisa pending. "
                  "Jangan ubah posisi yang telah terisi. Jawab keputusan eksplisit dan alasan singkat.")
        payload = self.payload(hashlib.sha256(b"request-one").hexdigest())
        payload["message"] = prompt
        self.assertEqual(self.dispatch(payload)[0], 200)
        payload["message"] = "Forged agent supplied replacement text"
        receipt = self.service.source_receipt("fixture-reply")
        self.assertEqual(receipt["request_text"], prompt)
        self.assertEqual(receipt["text"], "BATALKAN")
        self.assertEqual(receipt["phase"], "PENDING_REVIEW")
        self.assertEqual(receipt["symbol"], "BTCUSDT")
        self.assertEqual(receipt["order_ref"], order_ref)
        self.assertEqual(receipt["state"], "COMPLETED")
        self.service.clock = lambda: NOW + 900
        self.assertEqual(self.service.source_receipt("fixture-reply")["observed_at"], NOW)
        self.assertEqual(self.service.dispatch("GET", "/neurobro/v1/source_receipt", "Bearer " + TOKEN)[0], 404)

    def test_unknown_or_unverified_origin_never_becomes_source_receipt(self):
        self.assertEqual(self.dispatch(self.payload())[0], 200)
        self.assertIsNone(self.service.source_receipt("fixture-reply"))
        self.browser.origin_verified = True
        self.browser.error = DeliveryUncertain("Interrupted delivery")
        self.assertEqual(self.dispatch(self.payload("unknown-request"))[0], 409)
        self.assertIsNone(self.service.source_receipt("nonexistent-message"))

    def test_source_receipt_rejects_tampered_request_or_false_pending_template(self):
        self.browser.origin_verified = True
        self.browser.reply_text = "BATALKAN"
        payload = self.payload()
        payload["message"] = "PENDING_REVIEW symbol=BTCUSDT\norder_ref: " + "a" * 64 + "\nUnrelated question"
        self.assertEqual(self.dispatch(payload)[0], 200)
        self.assertIsNone(self.service.source_receipt("fixture-reply"))

    def test_dot_id_normalization_matches_wire_reply_and_private_source_receipt(self):
        self.browser.origin_verified = True
        self.browser.reply_id = "website.message.123"
        payload = self.payload()
        status, result = self.dispatch(payload)
        self.assertEqual(status, 200)
        self.assertRegex(result["response_id"], r"^[0-9a-f]{64}$")
        receipt = self.service.source_receipt(result["response_id"])
        self.assertEqual(receipt["message_id"], result["response_id"])
        self.assertIsNone(self.service.source_receipt(self.browser.reply_id))
        self.service.database.execute("UPDATE sends SET request=?", (json.dumps(self.payload("forged-new-id")),))
        self.service.database.commit()
        self.assertIsNone(self.service.source_receipt(result["response_id"]))


@unittest.skipUnless(os.environ.get("HARUN_RPC_INTEGRATION") == "1", "Set HARUN_RPC_INTEGRATION=1 for localhost HTTP fixture")
class ActualBrowserRPCHTTPTests(unittest.TestCase):
    def test_actual_http_auth_source_pin_and_idempotent_receipt(self):
        # Keep token parents private and under the workspace, never use real secrets.
        with tempfile.TemporaryDirectory(prefix=".browser-rpc-http-fixture-", dir=Path.cwd()) as directory:
            token = Path(directory) / "app-token"
            token.write_text(TOKEN)
            token.chmod(0o600)
            browser = Browser()
            service = BrowserRPCService(browser, str(token), str(Path(directory) / "receipts.sqlite3"))
            server = service.server()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = NeurobroRPC(f"http://127.0.0.1:{server.server_address[1]}", str(token), SOURCE, lambda: True)
                self.assertEqual(client.status()["status"], "CONNECTED")
                first = client.exchange("Question", request_id="actual-http-1")
                self.assertEqual(first, client.exchange("Question", request_id="actual-http-1"))
                self.assertEqual(browser.calls, 1)
                self.assertTrue(client.evidence()["verified"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                service.close()


if __name__ == "__main__":
    unittest.main()
