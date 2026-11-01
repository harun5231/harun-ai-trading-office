import json
import copy
import os
import re
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.llm import INSTRUCTIONS, SCHEMA, OpenAIDialogue, dialogue_schema, read_api_key, validated_market_context
from agent.memory import Memory
from agent.models import AgentError, BudgetExceeded


def response(value):
    return {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(value)}]}]}


class LLMTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "agent.sqlite3")
        self.calls = []

    def tearDown(self):
        self.memory.close()
        self.temp.cleanup()

    def model(self, output, **kwargs):
        def transport(payload):
            self.calls.append(payload)
            return response(output)
        return OpenAIDialogue("/not/read/by/fake", self.memory, transport=transport, **kwargs)

    def test_real_responses_payload_has_no_tools_or_price_authority(self):
        model = self.model({"action": "SCREENING", "symbols": ["ETHUSDT"], "question": ""})
        answer = model.plan([], "ETHUSDT", phase="SCREENING", jakarta_day="2026-10-19")
        self.assertEqual(answer.symbols, ("ETHUSDT",))
        payload = self.calls[0]
        self.assertEqual(payload["model"], "gpt-5-mini")
        self.assertIs(payload["store"], False)
        self.assertNotIn("tools", payload)
        self.assertEqual(set(payload["text"]["format"]["schema"]["properties"]), {"action", "symbols", "question"})

    def test_current_reply_schema_keeps_model_choice_between_recommendation_and_comparison(self):
        reply = 'Prioritas **$ZEC**, dibandingkan ($HYPE).'
        model = self.model({"action": "SCREENING", "symbols": ["ZECUSDT"], "question": ""})
        answer = model.plan([{"role": "assistant", "content": "$BTC"}], reply,
                            phase="SCREENING", jakarta_day="2026-10-19")
        schema = self.calls[0]["text"]["format"]["schema"]
        self.assertEqual(schema, dialogue_schema("SCREENING", reply))
        self.assertEqual(schema["properties"]["symbols"]["items"]["enum"], ["HYPEUSDT", "ZECUSDT"])
        self.assertEqual(answer.symbols, ("ZECUSDT",))
        self.assertIn("comparison", self.calls[0]["instructions"])
        self.assertIn("dollar amounts", self.calls[0]["instructions"])
        for symbols in (["BTCUSDT"], ["ZECIFEEDERRORHYPEZECUSDT", "HYPEUSDT"]):
            with self.subTest(symbols=symbols), self.assertRaisesRegex(AgentError, "MODEL_RESPONSE_INVALID"):
                self.model({"action": "SCREENING", "symbols": symbols, "question": ""}).plan(
                    [{"role": "assistant", "content": "$BTC"}], reply,
                    phase="SCREENING", jakarta_day="2026-10-19")

    def test_phase_actions_and_empty_symbols_form_a_valid_nonempty_item_schema(self):
        expected = {"SCREENING": ["SCREENING", "CLARIFY", "HOLD"],
                    "ANALYSIS": ["CLARIFY", "HOLD", "COMPLETE"],
                    "PENDING_REVIEW": ["CLARIFY", "HOLD", "TAHAN", "BATALKAN"]}
        original = copy.deepcopy(SCHEMA)
        for phase, actions in expected.items():
            with self.subTest(phase=phase):
                schema = dialogue_schema(phase, "No explicit symbols")
                self.assertEqual(schema["properties"]["action"]["enum"], actions)
                symbols = schema["properties"]["symbols"]
                self.assertEqual(symbols["maxItems"], 0)
                self.assertEqual(symbols["items"]["type"], "string")
                self.assertNotIn("enum", symbols["items"])
        self.assertEqual(SCHEMA, original)
        for phase, action, accepted in (("SCREENING", "COMPLETE", False),
                ("ANALYSIS", "COMPLETE", True), ("PENDING_REVIEW", "COMPLETE", False),
                ("ANALYSIS", "SCREENING", False), ("SCREENING", "TAHAN", False),
                ("ANALYSIS", "BATALKAN", False), ("PENDING_REVIEW", "TAHAN", True),
                ("PENDING_REVIEW", "BATALKAN", True), ("SCREENING", "CLARIFY", True)):
            with self.subTest(phase=phase, action=action):
                model = self.model({"action": action, "symbols": [], "question": "Konfirmasi?" if action == "CLARIFY" else ""})
                if accepted:
                    self.assertEqual(model.plan([], "TAHAN BATALKAN", phase=phase,
                        jakarta_day="2026-10-19").action, action)
                else:
                    with self.assertRaisesRegex(AgentError, "MODEL_RESPONSE_INVALID"):
                        model.plan([], "TAHAN BATALKAN", phase=phase, jakarta_day="2026-10-19")

    def test_schema_attribution_preserves_installed_validator_token_boundaries(self):
        from agent.dialogue_service import DialogueError, validate_decision
        replies = ('ZECUSDT', 'ZEC/USDT', 'ZEC-USDT', 'ZEC USDT', '$ZEC/USDT',
            '$ZEC', '($ZEC),', '**$ZEC**.', '$ZEC.', '$zec!', 'Pilihan: $ZEC;',
            '$HYPE', 'ZEC', 'Zcash', '$ZECO', '$AZEC', '$ZEC2', '$ZEC_USDT',
            '$ZEC/USDC', '$ZEC/', '$ZEC-USDC', '$ZEC.USDC', '$$ZEC', 'prefix$ZEC',
            'é$ZEC', '$ZECé', '$ZEC\u0301', 'é\u0301$ZEC', '/$ZEC', '-$ZEC', '$ZEC—',
            '$ıcp', '$ſol', '$ﬂoki', '$ß', '1000PEPEUSDT')
        candidates = ('ZECUSDT', 'HYPEUSDT', 'ZECOUSDT', 'AZECUSDT', 'ZEC2USDT',
                      'ICPUSDT', 'SOLUSDT', 'FLOKIUSDT', 'SSUSDT', '1000PEPEUSDT')
        for reply in replies:
            allowed = dialogue_schema("SCREENING", reply)["properties"]["symbols"]["items"].get("enum", [])
            for symbol in candidates:
                with self.subTest(reply=reply, symbol=symbol):
                    decision = {"action": "SCREENING", "symbols": [symbol], "question": ""}
                    try:
                        validate_decision(decision, {"phase": "SCREENING", "reply": reply})
                    except DialogueError:
                        accepted = False
                    else:
                        accepted = True
                    self.assertEqual(symbol in allowed, accepted)
        schema = dialogue_schema("SCREENING", '$BTCUSDT $zecusdt $HYPE $7M')
        self.assertEqual(schema["properties"]["symbols"]["items"]["enum"],
                         ['BTCUSDT', 'HYPEUSDT', 'ZECUSDT'])

    def test_dollar_amount_cashtags_are_excluded_without_filtering_explicit_pairs(self):
        reply = '$ZEC near $98 with $7M volume; compare $HYPE. $12k $9b $2T'
        symbols = dialogue_schema("SCREENING", reply)["properties"]["symbols"]["items"]["enum"]
        self.assertEqual(symbols, ['HYPEUSDT', 'ZECUSDT'])
        schema = dialogue_schema("SCREENING", '$7m $98')
        self.assertEqual(schema["properties"]["symbols"]["maxItems"], 0)
        self.assertNotIn("enum", schema["properties"]["symbols"]["items"])
        schema = dialogue_schema("SCREENING", '7MUSDT 98USDT $7MUSDT $1000SHIB $1INCH')
        self.assertEqual(schema["properties"]["symbols"]["items"]["enum"],
                         ['1000SHIBUSDT', '1INCHUSDT', '7MUSDT', '98USDT'])
        for amount in ('7MUSDT', '98USDT'):
            with self.subTest(amount=amount), self.assertRaisesRegex(AgentError, "MODEL_RESPONSE_INVALID"):
                self.model({"action": "SCREENING", "symbols": [amount], "question": ""}).plan(
                    [], reply, phase="SCREENING", jakarta_day="2026-10-19")

    def test_schema_bytes_are_reserved_before_transport_with_existing_limits(self):
        model = self.model({"action": "SCREENING", "symbols": ["ZECUSDT"], "question": ""})
        with patch.object(self.memory, "reserve_budget", wraps=self.memory.reserve_budget) as reserve:
            model.plan([], "$ZEC", phase="SCREENING", jakarta_day="2026-10-19")
        payload = self.calls[0]
        expected = len(json.dumps(payload["input"], ensure_ascii=False).encode()) + len(INSTRUCTIONS.encode())
        expected += len(json.dumps(payload["text"]["format"]["schema"]).encode())
        self.assertEqual(reserve.call_args.args, ("2026-10-19", expected, model.max_output_tokens))
        self.assertEqual(reserve.call_args.kwargs, model.limits)
        self.calls.clear()
        constrained = self.model({"action": "SCREENING", "symbols": ["ZECUSDT"], "question": ""},
                                 daily_input_tokens=expected - 1)
        with self.assertRaises(BudgetExceeded):
            constrained.plan([], "$ZEC", phase="SCREENING", jakarta_day="2026-10-20")
        self.assertEqual(self.calls, [])

    def test_unrepresentable_provider_enum_is_rejected_before_reservation_or_dispatch(self):
        reply = ' '.join('$A' + str(index) for index in range(1200))
        self.assertLess(len(reply), 16000)
        model = self.model({"action": "CLARIFY", "symbols": [], "question": "Konfirmasi?"})
        with patch.object(self.memory, "reserve_budget") as reserve:
            with self.assertRaisesRegex(AgentError, "MODEL_INPUT_TOO_LARGE"):
                model.plan([], reply, phase="SCREENING", jakarta_day="2026-10-19")
        reserve.assert_not_called()
        self.assertEqual(self.calls, [])
        boundary = ' '.join('$A' + str(index) for index in range(997))
        self.assertEqual(len(dialogue_schema("SCREENING", boundary)["properties"]["symbols"]["items"]["enum"]), 997)
        with self.assertRaisesRegex(AgentError, "MODEL_INPUT_TOO_LARGE"):
            dialogue_schema("PENDING_REVIEW", boundary)

    def test_model_authored_prices_are_rejected(self):
        model = self.model({"action": "COMPLETE", "symbols": [], "question": "", "entry": "100"})
        with self.assertRaisesRegex(AgentError, "RESPONSE_INVALID"):
            model.plan([], "text", phase="ANALYSIS", jakarta_day="2026-10-19")

    def test_unused_question_is_cleared_after_validation_without_changing_action_or_symbols(self):
        from agent.dialogue_service import DialogueError, validate_decision
        # Match the retained fault's SCREENING/ZECUSDT/77-character question shape.
        question = "Clarification text retained in the raw provider result.".ljust(77, "?")
        for phase, action in (("SCREENING", "SCREENING"), ("ANALYSIS", "COMPLETE"),
                ("ANALYSIS", "HOLD"), ("PENDING_REVIEW", "TAHAN"), ("PENDING_REVIEW", "BATALKAN")):
            with self.subTest(phase=phase, action=action):
                raw = {"action": action, "symbols": ["ZECUSDT"], "question": question}
                reply = "$ZEC " + action
                with self.assertRaisesRegex(DialogueError, "DIALOGUE_MODEL_RESULT_INVALID"):
                    validate_decision(raw, {"phase": phase, "reply": reply})
                answer = self.model(raw).plan([], reply, phase=phase, jakarta_day="2026-10-19")
                normalized = {"action": answer.action, "symbols": list(answer.symbols), "question": answer.question}
                self.assertEqual(normalized, {**raw, "question": ""})
                self.assertIs(validate_decision(normalized, {"phase": phase, "reply": reply}), normalized)
                self.assertEqual(raw["question"], question)
                self.assertEqual(len(raw["question"]), 77)

    def test_clarify_question_is_preserved_and_native_validation_stays_strict(self):
        from agent.dialogue_service import DialogueError, validate_decision
        for question in ("Mohon konfirmasi ZECUSDT?", "Apakah ENTRY: 100 tepat?"):
            raw = {"action": "CLARIFY", "symbols": [], "question": question}
            answer = self.model(raw).plan([], "$ZEC", phase="SCREENING", jakarta_day="2026-10-19")
            self.assertEqual(answer.question, question)
            normalized = {"action": answer.action, "symbols": list(answer.symbols), "question": answer.question}
            if "100" in question:
                with self.assertRaisesRegex(DialogueError, "DIALOGUE_PRICE_PROPOSAL_REFUSED"):
                    validate_decision(normalized, {"phase": "SCREENING", "reply": "$ZEC"})
            else:
                self.assertIs(validate_decision(normalized, {"phase": "SCREENING", "reply": "$ZEC"}), normalized)

    def test_strict_generation_schema_allows_natural_sourced_numeric_clarifications(self):
        for phase in ("SCREENING", "ANALYSIS", "PENDING_REVIEW"):
            with self.subTest(phase=phase):
                self.model({"action":"CLARIFY","symbols":[],"question":"Mohon perjelas konteks 15M dan 1H?"}).plan(
                    [], "$ZEC belum lengkap", phase=phase, jakarta_day="2026-10-19")
                payload=self.calls[-1];format=payload["text"]["format"]
                self.assertTrue(format["strict"])
                question=format["schema"]["properties"]["question"]
                self.assertEqual(question,{"type":"string","maxLength":1600})
                self.assertNotIn("pattern",question)
                self.assertIn("bahasa Indonesia",payload["instructions"])
                self.assertIn("Angka harus berasal",payload["instructions"])
                self.assertNotIn("no digit characters",payload["instructions"])

    def test_complete_current_hold_ends_analysis_without_coercing_ambiguous_mentions(self):
        from agent.dialogue_service import validate_decision
        reply='$ONDO: HOLD saat ini. Skenario masa depan menunggu konfirmasi.\n```json\n{"symbol":"ONDOUSDT","side":"HOLD","limit_entry":null,"take_profit":null,"stop_loss":null,"risk_reward":null}\n```\nPertanyaan lanjutan: cek 15M atau 1H?'
        raw={"action":"HOLD","symbols":["ONDOUSDT"],"question":""}
        answer=self.model(raw).plan([],reply,phase="ANALYSIS",jakarta_day="2026-10-19")
        self.assertEqual((answer.action,answer.question),("HOLD",""))
        self.assertIs(validate_decision(raw,{"phase":"ANALYSIS","reply":reply}),raw)
        self.assertIn("HOLD pasar yang jelas",self.calls[-1]["instructions"])
        self.assertIn("skenario lama",self.calls[-1]["instructions"])
        self.assertTrue(self.calls[-1]["input"][-1]["content"].endswith(reply))
        ambiguous='$ONDO previously HOLD; the current decision is incomplete.'
        result=self.model({"action":"CLARIFY","symbols":[],"question":"Mohon lengkapi keputusan saat ini?"}).plan(
            [],ambiguous,phase="ANALYSIS",jakarta_day="2026-10-19")
        self.assertEqual(result.action,"CLARIFY")
        self.assertEqual(result.question,"Mohon lengkapi keputusan saat ini?")

    def test_unvalidated_raw_output_is_retained_before_field_rejection(self):
        raw={"action":"CLARIFY","symbols":[],"question":"ENTRY: 100 tepat?","entry":100}
        with self.assertRaisesRegex(AgentError,"MODEL_RESPONSE_INVALID"):
            self.model(raw).plan([],"$ZEC belum lengkap",phase="SCREENING",jakarta_day="2026-10-19")
        records=[json.loads(row[0])for row in self.memory.db.execute("SELECT value FROM metadata WHERE key LIKE 'dialogue-model-raw:%'")]
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]["raw_decision"],raw)
        self.assertEqual(json.loads(records[0]["raw_output_text"]),raw)
        self.assertEqual(records[0]["authority"],"UNVALIDATED_MODEL_OUTPUT")
        self.assertTrue(records[0]["json_decoded"])

    def test_raw_evidence_failure_does_not_replace_the_original_model_rejection(self):
        with patch.object(self.memory,"put",side_effect=OSError("private evidence unavailable")):
            with self.assertRaisesRegex(AgentError,"MODEL_RESPONSE_INVALID"):
                self.model({"action":"FORGED","symbols":[],"question":""}).plan(
                    [],"$ZEC",phase="SCREENING",jakarta_day="2026-10-19")

    def test_unused_question_normalization_cannot_hide_invalid_raw_fields(self):
        base = {"action": "SCREENING", "symbols": ["ZECUSDT"], "question": "unused"}
        changes = ({"question": None}, {"question": 77}, {"question": ["unused"]},
            {"question": "x" * 1601}, {"action": "COMPLETE"}, {"action": []},
            {"symbols": "ZECUSDT"}, {"symbols": ["HYPEUSDT"]},
            {"symbols": ["ZECUSDT", "ZECUSDT"]}, {"entry": "100"})
        for change in changes:
            with self.subTest(change=change), self.assertRaisesRegex(AgentError, "MODEL_RESPONSE_INVALID"):
                self.model({**base, **change}).plan([], "$ZEC", phase="SCREENING", jakarta_day="2026-10-19")

    def test_output_timeout_has_no_automatic_retry(self):
        calls = []
        def transport(payload):
            calls.append(1)
            raise AgentError("MODEL_REQUEST_UNCONFIRMED")
        model = OpenAIDialogue("/unused", self.memory, transport=transport, daily_requests=1)
        with self.assertRaises(AgentError):
            model.plan([], "HOLD", phase="ANALYSIS", jakarta_day="2026-10-19")
        with self.assertRaises(BudgetExceeded):
            model.plan([], "HOLD", phase="ANALYSIS", jakarta_day="2026-10-19")
        self.assertEqual(calls, [1])

    def test_incomplete_responses_are_not_decisions(self):
        model = OpenAIDialogue("/unused", self.memory, transport=lambda _: {"status": "incomplete"})
        with self.assertRaisesRegex(AgentError, "INCOMPLETE"):
            model.plan([], "HOLD", phase="ANALYSIS", jakarta_day="2026-10-19")

    def test_invalid_symbols_and_duplicates_rejected(self):
        for symbols in (["BTCUSDT", "BTCUSDT"], ["BTC/USDT"], ["ETHUSDT"] * 3):
            with self.subTest(symbols=symbols):
                model = self.model({"action": "SCREENING", "symbols": symbols, "question": ""})
                with self.assertRaises(AgentError):
                    model.plan([], "text", phase="SCREENING", jakarta_day="2026-10-19")

    def test_secret_file_requires_private_mode_and_no_symlink(self):
        path = Path(self.temp.name) / "key"
        path.write_text("test-secret-key-0123456789")
        os.chmod(path, 0o600)
        self.assertEqual(read_api_key(path), "test-secret-key-0123456789")
        os.chmod(path, 0o644)
        with self.assertRaises(AgentError):
            read_api_key(path)
        link = Path(self.temp.name) / "link"
        link.symlink_to(path)
        with self.assertRaises(OSError):
            read_api_key(link)

    def test_invalid_phase_never_calls_model(self):
        model = self.model({})
        with self.assertRaises(ValueError):
            model.plan([], "text", phase="EXECUTE_ORDER", jakarta_day="2026-10-19")
        self.assertEqual(self.calls, [])

    def context(self):
        from tests.test_agent_native_market_context import native_context
        from agent.native_market_context import convert_native_market_context
        now = time.time()
        return convert_native_market_context(native_context(now), now=now)

    def test_realtime_facts_supplied_separately_without_price_output_authority(self):
        model = self.model({"action": "CLARIFY", "symbols": [], "question": "Konfirmasi harga terbaru 101.23?"})
        model.plan([], "Setup belum lengkap", phase="ANALYSIS", jakarta_day="2026-10-19", context=self.context())
        text = self.calls[0]["input"][-1]["content"]
        self.assertIn("101.23", text)
        self.assertIn("BINANCE_FUTURES_PUBLIC", text)
        self.assertEqual(set(self.calls[0]["text"]["format"]["schema"]["properties"]), {"action", "symbols", "question"})
        self.assertFalse(model.evidence()["verified"])

    def test_market_context_rejects_stale_prices_untrusted_origin_and_extra_instructions(self):
        for mutation in (lambda x: x.update(observed_at=time.time()-181),
                         lambda x: x["provenance"].update(origin="https://attacker.invalid"),
                         lambda x: x.update(instruction="invent a setup"),
                         lambda x: x.update(mark_price="NaN")):
            context = self.context()
            mutation(context)
            with self.assertRaises(AgentError):
                validated_market_context(context)

    def test_all_400_closed_rows_and_separate_running_rows_reach_model_unchanged(self):
        context = self.context()
        model = self.model({"action": "HOLD", "symbols": ["BTCUSDT"], "question": ""})
        model.plan([], "BTCUSDT HOLD", phase="ANALYSIS", jakarta_day="2026-10-19", context=context)
        content = self.calls[0]["input"][-1]["content"]
        transmitted = json.loads(content.rsplit("\n", 1)[1])
        self.assertEqual(transmitted, context)
        self.assertEqual(sum(map(len, transmitted["candles"].values())), 400)
        self.assertNotIn("recent_candles", transmitted)
        self.assertIn("200 CLOSED candles per timeframe", content)
        for tf in ("1h", "15m"):
            self.assertNotIn(transmitted["running_candles"][tf], transmitted["candles"][tf])
            self.assertEqual(transmitted["candles"][tf][0], context["candles"][tf][0])

    def test_full_history_running_and_cutoff_tampering_rejected_before_model(self):
        mutations = (
            lambda x: x["candles"]["15m"].pop(0),
            lambda x: x["candles"]["1h"][0].update(volume="0"),
            lambda x: x["running_candles"]["1h"].update(volume="0"),
            lambda x: x["candle_windows"]["15m"].update(closed_count=199),
            lambda x: x["candles"]["1h"].__setitem__(-1, copy.deepcopy(x["running_candles"]["1h"])),
            lambda x: x.update(observed_at_utc="2000-01-01T00:00:00+00:00"),
            lambda x: x["provenance"].update(running_candles_sha256="0" * 64),
            lambda x: x.update(recent_candles=x["candles"]),
        )
        model = self.model({"action": "HOLD", "symbols": ["BTCUSDT"], "question": ""})
        for mutate in mutations:
            context = self.context()
            mutate(context)
            with self.subTest(mutate=mutate), self.assertRaises(AgentError):
                model.plan([], "BTCUSDT HOLD", phase="ANALYSIS", jakarta_day="2026-10-19", context=context)
        self.assertEqual(self.calls, [])

    def test_bounded_collection_latency_and_future_host_skew_match_public_chart_contract(self):
        from agent.llm import market_context_from_snapshot
        from tests.test_agent_charts_closed_200 import snapshot_fixture, SERVER_MS
        import datetime as dt
        snapshot = snapshot_fixture()
        snapshot["provenance"]["chart_kind"] = "CANDLESTICK_RENDER_FROM_PUBLIC_KLINES"
        snapshot["observed_at"] = SERVER_MS / 1000 + 45
        snapshot["observed_at_utc"] = dt.datetime.fromtimestamp(snapshot["observed_at"], dt.timezone.utc).isoformat()
        snapshot["collection_finished_server_time_ms"] = SERVER_MS + 45000
        snapshot["symbol_rules"] = {"private_canary": object()}
        value = market_context_from_snapshot(snapshot, now=snapshot["observed_at"])
        self.assertNotIn("symbol_rules", value)
        self.assertNotIn("collection_finished_server_time_ms", value)
        # A verified Binance mark can be slightly ahead of the host clock.
        value["mark_time_ms"] = int((snapshot["observed_at"] + 20) * 1000)
        validated_market_context(value, now=snapshot["observed_at"])
        value["mark_time_ms"] += 11000
        with self.assertRaises(AgentError):
            validated_market_context(value, now=snapshot["observed_at"])

    def test_absolute_response_deadline_does_not_reset_for_slow_drip_body(self):
        reads, timeouts = [], []
        class Response:
            fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=lambda n: timeouts.append(n))))
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read1(self, count):
                reads.append(count)
                return b"{"
        opener = SimpleNamespace(open=lambda *args, **kwargs: Response())
        model = OpenAIDialogue("/not-read", self.memory)
        with patch("agent.llm.read_api_key", return_value="test-key-value-000000000"), \
             patch("agent.llm.urllib.request.build_opener", return_value=opener), \
             patch("agent.llm.time.monotonic", side_effect=(0, 0, 46)):
            with self.assertRaisesRegex(AgentError, "UNCONFIRMED"):
                model._request({})
        self.assertEqual(reads, [16384])
        self.assertEqual(timeouts, [45])


if __name__ == "__main__":
    unittest.main()
