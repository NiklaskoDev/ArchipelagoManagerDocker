"""Personal Discord mentions: players who linked their Discord account choose what they are mentioned for."""
import json
import sqlite3
import time

import pytest

from archipelago_manager.notify import MAX_MENTIONS, build_payload
from helpers import APClient, make_game, save_now, wait_for, wait_state

ALL_EVENTS = ["game_online", "game_crashed", "player_goal", "player_stuck", "player_inactive", "slot_handover", "hint_found",
              "generation", "signup", "apworld_pending", "apworld_update", "ap_update"]
KINDS = ["game_ready", "game_online", "hint_found", "player_stuck", "player_inactive", "slot_handover", "signup_open",
         "signup_closing"]
CHECKS_FINDER_BASE = 81000


def discord_ids(*names):
    """A Discord id for each name that no other test uses (each may be linked to one account only)."""
    base = 10 ** 17 + time.time_ns() % 10 ** 15 * 10
    return {name: str(base + i) for i, name in enumerate(names)}


def link(server, username, discord_id):
    """What signing in through Discord leaves behind, without Discord."""
    conn = sqlite3.connect(server.data_dir / "manager.sqlite3")
    with conn:
        conn.execute("UPDATE users SET discord_id = ?, discord_name = ? WHERE username = ?",
                     (discord_id, f"{username} on Discord", username))
    conn.close()


def choose(client, pings):
    r = client.put("/api/auth/discord/pings", json={"pings": pings})
    assert r.status_code == 200, r.text
    return r.json()


def mentioned(message):
    """Who a message mentions, checked against what Discord is allowed to turn into a ping."""
    users = message["allowed_mentions"].get("users", [])
    assert message["allowed_mentions"]["parse"] == []  # never @everyone, @here or roles
    assert message.get("content", "") == " ".join(f"<@{u}>" for u in users)
    return users


def test_only_the_listed_users_can_be_mentioned():
    plain = build_payload("Title")
    assert "content" not in plain and plain["allowed_mentions"] == {"parse": []}
    # only Discord ids: nothing else can end up in the message text
    payload = build_payload("Title", mentions=["123", "12a", "<@&999>", "@everyone", "", "456"])
    assert payload["content"] == "<@123> <@456>"
    assert payload["allowed_mentions"] == {"parse": [], "users": ["123", "456"]}
    # a message holds 2000 characters
    many = build_payload("Title", mentions=[str(10 ** 19 + i) for i in range(MAX_MENTIONS + 20)])
    assert len(many["allowed_mentions"]["users"]) == MAX_MENTIONS and len(many["content"]) <= 2000


def test_players_choose_their_mentions_and_sign_ups_mention_them(plain_server):
    from helpers import FakeWebhook
    hook = FakeWebhook()
    server = plain_server
    admin = server.client()
    assert admin.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123"}).status_code == 200
    users = {}
    for name in ("ann", "cy", "dan", "eve", "fay"):
        r = admin.post("/api/users", json={"username": name, "password": "password123", "role": "user"})
        users[name] = (r.json()["id"], server.client(name, "password123"))
    ids = discord_ids("ann", "cy", "eve", "fay")
    for name, discord_id in ids.items():
        link(server, name, discord_id)
    ann, cy = users["ann"][1], users["cy"][1]
    try:
        # while no messages are sent, there is nothing to be mentioned in
        assert ann.get("/api/auth/status").json()["ping_kinds"] == []
        # only with a linked account, and only for the kinds there are
        r = users["dan"][1].put("/api/auth/discord/pings", json={"pings": ["signup_open"]})
        assert r.status_code == 409 and "Link your Discord account" in r.json()["detail"]
        assert ann.put("/api/auth/discord/pings", json={"pings": ["everything"]}).status_code == 400
        assert choose(ann, ["signup_closing", "game_ready"])["discord_pings"] == ["game_ready", "signup_closing"]
        assert ann.get("/api/auth/status").json()["user"]["discord_pings"] == ["game_ready", "signup_closing"]
        choose(cy, KINDS)
        choose(users["eve"][1], ["signup_open"])
        choose(users["fay"][1], ["signup_closing"])

        # what players can choose follows the messages the admin sends
        r = admin.patch("/api/settings", json={"discord_webhook_url": hook.url,
                                               "notify_events": [e for e in ALL_EVENTS if e != "signup"]})
        assert r.status_code == 200 and r.json()["notify_mentions"] is True
        assert ann.get("/api/auth/status").json()["ping_kinds"] == ["game_ready", "game_online", "hint_found",
                                                                    "player_stuck", "player_inactive", "slot_handover"]
        admin.patch("/api/settings", json={"notify_events": ALL_EVENTS})
        assert ann.get("/api/auth/status").json()["ping_kinds"] == KINDS
        assert server.client().get("/api/auth/status").json()["ping_kinds"] == []  # not signed in

        # a disabled account is not mentioned
        assert admin.patch(f"/api/users/{users['eve'][0]}", json={"disabled": True}).status_code == 200
        r = admin.post("/api/signups", json={"name": "Pinged round"})
        assert r.status_code == 200, r.text
        signup = r.json()
        assert mentioned(hook.wait_for_message("Sign-ups open: Pinged round")) == [ids["cy"]]

        # the reminder only mentions those who have not entered yet (the clock runs every 20 seconds)
        conn = sqlite3.connect(server.data_dir / "manager.sqlite3")
        with conn:
            conn.execute("INSERT INTO signup_entries(signup_id, user_id, name, sha256, created_at, updated_at) "
                         "VALUES (?, ?, 'Fay', 'x', 0, 0)", (signup["id"], users["fay"][0]))
            conn.execute("UPDATE signups SET created_at = ?, deadline = ? WHERE id = ?",
                         (time.time() - 3 * 86400, time.time() + 6 * 3600, signup["id"]))
        conn.close()
        reminder = hook.wait_for_message("Sign-ups for Pinged round close soon", timeout=60)
        assert mentioned(reminder) == [ids["ann"], ids["cy"]]

        # an unlinked account is not mentioned (what it chose is kept for when it is linked again)
        assert ann.post("/api/auth/discord/unlink").json()["discord_pings"] == ["game_ready", "signup_closing"]
        choose(users["fay"][1], ["signup_open"])
        admin.post("/api/signups", json={"name": "Second round"})
        assert mentioned(hook.wait_for_message("Sign-ups open: Second round")) == [ids["cy"], ids["fay"]]

        # an administrator can keep everyone from being mentioned
        assert admin.patch("/api/settings", json={"notify_mentions": False}).json()["notify_mentions"] is False
        assert cy.get("/api/auth/status").json()["ping_kinds"] == []
        admin.post("/api/signups", json={"name": "Quiet round"})
        quiet = hook.wait_for_message("Sign-ups open: Quiet round")
        assert "content" not in quiet and quiet["allowed_mentions"] == {"parse": []}
    finally:
        hook.close()


@pytest.mark.ap
def test_players_are_mentioned_for_their_own_games(ap_server, admin, make_user, discord):
    ann_name, ann = make_user("ann")
    bob_name, bob = make_user("bob")
    cy_name, cy = make_user("cy")
    ids = discord_ids("ann", "bob", "cy")
    for key, name in (("ann", ann_name), ("bob", bob_name), ("cy", cy_name)):
        link(ap_server, name, ids[key])
    choose(ann, ["game_ready", "hint_found"])
    choose(bob, ["game_online", "hint_found"])
    choose(cy, KINDS)  # everything, but plays in none of these games

    # a new game that is already running tells those who want to know about either
    gid, _job = make_game(admin, names=("PingA", "PingB", "PingC"), owners={"PingA": ann, "PingB": ann, "PingC": bob},
                          host_after=True)
    ready = discord.wait_for_message("New game ready")
    assert mentioned(ready) == [ids["ann"], ids["bob"]]  # once each, also for two slots
    game = wait_state(admin, gid, "running")

    # a hinted item that is found mentions the player it is for
    port = game["port"]
    with APClient(port, "PingA", "ChecksFinder") as client:
        assert client.connected
        location = client.missing[0]
        tile = f"Tile {location - CHECKS_FINDER_BASE + 1}"
        assert admin.post(f"/api/games/{gid}/command", json={"command": f"/hint_location PingA {tile}"}).status_code == 200
        wait_for(lambda: any(f"is at {tile} in" in line["text"]
                             for line in admin.get(f"/api/games/{gid}/log").json()["lines"]), timeout=15, message="the hint")
        client.ws.send(json.dumps([{"cmd": "LocationChecks", "locations": [location]}]))
        time.sleep(1)
    save_now(admin, gid)
    found = discord.wait_for_message("Hinted item found")
    receiver = "bob" if "**PingC**'s" in found["embeds"][0]["description"] else "ann"
    assert mentioned(found) == [ids[receiver]]

    # a start by a person: only bob asked to hear about that
    admin.post(f"/api/games/{gid}/stop")
    wait_state(admin, gid, "stopped")
    assert ann.post(f"/api/games/{gid}/start").status_code == 200
    assert mentioned(discord.wait_for_message("is online", timeout=30)) == [ids["bob"]]
    admin.post(f"/api/games/{gid}/stop")
    admin.delete(f"/api/games/{gid}")
