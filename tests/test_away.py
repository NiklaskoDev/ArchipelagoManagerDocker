"""Coming back to a game: what happened while a player was away, and what players say about their slot (stuck, on a
break, ...)."""
import json
import sqlite3
import time

import pytest

from helpers import APClient, make_game, wait_for, wait_state

pytestmark = pytest.mark.ap

ALL_EVENTS = ["game_online", "game_crashed", "player_goal", "player_stuck", "slot_handover", "hint_found", "generation",
              "signup", "apworld_pending", "apworld_update", "ap_update"]


def visit(client, gid, **body):
    r = client.post(f"/api/games/{gid}/visit", json=body)
    assert r.status_code == 200, r.text
    return r.json()["summary"]


def away_for_an_hour(server, gid, client):
    """As if the player's last look at the game was an hour ago."""
    uid = client.get("/api/auth/status").json()["user"]["id"]
    conn = sqlite3.connect(server.data_dir / "manager.sqlite3")
    with conn:
        conn.execute("UPDATE game_visits SET seen_at = seen_at - 3600 WHERE game_id = ? AND user_id = ?", (gid, uid))
    conn.close()


def set_status(client, gid, slot, status, note=""):
    return client.put(f"/api/games/{gid}/slots/{slot}/status", json={"status": status, "note": note})


def test_a_player_coming_back_sees_what_happened_while_they_were_away(ap_server, admin, make_user, discord):
    admin.patch("/api/settings", json={"notify_events": ALL_EVENTS})
    _ann_name, ann = make_user("ann")
    _bob_name, bob = make_user("bob")
    gid, _job = make_game(admin, names=("Homer", "Rover"), owners={"Homer": ann, "Rover": bob})
    slots = {s["name"]: s["slot"] for s in admin.get(f"/api/games/{gid}").json()["slots"]}
    try:

        # nothing to sum up on a first visit, nor right after one; nor for someone without a slot
        assert visit(ann, gid) is None and visit(ann, gid) is None
        assert visit(admin, gid) is None
        visit(bob, gid)

        admin.post(f"/api/games/{gid}/start")
        port = wait_state(admin, gid, "running")["port"]
        for tile in range(1, 6):  # hints for what lies in ann's world: some of it is Rover's
            admin.post(f"/api/games/{gid}/command", json={"command": f"/hint_location Homer Tile {tile}"})
        assert set_status(bob, gid, slots["Rover"], "stuck", "need\nthe   sword").status_code == 200
        with APClient(port, "Rover", "APQuest") as client:
            assert client.connected
            client.ws.send(json.dumps([{"cmd": "Say", "text": "see you"}]))
            client.check(len(client.missing))  # everything in Rover's world, much of it for Homer
            client.ws.send(json.dumps([{"cmd": "StatusUpdate", "status": 30}]))
            time.sleep(1.5)
        feed = wait_for(lambda: (f := admin.get(f"/api/games/{gid}/activity").json()["entries"])
                        and any(e["kind"] == "goal" for e in f) and f, timeout=20, message="Rover's goal in the feed")

        away_for_an_hour(ap_server, gid, ann)
        summary = visit(ann, gid)
        sent = [e for e in feed if e["kind"] == "item" and e["receiver"] == "Homer" and e["finder"] == "Rover"]
        assert sent and summary["received"]["count"] == len(sent)
        assert {i["item"] for i in summary["received"]["items"]} <= {e["item"] for e in sent}
        assert summary["goals"] == ["Rover"] and summary["chat"] == 1 and not summary["partial"]
        assert [(s["player"], s["note"]) for s in summary["stuck"]] == [("Rover", "need the sword")]
        waiting = {(e["item"], e["location"]) for e in feed if e["kind"] == "hint" and e["finder"] == "Homer"
                   and e["receiver"] == "Rover" and e["status"] != "found"}
        checked = {e["location"] for e in feed if e["kind"] == "item" and e["finder"] == "Homer"}
        assert {(h["item"], h["location"]) for h in summary["hints"] if h["in_my_world"]} == \
            {hint for hint in waiting if hint[1] not in checked}
        # what Rover did in his own world and his chat are not news to Rover
        away_for_an_hour(ap_server, gid, bob)
        for_bob = visit(bob, gid)
        assert for_bob is None or (for_bob["chat"] == 0 and for_bob["goals"] == [] and for_bob["stuck"] == [])

        # it stays until it is put away, however often the page opens
        assert visit(ann, gid) == summary
        assert visit(ann, gid, dismiss=True) is None
        assert visit(ann, gid) is None
        admin.post(f"/api/games/{gid}/stop")
    finally:
        admin.delete(f"/api/games/{gid}")


def test_players_say_how_they_are_doing(ap_server, admin, make_user, discord):
    admin.patch("/api/settings", json={"notify_events": ALL_EVENTS})
    _ann_name, ann = make_user("ann")
    _bob_name, bob = make_user("bob")
    _cy_name, cy = make_user("cy")
    gid, _job = make_game(admin, names=("Stucky", "Helper"), owners={"Stucky": ann, "Helper": bob})
    game = admin.get(f"/api/games/{gid}").json()
    slots = {s["name"]: s["slot"] for s in game["slots"]}
    try:
        admin.patch(f"/api/games/{gid}", json={"visibility": "all"})

        # only the slot's player (and administrators) say how it is going
        assert set_status(bob, gid, slots["Stucky"], "stuck").status_code == 403
        assert set_status(cy, gid, slots["Stucky"], "stuck").status_code == 403
        assert set_status(ann, gid, slots["Stucky"], "sleeping").status_code == 400
        assert set_status(ann, gid, slots["Stucky"], "stuck", "x" * 141).status_code == 400

        r = set_status(ann, gid, slots["Stucky"], "stuck", " need\tthe\nHookshot ")
        assert r.status_code == 200, r.text
        mine = next(s for s in r.json()["slots"] if s["name"] == "Stucky")
        assert (mine["player_status"], mine["status_note"]) == ("stuck", "need the Hookshot") and mine["status_at"]
        # everyone who sees the game sees it
        seen = next(s for s in cy.get(f"/api/games/{gid}").json()["slots"] if s["name"] == "Stucky")
        assert (seen["player_status"], seen["status_note"]) == ("stuck", "need the Hookshot")

        # Discord hears about it once, with what the player needs
        message = discord.wait_for("Stucky is stuck")
        assert "need the Hookshot" in message["description"]
        assert set_status(ann, gid, slots["Stucky"], "playing").status_code == 200
        assert set_status(ann, gid, slots["Stucky"], "stuck").status_code == 200  # again, soon after
        assert set_status(admin, gid, slots["Stucky"], "away", "back tonight").status_code == 200
        time.sleep(2)
        assert discord.titles().count("Stucky is stuck") == 1

        # the feed has every change
        statuses = [(e["player"], e["status"], e.get("note")) for e in admin.get(f"/api/games/{gid}/activity").json()["entries"]
                    if e["kind"] == "status"]
        assert statuses == [("Stucky", "stuck", "need the Hookshot"), ("Stucky", "playing", None), ("Stucky", "stuck", None),
                            ("Stucky", "away", "back tonight")]

        # saying the same again changes nothing; no status takes the note with it
        assert set_status(ann, gid, slots["Stucky"], "away", "back tonight").status_code == 200
        cleared = set_status(ann, gid, slots["Stucky"], None, "ignored").json()
        mine = next(s for s in cleared["slots"] if s["name"] == "Stucky")
        assert (mine["player_status"], mine["status_note"]) == (None, None)
        assert len([e for e in admin.get(f"/api/games/{gid}/activity").json()["entries"] if e["kind"] == "status"]) == 5

        # a new player of the slot starts without the last one's status
        set_status(bob, gid, slots["Helper"], "away", "on holiday")
        assert admin.post(f"/api/games/{gid}/slots/{slots['Helper']}/assign", json={"user_id": None}).status_code == 200
        helper = next(s for s in admin.get(f"/api/games/{gid}").json()["slots"] if s["name"] == "Helper")
        assert (helper["user_id"], helper["player_status"], helper["status_note"]) == (None, None, None)
        # nor can anyone say anything for a slot nobody owns, but an administrator
        assert set_status(bob, gid, slots["Helper"], "playing").status_code == 403
    finally:
        admin.delete(f"/api/games/{gid}")
