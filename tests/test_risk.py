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

from worker.core import D, Ledger, Review, Rules, Signal, day, maximum_risk_quantity, preflight, risk_check, validated_risk_target, verified_rr
from worker.neuroapi import NeuroAPI, SETUP_SCHEMA, setup
from worker.prompts import ANALYSIS
from worker.robot_provenance import VERSION, stamp, verify
from support import GOOD, RULES


class RiskTests(unittest.TestCase):
    def test_long_short_levels_are_immutable_and_provider_quantity_is_audit_only(self):
        for side, sl, tp in [('LONG', 98, 104), ('SHORT', 102, 96)]:
            with self.subTest(side=side):
                source = {**GOOD, 'side': side, 'stop_loss': sl, 'take_profit': tp,
                          'position_size': D('.002')}
                before = copy.deepcopy(source)
                plan = risk_check(setup(source, 'BTCUSDT'), RULES())
                self.assertEqual(source, before)
                self.assertEqual((D(plan['entry']), D(plan['tp']), D(plan['sl'])), (D(100), D(tp), D(sl)))
                self.assertEqual(plan['neurobro_position_size'], '0.002')
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
        distance = D('1.000000000000000000000000000000000000000000000000000000000000001')
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
        self.assertEqual(plan['neurobro_position_size'], '999')

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
        self.operation = day() + ':robot-v8:0:analysis-v8:BTCUSDT'
        client = NeuroAPI(self.ledger, key='synthetic-order-proof',
            transport=lambda *args, **kwargs: (200, {}, dict(mode='smart', answer=None, output=copy.deepcopy(GOOD))))
        client.ask(self.operation, ANALYSIS, SETUP_SCHEMA, lambda value: setup(value, 'BTCUSDT'))
        rules = RULES()
        self.plan = risk_check(setup(GOOD, 'BTCUSDT'), rules)
        self.plan.update(risk_target_usdt='5', sizing_rules={
            key: str(value) if isinstance(value, D) else value for key, value in asdict(rules).items()})

    def test_current_intent_evidence_is_verified_without_writing_provider_journal(self):
        rows_before = [dict(row) for row in self.ledger.db.execute('SELECT * FROM api_requests')]
        stamp(self.ledger.db, self.plan, self.operation)
        self.assertEqual(self.plan['provenance']['analysis_version'], VERSION)
        self.assertEqual(VERSION, 'analysis-v8')
        self.assertTrue(verify(self.ledger.db, self.plan, self.operation))
        self.assertEqual([dict(row) for row in self.ledger.db.execute('SELECT * FROM api_requests')], rows_before)

    def test_changed_levels_quantity_risk_or_proof_fail_verification(self):
        stamp(self.ledger.db, self.plan, self.operation)
        for key, value in [('entry', '101'), ('execution_quantity', '.002'), ('risk_target_usdt', '10'), ('rr', '3')]:
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
        old_operation = self.operation.replace('robot-v8', 'robot-v7').replace('analysis-v8', 'analysis-v7')
        invalid = [(dict(self.plan, mode='DRY_RUN'), self.operation),
                   (copy.deepcopy(self.plan), old_operation),
                   (dict(self.plan, provenance=dict(analysis_version='analysis-v7')), self.operation)]
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
            stamp(self.ledger.db, self.plan, day() + ':robot-v8:1:analysis-v8:BTCUSDT')
        self.assertEqual(self.plan, before)


if __name__ == '__main__':
    unittest.main()
