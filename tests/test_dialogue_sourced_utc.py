"""Public cached composition with synthetic validated market context; no provider."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import unittest

from agent.dialogue_service import DialogueError,validate_question_sources,validate_web_question
from agent.native_market_context import convert_native_market_context
from tests.test_agent_native_market_context import native_context


class SourcedUtcQuestionTests(unittest.TestCase):
    def setUp(self):
        self.saved=json.loads((Path(__file__).parent/'fixtures/hermes_dot_composition_utc_cutoff_20261108.json').read_text())
        self.question=self.saved['question']
        self.now=datetime(2026,11,8,20,43,57,500000,tzinfo=timezone.utc).timestamp()
        source=native_context(self.now)
        source['symbol']='DOTUSDT'
        for frame in source['timeframes'].values():frame['symbol']='DOTUSDT'
        self.context=convert_native_market_context(source,now=self.now)
        # Exact source-approved values from the retained public proof, not
        # reconstructed private history or fabricated executable trade levels.
        self.request={'phase':'COMPOSE_ANALYSIS','reply':'DOTUSDT '+' '.join(self.saved['approved_question_values']),
            'market_context':self.context}

    def rejected(self,question,request=None):
        with self.assertRaisesRegex(DialogueError,'DIALOGUE_PRICE_PROPOSAL_REFUSED'):
            validate_question_sources(question,request or self.request)

    def test_actual_public_dot_question_accepts_the_source_utc_cutoff(self):
        self.assertEqual(hashlib.sha256(self.question.encode()).hexdigest(),self.saved['question_sha256'])
        before=deepcopy(self.request)
        validate_question_sources(self.question,self.request)
        self.assertEqual(validate_web_question(self.question,self.request),self.question)
        self.assertEqual(self.request,before)

    def test_valid_timestamp_does_not_authorize_its_numbers_as_prices(self):
        for suffix in(' ENTRY 20.',' SL 57.',' ENTRY 20 dan SL 57.'):
            self.rejected(self.question+suffix)

    def test_forged_date_time_missing_context_and_non_ascii_digits_remain_rejected(self):
        for old,new in(('2026','2027'),('8 November','7 November'),('November','October'),('20:43:57','21:43:57'),('20:43:57','20:43:56'),('20:43:57','２０:43:57')):
            with self.subTest(replacement=new):self.rejected(self.question.replace(old,new))
        self.rejected(self.question,{**self.request,'market_context':None})
        self.rejected(self.question,{**self.request,'market_context':{'server_time_ms':int(self.now*1000)}})

    def test_iso_and_full_utc_calendar_forms_match_only_the_same_source_instant(self):
        for stamp in('8 November 2026 20:43:57 UTC','08 November 2026 20:43:57 UTC',
                '8 Nov 2026 20:43:57 UTC','8Nov2026 20:43:57 UTC','November 8, 2026 20:43:57 UTC',
                '2026-11-08T20:43:57Z','2026-11-08T20:43:57+00:00',
                '2026-11-08T20:43:57.500Z','2026-11-08 20:43:57 UTC','2026-11-08 20:43:57+00:00',
                '2026-11-08T20:43:57 UTC'):
            with self.subTest(stamp=stamp):validate_question_sources('Cutoff '+stamp+'.',self.request)
        self.rejected('Cutoff 2026-11-08T20:43:56.500Z.')
        self.rejected('Cutoff 2026-11-08T20:43:57.501Z.')
        self.rejected('Jam 20:43:57 UTC.')

    def test_unvalidated_context_and_observed_time_do_not_replace_server_cutoff(self):
        invalid=deepcopy(self.request);invalid['market_context']['provenance']['candles_sha256']='0'*64
        self.rejected(self.question,invalid)
        changed=deepcopy(self.request)
        # Observed time differs, but the server cutoff still owns attribution.
        changed['market_context']['observed_at']+=1
        changed['market_context']['observed_at_utc']=datetime.fromtimestamp(changed['market_context']['observed_at'],timezone.utc).isoformat().replace('+00:00','Z')
        validate_question_sources(self.question,changed)
        self.rejected(self.question.replace('20:43:57','20:43:58'),changed)

    def fact_request(self,values):
        encoded=json.dumps(values,sort_keys=True,separators=(',',':')).encode()
        return {**self.request,'market_context':None,'source_facts':[{'producer':'OWNED_PENDING_ORDER',
            'operation':'pending:source-time-fixture','values':values,'sha256':hashlib.sha256(encoded).hexdigest()}]}

    def test_documented_public_temporal_facts_authorize_only_their_full_utc_renderings(self):
        question='Diterima 8 November 2026 20:43:57 UTC.'
        values=(('accepted_at','2026-11-08T20:43:57.500+00:00'),('observed_at',self.now),
            ('last_review_at','2026-11-08T20:43:57.500Z'),('next_review_at','2026-11-08T20:43:57.500Z'))
        for key,value in values:
            request=self.fact_request({key:value})
            with self.subTest(key=key):
                validate_question_sources(question,request)
                validate_web_question(question,request)
                self.rejected(question+' ENTRY 20 dan SL 57.',request)
        for values in({'accepted_at':int(self.now*1000)},{'next_review_at':self.now},
                {'entry_price':self.now},{'age_seconds':self.now},{'risk_usdt':self.now},
                {'accepted_at':'2026-11-08T20:43:57.500'},
                {'accepted_at':'2026-11-08T20:43:57.500+07:00'}):
            with self.subTest(values=values):self.rejected(question,self.fact_request(values))

    def test_indonesian_and_english_month_names_render_the_same_utc_source(self):
        now=datetime(2026,3,8,20,43,57,500000,tzinfo=timezone.utc).timestamp()
        source=native_context(now);source['symbol']='DOTUSDT'
        for frame in source['timeframes'].values():frame['symbol']='DOTUSDT'
        request={**self.request,'market_context':convert_native_market_context(source,now=now)}
        for month in('Maret','March','maret','MARCH'):
            with self.subTest(month=month):validate_question_sources('Cutoff 8 '+month+' 2026 20:43:57 UTC.',request)

    def test_year_boundary_and_month_aliases_remain_exactly_source_bound(self):
        now=datetime(2026,12,31,23,59,59,875000,tzinfo=timezone.utc).timestamp()
        source=native_context(now);source['symbol']='DOTUSDT'
        for frame in source['timeframes'].values():frame['symbol']='DOTUSDT'
        request={**self.request,'market_context':convert_native_market_context(source,now=now)}
        for stamp in('31 Desember 2026 23:59:59 UTC','31 Des 2026 23:59:59 UTC',
                '31 Dec 2026 23:59:59 UTC','December 31, 2026 23:59:59 UTC',
                '2026-12-31T23:59:59.875Z','2026-12-31 23:59:59.875+00:00'):
            with self.subTest(stamp=stamp):
                validate_question_sources('Cutoff '+stamp+'.',request)
                self.rejected('Cutoff '+stamp+'. ENTRY 20 dan SL 57.',request)
        for stamp in('31 Desember 2027 23:59:59 UTC','31 Desember 2026 23:59:58 UTC',
                '2027-01-01T00:00:00Z'):
            with self.subTest(stamp=stamp):self.rejected('Cutoff '+stamp+'.',request)
