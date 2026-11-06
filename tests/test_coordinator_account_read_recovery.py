"""Coordinator account reads recover only proven clock errors, before carry writes."""
import copy
import ast
from collections import Counter
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from worker.account_state import account_state
from worker.binance_private import BinanceCheckError,BinanceReadOnly


DAY='2026-11-06'
PATHS=('/fapi/v1/openOrders','/fapi/v1/openAlgoOrders','/fapi/v3/account')
CONFIG=dict(status='BINANCE_CONNECTED',position_mode='ONE_WAY',multi_assets_margin=False,
    can_trade=True,usdt_wallet_balance='130.2',usdt_available_balance='125.2')


class Client:
    def __init__(self,*,checks=(),syncs=(),reads=None):
        self.checks=list(checks)
        self.syncs=list(syncs)
        self.reads={path:list(values) for path,values in (reads or {}).items()}
        self.calls=[];self.completed=set()
    def check(self):
        self.calls.append('CHECK')
        value=self.checks.pop(0) if self.checks else CONFIG
        if isinstance(value,Exception):raise value
        return copy.deepcopy(value)
    def sync_time(self):
        self.calls.append('SYNC')
        value=self.syncs.pop(0) if self.syncs else None
        if isinstance(value,Exception):raise value
    def signed_get(self,path):
        if path not in PATHS:raise AssertionError('Unexpected account read')
        self.calls.append(path)
        choices=self.reads.get(path,[])
        value=choices.pop(0) if choices else ({'positions':[]} if path==PATHS[-1] else [])
        if isinstance(value,Exception):raise value
        self.completed.add(path)
        return copy.deepcopy(value)


class Store:
    def __init__(self):self.calls=[]
    def reconcile_carry(self,client,today,running):
        if client.completed!=set(PATHS):raise AssertionError('Carry before complete account reads')
        self.calls.append(('CARRY',today,running))
    def entries(self,today):self.calls.append(('ENTRIES',today));return 0
    def available_slots(self,running,symbols,today,*,pending_symbols):
        self.calls.append(('SLOTS',running,symbols,today,pending_symbols));return 2-running


class CoordinatorAccountReadRecoveryTests(unittest.TestCase):
    def test_actual_native_coordinator_account_import_contract_remains_available(self):
        from worker import account_state as account_module
        native=Path(__file__).resolve().parents[1]/'vps_native/worker/robot.py'
        tree=ast.parse(native.read_text())
        imports=[alias.name for node in tree.body if isinstance(node,ast.ImportFrom)
            and node.module=='account_state' for alias in node.names]
        self.assertIn('MAX_REPLACEMENTS',imports)
        for name in imports:
            with self.subTest(name=name):self.assertTrue(hasattr(account_module,name))
        # Compatibility cannot reintroduce a replacement limit into production.
        self.assertFalse(any(isinstance(node,ast.Name) and node.id=='MAX_REPLACEMENTS'
            for node in ast.walk(tree)))

    def test_first_config_clock_error_retries_only_check_then_completes_once(self):
        client=Client(checks=[BinanceCheckError('BINANCE_CLOCK_ERROR')]);store=Store()
        value=account_state(client,store,DAY)
        self.assertEqual(client.calls,['CHECK','CHECK','SYNC',*PATHS])
        self.assertEqual((value['running_positions'],value['available_slots'],value['bot_entries_today']),(0,2,0))
        self.assertEqual((value['usdt_wallet_balance'],value['usdt_available_balance']),('130.2','125.2'))
        self.assertEqual(sum(call[0]=='CARRY' for call in store.calls),1)

    def test_each_direct_get_recovers_one_clock_error_without_replaying_prior_reads(self):
        for path in PATHS:
            with self.subTest(path=path):
                client=Client(reads={path:[BinanceCheckError('BINANCE_CLOCK_ERROR')]});store=Store()
                account_state(client,store,DAY)
                expected=['CHECK','SYNC']
                for current in PATHS:
                    expected.extend([current,'SYNC',current] if current==path else [current])
                self.assertEqual(client.calls,expected)
                self.assertEqual(Counter(client.calls)['CHECK'],1)
                self.assertEqual(sum(call[0]=='CARRY' for call in store.calls),1)

    def test_explicit_time_sync_recovers_one_clock_error_before_any_get_or_carry(self):
        client=Client(syncs=[BinanceCheckError('BINANCE_CLOCK_ERROR')]);store=Store()
        value=account_state(client,store,DAY)
        self.assertEqual(client.calls,['CHECK','SYNC','SYNC',*PATHS])
        self.assertEqual(value['available_slots'],2)
        self.assertEqual(sum(call[0]=='CARRY' for call in store.calls),1)

    def test_repeated_time_sync_clock_error_fails_closed_before_gets_and_carry(self):
        client=Client(syncs=[BinanceCheckError('BINANCE_CLOCK_ERROR') for _ in range(2)])
        store=Store()
        with self.assertLogs('worker.account_state',level='WARNING') as captured:
            with self.assertRaisesRegex(BinanceCheckError,'^BINANCE_CLOCK_ERROR$'):
                account_state(client,store,DAY)
        self.assertEqual(client.calls,['CHECK','SYNC','SYNC'])
        self.assertEqual(store.calls,[])
        self.assertEqual(captured.output,[
            'WARNING:worker.account_state:ROBOT_ACCOUNT_READ_FAILED phase=TIME_SYNC code=BINANCE_CLOCK_ERROR'])

    def test_actual_readonly_client_recovers_expired_check_clock_without_relaxing_window(self):
        client=object.__new__(BinanceReadOnly)
        client._key='fixture';client._secret='fixture';client._server=None;client._synced=None
        elapsed=[0.0];requests=[];completed=set()
        client._clock=lambda:elapsed[0]
        def transport(url,headers):
            path=urlsplit(url).path;requests.append(path)
            elapsed[0]+=31 if path==PATHS[-1] and requests.count(path)==1 else .1
            if path=='/fapi/v1/time':return {'serverTime':1793952000000}
            if path=='/fapi/v1/accountConfig':
                return dict(canTrade=True,dualSidePosition=False,multiAssetsMargin=False)
            completed.add(path)
            if path==PATHS[-1]:return dict(assets=[],positions=[])
            return []
        client._transport=transport
        client.completed=completed
        store=Store()
        value=account_state(client,store,DAY)
        # The first 31-second account GET expires the unchanged 30-second
        # signing window. Only configuration is reread; carry occurs once.
        self.assertEqual(requests,['/fapi/v1/time',PATHS[-1],'/fapi/v1/time',PATHS[-1],
            '/fapi/v1/accountConfig','/fapi/v1/time',*PATHS])
        self.assertEqual(value['available_slots'],2)
        self.assertEqual(sum(call[0]=='CARRY' for call in store.calls),1)

    def test_repeated_clock_errors_fail_closed_after_exactly_one_retry_before_carry(self):
        for phase,path in [('CONFIG',None),*zip(('OPEN_ORDERS','OPEN_ALGO_ORDERS','ACCOUNT'),PATHS)]:
            with self.subTest(phase=phase):
                errors=[BinanceCheckError('BINANCE_CLOCK_ERROR') for _ in range(2)]
                client=Client(checks=errors if path is None else (),reads={path:errors} if path else None)
                store=Store()
                with self.assertLogs('worker.account_state',level='WARNING') as captured:
                    with self.assertRaisesRegex(BinanceCheckError,'^BINANCE_CLOCK_ERROR$'):
                        account_state(client,store,DAY)
                self.assertEqual(Counter(client.calls)['CHECK' if path is None else path],2)
                self.assertEqual(store.calls,[])
                self.assertEqual(captured.output,[
                    'WARNING:worker.account_state:ROBOT_ACCOUNT_READ_FAILED phase='+phase+' code=BINANCE_CLOCK_ERROR'])

    def test_nonclock_failures_are_not_retried_and_never_write_carry(self):
        for reason in ('BINANCE_AUTH_FAILED','BINANCE_PERMISSION_DENIED','BINANCE_ACCOUNT_UNAVAILABLE'):
            for path in (None,'SYNC',*PATHS):
                with self.subTest(reason=reason,path=path):
                    error=BinanceCheckError(reason)
                    client=Client(checks=[error] if path is None else (),
                        syncs=[error] if path=='SYNC' else (),reads={path:[error]} if path in PATHS else None)
                    store=Store()
                    with self.assertLogs('worker.account_state',level='WARNING') as captured:
                        with self.assertRaisesRegex(BinanceCheckError,'^'+reason+'$'):
                            account_state(client,store,DAY)
                    self.assertEqual(Counter(client.calls)['CHECK' if path is None else path],1)
                    self.assertEqual(store.calls,[])
                    if path=='SYNC':
                        self.assertEqual(client.calls,['CHECK','SYNC'])
                        self.assertEqual(captured.output,[
                            'WARNING:worker.account_state:ROBOT_ACCOUNT_READ_FAILED phase=TIME_SYNC code='+reason])

    def test_logs_never_include_untrusted_exception_text_and_preserve_original_exception(self):
        error=RuntimeError('https://private.invalid/?signature=do-not-log api-key=do-not-log')
        client=Client(reads={PATHS[-1]:[error]});store=Store()
        with self.assertLogs('worker.account_state',level='WARNING') as captured:
            with self.assertRaises(RuntimeError) as raised:account_state(client,store,DAY)
        self.assertIs(raised.exception,error)
        self.assertEqual(captured.output,[
            'WARNING:worker.account_state:ROBOT_ACCOUNT_READ_FAILED phase=ACCOUNT code=ROBOT_ACCOUNT_UNAVAILABLE'])
        self.assertEqual(store.calls,[])

    def test_invalid_configuration_and_pending_rows_still_fail_before_carry(self):
        for client in (Client(checks=[{**CONFIG,'position_mode':'HEDGE'}]),
                Client(reads={PATHS[0]:[{'reduceOnly':'false'}]})):
            with self.subTest(client=client):
                store=Store()
                with self.assertLogs('worker.account_state',level='WARNING'):
                    with self.assertRaisesRegex(Exception,'^ROBOT_ACCOUNT_UNAVAILABLE$'):
                        account_state(client,store,DAY)
                self.assertEqual(Counter(client.calls)['CHECK'],1)
                self.assertEqual(store.calls,[])


if __name__=='__main__':unittest.main()
