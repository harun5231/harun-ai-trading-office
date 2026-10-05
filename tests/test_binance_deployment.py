import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import yaml
from deploy import setup

class BinanceDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.repo=self.root/'checkout';self.repo.mkdir()
        self.private=self.root/'private';self.folder=self.private/'secrets';self.folder.mkdir(parents=True)
        self.neuro=self.folder/'neuroapi_key';self.neuro.write_text('existing-neuro-placeholder\n');self.neuro.chmod(0o600)
        (self.repo/'.env').write_text('OFFICE_PRIVATE_DIR='+str(self.private)+'\n')
        self.before=Path.cwd();os.chdir(self.repo);self.addCleanup(os.chdir,self.before)
    def test_setup_private_atomic_files_preserves_neuro_and_existing_keys(self):
        old=self.neuro.read_bytes();out=io.StringIO()
        with patch('deploy.setup.getpass.getpass',side_effect=['synthetic-binance-key','synthetic-binance-secret']) as prompt,contextlib.redirect_stdout(out):setup.main()
        self.assertEqual([c.args[0] for c in prompt.call_args_list],['Tempel Binance API Key (tidak terlihat), lalu Enter: ','Tempel Binance API Secret (tidak terlihat), lalu Enter: '])
        for name in ('binance_api_key','binance_api_secret'):
            p=self.folder/name;self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)
            self.assertNotIn(p.read_text().strip(),out.getvalue())
        for folder in (self.private,self.folder):self.assertEqual(stat.S_IMODE(folder.stat().st_mode),0o700)
        self.assertEqual(self.neuro.read_bytes(),old);self.assertFalse(list(self.folder.glob('.secret-*')))
        with patch('deploy.setup.getpass.getpass',side_effect=AssertionError('Existing secret reprompted')),contextlib.redirect_stdout(io.StringIO()):setup.main()
        self.assertNotIn('synthetic', (self.repo/'.env').read_text())
    def test_setup_invalid_input_not_saved(self):
        for value in ('short','bad key with spaces','é'*20,'x'*513):
            with patch('deploy.setup.getpass.getpass',return_value=value),self.assertRaisesRegex(SystemExit,'BINANCE_SECRET_INVALID'):
                setup.binance_secret(self.folder,'binance_api_key','prompt')
            self.assertFalse((self.folder/'binance_api_key').exists())
    def test_no_echo_fallback_without_private_terminal(self):
        import warnings,getpass
        def unsafe(_):warnings.warn('no tty',getpass.GetPassWarning)
        with patch('deploy.setup.getpass.getpass',side_effect=unsafe),self.assertRaisesRegex(SystemExit,'PRIVATE_TERMINAL_REQUIRED'):
            setup.binance_secret(self.folder,'binance_api_key','prompt')
        self.assertFalse((self.folder/'binance_api_key').exists())
    def test_atomic_failure_cleans_temp_does_not_replace_existing_empty_file(self):
        target=self.folder/'binance_api_key';target.write_bytes(b'');target.chmod(0o600)
        with patch('deploy.setup.getpass.getpass',return_value='synthetic-binance-key'),patch('deploy.setup.os.replace',side_effect=OSError('write failed')),self.assertRaises(OSError):
            setup.binance_secret(self.folder,target.name,'prompt')
        self.assertEqual(target.read_bytes(),b'');self.assertFalse(list(self.folder.glob('.secret-*')))
    def test_symlink_and_repository_destination_rejected(self):
        (self.folder/'binance_api_key').symlink_to(self.neuro)
        with self.assertRaisesRegex(SystemExit,'SECRET_SYMLINK_REJECTED'):setup.binance_secret(self.folder,'binance_api_key','prompt')
        (self.repo/'.env').write_text('OFFICE_PRIVATE_DIR='+str(self.repo/'private'))
        with self.assertRaises(SystemExit):setup.main()
    def test_compose_file_paths_only_and_readonly_worker(self):
        config=yaml.safe_load((self.before/'compose.yaml').read_text());worker=config['services']['worker']
        for name,env in [('binance_api_key','BINANCE_API_KEY_FILE'),('binance_api_secret','BINANCE_API_SECRET_FILE')]:
            self.assertIn(name,worker['secrets']);self.assertEqual(worker['environment'][env],'/run/office/'+name)
            self.assertEqual(config['secrets'][name]['file'],'${OFFICE_PRIVATE_DIR}/secrets/'+name)
            self.assertNotIn(env.removesuffix('_FILE'),worker['environment'])
        self.assertTrue(worker['read_only']);self.assertEqual(worker['volumes'],['worker_data:/data'])
        self.assertEqual(worker['environment']['OFFICE_AUTO_DRY_RUN'],'${OFFICE_AUTO_DRY_RUN:-false}')
    def test_boot_copies_private_files_then_drops_privileges(self):
        from deploy import container_boot
        base=self.root/'container';(base/'run/secrets').mkdir(parents=True)
        for name,value in [('read_token','r'*32),('control_token','c'*32),('neuroapi_key','existing-neuro'),('binance_api_key','synthetic-binance-key'),('binance_api_secret','synthetic-binance-secret')]:
            (base/'run/secrets'/name).write_text(value)
        def mapped(value):return base/str(value).lstrip('/')
        with patch('deploy.container_boot.Path',side_effect=mapped),patch('deploy.container_boot.os.chown'),patch('deploy.container_boot.os.setgroups') as groups,patch('deploy.container_boot.os.setgid') as gid,patch('deploy.container_boot.os.setuid') as uid,patch('deploy.container_boot.os.execvp') as execute,patch.dict(os.environ,{}):
            container_boot.main()
            for name,env in [('binance_api_key','BINANCE_API_KEY_FILE'),('binance_api_secret','BINANCE_API_SECRET_FILE')]:
                path=base/'run/office'/name
                self.assertEqual(path.read_bytes(),(base/'run/secrets'/name).read_bytes())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
                self.assertEqual(os.environ[env],str(path));self.assertNotIn(env.removesuffix('_FILE'),os.environ)
            groups.assert_called_once_with([]);gid.assert_called_once_with(10001);uid.assert_called_once_with(10001)
            execute.assert_called_once_with('python',['python','-m','worker.api_service'])
    def test_ignore_covers_secret_and_docker_never_copies_private_directory(self):
        ignore=(self.before/'.gitignore').read_text();self.assertIn('binance_api_secret',ignore);self.assertIn('*api_key*',ignore)
        dockerignore=(self.before/'.dockerignore').read_text();self.assertTrue(dockerignore.startswith('**'));self.assertNotIn('!secrets',dockerignore)
