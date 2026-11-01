"""The actual Coordinator consumes refreshed native materials without replay.

Only bound methods are loaded; all receipts, clocks and providers are isolated
fixtures. No app/daemon, HTTP connection or gateway is started.
"""
import ast
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from worker.core import Review, DYNAMIC_RISK_MODEL

ROOT = Path(__file__).resolve().parents[1]


def method(path, name, namespace):
    tree = ast.parse((ROOT / path).read_bytes())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Coordinator')
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


class CoordinatorMaterialTests(unittest.TestCase):
    def subject(self, path, policy):
        fresh = Mock(return_value=True)
        ns = dict(request_policy=Mock(return_value=policy), DYNAMIC_RISK_MODEL=DYNAMIC_RISK_MODEL,
            completed_decision_fresh=fresh, time=SimpleNamespace(time=lambda:1000),
            Review=Review, json=json, D=Decimal)
        obj = SimpleNamespace(db=object(), market=SimpleNamespace(max_age=180),
            original_context_fresh=Mock())
        return obj, method(path, 'completed_value', ns), fresh

    def test_recent_final_v4_receipt_drains_old_initial_request_without_provider_send(self):
        for path in ('worker/robot.py', 'vps_native/worker/robot.py'):
            with self.subTest(path=path):
                obj, fn, fresh = self.subject(path, DYNAMIC_RISK_MODEL)
                cached=({'created':300, 'output':'{"decision":"HOLD"}'},
                    {'kind':'ANALYSIS', 'operation':'owned-operation', 'symbol':'ETHUSDT'})
                validate=Mock()
                self.assertEqual(fn(obj,cached,validate), {'decision':'HOLD'})
                fresh.assert_called_once_with(obj.db,'owned-operation',obj.market,'ETHUSDT')
                obj.original_context_fresh.assert_not_called()
                validate.assert_called_once_with({'decision':'HOLD'})

    def test_stale_or_unverified_final_receipt_is_not_treated_as_hold(self):
        for path in ('worker/robot.py', 'vps_native/worker/robot.py'):
            obj, fn, fresh = self.subject(path,DYNAMIC_RISK_MODEL)
            fresh.side_effect=Review('STALE_MARKET_CONTEXT')
            with self.subTest(path=path), self.assertRaisesRegex(Review,'STALE_MARKET_CONTEXT'):
                fn(obj,({'created':300,'output':'{"decision":"HOLD"}'},
                    {'kind':'ANALYSIS','operation':'owned','symbol':'ETHUSDT'}),Mock())

    def test_archived_legacy_and_screening_keep_their_original_age_rule(self):
        for path in ('worker/robot.py', 'vps_native/worker/robot.py'):
            for kind,policy in (('ANALYSIS',None),('SCREENING',DYNAMIC_RISK_MODEL)):
                obj,fn,fresh=self.subject(path,policy)
                cached=({'created':300,'output':'{}'}, {'kind':kind,'operation':'owned','symbol':'ETHUSDT'})
                with self.subTest(path=path,kind=kind), self.assertRaisesRegex(Review,'STALE_MARKET_CONTEXT'):
                    fn(obj,cached,Mock())
                fresh.assert_not_called()
