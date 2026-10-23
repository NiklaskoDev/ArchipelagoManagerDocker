"""The tracker: a slot's received items, its locations and its hints, read from the server's save."""
import json
import time

import pytest

from helpers import APClient, make_game, save_now, wait_for, wait_state

pytestmark = pytest.mark.ap

CHECKS_FINDER_BASE = 81000  # ChecksFinder's locations are "Tile 1" (81000) to "Tile 25"


def tile(location: int) -> str:
    return f"Tile {location - CHECKS_FINDER_BASE + 1}"


def test_a_slots_tracker(admin, make_user):
    ann_name, ann = make_user("ann")
    _other_name, other = make_user("other")
    gid, _job = make_game(admin, names=("Trax", "Quill"), owners={"Trax": ann})
    slots = {s["name"]: s["slot"] for s in admin.get(f"/api/games/{gid}").json()["slots"]}
    url = f"/api/games/{gid}/slots/{slots['Trax']}/tracker"

    # before anyone played: everything still to check
    tracker = ann.get(url).json()
    assert (tracker["name"], tracker["game"], tracker["goal"]) == ("Trax", "ChecksFinder", False)
    assert tracker["items"] == [] and tracker["hints"] == []
    assert len(tracker["locations"]) == 25 and not any(loc["checked"] for loc in tracker["locations"])
    assert {loc["location"] for loc in tracker["locations"]} == {f"Tile {n}" for n in range(1, 26)}

    # a slot's tracker is for its owner and the admins
    assert other.get(url).status_code == 404
    assert ann.get(f"/api/games/{gid}/slots/{slots['Quill']}/tracker").status_code == 404
    assert admin.get(f"/api/games/{gid}/slots/{slots['Quill']}/tracker").json()["game"] == "APQuest"
    assert admin.get(f"/api/games/{gid}/slots/99/tracker").status_code == 404

    admin.post(f"/api/games/{gid}/start")
    port = wait_state(admin, gid, "running")["port"]
    log = lambda: [line["text"] for line in admin.get(f"/api/games/{gid}/log").json()["lines"]]  # noqa: E731
    with APClient(port, "Trax", "ChecksFinder") as client:
        first, hinted = client.missing[0], client.missing[1]
        client.ws.send(json.dumps([{"cmd": "LocationChecks", "locations": [first]}]))
        time.sleep(1)
        for command in ("/send Trax Map Width", f"/hint_location Trax {tile(hinted)}"):
            assert admin.post(f"/api/games/{gid}/command", json={"command": command}).status_code == 200
        wait_for(lambda: any(f"is at {tile(hinted)} in" in line for line in log()), timeout=15, message="the hint")
    save_now(admin, gid)

    tracker = ann.get(url).json()
    checked = {loc["location"] for loc in tracker["locations"] if loc["checked"]}
    assert checked == {tile(first)}
    # an item sent from the console comes from Archipelago itself (without the flags the multiworld gave it)
    assert {"item": "Map Width", "from": "Archipelago", "location": "Cheat Console", "flags": 0, "start": False} in tracker["items"]
    hint = next(h for h in tracker["hints"] if h["location"] == tile(hinted))
    assert hint["finder"] == "Trax" and hint["in_my_world"] and not hint["found"]
    assert hint["receiver"] in ("Trax", "Quill") and hint["item"]

    # once the hinted location is checked, the hint shows as found
    with APClient(port, "Trax", "ChecksFinder") as client:
        client.ws.send(json.dumps([{"cmd": "LocationChecks", "locations": [hinted]}]))
        time.sleep(1)
    save_now(admin, gid)
    tracker = ann.get(url).json()
    assert next(h for h in tracker["hints"] if h["location"] == tile(hinted))["found"] is True
    assert sum(loc["checked"] for loc in tracker["locations"]) == 2
    admin.post(f"/api/games/{gid}/stop")
    admin.delete(f"/api/games/{gid}")


def test_hint_points_and_release_and_collect(admin, make_user):
    """The tracker shows what the server's rules give a player, and the player releases and collects from the page."""
    _ann_name, ann = make_user("ann")
    _bob_name, bob = make_user("bob")
    gid, _job = make_game(admin, names=("Rel", "Col"), owners={"Rel": ann, "Col": bob})
    rules = {"release_mode": "goal", "collect_mode": "enabled", "hint_cost": 20, "location_check_points": 2}
    assert admin.patch(f"/api/games/{gid}", json={"options": rules}).status_code == 200
    slots = {s["name"]: s["slot"] for s in admin.get(f"/api/games/{gid}").json()["slots"]}
    url = f"/api/games/{gid}/slots/{slots['Rel']}"

    # before the server ran, from the game's settings: a hint costs 20% of the 25 locations
    tracker = ann.get(f"{url}/tracker").json()
    assert (tracker["hint_points"], tracker["hint_cost"], tracker["points_per_check"]) == (0, 5, 2)
    assert (tracker["release"], tracker["collect"]) == ("after_goal", "available")
    assert "server_rules" not in tracker
    # only the slot's player does this, and only while the server runs
    assert bob.post(f"{url}/action", json={"action": "release"}).status_code == 404
    assert admin.post(f"{url}/action", json={"action": "release"}).status_code == 404
    assert ann.post(f"{url}/action", json={"action": "everything"}).status_code == 400
    r = ann.post(f"{url}/action", json={"action": "collect"})
    assert r.status_code == 409 and "not running" in r.json()["detail"]

    admin.post(f"/api/games/{gid}/start")
    port = wait_state(admin, gid, "running")["port"]
    with APClient(port, "Rel", "ChecksFinder") as client:
        client.check(3)
    # spending a hint costs points
    assert ann.post(f"{url}/client", json={}).status_code == 200
    wait_for(lambda: ann.post(f"{url}/client", json={}).json()["state"] == "connected", timeout=30, message="connecting")
    assert ann.post(f"{url}/client/say", json={"text": "!hint Map Width"}).status_code == 200
    time.sleep(1)
    save_now(admin, gid)
    tracker = ann.get(f"{url}/tracker").json()
    assert tracker["hints_used"] == 1 and tracker["hint_points"] == 2 * 3 - 5

    # the server has the last word: !release only after the goal here
    r = ann.post(f"{url}/action", json={"action": "release"})
    assert r.status_code == 200 and r.json()["done"] is False and "beaten the game" in r.json()["message"]
    # !collect any time: everything of Rel's that was still somewhere comes to Rel
    assert tracker["items_left"] > 0
    r = ann.post(f"{url}/action", json={"action": "collect"})
    assert r.status_code == 200 and r.json()["done"] is True, r.text
    tracker = wait_for(lambda: (x := ann.get(f"{url}/tracker").json())["collect"] == "done" and x, timeout=30,
                       message="the collect in the save")
    assert tracker["items_left"] == 0

    # after the goal, !release sends the rest of Rel's world to its players
    with APClient(port, "Rel", "ChecksFinder") as client:
        client.ws.send(json.dumps([{"cmd": "StatusUpdate", "status": 30}]))
        time.sleep(1)
    save_now(admin, gid)
    assert ann.get(f"{url}/tracker").json()["release"] == "available"
    r = ann.post(f"{url}/action", json={"action": "release"})
    assert r.status_code == 200 and r.json()["done"] is True and "released" in r.json()["message"]
    tracker = wait_for(lambda: (x := ann.get(f"{url}/tracker").json())["release"] == "done" and x, timeout=30,
                       message="the release in the save")
    assert all(location["checked"] for location in tracker["locations"])

    # rules changed while the server runs show once it saved: free hints now
    rules = {"release_mode": "disabled", "hint_cost": 0}
    assert admin.patch(f"/api/games/{gid}", json={"options": rules}).status_code == 200
    save_now(admin, gid)
    tracker = ann.get(f"{url}/tracker").json()
    assert (tracker["hint_cost"], tracker["hint_points"]) == (0, 2 * 25)
    # a player without a text client open gets connected for the button; the server refuses what its rules forbid
    r = bob.post(f"/api/games/{gid}/slots/{slots['Col']}/action", json={"action": "release"})
    assert r.status_code == 200 and r.json()["done"] is False and "disabled" in r.json()["message"]
    admin.post(f"/api/games/{gid}/stop")
    admin.delete(f"/api/games/{gid}")
