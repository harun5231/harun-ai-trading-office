import copy
import unittest

from agent.providers import DeliveryUncertain, LoginRequired, NeurobroBrowser, ProviderUnavailable, RobotOff


def contract():
    return {"schema_version": 1, "provider": "NEUROBRO_WEB", "admitted": True,
            "source_sha256": "a" * 64, "origin": "https://chat.example.test",
            "selectors": {name: "#" + name for name in ("account", "composer", "submit", "assistant_response", "assistant_complete", "busy", "login_required", "conversation", "attachment")},
            "timeout_seconds": 5, "max_response_chars": 8192}


class Element:
    def __init__(self, page, role, *, visible=True):
        self.page, self.role, self.visible = page, role, visible

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return True

    def get_attribute(self, name):
        if name == "data-conversation-id":
            return "conversation-1"
        if name == "data-message-id":
            return getattr(self.page, "response_id", "reply-1")
        return None

    def fill(self, text):
        self.page.composed = text

    def input_value(self):
        return self.page.composed

    def inner_text(self):
        return "HOLD. Wait for confirmation."

    def click(self):
        self.page.submits += 1
        if self.page.fail_click:
            raise OSError("private exception detail never exported")

    def set_input_files(self, files):
        self.page.attachments = files


class Locator:
    def __init__(self, items):
        self.items = items

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]

    def set_input_files(self, files):
        self.items[0].set_input_files(files)


class Page:
    def __init__(self):
        self.url = "https://chat.example.test/chat/one"
        self.submits = 0
        self.seconds = 0.0
        self.fail_click = False
        self.login_expired = False
        self.respond = True
        self.attachments = []
        self.composed = ""

    def is_closed(self):
        return False

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    def locator(self, selector):
        role = selector[1:]
        if role == "login_required":
            return Locator([Element(self, role)] if self.login_expired else [])
        if role == "busy":
            return Locator([])
        if role in ("assistant_response", "assistant_complete"):
            return Locator([Element(self, role)] if self.submits and self.respond else [])
        if role == "composer":
            # Responsive DOM may include one hidden duplicate; only the visible one is used.
            return Locator([Element(self, role), Element(self, role, visible=False)])
        return Locator([Element(self, role)])

    def wait_for_timeout(self, milliseconds):
        self.seconds += milliseconds / 1000


class Engine:
    def __init__(self, page):
        self.contexts = [type("Context", (), {"pages": [page]})()]
        self.chromium = self
        self.stopped = 0
        self.connected = 0

    def start(self):
        return self

    def connect_over_cdp(self, url, timeout):
        self.connected += 1
        return self

    def stop(self):
        self.stopped += 1

    def close(self):
        raise AssertionError("The existing Chromium must never be closed")


class ProviderTests(unittest.TestCase):
    def setup_provider(self, gate=lambda: True):
        page = Page()
        engine = Engine(page)
        client = NeurobroBrowser("http://127.0.0.1:9222", contract(), gate, playwright_factory=lambda: engine, clock=lambda: page.seconds)
        return client, page, engine

    def test_connects_existing_page_and_returns_source_attributed_reply(self):
        client, page, engine = self.setup_provider()
        reply = client.exchange("Exact user prompt\nDo not rewrite.", request_id="cycle-1:analysis")
        self.assertEqual(page.composed, "Exact user prompt\nDo not rewrite.")
        self.assertEqual(page.submits, 1)
        self.assertEqual(reply.provider, "NEUROBRO_WEB")
        self.assertEqual(reply.response_id, "reply-1")
        self.assertEqual(len(reply.response_sha256), 64)
        self.assertEqual(engine.stopped, 1)

    def test_off_performs_no_browser_or_network_operation(self):
        client, page, engine = self.setup_provider(lambda: False)
        with self.assertRaises(RobotOff):
            client.exchange("Screen coins", request_id="off-1")
        self.assertEqual(engine.connected, 0)
        self.assertEqual(page.submits, 0)

    def test_session_status_is_passive_while_off(self):
        client, page, engine = self.setup_provider(lambda: False)
        self.assertEqual(client.status()["status"], "CONNECTED")
        self.assertEqual(page.submits, 0)

    def test_expired_login_never_submits(self):
        client, page, engine = self.setup_provider()
        page.login_expired = True
        with self.assertRaises(LoginRequired):
            client.exchange("Question", request_id="login-1")
        self.assertEqual(page.submits, 0)
        self.assertEqual(client.status()["status"], "LOGIN_REQUIRED")

    def test_duplicate_and_timeout_cannot_resend(self):
        client, page, engine = self.setup_provider()
        page.respond = False
        with self.assertRaises(DeliveryUncertain):
            client.exchange("Question", request_id="uncertain-1")
        with self.assertRaises(DeliveryUncertain):
            client.exchange("Question", request_id="uncertain-1")
        self.assertEqual(page.submits, 1)
        self.assertGreaterEqual(page.seconds, 5)

    def test_click_error_is_redacted_and_not_retried(self):
        client, page, engine = self.setup_provider()
        page.fail_click = True
        with self.assertRaises(DeliveryUncertain) as captured:
            client.exchange("Question", request_id="click-1")
        self.assertNotIn("private", str(captured.exception))
        self.assertEqual(page.submits, 1)

    def test_turning_off_during_poll_stops_without_cancel_action(self):
        client, page, engine = self.setup_provider()
        client.robot_enabled = lambda: page.seconds < 0.5
        with self.assertRaises(DeliveryUncertain):
            client.exchange("Question", request_id="off-mid-send")
        self.assertEqual(page.submits, 1)
        self.assertEqual(engine.stopped, 1)

    def test_budget_api_bounds_exchange_and_injected_engine_never_claims_live_evidence(self):
        client, page, engine = self.setup_provider()
        client.set_remaining_budget(lambda: 0.5 - page.seconds)
        with self.assertRaises(DeliveryUncertain):
            client.exchange("Question", request_id="budget-bounded")
        self.assertEqual(page.submits, 1)
        self.assertLessEqual(page.seconds, 0.5)
        self.assertFalse(client.evidence()["verified"])

    def test_pre_submit_login_is_not_sent_but_uncertain_delivery_is_not(self):
        from agent.models import NotSent
        self.assertTrue(issubclass(LoginRequired, NotSent))
        self.assertFalse(issubclass(DeliveryUncertain, NotSent))

    def test_website_id_with_dots_is_hashed_to_backend_compatible_identity(self):
        client, page, engine = self.setup_provider()
        page.response_id = "website.response.001"
        reply = client.exchange("Question", request_id="canonical-id-request")
        self.assertRegex(reply.response_id, r"^[0-9a-f]{64}$")
        self.assertNotIn(".", reply.response_id)

    def test_conversation_changes_and_multiple_matching_pages_refused(self):
        client, page, engine = self.setup_provider()
        with self.assertRaises(ProviderUnavailable):
            client.exchange("Question", request_id="context-1", conversation_id="wrong")
        self.assertEqual(page.submits, 0)
        engine.contexts[0].pages.append(Page())
        with self.assertRaises(LoginRequired):
            client.exchange("Question", request_id="context-2")
        self.assertEqual(page.submits, 0)

    def test_png_attached_from_memory_without_reading_profile_files(self):
        client, page, engine = self.setup_provider()
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 50
        client.exchange("Question", attachments_png=(png, png), request_id="images-1")
        self.assertEqual(len(page.attachments), 2)
        self.assertTrue(all(item["mimeType"] == "image/png" and item["buffer"] == png for item in page.attachments))

    def test_refuses_unadmitted_contract_and_unprotected_cdp(self):
        bad = copy.deepcopy(contract())
        bad["admitted"] = False
        with self.assertRaises(ProviderUnavailable):
            NeurobroBrowser("http://127.0.0.1:9222", bad, lambda: True)
        for url in ("http://10.10.0.241:9222", "http://user:secret@127.0.0.1:9222", "http://127.0.0.1:9222?token=secret", "http://example.test:9222"):
            with self.subTest(url=url), self.assertRaises(ProviderUnavailable):
                NeurobroBrowser(url, contract(), lambda: True)


if __name__ == "__main__":
    unittest.main()
