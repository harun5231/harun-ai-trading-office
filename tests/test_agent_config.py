import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.config import AgentConfig
from agent.__main__ import _LazyBrowser, build_controller
from agent.memory import Memory
from agent.models import NotSent


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.json"
        self.value = {"schema_version": 1, "data_directory": self.temp.name,
            "backend": {"url": "http://127.0.0.1:9001", "token_file": "/run/secrets/agent_backend_token",
                        "revision": "reviewed-revision1", "source_sha256": "a" * 64},
            "browser": {"cdp_url": "http://127.0.0.1:9222", "contract_file": "/app-config/browser-contract.json",
                        "contract_sha256": "b" * 64},
            "model": {"key_file": "/run/secrets/openai_api_key"},
            "loop": {"active_tick_seconds": 2, "idle_tick_seconds": 30}}

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        self.path.write_text(json.dumps(self.value, indent=2))
        os.chmod(self.path, 0o600)
        return self.path

    def test_closed_configuration_retains_authenticated_exact_byte_hash(self):
        config = AgentConfig.load(self.write())
        self.assertEqual(config.sha256, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(config.model, {"key_file": "/run/secrets/openai_api_key"})

    def test_inline_key_and_unknown_fields_rejected(self):
        self.value["model"]["api_key"] = "never-inline-credentials"
        with self.assertRaisesRegex(ValueError, "FIELDS_INVALID"):
            AgentConfig.load(self.write())

    def test_browser_contract_requires_nonzero_pin(self):
        for value in ("0"*64, "unreviewed", None):
            self.value["browser"]["contract_sha256"] = value
            with self.assertRaisesRegex(ValueError, "BROWSER_CONTRACT_PIN"):
                AgentConfig.load(self.write())

    def test_origin_scoped_rpc_form_replaces_broad_cdp_capability(self):
        self.value["browser"] = {"rpc_url": "https://browser.example.test",
            "token_file": "/run/secrets/backend_token", "source_sha256": "a"*64,
            "contract_file": "/app-config/browser.json", "contract_sha256": "b"*64}
        config = AgentConfig.load(self.write())
        self.assertIn("rpc_url", config.browser)
        self.assertNotIn("cdp_url", config.browser)
        self.value["browser"]["cdp_url"] = "http://127.0.0.1:9222"
        with self.assertRaisesRegex(ValueError, "FIELDS_INVALID"):
            AgentConfig.load(self.write())

    def test_writable_or_symlink_configuration_refused(self):
        self.write()
        os.chmod(self.path, 0o666)
        with self.assertRaisesRegex(ValueError, "UNSAFE"):
            AgentConfig.load(self.path)
        link = Path(self.temp.name) / "link.json"
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            AgentConfig.load(link)

    def test_duplicate_json_field_is_refused(self):
        self.path.write_text('{"schema_version":1,"schema_version":1}')
        os.chmod(self.path, 0o600)
        with self.assertRaisesRegex(ValueError, "DUPLICATE"):
            AgentConfig.load(self.path)

    def test_slow_active_poll_cannot_expire_every_chart_before_discussion(self):
        self.value["loop"]["active_tick_seconds"] = 120
        with self.assertRaisesRegex(ValueError, "LOOP_BOUND_INVALID"):
            AgentConfig.load(self.write())

    def test_composition_does_not_read_credentials_attach_browser_or_open_network(self):
        config = AgentConfig.load(self.write())
        memory = Memory(Path(self.temp.name) / "agent.sqlite3")
        try:
            with patch("socket.socket", side_effect=AssertionError("network at construction")), \
                 patch("agent.__main__.read_json_file", side_effect=AssertionError("browser contract read while OFF")), \
                 patch("agent.llm.read_api_key", side_effect=AssertionError("model key read while OFF")):
                controller = build_controller(config, memory)
                observation = controller.health_status()
            self.assertFalse(observation["integration_verified"])
            self.assertEqual(observation["state"], "STARTING")
        finally:
            memory.close()

    def test_browser_contract_mismatch_stops_before_attach_or_chat(self):
        contract = Path(self.temp.name) / "browser-contract.json"
        contract.write_text('{"schema_version":1}')
        os.chmod(contract, 0o600)
        browser = _LazyBrowser({"cdp_url": "http://127.0.0.1:9222", "contract_file": str(contract),
                                "contract_sha256": "f"*64}, lambda: True)
        with patch("agent.__main__.NeurobroBrowser", side_effect=AssertionError("must not attach")):
            with self.assertRaisesRegex(NotSent, "SHA256_MISMATCH"):
                browser.exchange("message", request_id="test")


if __name__ == "__main__":
    unittest.main()
