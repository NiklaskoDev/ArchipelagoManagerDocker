"""Integration test harness.

The tests start the real manager (`python -m archipelago_manager`) as a subprocess against a throw-away data folder
and talk to it over HTTP and websockets, exactly like a browser and an Archipelago client would.

Tests marked `ap` need a real Archipelago checkout and a Python environment with its requirements:

    AP_DIR=/path/to/Archipelago AP_PYTHON=/path/to/venv/bin/python pytest tests
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
MANAGER_DIR = ROOT / "manager"
AP_DIR = os.environ.get("AP_DIR", "")
AP_PYTHON = os.environ.get("AP_PYTHON", "")
HAVE_AP = bool(AP_DIR and AP_PYTHON and (Path(AP_DIR) / "Utils.py").is_file() and Path(AP_PYTHON).exists())

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(MANAGER_DIR))


def pytest_configure(config):
    config.addinivalue_line("markers", "ap: needs a real Archipelago installation (AP_DIR and AP_PYTHON)")


def pytest_collection_modifyitems(config, items):
    if HAVE_AP:
        return
    skip = pytest.mark.skip(reason="set AP_DIR and AP_PYTHON to run tests against a real Archipelago")
    for item in items:
        if "ap" in item.keywords:
            item.add_marker(skip)


def free_port_block(size: int) -> int:
    """First port of a block of `size` consecutive free ports."""
    import random
    while True:
        start = random.randint(30000, 60000 - size)
        sockets = []
        try:
            for port in range(start, start + size):
                s = socket.socket()
                s.bind(("0.0.0.0", port))
                sockets.append(s)
            return start
        except OSError:
            continue
        finally:
            for s in sockets:
                s.close()


def make_ap_copy(destination: Path) -> Path:
    """A private, hard-linked copy of the Archipelago checkout, so every test server has its own custom_worlds folder
    and host.yaml. Without it, worlds installed by one test would show up in all later ones. (Symlinks do not work:
    Python resolves a symlinked script to its real location and would run the original tree.)"""
    if destination.exists():
        return destination

    def link_or_copy(src, dst, *, follow_symlinks=True):
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)

    shutil.copytree(AP_DIR, destination, copy_function=link_or_copy, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "custom_worlds", "host.yaml*", "logs", "output"))
    (destination / "custom_worlds").mkdir()
    return destination


class Server:
    """A manager process. `stop()` + `start()` simulates a container restart on the same data folder."""

    def __init__(self, data_dir: Path, *, ap: bool, extra_env: dict[str, str] | None = None) -> None:
        self.data_dir = data_dir
        self.ap = ap
        self.web_port = free_port_block(1)
        self.game_port_start = free_port_block(20)
        self.extra_env = extra_env or {}
        self.proc: subprocess.Popen | None = None
        self.log_path = data_dir.parent / f"{data_dir.name}.log"
        self.base_url = f"http://127.0.0.1:{self.web_port}"
        self.ap_dir = make_ap_copy(data_dir.parent / f"{data_dir.name}-archipelago") if ap else None

    def start(self, wait_for_ap: bool = True) -> None:
        env = os.environ.copy()
        env.update({
            "DATA_DIR": str(self.data_dir),
            "AP_DIR": str(self.ap_dir) if self.ap else str(self.data_dir / "no-archipelago-here"),
            "AP_PYTHON": AP_PYTHON or sys.executable,
            "PORT": str(self.web_port),
            "HOST": "127.0.0.1",
            "PORT_RANGE": f"{self.game_port_start}-{self.game_port_start + 19}",
            "PUBLIC_HOST": "play.example.test",
            "PYTHONPATH": str(MANAGER_DIR),
            "UPDATE_CHECK": "false",  # no calls to GitHub from tests; test_updates turns it on against a fake
        })
        env.pop("XDG_DATA_HOME", None)
        env.update(self.extra_env)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "archipelago_manager"], cwd=MANAGER_DIR, env=env,
                                     stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 120
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"manager exited early:\n{self.log_path.read_text()[-2000:]}")
            try:
                health = httpx.get(f"{self.base_url}/api/health", timeout=2).json()
                if not (self.ap and wait_for_ap) or health.get("ready"):
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        raise RuntimeError(f"manager did not become ready:\n{self.log_path.read_text()[-2000:]}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None

    def client(self, username: str | None = None, password: str | None = None) -> httpx.Client:
        client = httpx.Client(base_url=self.base_url, headers={"X-Requested-With": "archipelago-manager"}, timeout=120)
        if username:
            r = client.post("/api/auth/login", json={"username": username, "password": password})
            assert r.status_code == 200, r.text
        return client

    def log(self) -> str:
        return self.log_path.read_text(errors="replace")


@pytest.fixture(scope="session")
def ap_server(tmp_path_factory):
    server = Server(tmp_path_factory.mktemp("apdata") / "data", ap=True)
    server.start()
    admin = server.client()
    assert admin.post("/api/auth/setup", json={"username": "admin", "password": "adminpass1"}).status_code == 200
    yield server
    server.stop()


@pytest.fixture
def plain_server(tmp_path):
    """A manager without Archipelago, enough for everything that does not need worlds."""
    server = Server(tmp_path / "data", ap=False)
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def admin(ap_server):
    return ap_server.client("admin", "adminpass1")


@pytest.fixture
def make_user(ap_server, admin):
    """Creates a user with a unique name and returns (name, logged-in client)."""
    created = []

    def _make(prefix: str = "user", role: str = "user"):
        name = f"{prefix}{len(created)}{int(time.time() * 1000) % 100000}"
        r = admin.post("/api/users", json={"username": name, "password": "password123", "role": role})
        assert r.status_code == 200, r.text
        created.append(name)
        return name, ap_server.client(name, "password123")

    return _make


@pytest.fixture
def discord(admin):
    """A fake Discord webhook set up on the shared server for one test, with links to a made-up address."""
    from helpers import FakeWebhook
    hook = FakeWebhook()
    r = admin.patch("/api/settings", json={"discord_webhook_url": hook.url, "site_url": "http://manager.test:8080"})
    assert r.status_code == 200, r.text
    yield hook
    admin.patch("/api/settings", json={"discord_webhook_url": "", "site_url": "",
                                       "notify_events": r.json()["notify_events"]})
    # Let messages still on their way reach this hook, so they do not hold up the next test's
    from helpers import wait_for
    wait_for(lambda: admin.get("/api/settings").json()["notify_status"]["queued"] == 0, timeout=60,
             message="queued notifications to be sent")
    hook.close()
