import copy
import unittest
from unittest.mock import patch

from agent.health import HealthServer
from agent.office_status import append_agent_status, sanitize_agent_health


NOW = 1792410000
RELEASE = {"source_commit": "a"*40,
           "image": "ghcr.io/harun5231/harun-neurobro-agent@sha256:"+"b"*64,
           "runtime_config_sha256": "c"*64}


def observed(**changes):
    result = {"service": "harun-neurobro-agent", "compose_project": "harun-neurobro-agent",
              "source_commit": RELEASE["source_commit"], "image_digest": RELEASE["image"],
              "runtime_config_sha256": RELEASE["runtime_config_sha256"],
              "uid": 10002, "gid": 10002, "runtime_implemented": True,
              "mode": "ROBOT_GATED_COORDINATION", "process_generation": "d"*32,
              "observed_at": NOW-1, "expires_at": NOW+59, "last_tick_at": NOW-5,
              "state": "PAUSED", "phase": "IDLE", "reason": "ROBOT_OFF",
              "integration_verified": False,
              "dependencies": {name: {"verified": False, "fresh": False}
                               for name in ("backend", "browser", "model")}}
    result.update(changes)
    return result


class OfficeAgentStatusTests(unittest.TestCase):
    def sanitize(self, value, release=RELEASE):
        return sanitize_agent_health(value, expected_release=release, now=NOW)

    def test_process_present_off_is_visible_without_claiming_full_integration(self):
        value = self.sanitize(observed())
        self.assertEqual((value["status"], value["state"], value["reason"]),
                         ("AVAILABLE", "PAUSED", "ROBOT_OFF"))
        self.assertFalse(value["integration_verified"])
        self.assertTrue(value["checked_at"].endswith("Z"))

    def test_approved_release_pins_are_not_learned_from_health_claim(self):
        for key, forged in (("source_commit", "f"*40), ("image_digest", RELEASE["image"][:-1]+"f"),
                            ("runtime_config_sha256", "f"*64), ("uid", 0), ("uid", True),
                            ("gid", 0), ("mode", "LIVE_WITHOUT_GATE"),
                            ("state", ["RUNNING"]), ("phase", "<secret>")):
            with self.subTest(key=key):
                self.assertEqual(self.sanitize(observed(**{key: forged}))["status"], "UNAVAILABLE")
        for release in (None, {}, {**RELEASE, "arbitrary": "approved"}):
            self.assertEqual(self.sanitize(observed(), release)["status"], "UNAVAILABLE")

    def test_stale_future_expired_or_stalled_observation_is_never_current(self):
        for changes in ({"observed_at": NOW-60}, {"observed_at": NOW+1},
                        {"expires_at": NOW}, {"expires_at": NOW+500},
                        {"last_tick_at": NOW-182}, {"last_tick_at": NOW+1},
                        {"observed_at": float("nan")}):
            with self.subTest(changes=changes):
                value = self.sanitize(observed(**changes))
                self.assertEqual(value["status"], "UNAVAILABLE")
                self.assertFalse(value["integration_verified"])

    def test_all_dependencies_require_real_fresh_evidence_not_single_top_level_flag(self):
        dependencies = {name: {"verified": True, "fresh": True, "observed_at": NOW-2}
                        for name in ("backend", "browser", "model")}
        self.assertTrue(self.sanitize(observed(dependencies=dependencies, integration_verified=True))["integration_verified"])
        for name, age in (("backend", 31), ("browser", 1201), ("model", 1201)):
            changed = copy.deepcopy(dependencies)
            changed[name]["observed_at"] = NOW-age
            value = self.sanitize(observed(dependencies=changed, integration_verified=True))
            self.assertFalse(value["integration_verified"])
            self.assertEqual(value["dependencies"][name], {"verified": False, "checked_at": None})
        self.assertFalse(self.sanitize(observed(integration_verified=True))["integration_verified"])

    def test_conversation_credentials_config_and_error_contents_are_never_forwarded(self):
        source = observed(api_key="SECRET_SENTINEL", conversation="SECRET_SENTINEL",
                          raw_logs=["SECRET_SENTINEL"], config={"token": "SECRET_SENTINEL"})
        clean = self.sanitize(source)
        self.assertNotIn("SECRET_SENTINEL", str(clean))
        self.assertNotIn("source_commit", clean)
        self.assertNotIn("process_generation", clean)
        self.assertNotIn("image_digest", clean)
        clean = self.sanitize(observed(reason="DO_NOT_RENDER_SECRET", phase="DO_NOT_RENDER_SECRET"))
        self.assertNotIn("DO_NOT_RENDER_SECRET", str(clean))
        self.assertEqual(clean["reason"], "AGENT_STATE_REPORTED")

    def test_actual_health_observe_with_advancing_clock_has_valid_exact_expiry(self):
        class PausedController:
            def health_status(self):
                return {"state": "PAUSED", "phase": "IDLE", "reason": "ROBOT_OFF",
                        "last_tick_at": NOW-5, "dependencies": {}}

        ticks = iter((NOW, NOW+1, NOW+2))
        # Observe the actual handler DTO without opening a server or contacting
        # a dependency. An advancing clock catches a second expiry timestamp.
        server = HealthServer.__new__(HealthServer)
        server.controller, server.clock = PausedController(), lambda: next(ticks)
        server.generation, server.config_sha256, server.started_at = "d"*32, RELEASE["runtime_config_sha256"], NOW-10
        with patch("agent.health.os.geteuid", return_value=10002), patch("agent.health.os.getegid", return_value=10002), patch.dict(
                "agent.health.os.environ", {"HARUN_SOURCE_COMMIT": RELEASE["source_commit"], "HARUN_IMAGE_DIGEST": RELEASE["image"]}):
            actual = server.observe()
        self.assertEqual(actual["expires_at"]-actual["observed_at"], 60)
        value = append_agent_status({"robot": {"robot_on": False}}, actual,
                                    expected_release=RELEASE, now=NOW+0.1)["agent"]
        self.assertEqual((value["status"], value["state"]), ("AVAILABLE", "PAUSED"))
        self.assertFalse(value["integration_verified"])
        self.assertFalse(any(item["verified"] for item in value["dependencies"].values()))

    def test_agent_optional_failure_preserves_existing_office_fields_and_objects(self):
        office = {"schema_version": 2, "robot": {"robot_on": False, "risk_target_usdt": "5"},
                  "research": {"provider": "NEUROBRO_WEB", "status": "LOGIN_REQUIRED"},
                  "account": {"positions": [{"symbol": "BTCUSDT", "quantity": "1"}]},
                  "employees": [{"id": "neuro", "status": "IDLE"}]}
        before = copy.deepcopy(office)
        result = append_agent_status(office, None, expected_release=RELEASE, now=NOW)
        self.assertEqual(office, before)
        self.assertEqual({key: value for key, value in result.items() if key != "agent"}, before)
        self.assertFalse(result["agent"]["integration_verified"])
        self.assertIs(result["robot"], office["robot"])


if __name__ == "__main__":
    unittest.main()
