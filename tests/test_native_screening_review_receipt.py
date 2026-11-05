"""The existing dialogue review writer retains real screening approvals."""
import copy
import unittest

from agent.native_analysis_receipt import persist_review,read_reviews,digest,SCREENING_KIND
from tests import test_native_browser_hook as fixtures
from worker.account_state import screening_contract
from worker.prompts import REPLACEMENT_SCREENING


class ScreeningReviewReceiptTests(unittest.TestCase):
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
