"""The text client in the browser: the manager talks to the game's server as the player."""
import time

import pytest

from helpers import APClient, make_game, wait_for, wait_state

pytestmark = pytest.mark.ap


def poll(client, url, after=0):
    r = client.post(url, json={"after": after})
    assert r.status_code == 200, r.text
    return r.json()


def text(message) -> str:
    return "".join(part["text"] for part in message["parts"])


def wait_message(client, url, predicate, what):
    return wait_for(lambda: next((m for m in poll(client, url)["messages"] if predicate(m)), None),
                    timeout=20, message=what)


def test_a_player_talks_to_the_server_from_the_browser(admin, make_user):
    ann_name, ann = make_user("ann")
    _other_name, other = make_user("other")
    gid, _job = make_game(admin, names=("Tex", "Quinn"), owners={"Tex": ann})
    slots = {s["name"]: s["slot"] for s in admin.get(f"/api/games/{gid}").json()["slots"]}
    url = f"/api/games/{gid}/slots/{slots['Tex']}/client"

    # nothing to connect to while the server is stopped
    assert poll(ann, url)["state"] == "offline"
    # only the player of the slot talks in its name; admins have the console
    assert other.post(url, json={}).status_code == 404
    assert admin.post(url, json={}).status_code == 404
    assert ann.post(f"/api/games/{gid}/slots/{slots['Quinn']}/client", json={}).status_code == 404
    assert ann.post(url, json={}, headers={"X-Requested-With": ""}).status_code == 403  # like every other change

    admin.post(f"/api/games/{gid}/start")
    port = wait_state(admin, gid, "running")["port"]
    state = wait_for(lambda: (s := poll(ann, url))["state"] == "connected" and s, timeout=30, message="connecting")
    assert state["hint_points"] == 0 and state["hint_cost"] >= 1
    joined = wait_message(ann, url, lambda m: m["type"] == "Join", "the join message")
    assert "Tex (Team #1) viewing ChecksFinder has joined" in text(joined) and joined["mine"]
    # the server warns clients that do not compress their connection
    assert not any("compressed" in text(m) for m in poll(ann, url)["messages"])
    log = [line["text"] for line in admin.get(f"/api/games/{gid}/log").json()["lines"]]
    assert any("TextOnly" in line for line in log)

    # chat and server commands go out in the player's name
    assert ann.post(f"{url}/say", json={"text": "hello from the browser"}).status_code == 200
    chat = wait_message(ann, url, lambda m: m["type"] == "Chat" and "hello from the browser" in text(m), "the chat")
    assert chat["mine"]
    assert ann.post(f"{url}/say", json={"text": "!hint"}).status_code == 200
    answer = wait_message(ann, url, lambda m: m["type"] == "CommandResult", "the answer to !hint")
    assert "points" in text(answer)
    # the client's own commands do not exist here, and they would only confuse the server
    r = ann.post(f"{url}/say", json={"text": "/received"})
    assert r.status_code == 400 and "start with !" in r.json()["detail"]
    assert ann.post(f"{url}/say", json={"text": "   "}).status_code == 400

    # what other players do arrives with names, colored like Archipelago's clients do
    seen = poll(ann, url)["last"]
    with APClient(port, "Quinn", "APQuest") as quinn:
        quinn.check(len(quinn.missing))
        sends = wait_for(lambda: [m for m in poll(ann, url, seen)["messages"] if m["type"] == "ItemSend"],
                         timeout=20, message="item messages")
    parts = [p for m in sends for p in m["parts"]]
    assert {p["text"] for p in parts if p.get("kind") == "player"} <= {"Tex", "Quinn"}
    items = [p for p in parts if p.get("kind") == "item"]
    assert items and all(p["text"] != "Unknown item" and p["item"] in ("progression", "useful", "trap", "filler")
                         for p in items)
    assert all(p["text"] != "Unknown location" for p in parts if p.get("kind") == "location")
    # Quinn finding Tex's items concerns Tex; Quinn finding their own does not
    for m in sends:
        players = [p["text"] for p in m["parts"] if p.get("kind") == "player"]
        assert m["mine"] == ("Tex" in players), (players, m["mine"])
        assert any(p.get("me") for p in m["parts"]) == ("Tex" in players)

    # names to suggest after !hint
    names = ann.get(f"{url}/names").json()
    assert "Map Width" in names["items"] and "Tile 1" in names["locations"]
    assert other.get(f"{url}/names").status_code == 404

    # when the server stops, so does the connection; it comes back with the server
    admin.post(f"/api/games/{gid}/stop")
    wait_for(lambda: poll(ann, url)["state"] == "offline", timeout=30, message="going offline")
    assert any("connection to the server was lost" in text(m) for m in poll(ann, url)["messages"])
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    wait_for(lambda: poll(ann, url)["state"] == "connected", timeout=30, message="connecting again")

    # closing the client leaves the multiworld; opening it again starts a fresh list of messages
    last = poll(ann, url)["last"]
    assert ann.delete(url).status_code == 200
    log = lambda: [line["text"] for line in admin.get(f"/api/games/{gid}/log").json()["lines"]]  # noqa: E731
    wait_for(lambda: any("Tex (Team #1) has stopped viewing the game" in line for line in log()), timeout=15,
             message="the part message")
    fresh = poll(ann, url, after=last)
    assert fresh["last"] < last and [m["seq"] for m in fresh["messages"]] == list(range(1, fresh["last"] + 1))
    ann.delete(url)
    admin.post(f"/api/games/{gid}/stop")
    admin.delete(f"/api/games/{gid}")


def test_a_password_protected_room(admin, make_user):
    _ann_name, ann = make_user("ann")
    gid, _job = make_game(admin, names=("Locke", "Key"), owners={"Locke": ann})
    assert admin.patch(f"/api/games/{gid}", json={"password": "sesame"}).status_code == 200
    slot = next(s["slot"] for s in admin.get(f"/api/games/{gid}").json()["slots"] if s["name"] == "Locke")
    url = f"/api/games/{gid}/slots/{slot}/client"
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    # the manager knows the password; the browser never needs it
    wait_for(lambda: poll(ann, url)["state"] == "connected", timeout=30, message="connecting with the password")
    ann.delete(url)
    admin.post(f"/api/games/{gid}/stop")
    admin.delete(f"/api/games/{gid}")


def test_tabs_share_one_connection(admin, make_user):
    _ann_name, ann = make_user("ann")
    gid, _job = make_game(admin, names=("Solo", "Duo"), owners={"Solo": ann})
    slot = next(s["slot"] for s in admin.get(f"/api/games/{gid}").json()["slots"] if s["name"] == "Solo")
    url = f"/api/games/{gid}/slots/{slot}/client"
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    wait_for(lambda: poll(ann, url)["state"] == "connected", timeout=30, message="connecting")
    # a second tab polls the same connection: the server sees one client, not one per tab
    poll(ann, url)
    time.sleep(1)
    joins = [line["text"] for line in admin.get(f"/api/games/{gid}/log").json()["lines"]
             if "Solo (Team #1) viewing" in line["text"]]
    assert len(joins) == 1, joins
    ann.delete(url)
    admin.post(f"/api/games/{gid}/stop")
    admin.delete(f"/api/games/{gid}")
