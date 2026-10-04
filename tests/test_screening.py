import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from worker.core import Ledger, Review
from worker.prompts import SCREENING
from worker.screening import ChatScreening, parse_screening, private_profile, futures_catalog, PROGRESS
from worker.workflow import Workflow

# Synthetic metadata only in tests; production always loads public exchange metadata.
CATALOG = {'ALPHAUSDT': 'ALPHA', 'BETAUSDT': 'BETA', 'GAMMAUSDT': 'GAMMA', '1000PEPEUSDT': '1000PEPE'}
CONFIG = dict(url='https://app.neurobro.ai/', composer='#composer', send='#send',
              assistant_messages='.assistant', completed_response='.done', response_text='.body',
              user_messages='.user', authenticated='#authenticated', login_required='#login',
              captcha='#captcha', loading='#loading', streaming='#streaming', new_chat='#new')

class ScreeningTests(unittest.TestCase):
    def test_pairs_and_markdown(self):
        for answer in ('ALPHAUSDT\nBETAUSDT', '1. **ALPHA/USDT** — likuid\n2. **BETAUSDT** — momentum',
                       '### 1. Alpha (ALPHA)\nAlasan: likuid.\n### 2. Beta (BETA)\nVolume tinggi.'):
            self.assertEqual(parse_screening(answer, CATALOG), ['ALPHAUSDT', 'BETAUSDT'])

    def test_wrong_count_duplicate_unknown_and_prose(self):
        for answer in ('', '1. ALPHA', '1. ALPHA\n2. ALPHA', '1. ALPHA\n2. BETA\n3. GAMMA',
                       '1. ALPHA\n2. UNKNOWNUSDT', 'ALPHA dan BETA bagus',
                       '1. ALPHA\n2. BETA\nPantau GAMMA juga.', '1. ALPHA atau BETA\n2. GAMMA',
                       'Contoh:\n1. ALPHA\n2. BETA', 'Jangan pilih\n1. ALPHA\n2. BETA',
                       '1. ALPHA\n2. PEPE', '1. ALPHA BETA\n2. GAMMA'):
            with self.subTest(answer=answer), self.assertRaises(Review):
                parse_screening(answer, CATALOG)

    def test_no_multiplier_guess_or_ambiguous_alias(self):
        self.assertEqual(parse_screening('1. 1000PEPE\n2. BETA', CATALOG), ['1000PEPEUSDT', 'BETAUSDT'])
        with self.assertRaises(Review):
            parse_screening('1. ALPHA\n2. BETA', {**CATALOG, 'XALPHAUSDT':'ALPHA'})

    def test_metadata_failure_no_fallback_coins(self):
        with patch('worker.screening.urlopen', side_effect=OSError('secret URL')):
            with self.assertRaisesRegex(Review, '^NEEDS_REVIEW: katalog Binance Futures tidak tersedia$'):
                futures_catalog()

    def test_profile_outside_repository_and_private(self):
        with self.assertRaises(Review): private_profile(Path(__file__).parent/'profile')
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(private_profile(Path(d)/'profile').stat().st_mode & 0o777, 0o700)

    def test_validate_url_and_missing_selectors(self):
        ChatScreening.validate(CONFIG)
        for url in ('http://app.neurobro.ai/', 'https://evil.test/', 'https://app.neurobro.ai/?token=x',
                    'https://user:password@app.neurobro.ai/', 'https://app.neurobro.ai:444/'):
            with self.assertRaises(Review): ChatScreening.validate({**CONFIG, 'url':url})
        with self.assertRaises(Review): ChatScreening.validate({**CONFIG, 'authenticated':''})

    def test_workflow_progress_and_screening_only(self):
        class Adapter:
            source='BROWSER_DRY_RUN';connected=False;progress=None
            def screen(self, prompt):
                assert prompt == SCREENING
                for state in PROGRESS: self.progress(state, 'test')
                return 'ALPHAUSDT\nBETAUSDT'
            def capture(self, *args): raise AssertionError('Screening must not enter Binance')
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'ledger.sqlite3')
            data=Workflow(ledger,Adapter(),Path(d)/'snapshot.json').run(screening_only=True)
            self.assertEqual(data['status'],'COINS_SELECTED');self.assertEqual(ledger.count(),0)
            self.assertFalse(data['live_enabled'])
            self.assertEqual([e['state'] for e in data['events']],
                             ['IDLE','SCREENING_NEUROBRO',*PROGRESS,'COINS_SELECTED'])
            ledger.db.close()

    def test_login_error_stops_workflow_without_order(self):
        class Adapter:
            source='BROWSER_DRY_RUN';connected=False;progress=None
            def screen(self,prompt):
                self.progress('OPENING_NEUROBRO','test')
                raise Review('NEEDS_LOGIN: CAPTCHA')
            def capture(self,*args): raise AssertionError('No further steps allowed')
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'ledger.sqlite3')
            data=Workflow(ledger,Adapter(),Path(d)/'snapshot.json').run()
            self.assertEqual(data['status'],'ERROR');self.assertEqual(ledger.count(),0)
            self.assertIn('NEEDS_LOGIN',json.dumps(data['events']));ledger.db.close()

if __name__=='__main__': unittest.main()
