import ast
import copy
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent import native_browser_hook as helper
from worker import core


NOW = 1793231000.0


def context():
    return {'symbol': 'ZECUSDT', 'contract_rules': {
        'source': 'Binance Futures /fapi/v1/exchangeInfo', 'symbol': 'ZECUSDT',
        'tickSize': '0.01', 'observed_at': NOW}, 'risk_constraints': {
        'fee_symbol': 'ZECUSDT', 'fee_source': core.FEE_SOURCE,
        'risk_model': core.RISK_MODEL, 'reward_risk_policy': core.NORMALIZED_REWARD_RISK_POLICY,
        'fee_observed_at': NOW, 'entry_fee_rate': '0.0005', 'sl_exit_fee_rate': '0.0005',
        'tp_exit_fee_rate': '0.0005', 'exit_slippage_rate': '0.005'}}


def output(side='LONG'):
    return {'symbol': 'ZECUSDT', 'side': side, 'limit_entry': 1280,
        'take_profit': 1328 if side == 'LONG' else 1232,
        'stop_loss': 1260 if side == 'LONG' else 1300}


class ProductionTPProjection(unittest.TestCase):
    def test_packaged_source_is_verbatim_from_pinned_core_without_io_definitions(self):
        raw = (Path(__file__).resolve().parents[1]/'worker/core.py').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), helper._NATIVE_TP_CORE_SHA256)
        self.assertEqual(hashlib.sha256(helper._NATIVE_TP_SOURCE.encode()).hexdigest(),
            helper._NATIVE_TP_SOURCE_SHA256)
        source = raw.decode()
        originals = {n.name if isinstance(n, (ast.FunctionDef, ast.ClassDef)) else n.targets[0].id:
            ast.get_source_segment(source, n)
            for n in ast.parse(source).body
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)) or isinstance(n, ast.Assign)
                and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)}
        packaged = ast.parse(helper._NATIVE_TP_SOURCE)
        for node in packaged.body:
            name = node.name if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else node.targets[0].id
            self.assertEqual(ast.get_source_segment(helper._NATIVE_TP_SOURCE, node), originals[name])
        self.assertEqual({n.name for n in packaged.body if isinstance(n, ast.ClassDef)}, {'Review'})
        self.assertFalse(any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in packaged.body))

    def test_real_historical_long_asks_for_risk_managers_exact_tp_without_mutation(self):
        setup, facts = output(), context()
        before = copy.deepcopy((setup, facts))
        question = helper._neurobro_tp_confirmation(setup, facts, now=NOW)
        self.assertIn('TP eksekusi 1343.16', question)
        self.assertIn('TP 1328', question)
        self.assertIn('belum diubah atau diotorisasi', question)
        self.assertIn('HOLD', question)
        self.assertEqual((setup, facts), before)

    def test_short_uses_original_production_formula_and_matching_answer_needs_no_turn(self):
        facts = context()
        rules = SimpleNamespace(tick=D('0.01'), taker_fee_rate=D('0.0005'),
            fee_symbol='ZECUSDT', fee_source=core.FEE_SOURCE, fee_observed_at=NOW)
        expected = core.normalized_reward_risk_tp(D('1280'), D('1300'), 'SHORT', rules,
            symbol='ZECUSDT', check_fresh=False)
        setup = output('SHORT')
        self.assertIn('TP eksekusi '+format(expected, 'f'),
            helper._neurobro_tp_confirmation(setup, facts, now=NOW))
        setup['take_profit'] = expected
        self.assertIsNone(helper._neurobro_tp_confirmation(setup, facts, now=NOW))

    def test_dom_json_float_is_read_by_its_decimal_text_and_zero_account_fee_is_valid(self):
        setup, facts = output(), context()
        setup.update(limit_entry=1280.0, take_profit=1328.0, stop_loss=1260.0)
        for key in ('entry_fee_rate', 'sl_exit_fee_rate', 'tp_exit_fee_rate'):
            facts['risk_constraints'][key] = '0'
        question = helper._neurobro_tp_confirmation(setup, facts, now=NOW)
        self.assertIn('fee akun 0 ', question)
        self.assertIn('Konfirmasi TP final', question)

    def test_hold_and_incomplete_direction_do_not_request_a_risk_clarification(self):
        for value in ({'side': 'HOLD'}, {}, None):
            self.assertIsNone(helper._neurobro_tp_confirmation(value, None, now=NOW))

    def test_immutable_risk_snapshot_does_not_add_a_conversation_expiry(self):
        facts = context()
        facts['risk_constraints']['fee_observed_at'] = NOW-1000
        facts['contract_rules']['observed_at'] = NOW-1000
        self.assertIn('TP eksekusi 1343.16',
            helper._neurobro_tp_confirmation(output(), facts, now=NOW))

    def test_bad_risk_evidence_cannot_authorize_directional_tp(self):
        for mutate in (
            lambda c: c['risk_constraints'].pop('entry_fee_rate'),
            lambda c: c['risk_constraints'].update(fee_source='ASSUMED_FEE'),
            lambda c: c['risk_constraints'].update(fee_observed_at=NOW+1),
            lambda c: c['contract_rules'].update(symbol='BTCUSDT'),
            lambda c: c['risk_constraints'].update(exit_slippage_rate='0.001')):
            facts = context(); mutate(facts)
            with self.assertRaises(helper.NativeTPConfirmationUnavailable):
                helper._neurobro_tp_confirmation(output(), facts, now=NOW)
        with patch.object(helper, '_NATIVE_TP_SOURCE', helper._NATIVE_TP_SOURCE+'\n'):
            with self.assertRaises(helper.NativeTPConfirmationUnavailable):
                helper._neurobro_tp_confirmation(output(), context(), now=NOW)


class NativeTPConversation(unittest.TestCase):
    """Exercise the existing native chat, fresh-chart and receipt callbacks."""
    def fixture(self, *, hold=False):
        from tests.test_native_browser_hook import NativeFreshFollowupTests, client
        from worker.analysis import analysis_context
        from worker.neuroapi import SETUP_SCHEMA
        from tests.support import Account, RobotMarket

        f = NativeFreshFollowupTests(
            'test_fresh_400_data_and_two_attachments_are_sent_once_with_native_final_identity')
        f.harness()
        facts, rules = analysis_context(RobotMarket(), 'BTCUSDT', D('5'), Account())
        # The public 400-candle fixture retains its own clock. Copy the actual
        # production contract shape, rather than inventing fee/rule fields.
        for key in ('contract_rules', 'risk_constraints'):
            f.value['context'][key] = copy.deepcopy(facts[key])
        f.value['context']['contract_rules']['observed_at'] = f.now
        f.value['context']['risk_constraints']['fee_observed_at'] = f.now
        f.value['output_schema'] = copy.deepcopy(SETUP_SCHEMA)
        first = {'symbol': 'BTCUSDT', 'side': 'LONG', 'limit_entry': 100,
            'take_profit': 104, 'stop_loss': 98, 'risk_reward': 2}
        expected = core.normalized_reward_risk_tp(D('100'), D('98'), 'LONG', rules,
            symbol='BTCUSDT')
        final = ({'symbol': 'BTCUSDT', 'side': 'HOLD', 'limit_entry': None,
            'take_profit': None, 'stop_loss': None, 'risk_reward': None} if hold else
            {**first, 'take_profit': float(expected), 'risk_reward': float((expected-D('100'))/D('2'))})
        f.bridge.initial, f.bridge.final = json.dumps(first), json.dumps(final)
        f.module.exact_json.side_effect = lambda text, marker: json.loads(text)
        # Both genuine model decisions say COMPLETE. The local required-price
        # question must not pretend that the model returned CLARIFY instead.
        f.reasoner = client('COMPLETE')
        f.hook.client = f.reasoner
        return f, first, final, expected

    def test_complete_model_decision_still_requires_neurobro_final_tp_in_same_chat(self):
        f, first, final, expected = self.fixture()
        before = copy.deepcopy(f.value)
        result = f.ask()
        self.assertEqual(result['assistant_text'], json.dumps(final))
        self.assertEqual(f.value, before)
        self.assertEqual((f.bridge.fresh_calls, f.bridge.clicks), (1, 2))
        self.assertEqual(f.bridge.messages[1]['text'], json.dumps(first))
        self.assertIn('TP eksekusi '+format(expected, 'f'), f.bridge.messages[2]['text'])
        self.assertIn('belum diubah atau diotorisasi', f.bridge.messages[2]['text'])
        self.assertEqual(f.reasoner.request.call_count, 2)
        self.assertEqual([e[0] for e in f.events], ['REFRESH', 'INTENT', 'COMPLETE'])
        self.assertEqual(f.events[1][2]['closed_candles_total'], 400)
        self.assertEqual(len(f.events[1][2]['followup_chart_evidence']), 2)
        self.assertNotIn('risk_constraints', f.fresh['context'])
        self.assertEqual(f.events[2][2]['final_reply_sha256'],
            hashlib.sha256(json.dumps(final).encode()).hexdigest())

    def test_neurobro_hold_after_price_question_is_final_without_another_turn(self):
        f, first, final, _ = self.fixture(hold=True)
        result = f.ask()
        self.assertEqual(result['assistant_text'], json.dumps(final))
        self.assertEqual((f.bridge.fresh_calls, f.bridge.clicks), (1, 2))
        self.assertEqual(f.reasoner.request.call_count, 2)
        self.assertEqual(len(f.events), 3)

    def test_initial_hold_needs_no_price_question_or_fresh_chart(self):
        f, _, final, _ = self.fixture(hold=True)
        f.bridge.initial = f.bridge.final
        result = f.ask()
        self.assertEqual(result['assistant_text'], json.dumps(final))
        self.assertEqual((f.bridge.fresh_calls, f.bridge.clicks), (1, 1))
        self.assertEqual(f.reasoner.request.call_count, 1)
        self.assertEqual(f.events, [])

    def test_unbound_hold_requires_only_same_decision_format_restatement(self):
        from tests.test_native_browser_hook import client
        f, _, final, _ = self.fixture(hold=True)
        original = ('Keputusan tetap HOLD; saya tidak mengeluarkan marker.\n```json\n'
            +json.dumps(final)+'\n```')
        bound = f.value['marker']+'\n```json\n'+json.dumps(final)+'\n```'
        f.bridge.initial, f.bridge.final = original, bound
        # The native parser cannot bind a fragment without its original marker.
        # This fixture models that contract, rather than accepting the JSON alone.
        f.module.exact_json.side_effect=lambda text,marker:(final if marker in text else None)
        f.reasoner=client('HOLD');f.hook.client=f.reasoner
        before=copy.deepcopy(f.value)
        result=f.ask()
        self.assertEqual(f.value,before)
        self.assertEqual(result['assistant_text'],bound)
        self.assertEqual((f.bridge.fresh_calls,f.bridge.clicks),(1,2))
        self.assertEqual(f.bridge.messages[1]['text'],original)
        question=f.bridge.messages[2]['text']
        self.assertIn('Pertahankan keputusan dan level',question)
        self.assertIn('Jika keputusanmu HOLD, tetap HOLD',question)
        self.assertIn(f.value['marker'],question)
        self.assertNotIn('Konfirmasi TP final',question)
        self.assertEqual(f.reasoner.request.call_count,2)
        self.assertEqual([row[0]for row in f.events],['REFRESH','INTENT','COMPLETE'])
        self.assertEqual(f.events[1][2]['closed_candles_total'],400)
        self.assertEqual(len(f.events[1][2]['followup_chart_evidence']),2)


if __name__ == '__main__':
    unittest.main()
