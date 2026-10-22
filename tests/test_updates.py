"""Noticing newer Archipelago releases. GitHub is replaced by a local fake so the tests run offline."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from archipelago_manager.updates import parse_version
from conftest import Server
from helpers import FakeWebhook, wait_for


class FakeGitHub:
    """Serves /latest like GitHub's releases API and counts the requests."""

    def __init__(self) -> None:
        self.release = {"tag_name": "0.9.9", "html_url": "https://example.test/releases/0.9.9"}
        self.status = 200
        self.requests = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.requests += 1
                body = json.dumps(fake.release).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/latest"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def github():
    fake = FakeGitHub()
    yield fake
    fake.close()


def start(tmp_path, github, **env):
    server = Server(tmp_path / "data", ap=True, extra_env={"AP_RELEASES_URL": github.url, "UPDATE_CHECK": "true", **env})
    server.start()
    admin = server.client()
    admin.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123"})
    return server, admin


@pytest.mark.parametrize("text, expected", [
    ("0.6.8", (0, 6, 8)), ("v1.2.3", (1, 2, 3)), ("0.7.0-rc1", None), ("nightly", None), ("", None), (None, None),
])
def test_parse_version(text, expected):
    assert parse_version(text) == expected


@pytest.mark.ap
def test_a_newer_release_is_reported(tmp_path, github):
    server, admin = start(tmp_path, github)
    try:
        update = admin.get("/api/system").json()["update"]
        assert update["enabled"] is True and update["installed"] == "0.6.8"
        assert update["latest"] == "0.9.9" and update["update_available"] is True
        assert update["url"] == "https://example.test/releases/0.9.9" and update["checked_at"]
        assert github.requests == 1  # checked once at startup, not on every page view
        admin.get("/api/system")
        assert github.requests == 1

        # once the image catches up (or GitHub's latest is older, e.g. a pinned newer build) there is nothing to do
        github.release = {"tag_name": "0.6.8", "html_url": "x"}
        r = admin.post("/api/system/check-updates")
        assert r.status_code == 200 and r.json()["update_available"] is False and r.json()["latest"] == "0.6.8"
        github.release = {"tag_name": "0.5.0", "html_url": "x"}
        assert admin.post("/api/system/check-updates").json()["update_available"] is False

        # release candidates and odd names are ignored, keeping the last good answer
        github.release = {"tag_name": "0.9.0-rc1", "html_url": "x"}
        r = admin.post("/api/system/check-updates").json()
        assert r["latest"] == "0.5.0" and "unexpected release name" in r["error"]

        # only admins may ask
        admin.post("/api/users", json={"username": "pleb", "password": "plebpass123"})
        user = server.client("pleb", "plebpass123")
        assert user.post("/api/system/check-updates").status_code == 403
        assert user.get("/api/system").status_code == 403
    finally:
        server.stop()


@pytest.mark.ap
def test_each_new_release_is_announced_on_discord_once(tmp_path, github):
    hook = FakeWebhook()
    server, admin = start(tmp_path, github)
    try:
        # the check at startup found 0.9.9 before anyone set up notifications: that is not "announced" yet
        admin.patch("/api/settings", json={"discord_webhook_url": hook.url})
        admin.post("/api/system/check-updates")
        message = hook.wait_for("Archipelago 0.9.9 is available", timeout=20)
        assert "This server runs 0.6.8" in message["description"]
        assert message["url"] == "https://example.test/releases/0.9.9"
        admin.post("/api/system/check-updates")
        github.release = {"tag_name": "1.0.0", "html_url": "https://example.test/releases/1.0.0"}
        admin.post("/api/system/check-updates")
        hook.wait_for("Archipelago 1.0.0 is available", timeout=20)

        server.stop()  # what was announced is remembered across restarts
        server.start()
        admin = server.client("boss", "bosspass123")
        wait_for(lambda: github.requests >= 4, timeout=20, message="the check at startup")
        admin.post("/api/system/check-updates")
        time.sleep(2)
        assert hook.titles() == ["Archipelago 0.9.9 is available", "Archipelago 1.0.0 is available"]
    finally:
        server.stop()
        hook.close()


@pytest.mark.ap
def test_being_offline_is_harmless(tmp_path, github):
    github.status = 500
    server, admin = start(tmp_path, github)
    try:
        update = admin.get("/api/system").json()["update"]
        assert update["latest"] is None and update["update_available"] is False
        assert update["error"].startswith("Could not check for updates")
        assert admin.get("/api/health").json()["ready"] is True  # nothing else is affected
    finally:
        server.stop()


@pytest.mark.ap
def test_checks_can_be_turned_off(tmp_path, github):
    server, admin = start(tmp_path, github, UPDATE_CHECK="false")
    try:
        update = admin.get("/api/system").json()["update"]
        assert update["enabled"] is False and update["latest"] is None
        assert admin.post("/api/system/check-updates").status_code == 409
        assert github.requests == 0  # never contacted
    finally:
        server.stop()
