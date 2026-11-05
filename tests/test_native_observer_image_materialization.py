"""A lazy posted PNG is loaded through its ordinary UI, never replaced."""
import copy
import json
from pathlib import Path
import unittest

from agent import native_received_analysis as observer
from agent.native_daily_room import JOURNAL_NAME
from tests.test_generic_received_analysis import GenericReceivedAnalysisTests
from tests.test_native_received_analysis import Message


class LazyImage:
    def __init__(self, case, name):self.case=case;self.name=name
    def get_attribute(self, key):
        return {'alt':self.name,'loading':'lazy','decoding':'async','fetchpriority':'auto'}.get(key)
    def is_visible(self):return True
    def evaluate(self, script):
        ready=self.case.ready_at.get(self.name)
        complete=ready is not None and self.case.case.raw.current>=ready
        return dict(complete=complete,width=1314 if complete else 0,height=738 if complete else 0)
    def scroll_into_view_if_needed(self, *, timeout):
        c=self.case
        self.case.assertGreater(timeout,0)
        captures=[json.loads(path.read_text())for path in c.case.root.glob('received-analysis-observation-*.json')]
        self.case.assertTrue(any(value.get('stage')=='BEFORE_IMAGE_MATERIALIZATION'
            and value.get('file_tiles') for value in captures))
        c.scrolls.append(self.name)
        if c.decode:
            c.ready_at[self.name]=c.case.raw.current+.1
        if c.mutate:c.mutate(c)


class LazyImages:
    def __init__(self, case, names):self.case=case;self.names=names
    def count(self):return len(self.names)
    def nth(self, index):return LazyImage(self.case,self.names[index])


class LazyMessage(Message):
    def __init__(self, case, row):super().__init__(row);self.case=case
    def locator(self, selector):
        if selector!='img':raise AssertionError('Unexpected selector')
        return LazyImages(self.case,self.row.get('display_names',[])[:2])


class ObserverImageMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=GenericReceivedAnalysisTests('test_doge_latest_reply_and_operation_scoped_proof_are_not_hardcoded_to_rune')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.case=self.fixture.case
        self.ready_at={};self.scrolls=[];self.decode=True;self.mutate=None
        old=self.case.room.original_visible
        def visible(selector):
            return [LazyMessage(self,row)for row in self.case.raw.messages]if selector=='messages'else old(selector)
        self.case.room.original_visible=visible

    def invoke(self):return self.fixture.invoke()

    def test_lazy_owned_images_decode_after_scroll_with_private_before_and_after_evidence(self):
        before=copy.deepcopy(self.case.raw.messages)
        result=self.invoke();proof=result['observation']
        self.assertEqual(result['error_count'],0)
        self.assertEqual(set(self.scrolls),set(self.fixture.names[:2]))
        self.assertEqual(len(self.scrolls),4)  # Both posted turns are witnessed separately.
        self.assertTrue(all(image['decoded']['complete'] and image['decoded']['width']==1314
            for tile in proof['file_tiles']for image in tile['images']))
        self.assertTrue(all(image['loading']=='lazy'for tile in proof['file_tiles']for image in tile['images']))
        self.assertEqual(proof['stable_snapshot_reads'],4)
        self.assertFalse(result['operation_completed'])
        self.assertEqual(self.case.raw.messages,before)
        self.assertEqual((self.case.root/JOURNAL_NAME).read_bytes(),self.fixture.before)
        values=[json.loads(path.read_text())for path in self.case.root.glob('received-analysis-observation-*.json')]
        initial=next(value for value in values if value['stage']=='BEFORE_IMAGE_MATERIALIZATION'
            and value.get('image_materialization',{}).get('actions')==[])
        self.assertTrue(all(image['decoded']['complete'] is False
            for tile in initial['file_tiles']for image in tile['images']))
        self.assertTrue(any(value['stage']=='AFTER_IMAGE_MATERIALIZATION'for value in values))
        self.assertTrue(all(value['provider_messages']==value['uploads']==value['model_calls']==0 for value in values))

    def test_never_decoded_image_remains_pending_with_loading_attributes_and_no_completion(self):
        self.decode=False
        result=self.invoke();proof=result['observation']
        self.assertEqual(result['error_count'],1)
        self.assertEqual(proof['errors'][0]['reason'],'RECEIVED_ANALYSIS_IMAGE_DECODE_PENDING')
        action=proof['image_materialization']['actions'][-1]
        self.assertEqual(action['last_observation']['loading'],'lazy')
        self.assertEqual(action['last_observation']['decoded'],dict(complete=False,width=0,height=0))
        self.assertLessEqual(self.case.raw.current,8)
        self.assertFalse(result['operation_completed'])
        self.assertEqual((self.case.root/JOURNAL_NAME).read_bytes(),self.fixture.before)

    def test_changed_message_id_url_or_manual_ownership_after_scroll_stops_loading(self):
        for change in ('id','url','manual'):
            with self.subTest(change=change):
                self.ready_at={};self.scrolls=[]
                old_rows=copy.deepcopy(self.case.raw.messages);old_url=self.case.raw.page.url
                def mutate(case):
                    if change=='id':case.case.raw.messages[-1]['id']='different-native-reply'
                    if change=='url':case.case.raw.page.url+='changed'
                    if change=='manual':case.case.actor.mode='MANUAL'
                self.mutate=mutate
                result=self.invoke()
                self.assertEqual(result['error_count'],1)
                self.assertFalse(result['operation_completed'])
                self.assertEqual(len(self.scrolls),1)
                self.case.raw.messages=old_rows;self.case.raw.page.url=old_url;self.case.actor.mode='AUTOMATION'
                self.assertEqual((self.case.root/JOURNAL_NAME).read_bytes(),self.fixture.before)

    def test_real_human_verification_after_scroll_is_reported_and_never_bypassed(self):
        self.mutate=lambda case:setattr(case.case,'status','CHALLENGE_REQUIRED')
        result=self.invoke()
        self.assertEqual(result['status'],'CHALLENGE_REQUIRED')
        self.assertEqual(result['error_count'],1)
        self.assertFalse(result['operation_completed'])
        self.assertEqual(len(self.scrolls),1)


if __name__=='__main__':unittest.main()
