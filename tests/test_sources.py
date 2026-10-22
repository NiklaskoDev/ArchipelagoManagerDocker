"""Custom games from GitHub repositories. A local fake stands in for GitHub, so nothing leaves the machine."""
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import pytest

from archipelago_manager.sources import SourceError, _stem, parse_repo
from conftest import Server
from helpers import FakeWebhook, build_apworld


class FakeGitHub:
    """Serves repositories, their latest release and its files like api.github.com does, and counts downloads."""

    def __init__(self) -> None:
        self.repos: dict[str, dict] = {}  # "owner/name" -> {"description", "tag", "assets": {name: bytes}}
        self.downloads: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self, status, body=b"", content_type="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parts = [unquote(p) for p in self.path.strip("/").split("/")]
                if parts[0] == "download" and len(parts) == 5:
                    repo = fake.repos.get(f"{parts[1]}/{parts[2]}".lower())
                    data = repo and repo["assets"].get(parts[4])
                    if data is None:
                        return self.answer(404, b"{}")
                    fake.downloads.append(parts[4])
                    return self.answer(200, data, "application/octet-stream")
                if parts[0] != "repos" or len(parts) not in (3, 5):
                    return self.answer(404, b"{}")
                repo = fake.repos.get(f"{parts[1]}/{parts[2]}".lower())
                if repo is None:
                    return self.answer(404, json.dumps({"message": "Not Found"}).encode())
                if len(parts) == 3:
                    return self.answer(200, json.dumps({"full_name": repo["full_name"], "description": repo["description"],
                                                        "html_url": f"https://github.com/{repo['full_name']}"}).encode())
                if repo["tag"] is None:
                    return self.answer(404, json.dumps({"message": "Not Found"}).encode())
                base = f"{fake.url}/download/{repo['full_name']}/{repo['tag']}"
                # like GitHub: a file uploaded again gets a new id and time, and the digest is its sha256
                assets = [{"name": name, "size": len(data), "browser_download_url": repo.get("url_override") or f"{base}/{name}",
                           "id": repo["stamp"] + i, "updated_at": str(repo["stamp"]),
                           "digest": repo.get("digest") or f"sha256:{hashlib.sha256(data).hexdigest()}"}
                          for i, (name, data) in enumerate(repo["assets"].items())]
                assets.append({"name": "notes.txt", "size": 5, "browser_download_url": f"{base}/notes.txt"})
                return self.answer(200, json.dumps({
                    "tag_name": repo["tag"], "html_url": f"https://github.com/{repo['full_name']}/releases/tag/{repo['tag']}",
                    "assets": assets}).encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(self, full_name: str, tag: str | None, assets: dict[str, bytes], description: str = "A custom world",
                **extra) -> None:
        self.repos[full_name.lower()] = {"full_name": full_name, "description": description, "tag": tag,
                                         "assets": assets, "stamp": time.time_ns() // 1000, **extra}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("text", ["someone/fake-world", "https://github.com/someone/fake-world",
                                  "github.com/someone/fake-world.git", " https://www.github.com/someone/fake-world/ ",
                                  "https://github.com/someone/fake-world/releases/tag/v1.0.0"])
def test_repository_addresses_people_paste(text):
    assert parse_repo(text) == "someone/fake-world"


@pytest.mark.parametrize("text", ["", "fake-world", "https://gitlab.com/someone/fake-world", "someone/..", "-x/world",
                                  "some one/world", "someone/wo rld"])
def test_things_that_are_not_repositories(text):
    with pytest.raises(SourceError):
        parse_repo(text)


def test_the_same_file_is_recognised_in_the_next_release():
    assert _stem("fakeworld-v1.0.0.apworld") == _stem("fakeworld-v2.1.0.apworld") == "fakeworld.apworld"
    assert _stem("fake_world_1.2.apworld") == _stem("fake_world_1.3.apworld")
    assert _stem("fakeworld.apworld") != _stem("otherworld.apworld")


# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def github():
    fake = FakeGitHub()
    yield fake
    fake.close()


@pytest.fixture(scope="module")
def site(tmp_path_factory, github):
    """A manager that talks to the fake GitHub and posts to a fake Discord."""
    server = Server(tmp_path_factory.mktemp("sources") / "data", ap=True, extra_env={"GITHUB_API_URL": github.url})
    server.start()
    admin = server.client()
    assert admin.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123"}).status_code == 200
    hook = FakeWebhook()
    admin.patch("/api/settings", json={"discord_webhook_url": hook.url, "site_url": "http://manager.test"})
    for name in ("pia", "olaf"):
        admin.post("/api/users", json={"username": name, "password": "password123", "role": "user"})
    yield {"server": server, "admin": admin, "hook": hook, "pia": server.client("pia", "password123"),
           "olaf": server.client("olaf", "password123")}
    server.stop()
    hook.close()


def apworld(tmp_path, game, module, version):
    return build_apworld(tmp_path / f"{module}-{version}.apworld", game=game, module=module, version=version).read_bytes()


@pytest.mark.ap
def test_a_players_repository_waits_for_the_admin(site, github, tmp_path):
    admin, pia, olaf, hook = site["admin"], site["pia"], site["olaf"], site["hook"]
    github.publish("Someone/Fake-World", "v1.0.0", {"fakeworld-v1.0.0.apworld": apworld(tmp_path, "Fake World", "fakeworld", "1.0.0")})

    # looking a repository up shows what it offers and downloads nothing
    found = pia.get("/api/apworld-sources/lookup", params={"repo": "https://github.com/someone/fake-world"}).json()
    assert found["repo"] == "Someone/Fake-World" and found["tag"] == "v1.0.0" and found["existing"] is None
    assert [a["name"] for a in found["assets"]] == ["fakeworld-v1.0.0.apworld"]  # only .apworld files
    r = pia.post("/api/apworld-sources", json={"repo": "someone/fake-world", "note": "for Friday"})
    assert r.status_code == 200, r.text
    source = r.json()
    assert source["status"] == "pending" and source["repo"] == "Someone/Fake-World" and source["installed"] is False
    assert github.downloads == []  # nothing from it is on the server yet
    pending = hook.wait_for("Custom game waiting for approval: Someone/Fake-World", timeout=20)
    assert "pia wants to add" in pending["description"] and "for Friday" in pending["description"]

    # only the admin and the player who proposed it see it; only the admin can approve it
    assert olaf.get("/api/apworld-sources").json() == []
    assert [s["repo"] for s in pia.get("/api/apworld-sources").json()] == ["Someone/Fake-World"]
    assert pia.post(f"/api/apworld-sources/{source['id']}/approve").status_code == 403
    assert olaf.delete(f"/api/apworld-sources/{source['id']}").status_code == 403
    assert pia.post("/api/apworld-sources", json={"repo": "someone/fake-world"}).status_code == 409
    assert "Fake World" not in {g["name"] for g in admin.get("/api/worlds").json()["games"]}

    r = admin.post(f"/api/apworld-sources/{source['id']}/approve")
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["status"] == "approved" and approved["installed"] and approved["installed_tag"] == "v1.0.0"
    assert approved["game"] == "Fake World" and github.downloads == ["fakeworld-v1.0.0.apworld"]
    assert "Fake World" in {g["name"] for g in admin.get("/api/worlds").json()["games"]}
    entry = next(a for a in admin.get("/api/apworlds").json()["items"] if a["game"] == "Fake World")
    assert entry["status"] == "installed" and entry["uploader"] == "pia" and entry["load_error"] is None
    assert [s["repo"] for s in olaf.get("/api/apworld-sources").json()] == ["Someone/Fake-World"]  # now for everyone

    # a player can take back a proposal that is still waiting, but not an approved one
    github.publish("someone/other-world", "1.0", {"other.apworld": apworld(tmp_path, "Other World", "otherworld", "1.0")})
    other = pia.post("/api/apworld-sources", json={"repo": "someone/other-world"}).json()
    assert pia.delete(f"/api/apworld-sources/{other['id']}").status_code == 200
    assert pia.delete(f"/api/apworld-sources/{source['id']}").status_code == 403


@pytest.mark.ap
def test_new_releases_are_announced_and_installed_by_the_admin(site, github, tmp_path):
    admin, pia, hook = site["admin"], site["pia"], site["hook"]
    github.publish("someone/updating-world", "v1.0.0",
                   {"updating-v1.0.0.apworld": apworld(tmp_path, "Updating World", "updatingworld", "1.0.0")})
    source = admin.post("/api/apworld-sources", json={"repo": "someone/updating-world"}).json()
    assert source["status"] == "approved" and source["installed_tag"] == "v1.0.0"  # an admin's repository installs at once
    assert admin.post(f"/api/apworld-sources/{source['id']}/check").json()["update_available"] is False

    # the next release renames its file with the version; it is still recognised as the same one
    github.publish("someone/updating-world", "v2.0.0",
                   {"updating-v2.0.0.apworld": apworld(tmp_path, "Updating World", "updatingworld", "2.0.0")})
    checked = admin.post(f"/api/apworld-sources/{source['id']}/check").json()
    assert checked["update_available"] is True and checked["latest_tag"] == "v2.0.0" and checked["installed_tag"] == "v1.0.0"
    message = hook.wait_for("New version of Updating World: v2.0.0", timeout=20)
    assert "has v1.0.0" in message["description"]
    admin.post(f"/api/apworld-sources/{source['id']}/check")
    assert hook.titles().count("New version of Updating World: v2.0.0") == 1  # told once per release

    assert pia.post(f"/api/apworld-sources/{source['id']}/update").status_code == 403
    downloads = len(github.downloads)
    updated = admin.post(f"/api/apworld-sources/{source['id']}/update").json()
    assert updated["installed_tag"] == "v2.0.0" and updated["update_available"] is False
    assert github.downloads[downloads:] == ["updating-v2.0.0.apworld"]
    versions = [a["version"] for a in admin.get("/api/apworlds").json()["items"] if a["game"] == "Updating World"]
    assert versions == ["2.0.0"]  # replaced, not added next to the old one


@pytest.mark.ap
def test_unusable_repositories_and_releases_are_refused(site, github, tmp_path):
    admin, pia, olaf = site["admin"], site["pia"], site["olaf"]
    r = pia.get("/api/apworld-sources/lookup", params={"repo": "nobody/nothing"})
    assert r.status_code == 404 and "no public GitHub repository" in r.json()["detail"]
    github.publish("someone/unreleased", None, {})
    assert "has no published release" in pia.get("/api/apworld-sources/lookup", params={"repo": "someone/unreleased"}).json()["detail"]
    github.publish("someone/no-apworld", "v1", {})
    r = pia.post("/api/apworld-sources", json={"repo": "someone/no-apworld"})
    assert r.status_code == 404 and "has no .apworld file" in r.json()["detail"]
    assert pia.post("/api/apworld-sources", json={"repo": "gitlab.com/someone/x"}).status_code == 400

    # several worlds in one release: say which
    github.publish("someone/bundle", "v1", {"one.apworld": apworld(tmp_path, "Bundle One", "bundleone", "1.0"),
                                            "two.apworld": apworld(tmp_path, "Bundle Two", "bundletwo", "1.0")})
    r = admin.post("/api/apworld-sources", json={"repo": "someone/bundle"})
    assert r.status_code == 409 and "several .apworld files" in r.json()["detail"]
    r = admin.post("/api/apworld-sources", json={"repo": "someone/bundle", "asset": "two.apworld"})
    assert r.status_code == 200 and r.json()["game"] == "Bundle Two"

    # a broken file is refused with the reason, and the repository is not kept
    github.publish("someone/broken", "v1", {"broken.apworld": b"this is not a zip"})
    r = admin.post("/api/apworld-sources", json={"repo": "someone/broken"})
    assert r.status_code == 400 and "not a valid zip" in r.json()["detail"]
    assert "someone/broken" not in [s["repo"] for s in admin.get("/api/apworld-sources").json()]
    # approving a broken one keeps it waiting, with the reason
    pending = pia.post("/api/apworld-sources", json={"repo": "someone/broken"}).json()
    assert admin.post(f"/api/apworld-sources/{pending['id']}/approve").status_code == 400
    again = next(s for s in admin.get("/api/apworld-sources").json() if s["id"] == pending["id"])
    assert again["status"] == "pending" and "not a valid zip" in again["error"]

    # GitHub's answer decides where the file is downloaded from, so only GitHub addresses are followed
    github.publish("someone/elsewhere", "v1", {"x.apworld": apworld(tmp_path, "Elsewhere", "elsewhere", "1.0")},
                   url_override="http://downloads.example.test/x.apworld")
    r = admin.post("/api/apworld-sources", json={"repo": "someone/elsewhere"})
    assert r.status_code == 502 and "outside of GitHub" in r.json()["detail"]

    # without permission to submit games, players cannot propose repositories either
    admin.patch("/api/settings", json={"users_can_upload_apworlds": False})
    try:
        assert olaf.get("/api/apworld-sources/lookup", params={"repo": "someone/bundle"}).status_code == 403
        assert olaf.post("/api/apworld-sources", json={"repo": "someone/bundle"}).status_code == 403
    finally:
        admin.patch("/api/settings", json={"users_can_upload_apworlds": True})


@pytest.mark.ap
def test_approving_installs_exactly_the_release_the_admin_saw(site, github, tmp_path):
    admin, pia = site["admin"], site["pia"]
    github.publish("someone/swapped-world", "v1", {"swapped.apworld": apworld(tmp_path, "Swapped World", "swappedworld", "1.0")})
    source = pia.post("/api/apworld-sources", json={"repo": "someone/swapped-world"}).json()
    # after the admin had a look, the player puts another file into the same release
    github.publish("someone/swapped-world", "v1", {"swapped.apworld": apworld(tmp_path, "Swapped World", "swappedworld", "6.6.6")})
    downloads = len(github.downloads)
    r = admin.post(f"/api/apworld-sources/{source['id']}/approve")
    assert r.status_code == 409 and "changed" in r.json()["detail"]
    assert github.downloads[downloads:] == []  # nothing was downloaded, nothing installed
    listed = next(s for s in admin.get("/api/apworld-sources").json() if s["id"] == source["id"])
    assert listed["status"] == "pending" and not listed["installed"]
    # the admin has been told and approves again: now that file is what was seen
    r = admin.post(f"/api/apworld-sources/{source['id']}/approve")
    assert r.status_code == 200, r.text
    assert [a["version"] for a in admin.get("/api/apworlds").json()["items"] if a["game"] == "Swapped World"] == ["6.6.6"]


@pytest.mark.ap
def test_a_download_that_does_not_match_its_digest_is_not_installed(site, github, tmp_path):
    admin = site["admin"]
    github.publish("someone/tampered-world", "v1", {"tampered.apworld": apworld(tmp_path, "Tampered", "tamperedworld", "1.0")},
                   digest="sha256:" + "0" * 64)
    r = admin.post("/api/apworld-sources", json={"repo": "someone/tampered-world"})
    assert r.status_code == 502 and "not the one GitHub lists" in r.json()["detail"]
    assert "Tampered" not in {a["game"] for a in admin.get("/api/apworlds").json()["items"]}


@pytest.mark.ap
def test_players_cannot_queue_up_endless_repositories(site, github, tmp_path):
    olaf = site["olaf"]
    data = apworld(tmp_path, "Queued", "queuedworld", "1.0")
    for i in range(6):
        github.publish(f"queue/world-{i}", "v1", {"queued.apworld": data})
    statuses = [olaf.post("/api/apworld-sources", json={"repo": f"queue/world-{i}"}).status_code for i in range(6)]
    assert statuses == [200] * 5 + [409]
    for source in olaf.get("/api/apworld-sources").json():
        if source["repo"].startswith("queue/"):
            olaf.delete(f"/api/apworld-sources/{source['id']}")
