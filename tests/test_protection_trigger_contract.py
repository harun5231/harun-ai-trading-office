"""Immutable protection trigger ABI: old MARK intents and new last-price plans."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

import test_order_pipeline as pipeline
from worker.core import D, Review, Rules, preflight, risk_check
from worker.neuroapi import setup
from worker.order_gateway import build_intent
from worker.robot_provenance import stamp, verify


# Legacy zero-fee ETH fixture command, captured before adding the plan flag.
# IDs and timestamps/proof hashes below are the only varying fixture metadata.
LEGACY_ETH = json.loads('''{
  "intent_id":"", "client_order_id":"", "symbol":"ETHUSDT",
  "position_side":"BOTH", "side":"LONG",
  "entry":{"order_type":"LIMIT","side":"BUY","price":"100","quantity":"2.5","time_in_force":"GTC"},
  "protection":{"exit_side":"SELL","stop_loss":"98","take_profit":"104","working_type":"MARK_PRICE"},
  "margin_mode":"CROSS", "leverage":75, "risk_target_usdt":"5", "risk_usdt":"5",
  "gross_risk_usdt":"5", "entry_fee_usdt":"0", "sl_exit_fee_usdt":"0", "tp_exit_fee_usdt":"0",
  "net_reward_usdt":"10", "net_reward_risk":"2",
  "fee_evidence":{"source":"BINANCE_FUTURES_COMMISSION_RATE","symbol":"ETHUSDT","observed_at":0,"taker_rate":"0"},
  "excluded_costs":["SLIPPAGE","FUNDING","GAPS"], "evidence_sha256":""
}''')


class ProtectionTriggerContractTests(unittest.TestCase):
    def setUp(self):
        network = patch('socket.socket', side_effect=AssertionError('Live exchange forbidden'))
        network.start()
        self.addCleanup(network.stop)
        self.fixture = pipeline.OrderPipelineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.account.position('HYPEUSDT', '4.16')
        self.fixture.screens = [['ETHUSDT']]
        self.fixture.on()
        self.fixture.ticks(2)
        self.row = self.fixture.ledger.db.execute('SELECT * FROM robot_candidates').fetchone()
        self.operation = self.row['id']
        current = json.loads(self.row['plan'])
        # Reconstruct the actual archived fee-only policy from its original
        # provider output and rules, without carrying V3 normalized evidence.
        self.rules = Rules(**{key: D(value) if isinstance(value, str) and
            key not in ('fee_source', 'fee_symbol') else value
            for key, value in current['sizing_rules'].items()})
        source = self.fixture.ledger.db.execute('SELECT output FROM api_requests WHERE operation=?',
            (self.operation,)).fetchone()[0]
        signal = setup(json.loads(source, parse_float=D), 'ETHUSDT', require_declared_rr=False)
        self.plan = risk_check(signal, self.rules, current['risk_target_usdt'])
        self.plan['sizing_rules'] = copy.deepcopy(current['sizing_rules'])
        stamp(self.fixture.ledger.db, self.plan, self.operation)
        self.fixture.ledger.db.execute('UPDATE robot_candidates SET plan=? WHERE id=?',
            (json.dumps(self.plan), self.operation))

    def test_legacy_eth_verified_payload_is_byte_identical_to_old_command(self):
        before = json.dumps(self.plan)
        expected = copy.deepcopy(LEGACY_ETH)
        expected['intent_id'] = self.operation
        expected['client_order_id'] = 'hao-' + hashlib.sha256(self.operation.encode()).hexdigest()[:28]
        expected['fee_evidence']['observed_at'] = self.plan['fee_observed_at']
        expected['evidence_sha256'] = self.plan['provenance']['payload_sha256']
        legacy_bytes = json.dumps(expected).encode()
        self.assertTrue(verify(self.fixture.ledger.db, self.plan, self.operation))
        preflight(self.plan, self.rules, self.plan['risk_target_usdt'])
        self.assertEqual(json.dumps(build_intent(self.plan, self.operation)).encode(), legacy_bytes)
        self.assertEqual(json.dumps(self.plan), before)
        self.assertNotIn('protection_working_type', self.plan)
        at = self.plan['fee_observed_at']
        self.fixture.ledger.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
            (self.operation, self.operation, 'ETHUSDT', 'NEEDS_REVIEW', legacy_bytes.decode(),
             None, 'BINANCE_ORDER_PROTECTION_OUTCOME_UNKNOWN', str(at), str(at)))
        row = self.fixture.intent()
        self.assertEqual(json.dumps(self.fixture.robot.verified_intent(row)).encode(), legacy_bytes)
        self.fixture.restart()
        self.assertEqual(self.fixture.intent()['payload'].encode(), legacy_bytes)
        self.assertEqual(json.dumps(self.fixture.robot.verified_intent(self.fixture.intent())).encode(), legacy_bytes)

    def test_explicit_contract_price_is_validated_stamped_and_built_without_repricing(self):
        old_command = build_intent(self.plan, self.operation)
        last = copy.deepcopy(self.plan)
        last['protection_working_type'] = 'CONTRACT_PRICE'
        stamp(self.fixture.ledger.db, last, self.operation)
        self.assertTrue(verify(self.fixture.ledger.db, last, self.operation))
        preflight(last, self.rules, last['risk_target_usdt'])
        command = build_intent(last, self.operation)
        self.assertEqual(command['protection']['working_type'], 'CONTRACT_PRICE')
        self.assertNotEqual(command['evidence_sha256'], old_command['evidence_sha256'])
        command['protection']['working_type'] = 'MARK_PRICE'
        command['evidence_sha256'] = old_command['evidence_sha256']
        self.assertEqual(command, old_command)
        self.assertEqual({key: value for key, value in last.items()
                          if key not in ('protection_working_type', 'provenance')},
                         {key: value for key, value in self.plan.items() if key != 'provenance'})

    def test_arbitrary_trigger_values_are_rejected_at_every_contract_boundary(self):
        for value in (None, True, 75, '', 'LAST_PRICE', 'mark_price', [], {}):
            with self.subTest(value=value):
                invalid = copy.deepcopy(self.plan)
                invalid['protection_working_type'] = value
                proof = copy.deepcopy(invalid['provenance'])
                with self.assertRaises(Review):
                    build_intent(invalid, self.operation)
                with self.assertRaises(Review):
                    preflight(invalid, self.rules, invalid['risk_target_usdt'])
                with self.assertRaises(Review):
                    stamp(self.fixture.ledger.db, invalid, self.operation)
                self.assertEqual(invalid['provenance'], proof)
                with self.assertRaises(Review):
                    verify(self.fixture.ledger.db, invalid, self.operation)

    def test_valid_trigger_tampering_fails_existing_provenance(self):
        for old, new in ((None, 'CONTRACT_PRICE'), ('CONTRACT_PRICE', 'MARK_PRICE')):
            with self.subTest(old=old):
                original = copy.deepcopy(self.plan)
                if old is not None:
                    original['protection_working_type'] = old
                    stamp(self.fixture.ledger.db, original, self.operation)
                changed = copy.deepcopy(original)
                changed['protection_working_type'] = new
                with self.assertRaises(Review):
                    verify(self.fixture.ledger.db, changed, self.operation)
                self.assertTrue(verify(self.fixture.ledger.db, original, self.operation))

    def test_restamped_candidate_cannot_silently_change_existing_mark_intent(self):
        original = build_intent(self.plan, self.operation)
        self.fixture.ledger.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
            (self.operation, self.operation, 'ETHUSDT', 'NEEDS_REVIEW', json.dumps(original),
             None, 'BINANCE_ORDER_PROTECTION_OUTCOME_UNKNOWN', 'original', 'original'))
        last = copy.deepcopy(self.plan)
        last['protection_working_type'] = 'CONTRACT_PRICE'
        stamp(self.fixture.ledger.db, last, self.operation)
        self.fixture.ledger.db.execute('UPDATE robot_candidates SET plan=? WHERE id=?',
            (json.dumps(last), self.operation))
        with self.assertRaisesRegex(Review, 'ORDER_EVIDENCE_UNVERIFIED'):
            self.fixture.robot.verified_intent(self.fixture.intent())
        self.assertEqual(json.loads(self.fixture.intent()['payload']), original)
        self.assertEqual(self.fixture.intent()['state'], 'NEEDS_REVIEW')
        self.assertEqual(self.fixture.receipt_count(), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
