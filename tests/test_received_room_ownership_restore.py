"""A foreign nonempty SPA chat cannot block the owned received operation."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agent.native_daily_room import JOURNAL_NAME
from tests import test_received_screening_rounds as rounds
from tests import test_generic_received_analysis as generic


class ReceivedRoomOwnershipRestoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture=rounds.ReceivedScreeningRoundsTests()
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.c=self.fixture.case
        self.owned=copy.deepcopy(self.c.raw.messages)
        self.original_journal=(self.c.root/JOURNAL_NAME).read_bytes()
        evidence=json.loads((Path(__file__).parent/'fixtures/screen3_foreign_btc_room_20261114.json').read_text())
        self.foreign=evidence['rows']
        self.assertEqual([hashlib.sha256(row['text'].encode()).hexdigest()for row in self.foreign],evidence['row_text_sha256'])
        self.assertTrue(all('BTCUSDT'in row['text']or row['role']=='assistant'for row in self.foreign))
        self.c.room.history.rooms=[('Analisis RUNE',self.owned)]

    def test_actual_foreign_btc_nonempty_same_spa_url_restores_original_bound_room(self):
        self.c.raw.messages=copy.deepcopy(self.foreign)
        with patch.object(self.c.room,'restore',wraps=self.c.room.restore)as restore:
            result=self.fixture.invoke()
        self.assertEqual(restore.call_args.args[2],'BOUND')
        self.assertEqual(restore.call_args.kwargs['prefix_identity'],('2026-11-05','a'*32))
        self.assertEqual(self.c.raw.page.url,'https://app.neurobro.ai/')
        self.assertTrue(result['history_restore_attempted']);self.assertTrue(result['history_restored'])
        self.assertTrue(result['observation']['operation_scope']['verified'])
        self.assertEqual(result['observation']['rows'][-1]['id'],'screen9-reply')
        self.assertFalse(result['operation_completed'])
        self.assertEqual((self.c.root/JOURNAL_NAME).read_bytes(),self.original_journal)

    def test_owned_nonempty_room_does_not_navigate_or_restore(self):
        with patch.object(self.c.room,'restore',side_effect=AssertionError('Owned room must stay loaded')):
            result=self.fixture.invoke()
        self.assertFalse(result['history_restore_attempted']);self.assertFalse(result['history_restored'])
        self.assertTrue(result['observation']['operation_scope']['verified'])
        self.assertEqual(self.c.room.history.selected,[])

    def test_empty_room_still_uses_same_existing_history_restore(self):
        self.c.raw.messages=[]
        result=self.fixture.invoke()
        self.assertTrue(result['history_restored']);self.assertTrue(result['observation']['operation_scope']['verified'])
        self.assertEqual(self.c.room.history.selected,['Analisis RUNE'])

    def test_foreign_history_without_owned_match_refuses_without_completion_or_send(self):
        self.c.raw.messages=copy.deepcopy(self.foreign)
        self.c.room.history.rooms=[('Analisis RUNE',copy.deepcopy(self.foreign))]
        result=self.fixture.invoke()
        self.assertTrue(result['history_restore_attempted']);self.assertFalse(result['history_restored'])
        self.assertGreater(result['error_count'],0);self.assertFalse(result['operation_completed'])
        self.assertEqual((self.c.root/JOURNAL_NAME).read_bytes(),self.original_journal)
        self.assertEqual(self.c.raw.fresh_calls,0);self.assertEqual(self.c.raw.clicks,0)

    def test_challenge_never_attempts_history_interaction(self):
        self.c.raw.messages=copy.deepcopy(self.foreign);self.c.status='CHALLENGE_REQUIRED'
        with patch.object(self.c.room,'restore',side_effect=AssertionError('Human gate')):
            result=self.fixture.invoke()
        self.assertFalse(result['history_restore_attempted']);self.assertFalse(result['operation_completed'])
        self.assertEqual((self.c.root/JOURNAL_NAME).read_bytes(),self.original_journal)

    def test_sent_restore_keeps_existing_seal_and_restart_idempotence(self):
        sent=generic.SentRoomCompletionObservationTests();sent.setUp();self.addCleanup(sent.doCleanups)
        c=sent.c;owned=copy.deepcopy(c.raw.messages);before=sent.row()
        c.room.history.rooms=[('Analisis RUNE',owned)];c.raw.messages=copy.deepcopy(self.foreign)
        with patch.object(c.room,'restore',wraps=c.room.restore)as restore:
            result=sent.invoke()
        self.assertEqual(restore.call_args.args[2],'SENT');self.assertTrue(result['history_restored'])
        self.assertEqual(result['error_count'],0);self.assertEqual(sent.row()['state'],'BOUND')
        for key in('day','room_id','url','binding'):self.assertEqual(sent.row()[key],before[key])
        after=sent.row();c.raw.messages=copy.deepcopy(self.foreign)
        with patch.object(c.room,'restore',wraps=c.room.restore)as restore:
            restarted=sent.invoke()
        self.assertEqual(restore.call_args.args[2],'BOUND');self.assertTrue(restarted['history_restored'])
        self.assertEqual(sent.row(),after);self.assertFalse(restarted['operation_completed'])
        self.assertEqual((restarted['observation']['provider_messages'],restarted['observation']['model_calls'],restarted['observation']['orders_sent']),(0,0,0))


if __name__=='__main__':unittest.main()
