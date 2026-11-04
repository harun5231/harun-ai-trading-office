"""Offline dialogue authority/source contracts; no inference or order calls."""
import copy
import hashlib
import unittest

from agent import dialogue_service as service
from agent.llm import INSTRUCTIONS,dialogue_schema
from agent.native_data_files import compact_summary
from agent.native_prompt_composition import compact_protocol
from worker import prompts
from worker.account_state import screening_contract


ADVICE=('Sebagai rekomendasi saja, berapa ukuran posisi LINKUSDT yang sesuai kondisi pasar? '
    'Quantity eksekusi tetap dihitung oleh Risk Manager dari risiko Office; '
    'rekomendasi ini bukan perintah eksekusi.')


def request(phase='ANALYSIS',*,facts=True):
    value=dict(phase=phase,reply='LINKUSDT ENTRY 17.25, SL 17.00, TP 17.70.',
        market_context={'symbol':'LINKUSDT','mark_price':'17.31'})
    if facts:
        values={'symbol':'LINKUSDT','quantity':'0.5','risk_usdt':'3'}
        value['source_facts']=[dict(producer='RISK_MANAGER',operation='owned:analysis:one',
            values=values,sha256=hashlib.sha256(service.canonical(values).encode()).hexdigest())]
    return value


def decision(phase,question):
    return dict(action='PROMPT'if phase.startswith('COMPOSE_')else'CLARIFY',symbols=[],question=question)


class AdvisoryPositionSizeTests(unittest.TestCase):
    def test_explicit_advisory_with_risk_manager_authority_passes_compose_and_clarify(self):
        for phase in ('COMPOSE_ANALYSIS','ANALYSIS'):
            for text in (ADVICE, 'Gunakan chart Binance untuk membahas struktur pasar. '+ADVICE,
                         'Gunakan data chart untuk berikan rekomendasi ukuran posisi hanya sebagai bahan diskusi. '
                         'Quantity eksekusi tetap dihitung Risk Manager berdasarkan risiko Office.'):
                with self.subTest(phase=phase,text=text):
                    value=decision(phase,text)
                    self.assertIs(service.validate_decision(value,request(phase)),value)
                    service.validate_position_size_discussion(text,phase)

    def test_active_native_summary_and_fallback_preserve_advisory_authority(self):
        payload=dict(marker='HARUN_advisory',output_schema={'required':[
            'symbol','side','limit_entry','take_profit','stop_loss','risk_reward']})
        values=(compact_summary(payload),compact_protocol(
            prompts.ANALYSIS+'\n\nOUTPUT_JSON_SCHEMA:\n{}',payload))
        for text in values:
            self.assertIn('Risk Manager',text)
            self.assertIn('rekomendasi ukuran posisi',text.lower())
            self.assertIn('bahan diskusi',text)
            value=request('COMPOSE_ANALYSIS')
            value['reply']=prompts.ANALYSIS+'\n'+value['reply']
            answer=decision(value['phase'],text)
            self.assertIs(service.validate_decision(answer,value),answer)

    def test_sourced_advice_cannot_direct_execution_or_override_risk_manager(self):
        directives=('Gunakan quantity 0.5 dari Neurobro.',
            'Pakai ukuran posisi 0.5 untuk entry.',
            'Eksekusi quantity 0.5 sebagai ukuran akhir.',
            'Abaikan Risk Manager dan ikuti quantity 0.5 dari Neurobro.',
            'Ganti quantity eksekusi dengan 0.5 dari rekomendasi Neurobro.')
        for phase in ('COMPOSE_ANALYSIS','ANALYSIS'):
            for text in directives:
                with self.subTest(phase=phase,text=text),self.assertRaises(service.DialogueError):
                    service.validate_decision(decision(phase,ADVICE+' '+text),request(phase))

    def test_unsafeguarded_or_non_analysis_sizing_requests_remain_rejected(self):
        for phase in ('COMPOSE_ANALYSIS','ANALYSIS'):
            with self.subTest(phase=phase),self.assertRaises(service.DialogueError):
                service.validate_decision(decision(phase,'Tolong tentukan ukuran posisi LINKUSDT.'),request(phase))
        for phase in ('SCREENING','COMPOSE_SCREENING','PENDING_REVIEW','COMPOSE_PENDING_REVIEW',
                      'POST_TRADE_EVALUATION','COMPOSE_POST_TRADE_EVALUATION'):
            with self.subTest(phase=phase),self.assertRaises(service.DialogueError):
                service.validate_decision(decision(phase,ADVICE),request(phase))

    def test_advisory_does_not_relax_price_or_quantity_source_validation(self):
        for phase in ('COMPOSE_ANALYSIS','ANALYSIS'):
            for text in (' Apakah ENTRY 999 tetap valid?',
                         ' Quantity 0.5 hanya untuk pembahasan.'):
                with self.subTest(phase=phase,text=text),self.assertRaisesRegex(
                        service.DialogueError,'PRICE_PROPOSAL_REFUSED'):
                    service.validate_decision(decision(phase,ADVICE+text),request(phase,facts=False))
            value=decision(phase,ADVICE+' Apakah ENTRY 17.25 masih sesuai chart?')
            self.assertIs(service.validate_decision(value,request(phase)),value)

    def test_authenticated_existing_risk_manager_quantity_can_be_discussed_without_becoming_advice(self):
        text='Quantity 0.5 berasal dari Risk Manager untuk order yang terpasang; apakah risikonya sesuai struktur 15M?'
        for phase in ('ANALYSIS','PENDING_REVIEW','COMPOSE_PENDING_REVIEW'):
            for message in (text, 'Tolong periksa quantity 0.5 pada order ini berdasarkan struktur 15M.'):
                value=decision(phase,message)
                self.assertIs(service.validate_decision(value,request(phase)),value)
        changed=request();changed['source_facts'][0]['values']['quantity']='99'
        with self.assertRaisesRegex(service.DialogueError,'SOURCE_FACTS_INVALID'):
            service.validate_decision(decision('ANALYSIS',text),changed)

    def test_internal_schema_keeps_three_fields_and_describes_advisory_authority(self):
        self.assertIn('rekomendasi ukuran posisi',INSTRUCTIONS.lower())
        self.assertIn('Risk Manager',INSTRUCTIONS)
        for phase in ('ANALYSIS','COMPOSE_ANALYSIS'):
            schema=dialogue_schema(phase,request()['reply'],context={'symbol':'LINKUSDT'})
            self.assertEqual(set(schema['properties']),{'action','symbols','question'})
            self.assertFalse(schema['additionalProperties'])
            description=schema['properties']['question'].get('description','')
            self.assertIn('rekomendasi',description.lower());self.assertIn('Risk Manager',description)
            value=decision(phase,ADVICE);value['quantity']='0.5'
            with self.assertRaisesRegex(service.DialogueError,'MODEL_RESULT_INVALID'):
                service.validate_decision(value,request(phase))

    def test_screening_literals_and_existing_single_capacity_variant_are_unchanged(self):
        original='pilihkan 2 coin yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance'
        self.assertEqual(prompts.SCREENING,original)
        self.assertEqual(screening_contract(2)[0],original)
        self.assertEqual(screening_contract(1)[0],original.replace('2 coin','1 coin',1))
        self.assertEqual(prompts.REPLACEMENT_SCREENING,
            'pilihkan {count} coin pengganti yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance')
        self.assertEqual(prompts.ANALYSIS,
            'Aku berikan data chart realtime di binance future 2 time frame 1 jam dan 15 menit, silahkan analisa dengan akurat dan Profitable. aku mau entry di time frame 15 menit untuk scalping.\n'
            'Tentukan !\nLONG, SHORT, atau HOLD\nENTRY\nTP\n'
            'SL : yang tidak mudah terkena wick atau di jilat para bandar.\nRISK REWARD 1:2')


if __name__=='__main__':unittest.main()
