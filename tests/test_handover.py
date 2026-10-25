"""Handing a slot over: its player offers it to someone, or to whoever takes it, who then plays on with its progress."""
import sqlite3

import pytest

from helpers import make_game

pytestmark = pytest.mark.ap

ALL_EVENTS = ["game_online", "game_crashed", "player_goal", "player_stuck", "slot_handover", "hint_found", "generation",
              "signup", "apworld_pending", "apworld_update", "ap_update"]


def offer(client, gid, slot, to=None, note=""):
    return client.put(f"/api/games/{gid}/slots/{slot}/handover", json={"to": to, "note": note})


def slot_of(client, gid, name):
    return next(s for s in client.get(f"/api/games/{gid}").json()["slots"] if s["name"] == name)


def test_a_player_hands_their_slot_to_someone_they_name(ap_server, admin, make_user, discord):
    admin.patch("/api/settings", json={"notify_events": ALL_EVENTS})
    _ann_name, ann = make_user("ann")
    bob_name, bob = make_user("bob")
    cy_name, cy = make_user("cy")
    gid, _job = make_game(admin, names=("Leaver", "Stayer"), owners={"Leaver": ann, "Stayer": bob})
    slots = {s["name"]: s["slot"] for s in admin.get(f"/api/games/{gid}").json()["slots"]}
    try:
        # only the slot's player (and administrators) hand it over, to someone who exists and does not play it yet
        assert offer(bob, gid, slots["Leaver"], cy_name).status_code == 403
        assert offer(cy, gid, slots["Leaver"], cy_name).status_code == 404  # cy cannot even see the game
        assert offer(ann, gid, slots["Leaver"], "nobody-of-this-name").status_code == 404
        assert offer(ann, gid, slots["Leaver"], cy_name, "x" * 141).status_code == 400
        assert cy.get("/api/games").json() == []

        r = offer(ann, gid, slots["Leaver"], cy_name.upper(), " no time\tany more ")
        assert r.status_code == 200, r.text
        handover = next(s for s in r.json()["slots"] if s["name"] == "Leaver")["handover"]
        assert (handover["to"], handover["note"], handover["to_me"], handover["can_accept"]) == \
            (cy_name, "no time any more", False, False)
        message = discord.wait_for(f"hands Leaver over to {cy_name}")
        assert cy_name in message["description"]

        # cy now sees the game, with the offer, but not its password or files until taking it
        listed = cy.get("/api/games").json()
        assert [g["id"] for g in listed] == [gid]
        assert [(o["name"], o["to_me"], o["from"]) for o in listed[0]["offered_slots"]] == [("Leaver", True, _ann_name)]
        seen = cy.get(f"/api/games/{gid}").json()
        assert seen["password"] is None and seen["my_slots"] == []
        assert next(s for s in seen["slots"] if s["name"] == "Leaver")["handover"]["can_accept"] is True
        # bob sees it is offered to cy, and cannot take it
        assert slot_of(bob, gid, "Leaver")["handover"]["can_accept"] is False
        assert bob.post(f"/api/games/{gid}/slots/{slots['Leaver']}/handover/accept").status_code == 403

        # cy takes it over: the slot is cy's, without the offer and the last player's status
        r = cy.post(f"/api/games/{gid}/slots/{slots['Leaver']}/handover/accept")
        assert r.status_code == 200, r.text
        taken = next(s for s in r.json()["slots"] if s["name"] == "Leaver")
        assert taken["mine"] and taken["owner"] == cy_name and taken["handover"] is None
        assert [s["name"] for s in r.json()["my_slots"]] == ["Leaver"]
        # ann has no slot in the game any more, so no longer sees it
        assert ann.get("/api/games").json() == [] and ann.get(f"/api/games/{gid}").status_code == 404
        assert discord.wait_for(f"{cy_name} takes over Leaver")
        # once is enough
        assert cy.post(f"/api/games/{gid}/slots/{slots['Leaver']}/handover/accept").status_code == 409

        feed = [e for e in admin.get(f"/api/games/{gid}/activity").json()["entries"] if e["kind"].startswith("handover")]
        assert feed[0]["kind"] == "handover" and (feed[0]["from"], feed[0]["to"]) == (_ann_name, cy_name)
        assert feed[1]["kind"] == "handover_done" and (feed[1]["from"], feed[1]["to"]) == (_ann_name, cy_name)
        actions = [(e["action"], e["username"]) for e in admin.get("/api/audit?action=slot").json()["entries"]]
        assert ("slot.handover_accept", cy_name) in actions

        # the user it is offered to can say no, and then no longer sees the game
        assert offer(bob, gid, slots["Stayer"], _ann_name).status_code == 200
        assert [g["id"] for g in ann.get("/api/games").json()] == [gid]
        r = ann.delete(f"/api/games/{gid}/slots/{slots['Stayer']}/handover")
        assert r.status_code == 200 and r.json() == {"declined": True}
        assert ann.get("/api/games").json() == [] and slot_of(bob, gid, "Stayer")["handover"] is None
        assert bob_name == slot_of(admin, gid, "Stayer")["owner"]
    finally:
        admin.delete(f"/api/games/{gid}")


def test_a_slot_offered_to_whoever_takes_it(ap_server, admin, make_user, discord):
    admin.patch("/api/settings", json={"notify_events": ALL_EVENTS})
    ann_name, ann = make_user("ann")
    _bob_name, bob = make_user("bob")
    cy_name, cy = make_user("cy")
    dee_name, dee = make_user("dee")
    gid, _job = make_game(admin, names=("Open", "Kept"), owners={"Open": ann, "Kept": bob})
    slots = {s["name"]: s["slot"] for s in admin.get(f"/api/games/{gid}").json()["slots"]}
    try:
        admin.patch(f"/api/games/{gid}", json={"visibility": "all", "single_claim": True})
        r = offer(ann, gid, slots["Open"])
        assert r.status_code == 200, r.text
        assert next(s for s in r.json()["slots"] if s["name"] == "Open")["handover"]["to"] is None
        assert discord.wait_for("Open needs a new player")

        # with "one slot each", bob cannot take a second one; cy and dee can, and the first one gets it
        assert slot_of(bob, gid, "Open")["handover"]["can_accept"] is False
        assert bob.post(f"/api/games/{gid}/slots/{slots['Open']}/handover/accept").status_code == 409
        assert [o["name"] for o in next(g for g in cy.get("/api/games").json() if g["id"] == gid)["offered_slots"]] == ["Open"]
        assert cy.post(f"/api/games/{gid}/slots/{slots['Open']}/handover/accept").status_code == 200
        assert dee.post(f"/api/games/{gid}/slots/{slots['Open']}/handover/accept").status_code == 409
        assert slot_of(admin, gid, "Open")["owner"] == cy_name

        # nobody else can take an offer back; its player and administrators can
        assert offer(cy, gid, slots["Open"]).status_code == 200
        assert dee.delete(f"/api/games/{gid}/slots/{slots['Open']}/handover").status_code == 403
        assert admin.delete(f"/api/games/{gid}/slots/{slots['Open']}/handover").status_code == 200
        assert slot_of(dee, gid, "Open")["handover"] is None
        assert dee.post(f"/api/games/{gid}/slots/{slots['Open']}/handover/accept").status_code == 409

        # an administrator assigning the slot ends the offer, and so does releasing it
        assert offer(cy, gid, slots["Open"], dee_name).status_code == 200
        admin.post(f"/api/games/{gid}/slots/{slots['Open']}/assign", json={"user_id": None})
        assert slot_of(admin, gid, "Open")["handover"] is None
        assert offer(bob, gid, slots["Kept"]).status_code == 200
        assert bob.post(f"/api/games/{gid}/slots/{slots['Kept']}/unclaim").status_code == 200
        assert slot_of(admin, gid, "Kept")["handover"] is None
        # a slot nobody plays is not handed over: it is claimed or assigned
        assert offer(admin, gid, slots["Kept"]).status_code == 400
    finally:
        admin.delete(f"/api/games/{gid}")


def test_deleting_a_user_ends_the_offers_made_to_them(ap_server, admin, make_user):
    _ann_name, ann = make_user("ann")
    cy_name, _cy = make_user("cy")
    gid, _job = make_game(admin, names=("Given",), owners={"Given": ann})
    slot = slot_of(admin, gid, "Given")["slot"]
    try:
        assert offer(ann, gid, slot, cy_name).status_code == 200
        cy_id = next(u["id"] for u in admin.get("/api/users").json() if u["username"] == cy_name)
        assert admin.delete(f"/api/users/{cy_id}").status_code == 200
        assert slot_of(ann, gid, "Given")["handover"] is None
        conn = sqlite3.connect(ap_server.data_dir / "manager.sqlite3")
        assert conn.execute("SELECT handover_to FROM game_slots WHERE game_id = ?", (gid,)).fetchone() == (None,)
        conn.close()
    finally:
        admin.delete(f"/api/games/{gid}")
