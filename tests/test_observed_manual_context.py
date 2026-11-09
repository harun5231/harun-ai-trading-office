"""A real formatting-only manual pair is context, never Send or trading approval."""
import copy
import hashlib
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from agent import native_received_continuation as c
from agent import native_received_analysis as o
from agent.native_daily_room import canonical as room_wire,identities
from tests import test_native_received_continuation as fixtures
from tests import test_native_lost_not_sent_custody as custody_fixtures


def manual_pair(symbol='DOGEUSDT'):
    return [dict(role='user',text='berikan format json coin tp sl',attachments=[]),
        dict(role='assistant',id='72356a4f-79d6-4744-bbca-5a75394ab61d',complete=True,
            text=json.dumps(dict(coin=symbol,keputusan=None,entry=None,tp=None,sl=None,
                alasan='Belum menetapkan setup pasar: gambar belum membuktikan bahwa 200 candle tertutup terlihat.'),ensure_ascii=False))]


class ManualContextTests(unittest.TestCase):
    def setUp(self):
        self.f=custody_fixtures.LostNotSentCustodyTests('test_actual_private_positive_closure_is_returned_with_exact_custody_and_no_writes')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.case=self.f.case;self.root=self.f.root;self.payload=self.f.payload
        source=copy.deepcopy(self.case.capture)
        # Actual incident shape: 16 earlier rows, four owned rows, then the
        # owner's formatting request and a complete null technical response.
        prefix=[]
        for number in range(5):prefix.extend([dict(role='user',text='Earlier context '+str(number),attachments=[]),
            dict(role='assistant',text='Earlier answer '+str(number),complete=True,id='earlier-extra-'+str(number))])
        source['rows'][6:6]=prefix
        for key in('initial_user','latest_user','reply_row'):source['operation_scope'][key]+=10
        source['operation_scope']['user_rows']=[i+10 for i in source['operation_scope']['user_rows']]
        for key in('initial_analysis_user_rows','followup_user_rows'):
            source['room_witness'][key]=[i+10 for i in source['room_witness'][key]]
        for tile in source['file_tiles']:tile['row']+=10
        source['rows'].extend(manual_pair(self.payload['context']['symbol']))
        source['finished_at']=self.case.grant['started_at']+2
        source['manual_context_tail']=c.manual_context_tail(source,self.payload)
        source['continuation_not_sent']=c.read_not_sent(self.root,self.payload,source)
        self.assertIsNotNone(source['continuation_not_sent'])
        saved=o._save(self.root,source)
        self.grant=dict(self.case.grant,source_capture_name=saved['capture_name'],source_capture_sha256=saved['capture_sha256'],
            source_capture_bytes=saved['capture_bytes'],source_observation_sha256=o.digest(o._project_operation(source)),
            started_at=source['finished_at']+1,expires_at=source['finished_at']+901)
        self.source=source;self.current=copy.deepcopy(source);self.current['finished_at']+=2

    def persist(self):
        c._persist_manual_context(self.root,self.payload,self.grant,self.source,self.current)
        entries=c.read_manual_contexts(self.root,self.payload);self.assertEqual(len(entries),1)
        self.current['manual_context_receipts']=entries
        return entries[0]

    def worker(self):
        from worker.analysis_continuation import SCHEMA
        db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row;db.execute(SCHEMA);self.addCleanup(db.close)
        original=o._project_operation(self.source)
        envelope=dict(capture_name=self.grant['source_capture_name'],capture_sha256=self.grant['source_capture_sha256'],
            capture_bytes=self.grant['source_capture_bytes'],observation_sha256=self.grant['source_observation_sha256'],observation=original)
        db.execute('INSERT INTO analysis_continuation_grants VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (self.grant['grant_id'],self.payload['operation'],self.payload['request_sha256'],json.dumps(self.grant),
                json.dumps(envelope),'{}','{}','GRANTED',None,self.grant['started_at'],self.grant['started_at']))
        return SimpleNamespace(db=db)

    def test_actual_22_rows_project_owned_four_and_preserve_complete_manual_room(self):
        original=copy.deepcopy(self.current['rows']);entry=self.persist()
        self.assertEqual(entry['value']['kind'],c.MANUAL_CONTEXT_KIND)
        self.assertEqual(entry['value']['authority'],'CONTEXT_ONLY_NO_SEND_NO_APPROVAL')
        self.assertEqual(len(self.current['rows']),22)
        seed=c._seed(self.current,self.payload,self.grant)
        self.assertEqual(len(seed['rows']),4);self.assertEqual(seed['offset'],16)
        self.assertEqual(seed['full_rows'],original)
        self.assertEqual(seed['manual_contexts'][0]['relative_row'],4)
        projected=o._project_operation(self.current)
        self.assertEqual(len(projected['rows']),4)
        self.assertEqual(projected['manual_context_observations'][0]['rows'],manual_pair(self.payload['context']['symbol']))
        self.assertEqual(projected['rows'][-1]['text'],original[19]['text'])
        self.assertEqual(original,self.current['rows'])
        from worker.analysis_continuation import validate_manual_context_receipts
        verified=validate_manual_context_receipts(self.worker(),projected,self.payload)
        self.assertEqual(verified[0]['after_owned_pairs'],2)
        # The manual JSON isn't a reply candidate or price source.
        self.assertTrue(all(row['text']!=manual_pair()[1]['text']for row in projected['rows']))

    def test_new_author_reply_after_fresh_followup_has_six_owned_rows_and_preserves_manual_context(self):
        entry=self.persist();value=self.current
        prompt='Chart Binance Futures DOGEUSDT terbaru 1H dan 15M. '+self.payload['marker']
        value['rows'].extend([dict(role='user',text=prompt,attachments=[]),
            dict(role='assistant',text='COIN: DOGEUSDT\nARAH: HOLD\nKeputusan pasar.',complete=True,id='new-author-after-material')])
        receipts=copy.deepcopy(value['receipt_files'])
        receipts['new.2.intent.json']=dict(value=dict(followup_sequence=2,followup_prompt_sha256=o.text_sha(prompt)))
        scope=o._operation_scope(value['rows'],value['room_witness'],receipts,
            manual_contexts=value['manual_context_receipts'],payload=self.payload)
        self.assertTrue(scope['verified']);self.assertEqual(scope['user_rows'],[16,18,22])
        value['operation_scope']=scope;value['room_witness']['followup_user_rows']=[18,22]
        # The UI file witness for the new turn remains at its physical row.
        tile=copy.deepcopy(value['file_tiles'][-1]);tile.update(row=22,prompt_sha256=o.text_sha(prompt));value['file_tiles'].append(tile)
        projected=o._project_operation(value)
        self.assertEqual(len(value['rows']),24);self.assertEqual(len(projected['rows']),6)
        self.assertEqual(projected['operation_scope']['user_rows'],[0,2,4])
        self.assertEqual(projected['file_tiles'][-1]['row'],4)
        self.assertEqual(projected['rows'][-1]['id'],'new-author-after-material')
        self.assertEqual(projected['manual_context_observations'][0]['after_owned_pairs'],2)
        from worker.analysis_continuation import validate_manual_context_receipts
        self.assertEqual(len(validate_manual_context_receipts(self.worker(),projected,self.payload)),1)

    def test_actor_checkpoint_retains_the_real_24_row_room_including_manual_pair(self):
        actor=self.case.fixture.case;actor.actor.dispatch_check=lambda payload:True
        actor.delegated.native_turn_batch=None
        actor.raw.messages=copy.deepcopy(self.current['rows'])
        with actor.room.store.open()as db:
            db.execute('UPDATE daily_rooms SET anchors=? WHERE day=?',
                (room_wire(identities(actor.raw.messages)),self.current['room']['day']))
        saved=o._save(self.root,self.current)
        answer='COIN: DOGEUSDT\nARAH: HOLD\nKeputusan pasar.'
        def native_followup(payload,check,**kwargs):
            seed=kwargs['_received'];self.assertEqual(len(seed['rows']),4)
            self.assertEqual(len(seed['full_rows']),22)
            self.assertTrue(list(self.root.glob('observed-manual-context-*.json')))
            seed['before_upload']();seed['before_send']()
            actor.raw.messages.extend([dict(role='user',text='New chart-backed followup',attachments=[]),
                dict(role='assistant',id='actual-new-test-author',text=answer,complete=True)])
            return dict(assistant_text=answer,response_id='dom-test-new-author',output={'side':'HOLD'},chart_evidence=[])
        actor.delegated._ask=Mock(side_effect=native_followup)
        observed=dict(saved,error_count=0,status='CONNECTED')
        with actor.actor.claim,patch.object(o,'observe',return_value=observed), \
                patch.object(c.time,'time',return_value=self.grant['started_at']+2):
            result=c.continue_received(actor.actor,actor.delegated,dict(action=c.ACTION,payload=self.payload,grant=self.grant))
        self.assertEqual(result['kind'],c.KIND)
        with actor.room.store.open()as db:
            anchors=json.loads(db.execute('SELECT anchors FROM daily_rooms WHERE day=?',(self.current['room']['day'],)).fetchone()[0])
        self.assertEqual(len(anchors),24);self.assertEqual(anchors,identities(actor.raw.messages))
        self.assertEqual(actor.raw.messages[20:22],manual_pair(self.payload['context']['symbol']))

    def test_later_unclosed_admission_prevents_manual_context_adoption(self):
        c._admit(self.root,self.case.grant,self.case.grant['source_capture_name'])
        with self.assertRaises(ValueError):
            c._persist_manual_context(self.root,self.payload,self.grant,self.source,self.current)
        self.assertEqual(list(self.root.glob('observed-manual-context-*.json')),[])

    def test_no_positive_closure_changed_tail_foreign_coin_nonnull_or_incomplete_refuses(self):
        mutations=(lambda value:value.pop('continuation_not_sent'),
            lambda value:value['rows'][-1].update(complete=False),
            lambda value:value['rows'][-1].update(text=json.dumps(dict(coin='ETHUSDT',keputusan=None,entry=None,tp=None,sl=None,alasan='Gambar belum terbaca'))),
            lambda value:value['rows'][-1].update(text=json.dumps(dict(coin=self.payload['context']['symbol'],keputusan='SHORT',entry=1,tp=0.8,sl=1.1,alasan='Gambar belum terbaca'))),
            lambda value:value['rows'][-2].update(text='berikan format json coin tp sl SHORT sekarang'))
        for mutate in mutations:
            value=copy.deepcopy(self.current);mutate(value)
            value['manual_context_tail']=c.manual_context_tail(value,self.payload)
            if value['manual_context_tail']is None:
                with self.assertRaises(ValueError):c._seed(value,self.payload,self.grant)
            else:
                with self.assertRaises(ValueError):c._persist_manual_context(self.root,self.payload,self.grant,self.source,value)
        self.assertEqual(list(self.root.glob('observed-manual-context-*.json')),[])

    def test_every_operation_read_checks_physical_uuid_text_and_no_new_tail_is_hidden(self):
        self.persist();seed=c._seed(self.current,self.payload,self.grant);rows=copy.deepcopy(self.current['rows'][16:])
        self.assertEqual(identities(c.filter_manual_operation_rows(rows,seed['manual_contexts'])),identities(seed['rows']))
        for change in (lambda value:value[4].update(text='Different manual prompt'),
                lambda value:value[5].update(id='00000000-0000-4000-8000-000000000000'),
                lambda value:value[5].update(complete=False)):
            value=copy.deepcopy(rows);change(value)
            with self.assertRaises(ValueError):c.filter_manual_operation_rows(value,seed['manual_contexts'])
        rows.extend([dict(role='user',text='Unowned new message',attachments=[])])
        self.assertEqual(c.filter_manual_operation_rows(rows,seed['manual_contexts'])[-1]['text'],'Unowned new message')
        # A new unowned row survives filtering and therefore fails the original
        # received-seed end guard instead of silently becoming trusted context.
        changed=copy.deepcopy(self.current);changed['rows'].append(rows[-1])
        with self.assertRaises(ValueError):c._seed(changed,self.payload,self.grant)

    def test_worker_rejects_forged_witness_null_prices_and_source_binding(self):
        self.persist();proof=o._project_operation(self.current);provider=self.worker()
        from worker.analysis_continuation import validate_manual_context_receipts
        from worker.core import Review
        for mutate in (lambda v:v['manual_context_receipts'][0]['value']['context']['rows'][1].update(text='{"coin":"ETHUSDT"}'),
                lambda v:v['manual_context_observations'][0]['rows'][1].update(complete=False),
                lambda v:v['manual_context_receipts'][0]['value']['grant'].update(source_observation_sha256='f'*64),
                lambda v:v['room'].update(room_id='f'*32)):
            changed=copy.deepcopy(proof);mutate(changed)
            from worker.analysis_response_recovery import wire,sha
            for entry in changed['manual_context_receipts']:
                raw=wire(entry['value']);entry.update(sha256=sha(raw),bytes=len(raw.encode()))
            with self.assertRaises((Review,ValueError,KeyError)):validate_manual_context_receipts(provider,changed,self.payload)

    def test_historical_projection_digest_is_unchanged_without_any_context_witness(self):
        # Historical grants hash the old exact projection; absent new witness
        # fields must not gain empty defaults or scope keys during upgrade.
        old=copy.deepcopy(self.case.capture)
        result=o._project_operation(old)
        self.assertNotIn('manual_context_receipts',result)
        self.assertNotIn('manual_context_observations',result)
        self.assertEqual(o.digest(result),self.case.grant['source_observation_sha256'])


class HookManualContextTests(fixtures.ReceivedHookTests):
    def test_existing_fresh_followup_starts_at_real_end_with_context_not_fake_intent(self):
        case,seed,calls=self.harness(prior_followup=True)
        pair=manual_pair('BTCUSDT');pair[0]['role']=case.bridge.messages[0]['role'];pair[1]['role']=case.bridge.messages[1]['role']
        case.bridge.messages.extend(copy.deepcopy(pair));before=copy.deepcopy(case.bridge.messages)
        original_turns=case.bridge._turns
        def actual_plain_manual_projection():
            rows=original_turns()
            for row in rows:
                if row.get('text')==pair[0]['text']:
                    row['attachments']=['UNCONFIRMED']if case.bridge.uploaded_names else[]
            return rows
        case.bridge._turns=actual_plain_manual_projection
        seed['manual_contexts']=[dict(relative_row=4,rows=pair,receipt=dict(name='observed-manual-context-fixture.json',sha256='a'*64,bytes=400))]
        result=self.run_received(case,seed)
        self.assertEqual(case.bridge.messages[:6],before)
        self.assertEqual(case.bridge.clicks,2)
        self.assertEqual(len(case.bridge.messages),8)
        self.assertEqual(result['output']['side'],'HOLD')
        intents=[value for stage,_,value in case.events if stage=='INTENT']
        self.assertEqual(len(intents),1)
        self.assertEqual(intents[0]['followup_sequence'],2)
        self.assertEqual(intents[0]['manual_context_receipts'],[seed['manual_contexts'][0]['receipt']])
        self.assertIn('BTCUSDT',case.bridge.messages[-2]['text'])
        self.assertIn('COIN, ARAH, ENTRY, TP, SL',case.bridge.messages[-2]['text'])
        self.assertIn('Jika keputusanmu sudah final',case.bridge.messages[-2]['text'])
        # The newly received, source-authored market HOLD is final. Its
        # verified native attachments/receipt do not need another paid review.
        case.reasoner.request.assert_not_called()
        case.bridge.ask.assert_not_called();case.bridge._fresh_conversation.assert_not_called()
