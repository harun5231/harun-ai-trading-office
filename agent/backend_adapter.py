"""App-scoped HTTP adapter for a reviewed existing production backend.

The host integration supplies its actual shared state lock and existing
gateway callbacks. This module never imports a gateway, starts Coordinator,
opens the financial ledger, reads Binance secrets, or turns ROBOT ON.

Installing this factory alone does not reconcile a production backend: its
source identity, sole research coordinator ownership and private browser
receipt verifier must be supplied by a separately reviewed host integration.
"""
from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import re
import time

from .bridge import BackendBridge, BridgeUnavailable, HEX64, IDENTITY, MAX_BODY, canonical, unique_pairs
from .models import AmbiguousOperation, RobotOff
from .risk import RiskRejected, evaluate_setup, explicit_pending_decision, fresh, parse_setup


class ExistingBackendAdapter:
    """Finite callback interface, with durable no-replay and source admission.

    Required backend methods:
      bindings() -> root-reviewed identity/ownership proof (no credentials)
      snapshot() -> sanitized account/risk evidence protocol
      source_receipt(message_id) -> immutable private browser receipt
      pending(order_id) -> current order, ownership, revision and status
      operation_lookup(key), operation_prepare(key,digest,kind),
      operation_complete(key,digest,result) -> existing durable backend journal
      enqueue_existing_gateway(plan,key,expected_generation) -> receipt ID
      cancel_existing_remaining(request,key) -> cancellation result

    The supplied state_lock must be the same lock used by production settings,
    snapshot reconciliation and execution admission. Durable operation_prepare
    must finish before the existing gateway can be called. A prepared operation
    is never resubmitted by this adapter after an exception or restart.
    """

    def __init__(self, backend, state_lock, backend_revision: str, source_sha256: str):
        if (not isinstance(backend_revision, str) or not IDENTITY.fullmatch(backend_revision)
                or not isinstance(source_sha256, str) or not HEX64.fullmatch(source_sha256)
                or source_sha256 == "0" * 64 or not hasattr(state_lock, "__enter__")):
            raise BridgeUnavailable("BACKEND_INTEGRATION_UNVERIFIED")
        self.backend, self.state_lock = backend, state_lock
        self.revision, self.source_sha256 = backend_revision, source_sha256
        if getattr(backend, "state_lock", None) is not state_lock:
            raise BridgeUnavailable("PRODUCTION_SHARED_STATE_LOCK_UNVERIFIED")

    def capabilities(self):
        bindings = self.backend.bindings()
        required = {"backend_revision": self.revision, "source_sha256": self.source_sha256,
            "existing_gateway_verified": True, "single_coordinator_verified": True,
            "source_receipts_verified": True, "shared_robot_gate_verified": True,
            "durable_idempotency_verified": True}
        if not isinstance(bindings, dict) or any(bindings.get(k) != v for k, v in required.items()):
            raise BridgeUnavailable("BACKEND_INTEGRATION_UNVERIFIED")
        for key in ("gateway_source_sha256", "browser_provider_source_sha256"):
            value = bindings.get(key)
            if not isinstance(value, str) or not HEX64.fullmatch(value) or value == "0" * 64:
                raise BridgeUnavailable("PRODUCTION_SOURCE_IDENTITY_UNVERIFIED")
        return {"protocol": "harun-agent-bridge-v1", "backend_revision": self.revision,
            "source_sha256": self.source_sha256, "gateway": "EXISTING_BINANCE_FUTURES_LIVE",
            "provider": "NEUROBRO_WEB", "atomic_robot_gate": True,
            "source_receipts_verified": True, "single_coordinator_verified": True,
            "idempotent_handoff": True, "idempotent_cancel_remaining": True,
            "daily_entry_limit": 2, "combined_slot_limit": 2, "risk_model": "FEE_SLIPPAGE_RISK_V3"}

    def _account(self):
        # Reuse the finite protocol parser, without any socket or injected
        # production-evidence claim. Returned authority stays inside the server.
        row = self.backend.snapshot()
        capabilities = self.capabilities()
        validator = BackendBridge("http://127.0.0.1", "unused", self.revision,
            self.source_sha256, transport=lambda _method, path, _payload:
                capabilities if path == "/capabilities" else row)
        account = validator.snapshot()
        return row, account, validator._authorities[account.generation][1]

    def _existing_operation(self, key, digest):
        row = self.backend.operation_lookup(key)
        if row is None:
            return None
        if not isinstance(row, dict) or row.get("request_sha256") != digest:
            raise RiskRejected("IDEMPOTENCY_PAYLOAD_CHANGED")
        if row.get("state") == "COMPLETE" and isinstance(row.get("result"), dict):
            return row["result"]
        raise AmbiguousOperation("EXISTING_OPERATION_REQUIRES_RECONCILIATION")

    def _source(self, source, symbol):
        if not isinstance(source, dict) or source.get("provider") != "NEUROBRO_WEB":
            raise RiskRejected("NEUROBRO_SOURCE_UNVERIFIED")
        message_id = source.get("message_id")
        if not isinstance(message_id, str) or not IDENTITY.fullmatch(message_id):
            raise RiskRejected("NEUROBRO_SOURCE_UNVERIFIED")
        # This lookup must query the private browser's immutable receipt, not
        # echo the text/hash supplied by the AI agent itself.
        receipt = self.backend.source_receipt(message_id)
        if (not isinstance(receipt, dict) or receipt.get("verified") is not True
                or receipt.get("provider") != "NEUROBRO_WEB"
                or receipt.get("message_id") != message_id or receipt.get("symbol") != symbol
                or receipt.get("text") != source.get("response_text")
                or receipt.get("response_sha256") != source.get("response_sha256")):
            raise RiskRejected("PRIVATE_BROWSER_RECEIPT_MISMATCH")
        fresh(receipt.get("observed_at"), time.time(), 180, "NEUROBRO_RECEIPT_STALE")
        setup = parse_setup(receipt["text"], symbol, message_id)
        if setup is None or setup.source_sha256 != receipt["response_sha256"]:
            raise RiskRejected("NEUROBRO_SETUP_UNVERIFIED")
        return setup

    def _cancel_source(self, source, order, order_ref):
        if (not isinstance(source, dict) or source.get("provider") != "NEUROBRO_WEB"
                or not isinstance(source.get("message_id"), str)
                or not IDENTITY.fullmatch(source["message_id"])):
            raise RiskRejected("NEUROBRO_CANCELLATION_SOURCE_UNVERIFIED")
        receipt = self.backend.source_receipt(source["message_id"])
        expected_ref = hashlib.sha256(order["order_id"].encode("utf-8")).hexdigest()
        expected_prefix = "PENDING_REVIEW symbol=" + order["symbol"] + "\norder_ref: " + expected_ref + "\n"
        expected_request = expected_prefix + (
            f"Evaluasi LIMIT {order['symbol']} yang belum terisi penuh selama 5 jam. "
            "Berdasarkan chart dan data terbaru, Neurobro pilih TAHAN atau BATALKAN sisa pending. "
            "Jangan ubah posisi yang telah terisi. Jawab keputusan eksplisit dan alasan singkat.")
        if (not isinstance(receipt, dict) or receipt.get("verified") is not True
                or receipt.get("state") != "COMPLETED" or receipt.get("provider") != "NEUROBRO_WEB"
                or receipt.get("message_id") != source["message_id"]
                or receipt.get("symbol") != order["symbol"] or receipt.get("phase") != "PENDING_REVIEW"
                or receipt.get("order_ref") != expected_ref or order_ref != expected_ref
                or not isinstance(receipt.get("request_id"), str)
                or not HEX64.fullmatch(receipt["request_id"])
                or not isinstance(receipt.get("request_text"), str)
                or not (receipt["request_text"] == expected_request or
                        receipt["request_text"].startswith(expected_request + "\nPertanyaan lanjutan: "))
                or receipt.get("text") != source.get("response_text")
                or receipt.get("response_sha256") != source.get("response_sha256")):
            raise RiskRejected("PRIVATE_BROWSER_CANCEL_RECEIPT_MISMATCH")
        text = receipt["text"]
        if (explicit_pending_decision(text) != "BATALKAN"
                or hashlib.sha256(text.encode("utf-8")).hexdigest() != receipt["response_sha256"]):
            raise RiskRejected("NEUROBRO_CANCELLATION_NOT_CONFIRMED")
        fresh(receipt.get("observed_at"), time.time(), 180, "NEUROBRO_CANCEL_RECEIPT_STALE")

    def request(self, method: str, path: str, payload=None):
        """Dispatch only five finite routes; no arbitrary command or URL input."""
        with self.state_lock:
            self.capabilities()
            if method == "GET" and path == "/capabilities":
                return self.capabilities()
            if method == "GET" and path == "/snapshot":
                row, _account, _authority = self._account()
                return row
            if method == "GET" and path.startswith("/pending/"):
                identifier = path.removeprefix("/pending/")
                if not IDENTITY.fullmatch(identifier):
                    raise RiskRejected("ORDER_ID_INVALID")
                result = self.backend.pending(identifier)
                if not isinstance(result, dict) or result.get("order_id") != identifier:
                    raise BridgeUnavailable("PENDING_ORDER_UNVERIFIED")
                return result
            if method != "POST" or path not in ("/handoff", "/cancel-remaining") or not isinstance(payload, dict):
                raise RiskRejected("BRIDGE_ROUTE_FORBIDDEN")
            key = payload.get("idempotency_key")
            if not isinstance(key, str) or not HEX64.fullmatch(key):
                raise RiskRejected("IDEMPOTENCY_KEY_INVALID")
            digest = hashlib.sha256(canonical({"path": path, "payload": payload})).hexdigest()
            previous = self._existing_operation(key, digest)
            if previous is not None:
                return previous
            _row, account, authority = self._account()
            if not account.robot_on:
                raise RobotOff("ROBOT_OFF")
            if payload.get("expected_generation") != account.generation:
                raise RiskRejected("ACCOUNT_REVISION_CHANGED")
            if path == "/handoff":
                fresh(payload.get("market_observed_at"), time.time(), 180, "STALE_MARKET")
                candidate = payload.get("plan")
                if not isinstance(candidate, dict):
                    raise RiskRejected("ORDER_PLAN_INVALID")
                setup = self._source(payload.get("source"), candidate.get("symbol"))
                plan = evaluate_setup(setup, account, authority)
                plan_sha = hashlib.sha256(canonical(plan)).hexdigest()
                if canonical(plan) != canonical(candidate) or payload.get("plan_sha256") != plan_sha:
                    raise RiskRejected("BACKEND_RISK_PLAN_CHANGED")
                self.backend.operation_prepare(key, digest, "HANDOFF")
                try:
                    receipt_id = self.backend.enqueue_existing_gateway(plan, key, account.generation)
                    if not isinstance(receipt_id, str) or not IDENTITY.fullmatch(receipt_id):
                        raise ValueError()
                    result = {"accepted": True, "receipt_id": receipt_id, "robot_on_at_gate": True,
                              "plan_sha256": plan_sha, "idempotency_key": key}
                    self.backend.operation_complete(key, digest, result)
                    return result
                except Exception:
                    raise AmbiguousOperation("HANDOFF_REQUIRES_BACKEND_RECONCILIATION") from None
            identifier = payload.get("order_id")
            if not isinstance(identifier, str) or not IDENTITY.fullmatch(identifier):
                raise RiskRejected("ORDER_ID_INVALID")
            order = self.backend.pending(identifier)
            if (not isinstance(order, dict) or order.get("owned") is not True
                    or order.get("order_id") != identifier
                    or order.get("status") not in ("NEW", "PARTIALLY_FILLED")
                    or order.get("revision") != payload.get("expected_order_revision")
                    or order.get("executed_quantity") != payload.get("expected_executed_quantity")
                    or order.get("symbol") != payload.get("symbol")):
                raise RiskRejected("ORDER_REVISION_OR_OWNERSHIP_CHANGED")
            from .risk import number
            remaining = number(order["quantity"]) - number(order["executed_quantity"], zero=True)
            if remaining <= 0 or remaining != number(payload.get("remaining_quantity")):
                raise RiskRejected("ORDER_REMAINING_QUANTITY_CHANGED")
            self._cancel_source(payload.get("source"), order, payload.get("order_ref"))
            self.backend.operation_prepare(key, digest, "CANCEL_REMAINING")
            try:
                response = self.backend.cancel_existing_remaining(payload, key)
                if not isinstance(response, dict) or response.get("preserves_filled_quantity") is not True:
                    raise ValueError()
                result = {"accepted": True, "robot_on_at_gate": True,
                          "preserves_filled_quantity": True, "idempotency_key": key}
                self.backend.operation_complete(key, digest, result)
                return result
            except Exception:
                raise AmbiguousOperation("CANCEL_REQUIRES_BACKEND_RECONCILIATION") from None


def handler(adapter: ExistingBackendAdapter, token: str):
    """Create an authenticated HTTP handler for an existing app server/TLS proxy."""
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,255}", token):
        raise BridgeUnavailable("BRIDGE_TOKEN_INVALID")

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _handle(self, method):
            self.connection.settimeout(5)
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                return self._reply(403, {"error": "FORBIDDEN"})
            if not self.path.startswith("/agent/v1/") or "?" in self.path or "#" in self.path:
                return self._reply(404, {"error": "NOT_FOUND"})
            try:
                payload = None
                if method == "POST":
                    length = self.headers.get("Content-Length", "")
                    if (self.headers.get("Transfer-Encoding") or not length.isdecimal()
                            or not 0 < int(length) <= MAX_BODY
                            or self.headers.get("Content-Type") != "application/json"):
                        raise ValueError()
                    raw = self.rfile.read(int(length))
                    if len(raw) != int(length):
                        raise ValueError()
                    payload = json.loads(raw, object_pairs_hook=unique_pairs,
                        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
                result = adapter.request(method, self.path.removeprefix("/agent/v1"), payload)
                return self._reply(200, result)
            except (RiskRejected, RobotOff, BridgeUnavailable, ValueError, TypeError, KeyError):
                return self._reply(409, {"error": "BACKEND_REQUEST_NOT_ADMITTED"})
            except Exception:
                return self._reply(503, {"error": "BACKEND_OUTCOME_REQUIRES_REVIEW"})

        def _reply(self, status, body):
            raw = canonical(body)
            if len(raw) > MAX_BODY:
                status, raw = 503, b'{"error":"BACKEND_RESULT_TOO_LARGE"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return Handler
