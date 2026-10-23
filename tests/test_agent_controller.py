from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import unittest

from agent.bridge import BackendBridge
from agent.controller import Controller
from agent.memory import Memory
from agent.models import BudgetExceeded, DialogueDecision, PendingOrder, Position, NotSent
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

    def cancel_remaining(self, order, account, key, *, source_reply):
        self.cancellations.append((order, account, key, source_reply))
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
        from tests.test_agent_native_market_context import native_context
        from agent.native_market_context import convert_native_market_context
        now = time.time()
        context = native_context(now)
        context['mark_price']['price'] = '101.23'
        context['symbol'] = symbol
        for frame in context['timeframes'].values():
            frame['symbol'] = symbol
        return convert_native_market_context(context, now=now)


class StubModel:
    def __init__(self, decisions):
        self.decisions, self.calls = list(decisions), []
    def plan(self, history, reply, **kwargs):
        self.calls.append((history, reply, kwargs))
        result = self.decisions.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


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

    def test_analysis_clarifications_continue_past_two_and_restart_with_fresh_charts(self):
        self.bridge.account = replace(self.bridge.account, positions=(Position("ETHUSDT", Decimal("1")),))
        clarify = DialogueDecision("CLARIFY", (), "Mohon lengkapi keputusan Neurobro?")
        controller = self.controller(("BTCUSDT",) + ("BTCUSDT belum lengkap",)*8 + (SETUP,),
            (DialogueDecision("SCREENING", ("BTCUSDT",)),) + (clarify,)*8)
        provider, model = self.provider, self.model
        restarted = False
        for _ in range(60):
            controller.tick()
            cycle = self.memory.get("active_cycle")
            if cycle and cycle.get("followups") == 4 and not restarted:
                restarted = True
                self.memory.close()
                self.memory = Memory(Path(self.temp.name)/"agent.sqlite3")
                controller = Controller(bridge=self.bridge,provider=provider,market=self.market,
                    llm=model,memory=self.memory,analysis_prompt=ANALYSIS,
                    render_chart=lambda *_:PNG,parse_setup=parse_setup)
            if self.bridge.handoffs:
                break
        self.assertEqual(len(self.bridge.handoffs),1)
        self.assertEqual(self.bridge.handoffs[0][0].source_text,SETUP)
        self.assertEqual(len(provider.calls),10)
        self.assertEqual(len(self.market.calls),9)
        self.assertTrue(all(len(kwargs["attachments_png"])==2 for _,kwargs in provider.calls[1:]))

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

    def test_hold_replacements_continue_past_three_rounds_and_survive_restart(self):
        symbols = ("BTCUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", "XRPUSDT", "DOGEUSDT", "LTCUSDT", "AVAXUSDT")
        self.bridge.account = replace(self.bridge.account, positions=(Position("ETHUSDT", Decimal("1")),),
            supported_symbols=("ETHUSDT", *symbols))
        replies, decisions = [], []
        for symbol in symbols:
            replies.extend((symbol, symbol + "\nHOLD"))
            decisions.extend((DialogueDecision("SCREENING", (symbol,)), DialogueDecision("HOLD")))
        controller = self.controller(replies, decisions)
        for _ in range(20):
            self.assertEqual(controller.tick().state, "RUNNING")
        original_id = self.memory.get("active_cycle")["id"]
        self.assertEqual(self.memory.get("active_cycle")["excluded"], list(symbols[:4]))
        path = self.memory.path
        self.memory.close()
        self.memory = Memory(path)
        controller = Controller(bridge=self.bridge, provider=self.provider, market=self.market,
            llm=self.model, memory=self.memory, analysis_prompt=ANALYSIS,
            render_chart=lambda *_: PNG, parse_setup=parse_setup)
        for _ in range(20):
            self.assertEqual(controller.tick().state, "RUNNING")
        cycle = self.memory.get("active_cycle")
        self.assertEqual(cycle["id"], original_id)
        self.assertEqual(cycle["round"], 8)
        self.assertEqual(cycle["excluded"], list(symbols))
        self.assertEqual(len(self.provider.calls), 16)
        self.assertEqual(self.market.calls, list(symbols))
        self.assertEqual(self.bridge.handoffs, [])
        for index in range(1, len(symbols)):
            for held in symbols[:index]:
                self.assertIn(held, self.provider.calls[2 * index][0])

    def test_no_eligible_coin_waits_without_repeating_hold_or_starting_new_cycle(self):
        self.bridge.account = replace(self.bridge.account, positions=(Position("ETHUSDT", Decimal("1")),),
            supported_symbols=("BTCUSDT", "ETHUSDT"))
        controller = self.controller(("BTCUSDT", "BTCUSDT\nHOLD"),
            (DialogueDecision("SCREENING", ("BTCUSDT",)), DialogueDecision("HOLD")))
        for _ in range(5): controller.tick()
        cycle = self.memory.get("active_cycle")
        for _ in range(5):
            self.assertEqual(controller.tick().reason, "NO_NEW_ELIGIBLE_SYMBOLS")
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(self.memory.get("active_cycle"), cycle)

    def test_two_free_slots_keep_requesting_two_different_coins_after_many_holds(self):
        symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", "XRPUSDT", "DOGEUSDT", "LTCUSDT")
        self.bridge.account = replace(self.bridge.account, supported_symbols=symbols)
        replies, decisions = [], []
        for index in range(0, len(symbols), 2):
            pair = symbols[index:index + 2]
            replies.extend((" ".join(pair), pair[0] + "\nHOLD", pair[1] + "\nHOLD"))
            decisions.extend((DialogueDecision("SCREENING", pair), DialogueDecision("HOLD"), DialogueDecision("HOLD")))
        controller = self.controller(replies, decisions)
        for _ in range(32):
            self.assertEqual(controller.tick().state, "RUNNING")
        self.assertEqual(self.market.calls, list(symbols))
        self.assertEqual(self.memory.get("active_cycle")["excluded"], list(symbols))
        self.assertEqual(self.bridge.handoffs, [])
        for index in range(4):
            prompt = self.provider.calls[3 * index][0]
            self.assertIn("pilihkan 2 coin", prompt)
            for held in symbols[:2 * index]:
                self.assertIn(held, prompt)

    def test_neurobro_repeating_hold_coin_requests_another_without_reanalysis(self):
        self.bridge.account = replace(self.bridge.account, positions=(Position("ETHUSDT", Decimal("1")),),
            supported_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"))
        controller = self.controller(("BTCUSDT", "BTCUSDT\nHOLD", "BTCUSDT", "SOLUSDT"),
            (DialogueDecision("SCREENING", ("BTCUSDT",)), DialogueDecision("HOLD"),
             DialogueDecision("SCREENING", ("BTCUSDT",)), DialogueDecision("SCREENING", ("SOLUSDT",))))
        for _ in range(5): controller.tick()
        cycle_id = self.memory.get("active_cycle")["id"]
        controller.tick()
        self.assertEqual(controller.tick().reason, "EXCLUDED_COIN_NOT_REPEATED")
        controller.tick(); controller.tick()
        self.assertEqual(self.memory.get("active_cycle")["id"], cycle_id)
        self.assertEqual(self.memory.get("active_cycle")["queue"], ["SOLUSDT"])
        self.assertEqual(self.market.calls, ["BTCUSDT"])
        self.assertIn("BTCUSDT", self.provider.calls[-1][0])

    def test_replacement_request_follows_current_capacity_in_both_directions(self):
        self.bridge.account = replace(self.bridge.account,
            supported_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", "XRPUSDT"))
        controller = self.controller(("BTCUSDT ETHUSDT", "BTCUSDT\nHOLD", "ETHUSDT\nHOLD",
                                      "SOLUSDT", "SOLUSDT\nHOLD", "BNBUSDT ADAUSDT"),
            (DialogueDecision("SCREENING", ("BTCUSDT", "ETHUSDT")), DialogueDecision("HOLD"),
             DialogueDecision("HOLD"), DialogueDecision("SCREENING", ("SOLUSDT",)), DialogueDecision("HOLD")))
        for _ in range(8): controller.tick()
        self.bridge.account = replace(self.bridge.account, positions=(Position("XRPUSDT", Decimal("1")),))
        controller.tick()
        self.assertIn("pilihkan 1 coin", self.provider.calls[-1][0])
        for _ in range(4): controller.tick()
        self.bridge.account = replace(self.bridge.account, positions=())
        controller.tick()
        self.assertIn("pilihkan 2 coin", self.provider.calls[-1][0])
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            self.assertIn(symbol, self.provider.calls[-1][0])

    def test_full_slots_quota_or_off_stop_replacement_screening_after_hold(self):
        controller = self.controller(("BTCUSDT ETHUSDT", "BTCUSDT\nHOLD", "ETHUSDT\nHOLD"),
            (DialogueDecision("SCREENING", ("BTCUSDT", "ETHUSDT")), DialogueDecision("HOLD"), DialogueDecision("HOLD")))
        for _ in range(8): controller.tick()
        self.bridge.account = replace(self.bridge.account, robot_on=False)
        self.assertEqual(controller.tick().state, "PAUSED")
        self.bridge.account = replace(self.bridge.account, robot_on=True, daily_new_trades=2)
        self.assertEqual(controller.tick().reason, "CAPACITY_REACHED")
        self.assertEqual(len(self.provider.calls), 3)
        self.memory.put("next_screen_at", 0)
        self.bridge.account = replace(self.bridge.account, daily_new_trades=0, pending=tuple(
            PendingOrder(str(i), symbol, time.time(), Decimal("1"), Decimal("0"), "r")
            for i, symbol in enumerate(("SOLUSDT", "BNBUSDT"))))
        self.assertEqual(controller.tick().state, "IDLE")
        self.assertEqual(len(self.provider.calls), 3)

    def test_model_budget_wait_preserves_hold_exclusions_without_restarting_paid_screening(self):
        self.bridge.account = replace(self.bridge.account, positions=(Position("ETHUSDT", Decimal("1")),),
            supported_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"))
        controller = self.controller(("BTCUSDT", "BTCUSDT\nHOLD", "SOLUSDT"),
            (DialogueDecision("SCREENING", ("BTCUSDT",)), DialogueDecision("HOLD"),
             BudgetExceeded("DAILY_MODEL_BUDGET_EXHAUSTED")))
        for _ in range(6): controller.tick()
        cycle = self.memory.get("active_cycle")
        self.assertEqual(controller.tick().reason, "DAILY_MODEL_BUDGET_EXHAUSTED")
        for _ in range(3): self.assertEqual(controller.tick().state, "WAITING")
        self.assertEqual(self.memory.get("active_cycle"), cycle)
        self.assertEqual(cycle["excluded"], ["BTCUSDT"])
        self.assertEqual(len(self.provider.calls), 3)
        path = self.memory.path
        self.memory.close()
        self.memory = Memory(path)
        controller = Controller(bridge=self.bridge, provider=self.provider, market=self.market,
            llm=self.model, memory=self.memory, analysis_prompt=ANALYSIS,
            render_chart=lambda *_: PNG, parse_setup=parse_setup)
        self.assertEqual(controller.tick().state, "WAITING")
        self.assertEqual(len(self.provider.calls), 3)
        self.assertEqual(self.memory.get("active_cycle"), cycle)
        self.bridge.account = replace(self.bridge.account, robot_on=False)
        self.assertEqual(controller.tick().state, "PAUSED")

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
        self.assertEqual(self.bridge.cancellations[0][3]["text"], "BATALKAN")
        self.assertEqual(self.bridge.cancellations[0][3]["response_id"], "message1")

    def test_pending_followup_retains_independent_order_binding_and_original_reply(self):
        import hashlib
        identifier = "private-order-123"
        self.bridge.account = replace(self.bridge.account, pending=(PendingOrder(identifier, "BTCUSDT", time.time()-19000,
            Decimal("1"), Decimal("0.2"), "r1"),))
        controller = self.controller(("belum jelas", "**Keputusan: BATALKAN.**"),
            (DialogueDecision("CLARIFY", (), "Konfirmasi keputusan sisa LIMIT?"), DialogueDecision("BATALKAN")))
        for _ in range(6): result = controller.tick()
        self.assertEqual(result.reason, "PENDING_BATALKAN")
        expected = "PENDING_REVIEW symbol=BTCUSDT\norder_ref: " + hashlib.sha256(identifier.encode()).hexdigest() + "\n"
        self.assertEqual(len(self.provider.calls), 2)
        for prompt, _ in self.provider.calls:
            self.assertTrue(prompt.startswith(expected))
            self.assertNotIn(identifier, prompt)
        self.assertIn("Pertanyaan lanjutan: Konfirmasi keputusan sisa LIMIT?", self.provider.calls[1][0])
        self.assertEqual(self.market.calls, ["BTCUSDT", "BTCUSDT"])
        self.assertEqual(len(self.provider.calls[1][1]["attachments_png"]), 2)
        self.assertEqual(self.bridge.cancellations[0][3]["response_id"], "message2")
        self.assertEqual(self.bridge.cancellations[0][3]["text"], "**Keputusan: BATALKAN.**")

    def test_unknown_cancel_is_not_reissued_after_partial_fill_revision_changes(self):
        self.bridge.account = replace(self.bridge.account, pending=(PendingOrder("old", "BTCUSDT", time.time()-19000,
            Decimal("1"), Decimal("0.2"), "r1"),))
        original = self.bridge.cancel_remaining
        def interrupted(order, account, key, *, source_reply):
            original(order, account, key, source_reply=source_reply)
            raise TimeoutError()
        self.bridge.cancel_remaining = interrupted
        controller = self.controller(("BATALKAN",), (DialogueDecision("BATALKAN"),))
        for _ in range(3): controller.tick()
        self.bridge.account = replace(self.bridge.account, pending=(replace(self.bridge.account.pending[0],
            executed_quantity=Decimal("0.7"), revision="r2"),))
        controller.tick()
        self.assertEqual(len(self.bridge.cancellations), 1)

    def test_pending_decision_must_be_explicitly_from_neurobro(self):
        self.bridge.account = replace(self.bridge.account, pending=(PendingOrder("old", "BTCUSDT", time.time() - 19000,
            Decimal("1"), Decimal("0"), "r1"),))
        controller = self.controller(("belum jelas",), (DialogueDecision("BATALKAN"),))
        controller.tick(); controller.tick(); controller.tick()
        self.assertEqual(self.bridge.cancellations, [])

    def test_negated_cancellation_word_cannot_become_a_cancellation(self):
        self.bridge.account = replace(self.bridge.account, pending=(PendingOrder("old", "BTCUSDT", time.time()-19000,
            Decimal("1"), Decimal("0.3"), "r1"),))
        controller = self.controller(("Jangan BATALKAN. Tunggu evaluasi selanjutnya.",), (DialogueDecision("BATALKAN"),))
        controller.tick(); controller.tick(); controller.tick()
        self.assertEqual(self.bridge.cancellations, [])

    def test_explicit_markdown_decision_is_accepted_without_rewriting_it(self):
        self.bridge.account = replace(self.bridge.account, pending=(PendingOrder("old", "BTCUSDT", time.time()-19000,
            Decimal("1"), Decimal("0.3"), "r1"),))
        controller = self.controller(("**Keputusan: TAHAN.**\nKondisi belum berubah.",), (DialogueDecision("TAHAN"),))
        controller.tick(); controller.tick()
        self.assertEqual(controller.tick().reason, "PENDING_TAHAN")
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
        context = self.model.calls[-1][2]["context"]
        self.assertEqual(sum(map(len, context["candles"].values())), 400)
        self.assertEqual(context["candle_windows"]["1h"]["closed_count"], 200)
        self.assertNotIn("recent_candles", context)
        self.assertEqual(self.bridge.handoffs, [])

    def test_model_followup_cannot_introduce_unobserved_price(self):
        controller = self.controller(("BTCUSDT ETHUSDT", "BTCUSDT\nLONG\nENTRY belum lengkap"),
            (DialogueDecision("SCREENING", ("BTCUSDT", "ETHUSDT")),
             DialogueDecision("CLARIFY", (), "Gunakan harga 777777?")))
        for _ in range(5): result = controller.tick()
        self.assertEqual(result.reason, "UNSUPPORTED_MODEL_NUMERIC_CLAIM")
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(self.bridge.handoffs, [])

    def test_analysis_followup_refreshes_400_candles_then_attaches_two_charts_after_restart(self):
        controller = self.controller(("BTCUSDT ETHUSDT", "BTCUSDT HOLD belum jelas", "BTCUSDT HOLD"),
            (DialogueDecision("SCREENING", ("BTCUSDT", "ETHUSDT")),
             DialogueDecision("CLARIFY", (), "Mohon konfirmasi keputusan Neurobro?"),
             DialogueDecision("HOLD")))
        for _ in range(5): controller.tick()
        original = self.memory.get("active_cycle")["market"]
        fresh = controller.tick()
        self.assertEqual(fresh.reason, "FRESH_FOLLOWUP_CHARTS_CAPTURED")
        self.assertEqual(len(self.provider.calls), 2)
        refreshed = self.memory.get("active_cycle")["market"]
        self.assertGreaterEqual(refreshed["observed_at"], original["observed_at"])
        self.assertEqual(sum(map(len, refreshed["candles"].values())), 400)
        self.assertEqual(self.market.calls, ["BTCUSDT", "BTCUSDT"])
        # A restart reuses the saved fresh snapshot and operation identity; it
        # never repeats collection or an already attempted browser send blindly.
        controller = self.controller(("BTCUSDT HOLD",), (DialogueDecision("HOLD"),))
        self.assertEqual(controller.tick().phase, "ANALYSIS_DECIDE")
        self.assertEqual(self.market.calls, ["BTCUSDT", "BTCUSDT"])
        self.assertEqual(self.provider.calls[0][0], "Mohon konfirmasi keputusan Neurobro?")
        self.assertEqual(len(self.provider.calls[0][1]["attachments_png"]), 2)
        controller.tick()
        self.assertEqual(self.model.calls[-1][2]["context"], refreshed)

    def test_off_between_fresh_followup_collection_and_send_preserves_pending_and_sends_nothing(self):
        self.bridge.account = replace(self.bridge.account, pending=(PendingOrder("old", "BTCUSDT", time.time()-19000,
            Decimal("1"), Decimal("0.2"), "r1"),))
        controller = self.controller(("belum jelas",),
            (DialogueDecision("CLARIFY", (), "Konfirmasi keputusan sisa LIMIT?"),))
        for _ in range(4): result = controller.tick()
        self.assertEqual(result.reason, "FRESH_FOLLOWUP_CHARTS_CAPTURED")
        calls = (list(self.market.calls), len(self.provider.calls), len(self.model.calls))
        pending = self.bridge.account.pending
        self.bridge.account = replace(self.bridge.account, robot_on=False)
        self.assertEqual(controller.tick().reason, "ROBOT_OFF")
        self.assertEqual((self.market.calls, len(self.provider.calls), len(self.model.calls)), calls)
        self.assertEqual(self.bridge.account.pending, pending)
        self.assertEqual(self.bridge.cancellations, [])

    def test_unconfirmed_followup_is_not_replayed_after_restart_or_refetched(self):
        controller = self.controller(("BTCUSDT ETHUSDT", "BTCUSDT belum lengkap", TimeoutError()),
            (DialogueDecision("SCREENING", ("BTCUSDT", "ETHUSDT")),
             DialogueDecision("CLARIFY", (), "Mohon lengkapi keputusan Neurobro?")))
        for _ in range(7): controller.tick()
        self.assertEqual(len(self.provider.calls), 3)
        self.assertEqual(self.market.calls, ["BTCUSDT", "BTCUSDT"])
        controller = self.controller(("must not send again",), ())
        result = controller.tick()
        self.assertEqual(result.reason, "UNCONFIRMED_OPERATION_NOT_REPLAYED")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.market.calls, ["BTCUSDT", "BTCUSDT"])

    def test_pending_fill_after_fresh_followup_capture_stops_before_another_chat(self):
        self.bridge.account = replace(self.bridge.account, pending=(PendingOrder("old", "BTCUSDT", time.time()-19000,
            Decimal("1"), Decimal("0.2"), "r1"),))
        controller = self.controller(("belum jelas",),
            (DialogueDecision("CLARIFY", (), "Konfirmasi keputusan sisa LIMIT?"),))
        for _ in range(4): controller.tick()
        self.bridge.account = replace(self.bridge.account, pending=(replace(self.bridge.account.pending[0],
            executed_quantity=Decimal("1"), revision="filled"),))
        self.assertEqual(controller.tick().reason, "PENDING_NO_LONGER_OPEN")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.bridge.cancellations, [])


if __name__ == "__main__":
    unittest.main()
