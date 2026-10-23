"""Progress over time and the archive of finished games, read from the server's save."""
import json
import sqlite3
import time

import pytest

from helpers import APClient, make_game, save_now, wait_for, wait_state

pytestmark = pytest.mark.ap


def history(client, gid):
    r = client.get(f"/api/games/{gid}/history")
    assert r.status_code == 200, r.text
    return r.json()


def last_checked(h, name):
    points = next(s for s in h["slots"] if s["name"] == name)["points"]
    return points[-1][1] if points else None


def reach_goal(port, name, game):
    with APClient(port, name, game) as client:
        assert client.connected
        client.ws.send(json.dumps([{"cmd": "StatusUpdate", "status": 30}]))
        time.sleep(1)


def test_progress_is_recorded_and_finished_games_go_to_the_archive(admin, make_user, discord):
    _ann_name, ann = make_user("ann")
    _other_name, other = make_user("other")
    gid, _job = make_game(admin, names=("Hist", "Ory"), owners={"Hist": ann})
    assert other.get(f"/api/games/{gid}/history").status_code == 404  # like the game itself
    h = history(ann, gid)
    assert h["recorded_from"] is None and h["finished_at"] is None
    assert {s["name"]: s["points"] for s in h["slots"]} == {"Hist": [], "Ory": []}
    assert next(s for s in h["slots"] if s["name"] == "Hist")["total"] == 25
    r = admin.patch(f"/api/games/{gid}", json={"share_spoiler_when_done": True})
    assert r.status_code == 200 and r.json()["share_spoiler_when_done"] is True
    assert not any(f["kind"] == "spoiler" for f in ann.get(f"/api/games/{gid}").json()["shared_files"])

    # the first look at a running game is where the chart starts
    admin.post(f"/api/games/{gid}/start")
    port = wait_state(admin, gid, "running")["port"]
    h = wait_for(lambda: (x := history(ann, gid))["recorded_from"] and x, timeout=30, message="the first look")
    assert last_checked(h, "Hist") == 0 and last_checked(h, "Ory") == 0 and h["play_started_at"] is None

    # checks are points in time; the first one starts the game
    before = time.time()
    with APClient(port, "Hist", "ChecksFinder") as client:
        client.check(3)
    save_now(admin, gid)
    h = wait_for(lambda: (x := history(ann, gid)) and last_checked(x, "Hist") == 3 and x, timeout=60,
                 message="the checks in the history")
    assert before - 1 <= h["play_started_at"] <= time.time()
    hist = next(s for s in h["slots"] if s["name"] == "Hist")
    assert hist["points"][-1][0] == h["play_started_at"]  # taken from the save's own time of the last check
    assert last_checked(h, "Ory") == 0 and hist["goal_at"] is None

    # everyone reaching their goal finishes the game
    reach_goal(port, "Hist", "ChecksFinder")
    save_now(admin, gid)
    wait_for(lambda: next(s for s in history(ann, gid)["slots"] if s["name"] == "Hist")["goal_at"], timeout=60,
             message="the first goal")
    assert admin.get(f"/api/games/{gid}").json()["finished_at"] is None
    reach_goal(port, "Ory", "APQuest")
    save_now(admin, gid)
    game = wait_for(lambda: (g := admin.get(f"/api/games/{gid}").json())["finished_at"] and g, timeout=60,
                    message="the game to finish")
    assert all(s["goal_at"] for s in game["slots"] if s["type"] == 1)
    listed = next(g for g in admin.get("/api/games").json() if g["id"] == gid)
    assert listed["finished_at"] == game["finished_at"] and listed["goal_count"] == 2
    done = discord.wait_for("Everyone finished")
    assert any(f["name"] == "Time" and f["value"].endswith("min") for f in done["fields"])
    # the spoiler log is now the players' too
    assert any(f["kind"] == "spoiler" for f in ann.get(f"/api/games/{gid}").json()["shared_files"])

    # an admin can take a game out of the archive and put it back; players cannot
    assert ann.patch(f"/api/games/{gid}", json={"finished": False}).status_code == 403
    assert admin.patch(f"/api/games/{gid}", json={"finished": False}).json()["finished_at"] is None
    assert admin.patch(f"/api/games/{gid}", json={"finished": True}).json()["finished_at"] is not None

    # resetting the progress starts the history over
    admin.post(f"/api/games/{gid}/stop")
    r = admin.post(f"/api/games/{gid}/reset-save")
    assert r.status_code == 200 and r.json()["finished_at"] is None
    h = history(admin, gid)
    assert h["recorded_from"] is None and all(not s["points"] and s["goal_at"] is None for s in h["slots"])
    admin.delete(f"/api/games/{gid}")


def test_a_game_played_before_the_history_is_archived_at_the_next_start(tmp_path):
    """After the update, games that already have a save get a first point where they stand, and finished ones go to
    the archive, without anyone having to start them."""
    from conftest import Server
    from helpers import upload_yaml, player_yaml, run_job
    server = Server(tmp_path / "data", ap=True)
    server.start()
    try:
        server.client().post("/api/auth/setup", json={"username": "admin", "password": "adminpass1"})
        admin = server.client("admin", "adminpass1")
        ids = [upload_yaml(admin, name, player_yaml(name)).json()["created"][0]["id"] for name in ("Old", "Timer")]
        gid = run_job(admin, ids)["game_id"]
        admin.post(f"/api/games/{gid}/start")
        port = wait_state(admin, gid, "running")["port"]
        with APClient(port, "Old", "ChecksFinder") as client:
            client.check(2)
        reach_goal(port, "Old", "ChecksFinder")
        reach_goal(port, "Timer", "ChecksFinder")
        admin.post(f"/api/games/{gid}/stop")
        wait_state(admin, gid, "stopped")
    finally:
        server.stop()
    # what a database from before the history looks like
    conn = sqlite3.connect(tmp_path / "data" / "manager.sqlite3")
    conn.execute("DELETE FROM game_progress")
    conn.execute("UPDATE game_slots SET goal_at = NULL")
    conn.execute("UPDATE games SET play_started_at = NULL, finished_at = NULL")
    conn.commit()
    conn.close()

    server.start()
    try:
        admin = server.client("admin", "adminpass1")
        game = wait_for(lambda: (g := admin.get(f"/api/games/{gid}").json())["finished_at"] and g, timeout=60,
                        message="the catch-up")
        assert game["state"] == "stopped"
        h = history(admin, gid)
        # where things stood when recording began; when play began is not known
        assert h["play_started_at"] is None and h["recorded_from"] is not None
        # one point each, where they stood (reaching the goal released the rest of their locations: release "auto")
        assert {s["name"]: s["points"] for s in h["slots"]} == {s["name"]: [[h["recorded_from"], s["checked"]]]
                                                               for s in game["slots"]}
        assert all(s["checked"] == 25 for s in game["slots"])
        assert all(s["goal_at"] == h["recorded_from"] for s in h["slots"])
    finally:
        server.stop()
