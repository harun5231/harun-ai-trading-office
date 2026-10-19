"""Run the isolated agent daemon: ``python -m agent --config /app-config/runtime.json``."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import threading

from worker.prompts import ANALYSIS

from .bridge import BackendBridge
from .browser_rpc import NeurobroRPC
from .charts import render_chart_png
from .config import AgentConfig, read_json_file
from .controller import Controller
from .health import HealthServer
from .llm import OpenAIDialogue
from .market import BinanceFuturesMarket
from .memory import Memory
from .models import NotSent
from .providers import NeurobroBrowser, validate_browser_contract
from .risk import parse_setup


class _LazyBrowser:
    """Do not attach to the persistent production browser while ROBOT is OFF."""
    def __init__(self, config, enabled):
        self.config, self.enabled, self._provider = config, enabled, None
        self._remaining_budget = lambda: 120.0

    def set_remaining_budget(self, callback):
        self._remaining_budget = callback
        if self._provider is not None:
            self._provider.set_remaining_budget(callback)

    def evidence(self):
        return self._provider.evidence() if self._provider is not None else {"verified": False}

    def exchange(self, *args, **kwargs):
        if self._provider is None:
            try:
                contract, contract_sha256 = read_json_file(self.config["contract_file"], include_sha=True)
            except (OSError, ValueError, UnicodeError):
                raise NotSent("BROWSER_CONTRACT_FILE_UNVERIFIED") from None
            if contract_sha256 != self.config["contract_sha256"]:
                raise NotSent("BROWSER_CONTRACT_SHA256_MISMATCH")
            if "rpc_url" in self.config:
                admitted = validate_browser_contract(contract)
                if admitted["source_sha256"] != self.config["source_sha256"]:
                    raise NotSent("BROWSER_SOURCE_PIN_MISMATCH")
                self._provider = NeurobroRPC(self.config["rpc_url"], self.config["token_file"],
                    self.config["source_sha256"], robot_enabled=self.enabled)
            else:
                self._provider = NeurobroBrowser(self.config["cdp_url"],
                    contract, robot_enabled=self.enabled)
            self._provider.set_remaining_budget(self._remaining_budget)
        return self._provider.exchange(*args, **kwargs)


def build_controller(config, memory):
    backend = config.backend
    bridge = BackendBridge(backend["url"], backend["token_file"], backend["revision"], backend["source_sha256"])
    holder = {}
    provider = _LazyBrowser(config.browser, lambda: holder["controller"].robot_enabled())
    controller = Controller(bridge=bridge, provider=provider, market=BinanceFuturesMarket(),
        llm=OpenAIDialogue(config.model["key_file"], memory), memory=memory,
        analysis_prompt=ANALYSIS, render_chart=render_chart_png, parse_setup=parse_setup)
    holder["controller"] = controller
    return controller


def run(config_path):
    config = AgentConfig.load(config_path)
    memory = Memory(Path(config.data_directory) / "agent.sqlite3")
    lock = os.open(Path(config.data_directory) / "agent.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    s = os.fstat(lock)
    if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid() or
            stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1):
        raise ValueError("CONTROLLER_LOCK_UNSAFE")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ValueError("CONTROLLER_ALREADY_RUNNING") from None
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    controller = build_controller(config, memory)
    health = HealthServer(controller, config_bytes=config.sha256.encode())
    # AgentConfig authenticated the exact bytes before parsing, not a second
    # mutable read or a hash of a hash.
    health.config_sha256 = config.sha256
    health.start()
    failures = 0
    try:
        while not stop.is_set():
            if memory.path.stat().st_size > 256 * 1024 * 1024:
                # Retain all evidence and stop rather than delete unknown outcomes.
                raise ValueError("AGENT_STORAGE_LIMIT_REACHED")
            result = controller.tick()
            print(json.dumps({"event": "AGENT_TICK", "state": result.state,
                              "phase": result.phase, "reason": result.reason}), flush=True)
            if result.state in ("BLOCKED", "REQUIRES_REVIEW"):
                failures = min(failures + 1, 8)
                delay = min(3600, config.loop["idle_tick_seconds"] * 2 ** failures)
            else:
                failures = 0
                delay = config.loop["active_tick_seconds"] if result.state == "RUNNING" else config.loop["idle_tick_seconds"]
            stop.wait(delay)
    finally:
        health.close()
        memory.close()
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description="Isolated Neurobro dialogue agent")
    parser.add_argument("--config", default=os.environ.get("HARUN_AGENT_CONFIG", "/app-config/runtime.json"))
    args = parser.parse_args()
    try:
        run(args.config)
    except Exception:
        print('{"event":"AGENT_STARTUP_REFUSED","reason":"CONFIGURATION_OR_DEPENDENCY_UNVERIFIED"}', flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
