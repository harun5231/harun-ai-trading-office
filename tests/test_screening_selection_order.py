"""Actual source-bound coin choices stay valid when Mini lists them differently.

All native/model/browser/order boundaries below are local fakes or retained
receipts. The historical prices in the screening reply grant no setup authority.
"""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.neurobro_reply import ReplyIncomplete, parse_analysis_reply, parse_screening_reply, screening_interpretation
from tests.test_semantic_screening import bound, receipt
from tests import test_generic_screening_response_recovery as recovery_fixtures
from tests import test_native_browser_hook as hook_fixtures
from worker import screening_response_recovery as recovery

FIXTURE = Path(__file__).parent / 'fixtures/neurobro_screening_selection_order_20261122.json'


class ScreeningSelectionOrderTests(unittest.TestCase):
    def setUp(self):
        self.original = FIXTURE.read_bytes()
        self.saved = json.loads(self.original)
        self.reply = self.saved['reply']
        self.symbols = self.saved['mini_symbols']

    def test_actual_reply_and_sealed_mini_agree_on_identity_without_rewriting_order_or_prices(self):
        self.assertEqual(hashlib.sha256(self.reply.encode()).hexdigest(), self.saved['reply_sha256'])
        self.assertEqual(self.saved['mini_review']['reply_sha256'], self.saved['reply_sha256'])
        self.assertEqual(self.saved['mini_review']['model_operation']['state'], 'DONE')
        self.assertEqual(self.saved['mini_review']['decision']['symbols'], ['ENAUSDT', 'ZECUSDT'])
        self.assertEqual(parse_screening_reply(self.reply), {'symbols': ['ZECUSDT', 'ENAUSDT']})
        args = bound(self.reply, self.symbols);before = copy.deepcopy(args)
        self.assertEqual(screening_interpretation(self.reply, *args), {'symbols': self.symbols})
        self.assertEqual(args, before);self.assertEqual(FIXTURE.read_bytes(), self.original)
        for coin in self.symbols:
            with self.assertRaises(ReplyIncomplete):
                parse_analysis_reply(self.reply, coin)

    def test_membership_not_presentation_order_is_shared_for_json_and_explicit_final_fields(self):
        for reply in ('Keputusan final\nCOIN: ZECUSDT, ENAUSDT',
                '{"SYMBOLS":["ZECUSDT","ENAUSDT"]}',
                '```json\n{"SYMBOLS":["ZECUSDT","ENAUSDT"]}\n```',
                'Pilihan final saya ZECUSDT dan ENAUSDT.\n{"SYMBOLS":["ZECUSDT","ENAUSDT"]}'):
            with self.subTest(reply=reply):
                self.assertEqual(screening_interpretation(reply, *bound(reply, self.symbols)),
                    {'symbols': self.symbols})

    def test_real_membership_conflict_duplicates_exclusion_capacity_and_source_remain_blocked(self):
        for names in (['ENAUSDT', 'BTCUSDT'], ['ENAUSDT', 'ENAUSDT']):
            with self.subTest(names=names), self.assertRaises(ReplyIncomplete):
                screening_interpretation(self.reply, *bound(self.reply, names))
        for fault in ('excluded', 'count', 'request', 'transcript'):
            args = bound(self.reply, self.symbols)
            if fault == 'excluded':args[0]['context']['excluded_symbols'] = ['ENAUSDT']
            elif fault == 'count':args[0]['context']['requested_count'] = 1
            elif fault == 'request':args[1]['request_id'] = 'f' * 64
            else:args[3][-1]['id'] = 'foreign-native-source'
            with self.subTest(fault=fault), self.assertRaises(ReplyIncomplete):
                screening_interpretation(self.reply, *args)
        conflicting = 'Keputusan final\nCOIN: BTCUSDT, ENAUSDT\n' + self.reply
        with self.assertRaises(ReplyIncomplete):
            screening_interpretation(conflicting, *bound(conflicting, self.symbols))
        for action in ('HOLD', 'CLARIFY'):
            payload,request,_,turns = bound(self.reply, self.symbols)
            response = receipt(request, self.symbols, action=action, question='Pilihan mana?' if action == 'CLARIFY' else '')
            with self.subTest(action=action), self.assertRaises(ReplyIncomplete):
                screening_interpretation(self.reply,payload,request,response,turns)

    def test_native_browser_accepts_exact_reply_once_without_format_followup_or_setup(self):
        from worker.account_state import screening_contract
        payload=hook_fixtures.request();_,payload['output_schema']=screening_contract(2)
        payload['context']={'requested_count':2,'excluded_symbols':[]}
        case=hook_fixtures.ComposedMaterialGateTests();case.harness(payload);self.addCleanup(case.doCleanups)
        case.bridge.initial=self.reply
        case.reasoner.request.side_effect=lambda request:receipt(request,self.symbols)
        result=case.hook.ask(payload,lambda:True,deadline=80)
        self.assertEqual(result['output'],{'symbols':self.symbols})
        self.assertEqual(result['assistant_text'],self.reply);self.assertEqual(result['chart_evidence'],[])
        self.assertEqual(case.bridge.clicks,1);case.reasoner.request.assert_called_once()
        case.converter.assert_not_called()

    def recovery_case(self):
        case=recovery_fixtures.GenericScreeningResponseRecoveryTests(methodName='runTest')
        case.setUp();self.addCleanup(case.doCleanups)
        operation=case.two_coin_contract();case.catalog.update({s:{} for s in self.symbols})
        envelope=case.contextual_review(case.fixture(operation=operation,reply=self.reply,symbols=self.symbols))
        return case,operation,envelope

    def test_source_bound_native_recovery_reuses_original_receipt_then_screening_advances_once(self):
        from worker import robot as robot_module
        case,operation,envelope=self.recovery_case();finances=case.finances()
        self.assertEqual(case.recover(envelope),'RESUME_CACHED')
        receipt_before=case.row('research_browser_responses',operation)
        self.assertEqual(receipt_before['assistant_text'],self.reply)
        self.assertEqual(json.loads(case.row('api_requests',operation)['output']),{'symbols':self.symbols})
        before=tuple(case.db.iterdump())
        self.assertEqual(case.recover(envelope,now=recovery_fixtures.NOW+1),'RESUME_CACHED')
        self.assertEqual(tuple(case.db.iterdump()),before)
        robot=object.__new__(robot_module.Coordinator);robot.db=case.db;robot.neuro=case.provider
        robot.allowed=lambda today:today==recovery_fixtures.DAY
        robot.market=SimpleNamespace(catalog=lambda:case.catalog,max_age=180);robot.exposed_symbols=lambda account:set()
        robot.store=SimpleNamespace(report=lambda status,account=None,reason=None,**kw:dict(bot_status=status,reason=reason,**kw))
        with patch.object(robot_module.time,'time',return_value=recovery_fixtures.NOW+2):
            result=robot.screen(recovery_fixtures.CYCLE,copy.deepcopy(case.data),2,{},recovery_fixtures.DAY,True)
        self.assertEqual(result['wait_reason'],'SCREENING_COMPLETE_ANALYSIS_PENDING')
        self.assertEqual(json.loads(case.row('robot_cycles')['data'])['queue'],self.symbols)
        self.assertEqual(case.finances(),finances)
        self.assertEqual(case.row('api_requests',operation)['attempts'],1)
        self.assertEqual(case.row('research_browser_responses',operation),receipt_before)

    def test_stale_received_order_difference_retires_once_and_restart_keeps_original_identity(self):
        case,operation,envelope=self.recovery_case();finances=case.finances()
        case.created=recovery_fixtures.NOW-1000
        case.db.execute('UPDATE api_requests SET created=? WHERE operation=?',(case.created,operation))
        self.assertEqual(case.recover(envelope),'NOEXEC')
        api=case.row('api_requests',operation)
        self.assertEqual((api['state'],api['failure_code'],api['output'],api['attempts'],api['created']),
            ('RECEIVED_STALE_NOT_EXECUTED','SCREENING_RESPONSE_RECEIVED_STALE',None,1,case.created))
        data=json.loads(case.row('robot_cycles')['data'])
        self.assertEqual(data['queue'],[]);self.assertEqual(data['replacement_due'],2)
        self.assertEqual(case.row('research_browser_responses',operation)['assistant_text'],self.reply)
        restarted_db=sqlite3.connect(':memory:',isolation_level=None);restarted_db.row_factory=sqlite3.Row
        self.addCleanup(restarted_db.close);case.db.backup(restarted_db)
        restarted=object.__new__(case.native.NeuroBrowser);restarted.db=restarted_db
        restarted.chart_max_age=180;restarted.transport=case.forbid;restarted.ask=case.forbid;restarted.require_key=case.forbid
        before=tuple(restarted_db.iterdump())
        self.assertEqual(recovery.recover_received_screening(restarted,envelope,catalog=case.catalog,
            max_age=180,now=recovery_fixtures.NOW+1),'NOEXEC')
        self.assertEqual(tuple(restarted_db.iterdump()),before)
        self.assertEqual(recovery.received_screening_replacement_due(restarted_db,recovery_fixtures.CYCLE,data),2)
        self.assertEqual(case.finances(),finances)


if __name__=='__main__':unittest.main()
