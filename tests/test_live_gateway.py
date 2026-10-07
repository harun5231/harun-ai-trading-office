"""Offline exchange ABI/lifecycle fixtures; no credentials, sockets, or live orders."""
import ast
import copy
import hashlib
import hmac
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl

from worker.core import D, Review, number, validated_risk_target
from worker.order_gateway import _MissingOrderImplementation

# Execute only the new reviewed template in a worker-relative test namespace.
# Never import a VPS-supplied module or invoke its constructors.
source = Path(__file__).resolve().parents[1] / 'deploy/live_gateway_template.py'
namespace = dict(__name__='worker.order_gateway', __package__='worker',
                 _MissingOrderImplementation=_MissingOrderImplementation,
                 Review=Review, D=D, number=number, validated_risk_target=validated_risk_target,
                 hashlib=hashlib, hmac=hmac, datetime=datetime, timezone=timezone, Decimal=Decimal)
exec(compile(source.read_text(), str(source), 'exec'), namespace)
OrderGateway = namespace['OrderGateway']


def intent(side='LONG'):
    q, e = Decimal('4'), Decimal('100')
    sl, tp = (Decimal('99'), Decimal('103')) if side == 'LONG' else (Decimal('101'), Decimal('97'))
    fee = Decimal('0.0005')
    gross = q*abs(e-sl)
    ef, sf, tf = q*e*fee, q*sl*fee, q*tp*fee
    risk = gross+ef+sf
    reward = q*abs(tp-e)-ef-tf
    op = 'offline-operation-'+side
    return dict(intent_id=op, client_order_id='hao-'+hashlib.sha256(op.encode()).hexdigest()[:28],
        symbol='BTCUSDT', position_side='BOTH', side=side, margin_mode='CROSS', leverage=75,
        entry=dict(order_type='LIMIT', side='BUY' if side == 'LONG' else 'SELL',
                   price=str(e), quantity=str(q), time_in_force='GTC'),
        protection=dict(exit_side='SELL' if side == 'LONG' else 'BUY',
                        stop_loss=str(sl), take_profit=str(tp), working_type='MARK_PRICE'),
        risk_target_usdt='5', risk_usdt=str(risk), gross_risk_usdt=str(gross),
        entry_fee_usdt=str(ef), sl_exit_fee_usdt=str(sf), tp_exit_fee_usdt=str(tf),
        net_reward_usdt=str(reward), net_reward_risk=str(reward/risk),
        fee_evidence=dict(source='BINANCE_FUTURES_COMMISSION_RATE', symbol='BTCUSDT',
                          observed_at=datetime.now(timezone.utc).timestamp(), taker_rate=str(fee)),
        excluded_costs=['SLIPPAGE','FUNDING','GAPS'], evidence_sha256='a'*64)


class Exchange(OrderGateway):
    """Deterministic in-memory GET/POST ABI fixture, never a runtime fallback."""
    def __init__(self, fill='0', side='LONG'):
        super().__init__(api_key='fixture-key-123456', api_secret='fixture-secret-123456')
        self.command = intent(side)
        self.calls = []
        self.fill_on_post = fill
        self.entry = None
        self.algos = {}
        self.exit_orders = {}
        self.trades = []
        self.manual = []
        self.manual_orders = []
        self.manual_algos = []
        self.config = dict(canTrade=True, dualSidePosition=False, multiAssetsMargin=False)
        self.symbol_config = dict(symbol='BTCUSDT', marginType='CROSSED', leverage=75, maxNotionalValue='10000')
        self.max_leverage = 125
        self.balance = '100'
        self.fee = '0.0005'
        self.ms = int(datetime.now(timezone.utc).timestamp()*1000)
        self.hook = None
        self.algo_counter = 500
        self.trade_counter = 100
        self.bad_proof = None
        self.timeout_entry = False
        self.fail_sl = False
        self.fail_tp = False
        self.margin_code = None

    def _get_server_time(self):
        return self.ms

    def fill(self, qty):
        current = Decimal(self.entry['executedQty'])
        total = Decimal(qty)
        if total > current:
            self.trade_counter += 1
            self.trades.append(dict(id=self.trade_counter, symbol='BTCUSDT', orderId=10,
                qty=str(total-current), side=self.command['entry']['side'], positionSide='BOTH',
                time=self.ms-1000+self.trade_counter-100))
        self.entry['executedQty'] = str(total)
        self.entry['status'] = 'FILLED' if total == 4 else 'PARTIALLY_FILLED' if total else 'NEW'

    def exit(self, kind, quantity=None):
        active = [a for a in self.algos.values() if a['orderType'] == ('STOP_MARKET' if kind == 'sl' else 'TAKE_PROFIT_MARKET') and a['algoStatus'] == 'NEW']
        algo = active[-1]
        qty = Decimal(quantity or algo['quantity'])
        oid = 2000+len(self.exit_orders)
        algo.update(algoStatus='FINISHED', actualOrderId=str(oid))
        self.exit_orders[str(oid)] = dict(orderId=oid, symbol='BTCUSDT', side=self.command['protection']['exit_side'],
            positionSide='BOTH', reduceOnly=True, closePosition=False, type='MARKET',
            origQty=algo['quantity'], executedQty=str(qty), time=self.ms-2000, status='FILLED' if qty == Decimal(algo['quantity']) else 'PARTIALLY_FILLED')
        self.trade_counter += 1
        self.trades.append(dict(id=self.trade_counter, symbol='BTCUSDT', orderId=oid,
            qty=str(qty), side=self.command['protection']['exit_side'], positionSide='BOTH',
            time=self.ms-1000+self.trade_counter-100))

    def _position(self):
        own = Decimal(self.entry['executedQty']) if self.entry else Decimal(0)
        exited = sum((Decimal(o['executedQty']) for o in self.exit_orders.values()), Decimal(0))
        qty = own-exited
        rows = copy.deepcopy(self.manual)
        if qty:
            rows.append(dict(symbol='BTCUSDT', positionSide='BOTH',
                positionAmt=str(qty if self.command['side']=='LONG' else -qty),
                liquidationPrice='0'))
        return rows

    def _request(self, method, path, params):
        self.calls.append((method, path, copy.deepcopy(params)))
        if self.hook:
            self.hook(method, path, params)
        if path == '/fapi/v1/accountConfig': return copy.deepcopy(self.config)
        if path == '/fapi/v3/account': return dict(availableBalance=self.balance)
        if path == '/fapi/v3/positionRisk':
            rows = self._position()
            return [r for r in rows if r['symbol']==params['symbol']] if 'symbol' in params else rows
        if path == '/fapi/v1/openOrders': return copy.deepcopy(self.manual_orders)
        if path == '/fapi/v1/openAlgoOrders': return copy.deepcopy(self.manual_algos)
        if path == '/fapi/v1/commissionRate': return dict(symbol='BTCUSDT', takerCommissionRate=self.fee)
        if path == '/fapi/v1/leverageBracket': return [dict(symbol='BTCUSDT', brackets=[dict(initialLeverage=self.max_leverage, notionalFloor=0, notionalCap=10000)])]
        if path == '/fapi/v1/symbolConfig': return [copy.deepcopy(self.symbol_config)]
        if path == '/fapi/v1/marginType':
            if self.margin_code: raise self._ExchangeError(self.margin_code)
            return dict(code=200)
        if path == '/fapi/v1/leverage': return dict(symbol='BTCUSDT', leverage=self.symbol_config['leverage'], maxNotionalValue=self.symbol_config['maxNotionalValue'])
        if path == '/fapi/v1/order':
            if method == 'POST':
                cfg = self.command['entry']
                self.entry = dict(symbol='BTCUSDT', clientOrderId=params['newClientOrderId'],
                    orderId=10, side=cfg['side'], positionSide='BOTH', type='LIMIT',
                    timeInForce='GTC', reduceOnly=False, closePosition=False,
                    price=cfg['price'], origQty=cfg['quantity'], executedQty='0', status='NEW', time=self.ms-2000, updateTime=self.ms)
                self.fill(self.fill_on_post)
                if self.timeout_entry: raise Review('BINANCE_ORDER_OUTCOME_UNKNOWN')
                return copy.deepcopy(self.entry)
            if method == 'DELETE':
                if 'orderId' in params and params['orderId'] in self.exit_orders:
                    self.exit_orders[params['orderId']]['status'] = 'CANCELED'
                    return copy.deepcopy(self.exit_orders[params['orderId']])
                self.entry['status'] = 'CANCELED'
                return copy.deepcopy(self.entry)
            if 'orderId' in params and params['orderId'] in self.exit_orders:
                return copy.deepcopy(self.exit_orders[params['orderId']])
            if self.entry is None: raise self._ExchangeError(-2013)
            return copy.deepcopy(self.entry)
        if path == '/fapi/v1/userTrades':
            rows = self.trades
            if 'orderId' in params: rows = [r for r in rows if str(r['orderId'])==str(params['orderId'])]
            if 'fromId' in params: rows = [r for r in rows if r['id']>=params['fromId']]
            if 'startTime' in params: rows = [r for r in rows if params['startTime'] <= r['time'] <= params['endTime']]
            return copy.deepcopy(rows[:params.get('limit',1000)])
        if path == '/fapi/v1/allAlgoOrders':
            return copy.deepcopy(list(self.algos.values()))
        if path == '/fapi/v1/algoOrder':
            if method == 'POST':
                if self.fail_sl and params['type']=='STOP_MARKET': raise self._ExchangeError(-2021)
                if self.fail_tp and params['type']=='TAKE_PROFIT_MARKET': raise self._ExchangeError(-2021)
                self.algo_counter += 1
                row = dict(algoId=self.algo_counter, clientAlgoId=params['clientAlgoId'], algoType='CONDITIONAL',
                    orderType=params['type'], symbol=params['symbol'], side=params['side'], positionSide=params['positionSide'],
                    quantity=params['quantity'], triggerPrice=params['triggerPrice'], workingType=params['workingType'],
                    reduceOnly=params['reduceOnly']=='true', closePosition=False, algoStatus='NEW', actualOrderId='')
                if self.bad_proof: row.update(self.bad_proof)
                self.algos[row['clientAlgoId']] = row
                return copy.deepcopy(row)
            if method == 'DELETE':
                row = next(a for a in self.algos.values() if str(a['algoId'])==params['algoId'])
                row['algoStatus'] = 'CANCELED'
                return dict(code='200')
            if params['clientAlgoId'] not in self.algos: raise self._ExchangeError(-2013)
            return copy.deepcopy(self.algos[params['clientAlgoId']])
        raise AssertionError((method,path,params))

    def post_entries(self):
        return [c for c in self.calls if c[:2] == ('POST','/fapi/v1/order')]


class LiveGatewayTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_template_has_only_class_and_no_module_imports_or_runtime_selection(self):
        nodes = ast.parse(source.read_text()).body
        self.assertEqual(len(nodes),1)
        self.assertIsInstance(nodes[0],ast.ClassDef)
        self.assertEqual(nodes[0].name,'OrderGateway')
        self.assertIn('class OrderGateway(_MissingOrderImplementation)',source.read_text())

    def test_construct_and_status_do_not_make_requests(self):
        with patch.object(OrderGateway,'_wire',side_effect=AssertionError('network forbidden')):
            gateway = OrderGateway(api_key='fixture-key-123456',api_secret='fixture-secret-123456')
            self.assertEqual(gateway.status()['status'],'CONFIGURED')
            self.assertTrue(gateway.status()['connected'])
            self.assertEqual(OrderGateway().status()['status'],'NOT_CONFIGURED')

    def test_file_secret_pair_has_priority_and_failure_never_falls_back(self):
        with patch.dict(os.environ,{'BINANCE_API_KEY_FILE':'/unused/key','BINANCE_API_SECRET_FILE':'/unused/secret'}):
            with patch('worker.binance_private.read_secret',side_effect=['file-key-123456789','file-secret-123456789']) as read:
                gateway = OrderGateway(api_key='other-key-123456',api_secret='other-secret-123456')
                self.assertEqual(gateway.api_key,'file-key-123456789')
                self.assertEqual(read.call_count,2)
            with patch('worker.binance_private.read_secret',side_effect=ValueError('sensitive')):
                with self.assertRaisesRegex(Review,'^BINANCE_ORDER_NOT_CONFIGURED$'):
                    OrderGateway(api_key='other-key-123456',api_secret='other-secret-123456')

    def test_foreign_base_and_incomplete_credential_pair_are_rejected_locally(self):
        with self.assertRaisesRegex(Review,'ORIGIN_DENIED'):
            OrderGateway(base_url='https://example.com')
        with self.assertRaisesRegex(Review,'NOT_CONFIGURED'):
            OrderGateway(api_key='fixture-key-123456')

    def test_pending_limit_has_no_false_fill_or_protection_and_is_not_replayed(self):
        gateway = Exchange()
        result = gateway.submit(gateway.command)
        self.assertEqual(result['state'],'ENTRY_PENDING')
        self.assertEqual(result['filled_quantity'],'0')
        self.assertNotIn('first_fill_at',result)
        self.assertFalse(gateway.algos)
        with self.assertRaises(Review): gateway.submit(gateway.command)
        self.assertEqual(len(gateway.post_entries()),1)
        self.assertEqual(gateway.reconcile(gateway.command)['state'],'ENTRY_PENDING')
        self.assertEqual(len(gateway.post_entries()),1)

    def test_long_short_exact_entry_cross75_and_algo_payloads(self):
        for side in ('LONG','SHORT'):
            with self.subTest(side=side):
                gateway = Exchange(fill='4',side=side)
                result = gateway.submit(gateway.command)
                self.assertEqual(result['state'],'POSITION_PROTECTED')
                entry = gateway.post_entries()[0][2]
                self.assertEqual(entry['positionSide'],'BOTH')
                self.assertEqual(entry['type'],'LIMIT')
                self.assertEqual(entry['quantity'],'4')
                self.assertEqual(entry['price'],'100')
                self.assertEqual(entry['side'],gateway.command['entry']['side'])
                configs = [(p,q) for m,p,q in gateway.calls if m=='POST']
                self.assertIn(('/fapi/v1/marginType',dict(symbol='BTCUSDT',marginType='CROSSED')),configs)
                self.assertIn(('/fapi/v1/leverage',dict(symbol='BTCUSDT',leverage=75)),configs)
                algo_posts = [q for m,p,q in gateway.calls if m=='POST' and p=='/fapi/v1/algoOrder']
                self.assertEqual([q['type'] for q in algo_posts],['STOP_MARKET','TAKE_PROFIT_MARKET'])
                for q in algo_posts:
                    self.assertEqual(q['workingType'],'MARK_PRICE')
                    self.assertEqual(q['reduceOnly'],'true')
                    self.assertEqual(q['quantity'],'4')
                    self.assertNotIn('closePosition',q)
                    self.assertIn('clientAlgoId',q)
                    self.assertNotIn('newClientOrderId',q)
                    self.assertEqual(q['side'],gateway.command['protection']['exit_side'])

    def test_contract_price_long_short_protections_match_actual_full_or_partial_fill(self):
        for side in ('LONG', 'SHORT'):
            for fill in ('2', '4'):
                with self.subTest(side=side, fill=fill):
                    gateway = Exchange(fill=fill, side=side)
                    gateway.command['protection']['working_type'] = 'CONTRACT_PRICE'
                    result = gateway.submit(gateway.command)
                    self.assertEqual(result['state'], 'POSITION_PROTECTED')
                    self.assertEqual(result['filled_quantity'], fill)
                    self.assertTrue(result['sl_confirmed'])
                    self.assertTrue(result['tp_confirmed'])
                    posts = [params for method, path, params in gateway.calls
                             if method == 'POST' and path == '/fapi/v1/algoOrder']
                    self.assertEqual([params['type'] for params in posts], ['STOP_MARKET', 'TAKE_PROFIT_MARKET'])
                    for params in posts:
                        self.assertEqual(params['workingType'], 'CONTRACT_PRICE')
                        self.assertEqual(params['quantity'], fill)
                        self.assertEqual(params['reduceOnly'], 'true')
                        self.assertEqual(params['side'], gateway.command['protection']['exit_side'])
                        self.assertEqual(params['positionSide'], 'BOTH')
                        self.assertNotIn('closePosition', params)
                    active = [algo for algo in gateway.algos.values() if algo['algoStatus'] == 'NEW']
                    self.assertEqual(len(active), 2)
                    self.assertEqual({algo['quantity'] for algo in active}, {fill})
                    self.assertEqual({algo['workingType'] for algo in active}, {'CONTRACT_PRICE'})
                    self.assertTrue(all(algo['reduceOnly'] is True and algo['closePosition'] is False for algo in active))

    def test_contract_price_partial_growth_replaces_only_owned_legs_at_actual_quantity(self):
        gateway = Exchange(fill='2')
        gateway.command['protection']['working_type'] = 'CONTRACT_PRICE'
        first = gateway.submit(gateway.command)
        gateway.fill('4')
        result = gateway.reconcile(gateway.command)
        self.assertEqual(result['first_fill_at'], first['first_fill_at'])
        self.assertEqual(result['filled_quantity'], '4')
        active = [algo for algo in gateway.algos.values() if algo['algoStatus'] == 'NEW']
        self.assertEqual(len(active), 2)
        self.assertEqual({algo['quantity'] for algo in active}, {'4'})
        self.assertEqual({algo['workingType'] for algo in active}, {'CONTRACT_PRICE'})
        self.assertTrue(all(algo['reduceOnly'] is True for algo in active))
        self.assertEqual(len(gateway.post_entries()), 1)
        posts = [params for method, path, params in gateway.calls
                 if method == 'POST' and path == '/fapi/v1/algoOrder']
        self.assertEqual([params['quantity'] for params in posts], ['2', '2', '4', '4'])
        self.assertTrue(all(params['workingType'] == 'CONTRACT_PRICE' for params in posts))

    def test_contract_price_intent_rejects_mark_price_protection_proof(self):
        for side in ('LONG', 'SHORT'):
            with self.subTest(side=side):
                gateway = Exchange(fill='2', side=side)
                gateway.command['protection']['working_type'] = 'CONTRACT_PRICE'
                gateway.bad_proof = {'workingType': 'MARK_PRICE'}
                with self.assertRaisesRegex(Review, 'PROTECTION_OUTCOME_UNKNOWN'):
                    gateway.submit(gateway.command)
                self.assertEqual(gateway.entry['status'], 'CANCELED')
                self.assertEqual(len(gateway.post_entries()), 1)
                self.assertFalse([params for method, path, params in gateway.calls
                                  if method == 'POST' and path == '/fapi/v1/algoOrder'
                                  and params['type'] == 'TAKE_PROFIT_MARKET'])

    def test_invalid_trigger_price_basis_refused_before_any_request(self):
        for working_type in ('', 'LAST_PRICE', 'contract_price', None, True, [], {}):
            with self.subTest(working_type=working_type):
                gateway = Exchange(fill='4')
                gateway.command['protection']['working_type'] = working_type
                with self.assertRaisesRegex(Review, 'INTENT_INVALID'):
                    gateway.submit(gateway.command)
                self.assertEqual(gateway.calls, [])

    def test_partial_first_fill_stable_across_growth_restart_and_midnight(self):
        gateway = Exchange(fill='2')
        first = gateway.submit(gateway.command)
        self.assertEqual(first['state'],'POSITION_PROTECTED')
        self.assertEqual(first['filled_quantity'],'2')
        self.assertEqual({a['quantity'] for a in gateway.algos.values()},{'2'})
        gateway.ms += 24*60*60*1000
        gateway.fill('4')
        gateway.entry['updateTime'] = gateway.ms
        # A reconstructed adapter has no timestamp cache or private DB state.
        restarted = Exchange()
        for name in ('command','entry','algos','trades','exit_orders','ms','algo_counter','trade_counter'):
            setattr(restarted,name,copy.deepcopy(getattr(gateway,name)))
        result = restarted.reconcile(restarted.command)
        self.assertEqual(result['first_fill_at'],first['first_fill_at'])
        self.assertEqual(result['filled_quantity'],'4')
        active = [a for a in restarted.algos.values() if a['algoStatus']=='NEW']
        self.assertEqual({a['quantity'] for a in active},{'4'})
        self.assertEqual(len(active),2)
        writes = [c for c in restarted.calls if c[0] in ('POST','DELETE')]
        self.assertEqual([w[:2] for w in writes[:2]],[('POST','/fapi/v1/algoOrder')]*2)
        self.assertFalse(restarted.post_entries())

    def test_manual_hype_and_pending_orders_reduce_capacity_without_mutation(self):
        gateway = Exchange()
        gateway.manual = [dict(symbol='HYPEUSDT',positionSide='BOTH',positionAmt='1')]
        gateway.manual_orders = [dict(symbol='ETHUSDT',positionSide='BOTH',reduceOnly=False)]
        with self.assertRaisesRegex(Review,'CAPACITY_FULL'): gateway.submit(gateway.command)
        self.assertFalse([c for c in gateway.calls if c[0] != 'GET'])
        gateway.manual_orders = []
        self.assertEqual(gateway.submit(gateway.command)['state'],'ENTRY_PENDING')
        self.assertTrue(all(q.get('symbol')!='HYPEUSDT' for m,p,q in gateway.calls if m!='GET'))

    def test_unsupported75_mode_fee_margin_and_config_fail_before_entry(self):
        changes = [('max_leverage',50),('balance','1'),('fee','0.001'),('fee','-0.1')]
        for name,value in changes:
            with self.subTest(name=name,value=value):
                gateway=Exchange();setattr(gateway,name,value)
                with self.assertRaises(Review): gateway.submit(gateway.command)
                self.assertFalse(gateway.post_entries())
        gateway=Exchange();gateway.config['dualSidePosition']=True
        with self.assertRaises(Review): gateway.submit(gateway.command)
        self.assertFalse(gateway.post_entries())
        gateway=Exchange();gateway.symbol_config['marginType']='ISOLATED'
        with self.assertRaises(Review): gateway.submit(gateway.command)
        self.assertFalse(gateway.post_entries())

    def test_wrong_protection_parameters_never_report_success_and_cancel_own_remainder(self):
        for change in ({'reduceOnly':False},{'closePosition':True},{'workingType':'CONTRACT_PRICE'},
                       {'triggerPrice':'98'},{'quantity':'3'},{'side':'BUY'},{'positionSide':'LONG'},
                       {'symbol':'ETHUSDT'},{'orderType':'TAKE_PROFIT_MARKET'}):
            with self.subTest(change=change):
                gateway=Exchange(fill='2');gateway.bad_proof=change
                with self.assertRaisesRegex(Review,'PROTECTION_OUTCOME_UNKNOWN'): gateway.submit(gateway.command)
                self.assertEqual(gateway.entry['status'],'CANCELED')
                self.assertEqual(len(gateway.post_entries()),1)
                deletes=[q for m,p,q in gateway.calls if m=='DELETE' and p=='/fapi/v1/order']
                self.assertEqual(deletes,[dict(symbol='BTCUSDT',origClientOrderId=gateway.command['client_order_id'])])

    def test_sl_and_tp_failure_are_explicit_unknown_not_except_pass(self):
        for which in ('fail_sl','fail_tp'):
            gateway=Exchange(fill='2');setattr(gateway,which,True)
            with self.assertRaisesRegex(Review,'PROTECTION_OUTCOME_UNKNOWN'): gateway.submit(gateway.command)
            self.assertEqual(gateway.entry['status'],'CANCELED')
            self.assertEqual(len(gateway.post_entries()),1)

    def test_transport_uncertain_entry_is_never_automatically_posted_again(self):
        gateway=Exchange(fill='2');gateway.timeout_entry=True
        with self.assertRaisesRegex(Review,'OUTCOME_UNKNOWN'): gateway.submit(gateway.command)
        self.assertEqual(len(gateway.post_entries()),1)
        with self.assertRaises(Review): gateway.submit(gateway.command)
        self.assertEqual(len(gateway.post_entries()),1)

    def test_zero_fill_cancel_is_rejected_positive_fill_cancel_remains_protected(self):
        for fill in ('0','2'):
            gateway=Exchange(fill=fill);gateway.submit(gateway.command)
            gateway.entry['status']='CANCELED'
            result=gateway.reconcile(gateway.command)
            self.assertEqual(result['state'],'REJECTED' if fill=='0' else 'POSITION_PROTECTED')
            self.assertEqual(result['order_id'],'10')

    def test_verified_exit_cancels_own_unfilled_entry_and_sibling_algo_then_closed(self):
        gateway=Exchange(fill='2');gateway.submit(gateway.command)
        gateway.exit('sl')
        result=gateway.reconcile(gateway.command)
        self.assertEqual(result['state'],'CLOSED')
        self.assertEqual(gateway.entry['status'],'CANCELED')
        self.assertEqual(result['exit_order_id'],'2000')
        self.assertTrue(result['closed_at'])
        self.assertFalse([a for a in gateway.algos.values() if a['algoStatus']=='NEW'])
        self.assertEqual(len(gateway.post_entries()),1)

    def test_partial_exit_reprotects_only_remaining_owned_size(self):
        gateway=Exchange(fill='4');gateway.submit(gateway.command)
        gateway.exit('tp','1')
        result=gateway.reconcile(gateway.command)
        self.assertEqual(result['state'],'POSITION_PROTECTED')
        self.assertEqual(result['filled_quantity'],'4')
        self.assertEqual({a['quantity'] for a in gateway.algos.values() if a['algoStatus']=='NEW'},{'3'})

    def test_trigger_during_tp_creation_cancels_pending_entry_before_unknown(self):
        gateway=Exchange(fill='2')
        fired=[]
        def hook(method,path,params):
            if not fired and method=='POST' and path=='/fapi/v1/algoOrder' and params['type']=='TAKE_PROFIT_MARKET':
                fired.append(True);gateway.exit('sl')
        gateway.hook=hook
        result=gateway.submit(gateway.command)
        self.assertEqual(result['state'],'CLOSED')
        self.assertEqual(gateway.entry['status'],'CANCELED')
        self.assertFalse([a for a in gateway.algos.values() if a['algoStatus']=='NEW'])

    def test_entry_growth_during_protection_cancels_remainder_and_resizes(self):
        gateway=Exchange(fill='1')
        fired=[]
        def hook(method,path,params):
            if not fired and method=='POST' and path=='/fapi/v1/algoOrder' and params['type']=='TAKE_PROFIT_MARKET':
                fired.append(True);gateway.fill('3')
        gateway.hook=hook
        result=gateway.submit(gateway.command)
        self.assertEqual(result['state'],'POSITION_PROTECTED')
        self.assertEqual(result['filled_quantity'],'3')
        self.assertEqual(gateway.entry['status'],'CANCELED')
        self.assertEqual({a['quantity'] for a in gateway.algos.values() if a['algoStatus']=='NEW'},{'3'})
        self.assertEqual(len(gateway.post_entries()),1)

    def test_manual_reopening_or_manual_fill_never_takes_over_or_closes(self):
        gateway=Exchange(fill='2');gateway.submit(gateway.command)
        gateway.trade_counter+=1
        gateway.trades.append(dict(id=gateway.trade_counter,symbol='BTCUSDT',orderId=999,
            qty='1',side='BUY',positionSide='BOTH',time=gateway.ms))
        before=len(gateway.calls)
        with self.assertRaisesRegex(Review,'FOREIGN_EXPOSURE'):gateway.reconcile(gateway.command)
        self.assertEqual([c[2] for c in gateway.calls[before:] if c[0]!='GET'],[dict(symbol='BTCUSDT',origClientOrderId=gateway.command['client_order_id'])])

    def test_incomplete_or_missing_entry_trades_fail_without_false_first_fill(self):
        gateway=Exchange(fill='2');gateway.submit(gateway.command)
        gateway.trades.clear()
        with self.assertRaisesRegex(Review,'HISTORY_INCOMPLETE'):gateway.reconcile(gateway.command)

    def test_unsafe_intent_never_makes_any_call(self):
        for key,value in [('symbol','HYPEUSDT'),('leverage',50),('position_side','LONG'),('risk_usdt','6')]:
            gateway=Exchange();gateway.command[key]=value
            with self.assertRaisesRegex(Review,'INTENT_INVALID'):gateway.submit(gateway.command)
            self.assertFalse(gateway.calls)

    def test_official_single_symbol_bracket_object_is_accepted_and_max_notional_checked(self):
        gateway=Exchange()
        old=gateway._request
        def request(method,path,params):
            value=old(method,path,params)
            return value[0] if path=='/fapi/v1/leverageBracket' else value
        gateway._request=request
        self.assertEqual(gateway.submit(gateway.command)['state'],'ENTRY_PENDING')
        gateway=Exchange();gateway.symbol_config['maxNotionalValue']='300'
        with self.assertRaisesRegex(Review,'LEVERAGE_UNSUPPORTED'):gateway.submit(gateway.command)
        self.assertFalse(gateway.post_entries())

    def test_margin_already_cross_is_explicit_only_known_code_allowed(self):
        gateway=Exchange();gateway.margin_code=-4046
        self.assertEqual(gateway.submit(gateway.command)['state'],'ENTRY_PENDING')
        gateway=Exchange();gateway.margin_code=-2015
        with self.assertRaises(Review):gateway.submit(gateway.command)
        self.assertFalse(gateway.post_entries())

    def test_aged_entry_proofs_use_explicit_windows_and_keep_original_first_fill(self):
        gateway=Exchange(fill='2')
        first=gateway.submit(gateway.command)
        gateway.ms += 8*24*60*60*1000
        result=gateway.reconcile(gateway.command)
        self.assertEqual(result['first_fill_at'],first['first_fill_at'])
        windows=[q for m,p,q in gateway.calls if p=='/fapi/v1/userTrades']
        self.assertTrue(all('startTime' in q and 'endTime' in q for q in windows))
        self.assertTrue(all(q['endTime']-q['startTime']<7*24*60*60*1000 for q in windows))
        gateway.ms += 90*24*60*60*1000
        with self.assertRaisesRegex(Review,'HISTORY_INCOMPLETE'):
            gateway.reconcile(gateway.command)

    def test_foreign_legacy_or_manual_pending_orders_block_reconcile_without_mutation(self):
        for fill in ('0','2'):
            for field,order in [('manual_orders',dict(symbol='BTCUSDT',orderId=900,clientOrderId='legacy-tp',side='SELL',positionSide='BOTH',reduceOnly=True,closePosition=True)),
                                ('manual_algos',dict(symbol='BTCUSDT',clientAlgoId='manual-algo'))]:
                gateway=Exchange(fill=fill);gateway.submit(gateway.command)
                setattr(gateway,field,[order])
                before=len(gateway.calls)
                with self.assertRaisesRegex(Review,'FOREIGN_PENDING_ORDER'):
                    gateway.reconcile(gateway.command)
                writes=[c for c in gateway.calls[before:] if c[0]!='GET']
                self.assertEqual(writes,[('DELETE','/fapi/v1/order',dict(symbol='BTCUSDT',origClientOrderId=gateway.command['client_order_id']))])
                self.assertEqual(getattr(gateway,field),[order])

    def test_trigger_without_actual_order_id_cancels_only_own_entry_and_stays_unknown(self):
        gateway=Exchange(fill='2');gateway.submit(gateway.command)
        stop=next(a for a in gateway.algos.values() if a['orderType']=='STOP_MARKET')
        stop['algoStatus']='TRIGGERED'
        with self.assertRaisesRegex(Review,'EXIT_OUTCOME_UNKNOWN'):
            gateway.reconcile(gateway.command)
        self.assertEqual(gateway.entry['status'],'CANCELED')
        self.assertEqual(len(gateway.post_entries()),1)

    def test_closed_cancels_owned_active_actual_exit_remainders_before_result(self):
        gateway=Exchange(fill='4');gateway.submit(gateway.command)
        gateway.exit('sl','1');gateway.exit('tp','3')
        result=gateway.reconcile(gateway.command)
        self.assertEqual(result['state'],'CLOSED')
        self.assertEqual({r['status'] for r in gateway.exit_orders.values()},{'CANCELED'})
        deletes=[q for m,p,q in gateway.calls if m=='DELETE' and p=='/fapi/v1/order']
        self.assertEqual({q.get('orderId') for q in deletes},{'2000','2001'})
        self.assertFalse([a for a in gateway.algos.values() if a['algoStatus']=='NEW'])

    def test_protection_failure_attempts_safe_own_entry_cancel_even_when_query_breaks(self):
        gateway=Exchange(fill='2');gateway.fail_sl=True
        failed=[]
        def hook(method,path,params):
            if method=='POST' and path=='/fapi/v1/algoOrder': failed.append(True)
            elif failed and method=='GET' and path=='/fapi/v1/order':
                raise Review('BINANCE_ORDER_OUTCOME_UNKNOWN')
        gateway.hook=hook
        with self.assertRaises(Review):gateway.submit(gateway.command)
        self.assertEqual(gateway.entry['status'],'CANCELED')
        self.assertEqual(len([c for c in gateway.calls if c[:2]==('DELETE','/fapi/v1/order')]),1)

    def test_financial_intent_accepts_core160_digit_ratio_but_rejects_tampered_costs(self):
        gateway=Exchange()
        self.assertGreater(len(gateway.command['net_reward_risk']),64)
        gateway._validate_intent(gateway.command)
        for field in ('gross_risk_usdt','entry_fee_usdt','sl_exit_fee_usdt','tp_exit_fee_usdt','net_reward_usdt'):
            command=copy.deepcopy(gateway.command);command[field]='0'
            with self.assertRaisesRegex(Review,'INTENT_INVALID'):gateway._validate_intent(command)

    def test_growth_during_initial_trade_read_cancels_then_resamples_before_protection(self):
        gateway=Exchange(fill='1')
        grew=[]
        def hook(method,path,params):
            if not grew and path=='/fapi/v1/userTrades' and 'orderId' in params:
                grew.append(True);gateway.fill('3')
        gateway.hook=hook
        result=gateway.submit(gateway.command)
        self.assertEqual(result['state'],'POSITION_PROTECTED')
        self.assertEqual(result['filled_quantity'],'3')
        self.assertEqual(gateway.entry['status'],'CANCELED')
        self.assertEqual({a['quantity'] for a in gateway.algos.values() if a['algoStatus']=='NEW'},{'3'})
        self.assertEqual(len(gateway.post_entries()),1)

    def test_early_read_failure_cancels_proven_own_remainder_without_protection_or_replay(self):
        gateway=Exchange(fill='2')
        def hook(method,path,params):
            if path=='/fapi/v1/userTrades':raise Review('BINANCE_ORDER_HISTORY_INCOMPLETE')
        gateway.hook=hook
        with self.assertRaisesRegex(Review,'HISTORY_INCOMPLETE'):gateway.submit(gateway.command)
        self.assertEqual(gateway.entry['status'],'CANCELED')
        self.assertEqual(len(gateway.post_entries()),1)
        self.assertFalse(gateway.algos)

    def test_late_sibling_trigger_with_empty_actual_id_never_returns_closed(self):
        gateway=Exchange(fill='2');gateway.submit(gateway.command);gateway.exit('sl')
        tp=next(a for a in gateway.algos.values() if a['orderType']=='TAKE_PROFIT_MARKET')
        reads=[]
        def hook(method,path,params):
            if method=='GET' and path=='/fapi/v1/algoOrder' and params.get('clientAlgoId')==tp['clientAlgoId']:
                reads.append(True)
                if len(reads)==2:tp['algoStatus']='TRIGGERED'
        gateway.hook=hook
        with self.assertRaisesRegex(Review,'EXIT_RACE'):gateway.reconcile(gateway.command)
        self.assertEqual(gateway.entry['status'],'CANCELED')
        self.assertEqual(len(gateway.post_entries()),1)

    def test_monotonic_callback_budget_prevents_entry_if_protection_time_not_reserved(self):
        gateway=Exchange()
        clock=[0.0]
        def hook(method,path,params):
            gateway._time_left();clock[0]+=4
        gateway.hook=hook
        with patch('time.monotonic',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(Review,'CALLBACK_DEADLINE'):gateway.submit(gateway.command)
        self.assertFalse(gateway.post_entries())
        self.assertIsNone(gateway._deadline)
        self.assertLessEqual(clock[0],120)
        # A later independent callback gets a fresh budget, without replaying a
        # previous order: the earlier operation never submitted an entry.
        gateway.hook=None
        self.assertEqual(gateway.submit(gateway.command)['state'],'ENTRY_PENDING')
        self.assertEqual(len(gateway.post_entries()),1)

    def test_deadline_reserves_cleanup_and_nested_submit_reconcile_share_one_budget(self):
        gateway=Exchange(fill='2')
        clock=[0.0]
        def hook(method,path,params):
            gateway._time_left()
            if method=='POST' and path=='/fapi/v1/algoOrder':clock[0]=91
            elif getattr(gateway,'_cleanup',False):clock[0]+=3
        gateway.hook=hook
        with patch('time.monotonic',side_effect=lambda:clock[0]):
            with self.assertRaises(Review):gateway.submit(gateway.command)
        self.assertEqual(gateway.entry['status'],'CANCELED')
        self.assertEqual(len(gateway.post_entries()),1)
        self.assertLessEqual(clock[0],120)
        self.assertIsNone(gateway._deadline)
        self.assertFalse(gateway._cleanup)

    def test_first_fill_racing_zero_snapshot_is_protected_after_safe_resample(self):
        gateway=Exchange(fill='0')
        filled=[]
        def hook(method,path,params):
            if gateway.entry is not None and not filled and path=='/fapi/v3/positionRisk':
                filled.append(True);gateway.fill('2')
        gateway.hook=hook
        result=gateway.submit(gateway.command)
        self.assertEqual(result['state'],'POSITION_PROTECTED')
        self.assertEqual(result['filled_quantity'],'2')
        self.assertEqual(gateway.entry['status'],'CANCELED')
        self.assertEqual({a['quantity'] for a in gateway.algos.values() if a['algoStatus']=='NEW'},{'2'})
        self.assertEqual(len(gateway.post_entries()),1)

    def test_production_wire_posts_signed_form_without_redirect_or_retry(self):
        import io
        class Response:
            code=200
            def __init__(self):self.body=io.BytesIO(b'{"orderId":10}')
            def __enter__(self):return self
            def __exit__(self,*args):return None
            def read(self,n):return self.body.read(n)
            read1=read
        gateway=OrderGateway(api_key='fixture-key-123456',api_secret='fixture-secret-123456')
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.return_value=Response()
            with gateway._operation():
                self.assertEqual(gateway._wire('POST','/fapi/v1/order','symbol=BTCUSDT&signature=fixture',True),{'orderId':10})
            request=build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url,'https://fapi.binance.com/fapi/v1/order')
            self.assertEqual(request.data,b'symbol=BTCUSDT&signature=fixture')
            self.assertEqual(request.get_method(),'POST')
            self.assertEqual(build.return_value.open.call_count,1)
            redirect=build.call_args.args[1]
            self.assertIsNone(redirect.redirect_request(None,None,302,'x',{},'https://evil.invalid'))
            self.assertLessEqual(build.return_value.open.call_args.kwargs['timeout'],15)

    def test_production_wire_redacts_provider_errors_and_rejects_duplicate_json(self):
        import io
        class Response:
            def __init__(self,body,code):self.body=io.BytesIO(body);self.code=code
            def __enter__(self):return self
            def __exit__(self,*args):return None
            def read(self,n):return self.body.read(n)
            read1=read
        for body,code in [(b'{"code":-2015,"msg":"credential-sensitive"}',400),
                          (b'{"orderId":10,"orderId":11}',200),
                          (b'x'*2_000_001,200)]:
            gateway=OrderGateway(api_key='fixture-key-123456',api_secret='fixture-secret-123456')
            with patch('urllib.request.build_opener') as build:
                build.return_value.open.return_value=Response(body,code)
                with self.assertRaises(Review) as error:
                    gateway._wire('GET','/fapi/v1/order','symbol=BTCUSDT&signature=sensitive',True)
                self.assertNotIn('sensitive',str(error.exception))
                self.assertNotIn('https',str(error.exception))
                self.assertEqual(build.return_value.open.call_count,1)

    def test_canonical_hmac_signing_and_no_query_mutation_or_retry(self):
        gateway=OrderGateway(api_key='fixture-key-123456',api_secret='fixture-secret-123456')
        calls=[]
        def wire(method,path,query='',signed=False):
            calls.append((method,path,query,signed))
            return {'serverTime':1234567890} if path=='/fapi/v1/time' else {'ok':True}
        gateway._wire=wire
        params={'symbol':'BTCUSDT','origClientOrderId':'x:y+a'}
        gateway._request('GET','/fapi/v1/order',params)
        self.assertEqual(params,{'symbol':'BTCUSDT','origClientOrderId':'x:y+a'})
        query=calls[-1][2]
        unsigned,signature=query.rsplit('&signature=',1)
        self.assertEqual(signature,hmac.new(b'fixture-secret-123456',unsigned.encode(),hashlib.sha256).hexdigest())
        self.assertIn('origClientOrderId=x%3Ay%2Ba',query)
        self.assertEqual(dict(parse_qsl(unsigned))['recvWindow'],'5000')
        self.assertEqual(len(calls),2)
        with self.assertRaisesRegex(Review,'ENDPOINT_DENIED'):gateway._request('POST','/fapi/v1/order/test',{})
        self.assertEqual(len(calls),2)

if __name__=='__main__':unittest.main()
