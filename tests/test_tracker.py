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
