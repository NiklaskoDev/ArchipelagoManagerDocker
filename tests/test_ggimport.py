"""Moving a game here from archipelago.gg: its progress comes from the room's tracker API (ggimport.py).

A stand-in for archipelago.gg answers with how a game played here stands, written the way archipelago.gg's API writes
it (WebHostLib/api: room_status, tracker, static_tracker). The same multiworld added again then has to end up where the
original is."""
import json
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from helpers import APClient, make_game, save_now, wait_state

pytestmark = pytest.mark.ap

ROOM = "AbCdEfGhIjKlMnOpQrStUv"
TRACKER = "TrAcKeRtRaCkErTrAcKeR0"

# How archipelago.gg's API describes a game, from the multidata and the save (WebHostLib.api.tracker and .room). Runs
# in Archipelago's own environment, which can read both.
TRACKER_SCRIPT = r"""
import json, sys, zlib, datetime
sys.path.insert(0, sys.argv[1])
import Utils
md = Utils.restricted_loads(zlib.decompress(open(sys.argv[2], "rb").read()[1:]))
save = Utils.restricted_loads(zlib.decompress(open(sys.argv[3], "rb").read()))
players = [s for s, info in sorted(md["slot_info"].items()) if int(info.type) == 1]
def http_date(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
def timers(name):
    found = {tuple(k)[1]: v for k, v in save.get(name, ())}
    return [{"team": 0, "player": p, "time": http_date(found[p]) if p in found else None} for p in players]
live = {
    "aliases": [{"team": 0, "player": p, "alias": save.get("name_aliases", {}).get((0, p))} for p in players],
    "player_items_received": [{"team": 0, "player": p, "items": [list(i) for i in save["received_items"].get((0, p, True), [])]} for p in players],
    "player_checks_done": [{"team": 0, "player": p, "locations": sorted(save["location_checks"].get((0, p), ()))} for p in players],
    "total_checks_done": [{"team": 0, "checks_done": sum(len(v) for v in save["location_checks"].values())}],
    "hints": [{"team": 0, "player": p, "hints": sorted([list(h[:7]) + [int(h[7])] for h in save["hints"].get((0, p), ())])} for p in players],
    "activity_timers": timers("client_activity_timers"),
    "connection_timers": timers("client_connection_timers"),
    "player_status": [{"team": 0, "player": p, "status": int(save["client_game_state"].get((0, p), 0))} for p in players],
}
static = {"groups": [], "datapackage": {},
          "player_locations_total": [{"team": 0, "player": p, "total_locations": len(md["locations"][p])} for p in players],
          "player_game": [{"team": 0, "player": p, "game": md["slot_info"][p].game} for p in players]}
status = {"tracker": sys.argv[4], "players": [[md["slot_info"][p].name, md["slot_info"][p].game] for p in players],
          "last_port": 38281, "last_activity": http_date(0), "timeout": 7200,
          "downloads": [{"slot": p, "download": f"/dl_patch/{sys.argv[5]}/{p}"} for p in players]}
print(json.dumps({"status": status, "live": live, "static": static}))
"""


def as_archipelago_gg(server, game_id):
    from conftest import AP_DIR, AP_PYTHON
    game_dir = server.data_dir / "games" / str(game_id)
    out = subprocess.run([AP_PYTHON, "-c", TRACKER_SCRIPT, AP_DIR, str(game_dir / "multidata.archipelago"),
                          str(game_dir / "multidata.apsave"), TRACKER, ROOM], check=True, capture_output=True, text=True,
                         cwd=AP_DIR, env={"SKIP_REQUIREMENTS_UPDATE": "true", "PATH": "/usr/bin:/bin",
                                          "XDG_DATA_HOME": tempfile.mkdtemp()})
    return json.loads(out.stdout.strip().splitlines()[-1])


class FakeArchipelagoGG:
    """Answers like archipelago.gg for one room. Slot 1 has a patch file to download, the others do not."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.requests: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.requests.append(self.path)
                routes = {f"/api/room_status/{ROOM}": "status", f"/api/tracker/{TRACKER}": "live",
                          f"/api/static_tracker/{TRACKER}": "static"}
                if self.path in routes:
                    self.answer(200, json.dumps(fake.data[routes[self.path]]).encode(), "application/json")
                elif self.path == f"/dl_patch/{ROOM}/1":
                    self.answer(200, b"patch-from-the-room", "application/octet-stream",
                                'attachment; filename=P1_Moved_room.apchecks')
                elif self.path.startswith(f"/dl_patch/{ROOM}/"):
                    self.answer(200, b"Patch not found", "text/html")
                else:
                    self.answer(404, b"Not Found", "text/html")

            def answer(self, status, body, kind, disposition=None):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                if disposition:
                    self.send_header("Content-Disposition", disposition)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(scope="module")
def moving(tmp_path_factory):
    from conftest import Server
    fake = FakeArchipelagoGG()
    server = Server(tmp_path_factory.mktemp("moving") / "data", ap=True, extra_env={"ARCHIPELAGO_GG_URL": fake.url})
    server.start()
    assert server.client().post("/api/auth/setup", json={"username": "admin", "password": "adminpass1"}
                                ).status_code == 200
    server.fake = fake
    yield server
    server.stop()
    fake.close()


def tracker(client, gid, slot):
    data = client.get(f"/api/games/{gid}/slots/{slot}/tracker").json()
    return {"items": sorted((i["item"], i["from"] or "", i["location"] or "") for i in data["items"]),
            "checked": sorted(loc["location"] for loc in data["locations"] if loc["checked"]),
            "hints": sorted((h["item"], h["location"], h["found"]) for h in data["hints"]), "goal": data["goal"]}


def test_a_game_moves_here_from_archipelago_gg_with_its_progress(moving):
    admin = moving.client("admin", "adminpass1")
    assert admin.post("/api/users", json={"username": "player", "password": "password123"}).status_code == 200
    player = moving.client("player", "password123")
    # the game as it was played on archipelago.gg
    there, _job = make_game(admin, names=("Moved", "Mover"))
    options = admin.get(f"/api/games/{there}").json()["options"]  # a goal that leaves the others' locations alone
    r = admin.patch(f"/api/games/{there}", json={"options": {**options, "release_mode": "disabled", "collect_mode": "disabled"}})
    assert r.status_code == 200, r.text
    admin.post(f"/api/games/{there}/start")
    port = wait_state(admin, there, "running")["port"]
    mover_slot = next(s["slot"] for s in admin.get(f"/api/games/{there}").json()["slots"] if s["name"] == "Mover")
    with APClient(port, "Moved", "ChecksFinder") as moved, APClient(port, "Mover", "APQuest") as mover:
        moved.check(6)
        mover.check(2)
        save_now(admin, there)
        unchecked = [loc["location"] for loc in
                     admin.get(f"/api/games/{there}/slots/{mover_slot}/tracker").json()["locations"] if not loc["checked"]]
        for location in unchecked[:3]:  # hints the server keeps: for locations nobody checked yet
            admin.post(f"/api/games/{there}/command", json={"command": f"/hint_location Mover {location}"})
        moved.ws.send(json.dumps([{"cmd": "StatusUpdate", "status": 30}]))
        time.sleep(1)
    save_now(admin, there)
    admin.post(f"/api/games/{there}/stop")
    wait_state(admin, there, "stopped")
    moving.fake.data = as_archipelago_gg(moving, there)

    # the same multiworld, added here from its .archipelago file alone
    multidata = admin.get(f"/api/games/{there}/multidata").content
    r = admin.post("/api/games", files={"file": ("AP_moved.archipelago", multidata)}, data={"name": "Moved here"})
    assert r.status_code == 200, r.text
    here = r.json()["id"]
    slots = {s["name"]: s["slot"] for s in r.json()["slots"]}

    # only whoever runs the game, with the address of a room on archipelago.gg, while the server is stopped
    assert player.post(f"/api/games/{here}/import-gg", json={"room": ROOM}).status_code == 404
    assert admin.post(f"/api/games/{here}/import-gg", json={"room": "https://evil.example/room/" + ROOM}).status_code == 400
    assert admin.post(f"/api/games/{here}/import-gg", json={"room": "not a room"}).status_code == 400
    assert admin.post(f"/api/games/{here}/import-gg", json={"room": "ZZCdEfGhIjKlMnOpQrStUv"}).status_code == 404
    admin.post(f"/api/games/{here}/start")
    wait_state(admin, here, "running")
    assert admin.post(f"/api/games/{here}/import-gg", json={"room": ROOM}).status_code == 409
    admin.post(f"/api/games/{here}/stop")
    wait_state(admin, here, "stopped")

    room_address = f"{moving.fake.url}/room/{ROOM}".replace("http://", "")
    r = admin.post(f"/api/games/{here}/import-gg", json={"room": room_address})
    assert r.status_code == 200, r.text
    result = r.json()["imported"]
    original = {s["name"]: s for s in admin.get(f"/api/games/{there}").json()["slots"]}
    assert result["room"] == ROOM and result["patches"] == 1 and result["hints"] == 3
    assert result["checked"] == sum(s["checked"] for s in original.values())
    for name, slot in slots.items():
        assert tracker(admin, here, slot) == tracker(admin, there, slot), name
    seen = {s["name"]: s for s in r.json()["slots"]}
    assert {n: s["checked"] for n, s in seen.items()} == {n: s["checked"] for n, s in original.items()}
    assert seen["Moved"]["goal_at"] and not seen["Mover"]["goal_at"]
    patch = next(f for f in r.json()["slots"][0]["files"])
    assert patch["filename"] == "P1_Moved_room.apchecks"
    assert admin.get(f"/api/games/{here}/files/{patch['id']}").content == b"patch-from-the-room"
    assert not seen["Mover"]["files"]  # the room had none to give
    # what was here before is a backup, the history starts where the room stood, and the log has it
    assert {b["reason"] for b in admin.get(f"/api/games/{here}/backups").json()["backups"]} <= {"before-import"}
    history = admin.get(f"/api/games/{here}/history").json()
    assert {s["name"]: s["points"][-1][1] for s in history["slots"]} == {n: s["checked"] for n, s in original.items()}
    assert history["play_started_at"] and history["play_started_at"] < time.time()
    assert "game.import" in [e["action"] for e in admin.get("/api/audit?action=game").json()["entries"]]

    # the server takes it as its own save: the players continue where they were
    admin.post(f"/api/games/{here}/start")
    port = wait_state(admin, here, "running")["port"]
    before = original["Mover"]["checked"]
    with APClient(port, "Mover", "APQuest") as mover:
        assert mover.connected and len(mover.response["checked_locations"]) == before
        mover.check(1)
    save_now(admin, here)
    assert len(tracker(admin, here, slots["Mover"])["checked"]) == before + 1
    admin.post(f"/api/games/{here}/stop")
    wait_state(admin, here, "stopped")
    # a second import keeps the progress made here as a backup
    assert admin.post(f"/api/games/{here}/import-gg", json={"room": ROOM}).status_code == 200
    assert "before-import" in [b["reason"] for b in admin.get(f"/api/games/{here}/backups").json()["backups"]]
    assert len(tracker(admin, here, slots["Mover"])["checked"]) == before

    # another multiworld is refused, and nothing changes: other players, or the same players in another seed
    other, _job = make_game(admin, names=("Moved", "Mover"))
    r = admin.post(f"/api/games/{other}/import-gg", json={"room": ROOM})
    assert r.status_code == 409 and r.json()["detail"].endswith("its items lie elsewhere"), r.text
    assert admin.get(f"/api/games/{other}").json()["progress"]["checked"] == 0
    elsewhere, _job = make_game(admin, names=("Stranger", "Danger"))
    r = admin.post(f"/api/games/{elsewhere}/import-gg", json={"room": ROOM})
    assert r.status_code == 409 and "another multiworld" in r.json()["detail"]
    assert admin.get(f"/api/games/{elsewhere}").json()["progress"]["checked"] == 0
