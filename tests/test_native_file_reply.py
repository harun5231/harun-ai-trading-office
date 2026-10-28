"""Native PNG arguments must still identify the submitted four-file user turn."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from agent.native_browser_hook import DelegatedDOMBridge
from tests.test_native_file_upload import Fixture, NotSubmitted
from tests.test_agent_closed_candles_200 import NOW, native_context_200


class OutcomeUnknown(Exception): pass


class StrictNativeBridge(Fixture):
    def __init__(self):
        super().__init__()
        self.url='https://app.neurobro.ai/chat/owned'
        self.before_callback=None
        self.status_value='CONNECTED'
    def status(self): return self.status_value
    def ask(self,payload,dispatch_check,*,chart_images=(),chart_evidence=(),deadline=None,receipt_callback=None):
        self.rows=[]
        bound=self._upload(self.composer,payload,[])
        prompt=payload['prompt']
        # This is the native DOM.ask contract: derive the PNG-only argument
        # from the image artifacts, regardless of additional data uploads.
        native_names=['15m.png','1h.png']
        if dispatch_check()is not True: raise NotSubmitted('RESEARCH_PAUSED')
        self.sent+=1
        self.rows=[{'role':'user','text':prompt},
            {'role':'assistant','text':'NEUROBRO ACTUAL ANSWER','complete':True}]
        previous=None;stable=0
        while self.current < deadline:
            answer=self._answer(prompt,native_names)
            stable=stable+1 if answer is not None and answer==previous else 0
            previous=answer
            if answer is not None and stable>=3:
                if receipt_callback is not None:
                    if self.before_callback:self.before_callback(self)
                    self.last_native_receipt={'rendered_prompt':prompt,'assistant_text':answer,
                        'response_id':'native-owned-response','chart_evidence':bound,
                        'attachment_names':native_names}
                    receipt_callback(self.last_native_receipt)
                return {'output':{},'assistant_text':answer,
                    'response_id':'native-owned-response','chart_evidence':bound}
            self.wait_for_timeout(250)
        raise OutcomeUnknown()


def native():
    def user_prompt(payload): return payload['prompt']
    user_prompt.__harun_data_files_v1__=True
    return SimpleNamespace(NotSubmitted=NotSubmitted,OutcomeUnknown=OutcomeUnknown,
        MESSAGE='messages',USER_BODY='native-user-body',MAX_ANSWER=65536,
        user_prompt=user_prompt,image_artifacts=lambda *_:[],
        exact_json=lambda *_:{'symbol':'ZECUSDT','side':'HOLD'})


def reasoner():
    def respond(request):
        return {'schema_version':1,'ok':True,
            **{key:request[key]for key in ('request_id','conversation_id','phase','reply_sha256')},
            'authority':'DIALOGUE_CONTROL_ONLY','trading_authority':False,
            'decision':{'action':'COMPLETE','symbols':[],'question':''},
            'needs_followup':False,'reason':'DIALOGUE_DECISION'}
    return SimpleNamespace(request=Mock(side_effect=respond))


class FourFileNativeReplyTests(unittest.TestCase):
    def setup(self):
        bridge=StrictNativeBridge();client=reasoner()
        hook=DelegatedDOMBridge(bridge,client,native=native(),monotonic=bridge.clock,
            wall_clock=lambda:NOW)
        payload={'operation':'off-analysis','marker':'native-marker','request_sha256':'a'*64,
            'prompt':'ORIGINAL OWNER ANALYSIS PROMPT','context':native_context_200(),
            'output_schema':{'required':['symbol','side']}}
        return bridge,client,hook,payload

    def test_native_png_argument_matches_four_files_before_mini_review(self):
        bridge,client,hook,payload=self.setup()
        result=hook.ask(payload,lambda:True,deadline=80)
        self.assertEqual(result['assistant_text'],'NEUROBRO ACTUAL ANSWER')
        self.assertEqual(bridge.sent,1);client.request.assert_called_once()
        self.assertEqual(client.request.call_args.args[0]['reply'],result['assistant_text'])
        self.assertEqual(len(bridge._turns()[0]['attachments']),4)
        self.assertEqual(bridge._answer,bridge.native_answer)

    def test_off_callback_consumes_once_and_verifies_csv_before_unchanged_native_receipt(self):
        bridge,client,hook,payload=self.setup();callback=Mock();calls=[]
        witness_counts=[]
        bridge.before_callback=lambda b:witness_counts.append(len(b.witnesses))
        callback.side_effect=lambda _:witness_counts.append(len(bridge.witnesses))
        def consume():
            calls.append('CONSUME')
            if len(calls)>1:raise AssertionError('one-use authorization consumed twice')
            return True
        result=hook.ask(payload,consume,deadline=80,receipt_callback=callback)
        self.assertEqual(calls,['CONSUME']);self.assertEqual(bridge.sent,1)
        client.request.assert_not_called();callback.assert_called_once()
        receipt=callback.call_args.args[0]
        self.assertIs(receipt,bridge.last_native_receipt)
        self.assertEqual(receipt['attachment_names'],['15m.png','1h.png'])
        self.assertGreater(witness_counts[1],witness_counts[0])
        self.assertEqual(len(bridge._turns()[0]['attachments']),4)
        self.assertEqual(receipt['assistant_text'],result['assistant_text'])
        self.assertEqual(receipt['response_id'],result['response_id'])
        self.assertEqual(receipt['chart_evidence'],result['chart_evidence'])

    def test_final_stopped_authority_does_not_send_or_reconsume(self):
        bridge,client,hook,payload=self.setup();consume=Mock(return_value=False);callback=Mock()
        with self.assertRaises(NotSubmitted):
            hook.ask(payload,consume,deadline=80,receipt_callback=callback)
        consume.assert_called_once();self.assertEqual(bridge.sent,0)
        callback.assert_not_called();client.request.assert_not_called()

    def test_removed_csv_tiles_cannot_be_attested_in_native_receipt(self):
        bridge,client,hook,payload=self.setup();callback=Mock()
        bridge.before_callback=lambda b:setattr(b,'file_tiles',False)
        with self.assertRaises(NotSubmitted):
            hook.ask(payload,lambda:True,deadline=80,receipt_callback=callback)
        self.assertEqual(bridge.sent,1);callback.assert_not_called()

    def test_receipt_uses_same_crlf_and_outer_whitespace_rules_as_native_answer(self):
        bridge,client,hook,payload=self.setup();callback=Mock()
        payload['prompt']='ORIGINAL OWNER\nANALYSIS PROMPT\n'
        bridge.before_callback=lambda b:b.rows[0].update(
            text='\r\n'+payload['prompt'].replace('\n','\r\n')+'\r\n')
        hook.ask(payload,lambda:True,deadline=80,receipt_callback=callback)
        callback.assert_called_once()
        self.assertEqual(callback.call_args.args[0]['rendered_prompt'],payload['prompt'])
        self.assertIs(callback.call_args.args[0],bridge.last_native_receipt)
        self.assertEqual(callback.call_args.args[0]['attachment_names'],['15m.png','1h.png'])
        self.assertEqual(len(bridge._turns()[0]['attachments']),4)

    def test_expired_callback_budget_consumes_nothing(self):
        bridge,client,hook,payload=self.setup();consume=Mock(return_value=True)
        with self.assertRaises(OutcomeUnknown):
            hook.ask(payload,consume,deadline=0,receipt_callback=Mock())
        consume.assert_not_called();self.assertEqual(bridge.sent,0)
        self.assertEqual(bridge.actions,[])


if __name__=='__main__':unittest.main()
