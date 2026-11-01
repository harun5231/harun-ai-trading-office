import copy
from dataclasses import asdict
from datetime import datetime,timezone
from decimal import Decimal
from fractions import Fraction
import importlib.util
from pathlib import Path
import time
import sys
import types
import unittest
from unittest.mock import patch

from worker.core import (D,Rules,Signal,RISK_MODEL,NORMALIZED_REWARD_RISK_POLICY,risk_check)
from worker.research_guard import gateway_mutations_allowed,gateway_protection_allowed,gateway_mutations
from worker.provenance import digest
from tests import test_dynamic_risk_v4 as dynamic_fixture


PATH=Path(__file__).resolve().parents[1]/'vps_native/worker/order_gateway.py'
SPEC=importlib.util.spec_from_file_location('worker.actual_gateway_revision',PATH)
gateway=importlib.util.module_from_spec(SPEC)
# The pinned gateway retains an unused requests import; its real transport is
# urllib. Tests intercept every request and do not install runtime packages.
with patch.dict(sys.modules,{'requests':types.ModuleType('requests')}):
    SPEC.loader.exec_module(gateway)


def legacy_plan():
    now=time.time()
    rules=Rules(D('.01'),D('.01'),D('1000'),D('.01'),D('1'),now,
        taker_fee_rate=D('.0005'),fee_observed_at=now,fee_symbol='ETHUSDT',
        fee_source='BINANCE_FUTURES_COMMISSION_RATE')
    value=risk_check(Signal('ETHUSDT','LONG',D('100'),D('104'),D('99')),rules,D('5'),
        risk_model=RISK_MODEL,reward_risk_policy=NORMALIZED_REWARD_RISK_POLICY)
    value['sizing_rules']=serialized_rules(rules)
    value['protection_working_type']='CONTRACT_PRICE'
    value['provenance']={'payload_sha256':'a'*64}
    return value


def serialized_rules(rules):
    def plain(value):
        if isinstance(value,Decimal):return format(value,'f')
        if isinstance(value,dict):return{k:plain(v)for k,v in value.items()}
        if isinstance(value,list):return[plain(v)for v in value]
        return value
    return plain(asdict(rules))


class FakeExchange(gateway.OrderGateway):
    def __init__(self,intent,*,fail=None,unknown_created=None,filled='0.4'):
        self.intent=copy.deepcopy(intent);self.server={};self.calls=[];self.fail=fail
        self.unknown_created=unknown_created;self.filled=filled;self.serial=500
        self._connected=True
    def _account_config(self):pass
    def _trades(self,symbol,oid,qty,side,since):
        return [{'id':'1','orderId':int(oid),'time':int(time.time()*1000)-1000}]
    def _owned_algos(self,intent,since):return list(copy.deepcopy(self.server).values())
    def _inventory(self,*args):pass
    def _ownership(self,*args):pass
    def _exits(self,intent,algos):return Fraction(0),[],[]
    def _request(self,method,path,params):
        self.calls.append((method,path,copy.deepcopy(params)))
        if method!='GET' and not (gateway_mutations_allowed() or gateway_protection_allowed(method,path,params)):
            raise RuntimeError('MUTATION_NOT_AUTHORIZED')
        if path=='/fapi/v1/order' and method=='GET':
            return dict(symbol=self.intent['symbol'],clientOrderId=self.intent['client_order_id'],
                side=self.intent['entry']['side'],positionSide='BOTH',type='LIMIT',timeInForce='GTC',
                reduceOnly=False,closePosition=False,price=self.intent['entry']['price'],
                origQty=self.intent['entry']['quantity'],executedQty=self.filled,
                status='PARTIALLY_FILLED',orderId=101,time=int(time.time()*1000)-5000)
        if path=='/fapi/v1/algoOrder' and method=='GET':
            if 'clientAlgoId'in params:key=params['clientAlgoId']
            else:key=next((k for k,v in self.server.items()if str(v['algoId'])==str(params['algoId'])),None)
            if key not in self.server:raise self._ExchangeError(-2013)
            return copy.deepcopy(self.server[key])
        if path=='/fapi/v1/algoOrder' and method=='POST':
            kind='sl'if params['type']=='STOP_MARKET'else'tp'
            if self.fail==kind:raise self._ExchangeError(-1111)
            self.serial+=1
            row=dict(symbol=params['symbol'],algoType='CONDITIONAL',orderType=params['type'],
                side=params['side'],positionSide='BOTH',workingType=params['workingType'],
                reduceOnly=True,closePosition=False,clientAlgoId=params['clientAlgoId'],
                algoId=self.serial,triggerPrice=params['triggerPrice'],quantity=params['quantity'],
                algoStatus='NEW',actualOrderId=0)
            self.server[params['clientAlgoId']]=row
            if self.unknown_created==kind:raise TimeoutError('uncertain response')
            return {'algoId':self.serial}
        if path=='/fapi/v1/algoOrder' and method=='DELETE':
            row=next(v for v in self.server.values()if str(v['algoId'])==str(params['algoId']))
            row['algoStatus']='CANCELED';return {}
        raise AssertionError((method,path,params))


class ActualGatewayOwnerRevisionTests(unittest.TestCase):
    def intent(self):return gateway.build_intent(legacy_plan(),'owned-existing-operation')

    def test_existing_legacy_intent_keeps_original_price_quantity_leverage_and_tpsl(self):
        intent=self.intent();original=copy.deepcopy(intent)
        fake=FakeExchange(intent)
        result=fake.reconcile_protection(intent)
        self.assertEqual(intent,original)
        self.assertEqual(intent['leverage'],75)
        self.assertEqual(result['state'],'POSITION_PROTECTED')
        self.assertTrue(result['sl_confirmed']and result['tp_confirmed'])
        posts=[p for m,u,p in fake.calls if m=='POST']
        self.assertEqual([p['type']for p in posts],['STOP_MARKET','TAKE_PROFIT_MARKET'])
        self.assertTrue(all(p['quantity']=='0.4'for p in posts))
        self.assertEqual(posts[0]['triggerPrice'],intent['protection']['stop_loss'])
        self.assertEqual(posts[1]['triggerPrice'],intent['protection']['take_profit'])
        self.assertLessEqual(result['sl_submitted_at'],result['sl_confirmed_at'])
        self.assertLessEqual(result['tp_submitted_at'],result['tp_confirmed_at'])
        self.assertTrue(result['accepted_at'])

    def test_sl_failure_still_attempts_tp_in_same_handler(self):
        intent=self.intent();fake=FakeExchange(intent,fail='sl')
        result=fake.reconcile_protection(intent)
        self.assertEqual(result['state'],'PROTECTION_INCOMPLETE')
        self.assertFalse(result['sl_confirmed']);self.assertTrue(result['tp_confirmed'])
        self.assertEqual([p['type']for m,u,p in fake.calls if m=='POST'],['STOP_MARKET','TAKE_PROFIT_MARKET'])
        self.assertTrue(result['tp_order_id'])
        self.assertEqual(result['sl_failure_code'],'BINANCE_PROTECTION_C1111')
        self.assertEqual(result['sl_exchange_code'],-1111)
        self.assertFalse(any(m=='DELETE'for m,u,p in fake.calls))

    def test_tp_failure_preserves_sl_and_next_get_resolves_without_duplicate_sl(self):
        intent=self.intent();fake=FakeExchange(intent,fail='tp')
        result=fake.reconcile_protection(intent)
        sl=result['sl_order_id'];self.assertTrue(result['sl_confirmed']);self.assertFalse(result['tp_confirmed'])
        fake.fail=None;fake.calls=[]
        result=fake.reconcile_protection(intent)
        self.assertEqual(result['state'],'POSITION_PROTECTED');self.assertEqual(result['sl_order_id'],sl)
        self.assertEqual([p['type']for m,u,p in fake.calls if m=='POST'],['TAKE_PROFIT_MARKET'])

    def test_uncertain_post_is_confirmed_by_fresh_read_not_resubmitted(self):
        intent=self.intent();fake=FakeExchange(intent,unknown_created='sl')
        result=fake.reconcile_protection(intent)
        self.assertEqual(result['state'],'POSITION_PROTECTED')
        self.assertEqual([p['type']for m,u,p in fake.calls if m=='POST'].count('STOP_MARKET'),1)

    def test_partial_fill_growth_keeps_old_coverage_until_both_new_legs_confirm(self):
        intent=self.intent();fake=FakeExchange(intent)
        first=fake.reconcile_protection(intent)
        old={first['sl_order_id'],first['tp_order_id']};fake.calls=[];fake.filled='1'
        second=fake.reconcile_protection(intent)
        self.assertEqual(second['filled_quantity'],'1')
        self.assertTrue(second['sl_confirmed']and second['tp_confirmed'])
        post_indices=[i for i,(m,u,p)in enumerate(fake.calls)if m=='POST']
        delete_indices=[i for i,(m,u,p)in enumerate(fake.calls)if m=='DELETE']
        self.assertEqual(len(post_indices),2);self.assertEqual(len(delete_indices),2)
        self.assertGreater(min(delete_indices),max(post_indices))
        self.assertEqual({str(p['algoId'])for m,u,p in fake.calls if m=='DELETE'},old)

    def test_exit_only_scope_cannot_submit_entry_or_change_configuration(self):
        intent=self.intent()
        for method,path,params in [('POST','/fapi/v1/order',{'symbol':'ETHUSDT'}),
                ('POST','/fapi/v1/leverage',{'symbol':'ETHUSDT','leverage':75}),
                ('POST','/fapi/v1/marginType',{'symbol':'ETHUSDT','marginType':'CROSSED'}),
                ('DELETE','/fapi/v1/order',{'symbol':'ETHUSDT','origClientOrderId':intent['client_order_id']}),
                ('DELETE','/fapi/v1/algoOrder',{'algoId':'900'})]:
            class Bad(FakeExchange):
                def reconcile(self,command):return self._request(method,path,params)
            fake=Bad(intent)
            with gateway_mutations(lambda:False),self.subTest(path=path),self.assertRaisesRegex(Exception,'PROTECTION_SCOPE_DENIED'):
                fake.reconcile_protection(intent)
            self.assertEqual(fake.calls,[])

    def test_unconfirmed_history_error_does_not_cancel_pending_entry(self):
        intent=self.intent()
        class BadHistory(FakeExchange):
            def _trades(self,*args):raise gateway.Review('BINANCE_ORDER_HISTORY_INCOMPLETE')
        fake=BadHistory(intent)
        with self.assertRaises(Exception):fake.reconcile_protection(intent)
        self.assertFalse(any(m=='DELETE'for m,u,p in fake.calls))

    def test_v4_dynamic_cost_math_and_lower_available_leverage_are_admitted(self):
        helper=dynamic_fixture.DynamicOwnerRiskTests();rules=helper.rules()
        rules=copy.deepcopy(rules)
        rules.leverage_brackets['brackets'][0]['max_leverage']=50
        plan=helper.plan(rules=rules);plan['sizing_rules']=serialized_rules(rules)
        plan['provenance']={'payload_sha256':digest(plan)}
        intent=gateway.build_intent(plan,'new-policy-operation')
        self.assertEqual(intent['leverage'],50)
        gateway.OrderGateway.__new__(gateway.OrderGateway)._validate_intent(intent)
        self.assertEqual(intent['normalized_plan']['neurobro_tp'],plan['neurobro_tp'])
        for key in ['risk_usdt','net_reward_usdt']:
            bad=copy.deepcopy(intent);bad[key]='999'
            with self.subTest(key=key),self.assertRaises(Exception):
                gateway.OrderGateway.__new__(gateway.OrderGateway)._validate_intent(bad)

    def test_accepted_v4_intent_remains_valid_for_protection_after_day_rollover(self):
        helper=dynamic_fixture.DynamicOwnerRiskTests();rules=helper.rules()
        plan=helper.plan(rules=rules);plan['sizing_rules']=serialized_rules(rules)
        plan['provenance']={'payload_sha256':digest(plan)}
        intent=gateway.build_intent(plan,'existing-v4-operation')
        frozen=copy.deepcopy(intent);later=time.time()+86400
        with patch('worker.core.time.time',return_value=later):
            gateway.OrderGateway.__new__(gateway.OrderGateway)._validate_intent(intent)
        self.assertEqual(intent,frozen)

    def test_new_v4_entry_expired_during_configuration_is_proven_unsent(self):
        from worker.research_guard import EntryNotSubmitted
        helper=dynamic_fixture.DynamicOwnerRiskTests();rules=helper.rules()
        plan=helper.plan(rules=rules);plan['sizing_rules']=serialized_rules(rules)
        plan['provenance']={'payload_sha256':digest(plan)}
        intent=gateway.build_intent(plan,'new-policy-operation')
        frozen=copy.deepcopy(intent);later=time.time()+301
        class SlowConfiguration(FakeExchange):
            def _preflight(self,command):pass
            def _configure(self,command):
                clock.return_value=later
        fake=SlowConfiguration(intent)
        with patch('worker.core.time.time',return_value=time.time())as clock,gateway_mutations(lambda:True):
            with self.assertRaisesRegex(EntryNotSubmitted,'ORDER_UNSENT_STALE_EVIDENCE'):
                fake.submit(intent)
        self.assertEqual(intent,frozen)
        self.assertFalse(any(method=='POST'and path=='/fapi/v1/order'for method,path,params in fake.calls))
        # This same frozen accepted intent remains eligible for protection.
        with patch('worker.core.time.time',return_value=later),gateway_mutations(lambda:False):
            result=fake.reconcile_protection(intent)
        self.assertEqual(result['state'],'POSITION_PROTECTED')

    def test_legacy_validation_cannot_silently_adopt_lower_leverage(self):
        intent=self.intent();intent['leverage']=50
        with self.assertRaises(Exception):gateway.OrderGateway.__new__(gateway.OrderGateway)._validate_intent(intent)

    def test_false_entry_permission_does_not_block_owned_exit_protection(self):
        intent=self.intent();fake=FakeExchange(intent)
        with gateway_mutations(lambda:False):
            self.assertFalse(gateway_mutations_allowed())
            result=fake.reconcile_protection(intent)
            self.assertFalse(gateway_mutations_allowed())
        self.assertEqual(result['state'],'POSITION_PROTECTED')
        self.assertEqual([p['type']for m,u,p in fake.calls if m=='POST'],['STOP_MARKET','TAKE_PROFIT_MARKET'])

    def test_entry_accepted_before_owner_off_still_attaches_existing_sl_and_tp(self):
        intent=self.intent();enabled=[True]
        class AcceptedThenOff(FakeExchange):
            def _preflight(self,command):pass
            def _configure(self,command):pass
            def _request(self,method,path,params):
                if method=='POST' and path=='/fapi/v1/order':
                    if not gateway_mutations_allowed():
                        raise RuntimeError('ENTRY_NOT_AUTHORIZED')
                    self.calls.append((method,path,copy.deepcopy(params)))
                    enabled[0]=False
                    return {'orderId':101}
                return super()._request(method,path,params)
        fake=AcceptedThenOff(intent)
        with gateway_mutations(lambda:enabled[0]):
            result=fake.submit(intent)
            self.assertFalse(gateway_mutations_allowed())
        self.assertEqual(result['state'],'POSITION_PROTECTED')
        self.assertEqual([path for method,path,params in fake.calls if method=='POST'],
            ['/fapi/v1/order','/fapi/v1/algoOrder','/fapi/v1/algoOrder'])
        self.assertFalse(enabled[0])

    def test_actual_signed_wire_accepts_only_owned_exit_under_false_entry_scope(self):
        import io,json
        intent=self.intent();sent=[]
        class Response(io.BytesIO):
            code=200
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        class Opener:
            def open(self,request,timeout):
                sent.append(request)
                return Response(json.dumps({'algoId':501}).encode())
        class SignedExit(FakeExchange):
            def _get_server_time(self):return 1700000000000
            def reconcile(self,command):
                self._ownership(command,[],[],Fraction('0.4'))
                params=dict(algoType='CONDITIONAL',symbol=command['symbol'],
                    side=command['protection']['exit_side'],positionSide='BOTH',
                    type='STOP_MARKET',quantity='0.4',reduceOnly='true',
                    triggerPrice=command['protection']['stop_loss'],
                    workingType=command['protection']['working_type'],
                    clientAlgoId=self._algo_id(command,'sl',D('0.4')),newOrderRespType='ACK')
                return gateway.OrderGateway._request(self,'POST','/fapi/v1/algoOrder',params)
        fake=SignedExit(intent);fake.base_url='https://fapi.binance.com'
        fake.api_key='offline-key';fake.api_secret='offline-secret'
        with gateway_mutations(lambda:False),patch('urllib.request.build_opener',return_value=Opener()):
            result=fake.reconcile_protection(intent)
        self.assertEqual(result,{'algoId':501});self.assertEqual(len(sent),1)
        self.assertEqual(sent[0].full_url,'https://fapi.binance.com/fapi/v1/algoOrder')
        self.assertIn(b'signature=',sent[0].data)

    def test_exit_authority_cannot_be_used_for_oversize_or_unproved_fill(self):
        intent=self.intent()
        class NotOwned(FakeExchange):
            def reconcile(self,command):
                self._ensure_algo(command,'sl',D('0.4'))
        fake=NotOwned(intent)
        with gateway_mutations(lambda:False):
            with self.assertRaisesRegex(Exception,'PROTECTION_SCOPE_DENIED'):
                fake.reconcile_protection(intent)
        self.assertFalse(any(m=='POST'for m,u,p in fake.calls))

    def test_exit_authority_requires_the_exact_current_proven_quantity(self):
        intent=self.intent()
        class Overstated(FakeExchange):
            def reconcile(self,command):
                self._ownership(command,[],[],Fraction('0.4'))
                self._ensure_algo(command,'sl',D('0.5'))
        fake=Overstated(intent)
        with gateway_mutations(lambda:False):
            with self.assertRaisesRegex(Exception,'PROTECTION_SCOPE_DENIED'):
                fake.reconcile_protection(intent)
        self.assertFalse(any(m=='POST'for m,u,p in fake.calls))

    def test_actual_configure_uses_account_bracket_20_50_or_capped_75(self):
        helper=dynamic_fixture.DynamicOwnerRiskTests()
        for maximum,expected in [(20,20),(50,50),(125,75)]:
            with self.subTest(maximum=maximum):
                rules=helper.rules();rules.leverage_brackets['brackets'][0]['max_leverage']=maximum
                plan=helper.plan(rules=rules);plan['sizing_rules']=serialized_rules(rules)
                plan['provenance']={'payload_sha256':digest(plan)}
                intent=gateway.build_intent(plan,'new-policy-operation')
                self.assertEqual(intent['leverage'],expected)
                class Configured(gateway.OrderGateway):
                    def __init__(self):self.calls=[]
                    def _preflight(self,intent):pass
                    def _request(self,method,path,params):
                        self.calls.append((method,path,params))
                        if path=='/fapi/v1/marginType':return{'code':200}
                        if path=='/fapi/v1/leverage':return{'symbol':'ETHUSDT','leverage':params['leverage'],'maxNotionalValue':'1000000'}
                        if path=='/fapi/v1/symbolConfig':return[{'symbol':'ETHUSDT','marginType':'CROSSED','leverage':expected,'maxNotionalValue':'1000000'}]
                        if path=='/fapi/v1/leverageBracket':return[{'symbol':'ETHUSDT','brackets':[{'notionalFloor':'0','notionalCap':'1000000','initialLeverage':maximum}]}]
                        raise AssertionError(path)
                fake=Configured();fake._validate_intent(intent);fake._configure(intent)
                self.assertEqual([p['leverage']for m,u,p in fake.calls if u=='/fapi/v1/leverage'],[expected])
                self.assertFalse(any(u=='/fapi/v1/order'for m,u,p in fake.calls))


if __name__=='__main__':unittest.main()
