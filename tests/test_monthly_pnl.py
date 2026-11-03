"""Local synthetic income facts through the real calendar collector/cache."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from worker import pnl_calendar as calendar
from tests.test_pnl_calendar import Client, KEY, IDENTITY, income, OPEN


def ms(value):
    return int(datetime.fromisoformat(value).timestamp()) * 1000


START = ms('2026-10-01T00:00:00+07:00')
FINISH = ms('2026-11-01T00:00:00+07:00') - 1
NOW = ms('2026-11-05T12:00:00+07:00')


def produced_cache(directory, *, end=NOW):
    client = Client(incomes=[income(1, '15', 'REALIZED_PNL'), income(2, '-2'),
                            income(3, '-1', 'FUNDING_FEE'), income(4, '-2', 'INSURANCE_CLEAR'),
                            income(5, '999', 'REALIZED_PNL', at=FINISH + 1)])
    client.end = end
    result = calendar.unavailable(end_ms=end)
    for _ in range(24):
        result = calendar.collect_calendar(client, directory, max_requests=8)
    with calendar.HistoryCache(directory, IDENTITY) as cache:
        state = cache.load()
    assert calendar._covers(state['income_coverage'], START, FINISH)
    return result, state


class MonthlyIncomeTests(unittest.TestCase):
    def adapter(self):
        self.assertTrue((Path(calendar.__file__).parent / 'monthly_pnl.py').exists(),
                        'verified monthly income adapter missing')
        return importlib.import_module('worker.monthly_pnl')

    def test_real_collector_partial_calendar_yields_verified_selected_month_income(self):
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root, 0o700)
            closed, state = produced_cache(root)
            self.assertFalse(closed['complete'])
            self.assertIn('SYMBOL_DISCOVERY_NOT_EXHAUSTIVE', closed['incomplete_reasons'])
            self.assertTrue(all(day['pnl_usdt'] is None for day in closed['days']))
            adapter = self.adapter()
            key_file = Path(root) / 'synthetic-api-key'
            key_file.write_text(KEY); key_file.chmod(0o600)
            with patch.dict(os.environ, BINANCE_API_KEY_FILE=str(key_file)), \
                    patch('worker.binance_private.BinanceReadOnly', side_effect=AssertionError('no client')), \
                    patch('socket.create_connection', side_effect=AssertionError('no network')):
                result = adapter.read_month(root, '2026-10', now_ms=NOW)
            from worker.monthly_billing import summarize
            from tests.test_monthly_billing import document
            self.assertTrue(result['complete'])
            self.assertEqual(result['kind'], 'MONTHLY_NET_TRADING_INCOME')
            self.assertEqual(result['coverage'], dict(start_ms=START, end_ms=FINISH,
                                                     income_history_complete=True))
            self.assertEqual(result['components_usdt'], dict(REALIZED_PNL='15', COMMISSION='-2',
                FUNDING_FEE='-1', INSURANCE_CLEAR='-2', COMMISSION_REBATE='0', FEE_RETURN='0'))
            self.assertEqual(summarize(document(), result)['trading_net_usdt'], '10')
            self.assertEqual(len(result['days']), 31)
            self.assertEqual(calendar.calendar(state, NOW), closed)


    def test_two_complete_historical_months_use_their_own_boundaries(self):
        from decimal import Decimal
        adapter = self.adapter()
        end = ms('2026-12-05T12:00:00+07:00')
        with tempfile.TemporaryDirectory() as root, patch.object(adapter, 'read_secret', return_value=KEY):
            os.chmod(root, 0o700)
            produced_cache(root, end=end)
            for month, total, count in [('2026-10', '10', 31), ('2026-11', '999', 30)]:
                result = adapter.read_month(root, month, now_ms=end)
                self.assertTrue(result['complete'], result)
                self.assertEqual(len(result['days']), count)
                self.assertEqual(sum(Decimal(day['pnl_usdt']) for day in result['days']), Decimal(total))
                self.assertEqual(result['start_date'], month+'-01')

    def test_coverage_holes_unclosed_month_and_unfinished_pagination_are_unknown(self):
        adapter = self.adapter()
        with tempfile.TemporaryDirectory() as root, patch.object(adapter, 'read_secret', return_value=KEY):
            os.chmod(root, 0o700)
            _, baseline = produced_cache(root)
            import copy
            for intervals in ([], [[START+1, FINISH]], [[START, FINISH-1]],
                              [[START, OPEN-1], [OPEN+1, FINISH]]):
                value = copy.deepcopy(baseline)
                value['income_coverage'] = intervals
                value['income_task'] = dict(start=OPEN, end=OPEN, page=2, seen_keys=[])
                with calendar.HistoryCache(root, IDENTITY) as cache:
                    cache.save(value)
                result = adapter.read_month(root, '2026-10', now_ms=NOW)
                self.assertFalse(result['complete'])
                self.assertEqual(result['days'], [])
                self.assertIsNone(result['components_usdt'])
                self.assertIn('MONTH_INCOME_HISTORY_INCOMPLETE', result['incomplete_reasons'])
            with calendar.HistoryCache(root, IDENTITY) as cache:
                cache.save(baseline)
            result = adapter.read_month(root, '2026-10', now_ms=FINISH)
            self.assertFalse(result['complete'])
            self.assertIn('MONTH_NOT_CLOSED', result['incomplete_reasons'])

    def test_unknown_types_and_non_usdt_trading_income_fail_closed_even_for_zero(self):
        adapter = self.adapter()
        import copy
        with tempfile.TemporaryDirectory() as root, patch.object(adapter, 'read_secret', return_value=KEY):
            os.chmod(root, 0o700)
            _, baseline = produced_cache(root)
            for kind, asset, reason in [('NEW_TRADING_FEE', 'USDT', 'UNKNOWN_INCOME_TYPE'),
                                        ('NEW_TRADING_FEE', 'BNB', 'UNKNOWN_INCOME_TYPE')] + [
                    (kind, 'BNB', 'NON_USDT_TRADING_INCOME') for kind in adapter.TRADING_TYPES]:
                for amount in ('0', '-1'):
                    with self.subTest(kind=kind, asset=asset, amount=amount):
                        value = copy.deepcopy(baseline)
                        row = income(123, amount, kind, asset=asset)
                        value['incomes'][calendar._key(row, True)] = row
                        with calendar.HistoryCache(root, IDENTITY) as cache:
                            cache.save(value)
                        result = adapter.read_month(root, '2026-10', now_ms=NOW)
                        self.assertFalse(result['complete'])
                        self.assertIsNone(result['components_usdt'])
                        self.assertEqual(result['days'], [])
                        self.assertIn(reason, result['incomplete_reasons'])

    def test_signed_rebate_and_explicit_non_trading_exclusions_are_not_double_counted(self):
        adapter = self.adapter()
        from decimal import Decimal
        with tempfile.TemporaryDirectory() as root, patch.object(adapter, 'read_secret', return_value=KEY):
            os.chmod(root, 0o700)
            _, value = produced_cache(root)
            rows = [income(123, '0.25', 'COMMISSION_REBATE'),
                    income(124, '1000', 'TRANSFER'), income(125, '10', 'WELCOME_BONUS'),
                    income(126, '5', 'REFERRAL_KICKBACK', asset='BNB'),
                    income(127, '1', 'COMMISSION', at=START),
                    income(128, '-1', 'COMMISSION', at=FINISH),
                    income(129, '1000', 'UNKNOWN_OUTSIDE_MONTH', at=START-1),
                    income(130, '0.5', 'FEE_RETURN'), income(131, '100', 'API_REBATE'),
                    income(132, '1000', 'INTERNAL_TRANSFER'), income(133, '1000', 'CROSS_COLLATERAL_TRANSFER'),
                    income(134, '1000', 'STRATEGY_UMFUTURES_TRANSFER'),
                    income(135, '10', 'CONTEST_REWARD'), income(136, '10', 'BFUSD_REWARD')]
            for row in rows:
                # The collector merge deduplicates a repeated fact by its actual schema key.
                calendar._put(value, 'incomes', [row], calendar._income, START-1, FINISH)
                calendar._put(value, 'incomes', [row], calendar._income, START-1, FINISH)
            with calendar.HistoryCache(root, IDENTITY) as cache:
                cache.save(value)
            result = adapter.read_month(root, '2026-10', now_ms=NOW)
            self.assertTrue(result['complete'])
            self.assertEqual(result['components_usdt']['COMMISSION_REBATE'], '0.25')
            self.assertEqual(result['components_usdt']['COMMISSION'], '-2')
            self.assertEqual(result['components_usdt']['FEE_RETURN'], '0.5')
            self.assertEqual(result['excluded_income_types'], sorted(['REFERRAL_KICKBACK', 'TRANSFER', 'WELCOME_BONUS',
                'API_REBATE', 'INTERNAL_TRANSFER', 'CROSS_COLLATERAL_TRANSFER',
                'STRATEGY_UMFUTURES_TRANSFER', 'CONTEST_REWARD', 'BFUSD_REWARD']))
            self.assertEqual(sum(Decimal(day['pnl_usdt']) for day in result['days']), Decimal('10.75'))

    def test_private_cache_validation_identity_and_busy_lock_do_not_create_zero_profit(self):
        adapter = self.adapter()
        import json
        with tempfile.TemporaryDirectory() as root, patch.object(adapter, 'read_secret', return_value=KEY):
            os.chmod(root, 0o700)
            _, value = produced_cache(root)
            with patch.object(adapter, 'read_secret', return_value='different-synthetic-key'):
                self.assertFalse(adapter.read_month(root, '2026-10', now_ms=NOW)['complete'])
            with calendar.HistoryCache(root, IDENTITY):
                result = adapter.read_month(root, '2026-10', now_ms=NOW)
                self.assertFalse(result['complete'])
                self.assertIn('MONTH_INCOME_CACHE_UNAVAILABLE', result['incomplete_reasons'])
            path = Path(root) / calendar.CACHE_NAME
            for mutation in ('bad_schema', 'bad_number', 'duplicate_key', 'unsafe_mode'):
                import copy
                invalid = copy.deepcopy(value)
                if mutation == 'bad_schema': invalid['arbitrary'] = True
                if mutation == 'bad_number': next(iter(invalid['incomes'].values()))['income'] = 'NaN'
                path.write_text(json.dumps(invalid) if mutation != 'duplicate_key' else '{"version":1,"version":1}')
                path.chmod(0o644 if mutation == 'unsafe_mode' else 0o600)
                result = adapter.read_month(root, '2026-10', now_ms=NOW)
                self.assertFalse(result['complete'])
                self.assertEqual(result['days'], [])
                self.assertIsNone(result['components_usdt'])


if __name__ == '__main__':
    unittest.main()
