"""Synthetic account/HTTP fixtures only; never contacts Binance or submits orders."""
import copy
import json
import os
import sqlite3
import tempfile
import traceback
import unittest
from datetime import datetime, timezone
from decimal import localcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from worker import binance_office as office
from worker.binance_private import BASE, BinanceReadOnly, BinanceCheckError, read_only_get, signature
from worker.account_state import account_state, newer_account, slots, account_order as observation_order

END=int(datetime(2026,10,6,12,tzinfo=timezone.utc).timestamp())*1000
TODAY=int(datetime(2026,10,5,17,tzinfo=timezone.utc).timestamp())*1000
KEY='synthetic-office-test-key-no-real-account'
SECRET='synthetic-office-test-secret-no-real-account'


def income(identity,kind,amount,*,symbol='BTCUSDT',asset='USDT',at=TODAY+1):
    return dict(tranId=identity,incomeType=kind,income=amount,symbol=symbol,asset=asset,time=at,
                info='PRIVATE_RAW_MUST_NOT_LEAK')


def fill(identity,*,symbol='BTCUSDT',at=TODAY+1,side='SELL',realized='1.2'):
    return dict(id=identity,orderId=2**53+identity,symbol=symbol,time=at,side=side,positionSide='BOTH',
                qty='0.00100',price='60000.10',realizedPnl=realized,commission='0.04',commissionAsset='USDT',
                private_note='PRIVATE_RAW_MUST_NOT_LEAK')


class FixtureClient:
    def __init__(self):
        self.calls=[]
        self.account=dict(assets=[dict(asset='USDT',walletBalance='116.92800000',availableBalance='110.5')],
                          positions=[dict(symbol='HYPEUSDT',positionSide='BOTH',positionAmt='10.000')])
        self.config=dict(canTrade=True,dualSidePosition=False,multiAssetsMargin=False)
        self.risks=[dict(symbol='HYPEUSDT',positionSide='BOTH',positionAmt='10.000',entryPrice='12.340',
                         markPrice='12.350',unRealizedProfit='0.1000')]
        self.incomes=[income(1,'REALIZED_PNL','1.2'),income(2,'COMMISSION','-0.04'),
                      income(3,'FUNDING_FEE','-0.01',symbol='HYPEUSDT'),
                      income(4,'COMMISSION','-0.001',asset='BNB'),income(5,'REALIZED_PNL','99',at=TODAY-1)]
        self.fills={'BTCUSDT':[fill(1)],'HYPEUSDT':[fill(1,symbol='HYPEUSDT',side='BUY',realized='0')]}
        self.history_hook=None
    def sync_time(self):self.calls.append(('TIME',{}))
    def server_time_ms(self):return END
    def signed_get(self,path,**options):
        self.calls.append((path,dict(options)))
        if path=='/fapi/v3/account':return copy.deepcopy(self.account)
        if path=='/fapi/v1/accountConfig':return copy.deepcopy(self.config)
        if path=='/fapi/v3/positionRisk':return copy.deepcopy(self.risks)
        if self.history_hook is not None:return self.history_hook(path,options)
        if path=='/fapi/v1/income':return copy.deepcopy(self.incomes)
        if path=='/fapi/v1/userTrades':return copy.deepcopy(self.fills.get(options['symbol'],[]))
        raise AssertionError('Unexpected read')


class OfficeTests(unittest.TestCase):
    def setUp(self):
        self.client=FixtureClient()
        self.network=patch('worker.binance_private.build_opener',side_effect=AssertionError('No real network'))
        self.network.start();self.addCleanup(self.network.stop)
    def test_real_exchange_fields_manual_position_preserved_and_inputs_unchanged(self):
        before=copy.deepcopy((self.client.account,self.client.risks,self.client.incomes,self.client.fills))
        result=office.collect(self.client)
        self.assertEqual(result['source'],'BINANCE_FUTURES')
        account=result['account'];self.assertEqual(account['status'],'CONNECTED')
        self.assertEqual(account['usdt_wallet_balance'],'116.92800000')
        self.assertEqual(account['active_positions'],1)
        position=account['positions'][0]
        self.assertEqual(position,dict(symbol='HYPEUSDT',side='LONG',position_side='BOTH',quantity='10.000',
                                      signed_quantity='10.000',entry_price='12.340',mark_price='12.350',unrealized_pnl='0.1000'))
        self.assertEqual(before,(self.client.account,self.client.risks,self.client.incomes,self.client.fills))
        serialized=json.dumps(result)
        for forbidden in ('PRIVATE_RAW_MUST_NOT_LEAK',KEY,SECRET,'DRY_RUN','SIMULATION','CLOSED','bot_owned'):
            self.assertNotIn(forbidden,serialized)
        self.assertTrue(all(path=='TIME' or path in ('/fapi/v3/account','/fapi/v1/accountConfig','/fapi/v3/positionRisk','/fapi/v1/income','/fapi/v1/userTrades') for path,_ in self.client.calls))
    def test_account_only_refresh_has_no_history_reads_or_keys(self):
        result=office.collect(self.client,include_history=False)
        self.assertEqual(set(result),{'source','generated_at','account'})
        self.assertEqual([path for path,_ in self.client.calls],['TIME','/fapi/v3/account','/fapi/v1/accountConfig','/fapi/v3/positionRisk'])
    def test_bangkok_boundary_exact_usdt_pnl_and_non_usdt_not_converted(self):
        result=office.collect(self.client)
        report=result['reports']
        self.assertEqual(report['period_start'],'2026-10-05T17:00:00.000Z')
        self.assertEqual(report['period_end'],'2026-10-06T12:00:00.000Z')
        self.assertEqual(report['pnl_today_usdt'],'1.15')
        self.assertEqual(report['realized_pnl_today_usdt'],'1.2')
        self.assertEqual(report['commission_today_usdt'],'-0.04')
        self.assertEqual(report['funding_today_usdt'],'-0.01')
        self.assertEqual(report['excluded_income_assets'],['BNB'])
        self.assertTrue(report['income_complete'])
        self.assertIsNone(report['trades_today']);self.assertFalse(report['complete'])
    def test_fills_have_true_order_identity_and_are_never_closed_position_inference(self):
        history=office.collect(self.client)['position_history']
        self.assertEqual(history['kind'],'BINANCE_FILLS');self.assertFalse(history['complete'])
        self.assertEqual(history['status'],'PARTIAL')
        self.assertEqual(history['symbol_scope'],'INCOME_AND_ACTIVE_POSITIONS')
        self.assertEqual(len(history['items']),2)
        row=next(row for row in history['items'] if row['symbol']=='BTCUSDT')
        self.assertEqual(row['id'],'1');self.assertEqual(row['order_id'],str(2**53+1))
        self.assertEqual(row['side'],'SELL');self.assertEqual(row['realized_pnl'],'1.2')
        self.assertEqual(row['quantity'],'0.00100');self.assertNotIn('status',row)
        self.assertIn('SYMBOL_DISCOVERY_NOT_EXHAUSTIVE',history['incomplete_reasons'])
    def test_short_and_hedge_positions_remain_separate_exposures(self):
        self.client.config['dualSidePosition']=True
        self.client.account['positions']=[dict(symbol='BTCUSDT',positionSide='SHORT',positionAmt='-0.20'),dict(symbol='BTCUSDT',positionSide='LONG',positionAmt='0.10')]
        self.client.risks=[dict(symbol=p['symbol'],positionSide=p['positionSide'],positionAmt=p['positionAmt'],entryPrice='60000',unRealizedProfit='-1') for p in self.client.account['positions']]
        result=office.collect(self.client,False)['account']
        self.assertEqual(result['position_mode'],'HEDGE');self.assertEqual(result['active_positions'],2)
        self.assertEqual(result['positions'][1]['side'],'SHORT')
        self.assertEqual(result['positions'][1]['quantity'],'0.20')
        self.assertEqual(result['positions'][1]['signed_quantity'],'-0.20')
    def test_delivery_symbol_and_actual_negative_commission_are_preserved(self):
        symbol='BTCUSDT_261225'
        self.client.account['positions'][0]['symbol']=symbol;self.client.risks[0]['symbol']=symbol
        self.client.incomes=[income(1,'COMMISSION','0.01',symbol=symbol)]
        row=fill(1,symbol=symbol);row['commission']='-0.01'
        self.client.fills={symbol:[row]}
        result=office.collect(self.client)
        self.assertEqual(result['account']['positions'][0]['symbol'],symbol)
        self.assertEqual(result['position_history']['items'][0]['commission'],'-0.01')
        self.assertEqual(result['reports']['commission_today_usdt'],'0.01')
    def test_exact_decimal_sums_do_not_depend_on_ambient_precision(self):
        self.client.incomes=[income(1,'REALIZED_PNL','123456789012345678901234567890.12345678901234567890'),income(2,'COMMISSION','-0.00000000000000000001')]
        with localcontext() as context:
            context.prec=6
            result=office.collect(self.client)['reports']
        self.assertEqual(result['pnl_today_usdt'],'123456789012345678901234567890.12345678901234567889')
    def test_maximum_decimal_span_keeps_tiny_actual_rebate_after_large_sum(self):
        self.client.incomes=[income(i,'REALIZED_PNL','9'*64) for i in range(101)]
        rebate='0.'+'0'*61+'1'
        self.client.incomes.append(income(200,'COMMISSION',rebate))
        report=office.collect(self.client)['reports']
        expected=str(int('9'*64)*101)+'.'+'0'*61+'1'
        self.assertTrue(report['income_complete']);self.assertEqual(report['pnl_today_usdt'],expected)
    def test_missing_or_inconsistent_account_does_not_become_empty_success(self):
        for mutate in (lambda c:c.account.update(positions=[]),lambda c:c.account.update(assets=[]),
                       lambda c:c.risks[0].update(positionAmt='NaN'),lambda c:c.risks.append(copy.deepcopy(c.risks[0])),
                       lambda c:c.config.update(canTrade='yes')):
            self.client=FixtureClient();mutate(self.client)
            with self.assertRaisesRegex(BinanceCheckError,'^BINANCE_ACCOUNT_UNAVAILABLE$'):office.collect(self.client,False)
    def test_zero_positions_do_not_hide_real_account_balances(self):
        self.client.account['positions']=[];self.client.risks=[]
        result=office.collect(self.client,False)['account']
        self.assertEqual(result['positions'],[]);self.assertEqual(result['active_positions'],0)
        self.assertEqual(result['usdt_wallet_balance'],'116.92800000')
    def test_historical_entry_receipt_cannot_claim_new_manual_position_or_free_capacity(self):
        database=sqlite3.connect(':memory:');self.addCleanup(database.close)
        database.execute('CREATE TABLE robot_entry_receipts(id TEXT,symbol TEXT,entry_day TEXT,confirmed_at TEXT)')
        database.execute("INSERT INTO robot_entry_receipts VALUES('old-bot-fill','BTCUSDT','2026-10-01','2026-10-01T12:00:00Z')")
        store=SimpleNamespace(db=database,entries=lambda today:0,
                              available_slots=lambda running,symbols,today:slots(running,0))
        self.client.account['positions'].append(dict(symbol='BTCUSDT',positionSide='BOTH',positionAmt='0.1'))
        self.client.check=lambda:dict(status='BINANCE_CONNECTED',position_mode='ONE_WAY',can_trade=True,
                                     multi_assets_margin=False,usdt_wallet_balance='116.92800000',usdt_available_balance='110.5')
        before=copy.deepcopy(self.client.account['positions'])
        result=account_state(self.client,store,'2026-10-06')
        self.assertEqual(result['manual_exposure'],['BTCUSDT','HYPEUSDT'])
        self.assertEqual(result['running_positions'],2);self.assertEqual(result['available_slots'],0)
        self.assertEqual(result['bot_entries_today'],0)
        self.assertEqual(self.client.account['positions'],before)
        self.assertEqual(database.execute('SELECT COUNT(*) FROM robot_entry_receipts').fetchone()[0],1)
    def test_history_failures_preserve_account_without_sensitive_error_text(self):
        def fail(path,options):raise RuntimeError(KEY+SECRET+' https://signed.example?signature=SECRET')
        self.client.history_hook=fail
        result=office.collect(self.client)
        self.assertEqual(result['account']['status'],'CONNECTED')
        self.assertEqual(result['reports']['status'],'UNAVAILABLE')
        self.assertIsNone(result['reports']['pnl_today_usdt'])
        self.assertEqual(result['position_history']['status'],'UNAVAILABLE')
        for value in (KEY,SECRET,'https://','signature='):self.assertNotIn(value,json.dumps(result))
    def test_income_pagination_cap_cannot_report_partial_sum_as_complete(self):
        def pages(path,options):
            if path.endswith('income'):return [income(options['page']*2,'REALIZED_PNL','1'),income(options['page']*2+1,'REALIZED_PNL','1')]
            return []
        self.client.history_hook=pages
        with patch.object(office,'PAGE_SIZE',2),patch.object(office,'MAX_INCOME_PAGES',2):result=office.collect(self.client)
        report=result['reports'];self.assertEqual(report['status'],'PARTIAL')
        self.assertFalse(report['income_complete']);self.assertIsNone(report['pnl_today_usdt'])
        self.assertIn('INCOME_PAGE_LIMIT',result['position_history']['incomplete_reasons'])
        self.assertEqual(len([p for p,_ in self.client.calls if p.endswith('income')]),2)
    def test_income_pagination_deduplicates_boundary_row_without_double_pnl(self):
        def pages(path,options):
            if path.endswith('income'):
                return [income(1,'REALIZED_PNL','1'),income(2,'COMMISSION','-0.1')] if options['page']==1 else [income(2,'COMMISSION','-0.1')]
            return []
        self.client.history_hook=pages
        with patch.object(office,'PAGE_SIZE',2):result=office.collect(self.client)
        # A page containing only repeated records cannot prove forward progress.
        self.assertFalse(result['reports']['income_complete']);self.assertIsNone(result['reports']['pnl_today_usdt'])
    def test_conflicting_duplicate_income_in_one_page_cannot_yield_false_complete_pnl(self):
        self.client.incomes=[income(1,'REALIZED_PNL','1'),income(1,'REALIZED_PNL','999')]
        result=office.collect(self.client)
        self.assertFalse(result['reports']['income_complete'])
        self.assertIsNone(result['reports']['pnl_today_usdt'])
    def test_latest_trade_window_pages_backwards_and_marks_same_ms_uncertainty(self):
        def pages(path,options):
            if path.endswith('income'):return []
            self.assertNotIn('from_id',options)
            if options['end_time']==END:return [fill(100,symbol='HYPEUSDT',at=TODAY+10),fill(101,symbol='HYPEUSDT',at=TODAY+10)]
            self.assertEqual(options['end_time'],TODAY+9)
            return [fill(99,symbol='HYPEUSDT',at=TODAY+9)]
        self.client.history_hook=pages
        with patch.object(office,'PAGE_SIZE',2),patch.object(office,'MAX_TRADE_PAGES',2):result=office.collect(self.client)
        self.assertEqual({row['id'] for row in result['position_history']['items']},{'99','100','101'})
        self.assertTrue(result['position_history']['truncated'])
        self.assertFalse(result['position_history']['complete'])
        self.assertIn('TRADE_WINDOW_FULL_SAME_MS_UNPROVEN',result['position_history']['incomplete_reasons'])
    def test_symbol_limit_is_explicit(self):
        self.client.incomes=[income(i,'COMMISSION','-0.1',symbol='X'+str(i)+'USDT') for i in range(4)]
        def pages(path,options):
            if path.endswith('income'):return self.client.incomes
            return [fill(1,symbol=options['symbol'])]
        self.client.history_hook=pages
        with patch.object(office,'MAX_HISTORY_SYMBOLS',2):result=office.collect(self.client)
        self.assertEqual(len(result['position_history']['symbols_checked']),2)
        self.assertIn('HISTORY_SYMBOL_LIMIT',result['position_history']['incomplete_reasons'])
        self.assertTrue(result['position_history']['truncated'])
    def test_trade_page_cap_is_explicit_without_repeating_the_entry_window(self):
        def pages(path,options):
            if path.endswith('income'):return []
            return [fill(options['end_time'],symbol='HYPEUSDT',at=options['end_time'])]
        self.client.history_hook=pages
        with patch.object(office,'PAGE_SIZE',1),patch.object(office,'MAX_TRADE_PAGES',2):result=office.collect(self.client)
        reads=[options for path,options in self.client.calls if path.endswith('userTrades')]
        self.assertEqual(len(reads),2);self.assertEqual(reads[1]['end_time'],END-1)
        self.assertEqual(reads[1]['start_time'],reads[0]['start_time'])
        self.assertNotIn('from_id',reads[1])
        self.assertIn('TRADE_PAGE_LIMIT',result['position_history']['incomplete_reasons'])
        self.assertTrue(result['position_history']['truncated'])
    def test_account_order_is_stamped_at_read_completion_before_history_latency(self):
        original=self.client.signed_get;events=[]
        def get(path,**options):
            events.append(path);return original(path,**options)
        self.client.signed_get=get
        def stamp():
            events.append('STAMP');return dict(_account_generation='fixture-generation',_account_revision=42)
        with patch.object(office,'account_order',side_effect=stamp):result=office.collect(self.client)
        self.assertEqual(events[events.index('STAMP')-1],'/fapi/v3/positionRisk')
        self.assertEqual(events[events.index('STAMP')+1],'/fapi/v1/income')
        self.assertEqual(result['account']['_account_revision'],42)
        self.assertEqual(result['account']['_account_generation'],'fixture-generation')
    def test_legacy_office_account_timestamps_preserve_newer_cache(self):
        cached=dict(status='CONNECTED',checked_at='2026-10-06T12:00:00Z')
        self.assertFalse(newer_account(dict(status='CONNECTED',checked_at='2026-10-06T11:00:00Z'),cached))
        self.assertTrue(newer_account(dict(status='CONNECTED',checked_at='2026-10-06T13:00:00Z'),cached))
        self.assertFalse(newer_account(dict(status='CONNECTED',checked_at=None),cached))
    def test_robot_bot_status_timestamp_cannot_stand_in_for_missing_account_read(self):
        cached=dict(account_checked_at='2026-10-06T12:00:00Z',checked_at='2026-10-06T13:00:00Z')
        late_status=dict(account_checked_at=None,checked_at='2026-10-06T14:00:00Z')
        self.assertFalse(newer_account(late_status,cached))
    def test_current_observation_order_wins_mixed_shapes_and_skewed_wall_timestamps(self):
        older=dict(checked_at='2099-01-01T00:00:00Z',**observation_order())
        newer=dict(account_checked_at='2000-01-01T00:00:00Z',**observation_order())
        self.assertTrue(newer_account(newer,older));self.assertFalse(newer_account(older,newer))
        self.assertFalse(newer_account(dict(checked_at='2099-01-01T00:00:00Z'),newer))
        previous_process=dict(checked_at='2099-01-01T00:00:00Z',_account_generation='previous-process',_account_revision=999999)
        self.assertTrue(newer_account(newer,previous_process))
        self.assertFalse(newer_account(previous_process,newer))
    def test_budget_stops_additional_history_reads_and_exposes_unknown_totals(self):
        with patch.object(office.time,'monotonic',side_effect=[0,61,61]):result=office.collect(self.client)
        self.assertFalse(result['reports']['income_complete'])
        self.assertIsNone(result['reports']['pnl_today_usdt'])
        self.assertIn('HISTORY_TIME_BUDGET',result['position_history']['incomplete_reasons'])
        self.assertFalse(any(p in ('/fapi/v1/income','/fapi/v1/userTrades') for p,_ in self.client.calls))
        self.assertEqual(result['position_history']['symbols_checked'],[])
        self.assertEqual(result['position_history']['symbols_selected'],['HYPEUSDT'])
    def test_malformed_or_out_of_window_income_never_exposes_false_sum(self):
        for row in (income(1,'REALIZED_PNL','NaN'),income(1,'REALIZED_PNL','1',at=END+1),dict(secret=KEY)):
            self.client=FixtureClient();self.client.incomes=[row]
            result=office.collect(self.client)
            self.assertFalse(result['reports']['income_complete']);self.assertIsNone(result['reports']['pnl_today_usdt'])
    def test_stale_signing_clock_resyncs_once_without_changing_history_window(self):
        original=self.client.signed_get;seen=[False]
        def get(path,**options):
            if path.endswith('income') and not seen[0]:
                seen[0]=True;raise BinanceCheckError('BINANCE_CLOCK_ERROR')
            return original(path,**options)
        self.client.signed_get=get
        result=office.collect(self.client)
        self.assertEqual(sum(path=='TIME' for path,_ in self.client.calls),2)
        self.assertEqual(result['reports']['period_end'],'2026-10-06T12:00:00.000Z')


class PrivateHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);key=root/'key';secret=root/'secret'
        for path,value in ((key,KEY),(secret,SECRET)):path.write_text(value+'\n');path.chmod(0o600)
        env=patch.dict(os.environ,{'BINANCE_API_KEY_FILE':str(key),'BINANCE_API_SECRET_FILE':str(secret)})
        env.start();self.addCleanup(env.stop);self.calls=[]
        def transport(url,headers):
            self.calls.append((url,headers))
            return {'serverTime':END} if url==BASE+'/fapi/v1/time' else []
        self.client=BinanceReadOnly(transport=transport,clock=lambda:10)
        self.client.sync_time()
    def test_signed_history_canonical_hmac_and_global_position_read(self):
        self.client.signed_get('/fapi/v1/income',start_time=END-100,end_time=END,limit=1000,page=2)
        self.client.signed_get('/fapi/v1/userTrades',symbol='BTCUSDT',from_id=2**53+1,limit=1000)
        self.client.signed_get('/fapi/v3/positionRisk')
        for url,headers in self.calls[1:]:
            parts=urlsplit(url);unsigned,sig=parts.query.rsplit('&signature=',1)
            self.assertEqual(sig,signature(SECRET,unsigned));self.assertEqual(headers['X-MBX-APIKEY'],KEY)
            self.assertTrue(unsigned.startswith('recvWindow=5000&timestamp='+str(END)))
        self.assertIn('fromId='+str(2**53+1),self.calls[2][0])
    def test_endpoint_specific_invalid_options_never_request(self):
        invalid=[('/fapi/v1/income',dict(start_time=END-10,end_time=END,limit=True)),
                 ('/fapi/v1/income',dict(start_time=END-10,end_time=END,limit=1001)),
                 ('/fapi/v1/income',dict(start_time=END-10,end_time=END,page=0)),
                 ('/fapi/v1/income',dict(start_time=END-8*office.DAY_MS,end_time=END)),
                 ('/fapi/v1/income',dict(start_time=END,end_time=END+1)),
                 ('/fapi/v1/income',dict(start_time=END)),('/fapi/v1/income',dict(from_id=1)),
                 ('/fapi/v1/income',dict(symbol='BTCUSDT',start_time=END-10,end_time=END)),
                 ('/fapi/v1/userTrades',dict(symbol='BTCUSDT',from_id=1,start_time=END-10,end_time=END)),
                 ('/fapi/v1/userTrades',dict(symbol='BTCUSDT',from_id=1,page=1)),
                 ('/fapi/v1/userTrades',dict(symbol='../../order',from_id=1)),
                 ('/fapi/v3/account',dict(start_time=END-10,end_time=END)),('/fapi/v1/order',{})]
        for path,options in invalid:
            with self.assertRaisesRegex(BinanceCheckError,'^BINANCE_ENDPOINT_DENIED$'):self.client.signed_get(path,**options)
        self.assertEqual(len(self.calls),1)
    def test_transport_checks_duplicate_unknown_encoded_and_mutating_queries(self):
        base='recvWindow=5000&timestamp='+str(END)
        queries=[base+'&startTime=1&endTime='+str(END),
                 base+'&startTime='+str(END-1)+'&endTime='+str(END)+'&side=BUY',
                 base+'&startTime='+str(END-1)+'&endTime='+str(END)+'&page=1&page=2',
                 base+'&startTime='+str(END-1)+'&endTime='+str(END)+'&limit=%31',
                 base+'&symbol=BTCUSDT&fromId=1&endTime='+str(END)]
        headers={'X-MBX-APIKEY':KEY,'Accept':'application/json','User-Agent':'test'}
        with patch('worker.binance_private.build_opener',side_effect=AssertionError('No network')):
            for query in queries:
                with self.assertRaisesRegex(BinanceCheckError,'BINANCE_ENDPOINT_DENIED'):
                    read_only_get(BASE+'/fapi/v1/income?'+query+'&signature='+'0'*64,headers)
    def test_actual_transport_history_is_get_without_body(self):
        query='recvWindow=5000&timestamp='+str(END)+'&symbol=BTCUSDT&startTime='+str(END-10)+'&endTime='+str(END)+'&limit=1000'
        response=Mock();response.code=200;response.read.return_value=b'[]'
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        with patch('worker.binance_private.build_opener') as opener:
            opener.return_value.open.return_value=response
            self.assertEqual(read_only_get(BASE+'/fapi/v1/userTrades?'+query+'&signature='+'0'*64,
                                          {'X-MBX-APIKEY':KEY,'Accept':'application/json','User-Agent':'test'}),[])
            request=opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(),'GET');self.assertIsNone(request.data)
    def test_provider_and_transport_failures_never_print_url_or_secrets(self):
        self.client._transport=Mock(side_effect=RuntimeError(KEY+SECRET+'?signature=PRIVATE'))
        try:self.client.signed_get('/fapi/v1/userTrades',symbol='BTCUSDT',from_id=1)
        except BinanceCheckError as error:
            self.assertEqual(str(error),'BINANCE_ACCOUNT_UNAVAILABLE');trace=traceback.format_exc()
            for value in (KEY,SECRET,'signature=PRIVATE'):self.assertNotIn(value,trace)
        else:self.fail('Expected sanitized failure')


if __name__=='__main__':unittest.main()
