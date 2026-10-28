"""Spectators: a link shows a game without signing in, for viewers and as an overlay in a stream (watch.html)."""
import json

import pytest

from helpers import APClient, make_game, save_now, wait_for, wait_state

pytestmark = pytest.mark.ap


def test_a_spectator_link_shows_the_game_and_nothing_to_play_it_with(ap_server, admin, make_user):
    ann_name, ann = make_user("ann")
    gid, _job = make_game(admin, names=("Watched", "Watcher"), owners={"Watched": ann})
    anyone = ap_server.client()  # not signed in
    try:
        admin.patch(f"/api/games/{gid}", json={"password": "hunter22"})
        assert admin.get(f"/api/games/{gid}").json()["spectate"] is None
        assert anyone.get("/api/watch/not-a-link").status_code == 404
        # only whoever runs the game makes the link
        assert ann.post(f"/api/games/{gid}/spectate").status_code == 403
        assert anyone.post(f"/api/games/{gid}/spectate").status_code in (401, 403)
        link = admin.post(f"/api/games/{gid}/spectate").json()["spectate"]
        assert link["activity"] is False and len(link["token"]) >= 20
        assert "spectate" not in ann.get(f"/api/games/{gid}").json()

        r = anyone.get(f"/api/watch/{link['token']}")
        assert r.status_code == 200, r.text
        seen = r.json()
        assert seen["state"] == "stopped" and seen["activity"] is False
        assert [(p["name"], p["checked"], p["goal_at"]) for p in seen["players"]] == [("Watched", 0, None), ("Watcher", 0, None)]
        # nothing players connect with, and no user names
        assert "hunter22" not in r.text and ann_name not in r.text and '"admin"' not in r.text
        assert not {"address", "port", "password", "owner"} & (set(seen) | {k for p in seen["players"] for k in p})
        assert all("owner" not in s for s in seen["history"]["slots"])
        assert anyone.get(f"/api/watch/{link['token']}/activity").status_code == 404
        assert anyone.get("/watch.html").status_code == 200

        # with the activity: items and goals, never the chat
        assert admin.patch(f"/api/games/{gid}", json={"spectate_activity": True}).json()["spectate"]["activity"] is True
        admin.post(f"/api/games/{gid}/start")
        port = wait_state(admin, gid, "running")["port"]
        with APClient(port, "Watched", "ChecksFinder", password="hunter22") as client:
            assert client.connected
            client.ws.send(json.dumps([{"cmd": "Say", "text": "my secret plan"}]))
            client.check(2)
            # who is online comes from the server's save, which it writes once a minute, or at once when asked
            save_now(admin, gid)
            assert anyone.get(f"/api/watch/{link['token']}").json()["players"][0]["status"] == "online"
        # once the feed has the player leaving, the server knows, and its next save says so
        feed = wait_for(lambda: (f := anyone.get(f"/api/watch/{link['token']}/activity").json())["entries"]
                        and {"item", "part"} <= {e["kind"] for e in f["entries"]} and f, timeout=30)
        kinds = {e["kind"] for e in feed["entries"]}
        assert "item" in kinds and "join" in kinds and "chat" not in kinds
        assert "my secret plan" not in json.dumps(feed)
        assert "my secret plan" in json.dumps(admin.get(f"/api/games/{gid}/activity").json())  # the players see it
        save_now(admin, gid)
        assert anyone.get(f"/api/watch/{link['token']}").json()["players"][0]["status"] == "offline"

        # a new link ends the old one, and the link can be taken away
        fresh = admin.post(f"/api/games/{gid}/spectate").json()["spectate"]["token"]
        assert fresh != link["token"]
        assert anyone.get(f"/api/watch/{link['token']}").status_code == 404
        assert anyone.get(f"/api/watch/{fresh}").status_code == 200
        assert admin.delete(f"/api/games/{gid}/spectate").json()["spectate"] is None
        assert anyone.get(f"/api/watch/{fresh}").status_code == 404
        actions = [e["action"] for e in admin.get("/api/audit?action=game").json()["entries"]]
        assert "game.spectate" in actions and "game.spectate_end" in actions
    finally:
        admin.post(f"/api/games/{gid}/stop")
        admin.delete(f"/api/games/{gid}")
