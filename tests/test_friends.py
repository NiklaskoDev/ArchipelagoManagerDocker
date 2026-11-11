"""Friends: players ask each other by name and answer; friends share configs with each other alone, and generate games
with each other in."""
import sqlite3

import pytest

from conftest import Server
from helpers import player_yaml, run_job, upload_yaml


def setup(server):
    admin = server.client()
    assert admin.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123",
                                               "code": server.setup_code()}).status_code == 200
    return admin


def user(server, admin, name):
    r = admin.post("/api/users", json={"username": name, "password": "password123", "role": "user"})
    assert r.status_code == 200, r.text
    return r.json()["id"], server.client(name, "password123")


def upload(client, name, content):
    r = upload_yaml(client, name, content)
    assert r.status_code == 200, r.text
    return r.json()["created"][0]


def share(client, config, share_with="everyone", friends=(), shared=True):
    return client.put(f"/api/configs/{config['id']}/shared",
                      json={"shared": shared, "share_with": share_with, "friends": list(friends)})


def shared_ids(client):
    return {c["id"] for c in client.get("/api/configs/shared").json()}


def names(people):
    return [p["username"] for p in people]


def test_players_ask_answer_and_part(plain_server):
    admin = setup(plain_server)
    ann_id, ann = user(plain_server, admin, "ann")
    bob_id, bob = user(plain_server, admin, "bob")
    carl_id, carl = user(plain_server, admin, "carl")

    # asking by name, whatever the case; it waits for an answer
    r = ann.post("/api/friends", json={"username": " BOB "})
    assert r.status_code == 200 and r.json() == {"status": "asked", "user": {"id": bob_id, "username": "bob"}}
    assert names(ann.get("/api/friends").json()["outgoing"]) == ["bob"]
    waiting = bob.get("/api/friends").json()
    assert names(waiting["incoming"]) == ["ann"] and waiting["friends"] == [] and waiting["incoming"][0]["asked_at"]
    for client, username, status, says in (
        (ann, "bob", 409, "asked bob already"),
        (ann, "ann", 400, "yourself"),
        (ann, "nobody", 404, "no user called 'nobody'"),
    ):
        r = client.post("/api/friends", json={"username": username})
        assert r.status_code == status and says in r.json()["detail"], r.text
    # the one asked cannot accept their own request for the other
    assert ann.post(f"/api/friends/{bob_id}/accept").status_code == 404

    assert bob.post(f"/api/friends/{ann_id}/accept").status_code == 200
    for client, other in ((ann, "bob"), (bob, "ann")):
        listing = client.get("/api/friends").json()
        assert names(listing["friends"]) == [other] and listing["incoming"] == listing["outgoing"] == []
        assert listing["friends"][0]["since"] and listing["friends"][0]["shared_configs"] == 0
    r = bob.post("/api/friends", json={"username": "ann"})
    assert r.status_code == 409 and "friends already" in r.json()["detail"]

    # asking someone who asked you already makes you friends
    assert carl.post("/api/friends", json={"username": "ann"}).json()["status"] == "asked"
    assert ann.post("/api/friends", json={"username": "carl"}).json()["status"] == "accepted"
    assert names(carl.get("/api/friends").json()["friends"]) == ["ann"]

    # ending it, turning a request down and taking one back all remove it
    assert carl.delete(f"/api/friends/{ann_id}").json() == {"removed": "friend"}
    assert ann.get("/api/friends").json()["friends"][0]["username"] == "bob"
    assert carl.post("/api/friends", json={"username": "bob"}).status_code == 200
    assert bob.delete(f"/api/friends/{carl_id}").json() == {"removed": "declined"}
    assert carl.post("/api/friends", json={"username": "bob"}).status_code == 200
    assert carl.delete(f"/api/friends/{bob_id}").json() == {"removed": "withdrawn"}
    assert carl.get("/api/friends").json()["outgoing"] == [] and bob.get("/api/friends").json()["incoming"] == []
    assert carl.delete(f"/api/friends/{bob_id}").status_code == 404

    # a disabled account cannot be asked and is not listed; a deleted one is gone from the list
    admin.patch(f"/api/users/{carl_id}", json={"disabled": True})
    assert ann.post("/api/friends", json={"username": "carl"}).status_code == 404
    admin.patch(f"/api/users/{bob_id}", json={"disabled": True})
    assert ann.get("/api/friends").json()["friends"] == []
    admin.patch(f"/api/users/{bob_id}", json={"disabled": False})
    assert names(ann.get("/api/friends").json()["friends"]) == ["bob"]
    assert admin.delete(f"/api/users/{bob_id}").status_code == 200
    assert ann.get("/api/friends").json()["friends"] == []


def test_friends_share_configs_with_each_other_alone(plain_server):
    admin = setup(plain_server)
    ann_id, ann = user(plain_server, admin, "ann")
    bob_id, bob = user(plain_server, admin, "bob")
    dan_id, dan = user(plain_server, admin, "dan")
    eve_id, eve = user(plain_server, admin, "eve")  # nobody's friend
    for username, other in (("bob", bob), ("dan", dan)):
        assert ann.post("/api/friends", json={"username": username}).status_code == 200
        assert other.post(f"/api/friends/{ann_id}/accept").status_code == 200

    config = upload(ann, "secret", player_yaml("Ann"))
    # with all of ann's friends
    r = share(ann, config, "friends")
    assert r.status_code == 200 and (r.json()["shared"], r.json()["share_with"]) == (True, "friends"), r.text
    assert config["id"] in shared_ids(bob) and config["id"] in shared_ids(dan)
    assert config["id"] not in shared_ids(eve)
    for path in (f"/api/configs/shared/{config['id']}", f"/api/configs/{config['id']}/download"):
        assert eve.get(path).status_code == 404 and bob.get(path).status_code == 200
    assert eve.post(f"/api/configs/{config['id']}/copy", json={}).status_code == 404
    seen = next(c for c in bob.get("/api/configs/shared").json() if c["id"] == config["id"])
    assert (seen["owner"], seen["from_friend"], seen["share_with"]) == ("ann", True, "friends")
    assert bob.get("/api/friends").json()["friends"][0]["shared_configs"] == 1

    # with chosen friends only
    r = share(ann, config, "chosen", [bob_id])
    assert r.status_code == 200 and r.json()["shared_with"] == [{"id": bob_id, "username": "bob"}], r.text
    assert config["id"] in shared_ids(bob) and config["id"] not in shared_ids(dan)
    assert dan.get(f"/api/configs/{config['id']}/download").status_code == 404
    mine = next(c for c in ann.get("/api/configs").json() if c["id"] == config["id"])
    assert (mine["share_with"], names(mine["shared_with"])) == ("chosen", ["bob"])
    for body_with, friends, says in (("chosen", [], "at least one friend"), ("chosen", [eve_id], "only with your friends"),
                                     ("nobody", [], "Share with one of")):
        r = share(ann, config, body_with, friends)
        assert r.status_code == 400 and says in r.json()["detail"], r.text
    assert bob.put(f"/api/configs/{config['id']}/shared", json={"shared": True}).status_code == 404

    # the friends' configs among one's own, for generating: not those of others who share with everyone
    public = upload(eve, "public", player_yaml("Eve"))
    assert share(eve, public).status_code == 200
    assert public["id"] in shared_ids(bob) and public["id"] in shared_ids(dan)
    listing = bob.get("/api/configs?friends=true").json()
    assert [(c["owner"], c["id"]) for c in listing] == [("ann", config["id"])]
    assert [c["id"] for c in dan.get("/api/configs?friends=true").json()] == []

    # parting ends what was shared by name; it was shared with bob alone, so it is no longer shared at all
    assert bob.delete(f"/api/friends/{ann_id}").status_code == 200
    assert config["id"] not in shared_ids(bob)
    assert next(c for c in ann.get("/api/configs").json() if c["id"] == config["id"])["shared"] is False
    # becoming friends again does not bring it back
    assert bob.post("/api/friends", json={"username": "ann"}).status_code == 200
    assert ann.post(f"/api/friends/{bob_id}/accept").status_code == 200
    assert config["id"] not in shared_ids(bob)

    # stopping sharing forgets who it was shared with
    assert share(ann, config, "chosen", [bob_id, dan_id]).status_code == 200
    assert share(ann, config, shared=False).json()["shared"] is False
    assert config["id"] not in shared_ids(bob) | shared_ids(dan)
    assert share(ann, config, "everyone").json()["shared_with"] == []
    assert config["id"] in shared_ids(eve)


def test_configs_shared_before_friends_stay_shared_with_everyone(tmp_path):
    """Schema v23 adds who a config is shared with; one shared before is shared with every player, as it was."""
    server = Server(tmp_path / "data", ap=False)
    server.start()
    try:
        admin = setup(server)
        _ann_id, ann = user(server, admin, "ann")
        config = upload(ann, "old", player_yaml("Old"))
        assert share(ann, config).status_code == 200
    finally:
        server.stop()
    conn = sqlite3.connect(tmp_path / "data" / "manager.sqlite3")
    conn.execute("DROP TABLE config_shares")  # what a v22 database looks like
    conn.execute("DROP TABLE friends")
    conn.execute("ALTER TABLE configs DROP COLUMN share_with")
    conn.execute("PRAGMA user_version = 22")
    conn.commit()
    conn.close()
    server.start()
    try:
        _bob_id, bob = user(server, server.client("boss", "bosspass123"), "bob")
        seen = next(c for c in bob.get("/api/configs/shared").json() if c["id"] == config["id"])
        assert (seen["share_with"], seen["from_friend"]) == ("everyone", False)
    finally:
        server.stop()


@pytest.mark.ap
def test_a_player_generates_a_game_with_friends_in_it(admin, make_user):
    ann_name, ann = make_user("ann")
    bob_name, bob = make_user("bob")
    eve_name, eve = make_user("eve")
    ids = {u["username"]: u["id"] for u in admin.get("/api/users").json()}
    assert ann.post("/api/friends", json={"username": bob_name}).status_code == 200
    assert bob.post(f"/api/friends/{ids[ann_name]}/accept").status_code == 200

    own = upload(ann, f"{ann_name}-own", player_yaml("AnnPlays"))
    friends_config = upload(bob, f"{bob_name}-mine", player_yaml("BobPlays", "APQuest"))
    strangers = upload(eve, f"{eve_name}-public", player_yaml("EvePlays"))
    assert share(eve, strangers).status_code == 200
    assert admin.patch("/api/settings", json={"users_can_generate": True}).status_code == 200
    try:
        # bob has not shared it yet: as if it did not exist
        r = ann.post("/api/jobs", json={"config_ids": [own["id"], friends_config["id"]], "host_after": False})
        assert r.status_code == 404, r.text
        # a stranger's shared config is to copy, not to put its owner in a game
        r = ann.post("/api/jobs", json={"config_ids": [own["id"], strangers["id"]], "host_after": False})
        assert r.status_code == 403 and "not your friend" in r.json()["detail"], r.text

        assert share(bob, friends_config, "friends").status_code == 200
        job = run_job(ann, [own["id"], friends_config["id"]])
        assert job["status"] == "succeeded", job
        assert {p["owner"] for p in job["players"]} == {ann_name, bob_name}
        # bob is in the game: it is among his games, with his slot
        game = next(g for g in bob.get("/api/games").json() if g["id"] == job["game_id"])
        assert [s["name"] for s in game["my_slots"]] == ["BobPlays"]
        # and they show as having played together
        assert ann.get("/api/friends").json()["friends"][0]["games_together"] == 1
        # someone in a game with ann who is not her friend is suggested to her
        slots = admin.get(f"/api/games/{job['game_id']}").json()["slots"]
        bobs = next(s["slot"] for s in slots if s["name"] == "BobPlays")
        r = admin.post(f"/api/games/{job['game_id']}/slots/{bobs}/assign", json={"user_id": ids[eve_name]})
        assert r.status_code == 200, r.text
        assert ann.get("/api/friends").json()["suggestions"] == [{"id": ids[eve_name], "username": eve_name,
                                                                  "games_together": 1}]
        admin.delete(f"/api/games/{job['game_id']}")
    finally:
        admin.patch("/api/settings", json={"users_can_generate": False})
