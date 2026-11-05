"""Offline factual performance and scheduler regressions; no actual analyst run."""
import copy
import hashlib
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tests.test_trade_evaluation import position
from tests.test_trade_evaluation_evidence import fixtures
from worker import trade_evaluation as journal
from worker import trade_evaluation_evidence as evidence
from worker import trade_evaluation_metrics as metrics
from worker import trade_evaluation_runtime as runtime


class PerformanceObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def observe(self, rows):
        return journal.observe_calendar(self.temp.name, dict(positions=rows, complete=False,
            incomplete_reasons=['SYMBOL_DISCOVERY_NOT_EXHAUSTIVE']))

    def test_monthly_wib_metrics_dedupe_and_net_costs_are_not_subtracted_twice(self):
        rows = [position('oct', pnl='9', closed='2026-10-31T16:59:59Z'),
            position('a', pnl='3', closed='2026-10-31T17:00:00Z'),
            position('b', pnl='-4', closed='2026-11-01T18:00:00Z'),
            position('c', pnl='2', closed='2026-11-02T18:00:00Z')]
        rows[1].update(commission_usdt='0.5', funding_usdt='-0.1', insurance_usdt='0')
        state = self.observe(rows)
        self.assertEqual(self.observe(rows[::-1] + rows[:1]), state)
        october, november = state['monthly_performance']
        self.assertEqual(october['month'], '2026-10')
        self.assertEqual(november['month'], '2026-11')
        self.assertEqual(november['closed_positions'], 3)
        self.assertEqual(november['wins'], 2)
        self.assertEqual(november['net_pnl_usdt'], '1')
        self.assertEqual(november['closed_net_pnl_drawdown_usdt'], '4')
        self.assertTrue(november['win_rate'].startswith('0.666666'))
        self.assertTrue(november['profitable_close_day_ratio'].startswith('0.666666'))
        self.assertEqual(november['scope'], 'OBSERVED_CLOSED_POSITIONS')
        self.assertFalse(state['streak_complete'])
        for field in ('operational_cost_usdt', 'after_operational_cost_net_usdt', 'equity_drawdown_percent'):
            self.assertIsNone(november[field])

    def test_incomplete_close_keeps_winrate_net_drawdown_unknown_and_never_uses_known_subtotal(self):
        rows = [position('a', pnl='1'), position('b', pnl=None, complete=False)]
        month = self.observe(rows)['monthly_performance'][0]
        self.assertFalse(month['complete'])
        self.assertEqual(month['unknown_positions'], 1)
        self.assertEqual(month['known_closed_net_pnl_usdt'], '1')
        for field in ('win_rate', 'net_pnl_usdt', 'closed_net_pnl_drawdown_usdt', 'profitable_close_day_ratio'):
            self.assertIsNone(month[field])

    def test_decline_trigger_is_stable_revision_safe_and_unknown_breaks_comparison(self):
        rows = [position(str(i), pnl=value, closed=f'2026-11-0{i+1}T00:00:00Z')
            for i, value in enumerate(('2', '1', '-1', '1', '-1', '-2'))]
        state = self.observe(rows)
        job = next(job for job in state['jobs'] if job['id'] == 'performance-decline:5')
        self.assertEqual(job['trade_ids'], [str(i) for i in range(6)])
        self.assertEqual(job['findings']['baseline_net_pnl_usdt'], '2')
        self.assertEqual(job['findings']['recent_net_pnl_usdt'], '-2')
        self.assertEqual(job['status'], 'BLOCKED_MISSING_EVIDENCE')
        self.assertEqual(job['experiment_outcome']['status'], 'NOT_RUN')
        with sqlite3.connect(self.temp.name + '/' + journal.FILENAME) as db:
            count = db.execute('SELECT count(*) FROM evaluation_events').fetchone()[0]
        self.assertEqual(self.observe(rows), state)
        with sqlite3.connect(self.temp.name + '/' + journal.FILENAME) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM evaluation_events').fetchone()[0], count)
        rows[1].update(complete=False, pnl_usdt=None)
        state = self.observe(rows)
        stale = next(job for job in state['jobs'] if job['id'] == 'performance-decline:5')
        self.assertEqual(stale['status'], 'SUPERSEDED_HISTORY_REVISION')
        self.assertEqual(stale['experiment_outcome']['status'], 'NOT_RUN')

    def test_decline_requires_both_fewer_wins_and_lower_net_not_one_losing_trade(self):
        for values in (('2','1','-1','1','1','-20'), ('2','1','-1','100','-1','-1')):
            rows = [position(str(i), pnl=value, closed=f'2026-11-0{i+1}T00:00:00Z')
                for i, value in enumerate(values)]
            self.assertEqual(list(metrics.decline_windows(rows)), [])


class EvaluationEvidenceConsumptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = fixtures()
        self.addCleanup(self.fixture.coordinator.db.close)
        f = self.fixture
        self.binding = evidence.bind_closed_trade(f.coordinator, f.position, history=f.history,
            archive_reader=lambda *_:copy.deepcopy(f.archive), source_reader=f.source)

    def seed(self, count, pnls=None, *, directory=None):
        """Synthetic bound closed rows exercise scheduling, not actual custody."""
        directory = directory or self.temp.name
        rows = []
        for i in range(count):
            row = copy.deepcopy(self.fixture.position)
            row.update(id=str(i), closed_at=f'2026-11-0{i+1}T00:00:00Z')
            if pnls is not None:
                row['pnl_usdt'] = pnls[i]
            rows.append(row)
        journal.observe_calendar(directory, dict(positions=rows))
        for row in rows:
            binding = copy.deepcopy(self.binding)
            binding.update(trade_id=row['id'], closed_position_sha256=evidence.digest(row))
            binding['outcome']['pnl_usdt'] = row['pnl_usdt']
            binding['binding_sha256'] = evidence.digest({k:v for k,v in binding.items() if k!='binding_sha256'})
            journal.attach_evidence(directory, binding)
        return rows

    def consumer(self, receipt=None, *, directory=None):
        return runtime.EvaluationConsumer(directory or self.temp.name, archive_reader=Mock(), room_reader=lambda:'a'*32,
            native_receipt_reader=receipt or Mock(side_effect=ValueError('NO_NATIVE_RECEIPT')))

    def test_signed_fills_cost_breakdown_actual_r_and_untested_lessons_reach_neurobro(self):
        f = self.fixture
        journal.observe_calendar(self.temp.name, dict(positions=[f.position]))
        state = journal.attach_evidence(self.temp.name, self.binding)
        self.assertEqual(state['monthly_performance'][0]['mean_actual_net_r'], '-2.099')
        request = evidence.evaluation_request(state['jobs'][0], {f.position['id']:self.binding}, 'a'*32)
        self.assertEqual(self.binding['fills'], sorted(f.history['fills'].values(), key=lambda row:row['id']))
        self.assertEqual(self.binding['outcome']['commission_usdt'], '0.099')
        for text in ('Realized PNL sebelum biaya -2', 'komisi 0.099', 'funding bertanda 0',
                'Fills riil', 'NOT_RUN', 'BELUM CUKUP BUKTI', 'jangan anggap nol'):
            self.assertIn(text, request['prompt'])
        half = copy.deepcopy(self.binding)
        half['execution']['quantity'] = '0.5'
        self.assertEqual(metrics.actual_reward_risk(half), '-4.198')
        half['execution']['sl'] = half['execution']['entry']
        self.assertIsNone(metrics.actual_reward_risk(half))
        # A source revision invalidates the old entry/SL denominator projection.
        revised = copy.deepcopy(f.position)
        revised['pnl_usdt'] = '-3'
        state = journal.observe_calendar(self.temp.name, dict(positions=[revised]))
        self.assertIsNone(state['monthly_performance'][0]['mean_actual_net_r'])

    def test_newest_ready_streak_precedes_single_trade_backlog_and_still_claims_once(self):
        self.seed(4)
        neuro = SimpleNamespace(require_key=Mock(), ask=Mock(side_effect=ValueError('SIMULATED_UNKNOWN_SEND')))
        actor = SimpleNamespace(neuro=neuro, allowed=lambda _:True)
        consumer = self.consumer()
        consumer.advance(actor, '2026-11-01')
        operation = neuro.ask.call_args.args[0]
        self.assertEqual(journal.read_evaluation_claim(self.temp.name, operation)['job_id'], 'losing-streak:3')
        self.assertEqual(neuro.ask.call_count, 1)
        consumer.advance(actor, '2026-11-01')
        self.assertEqual(neuro.ask.call_count, 1)

    def test_unknown_recovery_rotates_without_sending_more_or_holding_trading(self):
        self.seed(2)
        operations = [journal.claim_evaluation(self.temp.name, 'post-trade:'+str(i), 'a'*32)['operation']
            for i in range(2)]
        receipt = Mock(side_effect=ValueError('NO_NATIVE_RECEIPT'))
        consumer = self.consumer(receipt)
        neuro = SimpleNamespace(require_key=Mock(), ask=Mock())
        actor = SimpleNamespace(neuro=neuro, allowed=lambda _:True)
        for _ in range(3):
            self.assertEqual(consumer.advance(actor, '2026-11-01')['state'], 'EVALUATION_OUTCOME_UNKNOWN')
        self.assertEqual([call.args[0] for call in receipt.call_args_list], operations + operations[:1])
        neuro.ask.assert_not_called()
        neuro.require_key.assert_not_called()

    def test_superseded_aggregate_unknown_still_blocks_send_and_real_receipt_cannot_revive_findings(self):
        cases = [('losing-streak:2', ('-1','-2','-3'), 1),
            ('performance-decline:5', ('2','1','-1','1','-1','-2'), 5)]
        for job_id, pnls, revised_index in cases:
            with self.subTest(job_id=job_id), tempfile.TemporaryDirectory() as directory:
                rows = self.seed(len(pnls), pnls, directory=directory)
                claim = journal.claim_evaluation(directory, job_id, 'a'*32)
                revised = copy.deepcopy(rows[revised_index])
                revised['pnl_usdt'] = '10'
                state = journal.observe_calendar(directory, dict(positions=[revised]))
                stale = next(job for job in state['jobs'] if job['id'] == job_id)
                self.assertEqual(stale['status'], 'SUPERSEDED_HISTORY_REVISION')
                self.assertEqual(state['unresolved_evaluations'][0]['operation'], claim['operation'])
                with self.assertRaisesRegex(ValueError, 'HISTORY_REVISION_CHANGED'):
                    journal.read_evaluation_claim(directory, claim['operation'])
                neuro = SimpleNamespace(require_key=Mock(), ask=Mock())
                actor = SimpleNamespace(neuro=neuro, allowed=lambda _:True)
                consumer = self.consumer(directory=directory)
                consumer.advance(actor, '2026-11-01')
                neuro.ask.assert_not_called()
                text = 'Temuan berlaku untuk bukti semula.\nPENILAIAN SETUP: BELUM CUKUP BUKTI'
                receipt = dict(protocol=evidence.PROTOCOL, phase='POST_TRADE_EVALUATION',
                    provider='NEUROBRO_WEB', operation=claim['operation'], request_sha256=claim['request_sha256'],
                    input_evidence_sha256=claim['input_evidence_sha256'], room_id='a'*32, sent_this_request=True,
                    received_at=claim['reserved_at']+1, response_id='actual-original-request-receipt',
                    source_evidence_sha256='f'*64, assistant_text=text,
                    assistant_sha256=hashlib.sha256(text.encode()).hexdigest())
                consumer.receipt_reader = lambda _:receipt
                result = consumer.advance(actor, '2026-11-01')
                self.assertEqual(result['state'], 'RECEIPT_RETAINED_SUPERSEDED_EVIDENCE')
                neuro.ask.assert_not_called()
                state = journal.read_status(directory)
                self.assertEqual(state['unresolved_evaluations'], [])
                stale = next(job for job in state['jobs'] if job['id'] == job_id)
                self.assertEqual(stale['status'], 'SUPERSEDED_HISTORY_REVISION')
                self.assertNotIn('analyst_evaluation', stale)
                self.assertEqual(stale['experiment_outcome']['status'], 'NOT_RUN')
                # Replay retains the original source-bound receipt exactly.
                self.assertEqual(journal.complete_evaluation(directory, claim['operation'],
                    native_receipt_reader=lambda _:receipt), state)

    def test_completed_technical_rejection_is_idle_but_other_rejections_and_off_are_not(self):
        consumer = SimpleNamespace(advance=Mock(), status=dict(state='OBSERVED'))
        actor = SimpleNamespace(evaluation_consumer=consumer, db=SimpleNamespace(in_transaction=False),
            unresolved_research=lambda:False)
        for result in (dict(bot_status='REJECTED', reason='ROBOT_REQUEST_NEEDS_REVIEW'),
                dict(bot_status='OFF', wait_reason='ROBOT_OFF')):
            runtime.after_trading_tick(actor, '2026-11-01', result)
        consumer.advance.assert_not_called()
        result = dict(bot_status='REJECTED', wait_reason='ROBOT_CYCLE_COMPLETE')
        runtime.after_trading_tick(actor, '2026-11-01', result)
        consumer.advance.assert_called_once()
        actor.unresolved_research = lambda:True
        runtime.after_trading_tick(actor, '2026-11-01', result)
        self.assertEqual(consumer.advance.call_count, 1)

    def test_completed_market_hold_admits_streak_review_before_unlimited_replacement_loop(self):
        self.seed(3)
        neuro = SimpleNamespace(require_key=Mock(), ask=Mock(side_effect=ValueError('SIMULATED_UNKNOWN_SEND')))
        consumer = self.consumer()
        actor = SimpleNamespace(neuro=neuro, allowed=lambda _:True, evaluation_consumer=consumer,
            db=SimpleNamespace(in_transaction=False), unresolved_research=lambda:False)
        # Mid-analysis, generic HOLD, technical failures and open transactions
        # cannot create a historical Send under the new transaction boundary.
        for result in (dict(bot_status='ANALYZING'), dict(bot_status='HOLD'),
                dict(bot_status='HOLD', failure_code='STALE_MARKET_CONTEXT',
                    last_decision=dict(status='HOLD')),
                dict(bot_status='HOLD', last_decision=dict(status='NEEDS_REVIEW'))):
            runtime.after_trading_tick(actor, '2026-11-01', result)
        neuro.ask.assert_not_called()
        held = dict(bot_status='HOLD', failure_code=None, wait_reason=None,
            last_decision=dict(status='HOLD', failure_code=None, symbol='BTCUSDT'))
        actor.db.in_transaction = True
        runtime.after_trading_tick(actor, '2026-11-01', held)
        neuro.ask.assert_not_called()
        actor.db.in_transaction = False
        actor.unresolved_research = lambda:True
        runtime.after_trading_tick(actor, '2026-11-01', held)
        neuro.ask.assert_not_called()
        actor.unresolved_research = lambda:False
        runtime.after_trading_tick(actor, '2026-11-01', held)
        neuro.ask.assert_called_once()
        operation = neuro.ask.call_args.args[0]
        self.assertEqual(journal.read_evaluation_claim(self.temp.name, operation)['job_id'], 'losing-streak:2')
        # Future screening/analysis still gets its normal turn; this uncertain
        # operation is read-only recovered and never sent a second time.
        runtime.after_trading_tick(actor, '2026-11-01', dict(bot_status='SCREENING'))
        runtime.after_trading_tick(actor, '2026-11-01', held)
        self.assertEqual(neuro.ask.call_count, 1)

    def test_historical_display_omits_private_budget_preserves_signed_facts_and_rejects_before_upload(self):
        from agent.native_evaluation_browser import HistoricalEvaluation
        from agent.llm import web_prompt_allowed
        raw = 'ENTRY 17.25; SL 17.00.\nAnggaran risiko Office 5.123 USDT.\nPNL aktual -5 USDT; fee aktual 0.15 USDT; filled quantity 5.'
        source = dict(trade_id='original', text=raw, sha256=hashlib.sha256(raw.encode()).hexdigest())
        evaluation = HistoricalEvaluation.__new__(HistoricalEvaluation)
        evaluation.request = dict(room_id='a'*32, historical_materials=[self.binding['materials']],
            source_neurobro_replies=[source])
        original = copy.deepcopy(evaluation.request)
        evaluation.groups = {0:dict(symbol='BTCUSDT', captured_at={'15m':'original-15m','1h':'original-1h'},
            paths=[], chart_evidence=[])}
        evaluation.next_index = 0
        evaluation.owner = SimpleNamespace(native=SimpleNamespace(image_artifacts=Mock(return_value=[])),
            bridge=SimpleNamespace(uploaded_names=[]))
        delivery = SimpleNamespace(_upload=Mock(return_value=[]))
        rendered = evaluation.material_description(0)
        self.assertTrue(web_prompt_allowed(rendered))
        self.assertNotIn('5.123', rendered)
        for fact in ('17.25', '17.00', 'PNL aktual -5', 'fee aktual 0.15', 'filled quantity 5', source['sha256']):
            self.assertIn(fact, rendered)
        self.assertEqual(evaluation.request, original)
        with self.assertRaisesRegex(ValueError, 'PRIVATE_WEB_PROMPT_REJECTED'):
            evaluation.prepare_followup('Gunakan risk_usdt=5.123.', Mock(), delivery, {})
        delivery._upload.assert_not_called()
        evaluation.owner.native.image_artifacts.assert_not_called()
        evaluation.next_index = 0
        text, _, _ = evaluation.prepare_followup('Lanjutkan evaluasi bahan asli.', Mock(), delivery, {})
        self.assertTrue(web_prompt_allowed(text))
        self.assertIn('PNL aktual -5', text)
        delivery._upload.assert_called_once()


if __name__ == '__main__':
    unittest.main()
