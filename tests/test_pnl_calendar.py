"""Synthetic Futures history only: closed lifecycles, privacy and pagination."""
import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from worker import pnl_calendar as pnl

END = int(datetime(2026, 10, 7, 12, tzinfo=timezone.utc).timestamp()) * 1000
OPEN = int(datetime(2026, 10, 7, 8, 18, 23, tzinfo=timezone.utc).timestamp()) * 1000 + 715
CLOSE = int(datetime(2026, 10, 7, 10, 7, 39, tzinfo=timezone.utc).timestamp()) * 1000 + 52
KEY = 'synthetic-calendar-key-never-real'
IDENTITY = hashlib.sha256(KEY.encode()).hexdigest()


def fill(identity, side, qty, price, realized='0', commission='0.01', at=OPEN, symbol='ETHUSDT', position_side='BOTH', asset='USDT'):
    return dict(symbol=symbol, id=identity, orderId=identity + 9000000000000000, side=side,
                positionSide=position_side, qty=qty, price=price, realizedPnl=realized,
                commission=commission, commissionAsset=asset, time=at)


def income(identity, amount, kind='COMMISSION', at=OPEN, symbol='ETHUSDT', asset='USDT'):
    return dict(symbol=symbol, tranId=identity, incomeType=kind, income=amount, asset=asset, time=at)


def eth():
    return [fill(1, 'BUY', '0.181', '2610', commission='0.09448200'),
            fill(2, 'SELL', '0.181', '2575.81', '-6.18839000', '0.23311080', CLOSE)]


def state(rows=None, incomes=None, *, anchor=None, covered=True):
    value = pnl._empty(IDENTITY)
    rows = rows if rows is not None else eth()
    for row in rows:
        value['fills'][pnl._key(row)] = pnl._fill(row)
    if incomes is None:
        incomes = [income(1, '-0.09448200'), income(2, '-0.23311080', at=CLOSE),
                   income(3, '-6.18839000', 'REALIZED_PNL', CLOSE)]
    for row in incomes:
        value['incomes'][pnl._key(row, True)] = pnl._income(row)
    if covered:
        value['income_coverage'] = [[pnl.BOOTSTRAP_MS, END]]
    for symbol in {row['symbol'] for row in rows}:
        value['trade_coverage'][symbol] = [[pnl.BOOTSTRAP_MS, END]]
        value['anchors'][symbol] = dict(end=END, quantities=anchor or {})
    return value


class Client:
    def __init__(self, rows=None, incomes=None):
        self._key = KEY
        self.rows = copy.deepcopy(rows if rows is not None else eth())
        self.incomes = copy.deepcopy(list(incomes if incomes is not None else state()['incomes'].values()))
        self.risks = []
        self.calls = []
        self.end = END
        self.risk_hook = None

    def sync_time(self):
        self.calls.append(('TIME', {}))

    def server_time_ms(self):
        return self.end

    def signed_get(self, path, **options):
        self.calls.append((path, copy.deepcopy(options)))
        if path == '/fapi/v3/positionRisk':
            return copy.deepcopy(self.risk_hook(self) if self.risk_hook else self.risks)
        rows = self.incomes if path == '/fapi/v1/income' else self.rows if path == '/fapi/v1/userTrades' else None
        if rows is None:
            raise AssertionError('Only official history/position GETs are allowed')
        rows = [row for row in rows if options['start_time'] <= row['time'] <= options['end_time'] and
                ('symbol' not in options or row['symbol'] == options['symbol'])]
        rows.sort(key=lambda row: (row['time'], row.get('id', row.get('tranId'))))
        limit = options['limit']
        if path == '/fapi/v1/income':
            offset = (options['page'] - 1) * limit
            return copy.deepcopy(rows[offset:offset + limit])
        return copy.deepcopy(rows[-limit:])


class PositionCalendarTests(unittest.TestCase):
    def test_reference_eth_net_matches_binance_display_and_closing_day(self):
        result = pnl.calendar(state(), END)
        row = result['positions'][0]
        self.assertEqual(row['pnl_usdt'], '-6.51598280')
        self.assertEqual(round(Decimal(row['pnl_usdt']), 2), Decimal('-6.52'))
        self.assertEqual(row['commission_usdt'], '0.32759280')
        self.assertEqual(row['entry_price'], '2610')
        self.assertEqual(row['exit_price'], '2575.81')
        self.assertEqual(row['closed_quantity'], '0.181')
        self.assertEqual(row['close_date'], '2026-10-07')
        self.assertTrue(row['complete'])
        self.assertFalse(result['complete'])
        self.assertIn('SYMBOL_DISCOVERY_NOT_EXHAUSTIVE', result['incomplete_reasons'])
        self.assertNotIn('roi', row)
        self.assertNotIn('leverage', row)
        self.assertNotIn('margin_type', row)

    def test_wib_close_after_midnight_allocates_entry_commission_to_close_day(self):
        close = int(datetime(2026, 10, 6, 17, tzinfo=timezone.utc).timestamp()) * 1000
        rows = [fill(1, 'BUY', '1', '10', at=close - 1), fill(2, 'SELL', '1', '11', '1', at=close)]
        result = pnl.calendar(state(rows, []), END)
        position = result['positions'][0]
        self.assertEqual(position['close_date'], '2026-10-07')
        self.assertEqual(position['pnl_usdt'], '0.98')
        days = {row['date']: row for row in result['days']}
        self.assertEqual(days['2026-10-07']['known_pnl_usdt'], '0.98')
        self.assertIsNone(days['2026-10-06']['pnl_usdt'])
        self.assertEqual(days['2026-10-06']['closed_positions'], 0)

    def test_losing_short_still_has_short_direction(self):
        rows = [fill(1, 'SELL', '1', '10'), fill(2, 'BUY', '1', '11', '-1', at=CLOSE)]
        row = pnl.calendar(state(rows, []), END)['positions'][0]
        self.assertEqual(row['side'], 'SHORT')
        self.assertEqual(row['pnl_usdt'], '-1.02')

    def test_scale_in_and_partial_close_only_finish_on_flat_boundary(self):
        rows = [fill(1, 'BUY', '1', '10'), fill(2, 'BUY', '1', '12', at=OPEN + 1),
                fill(3, 'SELL', '0.5', '13', '1', at=OPEN + 2),
                fill(4, 'SELL', '1.5', '14', '4.5', at=CLOSE)]
        row = pnl.calendar(state(rows, []), END)['positions'][0]
        self.assertEqual(row['entry_price'], '11')
        self.assertEqual(row['exit_price'], '13.75')
        self.assertEqual(row['closed_quantity'], '2.0')
        self.assertEqual(row['pnl_usdt'], '5.46')
        active = state(rows[:-1], [], anchor={'BOTH': '1.5'})
        self.assertEqual(pnl.calendar(active, END)['positions'], [])

    def test_reversal_allocates_realized_to_close_and_splits_commission(self):
        rows = [fill(1, 'BUY', '1', '10', commission='0.1'),
                fill(2, 'SELL', '2', '11', '1', '0.2', OPEN + 1),
                fill(3, 'BUY', '1', '9', '2', '0.1', CLOSE)]
        positions = pnl.calendar(state(rows, []), END)['positions']
        self.assertEqual([(r['side'], r['pnl_usdt']) for r in positions], [('SHORT', '1.8'), ('LONG', '0.8')])
        self.assertEqual(sum(Decimal(r['commission_usdt']) for r in positions), Decimal('0.4'))

    def test_repeating_reversal_fee_preserves_original_total_and_bounded_contract(self):
        rows = [fill(1, 'BUY', '1', '10', commission='0.1'),
                fill(2, 'SELL', '3', '11', '1', '0.1', OPEN + 1),
                fill(3, 'BUY', '2', '9', '4', '0.1', CLOSE)]
        result = pnl.calendar(state(rows, []), END)
        positions = result['positions']
        self.assertEqual(len(positions), 2)
        self.assertEqual(sum(Decimal(r['commission_usdt']) for r in positions), Decimal('0.3'))
        self.assertTrue(all(len(row['pnl_usdt']) <= 64 for row in positions))

    def test_weighted_average_repeating_price_is_bounded_without_changing_pnl(self):
        rows = [fill(1, 'BUY', '1', '10'), fill(2, 'BUY', '2', '11', at=OPEN + 1),
                fill(3, 'SELL', '3', '12', '4', at=CLOSE)]
        row = pnl.calendar(state(rows, []), END)['positions'][0]
        self.assertEqual(row['entry_price'], '10.6666666666666667')
        self.assertEqual(row['pnl_usdt'], '3.97')
        self.assertLess(len(row['entry_price']), 64)

    def test_same_millisecond_order_uses_trade_id_not_input_order(self):
        rows = [fill(3, 'SELL', '1', '11', '1'), fill(1, 'BUY', '1', '10')]
        positions = pnl.calendar(state(rows, []), END)['positions']
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]['pnl_usdt'], '0.98')

    def test_opening_before_bootstrap_does_not_fabricate_entry_or_complete_pnl(self):
        value = state([eth()[1]])
        row = pnl.calendar(value, END)['positions'][0]
        self.assertIsNone(row['opened_at'])
        self.assertIsNone(row['entry_price'])
        self.assertIsNone(row['pnl_usdt'])
        self.assertFalse(row['complete'])
        self.assertIn('OPENING_BEFORE_HISTORY_WINDOW', row['incomplete_reasons'])
        self.assertEqual(row['known_pnl_usdt'], '-6.42150080')

    def test_income_before_verified_trade_suffix_is_not_added_to_unknown_opening(self):
        value = state([eth()[1]], [income(5, '-2', 'FUNDING_FEE', OPEN - 1)])
        value['trade_coverage']['ETHUSDT'] = [[OPEN, END]]
        row = pnl.calendar(value, END)['positions'][0]
        self.assertEqual(row['known_pnl_usdt'], '-6.42150080')
        self.assertIsNone(row['funding_usdt'])

    def test_non_usdt_fee_is_unknown_net_without_exchange_rate_conversion(self):
        rows = eth(); rows[0]['commissionAsset'] = 'BNB'
        row = pnl.calendar(state(rows), END)['positions'][0]
        self.assertIsNone(row['pnl_usdt'])
        self.assertIsNone(row['commission_usdt'])
        self.assertEqual(row['excluded_commission_assets'], ['BNB'])
        self.assertIn('NON_USDT_COMMISSION', row['incomplete_reasons'])

    def test_negative_commission_rebate_increases_net_pnl(self):
        rows = [fill(1, 'BUY', '1', '10', commission='-0.01'),
                fill(2, 'SELL', '1', '11', '1', '-0.01', CLOSE)]
        row = pnl.calendar(state(rows, []), END)['positions'][0]
        self.assertEqual(row['pnl_usdt'], '1.02')
        self.assertEqual(row['commission_usdt'], '-0.02')

    def test_funding_and_insurance_attribution_requires_unique_lifecycle(self):
        extra = [income(4, '-0.02', 'FUNDING_FEE', OPEN + 1000),
                 income(5, '-0.03', 'INSURANCE_CLEAR', OPEN + 2000)]
        row = pnl.calendar(state(incomes=extra), END)['positions'][0]
        self.assertEqual(row['funding_usdt'], '-0.02')
        self.assertEqual(row['insurance_usdt'], '-0.03')
        self.assertEqual(row['pnl_usdt'], '-6.56598280')

    def test_hedge_income_cannot_be_arbitrarily_assigned_to_long_or_short(self):
        rows = [fill(1, 'BUY', '1', '10', position_side='LONG'),
                fill(2, 'SELL', '1', '10', at=OPEN + 1, position_side='SHORT'),
                fill(3, 'SELL', '1', '11', '1', at=CLOSE, position_side='LONG'),
                fill(4, 'BUY', '1', '9', '1', at=CLOSE + 1, position_side='SHORT')]
        positions = pnl.calendar(state(rows, [income(1, '-0.02', 'FUNDING_FEE', OPEN + 1000)]), END)['positions']
        self.assertEqual(len(positions), 2)
        for row in positions:
            self.assertIsNone(row['pnl_usdt'])
            self.assertIn('POSITION_INCOME_ATTRIBUTION_UNKNOWN', row['incomplete_reasons'])

    def test_income_at_reversal_boundary_cannot_be_arbitrarily_assigned(self):
        rows = [fill(1, 'BUY', '1', '10'), fill(2, 'SELL', '2', '11', '1', at=OPEN + 1),
                fill(3, 'BUY', '1', '9', '2', at=CLOSE)]
        positions = pnl.calendar(state(rows, [income(1, '-0.02', 'FUNDING_FEE', OPEN + 1)]), END)['positions']
        self.assertTrue(all(row['pnl_usdt'] is None for row in positions))

    def test_unallocated_adjustment_posted_after_close_does_not_claim_zero_insurance(self):
        value = state(incomes=[income(4, '-2', 'INSURANCE_CLEAR', CLOSE + 1)])
        result = pnl.calendar(value, END)
        row = result['positions'][0]
        self.assertIsNone(row['pnl_usdt'])
        self.assertIsNone(row['insurance_usdt'])
        self.assertFalse(row['complete'])
        self.assertEqual(result['insurance_usdt'], '-2')
        self.assertIn('POSITION_INCOME_ATTRIBUTION_UNKNOWN', row['incomplete_reasons'])

    def test_missing_income_coverage_marks_fee_net_subtotal_partial(self):
        row = pnl.calendar(state(covered=False), END)['positions'][0]
        self.assertIsNone(row['pnl_usdt'])
        self.assertEqual(row['known_pnl_usdt'], '-6.51598280')
        self.assertIn('POSITION_INCOME_HISTORY_INCOMPLETE', row['incomplete_reasons'])

    def test_history_gap_cannot_be_crossed_to_prove_old_position(self):
        value = state()
        value['trade_coverage']['ETHUSDT'] = [[pnl.BOOTSTRAP_MS, OPEN], [CLOSE + 1, END]]
        self.assertEqual(pnl.calendar(value, END)['positions'], [])

    def test_bad_hedge_quantity_anchor_is_not_published_as_closed_position(self):
        rows = [fill(1, 'BUY', '1', '11', '1', at=CLOSE, position_side='LONG')]
        result = pnl.calendar(state(rows, [], anchor={'LONG': '0.5'}), END)
        self.assertEqual(result['positions'], [])
        self.assertIn('POSITION_FILL_QUANTITY_MISMATCH', result['incomplete_reasons'])

    def test_closed_positions_before_requested_start_are_excluded(self):
        rows = [fill(1, 'BUY', '1', '10', at=pnl.START_MS - 3),
                fill(2, 'SELL', '1', '11', '1', at=pnl.START_MS - 1)]
        self.assertEqual(pnl.calendar(state(rows, []), END)['positions'], [])


class CacheReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.client = Client()

    def read(self, **options):
        return pnl.collect_calendar(self.client, self.directory, **options)

    def test_offline_get_reader_first_refresh_persists_exact_eth_without_ledger(self):
        marker = self.directory / 'ledger.sqlite3'; marker.write_bytes(b'untouched financial state')
        result = self.read()
        self.assertEqual(result['positions'][0]['pnl_usdt'], '-6.51598280')
        self.assertEqual(marker.read_bytes(), b'untouched financial state')
        self.assertTrue(all(path in ('TIME', '/fapi/v1/income', '/fapi/v1/userTrades', '/fapi/v3/positionRisk') for path, _ in self.client.calls))
        path = self.directory / pnl.CACHE_NAME
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        raw = path.read_text()
        self.assertNotIn(KEY, raw)
        self.assertNotIn(KEY, json.dumps(result))

    def test_single_history_request_service_budget_resumes_income_and_trade(self):
        outputs = [self.read(max_requests=1, budget_seconds=10) for _ in range(4)]
        self.assertTrue(any(output['positions'] for output in outputs))
        paths = [path for path, _ in self.client.calls if path != 'TIME' and path != '/fapi/v3/positionRisk']
        self.assertEqual(paths, ['/fapi/v1/income', '/fapi/v1/userTrades', '/fapi/v1/income', '/fapi/v1/userTrades'])

    def test_single_request_rotation_does_not_starve_even_number_of_symbols(self):
        self.client.incomes.append(income(4, '-0.02', symbol='BTCUSDT'))
        self.client.rows.extend([fill(3, 'BUY', '1', '10', symbol='BTCUSDT'), fill(4, 'SELL', '1', '11', '1', at=CLOSE, symbol='BTCUSDT')])
        for unused in range(8):
            self.read(max_requests=1)
        symbols = {options['symbol'] for path, options in self.client.calls if path == '/fapi/v1/userTrades'}
        self.assertEqual(symbols, {'BTCUSDT', 'ETHUSDT'})

    def test_advancing_end_does_not_starve_earliest_backfill(self):
        self.client.end = pnl.START_MS + 10 * pnl.DAY_MS
        for unused in range(12):
            self.read(max_requests=1)
            self.client.end += 60_000
        with pnl.HistoryCache(self.directory, IDENTITY) as cache:
            value = cache.load()
        self.assertTrue(any(start == pnl.BOOTSTRAP_MS for start, end in value['income_coverage']))
        self.assertTrue(any(start == pnl.BOOTSTRAP_MS for start, end in value['trade_coverage']['ETHUSDT']))

    def test_restart_resumes_cache_without_duplicate_position_or_recounting(self):
        self.read()
        result = self.read()
        self.assertEqual(len(result['positions']), 1)
        with pnl.HistoryCache(self.directory, IDENTITY) as cache:
            value = cache.load()
        self.assertEqual(len(value['fills']), 2)
        self.assertEqual(len(value['incomes']), 3)

    def test_credential_rotation_resets_scope_before_history_can_mix(self):
        self.read()
        self.client._key = 'different-synthetic-account-key'
        self.client.rows = []; self.client.incomes = []
        result = self.read()
        self.assertEqual(result['positions'], [])
        identity = hashlib.sha256(self.client._key.encode()).hexdigest()
        with pnl.HistoryCache(self.directory, identity) as cache:
            value = cache.load()
        self.assertEqual(value['fills'], {})
        self.assertEqual(value['incomes'], {})

    def test_position_snapshot_change_does_not_advance_reverse_replay_anchor(self):
        calls = [0]
        def risk(client):
            calls[0] += 1
            return [] if calls[0] == 1 else [dict(symbol='ETHUSDT', positionSide='BOTH', positionAmt='1', updateTime=END + 1)]
        self.client.risk_hook = risk
        result = self.read()
        self.assertEqual(result['positions'], [])
        self.assertIn('POSITION_SNAPSHOT_CHANGED', result['incomplete_reasons'])

    def test_zero_position_post_cutoff_update_blocks_anchor_even_when_quantities_equal(self):
        calls = [0]
        def risk(client):
            calls[0] += 1
            return [dict(symbol='ETHUSDT', positionSide='BOTH', positionAmt='0', updateTime=END - 1 if calls[0] == 1 else END + 1)]
        self.client.risk_hook = risk
        result = self.read()
        self.assertEqual(result['positions'], [])
        self.assertIn('POSITION_SNAPSHOT_CHANGED', result['incomplete_reasons'])

    def test_failed_refresh_preserves_verified_historical_position_as_partial(self):
        self.read()
        def risk(client):
            raise RuntimeError('SIGNED_URL_AND_TOKEN_MUST_NOT_LEAK')
        self.client.risk_hook = risk
        result = self.read()
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['positions'][0]['pnl_usdt'], '-6.51598280')
        self.assertNotIn('SIGNED_URL_AND_TOKEN_MUST_NOT_LEAK', json.dumps(result))
        # Cache remains intact and next healthy refresh resumes the same evidence.
        self.client.risk_hook = None
        self.assertEqual(self.read()['positions'][0]['pnl_usdt'], '-6.51598280')

    def test_existing_cache_symlink_never_reads_or_overwrites_target(self):
        target = self.directory / 'private-token'; target.write_text('secret-preserved')
        (self.directory / pnl.CACHE_NAME).symlink_to(target)
        result = self.read()
        self.assertEqual(result['status'], 'UNAVAILABLE')
        self.assertEqual(target.read_text(), 'secret-preserved')
        self.assertEqual(self.client.calls, [])

    def test_existing_cache_hardlink_refused(self):
        target = self.directory / 'hardlink-source'; target.write_text('{}'); target.chmod(0o600)
        os.link(target, self.directory / pnl.CACHE_NAME)
        result = self.read()
        self.assertEqual(result['status'], 'UNAVAILABLE')
        self.assertIn('PNL_CACHE_NOT_PRIVATE', result['incomplete_reasons'])

    def test_non_private_cache_and_parent_refused_before_network(self):
        path = self.directory / pnl.CACHE_NAME; path.write_text('{}'); path.chmod(0o644)
        self.assertEqual(self.read()['status'], 'UNAVAILABLE')
        path.unlink(); self.directory.chmod(0o755)
        self.assertEqual(self.read()['status'], 'UNAVAILABLE')
        self.assertEqual(self.client.calls, [])

    def test_corrupt_cache_is_not_silently_replaced(self):
        path = self.directory / pnl.CACHE_NAME; path.write_text('{broken'); path.chmod(0o600)
        result = self.read()
        self.assertIn('PNL_CACHE_INVALID', result['incomplete_reasons'])
        self.assertEqual(path.read_text(), '{broken')

    def test_income_pagination_resumes_on_restart_and_deduplicates(self):
        with patch.object(pnl, 'PAGE_SIZE', 2):
            self.read(max_requests=1)
            self.read(max_requests=1)
            result = self.read(max_requests=1)
        pages = [options['page'] for path, options in self.client.calls if path == '/fapi/v1/income']
        self.assertEqual(pages[:2], [1, 2])
        with pnl.HistoryCache(self.directory, IDENTITY) as cache:
            self.assertEqual(len(cache.load()['incomes']), 3)

    def test_repeated_short_income_page_cannot_claim_complete_coverage(self):
        original = self.client.signed_get
        def get(path, **options):
            if path == '/fapi/v1/income' and options['page'] == 2:
                return copy.deepcopy(self.client.incomes[1:2])
            return original(path, **options)
        self.client.signed_get = get
        with patch.object(pnl, 'PAGE_SIZE', 2):
            self.read(max_requests=1)
            self.read(max_requests=1)
            result = self.read(max_requests=1)
        with pnl.HistoryCache(self.directory, IDENTITY) as cache:
            value = cache.load()
        self.assertEqual(value['income_coverage'], [])
        self.assertIn('PNL_HISTORY_PAGINATION_STALLED', result['incomplete_reasons'])

    def test_elapsed_budget_skips_post_history_risk_and_keeps_anchor_unknown(self):
        clock = [0]
        original = self.client.signed_get
        def get(path, **options):
            if path == '/fapi/v1/income':
                clock[0] = 15
            return original(path, **options)
        self.client.signed_get = get
        with patch.object(pnl.time, 'monotonic', side_effect=lambda: clock[0]):
            result = self.read(max_requests=1, budget_seconds=10)
        self.assertEqual(result['positions'], [])
        self.assertIn('HISTORY_TIME_BUDGET', result['incomplete_reasons'])
        self.assertEqual(sum(path == '/fapi/v3/positionRisk' for path, _ in self.client.calls), 1)

    def test_dense_same_ms_trade_page_is_partial_and_does_not_drop_boundary(self):
        self.client.rows = [fill(i, 'BUY', '1', '10', at=END) for i in range(1, 4)]
        self.client.incomes = [income(1, '-0.03', at=END)]
        value = pnl._empty(IDENTITY)
        value['income_coverage'] = [[pnl.BOOTSTRAP_MS, END]]
        value['trade_tasks']['ETHUSDT'] = [[END, END]]
        with patch.object(pnl, 'PAGE_SIZE', 2):
            reasons = pnl._update(self.client, value, {}, END, max_requests=4, budget_seconds=15)
        self.assertIn('DENSE_TRADE_TIMESTAMP_UNPROVEN', reasons)
        self.assertEqual(value['blocked']['ETHUSDT'], [[END, END]])
        self.assertFalse(pnl._covers(value['trade_coverage'].get('ETHUSDT', []), END, END))
        self.assertEqual(pnl.calendar(value, END)['positions'], [])

    def test_full_page_bisects_instead_of_dropping_oldest_millisecond(self):
        self.client.rows = [fill(1, 'BUY', '1', '10', at=END - 2),
                            fill(2, 'SELL', '1', '11', '1', at=END - 1)]
        self.client.incomes = [income(1, '-0.02', at=END)]
        value = pnl._empty(IDENTITY)
        value['income_coverage'] = [[pnl.BOOTSTRAP_MS, END]]
        value['trade_tasks']['ETHUSDT'] = [[END - 2, END]]
        with patch.object(pnl, 'PAGE_SIZE', 2):
            for unused in range(5):
                pnl._update(self.client, value, {}, END, max_requests=4, budget_seconds=15)
        self.assertTrue(pnl._covers(value['trade_coverage']['ETHUSDT'], END - 2, END))
        self.assertEqual(len(value['fills']), 2)

    def test_overlap_captures_late_arriving_trades_and_income(self):
        for unused in range(2):
            self.read()
        self.client.rows.extend([fill(3, 'BUY', '1', '10', at=OPEN + 1), fill(4, 'SELL', '1', '11', '1', at=CLOSE + 1)])
        self.client.incomes.extend([income(4, '-0.01', at=OPEN + 1), income(5, '-0.01', at=CLOSE + 1), income(6, '1', 'REALIZED_PNL', CLOSE + 1)])
        result = self.read()
        self.assertEqual(len(result['positions']), 1)  # Concurrent scale-in belongs to the same lifecycle.
        self.assertEqual(result['positions'][0]['closed_quantity'], '1.181')

    def test_unknown_retained_history_is_not_reported_as_zero(self):
        value = pnl._empty(IDENTITY)
        result = pnl.calendar(value, END)
        self.assertTrue(all(row['pnl_usdt'] is None for row in result['days']))
        self.assertEqual(result['status'], 'UNAVAILABLE')
        self.assertIn('HISTORY_LOADING', result['incomplete_reasons'])

    def test_private_raw_response_fields_never_enter_cache_or_api(self):
        self.client.rows[0]['private_note'] = 'PRIVATE_EXTRA_MUST_NOT_LEAK'
        self.client.incomes[0]['info'] = 'PRIVATE_EXTRA_MUST_NOT_LEAK'
        result = self.read()
        raw = (self.directory / pnl.CACHE_NAME).read_text()
        self.assertNotIn('PRIVATE_EXTRA_MUST_NOT_LEAK', raw + json.dumps(result))


if __name__ == '__main__':
    unittest.main()
