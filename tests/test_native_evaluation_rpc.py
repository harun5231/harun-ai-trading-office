"""Local native READ-RPC and source patch tests; no service or provider calls."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from urllib.parse import urlsplit

from agent import native_evaluation_rpc as rpc
from agent.native_chart_followup import ReceiptStore, sha
from agent.native_daily_room import JOURNAL_NAME
from scripts.patch_native_evaluation_service import patch as patch_source
from worker import trade_evaluation_runtime as runtime


class NativeEvaluationRPCTests(unittest.TestCase):
    def test_room_read_never_creates_missing_journal_and_never_changes_existing(self):
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root,0o700)
            with self.assertRaises(FileNotFoundError):rpc.handle({'kind':'BOUND_ROOM'},archive_root=root,now=1793491200)
            self.assertEqual(os.listdir(root),[])
            db=sqlite3.connect(Path(root)/JOURNAL_NAME);db.execute('CREATE TABLE daily_rooms(day TEXT PRIMARY KEY,room_id TEXT,state TEXT)')
            db.execute('INSERT INTO daily_rooms VALUES(?,?,?)',('2026-11-01','a'*32,'BOUND'));db.commit();db.close()
            os.chmod(Path(root)/JOURNAL_NAME,0o600)
            before={p.name:p.read_bytes() for p in Path(root).iterdir()}
            result=rpc.handle({'kind':'BOUND_ROOM'},archive_root=root,now=1793491200)
            self.assertEqual(result,{'kind':'BOUND_ROOM','room_id':'a'*32})
            self.assertEqual(before,{p.name:p.read_bytes() for p in Path(root).iterdir()})
            with self.assertRaises(ValueError):rpc.handle({'kind':'BOUND_ROOM','path':'/etc'},archive_root=root,now=1793491200)

    def test_actual_once_receipt_chain_cannot_skip_unknown_tail(self):
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root,0o700)
            store=ReceiptStore(root,monotonic=lambda:1,wall_clock=lambda:1793491200)
            op='post-trade-evaluation:'+'a'*32;wire='b'*64
            intent=dict(kind='NATIVE_POST_TRADE_EVALUATION',operation=op,request_sha256=wire,
                followup_sequence=1,previous_followup_intent_sha256=None)
            store(stage='INTENT',receipt=intent,deadline=2)
            with self.assertRaisesRegex(ValueError,'CHAIN_INCOMPLETE'):rpc.evaluation_chain(root,op,wire)
            complete={**intent,'intent_sha256':sha(intent),'response_id':'actual-fixture','final_reply_sha256':'c'*64}
            store(stage='COMPLETE',receipt=complete,deadline=2)
            self.assertEqual(rpc.evaluation_chain(root,op,wire),[{'intent':intent,'complete':complete}])
            second={**intent,'followup_sequence':2,'previous_followup_intent_sha256':sha(intent)}
            store(stage='INTENT',receipt=second,deadline=2)
            with self.assertRaisesRegex(ValueError,'CHAIN_INCOMPLETE'):rpc.evaluation_chain(root,op,wire)
            with self.assertRaises(FileExistsError):store(stage='INTENT',receipt=intent,deadline=2)

    def test_service_patch_authenticates_read_route_before_handler_without_actor(self):
        source=textwrap.dedent('''\
            class Handler:
                def do_POST(self):
                    path=urlsplit(self.path)
                    if path.query or path.fragment:return self.reply(404, {'error':'NOT_FOUND'})
                    path=path.path
                    if path=='/v1/ask':
                        if not self.authorized():return self.reply(403, {'error':'FORBIDDEN'})
                        return self.reply(200, actor.ask(self.body()))
                    return self.reply(404, {'error':'NOT_FOUND'})
            ''').encode()
        actual=hashlib.sha256(source).hexdigest()
        with self.assertRaisesRegex(ValueError,'SOURCE_UNVERIFIED'):patch_source(source,expected_sha256='f'*64)
        changed=patch_source(source,expected_sha256=actual)
        namespace={'urlsplit':urlsplit};exec(compile(changed,'fixture-handler','exec'),namespace)
        handler=namespace['Handler']();handler.path=rpc.PATH;handler.reply=lambda status,body:(status,body)
        handler.authorized=Mock(return_value=False);handler.body=Mock(return_value={'kind':'BOUND_ROOM'})
        with patch.object(rpc,'handle',return_value={'kind':'BOUND_ROOM','room_id':'a'*32}) as read:
            self.assertEqual(handler.do_POST()[0],403);read.assert_not_called();handler.body.assert_not_called()
            handler.authorized.return_value=True
            self.assertEqual(handler.do_POST()[0],200);handler.body.assert_called_once_with(1000000);read.assert_called_once()
        with self.assertRaisesRegex(ValueError,'PATCH_SITE_UNVERIFIED'):patch_source(changed,expected_sha256=hashlib.sha256(changed).hexdigest())

    def test_worker_read_rpc_only_uses_fixed_internal_endpoint_and_decodes_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            db=sqlite3.connect(Path(directory)/'ledger.sqlite3');self.addCleanup(db.close)
            provider=SimpleNamespace(db=db,headers=lambda:{'Authorization':'Bearer fixture-only'},transport=Mock())
            reads=runtime.NativeEvaluationReads(provider)
            provider.transport.return_value=(200,{},dict(kind='BOUND_ROOM',room_id='a'*32))
            with patch.dict(sys.modules,{'worker.neurobrowser':SimpleNamespace(BASE='http://neurobro-browser:8790')}):
                self.assertEqual(reads.room(),'a'*32)
            call=provider.transport.call_args.args
            self.assertEqual(call[:2],('POST','http://neurobro-browser:8790/v1/historical-evidence'))
            provider.transport.return_value=(409,{},dict(error='EVALUATION_OWNED_CAPTURE_NOT_INDEXED'))
            with patch.dict(sys.modules,{'worker.neurobrowser':SimpleNamespace(BASE='http://neurobro-browser:8790')}):
                with self.assertRaisesRegex(ValueError,'OWNED_CAPTURE_NOT_INDEXED'):reads.room()
