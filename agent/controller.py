"""Bounded, persisted Neurobro workflow. Every active operation rechecks ROBOT ON.

The controller owns no exchange credentials and never imports the production
coordinator or order gateway. Only the authenticated backend may execute a setup.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import math
import re
import threading
import time
import uuid

from worker.prompts import ANALYSIS, REPLACEMENT_SCREENING, SCREENING

from .models import AccountSnapshot, AgentError, AmbiguousOperation, BudgetExceeded, MarketContext, NotSent, RobotOff, TickResult
from .llm import market_context_from_snapshot


class Controller:
    def __init__(self, *, bridge, provider, market, llm, memory, analysis_prompt,
                 render_chart, parse_setup, screening_prompt="", clock=time.time,
                 max_followups=2, max_market_age=180,
                 account_max_age=30, monotonic=time.monotonic):
        if not isinstance(analysis_prompt, str) or not 1 <= len(analysis_prompt) <= 16000:
            raise ValueError("EXACT_ANALYSIS_PROMPT_REQUIRED")
        if max_followups not in (0, 1, 2):
            raise ValueError("DIALOGUE_BOUND_INVALID")
        if analysis_prompt != ANALYSIS:
            raise ValueError("EXACT_APPROVED_ANALYSIS_PROMPT_MISMATCH")
        self.bridge, self.provider, self.market, self.llm = bridge, provider, market, llm
        self.memory, self.analysis_prompt = memory, analysis_prompt
        self.render_chart, self.parse_setup = render_chart, parse_setup
        self.screening_prompt = screening_prompt
        self.clock, self.max_followups = clock, max_followups
        self.max_market_age = max_market_age
        self.account_max_age = account_max_age
        self._tick_lock = threading.Lock()
        self._last = TickResult("STARTING", "IDLE")
        self._last_tick = None
        self._observations = {}
        self._monotonic = monotonic
        self._deadline = None
        # Cooperative adapters constrain each new connection to the time left.
        # A submitted request that outlives this boundary remains unconfirmed;
        # this controller never retries it merely because its budget expired.
        for adapter in (self.bridge, self.provider, self.market, self.llm):
            configure = getattr(adapter, "set_remaining_budget", None)
            if callable(configure):
                configure(self.remaining_budget)

    def remaining_budget(self):
        remaining = 120.0 if self._deadline is None else self._deadline - self._monotonic()
        if remaining <= 0:
            raise NotSent("TASK_BUDGET_EXHAUSTED")
        return remaining

    def _observe(self, role, adapter):
        evidence = getattr(adapter, "evidence", None)
        if callable(evidence):
            try:
                value = evidence()
            except Exception:
                # Health metadata must never discard a valid delivery receipt.
                return
            if isinstance(value, dict):
                # Closed metadata only. Credentials, endpoints, conversation IDs
                # and arbitrary adapter diagnostics never enter health output.
                safe = {}
                if type(value.get("verified")) is bool:
                    safe["verified"] = value["verified"]
                if type(value.get("observed_at")) in (int, float) and math.isfinite(value["observed_at"]):
                    safe["observed_at"] = value["observed_at"]
                for key, pattern in (("source_sha256", r"[0-9a-f]{64}"),
                                     ("revision", r"[A-Za-z0-9_.:-]{1,128}"),
                                     ("model", r"gpt-5-mini"),
                                     ("transport", r"[A-Z_]{1,64}")):
                    if isinstance(value.get(key), str) and re.fullmatch(pattern, value[key]):
                        safe[key] = value[key]
                self._observations[role] = safe

    def _invalidate(self, role):
        self._observations[role] = {**self._observations.get(role, {}),
                                    "verified": False, "observed_at": self.clock()}

    def _account(self):
        self.remaining_budget()
        try:
            value = self.bridge.snapshot()
        except BaseException:
            self._invalidate("backend")
            raise
        if (not isinstance(value, AccountSnapshot) or type(value.robot_on) is not bool or
                not value.generation or not 0 <= self.clock() - value.observed_at <= self.account_max_age or
                type(value.daily_new_trades) is not int or value.daily_new_trades < 0 or
                not value.balance_usdt.is_finite() or not value.risk_usdt.is_finite() or
                value.balance_usdt < 0 or value.risk_usdt <= 0 or
                len(value.positions) > 100 or len(value.pending) > 100 or
                value.jakarta_day != datetime.fromtimestamp(self.clock(), timezone(timedelta(hours=7))).date().isoformat()):
            raise AgentError("ACCOUNT_OBSERVATION_INVALID_OR_STALE")
        self._observe("backend", self.bridge)
        return value

    def robot_enabled(self):
        try:
            return self._account().robot_on
        except Exception:
            return False

    def _on(self):
        account = self._account()
        if not account.robot_on:
            raise RobotOff("ROBOT_OFF")
        return account

    @staticmethod
    def _op_id(cycle, suffix):
        import json
        return hashlib.sha256(json.dumps([cycle["id"], suffix], separators=(",", ":")).encode()).hexdigest()

    def _exchange(self, cycle, phase, text, *, attachments=()):
        self._on()
        payload = {"phase": phase, "text": text, "conversation_id": cycle.get("conversation_id"),
                   "attachments": [hashlib.sha256(x).hexdigest() for x in attachments]}
        key = self._op_id(cycle, phase + ":" + str(cycle.get("round", 0)) + ":" +
                          str(cycle.get("active", "")) + ":" + str(cycle.get("followups", 0)))
        def send():
            self._on()
            try:
                reply = self.provider.exchange(text, attachments_png=attachments,
                    conversation_id=cycle.get("conversation_id"), request_id=key)
            except BaseException:
                self._invalidate("browser")
                raise
            if (not isinstance(reply.text, str) or not 1 <= len(reply.text) <= 16000 or
                    not isinstance(reply.conversation_id, str) or not 1 <= len(reply.conversation_id) <= 512 or
                    not isinstance(reply.response_id, str) or not 1 <= len(reply.response_id) <= 512):
                raise AgentError("NEUROBRO_RESPONSE_INVALID")
            self._observe("browser", self.provider)
            return {"text": reply.text, "conversation_id": reply.conversation_id,
                    "response_id": reply.response_id}
        reply = self.memory.operation(key, "NEUROBRO_EXCHANGE", payload, send)
        cycle["conversation_id"] = reply["conversation_id"]
        cycle["reply"] = reply
        self.memory.append(cycle["conversation_id"], "user", text, self.clock())
        self.memory.append(cycle["conversation_id"], "assistant", reply["text"], self.clock())

    def _decision(self, cycle, phase):
        account = self._on()
        key = self._op_id(cycle, "MODEL:" + phase + ":" + cycle["reply"]["response_id"])
        history = self.memory.history(cycle["conversation_id"])
        context = None
        if phase != "SCREENING":
            market = self._context(cycle)
            data = market.data
            context = market_context_from_snapshot(data, now=self.clock())
        payload = {"phase": phase, "reply": cycle["reply"]["text"], "history": history, "context": context}
        def plan():
            self._on()
            try:
                result = self.llm.plan(history, cycle["reply"]["text"], phase=phase,
                                       jakarta_day=account.jakarta_day, context=context)
            except BaseException:
                self._invalidate("model")
                raise
            self._observe("model", self.llm)
            return {"action": result.action, "symbols": list(result.symbols), "question": result.question}
        return self.memory.operation(key, "MODEL_DIALOGUE", payload, plan)

    def _market(self, symbol):
        self._on()
        data = self.market.snapshot(symbol)
        try:
            observed = float(data["observed_at"])
        except (ValueError, TypeError, KeyError):
            raise AgentError("MARKET_OBSERVATION_INVALID") from None
        if not 0 <= self.clock() - observed <= self.max_market_age:
            raise AgentError("MARKET_OBSERVATION_STALE")
        self._on()
        images = tuple(self.render_chart(data, timeframe) for timeframe in ("1h", "15m"))
        if any(not isinstance(image, bytes) or len(image) > 2 * 1024 * 1024 or not image.startswith(b"\x89PNG\r\n\x1a\n") for image in images):
            raise AgentError("MARKET_CHART_INVALID")
        return MarketContext(symbol, observed, data, images)

    def _context(self, cycle):
        data = cycle["market"]
        observed = float(data["observed_at"])
        if not 0 <= self.clock() - observed <= self.max_market_age:
            raise NotSent("SETUP_MARKET_CONTEXT_STALE")
        return MarketContext(cycle["active"], observed, data)

    def _screen_text(self, cycle, count):
        excluded = ", ".join(cycle["excluded"]) or "tidak ada"
        base = REPLACEMENT_SCREENING.format(count=count) if cycle["excluded"] else (SCREENING if count == 2 else SCREENING.replace("2 coin", "1 coin", 1))
        return base + ("\nCoin yang wajib dikecualikan: " + excluded if cycle["excluded"] else "")

    @staticmethod
    def _pending_prompt(cycle, question=None):
        reference = hashlib.sha256(cycle["order_id"].encode()).hexdigest()
        symbol = cycle["active"]
        text = (f"PENDING_REVIEW symbol={symbol}\norder_ref: {reference}\n"
                f"Evaluasi LIMIT {symbol} yang belum terisi penuh selama 5 jam. "
                "Berdasarkan chart dan data terbaru, Neurobro pilih TAHAN atau BATALKAN sisa pending. "
                "Jangan ubah posisi yang telah terisi. Jawab keputusan eksplisit dan alasan singkat.")
        return text if question is None else text + "\nPertanyaan lanjutan: " + question

    def _save(self, cycle):
        self.memory.put("active_cycle", cycle)

    def _finish(self, cycle, reason):
        self.memory.put("last_completed_cycle", {"id": cycle["id"], "reason": reason, "at": self.clock()})
        self.memory.put("active_cycle", None)
        self.memory.put("next_screen_at", self.clock() + 120)
        return TickResult("IDLE", "COMPLETE", reason)

    def _next_symbol(self, cycle):
        cycle["active"] = None
        cycle["followups"] = 0
        cycle.pop("market", None)
        cycle.pop("reply", None)
        cycle.pop("followup_market_id", None)
        cycle["phase"] = "ANALYSIS_REQUEST" if cycle["queue"] else "SCREEN_REQUEST"
        self._save(cycle)

    def _clarify(self, cycle, decision, phase):
        if cycle.get("followups", 0) >= self.max_followups:
            return self._finish(cycle, "DIALOGUE_LIMIT_REACHED")
        question = decision.get("question", "")
        if not question or len(question) > 400:
            return self._finish(cycle, "NO_BOUNDED_CLARIFICATION")
        # Numeric claims in a model question must already occur in the supplied
        # Neurobro response. The controller never accepts model-authored levels.
        supplied = set(re.findall(r"\d+(?:\.\d+)?", cycle["reply"]["text"]))
        if cycle.get("market"):
            import json
            supplied.update(re.findall(r"\d+(?:\.\d+)?", json.dumps(cycle["market"])))
        if set(re.findall(r"\d+(?:\.\d+)?", question)) - supplied:
            return self._finish(cycle, "UNSUPPORTED_MODEL_NUMERIC_CLAIM")
        cycle["question"] = question
        cycle["followups"] = cycle.get("followups", 0) + 1
        cycle["phase"] = phase + "_FOLLOWUP"
        self._save(cycle)
        return TickResult("RUNNING", cycle["phase"])

    def _new_cycle(self, account):
        reviews = self.memory.get("pending_reviews", {})
        due = sorted((p for p in account.pending if p.owned and p.quantity > p.executed_quantity and
            self.clock() >= reviews.get(p.order_id, p.created_at) + 5 * 3600), key=lambda p: p.created_at)
        if due:
            order = due[0]
            cycle = {"id": uuid.uuid4().hex, "day": account.jakarta_day, "kind": "PENDING",
                     "phase": "PENDING_REQUEST", "order_id": order.order_id, "active": order.symbol,
                     "conversation_id": None, "round": 0, "followups": 0, "queue": [], "excluded": []}
        elif account.available_slots and self.clock() >= self.memory.get("next_screen_at", 0):
            cycle = {"id": uuid.uuid4().hex, "day": account.jakarta_day, "kind": "RESEARCH",
                     "phase": "SCREEN_REQUEST", "conversation_id": None, "round": 0, "followups": 0,
                     "queue": [], "excluded": [], "active": None,
                     "target": account.available_slots, "delivered": 0}
        else:
            return None
        self._save(cycle)
        return cycle

    def tick(self):
        if not self._tick_lock.acquire(blocking=False):
            return TickResult("BUSY", "IDLE", "SINGLE_CONTROLLER_ONLY")
        self._deadline = self._monotonic() + 120
        try:
            account = self._account()
            if not account.robot_on:
                result = TickResult("PAUSED", "IDLE", "ROBOT_OFF")
            elif self.memory.get("model_budget_wait_day") == account.jakarta_day:
                # Cost admission is independent of the number of coins screened.
                # Retain this cycle and its HOLD exclusions; do not start fresh
                # browser conversations while today's model budget is exhausted.
                result = TickResult("WAITING", "IDLE", "DAILY_MODEL_BUDGET_EXHAUSTED")
            else:
                cycle = self.memory.get("active_cycle") or self._new_cycle(account)
                result = self._step(cycle, account) if cycle else TickResult("IDLE", "IDLE", "NO_CAPACITY_OR_REVIEW_DUE")
        except RobotOff:
            result = TickResult("PAUSED", "IDLE", "ROBOT_OFF")
        except BudgetExceeded:
            self.memory.put("model_budget_wait_day", account.jakarta_day)
            result = TickResult("WAITING", "IDLE", "DAILY_MODEL_BUDGET_EXHAUSTED")
        except NotSent as error:
            cycle = self.memory.get("active_cycle")
            if cycle:
                self._finish(cycle, "CONFIRMED_NOT_SENT")
            code = str(error)
            result = TickResult("WAITING", "IDLE", code if re.fullmatch(r"[A-Z0-9_]{1,100}", code) else "CONFIRMED_NOT_SENT")
        except AmbiguousOperation:
            result = TickResult("REQUIRES_REVIEW", "BLOCKED", "UNCONFIRMED_OPERATION_NOT_REPLAYED")
        except AgentError as error:
            code = str(error)
            result = TickResult("BLOCKED", "BLOCKED", code if re.fullmatch(r"[A-Z0-9_]{1,100}", code) else "ADAPTER_OPERATION_UNVERIFIED")
        except Exception:
            result = TickResult("BLOCKED", "BLOCKED", "ADAPTER_OPERATION_UNVERIFIED")
        finally:
            self._deadline = None
            self._tick_lock.release()
        self._last, self._last_tick = result, self.clock()
        return result

    def _step(self, cycle, account):
        phase = cycle["phase"]
        if cycle["kind"] == "RESEARCH" and cycle["day"] != account.jakarta_day:
            return self._finish(cycle, "DAY_CHANGED_RESCREEN_REQUIRED")
        if phase == "SCREEN_REQUEST":
            if account.available_slots == 0:
                return self._finish(cycle, "CAPACITY_REACHED")
            # Capacity may grow or shrink during a long HOLD sequence. Each new
            # request follows the current combined slot and daily-entry quota.
            remaining = account.available_slots
            cycle["target"] = cycle["delivered"] + remaining
            count = max(0, min(account.available_slots, remaining) - len(cycle["queue"]))
            if not count:
                cycle["phase"] = "ANALYSIS_REQUEST"
            else:
                eligible = (set(account.supported_symbols) - set(cycle["excluded"]) -
                            {p.symbol for p in account.positions} - {p.symbol for p in account.pending})
                if len(eligible) < count:
                    self._save(cycle)
                    return TickResult("WAITING", "SCREEN_REQUEST", "NO_NEW_ELIGIBLE_SYMBOLS")
                cycle["screen_count"] = count
                self._exchange(cycle, "SCREENING", self._screen_text(cycle, count))
                cycle["phase"] = "SCREEN_DECIDE"
        elif phase == "SCREEN_DECIDE":
            decision = self._decision(cycle, "SCREENING")
            if decision["action"] == "CLARIFY":
                return self._clarify(cycle, decision, "SCREEN")
            symbols = decision["symbols"]
            observed = set(re.findall(r"\b[A-Z0-9]{2,20}USDT\b", cycle["reply"]["text"]))
            if (decision["action"] != "SCREENING" or not symbols or len(symbols) != cycle["screen_count"] or len(symbols) > account.available_slots or
                    len(set(symbols)) != len(symbols) or not set(symbols) <= observed or
                    not set(symbols) <= set(account.supported_symbols) or
                    set(symbols) & ({p.symbol for p in account.positions} | {p.symbol for p in account.pending})):
                return self._finish(cycle, "SCREENING_SYMBOLS_NOT_VERIFIED")
            if set(symbols) & set(cycle["excluded"]):
                # An explicitly excluded HOLD coin is never analysed again in
                # this cycle. Ask for different coins without losing exclusions.
                cycle["round"] += 1
                cycle["followups"] = 0
                cycle["phase"] = "SCREEN_REQUEST"
                self._save(cycle)
                return TickResult("RUNNING", "SCREEN_REQUEST", "EXCLUDED_COIN_NOT_REPEATED")
            cycle["queue"] = symbols
            cycle["followups"] = 0
            cycle["phase"] = "ANALYSIS_REQUEST"
        elif phase == "ANALYSIS_REQUEST":
            if not cycle["active"]:
                if not cycle["queue"]:
                    return self._finish(cycle, "SCREENING_QUEUE_EMPTY")
                cycle["active"] = cycle["queue"].pop(0)
                self._save(cycle)
            if account.available_slots == 0:
                return self._finish(cycle, "CAPACITY_REACHED")
            if cycle["active"] in {p.symbol for p in account.positions} | {p.symbol for p in account.pending}:
                return self._finish(cycle, "SYMBOL_NOW_OCCUPIED")
            if "market" not in cycle:
                context = self._market(cycle["active"])
                cycle["market"] = dict(context.data)
                self._save(cycle)
                return TickResult("RUNNING", "ANALYSIS_REQUEST", "FRESH_CHARTS_CAPTURED")
            else:
                context = self._context(cycle)
                context = MarketContext(context.symbol, context.observed_at, context.data,
                    tuple(self.render_chart(context.data, tf) for tf in ("1h", "15m")))
            # Exact approved user text, byte-for-byte after UTF-8 decoding. Symbol,
            # venue and timeframes are carried by the chart attachments.
            self._exchange(cycle, "ANALYSIS", self.analysis_prompt, attachments=context.screenshots)
            cycle["phase"] = "ANALYSIS_DECIDE"
        elif phase == "ANALYSIS_DECIDE":
            reply = cycle["reply"]
            setup = self.parse_setup(reply["text"], cycle["active"], reply["response_id"])
            if setup is not None:
                context = self._context(cycle)
                account = self._on()
                key = self._op_id(cycle, "HANDOFF:" + setup.source_sha256)
                payload = {"source_sha256": setup.source_sha256, "symbol": setup.symbol}
                receipt = self.memory.operation(key, "RISK_MANAGER_HANDOFF", payload,
                    lambda: self.bridge.handoff(setup, context, self._on(), key))
                if not isinstance(receipt, dict) or receipt.get("accepted") is not True:
                    return self._finish(cycle, "RISK_MANAGER_DID_NOT_ACCEPT")
                cycle["excluded"].append(cycle["active"])
                cycle["delivered"] += 1
                self._next_symbol(cycle)
                if cycle["delivered"] >= cycle["target"]:
                    return self._finish(cycle, "SETUP_FORWARDED")
            else:
                decision = self._decision(cycle, "ANALYSIS")
                if decision["action"] == "HOLD" and re.search(r"\bHOLD\b", reply["text"], re.I):
                    cycle["excluded"].append(cycle["active"])
                    cycle["round"] += 1
                    self._next_symbol(cycle)
                    if not cycle["queue"]:
                        cycle["phase"] = "SCREEN_REQUEST"
                elif decision["action"] == "CLARIFY":
                    return self._clarify(cycle, decision, "ANALYSIS")
                else:
                    return self._finish(cycle, "NEUROBRO_SETUP_INCOMPLETE")
        elif phase.endswith("_FOLLOWUP"):
            family = phase.split("_", 1)[0]
            text = self._pending_prompt(cycle, cycle["question"]) if family == "PENDING" else cycle["question"]
            attachments = ()
            if family in {"ANALYSIS", "PENDING"}:
                if family == "PENDING":
                    order = self.bridge.pending_status(cycle["order_id"])
                    if order is None or order.quantity <= order.executed_quantity:
                        return self._finish(cycle, "PENDING_NO_LONGER_OPEN")
                observation_id = self._op_id(cycle, "FOLLOWUP_MARKET:" + phase + ":" +
                                               str(cycle.get("followups", 0)))
                if cycle.get("followup_market_id") != observation_id:
                    # Every chart-dependent question gets a new public snapshot,
                    # not the original setup's older chart. Persist collection in
                    # its own tick, just like initial analysis/pending review.
                    context = self._market(cycle["active"])
                    cycle["market"] = dict(context.data)
                    cycle["followup_market_id"] = observation_id
                    self._save(cycle)
                    return TickResult("RUNNING", phase, "FRESH_FOLLOWUP_CHARTS_CAPTURED")
                context = self._context(cycle)
                attachments = tuple(self.render_chart(context.data, tf) for tf in ("1h", "15m"))
            self._exchange(cycle, phase, text, attachments=attachments)
            cycle["phase"] = family + "_DECIDE"
        elif phase == "PENDING_REQUEST":
            order = self.bridge.pending_status(cycle["order_id"])
            if order is None or order.quantity <= order.executed_quantity:
                return self._finish(cycle, "PENDING_NO_LONGER_OPEN")
            if "market" not in cycle:
                context = self._market(order.symbol)
                cycle["market"] = dict(context.data)
                self._save(cycle)
                return TickResult("RUNNING", "PENDING_REQUEST", "FRESH_CHARTS_CAPTURED")
            context = self._context(cycle)
            context = MarketContext(context.symbol, context.observed_at, context.data,
                tuple(self.render_chart(context.data, tf) for tf in ("1h", "15m")))
            self._exchange(cycle, "PENDING_REVIEW",
                self._pending_prompt(cycle),
                attachments=context.screenshots)
            cycle["phase"] = "PENDING_DECIDE"
        elif phase == "PENDING_DECIDE":
            self._context(cycle)
            decision = self._decision(cycle, "PENDING_REVIEW")
            reply = cycle["reply"]["text"]
            words = {word.upper() for word in re.findall(r"\b(?:TAHAN|BATALKAN)\b", reply, re.I)}
            explicit = set()
            for line in reply.splitlines():
                line = re.sub(r"[*`_]", "", line).strip().lstrip("-• ")
                match = re.fullmatch(r"(?:(?:KEPUTUSAN|DECISION)\s*:\s*)?(TAHAN|BATALKAN)[.!]?", line, re.I)
                if match:
                    explicit.add(match.group(1).upper())
            if explicit != {decision["action"]} or words != explicit:
                return self._clarify(cycle, decision, "PENDING")
            if decision["action"] == "BATALKAN":
                # Status is read again immediately before the bridge's atomic
                # revision/partial-fill check. Never cancel by a historical ID alone.
                order = self.bridge.pending_status(cycle["order_id"])
                if order is not None and order.quantity > order.executed_quantity:
                    # One logical cancellation keeps its ID across partial-fill
                    # revisions. A changed payload after an unknown outcome must
                    # require reconciliation, rather than create a fresh request.
                    key = self._op_id(cycle, "CANCEL_REMAINDER:" + order.order_id)
                    source = cycle["reply"]
                    source_sha256 = hashlib.sha256(source["text"].encode()).hexdigest()
                    receipt = self.memory.operation(key, "BACKEND_CANCEL_REMAINDER",
                        {"order_id": order.order_id, "revision": order.revision,
                         "executed_quantity": str(order.executed_quantity),
                         "source_message_id": source["response_id"], "source_sha256": source_sha256},
                        lambda: self.bridge.cancel_remaining(order, self._on(), key, source_reply=source))
                    if not isinstance(receipt, dict) or receipt.get("accepted") is not True:
                        return self._finish(cycle, "CANCELLATION_NOT_ACCEPTED")
            reviews = self.memory.get("pending_reviews", {})
            reviews[cycle["order_id"]] = self.clock()
            self.memory.put("pending_reviews", reviews)
            return self._finish(cycle, "PENDING_" + decision["action"])
        else:
            raise AgentError("PERSISTED_PHASE_INVALID")
        self._save(cycle)
        return TickResult("RUNNING", cycle["phase"])

    def health_status(self):
        dependencies = {}
        for name, max_age in (("backend", 30), ("browser", 1200), ("model", 1200)):
            observation = self._observations.get(name, {})
            checked = observation.get("observed_at")
            fresh = type(checked) in (int, float) and 0 <= self.clock() - checked <= max_age
            pinned = (observation.get("model") == "gpt-5-mini" if name == "model" else
                      bool(re.fullmatch(r"[0-9a-f]{64}", str(observation.get("source_sha256", "")))) and
                      observation.get("source_sha256") != "0" * 64)
            dependencies[name] = {**observation, "fresh": bool(fresh),
                                  "verified": observation.get("verified") is True and bool(fresh) and pinned}
        return {"runtime": "neurobro-agent", "state": self._last.state, "phase": self._last.phase,
                "reason": self._last.reason, "last_tick_at": self._last_tick,
                "memory": self.memory.counts(), "trading_authority": "EXISTING_BACKEND_ONLY",
                "dependencies": dependencies,
                "integration_verified": all(value["verified"] for value in dependencies.values())}
