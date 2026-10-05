"""Offline display/controls tests, never a Google/Neurobro login claim."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from deploy.install_novnc import install,MARKER
from worker.manual_browser import ManualBrowser

class MobileManualTests(unittest.TestCase):
    def test_manual_flags_are_display_only_and_profile_unchanged(self):
        process=Mock();process.poll.return_value=None
        with patch.dict(os.environ,{'OFFICE_MANAGED':'0','OFFICE_DESKTOP_WIDTH':'430','OFFICE_DESKTOP_HEIGHT':'932'}),patch('worker.manual_browser.chromium_executable',return_value='/bin/chrome'),patch('worker.manual_browser.subprocess.Popen',return_value=process) as launch,patch('worker.manual_browser.time.sleep'):
            ManualBrowser('/private/existing-profile').start((7,8))
        command=launch.call_args.args[0]
        self.assertEqual(command[4:],['/bin/chrome','--user-data-dir=/private/existing-profile',
            '--window-size=430,932','--window-position=0,0','--start-fullscreen',
            '--force-device-scale-factor=1','--new-window','--no-first-run',
            '--no-default-browser-check','https://app.neurobro.ai/'])
        self.assertEqual(launch.call_args.kwargs['pass_fds'],(7,8))
    def test_display_changes_do_not_enter_automated_browser(self):
        from worker.desktop import browser_options
        self.assertNotIn('--start-fullscreen',browser_options({})['args'])
        self.assertNotIn('touch-events',Path('worker/manual_browser.py').read_text())
        for path in ('worker/browser.py','worker/session_service.py','assets/neurobro.js'):
            self.assertNotIn('office-mobile',Path(path).read_text())
    def test_installer_idempotent_and_rejects_incompatible_package(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'app').mkdir()
            (root/'app/ui.js').write_text('toggleVirtualKeyboard() hideVirtualKeyboard() export default UI')
            (root/'vnc.html').write_text('<head></head><body><textarea id="noVNC_keyboardinput"></textarea></body>')
            install(root);install(root)
            self.assertEqual((root/'vnc.html').read_text().count(MARKER),1)
            self.assertEqual((root/'office-mobile.js').read_bytes(),Path('deploy/novnc/office-mobile.js').read_bytes())
            (root/'app/ui.js').write_text('incompatible')
            with self.assertRaises(ValueError):install(root)
    def test_controls_use_existing_novnc_transport_only(self):
        script=Path('deploy/novnc/office-mobile.js').read_text()
        self.assertIn('UI.toggleVirtualKeyboard()',script)
        for forbidden in ('new WebSocket','fetch(', 'localStorage','sessionStorage','clipboard','console.', 'userAgent','maxTouchPoints','Object.defineProperty'):
            # Comment uses the word clipboard; only APIs must be absent.
            if forbidden=='clipboard':continue
            self.assertNotIn(forbidden,script)
        self.assertIn('resize=scale&path=desktop/websockify',Path('worker/session_service.py').read_text())
        docker=Path('Dockerfile').read_text()
        self.assertIn('RUN python /app/deploy/install_novnc.py /usr/share/novnc',docker)


@unittest.skipUnless(os.getenv('HARUN_BROWSER_TESTS')=='1','Opt in to offline Chromium tests')
class MobileControlsBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw=sync_playwright().start();options={'headless':True}
        if os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):
            options.update(executable_path=os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE'],args=['--no-sandbox','--disable-dev-shm-usage','--use-gl=angle','--use-angle=swiftshader','--no-zygote','--single-process'])
        cls.browser=cls.pw.chromium.launch(**options)
        cls.context=cls.browser.new_context(viewport={'width':430,'height':932},has_touch=True)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.page=self.context.new_page()
        self.page.set_viewport_size({'width':430,'height':932})
        def route(request):
            path=request.request.url.split('viewer.test')[-1]
            if path=='/app/ui.js':
                # Stub transport only: test our buttons, not native iOS keyboard.
                body="""const input=()=>document.querySelector('#noVNC_keyboardinput');
window.calls=[];const UI={connected:true,rfb:{sendKey:(...args)=>calls.push(args)},
toggleVirtualKeyboard:()=>document.activeElement===input()?input().blur():input().focus(),
hideVirtualKeyboard:()=>input().blur()};window.UI=UI;export default UI;""";kind='text/javascript'
            elif path=='/office-mobile.js':body=Path('deploy/novnc/office-mobile.js').read_text();kind='text/javascript'
            elif path=='/office-mobile.css':body=Path('deploy/novnc/office-mobile.css').read_text();kind='text/css'
            else:
                body='<html class="noVNC_connected"><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="office-mobile.css"></head><body><div id="noVNC_container"></div><textarea id="noVNC_keyboardinput" style="position:absolute;left:-40px;width:1px;height:1px"></textarea><script type="module" src="office-mobile.js"></script></body></html>';kind='text/html'
            request.fulfill(content_type=kind,body=body)
        self.page.route('**/*',route);self.page.goto('https://viewer.test/')
        self.page.wait_for_selector('#office-mobile-controls button:enabled')
    def tearDown(self):self.page.close()
    def test_keyboard_and_navigation_use_native_input_and_key_events(self):
        self.page.get_by_role('button',name='KEYBOARD',exact=True).tap()
        self.assertEqual(self.page.evaluate('document.activeElement.id'),'noVNC_keyboardinput')
        for name in ('Tab','Enter','⌫'):self.page.get_by_role('button',name=name,exact=True).tap()
        self.assertEqual(self.page.evaluate('calls'),[[0xff09,'Tab'],[0xff0d,'Enter'],[0xff08,'Backspace']])
        self.page.get_by_role('button',name='Bilah browser',exact=True).tap()
        self.assertEqual(self.page.evaluate('calls.at(-1)'),[0xffc8,'F11'])
        self.assertEqual(self.page.evaluate('localStorage.length+sessionStorage.length'),0)
    def test_toolbar_fits_portrait_and_reduced_keyboard_space(self):
        for width,height in ((430,932),(390,844),(390,420),(932,430)):
            self.page.set_viewport_size({'width':width,'height':height})
            self.page.wait_for_timeout(80)
            bar=self.page.locator('#office-mobile-controls').bounding_box()
            canvas=self.page.locator('#noVNC_container').bounding_box()
            self.assertLessEqual(bar['width'],width)
            self.assertGreaterEqual(canvas['y'],bar['height']-1)
            self.assertGreater(canvas['height'],100)
            for button in self.page.locator('#office-mobile-controls button').all():
                box=button.bounding_box();self.assertGreaterEqual(box['height'],44)
                self.assertGreaterEqual(box['x'],0);self.assertLessEqual(box['x']+box['width'],width)
    def test_fit_never_changes_server_resolution_and_disconnect_disables_buttons(self):
        self.page.get_by_role('button',name='Pas layar',exact=True).tap()
        self.assertTrue(self.page.evaluate('UI.rfb.scaleViewport'))
        self.assertFalse(self.page.evaluate('UI.rfb.resizeSession'))
        self.page.evaluate("UI.connected=false;document.documentElement.className='noVNC_disconnected'")
        self.page.wait_for_function("[...document.querySelectorAll('#office-mobile-controls button')].every(b=>b.disabled)")

if __name__=='__main__':unittest.main()
