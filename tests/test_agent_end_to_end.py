"""Offline full-stack composition; no production endpoint or real order is used.

Only external boundaries are fixtures: the website, OpenAI response, public
market responses and the existing gateway callback. Controller, SQLite memory,
both RPC/client protocols, backend admission, charts and Risk Manager are real.
"""
import copy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from agent.backend_adapter import ExistingBackendAdapter
from agent.bridge import BackendBridge
from agent.browser_rpc import BrowserRPCService, NeurobroRPC
from agent.charts import render_chart_png
from agent.controller import Controller
from agent.llm import OpenAIDialogue
from agent.market import BinanceFuturesMarket
from agent.memory import Memory
from agent.providers import BrowserReply, DeliveryUncertain
from agent.risk import parse_setup
from worker.prompts import ANALYSIS
from test_agent_backend_adapter import ExistingBackendFixture
from test_agent_bridge import SOURCE


BROWSER_SOURCE = "d" * 64
APP_TOKEN = "offline_fixture_only_app_token_0123456789"
SETUP = "BTCUSDT\nLONG\nENTRY: 100\nTP: 110\nSL: 99"


class WebsiteFixture:
    """External Neurobro website stand-in, behind the real durable RPC service."""
    contract = {"source_sha256": BROWSER_SOURCE}

    def __init__(self, backend):
        self.backend = backend
        self.calls = []
        self.last_reply = None
        self.last_observed = None

    def status(self):
        return {"status": "CONNECTED"}

    def exchange(self, message, attachments_png=(), conversation_id=None, request_id=None):
        if self.backend.account["robot_on"] is not True:
            raise AssertionError("The website must never be contacted while OFF")
        self.calls.append({"message": message, "images": attachments_png, "request_id": request_id})
        if len(self.calls) == 1:
            self.last_reply = BrowserReply("BTCUSDT", "b" * 64, "screening-message1")
        elif len(self.calls) == 2:
            self.last_reply = BrowserReply(SETUP, "b" * 64, "analysis-message2")
        elif len(self.calls) == 3:
            self.last_reply = BrowserReply("BATALKAN", "b" * 64, "pending-message3")
        else:
            raise AssertionError("Unexpected duplicate website discussion")
        self.last_observed = time.time()
        return self.last_reply

    def evidence(self):
        # Simulated origin evidence belongs to the explicitly fake external
        # website boundary, rather than the controller or RPC client payload.
        return {"verified": self.last_reply is not None, "provider": "NEUROBRO_WEB",
                "source_sha256": BROWSER_SOURCE, "observed_at": self.last_observed,
                "response_sha256": self.last_reply.response_sha256 if self.last_reply else None}


class BackendFixture(ExistingBackendFixture):
    """Simulated already-installed gateway; its admission adapter remains real."""
    def __init__(self, robot_on):
        super().__init__()
        self.account["robot_on"] = robot_on
        # An existing manual ETH position occupies one slot and is preserved.
        self.account["positions"] = [{"symbol": "ETHUSDT", "quantity": "1"}]
        self.account["risk_evidence"]["occupied_slots"] = 1
        self.browser_service = None
        self.source_receipts_read = []
        self.admitted_plans = []

    def source_receipt(self, message_id):
        # Read the independently written browser-service receipt database. Never
        # echo source/hash/text from the agent's POST payload or its memory DB.
        self.source_receipts_read.append(message_id)
        receipt = self.browser_service.source_receipt(message_id)
        if receipt is not None and receipt["source_sha256"] != BROWSER_SOURCE:
            raise AssertionError("Wrong independently pinned browser source")
        return receipt

    def enqueue_existing_gateway(self, plan, key, generation):
        result = super().enqueue_existing_gateway(plan, key, generation)
        self.admitted_plans.append(copy.deepcopy(plan))
        self.account["daily_new_trades"] += 1
        self.account["pending"] = [{"order_id": "fixture-entry1", "symbol": "BTCUSDT",
            "created_at": time.time(), "quantity": plan["quantity"], "executed_quantity": "0",
            "revision": "revision-entry1", "owned": True}]
        self.account["risk_evidence"]["occupied_slots"] = 2
        return result


class AgentEndToEndTests(unittest.TestCase):
    def test_actual_stack_admits_one_source_bound_risk_plan_and_off_has_no_active_effects(self):
        for enabled in (False, True):
            with self.subTest(robot_on=enabled), tempfile.TemporaryDirectory(prefix="agent-e2e-") as directory:
                backend = BackendFixture(enabled)
                website = WebsiteFixture(backend)
                browser_service = BrowserRPCService(website, "/unused-fixture-token",
                    str(Path(directory) / "browser-receipts.sqlite3"), token_reader=lambda: APP_TOKEN)
                backend.browser_service = browser_service
                memory = Memory(Path(directory) / "agent.sqlite3")
                calls = {"rpc": [], "model": [], "market": []}
                try:
                    adapter = ExistingBackendAdapter(backend, backend.state_lock, "revision1", SOURCE)
                    bridge = BackendBridge("http://127.0.0.1", "/unused-fixture-token", "revision1", SOURCE,
                                           transport=adapter.request)
                    holder = {}
                    def browser_transport(method, path, payload):
                        calls["rpc"].append((method, path))
                        status, result = browser_service.dispatch(method, path, "Bearer " + APP_TOKEN,
                            b"" if payload is None else json.dumps(payload).encode())
                        if status != 200:
                            raise DeliveryUncertain("FIXTURE_RPC_REJECTED")
                        return result
                    provider = NeurobroRPC("https://browser-fixture.invalid", "/unused-fixture-token",
                        BROWSER_SOURCE, lambda: holder["controller"].robot_enabled(), transport=browser_transport)

                    def model_transport(payload):
                        calls["model"].append(payload)
                        self.assertEqual(payload["model"], "gpt-5-mini")
                        self.assertNotIn("tools", payload)
                        self.assertEqual(set(payload["text"]["format"]["schema"]["properties"]),
                                         {"action", "symbols", "question"})
                        phase = payload["input"][-1]["content"]
                        decision = ({"action": "BATALKAN", "symbols": [], "question": ""}
                                    if phase.startswith("Phase: PENDING_REVIEW\n") else
                                    {"action": "SCREENING", "symbols": ["BTCUSDT"], "question": ""})
                        return {"model": "gpt-5-mini", "status": "completed", "output": [
                            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(decision)}]}]}
                    llm = OpenAIDialogue("/unused-no-real-openai-key", memory, transport=model_transport)

                    def market_transport(path, parameters):
                        calls["market"].append((path, parameters))
                        now = int(time.time() * 1000)
                        if path == "/fapi/v1/time":
                            return {"serverTime": now}
                        if path == "/fapi/v1/exchangeInfo":
                            return {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING",
                                "contractType": "PERPETUAL", "quoteAsset": "USDT", "marginAsset": "USDT", "filters": [
                                    {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
                                    {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}
                        if path == "/fapi/v1/premiumIndex":
                            return {"symbol": "BTCUSDT", "markPrice": "100", "time": now}
                        self.assertEqual(path, "/fapi/v1/klines")
                        interval = {"1h": 3600000, "15m": 900000}[parameters["interval"]]
                        latest = now // interval * interval
                        return [[latest-(29-i)*interval, "100", "101", "99", "100", "7",
                                 latest-(29-i)*interval+interval-1] for i in range(30)]
                    market = BinanceFuturesMarket(transport=market_transport)
                    controller = Controller(bridge=bridge, provider=provider, market=market, llm=llm,
                        memory=memory, analysis_prompt=ANALYSIS, render_chart=render_chart_png, parse_setup=parse_setup)
                    holder["controller"] = controller
                    outcomes = [controller.tick() for _ in range(5)]
                    if not enabled:
                        self.assertTrue(all(result.reason == "ROBOT_OFF" for result in outcomes))
                        self.assertEqual(calls, {"rpc": [], "model": [], "market": []})
                        self.assertEqual(website.calls, [])
                        self.assertEqual(backend.source_receipts_read, [])
                        self.assertEqual(backend.admitted_plans, [])
                        self.assertEqual(backend.operations, {})
                        continue

                    self.assertEqual(outcomes[-1].reason, "SETUP_FORWARDED")
                    self.assertEqual(len(backend.admitted_plans), 1)
                    self.assertEqual(backend.calls.count("SIMULATED_EXISTING_GATEWAY"), 1)
                    self.assertEqual(backend.source_receipts_read, ["analysis-message2"])
                    self.assertEqual(len(calls["model"]), 1)
                    self.assertEqual(len(calls["market"]), 5)
                    self.assertEqual(len(calls["rpc"]), 2)
                    self.assertEqual(website.calls[1]["message"], ANALYSIS)
                    self.assertEqual(len(website.calls[1]["images"]), 2)
                    self.assertTrue(all(image.startswith(b"\x89PNG\r\n\x1a\n") for image in website.calls[1]["images"]))
                    plan = backend.admitted_plans[0]
                    self.assertEqual(plan["neurobro_source"]["response_sha256"], hashlib.sha256(SETUP.encode()).hexdigest())
                    self.assertEqual((plan["entry"], plan["sl"], plan["order_type"], plan["margin_mode"], plan["leverage"]),
                                     ("100", "99", "LIMIT", "CROSS", 75))
                    self.assertEqual(plan["risk_model"], "FEE_SLIPPAGE_RISK_V3")
                    quantity, entry, stop, tp, fee = map(Decimal,
                        (plan["quantity"], plan["entry"], plan["sl"], plan["tp"], plan["entry_fee_rate"]))
                    slippage = Decimal(plan["exit_slippage_rate"])
                    risk_per_unit = entry-stop + entry*fee + stop*slippage + stop*(1-slippage)*fee
                    total_risk = quantity*risk_per_unit
                    reward_per_unit = tp*(1-slippage)-entry-entry*fee-tp*(1-slippage)*fee
                    self.assertEqual(total_risk, Decimal(plan["total_risk"]))
                    self.assertLessEqual(total_risk, Decimal("5"))
                    self.assertGreater((quantity+Decimal("0.001"))*risk_per_unit, Decimal("5"))
                    self.assertGreaterEqual(quantity*reward_per_unit, total_risk*2)
                    previous_tp = tp-Decimal("0.1")
                    previous_reward = quantity*(previous_tp*(1-slippage)-entry-entry*fee-previous_tp*(1-slippage)*fee)
                    self.assertLess(previous_reward, total_risk*2, "TP must be the first valid NET2R tick")
                    self.assertFalse(controller.health_status()["integration_verified"], "Fixture transport is never production connectivity proof")
                    # A later five-hour pending review travels through the same
                    # complete stack. The backend independently verifies the new
                    # durable browser receipt's phase, symbol and hashed order ID.
                    backend.account["pending"] = [copy.deepcopy(backend.order)]
                    reviews = [controller.tick() for _ in range(3)]
                    self.assertEqual(reviews[-1].reason, "PENDING_BATALKAN")
                    self.assertEqual(backend.calls.count("SIMULATED_REMAINING_CANCEL"), 1)
                    self.assertEqual(backend.source_receipts_read, ["analysis-message2", "pending-message3"])
                    receipt = browser_service.source_receipt("pending-message3")
                    self.assertEqual(receipt["phase"], "PENDING_REVIEW")
                    self.assertEqual(receipt["order_ref"], hashlib.sha256(backend.order["order_id"].encode()).hexdigest())
                    self.assertTrue(receipt["request_text"].startswith("PENDING_REVIEW symbol=BTCUSDT\norder_ref: "))
                    self.assertEqual(backend.order["executed_quantity"], "0.4")
                    self.assertEqual(len(backend.admitted_plans), 1)
                    # Owner turning OFF after admission causes no further website,
                    # model, market, cancellation or gateway activity.
                    backend.account["robot_on"] = False
                    before = (len(calls["rpc"]), len(calls["model"]), len(calls["market"]), len(backend.calls))
                    self.assertEqual(controller.tick().reason, "ROBOT_OFF")
                    self.assertEqual(before, (len(calls["rpc"]), len(calls["model"]), len(calls["market"]), len(backend.calls)))
                    self.assertEqual(backend.account["positions"], [{"symbol": "ETHUSDT", "quantity": "1"}])
                    self.assertTrue(all(row["state"] == "COMPLETE" for row in backend.operations.values()))
                finally:
                    memory.close()
                    browser_service.close()


if __name__ == "__main__":
    unittest.main()
