"""Actual four-file batches remain strict while a React file witness settles."""
import ast
import copy
import inspect
import textwrap
from types import SimpleNamespace
import unittest

from agent.native_browser_hook import DelegatedDOMBridge, _digest
from agent.native_file_upload import NativeFileTurnReader


class Unknown(Exception): pass
class NotSubmitted(Exception): pass


class BatchBridge:
    def __init__(self):
        # Real LINK failure pattern: new PNGs and 15M CSV, unchanged 1H CSV.
        self.batches = [
            ['initial-15m.png', 'initial-1h.png',
             'LINKUSDT_15m_200_closed_342f5567.csv', 'LINKUSDT_1h_200_closed_083105ec.csv'],
            ['fresh-15m.png', 'fresh-1h.png',
             'LINKUSDT_15m_200_closed_5c209121.csv', 'LINKUSDT_1h_200_closed_083105ec.csv']]
        self.messages = [
            {'role': 'user', 'id': 'user-1', 'text': 'initial400', 'png': self.batches[0][:2], 'csv': self.batches[0][2:]},
            {'role': 'assistant', 'id': 'reply-1', 'text': 'SHORT initial TP', 'complete': True},
            {'role': 'user', 'id': 'user-2', 'text': 'fresh400 confirm TP', 'png': self.batches[1][:2], 'csv': self.batches[1][2:]},
            {'role': 'assistant', 'id': 'reply-2', 'text': 'SHORT final TP confirmed', 'complete': True}]
        self.current = 0.; self.timeout = .1; self.operation_deadline = .1
        self.uploaded_names = list(self.batches[1]); self.waits = 0; self.sends = 0
        self.page = SimpleNamespace(url='https://fixture.invalid/chat/owned', wait_for_timeout=self.wait)
        self.gap_offset = None; self.gap_until = 0; self.after_wait = None
        self._turns = self.native_turns
        self.native = SimpleNamespace(MESSAGE='messages', NotSubmitted=NotSubmitted,
            OutcomeUnknown=Unknown, image_artifacts=lambda *_: None)
        self.reader = NativeFileTurnReader(self, self.native)
        self._turns = self.reader.read
        for batch in self.batches:
            self.reader.turn_files[tuple(batch)] = (tuple(batch[:2]), tuple(batch[2:]), self.witness)
    def clock(self): return self.current
    def status(self): return 'CONNECTED'
    def _until(self, seconds): return min(self.current + seconds, self.operation_deadline)
    def _timeout(self, milliseconds): return milliseconds
    def _visible(self, selector):
        assert selector == 'messages'
        return list(enumerate(self.messages))
    def wait(self, milliseconds):
        self.current += milliseconds / 1000; self.waits += 1
        if self.after_wait: self.after_wait(self)
    def witness(self, turn, names, *, user=False):
        offset, message = turn
        return (user and list(names) == message.get('csv')
            and not (offset == self.gap_offset and self.waits < self.gap_until))
    def native_turns(self):
        result = []
        for message in self.messages:
            row = {k: copy.deepcopy(v) for k, v in message.items() if k not in ('png', 'csv', 'foreign')}
            if row['role'] == 'user':
                row['attachments'] = (message['foreign'] if 'foreign' in message else
                    list(self.uploaded_names) if list(self.uploaded_names) == message.get('png') else ['UNCONFIRMED'])
            result.append(row)
        return result
    def transcript(self):
        # Exercise the exact production nested functions with the actual
        # NativeFileTurnReader, rather than a copy of the projection algorithm.
        syntax = ast.parse(textwrap.dedent(inspect.getsource(DelegatedDOMBridge._ask)))
        functions = [n for n in syntax.body[0].body if isinstance(n, ast.FunctionDef)
            and n.name in ('gate', 'turns_with_names', 'transcript')]
        prefix = copy.deepcopy(self.native_turns()[:2]); prefix[0]['attachments'] = list(self.batches[0])
        self.prefix = copy.deepcopy(prefix); self.delivery = SimpleNamespace(observations=[{}])
        payload = {'owned': 'immutable'}
        scope = {'self': SimpleNamespace(bridge=self, native=self.native, clock=self.clock),
            'state': {'prefix': prefix, 'turn_names': copy.deepcopy(self.batches),
                'initial_url': self.page.url, 'fresh': None},
            'until': self.operation_deadline, 'payload': payload, 'payload_digest': _digest(payload),
            'chart_images': (), 'chart_evidence': (), 'dispatch_check': lambda: True,
            'delivery': self.delivery, '_digest': _digest}
        def unknown(): raise Unknown()
        scope['unknown'] = unknown
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<actual-production-transcript>', 'exec'), scope)
        return scope['transcript'](allow_pending=True)


class NativeFollowupAttachmentSettle(unittest.TestCase):
    def test_completed_followup_waits_for_initial_or_fresh_four_file_witness(self):
        for offset in (0, 2):
            b = BatchBridge(); b.gap_offset = offset; b.gap_until = 2
            rows = b.transcript()
            self.assertEqual(rows[0]['attachments'], b.batches[0])
            self.assertEqual(rows[2]['attachments'], b.batches[1])
            self.assertEqual(rows[:2], b.prefix)
            self.assertEqual(b.batches[0][-1], b.batches[1][-1])
            self.assertEqual(b.uploaded_names, b.batches[1]); self.assertEqual(b.sends, 0)
            self.assertEqual(b.waits, 2); self.assertTrue(b.native_attachment_projection['recovered'])
            self.assertEqual(b.delivery.observations[-1]['turn_projection']['resamples'], 2)
    def test_permanently_missing_initial_or_fresh_witness_never_becomes_a_receipt(self):
        for offset in (0, 2):
            b = BatchBridge(); b.gap_offset = offset; b.gap_until = 1000
            with self.assertRaises(Unknown): b.transcript()
            self.assertGreaterEqual(b.clock(), b.operation_deadline)
            self.assertEqual(b.sends, 0); self.assertEqual(b.native_attachment_projection['offset'], offset)
            self.assertNotIn('recovered', b.native_attachment_projection)
    def test_changed_prefix_fields_fail_before_another_wait(self):
        for offset, key, value in ((0, 'text', 'changed'), (0, 'id', 'other-id'),
                (0, 'role', 'assistant'), (1, 'complete', False)):
            b = BatchBridge(); b.gap_offset = 2; b.gap_until = 2
            b.after_wait = lambda bridge: bridge.messages[offset].update({key: value})
            with self.assertRaises(Unknown): b.transcript()
            self.assertEqual(b.waits, 1); self.assertEqual(b.sends, 0)
    def test_prefix_changed_between_native_snapshots_cannot_hide_behind_missing_files(self):
        b = BatchBridge(); b.gap_offset = 0; b.gap_until = 2
        original = b.reader.original_turns; calls = []
        def changing():
            rows = original(); calls.append(True)
            if len(calls) == 1: b.messages[0]['id'] = 'different-native-id'
            return rows
        b.reader.original_turns = changing
        with self.assertRaises(Unknown): b.transcript()
        self.assertEqual(b.waits, 0); self.assertEqual(b.sends, 0)

    def test_confirmed_foreign_attachments_are_rejected_immediately(self):
        b = BatchBridge(); b.messages[0]['foreign'] = ['foreign.png']
        with self.assertRaises(Unknown): b.transcript()
        self.assertEqual(b.waits, 0); self.assertEqual(b.sends, 0)
    def test_changed_url_cannot_be_accepted_after_witness_settles(self):
        b = BatchBridge(); b.gap_offset = 2; b.gap_until = 1
        b.after_wait = lambda bridge: setattr(bridge.page, 'url', 'https://fixture.invalid/chat/other')
        with self.assertRaises(Unknown): b.transcript()
        self.assertEqual(b.waits, 1); self.assertEqual(b.sends, 0)


if __name__ == '__main__': unittest.main()
