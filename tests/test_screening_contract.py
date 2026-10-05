"""Screening uses an independent active exchange catalog, never provider-derived keys."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from worker.core import Ledger, Review
from worker.neuroapi import NeuroAPI, SCREEN_SCHEMA, canonical_output, selections
from worker.prompts import SCREENING
from worker.__main__ import main
from worker.diagnostics import read_records

CATALOG={'BTCUSDT':{},'ETHUSDT':{}}
OUTPUT={'symbols':['BTCUSDT','ETHUSDT']}
class ScreeningContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.db=Ledger(self.root/'test.sqlite3');self.addCleanup(self.db.db.close)
    def test_schema_exact_two_unique_and_symbol_contract(self):
        symbols=SCREEN_SCHEMA['properties']['symbols']
        self.assertEqual((symbols['minItems'],symbols['maxItems']),(2,2))
        self.assertTrue(symbols['uniqueItems']);self.assertFalse(SCREEN_SCHEMA['additionalProperties'])
        self.assertEqual(SCREEN_SCHEMA['required'],['symbols'])
        self.assertEqual(symbols['items']['pattern'],r'^[A-Z0-9]{2,18}USDT$')
        self.assertIn('not a coin recommendation',symbols['items']['description'])
    def test_valid_exact_symbols_are_not_modified(self):
        self.assertEqual(selections(OUTPUT,CATALOG),['BTCUSDT','ETHUSDT'])
        self.assertEqual(json.loads(canonical_output(OUTPUT,SCREEN_SCHEMA,CATALOG)),OUTPUT)
    def test_missing_suffix_lowercase_and_unknown_contract_rejected(self):
        for pair in (['BTC','ETH'],['btcusdt','ETHUSDT'],['BTCUSDT','FAKEUSDT'],['BTCUSDT ','ETHUSDT']):
            with self.subTest(pair=pair),self.assertRaisesRegex(Review,'INVALID_SCREENING_SYMBOL'):
                selections({'symbols':pair},CATALOG)
    def test_count_unique_and_extra_fields_rejected(self):
        for output in ({'symbols':['BTCUSDT']},{'symbols':['BTCUSDT','BTCUSDT']},{'symbols':['BTCUSDT','ETHUSDT','SOLUSDT']},{**OUTPUT,'extra':1}):
            with self.subTest(output=output),self.assertRaises(Review):selections(output,CATALOG)
    def test_missing_catalog_blocks_paid_request_and_canonicalization(self):
        transport=Mock();client=NeuroAPI(self.db,key='synthetic-test-placeholder',transport=transport)
        with self.assertRaisesRegex(Review,'SCREENING_CATALOG_REQUIRED'):
            client.ask('test',SCREENING,SCREEN_SCHEMA,lambda v:None)
        with self.assertRaisesRegex(Review,'SCREENING_CATALOG_REQUIRED'):canonical_output(OUTPUT,SCREEN_SCHEMA)
        transport.assert_not_called();self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM api_requests').fetchone()[0],0)
    def test_noop_validator_cannot_accept_unknown_coin(self):
        transport=Mock(return_value=(200,{},dict(mode='smart',answer=None,output={'symbols':['BTCUSDT','FAKEUSDT']})))
        client=NeuroAPI(self.db,key='synthetic-test-placeholder',transport=transport)
        with self.assertRaisesRegex(Review,'INVALID_SCREENING_SYMBOL'):
            client.ask('test',SCREENING,SCREEN_SCHEMA,lambda v:None,catalog=CATALOG)
        row=self.db.db.execute('SELECT state,output,failure_code FROM api_requests').fetchone()
        self.assertEqual(tuple(row),('NEEDS_REVIEW',None,'INVALID_SCREENING_SYMBOL'))
    def test_cached_selection_is_checked_against_current_catalog_without_replay(self):
        transport=Mock(return_value=(200,{},dict(mode='smart',answer=None,output=OUTPUT)))
        client=NeuroAPI(self.db,key='synthetic-test-placeholder',transport=transport)
        client.ask('test',SCREENING,SCREEN_SCHEMA,lambda v:None,catalog=CATALOG)
        with self.assertRaisesRegex(Review,'INVALID_SCREENING_SYMBOL'):
            client.ask('test',SCREENING,SCREEN_SCHEMA,lambda v:None,catalog={'BTCUSDT':{}})
        self.assertEqual(transport.call_count,1)
    def test_manual_command_fetches_catalog_first_prints_only_valid_symbols_no_cycle(self):
        events=[];market=Mock()
        market.catalog.side_effect=lambda:events.append('catalog') or CATALOG
        def transport(method,url,headers,body,timeout):
            events.append('provider');self.assertEqual(body['prompt'].encode(),b'pilihkan 2 coin yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance')
            return 200,{},dict(mode='smart',answer=None,output=OUTPUT)
        def make_client(ledger):return NeuroAPI(ledger,key='synthetic-test-placeholder',transport=transport)
        with patch('sys.argv',['worker','screening-once','--data-dir',str(self.root)]),patch('worker.__main__.Market',return_value=market),patch('worker.__main__.NeuroAPI',side_effect=make_client),patch('worker.__main__.day',return_value='2026-10-05'),patch('worker.__main__.Workflow',side_effect=AssertionError('Must not trade')):
            for _ in range(2):
                out=io.StringIO()
                with contextlib.redirect_stdout(out):self.assertEqual(main(),0)
                self.assertEqual(json.loads(out.getvalue()),OUTPUT['symbols'])
        self.assertEqual(events,['catalog','provider','catalog'])
        row=read_records(self.root)[0]
        self.assertEqual(row['operation'],'2026-10-05:manual-screening:v2');self.assertEqual(row['attempts'],1)
    def test_catalog_failure_prevents_provider_call(self):
        client=Mock();market=Mock();market.catalog.side_effect=Review('MARKET_UNAVAILABLE')
        with patch('sys.argv',['worker','screening-once','--data-dir',str(self.root)]),patch('worker.__main__.Market',return_value=market),patch('worker.__main__.NeuroAPI',return_value=client),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(),1)
        client.ask.assert_not_called()
