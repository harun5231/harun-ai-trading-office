"""The existing dialogue review writer retains real screening approvals."""
import copy
import json
import unittest
from unittest.mock import Mock

from agent.dialogue_service import DialogueError
from agent.native_analysis_receipt import persist_review,read_reviews,digest,SCREENING_KIND
from tests import test_native_browser_hook as fixtures
from worker.account_state import screening_contract
from worker.prompts import REPLACEMENT_SCREENING


class ScreeningReviewReceiptTests(unittest.TestCase):
    def child_hook(self, *, operation_prefix='delegated-model-screening-recovery:',
                   operation_suffix='', foreign_id=False, state='DONE'):
        case=fixtures.ComposedMaterialGateTests();self.addCleanup(case.doCleanups)
        payload=fixtures.request();_,schema=screening_contract(1)
        payload.update(operation='2026-11-05:robot-v9:0:screening:9:1',
            prompt=REPLACEMENT_SCREENING.format(count=1),output_schema=schema,
            context=dict(requested_count=1,excluded_symbols=['BTCUSDT','ETHUSDT'],
                replacement_instruction='Choose different Binance USD-M USDT perpetual coins that are not in excluded_symbols.'))
        case.harness(payload)
        answer=case.reasoner.request.side_effect
        case.reasoner.supports_screening_receipt_lookup=True
        def lookup(request):
            result=answer(request)
            identity='f'*64 if foreign_id else request['request_id']
            operation=dict(operation_id=operation_prefix+identity+operation_suffix,
                kind='DELEGATED_MODEL_DIALOGUE',request_hash='b'*64,state=state,
                result=json.dumps(result),updated_at=1793864322.0)
            return dict(result=result,operation=operation)
        case.reasoner.completed_screening=Mock(side_effect=lookup)
        case.reasoner.request.side_effect=AssertionError('Existing screening child must not call the model again')
        return case,payload

    def test_actual_child_identity_is_persisted_without_parent_alias_or_extra_send(self):
        case,payload=self.child_hook()
        result=case.hook.ask(payload,lambda:True,deadline=80)
        values=read_reviews(case.hook.daily_room.store.root,payload)
        self.assertEqual(len(values),1);proof=values[0]['value']
        self.assertEqual(proof['model_operation']['operation_id'],
            'delegated-model-screening-recovery:'+proof['request_id'])
        self.assertNotEqual(proof['model_operation']['operation_id'],'delegated-model:'+proof['request_id'])
        self.assertEqual(json.loads(proof['model_operation']['result']),proof['response'])
        self.assertEqual(proof['response']['decision']['symbols'],result['output']['symbols'])
        self.assertEqual(proof['response']['request_id'],proof['request']['request_id'])
        self.assertEqual(case.bridge.clicks,1)
        case.reasoner.request.assert_not_called()

    def test_unknown_parent_uses_one_interpretation_request_and_persists_the_real_child(self):
        case,payload=self.child_hook()
        stored=case.reasoner.completed_screening.side_effect
        seen=[]
        def lookup(request):
            seen.append(copy.deepcopy(request))
            if len(seen)==1:raise DialogueError('DIALOGUE_OPERATION_UNKNOWN')
            return stored(request)
        case.reasoner.completed_screening.side_effect=lookup
        case.reasoner.request.side_effect=lambda request:stored(request)['result']
        result=case.hook.ask(payload,lambda:True,deadline=80)
        self.assertEqual(len(seen),2)
        case.reasoner.request.assert_called_once()
        self.assertEqual(case.reasoner.request.call_args.args[0],seen[0])
        self.assertEqual(seen[0],seen[1])
        values=read_reviews(case.hook.daily_room.store.root,payload)
        self.assertEqual(len(values),1);proof=values[0]['value']
        self.assertEqual(proof['model_operation']['operation_id'],
            'delegated-model-screening-recovery:'+proof['request_id'])
        self.assertEqual(proof['response'],stored(seen[1])['result'])
        self.assertEqual(proof['response']['decision']['symbols'],result['output']['symbols'])
        self.assertEqual(case.bridge.clicks,1)

    def test_other_lookup_error_is_not_routed_to_a_new_interpretation(self):
        case,payload=self.child_hook()
        refused=DialogueError('DIALOGUE_AUTHENTICATION_FAILED')
        case.reasoner.completed_screening.side_effect=refused
        with self.assertRaises(fixtures.OutcomeUnknown) as failure:
            case.hook.ask(payload,lambda:True,deadline=80)
        # The native adapter preserves its existing sent-operation uncertainty,
        # while the original lookup error propagates out of the hook unchanged.
        self.assertIs(failure.exception.__context__,refused)
        case.reasoner.request.assert_not_called()
        case.reasoner.completed_screening.assert_called_once()
        self.assertEqual(read_reviews(case.hook.daily_room.store.root,payload),[])
        self.assertEqual(case.bridge.clicks,1)

    def test_unknown_or_wrong_child_identity_is_not_written_as_a_review(self):
        for options in (dict(state='UNKNOWN'),dict(foreign_id=True),dict(operation_suffix=':retry'),
                        dict(operation_suffix='0'),dict(operation_prefix='delegated-model-recovery:'),
                        dict(operation_prefix='delegated-model-screening-recovery:delegated-model:')):
            with self.subTest(options=options):
                case,payload=self.child_hook(**options)
                with self.assertRaises(fixtures.OutcomeUnknown):case.hook.ask(payload,lambda:True,deadline=80)
                self.assertEqual(read_reviews(case.hook.daily_room.store.root,payload),[])
                self.assertEqual(case.bridge.clicks,1)
                case.reasoner.request.assert_not_called()

    def test_real_hook_retains_one_or_two_coin_replacement_review_without_market_materials(self):
        for count in (1,2):
            with self.subTest(count=count):
                case=fixtures.ComposedMaterialGateTests();self.addCleanup(case.doCleanups)
                payload=fixtures.request();_,schema=screening_contract(count)
                payload.update(operation='2026-11-05:robot-v9:0:screening:9:'+str(count),
                    prompt=REPLACEMENT_SCREENING.format(count=count),output_schema=schema,
                    context=dict(requested_count=count,excluded_symbols=['BTCUSDT','ETHUSDT'],
                        replacement_instruction='Choose different Binance USD-M USDT perpetual coins that are not in excluded_symbols.'))
                case.harness(payload);before=copy.deepcopy(payload)
                result=case.hook.ask(payload,lambda:True,deadline=80)
                values=read_reviews(case.hook.daily_room.store.root,payload)
                self.assertEqual(len(values),1);proof=values[0]['value']
                self.assertEqual(proof['kind'],SCREENING_KIND);self.assertEqual(proof['phase'],'SCREENING')
                self.assertEqual(proof['context_sha256'],digest(payload['context']))
                self.assertIsNone(proof['request']['market_context'])
                self.assertEqual(proof['response']['decision']['symbols'],result['output']['symbols'])
                self.assertFalse(proof['response']['needs_followup']);self.assertFalse(proof['response']['trading_authority'])
                self.assertEqual(payload,before);self.assertEqual(case.bridge.clicks,1)
                request=copy.deepcopy(proof['request']);request['deadline_epoch']+=20
                retained=persist_review(case.hook.daily_room.store.root,payload,request,proof['response'],
                    case.bridge._turns(),context=payload['context'],observed_at=proof['observed_at']+1)
                self.assertEqual(retained,proof)


if __name__=='__main__':unittest.main()
