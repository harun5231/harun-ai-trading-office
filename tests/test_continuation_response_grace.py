"""HTTP response grace never extends a continuation's Send authority."""
import copy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent import native_screening_recovery as recovery
from agent import native_received_continuation as continuation
from tests import test_native_received_continuation as fixtures


class ContinuationResponseGraceTests(unittest.TestCase):
    def test_continuation_waits_two_seconds_for_terminal_receipt_without_changing_grant(self):
        now=1793879000.0;waits=[]
        payload=dict(operation='2026-11-05:robot-v9:0:analysis-v9:DOGEUSDT',request_sha256='a'*64)
        grant=dict(grant_id='b'*64,started_at=now-895,expires_at=now+5)
        value=dict(action=continuation.ACTION,payload=payload,grant=grant);original=copy.deepcopy(value)
        actor=SimpleNamespace(stopping=threading.Event(),claim=threading.Lock(),lock=threading.RLock(),
            sessions=SimpleNamespace(active=lambda:False),mode='AUTOMATION',current_status='CONNECTED',ready=True,
            _bridge=SimpleNamespace(native=SimpleNamespace()),_wake=SimpleNamespace(set=lambda:None))
        def queue(task):
            task['result']=dict(kind=continuation.NOT_SENT,operation=payload['operation'],
                request_sha256=payload['request_sha256'],grant_id=grant['grant_id'])
            actor.claim.release() # Existing actor finally owns this release.
        actor.queue=SimpleNamespace(put_nowait=queue)
        event=SimpleNamespace(wait=lambda seconds:waits.append(seconds)or True)
        with patch.object(continuation,'validate',return_value=(payload,grant)),\
                patch.object(recovery.threading,'Event',return_value=event),\
                patch.object(recovery.time,'time',return_value=now):
            self.assertEqual(recovery.enqueue(actor,value)['kind'],continuation.NOT_SENT)
        self.assertEqual(waits,[7.0]);self.assertEqual(value,original)
        # Grant validation remains expired even while HTTP may still await a
        # terminal receipt. No clock or expiry is rewritten by the grace.
        fixture=fixtures.ReceivedProofTests('test_private_capture_and_current_four_file_native_witness_seed_exact_existing_turns')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        complete=dict(action=continuation.ACTION,payload=fixture.payload,grant=fixture.grant)
        with self.assertRaisesRegex(ValueError,'CONTINUATION_EXPIRED'):
            continuation.validate(complete,now=fixture.grant['expires_at']+1)


if __name__=='__main__':unittest.main()
