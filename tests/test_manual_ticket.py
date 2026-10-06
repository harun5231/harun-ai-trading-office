"""Real v7 verifier, synthetic provider fixtures, and network-free lifecycle models."""
import copy
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from worker.core import D, Review
from worker.manual_ticket import build_ticket, simulate_ticket, SCENARIOS, PNL_BASIS
from worker.provenance import digest
import test_robot as robot_fixtures


class ManualTicketTests(unittest.TestCase):
    def setUp(self):
        self.seed()

    def seed(self, side='LONG', risk='5'):
        fixture = robot_fixtures.RobotTests('test_default_risk_five')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.account.position('HYPEUSDT', '4.16')
        fixture.screens = [['BTCUSDT']]
        fixture.decisions['BTCUSDT'] = side
        fixture.store.configure({'risk_target_usdt': risk})
        fixture.ready()
        self.fixture = fixture
        self.db = fixture.ledger.db
        self.row = self.db.execute('SELECT * FROM robot_setups').fetchone()
        self.assertEqual(self.row['status'], 'SETUP_READY')
        self.plan = json.loads(self.row['plan'])
        self.ticket = self.build()

    def build(self, plan=None, setup_id=None, status='SETUP_READY'):
        return build_ticket(self.db, plan if plan is not None else self.plan,
                            setup_id or self.row['id'], status)

    def test_verified_ticket_preserves_source_values_and_quantity(self):
        t, p = self.ticket, self.plan
        self.assertEqual(t['setup_id'], self.row['id'])
        self.assertEqual((t['symbol'], t['side'], t['margin_mode'], t['leverage']),
                         (p['symbol'], p['side'], p['margin_mode'], p['leverage']))
        for key in ('risk_target_usdt', 'risk', 'rr'):
            self.assertEqual(t[key], p[key])
        self.assertEqual(t['entry'], dict(order_type='LIMIT', side='BUY', price=p['entry'],
                                         quantity=p['execution_quantity'],
                                         time_in_force='GTC', position_side='BOTH'))
        self.assertEqual(t['take_profit'], dict(order_type='TAKE_PROFIT_MARKET', side='SELL',
                                               trigger_price=p['tp'], working_type='MARK_PRICE',
                                               close_position=True))
        self.assertEqual(t['stop_loss'], dict(order_type='STOP_MARKET', side='SELL',
                                             trigger_price=p['sl'], working_type='MARK_PRICE',
                                             close_position=True))
        self.assertFalse(t['submission_enabled'])
        self.assertTrue(t['review_required'])
        self.assertEqual(t['execution_mode'], 'MANUAL_ONLY')
        self.assertEqual(t['pnl_basis'], PNL_BASIS)
        self.assertEqual(D(t['estimated_loss_usdt']), D('5'))
        self.assertEqual(D(t['estimated_profit_usdt']), D('10'))
        self.assertNotEqual(t['entry']['quantity'], p['neurobro_position_size'])

    def test_short_uses_sell_entry_buy_protection_and_correct_pnl(self):
        self.seed('SHORT')
        self.assertEqual(self.ticket['entry']['side'], 'SELL')
        for leg in ('take_profit', 'stop_loss'):
            self.assertEqual(self.ticket[leg]['side'], 'BUY')
        for scenario, pnl in (('FULL_TP', '10'), ('FULL_SL', '-5'), ('PARTIAL_TP', '5')):
            with self.subTest(scenario=scenario):
                result = simulate_ticket(self.ticket, scenario)
                self.assertEqual(D(result['pnl_usdt']), D(pnl))
                self.assertEqual(result['status'], 'CLOSED')

    def test_ticket_creation_timestamp_and_checksum_are_stable(self):
        created = self.db.execute('SELECT created FROM api_requests WHERE operation=?',
                                  (self.row['id'],)).fetchone()[0]
        self.assertEqual(self.ticket['source_created_at'],
                         datetime.fromtimestamp(created, timezone.utc).isoformat())
        self.assertEqual(self.ticket['business_day'], self.row['id'][:10])
        self.assertIn('open orders Binance', self.ticket['manual_review_note'])
        self.assertEqual(self.build(), self.ticket)
        self.assertEqual(self.ticket['ticket_sha256'], digest({
            key: value for key, value in self.ticket.items() if key != 'ticket_sha256'
        }))

    def test_only_ready_or_approved_setup_can_have_ticket(self):
        self.assertEqual(self.build(status='APPROVED')['status'], 'APPROVED')
        for status in ('HOLD', 'REJECTED', 'USER_REJECTED', 'WAITING', None, 'CLOSED'):
            with self.subTest(status=status), self.assertRaisesRegex(Review, '^SETUP_NOT_READY$'):
                self.build(status=status)

    def test_manual_hype_exposure_never_has_ticket(self):
        changed = copy.deepcopy(self.plan)
        changed['symbol'] = 'HYPEUSDT'
        with self.assertRaisesRegex(Review, '^ROBOT_MANUAL_EXPOSURE_PROTECTED$'):
            self.build(plan=changed)
        self.assertEqual(self.fixture.account.positions[0]['positionAmt'], '4.16')

    def test_tampered_plan_levels_risk_quantity_or_provenance_fail_closed(self):
        for field, value in (('entry', '101'), ('tp', '110'), ('sl', '95'),
                             ('execution_quantity', '3'), ('risk', '6'), ('risk_target_usdt', '10')):
            changed = copy.deepcopy(self.plan)
            changed[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
                self.build(plan=changed)
        changed = copy.deepcopy(self.plan)
        changed.pop('provenance')
        with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
            self.build(plan=changed)
        with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
            self.build(setup_id=self.row['id'].replace('analysis-v7', 'analysis-v6'))

    def test_changed_provider_evidence_is_not_accepted(self):
        operation = self.row['id']
        row = self.db.execute('SELECT output FROM api_requests WHERE operation=?', (operation,)).fetchone()
        changed = json.loads(row[0])
        changed['take_profit'] = 110
        self.db.execute('UPDATE api_requests SET output=? WHERE operation=?', (json.dumps(changed), operation))
        with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
            self.build()

    def test_incomplete_request_cannot_be_promoted_to_ticket(self):
        self.db.execute("UPDATE api_requests SET state='NEEDS_REVIEW' WHERE operation=?", (self.row['id'],))
        with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
            self.build()

    def test_build_and_all_scenarios_make_no_network_calls_or_database_writes(self):
        changes = self.db.total_changes
        provider_calls = copy.deepcopy(self.fixture.calls)
        account_calls = copy.deepcopy(self.fixture.account.calls)
        market_calls = copy.deepcopy(self.fixture.market.calls)
        with patch('socket.socket', side_effect=AssertionError('Network forbidden')), \
                patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden')):
            ticket = self.build()
            for scenario in sorted(SCENARIOS):
                result = simulate_ticket(ticket, scenario)
                self.assertFalse(result['real_order_submitted'])
                self.assertFalse(result['live_execution'])
                self.assertTrue(result['synthetic_trace'])
                self.assertEqual(result['mode'], 'SIMULATION')
                self.assertTrue(all(step['synthetic'] and step['model_only'] for step in result['steps']))
        self.assertEqual(self.db.total_changes, changes)
        self.assertEqual(self.fixture.calls, provider_calls)
        self.assertEqual(self.fixture.account.calls, account_calls)
        self.assertEqual(self.fixture.market.calls, market_calls)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM trades').fetchone()[0], 0)

    def test_success_trace_ack_is_not_fill_and_sl_precedes_protection(self):
        result = simulate_ticket(self.ticket, 'FULL_TP')
        steps = result['steps']
        by_event = {step['event']: step for step in steps}
        ack = by_event['ENTRY_ACK']
        self.assertFalse(ack['fill_confirmed'])
        self.assertEqual(D(ack['filled_quantity']), 0)
        self.assertEqual(ack['state'], 'ENTRY_SUBMITTED')
        sl, tp = by_event['SL_CONFIRMED'], by_event['TP_CONFIRMED']
        self.assertLess(sl['sequence'], tp['sequence'])
        self.assertEqual(sl['state'], 'PROTECTION_INCOMPLETE')
        self.assertFalse(sl['tp_confirmed'])
        self.assertEqual(tp['state'], 'POSITION_PROTECTED')
        self.assertTrue(tp['fill_confirmed'] and tp['sl_confirmed'] and tp['tp_confirmed'])
        self.assertEqual(result['status'], 'CLOSED')
        self.assertFalse(result['blocks_next'])
        self.assertEqual(D(result['pnl_usdt']), 10)

    def test_full_sl_closes_with_estimated_loss_at_ticket_level(self):
        result = simulate_ticket(self.ticket, 'FULL_SL')
        self.assertEqual(result['status'], 'CLOSED')
        self.assertEqual(D(result['pnl_usdt']), -D(self.ticket['estimated_loss_usdt']))
        self.assertIn('SL_TRIGGERED', [step['event'] for step in result['steps']])
        self.assertNotIn('TP_TRIGGERED', [step['event'] for step in result['steps']])
        self.assertEqual(result['pnl_basis'], PNL_BASIS)

    def test_partial_fill_cancels_remainder_before_protecting_actual_fill(self):
        result = simulate_ticket(self.ticket, 'PARTIAL_TP')
        self.assertEqual(D(result['filled_quantity']), D('1.25'))
        self.assertEqual(D(result['canceled_quantity']), D('1.25'))
        self.assertEqual(D(result['remaining_quantity']), 0)
        self.assertEqual(D(result['pnl_usdt']), D('5'))
        events = [step['event'] for step in result['steps']]
        self.assertLess(events.index('REMAINDER_CANCEL_CONFIRMED'), events.index('SL_CONFIRMED'))
        self.assertLess(events.index('RECONCILED_FILL_REMAINDER_CLEARED'), events.index('SL_CONFIRMED'))
        partial = next(step for step in result['steps'] if step['event'] == 'PARTIAL_FILL')
        self.assertFalse(partial['fill_confirmed'])
        self.assertEqual(partial['state'], 'PROTECTION_INCOMPLETE')

    def test_partial_quantity_uses_legal_lots_when_exact_half_is_illegal(self):
        self.seed(risk='5.002')
        result = simulate_ticket(self.ticket, 'PARTIAL_TP')
        self.assertEqual(D(self.ticket['entry']['quantity']), D('2.501'))
        self.assertEqual(D(result['filled_quantity']), D('1.250'))
        self.assertEqual(D(result['canceled_quantity']), D('1.251'))
        self.assertEqual(D(result['filled_quantity']) % D(self.ticket['quantity_step']), 0)
        self.assertEqual(D(result['pnl_usdt']), D('5'))

    def test_invalid_source_creation_timestamp_cannot_produce_ticket(self):
        self.db.execute('UPDATE api_requests SET created=NULL WHERE operation=?', (self.row['id'],))
        with self.assertRaisesRegex(Review, '^ROBOT_SETUP_UNVERIFIED$'):
            self.build()

    def test_protection_failure_blocks_model_without_inventing_closed_pnl(self):
        result = simulate_ticket(self.ticket, 'PROTECTION_FAILURE')
        self.assertEqual(result['status'], 'PROTECTION_INCOMPLETE')
        self.assertTrue(result['blocks_next'])
        self.assertIsNone(result['pnl_usdt'])
        self.assertTrue(result['steps'][-1]['sl_confirmed'])
        self.assertFalse(result['steps'][-1]['tp_confirmed'])
        self.assertNotIn('POSITION_PROTECTED', [step['state'] for step in result['steps']])
        self.assertNotIn('CLOSED', [step['state'] for step in result['steps']])

    def test_uncertain_entry_needs_reconciliation_without_blind_resend(self):
        result = simulate_ticket(self.ticket, 'UNCERTAIN_ENTRY')
        self.assertEqual(result['status'], 'RECONCILIATION_REQUIRED')
        self.assertTrue(result['blocks_next'])
        self.assertIsNone(result['filled_quantity'])
        self.assertIsNone(result['remaining_quantity'])
        self.assertIsNone(result['pnl_usdt'])
        self.assertEqual([step['event'] for step in result['steps']], ['PLAN_READY', 'ENTRY_UNCERTAIN'])
        self.assertIn('tanpa kirim ulang', result['steps'][-1]['detail'])

    def test_simulation_is_deterministic_and_does_not_mutate_ticket_or_plan(self):
        original_plan, original_ticket = copy.deepcopy(self.plan), copy.deepcopy(self.ticket)
        for scenario in SCENARIOS:
            self.assertEqual(simulate_ticket(self.ticket, scenario), simulate_ticket(self.ticket, scenario))
        self.assertEqual(self.ticket, original_ticket)
        self.assertEqual(self.plan, original_plan)
        self.ticket['entry']['price'] = '999'
        self.assertEqual(self.build(), original_ticket)
        self.assertEqual(self.plan, original_plan)

    def test_detached_ticket_tampering_is_rejected(self):
        for field, value in (('price', '101'), ('quantity', '3')):
            changed = copy.deepcopy(self.ticket)
            changed['entry'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(Review, '^INVALID_MANUAL_TICKET$'):
                simulate_ticket(changed, 'FULL_TP')
        with self.assertRaisesRegex(Review, '^INVALID_MANUAL_TICKET$'):
            simulate_ticket({}, 'FULL_TP')

    def test_live_flags_and_extra_capabilities_are_rejected_even_with_recomputed_hash(self):
        for field, value in (('submission_enabled', True), ('review_required', False),
                             ('execution_mode', 'LIVE'), ('symbol', 'HYPEUSDT'),
                             ('network_endpoint', 'https://example.invalid')):
            changed = copy.deepcopy(self.ticket)
            changed[field] = value
            changed['ticket_sha256'] = digest({key: value for key, value in changed.items()
                                                if key != 'ticket_sha256'})
            with self.subTest(field=field), self.assertRaisesRegex(Review, '^INVALID_MANUAL_TICKET$'):
                simulate_ticket(changed, 'FULL_TP')

    def test_unsupported_scenario_is_rejected(self):
        for value in ('LIVE', 'TP', '', None, True, ['FULL_TP']):
            with self.subTest(value=value), self.assertRaisesRegex(Review, '^INVALID_SIMULATION_SCENARIO$'):
                simulate_ticket(self.ticket, value)

    def test_outputs_are_json_and_do_not_expose_provider_keys_or_raw_evidence(self):
        encoded = json.dumps(dict(ticket=self.ticket,
                                  simulation=simulate_ticket(self.ticket, 'FULL_TP')))
        for forbidden in ('synthetic-robot-key', 'X-API-Key', 'message_history',
                          'sizing_rules', 'robot_entry_receipts', 'newClientOrderId'):
            self.assertNotIn(forbidden, encoded)
        for value in (self.ticket['estimated_loss_usdt'], self.ticket['estimated_profit_usdt'],
                      simulate_ticket(self.ticket, 'FULL_SL')['pnl_usdt']):
            self.assertRegex(value, r'^-?\d+(?:\.\d+)?$')


if __name__ == '__main__':
    unittest.main()
