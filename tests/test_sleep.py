"""Games that fall asleep: with nobody connected for the time set for a game, the manager stops its server; a player
opening the game page wakes it up. Asleep and stopped games lend their port to a game that needs one when none is
free, so there can be more games than ports. A minute lasts two seconds here (SLEEP_MINUTE_SECONDS)."""
import time

import pytest

from helpers import APClient, make_game, wait_state

pytestmark = pytest.mark.ap

MINUTE = 2


@pytest.fixture(scope="module")
def sleepy(tmp_path_factory):
    """A manager of its own: a short minute, and two ports for all its games."""
    from conftest import Server, free_port_block
    first = free_port_block(2)
    server = Server(tmp_path_factory.mktemp("sleepy") / "data", ap=True,
                    extra_env={"SLEEP_MINUTE_SECONDS": str(MINUTE), "PORT_RANGE": f"{first}-{first + 1}"})
    server.start()
    assert server.client().post("/api/auth/setup", json={"username": "admin", "password": "adminpass1"}
                                ).status_code == 200
    server.ports = (first, first + 1)
    yield server
    server.stop()


def user(server, admin, name):
    assert admin.post("/api/users", json={"username": name, "password": "password123", "role": "user"}
                      ).status_code == 200
    return server.client(name, "password123")


def sleeping_after(admin, gid, minutes):
    game = admin.get(f"/api/games/{gid}").json()
    r = admin.patch(f"/api/games/{gid}", json={"options": {**game["options"], "auto_shutdown_minutes": minutes}})
    assert r.status_code == 200, r.text
    assert r.json()["sleep_minutes"] == minutes and not r.json()["restart_required"]  # read as it runs


def test_a_game_falls_asleep_when_nobody_plays_and_a_player_wakes_it_up(sleepy):
    admin = sleepy.client("admin", "adminpass1")
    ann, bob = user(sleepy, admin, "ann"), user(sleepy, admin, "bob")
    gid, _job = make_game(admin, names=("Sleepy", "Dozy"), owners={"Sleepy": ann})
    admin.patch(f"/api/games/{gid}", json={"visibility": "all"})
    sleeping_after(admin, gid, 4)
    # waking a game up is not starting it: players may not start games here
    assert admin.patch("/api/settings", json={"users_can_start_rooms": False}).status_code == 200
    assert admin.post(f"/api/games/{gid}/start").status_code == 200
    port = wait_state(admin, gid, "running")["port"]

    # a connected player keeps it awake, however long they take
    with APClient(port, "Sleepy", "ChecksFinder") as client:
        assert client.connected
        time.sleep(4 * MINUTE + 3)
        assert admin.get(f"/api/games/{gid}").json()["state"] == "running"
    left = time.time()
    game = wait_state(admin, gid, "sleeping", timeout=30)
    assert time.time() - left >= 4 * MINUTE - 1
    assert (game["port"], game["desired_state"]) == (port, "stopped")  # it keeps its port
    log = admin.get(f"/api/games/{gid}/log").json()["lines"]
    assert any("the server goes to sleep" in line["text"] for line in log)
    assert [g["state"] for g in admin.get("/api/games").json() if g["id"] == gid] == ["sleeping"]

    # only its players wake it up: bob only sees it
    assert ann.post(f"/api/games/{gid}/start").status_code == 403
    assert bob.post(f"/api/games/{gid}/wake").status_code == 403
    assert ann.post(f"/api/games/{gid}/wake").json()["state"] in ("starting", "running")
    assert wait_state(ann, gid, "running")["port"] == port
    woke = admin.get("/api/audit", params={"action": "game.wake"}).json()["entries"]
    assert [(e["username"], e["target"]["id"]) for e in woke] == [("ann", gid)]

    # a look at the page of a game that is awake keeps it awake a while longer, without a line in the log
    sleeping_after(admin, gid, 6)
    running_at = time.time()
    time.sleep(5)
    assert ann.post(f"/api/games/{gid}/wake").json()["state"] == "running"
    time.sleep(running_at + 6 * MINUTE + 3 - time.time())  # longer than it would have stayed awake without the look
    assert admin.get(f"/api/games/{gid}").json()["state"] == "running"
    assert len(admin.get("/api/audit", params={"action": "game.wake"}).json()["entries"]) == 1
    wait_state(admin, gid, "sleeping", timeout=30)

    # stopped by an administrator, it stays stopped
    assert admin.post(f"/api/games/{gid}/stop").json()["state"] == "stopped"
    assert ann.post(f"/api/games/{gid}/wake").json()["state"] == "stopped"

    # and a restart of the manager does not wake an asleep game
    admin.patch("/api/settings", json={"users_can_start_rooms": True})
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    wait_state(admin, gid, "sleeping", timeout=30)
    sleepy.stop()
    sleepy.start()
    admin = sleepy.client("admin", "adminpass1")
    assert admin.get(f"/api/games/{gid}").json()["state"] == "sleeping"


def test_more_games_than_ports(sleepy):
    admin = sleepy.client("admin", "adminpass1")
    for game in admin.get("/api/games").json():  # every port free to begin with
        assert admin.delete(f"/api/games/{game['id']}").status_code == 200
    cat = user(sleepy, admin, "cat")
    first, second = sleepy.ports
    napper, _job = make_game(admin, names=("Napper", "Nod"), owners={"Napper": cat})
    awake, _job = make_game(admin, names=("Owl", "Bat"))
    third, _job = make_game(admin, names=("Third", "Wheel"))
    sleeping_after(admin, napper, 2)
    assert admin.get(f"/api/games/{awake}").json()["sleep_minutes"] == 0  # never falls asleep

    admin.post(f"/api/games/{napper}/start")
    admin.post(f"/api/games/{awake}/start")
    assert [wait_state(admin, g, "running")["port"] for g in (napper, awake)] == [first, second]
    wait_state(admin, napper, "sleeping", timeout=30)

    # the third game gets the port of the one asleep, which gets a new one when it wakes up
    assert admin.post(f"/api/games/{third}/start").status_code == 200
    assert wait_state(admin, third, "running")["port"] == first
    game = admin.get(f"/api/games/{napper}").json()
    assert (game["state"], game["port"], game["address"]) == ("sleeping", None, None)
    assert any("went to another game" in line["text"] for line in admin.get(f"/api/games/{napper}/log").json()["lines"])

    # with every other server running, there is no port to wake up with
    r = cat.post(f"/api/games/{napper}/wake")
    assert r.status_code == 409 and "No free port left" in r.json()["detail"], r.text
    assert admin.get(f"/api/games/{napper}").json()["state"] == "sleeping"

    # a stopped game lends its port
    admin.post(f"/api/games/{third}/stop")
    assert cat.post(f"/api/games/{napper}/wake").status_code == 200
    assert wait_state(admin, napper, "running")["port"] == first
    assert admin.get(f"/api/games/{third}").json()["port"] is None

    # a stopped game lends its port before one that is asleep: the asleep one is woken up by its players
    admin.post(f"/api/games/{awake}/stop")
    wait_state(admin, napper, "sleeping", timeout=30)
    assert admin.post(f"/api/games/{third}/start").status_code == 200
    assert wait_state(admin, third, "running")["port"] == second
    assert admin.get(f"/api/games/{napper}").json()["port"] == first
    admin.post(f"/api/games/{third}/stop")
