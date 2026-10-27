"""What players who are not organizers may have of the games they generate, and the archive of games that are over."""
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from helpers import APClient, make_game, player_yaml, save_now, upload_yaml, wait_for, wait_state

pytestmark = pytest.mark.ap


@pytest.fixture
def generating(admin):
    """Players may generate games, and start the servers of the games they are in."""
    before = admin.get("/api/settings").json()
    admin.patch("/api/settings", json={"users_can_generate": True, "users_can_start_rooms": True})
    yield
    admin.patch("/api/settings", json={"users_can_generate": before["users_can_generate"],
                                       "users_can_start_rooms": before["users_can_start_rooms"]})


def config(client, *names):
    """One file with a world for each name."""
    r = upload_yaml(client, f"{names[0]}-{time.time_ns()}", "\n---\n".join(player_yaml(n) for n in names))
    assert r.status_code == 200, r.text
    return r.json()["created"][0]["id"]


def queue(client, config_id, **options):
    return client.post("/api/jobs", json={"config_ids": [config_id], "host_after": False, **options})


def done(client, job_id):
    job = wait_for(lambda: (j := client.get(f"/api/jobs/{job_id}").json())["status"] in ("succeeded", "failed") and j,
                   timeout=240, message=f"job {job_id}")
    assert job["status"] == "succeeded", job
    return job


def limits(client):
    return client.get("/api/auth/status").json()["limits"]


def test_a_player_generates_at_most_15_worlds(admin, make_user, generating):
    _name, ann = make_user("ann")
    r = queue(ann, config(ann, *[f"Big{i}" for i in range(16)]))
    assert r.status_code == 409 and "at most 15 worlds" in r.json()["detail"] and "hold 16" in r.json()["detail"]
    assert ann.get("/api/jobs").json() == []
    # administrators have no such limit
    r = queue(admin, config(admin, *[f"Huge{i}" for i in range(16)]))
    assert r.status_code == 200, r.text
    assert admin.post(f"/api/jobs/{r.json()['id']}/cancel").status_code == 200
    assert limits(admin) is None


def test_three_unfinished_games_and_one_running(admin, make_user, generating):
    ann_name, ann = make_user("ann")
    _bea_name, bea = make_user("bea")
    _out_name, outsider = make_user("out")
    assert limits(ann) == {"worlds": 15, "unfinished": 3, "running": 1, "unfinished_now": 0}

    # generations under way count: the fourth is refused before any of the three is done
    jobs = [queue(ann, config(ann, f"Ann{i}", f"Pal{i}"), name=f"Ann's game {i}") for i in range(3)]
    assert [r.status_code for r in jobs] == [200] * 3
    r = queue(ann, config(ann, "Ann3"))
    assert r.status_code == 409 and r.json()["detail"].startswith("You already have 3 unfinished games")
    assert limits(ann)["unfinished_now"] == 3
    first, second, third = games = [done(ann, r.json()["id"])["game_id"] for r in jobs]
    assert limits(ann)["unfinished_now"] == 3

    bea_id = next(u["id"] for u in admin.get("/api/users").json() if u["username"] == _bea_name)
    assert admin.post(f"/api/games/{second}/slots/2/assign", json={"user_id": bea_id}).status_code == 200

    try:
        # two starts at the same moment: one waits for the other, and is refused
        with ThreadPoolExecutor(2) as pool:
            codes = list(pool.map(lambda game: ann.post(f"/api/games/{game}/start").status_code, (first, second)))
        assert sorted(codes) == [200, 409]
        for game in (first, second):
            ann.post(f"/api/games/{game}/stop")
            wait_state(ann, game, "stopped")

        # one game of hers runs at a time, whoever starts it
        assert ann.post(f"/api/games/{first}/start").status_code == 200
        wait_state(ann, first, "running")
        r = ann.post(f"/api/games/{second}/start")
        assert r.status_code == 409 and r.json()["detail"] == (
            "You can have one of your games running at a time, and Ann's game 0 is running (or asleep until its "
            "players come back). Stop it first.")
        r = bea.post(f"/api/games/{second}/restart")
        assert r.status_code == 409 and r.json()["detail"].startswith(f"Only one game of {ann_name} can run at a time")
        assert ann.get(f"/api/games/{second}").json()["state"] == "stopped"
        # the one that runs can restart, and administrators can start any
        assert ann.post(f"/api/games/{first}/restart").status_code == 200
        assert admin.post(f"/api/games/{second}/start").status_code == 200
        wait_state(admin, second, "running")
        admin.post(f"/api/games/{second}/stop")
        # once it is stopped, another can run
        ann.post(f"/api/games/{first}/stop")
        wait_state(ann, first, "stopped")
        assert bea.post(f"/api/games/{second}/start").status_code == 200
        wait_state(bea, second, "running")

        # the player who generated a game moves it to the archive; others in it cannot, outsiders do not see it
        assert bea.post(f"/api/games/{third}/archive", json={"archived": True}).status_code == 404
        assert bea.post(f"/api/games/{second}/archive", json={"archived": True}).status_code == 403
        r = ann.post(f"/api/games/{third}/archive", json={"archived": True})
        assert r.status_code == 200 and r.json()["finished_at"] and r.json()["outcome"] == "aborted"
        assert outsider.post(f"/api/games/{third}/archive", json={"archived": False}).status_code == 404
        listed = {g["id"]: g for g in ann.get("/api/games").json()}
        assert listed[third]["outcome"] == "aborted" and listed[first]["outcome"] is None
        assert listed[third]["can_archive"] and not bea.get(f"/api/games/{second}").json()["can_archive"]
        assert limits(ann)["unfinished_now"] == 2

        # that frees a place; the new game's server waits while another of hers runs
        r = queue(ann, config(ann, "Ann4"), host_after=True)
        assert r.status_code == 200, r.text
        job = done(ann, r.json()["id"])
        games.append(job["game_id"])
        log = ann.get(f"/api/jobs/{job['id']}/log").json()["lines"]
        assert any(line["text"].startswith("The server was not started: You can have one of your games running")
                   for line in log), log
        assert ann.get(f"/api/games/{job['game_id']}").json()["state"] == "stopped"

        # taking a game back out of the archive needs a free place too, except for administrators
        r = ann.post(f"/api/games/{third}/archive", json={"archived": False})
        assert r.status_code == 409 and r.json()["detail"].startswith("There are 3 unfinished games of this player")
        r = admin.post(f"/api/games/{third}/archive", json={"archived": False})
        assert r.status_code == 200 and r.json()["finished_at"] is None and r.json()["outcome"] is None
        assert ann.post(f"/api/games/{third}/archive", json={"archived": True}).status_code == 200
        entries = admin.get("/api/audit", params={"action": "game.archive"}).json()["entries"]
        assert [e["username"] for e in entries[:3]] == [ann_name, "admin", ann_name]
    finally:  # each game holds a port of the test server's few
        for game in games:
            admin.delete(f"/api/games/{game}")


def test_a_game_everyone_finished_is_listed_as_finished(admin):
    gid, _job = make_game(admin, names=("Goalie",))
    admin.post(f"/api/games/{gid}/start")
    port = wait_state(admin, gid, "running")["port"]
    with APClient(port, "Goalie", "ChecksFinder") as client:
        assert client.connected
        client.ws.send(json.dumps([{"cmd": "StatusUpdate", "status": 30}]))
        time.sleep(1)
    save_now(admin, gid)
    game = wait_for(lambda: (g := admin.get(f"/api/games/{gid}").json())["finished_at"] and g, timeout=60,
                    message="the game to finish")
    assert game["outcome"] == "finished"
    admin.post(f"/api/games/{gid}/stop")
    admin.delete(f"/api/games/{gid}")
