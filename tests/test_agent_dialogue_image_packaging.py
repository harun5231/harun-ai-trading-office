"""Validate executable packaging; actual image smoke runs only inside Docker CI."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
PARENT_EVIDENCE=ROOT/'tests/fixtures/dialogue_agent_retained_b844.json'
BASE='ghcr.io/harun5231/harun-neurobro-agent@sha256:696219259ef3d2e8b5d336a4f784389a3a9626527ce387b9db2798752ad3c41d'


def run_blocks(workflow):
    rows=workflow.splitlines();result=[]
    for index,row in enumerate(rows):
        if row=='        run: |':
            block=[]
            for line in rows[index+1:]:
                if line and not line.startswith('          '):break
                block.append(line[10:] if line else '')
            result.append('\n'.join(block)+'\n')
    return result


class DialoguePackagingTests(unittest.TestCase):
    def setUp(self):
        self.docker=(ROOT/'deploy/dialogue-agent.Dockerfile').read_text()
        self.workflow=(ROOT/'.github/workflows/dialogue-agent-image.yml').read_text()

    def test_retained_b844_publication_evidence_remains_immutable_history(self):
        raw=PARENT_EVIDENCE.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
            '4499e7554d92c7ab8a037f8541bb4e581aa52b2879819f0720f508d0d6d725d2')
        value=json.loads(raw)
        self.assertEqual(value['source_commit'],'b844b60663adc27629d6921d77c7b255544fc1fb')
        self.assertEqual(value['image_id'],
            'sha256:7afbe0902a718590cefc5ea3fa58278246e3d51b4c4dff095e883025e6eb0953')
        self.assertNotEqual(value['image'].rsplit('@',1)[1],value['image_id'])
        self.assertEqual((value['uid'],value['native_caller_uid']),(10002,10001))

    def test_derived_image_pins_existing_base_and_inherits_its_process_contract(self):
        self.assertEqual(re.findall(r'^FROM (.+)$',self.docker,re.M),[BASE])
        self.assertEqual(re.findall(r'^ENV (.+)$',self.docker,re.M),
            ['HARUN_SOURCE_COMMIT="${REVISION}"'])
        self.assertIn('org.opencontainers.image.revision="${REVISION}"',self.docker)
        self.assertIn('io.harun.agent.contract.sha256="${CONTRACT_SHA}"',self.docker)
        contract_sha=hashlib.sha256((ROOT/'deploy/neurobro-dialogue.contract.json').read_bytes()).hexdigest()
        self.assertEqual(contract_sha,'e761f33a1575ff404535d71dc512d959eb80aca17b34a028b78640ee74d114fd')
        self.assertIn('ARG CONTRACT_SHA='+contract_sha,self.docker)
        self.assertFalse(re.search(r'^(?:RUN|USER|WORKDIR|ENTRYPOINT|CMD|EXPOSE|HEALTHCHECK|VOLUME|SHELL|ONBUILD|STOPSIGNAL)\b',
            self.docker,re.M))
        for forbidden in ('pip install','apt-get','COPY worker','COPY . ', 'agent/controller.py'):
            self.assertNotIn(forbidden,self.docker)

    def test_exact_copy_inputs_exist_and_deny_by_default_context_excludes_trading_and_credentials(self):
        rows=(ROOT/'deploy/dialogue-agent.Dockerfile.dockerignore').read_text().splitlines()
        self.assertEqual(rows[0],'**')
        allowed={row[1:] for row in rows if row.startswith('!')}
        copied={'agent/dialogue_service.py'}
        self.assertEqual(allowed,{'agent',*copied,'deploy','deploy/dialogue-agent.Dockerfile',
            'deploy/dialogue-agent.Dockerfile.dockerignore'})
        for path in allowed:
            self.assertTrue((ROOT/path).exists())
        copies=re.findall(r'^COPY --chmod=0444 (.+) /app/agent/$',self.docker,re.M)
        self.assertEqual(len(copies),1)
        self.assertEqual(set(copies[0].split()),copied)
        self.assertNotIn('hermes_model_broker.py',self.docker)
        self.assertNotIn('.env',allowed)
        self.assertFalse(any(path.startswith('worker') for path in allowed))

    def test_contract_describes_real_dialogue_without_trading_or_deployment_authority(self):
        value=json.loads((ROOT/'deploy/neurobro-dialogue.contract.json').read_bytes())
        self.assertEqual(value['runtime'],'agent.dialogue_service')
        self.assertEqual(value['mode'],'NATIVE_DELEGATED_DIALOGUE')
        self.assertEqual(value['model'],'gpt-5-mini')
        self.assertEqual(value['service_uid'],10002)
        self.assertEqual(value['native_caller_uid'],10001)
        self.assertIsNone(value['replacement_hold_limit'])
        self.assertIsNone(value['clarifications_per_conversation_phase'])
        for key in ('trading_authority','model_price_authority','financial_database_access',
            'browser_dispatch_authority','ambiguous_operation_replay','health_contact_provider',
            'deployment_authorized'):
            self.assertIs(value[key],False)
        self.assertTrue(value['native_coordinator_preserved'])

    def test_workflow_runs_only_hosted_tests_and_owner_main_manual_publish(self):
        self.assertNotIn('self-hosted',self.workflow)
        self.assertEqual(self.workflow.count('runs-on: ubuntu-24.04'),2)
        self.assertIn('permissions: {}',self.workflow)
        self.assertEqual(self.workflow.count('packages: write'),1)
        for guard in ("github.event_name == 'workflow_dispatch'", "github.repository_id == '1404087844'",
            "github.actor_id == '325709525'", "github.triggering_actor == 'harun5231'",
            "github.ref == 'refs/heads/main'",'needs: test','persist-credentials: false'):
            self.assertIn(guard,self.workflow)
        self.assertNotIn('pull_request_target',self.workflow)
        self.assertNotIn('secrets.',self.workflow)
        self.assertNotIn('curl ',self.workflow)
        self.assertNotIn('ssh ',self.workflow)
        self.assertNotIn('sudo ',self.workflow)
        for action in re.findall(r'uses: ([^\s#]+)',self.workflow):
            self.assertRegex(action,r'^[A-Za-z0-9_./-]+@[a-f0-9]{40}$')
        self.assertIn("'agent/hermes_*.py'",self.workflow)
        self.assertIn("'tests/test_hermes_*.py'",self.workflow)
        self.assertIn('httpx==0.28.1',self.workflow)
        for module in ('tests.test_hermes_dialogue','tests.test_hermes_dialogue_service',
            'tests.test_hermes_runtime','tests.test_hermes_model_broker','tests.test_dialogue_sourced_utc'):
            self.assertIn(module,self.workflow)

    def test_actual_smoke_invocations_use_network_none_uid_group_and_resource_caps(self):
        runs=[row.strip() for row in self.workflow.splitlines() if 'docker run ' in row]
        self.assertEqual(len(runs),2)
        for command in runs:
            for option in ('--network none','--read-only','--user 10002:10002','--group-add 10001',
                '--cap-drop ALL','--security-opt no-new-privileges','--pids-limit 64',
                '--memory 256m','--cpus 0.5','--entrypoint python','/smoke.py'):
                self.assertIn(option,command)
            self.assertNotIn('--privileged',command)
            self.assertNotIn('docker.sock',command)
            self.assertNotIn('OPENAI_API_KEY=',command)
        smoke=(ROOT/'scripts/smoke_dialogue_agent_image.py').read_text()
        ast.parse(smoke)
        self.assertIn('intentionally-absent-openai.key',smoke)
        self.assertIn('signal.SIGTERM',smoke)
        self.assertIn('child.kill()',smoke)
        self.assertIn('stale_socket_crash_recovery_verified',smoke)
        self.assertIn("('operations','messages','budgets')",smoke)
        self.assertIn("value['model_key_state']=='MISSING'",smoke)
        self.assertIn('error.code==503',smoke)

    def test_all_workflow_shell_and_embedded_python_are_syntax_valid(self):
        blocks=run_blocks(self.workflow)
        self.assertEqual(len(blocks),4)
        for block in blocks:
            result=subprocess.run(['/bin/bash','-n'],input=block,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            if "<<'PY'" in block:
                snippet=block.split("<<'PY'\n",1)[1].split('\nPY\n',1)[0]
                ast.parse(snippet)

    def test_artifact_builder_reports_actual_supplied_digest_and_no_deployment_approval(self):
        block=run_blocks(self.workflow)[-1]
        snippet=block.split("<<'PY'\n",1)[1].split('\nPY\n',1)[0]
        env={'SOURCE_COMMIT':'a'*40,'IMAGE_DIGEST':'ghcr.io/harun5231/harun-neurobro-agent@sha256:'+'b'*64,
             'IMAGE_ID':'sha256:'+'c'*64,'CONTRACT_SHA':'d'*64,'DIALOGUE_SHA':'e'*64}
        with tempfile.TemporaryDirectory() as temporary:
            result=subprocess.run([os.sys.executable,'-c',snippet],cwd=temporary,
                env={**os.environ,**env},capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            value=json.loads((Path(temporary)/'dialogue-agent-image.json').read_text())
        self.assertEqual(value['image'],env['IMAGE_DIGEST'])
        self.assertEqual(value['image_id'],env['IMAGE_ID'])
        self.assertEqual(value['source_commit'],env['SOURCE_COMMIT'])
        self.assertEqual(value['provider_contract_sha256'],env['CONTRACT_SHA'])
        self.assertEqual(value['base_image'],BASE)
        self.assertEqual(value['maintenance_profile'],'ISOLATED_HERMES_DIALOGUE_UTC_PROVENANCE_V1')
        self.assertEqual(value['source_files'],{'agent/dialogue_service.py':env['DIALOGUE_SHA']})
        self.assertTrue(value['hermes_runtime_inherited'])
        self.assertFalse(value['deployment_authorized'])
        self.assertTrue(value['missing_key_smoke_verified'])


if __name__=='__main__':unittest.main()
