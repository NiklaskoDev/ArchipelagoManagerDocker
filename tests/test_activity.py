"""The activity feed of a game, for its players: chat, items, hints, joins and goals, read from the server's log."""
import json
import time

import pytest

from helpers import APClient, make_game, wait_for, wait_state

pytestmark = pytest.mark.ap

CHECKS_FINDER_BASE = 81000


def feed(client, gid, after=0):
    r = client.get(f"/api/games/{gid}/activity", params={"after": after})
    assert r.status_code == 200, r.text
    return r.json()


def test_players_follow_what_happens_in_their_game(admin, make_user):
    ann_name, ann = make_user("ann")
    _bob_name, bob = make_user("bob")
    gid, _job = make_game(admin, names=("Feedy", "Quill"), owners={"Feedy": ann})
    assert feed(ann, gid) == {"entries": [], "last": 0}
    assert bob.get(f"/api/games/{gid}/activity").status_code == 404  # like the game itself

    # a player's start is in the audit log too
    assert ann.post(f"/api/games/{gid}/start").status_code == 200
    port = wait_state(admin, gid, "running")["port"]
    started = admin.get("/api/audit", params={"action": "game.start"}).json()["entries"][0]
    assert (started["username"], started["target"]) == (ann_name, {"kind": "game", "id": gid, "name":
                                                                   admin.get(f"/api/games/{gid}").json()["name"]})

    with APClient(port, "Feedy", "ChecksFinder") as client:
        assert client.connected
        location = client.missing[0]
        tile = f"Tile {location - CHECKS_FINDER_BASE + 1}"
        client.ws.send(json.dumps([{"cmd": "Say", "text": "hello, everyone"}]))
        client.ws.send(json.dumps([{"cmd": "Say", "text": "!admin login secret"}]))
        assert admin.post(f"/api/games/{gid}/command",
                          json={"command": f"/hint_location Feedy Tile {client.missing[1] - CHECKS_FINDER_BASE + 1}"}
                          ).status_code == 200
        client.ws.send(json.dumps([{"cmd": "LocationChecks", "locations": [location]}]))
        client.ws.send(json.dumps([{"cmd": "StatusUpdate", "status": 30}]))
        time.sleep(1.5)
    def with_goal():
        found = feed(ann, gid)["entries"]
        return found if any(e["kind"] == "goal" for e in found) else None
    entries = wait_for(with_goal, timeout=20, message="the goal in the feed")
    kinds = [e["kind"] for e in entries]
    assert {"join", "chat", "hint", "item", "goal", "part"} <= set(kinds), kinds
    join = next(e for e in entries if e["kind"] == "join")
    assert (join["player"], join["game"]) == ("Feedy", "ChecksFinder")
    assert [e["text"] for e in entries if e["kind"] == "chat"] == ["hello, everyone"]  # never remote administration
    hint = next(e for e in entries if e["kind"] == "hint")
    assert hint["finder"] == "Feedy" and hint["receiver"] in ("Feedy", "Quill") and hint["status"]
    item = next(e for e in entries if e["kind"] == "item" and e["location"] == tile)
    assert item["finder"] == "Feedy" and item["receiver"] in ("Feedy", "Quill")
    assert [e["seq"] for e in entries] == sorted(e["seq"] for e in entries) and all(e["at"] for e in entries)
    # the server's own lines and the console stay out
    assert not any("/hint_location" in json.dumps(e) for e in entries)

    # only what is new, after the last one seen
    last = feed(ann, gid)["last"]
    assert feed(ann, gid, after=last)["entries"] == []
    admin.post(f"/api/games/{gid}/stop")
    wait_state(admin, gid, "stopped")
    newer = feed(ann, gid, after=last)["entries"]
    assert all(e["seq"] > last for e in newer)
    # the feed outlives the server
    assert len(feed(ann, gid)["entries"]) >= len(entries)

    # the log names what really changed, though the settings form sends every field
    game = admin.get(f"/api/games/{gid}").json()
    admin.patch(f"/api/games/{gid}", json={"name": game["name"], "notes": "for the log", "claimable": game["claimable"],
                                           "options": game["options"]})
    assert admin.get("/api/audit", params={"action": "game.settings"}).json()["entries"][0]["details"] == {
        "changed": ["notes"]}

    # a console command with a password is in the audit log, masked
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    admin.post(f"/api/games/{gid}/command", json={"command": "/option password hunter2"})
    command = admin.get("/api/audit", params={"action": "game.command"}).json()["entries"][0]
    assert command["details"]["command"] == "/option password ********"
    admin.post(f"/api/games/{gid}/stop")
    admin.delete(f"/api/games/{gid}")
