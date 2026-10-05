"""Exercise actual urllib Request creation without contacting the provider."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from worker.core import Ledger, Review
from worker.neuroapi import NeuroAPI, SCREEN_SCHEMA
from worker.prompts import SCREENING

class HeaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ledger=Ledger(Path(self.tmp.name)/'ledger.sqlite3');self.addCleanup(self.ledger.db.close)
        self.key='synthetic-header-test-placeholder'
        self.client=NeuroAPI(self.ledger,key=self.key)
    def invoke(self,kind):
        if kind=='health':return self.client.health()
        return self.client.ask('screen',SCREENING,SCREEN_SCHEMA,lambda value:None)
    def verify_request(self,kind):
        response=MagicMock();response.__enter__.return_value=response
        response.code=200;response.headers={}
        data={'status':'healthy','authenticated':True} if kind=='health' else {'mode':'smart','answer':None,'output':{'symbols':['BTCUSDT','ETHUSDT']}}
        response.read.return_value=json.dumps(data).encode()
        output=io.StringIO()
        with patch('worker.http_client.build_opener') as factory, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            factory.return_value.open.return_value=response
            result=self.invoke(kind)
            req=factory.return_value.open.call_args.args[0]
        headers={k.lower():v for k,v in req.header_items()}
        self.assertEqual(headers['accept'],'application/json')
        self.assertEqual(headers['user-agent'],'harun-office/1.0')
        self.assertEqual(headers['x-api-key'],self.key)
        self.assertEqual(req.method,'GET' if kind=='health' else 'POST')
        self.assertEqual(req.full_url,'https://api.neurobro.ai/api/v1/'+('health' if kind=='health' else 'agent/ask'))
        if kind=='health':
            self.assertIsNone(req.data);self.assertNotIn('content-type',headers)
        else:
            self.assertEqual(headers['content-type'],'application/json')
            self.assertEqual(json.loads(req.data)['prompt'],SCREENING)
            self.assertNotIn(self.key,req.data.decode())
        self.assertEqual(output.getvalue(),'');self.assertNotIn(self.key,json.dumps(result))
        rows=[dict(row) for row in self.ledger.db.execute('SELECT * FROM api_requests')]
        self.assertNotIn(self.key,json.dumps(rows))
    def test_health_wire_headers_and_secret_isolation(self):self.verify_request('health')
    def test_ask_wire_headers_and_secret_isolation(self):self.verify_request('ask')
    def test_raw_header_errors_never_expose_key(self):
        for kind in ('health','ask'):
            with self.subTest(kind=kind):
                output=io.StringIO()
                with patch('worker.http_client.build_opener',side_effect=RuntimeError(str(self.client._headers()))), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    with self.assertRaises(Review) as caught:self.invoke(kind)
                self.assertNotIn(self.key,str(caught.exception));self.assertEqual(output.getvalue(),'')
