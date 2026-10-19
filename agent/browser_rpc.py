"""Origin-scoped browser RPC; the agent receives no Chromium/CDP capability.

Run the service inside the separately reviewed browser boundary, not in the
production worker. Only status and a bounded Neurobro exchange are exposed.
This protocol is not evidence that the private VPS browser has been reconciled.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import sqlite3
import ssl
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .bridge import _token, canonical, unique_pairs
from .models import NotSent
from .providers import BrowserReply, DeliveryUncertain, LoginRequired, ProviderUnavailable, RobotOff, canonical_response_id


SHA = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
REQUEST = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z", re.ASCII)
PENDING_HEADER = re.compile(r"PENDING_REVIEW symbol=([A-Z0-9]{2,24}USDT)\norder_ref: ([0-9a-f]{64})\n", re.ASCII)
MAX_REQUEST = 6 * 1024 * 1024
MAX_RESPONSE = 65536


class NeurobroRPC:
    """Authenticated app-scoped RPC client with no arbitrary method or URL."""
    def __init__(self, base_url, token_file, source_sha256, robot_enabled, *, transport=None, clock=time.time):
        try:
            url = urlsplit(base_url)
            if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or
                    url.query or url.fragment or url.path not in ("", "/") or
                    url.scheme == "http" and url.hostname != "127.0.0.1" or
                    not isinstance(source_sha256, str) or not SHA.fullmatch(source_sha256) or source_sha256 == "0" * 64):
                raise ValueError()
            self.port = url.port
        except (TypeError, ValueError):
            raise ProviderUnavailable("BROWSER_RPC_CONFIGURATION_UNVERIFIED") from None
        if not callable(robot_enabled):
            raise ProviderUnavailable("ROBOT_GATE_REQUIRED")
        self.scheme, self.host = url.scheme, url.hostname
        self.token_file, self.source_sha256 = token_file, source_sha256
        self.robot_enabled, self._transport, self._clock = robot_enabled, transport, clock
        self._remaining_budget = lambda: 120.0
        self._evidence = {"verified": False}

    def set_remaining_budget(self, callback):
        if not callable(callback):
            raise ProviderUnavailable("BROWSER_BUDGET_CALLBACK_INVALID")
        self._remaining_budget = callback

    def evidence(self):
        return dict(self._evidence)

    def _remaining(self):
        value = self._remaining_budget()
        if type(value) not in (int, float) or not 0 < value <= 120:
            raise ProviderUnavailable("BROWSER_TICK_BUDGET_EXHAUSTED")
        return min(value, 90)

    def _http(self, method, suffix, payload):
        timeout = self._remaining()
        if self._transport is not None:
            return self._transport(method, "/neurobro/v1" + suffix, payload)
        connection = (http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=ssl.create_default_context())
                      if self.scheme == "https" else http.client.HTTPConnection(self.host, self.port, timeout=timeout))
        submitted = False
        try:
            body = None if payload is None else canonical(payload)
            if body is not None and len(body) > MAX_REQUEST:
                raise ProviderUnavailable("BROWSER_RPC_REQUEST_TOO_LARGE")
            authorization = "Bearer " + _token(self.token_file)
            deadline = time.monotonic() + timeout
            submitted = method == "POST"
            connection.request(method, "/neurobro/v1" + suffix, body, {"Authorization": authorization, "Content-Type": "application/json", "Accept": "application/json"})
            sock = connection.sock
            response = connection.getresponse()
            expected_length = response.length
            if expected_length is not None and expected_length > MAX_RESPONSE:
                raise ValueError()
            chunks, size = [], 0
            while size <= MAX_RESPONSE:
                if response.isclosed() or response.length == 0:
                    break
                remaining = min(self._remaining(), deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError()
                if sock is not None:
                    sock.settimeout(remaining)
                block = response.read1(min(16384, MAX_RESPONSE + 1 - size))
                if not block:
                    break
                chunks.append(block)
                size += len(block)
            if (size > MAX_RESPONSE or expected_length is not None and size != expected_length or
                    response.getheader("Content-Type", "").split(";", 1)[0].lower() != "application/json"):
                raise ValueError()
            result = json.loads(b"".join(chunks), object_pairs_hook=unique_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if response.status != 200:
                if isinstance(result, dict) and result.get("not_sent") is True and result.get("reason") in {"ROBOT_OFF", "LOGIN_REQUIRED", "CONTRACT_UNAVAILABLE", "REQUEST_INVALID", "BUSY", "AUTH_REQUIRED", "AUTH_UNAVAILABLE"}:
                    if result["reason"] == "LOGIN_REQUIRED":
                        raise LoginRequired("NEUROBRO_LOGIN_REQUIRED")
                    if result["reason"] == "ROBOT_OFF":
                        raise RobotOff("ROBOT_OFF")
                    raise ProviderUnavailable("BROWSER_RPC_REQUEST_NOT_SENT")
                raise DeliveryUncertain("BROWSER_RPC_OUTCOME_REQUIRES_REVIEW")
            return result
        except (ProviderUnavailable, DeliveryUncertain):
            raise
        except Exception:
            if submitted:
                raise DeliveryUncertain("BROWSER_RPC_DELIVERY_UNCERTAIN_NO_AUTOMATIC_RETRY") from None
            raise ProviderUnavailable("BROWSER_RPC_UNAVAILABLE") from None
        finally:
            connection.close()

    def _binding(self, result):
        if (not isinstance(result, dict) or type(result.get("schema_version")) is not int or result.get("schema_version") != 1 or
                result.get("provider") != "NEUROBRO_WEB" or result.get("source_sha256") != self.source_sha256):
            raise ProviderUnavailable("BROWSER_RPC_SOURCE_MISMATCH")
        observed = result.get("observed_at")
        if type(observed) not in (int, float) or not 0 <= self._clock() - observed <= 120:
            raise ProviderUnavailable("BROWSER_RPC_OBSERVATION_STALE")

    def status(self):
        result = self._http("GET", "/status", None)
        self._binding(result)
        if result.get("status") not in ("CONNECTED", "LOGIN_REQUIRED", "UNAVAILABLE"):
            raise ProviderUnavailable("BROWSER_RPC_STATUS_INVALID")
        return result

    def exchange(self, message, attachments_png=(), conversation_id=None, request_id=None):
        if self.robot_enabled() is not True:
            raise RobotOff("ROBOT_OFF")
        if not isinstance(message, str) or not 1 <= len(message) <= 32000 or "\0" in message or not isinstance(request_id, str) or not REQUEST.fullmatch(request_id):
            raise ProviderUnavailable("CHAT_REQUEST_INVALID")
        if not isinstance(attachments_png, (tuple, list)) or len(attachments_png) > 2:
            raise ProviderUnavailable("CHAT_ATTACHMENT_COUNT_INVALID")
        for data in attachments_png:
            if not isinstance(data, bytes) or not data.startswith(b"\x89PNG\r\n\x1a\n") or not 32 <= len(data) <= 2 * 1024 * 1024:
                raise ProviderUnavailable("CHAT_ATTACHMENT_NOT_BOUNDED_PNG")
        if conversation_id is not None and (not isinstance(conversation_id, str) or not SHA.fullmatch(conversation_id)):
            raise ProviderUnavailable("CONVERSATION_ID_UNVERIFIED")
        payload = {"schema_version": 1, "source_sha256": self.source_sha256, "message": message,
                   "attachments_png": [base64.b64encode(data).decode("ascii") for data in attachments_png],
                   "conversation_id": conversation_id, "request_id": request_id}
        self._remaining()
        if self.robot_enabled() is not True:
            raise RobotOff("ROBOT_OFF")
        result = self._http("POST", "/exchange", payload)
        try:
            self._binding(result)
            if set(result) != {"schema_version", "provider", "source_sha256", "observed_at", "text", "conversation_id", "response_id", "response_sha256"}:
                raise ValueError()
            reply = BrowserReply(result["text"], result["conversation_id"], result["response_id"])
            if (not isinstance(reply.text, str) or not 1 <= len(reply.text) <= 16000 or
                    not isinstance(reply.conversation_id, str) or not SHA.fullmatch(reply.conversation_id) or
                    not isinstance(reply.response_id, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", reply.response_id, re.ASCII) or
                    reply.response_sha256 != result["response_sha256"]):
                raise ValueError()
        except Exception:
            raise DeliveryUncertain("BROWSER_RPC_REPLY_UNVERIFIED_NO_AUTOMATIC_RETRY") from None
        if self._transport is None:
            self._evidence = {"verified": True, "observed_at": self._clock(), "provider": "NEUROBRO_WEB", "source_sha256": self.source_sha256, "response_sha256": reply.response_sha256}
        return reply


class BrowserRPCService:
    """Narrow real server boundary and durable send receipts, independent of trading DB."""
    def __init__(self, browser, token_file, receipt_database, *, clock=time.time, token_reader=None):
        self.browser, self.token_file, self.clock = browser, token_file, clock
        self._token_reader = token_reader or (lambda: _token(token_file))
        self.receipt_database = receipt_database
        fd = os.open(receipt_database, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            item = os.fstat(fd)
            if not stat.S_ISREG(item.st_mode) or item.st_uid != os.geteuid() or stat.S_IMODE(item.st_mode) != 0o600 or item.st_nlink != 1:
                raise ProviderUnavailable("BROWSER_RPC_RECEIPT_DATABASE_UNSAFE")
            self.database_identity = (item.st_dev, item.st_ino)
        finally:
            os.close(fd)
        self.database = sqlite3.connect(receipt_database, check_same_thread=False)
        current = os.lstat(receipt_database)
        if (current.st_dev, current.st_ino) != self.database_identity:
            self.database.close()
            raise ProviderUnavailable("BROWSER_RPC_RECEIPT_DATABASE_CHANGED")
        self.database.execute("PRAGMA trusted_schema=OFF")
        self.database.execute("CREATE TABLE IF NOT EXISTS sends (id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, reply TEXT, request TEXT, origin TEXT, response_id TEXT)")
        # Preserve earlier app-only receipts. Missing request/origin in an old
        # row can never be promoted into independently verified source evidence.
        columns = {row[1] for row in self.database.execute("PRAGMA table_info(sends)")}
        for name in ("request", "origin", "response_id"):
            if name not in columns:
                self.database.execute("ALTER TABLE sends ADD COLUMN " + name + " TEXT")
        self.database.execute("CREATE INDEX IF NOT EXISTS sends_response_origin ON sends(response_id,state)")
        self.database.commit()
        self.lock = threading.Lock()

    def close(self):
        self.database.close()

    def source_receipt(self, message_id):
        """Trusted backend callback only; no source-evidence HTTP endpoint.

        The agent supplies a message identity, never the receipt or its fields.
        Prompt, request identity, reply and observed time come from the browser's
        own durable completed send. UNKNOWN and legacy rows are inadmissible.
        """
        if not isinstance(message_id, str) or not REQUEST.fullmatch(message_id):
            return None
        with self.lock:
            rows = self.database.execute("SELECT id,digest,state,reply,request,origin FROM sends WHERE response_id=? AND state='DONE' LIMIT 2", (message_id,)).fetchall()
            if len(rows) != 1:
                return None
            matches = []
            for request_id, digest, state, raw_reply, raw_request, raw_origin in rows:
                try:
                    if raw_request is None or raw_origin is None:
                        continue
                    reply = json.loads(raw_reply, object_pairs_hook=unique_pairs)
                    if reply.get("response_id") != message_id:
                        continue
                    payload = json.loads(raw_request, object_pairs_hook=unique_pairs)
                    origin = json.loads(raw_origin, object_pairs_hook=unique_pairs)
                    if (hashlib.sha256(canonical(payload)).hexdigest() != digest or payload["request_id"] != request_id or
                            origin.get("verified") is not True or origin.get("provider") != "NEUROBRO_WEB" or
                            origin.get("source_sha256") != self.browser.contract["source_sha256"] or
                            reply["source_sha256"] != origin["source_sha256"] or
                            reply["response_sha256"] != hashlib.sha256(reply["text"].encode()).hexdigest() or
                            origin.get("response_sha256") != reply["response_sha256"] or
                            type(reply["observed_at"]) not in (int, float) or
                            type(origin.get("observed_at")) not in (int, float) or
                            not 0 <= reply["observed_at"] - origin["observed_at"] <= 120):
                        continue
                    request_text = payload["message"]
                    receipt = {"state": "COMPLETED", "verified": True, "provider": "NEUROBRO_WEB",
                        "source_sha256": reply["source_sha256"], "message_id": message_id,
                        "text": reply["text"], "response_sha256": reply["response_sha256"],
                        "observed_at": reply["observed_at"], "request_id": request_id,
                        "request_text": request_text, "conversation_id": reply["conversation_id"]}
                    header = PENDING_HEADER.match(request_text)
                    if header:
                        symbol, order_ref = header.groups()
                        template = (f"Evaluasi LIMIT {symbol} yang belum terisi penuh selama 5 jam. "
                            "Berdasarkan chart dan data terbaru, Neurobro pilih TAHAN atau BATALKAN sisa pending. "
                            "Jangan ubah posisi yang telah terisi. Jawab keputusan eksplisit dan alasan singkat.")
                        remainder = request_text[header.end():]
                        followup = remainder[len(template):] if remainder.startswith(template) else None
                        if followup is None or (followup and not (followup.startswith("\nPertanyaan lanjutan: ") and 0 < len(followup[len("\nPertanyaan lanjutan: "):]) <= 4000)):
                            continue
                        receipt.update(phase="PENDING_REVIEW", symbol=symbol, order_ref=order_ref)
                    else:
                        symbols = set(re.findall(r"(?<![A-Z0-9])([A-Z0-9]{2,18}USDT)(?![A-Z0-9])", reply["text"].upper()))
                        receipt.update(phase="OTHER", symbol=next(iter(symbols)) if len(symbols) == 1 else None, order_ref=None)
                    matches.append(receipt)
                except (KeyError, TypeError, ValueError, UnicodeError):
                    continue
            # A repeated website message ID cannot select one of several origins.
            return matches[0] if len(matches) == 1 else None

    def dispatch(self, method, path, authorization, raw=b""):
        try:
            if not isinstance(authorization, str) or not hmac.compare_digest(authorization, "Bearer " + self._token_reader()):
                return 401, {"not_sent": True, "reason": "AUTH_REQUIRED"}
        except Exception:
            return 503, {"not_sent": True, "reason": "AUTH_UNAVAILABLE"}
        if path not in ("/neurobro/v1/status", "/neurobro/v1/exchange"):
            return 404, {"reason": "ENDPOINT_REFUSED"}
        source = self.browser.contract["source_sha256"]
        if method == "GET" and path.endswith("/status"):
            status = self.browser.status()
            return 200, {"schema_version": 1, "provider": "NEUROBRO_WEB", "source_sha256": source, "observed_at": self.clock(), "status": status["status"]}
        if method != "POST" or not path.endswith("/exchange") or not isinstance(raw, bytes) or len(raw) > MAX_REQUEST:
            return 400, {"not_sent": True, "reason": "REQUEST_INVALID"}
        try:
            payload = json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if set(payload) != {"schema_version", "source_sha256", "message", "attachments_png", "conversation_id", "request_id"} or type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or payload["source_sha256"] != source:
                raise ValueError()
            if not isinstance(payload["request_id"], str) or not REQUEST.fullmatch(payload["request_id"]):
                raise ValueError()
            if not isinstance(payload["message"], str) or not 1 <= len(payload["message"]) <= 32000 or "\0" in payload["message"]:
                raise ValueError()
            if not isinstance(payload["attachments_png"], list) or len(payload["attachments_png"]) > 2:
                raise ValueError()
            images = []
            for value in payload["attachments_png"]:
                if not isinstance(value, str) or len(value) > 2_796_204:
                    raise ValueError()
                image = base64.b64decode(value, validate=True)
                if not image.startswith(b"\x89PNG\r\n\x1a\n") or not 32 <= len(image) <= 2 * 1024 * 1024:
                    raise ValueError()
                images.append(image)
            conversation = payload["conversation_id"]
            if conversation is not None and (not isinstance(conversation, str) or not SHA.fullmatch(conversation)):
                raise ValueError()
        except Exception:
            return 400, {"not_sent": True, "reason": "REQUEST_INVALID"}
        if not self.lock.acquire(blocking=False):
            return 409, {"not_sent": True, "reason": "BUSY"}
        try:
            current = os.lstat(self.receipt_database)
            if (not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != self.database_identity or
                    current.st_uid != os.geteuid() or stat.S_IMODE(current.st_mode) != 0o600 or current.st_nlink != 1 or
                    current.st_size > 256 * 1024 * 1024):
                return 503, {"not_sent": True, "reason": "CONTRACT_UNAVAILABLE"}
            digest = hashlib.sha256(canonical(payload)).hexdigest()
            old = self.database.execute("SELECT digest,state,reply FROM sends WHERE id=?", (payload["request_id"],)).fetchone()
            if old:
                if old[0] == digest and old[1] == "DONE":
                    return 200, json.loads(old[2])
                if old[0] == digest and old[1] == "NOT_SENT":
                    return 409, {"not_sent": True, "reason": "CONTRACT_UNAVAILABLE"}
                return 409, {"reason": "OUTCOME_REQUIRES_REVIEW"}
            self.database.execute("INSERT INTO sends (id,digest,state,reply,request,origin) VALUES (?,?,?,NULL,?,NULL)",
                                  (payload["request_id"], digest, "PENDING", canonical(payload).decode()))
            self.database.commit()  # Persist before the first website operation.
            try:
                reply = self.browser.exchange(payload["message"], attachments_png=tuple(images), conversation_id=conversation, request_id=payload["request_id"])
            except NotSent as exc:
                self.database.execute("UPDATE sends SET state='NOT_SENT' WHERE id=?", (payload["request_id"],))
                self.database.commit()
                reason = "LOGIN_REQUIRED" if isinstance(exc, LoginRequired) else "ROBOT_OFF" if isinstance(exc, RobotOff) else "CONTRACT_UNAVAILABLE"
                return 409, {"not_sent": True, "reason": reason}
            except BaseException:
                self.database.execute("UPDATE sends SET state='UNKNOWN' WHERE id=?", (payload["request_id"],))
                self.database.commit()
                return 409, {"reason": "OUTCOME_REQUIRES_REVIEW"}
            response_id = canonical_response_id(reply.response_id, reply.conversation_id, payload["request_id"], reply.text)
            result = {"schema_version": 1, "provider": "NEUROBRO_WEB", "source_sha256": source, "observed_at": self.clock(), "text": reply.text, "conversation_id": reply.conversation_id, "response_id": response_id, "response_sha256": reply.response_sha256}
            evidence = self.browser.evidence() if callable(getattr(self.browser, "evidence", None)) else {"verified": False}
            self.database.execute("UPDATE sends SET state='DONE',reply=?,origin=?,response_id=? WHERE id=?",
                                  (canonical(result).decode(), canonical(evidence).decode(), response_id, payload["request_id"]))
            self.database.commit()
            return 200, result
        finally:
            self.lock.release()

    def server(self, port=0):
        """Bind loopback for a separately reviewed TLS frontend, never a public CDP proxy."""
        application = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            def log_message(self, *_):
                pass
            def do_GET(self):
                self._handle()
            def do_POST(self):
                self._handle()
            def _handle(self):
                self.connection.settimeout(15)
                try:
                    value = self.headers.get("Content-Length", "0")
                    if len(self.headers.get_all("Content-Length", [])) > 1 or not value.isdecimal() or int(value) > MAX_REQUEST or self.headers.get("Transfer-Encoding"):
                        raise ValueError()
                    remaining_size, chunks = int(value), []
                    deadline = time.monotonic() + 15
                    while remaining_size:
                        remaining_time = deadline - time.monotonic()
                        if remaining_time <= 0:
                            raise TimeoutError()
                        self.connection.settimeout(remaining_time)
                        chunk = self.rfile.read1(min(16384, remaining_size))
                        if not chunk:
                            raise ValueError()
                        chunks.append(chunk)
                        remaining_size -= len(chunk)
                    raw = b"".join(chunks)
                    status, result = application.dispatch(self.command, self.path, self.headers.get("Authorization"), raw)
                except Exception:
                    status, result = 400, {"reason": "REQUEST_INVALID"}
                body = canonical(result)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
        return ThreadingHTTPServer(("127.0.0.1", port), Handler)
