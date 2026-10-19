import json
import unittest
import urllib.error
import urllib.request
from agent.health import HealthServer


class Controller:
    def health_status(self):
        return {"state": "PAUSED_OFF", "phase": "OFF", "reason": "ROBOT_OFF",
                "last_tick_at": 123, "secret": "PRIVATE_CANARY", "messages": ["PRIVATE_CHAT"]}


class HealthTests(unittest.TestCase):
    def test_read_only_liveness_is_not_browser_or_live_execution_readiness(self):
        server = HealthServer(Controller(), config_bytes=b"approved-config", host="127.0.0.1", port=0).start()
        self.addCleanup(server.close)
        origin = "http://127.0.0.1:" + str(server.server.server_port)
        with urllib.request.urlopen(origin + "/health", timeout=3) as response:
            value = json.load(response)
        self.assertTrue(value["runtime_implemented"])
        self.assertFalse(value["integration_verified"])
        self.assertFalse(value["deployment_authorized"])
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertEqual(value["state"], "PAUSED_OFF")
        with self.assertRaises(urllib.error.HTTPError) as result:
            urllib.request.urlopen(origin + "/health/ready", timeout=3)
        self.assertEqual(result.exception.code, 503)
        self.assertEqual(server.observe()["process_generation"], value["process_generation"])

    def test_health_failure_hides_exception_and_configuration(self):
        class Broken:
            def health_status(self):
                raise RuntimeError("PRIVATE_KEY_CANARY")
        server = HealthServer(Broken(), host="127.0.0.1", port=0).start()
        self.addCleanup(server.close)
        with self.assertRaises(urllib.error.HTTPError) as result:
            urllib.request.urlopen("http://127.0.0.1:" + str(server.server.server_port) + "/health", timeout=3)
        self.assertEqual(result.exception.code, 503)
        self.assertNotIn(b"PRIVATE", result.exception.read())
