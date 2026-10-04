import unittest
from pathlib import Path
from urllib.parse import parse_qs
from worker.desktop import dimensions,geometry,browser_options

class DesktopTests(unittest.TestCase):
    def test_portrait_defaults(self):
        self.assertEqual(dimensions({}),(430,932))
        self.assertEqual(geometry({}),'430x932x24')
    def test_configurable_dimensions(self):
        env={'OFFICE_DESKTOP_WIDTH':'390','OFFICE_DESKTOP_HEIGHT':'844'}
        self.assertEqual(geometry(env),'390x844x24')
        self.assertIn('--window-size=390,844',browser_options(env)['args'])
        self.assertEqual(geometry({'OFFICE_DESKTOP_WIDTH':'1024','OFFICE_DESKTOP_HEIGHT':'768'}),'1024x768x24')
    def test_invalid_sizes_fail_before_launch(self):
        for bad in ('','0','319','4097','430.5','430x932','-430','932;sh',' 430','999999'):
            for key in ('OFFICE_DESKTOP_WIDTH','OFFICE_DESKTOP_HEIGHT'):
                with self.subTest(value=bad,key=key),self.assertRaises(ValueError):dimensions({key:bad})
    def test_browser_window_follows_desktop_without_identity_emulation(self):
        options=browser_options({})
        self.assertTrue(options['no_viewport'])
        self.assertIn('--window-size=430,932',options['args'])
        self.assertNotIn('user_agent',options);self.assertNotIn('is_mobile',options)
    def test_both_launchers_and_adapters_use_shared_geometry(self):
        self.assertIn('screen_geometry=geometry()',Path('deploy/supervise.py').read_text())
        self.assertIn('python -m worker.desktop',Path('deploy/start-desktop.sh').read_text())
        for filename in ('worker/browser.py','worker/session_service.py'):
            text=Path(filename).read_text()
            self.assertIn('str(self.profile),headless=False,accept_downloads=False,**browser_options()',text)
    def test_novnc_scales_but_retains_working_websocket_path(self):
        fragment='autoconnect=1&resize=scale&path=desktop/websockify'
        for filename in ('worker/session_service.py','assets/neurobro.js'):
            self.assertIn(fragment,Path(filename).read_text())
        values=parse_qs(fragment)
        self.assertEqual(values['path'],['desktop/websockify'])
        self.assertEqual(values['resize'],['scale'])
    def test_compose_defaults_work_without_modifying_existing_env(self):
        import yaml
        env=yaml.safe_load(Path('compose.yaml').read_text())['services']['worker']['environment']
        self.assertEqual(env['OFFICE_DESKTOP_WIDTH'],'${OFFICE_DESKTOP_WIDTH:-430}')
        self.assertEqual(env['OFFICE_DESKTOP_HEIGHT'],'${OFFICE_DESKTOP_HEIGHT:-932}')

    def test_update_rebuilds_only_worker_without_deleting_volumes(self):
        script=Path('deploy/update-portrait.sh').read_text()
        self.assertIn('--build --no-deps --force-recreate --wait --wait-timeout 240 worker',script)
        self.assertIn('docker compose restart proxy',script)
        self.assertNotIn('docker compose down',script)
        self.assertNotIn('volume rm',script)
        self.assertNotIn('browser-profile',script)

if __name__=='__main__':unittest.main()
