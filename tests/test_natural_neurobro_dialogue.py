import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace, ModuleType
import unittest
import textwrap
from contextlib import contextmanager
from unittest.mock import Mock

from agent import dialogue_service as service
from agent.native_prompt_composition import PromptCompositionCache
from agent.neurobro_reply import (ReplyIncomplete, parse_analysis_reply,
    parse_pending_reply, parse_screening_reply, parse_reply_for_payload)


class PerCoinReplyTests(unittest.TestCase):
    def test_link_long_and_aave_hold_are_separate_current_decisions(self):
        reply = ('Ringkasan akhir\nCOIN: LINKUSDT\nARAH: LONG\nENTRY: 17.25\n'
            'TP: 17.70\nSL: 17.00\n\nCOIN: AAVEUSDT\nARAH: HOLD\n'
            'Struktur belum jelas; tunggu konfirmasi pasar.')
        link = parse_analysis_reply(reply, 'LINKUSDT')
        self.assertEqual((link['side'], link['limit_entry'], link['take_profit']),
            ('LONG', '17.25', '17.70'))
        self.assertEqual(link['risk_reward'], 1.8)
        aave = parse_analysis_reply(reply, 'AAVEUSDT')
        self.assertEqual(aave['side'], 'HOLD')
        self.assertTrue(all(aave[key] is None for key in
            ('entry_price', 'limit_entry', 'take_profit', 'stop_loss', 'risk_reward')))

    def test_explicit_final_summary_supersedes_old_card_without_old_levels(self):
        reply = ('COIN: LINKUSDT\nARAH: HOLD\nKartu lama.\n\n'
            '### Ringkasan akhir\nCOIN: LINKUSDT\nARAH: SHORT\n'
            'ENTRY: 17.25\nTP: 16.80\nSL: 17.50')
        result = parse_analysis_reply(reply, 'LINKUSDT')
        self.assertEqual((result['side'], result['take_profit']), ('SHORT', '16.80'))
        with self.assertRaises(ReplyIncomplete):
            parse_analysis_reply(reply.replace('### Ringkasan akhir', 'Catatan berikutnya'), 'LINKUSDT')

    def test_markdown_coin_headings_keep_link_long_and_aave_hold_separate(self):
        reply = ('### LINK/USDT — LONG\nENTRY: 17.25\nTP: 17.70\nSL: 17.00\n'
            '### AAVEUSDT — HOLD\nStruktur pasar belum mendukung entry.')
        self.assertEqual(parse_analysis_reply(reply, 'LINKUSDT')['side'], 'LONG')
        self.assertEqual(parse_analysis_reply(reply, 'AAVEUSDT')['side'], 'HOLD')

    def test_same_coin_final_answer_does_not_need_to_repeat_its_header(self):
        reply = ('COIN: LINKUSDT\nARAH: HOLD\nSkenario lama jika struktur berubah.\n'
            'Keputusan akhir: LONG\nENTRY: 17.25\nTP: 17.70\nSL: 17.00')
        self.assertEqual(parse_analysis_reply(reply, 'LINKUSDT')['side'], 'LONG')

    def test_unreadable_input_hold_is_not_a_market_hold(self):
        for reason in ('Screenshot belum terbaca.', 'Saya tidak menerima lampiran chart.',
                       'Data CSV kurang lengkap.'):
            with self.subTest(reason=reason), self.assertRaisesRegex(ReplyIncomplete, 'INPUT_UNREADABLE'):
                parse_analysis_reply('COIN: LINKUSDT\nARAH: HOLD\n' + reason, 'LINKUSDT')

    def test_conditional_ranges_multiple_targets_and_foreign_coin_need_clarification(self):
        base = 'COIN: LINKUSDT\nARAH: LONG\nENTRY: 17.25\nTP: 17.70\nSL: 17.00'
        replies = (base + '\nJika breakout valid, skenario ini berlaku.',
            base.replace('17.25', '17.25-17.30'), base + '\nTP: 18.00',
            base.replace('LINKUSDT', 'AAVEUSDT'))
        for reply in replies:
            with self.subTest(reply=reply), self.assertRaises(ReplyIncomplete):
                parse_analysis_reply(reply, 'LINKUSDT')

    def test_decimal_comma_is_read_only_when_unambiguous(self):
        result = parse_analysis_reply('COIN: LINKUSDT\nARAH: LONG\nENTRY: 17,25\n'
            'TP: 17,70\nSL: 17,00', 'LINKUSDT')
        self.assertEqual(result['limit_entry'], '17.25')
        with self.assertRaisesRegex(ReplyIncomplete, 'LEVEL_AMBIGUOUS'):
            parse_analysis_reply('COIN: LINKUSDT\nARAH: LONG\nENTRY: 1,234\n'
                'TP: 2\nSL: 1', 'LINKUSDT')

    def test_explicit_usdt_price_units_do_not_require_a_programming_format(self):
        result = parse_analysis_reply('COIN: LINK/USDT\nARAH: LONG\nENTRY: 17.25 USDT\n'
            'TP: 17.70USDT\nSL: $17.00', 'LINKUSDT')
        self.assertEqual(result['limit_entry'], '17.25')

    def test_screening_reads_recommendations_not_comparisons(self):
        result = parse_screening_reply('Bandingkan dengan BTCUSDT.\nRingkasan akhir\n'
            'COIN: LINKUSDT, AAVEUSDT')
        self.assertEqual(result, {'symbols': ['LINKUSDT', 'AAVEUSDT']})
        for reply in ('COIN: LINKUSDT atau AAVEUSDT', 'LINKUSDT dibanding BTCUSDT'):
            with self.assertRaises(ReplyIncomplete): parse_screening_reply(reply)

    def test_screening_count_comes_from_actual_requested_capacity(self):
        payload = {'output_schema': {'required':['symbols'], 'properties': {
            'symbols':{'minItems':1,'maxItems':1}}}}
        self.assertEqual(parse_reply_for_payload('COIN: LINKUSDT', payload), {'symbols':['LINKUSDT']})
        with self.assertRaisesRegex(ReplyIncomplete, 'COUNT_MISMATCH'):
            parse_reply_for_payload('COIN: LINKUSDT, AAVEUSDT', payload)

    def test_pending_decision_is_scoped_and_cannot_be_conditional(self):
        reply = ('COIN: LINKUSDT\nKEPUTUSAN: TAHAN\nStruktur order masih valid.\n'
            'COIN: AAVEUSDT\nKEPUTUSAN: BATALKAN\nSetup invalid.')
        self.assertEqual(parse_pending_reply(reply, 'LINKUSDT'), 'TAHAN')
        self.assertEqual(parse_pending_reply(reply, 'AAVEUSDT'), 'BATALKAN')
        with self.assertRaises(ReplyIncomplete):
            parse_pending_reply('COIN: LINKUSDT\nKEPUTUSAN: BATALKAN jika gagal reclaim.', 'LINKUSDT')

    def test_native_numeric_wire_roundtrip_reaches_actual_worker_setup_without_price_change(self):
        from decimal import Decimal
        from worker.neuroapi import SETUP_SCHEMA, setup
        reply = 'COIN: LINKUSDT\nARAH: LONG\nENTRY: 17.25\nTP: 17.70\nSL: 17.00'
        payload = {'output_schema': SETUP_SCHEMA, 'context': {'symbol': 'LINKUSDT'}}
        projected = parse_reply_for_payload(reply, payload)
        self.assertNotIsInstance(projected['limit_entry'], str)
        native_wire = json.dumps(projected, allow_nan=False)
        worker_output = json.loads(native_wire, parse_float=Decimal)
        signal = setup(worker_output, 'LINKUSDT', require_declared_rr=False, require_gross_rr=False)
        self.assertEqual((signal.entry, signal.tp, signal.sl),
            (Decimal('17.25'), Decimal('17.70'), Decimal('17.00')))
        bad = reply.replace('17.25', '17.123456789123456789')
        with self.assertRaisesRegex(ReplyIncomplete, 'PRECISION_UNSUPPORTED'):
            parse_reply_for_payload(bad, payload)

    def test_pending_internal_order_binding_has_no_artificial_review_count_cap(self):
        reference = 'a' * 64
        payload = {'output_schema': {'required':['symbol','order_ref','decision']},
            'operation':'pending-review-v1:' + reference[:32] + ':10000000000',
            'context': {'symbol':'LINKUSDT','pending_review':{'symbol':'LINKUSDT','intent_ref':reference}}}
        projected = parse_reply_for_payload('COIN: LINKUSDT\nKEPUTUSAN: TAHAN', payload)
        self.assertEqual(projected, {'symbol':'LINKUSDT','order_ref':reference,'decision':'TAHAN'})
        payload['operation'] = 'pending-review-v1:' + 'b' * 32 + ':1'
        with self.assertRaisesRegex(ReplyIncomplete, 'ORDER_UNVERIFIED'):
            parse_reply_for_payload('COIN: LINKUSDT\nKEPUTUSAN: BATALKAN', payload)


class SourcedConversationTests(unittest.TestCase):
    def request(self):
        return {'phase': 'ANALYSIS', 'reply': 'LINKUSDT ENTRY 17.25, SL 17.00 dan TP 17.70.',
            'market_context': {'symbol': 'LINKUSDT', 'mark_price': '17.31'}}

    def test_natural_multiline_question_can_reference_real_levels_and_timeframes(self):
        question = ('Di Binance 15M harga 17.31, sedangkan jawabanmu ENTRY: 17.25.\n'
            'Apakah struktur 1H masih mendukung SL 17.00 dan TP 17.70?')
        decision = {'action': 'CLARIFY', 'symbols': ['LINKUSDT'], 'question': question}
        self.assertIs(service.validate_decision(decision, self.request()), decision)
        with self.assertRaisesRegex(service.DialogueError, 'PRICE_PROPOSAL_REFUSED'):
            service.validate_decision(dict(decision, question='Apakah ENTRY: 999 tepat?'), self.request())

    def test_risk_manager_facts_are_hash_bound_and_not_model_assertions(self):
        request = self.request()
        values = {'symbol': 'LINKUSDT', 'execution_tp': '17.85', 'risk_usdt': '3'}
        fact = {'producer': 'RISK_MANAGER', 'operation': 'owned:analysis:one', 'values': values,
            'sha256': hashlib.sha256(service.canonical(values).encode()).hexdigest()}
        self.assertEqual(service.validate_source_facts([fact]), [fact])
        request['source_facts'] = [fact]
        service.validate_decision({'action': 'CLARIFY', 'symbols': [],
            'question': 'Risk Manager menghitung TP 17.85 dari risiko 3; bagaimana struktur 1H?'}, request)
        changed = copy.deepcopy(fact); changed['values']['execution_tp'] = '99'
        with self.assertRaisesRegex(service.DialogueError, 'SOURCE_FACTS_INVALID'):
            service.validate_source_facts([changed])
        changed = dict(fact, producer='MODEL_SAYS_SO')
        with self.assertRaises(service.DialogueError): service.validate_source_facts([changed])

    def test_model_cannot_select_other_coin_or_ask_neurobro_for_quantity(self):
        with self.assertRaisesRegex(service.DialogueError, 'FOREIGN_SYMBOL'):
            service.validate_decision({'action': 'COMPLETE', 'symbols': ['AAVEUSDT'],
                'question': ''}, self.request())
        request = dict(self.request(), phase='COMPOSE_ANALYSIS')
        with self.assertRaises(service.DialogueError):
            service.validate_decision({'action': 'PROMPT', 'symbols': [],
                'question': 'Tolong tentukan ukuran posisi LINKUSDT.'}, request)


class PromptRenderReceiptTests(unittest.TestCase):
    def test_completed_draft_is_byte_stable_after_restart_and_has_no_json_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            original = lambda payload: payload['prompt'] + '\n\nOUTPUT_JSON_SCHEMA:\n' + json.dumps(payload['output_schema']) + '\n' + payload['marker']
            native = SimpleNamespace(user_prompt=original)
            payload = {'operation': 'owned:screening:one', 'prompt': 'Referensi workflow lama.',
                'marker': 'HARUN_' + 'a' * 32, 'output_schema': {'required': ['symbols']}}
            before = copy.deepcopy(payload)
            cache = PromptCompositionCache(native, root)
            rendered = cache.store(payload, 'Pilih coin futures yang paling jelas saat ini.', 'b' * 64)
            self.assertNotIn('OUTPUT_JSON_SCHEMA', rendered)
            self.assertNotIn('Referensi workflow lama.', rendered)
            self.assertEqual(rendered.count(payload['marker']), 1)
            self.assertEqual(native.user_prompt(payload), rendered)
            restarted = SimpleNamespace(user_prompt=original)
            PromptCompositionCache(restarted, root)
            self.assertEqual(restarted.user_prompt(payload), rendered)
            self.assertEqual(payload, before)
            self.assertEqual(next(root.glob('mini-prompt-*.json')).stat().st_mode & 0o777, 0o600)

    def test_altered_receipt_is_rejected_without_another_draft_or_send(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            native = SimpleNamespace(user_prompt=lambda value: value['prompt'])
            payload = {'operation': 'owned:one', 'prompt': 'Task.', 'marker': 'HARUN_' + 'a' * 32,
                'output_schema': {'required': ['symbols']}}
            cache = PromptCompositionCache(native, root)
            cache.store(payload, 'Pilih coin.', 'b' * 64)
            path = next(root.glob('mini-prompt-*.json'))
            value = json.loads(path.read_text()); value['rendered_prompt'] = 'Changed.'
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'RECEIPT_UNVERIFIED'): native.user_prompt(payload)


class ComposedPromptServiceTests(unittest.TestCase):
    def test_compose_is_real_model_work_memoized_across_restart_and_new_transport_deadline(self):
        from agent.llm import OpenAIDialogue
        from agent.memory import Memory
        from tests.test_agent_dialogue_service import request
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); key = root / 'key'; key.write_text('offline-key-012345678901234567890')
            key.chmod(0o600); memory = Memory(root / 'agent.sqlite3'); calls = []
            def transport(payload):
                calls.append(payload)
                return {'status': 'completed', 'output': [{'type': 'message', 'content': [
                    {'type': 'output_text', 'text': json.dumps({'action': 'PROMPT', 'symbols': [],
                     'question': 'Pilih 2 coin Binance Futures yang struktur 15M dan 1H paling jelas.'})}]}]}
            model = OpenAIDialogue(key, memory, transport=transport)
            engine = service.DialogueEngine(memory, key, model=model)
            value = request(phase='SCREENING', reply='Screening 2 coin dari Binance Futures.')
            value['phase'] = 'COMPOSE_SCREENING'
            first = engine.process(value)
            self.assertEqual(first['decision']['action'], 'PROMPT')
            self.assertFalse(first['needs_followup'])
            self.assertIn('COMPOSE', calls[0]['instructions'])
            value['deadline_epoch'] += 2
            self.assertEqual(engine.process(value), first)
            memory.close(); memory = Memory(root / 'agent.sqlite3')
            engine = service.DialogueEngine(memory, key,
                model=OpenAIDialogue(key, memory, transport=transport))
            value['deadline_epoch'] += 2
            self.assertEqual(engine.process(value), first)
            self.assertEqual(len(calls), 1)
            off = copy.deepcopy(value); off['gate']['robot_on'] = False
            with self.assertRaisesRegex(service.DialogueError, 'ROBOT_OFF'): engine.process(off)
            self.assertEqual(len(calls), 1); memory.close()

    def test_native_hook_uses_completed_mini_prompt_and_restores_same_render_for_receipts(self):
        from tests.test_native_browser_hook import Bridge, native, request, client
        from agent.native_browser_hook import DelegatedDOMBridge
        class Room:
            conversation_id = 'neurobro:existing-room'
            def __init__(self, root): self.store = SimpleNamespace(root=root)
            @contextmanager
            def operation(self, payload): yield self
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            module = native()
            module.user_prompt = lambda p: p['prompt'] + '\n\nOUTPUT_JSON_SCHEMA:\n{}\n' + p['marker']
            class ComposedBridge(Bridge):
                def ask(self, payload, *args, **kwargs):
                    copied = copy.deepcopy(payload); copied['prompt'] = module.user_prompt(payload)
                    return super().ask(copied, *args, **kwargs)
            bridge = ComposedBridge(initial='COIN: BTCUSDT', final='COIN: BTCUSDT')
            reasoner = client('SCREENING')
            def compose(value):
                return {'schema_version': 1, 'ok': True,
                    **{key: value[key] for key in ('request_id','conversation_id','phase','reply_sha256')},
                    'authority': 'DIALOGUE_CONTROL_ONLY', 'trading_authority': False,
                    'decision': {'action':'PROMPT','symbols':[], 'question':'Pilih coin yang paling jelas saat ini.'},
                    'needs_followup': False, 'reason':'DIALOGUE_DECISION'}
            reasoner.compose = Mock(side_effect=compose)
            hook = DelegatedDOMBridge(bridge, reasoner, native=module,
                monotonic=bridge.clock, wall_clock=time.time, daily_room=Room(root), compose_enabled=True)
            value = request(); before = copy.deepcopy(value)
            result = hook.ask(value, lambda: True, deadline=80)
            self.assertEqual(result['output'], {'symbols':['BTCUSDT']})
            sent = bridge.messages[0]['text']
            self.assertTrue(sent.startswith('Pilih coin yang paling jelas'))
            self.assertNotIn('OUTPUT_JSON_SCHEMA', sent)
            self.assertEqual(module.user_prompt(value), sent)
            self.assertEqual(module.user_prompt(value), sent)
            reasoner.compose.assert_called_once()
            self.assertEqual(value, before)
            self.assertEqual(bridge.clicks, 1)


class CurrentMaterialConversationTests(unittest.TestCase):
    def fixture(self):
        from tests.test_native_browser_hook import NativeFreshFollowupTests
        fixture = NativeFreshFollowupTests(
            'test_fresh_400_data_and_two_attachments_are_sent_once_with_native_final_identity')
        fixture.harness()
        return fixture

    def test_a_known_late_reply_refreshes_data_in_same_chat_before_model_or_acceptance(self):
        from datetime import datetime, timezone
        from tests.test_agent_closed_candles_200 import native_context_200
        fixture = self.fixture()
        original = fixture.bridge.after_click
        def after_click(bridge):
            original(bridge)
            if bridge.clicks == 1:
                fixture.now += 200  # Separate controlled-clock test; no production time change.
                fixture.fresh['context'] = native_context_200(fixture.now)
                for row in fixture.fresh['chart_evidence']:
                    row['captured_at'] = datetime.fromtimestamp(fixture.now, timezone.utc).isoformat()
        fixture.bridge.after_click = after_click
        result = fixture.ask()
        self.assertEqual(fixture.bridge.clicks, 2)
        self.assertTrue(fixture.bridge.messages[2]['text'].startswith('Materi pasar sebelumnya'))
        self.assertEqual(fixture.reasoner.request.call_count, 1)
        fixture.refresh.assert_called_once()
        self.assertEqual(result['assistant_text'], fixture.bridge.final)
        self.assertEqual(result['output']['side'], 'HOLD')

    def test_natural_complete_summary_can_receive_relevant_clarification_with_fresh_charts(self):
        fixture = self.fixture()
        fixture.bridge.initial = 'COIN: BTCUSDT\nARAH: LONG\nENTRY: 100\nTP: 102\nSL: 99'
        fixture.bridge.final = 'COIN: BTCUSDT\nARAH: HOLD\nMomentum pasar belum jelas.'
        fixture.module.exact_json.side_effect = lambda text, marker: None
        def reason(value):
            clarify = value['reply'] == fixture.bridge.initial
            return {'schema_version':1,'ok':True,
                **{key:value[key] for key in ('request_id','conversation_id','phase','reply_sha256')},
                'authority':'DIALOGUE_CONTROL_ONLY','trading_authority':False,
                'decision':{'action':'CLARIFY' if clarify else 'HOLD','symbols':['BTCUSDT'],
                    'question':'Pada 15M ENTRY 100, apakah wick membatalkan struktur ini?' if clarify else ''},
                'needs_followup':clarify,'reason':'DIALOGUE_DECISION'}
        fixture.hook.client = Mock(request=Mock(side_effect=reason))
        result = fixture.ask()
        self.assertEqual(result['output']['side'], 'HOLD')
        self.assertEqual(fixture.bridge.clicks, 2)
        fixture.refresh.assert_called_once()
        self.assertNotIn('OUTPUT_JSON_SCHEMA', fixture.bridge.messages[2]['text'])


class ActualNativeParserBoundaryTests(unittest.TestCase):
    def run_bridge(self, *, tamper=False):
        from tests.test_native_browser_hook import Bridge, native, request, client, OutcomeUnknown
        from agent.native_browser_hook import DelegatedDOMBridge
        module = native()
        original = Mock(side_effect=ValueError('Natural answer is not marker-bound JSON'))
        module.exact_json = original
        class ParserBoundaryBridge(Bridge):
            def ask(self, payload, *args, **kwargs):
                result = super().ask(payload, *args, **kwargs)
                try:
                    # This is the real f017 return boundary: exact_json runs
                    # before control returns to DelegatedDOMBridge._ask.
                    text = result['assistant_text'] + (' changed' if tamper else '')
                    return dict(result, output=module.exact_json(text, payload['marker']))
                except Exception:
                    raise OutcomeUnknown() from None
        bridge = ParserBoundaryBridge(initial='COIN: BTCUSDT')
        hook = DelegatedDOMBridge(bridge, client('SCREENING'), native=module,
            monotonic=bridge.clock, wall_clock=time.time)
        if tamper:
            with self.assertRaises(OutcomeUnknown): hook.ask(request(), lambda: True, deadline=80)
            self.assertIs(module.exact_json, original)
            return
        result = hook.ask(request(), lambda: True, deadline=80)
        self.assertEqual(result['output'], {'symbols': ['BTCUSDT']})
        self.assertEqual(result['assistant_text'], 'COIN: BTCUSDT')
        self.assertIs(module.exact_json, original)
        self.assertEqual(bridge.clicks, 1)

    def test_accepted_natural_reply_projects_at_actual_native_return_boundary(self):
        self.run_bridge()

    def test_changed_raw_reply_cannot_reuse_accepted_projection(self):
        self.run_bridge(tamper=True)

    def test_cached_actual_f017_ask_method_accepts_natural_reply_without_json_forgery(self):
        from tests.test_native_browser_hook import Bridge, request, client, NotSubmitted, OutcomeUnknown
        from agent.native_browser_hook import DelegatedDOMBridge
        source = Path('/workspace/evidence/native-f017-ask-current.txt')
        if not source.is_file():
            self.skipTest('Exact admitted VPS method is private; portable boundary tests run above')
        module = ModuleType('local_admitted_f017_fixture')
        module.NotSubmitted, module.OutcomeUnknown = NotSubmitted, OutcomeUnknown
        module.hashlib, module.time = hashlib, time
        module.image_artifacts = Mock(return_value=[])
        module.request_kind = lambda *args: 'SCREENING'
        module.user_prompt = lambda payload: payload['prompt']
        module.exact_json = Mock(side_effect=ValueError('No JSON in natural author reply'))
        module.MAX_ANSWER = 65536
        original = module.exact_json
        exec(compile(textwrap.dedent(source.read_text()), '<admitted-f017-ask>', 'exec'), module.__dict__)
        class ActualBridge(Bridge): pass
        ActualBridge.ask = module.ask
        bridge = ActualBridge(initial='COIN: BTCUSDT')
        hook = DelegatedDOMBridge(bridge, client('SCREENING'), native=module,
            monotonic=bridge.clock, wall_clock=time.time)
        result = hook.ask(request(), lambda: True, deadline=80)
        self.assertEqual(result['assistant_text'], 'COIN: BTCUSDT')
        self.assertEqual(result['output'], {'symbols': ['BTCUSDT']})
        self.assertTrue(result['response_id'].startswith('dom-'))
        self.assertIs(module.exact_json, original)


if __name__ == '__main__': unittest.main()
