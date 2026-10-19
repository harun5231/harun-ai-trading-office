"""Persistent Neurobro WEB adapter: attach to an admitted Chromium session.

The deployed DOM contract must be reconciled with the private browser runtime.
This module deliberately has no guessed production selectors, browser launch,
profile directory, NeuroAPI URL, Binance credentials or order operation.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from .models import AmbiguousOperation, NotSent


class ProviderUnavailable(NotSent):
    pass


class RobotOff(ProviderUnavailable):
    pass


class LoginRequired(ProviderUnavailable):
    pass


class DeliveryUncertain(AmbiguousOperation):
    """A send may have reached the website. Never retry it automatically."""


@dataclass(frozen=True)
class BrowserReply:
    text: str
    conversation_id: str
    response_id: str
    provider: str = "NEUROBRO_WEB"

    @property
    def response_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


_SHA = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_REQUEST = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z", re.ASCII)
_RESPONSE_ID = re.compile(r"[A-Za-z0-9:_-]{1,128}\Z", re.ASCII)
_SELECTORS = frozenset({"account", "composer", "submit", "assistant_response", "assistant_complete", "busy", "login_required", "conversation", "attachment"})


def canonical_response_id(value, conversation_id: str, request_id: str, text: str) -> str:
    """Use a backend-compatible ID without exposing arbitrary website ID text."""
    if isinstance(value, str) and _RESPONSE_ID.fullmatch(value):
        return value
    parts = (conversation_id, request_id, value if isinstance(value, str) else "", text)
    return hashlib.sha256(json.dumps(parts, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def validate_browser_contract(contract: dict[str, Any]) -> dict[str, Any]:
    required = {"schema_version", "provider", "admitted", "source_sha256", "origin", "selectors", "timeout_seconds", "max_response_chars"}
    if not isinstance(contract, dict) or set(contract) != required:
        raise ProviderUnavailable("BROWSER_CONTRACT_FIELDS_UNVERIFIED")
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 1 or contract["provider"] != "NEUROBRO_WEB" or contract["admitted"] is not True:
        raise ProviderUnavailable("BROWSER_CONTRACT_NOT_ADMITTED")
    witness = contract["source_sha256"]
    if not isinstance(witness, str) or not _SHA.fullmatch(witness) or witness == "0" * 64:
        raise ProviderUnavailable("BROWSER_SOURCE_UNVERIFIED")
    origin = urllib.parse.urlsplit(contract["origin"] if isinstance(contract["origin"], str) else "")
    if origin.scheme != "https" or not origin.hostname or origin.username or origin.password or origin.path not in ("", "/") or origin.query or origin.fragment or origin.port not in (None, 443):
        raise ProviderUnavailable("BROWSER_ORIGIN_UNVERIFIED")
    selectors = contract["selectors"]
    if not isinstance(selectors, dict) or set(selectors) != _SELECTORS:
        raise ProviderUnavailable("BROWSER_SELECTORS_UNVERIFIED")
    for selector in selectors.values():
        if not isinstance(selector, str) or not 1 <= len(selector) <= 300 or any(x in selector for x in ("\n", "\r", "\x00", "javascript:", ">>", "xpath=", "text=", "nth=")):
            raise ProviderUnavailable("BROWSER_SELECTOR_UNSAFE")
    if type(contract["timeout_seconds"]) is not int or not 5 <= contract["timeout_seconds"] <= 90:
        raise ProviderUnavailable("BROWSER_TIMEOUT_INVALID")
    if type(contract["max_response_chars"]) is not int or not 1024 <= contract["max_response_chars"] <= 16000:
        raise ProviderUnavailable("BROWSER_RESPONSE_LIMIT_INVALID")
    return json.loads(json.dumps(contract))


class NeurobroBrowser:
    def __init__(self, cdp_url: str, contract: dict[str, Any], robot_enabled: Callable[[], bool], *,
                 playwright_factory: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.contract = validate_browser_contract(contract)
        parsed = urllib.parse.urlsplit(cdp_url)
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
            local = address.is_loopback
        except ValueError:
            local = parsed.hostname == "localhost"
        if (parsed.scheme not in ("http", "https", "ws", "wss") or not local or parsed.username or parsed.password or
                parsed.query or parsed.fragment or not parsed.port or parsed.path not in ("", "/")):
            raise ProviderUnavailable("CDP_REQUIRES_LOCAL_PROTECTED_ENDPOINT")
        if not callable(robot_enabled):
            raise ProviderUnavailable("ROBOT_GATE_REQUIRED")
        self.cdp_url = cdp_url
        self.robot_enabled = robot_enabled
        self._factory = playwright_factory
        self._clock = clock
        self._sent_ids: set[str] = set()
        self._remaining_budget = lambda: 120.0
        self._active_deadline: float | None = None
        self._evidence: dict[str, Any] = {"verified": False}

    def set_remaining_budget(self, callback: Callable[[], float]) -> None:
        if not callable(callback):
            raise ProviderUnavailable("BROWSER_BUDGET_CALLBACK_INVALID")
        self._remaining_budget = callback

    def evidence(self) -> dict[str, Any]:
        return dict(self._evidence)

    def _remaining(self) -> float:
        available = self._remaining_budget()
        if isinstance(available, bool) or not isinstance(available, (int, float)) or not 0 < available <= 120:
            raise ProviderUnavailable("BROWSER_TICK_BUDGET_EXHAUSTED")
        if self._active_deadline is not None:
            available = min(available, self._active_deadline - self._clock())
        if available <= 0:
            raise ProviderUnavailable("BROWSER_TICK_BUDGET_EXHAUSTED")
        return available

    def _gate(self) -> None:
        self._remaining()
        if self.robot_enabled() is not True:
            raise RobotOff("ROBOT_OFF")

    def _connect(self):
        if self._factory is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                raise ProviderUnavailable("PLAYWRIGHT_NOT_INSTALLED") from None
            factory = sync_playwright
        else:
            factory = self._factory
        manager = factory()
        engine = manager.start()
        try:
            browser = engine.chromium.connect_over_cdp(self.cdp_url, timeout=max(1, min(15000, self._remaining() * 1000)))
            wanted = self.contract["origin"].rstrip("/")
            pages = []
            for context in browser.contexts:
                for page in context.pages:
                    parsed = urllib.parse.urlsplit(page.url)
                    if f"{parsed.scheme}://{parsed.netloc}" == wanted and not page.is_closed():
                        pages.append(page)
            if len(pages) != 1:
                raise LoginRequired("NEUROBRO_EXISTING_PAGE_UNVERIFIED")
            page = pages[0]
            page.set_default_timeout(max(1, min(5000, self._remaining() * 1000)))
            return engine, page
        except BaseException:
            engine.stop()  # Disconnect Playwright; never close the existing Chromium.
            raise

    def _visible(self, page, key: str):
        locator = page.locator(self.contract["selectors"][key])
        count = locator.count()
        if count > 500:
            raise ProviderUnavailable("BROWSER_CONTRACT_MATCH_LIMIT")
        return [locator.nth(i) for i in range(count) if locator.nth(i).is_visible()]

    def _session(self, page):
        if self._visible(page, "login_required"):
            raise LoginRequired("NEUROBRO_LOGIN_REQUIRED")
        accounts = self._visible(page, "account")
        composers = self._visible(page, "composer")
        markers = self._visible(page, "conversation")
        if len(accounts) != 1 or len(composers) != 1 or len(markers) != 1:
            raise LoginRequired("NEUROBRO_SESSION_CONTRACT_UNVERIFIED")
        conversation = markers[0].get_attribute("data-conversation-id")
        if not isinstance(conversation, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", conversation):
            raise ProviderUnavailable("CONVERSATION_ID_UNVERIFIED")
        # Return only a stable opaque identifier, never account text or profile data.
        conversation_id = hashlib.sha256((self.contract["origin"] + "\0" + conversation).encode()).hexdigest()
        return composers[0], conversation_id

    def status(self) -> dict[str, Any]:
        """Read session markers only; safe while the robot is OFF."""
        engine = None
        try:
            engine, page = self._connect()
            _, conversation_id = self._session(page)
            return {"provider": "NEUROBRO_WEB", "status": "CONNECTED", "conversation_id": conversation_id, "source_sha256": self.contract["source_sha256"]}
        except LoginRequired:
            return {"provider": "NEUROBRO_WEB", "status": "LOGIN_REQUIRED"}
        except Exception:
            return {"provider": "NEUROBRO_WEB", "status": "UNAVAILABLE"}
        finally:
            if engine is not None:
                engine.stop()

    def exchange(self, message: str, attachments_png=(), conversation_id: str | None = None,
                 request_id: str | None = None) -> BrowserReply:
        self._gate()
        if not isinstance(message, str) or not 1 <= len(message) <= 32000 or "\x00" in message:
            raise ProviderUnavailable("CHAT_MESSAGE_INVALID")
        if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
            raise ProviderUnavailable("CHAT_REQUEST_ID_REQUIRED")
        if request_id in self._sent_ids:
            raise DeliveryUncertain("CHAT_REQUEST_ALREADY_ATTEMPTED")
        if not isinstance(attachments_png, (tuple, list)) or len(attachments_png) > 2:
            raise ProviderUnavailable("CHAT_ATTACHMENT_COUNT_INVALID")
        for data in attachments_png:
            if not isinstance(data, bytes) or not data.startswith(b"\x89PNG\r\n\x1a\n") or not 32 <= len(data) <= 2 * 1024 * 1024:
                raise ProviderUnavailable("CHAT_ATTACHMENT_NOT_BOUNDED_PNG")
        engine = None
        submitted = False
        started = self._clock()
        self._active_deadline = started + min(self.contract["timeout_seconds"], self._remaining())
        try:
            engine, page = self._connect()
            composer, current_conversation = self._session(page)
            if conversation_id is not None and conversation_id != current_conversation:
                raise ProviderUnavailable("CONVERSATION_CHANGED")
            if self._visible(page, "busy"):
                raise ProviderUnavailable("NEUROBRO_RESPONSE_ALREADY_IN_PROGRESS")
            previous = self._visible(page, "assistant_response")
            previous_count = len(previous)
            previous_complete_count = len(self._visible(page, "assistant_complete"))
            if attachments_png:
                attachments = page.locator(self.contract["selectors"]["attachment"])
                if attachments.count() != 1:
                    raise ProviderUnavailable("ATTACHMENT_INPUT_UNVERIFIED")
                self._gate()
                attachments.set_input_files([{"name": f"binance-futures-{i+1}.png", "mimeType": "image/png", "buffer": data} for i, data in enumerate(attachments_png)])
            self._gate()
            composer.fill(message)
            # Text is verified exactly, so the user's fixed analysis prompt is not rewritten.
            if composer.get_attribute("contenteditable") == "true":
                actual = composer.inner_text()
            else:
                actual = composer.input_value()
            if actual != message:
                raise ProviderUnavailable("COMPOSER_TEXT_NOT_PRESERVED")
            submits = self._visible(page, "submit")
            if len(submits) != 1 or not submits[0].is_enabled():
                raise ProviderUnavailable("SUBMIT_CONTROL_UNVERIFIED")
            self._gate()
            self._sent_ids.add(request_id)
            submitted = True
            submits[0].click()
            stable = None
            stable_since = None
            while self._clock() < self._active_deadline:
                # OFF immediately stops all further browser interaction; no cancellation is sent.
                self._gate()
                _, observed_conversation = self._session(page)
                if observed_conversation != current_conversation:
                    raise DeliveryUncertain("CONVERSATION_CHANGED_AFTER_SUBMIT")
                responses = self._visible(page, "assistant_response")
                if len(responses) > previous_count:
                    if len(responses) != previous_count + 1:
                        raise DeliveryUncertain("NEUROBRO_RESPONSE_ATTRIBUTION_AMBIGUOUS")
                    text = responses[-1].inner_text()
                    if len(text) > self.contract["max_response_chars"]:
                        raise DeliveryUncertain("NEUROBRO_RESPONSE_TOO_LARGE")
                    completed = self._visible(page, "assistant_complete")
                    completion_proven = len(completed) == previous_complete_count + 1 and completed[-1].inner_text() == text
                    if text and completion_proven and not self._visible(page, "busy"):
                        if stable != text:
                            stable, stable_since = text, self._clock()
                        elif stable_since is not None and self._clock() - stable_since >= 2:
                            response_id = responses[-1].get_attribute("data-message-id")
                            response_id = canonical_response_id(response_id, current_conversation, request_id, text)
                            if self._factory is None:
                                self._evidence = {"verified": True, "provider": "NEUROBRO_WEB", "observed_at": time.time(),
                                    "source_sha256": self.contract["source_sha256"],
                                    "contract_sha256": hashlib.sha256(json.dumps(self.contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                                    "conversation_id": current_conversation, "response_sha256": hashlib.sha256(text.encode()).hexdigest()}
                            return BrowserReply(text, current_conversation, response_id)
                    else:
                        stable, stable_since = None, None
                page.wait_for_timeout(250)
            raise DeliveryUncertain("NEUROBRO_RESPONSE_TIMEOUT_NO_AUTOMATIC_RETRY")
        except DeliveryUncertain:
            raise
        except ProviderUnavailable:
            if submitted:
                raise DeliveryUncertain("NEUROBRO_STATE_CHANGED_AFTER_SUBMIT_NO_AUTOMATIC_RETRY") from None
            raise
        except KeyboardInterrupt:
            raise
        except Exception:
            if submitted:
                raise DeliveryUncertain("NEUROBRO_DELIVERY_UNCERTAIN_NO_AUTOMATIC_RETRY") from None
            raise ProviderUnavailable("NEUROBRO_BROWSER_UNAVAILABLE") from None
        finally:
            self._active_deadline = None
            if engine is not None:
                engine.stop()
