import hashlib
import time
import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from agent.models import AccountSnapshot, NeurobroSetup, PendingOrder, Position, RobotOff
from agent.risk import RiskRejected, capacity, evaluate_setup, parse_setup


def example_account(now=None, **changes):
    now = time.time() if now is None else now
    values = dict(robot_on=True, observed_at=now, generation="generation1",
        jakarta_day=datetime.fromtimestamp(now, ZoneInfo("Asia/Jakarta")).date().isoformat(),
        balance_usdt=Decimal("100"), risk_usdt=Decimal("5"), daily_new_trades=0,
        supported_symbols=("BTCUSDT", "ETHUSDT"))
    values.update(changes)
    return AccountSnapshot(**values)


def example_authority(now=None):
    now = time.time() if now is None else now
    return {"ownership_reconciled": True, "occupied_slots": 0,
        "position_mode": "ONE_WAY", "margin_mode": "CROSS",
        "risk_model": "FEE_SLIPPAGE_RISK_V3", "exit_slippage_rate": "0.005",
        "available_margin_usdt": "100", "symbol_rules": {"BTCUSDT": {
            "observed_at": now, "step_size": "0.001", "min_qty": "0.001",
            "max_qty": "100", "tick_size": "0.1", "min_notional": "5",
            "min_price": "0.1", "max_price": "1000000", "multiplier_up": "2",
            "multiplier_down": "0.5", "mark_price": "100", "max_leverage": 125,
            "notional_cap_75": "10000"}}, "commission": {"BTCUSDT": {
                "observed_at": now, "symbol": "BTCUSDT", "source": "BINANCE_FUTURES_COMMISSION_RATE",
                "taker_fee_rate": "0.0005"}}}


def example_setup(text=None):
    return parse_setup(text or "BTCUSDT\nLONG\nENTRY: 100\nTP: 110\nSL: 99", "BTCUSDT", "message1")


class ParseNeurobroSetupTests(unittest.TestCase):
    def test_copies_neurobro_original_exact_prices_and_proof(self):
        text = "**BTCUSDT**\n**KEPUTUSAN: LONG**\n**ENTRY:** 100.00\n**TP:** 105.0\n**SL:** 99"
        setup = example_setup(text)
        self.assertEqual(setup.entry, Decimal("100.00"))
        self.assertEqual(setup.source_text, text)
        self.assertEqual(setup.source_sha256, hashlib.sha256(text.encode()).hexdigest())

    def test_hold_incomplete_ranges_duplicate_values_wrong_symbol_rejected(self):
        for text in ("BTCUSDT\nHOLD", "BTCUSDT\nLONG\nENTRY: 100\nSL: 99",
                     "BTCUSDT\nLONG\nENTRY: 99-100\nTP: 105\nSL: 98",
                     "BTCUSDT\nLONG\nENTRY: 100\nENTRY: 101\nTP: 105\nSL: 99",
                     "BTCUSDT ETHUSDT\nLONG\nENTRY: 100\nTP: 105\nSL: 99",
                     "BTCUSDT\nLONG\nSHORT\nENTRY: 100\nTP: 105\nSL: 99",
                     "BTCUSDT\nLONG\nENTRY: 1,000\nTP: 1050\nSL: 990",
                     "BTCUSDT\nLONG\nENTRY: 100\nTP1: 105\nTP2: 110\nSL: 99"):
            with self.subTest(text=text):
                self.assertIsNone(example_setup(text))


class RiskBoundaryTests(unittest.TestCase):
    def test_existing_risk_manager_normalizes_net_2r_and_fee_slippage_under_5(self):
        plan = evaluate_setup(example_setup(), example_account(), example_authority())
        self.assertEqual(plan["risk_model"], "FEE_SLIPPAGE_RISK_V3")
        self.assertEqual(plan["margin_mode"], "CROSS")
        self.assertEqual(plan["leverage"], 75)
        self.assertEqual(plan["order_type"], "LIMIT")
        self.assertLessEqual(Decimal(plan["total_risk"]), Decimal("5"))
        self.assertGreaterEqual(Decimal(plan["net_rr"]), Decimal("2"))
        self.assertLess(Decimal(plan["tp"]), Decimal("110"))
        self.assertGreater(Decimal(plan["sl_slippage_usdt"]), Decimal("0"))

    def test_setting_increase_is_supported_without_permanent_live_disable(self):
        small = evaluate_setup(example_setup(), example_account(risk_usdt=Decimal("2")), example_authority())
        large = evaluate_setup(example_setup(), example_account(risk_usdt=Decimal("8")), example_authority())
        self.assertGreater(Decimal(large["quantity"]), Decimal(small["quantity"]))
        self.assertLessEqual(Decimal(large["total_risk"]), Decimal("8"))

    def test_short_works_and_prices_entry_sl_never_changed(self):
        setup = example_setup("BTCUSDT\nSHORT\nENTRY: 100\nTP: 90\nSL: 101")
        plan = evaluate_setup(setup, example_account(), example_authority())
        self.assertEqual((plan["entry"], plan["sl"]), ("100", "101"))
        self.assertLess(Decimal(plan["tp"]), Decimal("98"))

    def test_forged_or_llm_changed_setup_is_rejected(self):
        for setup in (replace(example_setup(), entry=Decimal("101")),
                      replace(example_setup(), source_sha256="0" * 64)):
            with self.assertRaisesRegex(RiskRejected, "SOURCE_LEVELS"):
                evaluate_setup(setup, example_account(), example_authority())

    def test_gross_reward_below_2_is_rejected_instead_of_repriced_up(self):
        with self.assertRaises(RiskRejected):
            evaluate_setup(example_setup("BTCUSDT\nLONG\nENTRY: 100\nTP: 101.9\nSL: 99"), example_account(), example_authority())

    def test_rule_fee_risk_account_mode_and_margin_evidence_fail_closed(self):
        mutations = (
            lambda a: a["commission"]["BTCUSDT"].update(observed_at=time.time()-121),
            lambda a: a["commission"]["BTCUSDT"].update(source="ASSUMED"),
            lambda a: a["symbol_rules"]["BTCUSDT"].update(observed_at=time.time()-181),
            lambda a: a["symbol_rules"]["BTCUSDT"].update(max_leverage=50),
            lambda a: a["symbol_rules"]["BTCUSDT"].update(notional_cap_75="1"),
            lambda a: a.update(available_margin_usdt="0.01"),
            lambda a: a.update(margin_mode="ISOLATED"),
            lambda a: a.update(position_mode="HEDGE"),
            lambda a: a.update(exit_slippage_rate="0"),
            lambda a: a.update(ownership_reconciled=False),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                evidence = example_authority()
                mutate(evidence)
                with self.assertRaises(RiskRejected):
                    evaluate_setup(example_setup(), example_account(), evidence)

    def test_off_never_produces_plan(self):
        with self.assertRaises(RobotOff):
            evaluate_setup(example_setup(), example_account(robot_on=False), example_authority())

    def test_daily_cap_and_pending_from_previous_day_still_occupy_slots(self):
        now = time.time()
        pending = PendingOrder("pending1", "ETHUSDT", now-86400, Decimal("1"), Decimal("0"), "rev1")
        authority = example_authority()
        authority["occupied_slots"] = 1
        self.assertEqual(capacity(example_account(pending=(pending,)), authority), 1)
        self.assertEqual(capacity(example_account(pending=(pending,), daily_new_trades=2), authority), 0)
        authority["occupied_slots"] = 2
        self.assertEqual(capacity(example_account(pending=(pending,)), authority), 0)

    def test_pending_same_symbol_and_manual_positions_block_duplicate_entry(self):
        authority = example_authority()
        authority["occupied_slots"] = 1
        with self.assertRaisesRegex(RiskRejected, "SYMBOL_ALREADY"):
            evaluate_setup(example_setup(), example_account(positions=(Position("BTCUSDT", Decimal("0.1")),)), authority)

    def test_account_stale_or_jakarta_day_mismatch_rejected(self):
        for account in (example_account(observed_at=time.time()-31), example_account(jakarta_day="2000-01-01")):
            with self.assertRaises(RiskRejected):
                evaluate_setup(example_setup(), account, example_authority())

    def test_lot_floor_uses_exact_existing_arithmetic_under_low_decimal_precision(self):
        with localcontext() as context:
            context.prec = 5
            plan = evaluate_setup(example_setup(), example_account(), example_authority())
        self.assertLessEqual(Decimal(plan["risk"]), Decimal("5"))
        self.assertEqual(Decimal(plan["quantity"]) % Decimal("0.001"), 0)


if __name__ == "__main__":
    unittest.main()
