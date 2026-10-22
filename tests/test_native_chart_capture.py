"""Offline capture contract tests; FakePlaywright never visits an external page."""
import base64
import binascii
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from agent.native_chart_capture import (IMAGE_READY, PHASES, SIDECAR, SOURCE,
    NativeCaptureConfigurationError, install)

if __package__:
    from .test_agent_closed_candles_200 import NOW, native_context_200
else:
    from test_agent_closed_candles_200 import NOW, native_context_200


class CaptureUnavailable(Exception):
    def __init__(self, message='unavailable', *, phase=None):
        super().__init__(message)
        self.phase = phase


def png_pixels(raw):
    if raw[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('PNG')
    cursor, compressed = 8, bytearray()
    width = height = channels = None
    while cursor < len(raw):
        size = struct.unpack('>I', raw[cursor:cursor+4])[0]
        kind, data = raw[cursor+4:cursor+8], raw[cursor+8:cursor+8+size]
        crc = struct.unpack('>I', raw[cursor+8+size:cursor+12+size])[0]
        if crc != binascii.crc32(kind+data) & 0xffffffff:
            raise ValueError('CRC')
        if kind == b'IHDR':
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB',data)
            if depth != 8 or color not in (2,6) or compression or filtering or interlace:
                raise ValueError('dimensions')
            channels = 3 if color == 2 else 4
        if kind == b'IDAT':
            compressed.extend(data)
        cursor += 12+size
        if kind == b'IEND':
            break
    if cursor != len(raw):
        raise ValueError('length')
    raw_rows = zlib.decompress(compressed)
    stride = width*channels
    if len(raw_rows) != (stride+1)*height:
        raise ValueError('rows')
    previous=bytearray(stride)
    pixels=bytearray(height*stride)
    for y in range(height):
        start=y*(stride+1)
        kind=raw_rows[start]
        if kind not in range(5):raise ValueError('filter')
        row=bytearray(raw_rows[start+1:start+stride+1])
        if kind==0:
            pixels[y*stride:(y+1)*stride]=row
            previous=row
            continue
        for x in range(stride):
            left=row[x-channels] if x>=channels else 0
            upper=previous[x]
            diagonal=previous[x-channels] if x>=channels else 0
            if kind==0:prediction=0
            elif kind==1:prediction=left
            elif kind==2:prediction=upper
            elif kind==3:prediction=(left+upper)//2
            else:
                value=left+upper-diagonal
                distances=abs(value-left),abs(value-upper),abs(value-diagonal)
                prediction=(left,upper,diagonal)[distances.index(min(distances))]
            row[x]=(row[x]+prediction)&255
        pixels[y*stride:(y+1)*stride]=row
        previous=row
    return width,height,channels,pixels


def png_from_rgb(raw, *, rgba=False, wrong_pixel=False):
    width, height, _, pixels = png_pixels(raw)
    if wrong_pixel:
        pixels[200] ^= 1
    channels = 4 if rgba else 3
    if rgba:
        value = bytearray(width*height*4)
        value[0::4],value[1::4],value[2::4] = pixels[0::3],pixels[1::3],pixels[2::3]
        value[3::4] = b'\xff'*(width*height)
        pixels=value
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',binascii.crc32(kind+data)&0xffffffff)
    rows = b''.join(b'\0'+bytes(pixels[i*width*channels:(i+1)*width*channels]) for i in range(height))
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,6 if rgba else 2,0,0,0))+
        chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b''))


class Page:
    def __init__(self,context):
        self.context=context
        self.closed=False
        self.documents=[]
        self.rendered=[]
        self.screenshots=[]
        self.viewport=None
        self.fail_method=None
        self.wrong_pixel=False
        self.rgba=False
    def set_viewport_size(self,value):
        self.viewport=value
    def set_content(self,html,*,wait_until,timeout):
        self.documents.append(html)
        encoded=re.search(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"',html).group(1)
        self.rendered.append(base64.b64decode(encoded))
    def wait_for_function(self,expression,*,timeout):
        if expression != IMAGE_READY:
            raise AssertionError('predicate')
        return True
    def screenshot(self,**kwargs):
        self.screenshots.append(kwargs)
        return png_from_rgb(self.rendered[-1],rgba=self.rgba,wrong_pixel=self.wrong_pixel)
    def goto(self,*args,**kwargs):
        raise AssertionError('network navigation forbidden')
    def close(self):
        self.closed=True
        self.context.pages.remove(self)


class Context:
    def __init__(self):
        self.pages=[]
        self.created=[]
        self.closed=False
        self.page_options={}
    def new_page(self):
        page=Page(self)
        page.__dict__.update(self.page_options)
        self.pages.append(page)
        self.created.append(page)
        return page
    def close(self):
        self.closed=True
        raise AssertionError('must not close caller context')


class Harness:
    def __init__(self):
        self.current=0.0
        self.calls=[]
        self.capture_calls=0
        self.archive_calls=0
        self.closed=[]
        self.bad_method=None
        self.deadline_method=None
        self.deadline_checks=[]
        self.helper_mutation=None
        self.native=self.namespace()
    def namespace(self):
        def capture(browser_context,context_dict,marker,request_sha256,archive_root='/private/native',*,deadline=None,progress=None,checkpoint=None):
            self.capture_calls+=1
            raise AssertionError('never legacy capture')
        def archive_images(root,marker,request_sha256,symbol,pictures):
            self.archive_calls+=1
            root=Path(root)
            root.mkdir(mode=0o700,exist_ok=True)
            attempt=Path(tempfile.mkdtemp(prefix='capture-',dir=root))
            attempt.chmod(0o700)
            paths,evidence=[],[]
            for timeframe,raw,captured_at in pictures:
                sha=hashlib.sha256(raw).hexdigest()
                path=attempt/(timeframe+'-'+sha+'.png')
                with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:
                    stream.write(raw)
                paths.append(str(path))
                evidence.append(dict(source=self.native['SOURCE'],symbol=symbol,timeframe=timeframe,
                    captured_at=captured_at,sha256=sha,bytes=len(raw)))
            value={'reviewed_images_key':paths,'evidence':evidence}
            (attempt/'native-manifest.json').write_text(json.dumps({'marker':marker,'request_sha256':request_sha256,'evidence':evidence}))
            if self.helper_mutation:
                self.helper_mutation(attempt,value)
            return value
        def close_resource(page):
            self.closed.append(page)
            page.close()
        return {'SOURCE':SOURCE,'capture':capture,'archive_images':archive_images,
            'validate_context':lambda ctx:ctx['symbol'],'png_pixels':png_pixels,
            'close_resource':close_resource,'ChartUnavailable':CaptureUnavailable,
            'MAX_PNG':2*1024*1024,'CAPTURE_PHASES':set(PHASES),'PROGRESS_PHASES':set(PHASES)}
    def rpc(self,resource,method,deadline,*args,**kwargs):
        if self.current>=deadline:
            raise TimeoutError()
        self.calls.append((resource,method,deadline,args,kwargs))
        self.deadline_checks.append(deadline)
        if method==self.deadline_method:
            self.current=deadline
            raise TimeoutError()
        if method==self.bad_method:
            raise OSError('sensitive path/credential details never echoed')
        self.current+=.01
        return getattr(resource,method)(*args,**kwargs)
    def install(self,**altered):
        args=dict(expected_source=SOURCE,expected_capture=self.native['capture'],
            images_key='reviewed_images_key',phase_map={p:p for p in PHASES},rpc=self.rpc,
            monotonic=lambda:self.current,wall_clock=lambda:NOW+self.current)
        args.update(altered)
        return install(self.native,**args)


class NativeChartCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'archive'
        self.harness=Harness()
        self.capture=self.harness.install()
        self.context=Context()
        self.market=native_context_200()
        self.progress=[]
        self.checkpoints=[]
    def run_capture(self,**kwargs):
        args=dict(deadline=80,progress=lambda *p:self.progress.append(p),checkpoint=self.checkpoints.append)
        args.update(kwargs)
        return self.capture(self.context,self.market,'request_marker','a'*64,str(self.root),**args)
    def test_actual_png_400_data_and_two_page_screenshots_keep_native_six_field_receipts(self):
        result=self.run_capture()
        self.assertEqual(set(result),{'reviewed_images_key','evidence'})
        self.assertEqual([row['timeframe'] for row in result['evidence']],['15m','1h'])
        for row in result['evidence']:
            self.assertEqual(set(row),{'source','symbol','timeframe','captured_at','sha256','bytes'})
            self.assertEqual(row['source'],SOURCE)
        page=self.context.created[0]
        self.assertTrue(page.closed)
        self.assertEqual(page.viewport,{'width':1024,'height':640})
        self.assertEqual(len(page.screenshots),2)
        self.assertEqual(self.harness.capture_calls,0)
        self.assertFalse(self.context.closed)
        self.assertEqual(self.context.pages,[])
        self.assertEqual(self.checkpoints[-1]['outcome'],'CAPTURED')
        for html in page.documents:
            self.assertIn("default-src 'none'; img-src data:",html)
            self.assertIn("connect-src 'none'",html)
            self.assertNotIn('https://',html)
            self.assertNotIn('<script',html)
            self.assertNotIn('<iframe',html)
        sidecar=json.loads((Path(result['reviewed_images_key'][0]).parent/SIDECAR).read_text())
        data=sidecar['public_market_context']
        self.assertEqual({tf:len(rows) for tf,rows in data['candles'].items()},{'1h':200,'15m':200})
        self.assertEqual(data['candles']['1h'][-1]['close_time'],self.market['server_time']//3600000*3600000-1)
        self.assertEqual(data['candles']['15m'][-1]['close_time'],self.market['server_time']//900000*900000-1)
        self.assertTrue(all(row['provenance']['closed_candle_count']==200 for row in sidecar['charts'].values()))
        self.assertTrue(all(row['browser_screenshot_of_data_render'] for row in sidecar['charts'].values()))
        self.assertFalse(sidecar['network_navigation_performed'])
        self.assertEqual(sidecar['request_sha256'],'a'*64)
        self.assertEqual(sidecar['native_context_canonical_sha256'],hashlib.sha256(json.dumps(self.market,
            sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')).hexdigest())
        self.assertEqual((Path(result['reviewed_images_key'][0]).parent/SIDECAR).stat().st_mode&0o777,0o600)
        methods=[entry[1] for entry in self.harness.calls]
        self.assertEqual(methods,['new_page','set_viewport_size','set_content','wait_for_function','screenshot',
                                 'set_content','wait_for_function','screenshot'])
        self.assertTrue(all(deadline<=75 for deadline in self.harness.deadline_checks))
        for _,method,deadline,_,kwargs in self.harness.calls:
            if method in ('set_content','wait_for_function','screenshot'):
                self.assertGreater(kwargs['timeout'],0)
                self.assertLessEqual(kwargs['timeout'],10000)
    def test_opaque_rgba_screenshot_is_verified_against_source_rgb(self):
        self.context.page_options['rgba']=True
        result=self.run_capture()
        self.assertEqual(len(result['evidence']),2)
    def test_running_candle_is_separate_and_never_plotted(self):
        self.market['timeframes']['1h']['candles'][-1]['high']='999999'
        result=self.run_capture()
        sidecar=json.loads((Path(result['reviewed_images_key'][0]).parent/SIDECAR).read_text())
        data=sidecar['public_market_context']
        self.assertEqual(data['running_candles']['1h']['high'],'999999')
        self.assertTrue(all(row['high']!='999999' for row in data['candles']['1h']))
        self.assertFalse(sidecar['charts']['1h']['provenance']['running_candle_rendered'])
    def test_incomplete_or_wrong_symbol_context_fails_before_page_creation(self):
        for mutation in ('few','symbol'):
            with self.subTest(mutation=mutation):
                ctx=copy.deepcopy(self.market)
                if mutation=='few':ctx['timeframes']['1h']['candles']=ctx['timeframes']['1h']['candles'][-200:]
                else:ctx['timeframes']['1h']['symbol']='ETHUSDT'
                with self.assertRaises(CaptureUnavailable):
                    self.capture(self.context,ctx,'request_marker','a'*64,str(self.root),deadline=80)
                self.assertEqual(self.context.created,[])
        self.assertEqual(self.harness.archive_calls,0)
    def test_one_wrong_screenshot_pixel_refuses_archiving_and_closes_only_owned_page(self):
        self.context.page_options['wrong_pixel']=True
        with self.assertRaises(CaptureUnavailable):self.run_capture()
        self.assertEqual(self.harness.archive_calls,0)
        self.assertTrue(self.context.created[0].closed)
        self.assertFalse(self.context.closed)
        self.assertEqual(self.checkpoints[-1]['outcome'],'FAILED')
    def test_existing_context_page_refused_without_profile_or_existing_page_actions(self):
        existing=object()
        self.context.pages=[existing]
        with self.assertRaises(CaptureUnavailable):self.run_capture()
        self.assertEqual(self.context.pages,[existing])
        self.assertEqual(self.context.created,[])
        self.assertEqual(self.harness.closed,[])
    def test_foreign_new_page_handle_is_never_closed_or_used(self):
        foreign_context=Context()
        foreign_page=foreign_context.new_page()
        self.harness=Harness()
        original=self.harness.rpc
        def wrong_page(resource,method,deadline,*args,**kwargs):
            if method=='new_page':return foreign_page
            return original(resource,method,deadline,*args,**kwargs)
        self.capture=self.harness.install(rpc=wrong_page)
        with self.assertRaises(CaptureUnavailable):self.run_capture()
        self.assertFalse(foreign_page.closed)
        self.assertEqual(foreign_context.pages,[foreign_page])
        self.assertEqual(self.context.created,[])
        self.assertEqual(self.harness.closed,[])
        self.assertEqual(self.harness.archive_calls,0)
    def test_context_membership_must_verify_ownership_before_cleanup(self):
        self.harness=Harness()
        original=self.harness.rpc
        def missing_membership(resource,method,deadline,*args,**kwargs):
            result=original(resource,method,deadline,*args,**kwargs)
            if method=='new_page':self.context.pages.remove(result)
            return result
        self.capture=self.harness.install(rpc=missing_membership)
        with self.assertRaises(CaptureUnavailable):self.run_capture()
        self.assertEqual(self.harness.closed,[])
        self.assertFalse(self.context.created[0].closed)
    def test_cleanup_does_not_close_page_whose_context_identity_changed(self):
        self.harness=Harness()
        original=self.harness.rpc
        foreign_context=Context()
        def changed_owner(resource,method,deadline,*args,**kwargs):
            result=original(resource,method,deadline,*args,**kwargs)
            if method=='screenshot':resource.context=foreign_context
            return result
        self.capture=self.harness.install(rpc=changed_owner)
        with self.assertRaises(CaptureUnavailable):self.run_capture()
        self.assertEqual(self.harness.closed,[])
        self.assertFalse(self.context.created[0].closed)
    def test_context_age_is_revalidated_before_archive_independently_of_native_helper(self):
        self.market=native_context_200(now=NOW-179)
        self.harness=Harness()
        original=self.harness.rpc
        def delayed(resource,method,deadline,*args,**kwargs):
            result=original(resource,method,deadline,*args,**kwargs)
            self.harness.current+=.6
            return result
        self.capture=self.harness.install(rpc=delayed)
        with self.assertRaises(CaptureUnavailable):self.run_capture()
        self.assertEqual(self.harness.archive_calls,0)
        self.assertTrue(self.context.created[0].closed)
    def test_mutated_original_context_after_render_cannot_be_archived(self):
        self.harness=Harness()
        original=self.harness.rpc
        def mutated(resource,method,deadline,*args,**kwargs):
            result=original(resource,method,deadline,*args,**kwargs)
            if method=='screenshot':self.market['mark_price']['price']='65001'
            return result
        self.capture=self.harness.install(rpc=mutated)
        with self.assertRaises(CaptureUnavailable):self.run_capture()
        self.assertEqual(self.harness.archive_calls,0)
        self.assertTrue(self.context.created[0].closed)
    def test_context_mutated_by_archive_helper_cannot_be_reported_complete(self):
        self.harness.helper_mutation=lambda attempt,result:self.market['mark_price'].__setitem__('price','65001')
        with self.assertRaises(CaptureUnavailable):self.run_capture()
        self.assertEqual(self.harness.archive_calls,1)
        self.assertEqual(self.checkpoints[-1]['outcome'],'FAILED')
        self.assertTrue(self.context.created[0].closed)
    def test_archive_name_swap_after_fd_check_is_rejected_by_named_identity(self):
        watched=[]
        def observe(attempt,result):
            path=Path(result['reviewed_images_key'][0])
            watched.append((path,path.stat().st_ino))
        self.harness.helper_mutation=observe
        original_fstat=os.fstat
        checked=[]
        swapped=[]
        def swapped_fstat(fd):
            info=original_fstat(fd)
            if watched and info.st_ino==watched[0][1]:
                checked.append(True)
                if len(checked)==2 and not swapped:
                    # The FD metadata was already observed unchanged. Model a
                    # concurrent rename after that read, before the named path
                    # is checked. FD-only validation would accept stale binding.
                    target=watched[0][0]
                    raw=target.read_bytes()
                    target.rename(target.with_suffix('.retained'))
                    with os.fdopen(os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:
                        stream.write(raw)
                    swapped.append(True)
            return info
        with patch('agent.native_chart_capture.os.fstat',side_effect=swapped_fstat):
            with self.assertRaises(CaptureUnavailable):self.run_capture()
        self.assertEqual(swapped,[True])
        self.assertFalse((watched[0][0].parent/SIDECAR).exists())
        self.assertTrue(watched[0][0].with_suffix('.retained').is_file())
        self.assertTrue(self.context.created[0].closed)
    def test_deadline_failure_cleans_owned_page_without_retry_or_legacy_capture(self):
        self.harness.deadline_method='set_content'
        with self.assertRaises(CaptureUnavailable):self.run_capture()
        self.assertTrue(self.context.created[0].closed)
        self.assertEqual(self.harness.archive_calls,0)
        self.assertEqual(self.harness.capture_calls,0)
        self.assertEqual(sum(method=='set_content' for _,method,*_ in self.harness.calls),1)
        self.assertEqual(self.checkpoints[-1]['outcome'],'FAILED')
    def test_insufficient_cleanup_budget_creates_no_page(self):
        with self.assertRaises(CaptureUnavailable):self.run_capture(deadline=4)
        self.assertEqual(self.context.created,[])
    def test_source_change_mid_capture_refused_without_archive(self):
        self.harness=Harness()
        original=self.harness.rpc
        def changed(resource,method,deadline,*args,**kwargs):
            result=original(resource,method,deadline,*args,**kwargs)
            if method=='set_content':self.harness.native['SOURCE']='WRONG'
            return result
        self.capture=self.harness.install(rpc=changed)
        with self.assertRaises(CaptureUnavailable):self.run_capture()
        self.assertEqual(self.harness.archive_calls,0)
        self.assertTrue(self.context.created[0].closed)
    def test_source_change_after_new_page_return_still_closes_known_owned_handle(self):
        self.harness=Harness()
        original=self.harness.rpc
        def changed(resource,method,deadline,*args,**kwargs):
            result=original(resource,method,deadline,*args,**kwargs)
            if method=='new_page':self.harness.native['SOURCE']='WRONG'
            return result
        self.capture=self.harness.install(rpc=changed)
        with self.assertRaises(CaptureUnavailable):self.run_capture()
        self.assertEqual(len(self.context.created),1)
        self.assertTrue(self.context.created[0].closed)
        self.assertEqual(self.harness.closed,[self.context.created[0]])
        self.assertEqual(self.harness.archive_calls,0)
    def test_nonfinite_or_invalid_deadline_refuses_before_browser_action(self):
        for deadline in (float('nan'),float('inf'),True,'80'):
            with self.subTest(deadline=str(deadline)),self.assertRaises(CaptureUnavailable):
                self.run_capture(deadline=deadline)
        self.assertEqual(self.context.created,[])
    def test_archive_symlink_or_existing_sidecar_never_overwritten(self):
        for mutation in ('symlink','sidecar'):
            harness=Harness()
            target=Path(self.temp.name)/('evidence-'+mutation)
            target.write_text('retained')
            def mutate(attempt,result,mutation=mutation):
                if mutation=='sidecar':(attempt/SIDECAR).write_text('retained')
                else:
                    path=Path(result['reviewed_images_key'][0])
                    path.unlink()
                    path.symlink_to(target)
            harness.helper_mutation=mutate
            capture=harness.install()
            context=Context()
            with self.subTest(mutation=mutation),self.assertRaises(CaptureUnavailable):
                capture(context,self.market,'request_marker','a'*64,str(self.root),deadline=80)
            self.assertTrue(context.created[0].closed)
            self.assertEqual(target.read_text(),'retained')
    def test_repeated_capture_preserves_prior_attempt_and_writes_new_private_archive(self):
        first=self.run_capture()
        first_paths=[Path(path) for path in first['reviewed_images_key']]
        hashes=[hashlib.sha256(path.read_bytes()).hexdigest() for path in first_paths]
        second=self.run_capture()
        self.assertNotEqual(first['reviewed_images_key'],second['reviewed_images_key'])
        self.assertEqual([hashlib.sha256(path.read_bytes()).hexdigest() for path in first_paths],hashes)
    def test_install_requires_explicit_reconciled_source_capture_phases_helpers_and_rpc(self):
        for mutation in ('source','capture','phase','helper','rpc','images_key'):
            harness=Harness()
            args={}
            if mutation=='source':args['expected_source']='GUESSED_OLD_SOURCE'
            elif mutation=='capture':args['expected_capture']=lambda:None
            elif mutation=='phase':args['phase_map']={}
            elif mutation=='helper':harness.native['png_pixels']=None
            elif mutation=='rpc':args['rpc']=None
            else:args['images_key']='evidence'
            with self.subTest(mutation=mutation),self.assertRaises(NativeCaptureConfigurationError):
                harness.install(**args)


if __name__=='__main__':
    unittest.main()
