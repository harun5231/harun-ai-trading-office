"""Offline strict admission for the separate native five-hour review namespace."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent import native_pending_protocol as protocol
from worker import neuroapi
from worker.core import Review


INTENT = hashlib.sha256(b'owned-intent-fixture').hexdigest()
OPERATION = 'pending-review-v1:' + INTENT[:32] + ':1'
NOW = 1_792_241_234.0


class NotSubmitted(Exception):
    pass


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


def context(now=NOW):
    server = int(now * 1000)
    result = {'source': 'Binance Futures', 'symbol': 'BTCUSDT',
        'fetched_at': now - 1, 'server_time': server - 1000,
        'mark_price': {'price': '100', 'time': server - 1000},
        'pending_review': {'intent_ref': INTENT, 'symbol': 'BTCUSDT',
            'entry_created_at': utc(now - 5 * 3600 - 60)}, 'timeframes': {}}
    for timeframe, interval in (('1h', 3_600_000), ('15m', 900_000)):
        last = (server - 1000) // interval * interval
        candles = []
        for index in range(20):
            opened = last - (19 - index) * interval
            candles.append({'open_time': opened, 'close_time': opened + interval - 1,
                'open': '100', 'high': '102', 'low': '99', 'close': '101', 'volume': '10'})
        result['timeframes'][timeframe] = {'symbol': 'BTCUSDT',
            'timeframe': timeframe, 'candles': candles}
    return result


def payload(now=NOW):
    return {'operation': OPERATION, 'output_schema': copy.deepcopy(protocol.REVIEW_SCHEMA),
        'context': context(now), 'marker': 'native-marker', 'request_sha256': 'a' * 64,
        'prompt': 'Keep original pending-LIMIT review prompt.'}


def evidence(now=NOW):
    return [{'source': 'NATIVE_CHART_SOURCE', 'symbol': 'BTCUSDT', 'timeframe': tf,
        'captured_at': utc(now), 'sha256': char * 64, 'bytes': 2048,
        'attachment_id': 'native-attachment-' + tf}
        for tf, char in (('1h', 'a'), ('15m', 'b'))]


def artifacts(now=NOW):
    rows = []
    for index, witness in enumerate(evidence(now)):
        raw = bytes([index + 1]) * witness['bytes']
        witness = {key: value for key, value in witness.items() if key != 'attachment_id'}
        witness['sha256'] = hashlib.sha256(raw).hexdigest()
        rows.append((None, raw, witness))
    return rows


def source_namespace():
    return {'__name__': 'worker.research_provider', 'Review': Review,
        'validate_chart_evidence': Mock(return_value='legacy-charts'),
        'CHART_SOURCE': 'NATIVE_CHART_SOURCE', 'MAX_CHART_BYTES': 4 * 1024 * 1024}


class NativePendingProtocolTests(unittest.TestCase):
    def setUp(self):
        # Installers deliberately modify the actual native API module, just as
        # native research_provider/source_evidence resolves it at call time.
        self.canonical_patch = patch.object(neuroapi, 'canonical_output', neuroapi.canonical_output)
        self.canonical_patch.start()
        self.addCleanup(self.canonical_patch.stop)

    def test_exact_schema_only_no_prices_or_side_or_extensible_result(self):
        self.assertTrue(protocol.is_pending_schema(copy.deepcopy(protocol.REVIEW_SCHEMA)))
        for change in (lambda v: v.update(additionalProperties=True),
                       lambda v: v['properties'].update(limit_entry={'type': 'number'}),
                       lambda v: v['required'].append('side'),
                       lambda v: v['properties']['decision']['enum'].append('LONG'),
                       lambda v: v.pop('type')):
            schema = copy.deepcopy(protocol.REVIEW_SCHEMA)
            change(schema)
            self.assertFalse(protocol.is_pending_schema(schema))
        self.assertFalse(protocol.is_pending_schema({'required': ['symbol', 'order_ref', 'decision']}))

    def test_request_namespace_is_bound_to_exact_intent_symbol_and_positive_sequence(self):
        with patch.object(protocol.time, 'time', return_value=NOW):
            self.assertTrue(protocol.is_pending_browser_request(payload()))
            cases = [None, 'pending-review-v1:' + 'a' * 32 + ':1',
                'pending-review-v1:' + INTENT[:32] + ':0',
                'pending-review-v1:' + INTENT[:32] + ':01',
                'pending-review-v1:' + INTENT[:32] + ':1000000000',
                '2026-10-17:analysis-v9:BTCUSDT', OPERATION + '\n']
            for operation in cases:
                value = payload();value['operation'] = operation
                with self.subTest(operation=operation):
                    self.assertFalse(protocol.is_pending_browser_request(value))
            for changed in ({'symbol': 'ETHUSDT'}, {'intent_ref': 'A' * 64}, {'unknown': None}):
                value = payload();value['context']['pending_review'].update(changed)
                self.assertFalse(protocol.is_pending_browser_request(value))

    def test_pending_metadata_requires_utc_existing_entry_and_closed_keys(self):
        with patch.object(protocol.time, 'time', return_value=NOW):
            for value in (utc(NOW + 1), utc(NOW).removesuffix('Z'),
                          utc(NOW).replace('Z', '+01:00'), 0, True, '2026-02-30T00:00:00Z'):
                request = payload();request['context']['pending_review']['entry_created_at'] = value
                with self.subTest(value=value):
                    self.assertFalse(protocol.is_pending_browser_request(request))
            request = payload();request['context']['pending_review']['price'] = '1'
            self.assertFalse(protocol.is_pending_browser_request(request))

    def test_native_market_facts_reject_stale_epochs_future_or_invented_candles(self):
        mutations = [lambda c: c.update(fetched_at=NOW - 181),
            lambda c: c.update(fetched_at=float('nan')),
            lambda c: c.update(fetched_at=True),
            lambda c: c.update(server_time=int((NOW + 40) * 1000)),
            lambda c: c['timeframes']['1h'].update(symbol='ETHUSDT'),
            lambda c: c['timeframes']['1h']['candles'].pop(),
            lambda c: c['timeframes']['1h']['candles'][-1].update(open='999'),
            lambda c: c['timeframes']['1h']['candles'][-1].update(close='NaN'),
            lambda c: c['timeframes']['1h']['candles'][-1].update(open_time=True),
            lambda c: c['timeframes']['1h']['candles'][-1].update(unknown='1'),
            lambda c: c['timeframes']['1h']['candles'][0].update(close_time=123),
            lambda c: c['timeframes']['1h']['candles'][0].update(volume=-1)]
        with patch.object(protocol.time, 'time', return_value=NOW):
            for change in mutations:
                value = payload();change(value['context'])
                with self.subTest(change=change):
                    self.assertFalse(protocol.is_pending_browser_request(value))

    def test_dialogue_phase_only_new_schema_context_and_unchanged_legacy_phase(self):
        with patch.object(protocol.time, 'time', return_value=NOW):
            request = payload();required = request['output_schema']['required']
            self.assertEqual(protocol.dialogue_phase_for_payload(request, required), 'PENDING_REVIEW')
            self.assertEqual(protocol.dialogue_phase_for_payload({}, ['symbols']), 'SCREENING')
            self.assertEqual(protocol.dialogue_phase_for_payload({}, ['symbol', 'side']), 'ANALYSIS')
            request['context']['pending_review']['symbol'] = 'ETHUSDT'
            self.assertEqual(protocol.dialogue_phase_for_payload(request, required), 'ANALYSIS')

    def test_dom_original_unchanged_and_new_review_requires_two_native_chart_artifacts(self):
        original = Mock(return_value='LEGACY_NATIVE_RESULT')
        namespace = {'request_kind': original, 'NotSubmitted': NotSubmitted}
        protocol.install_dom_protocol(namespace)
        first = namespace['request_kind'];protocol.install_dom_protocol(namespace)
        self.assertIs(namespace['request_kind'], first)
        legacy = {'output_schema': {'required': ['symbols']}}
        self.assertEqual(first(legacy, []), 'LEGACY_NATIVE_RESULT')
        original.assert_called_once_with(legacy, [])
        with patch.object(protocol.time, 'time', return_value=NOW):
            self.assertEqual(first(payload(), artifacts()), 'PENDING_REVIEW')
            for changed in (artifacts()[:1], []):
                with self.assertRaises(NotSubmitted):first(payload(), changed)
            for field, value in (('symbol', 'ETHUSDT'), ('timeframe', '1H'),
                                 ('sha256', 'a' * 64), ('bytes', True)):
                changed = artifacts();changed[0][2][field] = value
                with self.subTest(field=field), self.assertRaises(NotSubmitted):
                    first(payload(), changed)
            changed = artifacts();changed[1][2]['timeframe'] = '1h'
            with self.assertRaises(NotSubmitted):first(payload(), changed)
        self.assertEqual(original.call_count, 1)

    def test_canonical_new_decision_is_only_source_fields_and_legacy_outputs_identical(self):
        old = neuroapi.canonical_output
        screening = {'symbols': ['BTCUSDT']}
        expected = old(screening, neuroapi.SCREEN_ONE_SCHEMA, {'BTCUSDT': {}})
        namespace = source_namespace();protocol.install_source_protocol(namespace)
        answer = {'symbol': 'BTCUSDT', 'order_ref': INTENT, 'decision': 'BATALKAN'}
        self.assertEqual(json.loads(neuroapi.canonical_output(answer, protocol.REVIEW_SCHEMA)), answer)
        self.assertEqual(neuroapi.canonical_output(screening, neuroapi.SCREEN_ONE_SCHEMA,
            {'BTCUSDT': {}}), expected)
        for changed in ({'decision': 'CANCEL'}, {'decision': True}, {'order_ref': 'x' * 64},
                        {'symbol': 'BTC/USDT'}, {'limit_entry': '1'}, {'side': 'LONG'}):
            value = dict(answer);value.update(changed)
            with self.subTest(changed=changed), self.assertRaises(Review):
                neuroapi.canonical_output(value, protocol.REVIEW_SCHEMA)

    def test_source_chart_original_delegate_is_exact_and_installer_idempotent(self):
        namespace = source_namespace();original = namespace['validate_chart_evidence']
        protocol.install_source_protocol(namespace);first = namespace['validate_chart_evidence']
        protocol.install_source_protocol(namespace)
        self.assertIs(namespace['validate_chart_evidence'], first)
        args = ([], neuroapi.SCREEN_ONE_SCHEMA, None, NOW, NOW, 180)
        self.assertEqual(first(*args), 'legacy-charts');original.assert_called_once_with(*args)
        self.assertEqual(first(evidence(), protocol.REVIEW_SCHEMA, context(), NOW, NOW + 1, 180), evidence())

    def test_chart_receipt_has_exact_native_fields_hashes_attachments_source_and_freshness(self):
        namespace = source_namespace();protocol.install_source_protocol(namespace)
        validate = namespace['validate_chart_evidence']
        changes = [lambda r: r[0].update(source='UNTRUSTED'),
            lambda r: r[0].update(symbol='ETHUSDT'),
            lambda r: r[0].update(captured_at=utc(NOW + 2)),
            lambda r: r[0].update(captured_at=utc(NOW - 6)),
            lambda r: r[0].update(captured_at=utc(NOW).removesuffix('Z')),
            lambda r: r[0].update(bytes=True), lambda r: r[0].update(bytes=4 * 1024 * 1024 + 1),
            lambda r: r[0].update(sha256='A' * 64),
            lambda r: r[0].update(attachment_id='private/unsafe'),
            lambda r: r[0].update(unknown=None),
            lambda r: r[1].update(sha256='a' * 64),
            lambda r: r[1].update(attachment_id=r[0]['attachment_id']),
            lambda r: r[1].update(timeframe='1h')]
        for change in changes:
            rows = evidence();change(rows)
            with self.subTest(change=change), self.assertRaises(Review):
                validate(rows, protocol.REVIEW_SCHEMA, context(), NOW, NOW + 1, 180)
        for created, received, age in ((NOW, NOW + 181, 180), (NOW, NOW - 1, 180),
                                      (True, NOW, 180), (NOW, NOW, True), (NOW, NOW, 301)):
            with self.assertRaises(Review):
                validate(evidence(), protocol.REVIEW_SCHEMA, context(), created, received, age)

    def test_historical_completed_receipt_validates_at_native_receive_time_not_wall_clock(self):
        namespace = source_namespace();protocol.install_source_protocol(namespace)
        with patch.object(protocol.time, 'time', return_value=NOW + 86400):
            self.assertEqual(namespace['validate_chart_evidence'](evidence(), protocol.REVIEW_SCHEMA,
                context(), NOW, NOW + 1, 180), evidence())

    def test_worker_runs_native_first_and_only_new_namespace_reaches_new_verifier(self):
        original = Mock(return_value=False)
        alias = neuroapi.canonical_output
        namespace = {'__name__': 'worker.neurobrowser', 'Review': Review,
            'verify_dispatch': original, 'canonical_output': alias}
        protocol.install_worker_browser_protocol(namespace)
        check = namespace['verify_dispatch'];protocol.install_worker_browser_protocol(namespace)
        self.assertIs(namespace['verify_dispatch'], check)
        self.assertIs(namespace['canonical_output'], neuroapi.canonical_output)
        calls = []
        original.side_effect = lambda *a, **k: calls.append('legacy') or False
        verifier = Mock(side_effect=lambda *a, **k: calls.append('pending') or True)
        module = SimpleNamespace(verify_pending_dispatch=verifier)
        with patch.dict(sys.modules, {'agent.native_pending_review': module}):
            self.assertTrue(check(None, OPERATION, 'marker', 'a' * 64, '2026-10-17', NOW))
            self.assertEqual(calls, ['legacy', 'pending'])
            verifier.assert_called_once_with(None, OPERATION, 'marker', 'a' * 64,
                '2026-10-17', NOW, max_age=180)
            verifier.reset_mock();calls.clear()
            self.assertFalse(check(None, '2026-10-17:analysis-v9:BTCUSDT', '', '', '', NOW))
            self.assertEqual(calls, ['legacy']);verifier.assert_not_called()
            self.assertFalse(check(None, 'pending-review-v1:malformed', '', '', '', NOW))
            verifier.assert_not_called()

    def test_new_dispatch_cannot_bypass_current_on_ownership_or_verifier_exception(self):
        original = Mock(return_value=True)
        namespace = {'__name__': 'worker.neurobrowser', 'Review': Review,
            'verify_dispatch': original, 'canonical_output': neuroapi.canonical_output}
        protocol.install_worker_browser_protocol(namespace)
        check = namespace['verify_dispatch']
        # The dedicated verifier is the sole new namespace authority, even if a
        # native checker was accidentally made permissive for an unknown job.
        for result in (False, 1, None):
            verifier = Mock(return_value=result)
            with patch.dict(sys.modules, {'agent.native_pending_review':
                SimpleNamespace(verify_pending_dispatch=verifier)}):
                self.assertFalse(check(None, OPERATION, '', '', '', NOW))
        verifier = Mock(side_effect=Review('ROBOT_OFF_OR_INTENT_NOT_OWNED'))
        with patch.dict(sys.modules, {'agent.native_pending_review':
            SimpleNamespace(verify_pending_dispatch=verifier)}):
            self.assertFalse(check(None, OPERATION, '', '', '', NOW))
        self.assertTrue(check(None, 'legacy-op', '', '', '', NOW))

    def test_unsafe_native_module_or_missing_original_refuses_install_without_app_actions(self):
        for namespace in ({}, {'request_kind': Mock()}, {'NotSubmitted': NotSubmitted}):
            with self.assertRaises(ValueError):protocol.install_dom_protocol(namespace)
        namespace = source_namespace();namespace['__name__'] = 'worker;injected.neurobrowser'
        with self.assertRaises(ValueError):protocol.install_source_protocol(namespace)
        for value in (None, 0, True, '4'):
            namespace = source_namespace();namespace['MAX_CHART_BYTES'] = value
            with self.assertRaises(ValueError):protocol.install_source_protocol(namespace)


if __name__ == '__main__':
    unittest.main()
