"""Verbatim WIF drafts; synthetic source/transport fixtures never reach production."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import re
import time
import unittest
from unittest.mock import patch

from agent import dialogue_service as current
from agent.llm import OpenAIDialogue, web_prompt_allowed
from agent.memory import Memory
from agent.native_market_context import convert_native_market_context
from agent.native_prompt_composition import normalize_privacy_reminders, outbound_question
from scripts import prepare_mini_dialogue_runtime as mini_profile
from tests.test_agent_native_market_context import native_context
from tests import test_compose_privacy_recovery as privacy_tests
from tests.test_mini_dialogue_profile import retained_module

FIXTURE=json.loads((Path(__file__).parent/'fixtures/mini_wif_compose_structural_numbers_20261126.json').read_text())


def old_filter():
    node=next(n for n in ast.parse(Path(current.__file__).read_text()).body
              if isinstance(n,ast.FunctionDef) and n.name=='_without_discussion_structure')
    source=ast.get_source_segment(Path(current.__file__).read_text(),node)
    source=source.replace(r'(^|[,;:\n]|\.(?=[ \t]))',r'(^|[,;:\n])')
    source=source.replace('|persyaratan','').replace(r'|[\u2013\u2014-]','')
    # This is exactly the previously deployed f90 helper, not an approximation.
    assert hashlib.sha256(source.encode()).hexdigest()=='1fa1fa4840c6f0ffc04834ae55584a6da5d5e7de02697834bcc3f2bc026d6c42'
    scope={'re':re};exec(compile(source,'source-verified-prior-WIF-filter','exec'),scope)
    return scope['_without_discussion_structure']


def source_request(example):
    # Only numeric attribution is under test. This is deliberately NOT a valid
    # full market snapshot and does not claim to reconstruct private OHLC.
    return {'phase':'COMPOSE_ANALYSIS','reply':example['original_request_evidence']['reply'],
            'market_context':{'symbol':'WIFUSDT','mark_price':'0.1343'}}


class WifSourceGuardTests(unittest.TestCase):
    def test_verbatim_originals_reproduce_before_and_pass_after_in_both_runtime_sources(self):
        for service in (current,retained_module()):
            for example in FIXTURE['examples']:
                with self.subTest(source=service.__name__,operation=example['operation_id']):
                    raw=copy.deepcopy(example['raw_decision']);before=copy.deepcopy(raw)
                    self.assertEqual(hashlib.sha256(raw['question'].encode()).hexdigest(),example['question_sha256'])
                    request=source_request(example);normalized=normalize_privacy_reminders(raw['question'])
                    previous=old_filter()
                    stripped=re.sub(r'(?i)\b(?:15\s*M|1\s*H|2\s*R|1\s*:\s*2)\b','',previous(normalized))
                    self.assertEqual(stripped,example['before_numeric_source_evidence']['question_after_existing_metadata_filters'])
                    known=service._number_values(request['reply'])|service._number_values(request['market_context'])
                    self.assertEqual(sorted(map(str,service._number_values(stripped)-known)),
                                     example['before_numeric_source_evidence']['rejected_values'])
                    with patch.object(service,'_without_discussion_structure',previous):
                        with self.assertRaisesRegex(service.DialogueError,'DIALOGUE_PRICE_PROPOSAL_REFUSED'):
                            service.validate_decision(copy.deepcopy(raw),request)
                    decision=service.validate_decision(raw,request)
                    self.assertEqual(decision['question'],outbound_question(raw['question']))
                    self.assertEqual(service.validate_web_question(raw['question'],request),decision['question'])
                    self.assertTrue(web_prompt_allowed(decision['question']))
                    self.assertEqual(raw,before)

    def test_fixture_counts_only_proven_api_responses_not_all_matching_archives(self):
        self.assertEqual(FIXTURE['matching_archived_request_count'],11)
        apis=[row['official_api']for row in FIXTURE['examples']]
        self.assertEqual(len(apis),FIXTURE['proven_official_api_responses'])
        for api in apis:
            self.assertEqual(api['http_status'],200);self.assertTrue(api['request_sent'])
            self.assertTrue(api['provider_response_received']);self.assertEqual(api['usage_state'],'OPENAI_RESPONSE_USAGE')
        self.assertEqual({key:sum(api['usage'][key]for api in apis)for key in('input_tokens','output_tokens','total_tokens')},
                         FIXTURE['proven_official_usage'])

    def test_unsourced_prices_or_unicode_digits_still_fail_after_the_structural_fix(self):
        for service in (current,retained_module()):
            for example in FIXTURE['examples']:
                for suffix in (' ENTRY 123456.789.',' SL 123456.789.',' TP 123456.789.',' ENTRY １２３.'):
                    with self.subTest(source=service.__name__,suffix=suffix):
                        with self.assertRaisesRegex(service.DialogueError,'DIALOGUE_PRICE_PROPOSAL_REFUSED'):
                            service.validate_decision({**example['raw_decision'],
                                'question':example['raw_decision']['question']+suffix},source_request(example))


class WifKnownPaidResponseRecoveryTests(unittest.TestCase):
    def setUp(self):
        privacy_tests.ComposePrivacyRecoveryTests.setUp(self)
        self.service=retained_module()
        # Recovery checks the sibling llm.py source. A clean checkout has not
        # prepared Docker build inputs; keep both exact sources test-private.
        self.service.__file__=str(self.base/'dialogue_service.py')
        Path(self.service.__file__).write_bytes(mini_profile.build_source())
        model_source=Path(current.__file__).with_name('llm.py').read_bytes()
        self.assertEqual(hashlib.sha256(model_source).hexdigest(),mini_profile.MODEL_SOURCE_SHA256)
        (self.base/'llm.py').write_bytes(model_source)
        self.engine=self.service.DialogueEngine(self.memory,self.key,model=self.model)

    def tearDown(self):privacy_tests.ComposePrivacyRecoveryTests.tearDown(self)
    def transport(self,payload):return privacy_tests.ComposePrivacyRecoveryTests.transport(self,payload)

    def check_recovery(self,index):
        example=FIXTURE['examples'][index];now=time.time();native=native_context(now)
        native['symbol']='WIFUSDT';native['mark_price']['price']='0.1343'
        for frame in native['timeframes'].values():frame['symbol']='WIFUSDT'
        original=example['original_request_evidence']
        self.request.update(request_id=original['request_id'],conversation_id='compose:offline-WIF',
            reply=original['reply'],reply_sha256=original['reply_sha256'],
            market_context=convert_native_market_context(native,now=now),deadline_epoch=now+80)
        self.authored=copy.deepcopy(example['raw_decision']);service=self.service
        operation='delegated-model:'+self.request['request_id'];memo=service.dialogue_operation_memo(self.request)
        def paid_but_previously_rejected():
            service._archive_compose_request(self.memory,operation,self.request,time.time())
            self.model.plan([],self.request['reply'],phase=self.request['phase'],jakarta_day='2026-11-26',context=self.request['market_context'])
            with patch.object(service,'_without_discussion_structure',old_filter()):
                service.validate_decision(copy.deepcopy(self.authored),self.request)
        with self.assertRaisesRegex(service.DialogueError,'DIALOGUE_PRICE_PROPOSAL_REFUSED'):
            self.memory.operation(operation,'DELEGATED_MODEL_DIALOGUE',memo,paid_but_previously_rejected)
        before=list(self.memory.db.execute('SELECT * FROM operations'))
        metadata=list(self.memory.db.execute('SELECT * FROM metadata'))
        budgets=list(self.memory.db.execute('SELECT * FROM budgets'))
        self.assertEqual(len(self.calls),1)
        result=self.engine.process(copy.deepcopy(self.request))
        self.assertEqual(result['decision']['question'],outbound_question(self.authored['question']))
        self.assertEqual(list(self.memory.db.execute('SELECT * FROM operations')),before)
        self.assertEqual(before[0][3],'UNKNOWN')
        for key,value in metadata:self.assertEqual(self.memory.get(key),json.loads(value))
        audit_key='dialogue-compose-response-recovery-v1:'+operation
        audit=copy.deepcopy(self.memory.get(audit_key))
        self.assertEqual(audit['policy'],'VALUE_FREE_NEGATIVE_PRIVACY_REMINDER_V1')
        self.assertEqual(self.engine.process(copy.deepcopy(self.request)),result)
        self.memory.close();self.memory=Memory(self.base/'agent.sqlite3')
        self.model=OpenAIDialogue(self.key,self.memory,transport=self.transport)
        self.engine=service.DialogueEngine(self.memory,self.key,model=self.model)
        self.assertEqual(self.engine.process(copy.deepcopy(self.request)),result)
        self.assertEqual(self.memory.get(audit_key),audit)
        self.assertEqual(list(self.memory.db.execute('SELECT * FROM operations')),before)
        self.assertEqual(list(self.memory.db.execute('SELECT * FROM budgets')),budgets)
        self.assertEqual(len(self.calls),1)

    def test_rr_range_original_paid_unknown_is_reused_without_another_call(self):self.check_recovery(0)
    def test_inline_list_original_paid_unknown_is_reused_without_another_call(self):self.check_recovery(1)


if __name__=='__main__':unittest.main()
