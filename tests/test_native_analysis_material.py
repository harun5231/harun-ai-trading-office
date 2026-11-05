"""Upload preview survives restart without being relabeled a Send receipt."""
import copy
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from agent.native_analysis_material import retain_prepared,read_prepared,validate_prepared,digest
from agent.native_file_upload import NativeDataFileDelivery
from tests import test_native_file_upload as upload
from tests.test_agent_closed_candles_200 import NOW,native_context_200


class PreparedAnalysisMaterialTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.f=upload.Fixture();self.context=native_context_200();symbol=self.context['symbol']
        body=dict(operation='2026-11-05:robot-v9:0:analysis-v9:'+symbol,provider='NEUROBRO_WEB',contract_version='neurobro-browser-decision-v2',
            prompt='Analisa '+symbol,context=self.context,output_schema=dict(required=['symbol','side','limit_entry','take_profit','stop_loss','risk_reward']))
        body['marker']='HARUN_'+hashlib.sha256(body['operation'].encode()).hexdigest()[:32]
        self.payload={**body,'request_sha256':hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()}
        self.charts=[dict(source='BINANCE_USDM_FUTURES_WEBSITE_SCREENSHOT',symbol=symbol,timeframe=tf,sha256=d*64,bytes=12345,
            captured_at=datetime.fromtimestamp(NOW,timezone.utc).isoformat(),attachment_id='dom-'+d*64)
            for tf,d in(('15m','a'),('1h','b'))]
        self.names=[body['marker']+'-'+row['timeframe']+'-'+row['sha256'][:12]+'.png'for row in self.charts]
        def png_upload(*args):self.f.actions.append('native-png-upload');self.f.uploaded_names=self.names[:];return copy.deepcopy(self.charts)
        self.f._upload=png_upload
        self.native=SimpleNamespace(NotSubmitted=upload.NotSubmitted,MESSAGE='messages',USER_BODY='native-user-body',user_prompt=lambda p:p['prompt']+' '+p['marker'])
        self.delivery=NativeDataFileDelivery(self.f,self.native,lambda:True,wall_clock=lambda:NOW,archive_root=self.root).install()
        self.addCleanup(self.delivery.close)

    def test_actual_upload_preview_persists_two_real_csv_200_packets_without_send(self):
        before=copy.deepcopy(self.payload)
        self.f._upload(self.f.composer,self.payload,[])
        records=read_prepared(self.root,self.payload);self.assertEqual(len(records),1)
        value=records[0]['value'];validate_prepared(value,self.payload)
        self.assertEqual(value['state'],'PREPARED_NOT_SUBMITTED');self.assertFalse(value['send_attempted'])
        self.assertTrue(value['original_request_matches_material']);self.assertEqual(value['context_sha256'],digest(self.context))
        self.assertEqual(value['chart_evidence'],self.charts);self.assertEqual(len(value['attachment_names']),4)
        self.assertEqual([item['closed_count']for item in value['csv_receipts']],[200,200])
        self.assertEqual(self.f.sent,0);self.assertEqual(self.payload,before)
        self.assertEqual(self.f.actions,['native-png-upload','plus','File','choose-csv'])
        self.assertTrue((self.root/records[0]['name']).is_file())

    def test_prepared_receipt_does_not_make_posted_user_or_completion_evidence(self):
        self.f._upload(self.f.composer,self.payload,[])
        value=read_prepared(self.root,self.payload)[0]['value']
        self.assertEqual(self.f.rows,[]);self.assertFalse(value['send_attempted'])
        self.assertNotIn('response_id',value);self.assertNotIn('assistant_text',value)

    def test_a_false_csv_count_is_rejected_even_if_metadata_claims_200(self):
        self.f._upload(self.f.composer,self.payload,[])
        files=self.delivery.data_files(self.payload)
        files[0]['buffer']=b'open_time_utc_ms,open,high,low,close,volume,close_time_utc_ms\n'
        files[0]['sha256']=hashlib.sha256(files[0]['buffer']).hexdigest()
        with self.assertRaises(ValueError):retain_prepared(self.root,self.payload,self.charts,self.names,files,'prompt')
        self.assertEqual(len(read_prepared(self.root,self.payload)),1)


if __name__=='__main__':unittest.main()
