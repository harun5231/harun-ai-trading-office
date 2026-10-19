import copy
import hashlib
import http.server
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from agent.bridge import BackendBridge, BridgeUnavailable, _token, canonical, unique_pairs
from agent.models import AmbiguousOperation, MarketContext, RobotOff
from agent.risk import RiskRejected
from test_agent_risk import example_account, example_authority, example_setup


SOURCE = "a" * 64
KEY = "b" * 64


def snapshot_row():
    account = example_account()
    return {"schema_version": 1, "robot_on": True, "observed_at": account.observed_at,
        "generation": account.generation, "jakarta_day": account.jakarta_day,
        "balance_usdt": "100", "risk_usdt": "5", "daily_new_trades": 0,
        "positions": [], "pending": [], "supported_symbols": ["BTCUSDT", "ETHUSDT"],
        "risk_evidence": example_authority()}


def pending_row(**changes):
    row = {"order_id": "order1", "symbol": "BTCUSDT", "created_at": time.time()-19000,
        "quantity": "1", "executed_quantity": "0.4", "revision": "revision1", "owned": True}
    row.update(changes)
    return row


class Backend:
    def __init__(self):
        self.calls = []
        self.snapshot = snapshot_row()
        self.pending = pending_row()
        self.bad_handoff = False
        self.capabilities = {"protocol": "harun-agent-bridge-v1", "backend_revision": "revision1",
            "source_sha256": SOURCE, "gateway": "EXISTING_BINANCE_FUTURES_LIVE",
            "provider": "NEUROBRO_WEB", "atomic_robot_gate": True,
            "idempotent_handoff": True, "idempotent_cancel_remaining": True,
            "daily_entry_limit": 2, "combined_slot_limit": 2, "risk_model": "FEE_SLIPPAGE_RISK_V3"}

    def __call__(self, method, path, payload):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if path == "/capabilities":
            return copy.deepcopy(self.capabilities)
        if path == "/snapshot":
            return copy.deepcopy(self.snapshot)
        if path == "/pending/order1":
            return copy.deepcopy(self.pending)
        if path == "/handoff":
            return {"accepted": not self.bad_handoff, "receipt_id": "receipt1",
                "robot_on_at_gate": True, "idempotency_key": payload["idempotency_key"],
                "plan_sha256": payload["plan_sha256"]}
        if path == "/cancel-remaining":
            return {"accepted": True, "robot_on_at_gate": True, "preserves_filled_quantity": True,
                "idempotency_key": payload["idempotency_key"]}
        raise AssertionError(path)


class BridgeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.bridge = BackendBridge("https://bridge.invalid", "/not-read-test-only", "revision1",
                                    SOURCE, transport=self.backend)

    def market(self):
        return MarketContext("BTCUSDT", time.time(), {})

    def test_capabilities_revision_source_atomic_gate_strictly_pinned(self):
        for key, value in (("source_sha256", "f"*64), ("backend_revision", "unreviewed"),
                           ("atomic_robot_gate", False), ("gateway", "PUBLIC_PLACEHOLDER"),
                           ("provider", "NEUROAPI"), ("daily_entry_limit", 3)):
            with self.subTest(key=key):
                backend = Backend()
                backend.capabilities[key] = value
                bridge = BackendBridge("https://bridge.invalid", "unused", "revision1", SOURCE,
                                       transport=backend)
                with self.assertRaisesRegex(BridgeUnavailable, "CAPABILITIES"):
                    bridge.snapshot()
                self.assertEqual([c[1] for c in backend.calls], ["/capabilities"])

    def test_unsafe_plaintext_remote_credentials_url_path_query_and_zero_pin_rejected(self):
        for url in ("http://example.com", "https://user:secret@example.com", "https://example.com/admin", "https://example.com?a=b"):
            with self.subTest(url=url):
                with self.assertRaises(BridgeUnavailable):
                    BackendBridge(url, "unused", "revision1", SOURCE)
        with self.assertRaises(BridgeUnavailable):
            BackendBridge("https://example.com", "unused", "revision1", "0"*64)

    def test_off_does_not_post_handoff_or_cancel(self):
        account = replace(self.bridge.snapshot(), robot_on=False)
        with self.assertRaises(RobotOff):
            self.bridge.handoff(example_setup(), self.market(), account, KEY)
        with self.assertRaises(RobotOff):
            self.bridge.cancel_remaining(self.bridge.pending_status("order1"), account, KEY)
        self.assertFalse(any(c[0] == "POST" for c in self.backend.calls))

    def test_off_change_immediately_before_handoff_prevents_post(self):
        account = self.bridge.snapshot()
        self.backend.snapshot["robot_on"] = False
        with self.assertRaises(RobotOff):
            self.bridge.handoff(example_setup(), self.market(), account, KEY)
        self.assertFalse(any(c[0] == "POST" for c in self.backend.calls))

    def test_risk_setting_or_generation_change_requires_recalculation(self):
        for key, value in (("risk_usdt", "8"), ("generation", "generation2")):
            with self.subTest(key=key):
                self.backend.snapshot = snapshot_row()
                account = self.bridge.snapshot()
                self.backend.snapshot[key] = value
                with self.assertRaisesRegex(RiskRejected, "REVISION"):
                    self.bridge.handoff(example_setup(), self.market(), account, KEY)
        self.assertFalse(any(c[0] == "POST" for c in self.backend.calls))

    def test_real_risk_handoff_bound_to_source_market_revision_idempotency(self):
        account = self.bridge.snapshot()
        result = self.bridge.handoff(example_setup(), self.market(), account, KEY)
        self.assertTrue(result["accepted"])
        request = self.backend.calls[-1][2]
        self.assertEqual(request["idempotency_key"], KEY)
        self.assertEqual(request["expected_generation"], account.generation)
        self.assertEqual(request["source"]["provider"], "NEUROBRO_WEB")
        self.assertEqual(request["source"]["response_text"], example_setup().source_text)
        self.assertEqual(request["plan_sha256"], hashlib.sha256(canonical(request["plan"])).hexdigest())
        self.assertEqual(request["plan"]["leverage"], 75)

    def test_caller_metadata_cannot_override_authoritative_fees_or_capacity(self):
        account = self.bridge.snapshot()
        fake = replace(account, metadata={"commission": "0", "occupied_slots": 0})
        self.backend.snapshot["risk_evidence"]["occupied_slots"] = 2
        with self.assertRaisesRegex(RiskRejected, "NO_CAPACITY"):
            self.bridge.handoff(example_setup(), self.market(), fake, KEY)
        self.assertFalse(any(c[0] == "POST" for c in self.backend.calls))

    def test_partial_fill_cancels_remaining_only_with_revision_and_fill_guards(self):
        account = self.bridge.snapshot()
        order = self.bridge.pending_status("order1")
        self.bridge.cancel_remaining(order, account, KEY)
        request = self.backend.calls[-1][2]
        self.assertEqual(request["remaining_quantity"], "0.6")
        self.assertEqual(request["expected_executed_quantity"], "0.4")
        self.assertEqual(request["expected_order_revision"], "revision1")
        self.assertEqual(request["expected_generation"], account.generation)

    def test_fill_change_or_unowned_order_never_cancelled(self):
        account = self.bridge.snapshot()
        order = self.bridge.pending_status("order1")
        self.backend.pending["executed_quantity"] = "0.5"
        with self.assertRaisesRegex(RiskRejected, "FILL_CHANGED"):
            self.bridge.cancel_remaining(order, account, KEY)
        self.backend.pending["owned"] = False
        with self.assertRaisesRegex(RiskRejected, "OWNERSHIP"):
            self.bridge.pending_status("order1")
        self.assertFalse(any(c[0] == "POST" for c in self.backend.calls))

    def test_invalid_pending_path_or_execution_quantity_rejected(self):
        with self.assertRaises(BridgeUnavailable):
            self.bridge.pending_status("../credentials")
        self.backend.pending["executed_quantity"] = "2"
        with self.assertRaises(BridgeUnavailable):
            self.bridge.pending_status("order1")

    def test_handoff_uncertain_response_is_ambiguous_never_automatically_retried(self):
        account = self.bridge.snapshot()
        self.backend.bad_handoff = True
        with self.assertRaises(AmbiguousOperation):
            self.bridge.handoff(example_setup(), self.market(), account, KEY)
        self.assertEqual(len([c for c in self.backend.calls if c[0] == "POST"]), 1)

    def test_stale_snapshot_duplicate_position_and_float_numbers_rejected(self):
        for mutate in (lambda row: row.update(observed_at=time.time()-31),
                       lambda row: row.update(balance_usdt=100.5),
                       lambda row: row.update(positions=[{"symbol": "BTCUSDT", "quantity": "1"}]*2)):
            self.backend.snapshot = snapshot_row()
            mutate(self.backend.snapshot)
            with self.assertRaises(BridgeUnavailable):
                self.bridge.snapshot()

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(BridgeUnavailable):
            unique_pairs([("risk", "5"), ("risk", "500")])


class BridgeTokenTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.path = Path(self.temporary.name) / "bridge.token"
        self.path.write_text("a"*64)
        self.path.chmod(0o600)

    def tearDown(self):
        self.temporary.cleanup()

    def test_private_app_token_read_and_unsafe_modes_links_rejected(self):
        self.assertEqual(_token(str(self.path)), "a"*64)
        self.path.chmod(0o644)
        with self.assertRaises(BridgeUnavailable):
            _token(str(self.path))
        self.path.chmod(0o600)
        link = self.path.parent / "linked"
        os.link(self.path, link)
        with self.assertRaises(BridgeUnavailable):
            _token(str(self.path))

    def test_symlink_token_or_parent_rejected_without_reading(self):
        link = self.path.parent / "symbolic"
        link.symlink_to(self.path)
        with self.assertRaises(BridgeUnavailable):
            _token(str(link))
        parent_link = self.path.parent / "parent"
        parent_link.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaises(BridgeUnavailable):
            _token(str(parent_link / "bridge.token"))

    def test_loopback_http_transport_sends_app_token_to_fixed_protocol_path(self):
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.assertEqual(self.path, "/agent/v1/capabilities")
                owner.assertEqual(self.headers["Authorization"], "Bearer " + "a"*64)
                body = canonical(Backend().capabilities)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            bridge = BackendBridge(f"http://127.0.0.1:{server.server_port}", str(self.path), "revision1", SOURCE)
            self.assertTrue(bridge.capabilities()["atomic_robot_gate"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
