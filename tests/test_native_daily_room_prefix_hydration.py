"""Source-bound prefix hydration and native polling; all page actions are OFFLINE."""
import copy
from contextlib import nullcontext
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent import native_daily_room as daily
from tests import test_native_daily_room as legacy

FIXTURE=json.loads((Path(__file__).parent/'fixtures/native_daily_prefix_assistant_ids_20261126.json').read_text())


def rows():
    result=[]
    for index in range(4):
        result.extend([{'role':'user','text':'Synthetic prior request '+str(index),'id':'synthetic-user-'+str(index),'attachments':[]},
                       {'role':'assistant','text':'Synthetic prior authored answer '+str(index),'complete':True,'id':'synthetic-assistant-'+str(index)}])
    for change in FIXTURE['prefix_evidence']['changes']:result[change['row']]['id']=change['prior_id']
    return result


def hydrate(value):
    for change in FIXTURE['prefix_evidence']['changes']:value[change['row']]['id']=change['observed_id']
    return value


class DailyRoomPrefixHydrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.bridge=legacy.Bridge(rows());self.room=daily.DailyRoom(self.bridge,legacy.native(),self.temp.name)
        self.room.day='2026-11-26';self.room.room_id=FIXTURE['journal']['room_id']
        self.room.offset=8;self.room.prefix=daily.identities(rows())
        self.snapshot=daily.retain_prefix_snapshot(self.temp.name,self.room.day,self.room.room_id,rows())

    def test_actual_three_id_changes_are_bound_to_unchanged_text_and_user_rows(self):
        evidence=FIXTURE['prefix_evidence'];self.assertTrue(evidence['all_user_rows_exact'])
        self.assertEqual([row['row']for row in evidence['changes']],[3,5,7])
        for change in evidence['changes']:
            self.assertEqual(change['changed_fields'],['id'])
            self.assertEqual(change['prior_text_sha256'],change['observed_text_sha256'])
            self.assertTrue(change['prior_complete']);self.assertTrue(change['observed_complete'])
        self.assertEqual(FIXTURE['submitted_state'],'UNKNOWN')

    def test_turns_accept_completed_assistant_ids_only_without_changing_prefix_or_snapshot(self):
        before=copy.deepcopy(self.room.prefix);path=Path(self.temp.name)/self.snapshot['name'];raw=path.read_bytes()
        self.bridge.messages=hydrate(rows())+[{'role':'user','text':'Synthetic current request'},
            {'role':'assistant','text':'Synthetic current reply','complete':True,'id':'synthetic-current'}]
        with patch.object(daily,'prefix_snapshot_matches',wraps=daily.prefix_snapshot_matches)as check:
            self.assertEqual(self.room.turns(),self.bridge.messages[8:])
        self.assertEqual(check.call_args.kwargs,{'binding':None,'allow_window':False})
        self.assertEqual(self.room.prefix,before);self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(self.bridge.clicks,0);self.assertEqual(self.bridge.fresh_calls,0)

    def test_exact_identity_fast_path_does_not_need_or_read_snapshot(self):
        with patch.object(daily,'prefix_snapshot_matches',side_effect=AssertionError('exact path must stay exact')):
            self.assertEqual(self.room.turns(),[])

    def test_changed_user_or_authored_fields_order_and_incomplete_assistant_are_rejected(self):
        mutations=(lambda r:r[0].update(id='changed-user'),lambda r:r[2].update(text='Changed user text'),
            lambda r:r[3].update(text='Changed assistant text'),lambda r:r[3].update(role='unknown'),
            lambda r:r[3].update(complete=False),lambda r:r[3].update(extra='changed-field'),
            lambda r:r[3].update(id='invalid id'),lambda r:r.__setitem__(slice(2,4),r[4:6]),
            lambda r:r.reverse())
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                observed=hydrate(rows());mutate(observed);self.bridge.messages=observed
                with self.assertRaises(daily.DailyRoomUnavailable):self.room.turns()

    def test_missing_truncated_or_inconsistent_prefix_never_uses_history_window(self):
        for count in (0,2,6):
            with self.subTest(count=count):
                self.bridge.messages=hydrate(rows())[-count:]if count else[]
                with self.assertRaises(daily.DailyRoomUnavailable):self.room.turns()
        self.bridge.messages=hydrate(rows());self.room.offset=9
        with self.assertRaises(daily.DailyRoomUnavailable):self.room.turns()
        self.room.offset=7
        with self.assertRaises(daily.DailyRoomUnavailable):self.room.turns()

    def test_missing_or_tampered_snapshot_cannot_authorize_hydration(self):
        self.bridge.messages=hydrate(rows());path=Path(self.temp.name)/self.snapshot['name'];original=path.read_bytes()
        for altered in (None,b'{}',original.replace(b'Synthetic prior authored answer 1',b'Forged prior authored answer 1')):
            with self.subTest(altered=altered):
                if path.exists():path.unlink()
                if altered is not None:path.write_bytes(altered);path.chmod(0o600)
                with self.assertRaises(daily.DailyRoomUnavailable):self.room.turns()
        path.write_bytes(original);path.chmod(0o600)

    def test_snapshot_id_admission_cannot_apply_html_aliases_to_user_text(self):
        self.bridge.messages=hydrate(rows());self.bridge.messages[0]['text']='Synthetic prior request 0 <SIMBOL_USDT_1>'
        self.room._binding={'marker':'synthetic-marker','first_prompt_sha256':'a'*64}
        with self.assertRaises(daily.DailyRoomUnavailable):self.room.turns()

    def native_ask_case(self,old_guard=False):
        ask=FIXTURE['native_ask_evidence'];answer=FIXTURE['deployed_answer']
        self.assertEqual(hashlib.sha256(ask['source'].encode()).hexdigest(),ask['method_sha256'])
        self.assertEqual(hashlib.sha256(answer['source'].encode()).hexdigest(),answer['source_sha256'])
        scope={'NotSubmitted':legacy.NotSubmitted,'OutcomeUnknown':legacy.OutcomeUnknown,
               'hashlib':hashlib,'time':time,'MAX_ANSWER':65536,'image_artifacts':lambda *_:[],
               'request_kind':lambda *_:'SCREENING','user_prompt':legacy.rendered,
               'exact_json':lambda *_:{'symbols':['BTCUSDT']}}
        exec(compile(ask['source']+'\n\n'+answer['source'],'retained-source-pinned-native-DOM','exec'),scope)
        class NativeBridge(legacy.Bridge):pass
        NativeBridge.ask=scope['ask'];NativeBridge._answer=scope['_answer']
        bridge=NativeBridge(rows());bridge.button=legacy.Button(bridge)
        base_click=bridge.button.click
        def click(**kw):base_click(**kw);hydrate(bridge.messages)
        bridge.button.click=click
        now=datetime(2026,11,26,15,tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory()as directory:
            room=daily.DailyRoom(bridge,legacy.native(),directory,wall_clock=lambda:now,history=legacy.History(bridge))
            def exact_only(observed):daily.need(len(observed)>=room.offset and daily.identities(observed[:room.offset])==room.prefix)
            guard=patch.object(room,'verify_prefix',side_effect=exact_only)if old_guard else patch.object(room,'verify_prefix',wraps=room.verify_prefix)
            receipts=[]
            expected=self.assertRaises(legacy.OutcomeUnknown)if old_guard else nullcontext()
            with expected,guard,room.operation(legacy.request()):
                result=bridge.ask(legacy.request(),lambda:True,deadline=80,receipt_callback=receipts.append)
                if not old_guard:
                    self.assertEqual(result['assistant_text'],'FINAL_NATIVE_REPLY BTCUSDT')
                    self.assertEqual(len(receipts),1)
                    self.assertEqual(room.prefix,daily.identities(rows()))
                    state,anchors=room._connection.execute('SELECT state,anchors FROM daily_rooms').fetchone()
                    self.assertEqual(state,'SENT');self.assertEqual(json.loads(anchors),daily.identities(rows()))
                    self.assertGreaterEqual(bridge.current,0.75)
                self.assertEqual(bridge.clicks,1);self.assertEqual(bridge.fresh_calls,0)
                self.assertEqual(bridge.messages[:8],hydrate(rows()))
            self.assertEqual(bridge.clicks,1);self.assertEqual(bridge.fresh_calls,0)
            self.assertEqual(bridge.messages[:8],hydrate(rows()))

    def test_exact_old_guard_reproduces_post_send_unknown_without_a_second_send(self):self.native_ask_case(True)
    def test_source_pinned_native_poll_finishes_original_send_after_prefix_ids_hydrate(self):self.native_ask_case()


if __name__=='__main__':unittest.main()
