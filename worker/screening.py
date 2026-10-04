"""Fail-closed browser screening; no Neurobro HTTP/API client or credentials."""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen
from .core import Review
from .prompts import SCREENING

PROGRESS = ('OPENING_NEUROBRO', 'WAITING_NEUROBRO', 'SCREENING_SENT', 'WAITING_RESPONSE')


def private_profile(value):
    path = Path(value).expanduser().resolve()
    repo = Path(__file__).resolve().parent.parent
    if path == repo or repo in path.parents:
        raise Review('NEEDS_REVIEW: profil browser harus di luar repository')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def futures_catalog():
    """Public contract metadata only; never a Neurobro API call."""
    try:
        with urlopen('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=15) as response:
            info = json.load(response)
        rows = [r for r in info['symbols'] if r['status'] == 'TRADING'
                and r['contractType'] == 'PERPETUAL' and r['quoteAsset'] == 'USDT'
                and r['marginAsset'] == 'USDT']
        catalog = {r['symbol']: r['baseAsset'] for r in rows}
        if not catalog:
            raise ValueError()
        return catalog
    except Exception:
        raise Review('NEEDS_REVIEW: katalog Binance Futures tidak tersedia') from None


def parse_screening(text, catalog):
    """Require two explicit numbered/bulleted/heading selections, not prose mentions.

    Tickers resolve against fresh tradable USDT perpetual metadata. No fuzzy names,
    multiplier guessing (PEPE != 1000PEPE), alternative choices or silent truncation.
    Conservative rejection is intentional; the stored prompt is never rewritten.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 50000:
        raise Review('NEEDS_REVIEW: respons screening kosong/terlalu besar')
    aliases = {}
    for symbol, base in catalog.items():
        if not re.fullmatch(r'[A-Z0-9]{2,18}USDT', symbol):
            continue
        for alias in (symbol, base):
            aliases.setdefault(alias, set()).add(symbol)
    normalized = re.sub(r'\b([A-Z0-9]{2,18})\s*/\s*USDT\b', r'\1USDT', text)
    # Refusal/conditional language is not a confirmed selection.
    if re.search(r'\b(jangan|bukan rekomendasi|tidak (?:bisa|dapat)|contoh|misalnya|alternatif|'
                 r'atau|hindari|do not|cannot|can\x27t|example|instead|alternatively|avoid|or)\b', normalized, re.I):
        raise Review('NEEDS_REVIEW: pilihan bersyarat/penolakan/alternatif ambigu')
    def symbols_in(line):
        found = set()
        for token in re.findall(r'(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,22}(?![A-Za-z0-9])|(?<![A-Za-z0-9])\d+[A-Z][A-Z0-9]*(?![A-Za-z0-9])', line):
            if token.endswith('USDT') and token not in aliases:
                raise Review('NEEDS_REVIEW: kontrak tidak ada dalam katalog Futures')
            matches = aliases.get(token, set())
            if len(matches) > 1:
                raise Review('NEEDS_REVIEW: ticker cocok ke beberapa kontrak')
            found.update(matches)
        return found
    selected = []
    for line in normalized.splitlines():
        line = line.strip()
        # Deliberately accept an unadorned full pair as well as Markdown headings.
        explicit = re.match(r'^(?:\d+[.)]\s*|[-*]\s+|#{1,6}\s+|\*\*|[A-Z0-9]{2,18}USDT$)', line)
        if not explicit:
            continue
        found = symbols_in(line)
        if not found:
            if re.match(r'^\d+[.)]\s*', line):
                raise Review('NEEDS_REVIEW: pilihan bernomor tidak memiliki kontrak yang dikenal')
            continue
        if len(found) != 1:
            raise Review('NEEDS_REVIEW: satu pilihan memuat beberapa coin')
        selected.append(next(iter(found)))
    if len(selected) != 2 or len(set(selected)) != 2 or symbols_in(normalized) != set(selected):
        raise Review('NEEDS_REVIEW: wajib tepat dua pilihan coin yang tidak ambigu')
    return selected


class ChatScreening:
    """Synchronous Playwright page driver with guarded bounded waits and single send."""
    REQUIRED = ('composer', 'send', 'assistant_messages', 'completed_response',
                'response_text', 'user_messages', 'authenticated', 'login_required',
                'captcha', 'loading', 'streaming', 'new_chat')

    def __init__(self, page, config, emit, timeout=90, response_timeout=180):
        self.page, self.n, self.emit = page, config, emit
        self.timeout, self.response_timeout = timeout, response_timeout

    @classmethod
    def validate(cls, config):
        url = urlsplit(config.get('url', ''))
        if (url.scheme != 'https' or url.hostname != 'app.neurobro.ai'
                or url.username or url.password or url.query or url.fragment
                or url.port not in (None, 443)):
            raise Review('NEEDS_REVIEW: URL resmi chatbot Neurobro diperlukan')
        if any(not isinstance(config.get(key), str) or not config[key].strip() for key in cls.REQUIRED):
            raise Review('NEEDS_REVIEW: selector screening belum diverifikasi lengkap')

    def visible(self, selector, scope=None):
        loc = (scope or self.page).locator(selector)
        return any(loc.nth(i).is_visible() for i in range(loc.count()))

    def guard(self):
        for frame in self.page.frames:
            # Visible challenge only: an inert hidden Turnstile widget is not a CAPTCHA.
            if self.visible(self.n['captcha'], frame):
                raise Review('NEEDS_LOGIN: CAPTCHA/security challenge; selesaikan sendiri di browser lokal')
        if self.visible('iframe[src*="challenges.cloudflare.com"]'):
            raise Review('NEEDS_LOGIN: CAPTCHA Cloudflare; worker berhenti')
        if self.visible(self.n['login_required']):
            raise Review('NEEDS_LOGIN: sesi Neurobro belum login atau kedaluwarsa')
        actual = urlsplit(self.page.url)
        if actual.scheme != 'https' or actual.hostname != 'app.neurobro.ai':
            raise Review('NEEDS_LOGIN: halaman dialihkan dari chatbot Neurobro')

    def wait(self, predicate, seconds, failure):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.guard()
            if predicate():
                return
            self.page.wait_for_timeout(100)
        raise Review(f'NEEDS_REVIEW: {failure}')

    def ready(self):
        c = self.page.locator(self.n['composer'])
        s = self.page.locator(self.n['send'])
        return (self.visible(self.n['authenticated']) and not self.visible(self.n['loading'])
                and c.count() == 1 and c.is_visible() and c.is_editable()
                and s.count() == 1 and s.is_visible())

    def run(self, prompt):
        if prompt != SCREENING:
            raise Review('NEEDS_REVIEW: prompt screening tidak sama dengan literal tersimpan')
        try:
            return self._run(prompt)
        except Review:
            raise
        except Exception:
            # Never expose Playwright traces, page content, session URLs or cookies in logs.
            raise Review('NEEDS_REVIEW: halaman/selector berubah atau browser gagal; jangan kirim ulang otomatis') from None

    def _run(self, prompt):
        self.emit('WAITING_NEUROBRO', 'Menunggu sesi login dan chatbot siap')
        self.wait(self.ready, self.timeout, 'chatbot belum siap; periksa login/selector')
        new = self.page.locator(self.n['new_chat'])
        if new.count() != 1 or not new.is_visible() or not new.is_enabled():
            raise Review('NEEDS_REVIEW: kontrol percakapan baru tidak tersedia')
        new.click(timeout=5000)
        def empty_chat():
            return (self.ready() and self.page.locator(self.n['assistant_messages']).count() == 0
                    and self.page.locator(self.n['user_messages']).count() == 0
                    and not self.visible(self.n['streaming']))
        self.wait(empty_chat, self.timeout, 'percakapan baru tidak kosong')
        c = self.page.locator(self.n['composer'])
        c.fill(prompt, timeout=5000)
        value = c.input_value() if c.evaluate('(e)=>["INPUT","TEXTAREA"].includes(e.tagName)') else c.inner_text()
        if value != prompt:
            raise Review('NEEDS_REVIEW: isi prompt screening di browser berubah')
        self.wait(lambda: self.page.locator(self.n['send']).is_enabled(), self.timeout, 'tombol kirim belum siap')
        self.guard()
        if not self.visible(self.n['authenticated']):
            raise Review('NEEDS_LOGIN: sesi login hilang sebelum mengirim')
        self.page.locator(self.n['send']).click(timeout=5000)  # exactly once; no automatic retry
        self.emit('SCREENING_SENT', 'Prompt screening literal dikirim sekali melalui browser')
        self.emit('WAITING_RESPONSE', 'Menunggu jawaban baru selesai; tidak menggunakan respons lama')
        stable = {'text': None, 'since': None}
        def completed():
            if not self.visible(self.n['authenticated']):
                raise Review('NEEDS_LOGIN: sesi login hilang saat menunggu respons')
            users = self.page.locator(self.n['user_messages'])
            messages = self.page.locator(self.n['assistant_messages'])
            if users.count() > 1 or messages.count() > 1:
                raise Review('NEEDS_REVIEW: percakapan berubah; respons tidak tunggal')
            if users.count() != 1 or users.inner_text().strip() != prompt:
                if users.count():
                    raise Review('NEEDS_REVIEW: pesan terkirim tidak cocok dengan prompt')
                return False
            if messages.count() != 1:
                return False
            body = messages.locator(self.n['response_text'])
            marker = messages.locator(self.n['completed_response'])
            if (body.count() != 1 or marker.count() != 1 or not marker.is_visible()
                    or self.visible(self.n['streaming'])):
                stable.update(text=None, since=None)
                return False
            text = body.inner_text().strip()
            if not text:
                return False
            if stable['text'] != text:
                stable.update(text=text, since=time.monotonic())
                return False
            return time.monotonic() - stable['since'] >= 1
        self.wait(completed, self.response_timeout, 'respons belum selesai atau penanda selesai tidak valid')
        self.guard()
        return stable['text']
