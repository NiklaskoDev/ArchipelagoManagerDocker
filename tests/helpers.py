"""Builders and clients shared by the integration tests."""
from __future__ import annotations

import io
import json
import subprocess
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from websockets.sync.client import connect

from conftest import AP_DIR


def player_yaml(name: str, game: str = "ChecksFinder", extra: str = "") -> str:
    """A minimal valid player file: real ones come from templates and always have the game's options section."""
    return f"name: {name}\ngame: {game}\n{game}: {{}}\n{extra}"


def upload_yaml(client, name: str, content: str):
    r = client.post("/api/configs", files=[("files", (f"{name}.yaml", content))])
    return r


def wait_for(predicate, timeout: float = 60, interval: float = 0.25, message: str = "condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}")


def run_job(admin, config_ids, **options):
    r = admin.post("/api/jobs", json={"config_ids": config_ids, "host_after": False, **options})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    job = wait_for(lambda: (j := admin.get(f"/api/jobs/{job_id}").json())["status"] in ("succeeded", "failed", "cancelled") and j,
                   timeout=180, message=f"job {job_id}")
    return job


def wait_state(client, game_id: int, state: str, timeout: float = 60):
    return wait_for(lambda: (g := client.get(f"/api/games/{game_id}").json())["state"] == state and g,
                    timeout=timeout, message=f"game {game_id} to be {state}")


def make_game(admin, *, names=("Alice", "Bob"), owners=None, **options):
    """Generates a small multiworld through the manager. Returns (game_id, job)."""
    config_ids = []
    for i, name in enumerate(names):
        client = (owners or {}).get(name, admin)
        r = upload_yaml(client, f"{name}-{time.time_ns()}", player_yaml(name, "ChecksFinder" if i % 2 == 0 else "APQuest"))
        assert r.status_code == 200, r.text
        config_ids.append(r.json()["created"][0]["id"])
    job = run_job(admin, config_ids, **options)
    assert job["status"] == "succeeded", job
    return job["game_id"], job


def pregenerated_zip(admin, game_id: int, *, with_zip_slip: bool = True) -> bytes:
    """Builds an upload like one made by the generator elsewhere: multidata, patch files per slot, yamls, spoiler.

    Slot 1 gets one patch file, slot 2 gets two, and the first slot's yaml is bundled.
    """
    multidata = admin.get(f"/api/games/{game_id}/multidata").content
    game = admin.get(f"/api/games/{game_id}").json()
    seed = game["seed_name"]
    first, second = game["slots"][0], game["slots"][1]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"AP_{seed}.archipelago", multidata)
        zf.writestr(f"AP_{seed}_P1_{first['name']}.apchecks", f"patch-for-{first['name']}".encode() * 20)
        zf.writestr(f"AP_{seed}_P2_{second['name']}.apquest", f"patch-for-{second['name']}".encode() * 20)
        zf.writestr(f"AP_{seed}_P2_{second['name']}.extra", b"second-file")
        zf.writestr(f"AP_{seed}_Spoiler.txt", b"secret spoiler")
        zf.writestr(f"Players/{first['name'].lower()}-original.yaml", player_yaml(first["name"], first["game"], "# original upload\n"))
        zf.writestr("README.txt", b"hello")
        if with_zip_slip:
            zf.writestr("../../escaped.txt", b"zip slip attempt")
    return buf.getvalue()


def build_apworld(path: Path, *, game="Custom Checks", module="customchecks", version="1.2.0", minimum="0.6.0",
                  slow_seconds: int = 0) -> Path:
    """Repackages the built-in ChecksFinder world under another name: a real, loadable custom world."""
    src = Path(AP_DIR) / "worlds" / "checksfinder"
    manifest = {"version": 7, "compatible_version": 7, "game": game, "world_version": version, "minimum_ap_version": minimum}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in src.rglob("*"):
            if f.is_dir() or "__pycache__" in f.parts:
                continue
            data = f.read_bytes()
            if f.suffix == ".py":
                data = data.replace(b'"ChecksFinder"', f'"{game}"'.encode())
            if slow_seconds and f.name == "__init__.py":  # make generation take a while, for cancellation tests
                data = data.replace(b"    def create_regions(self):",
                                    f"    def create_regions(self):\n        import time; time.sleep({slow_seconds})".encode())
            zf.writestr(f"{module}/{f.relative_to(src)}", data)
        zf.writestr(f"{module}/archipelago.json", json.dumps(manifest))
    return path


class APClient:
    """The least an Archipelago client does: connect to a slot and report location checks."""

    def __init__(self, port: int, name: str, game: str, password: str | None = None, host: str = "127.0.0.1"):
        self.ws = connect(f"ws://{host}:{port}", open_timeout=15)
        self.room = json.loads(self.ws.recv(timeout=15))[0]
        self.ws.send(json.dumps([{
            "cmd": "Connect", "password": password, "name": name, "game": game, "uuid": "test-" + name,
            "version": {"major": 0, "minor": 6, "build": 8, "class": "Version"}, "tags": ["AP"],
            "items_handling": 7, "slot_data": False}]))
        self.response = json.loads(self.ws.recv(timeout=15))[0]

    @property
    def connected(self) -> bool:
        return self.response["cmd"] == "Connected"

    @property
    def missing(self) -> list[int]:
        return self.response["missing_locations"]

    def check(self, count: int) -> None:
        self.ws.send(json.dumps([{"cmd": "LocationChecks", "locations": self.missing[:count]}]))
        time.sleep(0.7)

    def close(self) -> None:
        self.ws.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def save_now(admin, game_id: int) -> None:
    """Asks the server to write its save file immediately instead of waiting for the periodic autosave."""
    assert admin.post(f"/api/games/{game_id}/command", json={"command": "/save"}).status_code == 200
    time.sleep(1.5)


def multidata_requiring_server(multidata: bytes, version: tuple[int, int, int]) -> bytes:
    """Rewrites a real multidata so it claims to need a server of at least `version` (uses Archipelago's own loader)."""
    import subprocess
    import tempfile
    from conftest import AP_PYTHON
    script = (
        "import sys, zlib\n"
        f"sys.path.insert(0, {AP_DIR!r})\n"
        "import Utils\n"
        "data = open(sys.argv[1], 'rb').read()\n"
        "md = Utils.restricted_loads(zlib.decompress(data[1:]))\n"
        f"md['minimum_versions']['server'] = {tuple(version)!r}\n"
        "open(sys.argv[2], 'wb').write(bytes([data[0]]) + zlib.compress(Utils.restricted_dumps(md), 9))\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        source, target = f"{tmp}/in.archipelago", f"{tmp}/out.archipelago"
        open(source, "wb").write(multidata)
        env = {"SKIP_REQUIREMENTS_UPDATE": "true", "XDG_DATA_HOME": f"{tmp}/xdg", "PATH": "/usr/bin:/bin"}
        subprocess.run([AP_PYTHON, "-c", script, source, target], check=True, env=env, cwd=AP_DIR, capture_output=True)
        return open(target, "rb").read()


def server_pid(game_id: int, ap_server) -> int | None:
    """pid of the MultiServer process for a game (matched by its multidata path, unique per game)."""
    marker = str(ap_server.data_dir / "games" / str(game_id) / "multidata.archipelago")
    out = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
    for pid in out:
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        except OSError:
            continue
        if any(a.endswith(b"MultiServer.py") for a in cmd):
            return int(pid)
    return None


class FakeWebhook:
    """Stands in for a Discord webhook and records what is posted to it.

    `answers` holds (status, json body) pairs to answer with first; after that every post gets Discord's 204.
    """

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.answers: list[tuple[int, dict]] = []
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                with fake.lock:
                    status, answer = fake.answers.pop(0) if fake.answers else (204, None)
                    fake.posts.append({"path": self.path, "user_agent": self.headers.get("User-Agent"), "body": body,
                                       "status": status})
                data = json.dumps(answer).encode() if answer is not None else b""
                self.send_response(status)
                if data:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/webhooks/1/secret-token"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def embeds(self) -> list[dict]:
        """The messages that were accepted (answered 204), oldest first."""
        with self.lock:
            return [p["body"]["embeds"][0] for p in self.posts if p["status"] < 300]

    def titles(self) -> list[str]:
        return [e["title"] for e in self.embeds]

    def wait_for(self, title_part: str, timeout: float = 60) -> dict:
        return wait_for(lambda: next((e for e in self.embeds if title_part in e["title"]), None), timeout=timeout,
                        message=f"a notification titled like {title_part!r} (got {self.titles()})")

    def clear(self) -> None:
        with self.lock:
            self.posts.clear()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()  # refuse connections from now on instead of leaving them hanging
