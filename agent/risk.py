"""Deterministic Neurobro parsing and the existing Risk Manager boundary.

This module has no Binance client and cannot place, cancel or alter an order.
The production gateway must independently recheck each plan under its ON gate.
"""
from __future__ import annotations

import hashlib
import re
import time
from decimal import Decimal
from fractions import Fraction
from typing import Mapping
from zoneinfo import ZoneInfo
from datetime import datetime

from .models import AccountSnapshot, NeurobroSetup, NotSent, RobotOff


class RiskRejected(NotSent):
    pass


SYMBOL = re.compile(r"[A-Z0-9]{2,18}USDT", re.ASCII)
DECIMAL = re.compile(r"-?\d{1,32}(?:\.\d{1,24})?", re.ASCII)
MAX_ACCOUNT_AGE = 30
MAX_RULE_AGE = 180
MAX_FEE_AGE = 120


def number(value, *, zero=False, signed=False) -> Decimal:
    """Accept explicit decimal strings, never float or exponent coercion."""
    if isinstance(value, Decimal):
        value = format(value, "f")
    if not isinstance(value, str) or not DECIMAL.fullmatch(value):
        raise RiskRejected("INVALID_DECIMAL")
    result = Decimal(value)
    if not result.is_finite() or (not signed and result < 0) or (not zero and result == 0):
        raise RiskRejected("INVALID_DECIMAL")
    return result


def parse_setup(text: str, symbol: str, source_message_id: str) -> NeurobroSetup | None:
    """Extract a single explicit setup; ambiguity requests clarification.

    Prices are copied only from the Neurobro response. Ranges, thousands
    separators, multiple targets, conflicting symbols and incomplete replies
    deliberately do not become an executable setup.
    """
    if (not isinstance(text, str) or not 0 < len(text.encode("utf-8")) <= 65536
            or not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol)
            or not isinstance(source_message_id, str) or not 0 < len(source_message_id) <= 160):
        return None
    mentioned = set(re.findall(r"(?<![A-Z0-9])([A-Z0-9]{2,18}USDT)(?![A-Z0-9])", text.upper()))
    if mentioned != {symbol}:
        return None
    sides, levels = [], {"entry": [], "tp": [], "sl": []}
    for original in text.splitlines():
        line = re.sub(r"[*`_]", "", original).strip()
        line = re.sub(r"^[\s\-•]+", "", line).strip()
        side = re.fullmatch(r"(?:(?:SIGNAL|SIDE|KEPUTUSAN|DIRECTION|SETUP)\s*:\s*)?(LONG|SHORT|HOLD)(?:\s+(?:[A-Z0-9]{2,18}USDT))?", line, re.I)
        if side:
            sides.append(side.group(1).upper())
        level = re.fullmatch(r"(ENTRY|TP|TAKE PROFIT|SL|STOP LOSS)\s*[:=]\s*(\d{1,32}(?:\.\d{1,24})?)\s*(?:USDT)?", line, re.I)
        if level:
            key = {"TAKE PROFIT": "tp", "STOP LOSS": "sl"}.get(level.group(1).upper(), level.group(1).lower())
            levels[key].append(level.group(2))
        elif re.match(r"^(?:ENTRY|TP\d*|TAKE PROFIT|SL|STOP LOSS)\s*[:=]", line, re.I):
            return None
    if len(sides) != 1 or sides[0] == "HOLD" or any(len(v) != 1 for v in levels.values()):
        return None
    try:
        entry, tp, sl = (number(levels[key][0]) for key in ("entry", "tp", "sl"))
    except RiskRejected:
        return None
    return NeurobroSetup(symbol, sides[0], entry, sl, tp, source_message_id,
                         text, hashlib.sha256(text.encode("utf-8")).hexdigest())


def explicit_pending_decision(text: str) -> str | None:
    """Admit one unconditional explicit decision copied from Neurobro text."""
    if not isinstance(text, str) or not 0 < len(text.encode("utf-8")) <= 16000:
        return None
    if re.search(r"\b(?:jika|apabila|kalau|kecuali|unless|if|until|conditional)\b", text, re.I):
        return None
    decisions = []
    for original in text.splitlines():
        line = re.sub(r"[*`_]", "", original).strip().lstrip("-• ")
        match = re.fullmatch(r"(?:(?:KEPUTUSAN|DECISION)\s*:\s*)?(TAHAN|BATALKAN)[.!]?", line, re.I)
        if match:
            decisions.append(match.group(1).upper())
        elif re.search(r"\b(?:TAHAN|BATALKAN)\b", line, re.I):
            return None
    return decisions[0] if len(decisions) == 1 else None


def fresh(value, now: float, max_age: int, code: str) -> float:
    if type(value) not in (int, float) or not 0 <= now - value <= max_age:
        raise RiskRejected(code)
    return value


def capacity(account: AccountSnapshot, authority: Mapping, *, now: float | None = None) -> int:
    """Count all positions/pending, including earlier days and manual trades."""
    now = time.time() if now is None else now
    fresh(account.observed_at, now, MAX_ACCOUNT_AGE, "STALE_ACCOUNT")
    if not account.robot_on:
        raise RobotOff("ROBOT_OFF")
    if authority.get("ownership_reconciled") is not True:
        raise RiskRejected("OWNERSHIP_NOT_RECONCILED")
    day = datetime.fromtimestamp(now, ZoneInfo("Asia/Jakarta")).date().isoformat()
    if account.jakarta_day != day:
        raise RiskRejected("ACCOUNT_DAY_CHANGED")
    count = authority.get("occupied_slots")
    if type(count) is not int or count < account.occupied_slots or not 0 <= count <= 4096:
        raise RiskRejected("CAPACITY_NOT_RECONCILED")
    if type(account.daily_new_trades) is not int or account.daily_new_trades < 0:
        raise RiskRejected("DAILY_ENTRIES_UNVERIFIED")
    return max(0, min(2 - count, 2 - account.daily_new_trades))


def evaluate_setup(setup: NeurobroSetup, account: AccountSnapshot, authority: Mapping,
                   *, now: float | None = None) -> dict:
    """Run the unchanged Risk Manager with source-bound, fresh backend evidence."""
    now = time.time() if now is None else now
    if capacity(account, authority, now=now) < 1:
        raise RiskRejected("NO_CAPACITY")
    parsed = parse_setup(setup.source_text, setup.symbol, setup.source_message_id)
    if parsed is None or parsed != setup:
        raise RiskRejected("NEUROBRO_SOURCE_LEVELS_UNVERIFIED")
    if setup.symbol not in account.supported_symbols:
        raise RiskRejected("SYMBOL_UNSUPPORTED")
    occupied = {p.symbol for p in account.positions if p.quantity != 0} | {
        p.symbol for p in account.pending if p.quantity > p.executed_quantity}
    if setup.symbol in occupied:
        raise RiskRejected("SYMBOL_ALREADY_OCCUPIED")
    if authority.get("position_mode") != "ONE_WAY" or authority.get("margin_mode") != "CROSS":
        raise RiskRejected("ACCOUNT_MODE_UNVERIFIED")
    risk = number(account.risk_usdt)
    if risk > 100:
        raise RiskRejected("RISK_SETTING_OUT_OF_RANGE")
    if number(account.balance_usdt, zero=True) <= 0:
        raise RiskRejected("BALANCE_UNAVAILABLE")
    try:
        rule = authority["symbol_rules"][setup.symbol]
        fee = authority["commission"][setup.symbol]
        fresh(rule["observed_at"], now, MAX_RULE_AGE, "STALE_SYMBOL_RULES")
        fresh(fee["observed_at"], now, MAX_FEE_AGE, "STALE_COMMISSION")
        if fee["source"] != "BINANCE_FUTURES_COMMISSION_RATE" or fee["symbol"] != setup.symbol:
            raise RiskRejected("COMMISSION_SOURCE_UNVERIFIED")
        if type(rule["max_leverage"]) is not int or rule["max_leverage"] < 75:
            raise RiskRejected("SYMBOL_75X_UNSUPPORTED")
        # Reuse pure risk arithmetic only. Never import robot/api_service/gateway.
        from worker.core import Rules, Signal, Review, risk_check, RISK_MODEL
        rules = Rules(
            step=number(rule["step_size"]), minimum=number(rule["min_qty"]),
            maximum=number(rule["max_qty"]), tick=number(rule["tick_size"]),
            min_notional=number(rule["min_notional"], zero=True), observed_at=rule["observed_at"],
            min_price=number(rule["min_price"], zero=True), max_price=number(rule["max_price"]),
            multiplier_up=number(rule["multiplier_up"]), multiplier_down=number(rule["multiplier_down"]),
            mark_price=number(rule["mark_price"]), taker_fee_rate=number(fee["taker_fee_rate"], zero=True),
            fee_observed_at=fee["observed_at"], fee_symbol=setup.symbol, fee_source=fee["source"])
        if authority.get("risk_model") != RISK_MODEL or authority.get("exit_slippage_rate") != "0.005":
            raise RiskRejected("EXISTING_RISK_MODEL_UNVERIFIED")
        plan = risk_check(Signal(setup.symbol, setup.side, setup.entry, setup.take_profit,
                                 setup.stop_loss), rules, risk, risk_model=RISK_MODEL)
        notional = Fraction(plan["quantity"]) * Fraction(plan["entry"])
        if notional > Fraction(number(rule["notional_cap_75"])):
            raise RiskRejected("LEVERAGE_BRACKET_EXCEEDED")
        margin = notional / 75 + Fraction(plan["entry_fee_usdt"])
        if margin > Fraction(number(authority["available_margin_usdt"], zero=True)):
            raise RiskRejected("INSUFFICIENT_AVAILABLE_MARGIN")
    except RiskRejected:
        raise
    except (KeyError, TypeError, ValueError, ImportError):
        raise RiskRejected("RISK_EVIDENCE_UNAVAILABLE") from None
    except Exception as exc:
        # Existing Review messages contain only classified risk failures; do not
        # surface arbitrary exception text from an integration implementation.
        if isinstance(exc, Review):
            raise RiskRejected("RISK_MANAGER_REJECTED") from None
        raise RiskRejected("RISK_MANAGER_UNAVAILABLE") from None
    plan["neurobro_source"] = {"provider": "NEUROBRO_WEB", "message_id": setup.source_message_id,
                               "response_sha256": setup.source_sha256}
    plan["account_generation"] = account.generation
    plan["jakarta_day"] = account.jakarta_day
    return plan
