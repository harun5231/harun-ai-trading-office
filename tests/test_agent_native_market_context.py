"""Native public data conversion never imports a worker or contacts providers."""
import copy
import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from agent.llm import validated_market_context
from agent.models import AgentError
from agent.native_market_context import (CANDLE_FIELDS, INTERVALS, NATIVE_CHART_KIND,
    NativeContextUnavailable, convert_native_market_context)


def native_context(now):
    server = int(now*1000)
    frames = {}
    for timeframe, interval in INTERVALS.items():
        current = server//interval*interval
        rows = []
        for index in range(202):
            opened = current-(201-index)*interval
            rows.append({'open_time': opened, 'open': '100.00', 'high': '102.00',
                'low': '99.00', 'close': '101.00', 'volume': '12.50',
                'close_time': opened+interval-1})
        frames[timeframe] = {'symbol': 'BTCUSDT', 'timeframe': timeframe, 'candles': rows}
    return {'source': 'Binance Futures', 'symbol': 'BTCUSDT', 'fetched_at': now,
        'server_time': server, 'mark_price': {'price': '101.2300', 'time': server-1},
        'timeframes': frames, 'quantity_unit': 'base_asset',
        'contract_rules': {'private_canary': 'CONTRACT_SETTINGS_NOT_FOR_MODEL'},
        'risk_constraints': {'private_canary': 'RISK_SETTINGS_NOT_FOR_MODEL'}}


class NativeMarketContextTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.context = native_context(self.now)

    def test_copies_exact_native_prices_400_closed_candles_and_bound_series_digest(self):
        before = copy.deepcopy(self.context)
        result = convert_native_market_context(self.context, now=self.now)
        self.assertEqual(self.context, before)
        self.assertEqual(result['mark_price'], '101.2300')
        self.assertEqual(result['observed_at'], self.now)
        self.assertEqual(set(result), {'symbol', 'observed_at', 'observed_at_utc', 'server_time_ms', 'mark_price', 'mark_time_ms', 'provenance', 'candles', 'running_candles', 'candle_windows'})
        for tf in INTERVALS:
            self.assertEqual(result['candles'][tf], self.context['timeframes'][tf]['candles'][-201:-1])
        raw = json.dumps({tf: self.context['timeframes'][tf]['candles'][-201:-1] for tf in INTERVALS},
            sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')
        self.assertEqual(result['provenance']['candles_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result['provenance']['chart_kind'], NATIVE_CHART_KIND)
        self.assertEqual(NATIVE_CHART_KIND, 'NATIVE_PUBLIC_KLINES_CONTEXT')
        with patch('agent.llm.time.time', return_value=self.now):
            self.assertEqual(validated_market_context(result), result)
        # Returned facts are independent values, not aliases of worker context.
        result['candles']['1h'][0]['close'] = '999'
        self.assertEqual(self.context, before)

    def test_private_and_unselected_extra_values_are_never_serialized_or_forwarded(self):
        self.context['risk_constraints']['arbitrary_object'] = object()
        self.context['unknown_private_data'] = {'credential_canary': object()}
        result = convert_native_market_context(self.context, now=self.now)
        encoded = json.dumps(result)
        self.assertNotIn('CANARY', encoded)
        self.assertNotIn('risk_constraints', encoded)
        self.assertNotIn('contract_rules', encoded)
        self.assertNotIn('credential', encoded)
        self.assertNotIn('screenshot', encoded.lower())

    def test_source_symbol_mark_timestamp_and_frame_binding_failures_are_rejected(self):
        mutations = (
            lambda row: row.update(source='SPOT'),
            lambda row: row.update(symbol='BTCUSDT\nprivate'),
            lambda row: row.update(fetched_at=self.now-181),
            lambda row: row.update(fetched_at=self.now+1),
            lambda row: row.update(server_time=True),
            lambda row: row.update(server_time=int((self.now-31)*1000)),
            lambda row: row['mark_price'].update(price='0'),
            lambda row: row['mark_price'].update(price='1e2'),
            lambda row: row['mark_price'].update(price=100),
            lambda row: row['mark_price'].update(time=int((self.now-181)*1000)),
            lambda row: row['mark_price'].update(time=row['server_time']+1),
            lambda row: row['mark_price'].update(symbol='BTCUSDT'),
            lambda row: row['timeframes']['1h'].update(symbol='ETHUSDT'),
            lambda row: row['timeframes']['1h'].update(timeframe='15m'),
            lambda row: row['timeframes'].update({'5m': row['timeframes']['15m']}))
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                row = copy.deepcopy(self.context)
                mutate(row)
                with self.assertRaises(NativeContextUnavailable):
                    convert_native_market_context(row, now=self.now)

    def test_invalid_candle_count_interval_sequence_or_prices_are_rejected(self):
        mutations = (
            lambda rows: rows.__delitem__(slice(-2, None)),
            lambda rows: rows.extend(copy.deepcopy(rows)*26),
            lambda rows: rows[0].update(open_time=rows[0]['open_time']+1),
            lambda rows: rows[0].update(close_time=rows[0]['close_time']-1),
            lambda rows: rows[3].update(open_time=rows[2]['open_time'], close_time=rows[2]['close_time']),
            lambda rows: rows[-1].update(close_time=rows[-1]['close_time']+1),
            lambda rows: rows[0].update(open_time=True),
            lambda rows: rows[0].update(low='101.50'),
            lambda rows: rows[0].update(high='100.50'),
            lambda rows: rows[0].update(volume='-1'),
            lambda rows: rows[0].update(close='NaN'),
            lambda rows: rows[0].update(private_field='NOT_EXPORTED'))
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                row = copy.deepcopy(self.context)
                mutate(row['timeframes']['1h']['candles'])
                with self.assertRaises(NativeContextUnavailable):
                    convert_native_market_context(row, now=self.now)

    def test_current_bar_must_enclose_server_and_volume_zero_is_supported(self):
        self.context['timeframes']['15m']['candles'][0]['volume'] = '0'
        result = convert_native_market_context(self.context, now=self.now)
        self.assertEqual(result['symbol'], 'BTCUSDT')
        row = copy.deepcopy(self.context)
        interval = INTERVALS['15m']
        for candle in row['timeframes']['15m']['candles']:
            candle['open_time'] -= interval
            candle['close_time'] -= interval
        with self.assertRaises(NativeContextUnavailable):
            convert_native_market_context(row, now=self.now)

    def test_native_server_clock_tolerance_does_not_reject_legitimate_ahead_observation(self):
        # Native Market.data/verify_dispatch allow a server/host offset of 30s.
        # Use a time away from interval boundaries so the captured bars are identical.
        now = int(self.now // 900) * 900 + 120
        row = native_context(now)
        row['server_time'] += 20_000
        result = convert_native_market_context(row, now=now)
        self.assertEqual(result['observed_at'], now)
        ahead_mark = copy.deepcopy(row)
        ahead_mark['mark_price']['time'] = int((now+10)*1000)
        # Unlike its later server-clock read, native Market.mark expressly
        # rejects a future mark timestamp (0 <= clock - mark.time <= 60).
        with self.assertRaises(NativeContextUnavailable):
            convert_native_market_context(ahead_mark, now=now)
        row['server_time'] += 11_000
        with self.assertRaises(NativeContextUnavailable):
            convert_native_market_context(row, now=now)

    def test_only_honest_existing_or_native_public_provenance_modes_are_admitted(self):
        value = convert_native_market_context(self.context, now=self.now)
        with patch('agent.llm.time.time', return_value=self.now):
            existing = copy.deepcopy(value)
            existing['provenance']['chart_kind'] = 'CANDLESTICK_RENDER_FROM_PUBLIC_KLINES'
            validated_market_context(existing)
            for key, altered in (('chart_kind', 'UNVERIFIED_SCREENSHOT'), ('provider', 'NEUROAPI'),
                                 ('market', 'SPOT'), ('origin', 'https://private.invalid')):
                changed = copy.deepcopy(value)
                changed['provenance'][key] = altered
                with self.assertRaises(AgentError):
                    validated_market_context(changed)

    def test_input_shapes_boolean_or_nonfinite_clocks_fail_without_worker_imports(self):
        for context in (None, [], {}, {'source': 'Binance Futures'}):
            with self.subTest(context=context), self.assertRaises(NativeContextUnavailable):
                convert_native_market_context(context, now=self.now)
        for now in (True, float('nan'), float('inf'), 0, -1, 10**1000):
            with self.subTest(now=now), self.assertRaises(NativeContextUnavailable):
                convert_native_market_context(self.context, now=now)
        source = (Path(__file__).resolve().parents[1]/'agent/native_market_context.py').read_text()
        self.assertNotIn('from worker', source)
        self.assertNotIn('import worker', source)
        self.assertNotIn('urllib', source)
        self.assertNotIn('subprocess', source)


if __name__ == '__main__':
    unittest.main()
