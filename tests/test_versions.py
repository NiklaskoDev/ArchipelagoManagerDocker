"""Further Archipelago versions: installing one, generating and hosting a game on it, and removing it again.

The "release" is made from the Archipelago the tests run against, with another version number, and served from a
file: the installation still makes its own Python environment and installs Archipelago's requirements with pip.
"""
import io
import re
import tarfile
import zipfile
from pathlib import Path

import pytest

from conftest import AP_DIR, Server
from helpers import make_game, server_pid, wait_for, wait_state

pytestmark = pytest.mark.ap

OTHER = "0.6.1"


def source_archive(folder: Path, version: str) -> Path:
    """A source archive of the tests' Archipelago that says it is `version`, laid out like GitHub's. The worlds' own
    requirements are left out (those worlds then do not load), which saves minutes of installing."""
    target = folder / f"{version}.tar.gz"
    top = f"Archipelago-{version}"
    with tarfile.open(target, "w:gz") as tar:
        for path in sorted(Path(AP_DIR).rglob("*")):
            relative = path.relative_to(AP_DIR)
            parts = relative.parts
            if parts[0] in (".git", "custom_worlds", "output", "logs", "Players") or "__pycache__" in parts \
                    or parts[0].startswith("host.yaml") or parts[-1].endswith((".so", ".pyd", ".c")) \
                    or (parts[0] == "worlds" and parts[-1] == "requirements.txt") or not path.is_file():
                continue
            if relative == Path("Utils.py"):
                text = re.sub(r'^__version__ = ".*"$', f'__version__ = "{version}"', path.read_text(), flags=re.M)
                data = text.encode()
                info = tarfile.TarInfo(f"{top}/Utils.py")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.add(path, f"{top}/{relative.as_posix()}", recursive=False)
    return target


@pytest.fixture(scope="module")
def versioned(tmp_path_factory):
    releases = tmp_path_factory.mktemp("releases")
    source_archive(releases, OTHER)
    server = Server(tmp_path_factory.mktemp("versions") / "data", ap=True,
                    extra_env={"AP_SOURCE_URL": f"file://{releases}/{{version}}.tar.gz"})
    server.start()
    admin = server.client()
    assert admin.post("/api/auth/setup", json={"username": "admin", "password": "adminpass1",
                                               "code": server.setup_code()}).status_code == 200
    yield server, server.client("admin", "adminpass1")
    server.stop()


def listing(admin):
    r = admin.get("/api/ap-versions")
    assert r.status_code == 200, r.text
    return r.json()


def test_a_game_runs_on_a_version_of_its_own(versioned):
    server, admin = versioned
    default = listing(admin)["default"]
    assert default and listing(admin)["versions"] == []

    assert admin.post("/api/ap-versions", json={"version": "six"}).status_code == 400
    assert admin.post("/api/ap-versions", json={"version": default}).status_code == 400
    r = admin.post("/api/ap-versions", json={"version": f"v{OTHER}"})
    assert r.status_code == 200, r.text
    assert [(v["version"], v["status"]) for v in r.json()["versions"]] == [(OTHER, "installing")]
    assert admin.post("/api/ap-versions", json={"version": "0.6.0"}).status_code == 409  # one at a time
    done = wait_for(lambda: (v := listing(admin)["versions"][0])["status"] != "installing" and v, timeout=900,
                    message="the installation")
    assert done["status"] == "installed", (done, admin.get(f"/api/ap-versions/{OTHER}/log").text[-3000:])
    assert done["world_count"] and done["size"] > 0 and done["games"] == []
    assert "done:" in admin.get(f"/api/ap-versions/{OTHER}/log").text
    assert admin.post("/api/ap-versions", json={"version": OTHER}).status_code == 409  # already there
    folder = server.data_dir / "ap-versions" / OTHER

    # a game generated with it runs on it
    gid, job = make_game(admin, names=("Old", "Timer"), ap_version=OTHER)
    assert job["options"]["ap_version"] == OTHER
    game = admin.get(f"/api/games/{gid}").json()
    assert (game["ap_version"], game["ap_version_own"], game["ap_version_missing"]) == (OTHER, OTHER, False)
    assert admin.post(f"/api/games/{gid}/start").status_code == 200
    wait_state(admin, gid, "running")
    pid = server_pid(gid, server)
    cmdline = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
    assert cmdline[0] == str(folder / "venv" / "bin" / "python").encode()
    # the launcher that hands MultiServer.py its passwords, and that version's MultiServer.py
    assert cmdline[1].endswith(b"/ap_server.py")
    assert cmdline[2] == str(folder / "archipelago" / "MultiServer.py").encode()
    slot = game["slots"][0]["slot"]
    assert admin.get(f"/api/games/{gid}/slots/{slot}/tracker").status_code == 200

    # it cannot go while a game runs on it
    r = admin.delete(f"/api/ap-versions/{OTHER}")
    assert r.status_code == 409 and game["name"] in r.json()["detail"]
    assert listing(admin)["versions"][0]["games"] == [game["name"]]

    # back to the server's own version: after a restart of the server
    r = admin.patch(f"/api/games/{gid}", json={"ap_version": ""})
    assert r.status_code == 200 and r.json()["restart_required"] is True
    assert (r.json()["ap_version"], r.json()["ap_version_own"]) == (default, None)
    assert admin.post(f"/api/games/{gid}/restart").status_code == 200
    wait_state(admin, gid, "running")
    assert open(f"/proc/{server_pid(gid, server)}/cmdline", "rb").read().split(b"\0")[0] != cmdline[0]
    admin.post(f"/api/games/{gid}/stop")
    assert admin.patch(f"/api/games/{gid}", json={"ap_version": "0.1.0"}).status_code == 409

    # an upload can run on it from the start
    multidata = admin.get(f"/api/games/{gid}/multidata").content
    r = admin.post("/api/games", files={"file": ("again.archipelago", multidata)}, data={"ap_version": OTHER})
    assert r.status_code == 200, r.text
    assert r.json()["ap_version_own"] == OTHER
    assert admin.post("/api/games", files={"file": ("x.archipelago", multidata)},
                      data={"ap_version": "0.1.0"}).status_code == 409
    admin.delete(f"/api/games/{r.json()['id']}")

    # it is not in the backup of everything
    r = admin.post("/api/system/backup")
    archive = zipfile.ZipFile(io.BytesIO(admin.get(r.json()["url"]).content))
    assert not [n for n in archive.namelist() if n.startswith("ap-versions/")]

    # and it goes, files and all
    r = admin.delete(f"/api/ap-versions/{OTHER}")
    assert r.status_code == 200 and r.json()["versions"] == []
    assert not folder.exists()
    admin.delete(f"/api/games/{gid}")


def test_a_release_that_cannot_be_downloaded_fails(versioned):
    _server, admin = versioned
    assert admin.post("/api/ap-versions", json={"version": "0.6.0"}).status_code == 200
    failed = wait_for(lambda: (v := next(x for x in listing(admin)["versions"] if x["version"] == "0.6.0"))["status"]
                      != "installing" and v, timeout=60, message="the failed installation")
    assert failed["status"] == "failed" and "download" in failed["error"]
    # players see only what they can use
    admin.post("/api/users", json={"username": "player", "password": "password123", "role": "user"})
    player = _server.client("player", "password123")
    assert player.get("/api/ap-versions").json()["versions"] == []
    assert player.post("/api/ap-versions", json={"version": "0.6.0"}).status_code == 403
    assert admin.delete("/api/ap-versions/0.6.0").status_code == 200
