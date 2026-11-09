"""Run the existing Mini regressions against the exact generated image source.

The canonical module remains available for historical Hermes tests. This fresh
process substitutes only the reviewed Mini build input, without any API calls.
"""
import json
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.prepare_mini_dialogue_runtime import GENERATED, SOURCE_SHA256, PROFILE, prepare, sha256


def main():
    prepare(ROOT)
    path = ROOT / GENERATED
    raw = path.read_bytes()
    if sha256(raw) != SOURCE_SHA256:
        raise ValueError('MINI_IMAGE_TEST_SOURCE_CHANGED')
    import agent
    module = types.ModuleType('agent.dialogue_service')
    module.__package__ = 'agent'
    module.__file__ = str(path)
    sys.modules['agent.dialogue_service'] = module
    agent.dialogue_service = module
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        'tests.test_agent_llm', 'tests.test_agent_dialogue_service',
        'tests.test_dialogue_sourced_utc','tests.test_mini_http_rejection','tests.test_mini_http_cooldown','tests.test_mini_paid_purpose','tests.test_mini_default_budget','tests.test_dialogue_discussion_structure','tests.test_mini_compose_archive'])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(json.dumps({'profile': PROFILE,
        'source_sha256': SOURCE_SHA256, 'tests_run': result.testsRun,
        'provider_calls': 0, 'orders': 0, 'passed': result.wasSuccessful()}, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
