"""Authenticated, narrowly scoped client for the existing production backend.

There is deliberately no exchange client, executor import, financial SQLite
connection or credential passthrough here. This protocol requires a reviewed
backend adapter; an unrelated public worker is not silently treated as one.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import ssl
import stat
import threading
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from .models import (AccountSnapshot, AmbiguousOperation, MarketContext, NotSent,
                     NeurobroSetup, PendingOrder, Position, RobotOff)
from .risk import RiskRejected, SYMBOL, capacity, evaluate_setup, fresh, number


class BridgeUnavailable(NotSent):
    pass


HEX64 = re.compile(r"[0-9a-f]{64}", re.ASCII)
IDENTITY = re.compile(r"[A-Za-z0-9:_-]{1,128}", re.ASCII)
MAX_BODY = 262144


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def unique_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise BridgeUnavailable("BACKEND_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _identity(value):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise BridgeUnavailable("BACKEND_IDENTITY_INVALID")
    return value


def _token(path: str) -> str:
    """Read an app-scoped token, never a Binance credential or environment dump."""
    target = Path(path)
    if not target.is_absolute():
        raise BridgeUnavailable("BRIDGE_TOKEN_PATH_UNSAFE")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in target.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            if metadata.st_uid not in (0, os.geteuid()) or metadata.st_mode & 0o022:
                raise BridgeUnavailable("BRIDGE_TOKEN_PARENT_UNSAFE")
        fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid not in (0, os.geteuid())
                    or stat.S_IMODE(before.st_mode) not in (0o400, 0o600)
                    or before.st_nlink != 1 or not 32 <= before.st_size <= 256):
                raise BridgeUnavailable("BRIDGE_TOKEN_METADATA_UNSAFE")
            raw = stream.read(257)
            after = os.fstat(stream.fileno())
            identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            if identity(before) != identity(after) or len(raw) != before.st_size:
                raise BridgeUnavailable("BRIDGE_TOKEN_CHANGED")
        try:
            text = raw.decode("ascii").rstrip("\n")
        except UnicodeError:
            raise BridgeUnavailable("BRIDGE_TOKEN_INVALID") from None
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,255}", text):
            raise BridgeUnavailable("BRIDGE_TOKEN_INVALID")
        return text
    except OSError:
        raise BridgeUnavailable("BRIDGE_TOKEN_UNAVAILABLE") from None
    finally:
        os.close(descriptor)


class BackendBridge:
    """Finite HTTP protocol, bound to an explicitly approved backend revision.

    The adapter must apply ROBOT ON, account revision, current risk setting,
    capacity and idempotency checks atomically before the existing gateway.
    The capability hash is supplied by the release manifest, never learned as
    an automatically trusted value from the remote endpoint.
    """

    def __init__(self, base_url: str, token_file: str, backend_revision: str,
                 backend_source_sha256: str, *, transport=None, clock=time.time):
        try:
            url = urlsplit(base_url)
            if (url.username or url.password or url.query or url.fragment or url.path not in ("", "/")
                    or not url.hostname or url.scheme not in ("https", "http")
                    or url.scheme == "http" and url.hostname != "127.0.0.1"):
                raise ValueError()
            if not isinstance(backend_source_sha256, str) or not HEX64.fullmatch(backend_source_sha256) or backend_source_sha256 == "0" * 64:
                raise ValueError()
            _identity(backend_revision)
            self.port = url.port
        except (ValueError, TypeError, BridgeUnavailable):
            raise BridgeUnavailable("BACKEND_CONFIGURATION_UNVERIFIED") from None
        self.scheme, self.host = url.scheme, url.hostname
        self.token_file = token_file
        self.backend_revision = backend_revision
        self.backend_source_sha256 = backend_source_sha256
        self._transport, self._clock = transport, clock
        self._capabilities_at = None
        self._authorities = {}
        self._lock = threading.RLock()
        self._remaining_budget = None
        self._evidence = {"verified": False}

    def set_remaining_budget(self, callback):
        self._remaining_budget = callback

    def _timeout(self):
        remaining = 15 if self._remaining_budget is None else self._remaining_budget()
        if type(remaining) not in (int, float) or not 0 < remaining:
            raise BridgeUnavailable("TASK_BUDGET_EXHAUSTED")
        return min(15, remaining)

    def evidence(self):
        result = dict(self._evidence)
        if result.get("verified") and not 0 <= self._clock() - result["observed_at"] <= 120:
            result["verified"] = False
        return result

    def _http(self, method: str, path: str, payload: dict | None):
        timeout = self._timeout()
        deadline = time.monotonic() + timeout
        submitted = False
        connection = (http.client.HTTPSConnection(self.host, self.port, timeout=timeout,
                      context=ssl.create_default_context()) if self.scheme == "https" else
                      http.client.HTTPConnection(self.host, self.port, timeout=timeout))
        try:
            body = None if payload is None else canonical(payload)
            authorization = "Bearer " + _token(self.token_file)
            submitted = True
            connection.request(method, "/agent/v1" + path, body=body,
                headers={"Authorization": authorization,
                         "Content-Type": "application/json", "Accept": "application/json"})
            sock = connection.sock
            response = connection.getresponse()
            chunks, size = [], 0
            while size <= MAX_BODY:
                remaining = min(self._timeout(), deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError()
                if sock is not None:
                    sock.settimeout(remaining)
                block = response.read1(min(16384, MAX_BODY + 1 - size))
                if not block:
                    break
                chunks.append(block)
                size += len(block)
            raw = b"".join(chunks)
            if len(raw) > MAX_BODY or response.status != 200:
                raise BridgeUnavailable("BACKEND_RESPONSE_REJECTED")
            if response.getheader("Content-Type", "").split(";", 1)[0].lower() != "application/json":
                raise BridgeUnavailable("BACKEND_RESPONSE_TYPE_INVALID")
            result = json.loads(raw, object_pairs_hook=unique_pairs,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(result, dict):
                raise BridgeUnavailable("BACKEND_RESPONSE_INVALID")
            return result
        except NotSent:
            if method == "POST" and submitted:
                raise AmbiguousOperation("BACKEND_MUTATION_OUTCOME_UNKNOWN") from None
            raise
        except (OSError, http.client.HTTPException, ValueError, UnicodeError):
            if method == "POST" and submitted:
                raise AmbiguousOperation("BACKEND_MUTATION_OUTCOME_UNKNOWN") from None
            raise BridgeUnavailable("BACKEND_READ_UNAVAILABLE") from None
        finally:
            connection.close()

    def _call(self, method, path, payload=None):
        self._timeout()
        result = (self._transport(method, path, payload) if self._transport is not None
                  else self._http(method, path, payload))
        if not isinstance(result, dict) or len(canonical(result)) > MAX_BODY:
            if method == "POST":
                raise AmbiguousOperation("BACKEND_MUTATION_OUTCOME_UNKNOWN")
            raise BridgeUnavailable("BACKEND_RESPONSE_INVALID")
        return result

    def capabilities(self) -> dict:
        with self._lock:
            expected = {
                "protocol": "harun-agent-bridge-v1", "backend_revision": self.backend_revision,
                "source_sha256": self.backend_source_sha256, "gateway": "EXISTING_BINANCE_FUTURES_LIVE",
                "provider": "NEUROBRO_WEB", "atomic_robot_gate": True,
                "idempotent_handoff": True, "idempotent_cancel_remaining": True,
                "daily_entry_limit": 2, "combined_slot_limit": 2,
                "risk_model": "FEE_SLIPPAGE_RISK_V3"}
            result = self._call("GET", "/capabilities")
            if canonical(result) != canonical(expected):
                raise BridgeUnavailable("BACKEND_CAPABILITIES_UNVERIFIED")
            self._capabilities_at = self._clock()
            return dict(result)

    def _ready(self):
        if self._capabilities_at is None or not 0 <= self._clock() - self._capabilities_at <= 300:
            self.capabilities()

    def _pending(self, row) -> PendingOrder:
        if not isinstance(row, dict):
            raise BridgeUnavailable("PENDING_ORDER_INVALID")
        try:
            identifier, revision = _identity(row["order_id"]), _identity(row["revision"])
            symbol = row["symbol"]
            if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
                raise ValueError()
            created = row["created_at"]
            if type(created) not in (int, float) or not 0 < created <= self._clock():
                raise ValueError()
            quantity = number(row["quantity"])
            executed = number(row["executed_quantity"], zero=True)
            if executed > quantity or type(row["owned"]) is not bool:
                raise ValueError()
            return PendingOrder(identifier, symbol, created, quantity, executed, revision, row["owned"])
        except (KeyError, TypeError, ValueError, RiskRejected):
            raise BridgeUnavailable("PENDING_ORDER_INVALID") from None

    def snapshot(self) -> AccountSnapshot:
        with self._lock:
            self._ready()
            row = self._call("GET", "/snapshot")
            try:
                if row["schema_version"] != 1 or type(row["robot_on"]) is not bool:
                    raise ValueError()
                fresh(row["observed_at"], self._clock(), 30, "STALE_ACCOUNT")
                generation = _identity(row["generation"])
                if type(row["daily_new_trades"]) is not int or not 0 <= row["daily_new_trades"] <= 10000:
                    raise ValueError()
                if not isinstance(row["jakarta_day"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["jakarta_day"]):
                    raise ValueError()
                if (not isinstance(row["positions"], list) or not isinstance(row["pending"], list)
                        or len(row["positions"]) + len(row["pending"]) > 4096
                        or not isinstance(row["supported_symbols"], list)
                        or len(row["supported_symbols"]) > 4096):
                    raise ValueError()
                positions = tuple(Position(item["symbol"], number(item["quantity"], zero=True, signed=True))
                                  for item in row["positions"])
                if any(not isinstance(p.symbol, str) or not SYMBOL.fullmatch(p.symbol) for p in positions):
                    raise ValueError()
                if len({p.symbol for p in positions}) != len(positions):
                    raise ValueError()
                pending = tuple(self._pending(item) for item in row["pending"])
                if len({p.order_id for p in pending}) != len(pending):
                    raise ValueError()
                supported = tuple(row["supported_symbols"])
                if len(set(supported)) != len(supported) or any(not isinstance(s, str) or not SYMBOL.fullmatch(s) for s in supported):
                    raise ValueError()
                authority = row["risk_evidence"]
                if not isinstance(authority, dict) or authority.get("ownership_reconciled") is not True:
                    raise ValueError()
                risk = number(row["risk_usdt"])
                if risk > 100:
                    raise ValueError()
                count = authority.get("occupied_slots")
                account = AccountSnapshot(row["robot_on"], row["observed_at"], generation,
                    row["jakarta_day"], number(row["balance_usdt"], zero=True), risk,
                    row["daily_new_trades"], positions, pending, supported,
                    {"backend_revision": self.backend_revision, "reconciled_occupied_slots": count})
                if type(count) is not int or not account.occupied_slots <= count <= 4096:
                    raise ValueError()
            except (KeyError, TypeError, ValueError, RiskRejected):
                raise BridgeUnavailable("ACCOUNT_SNAPSHOT_UNVERIFIED") from None
            # Bounded cache; caller metadata is never accepted as risk authority.
            self._authorities = {generation: (account, json.loads(canonical(authority)))}
            self._evidence = {"verified": self._transport is None,
                "observed_at": account.observed_at, "source_sha256": self.backend_source_sha256,
                "revision": self.backend_revision, "transport": "AUTHENTICATED_BACKEND_HTTP"
                    if self._transport is None else "INJECTED_TEST_TRANSPORT"}
            return account

    def _current(self, expected: AccountSnapshot):
        if not expected.robot_on:
            raise RobotOff("ROBOT_OFF")
        current = self.snapshot()
        if not current.robot_on:
            raise RobotOff("ROBOT_OFF")
        if current.generation != expected.generation or current.risk_usdt != expected.risk_usdt:
            raise RiskRejected("ACCOUNT_REVISION_CHANGED")
        return current, self._authorities[current.generation][1]

    def handoff(self, setup: NeurobroSetup, market: MarketContext,
                account: AccountSnapshot, idempotency_key: str) -> dict:
        with self._lock:
            if not isinstance(idempotency_key, str) or not HEX64.fullmatch(idempotency_key):
                raise RiskRejected("IDEMPOTENCY_KEY_INVALID")
            current, authority = self._current(account)
            fresh(market.observed_at, self._clock(), 180, "STALE_MARKET")
            if market.symbol != setup.symbol:
                raise RiskRejected("MARKET_SYMBOL_MISMATCH")
            plan = evaluate_setup(setup, current, authority, now=self._clock())
            plan_sha = hashlib.sha256(canonical(plan)).hexdigest()
            payload = {"idempotency_key": idempotency_key, "expected_generation": current.generation,
                       "source": {"provider": "NEUROBRO_WEB", "message_id": setup.source_message_id,
                                  "response_text": setup.source_text, "response_sha256": setup.source_sha256},
                       "plan": plan, "plan_sha256": plan_sha,
                       "market_observed_at": market.observed_at}
            result = self._call("POST", "/handoff", payload)
            if (result.get("accepted") is not True or result.get("robot_on_at_gate") is not True
                    or result.get("plan_sha256") != plan_sha
                    or result.get("idempotency_key") != idempotency_key
                    or not isinstance(result.get("receipt_id"), str)
                    or not IDENTITY.fullmatch(result["receipt_id"])):
                raise AmbiguousOperation("HANDOFF_RESULT_UNVERIFIED")
            return result

    def pending_status(self, order_id: str) -> PendingOrder:
        with self._lock:
            self._ready()
            identifier = _identity(order_id)
            result = self._call("GET", "/pending/" + identifier)
            order = self._pending(result)
            if order.order_id != identifier or result.get("owned") is not True:
                raise RiskRejected("ORDER_OWNERSHIP_UNVERIFIED")
            return order

    def cancel_remaining(self, order: PendingOrder, account: AccountSnapshot,
                         idempotency_key: str) -> dict:
        with self._lock:
            if not isinstance(idempotency_key, str) or not HEX64.fullmatch(idempotency_key):
                raise RiskRejected("IDEMPOTENCY_KEY_INVALID")
            current, _authority = self._current(account)
            observed = self.pending_status(order.order_id)
            if observed != order:
                raise RiskRejected("ORDER_REVISION_OR_FILL_CHANGED")
            remaining = order.quantity - order.executed_quantity
            if remaining <= 0:
                raise RiskRejected("ORDER_ALREADY_FILLED")
            payload = {"idempotency_key": idempotency_key, "expected_generation": current.generation,
                       "order_id": order.order_id, "symbol": order.symbol,
                       "expected_order_revision": order.revision,
                       "expected_executed_quantity": format(order.executed_quantity, "f"),
                       "remaining_quantity": format(remaining, "f")}
            result = self._call("POST", "/cancel-remaining", payload)
            if (result.get("accepted") is not True or result.get("robot_on_at_gate") is not True
                    or result.get("preserves_filled_quantity") is not True
                    or result.get("idempotency_key") != idempotency_key):
                raise AmbiguousOperation("CANCEL_RESULT_UNVERIFIED")
            return result
