"""Order-intent risk regressions; no exchange transport or paper lifecycle."""
import copy
import json
import sqlite3
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from decimal import Context, ROUND_HALF_EVEN, ROUND_UP, localcontext
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from worker.core import D, Ledger, Review, Rules, Signal, day, maximum_risk_quantity, number, preflight, risk_check, risk_costs, validated_risk_target, verified_rr
from worker.neuroapi import NeuroAPI, SETUP_SCHEMA, setup
from worker.prompts import ANALYSIS
from worker.robot_provenance import VERSION, stamp, verify
from support import GOOD as PUBLIC_GOOD, RULES as PUBLIC_RULES


FEE_SOURCE = 'BINANCE_FUTURES_COMMISSION_RATE'
GOOD = {field: value for field, value in PUBLIC_GOOD.items() if field != 'position_size'}


def RULES():
    """Existing price-only examples explicitly use a fresh zero-fee account rate."""
    return replace(PUBLIC_RULES(), taker_fee_rate=D('0'), fee_observed_at=time.time(),
                   fee_symbol='BTCUSDT', fee_source=FEE_SOURCE)


class RiskTests(unittest.TestCase):
    def test_long_short_levels_are_immutable_without_provider_quantity_in_intent(self):
        for side, sl, tp in [('LONG', 98, 104), ('SHORT', 102, 96)]:
            with self.subTest(side=side):
                source = {**GOOD, 'side': side, 'stop_loss': sl, 'take_profit': tp}
                before = copy.deepcopy(source)
                plan = risk_check(setup(source, 'BTCUSDT'), RULES())
                self.assertEqual(source, before)
                self.assertEqual((D(plan['entry']), D(plan['tp']), D(plan['sl'])), (D(100), D(tp), D(sl)))
                self.assertNotIn('neurobro_position_size', plan)
                self.assertEqual(D(plan['execution_quantity']), D('2.5'))
                self.assertEqual(D(plan['risk']), D('5'))
                self.assertEqual((plan['mode'], plan['margin_mode'], plan['leverage'], plan['order_type']),
                                 ('ORDER_INTENT', 'CROSS', 75, 'LIMIT'))

    def test_largest_legal_lot_obeys_risk_cap_and_next_lot_exceeds_it(self):
        rules = replace(RULES(), tick=D('.001'), min_notional=D('0'), maximum=D('1000000'))
        for target in ('0.01', '5', '10', '100'):
            for distance in ('1.2', '3', '7.123', '12345'):
                with self.subTest(target=target, distance=distance):
                    gap = D(distance)
                    if Fraction(D(target)) / Fraction(gap) < Fraction(rules.minimum):
                        with self.assertRaisesRegex(Review, 'NO_LEGAL_MAX_RISK_QUANTITY'):
                            maximum_risk_quantity(gap * 2, gap, rules, D(target))
                        continue
                    signal = Signal('BTCUSDT', 'LONG', gap * 2, gap * 4, gap)
                    plan = risk_check(signal, rules, D(target))
                    quantity = D(plan['execution_quantity'])
                    self.assertEqual(quantity % rules.step, 0)
                    self.assertLessEqual(Fraction(quantity) * Fraction(gap), Fraction(D(target)))
                    self.assertGreater(Fraction(quantity + rules.step) * Fraction(gap), Fraction(D(target)))
                    self.assertEqual(D(plan['risk']), quantity * gap)

    def test_exact_floor_keeps_quantity_below_near_lot_boundary(self):
        distance = D('1.' + '0' * 59 + '1')
        rules = replace(RULES(), step=D('1'), min_notional=D('0'))
        self.assertEqual(maximum_risk_quantity(distance * 2, distance, rules), D('4'))

    def test_contract_maximum_can_bind_before_risk_target(self):
        rules = replace(RULES(), step=D('.01'), minimum=D('.01'), maximum=D('2.005'))
        signal = Signal('BTCUSDT', 'LONG', D('100'), D('102.40'), D('98.80'), D('999'))
        plan = risk_check(signal, rules)
        self.assertEqual(D(plan['execution_quantity']), D('2.00'))
        self.assertLessEqual(D(plan['execution_quantity']), rules.maximum)
        self.assertGreater(D(plan['execution_quantity']) + rules.step, rules.maximum)
        self.assertEqual(D(plan['risk']), D('2.40'))
        self.assertNotIn('neurobro_position_size', plan)

    def test_impossible_minimum_quantity_and_notional_fail_without_resizing_levels(self):
        signal = setup(GOOD, 'BTCUSDT')
        for rules in (replace(RULES(), minimum=D('3')), replace(RULES(), min_notional=D('300'))):
            with self.subTest(rules=rules), self.assertRaisesRegex(Review, 'NO_LEGAL_MAX_RISK_QUANTITY'):
                risk_check(signal, rules)

    def test_actual_rr_and_price_filters_are_enforced(self):
        signal = setup(GOOD, 'BTCUSDT')
        invalid = [replace(signal, sl=D('100')), replace(signal, tp=D('103.99')),
                   replace(signal, entry=D('100.001'))]
        for value in invalid:
            with self.subTest(signal=value), self.assertRaises(Review):
                risk_check(value, RULES())
        percent_rules = replace(RULES(), multiplier_up=D('1.1'), multiplier_down=D('.9'), mark_price=D('200'))
        with self.assertRaisesRegex(Review, 'REJECT_PERCENT_PRICE'):
            risk_check(signal, percent_rules)
        self.assertEqual(D(risk_check(replace(signal, tp=D('112')), RULES())['rr']), D('6'))

    def test_stale_or_future_contract_observation_fails(self):
        signal = setup(GOOD, 'BTCUSDT')
        for observed in (time.time() - 301, time.time() + 30):
            with self.subTest(observed=observed), self.assertRaises(Review):
                risk_check(signal, replace(RULES(), observed_at=observed))

    def test_risk_target_is_positive_bounded_and_explicit(self):
        for value in ('0', '-1', '100.01', '1e1', 'NaN', True, 1.5, None):
            with self.subTest(value=value), self.assertRaises(Review):
                validated_risk_target(value)
        for value in ('0.01', '5', '100', D('10')):
            self.assertEqual(validated_risk_target(value), D(str(value)))

    def test_small_decimal_contract_value_is_explicit_while_scientific_input_string_is_rejected(self):
        self.assertEqual(number(D('1E-8')), D('0.00000001'))
        with self.assertRaises(Review):
            number('1E-8')

    def test_preflight_accepts_intent_and_rejects_quantity_tampering(self):
        rules = RULES()
        plan = risk_check(setup(GOOD, 'BTCUSDT'), rules)
        before = copy.deepcopy(plan)
        preflight(plan, rules)
        self.assertEqual(plan, before)
        for quantity in ('2.501', '.002'):
            altered = {**plan, 'quantity': quantity, 'execution_quantity': quantity,
                       'risk': str(D(quantity) * D('2'))}
            with self.subTest(quantity=quantity), self.assertRaises(Review):
                preflight(altered, rules)
        with self.assertRaisesRegex(Review, 'INVALID_EXECUTION_QUANTITY'):
            preflight({**plan, 'execution_quantity': '2.4'}, rules)

    def test_preflight_bad_contracts_raise_sanitized_review_codes(self):
        plan = risk_check(setup(GOOD, 'BTCUSDT'), RULES())
        bad = [{}, {**plan, 'mode': 'PRIVATE_MODE'}, {**plan, 'mode': 'DRY_RUN'},
               {**plan, 'mode': 'SIMULATION'}]
        for value in bad:
            with self.subTest(plan=value), self.assertRaises(Review) as caught:
                preflight(value, RULES())
            self.assertEqual(str(caught.exception), 'INVALID_ORDER_CONTRACT')
            self.assertNotIn('PRIVATE', str(caught.exception))

    def test_rr_repeating_fraction_uses_precise_saved_ratio_in_any_decimal_context(self):
        ratio = Fraction(19, 7)
        saved = str(Context(prec=160, rounding=ROUND_HALF_EVEN).divide(D(19), D(7)))
        plan = dict(entry='2715', tp='2734', sl='2708', rr=saved)
        self.assertGreater(len(saved), 64)
        for precision in (6, 28, 160):
            with self.subTest(precision=precision), localcontext() as context:
                context.prec = precision
                context.rounding = ROUND_UP
                self.assertEqual(verified_rr(plan), ratio)

    def test_rr_rejects_tampered_saved_ratio_and_rounded_under_two(self):
        saved = str(Context(prec=160, rounding=ROUND_HALF_EVEN).divide(D(19), D(7)))
        plan = dict(entry='2715', tp='2734', sl='2708', rr=saved)
        tampered = saved[:-1] + ('0' if saved[-1] != '0' else '1')
        for value in (tampered, '2.714285714285714', '3', 2.714, True, '1E+999'):
            with self.subTest(rr=value), self.assertRaisesRegex(Review, '^INVALID_RISK_REWARD$'):
                verified_rr({**plan, 'rr': value})
        with self.assertRaisesRegex(Review, '^INVALID_RISK_REWARD$'):
            verified_rr(dict(entry='100', tp='103.9999', sl='98', rr='2'))
        with self.assertRaisesRegex(Review, '^INVALID_RISK_REWARD$'):
            verified_rr(dict(entry='100', tp='104', sl='100', rr='2'))
        self.assertEqual(verified_rr(dict(entry='100', tp='105', sl='98', rr='2.5')), Fraction(5, 2))


class FeeRiskTests(unittest.TestCase):
    def rules(self, rate='0.0004', **changes):
        return replace(RULES(), taker_fee_rate=D(rate), **changes)

    def signal(self, side='LONG'):
        return Signal('BTCUSDT', side, D('100'), D('106') if side == 'LONG' else D('94'),
                      D('98') if side == 'LONG' else D('102'))

    def unit_costs(self, signal, rate):
        entry, tp, sl, fee = map(Fraction, (signal.entry, signal.tp, signal.sl, rate))
        loss = abs(entry - sl) + entry * fee + sl * fee
        reward = abs(tp - entry) - entry * fee - tp * fee
        return loss, reward

    def test_nonzero_taker_fees_limit_largest_lot_and_preserve_levels_for_both_sides(self):
        for side in ('LONG', 'SHORT'):
            for target in ('0.1', '5', '10', '100'):
                with self.subTest(side=side, target=target):
                    rules = self.rules(min_notional=D('0'))
                    signal = self.signal(side)
                    before = copy.deepcopy(signal)
                    plan = risk_check(signal, rules, D(target))
                    self.assertEqual(signal, before)
                    self.assertEqual((D(plan['entry']), D(plan['tp']), D(plan['sl'])),
                                     (signal.entry, signal.tp, signal.sl))
                    loss, reward = self.unit_costs(signal, rules.taker_fee_rate)
                    self.assertGreater(reward / loss, Fraction(2))
                    quantity = Fraction(D(plan['execution_quantity']))
                    self.assertLessEqual(quantity * loss, Fraction(D(target)))
                    self.assertGreater((quantity + Fraction(rules.step)) * loss, Fraction(D(target)))
                    self.assertEqual(Fraction(D(plan['risk'])), quantity * loss)
                    self.assertEqual(plan['risk'], plan['total_risk'])
                    self.assertEqual(Fraction(D(plan['net_reward'])), quantity * reward)
                    self.assertEqual(D(plan['risk_target_usdt']), D(target))
                    self.assertNotIn('neurobro_position_size', plan)
        plan = risk_check(self.signal(), self.rules())
        self.assertEqual(D(plan['execution_quantity']), D('2.404'))
        self.assertEqual(D(plan['total_risk']), D('4.9983968'))
        short = self.signal('SHORT')
        short_loss, _ = self.unit_costs(short, self.rules().taker_fee_rate)
        self.assertEqual(short_loss, Fraction(D('2.0808')))
        short_plan = risk_check(short, self.rules())
        self.assertEqual(D(short_plan['execution_quantity']), D('2.402'))
        self.assertEqual(D(short_plan['total_risk']), D('4.9980816'))

    def test_fee_components_and_net_reward_are_independently_accounted(self):
        rules, signal = self.rules(), self.signal()
        plan = risk_check(signal, rules)
        quantity, rate = Fraction(D(plan['quantity'])), Fraction(rules.taker_fee_rate)
        entry, tp, sl = map(Fraction, (signal.entry, signal.tp, signal.sl))
        self.assertEqual(Fraction(D(plan['gross_risk'])), quantity * abs(entry - sl))
        self.assertEqual(Fraction(D(plan['entry_fee_usdt'])), quantity * entry * rate)
        self.assertEqual(Fraction(D(plan['sl_exit_fee_usdt'])), quantity * sl * rate)
        self.assertEqual(Fraction(D(plan['tp_exit_fee_usdt'])), quantity * tp * rate)
        self.assertEqual(Fraction(D(plan['gross_reward'])), quantity * abs(tp - entry))
        self.assertEqual(Fraction(D(plan['total_risk'])),
                         Fraction(D(plan['gross_risk'])) + Fraction(D(plan['entry_fee_usdt'])) + Fraction(D(plan['sl_exit_fee_usdt'])))
        self.assertEqual(Fraction(D(plan['net_reward'])),
                         Fraction(D(plan['gross_reward'])) - Fraction(D(plan['entry_fee_usdt'])) - Fraction(D(plan['tp_exit_fee_usdt'])))
        loss, reward = self.unit_costs(signal, rules.taker_fee_rate)
        ratio = reward / loss
        expected = Context(prec=160, rounding=ROUND_HALF_EVEN).divide(D(ratio.numerator), D(ratio.denominator))
        self.assertEqual(D(plan['net_rr']), expected)
        self.assertEqual(D(plan['rr']), D('3'))
        self.assertEqual([D(plan[field]) for field in ('entry_fee_rate', 'exit_fee_rate', 'tp_fee_rate')],
                         [rules.taker_fee_rate] * 3)
        self.assertEqual((plan['fee_symbol'], plan['fee_source'], plan['fee_observed_at']),
                         ('BTCUSDT', FEE_SOURCE, rules.fee_observed_at))
        self.assertEqual(plan['sizing_method'], 'MAX_LOT_ENTRY_SL_TAKER_FEES_V2')
        self.assertEqual(plan['excluded_costs'], ['SLIPPAGE', 'FUNDING', 'GAPS'])

    def test_positive_fee_rejects_gross_rr_two_without_repricing_source(self):
        for side, sl, tp in [('LONG', 98, 104), ('SHORT', 102, 96)]:
            source = {**GOOD, 'side': side, 'stop_loss': sl, 'take_profit': tp}
            before = copy.deepcopy(source)
            with self.subTest(side=side), self.assertRaisesRegex(Review, '^NET_RISK_REWARD_BELOW_2$'):
                risk_check(setup(source, 'BTCUSDT'), self.rules())
            self.assertEqual(source, before)

    def test_exact_net_rr_boundary_is_accepted_and_one_tick_below_is_rejected(self):
        rules = self.rules(rate='0.04')
        signal = replace(self.signal(), tp=D('129'))
        loss, reward = self.unit_costs(signal, rules.taker_fee_rate)
        self.assertEqual(reward / loss, Fraction(2))
        plan = risk_check(signal, rules)
        self.assertEqual(D(plan['net_rr']), D('2'))
        self.assertEqual(D(plan['execution_quantity']), D('.504'))
        self.assertEqual(D(plan['total_risk']), D('4.99968'))
        lower = replace(signal, tp=D('128.99'))
        low_loss, low_reward = self.unit_costs(lower, rules.taker_fee_rate)
        self.assertLess(low_reward / low_loss, Fraction(2))
        with self.assertRaisesRegex(Review, '^NET_RISK_REWARD_BELOW_2$'):
            risk_check(lower, rules)

    def test_missing_fee_evidence_has_no_implicit_zero_rate_default(self):
        rules = Rules(D('.001'), D('.001'), D('1000'), D('.01'), D('5'), time.time())
        with self.assertRaisesRegex(Review, '^FEE_EVIDENCE_UNAVAILABLE$'):
            risk_check(self.signal(), rules)
        with self.assertRaisesRegex(Review, '^FEE_EVIDENCE_UNAVAILABLE$'):
            maximum_risk_quantity(D('100'), D('98'), rules)

    def test_invalid_fee_values_sources_and_symbols_are_rejected(self):
        invalid = [dict(taker_fee_rate=value) for value in
                   (D('-0.0001'), D('1'), D('NaN'), D('Infinity'), 0, 0.0004, '0.0004', True)]
        invalid += [dict(fee_source='PRIVATE_UNTRUSTED_SOURCE'), dict(fee_symbol='ETHUSDT'),
                    dict(fee_symbol='PRIVATE SYMBOL'), dict(fee_observed_at=float('nan')),
                    dict(fee_observed_at='PRIVATE_TIME')]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaisesRegex(Review, '^INVALID_FEE_EVIDENCE$'):
                risk_check(self.signal(), replace(self.rules(), **fields))

    def test_past_and_future_fee_observations_fail_even_when_price_rules_are_fresh(self):
        for observed in (time.time() - 301, time.time() + 30):
            rules = self.rules(fee_observed_at=observed)
            with self.subTest(observed=observed), self.assertRaisesRegex(Review, '^STALE_FEE_EVIDENCE$'):
                risk_check(self.signal(), rules)
            with self.assertRaisesRegex(Review, '^STALE_FEE_EVIDENCE$'):
                maximum_risk_quantity(D('100'), D('98'), rules)

    def test_low_decimal_precision_does_not_round_quantity_up_or_change_loss(self):
        rules = self.rules(step=D('.00000001'), minimum=D('.00000001'))
        signal = self.signal()
        expected = risk_check(signal, rules)
        expected_quantity = maximum_risk_quantity(signal.entry, signal.sl, rules)
        for precision in (3, 6, 28):
            with self.subTest(precision=precision), localcontext() as context:
                context.prec = precision
                context.rounding = ROUND_UP
                self.assertEqual(maximum_risk_quantity(signal.entry, signal.sl, rules), expected_quantity)
                plan = risk_check(signal, rules)
                for field in ('quantity', 'execution_quantity', 'risk', 'total_risk', 'gross_risk',
                              'entry_fee_usdt', 'sl_exit_fee_usdt', 'tp_exit_fee_usdt', 'net_reward', 'net_rr'):
                    self.assertEqual(plan[field], expected[field])

    def test_preflight_rejects_any_changed_fee_cost_rate_or_model(self):
        rules = self.rules()
        plan = risk_check(self.signal(), rules)
        before = copy.deepcopy(plan)
        preflight(plan, rules)
        self.assertEqual(plan, before)
        numeric_fields = ('risk', 'total_risk', 'gross_risk', 'entry_fee_usdt', 'sl_exit_fee_usdt',
                          'tp_exit_fee_usdt', 'gross_reward', 'net_reward', 'net_rr',
                          'entry_fee_rate', 'exit_fee_rate', 'tp_fee_rate')
        for field in numeric_fields:
            changed = {**plan, field: str(D(plan[field]) + D('.001'))}
            with self.subTest(field=field), self.assertRaises(Review):
                preflight(changed, rules)
        for field, value in [('sizing_method', 'GROSS_ONLY'), ('fee_symbol', 'ETHUSDT'),
                             ('fee_source', 'MAKER_ASSUMPTION'), ('fee_observed_at', time.time() - 301),
                             ('fee_observed_at', time.time() + 30),
                             ('excluded_costs', [])]:
            with self.subTest(field=field), self.assertRaises(Review):
                preflight({**plan, field: value}, rules)

    def test_preflight_accepts_newer_matching_fee_observation_without_changing_plan(self):
        earlier_rules = self.rules(fee_observed_at=time.time() - 10)
        plan = risk_check(self.signal(), earlier_rules)
        before = copy.deepcopy(plan)
        newer_rules = replace(earlier_rules, fee_observed_at=time.time())
        preflight(plan, newer_rules)
        self.assertEqual(plan, before)

    def test_archived_fee_costs_can_be_verified_without_live_freshness_override(self):
        signal = self.signal()
        old_rules = self.rules(fee_observed_at=time.time() - 86400)
        with self.assertRaisesRegex(Review, '^STALE_FEE_EVIDENCE$'):
            risk_costs(signal.entry, signal.tp, signal.sl, D('2.404'), old_rules, symbol=signal.symbol)
        stored = risk_costs(signal.entry, signal.tp, signal.sl, D('2.404'), old_rules,
                            symbol=signal.symbol, check_fresh=False)
        self.assertEqual(D(stored['total_risk']), D('4.9983968'))
        self.assertEqual(maximum_risk_quantity(signal.entry, signal.sl, old_rules, check_fresh=False), D('2.404'))
        with self.assertRaisesRegex(Review, '^INVALID_FEE_EVIDENCE$'):
            risk_costs(signal.entry, signal.tp, signal.sl, D('2.404'), replace(old_rules, fee_symbol='ETHUSDT'),
                       symbol=signal.symbol, check_fresh=False)


class AuditConnectionTests(unittest.TestCase):
    def test_fresh_ledger_has_wal_row_access_and_no_business_tables(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = Ledger(Path(root) / 'ledger.sqlite3')
            try:
                self.assertEqual(ledger.db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                self.assertEqual(ledger.db.execute('SELECT 7 AS value').fetchone()['value'], 7)
                self.assertEqual(list(ledger.db.execute("SELECT name FROM sqlite_master WHERE type='table'")), [])
                self.assertFalse(any(hasattr(ledger, method) for method in ('reserve', 'fill', 'close', 'snapshot', 'event')))
            finally:
                ledger.db.close()

    def test_opening_existing_ledger_preserves_legacy_audit_without_new_tables(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'ledger.sqlite3'
            with sqlite3.connect(path) as previous:
                previous.execute('CREATE TABLE trades(id TEXT PRIMARY KEY, plan TEXT)')
                previous.execute('INSERT INTO trades VALUES(?,?)', ('historical-record', '{"old":"audit"}'))
            ledger = Ledger(path)
            try:
                row = ledger.db.execute('SELECT * FROM trades').fetchone()
                self.assertEqual(dict(row), dict(id='historical-record', plan='{"old":"audit"}'))
                tables = [row[0] for row in ledger.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                self.assertEqual(tables, ['trades'])
                self.assertEqual(ledger.db.total_changes, 0)
            finally:
                ledger.db.close()


class OrderProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Ledger(Path(self.temp.name) / 'ledger.sqlite3')
        self.addCleanup(self.ledger.db.close)
        self.operation = day() + ':robot-v9:0:analysis-v9:BTCUSDT'
        self.source = {**GOOD, 'take_profit': 106, 'risk_reward': 3}
        client = NeuroAPI(self.ledger, key='synthetic-order-proof',
            transport=lambda *args, **kwargs: (200, {}, dict(mode='smart', answer=None, output=copy.deepcopy(self.source))))
        client.ask(self.operation, ANALYSIS, SETUP_SCHEMA, lambda value: setup(value, 'BTCUSDT'))
        rules = replace(RULES(), taker_fee_rate=D('0.0004'))
        self.plan = risk_check(setup(self.source, 'BTCUSDT'), rules)
        self.plan.update(risk_target_usdt='5', sizing_rules={
            key: str(value) if isinstance(value, D) else value for key, value in asdict(rules).items()})

    def test_current_intent_evidence_is_verified_without_writing_provider_journal(self):
        rows_before = [dict(row) for row in self.ledger.db.execute('SELECT * FROM api_requests')]
        stamp(self.ledger.db, self.plan, self.operation)
        self.assertEqual(self.plan['provenance']['analysis_version'], VERSION)
        self.assertEqual(VERSION, 'analysis-v9')
        self.assertTrue(verify(self.ledger.db, self.plan, self.operation))
        self.assertEqual(D(self.plan['total_risk']), D('4.9983968'))
        self.assertEqual([dict(row) for row in self.ledger.db.execute('SELECT * FROM api_requests')], rows_before)

    def test_restamping_cannot_validate_wrong_fee_math_or_old_sizing_model(self):
        numeric_fields = ('total_risk', 'gross_risk', 'entry_fee_usdt', 'sl_exit_fee_usdt',
                          'tp_exit_fee_usdt', 'entry_fee_rate', 'net_reward', 'net_rr')
        changes = [(field, str(D(self.plan[field]) + D('.001'))) for field in numeric_fields]
        changes += [('sizing_method', 'GROSS_ONLY')]
        for field, value in changes:
            changed = copy.deepcopy(self.plan)
            changed[field] = value
            with self.subTest(field=field):
                try:
                    stamp(self.ledger.db, changed, self.operation)
                except Review as error:
                    self.assertEqual(str(error), 'ROBOT_SETUP_UNVERIFIED')
                    continue
                with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
                    verify(self.ledger.db, changed, self.operation)

    def test_archived_valid_fee_proof_keeps_arithmetic_verifiable_after_expiry(self):
        stamp(self.ledger.db, self.plan, self.operation)
        future = time.time() + 86400
        with patch('worker.core.time.time', return_value=future):
            self.assertTrue(verify(self.ledger.db, self.plan, self.operation))

    def test_changed_levels_quantity_risk_or_proof_fail_verification(self):
        stamp(self.ledger.db, self.plan, self.operation)
        for key, value in [('entry', '101'), ('execution_quantity', '.002'), ('risk_target_usdt', '10'), ('rr', '4')]:
            changed = copy.deepcopy(self.plan)
            changed[key] = value
            with self.subTest(field=key), self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
                verify(self.ledger.db, changed, self.operation)

    def test_changed_provider_source_is_not_accepted(self):
        stamp(self.ledger.db, self.plan, self.operation)
        source = json.loads(self.ledger.db.execute('SELECT output FROM api_requests').fetchone()[0])
        source['limit_entry'] = 101
        self.ledger.db.execute('UPDATE api_requests SET output=?', (json.dumps(source),))
        with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
            verify(self.ledger.db, self.plan, self.operation)

    def test_legacy_mode_namespace_or_existing_proof_cannot_be_promoted(self):
        old_operation = self.operation.replace('robot-v9', 'robot-v8').replace('analysis-v9', 'analysis-v8')
        invalid = [(dict(self.plan, mode='DRY_RUN'), self.operation),
                   (copy.deepcopy(self.plan), old_operation),
                   (dict(self.plan, provenance=dict(analysis_version='analysis-v8')), self.operation)]
        for plan, operation in invalid:
            before = copy.deepcopy(plan)
            with self.subTest(operation=operation, plan=plan), self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
                stamp(self.ledger.db, plan, operation)
            self.assertEqual(plan, before)
        stamp(self.ledger.db, self.plan, self.operation)
        with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
            verify(self.ledger.db, self.plan, old_operation)

    def test_unfinished_or_missing_source_cannot_receive_provenance(self):
        self.ledger.db.execute("UPDATE api_requests SET state='PENDING', output=NULL")
        before = copy.deepcopy(self.plan)
        with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
            stamp(self.ledger.db, self.plan, self.operation)
        self.assertEqual(self.plan, before)
        with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
            stamp(self.ledger.db, self.plan, day() + ':robot-v9:1:analysis-v9:BTCUSDT')
        self.assertEqual(self.plan, before)


if __name__ == '__main__':
    unittest.main()
