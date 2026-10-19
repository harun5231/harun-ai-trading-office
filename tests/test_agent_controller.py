from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import unittest

from agent.bridge import BackendBridge
from agent.controller import Controller
from agent.memory import Memory
from agent.models import DialogueDecision, PendingOrder, Position, NotSent
from agent.providers import BrowserReply
from agent.risk import parse_setup
from worker.prompts import ANALYSIS, SCREENING
from test_agent_risk import example_account
from test_agent_bridge import Backend, SOURCE, pending_row


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
SETUP = "BTCUSDT\nLONG\nENTRY: 100\nTP: 110\nSL: 99"


class StubBridge:
    def __init__(self):
        self.account = example_account()
        self.handoffs, self.cancellations = [], []
        self.statuses = []

    def snapshot(self):
        return self.account

    def handoff(self, setup, market, account, key):
        self.handoffs.append((setup, market, key))
        self.account = replace(self.account, daily_new_trades=self.account.daily_new_trades + 1,
            pending=self.account.pending + (PendingOrder("order" + str(len(self.handoffs)), setup.symbol,
                time.time(), Decimal("1"), Decimal("0"), "r1"),))
        return {"accepted": True, "receipt_id": "receipt1"}

    def pending_status(self, order_id):
        self.statuses.append(order_id)
        return next((p for p in self.account.pending if p.order_id == order_id), None)

    def cancel_remaining(self, order, account, key):
        self.cancellations.append((order, account, key))
        return {"accepted": True}


class StubProvider:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def exchange(self, text, **kwargs):
        self.calls.append((text, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return BrowserReply(reply, "conversation1", "message" + str(len(self.calls)))


class StubMarket:
    def __init__(self):
        self.calls = []
    def snapshot(self, symbol):
        self.calls.append(symbol)
        return {"symbol": symbol, "observed_at": time.time(), "mark_price": "101.23", "candles": {}}


class StubModel:
    def __init__(self, decisions):
        self.decisions, self.calls = list(decisions), []
    def plan(self, history, reply, **kwargs):
        self.calls.append((history, reply, kwargs))
        return self.decisions.pop(0)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "agent.sqlite3")
        self.bridge = StubBridge()
        self.market = StubMarket()

    def tearDown(self):
        self.memory.close()
        self.temp.cleanup()

    def controller(self, replies=(), decisions=(), bridge=None, **kwargs):
        self.provider, self.model = StubProvider(replies), StubModel(decisions)
        return Controller(bridge=bridge or self.bridge, provider=self.provider, market=self.market,
            llm=self.model, memory=self.memory, analysis_prompt=ANALYSIS,
            render_chart=lambda *_: PNG, parse_setup=parse_setup, **kwargs)

    def test_off_never_starts_market_browser_model_handoff_or_pending_cancel(self):
        self.bridge.account = replace(self.bridge.account, robot_on=False, pending=(
            PendingOrder("old", "BTCUSDT", time.time() - 25000, Decimal("1"), Decimal("0"), "r"),))
        controller = self.controller()
        for _ in range(3):
            self.assertEqual(controller.tick().state, "PAUSED")
        self.assertEqual((self.market.calls, self.provider.calls, self.model.calls,
                          self.bridge.handoffs, self.bridge.statuses, self.bridge.cancellations), ([], [], [], [], [], []))

    def test_full_two_slot_cycle_forwards_neurobro_setup_without_ai_prices(self):
        controller = self.controller(("BTCUSDT ETHUSDT", SETUP, "ETHUSDT\nSHORT\nENTRY: 200\nTP: 180\nSL: 205"),
            (DialogueDecision("SCREENING", ("BTCUSDT", "ETHUSDT")),))
        for _ in range(8):
            controller.tick()
        self.assertEqual(len(self.bridge.handoffs), 2)
        self.assertEqual(self.provider.calls[0][0], SCREENING)
        self.assertEqual(self.provider.calls[1][0], ANALYSIS)
        self.assertEqual(self.provider.calls[2][0], ANALYSIS)
        self.assertEqual(len(self.provider.calls[1][1]["attachments_png"]), 2)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.bridge.handoffs[0][0].source_text, SETUP)
        self.assertEqual(len(self.bridge.handoffs[0][2]), 64)

    def test_two_pending_across_days_occupy_both_slots_no_screening(self):
        pending = tuple(PendingOrder(str(i), symbol, time.time() - 3600,
            Decimal("1"), Decimal("0"), "r") for i, symbol in enumerate(("BTCUSDT", "ETHUSDT")))
        self.bridge.account = replace(self.bridge.account, pending=pending)
        controller = self.controller()
        self.assertEqual(controller.tick().state, "IDLE")
        self.assertEqual(self.provider.calls, [])

    def test_two_daily_entries_prevent_new_screening_even_flat(self):
        self.bridge.account = replace(self.bridge.account, daily_new_trades=2)
        self.assertEqual(self.controller().tick().state, "IDLE")

    def test_manual_pending_occupies_slot_but_is_not_reviewed_or_modified(self):
        self.bridge.account = replace(self.bridge.account, pending=(PendingOrder("manual", "ETHUSDT", time.time() - 25000,
            Decimal("1"), Decimal("0"), "r", owned=False),))
        controller = self.controller(("BTCUSDT",), ())
        self.assertEqual(controller.tick().phase, "SCREEN_DECIDE")
        self.assertIn("pilihkan 1 coin", self.provider.calls[0][0])
        self.assertEqual(self.bridge.statuses, [])
        self.assertEqual(self.bridge.cancellations, [])

    def test_one_occupied_slot_requests_exactly_one_coin(self):
        self.bridge.account = replace(self.bridge.account, positions=(Position("ETHUSDT", Decimal("1")),))
        controller = self.controller(("BTCUSDT",), (DialogueDecision("SCREENING", ("BTCUSDT",)),))
        controller.tick()
        self.assertIn("pilihkan 1 coin", self.provider.calls[0][0])
        controller.tick()
        self.assertEqual(self.memory.get("active_cycle")["queue"], ["BTCUSDT"])

    def test_model_invented_symbol_is_not_accepted(self):
        controller = self.controller(("BTCUSDT ETHUSDT",),
            (DialogueDecision("SCREENING", ("SOLUSDT", "ETHUSDT")),))
        controller.tick()
        self.assertEqual(controller.tick().reason, "SCREENING_SYMBOLS_NOT_VERIFIED")
        self.assertEqual(self.market.calls, [])

    def test_off_change_before_analysis_prevents_market_and_send(self):
        controller = self.controller(("BTCUSDT ETHUSDT",),
            (DialogueDecision("SCREENING", ("BTCUSDT", "ETHUSDT")),))
        controller.tick(); controller.tick()
        self.bridge.account = replace(self.bridge.account, robot_on=False)
        self.assertEqual(controller.tick().state, "PAUSED")
        self.assertEqual(self.market.calls, [])
        self.assertEqual(len(self.provider.calls), 1)

    def test_unconfirmed_send_is_not_replayed_after_restart(self):
        controller = self.controller((TimeoutError(),))
        controller.tick()
        controller = self.controller(("unwanted retry",))
        self.assertEqual(controller.tick().state, "REQUIRES_REVIEW")
        self.assertEqual(self.provider.calls, [])

    def test_confirmed_pre_submit_login_failure_can_resume_after_login(self):
        controller = self.controller((NotSent("LOGIN_REQUIRED"),))
        self.assertEqual(controller.tick().reason, "LOGIN_REQUIRED")
        self.assertIsNone(self.memory.get("active_cycle"))
        self.assertEqual(self.memory.counts()["operations"], {"DONE": 1})
        self.memory.put("next_screen_at", 0)
        controller = self.controller(("BTCUSDT ETHUSDT",), ())
        self.assertEqual(controller.tick().phase, "SCREEN_DECIDE")

    def test_stale_cycle_after_off_is_abandoned_without_handoff(self):
        controller = self.controller(("BTCUSDT ETHUSDT", SETUP),
            (DialogueDecision("SCREENING", ("BTCUSDT", "ETHUSDT")),))
        for _ in range(3): controller.tick()
        cycle = self.memory.get("active_cycle")
        cycle["market"]["observed_at"] = time.time() - 200
        self.memory.put("active_cycle", cycle)
        self.assertEqual(controller.tick().reason, "SETUP_MARKET_CONTEXT_STALE")
        self.assertIsNone(self.memory.get("active_cycle"))
        self.assertEqual(self.bridge.handoffs, [])

    def test_hold_replacement_is_bounded_to_three_rounds(self):
        self.bridge.account = replace(self.bridge.account, positions=(Position("ETHUSDT", Decimal("1")),),
            supported_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT"))
        symbols = ("BTCUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT")
        replies, decisions = [], []
        for symbol in symbols:
            replies.extend((symbol, symbol + "\nHOLD"))
            decisions.extend((DialogueDecision("SCREENING", (symbol,)), DialogueDecision("HOLD")))
        controller = self.controller(replies, decisions)
        for _ in range(20):
            result = controller.tick()
            if result.reason == "REPLACEMENT_LIMIT_REACHED": break
        self.assertEqual(result.reason, "REPLACEMENT_LIMIT_REACHED")
        self.assertEqual(len(self.provider.calls), 8)
        self.assertEqual(self.bridge.handoffs, [])
        self.assertIn("BTCUSDT", self.provider.calls[2][0])

    def test_pending_hold_rechecks_after_another_five_hours(self):
        self.bridge.account = replace(self.bridge.account, pending=(PendingOrder("old", "BTCUSDT", time.time() - 19000,
            Decimal("1"), Decimal("0.4"), "r1"),))
        controller = self.controller(("TAHAN",), (DialogueDecision("TAHAN"),))
        controller.tick()
        controller.tick()
        self.assertEqual(controller.tick().reason, "PENDING_TAHAN")
        self.assertLess(abs(time.time() - self.memory.get("pending_reviews")["old"]), 5)
        self.assertEqual(self.bridge.cancellations, [])

    def test_pending_cancel_checks_latest_partial_fill_and_only_remaining(self):
        self.bridge.account = replace(self.bridge.account, pending=(PendingOrder("old", "BTCUSDT", time.time() - 19000,
            Decimal("1"), Decimal("0.4"), "r1"),))
        controller = self.controller(("BATALKAN",), (DialogueDecision("BATALKAN"),))
        controller.tick()
        controller.tick()
        self.bridge.account = replace(self.bridge.account, pending=(replace(self.bridge.account.pending[0],
            executed_quantity=Decimal("0.7"), revision="r2"),))
        self.assertEqual(controller.tick().reason, "PENDING_BATALKAN")
        self.assertEqual(self.bridge.cancellations[0][0].executed_quantity, Decimal("0.7"))
        self.assertEqual(self.bridge.cancellations[0][0].revision, "r2")

    def test_pending_decision_must_be_explicitly_from_neurobro(self):
        self.bridge.account = replace(self.bridge.account, pending=(PendingOrder("old", "BTCUSDT", time.time() - 19000,
            Decimal("1"), Decimal("0"), "r1"),))
        controller = self.controller(("belum jelas",), (DialogueDecision("BATALKAN"),))
        controller.tick(); controller.tick(); controller.tick()
        self.assertEqual(self.bridge.cancellations, [])

    def test_old_jakarta_day_is_rejected_before_chat(self):
        self.bridge.account = replace(self.bridge.account, jakarta_day="2020-01-01")
        self.assertEqual(self.controller().tick().reason, "ACCOUNT_OBSERVATION_INVALID_OR_STALE")
        self.assertEqual(self.provider.calls, [])

    def test_approved_analysis_text_must_not_be_modified(self):
        with self.assertRaisesRegex(ValueError, "MISMATCH"):
            Controller(bridge=None, provider=None, market=None, llm=None, memory=self.memory,
                analysis_prompt=ANALYSIS + " ", render_chart=None, parse_setup=None)

    def test_real_backend_bridge_handoff_through_whole_controller(self):
        backend = Backend()
        # One currently occupied slot produces a one-coin cycle, while the real
        # bridge still checks current authenticated fee/rule evidence and NET2R.
        backend.snapshot["positions"] = [{"symbol": "ETHUSDT", "quantity": "1"}]
        backend.snapshot["risk_evidence"]["occupied_slots"] = 1
        bridge = BackendBridge("https://bridge.invalid", "unused", "revision1", SOURCE, transport=backend)
        controller = self.controller(("BTCUSDT", SETUP), (DialogueDecision("SCREENING", ("BTCUSDT",)),), bridge=bridge)
        results = [controller.tick() for _ in range(5)]
        posts = [call for call in backend.calls if call[0] == "POST"]
        self.assertEqual(results[-1].reason, "SETUP_FORWARDED")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], "/handoff")
        self.assertRegex(posts[0][2]["idempotency_key"], r"^[0-9a-f]{64}$")
        self.assertEqual(posts[0][2]["source"]["response_text"], SETUP)
        self.assertEqual(posts[0][2]["plan"]["leverage"], 75)

    def test_real_backend_bridge_pending_cancel_through_controller(self):
        backend = Backend()
        backend.snapshot["pending"] = [pending_row()]
        backend.snapshot["risk_evidence"]["occupied_slots"] = 1
        bridge = BackendBridge("https://bridge.invalid", "unused", "revision1", SOURCE, transport=backend)
        controller = self.controller(("BATALKAN",), (DialogueDecision("BATALKAN"),), bridge=bridge)
        controller.tick()
        controller.tick()
        self.assertEqual(controller.tick().reason, "PENDING_BATALKAN")
        posts = [call for call in backend.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], "/cancel-remaining")
        self.assertEqual(posts[0][2]["remaining_quantity"], "0.6")
        self.assertRegex(posts[0][2]["idempotency_key"], r"^[0-9a-f]{64}$")

    def _evidence_adapters(self):
        now = time.time()
        self.bridge.evidence = lambda: {"verified": True, "observed_at": now, "source_sha256": "a" * 64, "revision": "r1"}
        self.provider.evidence = lambda: {"verified": True, "observed_at": now, "source_sha256": "b" * 64}
        self.model.evidence = lambda: {"verified": True, "observed_at": now, "model": "gpt-5-mini"}
        return now

    def test_health_requires_actual_current_process_operations_and_no_probe(self):
        controller = self.controller(("BTCUSDT ETHUSDT",),
            (DialogueDecision("SCREENING", ("BTCUSDT", "ETHUSDT")),))
        self._evidence_adapters()
        self.assertFalse(controller.health_status()["integration_verified"])
        controller.tick()
        self.assertFalse(controller.health_status()["integration_verified"])
        controller.tick()
        calls = (len(self.provider.calls), len(self.model.calls), len(self.market.calls))
        self.assertTrue(controller.health_status()["integration_verified"])
        self.assertEqual(calls, (len(self.provider.calls), len(self.model.calls), len(self.market.calls)))

    def test_health_observations_expire_and_off_does_not_probe_browser_or_model(self):
        controller = self.controller(("BTCUSDT ETHUSDT",),
            (DialogueDecision("SCREENING", ("BTCUSDT", "ETHUSDT")),))
        now = self._evidence_adapters()
        controller.tick(); controller.tick()
        self.bridge.account = replace(self.bridge.account, robot_on=False)
        controller.tick()
        self.assertEqual(controller.health_status()["state"], "PAUSED")
        self.assertTrue(controller.health_status()["integration_verified"])
        controller.clock = lambda: now + 1201
        observed = controller.health_status()
        self.assertFalse(observed["integration_verified"])
        self.assertFalse(observed["dependencies"]["browser"]["fresh"])
        self.assertFalse(observed["dependencies"]["model"]["fresh"])
        self.assertEqual((len(self.provider.calls), len(self.model.calls)), (1, 1))

    def test_persisted_success_does_not_claim_current_process_browser_connection(self):
        controller = self.controller(("BTCUSDT ETHUSDT",), ())
        self._evidence_adapters()
        controller.tick()
        controller = self.controller((), (DialogueDecision("SCREENING", ("BTCUSDT", "ETHUSDT")),))
        self._evidence_adapters()
        controller.tick()
        observation = controller.health_status()
        self.assertTrue(observation["dependencies"]["backend"]["verified"])
        self.assertFalse(observation["dependencies"]["browser"]["verified"])
        self.assertFalse(observation["integration_verified"])
        self.assertEqual(self.provider.calls, [])

    def test_task_deadline_stops_before_chat_when_account_read_consumes_budget(self):
        timer = [0.0]
        original = self.bridge.snapshot
        def slow_read():
            timer[0] = 121.0
            return original()
        self.bridge.snapshot = slow_read
        controller = self.controller(monotonic=lambda: timer[0])
        self.assertEqual(controller.tick().reason, "TASK_BUDGET_EXHAUSTED")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.model.calls, [])

    def test_market_capture_does_not_share_tick_with_neurobro_send(self):
        controller = self.controller(("BTCUSDT ETHUSDT",),
            (DialogueDecision("SCREENING", ("BTCUSDT", "ETHUSDT")),))
        controller.tick(); controller.tick()
        self.assertEqual(controller.tick().reason, "FRESH_CHARTS_CAPTURED")
        self.assertEqual(len(self.market.calls), 1)
        self.assertEqual(len(self.provider.calls), 1)

    def test_brief_followup_may_reference_actual_current_market_price(self):
        controller = self.controller(("BTCUSDT ETHUSDT", "BTCUSDT\nLONG\nENTRY belum lengkap"),
            (DialogueDecision("SCREENING", ("BTCUSDT", "ETHUSDT")),
             DialogueDecision("CLARIFY", (), "Apakah harga realtime 101.23 masih sesuai?")))
        for _ in range(5): result = controller.tick()
        self.assertEqual(result.phase, "ANALYSIS_FOLLOWUP")
        self.assertEqual(self.model.calls[-1][2]["context"]["mark_price"], "101.23")
        self.assertEqual(self.bridge.handoffs, [])

    def test_model_followup_cannot_introduce_unobserved_price(self):
        controller = self.controller(("BTCUSDT ETHUSDT", "BTCUSDT\nLONG\nENTRY belum lengkap"),
            (DialogueDecision("SCREENING", ("BTCUSDT", "ETHUSDT")),
             DialogueDecision("CLARIFY", (), "Gunakan harga 777777?")))
        for _ in range(5): result = controller.tick()
        self.assertEqual(result.reason, "UNSUPPORTED_MODEL_NUMERIC_CLAIM")
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(self.bridge.handoffs, [])


if __name__ == "__main__":
    unittest.main()
