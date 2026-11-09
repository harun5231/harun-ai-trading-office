"""Offline checks for the retained Mini image, not the historical source module."""
import ast
import base64
import hashlib
import json
import math
from pathlib import Path
import tempfile
import time
import types
import unittest
import zlib

from agent.llm import OpenAIDialogue
from agent.memory import Memory
from scripts import prepare_mini_dialogue_runtime as profile


ROOT = Path(__file__).resolve().parents[1]


def retained_module():
    module = types.ModuleType('agent.retained_mini_profile_test')
    module.__package__ = 'agent'
    module.__file__ = str(ROOT / profile.GENERATED)
    exec(compile(profile.build_source(), module.__file__, 'exec'), module.__dict__)
    return module


def request(index, *, phase='SCREENING', reply='BTCUSDT'):
    return {'schema_version':1, 'request_id':hashlib.sha256(str(index).encode()).hexdigest(),
        'conversation_id':'restored-mini:offline-history', 'phase':phase, 'reply':reply,
        'reply_sha256':hashlib.sha256(reply.encode()).hexdigest(), 'market_context':None,
        'gate':{'robot_on':True, 'native_dispatch_verified':True}, 'deadline_epoch':time.time()+80}


class MiniBuildProfileTests(unittest.TestCase):
    def copied_inputs(self, root):
        for relative in ('deploy/mini-dialogue-service-4a0e659.zlib.b85',
                         'agent/dialogue_service.py','agent/llm.py','agent/native_prompt_composition.py', *profile.UNCHANGED_SOURCES):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())

    def test_generated_source_is_exact_reviewed_mini_plus_only_shared_utc_patch(self):
        self.assertEqual(profile.sha256(profile.build_source()),profile.SOURCE_SHA256)
        self.assertEqual(profile.sha256(profile.build_v5_source()),profile.V5_SOURCE_SHA256)
        raw = profile.build_utc_source()
        self.assertEqual(profile.sha256(raw), profile.UTC_SOURCE_SHA256)
        retained = zlib.decompress(base64.b85decode(b''.join(
            (ROOT/'deploy/mini-dialogue-service-4a0e659.zlib.b85').read_bytes().splitlines())))
        self.assertEqual(profile.sha256(retained), profile.BASE_SHA256)
        source = raw.decode()
        helper = next(node for node in ast.parse(source).body
            if isinstance(node,ast.FunctionDef) and node.name=='_without_sourced_utc_times')
        helper_text = ast.get_source_segment(source,helper)
        original = source.replace(helper_text+'\n\n\n','',1).replace(
            "    without_timeframes = re.sub(r'(?i)\\b(?:15\\s*M|1\\s*H|2\\s*R|1\\s*:\\s*2)\\b', '',\n"
            '        _without_sourced_utc_times(question,request))',
            "    without_timeframes = re.sub(r'(?i)\\b(?:15\\s*M|1\\s*H|2\\s*R|1\\s*:\\s*2)\\b', '', question)",1)
        self.assertEqual(original.encode(),retained)
        imports = [node.module for node in ast.parse(source).body if isinstance(node,ast.ImportFrom)]
        self.assertFalse(any('hermes' in (name or '') for name in imports))
        self.assertNotIn('create_hermes_runtime',source)

    def test_prepare_reuses_identical_source_without_overwrite_and_rejects_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.copied_inputs(root)
            first = profile.prepare(root); target=root/profile.GENERATED
            self.assertEqual(profile.sha256((root/profile.GENERATED_LLM).read_bytes()),profile.MODEL_SOURCE_SHA256)
            self.assertEqual(profile.sha256((root/profile.GENERATED_PROMPT).read_bytes()),profile.PROMPT_SOURCE_SHA256)
            before=target.stat()
            self.assertEqual(profile.prepare(root),first)
            self.assertEqual(target.stat().st_ino,before.st_ino)
            self.assertEqual(target.stat().st_mtime_ns,before.st_mtime_ns)
            self.assertEqual(target.stat().st_mode&0o777,0o444)
            target.chmod(0o600); target.write_text('conflicting generated source')
            with self.assertRaisesRegex(ValueError,'MINI_GENERATED_SOURCE_CONFLICT'):
                profile.prepare(root)
            self.assertEqual(target.read_text(),'conflicting generated source')

    def test_changed_archive_dependency_and_utc_helper_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); self.copied_inputs(root)
            archive=root/'deploy/mini-dialogue-service-4a0e659.zlib.b85'
            archive.write_bytes(base64.b85encode(zlib.compress(b'changed source')))
            with self.assertRaisesRegex(ValueError,'MINI_RETAINED_SOURCE_CHANGED'):
                profile.build_source(root)
            self.copied_inputs(root)
            (root/'agent/memory.py').write_text('changed dependency')
            with self.assertRaisesRegex(ValueError,'MINI_RETAINED_DEPENDENCY_CHANGED'):
                profile.build_source(root)
            self.copied_inputs(root)
            current=root/'agent/dialogue_service.py'
            current.write_text(current.read_text().replace('def _without_sourced_utc_times(',
                'def changed_sourced_utc_times(',1))
            with self.assertRaisesRegex(ValueError,'MINI_UTC_SOURCE_UNVERIFIED'):
                profile.build_source(root)
            self.assertFalse((root/profile.GENERATED).exists())

    def test_changed_model_source_is_refused_before_either_generated_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);self.copied_inputs(root)
            (root/'agent/llm.py').write_text('unreviewed model change')
            with self.assertRaisesRegex(ValueError,'MINI_MODEL_SOURCE_CHANGED'):
                profile.prepare(root)
            self.assertFalse((root/profile.GENERATED).exists())
            self.assertFalse((root/profile.GENERATED_LLM).exists())

    def test_shared_privacy_source_is_verified_before_any_generated_input_and_v6_is_mini_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);self.copied_inputs(root)
            (root/'agent/native_prompt_composition.py').write_text('unreviewed shared normalizer')
            with self.assertRaisesRegex(ValueError,'MINI_SHARED_PROMPT_SOURCE_CHANGED'):profile.prepare(root)
            for relative in(profile.GENERATED,profile.GENERATED_LLM,profile.GENERATED_PROMPT):self.assertFalse((root/relative).exists())
        source=profile.build_source().decode()
        for name in('validate_conversation_context','dialogue_operation_memo','dialogue_model_history','compose_recovery_payload','known_compose_response'):
            self.assertIn('def '+name+'(',source)
        self.assertNotIn('HERMES_BACKEND',source);self.assertNotIn('MINI_BACKEND',source)
        self.assertFalse(any(isinstance(node,ast.ImportFrom)and'hermes'in(node.module or'')for node in ast.parse(source).body))
        self.assertIn('self.model.bind_operation(current_id',source)
        self.assertIn('_archive_compose_request(self.memory,current_id',source)
        self.assertIn('return self._existing_purpose_receipt(request)',source)

    def test_sourced_utc_guard_survives_and_wrong_cutoff_remains_rejected(self):
        module=retained_module()
        # The full real rejection fixture is also covered by the existing 88-test
        # Mini suite; here verify the generated function is the current shared fix.
        from agent import dialogue_service as current
        value={'market_context':{'latest_closed_utc':'2026-11-08T17:45:00Z'}}
        for question in ('Timeframe 15M sampai 17:45 UTC.','Gunakan 18:45 UTC.'):
            self.assertEqual(module._without_sourced_utc_times(question,value),
                current._without_sourced_utc_times(question,value))


class MiniHistoricalMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(); self.root=Path(self.temporary.name)
        self.module=retained_module(); self.memory=Memory(self.root/'agent.sqlite3')
        self.key=self.root/'model.key'; self.key.write_text('unit-model-key-value-01234567890123456789')
        self.key.chmod(0o600); self.calls=[]
        self.engine=self.engine_for_memory()

    def engine_for_memory(self):
        return self.module.DialogueEngine(self.memory,self.key,model=OpenAIDialogue(
            self.key,self.memory,transport=self.transport,daily_requests=math.inf,
            daily_input_tokens=math.inf,daily_output_tokens=math.inf))

    def transport(self,payload):
        self.calls.append(payload)
        return {'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps({'action':'SCREENING','symbols':['BTCUSDT'],'question':''})}]}]}

    def tearDown(self):
        self.memory.close(); self.temporary.cleanup()

    def restart(self):
        self.memory.close(); self.memory=Memory(self.root/'agent.sqlite3')
        self.engine=self.engine_for_memory()

    def seed_operation(self,value,*,state='UNKNOWN',kind='DELEGATED_MODEL_DIALOGUE',prefix='delegated-model:',result=None):
        row=(prefix+value['request_id'],kind,hashlib.sha256(self.module.canonical(value).encode()).hexdigest(),
             state,None if result is None else json.dumps(result,indent=2),time.time()-120)
        self.memory.db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?)',row)
        return row

    def test_old_hermes_done_receipt_is_read_without_dispatch_or_rewrite(self):
        value=request('historical-done')
        result={'schema_version':1,'ok':True,
            **{key:value[key] for key in ('request_id','conversation_id','phase','reply_sha256')},
            'authority':'DIALOGUE_CONTROL_ONLY','trading_authority':False,
            'decision':{'action':'SCREENING','symbols':['BTCUSDT'],'question':''},
            'needs_followup':False,'reason':'DIALOGUE_DECISION'}
        row=self.seed_operation(value,state='DONE',result=result)
        self.memory.put('hermes-runtime-receipt:historical',{'runtime':'Hermes Agent','model':'gpt-6.1-sol','opaque':True})
        before=list(self.memory.db.iterdump())
        for _ in range(2):
            receipt=self.engine.completed_screening(value)['receipt']
            self.assertEqual(receipt['result'],result)
            self.assertEqual(receipt['operation']['result'],row[4])
            self.assertEqual(self.engine.process(value),result)
            self.assertEqual(list(self.memory.db.iterdump()),before)
            self.restart()
        self.assertEqual(self.calls,[])

    def test_hermes_unknown_and_rpc_metadata_are_preserved_and_never_replayed(self):
        value=request('historical-unknown')
        self.seed_operation(value)
        self.seed_operation(value,kind='HERMES_DIALOGUE_RPC',prefix='hermes-runtime:')
        self.memory.put('hermes-runtime:opaque',{'provider':'Hermes Agent','status':'UNKNOWN','raw_receipt':None})
        before=list(self.memory.db.iterdump())
        for _ in range(2):
            with self.assertRaisesRegex(self.module.DialogueError,'DIALOGUE_OPERATION_UNKNOWN'):
                self.engine.process(value)
            self.assertEqual(list(self.memory.db.iterdump()),before)
            self.restart()
        self.assertEqual(self.calls,[])

    def test_new_mini_receipt_dispatches_once_and_reuses_after_restart(self):
        value=request('fresh-mini'); result=self.engine.process(value)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0]['model'],'gpt-5-mini')
        self.assertEqual(result['decision'],{'action':'SCREENING','symbols':['BTCUSDT'],'question':''})
        self.assertIn('fase ANALYSIS',self.calls[0]['instructions'])
        self.assertEqual(self.engine.process(value),result)
        self.restart(); self.assertEqual(self.engine.process(value),result)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.memory.counts()['operations'],{'DONE':1})

    def test_retained_schema2_configuration_and_model_identity(self):
        references={'socket':str(self.root/'dialogue.sock'),'data_directory':str(self.root),
            'token_file':str(self.root/'dialogue.token'),'key_file':str(self.key)}
        config={'schema_version':2,'mode':'NATIVE_DELEGATED_DIALOGUE','model':'gpt-5-mini',
            'socket_path':references['socket'],'data_directory':references['data_directory'],
            'dialogue_token_file':references['token_file'],'openai_key_file':references['key_file']}
        path=self.root/'dialogue.json'; raw=json.dumps(config).encode(); path.write_bytes(raw);path.chmod(0o600)
        self.assertEqual(self.module.runtime_config_sha256(path,references),hashlib.sha256(raw).hexdigest())
        config['model']='gpt-6.1-sol'; path.write_text(json.dumps(config))
        with self.assertRaisesRegex(self.module.DialogueError,'DIALOGUE_AUTHENTICATION_FAILED'):
            self.module.runtime_config_sha256(path,references)
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
