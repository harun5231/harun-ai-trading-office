"""Offline native-shaped receipts; no deployed source or provider is claimed.

The separately reviewed root fixture must exercise the exact, authenticated
production AST before installation. These fixtures verify the additive module's
isolation, cutoff, source pins, native hash guards and lack of database writes.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import functools
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent import native_chart_history as history
from agent import native_pending_protocol as pending
from worker import neuroapi
from worker.core import Review
from tests.test_native_pending_protocol import context, evidence, INTENT, NOW, utc


OLD = 'BINANCE_FUTURES_LEGACY_SCREENSHOT'
NEW = history.CURRENT_CHART_SOURCE

# Explicitly a native-shaped offline fixture, not reconstructed production code.
NATIVE_FIXTURE = '''import hashlib, json, math, re
from datetime import datetime, timezone
from worker.core import D, Review
from worker.http_client import unique
BROWSER_PROVIDER = 'NEUROBRO_WEB'
BROWSER_CONTRACT = 'fixture-contract-v1'
BROWSER_SOURCE = 'fixture-browser-receipt'
CHART_SOURCE = 'BINANCE_FUTURES_LEGACY_SCREENSHOT'
MAX_CHART_BYTES = 4194304
MAX_REQUEST_BYTES = 393216
def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
def sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()
def marker(operation):
    return 'fixture-marker-' + sha(operation)[:32]
def request_body(operation, prompt, schema, context):
    body = dict(operation=operation, marker=marker(operation), provider=BROWSER_PROVIDER,
        contract_version=BROWSER_CONTRACT, prompt=prompt, output_schema=schema, context=context)
    serialized = encode(body)
    if len(serialized.encode()) > MAX_REQUEST_BYTES: raise Review('REQUEST_LIMIT')
    return body, serialized, sha(serialized)
def validate_chart_evidence(value, schema, context, created, received_at, max_age):
    from .neuroapi import SETUP_SCHEMA, screening_count
    try:
        if type(created) not in (int,float) or not math.isfinite(created) or created <= 0 or type(received_at) not in (int,float) or not math.isfinite(received_at) or received_at < created or type(max_age) is not int or not 30 <= max_age <= 300: raise ValueError
        if screening_count(schema) is not None:
            if type(value) is not list or value: raise ValueError
            return value
        if schema != SETUP_SCHEMA or type(value) is not list or len(value) != 2: raise ValueError
        frames, attachments, digests = set(), set(), set()
        for row in value:
            if type(row) is not dict or set(row) != {'source','symbol','timeframe','captured_at','sha256','bytes','attachment_id'} or row['source'] != CHART_SOURCE or row['symbol'] != context['symbol'] or row['timeframe'] not in ('1h','15m') or row['timeframe'] in frames or type(row['bytes']) is not int or not 0 < row['bytes'] <= MAX_CHART_BYTES or not re.fullmatch('[a-f0-9]{64}',row['sha256']) or row['sha256'] in digests or not re.fullmatch('[A-Za-z0-9_.:-]{1,160}',row['attachment_id']) or row['attachment_id'] in attachments: raise ValueError
            instant = datetime.fromisoformat(row['captured_at'].replace('Z','+00:00'))
            if instant.tzinfo is None or instant.utcoffset() != timezone.utc.utcoffset(instant): raise ValueError
            captured = instant.timestamp()
            if not created - 5 <= captured <= received_at or not 0 <= received_at-captured <= max_age: raise ValueError
            frames.add(row['timeframe']); attachments.add(row['attachment_id']); digests.add(row['sha256'])
        if frames != {'1h','15m'}: raise ValueError
        return value
    except Exception:
        raise Review('INVALID_CHART_EVIDENCE') from None
def source_evidence(db, operation):
    table = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='browser_requests'").fetchone()
    row = db.execute('SELECT * FROM browser_requests WHERE operation=?',(operation,)).fetchone() if table else None
    if row is None: return dict(source_type='UNKNOWN',contract_version='UNKNOWN')
    from .neuroapi import canonical_output, screening_count
    try:
        if (row['provider'],row['contract_version'],row['marker']) != (BROWSER_PROVIDER,BROWSER_CONTRACT,marker(operation)): raise ValueError
        body = json.loads(row['body'],object_pairs_hook=unique)
        if set(body) != {'operation','marker','provider','contract_version','prompt','output_schema','context'}: raise ValueError
        rebuilt,serialized,request_hash = request_body(operation,body['prompt'],body['output_schema'],body['context'])
        if body != rebuilt or row['body'] != serialized or row['request_sha256'] != request_hash: raise ValueError
        journal = db.execute('SELECT * FROM api_requests WHERE operation=?',(operation,)).fetchone()
        response = db.execute('SELECT * FROM browser_responses WHERE operation=?',(operation,)).fetchone()
        if not journal or not response or journal['body_hash'] != request_hash: raise ValueError
        if sha(journal['output']) != response['output_sha256'] or sha(response['assistant_text']) != response['assistant_sha256']: raise ValueError
        if not re.fullmatch('[A-Za-z0-9_.:-]{1,160}',response['response_id']): raise ValueError
        value = json.loads(response['assistant_text'],parse_float=D,object_pairs_hook=unique)
        schema = body['output_schema']
        catalog = {symbol:{} for symbol in value.get('symbols',[]) if isinstance(symbol,str)} if screening_count(schema) is not None else None
        if canonical_output(value,schema,catalog) != journal['output']: raise ValueError
        charts = json.loads(response['chart_evidence'],object_pairs_hook=unique)
        if encode(charts) != response['chart_evidence']: raise ValueError
        validate_chart_evidence(charts,schema,body['context'],journal['created'],response['received_at'],response['chart_max_age'])
        return dict(source_type=BROWSER_SOURCE,contract_version=BROWSER_CONTRACT,
            provider_evidence_sha256=sha(encode(dict(request_sha256=request_hash,marker=row['marker'],
                response_id=response['response_id'],output_sha256=response['output_sha256'],
                assistant_sha256=response['assistant_sha256'],chart_evidence_sha256=sha(response['chart_evidence']),
                received_at=response['received_at'],chart_max_age=response['chart_max_age']))))
    except Exception:
        raise Review('INVALID_RESEARCH_SOURCE_EVIDENCE') from None
'''


class NativeChartHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='harun-chart-history-')
        self.path = Path(self.temp.name) / 'native_fixture.py'
        self.path.write_text(NATIVE_FIXTURE)
        self.ns = {'__name__':'worker.chart_history_fixture','__package__':'worker'}
        exec(compile(NATIVE_FIXTURE, str(self.path), 'exec'), self.ns)
        self.canonical_patch = patch.object(neuroapi, 'canonical_output', neuroapi.canonical_output)
        self.canonical_patch.start()
        self.addCleanup(self.canonical_patch.stop)
        pending.install_source_protocol(self.ns)
        self.original = self.ns['source_evidence']
        self.validator = self.ns['validate_chart_evidence']
        self.pins = {'source_evidence_sha256':hashlib.sha256(inspect.getsource(self.original).encode()).hexdigest(),
            'validator_sha256':hashlib.sha256(inspect.getsource(inspect.unwrap(self.validator)).encode()).hexdigest()}
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE browser_requests(operation TEXT PRIMARY KEY,provider TEXT,contract_version TEXT,marker TEXT,body TEXT,request_sha256 TEXT);
            CREATE TABLE api_requests(operation TEXT PRIMARY KEY,body_hash TEXT,created REAL,output TEXT);
            CREATE TABLE browser_responses(operation TEXT PRIMARY KEY,response_id TEXT,output_sha256 TEXT,assistant_text TEXT,assistant_sha256 TEXT,chart_evidence TEXT,received_at REAL,chart_max_age INTEGER);
        ''')
        self.addCleanup(self.db.close)
        self.addCleanup(self.temp.cleanup)

    def rows(self, source, now=NOW):
        return [dict(row, source=source) for row in evidence(now)]

    def record(self, operation, *, source=OLD, created=NOW-1, is_pending=False, screening=False, charts=None):
        schema = copy.deepcopy(pending.REVIEW_SCHEMA if is_pending else neuroapi.SCREEN_ONE_SCHEMA if screening else neuroapi.SETUP_SCHEMA)
        facts = context(created)
        if not is_pending:
            facts.pop('pending_review')
        value = ({'symbols':['BTCUSDT']} if screening else
            {'symbol':'BTCUSDT','order_ref':INTENT,'decision':'TAHAN'} if is_pending else
            {'symbol':'BTCUSDT','side':'LONG','limit_entry':100,'take_profit':102,'stop_loss':99,'risk_reward':2})
        body, raw, digest = self.ns['request_body'](operation, 'Unchanged fixture prompt.', schema, facts)
        assistant = self.ns['encode'](value)
        output = neuroapi.canonical_output(value, schema, {'BTCUSDT':{}} if screening else None)
        chart_rows = [] if screening else self.rows(source, created) if charts is None else charts
        chart_text = self.ns['encode'](chart_rows)
        self.db.execute('INSERT INTO browser_requests VALUES(?,?,?,?,?,?)',
            (operation,self.ns['BROWSER_PROVIDER'],self.ns['BROWSER_CONTRACT'],self.ns['marker'](operation),raw,digest))
        self.db.execute('INSERT INTO api_requests VALUES(?,?,?,?)', (operation,digest,created,output))
        self.db.execute('INSERT INTO browser_responses VALUES(?,?,?,?,?,?,?,?)',
            (operation,'response-'+self.ns['sha'](operation)[:20],self.ns['sha'](output),assistant,
             self.ns['sha'](assistant),chart_text,created+1,180))
        return body

    def freeze_db(self):
        self.db.commit()
        self.denied = []
        def authorize(action, *_):
            if action not in {sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION}:
                self.denied.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.db.set_authorizer(authorize)

    def manifest(self):
        return {name:tuple(tuple(row) for row in self.db.execute('SELECT * FROM '+name+' ORDER BY operation'))
            for name in ('browser_requests','api_requests','browser_responses')}

    def install(self, **changes):
        values = dict(legacy_source=OLD,release_cutoff_utc=utc(NOW),**self.pins)
        values.update(changes)
        self.ns['CHART_SOURCE'] = NEW
        history.install_chart_history(self.ns, **values)

    def test_old_setup_and_owned_pending_receipts_replay_exactly_without_rewriting_or_writes(self):
        self.record('old-setup')
        self.record('pending-review-v1:'+INTENT[:32]+':1', is_pending=True)
        self.record('at-cutoff', created=NOW)
        expected = {op:self.original(self.db,op) for op in ('old-setup','pending-review-v1:'+INTENT[:32]+':1','at-cutoff')}
        before = self.manifest()
        self.freeze_db()
        self.install()
        for op, proof in expected.items():
            for _ in range(2):
                self.assertEqual(self.ns['source_evidence'](self.db,op), proof)
        self.assertEqual(self.manifest(), before)
        self.assertEqual(self.denied, [])

    def test_new_receipt_after_cutoff_uses_current_source_and_old_label_never_enters_live_validator(self):
        body = self.record('new-setup', source=NEW, created=NOW+10)
        self.freeze_db()
        self.install()
        self.assertEqual(self.ns['source_evidence'](self.db,'new-setup'), self.original(self.db,'new-setup'))
        with self.assertRaises(Review):
            self.ns['validate_chart_evidence'](self.rows(OLD,NOW+10),body['output_schema'],body['context'],NOW+10,NOW+11,180)
        self.assertIs(self.ns['validate_chart_evidence'], self.validator)
        self.assertEqual(self.ns['CHART_SOURCE'], NEW)

    def test_future_old_mixed_or_unknown_chart_sources_cannot_use_historical_fallback(self):
        self.record('future-legacy',created=NOW+0.001)
        self.record('pending-review-v1:'+INTENT[:32]+':2',created=NOW+0.001,is_pending=True)
        self.record('unknown-source',source='UNREVIEWED_SOURCE')
        self.record('mixed-source',charts=[self.rows(OLD)[0],self.rows(NEW)[1]])
        self.freeze_db()
        self.install()
        for op in ('future-legacy','pending-review-v1:'+INTENT[:32]+':2','unknown-source','mixed-source'):
            with self.subTest(operation=op), self.assertRaises(Review):
                self.ns['source_evidence'](self.db,op)
        self.assertEqual(self.denied, [])

    def test_new_pending_receipt_validates_but_legacy_label_is_historical_only(self):
        op = 'pending-review-v1:'+INTENT[:32]+':3'
        body = self.record(op,source=NEW,created=NOW+10,is_pending=True)
        self.freeze_db()
        self.install()
        expected = self.original(self.db,op)
        self.assertEqual(self.ns['source_evidence'](self.db,op),expected)
        with self.assertRaises(Review):
            self.ns['validate_chart_evidence'](self.rows(OLD,NOW+10),body['output_schema'],body['context'],NOW+10,NOW+11,180)

    def test_bad_native_marker_request_assistant_output_or_chart_hash_is_never_rescued(self):
        mutations = [
            ('browser_requests','marker','wrong'),
            ('browser_requests','request_sha256','0'*64),
            ('browser_requests','body','{}'),
            ('api_requests','body_hash','0'*64),
            ('api_requests','output','{}'),
            ('browser_responses','assistant_sha256','0'*64),
            ('browser_responses','output_sha256','0'*64),
            ('browser_responses','response_id','invalid id'),
            ('browser_responses','chart_evidence','[]'),
        ]
        for i,(table,column,value) in enumerate(mutations):
            op='bad-'+str(i)
            self.record(op)
            self.db.execute('UPDATE '+table+' SET '+column+'=? WHERE operation=?',(value,op))
        duplicate = self.rows(OLD)
        duplicate[1]['sha256'] = duplicate[0]['sha256']
        self.record('duplicate-chart',charts=duplicate)
        self.freeze_db()
        self.install()
        for op in ['bad-'+str(i) for i in range(len(mutations))]+['duplicate-chart']:
            with self.subTest(operation=op), self.assertRaises(Review):
                self.ns['source_evidence'](self.db,op)
        self.assertEqual(self.denied, [])

    def test_current_and_historical_calls_are_concurrent_without_changing_any_live_globals(self):
        self.record('old-thread')
        self.record('new-thread',source=NEW,created=NOW+10)
        expected_old = self.original(self.db,'old-thread')
        self.db.commit()
        # Each concurrent reader owns its SQLite connection, as independent
        # native threads must. A single Python SQLite authorizer connection is
        # not itself a concurrency contract for this provenance wrapper.
        readers = []
        for _ in range(4):
            reader = sqlite3.connect(':memory:',check_same_thread=False)
            reader.row_factory = sqlite3.Row
            self.db.backup(reader)
            reader.set_authorizer(lambda action,*_: sqlite3.SQLITE_OK if action in {
                sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION} else sqlite3.SQLITE_DENY)
            readers.append(reader)
            self.addCleanup(reader.close)
        self.freeze_db()
        self.install()
        expected_new = self.original(self.db,'new-thread')
        current_validator = self.ns['validate_chart_evidence']
        canonical = neuroapi.canonical_output
        before = dict(self.ns)
        barrier = threading.Barrier(4)
        def run(index):
            barrier.wait(timeout=3)
            for _ in range(10):
                op, expected = ('old-thread',expected_old) if index%2 else ('new-thread',expected_new)
                self.assertEqual(self.ns['source_evidence'](readers[index],op),expected)
                self.assertEqual(self.ns['CHART_SOURCE'],NEW)
                self.assertIs(self.ns['validate_chart_evidence'],current_validator)
            return True
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(run,range(4))),[True]*4)
        self.assertEqual(self.ns,before)
        self.assertIs(neuroapi.canonical_output,canonical)
        self.assertEqual(self.denied, [])

    def test_screening_and_unknown_legacy_returns_are_unchanged(self):
        self.record('old-screen',screening=True)
        expected = self.original(self.db,'old-screen')
        unknown = self.original(self.db,'absent')
        self.freeze_db()
        self.install()
        self.assertEqual(self.ns['source_evidence'](self.db,'old-screen'),expected)
        self.assertEqual(self.ns['source_evidence'](self.db,'absent'),unknown)

    def test_bad_source_pins_cutoff_policy_and_unreviewed_existing_wrappers_refuse_install(self):
        for changes in ({'source_evidence_sha256':'0'*64},{'validator_sha256':'f'*64},
                        {'release_cutoff_utc':'2026-10-22T00:00:00+07:00'},
                        {'release_cutoff_utc':'2026-10-22T00:00:00'},
                        {'legacy_source':NEW},{'legacy_source':'bad\nsource'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.install(**changes)
            self.assertIs(self.ns['source_evidence'],self.original)
        @functools.wraps(self.original)
        def unreviewed(db, operation):
            return self.original(db,operation)
        self.ns['source_evidence'] = unreviewed
        with self.assertRaises(ValueError):self.install()
        self.assertIs(self.ns['source_evidence'],unreviewed)

    def test_install_is_idempotent_for_exact_release_and_changed_policy_refuses(self):
        self.install()
        first = self.ns['source_evidence']
        self.install()
        self.assertIs(self.ns['source_evidence'],first)
        with self.assertRaises(ValueError):self.install(release_cutoff_utc=utc(NOW+1))
        self.assertIs(self.ns['source_evidence'],first)
        self.ns['validate_chart_evidence'] = len
        with self.assertRaises(ValueError):self.install()
        self.assertIs(self.ns['source_evidence'],first)

    def test_approved_legacy_source_can_contain_spaces_without_changing_its_receipt(self):
        source = 'Binance Futures legacy chart screenshot'
        self.ns['CHART_SOURCE'] = source
        self.record('spaced-source',source=source)
        expected = self.original(self.db,'spaced-source')
        self.freeze_db()
        self.install(legacy_source=source)
        self.assertEqual(self.ns['source_evidence'](self.db,'spaced-source'),expected)
        self.assertEqual(self.denied, [])

    def test_foreign_validator_policy_is_never_silently_removed_by_unwrap(self):
        self.record('policy-blocked-old')
        current = self.ns['validate_chart_evidence']
        @functools.wraps(current)
        def blocked(*args):
            raise Review('ADDITIONAL_POLICY_REQUIRED')
        self.ns['validate_chart_evidence'] = blocked
        self.freeze_db()
        with self.assertRaises(Review):self.original(self.db,'policy-blocked-old')
        with self.assertRaises(ValueError):self.install()
        self.assertIs(self.ns['source_evidence'],self.original)
        self.assertIs(self.ns['validate_chart_evidence'],blocked)

    def test_missing_pending_wrapper_or_changed_pending_closure_refuses_install(self):
        pure = inspect.unwrap(self.validator)
        self.ns['validate_chart_evidence'] = pure
        with self.assertRaises(ValueError):self.install()
        self.ns['validate_chart_evidence'] = self.validator
        other = dict(self.ns)
        # Even the right public wrapper code cannot bind to a foreign namespace.
        other['validate_chart_evidence'] = pure
        pending.install_source_protocol(other)
        self.ns['validate_chart_evidence'] = other['validate_chart_evidence']
        with self.assertRaises(ValueError):self.install()
        self.assertIs(self.ns['source_evidence'],self.original)

    def test_forged_installed_marker_or_missing_pending_canonical_does_not_pass_idempotency(self):
        self.install()
        installed = self.ns['source_evidence']
        @functools.wraps(installed)
        def forged(db, operation):
            return installed(db,operation)
        self.ns['source_evidence'] = forged
        with self.assertRaises(ValueError):self.install()
        self.ns['source_evidence'] = installed
        with patch.object(neuroapi,'canonical_output',lambda *_:None):
            with self.assertRaises(ValueError):self.install()

    def test_foreign_globals_builtin_or_missing_pending_canonical_are_unsupported(self):
        self.ns['CHART_SOURCE'] = NEW
        other = dict(self.ns)
        with self.assertRaises(ValueError):
            history.install_chart_history(other,legacy_source=OLD,release_cutoff_utc=utc(NOW),**self.pins)
        self.ns['source_evidence'] = len
        with self.assertRaises(ValueError):self.install()
        self.ns['source_evidence'] = self.original
        with patch.object(neuroapi,'canonical_output',lambda *_:None):
            with self.assertRaises(ValueError):self.install()
        self.assertIs(self.ns['source_evidence'],self.original)

    def test_native_signature_or_closed_over_reader_is_not_an_approved_shape(self):
        alternate = '''def source_evidence(db, *, operation):
    return {'source_type':'wrong-signature'}
'''
        path = Path(self.temp.name) / 'alternate.py'
        path.write_text(alternate)
        exec(compile(alternate,str(path),'exec'),self.ns)
        pin = hashlib.sha256(inspect.getsource(self.ns['source_evidence']).encode()).hexdigest()
        with self.assertRaises(ValueError):self.install(source_evidence_sha256=pin)
        closed = '''def factory(secret):
    def source_evidence(db, operation):
        return {'source_type':secret}
    return source_evidence
source_evidence = factory('closed-over-value')
'''
        path = Path(self.temp.name) / 'closed.py'
        path.write_text(closed)
        exec(compile(closed,str(path),'exec'),self.ns)
        pin = hashlib.sha256(inspect.getsource(self.ns['source_evidence']).encode()).hexdigest()
        with self.assertRaises(ValueError):self.install(source_evidence_sha256=pin)




class WebsiteChartHistoryTests(unittest.TestCase):
    setUp = NativeChartHistoryTests.setUp
    rows = NativeChartHistoryTests.rows
    record = NativeChartHistoryTests.record
    freeze_db = NativeChartHistoryTests.freeze_db
    manifest = NativeChartHistoryTests.manifest
    def website_install(self):
        from agent.binance_website_chart import SOURCE as website_source
        self.ns['CHART_SOURCE'] = website_source
        history.install_website_chart_history(self.ns, legacy_source=OLD,
            release_cutoff_utc=utc(NOW), data_render_release_cutoff_utc=utc(NOW+100), **self.pins)
        return website_source

    def test_two_legacy_sources_read_without_writes_and_live_input_stays_website(self):
        website = 'BINANCE_USDM_FUTURES_WEBSITE_SCREENSHOT'
        self.record('old-web',source=OLD,created=NOW-1)
        self.record('old-render',source=NEW,created=NOW+50)
        self.record('new-web',source=website,created=NOW+150)
        before=self.manifest();self.freeze_db();self.website_install()
        for operation in ('old-web','old-render','new-web'):
            self.assertEqual(self.ns['source_evidence'](self.db,operation)['source_type'],'fixture-browser-receipt')
        self.assertEqual(self.manifest(),before);self.assertEqual(self.denied,[])
        self.assertEqual(self.ns['CHART_SOURCE'],website)
        for source in (OLD,NEW):
            with self.assertRaises(Review):self.ns['validate_chart_evidence'](
                self.rows(source,NOW+150),neuroapi.SETUP_SCHEMA,context(NOW+150),NOW+150,NOW+151,180)

    def test_each_legacy_cutoff_remains_independent_and_reinstallation_is_idempotent(self):
        self.record('late-old-web',source=OLD,created=NOW+1)
        self.record('late-old-render',source=NEW,created=NOW+101)
        self.website_install();installed=self.ns['source_evidence'];self.website_install()
        self.assertIs(self.ns['source_evidence'],installed)
        for operation in ('late-old-web','late-old-render'):
            with self.assertRaises(Review):self.ns['source_evidence'](self.db,operation)

    def test_mixed_old_source_pair_is_not_admitted_by_either_historical_reader(self):
        self.record('mixed-old-sources',charts=[self.rows(OLD)[0],self.rows(NEW)[1]])
        before=self.manifest();self.freeze_db();self.website_install()
        with self.assertRaises(Review):self.ns['source_evidence'](self.db,'mixed-old-sources')
        self.assertEqual(self.manifest(),before);self.assertEqual(self.denied,[])


if __name__ == '__main__':unittest.main()
