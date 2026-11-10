"""New WEB-only JSON instructions; old native requests/renders stay immutable."""
import copy
import hashlib
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from contextlib import contextmanager
from datetime import datetime, timezone

from agent.native_browser_hook import _store_new_composed_prompt, install_native_bridge
from agent.native_data_files import final_json_prompt, FINAL_JSON_REQUEST_ENDING
from agent.native_prompt_composition import PromptCompositionCache, digest
from worker.account_state import screening_contract
from agent.native_pending_protocol import REVIEW_SCHEMA


def payload(phase, count=1):
    if phase == 'SCREENING':
        prompt, schema = screening_contract(count)
        context = None
    else:
        prompt = 'Analisis struktur dan invalidasi dari chart terbaru untuk coin ini.'
        context = {'symbol': 'ATOMUSDT'}
        schema = REVIEW_SCHEMA if phase == 'PENDING_REVIEW' else {
            'required': ['symbol', 'side', 'limit_entry', 'take_profit', 'stop_loss', 'risk_reward']}
    return {'operation': 'owned:' + phase + ':' + str(count), 'marker': 'HARUN_' + 'a' * 32,
        'prompt': prompt, 'context': context, 'output_schema': copy.deepcopy(schema)}


def old_formatter(value):
    return value['prompt'] + '\n\nOUTPUT_JSON_SCHEMA:\n' + json.dumps(value['output_schema']) + '\n' + value['marker']


class FinalJsonPromptTests(unittest.TestCase):
    def test_phase_suffix_has_capacity_correct_shape_and_never_seeds_real_screening_coins(self):
        for count in (1, 2):
            value = payload('SCREENING', count)
            before = copy.deepcopy(value)
            result = final_json_prompt(value, 'Alasan singkat.\n' + value['marker'])
            example = json.JSONDecoder().raw_decode(result.split('Format: ', 1)[1])[0]
            self.assertEqual(len(example['SYMBOLS']), count)
            self.assertEqual(len(set(example['SYMBOLS'])), count)
            self.assertTrue(all(re.fullmatch(r'[A-Z0-9]{2,24}USDT', coin) is None for coin in example['SYMBOLS']))
            self.assertNotIn('ENTRY', result)
            self.assertNotIn('SL', result)
            self.assertTrue(result.endswith(FINAL_JSON_REQUEST_ENDING))
            self.assertEqual(result.count(value['marker']), 1)
            self.assertEqual(final_json_prompt(value, result), result)
            self.assertEqual(value, before)

    def test_analysis_and_pending_use_only_the_actual_symbol_and_no_risk_or_order_identity(self):
        for phase in ('ANALYSIS', 'PENDING_REVIEW'):
            value = payload(phase)
            value['context'].update(risk_usdt='12.345678', quantity='987.654',
                pending_review={'symbol': 'ATOMUSDT', 'intent_ref': 'd' * 64})
            result = final_json_prompt(value, 'Pertanyaan teknikal yang sah.')
            self.assertIn('"COIN":"ATOMUSDT"', result)
            self.assertTrue(result.endswith(FINAL_JSON_REQUEST_ENDING))
            for secret in ('12.345678', '987.654', 'd' * 64, 'order_ref', 'risk_usdt', 'quantity'):
                self.assertNotIn(secret, result)
            if phase == 'ANALYSIS':
                self.assertIn('"ARAH":"HOLD","ENTRY":null,"TP":null,"SL":null', result)
                self.assertIn('"ENTRY":"<harga>"', result)
                self.assertNotIn('"KEPUTUSAN"', result)
            else:
                self.assertIn('"KEPUTUSAN"', result)
                self.assertIn('TAHAN', result); self.assertIn('BATALKAN', result)
                self.assertNotIn('"ENTRY"', result)

    def test_changed_capacity_or_foreign_symbol_context_refuses_before_render(self):
        value = payload('SCREENING', 1)
        for count in (2, True, 0):
            bad = copy.deepcopy(value); bad['context'] = {'requested_count': count}
            with self.subTest(count=count), self.assertRaises(ValueError):
                final_json_prompt(bad, 'Pesan.')
        value = payload('ANALYSIS'); value['context']['symbol'] = 'wrong coin'
        with self.assertRaises(ValueError):final_json_prompt(value, 'Pesan.')

    def test_historical_evaluation_is_not_changed_into_a_trading_phase(self):
        value = payload('ANALYSIS'); value['output_schema'] = {'required': ['assessment', 'findings']}
        self.assertEqual(final_json_prompt(value, 'Evaluasi historis.'), 'Evaluasi historis.')

    def test_new_composition_stores_exact_footer_for_all_actual_production_phases(self):
        for phase in ('SCREENING', 'ANALYSIS', 'PENDING_REVIEW'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); root.chmod(0o700)
                native = SimpleNamespace(user_prompt=old_formatter)
                cache = PromptCompositionCache(native, root)
                original = cache.original
                value = payload(phase); before = copy.deepcopy(value)
                rendered = _store_new_composed_prompt(cache, value, value['prompt'], 'b' * 64)
                receipt = cache.read(value)
                self.assertEqual(native.user_prompt(value), rendered)
                self.assertTrue(rendered.endswith(FINAL_JSON_REQUEST_ENDING))
                self.assertEqual(rendered.count(value['marker']), 1)
                self.assertEqual(receipt['rendered_sha256'], hashlib.sha256(rendered.encode()).hexdigest())
                self.assertEqual(receipt['payload_sha256'], digest(before))
                self.assertEqual(receipt['model_request_id'], 'b' * 64)
                self.assertIs(cache.original, original)
                self.assertEqual(value, before)

    def test_old_cached_and_cacheless_receipts_are_not_rewritten_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            value = payload('ANALYSIS'); native = SimpleNamespace(user_prompt=old_formatter)
            cache = PromptCompositionCache(native, root)
            old = cache.store(value, 'Penjelasan lama yang sudah dikirim.', 'c' * 64)
            leaf = root / cache._name(value); raw = leaf.read_bytes()
            before = copy.deepcopy(value)
            restarted_native = SimpleNamespace(user_prompt=old_formatter)
            restarted = PromptCompositionCache(restarted_native, root)
            result = _store_new_composed_prompt(restarted, value, 'Pertanyaan baru tidak boleh mengganti operasi lama.', 'd' * 64)
            self.assertEqual(result, old); self.assertEqual(restarted_native.user_prompt(value), old)
            self.assertEqual(leaf.read_bytes(), raw)
            self.assertNotIn(FINAL_JSON_REQUEST_ENDING, old)
            # An older cacheless source remains readable byte-for-byte too.
            legacy = copy.deepcopy(value); legacy['operation'] = 'older:uncached:operation'
            self.assertEqual(restarted_native.user_prompt(legacy), old_formatter(legacy))
            self.assertEqual(value, before)

    def test_configured_install_composes_the_first_send_in_all_three_phases(self):
        """Real installation/ask/cache boundary; all transports are fake.

        This checks public rendering, not file delivery. Existing four-file
        witness tests own upload admission; the real screenshot/200-bar
        dispatch guard is retained here with public-market fixtures.
        """
        from tests.test_native_browser_hook import Bridge, native as native_fixture
        from tests.test_agent_closed_candles_200 import NOW, native_context_200
        from agent.binance_website_chart import SOURCE
        from agent.native_market_context import convert_native_market_context
        from worker.neuroapi import SETUP_SCHEMA_V4
        from agent import native_browser_hook as hook_module
        for phase in ('SCREENING', 'ANALYSIS', 'PENDING_REVIEW'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); root.chmod(0o700)
                module = native_fixture()
                # The existing installed chart renderer keeps the internal
                # setup schema out of the public WEB message.
                module.user_prompt = lambda request: request['prompt'] + '\n\n' + request['marker']
                value = payload(phase); value['request_sha256'] = 'e' * 64
                if phase == 'SCREENING':
                    output = {'symbols': ['BTCUSDT']}; answer = '{"SYMBOLS":["BTCUSDT"]}'
                    self.assertIsNone(value['context'])
                else:
                    value['context'] = native_context_200(NOW)
                    if phase == 'ANALYSIS':
                        value['output_schema'] = copy.deepcopy(SETUP_SCHEMA_V4)
                        output = {'symbol': 'BTCUSDT', 'side': 'HOLD', 'limit_entry': None,
                            'take_profit': None, 'stop_loss': None, 'risk_reward': None}
                        answer = ('Keputusan akhir HOLD karena struktur pasar belum layak.\n'
                            '{"COIN":"BTCUSDT","ARAH":"HOLD","ENTRY":null,"TP":null,"SL":null}')
                    else:
                        reference = 'c' * 64
                        value['operation'] = 'pending-review-v1:' + reference[:32] + ':1'
                        value['context']['pending_review'] = {'intent_ref': reference, 'symbol': 'BTCUSDT',
                            'entry_created_at': datetime.fromtimestamp(NOW - 18001, timezone.utc).isoformat()}
                        output = {'symbol': 'BTCUSDT', 'order_ref': reference, 'decision': 'TAHAN'}
                        answer = '{"COIN":"BTCUSDT","KEPUTUSAN":"TAHAN"}'
                module.exact_json.return_value = output
                class RenderedBridge(Bridge):
                    def _turns(self):
                        return [dict(row, role={'native_user_role': 'user',
                            'native_assistant_role': 'assistant'}[row['role']]) for row in super()._turns()]
                    def ask(self, request, *args, **kwargs):
                        projected = copy.deepcopy(request)
                        projected['prompt'] = module.user_prompt(request)
                        return super().ask(projected, *args, **kwargs)
                bridge = RenderedBridge(initial=answer)
                class Room:
                    store = SimpleNamespace(root=root)
                    conversation_id = 'neurobro:existing-room'
                    @contextmanager
                    def operation(self, request):yield self
                def response(request, action, question=''):
                    return {'schema_version': 1, 'ok': True,
                        **{key: request[key] for key in ('request_id', 'conversation_id', 'phase', 'reply_sha256')},
                        'authority': 'DIALOGUE_CONTROL_ONLY', 'trading_authority': False,
                        'decision': {'action': action, 'symbols': ['BTCUSDT'] if action == 'SCREENING' else [],
                            'question': question}, 'needs_followup': False, 'reason': 'DIALOGUE_DECISION'}
                client = Mock(compose=Mock(side_effect=lambda request: response(request, 'PROMPT', request['reply'])),
                    request=Mock(side_effect=lambda request: response(request,
                        {'SCREENING': 'SCREENING', 'ANALYSIS': 'HOLD', 'PENDING_REVIEW': 'TAHAN'}[phase])))
                importer = hook_module.importlib.import_module
                def native_module(name):return module if name == type(bridge).__module__ else importer(name)
                with patch.dict('os.environ', {'HARUN_AGENT_DIALOGUE_SOCKET': '/offline/dialogue.sock',
                        'HARUN_AGENT_DIALOGUE_TOKEN_FILE': '/offline/dialogue.token'}), \
                        patch('agent.native_browser_hook.importlib.import_module', side_effect=native_module), \
                        patch('agent.dialogue_client.DialogueClient', return_value=client), \
                        patch('agent.native_daily_room.DailyRoom', return_value=Room()), \
                        patch('agent.native_turn_batch.install_native_turn_batch', return_value=Mock()):
                    installed = install_native_bridge(bridge, archive_root=root)
                installed.clock = bridge.clock; installed.wall_clock = lambda: NOW
                installed.converter = convert_native_market_context
                evidence = [] if phase == 'SCREENING' else [{'source': SOURCE, 'symbol': 'BTCUSDT',
                    'timeframe': timeframe, 'sha256': char * 64,
                    'captured_at': datetime.fromtimestamp(NOW, timezone.utc).isoformat()}
                    for timeframe, char in (('1h', 'a'), ('15m', 'b'))]
                before = copy.deepcopy(value)
                result = installed.ask(value, lambda: True, deadline=80, chart_evidence=evidence)
                self.assertEqual(result['output'], output); self.assertEqual(value, before)
                sent = bridge.messages[0]['text']
                self.assertTrue(sent.endswith(FINAL_JSON_REQUEST_ENDING)); self.assertEqual(sent.count(value['marker']), 1)
                self.assertEqual(installed.prompt_cache.read(value)['rendered_prompt'], sent)
                self.assertEqual(bridge.clicks, 1); client.compose.assert_called_once()
                if phase == 'ANALYSIS':client.request.assert_not_called()  # Clear market HOLD is already final.
                else:client.request.assert_called_once()

    def test_screening_and_fresh_analysis_followups_archive_the_exact_new_text(self):
        from tests.test_native_browser_hook import NativeHookTests, NativeFreshFollowupTests, request
        screening = NativeHookTests(); self.addCleanup(screening.doCleanups)
        screening.setup_hook(); value = request(); before = copy.deepcopy(value)
        screening.hook.ask(value, screening.gate, deadline=80)
        sent = screening.bridge.messages[2]['text']
        self.assertTrue(sent.endswith(FINAL_JSON_REQUEST_ENDING))
        self.assertIn('"SYMBOLS"', sent); self.assertEqual(value, before)
        analysis = NativeFreshFollowupTests(); self.addCleanup(analysis.doCleanups); analysis.harness()
        before = copy.deepcopy(analysis.value); result = analysis.ask()
        sent = analysis.bridge.messages[2]['text']
        intent = next(value for stage, _, value in analysis.events if stage == 'INTENT')
        self.assertTrue(sent.endswith(FINAL_JSON_REQUEST_ENDING)); self.assertEqual(sent.count(analysis.value['marker']), 1)
        self.assertEqual(intent['followup_prompt_sha256'], hashlib.sha256(sent.encode()).hexdigest())
        self.assertEqual(analysis.value, before); self.assertEqual(result['output']['side'], 'HOLD')

    def test_old_received_screening_question_stays_answered_without_another_paid_review_or_send(self):
        from tests.test_semantic_screening import SemanticScreeningHookTests, receipt
        from tests.test_native_browser_hook import OutcomeUnknown
        case = SemanticScreeningHookTests(); self.addCleanup(case.doCleanups)
        reply = 'Belum ada pilihan final antara FILUSDT dan ATOMUSDT.'
        fixture, value = case.setup_hook(reply); fixture.bridge.final = reply
        fixture.reasoner.request.side_effect = lambda request: receipt(request, [], action='CLARIFY',
            question='Coin mana yang menjadi pilihan akhir?')
        # Produce authentic immutable cache/reviews under the previous public
        # rendering. The new code must use those exact bytes after a restart.
        with patch('agent.native_browser_hook.final_json_prompt', side_effect=lambda payload, text: text):
            with self.assertRaises(OutcomeUnknown):fixture.hook.ask(value, lambda: True, deadline=80)
        rows = fixture.bridge._turns(); root = fixture.hook.daily_room.store.root
        files = {path.name: path.read_bytes() for path in root.iterdir()}
        calls = fixture.reasoner.request.call_count; clicks = fixture.bridge.clicks
        self.assertFalse(any(FINAL_JSON_REQUEST_ENDING in row['text'] for row in rows if row['role'] == 'user'))
        seed = dict(rows=copy.deepcopy(rows), turn_names=[[] for _ in range(len(rows)//2)],
            prompt=rows[0]['text'], followup_intents=[], bound_charts=None)
        fixture.bridge.operation_deadline = 80; fixture.bridge._operation_contract = fixture.bridge._dom_contract
        with self.assertRaisesRegex(ValueError, 'NEUROBRO_CLARIFICATION_NO_PROGRESS'):
            fixture.hook._ask(value, lambda: True, deadline=80, _received=seed)
        self.assertEqual(fixture.reasoner.request.call_count, calls); self.assertEqual(fixture.bridge.clicks, clicks)
        self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, files)

    def test_new_pending_followup_keeps_original_order_binding_and_public_json(self):
        from tests.test_native_browser_hook import NativeFreshFollowupTests
        fixture = NativeFreshFollowupTests(); self.addCleanup(fixture.doCleanups); fixture.harness()
        reference = 'c' * 64
        fixture.value.update(operation='pending-review-v1:' + reference[:32] + ':1',
            output_schema=copy.deepcopy(REVIEW_SCHEMA))
        pending = {'intent_ref': reference, 'symbol': 'BTCUSDT',
            'entry_created_at': datetime.fromtimestamp(fixture.now - 18001, timezone.utc).isoformat()}
        fixture.value['context']['pending_review'] = copy.deepcopy(pending)
        fixture.fresh['context']['pending_review'] = copy.deepcopy(pending)
        fixture.bridge.final = 'Keputusan akhir BTCUSDT: TAHAN.'
        output = {'symbol': 'BTCUSDT', 'order_ref': reference, 'decision': 'TAHAN'}
        fixture.module.exact_json.side_effect = lambda text, marker: output if text == fixture.bridge.final else None
        original = fixture.reasoner.request.side_effect
        def review(request):
            response = original(request)
            if request['reply'] == fixture.bridge.final:
                response['decision'].update(action='TAHAN', symbols=[], question='')
                response['needs_followup'] = False
            return response
        fixture.reasoner.request.side_effect = review
        before = copy.deepcopy(fixture.value); result = fixture.ask()
        sent = fixture.bridge.messages[2]['text']
        intent = next(value for stage, _, value in fixture.events if stage == 'INTENT')
        self.assertEqual(result['output'], output); self.assertEqual(fixture.value, before)
        self.assertTrue(sent.endswith(FINAL_JSON_REQUEST_ENDING)); self.assertIn('"KEPUTUSAN"', sent)
        self.assertEqual(sent.count(fixture.value['marker']), 1)
        self.assertEqual(intent['operation'], before['operation'])
        self.assertEqual(intent['followup_prompt_sha256'], hashlib.sha256(sent.encode()).hexdigest())


if __name__ == '__main__':unittest.main()
