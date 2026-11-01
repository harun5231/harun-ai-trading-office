"""Capture costs from existing proven gateway exits, never symbol-only events."""
import copy
from decimal import Decimal
import sqlite3
import time
import unittest

from worker import robot
from worker.dynamic_market import execution_samples,record_execution
from worker.core import Review
from worker.research_guard import gateway_mutations
from tests import test_native_gateway_owner_revision as native
from tests.test_coordinator_protection_revision import native_coordinator


class ProvenExitExchange(native.gateway.OrderGateway):
    def __init__(self,intent,*,foreign_trade=False,missing_history=False,quantity=None):
        self.intent=copy.deepcopy(intent);self.calls=[];self._connected=True
        self.now=int(time.time()*1000);self.first=self.now-4000
        self.foreign_trade,self.missing_history=foreign_trade,missing_history
        q=intent['entry']['quantity'];exit_qty=quantity or q
        self.partial=Decimal(exit_qty)<Decimal(q)
        self.entry=dict(symbol=intent['symbol'],clientOrderId=intent['client_order_id'],
            side=intent['entry']['side'],positionSide='BOTH',type='LIMIT',timeInForce='GTC',
            reduceOnly=False,closePosition=False,price=intent['entry']['price'],origQty=q,
            executedQty=q,status='FILLED',orderId=101,time=self.now-5000)
        self.algo=dict(symbol=intent['symbol'],algoType='CONDITIONAL',orderType='STOP_MARKET',
            side=intent['protection']['exit_side'],positionSide='BOTH',
            workingType=intent['protection']['working_type'],reduceOnly=True,closePosition=False,
            clientAlgoId=self._algo_id(intent,'sl',Decimal(q)),quantity=q,
            triggerPrice=intent['protection']['stop_loss'],algoId=202,
            algoStatus='FINISHED',actualOrderId=303)
        self.exit=dict(symbol=intent['symbol'],orderId=303,side=intent['protection']['exit_side'],
            positionSide='BOTH',reduceOnly=True,closePosition=False,type='MARKET',origQty=q,
            executedQty=exit_qty,status='CANCELED'if self.partial else'FILLED',time=self.now-2000)
        self.entry_trade=dict(id=11,orderId=101,symbol=intent['symbol'],side=intent['entry']['side'],
            positionSide='BOTH',qty=q,price=intent['entry']['price'],time=self.first,
            commission='0.05',commissionAsset='USDT')
        self.exit_trade=dict(id=12,orderId=303,symbol=intent['symbol'],side=intent['protection']['exit_side'],
            positionSide='BOTH',qty=exit_qty,
            price=format(Decimal(intent['protection']['stop_loss'])-Decimal('.01'),'f'),
            time=self.now-1000,commission='0.06',commissionAsset='USDT')
    def _get_server_time(self):return self.now
    def _ensure_pair(self,intent,quantity):
        # Partial-exit test leaves remaining coverage visibly incomplete; no
        # fake claim of protected exposure is needed to verify fill economics.
        return{}
    def _request(self,method,path,params):
        self.calls.append((method,path,copy.deepcopy(params)))
        if method!='GET':raise AssertionError('cost proof must not add writes')
        if path=='/fapi/v1/accountConfig':return dict(canTrade=True,dualSidePosition=False,multiAssetsMargin=False)
        if path=='/fapi/v1/order':
            return copy.deepcopy(self.exit if 'orderId'in params else self.entry)
        if path=='/fapi/v1/allAlgoOrders':return[copy.deepcopy(self.algo)]
        if path=='/fapi/v1/algoOrder':return copy.deepcopy(self.algo)
        if path in('/fapi/v1/openOrders','/fapi/v1/openAlgoOrders'):return[]
        if path=='/fapi/v3/positionRisk':
            if not self.partial:return[]
            return[dict(symbol=self.intent['symbol'],positionSide='BOTH',liquidationPrice='0',
                positionAmt=format(Decimal(self.entry['executedQty'])-Decimal(self.exit['executedQty']),'f'))]
        if path=='/fapi/v1/userTrades':
            if 'orderId'in params:
                if str(params['orderId'])=='101':return[copy.deepcopy(self.entry_trade)]
                if self.missing_history:return[]
                return[copy.deepcopy(self.exit_trade)]
            rows=[copy.deepcopy(self.entry_trade),copy.deepcopy(self.exit_trade)]
            if self.foreign_trade:rows.append(dict(self.exit_trade,id=99,orderId=999))
            return rows
        raise AssertionError(path)


class ConfirmedExitCostTests(unittest.TestCase):
    def intent(self,*,dynamic=False):
        if dynamic:
            helper=native.dynamic_fixture.DynamicOwnerRiskTests();rules=helper.rules()
            plan=helper.plan(rules=rules);plan['sizing_rules']=native.serialized_rules(rules)
            plan['provenance']={'payload_sha256':native.digest(plan)}
        else:plan=native.legacy_plan()
        return native.gateway.build_intent(plan,'owned-exit-cost-operation')

    def record(self,exchange,coordinator_class=robot.Coordinator):
        db=sqlite3.connect(':memory:',isolation_level=None);self.addCleanup(db.close)
        coordinator=coordinator_class.__new__(coordinator_class)
        coordinator.db=db;coordinator.gateway=exchange
        coordinator.record_gateway_execution_costs()
        return db

    def test_v4_closing_fill_binds_algo_child_trade_quantity_and_frozen_book_impact(self):
        intent=self.intent(dynamic=True);exchange=ProvenExitExchange(intent)
        with gateway_mutations(lambda:False):result=exchange.reconcile_protection(intent)
        self.assertEqual(result['state'],'CLOSED')
        self.assertTrue(all(method=='GET'for method,path,params in exchange.calls))
        samples=exchange.confirmed_execution_observations();self.assertEqual(len(samples),1)
        sample=samples[0]
        self.assertEqual((sample['order_id'],sample['execution_id']),('303','12'))
        self.assertEqual(sample['reference_price'],intent['protection']['stop_loss'])
        self.assertEqual(sample['expected_impact_rate'],intent['cost_evidence']['expected_book_impact_rate'])
        self.assertEqual(sample['quantity'],intent['entry']['quantity'])
        self.assertEqual(sample['source'],'BINANCE_FUTURES_USER_TRADES')
        request_count=len(exchange.calls)
        for coordinator_class in(robot.Coordinator,native_coordinator()):
            db=self.record(exchange,coordinator_class)
            self.assertEqual(len(exchange.calls),request_count)
            bad=dict(sample,source='UNVERIFIED_SYMBOL_MATCH')
            with self.assertRaisesRegex(Review,'INVALID_EXECUTION_COST_EVIDENCE'):
                record_execution(db,**bad)
            self.assertEqual(len(execution_samples(db,intent['symbol'])),1)
            self.assertEqual(db.execute('SELECT fee_amount,fee_asset FROM execution_cost_observations').fetchone(),('0.06','USDT'))
            coordinator=coordinator_class.__new__(coordinator_class);coordinator.db=db;coordinator.gateway=exchange
            coordinator.record_gateway_execution_costs()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM execution_cost_observations').fetchone()[0],1)

    def test_legacy_exit_fees_are_recorded_but_unknown_book_impact_is_not_calibration(self):
        exchange=ProvenExitExchange(self.intent())
        with gateway_mutations(lambda:False):exchange.reconcile_protection(exchange.intent)
        db=self.record(exchange)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM execution_cost_observations').fetchone()[0],1)
        self.assertEqual(execution_samples(db,exchange.intent['symbol']),[])
        self.assertIsNone(db.execute('SELECT expected_impact_rate FROM execution_cost_observations').fetchone()[0])

    def test_unproven_foreign_or_missing_history_never_becomes_calibration(self):
        for options in({'foreign_trade':True},{'missing_history':True}):
            exchange=ProvenExitExchange(self.intent(dynamic=True),**options)
            with self.subTest(options=options),gateway_mutations(lambda:False),self.assertRaises(Exception):
                exchange.reconcile_protection(exchange.intent)
            self.assertEqual(exchange.confirmed_execution_observations(),[])
            self.assertTrue(all(method=='GET'for method,path,params in exchange.calls))

    def test_proved_partial_exit_records_only_executed_quantity(self):
        exchange=ProvenExitExchange(self.intent(dynamic=True),quantity='0.4')
        with gateway_mutations(lambda:False):result=exchange.reconcile_protection(exchange.intent)
        self.assertEqual(result['state'],'PROTECTION_INCOMPLETE')
        self.assertFalse(result['sl_confirmed']or result['tp_confirmed'])
        self.assertEqual(exchange.confirmed_execution_observations()[0]['quantity'],'0.4')
        db=self.record(exchange)
        self.assertEqual(db.execute('SELECT quantity FROM execution_cost_observations').fetchone()[0],'0.4')


if __name__=='__main__':unittest.main()
