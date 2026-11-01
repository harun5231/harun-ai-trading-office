"""Controlled website-ABI fixtures; live UI evidence is recorded separately."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import binascii
import struct
import zlib
from tests.test_native_chart_capture import png_pixels
from agent.binance_website_chart import (SOURCE, CONTRACT, VIEWPORT, PERIODS,
    WebsiteChartUnavailable, observation, coverage, hover_open_time, url_for)
from agent.native_chart_capture import PHASES, WEBSITE_SIDECAR, install_website_capture
from agent.native_chart_followup import (verify_website_capture, material_dispatch_provenance,
    FollowupUnavailable)
from agent.native_market_context import convert_native_market_context, NativeContextUnavailable
from tests.test_agent_closed_candles_200 import NOW, native_context_200


def observed(snapshot, tf, *, left=False, opened_override=None):
    period = PERIODS[tf]
    opened = (snapshot['candles'][tf][0]['open_time']-3*period if left
              else snapshot['running_candles'][tf]['open_time'])
    if opened_override is not None:opened=opened_override
    from datetime import datetime, timezone
    label = datetime.fromtimestamp(opened/1000, timezone.utc).strftime('%Y/%m/%d %H:%M')
    return {'url':url_for(snapshot['symbol']),
        'title':f"101 | {snapshot['symbol']} USDⓈ-Margined Perpetual Chart | Binance Futures",
        'timezone':'UTC','chart_count':1,'canvas_count':1,
        'timeframes':{frame:{'count':1,'active':frame==tf}for frame in ('15m','1h')},
        'original_chart_visible':True,'visible_price':'101','price_visible':True,
        'header_rows':[label+'\nOpen\n100\nClose\n101'],
        'capture_clip':dict(x=0,y=0,width=1920,height=1080),
        'canvas':{'x':0,'y':200,'width':1250,'height':500,'sampled_color_count':20}}


class WebsiteChartContractTests(unittest.TestCase):
    def setUp(self):
        self.native = native_context_200()
        self.snapshot = convert_native_market_context(self.native, now=NOW)

    def test_visible_hover_range_covers_200_closed_and_excludes_running_bar(self):
        for tf in ('15m','1h'):
            row=coverage(self.snapshot,tf,observed(self.snapshot,tf,left=True),observed(self.snapshot,tf))
            self.assertEqual(row['required_closed_count'],200)
            self.assertGreater(row['covered_closed_count'],200)
            self.assertTrue(row['running_candle_visible'])
            self.assertFalse(row['running_candle_in_closed_count'])
            self.assertEqual(row['required_first_open_time_ms'],self.snapshot['candles'][tf][0]['open_time'])

    def test_spot_foreign_symbol_wrong_frame_blank_chart_and_non_utc_refused(self):
        mutations=[lambda v:v.update(url='https://www.binance.com/en/trade/BTC_USDT'),
            lambda v:v.update(title='101 | ETHUSDT USDⓈ-Margined Perpetual Chart | Binance Futures'),
            lambda v:v['timeframes']['15m'].update(active=False),
            lambda v:v['canvas'].update(sampled_color_count=1),
            lambda v:v.update(timezone='Asia/Jakarta')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value=observed(self.snapshot,'15m');mutate(value)
                with self.assertRaises(WebsiteChartUnavailable):observation(value,'BTCUSDT','15m')

    def test_partial_visible_history_is_not_called_200_closed(self):
        left=observed(self.snapshot,'15m',left=True)
        left['header_rows']=observed(self.snapshot,'15m')['header_rows']
        with self.assertRaisesRegex(WebsiteChartUnavailable,'200_CLOSED_RANGE'):
            coverage(self.snapshot,'15m',left,observed(self.snapshot,'15m'))

    def test_same_scope_dispatch_freshness_checked_at_send_not_file_creation(self):
        rows=[dict(source=SOURCE,symbol='BTCUSDT',timeframe=tf,captured_at=self.snapshot['observed_at_utc'],
            sha256='a'*64,bytes=2000)for tf in ('15m','1h')]
        facts=material_dispatch_provenance(self.native,rows,now=NOW+2)
        self.assertEqual(facts['closed_candles_total'],400)
        self.assertEqual(facts['send_checked_at'],NOW+2)
        with self.assertRaises(NativeContextUnavailable):material_dispatch_provenance(self.native,rows,now=NOW+181)
        rows[0]['symbol']='ETHUSDT'
        with self.assertRaises(FollowupUnavailable):material_dispatch_provenance(self.native,rows,now=NOW+2)

    def test_dispatch_receipts_are_timeframe_order_agnostic_and_detect_changed_hash(self):
        from agent.native_chart_followup import validate_material_send_check
        images=[dict(source=SOURCE,symbol='BTCUSDT',timeframe=tf,captured_at=self.snapshot['observed_at_utc'],
            sha256=('a'if tf=='15m'else'b')*64,bytes=2000)for tf in ('1h','15m')]
        facts=material_dispatch_provenance(self.native,images,now=NOW+1)
        receipt={'observed_at':self.snapshot['observed_at'],'followup_chart_evidence':images,'material_send_check':facts}
        self.assertIs(validate_material_send_check(receipt,now=NOW+1,check_freshness=True),facts)
        receipt['followup_chart_evidence'][0]['sha256']='c'*64
        with self.assertRaises(FollowupUnavailable):validate_material_send_check(receipt,now=NOW+1,check_freshness=True)

    def test_intent_freshness_does_not_expire_completed_receipt_after_slow_reply(self):
        from agent.native_chart_followup import ReceiptStore, sha
        images=[dict(source=SOURCE,symbol='BTCUSDT',timeframe=tf,captured_at=self.snapshot['observed_at_utc'],
            sha256='a'*64,bytes=2000)for tf in ('15m','1h')]
        intent={'operation':'analysis:fixture','request_sha256':'b'*64,'observed_at':self.snapshot['observed_at'],
            'followup_chart_evidence':images,
            'material_send_check':material_dispatch_provenance(self.native,images,now=NOW+1)}
        with tempfile.TemporaryDirectory()as directory:
            clock=[NOW+1];store=ReceiptStore(directory,monotonic=lambda:10,wall_clock=lambda:clock[0])
            self.assertTrue(store(stage='INTENT',receipt=intent,deadline=100))
            clock[0]=NOW+600
            complete={**intent,'intent_sha256':sha(intent),'response_id':'dom-fixture','final_reply_sha256':'c'*64}
            self.assertTrue(store(stage='COMPLETE',receipt=complete,deadline=100))
            self.assertEqual(len(list((Path(directory)/'dialogue-200-followup').glob('*.json'))),2)
            stale={**intent,'operation':'analysis:next'}
            with self.assertRaises(FollowupUnavailable):store(stage='INTENT',receipt=stale,deadline=100)


class WebsiteCaptureFixtureTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.root=Path(self.directory.name)/'private'
        self.root.mkdir(mode=0o700);self.native_context=native_context_200()
        self.snapshot=convert_native_market_context(self.native_context,now=NOW)
        self.context=SimpleNamespace(pages=[]);self.calls=[];self.frame='15m';self.hover_x=1200
        def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',binascii.crc32(kind+data)&0xffffffff)
        scan=(b'\0'+bytes((12,20,30))*1920)*1080
        self.png=(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1920,1080,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(scan))+chunk(b'IEND',b''))
        decode=png_pixels
        def original(browser_context,context_dict,marker,request_sha256,archive_root='/tmp/old',*,deadline=None,progress=None,checkpoint=None):
            raise AssertionError('old capture must not run')
        def archive(root,marker,request,symbol,pictures):
            attempt=Path(root)/marker;attempt.mkdir(mode=0o700);paths=[];evidence=[]
            for tf,raw,stamp in pictures:
                path=attempt/(tf+'.png');path.write_bytes(raw);path.chmod(0o600);paths.append(str(path))
                evidence.append(dict(source=SOURCE,symbol=symbol,timeframe=tf,captured_at=stamp,
                    sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw)))
            return {'paths':paths,'evidence':evidence}
        class Error(ValueError):
            def __init__(self,message,phase=None):super().__init__(message);self.phase=phase
        self.namespace={'SOURCE':SOURCE,'capture':original,'archive_images':archive,'validate_context':lambda c:c['symbol'],
            'png_pixels':decode,'close_resource':lambda p:self.context.pages.remove(p),'ChartUnavailable':Error,
            'MAX_PNG':2097152,'CAPTURE_PHASES':set(PHASES),'PROGRESS_PHASES':set(PHASES)}
        def rpc(resource,method,deadline,*args):
            self.calls.append(method)
            if method=='new_page':
                page=SimpleNamespace(context=self.context);self.context.pages.append(page);return page
            if method=='goto':return {'status':200,'url':url_for(args[0])}
            if method=='observe':
                opened=None
                if self.hover_x<25:
                    opened=self.snapshot['candles'][self.frame][0]['open_time']+((int(self.hover_x)-5)//5-3)*PERIODS[self.frame]
                return observed(self.snapshot,self.frame,opened_override=opened)
            if method=='select_timeframe':self.frame=args[0];return True
            if method=='hover':self.hover_x=args[0];return None
            if method=='screenshot':return self.png
            return None
        self.capture=install_website_capture(self.namespace,expected_source=SOURCE,expected_capture=original,
            images_key='paths',phase_map={p:p for p in PHASES},rpc=rpc,monotonic=lambda:10,wall_clock=lambda:NOW+1)

    def tearDown(self):self.directory.cleanup()

    def test_real_page_path_does_not_call_data_renderer_and_receipts_bind_context(self):
        with patch('agent.native_chart_capture.render_chart_png',side_effect=AssertionError('rendered image forbidden')):
            output=self.capture(self.context,self.native_context,'HARUN_TEST','a'*64,archive_root=self.root)
        self.assertIn('goto',self.calls);self.assertNotIn('set_content',self.calls)
        self.assertFalse(self.context.pages)
        manifest=json.loads((Path(output['paths'][0]).parent/WEBSITE_SIDECAR).read_text())
        self.assertEqual(manifest['source'],SOURCE);self.assertTrue(manifest['network_navigation_performed'])
        self.assertTrue(all(r['exchange_website_screenshot'] for r in manifest['charts'].values()))
        self.assertTrue(all(not r['browser_screenshot_of_data_render'] for r in manifest['charts'].values()))
        verified=verify_website_capture(SimpleNamespace(**self.namespace),output,self.native_context,
            {'marker':'HARUN_TEST','request_sha256':'a'*64},archive_root=self.root,now=NOW+2,gate=lambda:True)
        self.assertEqual(verified,output)
        manifest['charts']['15m']['exchange_website_screenshot']=False
        (Path(output['paths'][0]).parent/WEBSITE_SIDECAR).write_text(json.dumps(manifest))
        with self.assertRaises(FollowupUnavailable):
            verify_website_capture(SimpleNamespace(**self.namespace),output,self.native_context,
                {'marker':'HARUN_TEST','request_sha256':'a'*64},archive_root=self.root,now=NOW+2,gate=lambda:True)

    def test_existing_context_page_is_never_closed_or_reused(self):
        owner=SimpleNamespace();self.context.pages.append(owner)
        with self.assertRaises(ValueError):
            self.capture(self.context,self.native_context,'HARUN_TEST','a'*64,archive_root=self.root)
        self.assertEqual(self.context.pages,[owner]);self.assertNotIn('new_page',self.calls)


if __name__=='__main__':unittest.main()
