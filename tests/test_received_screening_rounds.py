"""Any screening round is read/reviewed in place without replaying WEB Send."""
import copy
import json
import time
import unittest
from unittest.mock import Mock

from agent import native_received_screening as screening
from agent import native_received_analysis as observer
from agent.native_browser_hook import DelegatedDOMBridge
from agent.native_daily_room import identities
from agent.native_screening_recovery import recover,clarification
from agent.native_analysis_receipt import digest,read_reviews
from tests import test_native_received_analysis as legacy
from worker.account_state import screening_contract
from worker.prompts import REPLACEMENT_SCREENING


class ReceivedScreeningRoundsTests(unittest.TestCase):
    def setUp(self):
        self.case=legacy.ReceivedAnalysisObservationTests('test_current_ten_rows_four_file_tiles_and_status_are_saved_without_completion')
        self.case.setUp();self.addCleanup(self.case.doCleanups)
        c=self.case;_,schema=screening_contract(1)
        operation='2026-11-05:robot-v9:0:screening:9:1'
        body=dict(operation=operation,marker='HARUN_'+observer.text_sha(operation)[:32],provider='NEUROBRO_WEB',
            contract_version='neurobro-browser-decision-v2',prompt=REPLACEMENT_SCREENING.format(count=1),output_schema=schema,
            context=dict(requested_count=1,excluded_symbols=['AVAXUSDT','INJUSDT'],
                replacement_instruction='Choose different Binance USD-M USDT perpetual coins that are not in excluded_symbols.'))
        self.payload={**body,'request_sha256':observer.digest(body)}
        rendered=c.cache.store(self.payload,body['prompt'],'a'*64)
        c.raw.messages=c.raw.messages[:6]+[dict(role='user',text=rendered,attachments=[],display_names=[]),
            dict(role='assistant',text='COIN: SOLUSDT\nLikuiditas dan momentum sedang baik.',complete=True,id='screen9-reply')]
        self.anchor();self.before=list(copy.deepcopy(c.raw.messages))
        self.request=dict(action=screening.OBSERVE,payload=self.payload)

    def anchor(self):
        with self.case.room.store.open()as db:
            db.execute('UPDATE daily_rooms SET anchors=?',(json.dumps(identities(self.case.raw._turns())),))

    def invoke(self):
        with self.case.actor.claim:return recover(self.case.actor,self.case.delegated,self.request)

    def test_round9_reads_actual_owned_reply_in_same_bound_room_without_model_or_web_send(self):
        result=self.invoke();proof=result['observation']
        self.assertEqual(result['kind'],screening.KIND);self.assertFalse(result['operation_completed'])
        self.assertEqual(proof['row_offset'],6);self.assertEqual(len(proof['rows']),2)
        self.assertEqual(proof['rows'][-1]['id'],'screen9-reply')
        self.assertEqual(proof['room_witness']['initial_screening_user_rows'],[0])
        self.assertTrue(proof['operation_scope']['verified']);self.assertTrue(proof['operation_scope']['reply_complete'])
        self.assertEqual(proof['file_tiles'],[]);self.assertEqual(proof['review_receipts'],[])
        self.assertEqual(self.case.raw.messages,self.before)

    def test_unproved_extra_user_turn_does_not_become_owned_screening_context(self):
        c=self.case;c.raw.messages.extend([dict(role='user',text='Unbound manual question',attachments=[]),
            dict(role='assistant',text='COIN: ETHUSDT',complete=True,id='foreign-turn')]);self.anchor()
        result=self.invoke()
        self.assertFalse(result['observation']['operation_scope']['verified'])
        self.assertFalse(result['operation_completed'])

    def test_existing_exact_coin_clarification_is_scoped_without_new_send(self):
        c=self.case;c.raw.messages.extend([dict(role='user',text=clarification(1),attachments=[]),
            dict(role='assistant',text='COIN: SOLUSDT',complete=True,id='screen9-summary')]);self.anchor()
        result=self.invoke();proof=result['observation']
        self.assertEqual(len(proof['rows']),4);self.assertTrue(proof['operation_scope']['verified'])
        self.assertEqual(proof['room_witness']['followup_user_rows'],[2])

    def install_review(self,*,clarify=False):
        c=self.case
        def native_answer(prompt,names=()):
            rows=c.raw._turns()
            return rows[-1]['text']if len(rows)==2 and rows[0]['text']==prompt and rows[-1].get('complete')is True else None
        c.raw._answer=native_answer
        class Client:
            supports_screening_receipt_lookup=True
            def __init__(client):client.calls=0;client.done=None
            def completed_screening(client,request):return copy.deepcopy(client.done)
            def request(client,request):
                client.calls+=1
                response=dict(schema_version=1,ok=True,**{k:request[k]for k in('request_id','conversation_id','phase','reply_sha256')},
                    authority='DIALOGUE_CONTROL_ONLY',trading_authority=False,
                    decision=dict(action='CLARIFY'if clarify else'SCREENING',symbols=[]if clarify else['SOLUSDT'],
                        question='Coin mana yang dipilih?'if clarify else''),needs_followup=clarify,reason='DIALOGUE_DECISION')
                row=dict(operation_id='delegated-model:'+request['request_id'],kind='DELEGATED_MODEL_DIALOGUE',
                    request_hash=digest(request),state='DONE',result=json.dumps(response),updated_at=time.time())
                client.done=dict(result=response,operation=row);return response
        client=Client();hook=DelegatedDOMBridge(c.raw,client,native=c.native,daily_room=c.room,
            monotonic=c.raw.clock,wall_clock=time.time,compose_enabled=True)
        c.delegated=hook;c.actor._bridge=hook
        return client

    def review(self,result):
        request=dict(action=screening.REVIEW,payload=self.payload,
            source_capture_name=result['capture_name'],source_capture_sha256=result['capture_sha256'],
            source_capture_bytes=result['capture_bytes'],source_observation_sha256=result['observation_sha256'])
        with self.case.actor.claim:return recover(self.case.actor,self.case.delegated,request)

    def test_existing_review_path_calls_mini_once_and_caches_real_done_without_web_send(self):
        client=self.install_review();observed=self.invoke();result=self.review(observed)
        self.assertEqual(result['kind'],screening.REVIEW_KIND)
        self.assertEqual(result['native_result']['output'],dict(symbols=['SOLUSDT']))
        self.assertEqual(client.calls,1);self.assertEqual(self.case.raw.messages,self.before)
        saved=read_reviews(self.case.root,self.payload)
        self.assertEqual(saved[0]['value']['model_operation']['state'],'DONE')
        # A second review reads the actual DONE row, never pays Mini twice.
        self.review(self.invoke());self.assertEqual(client.calls,1)

    def test_review_clarification_is_typed_incomplete_and_cannot_fill_or_send(self):
        client=self.install_review(clarify=True);result=self.review(self.invoke())
        self.assertEqual(result['kind'],screening.INCOMPLETE);self.assertFalse(result['operation_completed'])
        self.assertEqual(client.calls,1);self.assertEqual(self.case.raw.messages,self.before)
        self.assertTrue(read_reviews(self.case.root,self.payload)[0]['value']['response']['needs_followup'])


if __name__=='__main__':unittest.main()
