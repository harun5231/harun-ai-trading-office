"""Historical redelivery only: controlled local UI and immutable receipt fixtures."""
import copy
import hashlib
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from agent.native_chart_followup import ReceiptStore,sha
from agent.native_evaluation_browser import HistoricalEvaluation,HistoricalFileDelivery
from agent.native_evaluation_protocol import validate_material_access_chain,native_arguments
from tests.test_native_evaluation_protocol import prepared,ROOM,TEXT
from tests.test_native_file_upload import Fixture,NotSubmitted
from tests.test_trade_evaluation_evidence import NOW
from worker.trade_evaluation_evidence import digest


class HistoricalDeliveryTests(unittest.TestCase):
    def test_real_existing_file_menu_delivers_recorded_csvs_with_once_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            f,claim,args=prepared(directory);self.addCleanup(f.coordinator.db.close)
            args.update(marker='HARUN_EVALUATION',request_sha256='c'*64)
            ui=Fixture();ui._fresh_conversation=Mock();ui.page.url='https://fixture.invalid/owned-room'
            native=SimpleNamespace(image_artifacts=Mock(),user_prompt=lambda _:args['prompt'],
                NotSubmitted=NotSubmitted,MESSAGE='messages',USER_BODY='native-user-body')
            db=sqlite3.connect(':memory:');self.addCleanup(db.close)
            db.execute('CREATE TABLE daily_rooms(day TEXT,room_id TEXT,state TEXT)')
            db.execute('INSERT INTO daily_rooms VALUES(?,?,?)',('2026-11-01',ROOM,'BOUND'))
            room=SimpleNamespace(_connection=db,day='2026-11-01',room_id=ROOM,_prepared=True,
                store=SimpleNamespace(root=directory),full_turns=lambda:[],verify_prefix=Mock())
            store=ReceiptStore(directory,monotonic=ui.clock)
            def receipt(**kwargs):
                ui.actions.append('intent-'+kwargs['receipt'].get('action','completion'))
                return store(**kwargs)
            owner=SimpleNamespace(daily_room=room,bridge=ui,native=native,followup_receipt=receipt,
                wall_clock=lambda:NOW+100000)
            evaluation=HistoricalEvaluation(owner,args,100)
            pngs=[r for r in f.archive['files'] if r['kind']=='PNG']
            charts=__import__('json').loads(f.archive['delivery'] and f.coordinator.db.execute(
                'SELECT chart_evidence FROM research_browser_responses').fetchone()[0])
            csvs=[]
            for row in f.archive['files']:
                if row['kind']=='CSV':
                    csvs.append({**row,'symbol':'BTCUSDT','mimeType':'text/csv','sha256':hashlib.sha256(row['buffer']).hexdigest()})
            group=dict(paths=['/original/15m.png','/original/1h.png'],pngs=pngs,csvs=csvs,
                chart_evidence=charts,captured_at={r['timeframe']:r['captured_at'] for r in charts},symbol='BTCUSDT')
            evaluation.active=0;evaluation.groups[0]=group
            bound=[dict(row,attachment_id='new-ui-'+row['timeframe']) for row in charts]
            def native_upload(*_args):
                ui.actions.append('native-png-upload')
                ui.uploaded_names=[args['marker']+'-'+r['timeframe']+'-'+r['sha256'][:12]+'.png' for r in charts]
                return copy.deepcopy(bound)
            ui._upload=native_upload
            delivery=HistoricalFileDelivery(evaluation,lambda:None).install();self.addCleanup(delivery.close)
            with patch('agent.native_file_upload.deliver_market_data_files',side_effect=AssertionError('fresh candles requested')):
                result=ui._upload(ui.composer,args,[(p['name'],p['buffer'],c) for p,c in zip(pngs,charts)])
            self.assertEqual(result,bound)
            self.assertEqual(ui.actions[:2],['intent-PREPARE_MATERIAL','native-png-upload'])
            self.assertEqual(ui.files,[{k:r[k] for k in ('name','mimeType','buffer')} for r in csvs])
            self.assertEqual(ui.sent,0);self.assertEqual(len(evaluation.intents),1)
            prompt='Materi HISTORIS transaksi asli, bukan pasar sekarang.'
            evaluation.before_send(prompt,transcript=[])
            self.assertEqual([r['action'] for r in evaluation.intents],['PREPARE_MATERIAL','SEND_MATERIAL'])
            self.assertEqual(evaluation.intents[-1]['bound_charts'],bound)
            turns=[{'role':'user','text':prompt,'attachments':list(ui.uploaded_names)},
                {'role':'assistant','text':TEXT,'complete':True,'id':'ui-native-reply-1'}]
            evaluation.note_reply(TEXT,turns)
            final=dict(assistant_text=TEXT,response_id='genuine-native-fixture',chart_evidence=bound)
            self.assertEqual(evaluation.complete(final),bound)
            actual=__import__('agent.native_evaluation_rpc',fromlist=('evaluation_chain',)).evaluation_chain(
                directory,args['operation'],args['request_sha256'])
            self.assertEqual(len(actual),3)
            self.assertEqual(actual[0]['complete']['response_id'],'ui-native-reply-1')
            self.assertEqual(actual[-1]['complete']['response_id'],'genuine-native-fixture')
            self.assertNotEqual(actual[0]['complete']['response_id'],actual[-1]['complete']['response_id'])
            self.assertEqual(actual[0]['complete']['final_reply_sha256'],hashlib.sha256(TEXT.encode()).hexdigest())
            # Source filenames do not become actual new attachment identities.
            self.assertNotEqual(pngs[0]['name'],evaluation.intents[-2]['attachment_names'][0])

    def test_failed_upload_leaves_durable_unknown_plan_and_no_send_witness(self):
        with tempfile.TemporaryDirectory() as directory:
            f,claim,args=prepared(directory);self.addCleanup(f.coordinator.db.close)
            args.update(marker='HARUN_EVALUATION',request_sha256='c'*64)
            ui=Fixture();ui._fresh_conversation=Mock();ui.page.url='https://fixture.invalid/owned-room'
            native=SimpleNamespace(image_artifacts=Mock(),user_prompt=Mock(),NotSubmitted=NotSubmitted,
                MESSAGE='messages',USER_BODY='native-user-body')
            db=sqlite3.connect(':memory:');self.addCleanup(db.close)
            db.execute('CREATE TABLE daily_rooms(day TEXT,room_id TEXT,state TEXT)')
            db.execute('INSERT INTO daily_rooms VALUES(?,?,?)',('2026-11-01',ROOM,'BOUND'))
            room=SimpleNamespace(_connection=db,day='2026-11-01',room_id=ROOM,_prepared=True,
                store=SimpleNamespace(root=directory),full_turns=lambda:[],verify_prefix=Mock())
            store=ReceiptStore(directory,monotonic=ui.clock)
            owner=SimpleNamespace(daily_room=room,bridge=ui,native=native,followup_receipt=store,wall_clock=lambda:NOW+100000)
            evaluation=HistoricalEvaluation(owner,args,100);evaluation.active=0
            csvs=[dict(row,symbol='BTCUSDT',mimeType='text/csv',sha256=hashlib.sha256(row['buffer']).hexdigest())
                for row in f.archive['files'] if row['kind']=='CSV']
            evaluation.groups[0]={'csvs':csvs}
            ui._upload=Mock(side_effect=RuntimeError('actual upload outcome unknown'))
            delivery=HistoricalFileDelivery(evaluation,lambda:None).install();self.addCleanup(delivery.close)
            with self.assertRaises(RuntimeError):ui._upload(ui.composer,args,[('old.png',b'x',{})])
            self.assertEqual(evaluation.intents[0]['action'],'PREPARE_MATERIAL')
            with self.assertRaisesRegex(ValueError,'CHAIN_INCOMPLETE'):
                __import__('agent.native_evaluation_rpc',fromlist=('evaluation_chain',)).evaluation_chain(
                    directory,args['operation'],args['request_sha256'])
            self.assertEqual(ui.sent,0)

    def test_three_groups_mixed_reuse_and_sequential_delivery_require_complete_set(self):
        with tempfile.TemporaryDirectory() as directory:
            f,claim,args=prepared(directory);self.addCleanup(f.coordinator.db.close)
            original=claim['request']['historical_materials'][0]
            materials=[]
            for n in range(3):
                value=copy.deepcopy(original);value['delivery_custody']['operation']='original-analysis-'+str(n)
                materials.append(value)
            request={**claim['request'],'historical_materials':materials}
            reused=dict(material_sha256=digest(materials[0]),native_completion_sha256=materials[0]['delivery_custody']['native_completion_sha256'],
                assistant_sha256=materials[0]['delivery_custody']['assistant_sha256'],attachment_names=['old15m.png','old1h.png','old15m.csv','old1h.csv'])
            chain=[];allcharts=[]
            for n in (1,2):
                material=materials[n];key=digest(material)
                prepare=dict(action='PREPARE_MATERIAL',response_id_kind='NATIVE_UI_MESSAGE_ID',material_sha256=key,marker='CURRENT',reused_materials=[reused] if n==1 else [],
                    planned_files=[{k:r[k] for k in ('kind','timeframe','sha256','bytes')} for r in material['files']])
                reply_sha=hashlib.sha256(('raw intermediate '+str(n)).encode()).hexdigest()
                chain.append({'intent':prepare,'complete':dict(prepare,intent_sha256=sha(prepare),
                    response_id='ui-response-'+str(n),final_reply_sha256=reply_sha)})
                pngs=sorted([r for r in material['files'] if r['kind']=='PNG'],key=lambda r:r['timeframe'])
                csvs=sorted([r for r in material['files'] if r['kind']=='CSV'],key=lambda r:r['timeframe'])
                charts=[{**{k:r[k] for k in ('source','timeframe','captured_at','sha256','bytes')},
                    'symbol':'BTCUSDT','attachment_id':str(n)+'-'+r['timeframe']} for r in pngs]
                send=dict(action='SEND_MATERIAL',response_id_kind='NATIVE_UI_MESSAGE_ID',marker='CURRENT',reused_materials=[],material_sha256=key,
                    prepared_intent_sha256=sha(prepare),bound_charts=charts,rendered_prompt_sha256='d'*64,
                    attachment_names=['CURRENT-'+r['timeframe']+'-'+r['sha256'][:12]+'.png' for r in charts]+[r['name'] for r in csvs])
                chain.append({'intent':send,'complete':dict(send,intent_sha256=sha(send),
                    response_id='ui-response-'+str(n),final_reply_sha256=reply_sha)});allcharts.extend(charts)
            final_response=dict(response_id='actual-final-native-response',assistant_sha256=reply_sha)
            final=dict(action='FINAL_ASSESSMENT',response_id_kind='NATIVE_FINAL_RESPONSE_ID',
                source_chain_sha256=digest(chain),final_ui_message_id='ui-response-2',final_ui_reply_sha256=reply_sha)
            chain.append({'intent':final,'complete':dict(final,intent_sha256=sha(final),
                response_id=final_response['response_id'],final_reply_sha256=reply_sha)})
            self.assertEqual(len(validate_material_access_chain(request,chain,allcharts,final_response=final_response)['materials']),3)
            with self.assertRaisesRegex(ValueError,'INCOMPLETE'):
                validate_material_access_chain(request,chain,allcharts[-2:],final_response=final_response)
            with self.assertRaises(ValueError):
                validate_material_access_chain(request,chain[:-2],allcharts[:-2],final_response=final_response)
            duplicate=copy.deepcopy(chain[:-1]);duplicate.extend(chain[-3:-1]);duplicate.append(chain[-1])
            with self.assertRaises(ValueError):
                validate_material_access_chain(request,duplicate,allcharts+allcharts[-2:],final_response=final_response)
            rewritten=copy.deepcopy(chain);rewritten[1]['complete']['response_id']='ui-response-2'
            with self.assertRaises(ValueError):
                validate_material_access_chain(request,rewritten,allcharts,final_response=final_response)


class FreshNativeUploadABITests(unittest.TestCase):
    def test_exact_runtime_upload_source_keeps_original_buffers_and_new_native_ids(self):
        import json
        from tests.test_native_file_upload import Area,Items
        path=Path(__file__).parent/'fixtures/native_evaluation_upload_abi.json'
        fixture=json.loads(path.read_text())
        self.assertEqual(fixture['sha256'],'c842713f886992af7289bc6503b57ef6d06fc59dcb85f3be7cc1230ef6fd1071')
        source=fixture['functions']['_upload']['source']
        self.assertEqual(hashlib.sha256(source.encode()).hexdigest(),fixture['functions']['_upload']['sha256'])
        namespace={'NotSubmitted':NotSubmitted,'RAIL_INPUT_GROUP':object(),'hashlib':hashlib}
        exec(compile(source,'source-pinned-native-dom-upload','exec'),namespace)
        ui=Fixture();ui._dom_contract.picker='original-native-picker'
        selected=[]
        picker=SimpleNamespace(count=lambda:1,set_input_files=lambda files,**kwargs:selected.extend(files))
        class PNGArea(Area):
            def locator(self,selector):
                return picker if selector=='original-native-picker' else super().locator(selector)
        ui.composer.locator=lambda _:PNGArea(ui)
        ui._attachments_ready=lambda container,names,artifacts:(selected==[
            dict(name=name,mimeType='image/png',buffer=raw) for name,(_,raw,_) in zip(names,artifacts)])
        witnesses=[dict(source='BINANCE_USDM_FUTURES_WEBSITE_SCREENSHOT',symbol='BTCUSDT',timeframe=tf,
            captured_at='2026-10-01T00:00:00Z',sha256=hashlib.sha256(tf.encode()).hexdigest(),bytes=len(tf))
            for tf in ('15m','1h')]
        artifacts=[(Path('/original/'+r['timeframe']+'.png'),r['timeframe'].encode(),r) for r in witnesses]
        result=namespace['_upload'](ui,ui.composer,{'marker':'CURRENT_EVAL'},artifacts)
        self.assertEqual([row['buffer'] for row in selected],[b'15m',b'1h'])
        self.assertEqual([row['name'] for row in selected],[
            'CURRENT_EVAL-'+r['timeframe']+'-'+r['sha256'][:12]+'.png' for r in witnesses])
        self.assertEqual([{k:v for k,v in row.items() if k!='attachment_id'} for row in result],witnesses)
        self.assertTrue(all(row['attachment_id'].startswith('dom-') for row in result))
        self.assertEqual(ui.sent,0)

    def test_historical_scope_restores_exact_live_freshness_function(self):
        import json,os,re
        from datetime import datetime,timezone
        from tests.test_trade_evaluation_archive import HistoricalArchiveCustodyTests
        from worker.trade_evaluation_archive import read_bound_material_files
        archival=HistoricalArchiveCustodyTests();archival.setUp();self.addCleanup(archival.doCleanups)
        group=read_bound_material_files(archival.root,archival.manifest(),charts=archival.reader.charts)
        abi=json.loads((Path(__file__).parent/'fixtures/native_evaluation_upload_abi.json').read_text())
        source=abi['functions']['image_artifacts']['source']
        namespace={'NotSubmitted':NotSubmitted,'Path':Path,'os':os,'re':re,'hashlib':hashlib,
            'datetime':datetime,'timezone':timezone,'png_valid':lambda _:True}
        exec(compile(source,'source-pinned-native-live-artifacts','exec'),namespace)
        original=namespace['image_artifacts']
        with self.assertRaises(NotSubmitted):original(group['paths'],group['chart_evidence'])
        with tempfile.TemporaryDirectory() as directory:
            f,claim,args=prepared(directory);self.addCleanup(f.coordinator.db.close)
            # This test isolates scope restoration; archive ownership/content
            # verification is exercised by the actual helper call above.
            db=sqlite3.connect(':memory:');self.addCleanup(db.close)
            db.execute('CREATE TABLE daily_rooms(day TEXT,room_id TEXT,state TEXT)')
            db.execute('INSERT INTO daily_rooms VALUES(?,?,?)',('2026-11-01',ROOM,'BOUND'))
            composer=SimpleNamespace(input_value=lambda:'')
            fresh=Mock(return_value=composer)
            bridge=SimpleNamespace(_fresh_conversation=fresh,uploaded_names=[],page=SimpleNamespace(url='https://fixture.invalid/owned'))
            native=SimpleNamespace(image_artifacts=original,user_prompt=lambda _:args['prompt'],NotSubmitted=NotSubmitted)
            room=SimpleNamespace(_connection=db,day='2026-11-01',room_id=ROOM,_prepared=True,
                fresh=fresh,store=SimpleNamespace(root=directory),full_turns=lambda:[],verify_prefix=Mock())
            owner=SimpleNamespace(daily_room=room,bridge=bridge,native=native,followup_receipt=Mock(return_value=True))
            evaluation=HistoricalEvaluation(owner,args,100);evaluation.groups[0]=group
            with patch.object(evaluation,'_turn_witness',side_effect=ValueError('EVALUATION_HISTORICAL_TURN_NOT_READABLE_IN_ROOM')):
                paths,rows=evaluation.prepare_room()
            artifacts=native.image_artifacts(paths,rows)
            self.assertEqual([a[1] for a in artifacts],[r['buffer'] for r in group['pngs']])
            self.assertTrue(all(isinstance(a[0],Path) for a in artifacts))
            evaluation.close();self.assertIs(native.image_artifacts,original);self.assertIs(bridge._fresh_conversation,fresh)
            with self.assertRaises(NotSubmitted):native.image_artifacts(paths,rows)
